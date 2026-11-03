"""Explicitly confirmed AI revision-planning requests for fictional test text."""
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
from django.utils.translation import gettext_lazy as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .ai_gateway import OPENAI, REVISION_PLAN, run_ai_task
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since
from .ai_views import _policy, _provider_configured
from .models import EditorialDecision, Journal, ReviewRound, RevisionPlanRun, Submission
from .revision_plan_provider import MAX_COMMENT, MAX_NOTES, PROMPT_VERSION, input_fingerprint
from .views import _author_can_view, _event, _submission


class RevisionPlanForm(forms.Form):
    comment = forms.CharField(label=_("Editorial comment to address"), max_length=MAX_COMMENT,
                              widget=forms.Textarea(attrs={"rows": 7}))
    notes = forms.CharField(label=_("Your notes (optional)"), max_length=MAX_NOTES, required=False,
                            widget=forms.Textarea(attrs={"rows": 4}))


def _current_decision(submission):
    if submission.status != Submission.Status.REVISION_REQUESTED or submission.rounds.filter(status=ReviewRound.Status.OPEN).exists():
        return None
    latest = submission.versions.order_by("-number").first()
    if not latest:
        return None
    return (EditorialDecision.objects.filter(submission=submission, round__submission=submission,
        round__version=latest, round__version__submission=submission, round__status=ReviewRound.Status.CLOSED,
        outcome__in=[EditorialDecision.Outcome.MINOR, EditorialDecision.Outcome.MAJOR])
        .select_related("round__version").order_by("-round__number", "-pk").first())


def _enabled(policy):
    return bool(policy and policy.revision_plan_enabled and 1 <= policy.journal_daily_limit <= 100)


def _context(request, submission, decision, policy, form=None):
    runs = RevisionPlanRun.objects.filter(submission=submission, requested_by=request.user,
        decision__submission=submission, decision__round__submission=submission,
        decision__round__version__submission=submission)[:20]
    return {"submission": submission, "decision": decision, "policy": policy,
            "policy_revision": policy.revision if policy else 0, "policy_enabled": _enabled(policy),
            "provider_configured": _provider_configured(), "model_name": settings.IPS_OPENAI_MODEL,
            "request_id": uuid.uuid4(), "form": form if form is not None else RevisionPlanForm(), "runs": runs}


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def revision_plan(request, pk):
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    decision = _current_decision(submission)
    if not decision:
        return HttpResponse(_("A current revision decision is required."), status=409)
    policy = _policy(submission.journal)
    if request.method == "GET":
        return render(request, "publishing/revision_plan.html", _context(request, submission, decision, policy))
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        run_id = uuid.UUID(request.POST.get("request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Invalid request identifier."), status=400)
    previous = RevisionPlanRun.objects.filter(pk=run_id).first()
    if previous:
        if previous.requested_by_id != request.user.pk or previous.submission_id != submission.pk:
            raise Http404
        return redirect("revision_plan_result", pk=previous.pk)
    form = RevisionPlanForm(request.POST)
    if not form.is_valid():
        return render(request, "publishing/revision_plan.html", _context(request, submission, decision, policy, form), status=400)
    comment, notes = form.cleaned_data["comment"], form.cleaned_data["notes"]
    if not _provider_configured() or not _enabled(policy):
        return HttpResponse(_("AI revision planning is not enabled for this journal and test environment."), status=409)
    if (request.POST.get("policy_revision") != str(policy.revision) or
            request.POST.get("decision_id") != str(decision.pk) or request.POST.get("model") != settings.IPS_OPENAI_MODEL):
        return HttpResponse(_("The decision or AI configuration changed; review the current details."), status=409)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(request.user, submission):
            raise Http404
        policy = _policy(submission.journal, locked=True)
        decision = _current_decision(submission)
        if (not _provider_configured() or not _enabled(policy) or not decision or
                request.POST.get("policy_revision") != str(policy.revision) or
                request.POST.get("decision_id") != str(decision.pk) or request.POST.get("model") != settings.IPS_OPENAI_MODEL):
            return HttpResponse(_("The decision or AI configuration changed; review the current details."), status=409)
        previous = RevisionPlanRun.objects.filter(pk=run_id).first()
        if previous:
            if previous.requested_by_id != request.user.pk or previous.submission_id != submission.pk:
                raise Http404
            return redirect("revision_plan_result", pk=previous.pk)
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT or journal_requests_since(submission.journal, since) >= policy.journal_daily_limit:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        fingerprint = input_fingerprint(decision.pk, comment, notes)
        run = RevisionPlanRun.objects.create(id=run_id, submission=submission, decision=decision,
            requested_by=request.user, model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION,
            policy_revision=policy.revision, input_fingerprint=fingerprint)
        policy_snapshot = (policy.pk, policy.revision, policy.revision_plan_enabled, policy.journal_daily_limit)
        _event(submission, request.user, "revision_ai_requested")

    # The check cannot recall an external request after it has begun.
    actor = get_user_model().objects.get(pk=request.user.pk)
    fresh = _submission(submission.pk)
    current = _current_decision(fresh)
    fresh_policy = _policy(fresh.journal)
    snapshot = ((fresh_policy.pk, fresh_policy.revision, fresh_policy.revision_plan_enabled,
                 fresh_policy.journal_daily_limit) if fresh_policy else None)
    if (not _provider_configured() or settings.IPS_OPENAI_MODEL != run.model_name or
            not _author_can_view(actor, fresh) or not current or current.pk != run.decision_id or
            not _enabled(fresh_policy) or snapshot != policy_snapshot):
        run.status, run.error_code, run.finished_at = RevisionPlanRun.Status.CANCELLED, "authorization_changed", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
        return redirect("revision_plan_result", pk=run.pk)
    try:
        result = run_ai_task(task=REVISION_PLAN, provider=OPENAI, api_key=os.environ.get("OPENAI_API_KEY", ""),
                             model=run.model_name, payload={"comment": comment, "notes": notes})
    except Exception:
        run.status, run.error_code, run.finished_at = RevisionPlanRun.Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.result, run.finished_at = RevisionPlanRun.Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "result", "finished_at"])
    return redirect("revision_plan_result", pk=run.pk)


@login_required
@never_cache
@require_GET
def revision_plan_result(request, pk):
    run = get_object_or_404(RevisionPlanRun.objects.select_related(
        "submission__journal__organisation", "decision__round__version"), pk=pk, requested_by=request.user)
    if (not _author_can_view(request.user, run.submission) or run.decision.submission_id != run.submission_id or
            run.decision.round.submission_id != run.submission_id or
            run.decision.round.version.submission_id != run.submission_id):
        raise Http404
    current = _current_decision(run.submission)
    can_start_response = bool(current and current.pk == run.decision_id and
                              run.status == RevisionPlanRun.Status.COMPLETED and run.result)
    return render(request, "publishing/revision_plan_result.html", {"run": run, "submission": run.submission,
                                                                     "can_start_response": can_start_response})
