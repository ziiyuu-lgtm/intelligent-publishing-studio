"""Submitting-author control over editorial AI comparison of fictional revisions."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .editor_comparison_consent import PROVIDER, PURPOSE, active_consent, source_fingerprint
from .editor_comparison_requests import current_permission_request
from .editor_revision_views import author_current_revision_package
from .models import EditorAIPermissionReplyNotice, EditorComparisonPermissionRequest, EditorRevisionComparisonConsent, Journal, Submission
from .views import _author_can_view, _event, _submission

TOKEN_SALT = "publishing.editor-comparison-consent.v1"
TOKEN_MAX_AGE = 7 * 24 * 60 * 60


def _existing(submission, user, locked=False):
    consents = EditorRevisionComparisonConsent.objects.select_for_update() if locked else EditorRevisionComparisonConsent.objects
    return consents.filter(submission=submission, granted_by=user, provider=PROVIDER, purpose=PURPOSE,
                           ended_at__isnull=True).first()


def _snapshot(submission, user, current, decision, existing):
    return {"submission": str(submission.pk), "author": user.pk, "provider": PROVIDER, "purpose": PURPOSE,
            "round": current.pk if current else None, "version": current.version_id if current else None,
            "decision": decision.pk if decision else None,
            "source": source_fingerprint(current, decision) if current and decision else None,
            "consent": str(existing.pk) if existing else None}


def _context(submission, user, current, decision, existing):
    active = active_consent(current, decision)
    return {"submission": submission, "current": current, "decision": decision, "consent": active,
            "permission_request": current_permission_request(current, decision),
            "prior_consent": existing if existing and (not active or existing.pk != active.pk) else None,
            "consent_token": signing.dumps(_snapshot(submission, user, current, decision, existing), salt=TOKEN_SALT, compress=True)}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def author_editor_ai_consent(request, pk):
    if not settings.DEBUG:
        raise Http404
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    current, decision = author_current_revision_package(request.user, pk)
    existing = _existing(submission, request.user)
    if request.method == "GET":
        return render(request, "publishing/author_editor_ai_consent.html", _context(submission, request.user, current, decision, existing))
    action = request.POST.get("action")
    if action not in ("grant", "withdraw", "decline"):
        return HttpResponse(_("Invalid authorisation action."), status=400)
    if action == "grant" and (request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes"):
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        signed = signing.loads(request.POST.get("consent_token", ""), salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return HttpResponse(_("The authorisation form expired or changed; reload the page."), status=409)
    with transaction.atomic():
        author = get_user_model().objects.select_for_update().get(pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(author, submission):
            raise Http404
        current, decision = author_current_revision_package(author, pk)
        existing = _existing(submission, author, locked=True)
        if signed != _snapshot(submission, author, current, decision, existing):
            return HttpResponse(_("The authorisation or revision changed; reload the page."), status=409)
        active = active_consent(current, decision)
        permission_request = current_permission_request(current, decision)
        if action == "withdraw":
            if existing:
                existing.ended_at, existing.end_reason = timezone.now(), EditorRevisionComparisonConsent.EndReason.WITHDRAWN
                existing.save(update_fields=["ended_at", "end_reason"])
                _event(submission, author, "editor_ai_consent_withdrawn")
        elif action == "decline":
            if (active or not permission_request or permission_request.status != EditorComparisonPermissionRequest.Status.PENDING or
                    request.POST.get("permission_request_id") != str(permission_request.pk)):
                return HttpResponse(_("The permission request changed; reload the page."), status=409)
            permission_request.status, permission_request.responded_at = EditorComparisonPermissionRequest.Status.DECLINED, timezone.now()
            permission_request.save(update_fields=["status", "responded_at"])
            EditorAIPermissionReplyNotice.objects.create(permission_request=permission_request,
                recipient_id=permission_request.requested_by_id, outcome=permission_request.status)
            _event(submission, author, "editor_ai_permission_declined")
        else:
            if not current or not decision:
                return HttpResponse(_("A current returned revision is required."), status=409)
            if existing and (not active or existing.pk != active.pk):
                existing.ended_at, existing.end_reason = timezone.now(), EditorRevisionComparisonConsent.EndReason.SUPERSEDED
                existing.save(update_fields=["ended_at", "end_reason"])
                _event(submission, author, "editor_ai_consent_superseded")
                existing = None
            if not existing:
                EditorRevisionComparisonConsent.objects.create(submission=submission, review_round=current,
                    version=current.version, decision=decision, granted_by=author, provider=PROVIDER,
                    purpose=PURPOSE, source_fingerprint=source_fingerprint(current, decision))
                _event(submission, author, "editor_ai_consent_granted")
            if permission_request and permission_request.status == EditorComparisonPermissionRequest.Status.PENDING:
                permission_request.status, permission_request.responded_at = EditorComparisonPermissionRequest.Status.GRANTED, timezone.now()
                permission_request.save(update_fields=["status", "responded_at"])
                EditorAIPermissionReplyNotice.objects.create(permission_request=permission_request,
                    recipient_id=permission_request.requested_by_id, outcome=permission_request.status)
    feedback = {"withdraw": _("Authorisation withdrawn for future requests."),
                "decline": _("The in-app request was declined. No AI request was made."),
                "grant": _("Authorisation recorded for this revision.")}
    messages.success(request, feedback[action])
    return redirect("author_editor_ai_consent", pk=pk)
