from io import StringIO
import os
import ssl
from unittest.mock import patch

from django.conf import settings
from django.core.mail.backends.smtp import EmailBackend
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from config.smtp_policy import SMTPConfigurationError, SMTPPolicy


FAKE = {
    "ALTAVIEN_SMTP_HOST": "smtp.example.invalid",
    "ALTAVIEN_SMTP_PORT": "587",
    "ALTAVIEN_SMTP_SECURITY": "starttls",
    "ALTAVIEN_SMTP_USERNAME": "fictional-smtp-user",
    "ALTAVIEN_SMTP_PASSWORD": "fictional-smtp-password",
    "ALTAVIEN_SMTP_FROM": "no-reply@altavien.com",
}


class SMTPPolicyTests(SimpleTestCase):
    def test_starttls_and_implicit_tls_are_exclusive_and_verify_certificates(self):
        for mode in ("starttls", "tls"):
            with self.subTest(mode=mode), patch("smtplib.SMTP") as smtp, patch("smtplib.SMTP_SSL") as smtp_ssl:
                policy = SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_SECURITY=mode))
                options = policy.backend_options()
                self.assertEqual(options["use_tls"], mode == "starttls")
                self.assertEqual(options["use_ssl"], mode == "tls")
                self.assertEqual(options["timeout"], 15)
                self.assertFalse(options["fail_silently"])
                backend = EmailBackend(**options)
                self.assertTrue(backend.ssl_context.check_hostname)
                self.assertEqual(backend.ssl_context.verify_mode, ssl.CERT_REQUIRED)
                smtp.assert_not_called()
                smtp_ssl.assert_not_called()

    def test_host_must_be_a_hostname_without_urls_or_control_characters(self):
        for host in ("", "localhost", "smtp", "127.0.0.1", "::1", "*.example.invalid",
                     "https://smtp.example.invalid", "smtp.example.invalid:587", "smtp_bad.example.invalid",
                     "smtp.example.invalid\nextra", "smtp.example.123", "mail..example.invalid"):
            with self.subTest(host=host), self.assertRaises(SMTPConfigurationError) as caught:
                SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_HOST=host))
            self.assertIn("ALTAVIEN_SMTP_HOST", str(caught.exception))
            if host:
                self.assertNotIn(host, str(caught.exception))

    def test_sender_must_be_an_altavien_domain_address(self):
        for address in ("bad", "noreply@example.invalid", "noreply@altavien.com.example.invalid",
                        "Name <noreply@altavien.com>", "noreply@altavien.com\r\nBcc:fake@example.invalid"):
            with self.subTest(address=address), self.assertRaises(SMTPConfigurationError) as caught:
                SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_FROM=address))
            self.assertNotIn(address, str(caught.exception))
        policy = SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_FROM="sender@mail.altavien.com"))
        self.assertEqual(policy.from_address, "sender@mail.altavien.com")

    def test_ports_timeouts_and_tls_modes_are_bounded(self):
        for key, value in (("ALTAVIEN_SMTP_PORT", "0"), ("ALTAVIEN_SMTP_PORT", "65536"),
                           ("ALTAVIEN_SMTP_PORT", "+587"), ("ALTAVIEN_SMTP_PORT", "１２３"),
                           ("ALTAVIEN_SMTP_TIMEOUT", "0"), ("ALTAVIEN_SMTP_TIMEOUT", "121"),
                           ("ALTAVIEN_SMTP_SECURITY", "none"), ("ALTAVIEN_SMTP_SECURITY", "ssl-or-tls")):
            with self.subTest(key=key, value=value), self.assertRaises(SMTPConfigurationError):
                SMTPPolicy.from_mapping(dict(FAKE, **{key: value}))
        policy = SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_TIMEOUT="30"))
        self.assertEqual(policy.timeout, 30)

    def test_required_credentials_are_not_printed_in_repr_or_errors(self):
        policy = SMTPPolicy.from_mapping(FAKE)
        self.assertNotIn(FAKE["ALTAVIEN_SMTP_USERNAME"], repr(policy))
        self.assertNotIn(FAKE["ALTAVIEN_SMTP_PASSWORD"], repr(policy))
        self.assertNotIn(FAKE["ALTAVIEN_SMTP_FROM"], repr(policy))
        for key in FAKE:
            missing = dict(FAKE)
            missing.pop(key)
            with self.subTest(key=key), self.assertRaises(SMTPConfigurationError) as caught:
                SMTPPolicy.from_mapping(missing)
            self.assertNotIn(FAKE["ALTAVIEN_SMTP_USERNAME"], str(caught.exception))
            self.assertNotIn(FAKE["ALTAVIEN_SMTP_PASSWORD"], str(caught.exception))

    def test_password_spaces_are_preserved_but_controls_are_rejected(self):
        value = " fictional password "
        policy = SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_PASSWORD=value))
        self.assertEqual(policy.backend_options()["password"], value)
        for value in ("", "   ", "bad\npassword", "bad\x00password", "bad\u0085password"):
            with self.subTest(value=repr(value)), self.assertRaises(SMTPConfigurationError):
                SMTPPolicy.from_mapping(dict(FAKE, ALTAVIEN_SMTP_PASSWORD=value))

    @override_settings(EMAIL_BACKEND="config.unavailable_email.EmailBackend")
    def test_management_command_is_offline_and_does_not_enable_mail_or_print_credentials(self):
        output = StringIO()
        with patch.dict(os.environ, FAKE, clear=True), patch("socket.create_connection") as connect, \
                patch("socket.getaddrinfo") as dns, patch("smtplib.SMTP") as smtp, patch("smtplib.SMTP_SSL") as smtp_ssl:
            call_command("check_smtp_config", stdout=output)
        self.assertIn("ALTAVIEN_SMTP_FORMAT_VALID", output.getvalue())
        for key in ("ALTAVIEN_SMTP_USERNAME", "ALTAVIEN_SMTP_PASSWORD", "ALTAVIEN_SMTP_FROM"):
            self.assertNotIn(FAKE[key], output.getvalue())
        self.assertEqual(settings.EMAIL_BACKEND, "config.unavailable_email.EmailBackend")
        connect.assert_not_called()
        dns.assert_not_called()
        smtp.assert_not_called()
        smtp_ssl.assert_not_called()

    def test_invalid_command_error_never_contains_values(self):
        output = StringIO()
        env = dict(FAKE, ALTAVIEN_SMTP_HOST="not-a-host-" + FAKE["ALTAVIEN_SMTP_PASSWORD"])
        with patch.dict(os.environ, env, clear=True), self.assertRaises(CommandError) as caught:
            call_command("check_smtp_config", stdout=output)
        self.assertNotIn(FAKE["ALTAVIEN_SMTP_PASSWORD"], str(caught.exception))
        self.assertNotIn(FAKE["ALTAVIEN_SMTP_USERNAME"], str(caught.exception))
        self.assertEqual(output.getvalue(), "")
