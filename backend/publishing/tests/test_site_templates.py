from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Journal, JournalSiteDraft, JournalSiteRelease, Membership, Organisation
from publishing.site_design import SAMPLES, contrast_text, sample_by_id


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SiteTemplateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("template-manager")
        cls.other = users.create_user("other-template-manager")
        org = Organisation.objects.create(name="Fictional publisher", slug="template-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Fictional journal", slug="template-journal")
        cls.other_journal = Journal.objects.create(organisation=org, name="Other journal", slug="template-other")
        Membership.objects.create(user=cls.manager, journal=cls.journal, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.other, journal=cls.other_journal, role=Membership.Role.SITE_MANAGER)
        cls.site = JournalSiteDraft.objects.create(journal=cls.journal, brand_name="Fictional site",
            headline="Fictional headline", introduction="Fictional introduction", created_by=cls.manager)

    def setUp(self):
        self.client.force_login(self.manager)

    def manage(self):
        return self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))

    def tokens(self):
        response = self.manage()
        self.assertEqual(response.status_code, 200)
        return {"revision":str(response.context["site"].revision), "fingerprint":response.context["fingerprint"]}

    def sample_url(self, sample_id, action="preview"):
        return reverse("preview_site_sample" if action == "preview" else "apply_site_sample", args=[self.journal.slug, sample_id])

    def apply(self, sample_id, **values):
        return self.client.post(self.sample_url(sample_id, "apply"), {**self.tokens(), "confirm":"yes", **values})

    def publish(self):
        return self.client.post(reverse("publish_journal_site", args=[self.journal.slug]),
                                {**self.tokens(), "confirm":"yes", "synthetic":"yes"})

    def test_gallery_contains_six_distinct_samples_with_private_working_previews(self):
        page = self.manage()
        self.assertContains(page, "Journal website templates")
        self.assertEqual(len(page.context["design_samples"]), 6)
        self.assertEqual(len({x["config"]["composition"] for x in SAMPLES}), 6)
        for sample in SAMPLES:
            with self.subTest(sample=sample["id"]):
                self.assertContains(page, str(sample["name"]))
                preview = self.client.get(self.sample_url(sample["id"]))
                self.assertEqual(preview.status_code, 200)
                self.assertContains(preview, "composition-" + sample["config"]["composition"])
                self.assertContains(preview, "--site-accent:" + sample["config"]["primary"])
                self.assertIn("no-store", preview["Cache-Control"])
                self.assertEqual(preview["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.site.refresh_from_db()
        self.assertEqual((self.site.design_config, self.site.revision), ({}, 1))
        self.assertEqual(Client().get(self.sample_url("heritage")).status_code, 302)
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.sample_url("heritage")).status_code, 404)
        self.assertEqual(self.client.post(self.sample_url("heritage", "apply"), {}).status_code, 400)
        self.assertEqual(self.client.post(self.sample_url("heritage", "apply"), {"confirm":"yes"}).status_code, 404)

    def test_template_library_has_chinese_labels(self):
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.manage()
        self.assertContains(page, "期刊网站模板")
        self.assertContains(page, "数字前沿")
        self.assertContains(page, "自定义设计")
        self.assertContains(page, "主色")

    def test_sample_application_is_scoped_confirmed_and_stale_safe(self):
        stale = self.tokens()
        self.assertEqual(self.client.get(self.sample_url("unknown")).status_code, 404)
        self.assertEqual(self.apply("heritage", confirm="").status_code, 400)
        self.assertEqual(self.apply("heritage").status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.design_config, sample_by_id("heritage")["config"])
        self.assertEqual(self.site.revision, 2)
        self.assertEqual(self.client.post(self.sample_url("frontier", "apply"), {**stale, "confirm":"yes"}).status_code, 409)
        self.assertEqual(self.apply("heritage").status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.revision, 2)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.manager)
        self.assertEqual(csrf.post(self.sample_url("frontier", "apply"), {}).status_code, 403)

    def test_custom_design_accepts_open_colours_and_can_be_edited(self):
        config = {**sample_by_id("frontier")["config"], "primary":"#a1b2c3", "surface":"#103456", "background":"#f7f6e9",
                  "composition":"split", "font":"literary", "alignment":"left", "section_order":"about_first", "show_feature":"on"}
        url = reverse("edit_site_appearance", args=[self.journal.slug])
        self.assertEqual(self.client.post(url, {**self.tokens(), **config}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.design_config["primary"], "#a1b2c3")
        self.assertTrue(self.site.design_config["show_feature"])
        preview = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(preview, "composition-split font-literary")
        self.assertContains(preview, "--site-accent:#a1b2c3")
        tokens = self.tokens()
        config.update(primary="#663399", show_feature="")
        self.assertEqual(self.client.post(url, {**tokens, **config}).status_code, 302)
        self.site.refresh_from_db()
        self.assertFalse(self.site.design_config["show_feature"])
        self.assertContains(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])), "feature-hidden")
        self.assertEqual(contrast_text("#000000"), "#ffffff")
        self.assertEqual(contrast_text("#ffffff"), "#000000")

    def test_bad_design_or_css_is_rejected_before_rendering(self):
        url = reverse("edit_site_appearance", args=[self.journal.slug])
        config = sample_by_id("heritage")["config"]
        for changes in ({"primary":"red; background:url(evil)"}, {"font":"<script>"}, {"composition":"unknown"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.client.post(url, {**self.tokens(), **config, **changes}).status_code, 400)
        self.site.refresh_from_db()
        self.assertEqual(self.site.design_config, {})
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(design_config={**config, "primary":"#fff;url(evil)"})
        self.assertEqual(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])).status_code, 404)
        # Do not render the corrupted value even if a caller directly changes database content.
        self.assertEqual(self.client.get(reverse("manage_journal_site", args=[self.journal.slug])).status_code, 404)

    def test_snapshot_and_restore_preserve_visual_design_and_site_text(self):
        self.assertEqual(self.publish().status_code, 302)
        first = JournalSiteRelease.objects.get(site=self.site)
        self.assertEqual(first.design_config, {})
        public = Client()
        public_url = reverse("public_journal_site", args=[self.journal.slug])
        self.assertNotContains(public.get(public_url), "composition-editorial")
        self.assertEqual(self.apply("editorial").status_code, 302)
        self.assertNotContains(public.get(public_url), "composition-editorial")
        self.assertEqual(self.publish().status_code, 302)
        self.assertContains(public.get(public_url), "composition-editorial")
        published = JournalSiteRelease.objects.get(site=self.site, revision=2)
        self.assertEqual(published.design_config, sample_by_id("editorial")["config"])
        self.assertEqual(self.apply("frontier").status_code, 302)
        self.assertContains(public.get(public_url), "composition-editorial")
        self.site.refresh_from_db()
        restore_url = reverse("restore_journal_site", args=[self.journal.slug, first.pk])
        self.assertEqual(self.client.post(restore_url, {"revision":str(self.site.revision), "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.design_config, {})
        self.assertEqual(self.site.brand_name, "Fictional site")
        self.assertContains(public.get(public_url), "composition-editorial")

    def test_text_edit_preserves_custom_design(self):
        self.assertEqual(self.apply("horizon").status_code, 302)
        self.site.refresh_from_db()
        edit = {"revision":str(self.site.revision), "brand_name":"Updated fictional site", "headline":self.site.headline,
                "tagline":self.site.tagline, "introduction":self.site.introduction, "theme":self.site.theme, "layout":self.site.layout}
        self.assertEqual(self.client.post(reverse("edit_journal_site", args=[self.journal.slug]), edit).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.design_config, sample_by_id("horizon")["config"])
        self.assertEqual(self.site.brand_name, "Updated fictional site")
