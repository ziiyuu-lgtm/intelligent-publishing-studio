import shutil
import tempfile
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.author_responses import author_responses_ready, response_snapshot_digest
from publishing.models import (ContributorResponseEvent, ContributorResponseRequest, EditorialAssignment,
                               Membership, ReviewAssignment, Submission)
from publishing.tests import test_contributor_changes, test_contributors, test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthorResponseTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-responses-")
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
    upload_and_verify = test_contributor_changes.ContributorChangeTests.upload_and_verify

    def account(self, name):
        user = get_user_model().objects.create_user(name, password="long-fictional-password")
        Membership.objects.create(user=user, journal=self.journal, role=Membership.Role.AUTHOR)
        return user

    def manage_url(self, change, side="original", position=1):
        return reverse("manage_author_response", args=[change.submission_id, change.pk, side, position])

    def assign(self, change, recipient, side="original", position=1):
        self.client.force_login(self.editor)
        response = self.client.post(self.manage_url(change, side, position), {"recipient":recipient.pk, "verified":"on"})
        self.assertEqual(response.status_code, 302)
        return ContributorResponseRequest.objects.get(change=change, side=side, position=position, status="pending")

    def response_url(self, task):
        return reverse("respond_author_change", args=[task.pk])

    def test_account_response_is_private_and_blocks_approval_until_agreed(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor = self.account("fictional-coauthor")
        task = self.assign(change, coauthor)
        self.upload_and_verify(change)
        decision = {"outcome":"approved", "note":"Fictional review complete.", "verified":"on"}
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 409)
        self.client.force_login(coauthor)
        workspace = self.client.get(reverse("workspace"))
        self.assertContains(workspace, submission.title)
        page = self.client.get(self.response_url(task))
        self.assertContains(page, "Fictional Beta")
        self.assertContains(page, "Fictional Gamma")
        self.assertNotContains(page, "alpha@example.org")
        self.assertNotContains(page, "beta@example.org")
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("checks_page", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, "agreed")
        self.assertEqual(task.revision, 1)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 302)
        self.assertEqual(list(task.events.values_list("action", flat=True)), ["issued", "agreed"])
        self.client.force_login(coauthor)
        self.assertContains(self.client.get(self.response_url(task)), "response history is read-only")
        self.assertEqual(self.client.post(self.response_url(task), {"response":"disagreed", "reviewed":"on", "revision":"1"}).status_code, 409)

    def test_disagreement_cannot_be_cancelled_and_holder_can_change_response(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor = self.account("fictional-coauthor")
        task = self.assign(change, coauthor)
        self.upload_and_verify(change)
        self.client.force_login(coauthor)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"disagreed", "reviewed":"on", "revision":"0"}).status_code, 302)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 409)
        self.client.force_login(self.editor)
        cancel = reverse("revoke_author_response", args=[submission.pk, change.pk, task.pk])
        self.assertEqual(self.client.post(cancel, {"reason":"Fictional reason"}).status_code, 409)
        decision = {"outcome":"approved", "note":"Fictional", "verified":"on"}
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 409)
        self.client.force_login(coauthor)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"1"}).status_code, 302)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(cancel, {"reason":"Fictional reason"}).status_code, 409)
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 302)
        self.assertEqual(list(task.events.values_list("action", flat=True)), ["issued", "disagreed", "agreed"])
        history = self.client.get(self.manage_url(change))
        self.assertContains(history, "Disagreed")
        self.assertContains(history, "Agreed")

    def test_only_selected_active_account_can_respond_and_csrf_is_required(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor, stranger = self.account("fictional-coauthor"), self.account("fictional-stranger")
        task = self.assign(change, coauthor)
        for person in (self.author, self.editor, self.reviewer, stranger):
            self.client.force_login(person)
            self.assertEqual(self.client.get(self.response_url(task)).status_code, 404)
            self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 404)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(coauthor)
        self.assertEqual(csrf.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 403)
        csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.manage_url(change, "original", 0), {"recipient":stranger.pk, "verified":"on"}).status_code, 403)
        cancel = reverse("revoke_author_response", args=[submission.pk, change.pk, task.pk])
        self.assertEqual(csrf.post(cancel, {"reason":"Fictional correction"}).status_code, 403)
        Membership.objects.filter(user=coauthor, journal=self.journal, role=Membership.Role.AUTHOR).update(active=False)
        self.client.force_login(coauthor)
        self.assertEqual(self.client.get(self.response_url(task)).status_code, 404)
        self.assertNotContains(self.client.get(reverse("workspace")), submission.title)

    def test_pending_cancellation_and_expired_task_keep_history_and_review_separation(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor, replacement = self.account("fictional-coauthor"), self.account("fictional-replacement")
        task = self.assign(change, coauthor)
        ContributorResponseRequest.objects.filter(pk=task.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.client.force_login(coauthor)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 409)
        self.client.force_login(self.editor)
        cancel = reverse("revoke_author_response", args=[submission.pk, change.pk, task.pk])
        self.assertEqual(self.client.post(cancel, {}).status_code, 400)
        self.assertEqual(self.client.post(cancel, {"reason":"Incorrect fictional account"}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, "revoked")
        self.assertEqual(list(task.events.values_list("action", flat=True)), ["issued", "revoked"])
        self.client.force_login(coauthor)
        self.assertEqual(self.client.get(self.response_url(task)).status_code, 404)
        Membership.objects.create(user=coauthor, journal=self.journal, role=Membership.Role.REVIEWER)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[submission.pk]), {"reviewer":coauthor.pk})
        self.assertFalse(ReviewAssignment.objects.filter(submission=submission, reviewer=coauthor).exists())
        self.assertIsNotNone(self.assign(change, replacement))

    def test_editor_cannot_bind_existing_reviewer_or_editor_and_prevents_future_staff_assignment(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        self.invite(submission)
        Membership.objects.create(user=self.reviewer, journal=self.journal, role=Membership.Role.AUTHOR)
        Membership.objects.create(user=self.editor, journal=self.journal, role=Membership.Role.AUTHOR)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.manage_url(change), {"recipient":self.reviewer.pk, "verified":"on"}).status_code, 400)
        self.assertEqual(self.client.post(self.manage_url(change), {"recipient":self.editor.pk, "verified":"on"}).status_code, 400)
        coauthor = self.account("fictional-coauthor")
        task = self.assign(change, coauthor)
        Membership.objects.create(user=coauthor, journal=self.journal, role=Membership.Role.EDITOR)
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.post(reverse("submission_editors", args=[submission.pk]), {"editor":coauthor.pk}).status_code, 400)
        self.assertFalse(EditorialAssignment.objects.filter(submission=submission, editor=coauthor).exists())
        self.client.force_login(coauthor)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.response_url(task)).status_code, 200)

    def test_snapshot_binding_and_one_account_per_list(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor = self.account("fictional-coauthor")
        task = self.assign(change, coauthor)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.manage_url(change, "original", 0), {"recipient":coauthor.pk, "verified":"on"}).status_code, 400)
        another = self.assign(change, coauthor, "proposed", 0)
        self.assertNotEqual(task.pk, another.pk)
        self.client.force_login(coauthor)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 302)
        change.reason = "A different fictional reason"
        change.save(update_fields=["reason"])
        self.assertEqual(self.client.get(self.response_url(task)).status_code, 409)
        self.assertFalse(author_responses_ready(change, change.before, change.proposed))

    def test_database_enforces_one_active_task_per_slot_and_account(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor, other = self.account("fictional-coauthor"), self.account("fictional-other")
        task = self.assign(change, coauthor)
        values = {"change":change, "side":"original", "assigned_by":self.editor,
                  "snapshot_digest":response_snapshot_digest(change), "expires_at":timezone.now() + timedelta(days=1)}
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContributorResponseRequest.objects.create(position=1, recipient=other, **values)
        with self.assertRaises(IntegrityError), transaction.atomic():
            ContributorResponseRequest.objects.create(position=0, recipient=coauthor, **values)
        self.assertEqual(ContributorResponseEvent.objects.filter(response_request=task, action="issued").count(), 1)

    def test_submitter_can_confirm_their_own_explicitly_assigned_entry(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        task = self.assign(change, self.author, "original", 0)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(reverse("workspace")), "Author-list confirmations")
        self.assertEqual(self.client.post(self.response_url(task),
            {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, "agreed")

    def test_prior_inviter_and_assignment_ender_are_ineligible(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        past_editor = self.account("fictional-past-editor")
        assignment = EditorialAssignment.objects.get(submission=submission, editor=self.editor)
        assignment.ended_by = past_editor
        assignment.save(update_fields=["ended_by"])
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.manage_url(change), {"recipient":past_editor.pk, "verified":"on"}).status_code, 400)

    def test_agreement_is_not_usable_if_account_loses_author_access(self):
        submission = self.submitted_with_authors()
        change = self.create_request(submission)
        coauthor = self.account("fictional-coauthor")
        task = self.assign(change, coauthor)
        self.upload_and_verify(change)
        self.client.force_login(coauthor)
        self.assertEqual(self.client.post(self.response_url(task), {"response":"agreed", "reviewed":"on", "revision":"0"}).status_code, 302)
        Membership.objects.filter(user=coauthor, journal=self.journal, role=Membership.Role.AUTHOR).update(active=False)
        self.client.force_login(self.editor)
        decision = {"outcome":"approved", "note":"Fictional review", "verified":"on"}
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 409)
        Membership.objects.filter(user=coauthor, journal=self.journal, role=Membership.Role.AUTHOR).update(active=True)
        self.assertEqual(self.client.post(self.detail_url(change), decision).status_code, 302)
