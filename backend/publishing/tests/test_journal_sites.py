from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (Journal, JournalSiteDraft, JournalSiteEvent, JournalSiteRelease, Membership,
                               Organisation, Submission)


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class JournalSiteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("site-manager-a")
        cls.other_manager = users.create_user("site-manager-b")
        cls.author = users.create_user("site-test-author")
        cls.superuser = users.create_superuser("site-test-admin", password="synthetic-long-password")
        org = Organisation.objects.create(name="Fictional publisher", slug="site-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Fictional Journal A", slug="site-a")
        cls.other = Journal.objects.create(organisation=org, name="Fictional Journal B", slug="site-b")
        Membership.objects.create(user=cls.manager, journal=cls.journal, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.other_manager, journal=cls.other, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AUTHOR)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author, title="PRIVATE fictional manuscript",
            abstract="PRIVATE fictional abstract", manuscript="manuscripts/private.pdf", status=Submission.Status.SUBMITTED)

    def start(self, journal=None, actor=None):
        journal = journal or self.journal
        self.client.force_login(actor or self.manager)
        response = self.client.post(reverse("start_journal_site", args=[journal.slug]), {"synthetic":"yes"})
        self.assertEqual(response.status_code, 302)
        return JournalSiteDraft.objects.get(journal=journal)

    def edit(self, site, **overrides):
        site.refresh_from_db()
        self.client.force_login(self.manager if site.journal_id == self.journal.pk else self.other_manager)
        data = {"revision": str(site.revision), "brand_name":site.journal.name, "headline":"Fictional research journal", "tagline":"A test of scholarly design",
                "introduction":"A fictional introduction.", "theme":"ink", "layout":"magazine"}
        data.update(overrides)
        return self.client.post(reverse("edit_journal_site", args=[site.journal.slug]), data)

    def publish_data(self, site, actor=None):
        site.refresh_from_db()
        self.client.force_login(actor or self.manager)
        page = self.client.get(reverse("manage_journal_site", args=[site.journal.slug]))
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"],
                "confirm":"yes", "synthetic":"yes"}

    def publish(self, site, actor=None):
        return self.client.post(reverse("publish_journal_site", args=[site.journal.slug]), self.publish_data(site, actor))

    def test_disabled_and_debug_off_block_design_and_public_routes(self):
        for settings_values in ({"IPS_LOCAL_PUBLICATION_ENABLED":False}, {"DEBUG":False}):
            with self.subTest(settings_values=settings_values), override_settings(**settings_values):
                self.client.force_login(self.manager)
                self.assertEqual(self.client.get(reverse("manage_journal_site", args=[self.journal.slug])).status_code, 404)
                self.assertEqual(self.client.get(reverse("public_journal_site", args=[self.journal.slug])).status_code, 404)
        self.assertFalse(JournalSiteDraft.objects.exists())

    def test_only_authorised_site_manager_can_create_or_preview(self):
        url = reverse("manage_journal_site", args=[self.journal.slug])
        for user in (self.author, self.other_manager, self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(reverse("start_journal_site", args=[self.journal.slug]), {"synthetic":"yes"}).status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertFalse(JournalSiteDraft.objects.exists())
        self.assertEqual(self.client.post(reverse("start_journal_site", args=[self.journal.slug]), {}).status_code, 400)
        site = self.start()
        self.assertEqual(self.start().pk, site.pk)
        self.assertEqual(JournalSiteEvent.objects.filter(action="site_draft_created").count(), 1)
        self.client.force_login(self.manager)
        workspace = self.client.get(reverse("workspace"))
        self.assertContains(workspace, self.journal.name)
        self.assertNotContains(workspace, self.other.name)
        self.assertNotContains(workspace, self.submission.title)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(Client().get(reverse("preview_journal_site", args=[self.journal.slug])).status_code, 302)
        self.client.force_login(self.other_manager)
        self.assertEqual(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])).status_code, 404)

    def test_preview_publish_history_and_restore_to_draft(self):
        site = self.start()
        self.assertEqual(self.edit(site).status_code, 302)
        preview = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(preview, "Fictional research journal")
        self.assertContains(preview, "theme-ink layout-magazine")
        self.assertEqual(preview["X-Robots-Tag"], "noindex, nofollow, noarchive")
        public = Client()
        public_url = reverse("public_journal_site", args=[self.journal.slug])
        self.assertEqual(public.get(public_url).status_code, 404)
        self.assertNotContains(public.get(reverse("home")), "View test journal site")
        self.assertEqual(self.publish(site).status_code, 302)
        first = JournalSiteRelease.objects.get(site=site)
        self.assertEqual(first.revision, 2)
        page = public.get(public_url)
        self.assertContains(page, "Fictional research journal")
        self.assertContains(page, "theme-ink layout-magazine")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertContains(public.get(reverse("home")), "View test journal site")
        Journal.objects.filter(pk=self.journal.pk).update(name="Renamed canonical journal")
        self.assertContains(public.get(public_url), "Fictional Journal A")
        self.assertNotContains(public.get(public_url), "Renamed canonical journal")
        self.assertEqual(self.publish(site).status_code, 409)
        self.assertEqual(self.edit(site, brand_name="Another site name", headline="Another fictional design", theme="terracotta", layout="classic").status_code, 302)
        self.assertContains(public.get(public_url), "Fictional research journal")
        self.assertNotContains(public.get(public_url), "Another fictional design")
        self.assertNotContains(public.get(public_url), "Another site name")
        self.assertEqual(self.publish(site).status_code, 302)
        self.assertContains(public.get(public_url), "Another fictional design")
        self.assertContains(public.get(public_url), "Another site name")
        site.refresh_from_db()
        self.client.force_login(self.manager)
        restored = self.client.post(reverse("restore_journal_site", args=[self.journal.slug, first.pk]),
                                    {"revision":str(site.revision), "confirm":"yes"})
        self.assertEqual(restored.status_code, 302)
        self.assertContains(public.get(public_url), "Another fictional design")
        self.assertContains(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])), "Fictional research journal")
        self.assertEqual(self.publish(site).status_code, 302)
        self.assertContains(public.get(public_url), "Fictional research journal")
        self.assertContains(public.get(public_url), "Fictional Journal A")
        self.assertNotContains(public.get(public_url), "Another site name")
        self.assertEqual(list(JournalSiteRelease.objects.filter(site=site).order_by("revision").values_list("revision", flat=True)), [2, 3, 4])
        self.assertEqual(JournalSiteEvent.objects.filter(site=site, action="site_draft_restored", source_release=first).count(), 1)

    def test_stale_or_unconfirmed_actions_do_not_publish_or_replace(self):
        site = self.start()
        old = self.publish_data(site)
        self.assertEqual(self.edit(site).status_code, 302)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.post(reverse("publish_journal_site", args=[self.journal.slug]), old).status_code, 409)
        current = self.publish_data(site)
        self.assertEqual(self.client.post(reverse("publish_journal_site", args=[self.journal.slug]), {**current, "fingerprint":"bad"}).status_code, 409)
        self.assertEqual(self.client.post(reverse("publish_journal_site", args=[self.journal.slug]), {**current, "synthetic":""}).status_code, 400)
        self.assertFalse(JournalSiteRelease.objects.exists())
        self.assertEqual(self.publish(site).status_code, 302)
        release = JournalSiteRelease.objects.get(site=site)
        site.refresh_from_db()
        self.assertEqual(self.client.post(reverse("restore_journal_site", args=[self.journal.slug, release.pk]), {"revision":"1", "confirm":"yes"}).status_code, 409)
        self.assertEqual(self.client.post(reverse("restore_journal_site", args=[self.journal.slug, release.pk]), {"revision":str(site.revision)}).status_code, 400)

    def test_user_text_is_escaped_and_preset_choices_are_validated(self):
        site = self.start()
        self.assertEqual(self.edit(site, brand_name='<svg onload=alert(1)>', headline='<script>alert("x")</script>', introduction='<img src=x onerror=alert(1)>').status_code, 302)
        self.assertEqual(self.publish(site).status_code, 302)
        page = Client().get(reverse("public_journal_site", args=[self.journal.slug]))
        self.assertNotContains(page, '<script>alert("x")</script>')
        self.assertNotContains(page, '<img src=x onerror=alert(1)>')
        self.assertNotContains(page, '<svg onload=alert(1)>')
        self.assertContains(page, "&lt;script&gt;")
        self.assertContains(page, "&lt;img src=x onerror=alert(1)&gt;")
        self.assertContains(page, "&lt;svg onload=alert(1)&gt;")
        self.assertEqual(self.edit(site, theme="not-a-theme").status_code, 400)

    def test_site_management_and_preview_support_chinese_interface(self):
        self.start()
        self.client.post(reverse("set_language"), {"language": "zh-hans", "next": "/"})
        management = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        self.assertContains(management, "期刊网站设计")
        self.assertContains(management, "网站显示名称")
        preview = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(preview, "期刊介绍")

    def test_cross_journal_restore_is_denied_and_invalid_release_pointer_is_hidden(self):
        first_site = self.start()
        self.edit(first_site)
        self.publish(first_site)
        first_release = JournalSiteRelease.objects.get(site=first_site)
        second_site = self.start(self.other, self.other_manager)
        self.edit(second_site)
        self.publish(second_site, self.other_manager)
        second_site.refresh_from_db()
        self.client.force_login(self.other_manager)
        self.assertEqual(self.client.post(reverse("restore_journal_site", args=[self.other.slug, first_release.pk]),
                                         {"revision":str(second_site.revision), "confirm":"yes"}).status_code, 404)
        JournalSiteDraft.objects.filter(pk=second_site.pk).update(current_release=first_release)
        self.assertEqual(Client().get(reverse("public_journal_site", args=[self.other.slug])).status_code, 404)

    def test_revoked_role_and_inactive_journal_block_management_and_site(self):
        site = self.start()
        self.edit(site)
        self.publish(site)
        public_url = reverse("public_journal_site", args=[self.journal.slug])
        Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.SITE_MANAGER).update(active=False)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse("manage_journal_site", args=[self.journal.slug])).status_code, 404)
        self.assertEqual(self.client.get(reverse("preview_journal_site", args=[self.journal.slug])).status_code, 404)
        self.assertEqual(Client().get(public_url).status_code, 200)
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(Client().get(public_url).status_code, 404)

    def test_mutations_require_csrf_and_post(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(reverse("start_journal_site", args=[self.journal.slug]), {"synthetic":"yes"}).status_code, 403)
        site = self.start()
        self.assertEqual(client.post(reverse("edit_journal_site", args=[self.journal.slug]), {}).status_code, 403)
        self.assertEqual(client.post(reverse("publish_journal_site", args=[self.journal.slug]), {}).status_code, 403)
        self.assertEqual(client.get(reverse("publish_journal_site", args=[self.journal.slug])).status_code, 405)
        self.assertFalse(JournalSiteRelease.objects.filter(site=site).exists())
