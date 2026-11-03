import tempfile
from datetime import timedelta

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.author_responses import response_snapshot_digest
from publishing.models import (ContributorChangeRequest, ContributorConfirmationEvent, ContributorResponseEvent,
                               ContributorResponseRequest, EditorialAssignment, Membership, Submission)
from publishing.tests import test_author_responses, test_contributor_changes, test_contributors, test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AuthorDashboardTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    form_data = test_contributors.ContributorTests.form_data
    submitted_with_authors = test_contributor_changes.ContributorChangeTests.submitted_with_authors
    request_data = test_contributor_changes.ContributorChangeTests.request_data
    create_request = test_contributor_changes.ContributorChangeTests.create_request
    account = test_author_responses.AuthorResponseTests.account
    assign = test_author_responses.AuthorResponseTests.assign
    manage_url = test_author_responses.AuthorResponseTests.manage_url

    def setUp(self):
        super().setUp()
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix="ips-dashboard-")
        self.addCleanup(directory.cleanup)
        settings = override_settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        submission = self.submitted_with_authors()
        self.change = self.create_request(submission)
        self.url = reverse("author_change_dashboard")

    def extra_change(self, title, journal=None, editor=None):
        submission = Submission.objects.create(journal=journal or self.journal, author=self.author, title=title,
            abstract="Fictional summary", manuscript="fictional.pdf", status=Submission.Status.SUBMITTED,
            contributors_config=self.change.before, contributors_revision=1)
        EditorialAssignment.objects.create(submission=submission, editor=editor or self.editor, assigned_by=self.coordinator)
        return ContributorChangeRequest.objects.create(submission=submission, requested_by=self.author,
            reason="Fictional private change reason", before=self.change.before, proposed=self.change.proposed,
            base_revision=1, acknowledged=True)

    def test_private_scope_and_workspace_navigation(self):
        foreign = self.extra_change("Separate journal confidential title", self.other, self.other_editor)
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertContains(page, self.change.submission.title)
        self.assertNotContains(page, foreign.submission.title)
        self.assertNotContains(page, "Fictional Beta")
        self.assertNotContains(page, "beta@example.org")
        self.assertNotContains(page, "A fictional update to author details.")
        self.assertContains(self.client.get(reverse("workspace")), self.url)
        self.client.force_login(self.other_editor)
        page = self.client.get(self.url)
        self.assertContains(page, foreign.submission.title)
        self.assertNotContains(page, self.change.submission.title)
        for user in (self.author, self.reviewer, self.coordinator, self.superuser):
            self.client.force_login(user)
            page = self.client.get(self.url)
            self.assertNotContains(page, self.change.submission.title)
            self.assertNotContains(page, foreign.submission.title)
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_filters_count_requests_not_accounts_and_ignore_cancelled_tasks(self):
        colleague = self.account("fictional-confirmation-1")
        task = self.assign(self.change, colleague)
        task.expires_at = timezone.now() - timedelta(minutes=1)
        task.save(update_fields=["expires_at"])
        colleague2 = self.account("fictional-confirmation-2")
        second = self.assign(self.change, colleague2, "proposed", 1)
        second.status = ContributorResponseRequest.Status.DISAGREED
        second.save(update_fields=["status"])
        ContributorResponseEvent.objects.create(response_request=second, actor=colleague2, action="disagreed")
        self.extra_change("No account tasks assigned")
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertEqual(page.context["counts"], {"all":2, "waiting":1, "overdue":1, "objection":1})
        self.assertContains(page, "Author disagreed")
        self.assertContains(page, "Response overdue")
        self.assertContains(page, "Evidence checks incomplete")
        for selection in ("waiting", "overdue", "objection"):
            filtered = self.client.get(self.url, {"filter":selection})
            self.assertContains(filtered, self.change.submission.title)
            self.assertNotContains(filtered, "No account tasks assigned")
        task.status = ContributorResponseRequest.Status.REVOKED
        task.save(update_fields=["status"])
        page = self.client.get(self.url)
        self.assertEqual(page.context["counts"]["waiting"], 0)
        self.assertEqual(page.context["counts"]["overdue"], 0)
        self.assertEqual(page.context["counts"]["objection"], 1)

    def test_role_and_assignment_changes_remove_visibility(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.url), self.change.submission.title)
        membership = Membership.objects.get(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR)
        membership.active = False
        membership.save(update_fields=["active"])
        self.assertNotContains(self.client.get(self.url), self.change.submission.title)
        membership.active = True
        membership.save(update_fields=["active"])
        assignment = EditorialAssignment.objects.get(submission=self.change.submission, editor=self.editor)
        assignment.status = EditorialAssignment.Status.REVOKED
        assignment.save(update_fields=["status"])
        self.assertNotContains(self.client.get(self.url), self.change.submission.title)
        assignment.status = EditorialAssignment.Status.ACTIVE
        assignment.save(update_fields=["status"])
        # A person ever assigned an author response is excluded even after cancellation.
        ContributorResponseRequest.objects.create(change=self.change, side="original", position=1,
            recipient=self.editor, assigned_by=self.coordinator, status="revoked",
            snapshot_digest=response_snapshot_digest(self.change), expires_at=timezone.now())
        self.assertNotContains(self.client.get(self.url), self.change.submission.title)

    def test_warning_for_dispute_invalid_data_and_inconsistent_agreement(self):
        ContributorConfirmationEvent.objects.create(change=self.change, side="original", position=0,
            state="disputed", recorded_by=self.editor)
        coauthor = self.account("fictional-confirmation")
        task = self.assign(self.change, coauthor)
        task.status = "agreed"  # Deliberately missing the matching event.
        task.save(update_fields=["status"])
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertContains(page, "Evidence check disputed")
        self.assertContains(page, "Account or response needs review")
        self.change.before = {"invalid":"snapshot"}
        self.change.save(update_fields=["before"])
        page = self.client.get(self.url)
        self.assertContains(page, "Records need review")

    def test_pending_task_with_inactive_author_account_is_flagged(self):
        coauthor = self.account("fictional-inactive-author")
        self.assign(self.change, coauthor)
        Membership.objects.filter(user=coauthor, journal=self.journal, role=Membership.Role.AUTHOR).update(active=False)
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertContains(page, "Awaiting response")
        self.assertContains(page, "Account or response needs review")

    def test_pagination_and_read_only(self):
        for number in range(13):
            self.extra_change(f"Fictional item {number:02d}")
        self.client.force_login(self.editor)
        first = self.client.get(self.url)
        self.assertEqual(first.context["counts"]["all"], 14)
        self.assertEqual(len(first.context["rows"]), 12)
        self.assertEqual(len(self.client.get(self.url, {"page":2}).context["rows"]), 2)
        before = ContributorChangeRequest.objects.count()
        self.assertEqual(self.client.post(self.url, {}).status_code, 405)
        self.assertEqual(ContributorChangeRequest.objects.count(), before)
