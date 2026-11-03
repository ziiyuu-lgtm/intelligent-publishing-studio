"""Explicit, editor-controlled comparison of two fictional source excerpts."""
import os
import uuid
from datetime import timedelta

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as lazy_
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .ai_gateway import EDITOR_REVISION_COMPARISON, OPENAI, run_ai_task
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since
from .ai_views import _policy, _provider_configured
from .editor_comparison_consent import active_consent, source_fingerprint as _source_fingerprint
from .editor_revision_comparison_provider import MAX_EXCERPT, PROMPT_VERSION, fingerprint
from .editor_revision_views import current_revision_package
from .models import EditorRevisionComparisonRun, Journal, Submission
from .views import _event


class ComparisonForm(forms.Form):
    decision_excerpt = forms.CharField(label=lazy_("Selected decision excerpt"), max_length=MAX_EXCERPT, strip=False,
                                      widget=forms.Textarea(attrs={"rows": 5}))
    response_excerpt = forms.CharField(label=lazy_("Selected author response excerpt"), max_length=MAX_EXCERPT, strip=False,
                                      widget=forms.Textarea(attrs={"rows": 5}))

    def __init__(self, *args, decision_text, response_text, **kwargs):
        super().__init__(*args, **kwargs)
        self.decision_text, self.response_text = decision_text, response_text

    def clean(self):
        data = super().clean()
        for field, source in (("decision_excerpt", self.decision_text), ("response_excerpt", self.response_text)):
            value = data.get(field)
            if isinstance(value, str):
                value = value.replace("\r\n", "\n").replace("\r", "\n")
                data[field] = value
            if value is not None and (not value.strip() or value not in source):
                self.add_error(field, lazy_("Copy an exact, non-empty excerpt from the displayed source."))
        return data


def _enabled(policy):
    return bool(policy and policy.editor_revision_comparison_enabled and 1 <= policy.journal_daily_limit <= 100)


def _context(request, current, decision, policy, form=None):
    runs = EditorRevisionComparisonRun.objects.filter(submission=current.submission, review_round=current,
        version=current.version, decision=decision, requested_by=request.user,
        source_fingerprint=_source_fingerprint(current, decision))[:20]
    if form is None:
        form = ComparisonForm(decision_text=decision.author_message, response_text=current.version.author_response)
    return {"submission": current.submission, "current": current, "decision": decision, "form": form,
            "policy_revision": policy.revision if policy else 0, "policy_enabled": _enabled(policy),
            "provider_configured": _provider_configured(), "model_name": settings.IPS_OPENAI_MODEL,
            "author_consent": active_consent(current, decision),
            "source_fingerprint": _source_fingerprint(current, decision), "request_id": uuid.uuid4(), "runs": runs}


def _matches(run, current, decision):
    return (run.submission_id == current.submission_id and run.review_round_id == current.pk and
            run.version_id == current.version_id and run.decision_id == decision.pk and
            run.source_fingerprint == _source_fingerprint(current, decision))


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def editor_revision_comparison(request, pk):
    current, decision = current_revision_package(request.user, pk)
    policy = _policy(current.submission.journal)
    if request.method == "GET":
        return render(request, "publishing/editor_revision_comparison.html", _context(request, current, decision, policy))
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        run_id = uuid.UUID(request.POST.get("request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Invalid request identifier."), status=400)
    existing = EditorRevisionComparisonRun.objects.filter(pk=run_id).first()
    if existing:
        if existing.requested_by_id != request.user.pk or existing.submission_id != current.submission_id:
            raise Http404
        if not _matches(existing, current, decision):
            return HttpResponse(_("The revision package changed; reload the page."), status=409)
        return redirect("editor_revision_comparison_result", pk=existing.pk)
    form = ComparisonForm(request.POST, decision_text=decision.author_message, response_text=current.version.author_response)
    if not form.is_valid():
        return render(request, "publishing/editor_revision_comparison.html", _context(request, current, decision, policy, form), status=400)
    selected = {key: form.cleaned_data[key] for key in ("decision_excerpt", "response_excerpt")}
    if not _provider_configured() or not _enabled(policy):
        return HttpResponse(_("AI revision comparison is not enabled for this journal and test environment."), status=409)
    consent = active_consent(current, decision)
    if not consent or request.POST.get("author_consent_id") != str(consent.pk):
        return HttpResponse(_("The author's current authorisation is required; reload the page."), status=409)
    source_digest = _source_fingerprint(current, decision)
    if (request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("decision_id") != str(decision.pk) or
            request.POST.get("round_id") != str(current.pk) or request.POST.get("version_id") != str(current.version_id) or
            request.POST.get("source_fingerprint") != source_digest or request.POST.get("model") != settings.IPS_OPENAI_MODEL):
        return HttpResponse(_("The revision package or AI configuration changed; review the current details."), status=409)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=current.submission.journal_id)
        get_object_or_404(Submission.objects.select_for_update(), pk=pk)
        current, decision = current_revision_package(request.user, pk)
        policy = _policy(current.submission.journal, locked=True)
        consent = active_consent(current, decision)
        if (not _provider_configured() or not _enabled(policy) or request.POST.get("policy_revision") != str(policy.revision) or
                not consent or request.POST.get("author_consent_id") != str(consent.pk) or
                request.POST.get("decision_id") != str(decision.pk) or request.POST.get("round_id") != str(current.pk) or
                request.POST.get("version_id") != str(current.version_id) or request.POST.get("model") != settings.IPS_OPENAI_MODEL or
                source_digest != _source_fingerprint(current, decision) or selected["decision_excerpt"] not in decision.author_message or
                selected["response_excerpt"] not in current.version.author_response):
            return HttpResponse(_("The revision package or AI configuration changed; review the current details."), status=409)
        existing = EditorRevisionComparisonRun.objects.filter(pk=run_id).first()
        if existing:
            if existing.requested_by_id != request.user.pk or not _matches(existing, current, decision):
                raise Http404
            return redirect("editor_revision_comparison_result", pk=existing.pk)
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT or journal_requests_since(current.submission.journal, since) >= policy.journal_daily_limit:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        run = EditorRevisionComparisonRun.objects.create(id=run_id, submission=current.submission, review_round=current,
            version=current.version, decision=decision, requested_by=request.user, author_consent=consent, model_name=settings.IPS_OPENAI_MODEL,
            prompt_version=PROMPT_VERSION, policy_revision=policy.revision, source_fingerprint=source_digest,
            input_fingerprint=fingerprint(**selected))
        policy_snapshot = (policy.pk, policy.revision, policy.editor_revision_comparison_enabled, policy.journal_daily_limit)
        _event(current.submission, request.user, "editor_revision_comparison_requested")

    # A request already sent to an external provider cannot be recalled.
    actor = get_user_model().objects.get(pk=request.user.pk)
    try:
        fresh_current, fresh_decision = current_revision_package(actor, pk)
        fresh_policy = _policy(fresh_current.submission.journal)
    except Http404:
        fresh_current, fresh_decision, fresh_policy = None, None, None
    snapshot = ((fresh_policy.pk, fresh_policy.revision, fresh_policy.editor_revision_comparison_enabled,
                 fresh_policy.journal_daily_limit) if fresh_policy else None)
    fresh_consent = active_consent(fresh_current, fresh_decision)
    if (not _provider_configured() or settings.IPS_OPENAI_MODEL != run.model_name or not fresh_current or
            not fresh_consent or fresh_consent.pk != run.author_consent_id or
            not _matches(run, fresh_current, fresh_decision) or not _enabled(fresh_policy) or snapshot != policy_snapshot):
        run.status, run.error_code, run.finished_at = run.Status.CANCELLED, "authorization_changed", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
        return redirect("editor_revision_comparison_result", pk=run.pk)
    try:
        result = run_ai_task(task=EDITOR_REVISION_COMPARISON, provider=OPENAI, api_key=os.environ.get("OPENAI_API_KEY", ""),
                             model=run.model_name, payload=selected)
    except Exception:
        run.status, run.error_code, run.finished_at = run.Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.result, run.finished_at = run.Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "result", "finished_at"])
    return redirect("editor_revision_comparison_result", pk=run.pk)


@login_required
@never_cache
@require_GET
def editor_revision_comparison_result(request, pk):
    run = get_object_or_404(EditorRevisionComparisonRun, pk=pk, requested_by=request.user)
    current, decision = current_revision_package(request.user, run.submission_id)
    if not _matches(run, current, decision):
        raise Http404
    return render(request, "publishing/editor_revision_comparison_result.html", {"run": run, "submission": current.submission})
