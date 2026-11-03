"""Opt-in keyword suggestions, scoped to the saved fictional draft metadata."""
import os
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
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .ai_gateway import KEYWORD_SUGGESTIONS, OPENAI, run_ai_task
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since
from .ai_views import _policy, _provider_configured
from .draft_edit_views import _version
from .keyword_ai_provider import PROMPT_VERSION, input_fingerprint
from .keywords import validate_keywords
from .models import Journal, KeywordSuggestionRun, Submission
from .views import _author_can_view, _event, _submission


def _enabled(policy):
    return bool(policy and policy.keyword_suggestions_enabled and 1 <= policy.journal_daily_limit <= 100)


def _fingerprint(submission, version):
    return input_fingerprint(version.pk, submission.title, submission.abstract, submission.keywords_config)


def _current(submission):
    version = _version(submission)
    if not version or not submission.title.strip() or not submission.abstract.strip():
        return None
    try:
        validate_keywords(submission.keywords_config)
        validate_keywords(version.keywords_snapshot)
    except ValueError:
        return None
    if (version.title_snapshot != submission.title or version.abstract_snapshot != submission.abstract or
            version.keywords_snapshot != submission.keywords_config):
        return None
    return version


def _snapshot(policy):
    return (policy.pk, policy.revision, policy.keyword_suggestions_enabled, policy.journal_daily_limit) if policy else None


def _author(request, submission):
    if not settings.DEBUG or not _author_can_view(request.user, submission):
        raise Http404


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def keyword_suggestions(request, pk):
    submission = _submission(pk)
    _author(request, submission)
    version = _current(submission)
    if not version:
        return HttpResponse(_('A consistent saved draft with a title and abstract is required.'), status=409)
    policy = _policy(submission.journal)
    if request.method == 'GET':
        return render(request, 'publishing/keyword_ai.html', {'submission':submission, 'version':version,
            'policy_enabled':_enabled(policy), 'provider_configured':_provider_configured(),
            'policy_revision':policy.revision if policy else 0, 'model_name':settings.IPS_OPENAI_MODEL,
            'fingerprint':_fingerprint(submission, version), 'request_id':uuid.uuid4(),
            'runs':KeywordSuggestionRun.objects.filter(submission=submission, requested_by=request.user)[:10]})
    if request.POST.get('confirm') != 'yes' or request.POST.get('synthetic') != 'yes':
        return HttpResponse(_('Both confirmations are required.'), status=400)
    try:
        run_id = uuid.UUID(request.POST.get('request_id', ''))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_('Invalid request identifier.'), status=400)
    previous = KeywordSuggestionRun.objects.filter(pk=run_id).first()
    if previous:
        if previous.requested_by_id != request.user.pk or previous.submission_id != submission.pk:
            raise Http404
        return redirect('keyword_result', pk=previous.pk)
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=pk)
        if not _author_can_view(actor, submission):
            raise Http404
        policy = _policy(submission.journal, locked=True)
        version = _current(submission)
        if (not version or not _provider_configured() or not _enabled(policy) or
            request.POST.get('policy_revision') != str(policy.revision) or
            request.POST.get('model') != settings.IPS_OPENAI_MODEL or
            request.POST.get('fingerprint') != _fingerprint(submission, version)):
            return HttpResponse(_('The draft or AI configuration changed; review the current details.'), status=409)
        previous = KeywordSuggestionRun.objects.filter(pk=run_id).first()
        if previous:
            if previous.requested_by_id != actor.pk or previous.submission_id != submission.pk:
                raise Http404
            return redirect('keyword_result', pk=previous.pk)
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(actor, since) >= DAILY_REQUEST_LIMIT or journal_requests_since(submission.journal, since) >= policy.journal_daily_limit:
            return HttpResponse(_('The local test request limit has been reached.'), status=429)
        title, abstract = submission.title, submission.abstract
        run = KeywordSuggestionRun.objects.create(id=run_id, submission=submission, version=version,
            requested_by=actor, model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION,
            policy_revision=policy.revision, input_fingerprint=_fingerprint(submission, version))
        policy_snapshot = _snapshot(policy)
        _event(submission, actor, 'keyword_ai_requested')
    actor = get_user_model().objects.get(pk=request.user.pk)
    fresh = _submission(submission.pk)
    current = _current(fresh)
    fresh_policy = _policy(fresh.journal)
    if (not _provider_configured() or settings.IPS_OPENAI_MODEL != run.model_name or
        not _author_can_view(actor, fresh) or not current or current.pk != run.version_id or
        _fingerprint(fresh, current) != run.input_fingerprint or not _enabled(fresh_policy) or _snapshot(fresh_policy) != policy_snapshot):
        run.status, run.error_code, run.finished_at = KeywordSuggestionRun.Status.CANCELLED, 'authorization_changed', timezone.now()
        run.save(update_fields=['status','error_code','finished_at'])
        return redirect('keyword_result', pk=run.pk)
    try:
        result = run_ai_task(task=KEYWORD_SUGGESTIONS, provider=OPENAI, api_key=os.environ.get('OPENAI_API_KEY',''),
            model=run.model_name, payload={'title':title, 'abstract':abstract})
    except Exception:
        result = None
    # Re-check after the response, under the same locks as editing, before retaining suggestions.
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
        fresh = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=pk)
        policy = _policy(fresh.journal, locked=True)
        current = _current(fresh)
        run = KeywordSuggestionRun.objects.select_for_update().get(pk=run.pk)
        if (not _provider_configured() or settings.IPS_OPENAI_MODEL != run.model_name or
            not _author_can_view(actor, fresh) or not current or current.pk != run.version_id or
            _fingerprint(fresh, current) != run.input_fingerprint or not _enabled(policy) or _snapshot(policy) != policy_snapshot):
            run.status, run.error_code = KeywordSuggestionRun.Status.CANCELLED, 'authorization_changed'
        elif result is None:
            run.status, run.error_code = KeywordSuggestionRun.Status.FAILED, 'provider_unavailable'
        else:
            run.status, run.result = KeywordSuggestionRun.Status.COMPLETED, result
        run.finished_at = timezone.now()
        run.save(update_fields=['status','result','error_code','finished_at'])
    return redirect('keyword_result', pk=run.pk)


def _owned_run(request, pk):
    run = get_object_or_404(KeywordSuggestionRun.objects.select_related('submission__journal__organisation','version'), pk=pk, requested_by=request.user)
    _author(request, run.submission)
    if run.version.submission_id != run.submission_id:
        raise Http404
    return run


@login_required
@never_cache
@require_GET
def keyword_result(request, pk):
    run = _owned_run(request, pk)
    version = _current(run.submission)
    policy = _policy(run.submission.journal)
    applicable = bool(version and version.pk == run.version_id and _fingerprint(run.submission, version) == run.input_fingerprint and
        _enabled(policy) and policy.revision == run.policy_revision and run.status == KeywordSuggestionRun.Status.COMPLETED and
        run.result and not run.applied_at)
    return render(request, 'publishing/keyword_ai_result.html', {'run':run,'submission':run.submission,'applicable':applicable})


@login_required
@never_cache
@require_POST
def apply_keywords(request, pk):
    initial = _owned_run(request, pk)
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Please confirm your selection.'), status=400)
    selected = request.POST.getlist('keyword')
    if not selected or len(selected) > 8:
        return HttpResponse(_('Choose at least one keyword.'), status=400)
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=initial.submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=initial.submission_id)
        run = get_object_or_404(KeywordSuggestionRun.objects.select_for_update().select_related('version'), pk=pk, requested_by=actor)
        if not _author_can_view(actor, submission) or run.version.submission_id != submission.pk:
            raise Http404
        if run.applied_at:
            return redirect('submission_checklist', pk=submission.pk)
        version = _current(submission)
        policy = _policy(submission.journal, locked=True)
        if (not version or version.pk != run.version_id or _fingerprint(submission, version) != run.input_fingerprint or
            not _enabled(policy) or policy.revision != run.policy_revision or run.status != KeywordSuggestionRun.Status.COMPLETED):
            return HttpResponse(_('The draft or AI policy changed; request fresh suggestions.'), status=409)
        try:
            offered = validate_keywords([item['keyword'] for item in run.result])
            validate_keywords(selected)
            current = validate_keywords(submission.keywords_config) or []
            if not set(selected).issubset(set(offered)):
                raise ValueError('unoffered keyword')
            combined = list(current)
            seen = {item.casefold() for item in combined}
            for item in selected:
                if item.casefold() not in seen:
                    combined.append(item)
                    seen.add(item.casefold())
            validate_keywords(combined)
        except (ValueError, TypeError, KeyError):
            return HttpResponse(_('Select valid suggestions and keep the total at ten keywords or fewer.'), status=400)
        submission.keywords_config = combined
        version.keywords_snapshot = combined
        submission.save(update_fields=['keywords_config'])
        version.save(update_fields=['keywords_snapshot'])
        run.applied_at = timezone.now()
        run.save(update_fields=['applied_at'])
        _event(submission, actor, 'keyword_ai_selection_applied')
    messages.success(request, _('Selected keywords were added to your draft. Review them before submitting.'))
    return redirect('submission_checklist', pk=submission.pk)
