import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (JournalAIPolicy, JournalSiteAIDesignRun, JournalSiteRelease,
    OrganisationAIPolicy, OrganisationPortalAIDesignRun, OrganisationPortalRelease, SiteImageAsset)
from publishing import portal_ai_modules, site_ai_modules
from . import test_site_images as image_helpers


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ModuleImageTests(TestCase):
    setUpTestData = image_helpers.SiteImageTests.__dict__["setUpTestData"]
    owner = image_helpers.SiteImageTests.owner
    route = image_helpers.SiteImageTests.route
    login = image_helpers.SiteImageTests.login
    token = image_helpers.SiteImageTests.token
    upload = image_helpers.SiteImageTests.upload
    publish = image_helpers.SiteImageTests.publish

    def add_image(self, kind, alt):
        self.assertEqual(self.upload(kind, alt=alt, use_as="library").status_code, 302)
        site = self.owner(kind)[2]
        site.refresh_from_db()
        return site.images.first()

    def save_modules(self, kind, entries):
        self.login(kind)
        site = self.owner(kind)[2]
        site.refresh_from_db()
        data = {**self.token(kind), "sections-TOTAL_FORMS":str(len(entries)),
                "sections-INITIAL_FORMS":str(len(site.sections_config) if site.sections_config else 2),
                "sections-MIN_NUM_FORMS":"0", "sections-MAX_NUM_FORMS":"12"}
        for index, entry in enumerate(entries):
            for key in ("kind", "heading", "body", "image_asset_id", "image_alt", "image_fit", "image_position"):
                data[f"sections-{index}-{key}"] = str(entry.get(key, ""))
            data[f"sections-{index}-ORDER"] = str(index + 1)
            if entry.get("visible"):
                data[f"sections-{index}-visible"] = "on"
        route = "edit_site_sections" if kind == "journal" else "edit_publisher_sections"
        return self.client.post(reverse(route, args=[self.owner(kind)[1]]), data)

    @staticmethod
    def modules(asset, *, visible=True, alt="An invented <landscape>"):
        return [{"kind":"about", "heading":"", "body":"", "visible":True},
                {"kind":"custom", "heading":"Feature", "body":"Fictional feature", "visible":visible,
                 "image_asset_id":str(asset.pk), "image_alt":alt, "image_fit":"contain", "image_position":"top"}]

    def test_library_images_preview_publication_hidden_and_release_restoration(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                first = self.add_image(kind, "First image")
                site = self.owner(kind)[2]
                self.assertEqual((site.hero_asset_id, site.revision), (None, 1))
                self.assertEqual(self.save_modules(kind, self.modules(first)).status_code, 302)
                self.assertEqual(self.client.post(self.route(kind, "delete"),
                    {"asset_id":str(first.pk), "confirm":"yes"}).status_code, 409)
                self.assertContains(self.client.get(self.route(kind, "preview")), 'alt="An invented &lt;landscape&gt;"')
                self.assertContains(self.client.get(self.route(kind, "preview")), self.route(kind, "private", first.pk))
                public = Client()
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                original = model.objects.get(site=site)
                self.assertContains(public.get(self.route(kind, "public")), self.route(kind, "public_image", first.pk))
                self.assertContains(public.get(self.route(kind, "public")), 'object-fit:contain;object-position:top')
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)

                second = self.add_image(kind, "Second image")
                entries = self.modules(first, visible=False)
                entries.append({**self.modules(second, alt="Second visible image")[1], "heading":"Another feature"})
                self.assertEqual(self.save_modules(kind, entries).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 200)
                self.assertNotContains(public.get(self.route(kind, "public")), "An invented &lt;landscape&gt;")
                self.assertEqual(self.client.post(self.route(kind, "delete"),
                    {"asset_id":str(first.pk), "confirm":"yes"}).status_code, 409)
                self.assertEqual(self.client.post(self.route(kind, "restore", original.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertContains(self.client.get(self.route(kind, "preview")), self.route(kind, "private", first.pk))
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)

    def test_invalid_foreign_and_corrupt_images_are_rejected(self):
        journal_image = self.add_image("journal", "Journal asset")
        portal_image = self.add_image("portal", "Portal asset")
        for kind, asset, foreign in (("journal", journal_image, portal_image), ("portal", portal_image, journal_image)):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_modules(kind, self.modules(foreign)).status_code, 400)
                self.assertEqual(self.save_modules(kind, self.modules(asset, alt=" ")).status_code, 400)
                bad = self.modules(asset)
                bad[1]["image_fit"] = "cover; color:red"
                self.assertEqual(self.save_modules(kind, bad).status_code, 400)
                self.assertEqual(self.save_modules(kind, self.modules(asset)).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]
                malformed = self.modules(asset)
                malformed[1]["image_fit"] = []
                type(site).objects.filter(pk=site.pk).update(sections_config=malformed)
                self.assertEqual(self.client.get(self.route(kind, "preview")).status_code, 404)
                corrupt = self.modules(foreign)
                type(site).objects.filter(pk=site.pk).update(sections_config=corrupt)
                self.assertEqual(self.client.get(self.route(kind, "preview")).status_code, 404)
                site.refresh_from_db()
                from publishing.site_views import _fingerprint as journal_fingerprint
                from publishing.portal_views import _fingerprint as portal_fingerprint
                fingerprint = journal_fingerprint(site) if kind == "journal" else portal_fingerprint(site)
                self.assertEqual(self.client.post(self.route(kind, "publish"), {"revision":site.revision,
                    "fingerprint":fingerprint, "confirm":"yes", "synthetic":"yes"}).status_code, 409)
                SiteImageAsset.objects.filter(pk=asset.pk).update(image_data=b"damaged")
                public = Client()
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 404)
                self.assertEqual(public.get(self.route(kind, "public")).status_code, 404)

    def test_upload_to_library_does_not_replace_the_hero(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.upload(kind, alt="Original hero").status_code, 302)
                site = self.owner(kind)[2]
                site.refresh_from_db()
                hero, revision = site.hero_asset_id, site.revision
                self.add_image(kind, "Library only")
                site.refresh_from_db()
                self.assertEqual((site.hero_asset_id, site.hero_alt, site.revision), (hero, "Original hero", revision))

    def test_module_image_controls_have_simplified_chinese_labels(self):
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.login(kind)
                page = self.client.get(self.route(kind, "manage"))
                self.assertContains(page, "栏目图片描述")
                self.assertContains(page, "仅上传至素材库")

    def test_ai_receives_no_image_details_and_preserves_them_on_reorder(self):
        for kind, module in (("journal", site_ai_modules), ("portal", portal_ai_modules)):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Private image description")
                sections = self.modules(asset, alt="Private alt text")
                descriptors = module.describe_sections(sections)
                sent = json.dumps(descriptors)
                self.assertNotIn(str(asset.pk), sent)
                self.assertNotIn("Private alt text", sent)
                result = module.materialize_option({"modules":list(reversed(descriptors))}, sections)
                self.assertEqual(result[0], sections[1])
                self.assertEqual(result[1], sections[0])

    @override_settings(IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL="test-model")
    @patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
    @patch("publishing.ai_provider._send_request")
    def test_ai_preview_and_apply_keep_images_private_until_publish(self, transport):
        JournalAIPolicy.objects.create(journal=self.journal, site_modules_enabled=True)
        OrganisationAIPolicy.objects.create(organisation=self.org, portal_modules_enabled=True)
        for kind, module in (("journal", site_ai_modules), ("portal", portal_ai_modules)):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Private image name")
                sections = self.modules(asset, alt="Private module description")
                self.assertEqual(self.save_modules(kind, sections).status_code, 302)
                site = self.owner(kind)[2]
                site.refresh_from_db()
                descriptors = module.describe_sections(site.sections_config)
                option = {"modules":list(reversed(descriptors))}
                transport.return_value = {"status":"completed", "output":[{"type":"message", "role":"assistant",
                    "content":[{"type":"output_text", "text":json.dumps({"options":[option]})}]}]}
                page = self.client.get(self.route(kind, "manage"))
                data = {**self.token(kind), "request_id":str(uuid.uuid4()),
                        "policy_revision":str(page.context["ai_policy_revision"]), "model":"test-model",
                        "brief":"Arrange a fictional page", "confirm":"yes", "synthetic":"yes"}
                request_name = "request_site_ai_modules" if kind == "journal" else "request_publisher_ai_modules"
                self.assertEqual(self.client.post(reverse(request_name, args=[self.owner(kind)[1]]), data).status_code, 302)
                sent = transport.call_args.args[0].data.decode()
                self.assertNotIn(str(asset.pk), sent)
                self.assertNotIn("Private module description", sent)
                run_model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = run_model.objects.latest("created_at")
                preview_name = "preview_site_ai_modules" if kind == "journal" else "preview_publisher_ai_modules"
                preview = self.client.get(reverse(preview_name, args=[self.owner(kind)[1], run.pk, 0]))
                self.assertContains(preview, self.route(kind, "private", asset.pk))
                self.assertContains(preview, 'alt="Private module description"')
                apply_name = "apply_site_ai_modules" if kind == "journal" else "apply_publisher_ai_modules"
                self.assertEqual(self.client.post(reverse(apply_name, args=[self.owner(kind)[1], run.pk, 0]),
                    {"revision":str(site.revision), "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                self.assertEqual(site.sections_config[0]["image_asset_id"], str(asset.pk))
                self.assertEqual(site.sections_config[0]["image_alt"], "Private module description")
                public = Client()
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 200)
