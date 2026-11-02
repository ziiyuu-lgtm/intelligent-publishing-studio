"""Local checks and opt-in, metadata-only AI suggestions for the test workspace."""
import os
import re
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .ai_provider import PROMPT_VERSION, metadata_fingerprint, request_metadata_suggestions
from .models import AIConsent, AIAssistanceRun, Journal, Submission
from .views import _author_can_view, _editor_can_view, _event, _file_exists, _submission

DAILY_REQUEST_LIMIT = 3


def _provider_configured():
    key = os.environ.get("OPENAI_API_KEY", "")
    model = settings.IPS_OPENAI_MODEL
    return bool(settings.IPS_OPENAI_ENABLED and key and "\r" not in key and "\n" not in key and
                re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", model or ""))


def _permitted(user, submission):
    return _author_can_view(user, submission) or _editor_can_view(user, submission)


def _latest_version(submission):
    version = submission.versions.order_by("-number").first()
    if not version:
        raise Http404
    return version


def _active_consent(submission, version, fingerprint):
    return AIConsent.objects.filter(version=version, provider="openai", granted_by=submission.author,
                                    purpose=PROMPT_VERSION, revoked_at__isnull=True, data_fingerprint=fingerprint).first()


def _local_checks(submission, version):
    checks = [
        (_("Title"), bool(submission.title.strip()), _("Checks whether the title field contains text; accuracy is not verified.")),
        (_("Abstract"), bool(submission.abstract.strip()), _("Checks whether the abstract field contains text; content is not verified.")),
        (_("Manuscript PDF"), _file_exists(version.manuscript), _("Checks whether the test file exists, not whether it is safe or valid.")),
    ]
    if submission.review_mode == Journal.ReviewMode.SEPARATE:
        checks.append((_("Separate reviewer PDF"), bool(version.review_copy_confirmed and _file_exists(version.review_copy)),
                       _("A separate reviewer file and author confirmation are required.")))
        if submission.status != Submission.Status.DRAFT:
            checks.append((_("Reviewer file check"), bool(version.review_copy_checked_at and version.review_copy_checked_by_id),
                           _("An editor must record a manual check; anonymity is not guaranteed.")))
    return checks


@login_required
@never_cache
@require_GET
def checks_page(request, pk):
    submission = _submission(pk)
    if not _permitted(request.user, submission):
        raise Http404
    version = _latest_version(submission)
    fingerprint = metadata_fingerprint(version.pk, submission.title, submission.abstract)
    consent = _active_consent(submission, version, fingerprint)
    author_view = _author_can_view(request.user, submission)
    context = {
        "submission": submission, "version": version, "checks": _local_checks(submission, version),
        "author_view": author_view, "provider_configured": _provider_configured(),
        "journal_allows": submission.journal.allow_openai_metadata, "consent": consent,
        "request_id": uuid.uuid4(), "model_name": settings.IPS_OPENAI_MODEL, "fingerprint": fingerprint,
        "runs": AIAssistanceRun.objects.filter(version__submission=submission, requested_by=request.user).select_related("version")[:20],
    }
    if author_view:
        context["consents"] = AIConsent.objects.filter(version__submission=submission, granted_by=request.user,
                                                       revoked_at__isnull=True).select_related("version")
    return render(request, "publishing/checks.html", context)


@login_required
@never_cache
@require_POST
def grant_ai_consent(request, pk):
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(request.user, submission):
            raise Http404
        if not submission.journal.allow_openai_metadata or not _provider_configured():
            return HttpResponse(_("External AI is not enabled for this journal and test environment."), status=409)
        version = _latest_version(submission)
        if request.POST.get("version") != str(version.pk):
            return HttpResponse(_("The manuscript version changed; review the current details."), status=409)
        fingerprint = metadata_fingerprint(version.pk, submission.title, submission.abstract)
        if request.POST.get("fingerprint") != fingerprint:
            return HttpResponse(_("The title or abstract changed; review the current details."), status=409)
        existing = AIConsent.objects.select_for_update().filter(version=version, provider="openai", revoked_at__isnull=True).first()
        if existing and (existing.data_fingerprint != fingerprint or existing.granted_by_id != request.user.pk or existing.purpose != PROMPT_VERSION):
            existing.revoked_at = timezone.now()
            existing.save(update_fields=["revoked_at"])
            _event(submission, request.user, "ai_consent_revoked")
            existing = None
        if not existing:
            AIConsent.objects.create(version=version, provider="openai", purpose=PROMPT_VERSION, granted_by=request.user, data_fingerprint=fingerprint)
            _event(submission, request.user, "ai_consent_granted")
    messages.success(request, _("Authorisation recorded for this version and metadata."))
    return redirect("checks_page", pk=pk)


@login_required
@never_cache
@require_POST
def revoke_ai_consent(request, pk):
    with transaction.atomic():
        consent = get_object_or_404(AIConsent.objects.select_for_update().select_related("version__submission__journal__organisation"), pk=pk)
        submission = consent.version.submission
        if not _author_can_view(request.user, submission) or consent.granted_by_id != request.user.pk:
            raise Http404
        if not consent.revoked_at:
            consent.revoked_at = timezone.now()
            consent.save(update_fields=["revoked_at"])
            _event(submission, request.user, "ai_consent_revoked")
    messages.success(request, _("Authorisation revoked for future requests."))
    return redirect("checks_page", pk=submission.pk)


@login_required
@never_cache
@require_POST
def request_ai_suggestions(request, pk):
    submission = _submission(pk)
    if not _permitted(request.user, submission):
        raise Http404
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        run_id = uuid.UUID(request.POST.get("request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Invalid request identifier."), status=400)
    previous = AIAssistanceRun.objects.filter(pk=run_id).select_related("version").first()
    if previous:
        if previous.requested_by_id != request.user.pk or previous.version.submission_id != submission.pk:
            raise Http404
        return redirect("ai_result", pk=previous.pk)
    if not _provider_configured() or not submission.journal.allow_openai_metadata:
        return HttpResponse(_("External AI is not enabled for this journal and test environment."), status=409)
    if request.POST.get("model") != settings.IPS_OPENAI_MODEL:
        return HttpResponse(_("The AI model configuration changed; review the current details."), status=409)
    with transaction.atomic():
        # Serialize a single account's quota when the database supports row locks.
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _permitted(request.user, submission):
            raise Http404
        if not _provider_configured() or not submission.journal.allow_openai_metadata:
            return HttpResponse(_("External AI is not enabled for this journal and test environment."), status=409)
        if request.POST.get("model") != settings.IPS_OPENAI_MODEL:
            return HttpResponse(_("The AI model configuration changed; review the current details."), status=409)
        previous = AIAssistanceRun.objects.filter(pk=run_id).select_related("version").first()
        if previous:
            if previous.requested_by_id != request.user.pk or previous.version.submission_id != submission.pk:
                raise Http404
            return redirect("ai_result", pk=previous.pk)
        version = _latest_version(submission)
        if request.POST.get("version") != str(version.pk):
            return HttpResponse(_("The manuscript version changed; review the current details."), status=409)
        title, abstract = submission.title, submission.abstract
        fingerprint = metadata_fingerprint(version.pk, title, abstract)
        if request.POST.get("fingerprint") != fingerprint:
            return HttpResponse(_("The title or abstract changed; review the current details."), status=409)
        consent = AIConsent.objects.select_for_update().filter(version=version, provider="openai", granted_by=submission.author,
                    purpose=PROMPT_VERSION, data_fingerprint=fingerprint, revoked_at__isnull=True).first()
        if not consent:
            return HttpResponse(_("The author has not authorised these exact metadata for this version."), status=403)
        since = timezone.now() - timedelta(hours=24)
        if AIAssistanceRun.objects.filter(requested_by=request.user, created_at__gte=since).count() >= DAILY_REQUEST_LIMIT:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        run = AIAssistanceRun.objects.create(id=run_id, version=version, requested_by=request.user, consent=consent,
                   model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION, data_fingerprint=fingerprint)
        _event(submission, request.user, "ai_suggestion_requested")

    # Recheck immediately before sending. A request already in progress cannot be recalled.
    fresh_actor = get_user_model().objects.get(pk=request.user.pk)
    fresh_submission = _submission(submission.pk)
    fresh_version = fresh_submission.versions.order_by("-number").first()
    if (not _provider_configured() or settings.IPS_OPENAI_MODEL != run.model_name or not _permitted(fresh_actor, fresh_submission) or
            not fresh_version or fresh_version.pk != version.pk or
            metadata_fingerprint(version.pk, fresh_submission.title, fresh_submission.abstract) != fingerprint or
            not Journal.objects.filter(pk=submission.journal_id, active=True,
            organisation__active=True, allow_openai_metadata=True).exists() or
            not AIConsent.objects.filter(pk=consent.pk, purpose=PROMPT_VERSION, revoked_at__isnull=True, data_fingerprint=fingerprint).exists()):
        run.status, run.error_code, run.finished_at = AIAssistanceRun.Status.CANCELLED, "authorization_changed", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
        return redirect("ai_result", pk=run.pk)
    try:
        result = request_metadata_suggestions(api_key=os.environ.get("OPENAI_API_KEY", ""), model=run.model_name,
                                              title=title, abstract=abstract)
    except Exception:
        # Do not persist the provider response, exception text, input text, or credentials.
        run.status, run.error_code, run.finished_at = AIAssistanceRun.Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.result, run.finished_at = AIAssistanceRun.Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "result", "finished_at"])
    return redirect("ai_result", pk=run.pk)


@login_required
@never_cache
@require_GET
def ai_result(request, pk):
    run = get_object_or_404(AIAssistanceRun.objects.select_related("version__submission__journal__organisation"), pk=pk, requested_by=request.user)
    submission = run.version.submission
    if not _permitted(request.user, submission):
        raise Http404
    return render(request, "publishing/ai_result.html", {"run": run, "submission": submission})
