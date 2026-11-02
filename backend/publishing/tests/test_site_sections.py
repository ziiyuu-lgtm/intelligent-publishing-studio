from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Journal, JournalSiteDraft, JournalSiteEvent, JournalSiteRelease, Membership, Organisation


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SiteSectionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("modules-manager")
        cls.other_manager = users.create_user("other-modules-manager")
        cls.author = users.create_user("modules-author")
        org = Organisation.objects.create(name="Fictional publisher", slug="modules-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Fictional journal", slug="modules-journal")
        cls.other = Journal.objects.create(organisation=org, name="Other journal", slug="modules-other")
        Membership.objects.create(user=cls.manager, journal=cls.journal, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.other_manager, journal=cls.other, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AUTHOR)
        cls.site = JournalSiteDraft.objects.create(journal=cls.journal, brand_name="Fictional site",
            headline="Fictional headline", introduction="Original fictional introduction", created_by=cls.manager)

    def setUp(self):
        self.client.force_login(self.manager)

    def manage(self):
        return self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))

    def tokens(self):
        page = self.manage()
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"]}

    def payload(self, entries=None):
        self.site.refresh_from_db()
        initial = len(self.site.sections_config) if self.site.sections_config else 2
        entries = entries if entries is not None else [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":2},
            {"kind":"archive", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"scope", "heading":"Research scope", "body":"Fictional topic areas", "visible":True, "ORDER":3},
        ]
        result = {**self.tokens(), "sections-TOTAL_FORMS":str(len(entries)), "sections-INITIAL_FORMS":str(initial),
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
        return self.client.post(reverse("edit_site_sections", args=[self.journal.slug]), self.payload(entries))

    def publish(self):
        return self.client.post(reverse("publish_journal_site", args=[self.journal.slug]),
                                {**self.tokens(), "confirm":"yes", "synthetic":"yes"})

    def test_editor_starts_with_legacy_modules_without_mutating_site(self):
        page = self.manage()
        self.assertContains(page, "Page modules")
        self.assertContains(page, "Move up")
        self.assertContains(page, "section_editor.js")
        self.assertEqual(page.context["section_formset"].total_form_count(), 3)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, [])
        preview = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(preview, "About the journal")
        self.assertNotContains(preview, "has-modules")

    def test_save_reorders_adds_and_hides_modules(self):
        self.assertEqual(self.save().status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual([x["kind"] for x in self.site.sections_config], ["archive", "about", "scope"])
        self.assertEqual(self.site.revision, 2)
        preview = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        html = preview.content.decode()
        self.assertIn("has-modules", html)
        self.assertLess(html.index('module-archive'), html.index('module-about'))
        self.assertLess(html.index('module-about'), html.index('module-scope'))
        self.assertContains(preview, "Fictional topic areas")
        self.assertContains(preview, "Original fictional introduction")
        self.assertEqual(preview["X-Robots-Tag"], "noindex, nofollow, noarchive")
        entries = [dict(item, ORDER=i + 1) for i, item in enumerate(self.site.sections_config)]
        entries[2]["visible"] = False
        self.assertEqual(self.save(entries).status_code, 302)
        self.assertNotContains(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])), "Fictional topic areas")

    def test_snapshot_and_restore_preserve_custom_page_modules(self):
        self.assertEqual(self.publish().status_code, 302)
        legacy = JournalSiteRelease.objects.get(site=self.site)
        public = Client()
        public_url = reverse("public_journal_site", args=[self.journal.slug])
        self.assertEqual(self.save().status_code, 302)
        self.assertNotContains(public.get(public_url), "Fictional topic areas")
        self.assertEqual(self.publish().status_code, 302)
        self.assertContains(public.get(public_url), "Fictional topic areas")
        release = JournalSiteRelease.objects.get(site=self.site, revision=2)
        self.assertEqual([x["kind"] for x in release.sections_config], ["archive", "about", "scope"])
        self.site.refresh_from_db()
        url = reverse("restore_journal_site", args=[self.journal.slug, legacy.pk])
        self.assertEqual(self.client.post(url, {"revision":str(self.site.revision), "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, [])
        self.assertContains(public.get(public_url), "Fictional topic areas")
        self.assertNotContains(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])), "Fictional topic areas")

    def test_user_text_is_escaped_and_about_navigation_tracks_visibility(self):
        entries = [
            {"kind":"about", "heading":"", "body":"", "visible":False, "ORDER":1},
            {"kind":"archive", "heading":"", "body":"", "visible":True, "ORDER":2},
            {"kind":"custom", "heading":"<script>heading</script>", "body":"<img src=x onerror=alert(1)>", "visible":True, "ORDER":3},
        ]
        self.assertEqual(self.save(entries).status_code, 302)
        page = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(page, "&lt;script&gt;heading&lt;/script&gt;")
        self.assertContains(page, "&lt;img src=x onerror=alert(1)&gt;")
        self.assertNotContains(page, '<img src=x onerror=alert(1)>')
        self.assertNotContains(page, 'href="#journal-about"')

    def test_invalid_sections_and_too_many_are_rejected(self):
        base = [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"archive", "heading":"", "body":"", "visible":True, "ORDER":2},
        ]
        cases = [
            [dict(x, visible=False) for x in base],
            [base[0], dict(base[1], kind="about")],
            base + [{"kind":"custom", "heading":"", "body":"x", "visible":True, "ORDER":3}],
            base + [{"kind":"bad-kind", "heading":"X", "body":"x", "visible":True, "ORDER":3}],
            base + [{"kind":"custom", "heading":"X" * 121, "body":"x", "visible":True, "ORDER":3}],
            base + [{"kind":"custom", "heading":"X", "body":"x" * 2001, "visible":True, "ORDER":3}],
            base + [{"kind":"custom", "heading":str(i), "body":"x", "visible":True, "ORDER":i+3} for i in range(11)],
        ]
        for entries in cases:
            with self.subTest(entries=entries):
                self.assertEqual(self.save(entries).status_code, 400)
        self.site.refresh_from_db()
        self.assertEqual((self.site.sections_config, self.site.revision), ([], 1))

    def test_empty_extra_form_is_ignored_and_saving_is_idempotent(self):
        entries = [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"archive", "heading":"", "body":"", "visible":True, "ORDER":2},
            {"kind":"", "heading":"", "body":"", "visible":False, "ORDER":""},
        ]
        self.assertEqual(self.save(entries).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(len(self.site.sections_config), 2)
        self.assertEqual(self.site.revision, 2)
        self.assertEqual(self.save(entries).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.revision, 2)
        self.assertEqual(JournalSiteEvent.objects.filter(action="site_sections_updated").count(), 1)

    def test_deletion_and_multiple_custom_modules(self):
        entries = [
            {"kind":"about", "heading":"", "body":"", "visible":True, "ORDER":1},
            {"kind":"archive", "heading":"", "body":"", "visible":True, "ORDER":2, "DELETE":True},
            {"kind":"custom", "heading":"Special collections", "body":"First fictional module", "visible":True, "ORDER":3},
            {"kind":"custom", "heading":"Editorial news", "body":"Second fictional module", "visible":True, "ORDER":4},
        ]
        self.assertEqual(self.save(entries).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual([item["kind"] for item in self.site.sections_config], ["about", "custom", "custom"])
        page = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(page, "First fictional module")
        self.assertContains(page, "Second fictional module")
        self.assertNotContains(page, 'class="journal-site__module module-archive"')

    def test_stale_scope_csrf_and_invalid_stored_data(self):
        url = reverse("edit_site_sections", args=[self.journal.slug])
        data = self.payload()
        self.assertEqual(self.client.post(url, {**data, "fingerprint":"bad"}).status_code, 409)
        self.client.force_login(self.other_manager)
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(url, data).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.manager)
        self.assertEqual(csrf.post(url, data).status_code, 403)
        self.assertEqual(csrf.get(url).status_code, 405)
        self.client.force_login(self.manager)
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(sections_config=[{"kind":"<script>"}])
        self.assertEqual(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("manage_journal_site", args=[self.journal.slug])).status_code, 404)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.post(url, data).status_code, 404)

    def test_module_ui_translates_to_chinese(self):
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.manage(), "页面模块")
        self.assertContains(self.manage(), "新增模块")
