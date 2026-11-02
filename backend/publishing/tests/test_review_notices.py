import tempfile
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.models import (ContributorChangeRequest, ContributorResponseRequest, EditorialDecision, Membership,
                               ProductionAssignment, PublicationDraft, ReviewAssignment, ReviewDeadlineEvent,
                               ReviewNotice, ReviewRound, Submission, SubmissionVersion)
from publishing.review_notices import visible_review_notices
from publishing.tests import test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewNoticeTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    def setUp(self):
        super().setUp()
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix="ips-notices-")
        self.addCleanup(directory.cleanup)
        setting = override_settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.submission = self.create_draft()
        self.submit(self.submission)
        self.assignment = self.invite(self.submission)
        self.inbox = reverse("review_notice_inbox")
        self.notice = ReviewNotice.objects.get(assignment=self.assignment, kind="invitation")
        self.read_url = reverse("read_review_notice", args=[self.notice.pk])

    def deadline(self, revision, date=None, reason="Private fictional editorial reason"):
        self.client.force_login(self.editor)
        return self.client.post(reverse("manage_review_deadline", args=[self.assignment.pk]), {
            "due_on":date or (timezone.now().date() + timedelta(days=14)).isoformat(),
            "reason":reason, "revision":revision})

    def test_invitation_notice_is_unique_and_visible_to_its_reviewer(self):
        self.assertIsNone(self.notice.read_at)
        self.assertEqual(self.notice.revision, 0)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("review_invite", args=[self.submission.pk]), {"reviewer":self.reviewer.pk}).status_code, 302)
        self.assertEqual(ReviewNotice.objects.filter(assignment=self.assignment, kind="invitation").count(), 1)
        self.client.force_login(self.reviewer)
        page = self.client.get(self.inbox)
        self.assertContains(page, self.submission.title)
        self.assertContains(page, "Review invitation")
        self.assertEqual(page.context["unread_count"], 1)
        self.assertContains(self.client.get(reverse("workspace")), self.inbox)
        self.assertEqual(self.client.get(self.read_url).status_code, 405)
        self.notice.refresh_from_db()
        self.assertIsNone(self.notice.read_at)

    def test_mark_read_is_private_idempotent_and_requires_csrf(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.reviewer)
        self.assertEqual(csrf.post(self.read_url, {}).status_code, 403)
        for user in (self.author, self.editor, self.coordinator, self.other_reviewer, self.superuser):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(self.inbox), self.submission.title)
            self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 302)
        self.notice.refresh_from_db()
        first_read = self.notice.read_at
        self.assertIsNotNone(first_read)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 302)
        self.notice.refresh_from_db()
        self.assertEqual(self.notice.read_at, first_read)
        self.assertNotContains(self.client.get(self.inbox, {"filter":"unread"}), self.submission.title)
        self.assertContains(self.client.get(self.inbox), self.submission.title)

    def test_deadline_change_notice_is_generic_and_bound_to_revision(self):
        self.editor.username = "fictional-private-editor-416"
        self.editor.save(update_fields=["username"])
        self.assertEqual(self.deadline(0).status_code, 302)
        self.assertEqual(self.deadline(1).status_code, 302)  # Same date is a no-op.
        future = (timezone.now().date() + timedelta(days=20)).isoformat()
        self.assertEqual(self.deadline(1, future).status_code, 302)
        self.assertEqual(self.deadline(1, future).status_code, 409)  # Stale revision.
        self.assertEqual(list(ReviewNotice.objects.filter(kind="deadline").order_by("revision").values_list("revision", flat=True)), [1, 2])
        self.client.force_login(self.reviewer)
        response = self.client.get(self.inbox)
        self.assertEqual(response.context["unread_count"], 3)
        self.assertContains(response, "Review deadline changed", count=2)
        self.assertNotContains(response, "Private fictional editorial reason")
        self.assertNotContains(response, self.editor.username)
        self.assertNotContains(response, self.submission.abstract)
        task = self.client.get(reverse("review_detail", args=[self.assignment.pk]))
        self.assertContains(task, future)
        self.assertNotContains(task, "Private fictional editorial reason")

    def test_permission_removal_hides_notice_and_blocks_marking(self):
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(self.inbox), self.submission.title)
        membership = Membership.objects.get(user=self.reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
        membership.active = False
        membership.save(update_fields=["active"])
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)
        membership.active = True
        membership.save(update_fields=["active"])
        self.journal.active = False
        self.journal.save(update_fields=["active"])
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)
        self.notice.refresh_from_db()
        self.assertIsNone(self.notice.read_at)

    def test_author_response_or_production_role_excludes_notice(self):
        change = ContributorChangeRequest.objects.create(submission=self.submission, requested_by=self.author,
            before=[], proposed=[], reason="Fictional", base_revision=0, acknowledged=True)
        ContributorResponseRequest.objects.create(change=change, side="original", position=0,
            recipient=self.reviewer, assigned_by=self.editor, status="revoked", snapshot_digest="0"*64,
            expires_at=timezone.now())
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)
        ContributorResponseRequest.objects.filter(change=change).delete()
        decision = EditorialDecision.objects.create(submission=self.submission, round=self.assignment.round,
            outcome="accept", author_message="Fictional", decided_by=self.editor)
        draft = PublicationDraft.objects.create(submission=self.submission, decision=decision, created_by=self.editor,
            title="Fictional", abstract="Fictional")
        ProductionAssignment.objects.create(draft=draft, producer=self.reviewer, assigned_by=self.coordinator)
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)

    def test_reassigned_account_does_not_receive_previous_notices(self):
        Membership.objects.create(user=self.other_reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
        self.assignment.reviewer = self.other_reviewer
        self.assignment.save(update_fields=["reviewer"])
        self.client.force_login(self.other_reviewer)
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)

    def test_invalid_round_or_version_cannot_leak_notices(self):
        other_submission = Submission.objects.create(journal=self.journal, author=self.author, title="Other fictional",
            abstract="Fictional", manuscript="fictional.pdf", status=Submission.Status.SUBMITTED)
        other_version = SubmissionVersion.objects.create(submission=other_submission, number=1,
            manuscript="fictional.pdf", uploaded_by=self.author)
        other_round = ReviewRound.objects.create(submission=other_submission, version=other_version, number=1)
        self.assignment.round = other_round
        self.assignment.save(update_fields=["round"])
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)

    def test_deadline_notice_failure_rolls_back_deadline_and_audit_event(self):
        self.client.force_login(self.editor)
        with patch("publishing.review_deadline_views.ReviewNotice.objects.create", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                self.deadline(0)
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.due_on)
        self.assertEqual(self.assignment.deadline_revision, 0)
        self.assertFalse(ReviewDeadlineEvent.objects.filter(assignment=self.assignment).exists())
        self.assertEqual(ReviewNotice.objects.filter(assignment=self.assignment).count(), 1)

    def test_invitation_notice_failure_rolls_back_assignment(self):
        other = get_user_model().objects.create_user("fictional-reviewer-new", password="long-fictional-password")
        Membership.objects.create(user=other, journal=self.journal, role=Membership.Role.REVIEWER)
        self.client.force_login(self.editor)
        with patch("publishing.views.ReviewNotice.objects.create", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("review_invite", args=[self.submission.pk]), {"reviewer":other.pk})
        self.assertFalse(ReviewAssignment.objects.filter(submission=self.submission, reviewer=other).exists())

    def test_database_constraints_and_pagination(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewNotice.objects.create(assignment=self.assignment, recipient=self.reviewer, kind="invitation", revision=0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ReviewNotice.objects.create(assignment=self.assignment, recipient=self.reviewer, kind="deadline", revision=0)
        ReviewNotice.objects.bulk_create([ReviewNotice(assignment=self.assignment, recipient=self.reviewer, kind="deadline", revision=i) for i in range(1, 22)])
        self.client.force_login(self.reviewer)
        page = self.client.get(self.inbox)
        self.assertEqual(page.context["unread_count"], 22)
        self.assertEqual(len(page.context["page"].object_list), 20)
        self.assertEqual(len(self.client.get(self.inbox, {"page":2}).context["page"].object_list), 2)
        self.assertEqual(visible_review_notices(self.other_reviewer).count(), 0)
