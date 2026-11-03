import base64
import hashlib
import io
import json
import os
import uuid
from unittest.mock import patch

from PIL import Image, PngImagePlugin, features
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (Journal, JournalAIPolicy, JournalSiteDraft, JournalSiteRelease, Membership,
    Organisation, OrganisationAIPolicy, OrganisationMembership, OrganisationPortalDraft,
    OrganisationPortalRelease, SiteImageAsset)
from publishing.site_design import sample_by_id
from publishing.site_images import MAX_UPLOAD_BYTES


def image_bytes(fmt="PNG", mode="RGB", size=(32, 24), metadata=False, animated=False):
    im = Image.new(mode, size, (40, 90, 150, 120) if mode == "RGBA" else (40, 90, 150))
    out = io.BytesIO()
    kwargs = {}
    if metadata and fmt == "PNG":
        info = PngImagePlugin.PngInfo()
        info.add_text("Comment", "UNIQUE_PRIVATE_METADATA")
        kwargs["pnginfo"] = info
    if metadata and fmt == "JPEG":
        exif = Image.Exif()
        exif[0x0131] = "UNIQUE_PRIVATE_METADATA"
        kwargs["exif"] = exif
    if animated:
        kwargs.update(save_all=True, append_images=[Image.new(mode, size, (60, 80, 100))], duration=100, loop=0)
    im.save(out, format=fmt, **kwargs)
    return out.getvalue()


def uploaded(data=None, name="fictional.png", content_type="image/png"):
    return SimpleUploadedFile(name, data if data is not None else image_bytes(), content_type=content_type)


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SiteImageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.journal_manager = users.create_user("image-journal-manager")
        cls.other_journal_manager = users.create_user("image-other-journal-manager")
        cls.portal_manager = users.create_user("image-portal-manager")
        cls.other_portal_manager = users.create_user("image-other-portal-manager")
        cls.author = users.create_user("image-author")
        cls.admin = users.create_superuser("image-superuser", password="synthetic-admin-password")
        cls.org = Organisation.objects.create(name="Fictional publisher", slug="image-publisher")
        cls.other_org = Organisation.objects.create(name="Other publisher", slug="image-other-publisher")
        cls.journal = Journal.objects.create(organisation=cls.org, name="Fictional journal", slug="image-journal")
        cls.other_journal = Journal.objects.create(organisation=cls.org, name="Other journal", slug="image-other-journal")
        Membership.objects.create(journal=cls.journal, user=cls.journal_manager, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(journal=cls.other_journal, user=cls.other_journal_manager, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(journal=cls.journal, user=cls.author, role=Membership.Role.AUTHOR)
        OrganisationMembership.objects.create(organisation=cls.org, user=cls.portal_manager, role=OrganisationMembership.Role.PORTAL_MANAGER)
        OrganisationMembership.objects.create(organisation=cls.other_org, user=cls.other_portal_manager, role=OrganisationMembership.Role.PORTAL_MANAGER)
        cls.jsite = JournalSiteDraft.objects.create(journal=cls.journal, brand_name="Fictional journal",
            headline="Journal headline", introduction="Fictional introduction", created_by=cls.journal_manager)
        cls.psite = OrganisationPortalDraft.objects.create(organisation=cls.org, brand_name="Fictional publisher",
            headline="Publisher headline", introduction="Fictional publisher introduction", created_by=cls.portal_manager,
            design_config=sample_by_id("heritage")["config"])

    def owner(self, kind):
        return (self.journal_manager, self.journal.slug, self.jsite) if kind == "journal" else (self.portal_manager, self.org.slug, self.psite)

    def route(self, kind, action, *args):
        _, slug, _ = self.owner(kind)
        if action == "manage":
            name = "manage_journal_site" if kind == "journal" else "manage_publisher_portal"
        elif action == "preview":
            name = "preview_journal_site" if kind == "journal" else "preview_publisher_portal"
        elif action == "public":
            name = "public_journal_site" if kind == "journal" else "public_publisher_portal"
        elif action == "public_image":
            name = f"public_{kind}_image"
        elif action == "publish":
            name = "publish_journal_site" if kind == "journal" else "publish_publisher_portal"
        elif action == "restore":
            name = "restore_journal_site" if kind == "journal" else "restore_publisher_portal"
        else:
            name = f"{action}_{kind}_image"
        return reverse(name, args=[slug, *args])

    def login(self, kind):
        self.client.force_login(self.owner(kind)[0])

    def token(self, kind):
        page = self.client.get(self.route(kind, "manage"))
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"]}

    def upload(self, kind, data=None, alt="Fictional landscape", **overrides):
        self.login(kind)
        payload = {**self.token(kind), "synthetic":"yes", "alt":alt, "image":uploaded(data)}
        payload.update(overrides)
        return self.client.post(self.route(kind, "upload"), payload)

    def publish(self, kind):
        self.login(kind)
        return self.client.post(self.route(kind, "publish"),
                                {**self.token(kind), "confirm":"yes", "synthetic":"yes"})

    def selected(self, kind):
        site = self.owner(kind)[2]
        site.refresh_from_db()
        return SiteImageAsset.objects.get(pk=site.hero_asset_id)

    def test_upload_reencodes_images_removes_metadata_and_requires_alt(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                original = image_bytes("PNG", "RGBA", metadata=True)
                self.assertEqual(self.upload(kind, original, alt="An invented illustration").status_code, 302)
                asset = self.selected(kind)
                data = bytes(asset.image_data)
                self.assertEqual(asset.media_type, "image/png")
                self.assertEqual(asset.sha256, hashlib.sha256(data).hexdigest())
                self.assertNotIn(b"UNIQUE_PRIVATE_METADATA", data)
                self.assertNotEqual(data, original)
                with Image.open(io.BytesIO(data)) as im:
                    self.assertEqual(im.mode, "RGBA")
                    self.assertEqual(im.getpixel((0, 0))[3], 120)
                    self.assertFalse(im.getexif())
                    self.assertNotIn("Comment", im.info)
                page = self.client.get(self.route(kind, "preview"))
                self.assertContains(page, "An invented illustration")
                self.assertContains(page, self.route(kind, "private", asset.pk))
                private = self.client.get(self.route(kind, "private", asset.pk))
                self.assertEqual(private.content, data)
                self.assertEqual(private["X-Content-Type-Options"], "nosniff")
                self.assertIn("no-store", private["Cache-Control"])
                self.assertEqual(Client().get(self.route(kind, "public_image", asset.pk)).status_code, 404)

    def test_jpeg_and_webp_are_decoded_and_reencoded(self):
        jpeg = image_bytes("JPEG", metadata=True)
        self.assertEqual(self.upload("journal", jpeg).status_code, 302)
        asset = self.selected("journal")
        self.assertEqual(asset.media_type, "image/jpeg")
        self.assertNotIn(b"UNIQUE_PRIVATE_METADATA", bytes(asset.image_data))
        with Image.open(io.BytesIO(bytes(asset.image_data))) as im:
            self.assertEqual(im.format, "JPEG")
            self.assertFalse(im.getexif())
        if features.check("webp"):
            self.assertEqual(self.upload("portal", image_bytes("WEBP")).status_code, 302)
            self.assertEqual(self.selected("portal").media_type, "image/jpeg")

    def test_bad_files_dimensions_animation_confirmation_and_staleness_are_rejected(self):
        for bad in (b"<svg onload='alert(1)'/>", b"%PDF-1.7", b"corrupt",
                    image_bytes("GIF"), image_bytes("PNG", size=(4097, 2)),
                    image_bytes("PNG", size=(4000, 3001)),
                    image_bytes("PNG", animated=True), b"x"*(MAX_UPLOAD_BYTES+1)):
            with self.subTest(length=len(bad)):
                self.assertEqual(self.upload("journal", bad).status_code, 400)
        self.assertEqual(self.upload("journal", alt="").status_code, 400)
        self.assertEqual(self.upload("journal", alt="x"*201).status_code, 400)
        self.assertEqual(self.upload("journal", synthetic="").status_code, 400)
        self.assertEqual(self.upload("journal", fit="cover; background:url(evil)").status_code, 400)
        self.assertEqual(self.upload("journal", position="top; color:red").status_code, 400)
        self.assertEqual(self.upload("journal", fingerprint="stale").status_code, 409)
        self.jsite.refresh_from_db()
        self.assertEqual((self.jsite.hero_asset_id, self.jsite.revision), (None, 1))
        self.assertFalse(SiteImageAsset.objects.exists())

    def test_roles_csrf_and_site_isolation(self):
        self.assertEqual(self.upload("journal").status_code, 302)
        asset = self.selected("journal")
        private_url = self.route("journal", "private", asset.pk)
        self.assertEqual(Client().get(private_url).status_code, 302)
        for user in (self.other_journal_manager, self.portal_manager, self.author, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(private_url).status_code, 404)
        self.login("portal")
        self.assertEqual(self.client.get(self.route("portal", "private", asset.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.route("portal", "select"),
            {**self.token("portal"), "asset_id":str(asset.pk), "alt":"Fictional"}).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.journal_manager)
        self.assertEqual(csrf.post(self.route("journal", "upload"), {}).status_code, 403)
        self.assertEqual(csrf.post(self.route("journal", "delete"), {}).status_code, 403)
        Membership.objects.filter(journal=self.journal, user=self.journal_manager).update(active=False)
        self.login("journal")
        self.assertEqual(self.client.get(private_url).status_code, 404)

    def test_release_snapshot_replacement_restore_and_old_image_visibility(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.upload(kind, alt="First fictional image", fit="contain", position="top").status_code, 302)
                first = self.selected(kind)
                public = Client()
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]
                release_model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                first_release = release_model.objects.get(site=site)
                page = public.get(self.route(kind, "public"))
                self.assertContains(page, self.route(kind, "public_image", first.pk))
                self.assertContains(page, 'alt="First fictional image"')
                self.assertContains(page, 'object-fit:contain;object-position:top')
                first_response = public.get(self.route(kind, "public_image", first.pk))
                self.assertEqual(first_response.status_code, 200)
                self.assertEqual(first_response["X-Robots-Tag"], "noindex, nofollow, noarchive")
                self.assertEqual(first_response["Content-Security-Policy"], "default-src 'none'")
                self.assertEqual(self.upload(kind, image_bytes("PNG", "RGBA"), alt="Second fictional image").status_code, 302)
                second = self.selected(kind)
                self.assertContains(self.client.get(self.route(kind, "preview")), 'alt="Second fictional image"')
                self.assertContains(public.get(self.route(kind, "public")), 'alt="First fictional image"')
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 404)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 200)
                self.assertEqual(self.client.post(self.route(kind, "restore", first_release.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertContains(self.client.get(self.route(kind, "preview")), 'alt="First fictional image"')
                self.assertContains(self.client.get(self.route(kind, "preview")), 'object-fit:contain;object-position:top')
                self.assertContains(public.get(self.route(kind, "public")), 'alt="Second fictional image"')
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(public.get(self.route(kind, "public_image", first.pk)).status_code, 200)
                self.assertEqual(public.get(self.route(kind, "public_image", second.pk)).status_code, 404)

    def test_library_selection_alt_text_clearing_and_safe_deletion(self):
        self.assertEqual(self.upload("journal", alt="First image").status_code, 302)
        first = self.selected("journal")
        self.assertEqual(self.upload("journal", image_bytes("PNG", "RGBA"), alt="Second image").status_code, 302)
        second = self.selected("journal")
        data = {**self.token("journal"), "asset_id":str(first.pk), "alt":'<script>alert(1)</script>'}
        self.assertEqual(self.client.post(self.route("journal", "select"), data).status_code, 302)
        page = self.client.get(self.route("journal", "preview"))
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, '<script>alert(1)</script>')
        site = self.owner("journal")[2]
        site.refresh_from_db()
        current_revision = site.revision
        self.assertEqual(self.client.post(self.route("journal", "select"), data).status_code, 409)
        self.assertEqual(self.client.post(self.route("journal", "select"),
            {**self.token("journal"), "asset_id":str(first.pk), "alt":'<script>alert(1)</script>'}).status_code, 302)
        site.refresh_from_db()
        self.assertEqual(site.revision, current_revision)
        self.assertEqual(self.client.post(self.route("journal", "select"),
            {**self.token("journal"), "asset_id":str(first.pk), "alt":"Adjusted image",
             "fit":"contain", "position":"left"}).status_code, 302)
        self.assertContains(self.client.get(self.route("journal", "preview")), "object-fit:contain;object-position:left")
        self.assertEqual(self.client.post(self.route("journal", "select"),
            {**self.token("journal"), "asset_id":str(first.pk), "alt":"Adjusted image",
             "fit":"cover; color:red", "position":"left"}).status_code, 400)
        self.assertEqual(self.client.post(self.route("journal", "delete"), {"asset_id":str(first.pk), "confirm":"yes"}).status_code, 409)
        self.assertEqual(self.client.post(self.route("journal", "delete"), {"asset_id":str(second.pk)}).status_code, 400)
        self.assertEqual(self.client.post(self.route("journal", "delete"), {"asset_id":str(second.pk), "confirm":"yes"}).status_code, 302)
        self.assertFalse(SiteImageAsset.objects.filter(pk=second.pk).exists())
        self.assertEqual(self.client.post(self.route("journal", "clear"), self.token("journal")).status_code, 302)
        site.refresh_from_db()
        self.assertIsNone(site.hero_asset_id)
        self.assertEqual(site.hero_alt, "")
        self.assertEqual((site.hero_fit, site.hero_position), ("cover", "center"))
        self.assertNotContains(self.client.get(self.route("journal", "preview")), "journal-site__hero-image")

    def test_published_or_foreign_assets_cannot_be_deleted_or_attached(self):
        self.assertEqual(self.upload("journal").status_code, 302)
        first = self.selected("journal")
        self.assertEqual(self.publish("journal").status_code, 302)
        self.assertEqual(self.upload("journal", image_bytes("PNG", "RGBA")).status_code, 302)
        self.assertEqual(self.client.post(self.route("journal", "delete"), {"asset_id":str(first.pk), "confirm":"yes"}).status_code, 409)
        self.assertEqual(self.upload("portal").status_code, 302)
        foreign = self.selected("portal")
        self.login("journal")
        self.assertEqual(self.client.post(self.route("journal", "select"),
            {**self.token("journal"), "asset_id":str(foreign.pk), "alt":"Foreign"}).status_code, 404)
        JournalSiteDraft.objects.filter(pk=self.jsite.pk).update(hero_asset=foreign, hero_alt="Foreign")
        self.assertEqual(self.client.get(self.route("journal", "preview")).status_code, 404)
        self.assertEqual(self.publish("journal").status_code, 409)

    def test_damaged_or_invalid_release_images_fail_closed(self):
        self.assertEqual(self.upload("portal").status_code, 302)
        asset = self.selected("portal")
        self.assertEqual(self.publish("portal").status_code, 302)
        public = Client()
        self.assertEqual(public.get(self.route("portal", "public_image", asset.pk)).status_code, 200)
        SiteImageAsset.objects.filter(pk=asset.pk).update(image_data=b"tampered")
        self.assertEqual(public.get(self.route("portal", "public_image", asset.pk)).status_code, 404)
        self.assertEqual(public.get(self.route("portal", "public")).status_code, 404)
        self.assertEqual(self.publish("portal").status_code, 409)

    def test_site_image_count_limit_and_one_owner_constraint(self):
        with patch("publishing.site_image_views.MAX_SITE_IMAGES", 1):
            self.assertEqual(self.upload("journal").status_code, 302)
            self.assertEqual(self.upload("journal").status_code, 409)
        asset = self.selected("journal")
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteImageAsset.objects.create(image_data=b"x", media_type="image/png", width=1, height=1,
                sha256="0"*64, default_alt="x", uploaded_by=self.journal_manager)
        with self.assertRaises(IntegrityError), transaction.atomic():
            SiteImageAsset.objects.create(journal_site=self.jsite, portal_site=self.psite,
                image_data=b"x", media_type="image/png", width=1, height=1, sha256="0"*64,
                default_alt="x", uploaded_by=self.journal_manager)
        self.assertTrue(SiteImageAsset.objects.filter(pk=asset.pk).exists())

    def test_local_gate_and_chinese_ui(self):
        self.assertEqual(self.upload("portal").status_code, 302)
        asset = self.selected("portal")
        for config in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with override_settings(**config):
                self.assertEqual(self.client.get(self.route("portal", "private", asset.pk)).status_code, 404)
                self.assertEqual(Client().get(self.route("portal", "public_image", asset.pk)).status_code, 404)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(self.route("portal", "manage"))
        self.assertContains(page, "首页主视觉图片与图片库")
        self.assertContains(page, "上传并设为草稿图片")
        self.assertContains(page, "图片显示方式")
        self.assertContains(page, "图片焦点位置")

    @override_settings(IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL="test-model")
    def test_visual_ai_request_does_not_send_image_or_alt_text(self):
        self.assertEqual(self.upload("portal", alt="PRIVATE IMAGE DESCRIPTION").status_code, 302)
        asset = self.selected("portal")
        OrganisationAIPolicy.objects.create(organisation=self.org, portal_design_enabled=True)
        self.login("portal")
        page = self.client.get(self.route("portal", "manage"))
        data = {"request_id":str(uuid.uuid4()), "revision":str(page.context["site"].revision),
                "fingerprint":page.context["fingerprint"], "policy_revision":str(page.context["ai_policy_revision"]),
                "model":"test-model", "brief":"Fictional visual request", "confirm":"yes", "synthetic":"yes"}
        valid = sample_by_id("frontier")["config"]
        response = {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
            {"type":"output_text", "text":json.dumps({"options":[valid]})}]}]}
        with patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"}), patch("publishing.ai_provider._send_request", return_value=response) as transport:
            result = self.client.post(reverse("request_publisher_ai_design", args=[self.org.slug]), data)
            self.assertEqual(result.status_code, 302)
        sent = transport.call_args.args[0].data.decode()
        self.assertNotIn("PRIVATE IMAGE DESCRIPTION", sent)
        self.assertNotIn(str(asset.pk), sent)
        self.assertNotIn(base64.b64encode(bytes(asset.image_data)).decode(), sent)
        payload = json.loads(sent)
        self.assertEqual(set(json.loads(payload["input"])), {"design_brief", "current_appearance"})
        run_id = result.url.rstrip("/").split("/")[-1]
        preview = self.client.get(reverse("preview_publisher_ai_option", args=[self.org.slug, run_id, 0]))
        self.assertContains(preview, "PRIVATE IMAGE DESCRIPTION")
        self.assertContains(preview, self.route("portal", "private", asset.pk))
        self.assertEqual(self.client.post(reverse("apply_publisher_ai_option", args=[self.org.slug, run_id, 0]),
                                          {"revision":data["revision"], "confirm":"yes"}).status_code, 302)
        self.psite.refresh_from_db()
        self.assertEqual(self.psite.hero_asset_id, asset.pk)

    @override_settings(IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL="test-model")
    def test_ai_module_previews_preserve_images_without_sending_them(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.upload(kind, alt="PRIVATE IMAGE DESCRIPTION").status_code, 302)
                asset = self.selected(kind)
                if kind == "journal":
                    JournalAIPolicy.objects.create(journal=self.journal, site_modules_enabled=True)
                    request_name, preview_name, first_kind = "request_site_ai_modules", "preview_site_ai_modules", "archive"
                else:
                    OrganisationAIPolicy.objects.create(organisation=self.org, portal_modules_enabled=True)
                    request_name, preview_name, first_kind = "request_publisher_ai_modules", "preview_publisher_ai_modules", "journals"
                _, slug, site = self.owner(kind)
                page = self.client.get(self.route(kind, "manage"))
                data = {"request_id":str(uuid.uuid4()), "revision":str(page.context["site"].revision),
                        "fingerprint":page.context["fingerprint"], "policy_revision":str(page.context["ai_policy_revision"]),
                        "model":"test-model", "brief":"Fictional module request", "confirm":"yes", "synthetic":"yes"}
                option = {"modules":[{"id":"m1", "kind":first_kind, "visible":True},
                                     {"id":"m0", "kind":"about", "visible":True}]}
                response = {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
                    {"type":"output_text", "text":json.dumps({"options":[option]})}]}]}
                with patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"}), patch("publishing.ai_provider._send_request", return_value=response) as transport:
                    result = self.client.post(reverse(request_name, args=[slug]), data)
                self.assertEqual(result.status_code, 302)
                sent = transport.call_args.args[0].data.decode()
                self.assertNotIn("PRIVATE IMAGE DESCRIPTION", sent)
                self.assertNotIn(str(asset.pk), sent)
                self.assertNotIn(base64.b64encode(bytes(asset.image_data)).decode(), sent)
                run_id = result.url.rstrip("/").split("/")[-1]
                preview = self.client.get(reverse(preview_name, args=[slug, run_id, 0]))
                self.assertContains(preview, "PRIVATE IMAGE DESCRIPTION")
                self.assertContains(preview, self.route(kind, "private", asset.pk))
                site.refresh_from_db()
                self.assertEqual(site.hero_asset_id, asset.pk)
