import importlib
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase


class PostgreSQLProbeSettingsTests(SimpleTestCase):
    def load_profile(self, *, args=None, database="altavien_probe_0123456789abcdef01234567", confirm="1", uid=None):
        env = {"ALTAVIEN_PROBE_DB": database, "ALTAVIEN_PROBE_CONFIRM_SYNTHETIC": confirm,
               "IPS_OPENAI_ENABLED": "1", "IPS_OPENAI_IMAGE_ENABLED": "1"}
        if uid is None:
            uid = os.geteuid()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(sys, "argv", args or ["manage.py", "test"]), \
             patch("pwd.getpwnam", return_value=SimpleNamespace(pw_uid=uid)):
            module = importlib.import_module("config.settings_postgres_probe")
            return importlib.reload(module)

    def test_profile_targets_only_randomly_named_local_databases(self):
        settings = self.load_profile()
        database = settings.DATABASES["default"]
        self.assertEqual(database["NAME"], "altavien_probe_0123456789abcdef01234567")
        self.assertEqual(database["TEST"]["NAME"], database["NAME"] + "_test")
        self.assertEqual(database["HOST"], "/var/run/postgresql")
        self.assertEqual(database["USER"], "postgres")
        self.assertFalse(settings.IPS_OPENAI_ENABLED)
        self.assertFalse(settings.IPS_OPENAI_IMAGE_ENABLED)
        self.assertEqual(settings.EMAIL_BACKEND, "config.unavailable_email.EmailBackend")

    def test_profile_rejects_other_commands_accounts_and_database_names(self):
        cases = [
            {"args": ["manage.py", "runserver"]},
            {"args": ["manage.py", "migrate"]},
            {"args": ["django-admin", "test"]},
            {"database": "postgres"},
            {"database": "altavien_probe_existing"},
            {"database": "altavien_probe_0123456789abcdef01234567_test"},
            {"confirm": "0"},
            {"uid": os.geteuid() + 1},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ImproperlyConfigured):
                self.load_profile(**case)

    def test_script_has_valid_syntax_and_refuses_an_unexpected_checkout(self):
        script = Path(__file__).resolve().parents[3] / "scripts/run_altavien_postgres_probe.sh"
        checked = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=10)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        if os.geteuid() == 0:
            result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("STOP_UNEXPECTED_CHECKOUT", result.stdout)
