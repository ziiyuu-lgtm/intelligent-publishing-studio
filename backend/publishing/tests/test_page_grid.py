import json

from django.test import Client, TestCase, override_settings

from publishing.models import JournalSiteRelease, OrganisationPortalRelease
from publishing.page_block_layout import validate_block_layout, validate_grid_span
from publishing.site_pages import InvalidPages, validate_pages
from . import test_page_blocks as page_helpers
from . import test_page_block_ai as ai_helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PageGridTests(TestCase):
    setUpTestData = page_helpers.PageBlockTests.__dict__["setUpTestData"]
    owner = page_helpers.PageBlockTests.owner
    route = page_helpers.PageBlockTests.route
    login = page_helpers.PageBlockTests.login
    token = page_helpers.PageBlockTests.token
    upload = page_helpers.PageBlockTests.upload
    publish = page_helpers.PageBlockTests.publish
    add_image = page_helpers.PageBlockTests.add_image
    page_url = page_helpers.PageBlockTests.page_url
    pages_token = page_helpers.PageBlockTests.pages_token
    payload = page_helpers.PageBlockTests.payload
    save_pages = page_helpers.PageBlockTests.save_pages
    page = staticmethod(page_helpers.PageBlockTests.page)
    block = staticmethod(page_helpers.PageBlockTests.block)
    block_url = page_helpers.PageBlockTests.block_url
    block_payload = page_helpers.PageBlockTests.block_payload
    save_blocks = page_helpers.PageBlockTests.save_blocks

    def enable_grid(self, kind, blocks, *, gap=20, alignment="start"):
        data = self.block_payload(kind, blocks)
        data.update({"layout-enabled":"on", "layout-gap":str(gap), "layout-alignment":alignment})
        return self.client.post(self.block_url(kind, "save"), data)

    def test_validation_accepts_flexible_spans_and_rejects_unsafe_values(self):
        self.assertEqual(validate_block_layout({"gap":0, "alignment":"stretch"}), {"gap":0, "alignment":"stretch"})
        self.assertEqual([validate_grid_span(i) for i in range(1, 13)], list(range(1, 13)))
        for bad in (0, 13, True, "6", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_grid_span(bad)
        for bad in ({"gap":65, "alignment":"start"}, {"gap":True, "alignment":"start"},
                    {"gap":10, "alignment":"start;display:none"}, {"gap":10}, {"gap":10, "alignment":"start", "x":1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_block_layout(bad)
        with self.assertRaises(InvalidPages):
            validate_pages([{**self.page(), "block_layout":{"gap":1, "alignment":"x"}}])
        with self.assertRaises(InvalidPages):
            validate_pages([{**self.page(), "blocks":[{**self.block(), "grid_span":13}]}])

    def test_grid_preview_snapshot_rename_disable_and_restore(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Grid image")
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                entries = [{**self.block("Main", asset), "grid_span":8},
                           {**self.block("Side", kind="text"), "grid_span":4},
                           {**self.block("Full width", kind="text")}]
                self.assertEqual(self.enable_grid(kind, entries, gap=18, alignment="stretch").status_code, 302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["block_layout"], {"gap":18, "alignment":"stretch"})
                self.assertEqual([b.get("grid_span", 12) for b in site.pages_config[0]["blocks"]], [8, 4, 12])
                preview = self.client.get(self.page_url(kind, "preview", "author-guidelines"))
                self.assertContains(preview, "has-block-grid")
                self.assertContains(preview, "--block-grid-gap:18px;--block-grid-align:stretch")
                for span in (8, 4, 12):
                    self.assertContains(preview, f"--block-grid-span:{span}")
                public = Client(); public_url = self.page_url(kind, "public", "author-guidelines")
                self.assertEqual(public.get(public_url).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                release = model.objects.get(site=site)
                self.assertContains(public.get(public_url), "--block-grid-span:8")

                # Ordinary page edits and renaming must retain the grid and block widths.
                self.assertEqual(self.save_pages(kind, [self.page(slug="new-guide")]).status_code, 302)
                site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["block_layout"]["gap"], 18)
                self.assertEqual(site.pages_config[0]["blocks"][0]["grid_span"], 8)
                self.assertContains(public.get(public_url), "--block-grid-span:8")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(public_url).status_code, 404)

                self.assertEqual(self.client.post(self.route(kind, "restore", release.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertContains(self.client.get(self.page_url(kind, "preview", "author-guidelines")), "--block-grid-span:8")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(public_url), "--block-grid-span:8")
                data = self.block_payload(kind, entries); data.pop("layout-enabled")
                self.assertEqual(self.client.post(self.block_url(kind, "save"), data).status_code, 302)
                site.refresh_from_db()
                self.assertNotIn("block_layout", site.pages_config[0])
                self.assertEqual(site.pages_config[0]["blocks"][0]["grid_span"], 8)
                self.assertNotContains(self.client.get(self.page_url(kind, "preview", "author-guidelines")), "has-block-grid")
                self.assertContains(public.get(public_url), "has-block-grid")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertNotContains(public.get(public_url), "has-block-grid")
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 200)

    def test_invalid_stale_legacy_and_unauthorised_saves_do_not_change_grid(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                entry = {**self.block("Existing", kind="text"), "grid_span":6}
                self.assertEqual(self.enable_grid(kind, [entry]).status_code, 302)
                valid = self.block_payload(kind, [entry])
                for key,value in (("sections-0-grid_span", "13"), ("sections-0-grid_span", "6;display:none"),
                                  ("layout-gap", "65"), ("layout-alignment", "start;display:none")):
                    self.assertEqual(self.client.post(self.block_url(kind, "save"), {**valid, key:value}).status_code, 400)
                old = dict(valid); old.pop("layout-form_present")
                self.assertEqual(self.client.post(self.block_url(kind, "save"), old).status_code, 400)
                old = dict(valid); old.pop("sections-0-grid_form_present")
                self.assertEqual(self.client.post(self.block_url(kind, "save"), old).status_code, 400)
                self.assertEqual(self.client.post(self.block_url(kind, "save"), {**valid, "fingerprint":"stale"}).status_code, 409)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["block_layout"], {"gap":20, "alignment":"start"})
                self.assertEqual(site.pages_config[0]["blocks"][0]["grid_span"], 6)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.post(self.block_url(kind, "save"), valid).status_code, 404)
                csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(self.block_url(kind, "save"), valid).status_code, 403)


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AIPageGridTests(TestCase):
    setUpTestData = ai_helpers.PageBlockAITests.__dict__["setUpTestData"]
    setUp = ai_helpers.PageBlockAITests.setUp
    owner = ai_helpers.PageBlockAITests.owner
    route = ai_helpers.PageBlockAITests.route
    login = ai_helpers.PageBlockAITests.login
    token = ai_helpers.PageBlockAITests.token
    upload = ai_helpers.PageBlockAITests.upload
    publish = ai_helpers.PageBlockAITests.publish
    add_image = ai_helpers.PageBlockAITests.add_image
    page_url = ai_helpers.PageBlockAITests.page_url
    pages_token = ai_helpers.PageBlockAITests.pages_token
    payload = ai_helpers.PageBlockAITests.payload
    save_pages = ai_helpers.PageBlockAITests.save_pages
    page = staticmethod(ai_helpers.PageBlockAITests.page)
    block = staticmethod(ai_helpers.PageBlockAITests.block)
    block_url = ai_helpers.PageBlockAITests.block_url
    block_payload = ai_helpers.PageBlockAITests.block_payload
    save_blocks = ai_helpers.PageBlockAITests.save_blocks
    ai_url = ai_helpers.PageBlockAITests.ai_url
    request_data = ai_helpers.PageBlockAITests.request_data
    preview_data = ai_helpers.PageBlockAITests.preview_data
    enable_grid = PageGridTests.enable_grid

    def test_ai_block_width_review_preview_return_and_append(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                original = {**self.block("PRIVATE EXISTING", kind="text"), "grid_span":6}
                self.assertEqual(self.enable_grid(kind, [original], gap=16).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                data = self.request_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                wire = self.transport.call_args.args[0].data.decode()
                self.assertNotIn("PRIVATE EXISTING", wire)
                self.assertNotIn("grid_span", wire)
                pv = self.preview_data(kind, data["request_id"])
                pv.update({"choice_0-0-grid_span":"8", "choice_0-1-grid_span":"4"})
                preview = self.client.post(self.ai_url(kind, "preview", data["request_id"], 0), pv)
                self.assertContains(preview, "has-block-grid")
                self.assertContains(preview, "--block-grid-span:4")
                self.assertContains(preview, "--block-grid-span:8")
                blocks = json.loads(preview.context["block_reviewed"])
                self.assertEqual([b["grid_span"] for b in blocks], [4, 8])
                back = self.client.post(self.ai_url(kind, "edit", data["request_id"], 0),
                    {"revision":pv["revision"], "reviewed_blocks":preview.context["block_reviewed"]})
                self.assertEqual([form.initial["grid_span"] for form in back.context["choices"][0]["formset"].forms], [4, 8])
                public = Client(); url = self.page_url(kind, "public", "author-guidelines")
                self.assertNotContains(public.get(url), "Reviewed focus")
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", data["request_id"], 0),
                    {"revision":pv["revision"], "reviewed_blocks":preview.context["block_reviewed"], "confirm":"yes"}).status_code, 302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["block_layout"], {"gap":16, "alignment":"start"})
                self.assertEqual([b["grid_span"] for b in site.pages_config[0]["blocks"]], [6, 4, 8])
                self.assertNotContains(public.get(url), "Reviewed focus")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(url), "Reviewed focus")
                self.assertContains(public.get(url), "--block-grid-span:4")
