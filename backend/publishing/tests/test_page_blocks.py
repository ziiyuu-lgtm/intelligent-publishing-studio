import json
import os
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (JournalAIPolicy, JournalSiteAIDesignRun, JournalSiteRelease,
    OrganisationAIPolicy, OrganisationPortalAIDesignRun, OrganisationPortalRelease)
from publishing.page_blocks import validate_blocks, validate_link
from . import test_site_pages as page_helpers
from . import test_page_ai as ai_helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PageBlockTests(TestCase):
    setUpTestData = page_helpers.SitePageTests.__dict__["setUpTestData"]
    owner = page_helpers.SitePageTests.owner
    route = page_helpers.SitePageTests.route
    login = page_helpers.SitePageTests.login
    token = page_helpers.SitePageTests.token
    upload = page_helpers.SitePageTests.upload
    publish = page_helpers.SitePageTests.publish
    add_image = page_helpers.SitePageTests.add_image
    page_url = page_helpers.SitePageTests.page_url
    pages_token = page_helpers.SitePageTests.pages_token
    payload = page_helpers.SitePageTests.payload
    save_pages = page_helpers.SitePageTests.save_pages
    page = staticmethod(page_helpers.SitePageTests.page)
    ai_url = ai_helpers.IndependentPageAITests.ai_url

    def block_url(self, kind, action, slug="author-guidelines"):
        route = f"{action}_{'journal' if kind == 'journal' else 'publisher'}_page_blocks"
        return reverse(route, args=[self.owner(kind)[1], slug])

    @staticmethod
    def block(heading="Featured research", asset=None, visible=True, kind="image", url=""):
        result = {"kind":kind, "heading":heading, "body":"Fictional <script> text", "visible":visible,
                  "link_label":"Read more" if url else "", "link_url":url}
        if asset:
            result.update(image_asset_id=str(asset.pk), image_alt="Fictional image", image_fit="contain", image_position="top")
        return result

    def block_payload(self, kind, entries, slug="author-guidelines"):
        self.login(kind)
        response = self.client.get(self.block_url(kind, "manage", slug))
        self.assertEqual(response.status_code, 200)
        data = {"revision":str(response.context["site"].revision), "fingerprint":response.context["fingerprint"],
                "sections-TOTAL_FORMS":str(len(entries)), "sections-INITIAL_FORMS":str(len(response.context["page"].get("blocks", []))),
                "sections-MIN_NUM_FORMS":"0", "sections-MAX_NUM_FORMS":"20", "layout-form_present":"yes"}
        layout = response.context["page"].get("block_layout")
        if layout:
            data.update({"layout-enabled":"on", "layout-gap":str(layout["gap"]), "layout-alignment":layout["alignment"]})
        for i, entry in enumerate(entries):
            data[f"sections-{i}-design_form_present"] = "yes"
            data[f"sections-{i}-grid_form_present"] = "yes"
            if "grid_span" in entry:
                data[f"sections-{i}-grid_span"] = str(entry["grid_span"])
            for key in ("kind", "heading", "body", "image_asset_id", "image_alt", "image_fit", "image_position", "link_label", "link_url"):
                data[f"sections-{i}-{key}"] = str(entry.get(key, ""))
            if entry.get("design"):
                data[f"sections-{i}-design_enabled"] = "on"
                for key, value in entry["design"].items():
                    data[f"sections-{i}-design_{key}"] = str(value)
            data[f"sections-{i}-ORDER"] = str(entry.get("ORDER", i + 1))
            if entry.get("visible"):
                data[f"sections-{i}-visible"] = "on"
            if entry.get("DELETE"):
                data[f"sections-{i}-DELETE"] = "on"
        return data

    def save_blocks(self, kind, entries, slug="author-guidelines"):
        return self.client.post(self.block_url(kind, "save", slug), self.block_payload(kind, entries, slug))

    def test_blocks_publish_visibility_reference_protection_and_restore(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                first, second = self.add_image(kind, "Block one"), self.add_image(kind, "Block two")
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                blocks = [self.block("Visible block", first, True, url="https://example.org/help"),
                          self.block("Hidden block", second, False)]
                blocks[0]["ORDER"], blocks[1]["ORDER"] = 2, 1
                self.assertEqual(self.save_blocks(kind, blocks).status_code, 302)
                preview = self.client.get(self.page_url(kind, "preview", "author-guidelines"))
                self.assertContains(preview, "Visible block")
                self.assertNotContains(preview, "Hidden block")
                self.assertContains(preview, self.route(kind, "private", first.pk))
                self.assertContains(preview, "Fictional &lt;script&gt; text")
                self.assertContains(preview, 'href="https://example.org/help"')
                public = Client()
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]
                release_model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                release = release_model.objects.get(site=site)
                self.assertContains(public.get(self.page_url(kind, "public", "author-guidelines")), "Visible block")
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)
                self.assertEqual(self.client.post(self.route(kind, "delete"), {"asset_id":str(second.pk), "confirm":"yes"}).status_code, 409)
                page_entry = self.page(slug="new-guide", title="Changed page title")
                self.assertEqual(self.save_pages(kind, [page_entry]).status_code, 302)
                site.refresh_from_db()
                self.assertEqual([b["heading"] for b in site.pages_config[0]["blocks"]], ["Hidden block", "Visible block"])
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 200)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertContains(public.get(self.page_url(kind, "public", "new-guide")), "Visible block")
                self.assertEqual(self.client.post(self.route(kind, "restore", release.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertContains(self.client.get(self.page_url(kind, "preview", "author-guidelines")), "Visible block")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(self.page_url(kind, "public", "author-guidelines")), "Visible block")
                self.assertEqual(self.save_pages(kind, [self.page(visible=False)]).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertEqual(self.client.post(self.route(kind, "delete"), {"asset_id":str(first.pk), "confirm":"yes"}).status_code, 409)

    def test_validation_role_csrf_stale_and_form_truncation(self):
        journal_asset = self.add_image("journal", "Journal block image")
        portal_asset = self.add_image("portal", "Portal block image")
        for kind, asset, foreign in (("journal", journal_asset, portal_asset), ("portal", portal_asset, journal_asset)):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                for link in ("javascript:alert(1)", "//example.org/path", "http://example.org/", "https://user@example.org/", "https://good.example\\@evil.example/"):
                    with self.subTest(link=link):
                        self.assertEqual(self.save_blocks(kind, [self.block(asset=asset, url=link)]).status_code, 400)
                invalid = self.block(asset=foreign)
                self.assertEqual(self.save_blocks(kind, [invalid]).status_code, 400)
                invalid = self.block(asset=asset); invalid["image_alt"] = " "
                self.assertEqual(self.save_blocks(kind, [invalid]).status_code, 400)
                invalid = self.block(kind="link"); invalid["link_label"] = "Incomplete"
                self.assertEqual(self.save_blocks(kind, [invalid]).status_code, 400)
                data = self.block_payload(kind, [self.block(asset=asset, url="/local/help")])
                self.assertEqual(self.client.post(self.block_url(kind, "save"), {**data, "fingerprint":"stale"}).status_code, 409)
                self.assertEqual(self.client.post(self.block_url(kind, "save"), data).status_code, 302)
                data = self.block_payload(kind, [self.block(asset=asset)])
                self.assertEqual(self.client.post(self.block_url(kind, "save"), {**data, "sections-TOTAL_FORMS":"0"}).status_code, 409)
                pages_data = self.payload(kind, [self.page()])
                self.assertEqual(self.client.post(self.page_url(kind, "save"), {**pages_data, "pages-TOTAL_FORMS":"0"}).status_code, 409)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(len(site.pages_config[0]["blocks"]), 1)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(self.block_url(kind, "manage")).status_code, 404)
                csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(self.block_url(kind, "save"), data).status_code, 403)
                self.assertEqual(self.client.get(self.block_url(kind, "save")).status_code, 405)

    def test_schema_and_link_validation(self):
        for url in ("https://example.org/a?x=1", "/journals/test/"):
            self.assertEqual(validate_link(url), url)
        for url in ("//example.org", "data:text/html,x", "https://example.org/\nother", "https://example.org\\evil"):
            with self.assertRaises(ValueError):
                validate_link(url)
        for blocks in (None, {}, [self.block()] * 21, [{**self.block(), "kind":"<script>"}],
                       [{**self.block(), "visible":"yes"}], [{**self.block(), "extra":"bad"}]):
            with self.assertRaises(ValueError):
                validate_blocks(blocks)

    def test_deleted_block_and_corrupt_release_fail_closed(self):
        journal_asset = self.add_image("journal", "Journal image")
        portal_asset = self.add_image("portal", "Publisher image")
        for kind, asset, foreign in (("journal", journal_asset, portal_asset), ("portal", portal_asset, journal_asset)):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                block = self.block("Remove later", asset)
                self.assertEqual(self.save_blocks(kind, [block]).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                block["DELETE"] = True
                self.assertEqual(self.save_blocks(kind, [block]).status_code, 302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertNotIn("blocks", site.pages_config[0])
                public = Client()
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 200)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 404)
                self.assertEqual(self.client.post(self.route(kind, "delete"), {"asset_id":str(asset.pk), "confirm":"yes"}).status_code, 409)
                block.pop("DELETE")
                self.assertEqual(self.save_blocks(kind, [block]).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                site.refresh_from_db()
                release_model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                release = release_model.objects.get(pk=site.current_release_id)
                tampered = release.pages_config
                tampered[0]["blocks"][0]["image_asset_id"] = str(foreign.pk)
                release_model.objects.filter(pk=release.pk).update(pages_config=tampered)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 404)

    def test_chinese_block_controls(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
                response = self.client.get(self.block_url(kind, "manage"))
                self.assertContains(response, "页面内容区块")
                self.assertContains(response, "区块图片描述")
                self.assertContains(response, "链接地址")
                self.assertContains(response, "区块视觉设计")
                self.assertContains(response, "选择背景颜色")
                self.assertContains(response, "页面区块布局")
                self.assertContains(response, "三分之二")

    @override_settings(IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL="test-model")
    @patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
    @patch("publishing.ai_provider._send_request", return_value=ai_helpers.provider_response())
    def test_page_ai_preserves_blocks_without_sending_content(self, transport):
        JournalAIPolicy.objects.create(journal=self.journal, site_pages_enabled=True, journal_daily_limit=10)
        OrganisationAIPolicy.objects.create(organisation=self.org, portal_pages_enabled=True, portal_daily_limit=10)
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "PRIVATE BLOCK IMAGE")
                self.assertEqual(self.save_pages(kind, [self.page(slug="private-page-name")]).status_code, 302)
                block = self.block("PRIVATE BLOCK HEADING", asset=asset, url="https://example.org/private-path")
                block["body"] = "PRIVATE BLOCK BODY"
                self.assertEqual(self.save_blocks(kind, [block], "private-page-name").status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                data = ai_helpers.IndependentPageAITests.ai_data(self, kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                run_model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = run_model.objects.get(pk=data["request_id"])
                sent = transport.call_args.args[0].data.decode()
                self.assertFalse(json.loads(json.loads(sent)["input"])["has_image"])
                for secret in ("PRIVATE BLOCK", "private-path", str(asset.pk)):
                    self.assertNotIn(secret, sent)
                self.assertContains(self.client.get(self.ai_url(kind, "preview", run.pk, 0)), "PRIVATE BLOCK HEADING")
                site = self.owner(kind)[2]; site.refresh_from_db()
                before = site.pages_config[0]["blocks"]
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run.pk, 0),
                    {"revision":str(site.revision), "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["blocks"], before)
                self.assertEqual(Client().get(self.route(kind, "public_image", asset.pk)).status_code, 200)
