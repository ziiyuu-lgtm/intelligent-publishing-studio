import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing import portal_ai_modules, site_ai_modules
from publishing.models import (JournalAIPolicy, JournalSiteAIDesignRun, JournalSiteRelease, OrganisationAIPolicy,
                               OrganisationPortalAIDesignRun, OrganisationPortalRelease, SiteImageAsset)
from publishing.site_pages import InvalidPages, validate_pages
from publishing.site_sections import default_sections as journal_default_sections
from publishing.portal_sections import default_sections as portal_default_sections
from . import test_module_images as module_helpers
from . import test_site_images as image_helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SitePageTests(TestCase):
    setUpTestData = image_helpers.SiteImageTests.__dict__["setUpTestData"]
    owner = image_helpers.SiteImageTests.owner
    route = image_helpers.SiteImageTests.route
    login = image_helpers.SiteImageTests.login
    token = image_helpers.SiteImageTests.token
    upload = image_helpers.SiteImageTests.upload
    publish = image_helpers.SiteImageTests.publish
    add_image = module_helpers.ModuleImageTests.add_image
    save_modules = module_helpers.ModuleImageTests.save_modules
    modules = staticmethod(module_helpers.ModuleImageTests.modules)

    def page_url(self, kind, action, page_slug=None):
        names = {("journal", "manage"):"manage_journal_pages", ("journal", "save"):"save_journal_pages",
                 ("journal", "preview"):"preview_journal_page", ("journal", "public"):"public_journal_page",
                 ("portal", "manage"):"manage_publisher_pages", ("portal", "save"):"save_publisher_pages",
                 ("portal", "preview"):"preview_publisher_page", ("portal", "public"):"public_publisher_page"}
        args = [self.owner(kind)[1]] + ([page_slug] if page_slug is not None else [])
        return reverse(names[(kind, action)], args=args)

    def pages_token(self, kind):
        self.login(kind)
        page = self.client.get(self.page_url(kind, "manage"))
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"]}

    def payload(self, kind, entries):
        site = self.owner(kind)[2]
        site.refresh_from_db()
        result = {**self.pages_token(kind), "pages-TOTAL_FORMS":str(len(entries)),
                  "pages-INITIAL_FORMS":str(len(site.pages_config)), "pages-MIN_NUM_FORMS":"0", "pages-MAX_NUM_FORMS":"20"}
        for index, entry in enumerate(entries):
            for key in ("slug", "title", "body", "image_asset_id", "image_alt", "image_fit", "image_position"):
                result[f"pages-{index}-{key}"] = str(entry.get(key, ""))
            result[f"pages-{index}-ORDER"] = str(entry.get("ORDER", index+1))
            if entry.get("visible"):
                result[f"pages-{index}-visible"] = "on"
            if entry.get("DELETE"):
                result[f"pages-{index}-DELETE"] = "on"
        return result

    def save_pages(self, kind, entries):
        return self.client.post(self.page_url(kind, "save"), self.payload(kind, entries))

    @staticmethod
    def page(asset=None, slug="author-guidelines", visible=True, title="Author <guidelines>"):
        result = {"slug":slug, "title":title, "body":"First line\n<script>untrusted</script>", "visible":visible}
        if asset:
            result.update(image_asset_id=str(asset.pk), image_alt="Accessible image <description>", image_fit="contain", image_position="top")
        return result

    def test_pages_preview_navigation_visibility_versions_and_images(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                first, second = self.add_image(kind, "First page image"), self.add_image(kind, "Second page image")
                entries = [self.page(first), self.page(second, "editorial-board", False, "Editorial board")]
                self.assertEqual(self.save_pages(kind, entries).status_code, 302)
                preview = self.client.get(self.page_url(kind, "preview", "author-guidelines"))
                self.assertContains(preview, "Author &lt;guidelines&gt;")
                self.assertContains(preview, "&lt;script&gt;untrusted&lt;/script&gt;")
                self.assertNotContains(preview, "<script>untrusted</script>")
                self.assertContains(preview, self.route(kind, "private", first.pk))
                self.assertContains(self.client.get(self.page_url(kind, "preview", "editorial-board")), "Editorial board")
                self.assertEqual(Client().get(self.page_url(kind, "preview", "author-guidelines")).status_code, 302)
                public = Client()
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]
                model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                release = model.objects.get(site=site)
                homepage = public.get(self.route(kind, "public"))
                self.assertContains(homepage, self.page_url(kind, "public", "author-guidelines"))
                self.assertNotContains(homepage, self.page_url(kind, "public", "editorial-board"))
                self.assertContains(public.get(self.page_url(kind, "public", "author-guidelines")), 'object-fit:contain;object-position:top')
                self.assertEqual(public.get(self.page_url(kind, "public", "editorial-board")).status_code, 404)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)

                entries[0]["visible"], entries[1]["visible"] = False, True
                entries[1]["ORDER"] = 1
                entries[0]["ORDER"] = 2
                self.assertEqual(self.save_pages(kind, entries).status_code, 302)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 200)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(public.get(self.page_url(kind, "public", "editorial-board")).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 200)
                self.assertEqual(self.client.post(self.route(kind, "delete"),
                    {"asset_id":str(first.pk), "confirm":"yes"}).status_code, 409)
                self.assertEqual(self.client.post(self.route(kind, "restore", release.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertContains(self.client.get(self.page_url(kind, "preview", "author-guidelines")), "Author &lt;guidelines&gt;")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)

    def test_shared_homepage_and_page_image_stays_protected(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Shared image")
                self.assertEqual(self.save_modules(kind, self.modules(asset)).status_code, 302)
                self.assertEqual(self.save_pages(kind, [self.page(asset)]).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                entries = self.modules(asset)
                entries[1].pop("image_asset_id"); entries[1].pop("image_alt")
                entries[1].pop("image_fit"); entries[1].pop("image_position")
                self.assertEqual(self.save_modules(kind, entries).status_code, 302)
                self.assertContains(self.client.get(self.page_url(kind, "preview", "author-guidelines")), self.route(kind, "private", asset.pk))
                self.assertEqual(self.client.post(self.route(kind, "delete"), {"asset_id":str(asset.pk), "confirm":"yes"}).status_code, 409)
                removed = self.page(asset); removed["DELETE"] = True
                self.assertEqual(self.save_pages(kind, [removed]).status_code, 302)
                self.assertEqual(self.client.post(self.route(kind, "delete"), {"asset_id":str(asset.pk), "confirm":"yes"}).status_code, 409)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(Client().get(self.route(kind, "public_image", asset.pk)).status_code, 404)

    def test_validation_stale_roles_csrf_and_corrupt_snapshots(self):
        journal_image = self.add_image("journal", "Journal image")
        portal_image = self.add_image("portal", "Publisher image")
        for kind, asset, foreign in (("journal", journal_image, portal_image), ("portal", portal_image, journal_image)):
            with self.subTest(kind=kind):
                cases = [[self.page(asset, "INVALID SLUG")], [self.page(asset, "bad_url")],
                         [self.page(asset), self.page(asset)], [self.page(foreign)]]
                no_alt = self.page(asset); no_alt["image_alt"] = " "
                cases.append([no_alt])
                for entries in cases:
                    self.assertEqual(self.save_pages(kind, entries).status_code, 400)
                valid = self.payload(kind, [self.page(asset)])
                self.assertEqual(self.client.post(self.page_url(kind, "save"), {**valid, "fingerprint":"stale"}).status_code, 409)
                self.assertEqual(self.client.get(self.page_url(kind, "save")).status_code, 405)
                self.assertEqual(self.client.post(self.page_url(kind, "save"), valid).status_code, 302)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(self.page_url(kind, "manage")).status_code, 404)
                self.assertEqual(self.client.get(self.page_url(kind, "preview", "author-guidelines")).status_code, 404)
                csrf = Client(enforce_csrf_checks=True)
                csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(self.page_url(kind, "save"), valid).status_code, 403)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]
                site.refresh_from_db()
                release_model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                release_model.objects.filter(pk=site.current_release_id).update(pages_config=[self.page(foreign)])
                self.assertEqual(Client().get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertEqual(Client().get(self.route(kind, "public_image", asset.pk)).status_code, 404)
                self.assertEqual(Client().get(self.route(kind, "public" )).status_code, 404)

    def test_damaged_page_image_is_not_served_or_rendered(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Image to check")
                self.assertEqual(self.save_pages(kind, [self.page(asset)]).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                public = Client()
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 200)
                SiteImageAsset.objects.filter(pk=asset.pk).update(image_data=b"damaged")
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 404)
                self.assertEqual(public.get(self.page_url(kind, "public", "author-guidelines")).status_code, 404)
                self.assertEqual(self.client.get(self.page_url(kind, "preview", "author-guidelines")).status_code, 404)

    def test_stored_page_validation_and_chinese_controls(self):
        for bad in (None, {}, [self.page(slug="UPPER")], [self.page(slug="contains space")],
                    [{**self.page(), "visible":"yes"}], [{**self.page(), "unexpected":"field"}],
                    [self.page() for _ in range(21)]):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidPages):
                    validate_pages(bad)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        for kind in ("journal", "portal"):
            self.login(kind)
            page = self.client.get(self.page_url(kind, "manage"))
            self.assertContains(page, "独立页面")
            self.assertContains(page, "页面标题")
            self.assertContains(page, "页面图片描述")

    @override_settings(IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL="test-model")
    @patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
    @patch("publishing.ai_provider._send_request")
    def test_ai_arrangement_preserves_pages_without_sending_page_details(self, transport):
        JournalAIPolicy.objects.create(journal=self.journal, site_modules_enabled=True)
        OrganisationAIPolicy.objects.create(organisation=self.org, portal_modules_enabled=True)
        for kind, module in (("journal", site_ai_modules), ("portal", portal_ai_modules)):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Sensitive page image")
                self.assertEqual(self.save_pages(kind, [self.page(asset, "private-page-name", True, "Private page title")]).status_code, 302)
                site = self.owner(kind)[2]
                site.refresh_from_db()
                source = (journal_default_sections if kind == "journal" else portal_default_sections)(site)
                descriptors = module.describe_sections(source)
                option = {"modules":list(reversed(descriptors))}
                transport.return_value = {"status":"completed", "output":[{"type":"message", "role":"assistant",
                    "content":[{"type":"output_text", "text":json.dumps({"options":[option]})}]}]}
                page = self.client.get(self.route(kind, "manage"))
                data = {**self.token(kind), "request_id":str(uuid.uuid4()), "policy_revision":str(page.context["ai_policy_revision"]),
                        "model":"test-model", "brief":"Arrange a fictional site", "confirm":"yes", "synthetic":"yes"}
                name = "request_site_ai_modules" if kind == "journal" else "request_publisher_ai_modules"
                self.assertEqual(self.client.post(reverse(name, args=[self.owner(kind)[1]]), data).status_code, 302)
                sent = transport.call_args.args[0].data.decode()
                for text in ("Private page title", "private-page-name", "First line", str(asset.pk)):
                    self.assertNotIn(text, sent)
                run_model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = run_model.objects.latest("created_at")
                preview_name = "preview_site_ai_modules" if kind == "journal" else "preview_publisher_ai_modules"
                self.assertContains(self.client.get(reverse(preview_name, args=[self.owner(kind)[1], run.pk, 0])), "Private page title")
                apply_name = "apply_site_ai_modules" if kind == "journal" else "apply_publisher_ai_modules"
                self.assertEqual(self.client.post(reverse(apply_name, args=[self.owner(kind)[1], run.pk, 0]),
                    {"revision":str(site.revision), "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["image_asset_id"], str(asset.pk))
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(Client().get(self.page_url(kind, "public", "private-page-name")).status_code, 200)
