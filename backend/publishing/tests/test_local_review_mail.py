from concurrent.futures import ThreadPoolExecutor
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
import tempfile
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import close_old_connections, connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from publishing.local_review_mail import enqueue_invitation, simulate_invitation
from publishing.models import LocalReviewMailJob, Membership, ReviewAssignment, ReviewNotice
from publishing.tests import test_workflow

TEST_KEY = "fictional-local-review-mail-key-for-testing-0123456789"


class LocalMailConfigurationTests(SimpleTestCase):
    def test_enabled_simulation_requires_a_private_key(self):
        for key in ("", "short"):
            env = dict(os.environ, IPS_LOCAL_REVIEW_MAIL_ENABLED="1", IPS_LOCAL_REVIEW_MAIL_KEY=key)
            result = subprocess.run([sys.executable, "-c", "import config.settings"],
                                    cwd=Path(__file__).resolve().parents[2], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("IPS_LOCAL_REVIEW_MAIL_KEY", result.stderr)


@override_settings(DEBUG=True, IPS_LOCAL_REVIEW_MAIL_ENABLED=True, IPS_LOCAL_REVIEW_MAIL_KEY=TEST_KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalReviewMailTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix="ips-local-mail-")
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name)
        media.enable()
        self.addCleanup(media.disable)
        self.reviewer.email = "fictional-reviewer@example.invalid"
        self.reviewer.save(update_fields=["email"])
        self.submission = self.create_draft(title="Private fictional manuscript title")
        self.submit(self.submission)
        self.assignment = self.invite(self.submission)
        self.job = LocalReviewMailJob.objects.get(notice__assignment=self.assignment)

    def test_queue_is_idempotent_and_simulated_message_is_generic(self):
        self.assertEqual(self.job.status, "pending")
        self.assertEqual(len(mail.outbox), 0)
        self.client.force_login(self.editor)
        self.client.post(reverse("review_invite", args=[self.submission.pk]), {"reviewer": self.reviewer.pk})
        self.assertEqual(LocalReviewMailJob.objects.filter(notice__assignment=self.assignment).count(), 1)
        self.assertEqual(simulate_invitation(self.job.pk), "simulated")
        self.assertEqual(simulate_invitation(self.job.pk), "unchanged")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["fictional-reviewer@example.invalid"])
        self.assertNotIn(self.submission.title, message.body)
        self.assertNotIn(self.submission.abstract, message.body)
        self.assertNotIn(self.author.username, message.body)
        self.assertNotIn(self.editor.username, message.body)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "simulated")
        self.assertIsNotNone(self.job.processed_at)
        self.assertNotIn(self.reviewer.email, self.job.recipient_digest)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend")
    def test_simulation_does_not_use_the_configured_smtp_backend(self):
        with patch("django.core.mail.backends.smtp.EmailBackend.send_messages") as smtp:
            self.assertEqual(simulate_invitation(self.job.pk), "simulated")
        smtp.assert_not_called()
        self.assertEqual(len(mail.outbox), 1)

    def test_nonfictional_or_invalid_address_never_enters_queue(self):
        for index, address in enumerate(["someone@example.com", "broken", "person@invalid"]):
            reviewer = get_user_model().objects.create_user(f"new-reviewer-{index}", email=address, password="fictional-password")
            Membership.objects.create(user=reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
            self.client.force_login(self.editor)
            self.client.post(reverse("review_invite", args=[self.submission.pk]), {"reviewer": reviewer.pk})
            notice = ReviewNotice.objects.get(assignment__reviewer=reviewer, kind="invitation")
            self.assertFalse(LocalReviewMailJob.objects.filter(notice=notice).exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_changed_address_suppresses_the_queued_job(self):
        self.reviewer.email = "different@example.invalid"
        self.reviewer.save(update_fields=["email"])
        self.assertEqual(simulate_invitation(self.job.pk), "suppressed")
        self.assertEqual(len(mail.outbox), 0)

    def test_revoked_role_or_closed_invitation_suppresses_the_job(self):
        Membership.objects.filter(user=self.reviewer, journal=self.journal, role="reviewer").update(active=False)
        self.assertEqual(simulate_invitation(self.job.pk), "suppressed")
        self.assertEqual(len(mail.outbox), 0)

    def test_responded_invitation_is_suppressed(self):
        ReviewAssignment.objects.filter(pk=self.assignment.pk).update(status="declined")
        self.assertEqual(simulate_invitation(self.job.pk), "suppressed")
        self.assertEqual(len(mail.outbox), 0)

    def test_failed_simulation_remains_pending_for_inspection(self):
        with patch("publishing.local_review_mail.EmailBackend.send_messages", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                simulate_invitation(self.job.pk)
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "pending")
        self.assertIsNone(self.job.processed_at)

    def test_queue_error_rolls_back_invitation_and_notice(self):
        reviewer = get_user_model().objects.create_user("new-fake-reviewer", email="new@example.invalid", password="fictional-password")
        Membership.objects.create(user=reviewer, journal=self.journal, role=Membership.Role.REVIEWER)
        self.client.force_login(self.editor)
        with patch("publishing.local_review_mail.LocalReviewMailJob.objects.get_or_create", side_effect=RuntimeError("synthetic failure")):
            with self.assertRaises(RuntimeError):
                self.client.post(reverse("review_invite", args=[self.submission.pk]), {"reviewer": reviewer.pk})
        self.assertFalse(ReviewAssignment.objects.filter(submission=self.submission, reviewer=reviewer).exists())
        self.assertFalse(ReviewNotice.objects.filter(assignment__reviewer=reviewer).exists())

    def test_operator_command_is_bounded_and_reports_no_actual_delivery(self):
        output = StringIO()
        call_command("simulate_local_review_mail", limit=1, stdout=output)
        self.assertIn("Simulated: 1", output.getvalue())
        self.assertIn("No email was sent", output.getvalue())
        with self.assertRaises(CommandError):
            call_command("simulate_local_review_mail", limit=101)

    @override_settings(IPS_LOCAL_REVIEW_MAIL_ENABLED=False)
    def test_disabled_feature_does_not_process_or_requeue(self):
        self.assertEqual(simulate_invitation(self.job.pk), "disabled")
        self.assertIsNone(enqueue_invitation(self.job.notice))
        with self.assertRaises(CommandError):
            call_command("simulate_local_review_mail")
        self.job.refresh_from_db()
        self.assertEqual(self.job.status, "pending")
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(DEBUG=False)
    def test_non_debug_environment_cannot_simulate_mail(self):
        self.assertEqual(simulate_invitation(self.job.pk), "disabled")
        self.assertEqual(len(mail.outbox), 0)


@override_settings(DEBUG=True, IPS_LOCAL_REVIEW_MAIL_ENABLED=True, IPS_LOCAL_REVIEW_MAIL_KEY=TEST_KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PostgreSQLLocalMailConcurrencyTests(TransactionTestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-locking test")
    def test_concurrent_workers_simulate_a_job_once(self):
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix="ips-local-mail-concurrency-")
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name)
        media.enable()
        self.addCleanup(media.disable)
        self.reviewer.email = "fictional-reviewer@example.invalid"
        self.reviewer.save(update_fields=["email"])
        submission = self.create_draft()
        self.submit(submission)
        assignment = self.invite(submission)
        job = LocalReviewMailJob.objects.get(notice__assignment=assignment)
        mail.outbox = []
        barrier = Barrier(6)

        def attempt(_):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return simulate_invitation(job.pk)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(attempt, range(6)))
        self.assertEqual(results.count("simulated"), 1)
        self.assertEqual(results.count("unchanged"), 5)
        self.assertEqual(len(mail.outbox), 1)
