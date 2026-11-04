"""Separate test-only email-link simulation; never sends an actual message."""
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods

from .local_mail_verification import enabled, simulate_confirmation_message
from .local_registration import FictionalRegistrationForm, activate_attempt, create_attempt


@sensitive_post_parameters("password1", "password2")
@never_cache
@require_http_methods(["GET", "POST"])
def register_by_fictional_mail(request):
    if not enabled():
        raise Http404
    if request.user.is_authenticated:
        return redirect("workspace")
    form = FictionalRegistrationForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        if form.is_valid():
            try:
                with transaction.atomic():
                    attempt_id, token = create_attempt(form)
                    # This avoids generating a message if the transaction rolls
                    # back. A production system still needs a durable outbox.
                    transaction.on_commit(lambda: simulate_confirmation_message(attempt_id, token), robust=True)
            except IntegrityError:
                pass  # A duplicate is answered in the same way as other inputs.
        return render(request, "registration/test_mail_check.html")
    return render(request, "registration/test_mail_register.html", {"form": form})


@sensitive_post_parameters("token")
@never_cache
@require_http_methods(["GET", "POST"])
def confirm_by_fictional_mail(request, attempt_id):
    if not enabled():
        raise Http404
    result = None
    if request.method == "POST":
        result = "confirmed" if activate_attempt(attempt_id, request.POST.get("token", "")) else "invalid"
    response = render(request, "registration/test_mail_confirm.html", {"result": result})
    response["Referrer-Policy"] = "no-referrer"
    return response
