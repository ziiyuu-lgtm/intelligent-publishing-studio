"""Editor-initiated AI comparison of explicitly selected, authorised review excerpts."""
import os
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .ai_gateway import OPENAI, REVIEW_SYNTHESIS, run_ai_task
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since
from .ai_views import _policy, _provider_configured
from .models import Journal, ReviewAIConsent, ReviewAssignment, ReviewRound, ReviewSynthesisRun, Submission
from .review_ai_consent import (_digest, author_active, eligible_reports, existing_author,
    report_fingerprint, reviewer_active, round_fingerprint)
from .review_synthesis_provider import LABELS, MAX_EXCERPT, MAX_REPORTS, MIN_REPORTS, PROMPT_VERSION
from .views import _author_can_view, _editor_can_view, _event, _review, _submission

SALT = 'publishing.review-synthesis.request.v1'
MAX_AGE = 60 * 60


def _enabled(policy):
    return bool(policy and policy.review_synthesis_enabled and 1 <= policy.journal_daily_limit <= 100)


def _state(actor, submission_id, number):
    if not settings.DEBUG:
        raise Http404
    submission = _submission(submission_id)
    if not _editor_can_view(actor, submission):
        raise Http404
    round_ = get_object_or_404(ReviewRound.objects.select_related('version', 'submission'),
        submission=submission, number=number, version__submission=submission)
    reports = eligible_reports(round_)
    author = existing_author(round_, submission.author)
    author = author if _author_can_view(submission.author, submission) and author_active(author, round_, reports) else None
    consents = ReviewAIConsent.objects.filter(assignment__in=reports, subject=ReviewAIConsent.Subject.REVIEWER,
        provider='openai', purpose='review-report-synthesis-v1', ended_at__isnull=True)
    by_assignment = {c.assignment_id:c for c in consents}
    reviewers = {}
    for report in reports:
        if not reviewer_active(by_assignment.get(report.pk), report):
            continue
        try:
            _review(report.pk, report.reviewer)
        except Http404:
            continue
        reviewers[report.pk] = by_assignment[report.pk]
    return {'submission':submission, 'round':round_, 'reports':reports, 'author':author,
            'reviewers':reviewers, 'policy':_policy(submission.journal),
            'source_fingerprint':round_fingerprint(round_, reports)}


def _snapshot(actor, state, request_id):
    policy = state['policy']
    return {'actor':actor.pk, 'submission':str(state['submission'].pk), 'round':state['round'].pk,
        'version':state['round'].version_id, 'source':state['source_fingerprint'], 'request_id':str(request_id),
        'author':str(state['author'].pk) if state['author'] else None,
        'reviewers':[[str(r.pk), str(state['reviewers'][r.pk].pk) if r.pk in state['reviewers'] else None]
                     for r in state['reports']],
        'policy':policy.pk if policy else None, 'policy_revision':policy.revision if policy else 0,
        'policy_enabled':_enabled(policy), 'model':settings.IPS_OPENAI_MODEL}


def _context(request, state, request_id=None, error=None, posted=None):
    request_id = request_id or uuid.uuid4()
    posted = posted or {}
    selected = posted.getlist('selected') if hasattr(posted, 'getlist') else []
    rows = [{'assignment':r, 'permission':r.pk in state['reviewers'], 'selected':str(r.pk) in selected,
             'excerpt':posted.get('excerpt_'+str(r.pk), '')[:MAX_EXCERPT+1]} for r in state['reports']]
    active_count = len(state['reviewers'])
    allowed = bool(state['author'] and active_count >= MIN_REPORTS and _enabled(state['policy']) and _provider_configured())
    candidates = ReviewSynthesisRun.objects.filter(submission=state['submission'], review_round=state['round'],
        requested_by=request.user, source_fingerprint=state['source_fingerprint'])[:10] if state['author'] else []
    history = [run for run in candidates if _readable(run, request.user)]
    return {'submission':state['submission'], 'current':state['round'], 'rows':rows, 'author_permission':bool(state['author']),
        'reviewer_count':active_count, 'policy_enabled':_enabled(state['policy']), 'provider_configured':_provider_configured(),
        'allowed':allowed, 'request_id':request_id, 'scope_token':signing.dumps(_snapshot(request.user, state, request_id),
            salt=SALT, compress=True), 'max_excerpt':MAX_EXCERPT, 'max_reports':MAX_REPORTS, 'history':history, 'error':error}


def _select(request, state):
    selected = request.POST.getlist('selected')
    if not MIN_REPORTS <= len(selected) <= MAX_REPORTS or len(set(selected)) != len(selected):
        return None
    by_id = {str(r.pk):r for r in state['reports']}
    if any(value not in by_id or by_id[value].pk not in state['reviewers'] for value in selected):
        return None
    reports = [r for r in state['reports'] if str(r.pk) in selected]
    sources, bindings = [], []
    for index, report in enumerate(reports):
        excerpt = request.POST.get('excerpt_'+str(report.pk), '').replace('\r\n','\n').replace('\r','\n')
        original = report.comments.replace('\r\n','\n').replace('\r','\n')
        if not excerpt.strip() or len(excerpt) > MAX_EXCERPT or excerpt not in original:
            return None
        label = LABELS[index]
        sources.append({'source':label, 'excerpt':excerpt})
        bindings.append({'source':label, 'assignment':str(report.pk),
            'consent':str(state['reviewers'][report.pk].pk), 'fingerprint':report_fingerprint(report)})
    return sources, bindings


def _readable(run, actor, *, require_policy=False):
    if run.requested_by_id != actor.pk or not actor.is_active:
        return False
    try:
        state = _state(actor, run.submission_id, run.review_round.number)
    except Http404:
        return False
    if (run.review_round_id != state['round'].pk or run.version_id != state['round'].version_id or
            not state['author'] or state['author'].pk != run.author_consent_id or
            not state['source_fingerprint'] or state['source_fingerprint'] != run.source_fingerprint or
            not isinstance(run.sources, list) or not MIN_REPORTS <= len(run.sources) <= MAX_REPORTS):
        return False
    reports = {str(r.pk):r for r in state['reports']}
    seen = set()
    for index, binding in enumerate(run.sources):
        if not isinstance(binding, dict) or set(binding) != {'source','assignment','consent','fingerprint'}:
            return False
        if not all(isinstance(binding[key], str) for key in ('source','assignment','consent','fingerprint')):
            return False
        if binding['source'] != LABELS[index] or binding['assignment'] in seen:
            return False
        seen.add(binding['assignment'])
        report = reports.get(binding['assignment'])
        consent = state['reviewers'].get(report.pk) if report else None
        if (not report or not consent or str(consent.pk) != binding['consent'] or
                report_fingerprint(report) != binding['fingerprint']):
            return False
    if require_policy:
        policy = state['policy']
        if (not _provider_configured() or not _enabled(policy) or policy.revision != run.policy_revision or
                settings.IPS_OPENAI_MODEL != run.model_name):
            return False
    return True


def _lock_scope(actor, submission, round_):
    get_object_or_404(get_user_model().objects.select_for_update(), pk=actor.pk)
    get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
    get_object_or_404(Submission.objects.select_for_update(), pk=submission.pk)
    get_object_or_404(ReviewRound.objects.select_for_update(), pk=round_.pk, submission=submission)
    list(ReviewAssignment.objects.select_for_update().filter(submission=submission, round=round_).order_by('pk'))
    list(ReviewAIConsent.objects.select_for_update().filter(submission=submission, review_round=round_).order_by('pk'))


@login_required
@never_cache
@require_http_methods(['GET','POST'])
def request_synthesis(request, pk, number):
    state = _state(request.user, pk, number)
    if request.method == 'GET':
        return render(request, 'publishing/review_synthesis.html', _context(request, state))
    if request.POST.get('confirm') != 'yes' or request.POST.get('synthetic') != 'yes':
        return HttpResponse(_('Both confirmations are required.'), status=400)
    try:
        request_id = uuid.UUID(request.POST.get('request_id', ''))
        signed = signing.loads(request.POST.get('scope_token', ''), salt=SALT, max_age=MAX_AGE)
    except (ValueError, TypeError, AttributeError, signing.BadSignature):
        return HttpResponse(_('The review or permission changed; reload the page.'), status=409)
    if signed != _snapshot(request.user, state, request_id):
        return HttpResponse(_('The review or permission changed; reload the page.'), status=409)
    selection = _select(request, state)
    if selection is None:
        return render(request, 'publishing/review_synthesis.html', _context(request, state, request_id,
            _('Choose two to four permitted reports and copy an exact, non-empty excerpt from each.'), request.POST), status=400)
    sources, bindings = selection
    if not state['author'] or not _enabled(state['policy']) or not _provider_configured():
        return HttpResponse(_('The required author permission, journal policy or server configuration is unavailable.'), status=409)
    digest = _digest({'sources':sources, 'bindings':bindings, 'round':state['source_fingerprint']})
    with transaction.atomic():
        _lock_scope(request.user, state['submission'], state['round'])
        actor = get_user_model().objects.get(pk=request.user.pk)
        state = _state(actor, pk, number)
        if (signed != _snapshot(actor, state, request_id) or not state['author'] or
                not _enabled(state['policy']) or not _provider_configured() or _select(request, state) != (sources, bindings)):
            return HttpResponse(_('The review or permission changed; reload the page.'), status=409)
        existing = ReviewSynthesisRun.objects.filter(pk=request_id).first()
        if existing:
            if existing.requested_by_id != actor.pk or existing.submission_id != state['submission'].pk:
                raise Http404
            if existing.input_fingerprint != digest or not _readable(existing, actor):
                return HttpResponse(_('This request has changed; start a new one.'), status=409)
            return redirect('review_synthesis_result', pk=existing.pk)
        since = timezone.now() - timedelta(hours=24)
        if (account_requests_since(actor, since) >= DAILY_REQUEST_LIMIT or
                journal_requests_since(state['submission'].journal, since) >= state['policy'].journal_daily_limit):
            return HttpResponse(_('The local test request limit has been reached.'), status=429)
        run = ReviewSynthesisRun.objects.create(id=request_id, submission=state['submission'], review_round=state['round'],
            version=state['round'].version, requested_by=actor, author_consent=state['author'], sources=bindings,
            model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION, policy_revision=state['policy'].revision,
            source_fingerprint=state['source_fingerprint'], input_fingerprint=digest)
        _event(state['submission'], actor, 'review_synthesis_requested')
    actor = get_user_model().objects.get(pk=request.user.pk)
    if not _readable(run, actor, require_policy=True):
        run.status, run.error_code, run.finished_at = run.Status.CANCELLED, 'authorization_changed', timezone.now()
        run.save(update_fields=['status','error_code','finished_at'])
        return HttpResponse(_('Permissions changed; the request was cancelled.'), status=409)
    try:
        result = run_ai_task(task=REVIEW_SYNTHESIS, provider=OPENAI, api_key=os.environ.get('OPENAI_API_KEY',''),
                             model=run.model_name, payload={'sources':sources})
        failed = False
    except Exception:
        result, failed = [], True
    with transaction.atomic():
        _lock_scope(actor, state['submission'], state['round'])
        actor = get_user_model().objects.get(pk=actor.pk)
        run = ReviewSynthesisRun.objects.select_for_update().get(pk=run.pk)
        if not _readable(run, actor, require_policy=True):
            run.status, run.error_code = run.Status.CANCELLED, 'authorization_changed'
        elif failed:
            run.status, run.error_code = run.Status.FAILED, 'provider_unavailable'
        else:
            run.status, run.result = run.Status.COMPLETED, result
        run.finished_at = timezone.now()
        run.save(update_fields=['status','error_code','result','finished_at'])
    if run.status == run.Status.CANCELLED:
        return HttpResponse(_('Permissions changed; the request was cancelled.'), status=409)
    return redirect('review_synthesis_result', pk=run.pk)


@login_required
@never_cache
@require_GET
def synthesis_result(request, pk):
    run = get_object_or_404(ReviewSynthesisRun.objects.select_related('review_round', 'submission__journal'),
                            pk=pk, requested_by=request.user)
    if not _readable(run, request.user):
        raise Http404
    ids = [binding['assignment'] for binding in run.sources]
    reports = {str(item.pk):item for item in ReviewAssignment.objects.filter(pk__in=ids).select_related('reviewer')}
    labels = [{'label':binding['source'], 'reviewer':reports[binding['assignment']].reviewer.get_username()}
              for binding in run.sources]
    return render(request, 'publishing/review_synthesis_result.html', {'run':run, 'submission':run.submission, 'labels':labels})
