import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse

from publishing.models import EditorialAssignment, Membership
from publishing.review_copy import read_review_copy
from publishing.tests import test_workflow as workflow


@override_settings(DEBUG=True)
class ReviewerCopyIntegrityTests(TestCase):
    setUp = workflow.WorkflowTests.setUp
    create_separate_draft = workflow.WorkflowTests.create_separate_draft
    submit = workflow.WorkflowTests.submit
    invite = workflow.WorkflowTests.invite
    confirm_review_copy = workflow.WorkflowTests.confirm_review_copy

    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-review-copy-integrity-")
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def prepared_submission(self):
        submission = self.create_separate_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        return submission

    def test_changed_file_blocks_invites_and_both_downloads_until_rechecked(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]),
            {"response": "accept", "declaration": "none"}).status_code, 302)
        version = submission.versions.get(number=1)
        old_bytes = Path(version.review_copy.path).read_bytes()
        self.assertEqual(b"".join(self.client.get(reverse("review_file", args=[assignment.pk])).streaming_content), old_bytes)

        new_bytes = b"%PDF-1.4\nA changed fictional reviewer copy\n%%EOF"
        Path(version.review_copy.path).write_bytes(new_bytes)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.editor)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertFalse(page.context["review_copy_ready"])
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]),
            {"reviewer": self.reviewer.pk}).status_code, 409)
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 2)
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("review_file", args=[assignment.pk]))
        self.assertEqual(b"".join(response.streaming_content), new_bytes)

    def test_old_page_token_cannot_confirm_replaced_or_invalid_pdf(self):
        submission = self.prepared_submission()
        version = submission.versions.get(number=1)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        token = page.context["review_copy_check_token"]
        Path(version.review_copy.path).write_bytes(b"%PDF-1.4\nDifferent file\n%%EOF")
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        version.refresh_from_db()
        self.assertFalse(version.review_copy_checked_sha256)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 0)
        Path(version.review_copy.path).write_bytes(b"not a PDF")
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        self.assertNotIn("review_copy_check_token", self.client.get(
            reverse("submission_detail", args=[submission.pk])).context)

    def test_legacy_check_requires_reconfirmation(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        version = submission.versions.get(number=1)
        version.review_copy_checked_sha256 = ""
        version.save(update_fields=["review_copy_checked_sha256"])
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertFalse(page.context["review_copy_ready"])
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        version.refresh_from_db()
        self.assertEqual(len(version.review_copy_checked_sha256), 64)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 2)

    def test_token_is_bound_to_assigned_editor_and_requires_current_access(self):
        submission = self.prepared_submission()
        token = self.client.get(reverse("submission_detail", args=[submission.pk])).context["review_copy_check_token"]
        Membership.objects.create(user=self.other_editor, journal=self.journal, role=Membership.Role.EDITOR)
        EditorialAssignment.objects.create(submission=submission, editor=self.other_editor, assigned_by=self.coordinator)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        self.client.force_login(self.editor)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 404)
        self.assertFalse(submission.events.filter(action="review_copy_checked").exists())

    def test_download_returns_the_verified_bytes_even_if_file_changes_after_read(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"})
        version = submission.versions.get(number=1)
        expected = Path(version.review_copy.path).read_bytes()

        def replace_after_read(current_version):
            data = read_review_copy(current_version)
            Path(current_version.review_copy.path).write_bytes(b"%PDF-1.4\nchanged after check\n%%EOF")
            return data

        with patch("publishing.review_copy.read_review_copy", side_effect=replace_after_read):
            response = self.client.get(reverse("review_file", args=[assignment.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), expected)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
