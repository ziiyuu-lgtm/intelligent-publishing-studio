import shutil
import tempfile
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from unittest.mock import patch
from pathlib import Path
from publishing.models import (ConflictDeclaration, EditorialAssignment, EditorialDecision, Journal, Membership, Organisation, ReviewAssignment,
                               ReviewRound, Submission, SubmissionEvent, SubmissionVersion)

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
        self.coordinator = users.create_user("coordinator", password="a-long-synthetic-password")
        self.reviewer = users.create_user("fictional-reviewer-identity-824", password="a-long-synthetic-password")
        self.other_editor = users.create_user("other-editor", password="a-long-synthetic-password")
        self.other_reviewer = users.create_user("other-reviewer", password="a-long-synthetic-password")
        self.superuser = users.create_superuser("administrator", password="a-long-synthetic-password")
        organisation = Organisation.objects.create(name="Test group", slug="test-group")
        self.journal = Journal.objects.create(organisation=organisation, name="Journal A", slug="journal-a", review_mode=Journal.ReviewMode.DIRECT)
        self.other = Journal.objects.create(organisation=organisation, name="Journal B", slug="journal-b", review_mode=Journal.ReviewMode.DIRECT)
        for user, journal, role in [
            (self.author, self.journal, Membership.Role.AUTHOR),
            (self.editor, self.journal, Membership.Role.EDITOR),
            (self.coordinator, self.journal, Membership.Role.COORDINATOR),
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
        EditorialAssignment.objects.create(submission=submission, editor=self.editor, assigned_by=self.coordinator)

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
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"}).status_code, 302)
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
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"}).status_code, 404)

    def test_role_revocation_immediately_blocks_review_access(self):
        submission = self.create_draft()
        self.submit(submission)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"})
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
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"}).status_code, 409)

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

    def finish_review(self, assignment, report="Confidential report for the editor"):
        self.client.force_login(assignment.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]), {"recommendation": "major", "comments": report}).status_code, 302)

    def decide(self, submission, outcome="major", message="Please revise the synthetic manuscript."):
        self.client.force_login(self.editor)
        return self.client.post(reverse("editorial_decision", args=[submission.pk]), {"outcome": outcome, "author_message": message})

    def revise(self, submission, content=b"%PDF-1.4\n% revised fictional paper\n%%EOF\n", response="We addressed the editor's request."):
        self.client.force_login(self.author)
        return self.client.post(reverse("submission_revision", args=[submission.pk]), {
            "manuscript": SimpleUploadedFile("revision.pdf", content, content_type="application/pdf"),
            "author_response": response,
        })

    def test_two_rounds_preserve_versions_and_scope_reviewer_access(self):
        submission = self.create_draft()
        self.submit(submission)
        first = self.invite(submission)
        self.finish_review(first, "First confidential review")
        self.assertEqual(self.decide(submission, "major", "Please revise section two.").status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.REVISION_REQUESTED)
        self.assertEqual(ReviewRound.objects.get(submission=submission, number=1).status, ReviewRound.Status.CLOSED)
        self.client.force_login(self.author)
        author_page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertContains(author_page, "Please revise section two.")
        self.assertNotContains(author_page, "First confidential review")
        self.assertNotContains(author_page, self.reviewer.username)
        revised_pdf = b"%PDF-1.4\n% different version\n%%EOF\n"
        self.assertEqual(self.revise(submission, revised_pdf).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.SUBMITTED)
        self.assertEqual(list(submission.versions.values_list("number", flat=True)), [1, 2])
        first_version = submission.versions.get(number=1)
        second_version = submission.versions.get(number=2)
        self.assertNotEqual(first_version.manuscript.name, second_version.manuscript.name)
        with first_version.manuscript.open("rb") as original:
            self.assertEqual(original.read(), PDF)
        self.assertEqual(submission.rounds.get(number=2).version_id, second_version.pk)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("review_file", args=[first.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_version_file", args=[submission.pk, 1])).status_code, 404)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.reviewer.pk}).status_code, 302)
        second = ReviewAssignment.objects.get(submission=submission, reviewer=self.reviewer, round__number=2)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("review_file", args=[second.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_respond", args=[second.pk]), {"response": "accept", "declaration": "none"}).status_code, 302)
        revised_response = self.client.get(reverse("review_file", args=[second.pk]))
        self.assertEqual(b"".join(revised_response.streaming_content), revised_pdf)
        self.assertEqual(self.client.post(reverse("review_report", args=[second.pk]), {"recommendation": "accept", "comments": "Second confidential review"}).status_code, 302)
        self.assertEqual(self.decide(submission, "accept", "Accepted after review.").status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.ACCEPTED)
        self.assertEqual(EditorialDecision.objects.filter(submission=submission).count(), 2)
        self.client.force_login(self.author)
        author_page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertContains(author_page, "Accepted after review.")
        self.assertNotContains(author_page, "Second confidential review")
        self.assertEqual(self.client.get(reverse("submission_revision", args=[submission.pk])).status_code, 409)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("editorial_decision", args=[submission.pk]), {"outcome":"reject", "author_message":"Duplicate"}).status_code, 409)

    def test_decision_requires_review_except_for_desk_rejection(self):
        submission = self.create_draft()
        self.submit(submission)
        for outcome in ["accept", "minor", "major"]:
            result = self.decide(submission, outcome)
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.context["form"].errors)
        self.assertFalse(EditorialDecision.objects.filter(submission=submission).exists())
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.post(reverse("editorial_decision", args=[submission.pk]), {"outcome":"reject", "author_message":"No"}).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("editorial_decision", args=[submission.pk]), {"outcome":"reject", "author_message":"No"}).status_code, 404)
        self.assertEqual(self.decide(submission, "reject", "The submission is outside scope.").status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.REJECTED)
        self.assertEqual(submission.rounds.get(number=1).status, ReviewRound.Status.CLOSED)

    def test_closing_round_freezes_unfinished_reviews(self):
        extra_reviewer = get_user_model().objects.create_user("reviewer-two", password="a-long-synthetic-password")
        Membership.objects.create(user=extra_reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
        submission = self.create_draft()
        self.submit(submission)
        first = self.invite(submission)
        self.finish_review(first)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": extra_reviewer.pk})
        second = ReviewAssignment.objects.get(submission=submission, reviewer=extra_reviewer)
        self.client.force_login(extra_reviewer)
        self.client.post(reverse("review_respond", args=[second.pk]), {"response": "accept", "declaration": "none"})
        self.assertEqual(self.decide(submission).status_code, 302)
        self.client.force_login(extra_reviewer)
        self.assertEqual(self.client.get(reverse("review_file", args=[second.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_report", args=[second.pk]), {"recommendation":"accept", "comments":"Too late"}).status_code, 409)
        self.assertEqual(self.client.post(reverse("review_respond", args=[second.pk]), {"response":"accept", "declaration":"none"}).status_code, 409)
        second.refresh_from_db()
        self.assertEqual(second.status, ReviewAssignment.Status.ACCEPTED)

    def test_revision_requires_valid_pdf_response_and_author(self):
        submission = self.create_draft()
        self.submit(submission)
        review = self.invite(submission)
        self.finish_review(review)
        self.decide(submission)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(reverse("submission_revision", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.revise(submission, b"not a PDF").status_code, 200)
        self.assertEqual(self.revise(submission, response="  ").status_code, 200)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.REVISION_REQUESTED)
        self.assertEqual(submission.versions.count(), 1)
        self.assertEqual(self.revise(submission).status_code, 302)
        self.assertEqual(self.revise(submission).status_code, 409)
        self.assertEqual(submission.versions.count(), 2)
        self.assertEqual(submission.events.filter(action="revision_submitted").count(), 1)

    def test_failed_revision_transaction_removes_uploaded_orphan(self):
        submission = self.create_draft()
        self.submit(submission)
        review = self.invite(submission)
        self.finish_review(review)
        self.decide(submission)
        before = set(Path(self.media_dir).rglob("*.pdf"))
        with patch("publishing.views.ReviewRound.objects.create", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.revise(submission)
        self.assertEqual(set(Path(self.media_dir).rglob("*.pdf")), before)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.REVISION_REQUESTED)
        self.assertEqual(submission.versions.count(), 1)

    def test_assigned_reviewer_cannot_act_as_editor_for_same_submission(self):
        submission = self.create_draft()
        self.submit(submission)
        review = self.invite(submission)
        Membership.objects.create(user=self.reviewer, journal=self.journal, role=Membership.Role.EDITOR)
        self.finish_review(review)
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(reverse("workspace")), "Editorial decision")
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("editorial_decision", args=[submission.pk]), {"outcome":"reject", "author_message":"Conflict"}).status_code, 404)

    def test_csrf_blocks_editor_decision_and_revision(self):
        submission = self.create_draft()
        self.submit(submission)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)
        self.assertEqual(client.post(reverse("editorial_decision", args=[submission.pk]), {"outcome":"reject", "author_message":"No"}).status_code, 403)
        client.force_login(self.author)
        self.assertEqual(client.post(reverse("submission_revision", args=[submission.pk]), {}).status_code, 403)
        self.assertFalse(EditorialDecision.objects.filter(submission=submission).exists())

    def test_prior_editor_cannot_be_invited_as_reviewer_in_later_round(self):
        submission = self.create_draft()
        self.submit(submission)
        review = self.invite(submission)
        self.finish_review(review)
        self.decide(submission)
        self.revise(submission)
        Membership.objects.create(user=self.editor, journal=self.journal, role=Membership.Role.REVIEWER)
        second_editor = get_user_model().objects.create_user("second-editor", password="a-long-synthetic-password")
        Membership.objects.create(user=second_editor, journal=self.journal, role=Membership.Role.EDITOR)
        EditorialAssignment.objects.create(submission=submission, editor=second_editor, assigned_by=self.coordinator)
        self.client.force_login(second_editor)
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer": self.editor.pk}).status_code, 302)
        self.assertFalse(ReviewAssignment.objects.filter(submission=submission, reviewer=self.editor).exists())

    def test_new_decision_form_is_available_in_simplified_chinese(self):
        submission = self.create_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        self.client.post(reverse("set_language"), {"language": "zh-hans", "next": "/"})
        response = self.client.get(reverse("editorial_decision", args=[submission.pk]))
        self.assertContains(response, "记录编辑决定")
        self.assertContains(response, "给作者的说明")

    def create_separate_draft(self):
        self.journal.review_mode = Journal.ReviewMode.SEPARATE
        self.journal.save(update_fields=["review_mode"])
        self.client.force_login(self.author)
        result = self.client.post(reverse("submission_create", args=[self.journal.slug]), {
            "title": "Fictional separate-copy paper", "abstract": "Fictional abstract.",
            "manuscript": SimpleUploadedFile("full.pdf", b"%PDF-1.4\nFull author version\n%%EOF"),
            "review_copy": SimpleUploadedFile("review.pdf", b"%PDF-1.4\nReviewer copy\n%%EOF"),
            "review_copy_confirmed": "on",
        })
        self.assertEqual(result.status_code, 302)
        return Submission.objects.get()

    def test_separate_copy_requires_checks_and_never_returns_author_file_to_reviewer(self):
        self.journal.review_mode = Journal.ReviewMode.SEPARATE
        self.journal.save(update_fields=["review_mode"])
        self.client.force_login(self.author)
        missing = self.client.post(reverse("submission_create", args=[self.journal.slug]), {
            "title":"Missing copy", "abstract":"Fictional", "manuscript":SimpleUploadedFile("file.pdf", PDF),
        })
        self.assertEqual(missing.status_code, 200)
        self.assertIn("review_copy", missing.context["form"].errors)
        self.assertIn("review_copy_confirmed", missing.context["form"].errors)
        self.assertFalse(Submission.objects.exists())
        invalid = self.client.post(reverse("submission_create", args=[self.journal.slug]), {
            "title":"Invalid copy", "abstract":"Fictional", "manuscript":SimpleUploadedFile("file.pdf", PDF),
            "review_copy":SimpleUploadedFile("review.pdf", b"not a PDF"), "review_copy_confirmed":"on",
        })
        self.assertEqual(invalid.status_code, 200)
        self.assertIn("review_copy", invalid.context["form"].errors)
        self.assertFalse(Submission.objects.exists())
        submission = self.create_separate_draft()
        self.journal.review_mode = Journal.ReviewMode.DIRECT
        self.journal.save(update_fields=["review_mode"])
        submission.refresh_from_db()
        self.assertEqual(submission.review_mode, Journal.ReviewMode.SEPARATE)
        self.submit(submission)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer":self.reviewer.pk}).status_code, 409)
        original = self.client.get(reverse("submission_version_file", args=[submission.pk, 1]))
        self.assertIn(b"Full author version", b"".join(original.streaming_content))
        copy = self.client.get(reverse("submission_review_copy_file", args=[submission.pk, 1]))
        self.assertIn(b"Reviewer copy", b"".join(copy.streaming_content))
        self.assertEqual(self.client.post(reverse("review_copy_screen", args=[submission.pk]), {}).status_code, 400)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"}).status_code, 404)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"}).status_code, 404)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"}).status_code, 302)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 1)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response":"accept"}).status_code, 400)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response":"accept", "declaration":"none"}).status_code, 302)
        for name, args in [("review_file", [assignment.pk]), ("submission_file", [submission.pk])]:
            reply = self.client.get(reverse(name, args=args))
            content = b"".join(reply.streaming_content)
            self.assertIn(b"Reviewer copy", content)
            self.assertNotIn(b"Full author version", content)
        self.assertEqual(self.client.get(reverse("submission_version_file", args=[submission.pk, 1])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_review_copy_file", args=[submission.pk, 1])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response":"conflict", "conflict_details":"A fictional collaboration"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]), {"recommendation":"accept", "comments":"test"}).status_code, 409)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, ReviewAssignment.Status.CONFLICT)
        self.assertEqual(list(assignment.conflict_history.values_list("choice", flat=True)), ["none", "yes"])
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("submission_detail", args=[submission.pk])), "A fictional collaboration")
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(reverse("submission_detail", args=[submission.pk])), "A fictional collaboration")

    def test_each_separate_revision_needs_fresh_file_and_editor_check(self):
        submission = self.create_separate_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"})
        assignment = self.invite(submission)
        self.finish_review(assignment)
        self.decide(submission)
        self.assertEqual(self.revise(submission).status_code, 200)
        self.assertEqual(submission.versions.count(), 1)
        self.client.force_login(self.author)
        response = self.client.post(reverse("submission_revision", args=[submission.pk]), {
            "manuscript":SimpleUploadedFile("full2.pdf", b"%PDF-1.4\nFull v2\n%%EOF"),
            "review_copy":SimpleUploadedFile("review2.pdf", b"%PDF-1.4\nReviewer v2\n%%EOF"),
            "review_copy_confirmed":"on", "author_response":"Fictional response",
        })
        self.assertEqual(response.status_code, 302)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer":self.reviewer.pk}).status_code, 409)
        self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"})
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer":self.reviewer.pk})
        second = ReviewAssignment.objects.get(submission=submission, reviewer=self.reviewer, round__number=2)
        self.client.force_login(self.reviewer)
        self.client.post(reverse("review_respond", args=[second.pk]), {"response":"accept", "declaration":"none"})
        version = submission.versions.get(number=2)
        version.review_copy.storage.delete(version.review_copy.name)
        self.assertEqual(self.client.get(reverse("review_file", args=[second.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.author)
        author_response = self.client.get(reverse("submission_version_file", args=[submission.pk, 2]))
        self.assertIn(b"Full v2", b"".join(author_response.streaming_content))

    def test_conflict_after_report_prevents_counting_but_does_not_erase_history(self):
        submission = self.create_draft()
        self.submit(submission)
        first = self.invite(submission)
        self.finish_review(first, "Previously submitted report")
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[first.pk]), {"response":"conflict", "conflict_details":"Fictional financial relationship"}).status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.status, ReviewAssignment.Status.SUBMITTED)
        self.assertEqual(first.conflict, ReviewAssignment.Conflict.YES)
        self.assertEqual(first.comments, "Previously submitted report")
        self.assertEqual(self.decide(submission, "accept").status_code, 200)
        self.assertFalse(EditorialDecision.objects.filter(submission=submission).exists())
        second_reviewer = get_user_model().objects.create_user("reviewer-other", password="a-long-synthetic-password")
        Membership.objects.create(user=second_reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer":second_reviewer.pk})
        second = ReviewAssignment.objects.get(submission=submission, reviewer=second_reviewer)
        self.finish_review(second, "Second report")
        self.assertEqual(self.decide(submission, "accept").status_code, 302)
        self.client.force_login(second_reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[second.pk]), {"response":"conflict", "conflict_details":"Newly discovered relationship"}).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.ACCEPTED)
        self.assertEqual(EditorialDecision.objects.filter(submission=submission).count(), 1)
        self.client.force_login(self.editor)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertContains(page, "Newly discovered relationship")
        self.assertContains(page, "The editor must assess it")
        self.assertContains(self.client.get(reverse("workspace")), "Conflict declared")
        self.client.force_login(self.author)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertNotContains(page, "Newly discovered relationship")
        self.assertNotContains(page, "Fictional financial relationship")

    def test_legacy_accepted_review_requires_explicit_declaration(self):
        submission = self.create_draft()
        self.submit(submission)
        assignment = self.invite(submission)
        ReviewAssignment.objects.filter(pk=assignment.pk).update(status=ReviewAssignment.Status.ACCEPTED, conflict=ReviewAssignment.Conflict.UNKNOWN)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]), {"recommendation":"accept", "comments":"no declaration"}).status_code, 409)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response":"confirm_no_conflict"}).status_code, 400)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]), {"response":"confirm_no_conflict", "declaration":"none"}).status_code, 302)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 200)
        self.assertTrue(ConflictDeclaration.objects.filter(assignment=assignment, choice="none").exists())

    def test_current_or_recorded_journal_editor_cannot_be_invited_as_reviewer(self):
        Membership.objects.create(user=self.reviewer, journal=self.journal, role=Membership.Role.EDITOR, active=False)
        submission = self.create_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer":self.reviewer.pk})
        self.assertFalse(ReviewAssignment.objects.filter(submission=submission, reviewer=self.reviewer).exists())

    def test_failed_separate_draft_removes_both_uploaded_files(self):
        self.journal.review_mode = Journal.ReviewMode.SEPARATE
        self.journal.save(update_fields=["review_mode"])
        self.client.force_login(self.author)
        before = set(Path(self.media_dir).rglob("*.pdf"))
        with patch("publishing.views._event", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("submission_create", args=[self.journal.slug]), {
                    "title":"Test", "abstract":"Test", "manuscript":SimpleUploadedFile("full.pdf", PDF),
                    "review_copy":SimpleUploadedFile("review.pdf", PDF), "review_copy_confirmed":"on",
                })
        self.assertEqual(set(Path(self.media_dir).rglob("*.pdf")), before)
        self.assertFalse(Submission.objects.exists())

    def test_csrf_protects_manual_screening_and_conflict_declaration(self):
        submission = self.create_separate_draft()
        self.submit(submission)
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.editor)
        self.assertEqual(client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"}).status_code, 403)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"})
        assignment = self.invite(submission)
        client.force_login(self.reviewer)
        self.assertEqual(client.post(reverse("review_respond", args=[assignment.pk]), {"response":"conflict", "conflict_details":"test"}).status_code, 403)
        assignment.refresh_from_db()
        self.assertEqual(assignment.conflict, ReviewAssignment.Conflict.UNKNOWN)
        self.assertFalse(assignment.conflict_history.exists())

    def test_failed_separate_revision_removes_both_uploaded_files(self):
        submission = self.create_separate_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_copy_screen", args=[submission.pk]), {"confirmed":"yes"})
        assignment = self.invite(submission)
        self.finish_review(assignment)
        self.decide(submission)
        self.client.force_login(self.author)
        before = set(Path(self.media_dir).rglob("*.pdf"))
        with patch("publishing.views._event", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("submission_revision", args=[submission.pk]), {
                    "manuscript":SimpleUploadedFile("full2.pdf", PDF), "review_copy":SimpleUploadedFile("review2.pdf", PDF),
                    "review_copy_confirmed":"on", "author_response":"fictional response",
                })
        self.assertEqual(set(Path(self.media_dir).rglob("*.pdf")), before)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.REVISION_REQUESTED)
        self.assertEqual(submission.versions.count(), 1)

    def test_separate_copy_form_explains_privacy_limits_in_chinese(self):
        self.journal.review_mode = Journal.ReviewMode.SEPARATE
        self.journal.save(update_fields=["review_mode"])
        self.client.force_login(self.author)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(reverse("submission_create", args=[self.journal.slug]))
        self.assertContains(page, "独立审稿用 PDF")
        self.assertContains(page, "系统不会自动匿名化")

    def test_standard_review_form_warns_author_the_original_file_is_shared(self):
        self.client.force_login(self.author)
        page = self.client.get(reverse("submission_create", args=[self.journal.slug]))
        self.assertContains(page, "uses the author PDF for review")
        self.assertContains(page, "may include identifying information")
