from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import transaction
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.local_mail_verification import simulate_confirmation_message
from publishing.local_registration import FictionalRegistrationForm, create_attempt
from publishing.models import LocalTestRegistration, Membership, OrganisationMembership
from publishing.tests.test_local_registration import KEY, PASSWORD, registration_data


class LocalMailVerificationConfigurationTests(SimpleTestCase):
    def test_local_mail_experiment_requires_local_registration(self):
        env = dict(os.environ, IPS_LOCAL_MAIL_VERIFICATION_ENABLED="1", IPS_LOCAL_REGISTRATION_ENABLED="0")
        result = subprocess.run([sys.executable, "-c", "import config.settings"],
                                cwd=Path(__file__).resolve().parents[2], env=env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires local registration", result.stderr)


@override_settings(DEBUG=True, IPS_LOCAL_REGISTRATION_ENABLED=True, IPS_LOCAL_REGISTRATION_KEY=KEY,
                   IPS_LOCAL_MAIL_VERIFICATION_ENABLED=True,
                   EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalMailVerificationTests(TestCase):
    def register(self, username="test_mailuser", email="mailuser@example.invalid"):
        # TestCase wraps the test in a transaction that is rolled back; execute
        # the registered commit callback explicitly for these request tests.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("test_mail_registration"), registration_data(username, email))

    def link(self, index=0):
        return urlsplit(mail.outbox[index].body.splitlines()[-1])

    def test_fictional_message_link_confirms_in_another_browser_without_smtp(self):
        with patch("django.core.mail.backends.smtp.EmailBackend.send_messages") as smtp:
            response = self.register()
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "No email was sent")
            self.assertNotContains(response, "mailuser@example.invalid")
            self.assertNotContains(response, "test_mailuser")
            self.assertEqual(len(mail.outbox), 1)
            self.assertEqual(mail.outbox[0].to, ["mailuser@example.invalid"])
            parsed = self.link()
            self.assertEqual(parsed.scheme, "https")
            self.assertEqual(parsed.netloc, "mailbox.example.invalid")
            self.assertFalse(parsed.query)
            self.assertEqual(len(parsed.fragment), 43)
            self.assertNotIn(parsed.fragment, response.content.decode())
            self.assertNotIn(parsed.fragment, str(dict(self.client.session)))
            attempt = LocalTestRegistration.objects.get(user__username="test_mailuser")
            self.assertFalse(attempt.user.is_active)
            self.assertNotIn(parsed.fragment, attempt.token_digest)

            other = Client(enforce_csrf_checks=True)
            page = other.get(parsed.path)
            self.assertEqual(page.status_code, 200)
            self.assertIn("no-store", page["Cache-Control"])
            self.assertEqual(page["Referrer-Policy"], "no-referrer")
            self.assertNotContains(page, parsed.fragment)
            attempt.user.refresh_from_db()
            self.assertFalse(attempt.user.is_active)
            csrf = other.cookies["csrftoken"].value
            result = other.post(parsed.path, {"token": parsed.fragment}, HTTP_X_CSRFTOKEN=csrf)
            self.assertContains(result, "fictional test account is active")
            attempt.user.refresh_from_db()
            self.assertTrue(attempt.user.is_active)
            self.assertFalse(Membership.objects.filter(user=attempt.user).exists())
            self.assertFalse(OrganisationMembership.objects.filter(user=attempt.user).exists())
            replay = other.post(parsed.path, {"token": parsed.fragment}, HTTP_X_CSRFTOKEN=csrf)
            self.assertContains(replay, "invalid or expired")
            self.assertEqual(len(mail.outbox), 1)
            smtp.assert_not_called()

    def test_missing_csrf_token_cannot_confirm(self):
        self.register()
        parsed = self.link()
        client = Client(enforce_csrf_checks=True)
        client.get(parsed.path)
        response = client.post(parsed.path, {"token": parsed.fragment})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LocalTestRegistration.objects.get().user.is_active)

    def test_wrong_account_token_is_rejected_and_original_token_still_works(self):
        self.register()
        first = self.link()
        self.register(username="test_second", email="second@example.invalid")
        second = self.link(1)
        other = Client()
        self.assertContains(other.post(first.path, {"token": second.fragment}), "invalid or expired")
        self.assertFalse(get_user_model().objects.get(username="test_mailuser").is_active)
        self.assertContains(other.post(first.path, {"token": first.fragment}), "fictional test account is active")
        self.assertFalse(get_user_model().objects.get(username="test_second").is_active)

    def test_expiry_and_address_change_prevent_confirmation(self):
        self.register()
        first = self.link()
        attempt = LocalTestRegistration.objects.get()
        attempt.expires_at = timezone.now() - timedelta(seconds=1)
        attempt.save(update_fields=["expires_at"])
        self.assertContains(self.client.post(first.path, {"token": first.fragment}), "invalid or expired")
        self.assertFalse(get_user_model().objects.get(pk=attempt.user_id).is_active)

        self.register(username="test_second", email="second@example.invalid")
        second = self.link(1)
        user = get_user_model().objects.get(username="test_second")
        user.email = "changed@example.invalid"
        user.save(update_fields=["email"])
        self.assertContains(self.client.post(second.path, {"token": second.fragment}), "invalid or expired")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_duplicate_and_invalid_inputs_use_the_same_generic_response(self):
        response = self.register()
        self.assertContains(response, "If fictional details were accepted")
        duplicate = self.register()
        invalid = self.register(username="test_other", email="someone@example.com")
        for item in (duplicate, invalid):
            self.assertEqual(item.status_code, response.status_code)
            self.assertContains(item, "If fictional details were accepted")
            self.assertNotContains(item, "already in use")
            self.assertNotContains(item, "someone@example.com")
        self.assertEqual(get_user_model().objects.count(), 1)
        self.assertEqual(len(mail.outbox), 1)

    def test_message_helper_refuses_nonfictional_or_changed_addresses(self):
        form = FictionalRegistrationForm(registration_data())
        self.assertTrue(form.is_valid(), form.errors)
        attempt_id, token = create_attempt(form)
        attempt = LocalTestRegistration.objects.get(pk=attempt_id)
        attempt.user.email = "someone@example.com"
        attempt.user.save(update_fields=["email"])
        with self.assertRaises(ValueError):
            simulate_confirmation_message(attempt_id, token)
        self.assertEqual(len(mail.outbox), 0)

    def test_password_change_invalidates_a_pending_confirmation(self):
        self.register()
        parsed = self.link()
        user = get_user_model().objects.get(username="test_mailuser")
        user.set_password("A-different-fictional-password-9284!")
        user.save(update_fields=["password"])
        self.assertContains(self.client.post(parsed.path, {"token": parsed.fragment}), "invalid or expired")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_pending_account_cannot_activate_after_staff_access_is_granted(self):
        self.register()
        parsed = self.link()
        user = get_user_model().objects.get(username="test_mailuser")
        user.is_staff = True
        user.save(update_fields=["is_staff"])
        self.assertContains(self.client.post(parsed.path, {"token": parsed.fragment}), "invalid or expired")
        user.refresh_from_db()
        self.assertFalse(user.is_active)

    def test_rollback_discards_the_message_callback(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.client.post(reverse("test_mail_registration"), registration_data())
                raise RuntimeError("roll back the outer transaction")
        self.assertEqual(len(callbacks), 0)
        self.assertEqual(len(mail.outbox), 0)
        self.assertFalse(get_user_model().objects.exists())
        self.assertFalse(LocalTestRegistration.objects.exists())

    def test_message_is_only_created_after_commit_callback(self):
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            self.client.post(reverse("test_mail_registration"), registration_data())
            self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(len(callbacks), 1)
        callbacks[0]()
        self.assertEqual(len(mail.outbox), 1)

    def test_callback_failure_keeps_account_inactive_and_response_generic(self):
        with patch("publishing.local_mail_verification.EmailBackend.send_messages", side_effect=RuntimeError("synthetic")):
            # TestCase's callback-capture helper uses the django.test logger.
            with self.assertLogs("django.test", level="ERROR"):
                response = self.register()
        self.assertContains(response, "If fictional details were accepted")
        self.assertFalse(get_user_model().objects.get(username="test_mailuser").is_active)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(IPS_LOCAL_MAIL_VERIFICATION_ENABLED=False)
    def test_experiment_is_disabled_by_default(self):
        self.assertEqual(self.client.get(reverse("test_mail_registration")).status_code, 404)
        self.assertEqual(self.client.get(reverse("test_mail_confirmation", args=["00000000-0000-0000-0000-000000000000"])).status_code, 404)
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(DEBUG=False)
    def test_experiment_is_unavailable_when_debug_is_false(self):
        self.assertEqual(self.client.get(reverse("test_mail_registration")).status_code, 404)
        self.assertEqual(self.register().status_code, 404)
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(IPS_LOCAL_REGISTRATION_ENABLED=False)
    def test_experiment_cannot_run_when_registration_is_disabled(self):
        self.assertEqual(self.client.get(reverse("test_mail_registration")).status_code, 404)
        self.assertEqual(self.register().status_code, 404)
        self.assertFalse(get_user_model().objects.exists())
