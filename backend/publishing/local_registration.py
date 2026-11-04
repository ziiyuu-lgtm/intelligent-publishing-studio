"""Fictional, opt-in account lifecycle demonstration. This verifies no mailbox.

The one-time code is intentionally shown in the same browser for local testing.
Never use this workflow for real people or a public service.
"""
from datetime import timedelta
import secrets
import uuid

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac
from django.utils.translation import gettext_lazy as _

from .local_review_mail import _fictional_address
from .models import LocalTestRegistration

MAX_ATTEMPTS = 5
LIFETIME = timedelta(minutes=15)


def enabled():
    key = getattr(settings, "IPS_LOCAL_REGISTRATION_KEY", "")
    return bool(settings.DEBUG and getattr(settings, "IPS_LOCAL_REGISTRATION_ENABLED", False)
                and isinstance(key, str) and len(key) >= 32 and not any(c in key for c in "\r\n\0"))


def _digest(scope, value):
    return salted_hmac("ips.local_registration." + scope, value,
                       secret=settings.IPS_LOCAL_REGISTRATION_KEY, algorithm="sha256").hexdigest()


def _address_digest(address):
    return _digest("address", address.casefold())


def _token_digest(registration_id, token, password_hash):
    # A changed password invalidates a pending test link without storing it.
    return _digest("token", str(registration_id) + "\0" + token + "\0" + password_hash)


class FictionalRegistrationForm(UserCreationForm):
    email = forms.EmailField(label=_("Fictional email address"), max_length=254,
                             help_text=_("Use an invented address ending in .invalid."))

    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username", "email")

    def clean_username(self):
        username = super().clean_username()
        if username and not username.startswith("test_"):
            raise forms.ValidationError(_("A fictional username must start with test_."))
        return username

    def clean_email(self):
        address = _fictional_address(self.cleaned_data["email"])
        if not address:
            raise forms.ValidationError(_("Use an invented address ending in .invalid."))
        if get_user_model().objects.filter(email__iexact=address).exists():
            raise forms.ValidationError(_("This fictional address is already in use."))
        return address


def create_attempt(form):
    """Create an inactive user and an attempt atomically; no email is sent."""
    if not enabled():
        raise RuntimeError("The fictional registration demonstration is disabled.")
    token = secrets.token_urlsafe(32)
    attempt_id = uuid.uuid4()
    with transaction.atomic():
        user = form.save(commit=False)
        user.is_active = False
        user.save()
        LocalTestRegistration.objects.create(
            id=attempt_id, user=user, address_digest=_address_digest(user.email),
            token_digest=_token_digest(attempt_id, token, user.password), expires_at=timezone.now() + LIFETIME,
        )
    return attempt_id, token


def activate_attempt(attempt_id, token):
    """Activate at most once after a test token check. This proves no identity."""
    if not enabled():
        return False
    try:
        attempt_id = uuid.UUID(str(attempt_id))
    except (TypeError, ValueError, AttributeError):
        return False
    if not isinstance(token, str) or len(token) > 128 or not token.isascii():
        token = ""
    with transaction.atomic():
        attempt = LocalTestRegistration.objects.select_for_update().filter(pk=attempt_id).first()
        if not attempt:
            return False
        user = get_user_model().objects.select_for_update().get(pk=attempt.user_id)
        address = _fictional_address(user.email)
        if (attempt.activated_at is not None or attempt.expires_at <= timezone.now()
                or attempt.failed_attempts >= MAX_ATTEMPTS or user.is_active
                or user.is_staff or user.is_superuser or not address
                or not constant_time_compare(attempt.address_digest, _address_digest(address))):
            return False
        if not constant_time_compare(attempt.token_digest, _token_digest(attempt.pk, token, user.password)):
            attempt.failed_attempts += 1
            attempt.save(update_fields=["failed_attempts"])
            return False
        user.is_active = True
        user.save(update_fields=["is_active"])
        attempt.activated_at = timezone.now()
        attempt.save(update_fields=["activated_at"])
        return True
