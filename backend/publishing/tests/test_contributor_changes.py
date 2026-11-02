import shutil
import tempfile

from django.db import IntegrityError, transaction
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import ContributorChangeRequest, ContributorEvidence, Membership, Submission, SubmissionEvent
from publishing.tests import test_contributors, test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ContributorChangeTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-changes-")
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

    def submitted_with_authors(self):
        submission = self.create_draft()
        self.original = [
            {"name":"Fictional Alpha", "email":"alpha@example.org", "affiliation":"Imaginary Institute", "corresponding":True, "ORDER":1},
            {"name":"Fictional Beta", "email":"beta@example.org", "affiliation":"Imaginary College", "ORDER":2},
        ]
        url = reverse("submission_contributors", args=[submission.pk])
        self.assertEqual(self.client.post(url, self.form_data(self.original)).status_code, 302)
        self.submit(submission)
        return submission

    def request_data(self, entries, initial=2, revision=1, **extra):
        data = self.form_data(entries, initial=initial, revision=revision)
        data.update({"reason":"A fictional update to author details.", "acknowledgement":"on"})
        data.update(extra)
        return data

    def create_request(self, submission):
        self.client.force_login(self.author)
        revised = [dict(p) for p in self.original]
        revised[1]["name"] = "Fictional Gamma"
        response = self.client.post(reverse("request_contributor_change", args=[submission.pk]), self.request_data(revised))
        self.assertEqual(response.status_code, 302)
        return ContributorChangeRequest.objects.get(submission=submission, status=ContributorChangeRequest.Status.PENDING)

    def detail_url(self, change):
        return reverse("contributor_change_detail", args=[change.submission_id, change.pk])

    def upload_and_verify(self, change):
        self.client.force_login(self.author)
        upload = reverse("upload_contributor_evidence", args=[change.submission_id, change.pk])
        document = SimpleUploadedFile("fictional.pdf", test_workflow.PDF, content_type="application/pdf")
        self.assertEqual(self.client.post(upload, {"label":"Fictional collective record", "file":document, "synthetic":"on"}).status_code, 302)
        evidence = ContributorEvidence.objects.filter(change=change).latest("created_at")
        self.client.force_login(self.editor)
        for side, people in [("original", change.before), ("proposed", change.proposed)]:
            for position in range(len(people)):
                url = reverse("record_contributor_confirmation", args=[change.submission_id, change.pk, side, position])
                response = self.client.post(url, {"state":"confirmed", "evidence":str(evidence.pk), "last_event":"0"})
                self.assertEqual(response.status_code, 302)
        return evidence

    def test_approval_requires_attestation_preserves_history_and_excludes_reviewer(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_config[1]["name"], "Fictional Beta")
        self.assertEqual(change.status, "pending")
        self.assertContains(self.client.get(self.detail_url(change)), "Fictional Gamma")
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(self.detail_url(change)).status_code, 404)
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"approved", "note":"test", "verified":"on"}).status_code, 404)
        review_page = self.client.get(reverse("review_detail", args=[assignment.pk]))
        self.assertNotContains(review_page, "Fictional Gamma")
        self.assertNotContains(review_page, "beta@example.org")
        self.client.force_login(self.editor)
        page = self.client.get(self.detail_url(change))
        self.assertContains(page, "Fictional Beta")
        self.assertContains(page, "Fictional Gamma")
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"approved", "note":"Fictional evidence checked."}).status_code, 400)
        change.refresh_from_db()
        self.assertEqual(change.status, "pending")
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"approved", "note":"Fictional evidence checked.", "verified":"on"}).status_code, 409)
        self.upload_and_verify(change)
        response = self.client.post(self.detail_url(change), {"outcome":"approved", "note":"Fictional evidence checked.", "verified":"on"})
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        change.refresh_from_db()
        self.assertEqual(submission.contributors_config[1]["name"], "Fictional Gamma")
        self.assertEqual(submission.contributors_revision, 2)
        self.assertEqual(change.before[1]["name"], "Fictional Beta")
        self.assertEqual(change.proposed[1]["name"], "Fictional Gamma")
        self.assertTrue(change.editor_attested)
        self.assertEqual(change.closed_by, self.editor)
        self.assertIsNotNone(change.closed_at)
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"approved", "note":"again", "verified":"on"}).status_code, 409)
        self.assertEqual(SubmissionEvent.objects.filter(submission=submission, action="contributor_change_reviewed").count(), 1)

    def test_decline_or_withdraw_does_not_change_authors_and_allows_new_request(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"declined", "note":"Please provide more information."}).status_code, 302)
        change.refresh_from_db()
        submission.refresh_from_db()
        self.assertEqual(change.status, "declined")
        self.assertFalse(change.editor_attested)
        self.assertEqual(submission.contributors_config[1]["name"], "Fictional Beta")
        new_change = self.create_request(submission)
        self.assertEqual(self.client.post(reverse("withdraw_contributor_change", args=[submission.pk, new_change.pk]), {}).status_code, 400)
        self.assertEqual(self.client.post(reverse("withdraw_contributor_change", args=[submission.pk, new_change.pk]), {"confirm":"yes"}).status_code, 302)
        new_change.refresh_from_db()
        self.assertEqual(new_change.status, "withdrawn")
        self.assertEqual(new_change.closed_by, self.author)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_revision, 1)

    def test_pending_request_blocks_editorial_decision_and_second_request(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        new_url = reverse("request_contributor_change", args=[submission.pk])
        self.assertEqual(self.client.get(new_url).status_code, 409)
        self.client.force_login(self.editor)
        decision_url = reverse("editorial_decision", args=[submission.pk])
        self.assertContains(self.client.get(decision_url), "Resolve the pending author change request")
        self.assertEqual(self.client.post(decision_url, {"outcome":"reject", "author_message":"Fictional decision."}).status_code, 409)
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"declined", "note":"Fictional response."}).status_code, 302)
        self.assertEqual(self.client.post(decision_url, {"outcome":"reject", "author_message":"Fictional decision."}).status_code, 302)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(new_url).status_code, 409)

    def test_stale_or_final_submission_cannot_be_approved_but_can_be_declined(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        Submission.objects.filter(pk=submission.pk).update(contributors_revision=3)
        self.client.force_login(self.editor)
        data = {"outcome":"approved", "note":"Checked.", "verified":"on"}
        self.assertEqual(self.client.post(self.detail_url(change), data).status_code, 409)
        Submission.objects.filter(pk=submission.pk).update(contributors_revision=1, status=Submission.Status.ACCEPTED)
        self.assertEqual(self.client.post(self.detail_url(change), data).status_code, 409)
        self.assertEqual(self.client.post(self.detail_url(change), {"outcome":"declined", "note":"Resolve separately."}).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_config[1]["name"], "Fictional Beta")

    def test_invalid_request_does_not_write_or_update_submission(self):
        submission = self.submitted_with_authors()
        self.client.force_login(self.author)
        url = reverse("request_contributor_change", args=[submission.pk])
        self.assertEqual(self.client.post(url, self.request_data(self.original)).status_code, 400)
        no_corresponding = [dict(p, corresponding=False) for p in self.original]
        no_corresponding[1]["name"] = "Fictional Gamma"
        self.assertEqual(self.client.post(url, self.request_data(no_corresponding)).status_code, 400)
        deleted = [dict(p, DELETE=True) for p in self.original]
        self.assertEqual(self.client.post(url, self.request_data(deleted)).status_code, 400)
        revised = [dict(p) for p in self.original]
        revised[1]["name"] = "Fictional Gamma"
        self.assertEqual(self.client.post(url, self.request_data(revised, acknowledgement="")).status_code, 400)
        self.assertEqual(self.client.post(url, self.request_data(revised, revision=0)).status_code, 409)
        self.assertEqual(self.client.post(url, self.request_data(revised, **{"authors-INITIAL_FORMS":"1"})).status_code, 409)
        self.assertFalse(ContributorChangeRequest.objects.exists())
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_revision, 1)

    def test_only_active_assigned_editor_may_decide_and_posts_require_csrf(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        url = self.detail_url(change)
        self.assertEqual(self.client.post(url, {"outcome":"declined", "note":"Not permitted."}).status_code, 404)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.editor)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.assertEqual(self.client.get(url).status_code, 404)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=True)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.editor)
        self.assertEqual(csrf_client.post(url, {"outcome":"approved", "note":"Test", "verified":"on"}).status_code, 403)
        csrf_client.force_login(self.author)
        self.assertEqual(csrf_client.post(reverse("withdraw_contributor_change", args=[submission.pk, change.pk]), {"confirm":"yes"}).status_code, 403)

    def test_database_rejects_two_pending_requests(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContributorChangeRequest.objects.create(submission=submission, requested_by=self.author,
                reason="Duplicate", before=change.before, proposed=change.proposed, base_revision=1, acknowledged=True)
        self.assertEqual(ContributorChangeRequest.objects.filter(status="pending").count(), 1)

    def test_request_can_be_reviewed_during_revision(self):
        submission = self.submitted_with_authors()
        Submission.objects.filter(pk=submission.pk).update(status=Submission.Status.REVISION_REQUESTED)
        change = self.create_request(submission)
        self.upload_and_verify(change)
        self.assertEqual(self.client.post(self.detail_url(change),
            {"outcome":"approved", "note":"Fictional checks completed.", "verified":"on"}).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.REVISION_REQUESTED)
        self.assertEqual(submission.contributors_config[1]["name"], "Fictional Gamma")

    def test_author_replacement_at_maximum_keeps_the_limit(self):
        submission = self.submitted_with_authors()
        people = [{"name":f"Fictional {i}", "email":f"author{i}@example.org", "affiliation":"",
                   "orcid":"", "corresponding":i == 0} for i in range(100)]
        Submission.objects.filter(pk=submission.pk).update(contributors_config=people)
        forms = [{**p, "ORDER":i+1} for i,p in enumerate(people)]
        forms[1]["DELETE"] = True
        forms.append({"name":"Fictional replacement", "email":"replacement@example.org", "ORDER":101})
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("request_contributor_change", args=[submission.pk]),
            self.request_data(forms, initial=100)).status_code, 302)
        change = ContributorChangeRequest.objects.get(submission=submission)
        self.assertEqual(len(change.proposed), 100)
        self.assertEqual(change.proposed[-1]["name"], "Fictional replacement")

    def test_escaped_reason_and_cross_submission_ids(self):
        submission = self.submitted_with_authors()
        self.client.force_login(self.author)
        revised = [dict(p) for p in self.original]
        revised[1]["name"] = "Fictional Gamma"
        payload = self.request_data(revised, reason="<script>fiction</script>")
        self.assertEqual(self.client.post(reverse("request_contributor_change", args=[submission.pk]), payload).status_code, 302)
        change = ContributorChangeRequest.objects.get()
        page = self.client.get(self.detail_url(change))
        self.assertNotContains(page, "<script>fiction</script>")
        self.assertContains(page, "&lt;script&gt;fiction&lt;/script&gt;", html=False)
        other = Submission.objects.create(journal=self.journal, author=self.author, title="Other", abstract="Fake", manuscript="fake.pdf")
        self.assertEqual(self.client.get(reverse("contributor_change_detail", args=[other.pk, change.pk])).status_code, 404)
