import tempfile
from datetime import datetime, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from publishing.models import (AuthorNotice, ContributorChangeRequest, EditorialDecision, Membership, ReviewAssignment,
                               ReviewRound, Submission, SubmissionVersion)
from publishing.tests import test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthorNoticeTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    def setUp(self):
        super().setUp()
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix="ips-author-notices-")
        self.addCleanup(directory.cleanup)
        setting = override_settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.submission = self.create_draft()
        self.assertFalse(AuthorNotice.objects.exists())
        self.submit(self.submission)
        self.notice = AuthorNotice.objects.get(submission=self.submission, kind="submitted")
        self.inbox = reverse("author_notice_inbox")
        self.read_url = reverse("read_author_notice", args=[self.notice.pk])

    def test_submission_notice_is_private_unique_and_linked_to_owner(self):
        self.assertEqual(self.notice.recipient, self.author)
        self.assertIsNone(self.notice.read_at)
        self.client.force_login(self.author)
        page = self.client.get(self.inbox)
        self.assertContains(page, self.submission.title)
        self.assertContains(page, "Submission received")
        self.assertNotContains(page, self.submission.abstract)
        self.assertEqual(page.context["unread_count"], 1)
        self.assertContains(self.client.get(reverse("workspace")), self.inbox)
        self.assertEqual(self.client.post(reverse("submission_submit", args=[self.submission.pk])).status_code, 409)
        self.assertEqual(AuthorNotice.objects.filter(kind="submitted").count(), 1)
        for user in (self.editor, self.reviewer, self.coordinator, self.other_editor, self.superuser):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(self.inbox), self.submission.title)
            self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)

    def test_unread_filter_read_state_and_csrf(self):
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.read_url, {}).status_code, 403)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(self.read_url).status_code, 405)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 302)
        self.notice.refresh_from_db()
        first = self.notice.read_at
        self.assertIsNotNone(first)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 302)
        self.notice.refresh_from_db()
        self.assertEqual(first, self.notice.read_at)
        self.assertNotContains(self.client.get(self.inbox, {"filter":"unread"}), self.submission.title)
        self.assertContains(self.client.get(self.inbox), self.submission.title)

    def test_editorial_decision_notice_excludes_review_identity_and_contents(self):
        assignment = self.invite(self.submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]),
            {"response":"accept", "declaration":"none"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]),
            {"recommendation":"minor", "comments":"Private review text 7654."}).status_code, 302)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("editorial_decision", args=[self.submission.pk]),
            {"outcome":"minor", "author_message":"A fictional decision for the author."}).status_code, 302)
        decision = EditorialDecision.objects.get(submission=self.submission)
        self.assertTrue(AuthorNotice.objects.filter(kind="decision", decision=decision, recipient=self.author).exists())
        self.client.force_login(self.author)
        page = self.client.get(self.inbox)
        self.assertEqual(page.context["unread_count"], 2)
        self.assertContains(page, "Editorial decision recorded")
        self.assertNotContains(page, "Private review text 7654.")
        self.assertNotContains(page, self.reviewer.username)
        self.assertNotContains(page, "A fictional decision for the author.")
        self.assertContains(self.client.get(reverse("submission_detail", args=[self.submission.pk])), "A fictional decision for the author.")

    def test_author_change_decision_generates_notice_without_editor_reason(self):
        change = ContributorChangeRequest.objects.create(submission=self.submission, requested_by=self.author,
            reason="Fictional request", before=[], proposed=[], base_revision=0, acknowledged=True)
        self.client.force_login(self.editor)
        detail = reverse("contributor_change_detail", args=[self.submission.pk, change.pk])
        self.assertEqual(self.client.post(detail, {"outcome":"declined", "note":"Fictional confidential editor note"}).status_code, 302)
        self.assertTrue(AuthorNotice.objects.filter(kind="author_change", author_change=change, recipient=self.author).exists())
        self.client.force_login(self.author)
        page = self.client.get(self.inbox)
        self.assertContains(page, "Author change reviewed")
        self.assertNotContains(page, "Fictional confidential editor note")

    def test_removed_author_role_does_not_revoke_owners_access_but_journal_deactivation_does(self):
        Membership.objects.filter(user=self.author, journal=self.journal, role=Membership.Role.AUTHOR).update(active=False)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(self.inbox), self.submission.title)
        self.journal.active = False
        self.journal.save(update_fields=["active"])
        self.assertNotContains(self.client.get(self.inbox), self.submission.title)
        self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)

    def test_reassigned_submission_does_not_transfer_existing_notices(self):
        new_owner = get_user_model().objects.create_user("fictional-new-submitter", password="long-fictional-password")
        self.submission.author = new_owner
        self.submission.save(update_fields=["author"])
        for user in (self.author, new_owner):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(self.inbox), self.submission.title)
            self.assertEqual(self.client.post(self.read_url, {}).status_code, 404)

    def test_inconsistent_source_is_hidden_and_constraints_prevent_wrong_kind(self):
        other = Submission.objects.create(journal=self.other, author=self.author, title="Other test",
            abstract="Fictional", manuscript="fictional.pdf", status=Submission.Status.SUBMITTED)
        version = SubmissionVersion.objects.create(submission=other, number=1, manuscript="fictional.pdf", uploaded_by=self.author)
        review_round = ReviewRound.objects.create(submission=other, version=version, number=1)
        decision = EditorialDecision.objects.create(submission=other, round=review_round, outcome="reject",
            author_message="Fictional", decided_by=self.other_editor)
        invalid = AuthorNotice.objects.create(submission=self.submission, recipient=self.author, kind="decision", decision=decision)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("read_author_notice", args=[invalid.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.inbox).context["unread_count"], 1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthorNotice.objects.create(submission=self.submission, recipient=self.author, kind="submitted", decision=decision)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthorNotice.objects.create(submission=self.submission, recipient=self.author, kind="submitted")

    def test_decision_notification_failure_rolls_back_decision_and_round(self):
        self.client.force_login(self.editor)
        with patch("publishing.views.AuthorNotice.objects.create", side_effect=RuntimeError("test")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("editorial_decision", args=[self.submission.pk]),
                    {"outcome":"reject", "author_message":"Fictional"})
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status, Submission.Status.SUBMITTED)
        self.assertFalse(EditorialDecision.objects.filter(submission=self.submission).exists())
        self.assertEqual(self.submission.rounds.get(number=1).status, ReviewRound.Status.OPEN)

    def test_submission_notification_failure_rolls_back_submission(self):
        draft = Submission.objects.create(journal=self.journal, author=self.author, title="Fictional draft",
            abstract="Fictional", manuscript="fictional.pdf", status=Submission.Status.DRAFT)
        SubmissionVersion.objects.create(submission=draft, number=1, manuscript="fictional.pdf", uploaded_by=self.author)
        self.client.force_login(self.author)
        fake_file = {"path":"fictional.pdf", "size":12, "sha256":"a" * 64}
        with patch("publishing.submission_checklist._fingerprint", return_value=fake_file), patch("publishing.views.AuthorNotice.objects.create", side_effect=RuntimeError("test")):
            page = self.client.get(reverse("submission_checklist", args=[draft.pk]))
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("submission_submit", args=[draft.pk]),
                    {"review_token":page.context["review_token"], "confirm":"yes"})
        draft.refresh_from_db()
        self.assertEqual(draft.status, Submission.Status.DRAFT)
        self.assertFalse(draft.rounds.exists())
        self.assertFalse(AuthorNotice.objects.filter(submission=draft).exists())

    def test_pagination_and_default_english_chinese_translation(self):
        changes = [ContributorChangeRequest(submission=self.submission, requested_by=self.author, reason="Fictional",
            before=[], proposed=[], base_revision=0, acknowledged=True, status="declined") for _ in range(21)]
        ContributorChangeRequest.objects.bulk_create(changes)
        AuthorNotice.objects.bulk_create([AuthorNotice(submission=self.submission, recipient=self.author,
            kind="author_change", author_change=change) for change in changes])
        self.client.force_login(self.author)
        with translation.override("en"):
            page = self.client.get(self.inbox)
        self.assertContains(page, "Submission notifications")
        self.assertEqual(page.context["unread_count"], 22)
        self.assertEqual(len(page.context["page"].object_list), 20)
        self.assertEqual(len(self.client.get(self.inbox, {"page":2}).context["page"].object_list), 2)
        # Existing Chinese interface remains available after the new strings are translated.
        with translation.override("zh-hans"):
            page = self.client.get(self.inbox, HTTP_ACCEPT_LANGUAGE="zh-hans")
        self.assertContains(page, "投稿通知")

    def test_notice_timestamps_are_always_labelled_utc(self):
        instant = datetime(2026, 11, 2, 23, 30, tzinfo=datetime_timezone.utc)
        AuthorNotice.objects.filter(pk=self.notice.pk).update(created_at=instant)
        self.client.force_login(self.author)
        with timezone.override("Asia/Shanghai"):
            page = self.client.get(self.inbox)
        self.assertContains(page, "2026-11-02 23:30 UTC")
