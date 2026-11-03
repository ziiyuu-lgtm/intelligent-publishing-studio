import tempfile
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from publishing.review_deadline_views import ReviewDeadlineForm
from publishing.models import (EditorialAssignment, Membership, ReviewAssignment, ReviewDeadlineEvent,
                               ReviewRound, Submission, SubmissionEvent, SubmissionVersion)
from publishing.tests import test_workflow


@override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewDeadlineTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    def setUp(self):
        super().setUp()
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix="ips-deadlines-")
        self.addCleanup(directory.cleanup)
        setting = override_settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.submission = self.create_draft()
        self.submit(self.submission)
        self.assignment = self.invite(self.submission)
        self.url = reverse("manage_review_deadline", args=[self.assignment.pk])
        self.dashboard = reverse("review_progress")
        self.future = timezone.localdate() + timedelta(days=14)

    def save(self, due_on, revision=0, reason="Fictional test reason"):
        return self.client.post(self.url, {"due_on":due_on or "", "reason":reason, "revision":revision})

    def test_set_change_clear_history_and_reviewer_privacy(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("workspace")), self.dashboard)
        self.assertContains(self.client.get(self.dashboard), self.submission.title)
        self.assertEqual(self.save(self.future.isoformat()).status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.due_on, self.future)
        self.assertEqual(self.assignment.deadline_revision, 1)
        event = ReviewDeadlineEvent.objects.get(assignment=self.assignment)
        self.assertEqual((event.previous_date, event.new_date, event.recorded_by), (None, self.future, self.editor))
        self.assertEqual(SubmissionEvent.objects.filter(submission=self.submission, action="review_deadline_changed").count(), 1)
        self.assertEqual(self.save((self.future + timedelta(days=3)).isoformat(), 0).status_code, 409)
        self.assertEqual(self.save((self.future + timedelta(days=3)).isoformat(), 1, "Fictional extension").status_code, 302)
        self.assertEqual(self.save("", 2, "Fictional removal").status_code, 302)
        self.assignment.refresh_from_db()
        self.assertIsNone(self.assignment.due_on)
        self.assertEqual(self.assignment.deadline_revision, 3)
        self.assertEqual(list(self.assignment.deadline_events.values_list("revision", flat=True)), [3, 2, 1])
        self.assertContains(self.client.get(self.url), "Fictional extension")
        self.assertEqual(self.save(self.future.isoformat(), 3, "Fictional reset").status_code, 302)
        self.client.force_login(self.reviewer)
        reviewer_page = self.client.get(reverse("review_detail", args=[self.assignment.pk]))
        self.assertContains(reviewer_page, self.future.isoformat())
        self.assertNotContains(reviewer_page, "Fictional reset")
        self.assertContains(self.client.get(reverse("workspace")), self.future.isoformat())
        self.client.force_login(self.author)
        author_page = self.client.get(reverse("submission_detail", args=[self.submission.pk]))
        self.assertNotContains(author_page, self.reviewer.username)
        self.assertNotContains(author_page, self.future.isoformat())
        self.assertNotContains(author_page, "Fictional reset")

    def test_existing_date_is_machine_readable_in_chinese_input(self):
        self.assignment.due_on = self.future
        self.assignment.save(update_fields=["due_on"])
        self.client.force_login(self.editor)
        with translation.override("zh-hans"):
            response = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE="zh-hans")
        self.assertContains(response, f'value="{self.future.isoformat()}"')
        self.assertContains(response, 'type="date"')

    def test_deadline_uses_utc_even_under_another_active_timezone(self):
        instant = datetime(2026, 11, 2, 23, 30, tzinfo=datetime_timezone.utc)
        with timezone.override("Pacific/Kiritimati"), patch("publishing.review_deadline_views.timezone.now", return_value=instant):
            form = ReviewDeadlineForm({"due_on":"2026-11-02", "reason":"Fictional", "revision":0})
            self.assertTrue(form.is_valid(), form.errors)
            old = ReviewDeadlineForm({"due_on":"2026-11-01", "reason":"Fictional", "revision":0})
            self.assertFalse(old.is_valid())

    def test_validations_scope_and_csrf(self):
        self.client.force_login(self.editor)
        self.assertEqual(self.save((timezone.localdate() - timedelta(days=1)).isoformat()).status_code, 400)
        self.assertEqual(self.save(self.future.isoformat(), reason=" ").status_code, 400)
        self.assertFalse(ReviewDeadlineEvent.objects.exists())
        for user in (self.author, self.reviewer, self.coordinator, self.other_editor, self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.save(self.future.isoformat()).status_code, 404)
            self.assertNotContains(self.client.get(self.dashboard), self.submission.title)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.url, {"due_on":self.future.isoformat(), "revision":0, "reason":"test"}).status_code, 403)
        self.assertFalse(ReviewDeadlineEvent.objects.exists())

    def test_active_assignment_and_journal_role_are_required(self):
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.dashboard), self.submission.title)
        membership = Membership.objects.get(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR)
        membership.active = False
        membership.save(update_fields=["active"])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotContains(self.client.get(self.dashboard), self.submission.title)
        membership.active = True
        membership.save(update_fields=["active"])
        editor_assignment = EditorialAssignment.objects.get(submission=self.submission, editor=self.editor)
        editor_assignment.status = EditorialAssignment.Status.REVOKED
        editor_assignment.save(update_fields=["status"])
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertNotContains(self.client.get(self.dashboard), self.submission.title)

    def test_overdue_does_not_block_acceptance_or_report(self):
        self.assignment.due_on = timezone.localdate() - timedelta(days=1)
        self.assignment.save(update_fields=["due_on"])
        self.client.force_login(self.editor)
        page = self.client.get(self.dashboard, {"filter":"overdue"})
        self.assertContains(page, self.reviewer.username)
        self.assertEqual(page.context["counts"]["overdue"], 1)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[self.assignment.pk]),
            {"response":"accept", "declaration":"none"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_report", args=[self.assignment.pk]),
            {"recommendation":"minor", "comments":"Fictional review."}).status_code, 302)
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.status, ReviewAssignment.Status.SUBMITTED)
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(self.dashboard), self.reviewer.username)
        self.assertEqual(self.client.post(self.url, {"due_on":self.future.isoformat(), "revision":0, "reason":"late"}).status_code, 409)
        self.assertContains(self.client.get(self.url), "History remains read-only")

    def test_closed_round_disables_deadline_changes(self):
        self.assignment.round.status = ReviewRound.Status.CLOSED
        self.assignment.round.save(update_fields=["status"])
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(self.dashboard), self.reviewer.username)
        self.assertEqual(self.save(self.future.isoformat()).status_code, 409)
        self.assertContains(self.client.get(self.url), "History remains read-only")
        self.assertFalse(ReviewDeadlineEvent.objects.exists())

    def test_filters_count_tasks_and_exclude_closed_or_inconsistent_rounds(self):
        users = get_user_model().objects
        second = users.create_user("fictional-reviewer-b", password="long-fictional-password")
        Membership.objects.create(user=second, journal=self.journal, role=Membership.Role.REVIEWER)
        ReviewAssignment.objects.create(submission=self.submission, round=self.assignment.round,
            reviewer=second, invited_by=self.editor, status=ReviewAssignment.Status.ACCEPTED,
            conflict=ReviewAssignment.Conflict.NONE, due_on=timezone.localdate()-timedelta(days=1))
        self.client.force_login(self.editor)
        response = self.client.get(self.dashboard)
        self.assertEqual(response.context["counts"], {"all":2, "invited":1, "accepted":1, "overdue":1, "no_deadline":1})
        self.assertContains(self.client.get(self.dashboard, {"filter":"overdue"}), "fictional-reviewer-b")
        self.assertNotContains(self.client.get(self.dashboard, {"filter":"overdue"}), self.reviewer.username)
        self.assertContains(self.client.get(self.dashboard, {"filter":"no_deadline"}), self.reviewer.username)
        Membership.objects.filter(user=second, journal=self.journal, role=Membership.Role.REVIEWER).update(active=False)
        self.assertContains(self.client.get(self.dashboard), "Reviewer account or role inactive")

    def test_separate_journal_and_editor_scope(self):
        other_submission = Submission.objects.create(journal=self.other, author=self.author, title="Separate fictional manuscript",
            abstract="Fictional", manuscript="fictional.pdf", status=Submission.Status.SUBMITTED)
        other_version = SubmissionVersion.objects.create(submission=other_submission, number=1,
            manuscript="fictional.pdf", uploaded_by=self.author)
        other_round = ReviewRound.objects.create(submission=other_submission, version=other_version, number=1)
        EditorialAssignment.objects.create(submission=other_submission, editor=self.other_editor, assigned_by=self.coordinator)
        other_task = ReviewAssignment.objects.create(submission=other_submission, round=other_round,
            reviewer=self.other_reviewer, invited_by=self.other_editor)
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(self.dashboard), other_submission.title)
        self.assertEqual(self.client.get(reverse("manage_review_deadline", args=[other_task.pk])).status_code, 404)
        self.client.force_login(self.other_editor)
        self.assertContains(self.client.get(self.dashboard), other_submission.title)
        self.assertNotContains(self.client.get(self.dashboard), self.submission.title)

    def test_pagination_and_read_only_dashboard(self):
        users = get_user_model().objects
        for i in range(13):
            reviewer = users.create_user(f"pagination-reviewer-{i}", password="long-fictional-password")
            Membership.objects.create(user=reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
            ReviewAssignment.objects.create(submission=self.submission, round=self.assignment.round,
                reviewer=reviewer, invited_by=self.editor)
        self.client.force_login(self.editor)
        first = self.client.get(self.dashboard)
        self.assertEqual(first.context["counts"]["all"], 14)
        self.assertEqual(len(first.context["page"].object_list), 12)
        self.assertEqual(len(self.client.get(self.dashboard, {"page":2}).context["page"].object_list), 2)
        self.assertEqual(self.client.post(self.dashboard, {}).status_code, 405)

    def test_newer_version_prevents_deadline_changes(self):
        SubmissionVersion.objects.create(submission=self.submission, number=2, manuscript="fictional.pdf", uploaded_by=self.author)
        self.client.force_login(self.editor)
        self.assertNotContains(self.client.get(self.dashboard), self.reviewer.username)
        self.assertEqual(self.save(self.future.isoformat()).status_code, 409)
