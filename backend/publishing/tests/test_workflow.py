import shutil
import tempfile
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from publishing.models import Journal, Membership, Organisation, ReviewAssignment, Submission, SubmissionEvent

PDF = b"%PDF-1.4\n% fictional test document\n%%EOF\n"


class WorkflowTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-tests-")
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def setUp(self):
        users = get_user_model().objects
        self.author = users.create_user("writer", password="a-long-synthetic-password")
        self.editor = users.create_user("editor", password="a-long-synthetic-password")
        self.reviewer = users.create_user("reviewer", password="a-long-synthetic-password")
        self.other_editor = users.create_user("other-editor", password="a-long-synthetic-password")
        self.other_reviewer = users.create_user("other-reviewer", password="a-long-synthetic-password")
        self.superuser = users.create_superuser("administrator", password="a-long-synthetic-password")
        organisation = Organisation.objects.create(name="Test group", slug="test-group")
        self.journal = Journal.objects.create(organisation=organisation, name="Journal A", slug="journal-a")
        self.other = Journal.objects.create(organisation=organisation, name="Journal B", slug="journal-b")
        for user, journal, role in [
            (self.author, self.journal, Membership.Role.AUTHOR),
            (self.editor, self.journal, Membership.Role.EDITOR),
            (self.reviewer, self.journal, Membership.Role.REVIEWER),
            (self.other_editor, self.other, Membership.Role.EDITOR),
            (self.other_reviewer, self.other, Membership.Role.REVIEWER),
        ]:
            Membership.objects.create(user=user, journal=journal, role=role)

    def create_draft(self, title="An imaginary test manuscript"):
        self.client.force_login(self.author)
        response = self.client.post(reverse("submission_create", args=[self.journal.slug]), {
            "title": title, "abstract": "Fictional abstract.",
            "manuscript": SimpleUploadedFile("test.pdf", PDF, content_type="application/pdf"),
        })
        self.assertEqual(response.status_code, 302)
        return Submission.objects.get()

    def submit(self, submission):
        self.client.force_login(self.author)
        response = self.client.post(reverse("submission_submit", args=[submission.pk]))
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()

    def invite(self, submission):
        self.client.force_login(self.editor)
        response = self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.reviewer.pk})
        self.assertEqual(response.status_code, 302)
        return ReviewAssignment.objects.get(submission=submission, reviewer=self.reviewer)

    def test_full_workflow_protects_drafts_files_and_confidential_reports(self):
        submission = self.create_draft()
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.submit(submission)
        self.assertEqual(submission.status, Submission.Status.SUBMITTED)
        self.assertEqual(self.client.post(reverse("submission_submit", args=[submission.pk])).status_code, 409)
        self.assertEqual(SubmissionEvent.objects.filter(submission=submission, action="submitted").count(), 1)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept"}).status_code, 302)
        file_response = self.client.get(reverse("submission_file", args=[submission.pk]))
        self.assertEqual(file_response.status_code, 200)
        self.assertEqual(b"".join(file_response.streaming_content), PDF)
        self.assertEqual(file_response["X-Content-Type-Options"], "nosniff")
        report = "Confidential test review text"
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]), {"recommendation": "major", "comments": report}).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]), {"recommendation": "reject", "comments": "second"}).status_code, 409)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("submission_detail", args=[submission.pk])), report)
        self.client.force_login(self.author)
        author_page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertNotContains(author_page, report)
        self.assertNotContains(author_page, self.reviewer.username)
        self.assertEqual(self.client.get(reverse("review_detail", args=[assignment.pk])).status_code, 404)

    def test_editor_and_reviewer_cannot_cross_journals(self):
        submission = self.create_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.other_reviewer.pk})
        self.assertFalse(ReviewAssignment.objects.exists())
        self.client.force_login(self.other_editor)
        self.assertNotContains(self.client.get(reverse("workspace")), submission.title)
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.other_reviewer.pk}).status_code, 404)
        assignment = self.invite(submission)
        self.client.force_login(self.other_reviewer)
        self.assertEqual(self.client.get(reverse("review_detail", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept"}).status_code, 404)

    def test_role_revocation_immediately_blocks_review_access(self):
        submission = self.create_draft()
        self.submit(submission)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept"})
        Membership.objects.filter(user=self.reviewer, journal=self.journal).update(active=False)
        self.assertEqual(self.client.get(reverse("review_detail", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)

    def test_author_with_editor_role_cannot_review_own_paper(self):
        Membership.objects.create(user=self.author, journal=self.journal, role=Membership.Role.EDITOR)
        Membership.objects.create(user=self.author, journal=self.journal, role=Membership.Role.REVIEWER)
        submission = self.create_draft()
        self.submit(submission)
        self.assertNotContains(self.client.get(reverse("submission_detail", args=[submission.pk])), "Invite a reviewer")
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.reviewer.pk}).status_code, 404)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.author.pk})
        self.assertFalse(ReviewAssignment.objects.exists())

    def test_validate_pdf_and_prevent_direct_media_access(self):
        self.client.force_login(self.author)
        url = reverse("submission_create", args=[self.journal.slug])
        for name, data in [("file.pdf", b"not a pdf"), ("file.txt", PDF), ("huge.pdf", b"%PDF-" + b"a" * (10 * 1024 * 1024))]:
            response = self.client.post(url, {"title": "Test", "abstract": "Test", "manuscript": SimpleUploadedFile(name, data)})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
        self.assertFalse(Submission.objects.exists())
        submission = self.create_draft()
        self.assertEqual(self.client.get("/private-media-is-not-served/" + submission.manuscript.name).status_code, 404)

    def test_csrf_and_method_guards_protect_mutations(self):
        submission = self.create_draft()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        self.assertEqual(client.post(reverse("submission_submit", args=[submission.pk])).status_code, 403)
        self.assertEqual(client.get(reverse("submission_submit", args=[submission.pk])).status_code, 405)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.DRAFT)

    def test_user_content_is_escaped_in_templates(self):
        submission = self.create_draft(title="<script>alert(1)</script>")
        response = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertNotContains(response, "<script>alert(1)</script>")
        self.assertContains(response, "&lt;script&gt;alert(1)&lt;/script&gt;")

    def test_declined_or_unaccepted_review_has_no_file_access(self):
        submission = self.create_draft()
        self.submit(submission)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]), {"recommendation": "accept", "comments": "Not yet"}).status_code, 409)
        self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "decline"})
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept"}).status_code, 409)

    def test_language_selection_changes_the_workspace_interface(self):
        response = self.client.post(reverse("set_language"), {"language": "zh-hans", "next": "/"})
        self.assertEqual(response.status_code, 302)
        self.assertContains(self.client.get(reverse("home")), "隔离开发环境")

    def test_disabling_a_journal_blocks_its_manuscript_routes(self):
        submission = self.create_draft()
        self.submit(submission)
        self.journal.active = False
        self.journal.save(update_fields=["active"])
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)

    def test_repeated_reviewer_invitation_does_not_duplicate_assignment_or_audit_event(self):
        submission = self.create_draft()
        self.submit(submission)
        self.invite(submission)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.reviewer.pk})
        self.assertEqual(ReviewAssignment.objects.filter(submission=submission).count(), 1)
        self.assertEqual(SubmissionEvent.objects.filter(submission=submission, action="review_invited").count(), 1)

    def test_editor_in_a_different_organisation_has_no_manuscript_access(self):
        other_org = Organisation.objects.create(name="Other publisher", slug="other-publisher")
        external_journal = Journal.objects.create(organisation=other_org, name="Other journal", slug="other-journal")
        external_editor = get_user_model().objects.create_user("external-editor", password="a-long-synthetic-password")
        Membership.objects.create(user=external_editor, journal=external_journal, role=Membership.Role.EDITOR)
        submission = self.create_draft()
        self.submit(submission)
        self.client.force_login(external_editor)
        self.assertNotContains(self.client.get(reverse("workspace")), submission.title)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)

    def test_login_rejects_bad_password_and_logout_requires_post(self):
        login_url = reverse("login")
        response = self.client.post(login_url, {"username": self.author.username, "password": "incorrect"})
        self.assertContains(response, "Sign-in failed")
        self.assertEqual(self.client.get(reverse("workspace")).status_code, 302)
        response = self.client.post(login_url, {"username": self.author.username, "password": "a-long-synthetic-password"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get(reverse("workspace")).status_code, 200)
        self.assertEqual(self.client.get(reverse("logout")).status_code, 405)
        self.assertEqual(self.client.post(reverse("logout")).status_code, 302)
        self.assertEqual(self.client.get(reverse("workspace")).status_code, 302)
