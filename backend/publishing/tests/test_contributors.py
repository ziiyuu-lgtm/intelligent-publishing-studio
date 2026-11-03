import shutil
import tempfile

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.contributors import clean_orcid
from publishing.models import Submission, SubmissionEvent
from publishing.tests import test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ContributorTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-authors-")
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

    def form_data(self, entries, initial=0, revision=0, blank_extra=False):
        data = {"revision": str(revision), "authors-TOTAL_FORMS": str(len(entries) + int(blank_extra)),
                "authors-INITIAL_FORMS": str(initial), "authors-MIN_NUM_FORMS": "0", "authors-MAX_NUM_FORMS": "100"}
        for n, entry in enumerate(entries):
            for field, value in entry.items():
                if value is True:
                    value = "on"
                elif value is False:
                    continue
                data[f"authors-{n}-{field}"] = str(value)
        return data

    def test_authors_can_be_saved_reordered_and_seen_by_assigned_editor_only(self):
        submission = self.create_draft()
        url = reverse("submission_contributors", args=[submission.pk])
        a = {"name":"Imaginary Alpha", "email":"alpha@example.org", "affiliation":"Example Institute", "corresponding":True, "ORDER":2}
        b = {"name":"Imaginary Beta", "email":"beta@example.org", "corresponding":True, "ORDER":1}
        response = self.client.post(url, self.form_data([a, b], blank_extra=True))
        self.assertEqual(response.status_code, 302)
        submission.refresh_from_db()
        self.assertEqual([p["name"] for p in submission.contributors_config], ["Imaginary Beta", "Imaginary Alpha"])
        self.assertTrue(all(p["corresponding"] for p in submission.contributors_config))
        self.assertEqual(submission.contributors_revision, 1)
        self.assertEqual(SubmissionEvent.objects.filter(submission=submission, action="draft_contributors_updated").count(), 1)
        self.assertContains(self.client.get(reverse("submission_detail", args=[submission.pk])), "Imaginary Alpha")
        self.submit(submission)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("submission_detail", args=[submission.pk])), "alpha@example.org")
        self.assertEqual(self.client.get(url).status_code, 404)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        reviewer_page = self.client.get(reverse("review_detail", args=[assignment.pk]))
        self.assertNotContains(reviewer_page, "Imaginary Alpha")
        self.assertNotContains(reviewer_page, "alpha@example.org")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code, 409)
        self.assertEqual(self.client.post(url, self.form_data([a], initial=2, revision=1)).status_code, 409)

    def test_optional_list_and_corresponding_author_requirements(self):
        submission = self.create_draft()
        url = reverse("submission_contributors", args=[submission.pk])
        self.assertEqual(self.client.post(url, self.form_data([], blank_extra=True)).status_code, 302)
        person = {"name":"Imaginary author", "email":"", "corresponding":True, "ORDER":1}
        self.assertEqual(self.client.post(url, self.form_data([person])).status_code, 302)
        self.assertEqual(self.client.post(reverse("submission_submit", args=[submission.pk])).status_code, 409)
        person["email"] = "author@example.org"
        person["corresponding"] = False
        self.assertEqual(self.client.post(url, self.form_data([person], initial=1, revision=1)).status_code, 302)
        self.assertEqual(self.client.post(reverse("submission_submit", args=[submission.pk])).status_code, 409)
        person["corresponding"] = True
        self.assertEqual(self.client.post(url, self.form_data([person], initial=1, revision=2)).status_code, 302)
        review = self.client.get(reverse("submission_checklist", args=[submission.pk]))
        self.assertEqual(self.client.post(reverse("submission_submit", args=[submission.pk]),
            {"review_token":review.context["review_token"], "confirm":"yes"}).status_code, 302)

    def test_existing_empty_author_lists_can_still_be_submitted(self):
        submission = self.create_draft()
        review = self.client.get(reverse("submission_checklist", args=[submission.pk]))
        self.assertEqual(self.client.post(reverse("submission_submit", args=[submission.pk]),
            {"review_token":review.context["review_token"], "confirm":"yes"}).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_config, [])

    def test_orcid_format_checksum_and_duplicates(self):
        self.assertEqual(clean_orcid("https://orcid.org/0000-0000-0000-0001"), "0000-0000-0000-0001")
        for bad in ["0000-0000-0000-0002", "http://orcid.org/0000-0000-0000-0001", "not-an-id"]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                clean_orcid(bad)
        submission = self.create_draft()
        url = reverse("submission_contributors", args=[submission.pk])
        a = {"name":"Imaginary A", "email":"same@example.org", "orcid":"0000-0000-0000-0001"}
        b = {"name":"Imaginary B", "email":"SAME@example.org"}
        self.assertEqual(self.client.post(url, self.form_data([a, b])).status_code, 400)
        b.update(email="b@example.org", orcid="https://orcid.org/0000-0000-0000-0001")
        self.assertEqual(self.client.post(url, self.form_data([a, b])).status_code, 400)
        b["orcid"] = "0000-0000-0000-0002"
        self.assertEqual(self.client.post(url, self.form_data([a, b])).status_code, 400)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_config, [])

    def test_stale_editor_and_omitted_initial_forms_do_not_overwrite(self):
        submission = self.create_draft()
        url = reverse("submission_contributors", args=[submission.pk])
        first = {"name":"First fictional author", "ORDER":1}
        self.assertEqual(self.client.post(url, self.form_data([first])).status_code, 302)
        self.assertEqual(self.client.post(url, self.form_data([], initial=0, revision=0)).status_code, 409)
        altered = {"name":"New fictional author", "ORDER":1}
        self.assertEqual(self.client.post(url, self.form_data([altered], initial=1, revision=0)).status_code, 409)
        submission.refresh_from_db()
        self.assertEqual(submission.contributors_config[0]["name"], "First fictional author")
        self.assertEqual(submission.contributors_revision, 1)

    def test_explicit_deletion_updates_list_without_granting_account_access(self):
        submission = self.create_draft()
        url = reverse("submission_contributors", args=[submission.pk])
        a = {"name":"First fictional author", "email":"one@example.org", "corresponding":True, "ORDER":1}
        b = {"name":"Second fictional author", "email":"two@example.org", "ORDER":2}
        self.assertEqual(self.client.post(url, self.form_data([a, b])).status_code, 302)
        b["DELETE"] = True
        self.assertEqual(self.client.post(url, self.form_data([a, b], initial=2, revision=1)).status_code, 302)
        submission.refresh_from_db()
        self.assertEqual([p["name"] for p in submission.contributors_config], ["First fictional author"])
        self.assertEqual(submission.contributors_revision, 2)

    def test_invalid_stored_data_blocks_submission(self):
        submission = self.create_draft()
        Submission.objects.filter(pk=submission.pk).update(contributors_config=[{"name":"Incomplete record"}])
        self.assertEqual(self.client.get(reverse("submission_contributors", args=[submission.pk])).status_code, 409)
        self.assertEqual(self.client.post(reverse("submission_submit", args=[submission.pk])).status_code, 409)

    def test_html_is_escaped_and_editor_requires_csrf(self):
        submission = self.create_draft()
        url = reverse("submission_contributors", args=[submission.pk])
        self.assertEqual(self.client.post(url, self.form_data([{"name":"<script>fiction</script>"}])).status_code, 302)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertNotContains(page, "<script>fiction</script>")
        self.assertContains(page, "&lt;script&gt;fiction&lt;/script&gt;", html=False)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.author)
        self.assertEqual(csrf_client.post(url, self.form_data([], initial=1, revision=1)).status_code, 403)
