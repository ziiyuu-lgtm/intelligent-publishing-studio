import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.contributor_evidence import MAX_EVIDENCE_BYTES
from publishing.models import ContributorChangeRequest, ContributorConfirmationEvent, ContributorEvidence, Membership, Submission
from publishing.tests import test_contributor_changes, test_contributors, test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ContributorEvidenceTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-evidence-")
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    setUp = test_workflow.WorkflowTests.setUp
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite
    form_data = test_contributors.ContributorTests.form_data
    submitted_with_authors = test_contributor_changes.ContributorChangeTests.submitted_with_authors
    request_data = test_contributor_changes.ContributorChangeTests.request_data
    create_request = test_contributor_changes.ContributorChangeTests.create_request
    detail_url = test_contributor_changes.ContributorChangeTests.detail_url

    def upload(self, change, user, label="Fictional document", content=test_workflow.PDF, name="evidence.pdf"):
        self.client.force_login(user)
        url = reverse("upload_contributor_evidence", args=[change.submission_id, change.pk])
        file = SimpleUploadedFile(name, content, content_type="application/pdf")
        response = self.client.post(url, {"label":label, "file":file, "synthetic":"on"})
        self.assertEqual(response.status_code, 302)
        return ContributorEvidence.objects.filter(change=change, uploaded_by=user).latest("created_at")

    def record_all(self, change, evidence):
        self.client.force_login(self.editor)
        for side, people in [("original", change.before), ("proposed", change.proposed)]:
            for pos in range(len(people)):
                last = ContributorConfirmationEvent.objects.filter(change=change, side=side, position=pos).order_by("-pk").first()
                url = reverse("record_contributor_confirmation", args=[change.submission_id, change.pk, side, pos])
                result = self.client.post(url, {"state":"confirmed", "evidence":str(evidence.pk), "last_event":str(last.pk if last else 0)})
                self.assertEqual(result.status_code, 302)

    def test_private_uploads_and_downloads_are_scoped_and_not_public(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        author_file = self.upload(change, self.author, label="Author evidence")
        self.assertNotIn("evidence.pdf", author_file.file.name)
        editor_file = self.upload(change, self.editor, label="Editor evidence")
        self.client.force_login(self.editor)
        detail = self.client.get(self.detail_url(change))
        self.assertContains(detail, "Author evidence")
        self.assertContains(detail, "Editor evidence")
        url = reverse("download_contributor_evidence", args=[submission.pk, change.pk, editor_file.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), test_workflow.PDF)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("no-store", response["Cache-Control"])
        self.client.force_login(self.author)
        detail = self.client.get(self.detail_url(change))
        self.assertContains(detail, "Author evidence")
        self.assertNotContains(detail, "Editor evidence")
        self.assertEqual(self.client.get(url).status_code, 404)
        own_url = reverse("download_contributor_evidence", args=[submission.pk, change.pk, author_file.pk])
        self.assertEqual(self.client.get(own_url).status_code, 200)
        self.assertEqual(self.client.get("/private-media-is-not-served/" + author_file.file.name).status_code, 404)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(own_url).status_code, 404)
        self.assertEqual(self.client.get(self.detail_url(change)).status_code, 404)
        self.assertNotContains(self.client.get(reverse("review_detail", args=[assignment.pk])), "Author evidence")

    def test_approval_requires_all_checked_entries_and_intact_evidence(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        file = self.upload(change, self.author)
        self.client.force_login(self.editor)
        decision = {"outcome":"approved", "note":"Fictional checks complete.", "verified":"on"}
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 409)
        url = reverse("record_contributor_confirmation", args=[submission.pk, change.pk, "original", 0])
        self.assertEqual(self.client.post(url, {"state":"confirmed", "last_event":"0"}).status_code, 400)
        self.assertEqual(self.client.post(url, {"state":"confirmed", "evidence":str(file.pk), "last_event":"0"}).status_code, 302)
        event = ContributorConfirmationEvent.objects.get(change=change, side="original", position=0)
        self.assertEqual(self.client.post(url, {"state":"disputed", "last_event":"0"}).status_code, 409)
        self.assertEqual(self.client.post(url, {"state":"disputed", "last_event":str(event.pk)}).status_code, 302)
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 409)
        self.record_all(change, file)
        self.assertEqual(ContributorConfirmationEvent.objects.filter(change=change, side="original", position=0).count(), 3)
        Path(file.file.path).write_bytes(b"%PDF-tampered")
        download = reverse("download_contributor_evidence", args=[submission.pk, change.pk, file.pk])
        self.assertEqual(self.client.get(download).status_code, 404)
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 409)
        Path(file.file.path).write_bytes(test_workflow.PDF)
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_config[1]["name"], "Fictional Gamma")

    def test_invalid_files_and_missing_synthetic_declaration_are_rejected(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        self.client.force_login(self.author)
        url = reverse("upload_contributor_evidence", args=[submission.pk, change.pk])
        for name, content in [("not.pdf", b"not-pdf"), ("fiction.txt", test_workflow.PDF),
                              ("too-large.pdf", b"%PDF-" + b"a" * MAX_EVIDENCE_BYTES)]:
            with self.subTest(name=name):
                file = SimpleUploadedFile(name, content, content_type="application/pdf")
                self.assertEqual(self.client.post(url, {"label":"Fictional", "file":file, "synthetic":"on"}).status_code, 400)
        file = SimpleUploadedFile("fiction.pdf", test_workflow.PDF, content_type="application/pdf")
        self.assertEqual(self.client.post(url, {"label":"Fictional", "file":file}).status_code, 400)
        self.assertFalse(ContributorEvidence.objects.exists())

    def test_confirmation_is_editor_only_and_requires_csrf_and_valid_snapshot_position(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        file = self.upload(change, self.author)
        url = reverse("record_contributor_confirmation", args=[submission.pk, change.pk, "proposed", 0])
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("record_contributor_confirmation", args=[submission.pk, change.pk, "other", 0])).status_code, 404)
        self.assertEqual(self.client.get(reverse("record_contributor_confirmation", args=[submission.pk, change.pk, "original", 50])).status_code, 404)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.editor)
        self.assertEqual(csrf_client.post(url, {"state":"confirmed", "evidence":str(file.pk), "last_event":"0"}).status_code, 403)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.assertEqual(self.client.post(url, {"state":"confirmed", "evidence":str(file.pk), "last_event":"0"}).status_code, 404)
        self.assertFalse(ContributorConfirmationEvent.objects.exists())

    def test_evidence_from_another_submission_cannot_be_used(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        other = Submission.objects.create(journal=self.journal, author=self.author, title="Other fictional submission",
                                          abstract="Fictional", manuscript="fake.pdf", status=Submission.Status.SUBMITTED)
        other_change = ContributorChangeRequest.objects.create(submission=other, requested_by=self.author, reason="Fictional",
            before=change.before, proposed=change.proposed, base_revision=0, acknowledged=True)
        other_file = self.upload(other_change, self.author)
        self.client.force_login(self.editor)
        url = reverse("record_contributor_confirmation", args=[submission.pk, change.pk, "original", 0])
        self.assertEqual(self.client.post(url, {"state":"confirmed", "evidence":str(other_file.pk), "last_event":"0"}).status_code, 400)
        wrong_url = reverse("download_contributor_evidence", args=[submission.pk, change.pk, other_file.pk])
        self.assertEqual(self.client.get(wrong_url).status_code, 404)

    def test_upload_rollback_removes_new_file_and_resolved_request_rejects_uploads(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        self.client.force_login(self.author)
        url = reverse("upload_contributor_evidence", args=[submission.pk, change.pk])
        evidence_dir = Path(self.media_dir) / "contributor-evidence"
        before = set(evidence_dir.iterdir()) if evidence_dir.exists() else set()
        file = SimpleUploadedFile("fiction.pdf", test_workflow.PDF, content_type="application/pdf")
        with patch("publishing.contributor_evidence_views._event", side_effect=RuntimeError("test rollback")):
            with self.assertRaises(RuntimeError):
                self.client.post(url, {"label":"Fictional", "file":file, "synthetic":"on"})
        self.assertFalse(ContributorEvidence.objects.exists())
        after = set(evidence_dir.iterdir()) if evidence_dir.exists() else set()
        self.assertEqual(after, before)
        withdraw = reverse("withdraw_contributor_change", args=[submission.pk, change.pk])
        self.assertEqual(self.client.post(withdraw, {"confirm":"yes"}).status_code, 302)
        self.assertEqual(self.client.get(url).status_code, 409)
