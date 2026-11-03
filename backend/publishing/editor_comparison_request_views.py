"""Explicit in-app requests for an author's fictional-data AI permission."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .ai_views import _policy
from .author_notices import visible_author_notices
from .editor_comparison_consent import PROVIDER, PURPOSE, active_consent, source_fingerprint
from .editor_comparison_requests import current_permission_request
from .editor_revision_views import author_current_revision_package, current_revision_package
from .models import AuthorNotice, EditorComparisonPermissionRequest, Journal, Submission
from .views import _author_can_view, _event

TOKEN_SALT = "publishing.editor-ai-permission-request.v1"
TOKEN_MAX_AGE = 7 * 24 * 60 * 60


def _policy_enabled(policy):
    return bool(policy and policy.editor_revision_comparison_enabled and 1 <= policy.journal_daily_limit <= 100)


def _snapshot(user, current, decision, policy):
    return {"submission": str(current.submission_id), "requester": user.pk, "recipient": current.submission.author_id,
            "round": current.pk, "version": current.version_id, "decision": decision.pk,
            "source": source_fingerprint(current, decision), "provider": PROVIDER, "purpose": PURPOSE,
            "policy_revision": policy.revision if policy else 0}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def request_author_editor_ai_permission(request, pk):
    if not settings.DEBUG:
        raise Http404
    current, decision = current_revision_package(request.user, pk)
    policy = _policy(current.submission.journal)
    existing = current_permission_request(current, decision)
    consent = active_consent(current, decision)
    if request.method == "GET":
        return render(request, "publishing/request_author_editor_ai_permission.html", {
            "submission": current.submission, "current": current, "decision": decision, "existing": existing,
            "consent": consent, "policy_enabled": _policy_enabled(policy),
            "author_active": current.submission.author.is_active,
            "request_token": signing.dumps(_snapshot(request.user, current, decision, policy), salt=TOKEN_SALT, compress=True),
        })
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        signed = signing.loads(request.POST.get("request_token", ""), salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return HttpResponse(_("The request form expired or changed; reload the page."), status=409)
    with transaction.atomic():
        actor = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=current.submission.journal_id)
        get_object_or_404(Submission.objects.select_for_update(), pk=pk)
        current, decision = current_revision_package(actor, pk)
        policy = _policy(current.submission.journal, locked=True)
        if signed != _snapshot(actor, current, decision, policy):
            return HttpResponse(_("The revision or AI policy changed; reload the page."), status=409)
        existing = current_permission_request(current, decision)
        if existing:
            return redirect("request_author_editor_ai_permission", pk=pk)
        if not _policy_enabled(policy) or not current.submission.author.is_active:
            return HttpResponse(_("The journal policy or author account does not permit a request."), status=409)
        if active_consent(current, decision):
            return HttpResponse(_("The author has already allowed this use for the current revision."), status=409)
        permission_request = EditorComparisonPermissionRequest.objects.create(
            submission=current.submission, review_round=current, version=current.version, decision=decision,
            requested_by=actor, recipient_id=current.submission.author_id, provider=PROVIDER, purpose=PURPOSE,
            source_fingerprint=source_fingerprint(current, decision))
        AuthorNotice.objects.create(submission=current.submission, recipient_id=current.submission.author_id,
                                    kind=AuthorNotice.Kind.AI_PERMISSION, ai_permission_request=permission_request)
        _event(current.submission, actor, "editor_ai_permission_requested")
    messages.success(request, _("An in-app request was recorded for the submitting author. No email was sent."))
    return redirect("request_author_editor_ai_permission", pk=pk)


@login_required
@never_cache
@require_GET
def author_editor_ai_request(request, pk):
    notice = get_object_or_404(visible_author_notices(request.user).filter(kind=AuthorNotice.Kind.AI_PERMISSION)
        .select_related("submission__journal__organisation", "ai_permission_request"), pk=pk)
    if not _author_can_view(request.user, notice.submission):
        raise Http404
    current, decision = author_current_revision_package(request.user, notice.submission_id)
    current_request = current_permission_request(current, decision)
    return render(request, "publishing/author_editor_ai_request.html", {"notice": notice,
        "permission_request": notice.ai_permission_request, "submission": notice.submission,
        "is_current": bool(current_request and current_request.pk == notice.ai_permission_request_id)})
