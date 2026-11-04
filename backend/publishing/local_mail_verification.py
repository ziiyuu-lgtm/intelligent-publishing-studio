"""Fictional email-link experiment; no SMTP, real addresses, or delivery.

The in-memory message is useful only to tests in this process. This is not a
production mail queue or proof of control of a real mailbox.
"""
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.mail import EmailMessage
from django.core.mail.backends.locmem import EmailBackend
from django.urls import reverse
from django.utils import timezone
from django.utils.crypto import constant_time_compare

from .local_registration import MAX_ATTEMPTS, _address_digest, _token_digest, enabled as registration_enabled
from .local_review_mail import _fictional_address
from .models import LocalTestRegistration


def enabled():
    return bool(registration_enabled() and getattr(settings, "IPS_LOCAL_MAIL_VERIFICATION_ENABLED", False))


def simulate_confirmation_message(attempt_id, token):
    """Put one fictional confirmation message in memory for a test to inspect."""
    if not enabled():
        raise ImproperlyConfigured("The fictional email confirmation experiment is disabled.")
    if not isinstance(token, str) or len(token) != 43 or not token.isascii():
        raise ValueError("Invalid fictional confirmation request.")
    attempt = LocalTestRegistration.objects.select_related("user").get(pk=attempt_id)
    user = attempt.user
    address = _fictional_address(user.email)
    if (not address or user.is_active or user.is_staff or user.is_superuser or attempt.activated_at
            or attempt.expires_at <= timezone.now() or attempt.failed_attempts >= MAX_ATTEMPTS
            or not constant_time_compare(attempt.address_digest, _address_digest(address))
            or not constant_time_compare(attempt.token_digest, _token_digest(attempt.pk, token, user.password))):
        raise ValueError("Invalid fictional confirmation request.")

    # A fragment is not sent in the HTTP request. The domain cannot receive
    # actual email and the in-memory backend is chosen directly, ignoring SMTP
    # or any other globally configured email backend.
    link = "https://mailbox.example.invalid" + reverse("test_mail_confirmation", args=[attempt.pk]) + "#" + token
    backend = EmailBackend()
    message = EmailMessage(
        subject="Fictional account confirmation",
        body="This link is for an isolated test. No email was sent.\n" + link,
        from_email="no-reply@example.invalid", to=[address], connection=backend,
    )
    if backend.send_messages([message]) != 1:
        raise RuntimeError("The fictional message simulation failed.")
