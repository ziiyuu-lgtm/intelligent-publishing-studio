import hashlib
import json

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (Journal, Organisation, OrganisationMembership, OrganisationPortalDraft,
    OrganisationPortalEvent, OrganisationPortalRelease)
from publishing.portal_views import _fingerprint
from publishing.site_design import sample_by_id


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PublisherPortalSectionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("portal-sections-manager")
        cls.other = users.create_user("portal-sections-other")
        cls.org = Organisation.objects.create(name="Fictional publisher", slug="portal-sections")
        cls.foreign_org = Organisation.objects.create(name="Other publisher", slug="portal-sections-other")
        cls.journal = Journal.objects.create(organisation=cls.org, name="Fictional selected journal", slug="selected-journal")
        cls.foreign = Journal.objects.create(organisation=cls.foreign_org, name="Foreign journal", slug="foreign-journal")
        OrganisationMembership.objects.create(organisation=cls.org, user=cls.manager, role=OrganisationMembership.Role.PORTAL_MANAGER)
        OrganisationMembership.objects.create(organisation=cls.foreign_org, user=cls.other, role=OrganisationMembership.Role.PORTAL_MANAGER)
        cls.site = OrganisationPortalDraft.objects.create(organisation=cls.org, created_by=cls.manager,
            brand_name="Fictional press", headline="Fictional portal", introduction="Original publisher introduction",
            design_config=sample_by_id("heritage")["config"], journal_ids=[cls.journal.pk])

    def setUp(self):
        self.client.force_login(self.manager)

    def url(self, name, *extra):
        return reverse(name, args=[self.org.slug, *extra])

    def manage(self):
        return self.client.get(self.url("manage_publisher_portal"))

    def token(self):
        page = self.manage()
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"]}

    def payload(self, entries=None):
        self.site.refresh_from_db()
        initial = len(self.site.sections_config) if self.site.sections_config else 2
        entries = entries if entries is not None else [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":2},
            {"kind":"journals", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"news", "heading":"Test news", "body":"Fictional news text", "visible":True, "ORDER":3},
        ]
        result = {**self.token(), "sections-TOTAL_FORMS":str(len(entries)), "sections-INITIAL_FORMS":str(initial),
                  "sections-MIN_NUM_FORMS":"0", "sections-MAX_NUM_FORMS":"12"}
        for index, entry in enumerate(entries):
            for key in ("kind", "heading", "body", "ORDER"):
                result[f"sections-{index}-{key}"] = str(entry.get(key, ""))
            if entry.get("visible"):
                result[f"sections-{index}-visible"] = "on"
            if entry.get("DELETE"):
                result[f"sections-{index}-DELETE"] = "on"
        return result

    def save(self, entries=None):
        return self.client.post(self.url("edit_publisher_sections"), self.payload(entries))

    def publish(self):
        return self.client.post(self.url("publish_publisher_portal"), {**self.token(), "confirm":"yes", "synthetic":"yes"})

    def test_legacy_page_and_fingerprint_are_unchanged_until_saved(self):
        payload = {"site":self.site.pk, "revision":self.site.revision, "design":self.site.design_config,
                   "ids":self.site.journal_ids, "journals":[(self.journal.pk, self.journal.slug, self.journal.name, False)],
                   "brand_name":self.site.brand_name, "headline":self.site.headline, "tagline":self.site.tagline,
                   "introduction":self.site.introduction}
        old_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(_fingerprint(self.site), old_hash)
        page = self.manage()
        self.assertContains(page, "Page modules")
        self.assertContains(page, "section_editor.js")
        self.assertEqual(page.context["section_formset"].total_form_count(), 3)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, [])
        preview = self.client.get(self.url("preview_publisher_portal"))
        self.assertNotContains(preview, "has-modules")
        self.assertContains(preview, self.journal.name)

    def test_save_reorders_adds_hides_and_keeps_journal_selection(self):
        self.assertEqual(self.save().status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual([x["kind"] for x in self.site.sections_config], ["journals", "about", "news"])
        self.assertEqual(self.site.journal_ids, [self.journal.pk])
        self.assertEqual(self.site.revision, 2)
        page = self.client.get(self.url("preview_publisher_portal"))
        html = page.content.decode()
        self.assertIn("has-modules", html)
        self.assertLess(html.index("publisher-module-journals"), html.index("publisher-module-about"))
        self.assertLess(html.index("publisher-module-about"), html.index("publisher-module-news"))
        self.assertIn("Fictional news text", html)
        self.assertIn("Original publisher introduction", html)
        self.assertIn(self.journal.name, html)
        self.assertNotIn(self.foreign.name, html)
        entries = [dict(item, ORDER=i+1) for i, item in enumerate(self.site.sections_config)]
        entries[2]["visible"] = False
        self.assertEqual(self.save(entries).status_code, 302)
        self.assertNotContains(self.client.get(self.url("preview_publisher_portal")), "Fictional news text")

    def test_snapshots_restore_modules_and_never_publish_automatically(self):
        self.assertEqual(self.publish().status_code, 302)
        legacy = OrganisationPortalRelease.objects.get(site=self.site)
        public = Client()
        public_url = self.url("public_publisher_portal")
        self.assertEqual(self.save().status_code, 302)
        self.assertNotContains(public.get(public_url), "Fictional news text")
        self.assertEqual(self.publish().status_code, 302)
        self.assertContains(public.get(public_url), "Fictional news text")
        release = OrganisationPortalRelease.objects.get(site=self.site, revision=2)
        self.assertEqual([x["kind"] for x in release.sections_config], ["journals", "about", "news"])
        self.assertEqual(self.client.post(self.url("restore_publisher_portal", legacy.pk),
            {**self.token(), "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, [])
        self.assertContains(public.get(public_url), "Fictional news text")
        self.assertNotContains(self.client.get(self.url("preview_publisher_portal")), "Fictional news text")

    def test_hidden_journal_module_removes_links_and_escapes_user_text(self):
        entries = [
            {"kind":"about", "heading":"", "body":"", "visible":False, "ORDER":1},
            {"kind":"journals", "heading":"", "body":"", "visible":False, "ORDER":2},
            {"kind":"custom", "heading":"<script>heading</script>", "body":"<img src=x onerror=alert(1)>", "visible":True, "ORDER":3},
        ]
        self.assertEqual(self.save(entries).status_code, 302)
        page = self.client.get(self.url("preview_publisher_portal"))
        self.assertContains(page, "&lt;script&gt;heading&lt;/script&gt;")
        self.assertContains(page, "&lt;img src=x onerror=alert(1)&gt;")
        self.assertNotContains(page, '<img src=x onerror=alert(1)>')
        self.assertNotContains(page, 'href="#publisher-about"')
        self.assertNotContains(page, 'href="#publisher-journals"')
        self.assertNotContains(page, self.journal.name)

    def test_invalid_duplicate_missing_heading_and_excess_modules_are_rejected(self):
        base = [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"journals", "heading":"", "body":"", "visible":True, "ORDER":2},
        ]
        cases = [[dict(x, visible=False) for x in base],
                 [base[0], dict(base[1], kind="about")],
                 base + [{"kind":"custom", "heading":"", "body":"test", "visible":True, "ORDER":3}],
                 base + [{"kind":"unknown", "heading":"X", "body":"x", "visible":True, "ORDER":3}],
                 base + [{"kind":"custom", "heading":"X"*121, "body":"x", "visible":True, "ORDER":3}],
                 base + [{"kind":"custom", "heading":str(i), "body":"x", "visible":True, "ORDER":i+3} for i in range(11)]]
        for entries in cases:
            with self.subTest(entries=entries):
                self.assertEqual(self.save(entries).status_code, 400)
        self.site.refresh_from_db()
        self.assertEqual((self.site.sections_config, self.site.revision), ([], 1))

    def test_delete_multiple_custom_and_idempotency(self):
        entries = [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"journals", "heading":"", "body":"", "visible":True, "ORDER":2, "DELETE":True},
            {"kind":"custom", "heading":"Collections", "body":"First fictional module", "visible":True, "ORDER":3},
            {"kind":"custom", "heading":"Another", "body":"Second fictional module", "visible":True, "ORDER":4},
        ]
        self.assertEqual(self.save(entries).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual([x["kind"] for x in self.site.sections_config], ["about", "custom", "custom"])
        refreshed = [dict(item, ORDER=i+1) for i, item in enumerate(self.site.sections_config)]
        self.assertEqual(self.save(refreshed).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.revision, 2)
        self.assertEqual(OrganisationPortalEvent.objects.filter(action="portal_sections_updated").count(), 1)

    def test_stale_cross_org_csrf_and_bad_stored_modules_fail_closed(self):
        url = self.url("edit_publisher_sections")
        data = self.payload()
        self.assertEqual(self.client.post(url, {**data, "fingerprint":"bad"}).status_code, 409)
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(url, data).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.manager)
        self.assertEqual(csrf.post(url, data).status_code, 403)
        self.client.force_login(self.manager)
        OrganisationPortalDraft.objects.filter(pk=self.site.pk).update(sections_config=[{"kind":"<script>"}])
        self.assertEqual(self.client.get(self.url("preview_publisher_portal")).status_code, 404)
        self.assertEqual(self.manage().status_code, 404)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.post(url, data).status_code, 404)

    def test_invalid_published_modules_are_not_rendered(self):
        self.assertEqual(self.publish().status_code, 302)
        release = OrganisationPortalRelease.objects.get(site=self.site)
        OrganisationPortalRelease.objects.filter(pk=release.pk).update(
            sections_config=[{"kind":"custom", "heading":"<script>", "body":"x", "visible":"invalid"}])
        self.assertEqual(Client().get(self.url("public_publisher_portal")).status_code, 404)

    def test_modules_and_template_previews_support_chinese(self):
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.manage(), "页面模块")
        self.assertContains(self.manage(), "新增模块")
        self.assertEqual(self.save().status_code, 302)
        page = self.client.get(self.url("preview_publisher_sample", "frontier"))
        self.assertContains(page, "Fictional news text")
        self.assertContains(page, "旗下期刊")
