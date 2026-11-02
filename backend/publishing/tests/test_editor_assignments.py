import tempfile
from io import StringIO
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.forms import InviteForm
from publishing.models import (EditorialAssignment, EditorialDecision, Journal, Membership, Organisation, ReviewAssignment,
                               ReviewRound, Submission, SubmissionEvent, SubmissionVersion)


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class EditorialAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user("fictional-author")
        cls.coordinator = users.create_user("fictional-coordinator")
        cls.editor = users.create_user("fictional-editor")
        cls.other_editor = users.create_user("fictional-other-editor")
        cls.reviewer = users.create_user("fictional-reviewer")
        cls.foreign = users.create_user("foreign-editor")
        org = Organisation.objects.create(name="Test publisher", slug="assignment-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Journal A", slug="assignment-a", review_mode="direct")
        cls.other_journal = Journal.objects.create(organisation=org, name="Journal B", slug="assignment-b")
        for user, journal, role in [
            (cls.author, cls.journal, Membership.Role.AUTHOR),
            (cls.coordinator, cls.journal, Membership.Role.COORDINATOR),
            (cls.editor, cls.journal, Membership.Role.EDITOR),
            (cls.other_editor, cls.journal, Membership.Role.EDITOR),
            (cls.reviewer, cls.journal, Membership.Role.REVIEWER),
            (cls.foreign, cls.other_journal, Membership.Role.EDITOR),
        ]:
            Membership.objects.create(user=user, journal=journal, role=role)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author, title="Fictional title",
            abstract="PRIVATE fictional abstract", manuscript="manuscripts/test.pdf", status=Submission.Status.SUBMITTED)
        cls.version = SubmissionVersion.objects.create(submission=cls.submission, number=1,
            manuscript="manuscripts/test.pdf", uploaded_by=cls.author)
        cls.round = ReviewRound.objects.create(submission=cls.submission, version=cls.version, number=1)

    def assign(self, editor=None, coordinator=None):
        self.client.force_login(coordinator or self.coordinator)
        return self.client.post(reverse("submission_editors", args=[self.submission.pk]),
                                {"editor": (editor or self.editor).pk})

    def test_editor_sees_only_assigned_work_and_coordinator_gets_no_content_access(self):
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(reverse("workspace")), self.submission.title)
        for name, args in [("submission_detail", [self.submission.pk]), ("submission_file", [self.submission.pk]),
                           ("checks_page", [self.submission.pk]), ("editorial_decision", [self.submission.pk])]:
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 404)
        self.client.force_login(self.coordinator)
        queue = self.client.get(reverse("coordination_workspace"))
        self.assertContains(queue, self.submission.title)
        self.assertNotContains(queue, self.submission.abstract)
        self.assertEqual(self.client.get(reverse("submission_file", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.assign().status_code, 302)
        self.assertEqual(self.assign().status_code, 302)
        self.assertEqual(EditorialAssignment.objects.count(), 1)
        self.assertEqual(SubmissionEvent.objects.filter(action="editor_assigned").count(), 1)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("workspace")), self.submission.title)
        self.assertContains(self.client.get(reverse("submission_detail", args=[self.submission.pk])), self.submission.abstract)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)

    def test_cross_journal_author_and_reviewers_cannot_be_assigned(self):
        Membership.objects.create(user=self.author, journal=self.journal, role=Membership.Role.EDITOR)
        Membership.objects.create(user=self.reviewer, journal=self.journal, role=Membership.Role.EDITOR)
        ReviewAssignment.objects.create(submission=self.submission, round=self.round,
                                        reviewer=self.reviewer, invited_by=self.editor)
        for user in (self.author, self.reviewer, self.foreign):
            self.assertEqual(self.assign(user).status_code, 400)
        self.assertFalse(EditorialAssignment.objects.exists())
        self.client.force_login(self.foreign)
        self.assertEqual(self.client.get(reverse("submission_editors", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("submission_editors", args=[self.submission.pk]), {"editor":self.foreign.pk}).status_code, 404)

    def test_coordinator_cannot_grant_editorial_access_to_their_own_account(self):
        Membership.objects.create(user=self.coordinator, journal=self.journal, role=Membership.Role.EDITOR)
        self.assertEqual(self.assign(self.coordinator).status_code, 400)
        self.assertFalse(EditorialAssignment.objects.exists())
        self.client.force_login(self.coordinator)
        page = self.client.get(reverse("submission_editors", args=[self.submission.pk]))
        self.assertNotContains(page, f'value="{self.coordinator.pk}"')

    def test_author_reviewer_and_recused_coordinator_cannot_coordinate_that_submission(self):
        for user in (self.author, self.reviewer, self.editor):
            Membership.objects.create(user=user, journal=self.journal, role=Membership.Role.COORDINATOR)
        ReviewAssignment.objects.create(submission=self.submission, round=self.round,
                                        reviewer=self.reviewer, invited_by=self.editor)
        for user in (self.author, self.reviewer):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(reverse("coordination_workspace")), self.submission.title)
            self.assertEqual(self.client.get(reverse("submission_editors", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.assign().status_code, 302)
        assignment = EditorialAssignment.objects.get(editor=self.editor)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("recuse_editor", args=[assignment.pk]), {"confirm":"yes"}).status_code, 302)
        self.assertNotContains(self.client.get(reverse("coordination_workspace")), self.submission.title)
        self.assertEqual(self.client.get(reverse("submission_editors", args=[self.submission.pk])).status_code, 404)

    def test_revocation_and_recusal_stop_access_and_keep_history(self):
        self.assign()
        first = EditorialAssignment.objects.get(editor=self.editor)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("remove_editor_assignment", args=[first.pk])).status_code, 302)
        self.assertEqual(self.client.post(reverse("remove_editor_assignment", args=[first.pk])).status_code, 409)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.assign().status_code, 302)
        current = EditorialAssignment.objects.get(editor=self.editor, status="active")
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("recuse_editor", args=[current.pk]), {}).status_code, 400)
        self.assertEqual(self.client.post(reverse("recuse_editor", args=[current.pk]), {"confirm":"yes"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("checks_page", args=[self.submission.pk])).status_code, 404)
        self.assertEqual(self.assign().status_code, 400)
        self.assertEqual(self.assign(self.other_editor).status_code, 302)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 200)
        self.assertEqual(list(EditorialAssignment.objects.filter(editor=self.editor).order_by("pk").values_list("status", flat=True)), ["revoked", "recused"])

    def test_role_revocation_and_disabling_journal_end_editor_access(self):
        self.assign()
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=True)
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("submission_editors", args=[self.submission.pk]), {"editor":self.other_editor.pk}).status_code, 404)

    def test_prior_editor_is_excluded_from_review_even_after_role_removed(self):
        self.assign()
        old = EditorialAssignment.objects.get(editor=self.editor)
        self.client.force_login(self.coordinator)
        self.client.post(reverse("remove_editor_assignment", args=[old.pk]))
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).delete()
        Membership.objects.create(user=self.editor, journal=self.journal, role=Membership.Role.REVIEWER)
        form = InviteForm(submission=self.submission, actor=self.other_editor)
        self.assertNotIn(self.editor, form.fields["reviewer"].queryset)

    def test_csrf_and_method_restrictions(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coordinator)
        self.assertEqual(client.post(reverse("submission_editors", args=[self.submission.pk]), {"editor":self.editor.pk}).status_code, 403)
        self.assign()
        assignment = EditorialAssignment.objects.get(editor=self.editor)
        self.assertEqual(client.post(reverse("remove_editor_assignment", args=[assignment.pk])).status_code, 403)
        client.force_login(self.editor)
        self.assertEqual(client.post(reverse("recuse_editor", args=[assignment.pk]), {"confirm":"yes"}).status_code, 403)
        self.assertEqual(client.get(reverse("recuse_editor", args=[assignment.pk])).status_code, 405)
        self.assertEqual(EditorialAssignment.objects.get(pk=assignment.pk).status, "active")

    def test_web_assignment_to_review_and_decision_end_to_end(self):
        with tempfile.TemporaryDirectory(prefix="ips-editor-workflow-") as media:
            with override_settings(MEDIA_ROOT=media):
                version = SubmissionVersion.objects.get(pk=self.version.pk)
                version.manuscript.save("fictional.pdf", ContentFile(b"%PDF-1.4\nfictional\n%%EOF"), save=True)
                self.assertEqual(self.assign().status_code, 302)
                self.client.force_login(self.editor)
                self.assertEqual(self.client.post(reverse("review_invite", args=[self.submission.pk]), {"reviewer":self.reviewer.pk}).status_code, 302)
                review = ReviewAssignment.objects.get(submission=self.submission, reviewer=self.reviewer)
                self.client.force_login(self.reviewer)
                self.assertEqual(self.client.post(reverse("review_respond", args=[review.pk]), {"response":"accept", "declaration":"none"}).status_code, 302)
                self.assertEqual(self.client.post(reverse("review_report", args=[review.pk]), {"recommendation":"accept", "comments":"Fictional confidential review"}).status_code, 302)
                self.client.force_login(self.editor)
                self.assertEqual(self.client.post(reverse("editorial_decision", args=[self.submission.pk]), {"outcome":"accept", "author_message":"Fictional decision"}).status_code, 302)
                decision = EditorialDecision.objects.get(submission=self.submission)
                assignment = EditorialAssignment.objects.get(submission=self.submission, editor=self.editor)
                self.assertEqual(self.client.post(reverse("recuse_editor", args=[assignment.pk]), {"confirm":"yes"}).status_code, 302)
                decision.refresh_from_db()
                self.assertEqual(decision.outcome, "accept")
                self.assertEqual(Submission.objects.get(pk=self.submission.pk).status, Submission.Status.ACCEPTED)

    def test_coordination_and_recusal_pages_support_simplified_chinese(self):
        self.client.force_login(self.coordinator)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(reverse("coordination_workspace")), "管理编辑分配")
        self.assertContains(self.client.get(reverse("submission_editors", args=[self.submission.pk])), "分配编辑")
        self.assign()
        self.client.force_login(self.editor)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(reverse("submission_detail", args=[self.submission.pk])), "编辑回避")

    @override_settings(DEBUG=True)
    def test_seed_creates_a_coordinator_for_the_fictional_journal(self):
        output = StringIO()
        call_command("seed_demo", stdout=output)
        journal = Journal.objects.get(name="AIRES (fictional test)")
        self.assertTrue(Membership.objects.filter(journal=journal, role=Membership.Role.COORDINATOR, active=True).exists())
        self.assertTrue(Membership.objects.filter(journal=journal, role=Membership.Role.PRODUCER, active=True).exists())
        self.assertTrue(Membership.objects.filter(journal=journal, role=Membership.Role.SITE_MANAGER, active=True).exists())
