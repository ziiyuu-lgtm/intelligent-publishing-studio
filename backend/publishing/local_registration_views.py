"""Opt-in fictional account demonstration, with no external communication."""
import uuid

from django.contrib import messages
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .local_registration import FictionalRegistrationForm, MAX_ATTEMPTS, activate_attempt, create_attempt, enabled
from .models import LocalTestRegistration

SESSION_KEY = "local_test_registration"


@sensitive_post_parameters("password1", "password2")
@never_cache
@require_http_methods(["GET", "POST"])
def register_test_account(request):
    if not enabled():
        raise Http404
    if request.user.is_authenticated:
        return redirect("workspace")
    form = FictionalRegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            attempt_id, token = create_attempt(form)
        except IntegrityError:
            form.add_error(None, _("This fictional username or address is already in use."))
        else:
            request.session.cycle_key()
            request.session[SESSION_KEY] = {"id": str(attempt_id), "token": token}
            return redirect("test_registration_confirm")
    return render(request, "registration/test_register.html", {"form": form})


def _pending(request):
    entry = request.session.get(SESSION_KEY)
    if not isinstance(entry, dict) or not isinstance(entry.get("token"), str):
        return None
    try:
        attempt_id = uuid.UUID(entry.get("id", ""))
    except (ValueError, TypeError, AttributeError):
        request.session.pop(SESSION_KEY, None)
        return None
    attempt = LocalTestRegistration.objects.filter(pk=attempt_id).first()
    if (not attempt or attempt.activated_at is not None or attempt.expires_at <= timezone.now()
            or attempt.failed_attempts >= MAX_ATTEMPTS):
        request.session.pop(SESSION_KEY, None)
        return None
    return entry


@sensitive_post_parameters("token")
@never_cache
@require_http_methods(["GET", "POST"])
def confirm_test_account(request):
    if not enabled():
        raise Http404
    entry = _pending(request)
    failed = False
    if request.method == "POST" and entry:
        if activate_attempt(entry["id"], request.POST.get("token", "")):
            request.session.pop(SESSION_KEY, None)
            messages.success(request, _("Fictional account activated. No email was sent or verified."))
            return redirect("login")
        failed = True
        entry = _pending(request)
    return render(request, "registration/test_register_confirm.html", {"entry": entry, "failed": failed})
