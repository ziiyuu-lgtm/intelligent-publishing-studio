from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Journal, JournalAIPolicy, JournalAIPolicyEvent, Membership, Organisation, Submission


@override_settings(DEBUG=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class JournalAIPolicyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("ai-manager-a")
        cls.other_manager = users.create_user("ai-manager-b")
        cls.author = users.create_user("ai-policy-author")
        cls.superuser = users.create_superuser("ai-policy-admin", password="fictional-admin-password")
        org = Organisation.objects.create(name="Fictional publisher", slug="ai-policy-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Fictional Journal A", slug="ai-policy-a")
        cls.other = Journal.objects.create(organisation=org, name="Fictional Journal B", slug="ai-policy-b")
        Membership.objects.create(user=cls.manager, journal=cls.journal, role=Membership.Role.AI_MANAGER)
        Membership.objects.create(user=cls.other_manager, journal=cls.other, role=Membership.Role.AI_MANAGER)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AUTHOR)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author,
            title="PRIVATE FICTIONAL TITLE", abstract="PRIVATE FICTIONAL ABSTRACT", manuscript="manuscripts/test.pdf",
            status=Submission.Status.SUBMITTED)

    def url(self, journal=None):
        return reverse("manage_ai_policy", args=[(journal or self.journal).slug])

    def save(self, revision="0", **overrides):
        self.client.force_login(self.manager)
        data = {"revision":revision, "metadata_enabled":"on", "allow_author":"on", "allow_editor":"on",
                "journal_daily_limit":"4", "confirm":"yes", "synthetic":"yes"}
        data.update(overrides)
        return self.client.post(self.url(), data)

    def test_manager_is_scoped_and_has_no_manuscript_access(self):
        for user in (self.author, self.other_manager, self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url()).status_code, 404)
            self.assertEqual(self.client.post(self.url(), {"confirm":"yes", "synthetic":"yes"}).status_code, 404)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url(self.other)).status_code, 404)
        page = self.client.get(self.url())
        self.assertContains(page, "Journal AI policy")
        self.assertContains(page, "Journal administrator switch")
        self.assertContains(page, "Disabled")
        self.assertNotContains(page, self.submission.title)
        self.assertNotContains(page, self.submission.abstract)
        workspace = self.client.get(reverse("workspace"))
        self.assertContains(workspace, self.journal.name)
        self.assertNotContains(workspace, self.other.name)
        self.assertNotContains(workspace, self.submission.title)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[self.submission.pk])).status_code, 404)

    def test_site_design_can_be_enabled_independently_of_metadata(self):
        response = self.save(metadata_enabled="", allow_author="", allow_editor="", site_design_enabled="on")
        self.assertEqual(response.status_code, 302)
        policy = JournalAIPolicy.objects.get(journal=self.journal)
        self.assertTrue(policy.site_design_enabled)
        self.assertFalse(policy.metadata_enabled)
        self.assertFalse(policy.allow_author)
        self.assertFalse(policy.allow_editor)
        self.assertTrue(JournalAIPolicyEvent.objects.get(policy=policy).configuration["site_design_enabled"])

    def test_module_arrangement_is_independent_of_visual_design_and_metadata(self):
        response = self.save(metadata_enabled="", allow_author="", allow_editor="", site_modules_enabled="on")
        self.assertEqual(response.status_code, 302)
        policy = JournalAIPolicy.objects.get(journal=self.journal)
        self.assertTrue(policy.site_modules_enabled)
        self.assertFalse(policy.site_design_enabled)
        self.assertFalse(policy.metadata_enabled)
        self.assertTrue(JournalAIPolicyEvent.objects.get(policy=policy).configuration["site_modules_enabled"])

    def test_page_suggestions_have_their_own_policy_switch(self):
        response = self.save(metadata_enabled="", allow_author="", allow_editor="", site_pages_enabled="on")
        self.assertEqual(response.status_code, 302)
        policy = JournalAIPolicy.objects.get(journal=self.journal)
        self.assertTrue(policy.site_pages_enabled)
        self.assertFalse(policy.site_design_enabled)
        self.assertFalse(policy.site_modules_enabled)
        self.assertTrue(JournalAIPolicyEvent.objects.get(policy=policy).configuration["site_pages_enabled"])

    def test_page_block_suggestions_have_a_separate_policy_switch(self):
        response = self.save(metadata_enabled="", allow_author="", allow_editor="", site_page_blocks_enabled="on")
        self.assertEqual(response.status_code, 302)
        policy = JournalAIPolicy.objects.get(journal=self.journal)
        self.assertTrue(policy.site_page_blocks_enabled)
        self.assertFalse(policy.site_pages_enabled)
        self.assertTrue(JournalAIPolicyEvent.objects.get(policy=policy).configuration["site_page_blocks_enabled"])

    def test_create_change_history_and_stale_update(self):
        self.assertEqual(self.save().status_code, 302)
        policy = JournalAIPolicy.objects.get(journal=self.journal)
        self.assertEqual(policy.revision, 1)
        self.assertTrue(policy.metadata_enabled)
        self.assertEqual(policy.journal_daily_limit, 4)
        event = JournalAIPolicyEvent.objects.get(policy=policy)
        self.assertEqual(event.configuration, {"metadata_enabled":True, "allow_author":True, "allow_editor":True, "site_design_enabled":False, "site_modules_enabled":False, "site_pages_enabled":False, "site_page_blocks_enabled":False, "journal_daily_limit":4})
        self.assertNotIn(self.submission.title, str(event.configuration))
        self.assertFalse(Journal.objects.get(pk=self.journal.pk).allow_openai_metadata)
        self.assertEqual(self.save(revision="0", journal_daily_limit="5").status_code, 409)
        self.assertEqual(self.save(revision="1", journal_daily_limit="5", allow_editor="").status_code, 302)
        policy.refresh_from_db()
        self.assertEqual((policy.revision, policy.journal_daily_limit, policy.allow_editor), (2, 5, False))
        self.assertEqual(JournalAIPolicyEvent.objects.filter(policy=policy).count(), 2)
        self.assertEqual(self.save(revision="2", journal_daily_limit="5", allow_editor="").status_code, 302)
        self.assertEqual(JournalAIPolicyEvent.objects.filter(policy=policy).count(), 2)
        page = self.client.get(self.url())
        self.assertContains(page, "Policy revision 2")
        self.assertContains(page, "Editor requests")

    def test_confirmation_validation_and_csrf(self):
        self.assertEqual(self.save(confirm="").status_code, 400)
        self.assertEqual(self.save(synthetic="").status_code, 400)
        self.assertEqual(self.save(allow_author="", allow_editor="").status_code, 400)
        self.assertEqual(self.save(journal_daily_limit="101").status_code, 400)
        self.assertEqual(self.save(journal_daily_limit="0").status_code, 400)
        self.assertFalse(JournalAIPolicy.objects.exists())
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(self.url(), {"confirm":"yes", "synthetic":"yes"}).status_code, 403)

    def test_inactive_role_journal_and_debug_off_block_management(self):
        self.assertEqual(self.save().status_code, 302)
        Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.AI_MANAGER).update(active=False)
        self.assertEqual(self.client.get(self.url()).status_code, 404)
        self.assertEqual(self.save(revision="1").status_code, 404)
        Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.AI_MANAGER).update(active=True)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(self.url()).status_code, 404)
            self.assertNotContains(self.client.get(reverse("workspace")), self.url())
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_chinese_policy_interface(self):
        self.client.force_login(self.manager)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(self.url())
        self.assertContains(page, "期刊 AI 策略")
        self.assertContains(page, "期刊每 24 小时请求上限")
