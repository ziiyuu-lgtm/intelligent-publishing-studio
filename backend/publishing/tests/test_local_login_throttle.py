from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
from threading import Barrier
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import close_old_connections, connection
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.local_login_throttle import PAIR_LIMIT, SOURCE_LIMIT, WINDOW_SECONDS, consume_budget
from publishing.models import LocalLoginThrottleBucket

TEST_KEY = "fictional-throttle-key-for-isolated-test-only-0123456789"


class LocalLoginThrottleConfigurationTests(SimpleTestCase):
    def test_enabled_profile_rejects_a_missing_or_short_key(self):
        for key in ("", "short"):
            env = dict(os.environ, IPS_LOCAL_LOGIN_THROTTLE_ENABLED="1", IPS_LOCAL_LOGIN_THROTTLE_KEY=key)
            result = subprocess.run([sys.executable, "-c", "import config.settings"],
                                    cwd=Path(__file__).resolve().parents[2], env=env,
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("IPS_LOCAL_LOGIN_THROTTLE_KEY", result.stderr)


@override_settings(DEBUG=True, IPS_LOCAL_LOGIN_THROTTLE_ENABLED=True,
                   IPS_LOCAL_LOGIN_THROTTLE_KEY=TEST_KEY,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class LocalLoginThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        get_user_model().objects.create_user("fictional-author", password="fictional-test-password")

    def post_login(self, username="fictional-author", password="wrong", **headers):
        return self.client.post(reverse("login"), {"username": username, "password": password}, **headers)

    def test_pair_limit_returns_generic_429_and_does_not_authenticate(self):
        for _ in range(PAIR_LIMIT):
            self.assertEqual(self.post_login().status_code, 200)
        response = self.post_login(password="fictional-test-password")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.content, b"Too many sign-in attempts. Try again later.")
        self.assertGreaterEqual(int(response["Retry-After"]), 1)
        self.assertIn("no-store", response["Cache-Control"])
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_username_and_address_are_not_stored_as_plain_text(self):
        self.post_login(REMOTE_ADDR="192.0.2.87")
        rows = list(LocalLoginThrottleBucket.objects.values("scope", "digest"))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(len(row["digest"]) == 64 for row in rows))
        self.assertNotIn("fictional-author", str(rows))
        self.assertNotIn("192.0.2.87", str(rows))

    def test_one_source_cannot_exhaust_another_sources_pair_budget(self):
        for _ in range(PAIR_LIMIT):
            self.post_login(REMOTE_ADDR="192.0.2.1")
        self.assertEqual(self.post_login(REMOTE_ADDR="192.0.2.1").status_code, 429)
        self.assertEqual(self.post_login(password="fictional-test-password", REMOTE_ADDR="192.0.2.2").status_code, 302)

    def test_source_budget_covers_different_usernames(self):
        for index in range(SOURCE_LIMIT):
            response = self.post_login(username=f"fictional-user-{index}", REMOTE_ADDR="192.0.2.3")
            self.assertEqual(response.status_code, 200)
        self.assertEqual(self.post_login(username="another", REMOTE_ADDR="192.0.2.3").status_code, 429)
        self.assertEqual(LocalLoginThrottleBucket.objects.filter(scope="pair").count(), SOURCE_LIMIT)

    def test_forwarded_headers_cannot_override_remote_source(self):
        for index in range(PAIR_LIMIT):
            self.post_login(REMOTE_ADDR="192.0.2.4", HTTP_X_FORWARDED_FOR=f"198.51.100.{index}")
        response = self.post_login(REMOTE_ADDR="192.0.2.4", HTTP_X_FORWARDED_FOR="203.0.113.1")
        self.assertEqual(response.status_code, 429)

    def test_invalid_address_fails_closed(self):
        self.assertEqual(self.post_login(REMOTE_ADDR="not-an-ip").status_code, 429)
        self.assertFalse(LocalLoginThrottleBucket.objects.exists())

    def test_new_time_window_restores_budget_and_prune_only_removes_expired(self):
        start = timezone.now()
        with patch("publishing.local_login_throttle.timezone.now", return_value=start):
            for _ in range(PAIR_LIMIT):
                self.post_login()
            self.assertEqual(self.post_login().status_code, 429)
        later = start + timedelta(seconds=WINDOW_SECONDS + 1)
        with patch("publishing.local_login_throttle.timezone.now", return_value=later):
            self.assertEqual(self.post_login().status_code, 200)
        self.assertEqual(LocalLoginThrottleBucket.objects.count(), 4)
        with patch("publishing.management.commands.prune_local_login_throttle.timezone.now", return_value=later):
            output = StringIO()
            call_command("prune_local_login_throttle", stdout=output)
        self.assertEqual(LocalLoginThrottleBucket.objects.count(), 2)
        self.assertIn("removed: 2", output.getvalue())

    def test_get_and_missing_csrf_token_do_not_consume_budget(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(client.get(reverse("login")).status_code, 200)
        self.assertEqual(client.post(reverse("login"), {"username": "fictional-author", "password": "wrong"}).status_code, 403)
        self.assertFalse(LocalLoginThrottleBucket.objects.exists())

    @override_settings(IPS_LOCAL_LOGIN_THROTTLE_ENABLED=False)
    def test_disabled_feature_preserves_existing_login(self):
        for _ in range(PAIR_LIMIT + 1):
            self.assertEqual(self.post_login().status_code, 200)
        self.assertFalse(LocalLoginThrottleBucket.objects.exists())

    @override_settings(DEBUG=False)
    def test_feature_is_unavailable_when_debug_is_false(self):
        for _ in range(PAIR_LIMIT + 1):
            self.assertEqual(self.post_login().status_code, 200)
        self.assertFalse(LocalLoginThrottleBucket.objects.exists())


@override_settings(IPS_LOCAL_LOGIN_THROTTLE_KEY=TEST_KEY)
class PostgreSQLLoginThrottleConcurrencyTests(TransactionTestCase):
    @skipUnless(connection.vendor == "postgresql", "PostgreSQL row-locking test")
    def test_concurrent_requests_cannot_exceed_pair_budget(self):
        count = 12
        barrier = Barrier(count)
        now = timezone.now()

        def attempt(_):
            close_old_connections()
            try:
                barrier.wait(timeout=15)
                return consume_budget("192.0.2.89", "fictional-author", now=now)[0]
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=count) as pool:
            allowed = list(pool.map(attempt, range(count)))
        self.assertEqual(sum(allowed), PAIR_LIMIT)
        self.assertEqual(LocalLoginThrottleBucket.objects.get(scope="source").attempts, count)
        self.assertEqual(LocalLoginThrottleBucket.objects.get(scope="pair").attempts, PAIR_LIMIT)
