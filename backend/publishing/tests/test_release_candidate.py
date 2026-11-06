import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest import skipUnless
from unittest.mock import patch

from django.core.checks import run_checks
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.test import Client, SimpleTestCase, override_settings

from config.unavailable_email import EmailBackend
from publishing.release_guard import BlockAllRequestsMiddleware


class BlockedCandidateTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ips-release-candidate-")
        self.addCleanup(self.temp.cleanup)
        self.env = {
            "DJANGO_SECRET_KEY": "synthetic-secret-ABCDEFGHIJKLMNOPQRSTUVWXYZ-1234567890-abcdef",
            "IPS_REVIEW_AI_FINGERPRINT_KEY": "different-synthetic-fingerprint-1234567890-ABCDEFGH-uvwxyz",
            "IPS_PRIVATE_DATA_ROOT": self.temp.name,
            "IPS_ALLOWED_HOSTS": "altavien.example,www.altavien.example",
            "IPS_DB_NAME": "altavien_candidate",
            "IPS_DB_USER": "altavien_candidate",
            "IPS_DB_PASSWORD": "fictional-db-password",
            "IPS_DB_PORT": "5432",
            "IPS_OPENAI_ENABLED": "1",
            "IPS_OPENAI_IMAGE_ENABLED": "1",
            "IPS_LOCAL_PUBLICATION_ENABLED": "1",
            "IPS_LOCAL_REGISTRATION_ENABLED": "1",
            "IPS_LOCAL_MAIL_VERIFICATION_ENABLED": "1",
            "ALTAVIEN_SMTP_HOST": "smtp.example.invalid",
            "ALTAVIEN_SMTP_PORT": "587",
            "ALTAVIEN_SMTP_SECURITY": "starttls",
            "ALTAVIEN_SMTP_USERNAME": "fictional-smtp-user",
            "ALTAVIEN_SMTP_PASSWORD": "fictional-smtp-password",
            "ALTAVIEN_SMTP_FROM": "no-reply@altavien.com",
        }

    def load_candidate(self, **changes):
        env = dict(self.env)
        for key, value in changes.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        with patch.dict(os.environ, env, clear=True):
            module = importlib.import_module("config.settings_release_candidate")
            return importlib.reload(module)

    def test_separate_configuration_is_locked_and_disables_external_services(self):
        candidate = self.load_candidate()
        self.assertFalse(candidate.DEBUG)
        self.assertTrue(candidate.IPS_RELEASE_CANDIDATE_BLOCKED)
        self.assertFalse(candidate.IPS_LOCAL_PUBLICATION_ENABLED)
        self.assertFalse(candidate.IPS_OPENAI_ENABLED)
        self.assertFalse(candidate.IPS_OPENAI_IMAGE_ENABLED)
        self.assertFalse(candidate.IPS_LOCAL_LOGIN_THROTTLE_ENABLED)
        self.assertFalse(candidate.IPS_LOCAL_REVIEW_MAIL_ENABLED)
        self.assertFalse(candidate.IPS_LOCAL_REGISTRATION_ENABLED)
        self.assertFalse(candidate.IPS_LOCAL_MAIL_VERIFICATION_ENABLED)
        self.assertEqual(candidate.EMAIL_BACKEND, "config.unavailable_email.EmailBackend")
        self.assertEqual(candidate.DATABASES["default"]["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(candidate.DATABASES["default"]["HOST"], "127.0.0.1")
        self.assertEqual(candidate.ALLOWED_HOSTS, ["altavien.example", "www.altavien.example"])
        self.assertTrue(candidate.SESSION_COOKIE_SECURE)
        self.assertTrue(candidate.CSRF_COOKIE_SECURE)
        self.assertTrue(candidate.SECURE_SSL_REDIRECT)
        self.assertIsNone(candidate.SECURE_PROXY_SSL_HEADER)
        self.assertEqual(candidate.MIDDLEWARE[0], "publishing.release_guard.BlockAllRequestsMiddleware")
        self.assertEqual(candidate.MEDIA_ROOT, Path(self.temp.name) / "private-media")
        self.assertEqual(candidate.SECRET_KEY, self.load_candidate().SECRET_KEY)

    def test_incomplete_or_unsafe_configuration_fails_closed(self):
        cases = [
            {"DJANGO_SECRET_KEY": None},
            {"DJANGO_SECRET_KEY": "short"},
            {"IPS_REVIEW_AI_FINGERPRINT_KEY": self.env["DJANGO_SECRET_KEY"]},
            {"IPS_PRIVATE_DATA_ROOT": "relative/path"},
            {"IPS_PRIVATE_DATA_ROOT": str(Path(__file__).resolve().parents[2])},
            {"IPS_ALLOWED_HOSTS": "*.example.com"},
            {"IPS_ALLOWED_HOSTS": ".example.com"},
            {"IPS_ALLOWED_HOSTS": "https://example.com"},
            {"IPS_ALLOWED_HOSTS": "example.com:443"},
            {"IPS_ALLOWED_HOSTS": "example.com,"},
            {"IPS_DB_NAME": "invalid-db"},
            {"IPS_DB_PASSWORD": None},
            {"IPS_DB_PORT": "65536"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ImproperlyConfigured) as context:
                self.load_candidate(**case)
            self.assertNotIn(self.env["DJANGO_SECRET_KEY"], str(context.exception))
            self.assertNotIn(self.env["IPS_DB_PASSWORD"], str(context.exception))

    def test_non_private_directory_and_symlink_are_rejected(self):
        os.chmod(self.temp.name, 0o750)
        with self.assertRaises(ImproperlyConfigured):
            self.load_candidate()
        os.chmod(self.temp.name, 0o700)
        link = Path(self.temp.name).parent / (Path(self.temp.name).name + "-link")
        link.symlink_to(self.temp.name)
        self.addCleanup(link.unlink)
        with self.assertRaises(ImproperlyConfigured):
            self.load_candidate(IPS_PRIVATE_DATA_ROOT=str(link))

    @skipUnless(hasattr(os, "geteuid"), "POSIX directory ownership check")
    def test_private_root_must_be_owned_by_the_service_account(self):
        with patch.object(os, "geteuid", return_value=os.geteuid() + 1):
            with self.assertRaises(ImproperlyConfigured):
                self.load_candidate()

    def test_existing_media_directory_must_be_private_and_not_a_symlink(self):
        media = Path(self.temp.name) / "private-media"
        self.assertEqual(self.load_candidate().MEDIA_ROOT, media)
        self.assertFalse(media.exists())
        with tempfile.TemporaryDirectory(prefix="ips-untrusted-media-") as outside:
            media.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ImproperlyConfigured):
                self.load_candidate()
            media.unlink()
            media.symlink_to(Path(outside) / "missing", target_is_directory=True)
            with self.assertRaises(ImproperlyConfigured):
                self.load_candidate()
            media.unlink()
        media.write_text("not a directory")
        with self.assertRaises(ImproperlyConfigured):
            self.load_candidate()
        media.unlink()
        media.mkdir(mode=0o700)
        os.chmod(media, 0o755)
        with self.assertRaises(ImproperlyConfigured):
            self.load_candidate()
        os.chmod(media, 0o700)
        self.assertEqual(self.load_candidate().MEDIA_ROOT, media)

    @skipUnless(hasattr(os, "geteuid"), "POSIX directory ownership check")
    def test_existing_media_directory_must_be_owned_by_the_service_account(self):
        media = Path(self.temp.name) / "private-media"
        media.mkdir(mode=0o700)
        original = Path.lstat

        def foreign_owner(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path == media:
                fields = list(info)
                fields[4] = os.geteuid() + 1
                return os.stat_result(fields)
            return info

        with patch.object(Path, "lstat", autospec=True, side_effect=foreign_owner):
            with self.assertRaises(ImproperlyConfigured):
                self.load_candidate()

    def test_release_guard_never_calls_the_application(self):
        def app(request):
            self.fail("The release guard must not call the application")

        response = BlockAllRequestsMiddleware(app)(object())
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertEqual(response.content, b"Service unavailable.\n")

    @override_settings(MIDDLEWARE=["publishing.release_guard.BlockAllRequestsMiddleware"])
    def test_request_methods_and_private_paths_are_blocked(self):
        client = Client(enforce_csrf_checks=True)
        for method, path in [("get", "/"), ("get", "/admin/"), ("post", "/login/"), ("get", "/private-media-is-not-served/a.pdf")]:
            with self.subTest(method=method, path=path):
                response = getattr(client, method)(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_mail_cannot_be_silently_sent_or_discarded(self):
        backend = EmailBackend()
        self.assertEqual(backend.send_messages([]), 0)
        with self.assertRaises(ImproperlyConfigured):
            backend.send_messages([EmailMessage("test", "fictional", "from@example.invalid", ["to@example.invalid"])])

    @override_settings(IPS_RELEASE_CANDIDATE_BLOCKED=True)
    def test_deployment_check_reports_the_explicit_release_block(self):
        errors = run_checks(include_deployment_checks=True)
        self.assertIn("publishing.E001", [item.id for item in errors])

    def test_security_check_with_test_sqlite_still_fails_release_gate(self):
        # This exercises Django's deployment checks only. It does not test a
        # PostgreSQL connection or change the candidate's real DB setting.
        script = r'''
import os
import config.settings_release_candidate as candidate
candidate.DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings_release_candidate'
import django
django.setup()
from django.core.checks import run_checks
issues = run_checks(include_deployment_checks=True)
ids = {item.id for item in issues}
assert 'publishing.E001' in ids, ids
# Subdomains and preload stay disabled deliberately until deployment scope and
# certificate handling have been reviewed; Django suggests enabling both.
assert {item for item in ids if item.startswith('security.W')} == {'security.W005', 'security.W021'}, ids
print('blocked candidate security check exercised with test SQLite')
'''
        env = dict(os.environ)
        env.update(self.env)
        result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                                env=env, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("blocked candidate security check", result.stdout)
