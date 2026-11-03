"""In-app requests for individually scoped review-comparison permission."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.paginator import Paginator
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .models import ReviewAIConsent, ReviewAIPermissionRequest
from .review_ai_consent import (PROVIDER, PURPOSE, author_active, existing_author, existing_reviewer,
    report_fingerprint, reviewer_active)
from .review_ai_consent_views import _reviewer_may_grant
from .review_ai_requests import current_request, entry_current, visible_entries
from .review_synthesis_views import _enabled, _lock_scope, _state
from .views import _author_can_view, _event

SALT = 'publishing.review-ai.permission-request.v1'
MAX_AGE = 3600


def _snapshot(actor, state):
    policy = state['policy']
    return {'actor':actor.pk, 'submission':str(state['submission'].pk), 'round':state['round'].pk,
        'version':state['round'].version_id, 'source':state['source_fingerprint'],
        'author':str(state['author'].pk) if state['author'] else None,
        'reports':[[str(r.pk), r.reviewer_id, report_fingerprint(r),
            str(state['reviewers'][r.pk].pk) if r.pk in state['reviewers'] else None] for r in state['reports']],
        'policy':policy.pk if policy else None, 'policy_revision':policy.revision if policy else 0,
        'enabled':_enabled(policy)}


def _rows(state):
    submission, round_ = state['submission'], state['round']
    fingerprint = state['source_fingerprint']
    author_request = current_request(round_, submission.author, ReviewAIConsent.Subject.AUTHOR, fingerprint)
    author_available = bool(fingerprint and _author_can_view(submission.author, submission))
    author = {'subject':'author', 'person':submission.author, 'permission':bool(state['author']),
              'request':author_request, 'available':author_available}
    rows = []
    for report in state['reports']:
        rows.append({'subject':str(report.pk), 'person':report.reviewer, 'assignment':report,
            'permission':report.pk in state['reviewers'],
            'request':current_request(round_, report.reviewer, ReviewAIConsent.Subject.REVIEWER,
                                      report_fingerprint(report), assignment=report),
            'available':_reviewer_may_grant(report.reviewer, report)})
    return author, rows


def _conflict():
    return HttpResponse(_('The source, permission or journal policy changed; reload the page.'), status=409)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def request_permissions(request, pk, number):
    state = _state(request.user, pk, number)
    author, reviewers = _rows(state)
    if request.method == 'GET':
        return render(request, 'publishing/review_ai_requests.html', {
            'submission':state['submission'], 'current':state['round'], 'author_row':author,
            'rows':reviewers, 'policy_enabled':_enabled(state['policy']),
            'token':signing.dumps(_snapshot(request.user, state), salt=SALT, compress=True)})
    if request.POST.get('confirm') != 'yes' or request.POST.get('synthetic') != 'yes':
        return HttpResponse(_('Both confirmations are required.'), status=400)
    selected = request.POST.getlist('selected')
    if not 1 <= len(selected) <= 50 or len(set(selected)) != len(selected):
        return HttpResponse(_('Select between one and fifty recipients.'), status=400)
    try:
        signed = signing.loads(request.POST.get('token', ''), salt=SALT, max_age=MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return _conflict()
    if signed != _snapshot(request.user, state) or not _enabled(state['policy']):
        return _conflict()
    with transaction.atomic():
        _lock_scope(request.user, state['submission'], state['round'])
        actor = get_user_model().objects.get(pk=request.user.pk)
        state = _state(actor, pk, number)
        if signed != _snapshot(actor, state) or not _enabled(state['policy']):
            return _conflict()
        author, reviewers = _rows(state)
        by_key = {row['subject']:row for row in [author] + reviewers}
        if any(key not in by_key or not by_key[key]['available'] or by_key[key]['permission'] for key in selected):
            return _conflict()
        for key in selected:
            row = by_key[key]
            # Existing requests, including a decline, are never sent again for this scope.
            if row['request']:
                continue
            is_author = key == 'author'
            ReviewAIPermissionRequest.objects.create(submission=state['submission'], review_round=state['round'],
                version=state['round'].version, assignment=None if is_author else row['assignment'],
                requested_by=actor, recipient=row['person'],
                subject=ReviewAIConsent.Subject.AUTHOR if is_author else ReviewAIConsent.Subject.REVIEWER,
                provider=PROVIDER, purpose=PURPOSE,
                source_fingerprint=state['source_fingerprint'] if is_author else report_fingerprint(row['assignment']))
            _event(state['submission'], actor, 'review_ai_permission_requested')
    messages.success(request, _('The selected in-app requests are recorded. No email or AI request was sent.'))
    return redirect('review_ai_request_permissions', pk=pk, number=number)


@login_required
@never_cache
@require_GET
def permission_inbox(request):
    if not settings.DEBUG:
        raise Http404
    entries = visible_entries(request.user)
    page = Paginator(entries, 20).get_page(request.GET.get('page'))
    rows = []
    for entry in page.object_list:
        current = entry_current(entry)
        active = (author_active(existing_author(entry.review_round, request.user), entry.review_round)
                  if entry.subject == ReviewAIConsent.Subject.AUTHOR else
                  reviewer_active(existing_reviewer(entry.assignment, request.user), entry.assignment))
        rows.append({'entry':entry, 'current':current, 'permission_active':active})
    return render(request, 'publishing/review_ai_request_inbox.html', {'rows':rows, 'page':page})
