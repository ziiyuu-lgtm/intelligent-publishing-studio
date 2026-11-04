from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, close_old_connections, connection
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.local_registration import FictionalRegistrationForm, MAX_ATTEMPTS, activate_attempt, create_attempt
from publishing.local_registration_views import SESSION_KEY
from publishing.models import LocalTestRegistration, Membership, OrganisationMembership

KEY = "fictional-registration-key-for-isolated-tests-only-123456789"
PASSWORD = "Fictional-phrase-8391-demo!"


def registration_data(username="test_newperson", email="newperson@example.invalid"):
    return {"username": username, "email": email, "password1": PASSWORD, "password2": PASSWORD}


class RegistrationConfigurationTests(SimpleTestCase):
    def test_enabled_profile_requires_a_private_key(self):
        for key in ("", "short"):
            env = dict(os.environ, IPS_LOCAL_REGISTRATION_ENABLED="1", IPS_LOCAL_REGISTRATION_KEY=key)
            result = subprocess.run([sys.executable, "-c", "import config.settings"],
                                    cwd=Path(__file__).resolve().parents[2], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("IPS_LOCAL_REGISTRATION_KEY", result.stderr)


@override_settings(DEBUG=True, IPS_LOCAL_REGISTRATION_ENABLED=True, IPS_LOCAL_REGISTRATION_KEY=KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalRegistrationTests(TestCase):
    def register(self, **changes):
        return self.client.post(reverse("test_registration"), registration_data(**changes))

    def pending(self):
        return self.client.session[SESSION_KEY]

    def test_opt_in_registration_activation_and_sign_in_are_local_only(self):
        with patch("django.core.mail.backends.smtp.EmailBackend.send_messages") as smtp:
            self.assertContains(self.client.get(reverse("login")), "Create a fictional test account")
            response = self.register()
            self.assertRedirects(response, reverse("test_registration_confirm"))
            pending = self.pending()
            attempt = LocalTestRegistration.objects.get(pk=pending["id"])
            user = attempt.user
            self.assertFalse(user.is_active)
            self.assertTrue(user.check_password(PASSWORD))
            self.assertNotEqual(attempt.token_digest, pending["token"])
            self.assertNotIn(pending["token"], attempt.token_digest)
            self.assertNotIn(user.email, attempt.address_digest)
            self.assertFalse(user.is_staff or user.is_superuser)
            self.assertFalse(Membership.objects.filter(user=user).exists())
            self.assertFalse(OrganisationMembership.objects.filter(user=user).exists())
            confirmation = self.client.get(reverse("test_registration_confirm"))
            self.assertContains(confirmation, "no mailbox or identity was verified")
            self.assertContains(confirmation, pending["token"])
            self.assertIn("no-store", confirmation["Cache-Control"])
            result = self.client.post(reverse("test_registration_confirm"), {"token": pending["token"]})
            self.assertRedirects(result, reverse("login"))
            user.refresh_from_db()
            attempt.refresh_from_db()
            self.assertTrue(user.is_active)
            self.assertIsNotNone(attempt.activated_at)
            self.assertNotIn(SESSION_KEY, self.client.session)
            self.assertFalse(activate_attempt(attempt.pk, pending["token"]))
            login = self.client.post(reverse("login"), {"username": user.username, "password": PASSWORD})
            self.assertRedirects(login, reverse("workspace"))
            self.assertEqual(len(mail.outbox), 0)
            smtp.assert_not_called()

    def test_only_fictional_identifiers_and_passwords_are_accepted(self):
        for username, email in [("realname", "person@example.invalid"),
                                ("test_someone", "person@example.com"),
                                ("test_someone", "not-an-email")]:
            with self.subTest(username=username, email=email):
                self.assertEqual(self.register(username=username, email=email).status_code, 200)
        weak = registration_data()
        weak["password1"] = weak["password2"] = "short"
        self.assertEqual(self.client.post(reverse("test_registration"), weak).status_code, 200)
        self.assertFalse(get_user_model().objects.exists())

    def test_duplicate_address_is_rejected_without_revealing_password(self):
        get_user_model().objects.create_user("existing", email="EXISTING@example.invalid", password=PASSWORD)
        response = self.register(email="existing@example.invalid")
        self.assertContains(response, "already in use")
        self.assertNotContains(response, PASSWORD)
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_duplicate_address_race_rolls_back_new_account(self):
        first = FictionalRegistrationForm(registration_data("test_one", "same@example.invalid"))
        second = FictionalRegistrationForm(registration_data("test_two", "SAME@example.invalid"))
        self.assertTrue(first.is_valid(), first.errors)
        self.assertTrue(second.is_valid(), second.errors)
        create_attempt(first)
        with self.assertRaises(IntegrityError):
            create_attempt(second)
        self.assertFalse(get_user_model().objects.filter(username="test_two").exists())
        self.assertEqual(LocalTestRegistration.objects.count(), 1)

    def test_registration_error_rolls_back_the_new_user(self):
        form = FictionalRegistrationForm(registration_data())
        self.assertTrue(form.is_valid(), form.errors)
        with patch("publishing.local_registration.LocalTestRegistration.objects.create", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                create_attempt(form)
        self.assertFalse(get_user_model().objects.exists())

    def test_other_browser_cannot_activate_the_pending_account(self):
        self.register()
        pending = self.pending()
        other = Client()
        response = other.post(reverse("test_registration_confirm"), {"token": pending["token"]})
        self.assertContains(response, "no pending test confirmation")
        self.assertFalse(LocalTestRegistration.objects.get(pk=pending["id"]).user.is_active)

    def test_get_does_not_activate_and_missing_csrf_is_rejected(self):
        client = Client(enforce_csrf_checks=True)
        client.get(reverse("test_registration"))
        response = client.post(reverse("test_registration"), registration_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(get_user_model().objects.exists())
        self.register()
        pending = self.pending()
        self.client.get(reverse("test_registration_confirm"))
        self.assertFalse(LocalTestRegistration.objects.get(pk=pending["id"]).user.is_active)
        client.cookies.update(self.client.cookies)
        client.get(reverse("test_registration_confirm"))
        response = client.post(reverse("test_registration_confirm"), {"token": pending["token"]})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(LocalTestRegistration.objects.get(pk=pending["id"]).user.is_active)

    def test_confirmation_expires_and_removes_code_from_session(self):
        self.register()
        pending = self.pending()
        LocalTestRegistration.objects.filter(pk=pending["id"]).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertContains(self.client.get(reverse("test_registration_confirm")), "has expired")
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertFalse(activate_attempt(pending["id"], pending["token"]))
        self.assertFalse(LocalTestRegistration.objects.get(pk=pending["id"]).user.is_active)

    def test_failed_tokens_are_limited(self):
        self.register()
        pending = self.pending()
        for _ in range(MAX_ATTEMPTS):
            self.client.post(reverse("test_registration_confirm"), {"token": "incorrect"})
        attempt = LocalTestRegistration.objects.get(pk=pending["id"])
        self.assertEqual(attempt.failed_attempts, MAX_ATTEMPTS)
        self.assertNotIn(SESSION_KEY, self.client.session)
        self.assertFalse(activate_attempt(attempt.pk, pending["token"]))
        self.assertFalse(attempt.user.is_active)

    def test_changed_address_invalidates_the_attempt(self):
        self.register()
        pending = self.pending()
        attempt = LocalTestRegistration.objects.get(pk=pending["id"])
        attempt.user.email = "changed@example.invalid"
        attempt.user.save(update_fields=["email"])
        self.assertFalse(activate_attempt(attempt.pk, pending["token"]))
        attempt.user.refresh_from_db()
        self.assertFalse(attempt.user.is_active)

    @override_settings(IPS_LOCAL_REGISTRATION_ENABLED=False)
    def test_feature_is_hidden_and_unavailable_by_default(self):
        self.assertNotContains(self.client.get(reverse("login")), "Create a fictional test account")
        self.assertEqual(self.client.get(reverse("test_registration")).status_code, 404)
        self.assertEqual(self.client.get(reverse("test_registration_confirm")).status_code, 404)
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(DEBUG=False)
    def test_feature_is_unavailable_outside_debug(self):
        self.assertEqual(self.client.get(reverse("test_registration")).status_code, 404)
        self.assertEqual(self.client.post(reverse("test_registration"), registration_data()).status_code, 404)
        self.assertFalse(get_user_model().objects.exists())

    @override_settings(IPS_LOCAL_REGISTRATION_KEY="short")
    def test_invalid_key_disables_feature_even_if_flag_is_enabled(self):
        self.assertEqual(self.client.get(reverse("test_registration")).status_code, 404)
        self.assertNotContains(self.client.get(reverse("login")), "Create a fictional test account")


@override_settings(DEBUG=True, IPS_LOCAL_REGISTRATION_ENABLED=True, IPS_LOCAL_REGISTRATION_KEY=KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalRegistrationConcurrencyTests(TransactionTestCase):
    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-locking test")
    def test_concurrent_confirmation_activates_once(self):
        form = FictionalRegistrationForm(registration_data())
        self.assertTrue(form.is_valid(), form.errors)
        attempt_id, token = create_attempt(form)
        barrier = Barrier(6)

        def confirm(_):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return activate_attempt(attempt_id, token)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(confirm, range(6)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 5)
        self.assertTrue(LocalTestRegistration.objects.get(pk=attempt_id).user.is_active)
