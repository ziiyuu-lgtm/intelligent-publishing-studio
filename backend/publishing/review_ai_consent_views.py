"""Separate, reversible author/reviewer permission for a future review AI task."""
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

from .models import Journal, ReviewAIConsent, ReviewAIPermissionRequest, ReviewAssignment, ReviewRound, Submission
from .review_ai_consent import (PROVIDER, PURPOSE, author_active, eligible_reports, existing_author,
    existing_reviewer, report_fingerprint, reviewer_active, round_fingerprint)
from .views import _author_can_view, _event, _review, _submission
from .review_ai_requests import current_request

SALT = 'publishing.review-ai.permission.v1'
MAX_AGE = 60 * 60


def _rounds(submission):
    return list(submission.rounds.filter(version__submission=submission).select_related('version', 'submission').order_by('-number'))


def _select_round(request, rounds):
    values = request.GET.getlist('round')
    if values:
        raw = values[0]
        if len(values) != 1 or not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            raise Http404
        selected = next((item for item in rounds if item.number == int(raw)), None)
        if not selected:
            raise Http404
        return selected
    return next((item for item in rounds if item.status == ReviewRound.Status.OPEN), None) or (rounds[0] if rounds else None)


def _snapshot(actor, submission, round_, assignment, existing, fingerprint, permission_request=None):
    return {'actor': actor.pk, 'submission': str(submission.pk), 'round': round_.pk if round_ else None,
        'version': round_.version_id if round_ else None, 'assignment': str(assignment.pk) if assignment else None,
        'provider': PROVIDER, 'purpose': PURPOSE, 'source': fingerprint,
        'permission': str(existing.pk) if existing else None,
        'request': [str(permission_request.pk), permission_request.status] if permission_request else None}


def _owned_reviewer(assignment_id, actor, locked=False):
    query = ReviewAssignment.objects.select_for_update() if locked else ReviewAssignment.objects
    assignment = get_object_or_404(query.select_related('submission__journal__organisation', 'round__version'),
                                   pk=assignment_id, reviewer=actor)
    if (not actor.is_active or assignment.round.submission_id != assignment.submission_id or
            assignment.round.version.submission_id != assignment.submission_id):
        raise Http404
    return assignment


def _reviewer_may_grant(actor, assignment):
    try:
        _review(assignment.pk, actor)
    except Http404:
        return False
    return report_fingerprint(assignment) is not None


def _end(consent, reason):
    consent.ended_at, consent.end_reason = timezone.now(), reason
    consent.save(update_fields=['ended_at', 'end_reason'])


def _form_token(request):
    try:
        return signing.loads(request.POST.get('token', ''), salt=SALT, max_age=MAX_AGE)
    except (signing.BadSignature, TypeError, ValueError):
        return None


def _invalid():
    return HttpResponse(_('The permission or source changed; reload the page.'), status=409)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def author_permission(request, pk):
    if not settings.DEBUG:
        raise Http404
    submission = _submission(pk)
    if not request.user.is_active or submission.author_id != request.user.pk:
        raise Http404
    rounds = _rounds(submission)
    selected = _select_round(request, rounds)
    may_grant = bool(_author_can_view(request.user, submission) and submission.status != Submission.Status.DRAFT)
    existing = existing_author(selected, request.user) if selected else None
    fingerprint = round_fingerprint(selected) if selected else None
    active = author_active(existing, selected) if selected else False
    permission_request = current_request(selected, request.user, ReviewAIConsent.Subject.AUTHOR, fingerprint)
    if request.method == 'GET':
        token = signing.dumps(_snapshot(request.user, submission, selected, None, existing, fingerprint, permission_request), salt=SALT, compress=True)
        return render(request, 'publishing/review_ai_permission.html', {'kind':'author', 'submission':submission,
            'rounds':rounds, 'selected_round':selected, 'existing':existing, 'active':active,
            'may_grant':may_grant and bool(fingerprint), 'show_title':may_grant, 'token':token,
            'permission_request':permission_request if may_grant else None})
    action = request.POST.get('action')
    if action not in ('grant', 'withdraw', 'decline'):
        return HttpResponse(_('Invalid authorisation action.'), status=400)
    if action == 'grant' and (request.POST.get('confirm') != 'yes' or request.POST.get('synthetic') != 'yes'):
        return HttpResponse(_('Both confirmations are required.'), status=400)
    signed = _form_token(request)
    if not signed or not selected:
        return _invalid()
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=pk)
        if not actor.is_active or submission.author_id != actor.pk:
            raise Http404
        selected = get_object_or_404(ReviewRound.objects.select_for_update().select_related('version', 'submission'),
                                     pk=selected.pk, submission=submission, version__submission=submission)
        # Lock participating assignments before checking the signed scope.
        list(ReviewAssignment.objects.select_for_update().filter(submission=submission, round=selected).order_by('pk'))
        existing = existing_author(selected, actor, locked=True)
        fingerprint = round_fingerprint(selected)
        permission_request = current_request(selected, actor, ReviewAIConsent.Subject.AUTHOR, fingerprint, locked=True)
        if signed != _snapshot(actor, submission, selected, None, existing, fingerprint, permission_request):
            return _invalid()
        if action == 'withdraw':
            if not existing:
                return _invalid()
            _end(existing, ReviewAIConsent.EndReason.WITHDRAWN)
            _event(submission, actor, 'review_ai_author_withdrawn')
        elif action == 'decline':
            if (not _author_can_view(actor, submission) or not permission_request or
                    permission_request.status != ReviewAIPermissionRequest.Status.PENDING or author_active(existing, selected)):
                return _invalid()
            permission_request.status = ReviewAIPermissionRequest.Status.DECLINED
            permission_request.responded_at = timezone.now()
            permission_request.save(update_fields=['status', 'responded_at'])
            _event(submission, actor, 'review_ai_author_declined')
        else:
            if not _author_can_view(actor, submission) or submission.status == Submission.Status.DRAFT or not fingerprint:
                return _invalid()
            if existing and not author_active(existing, selected):
                _end(existing, ReviewAIConsent.EndReason.SUPERSEDED)
                existing = None
            if not existing:
                ReviewAIConsent.objects.create(submission=submission, review_round=selected, version=selected.version,
                    granted_by=actor, subject=ReviewAIConsent.Subject.AUTHOR, provider=PROVIDER,
                    purpose=PURPOSE, source_fingerprint=fingerprint)
                _event(submission, actor, 'review_ai_author_granted')
            if permission_request and permission_request.status == ReviewAIPermissionRequest.Status.PENDING:
                permission_request.status = ReviewAIPermissionRequest.Status.GRANTED
                permission_request.responded_at = timezone.now()
                permission_request.save(update_fields=['status', 'responded_at'])
    messages.success(request, _('Your choice was recorded. No AI request was made.'))
    return redirect(request.path + '?round=' + str(selected.number))


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def reviewer_permission(request, pk):
    if not settings.DEBUG:
        raise Http404
    assignment = _owned_reviewer(pk, request.user)
    submission, selected = assignment.submission, assignment.round
    existing = existing_reviewer(assignment, request.user)
    fingerprint = report_fingerprint(assignment)
    active = reviewer_active(existing, assignment)
    may_grant = _reviewer_may_grant(request.user, assignment)
    permission_request = current_request(selected, request.user, ReviewAIConsent.Subject.REVIEWER, fingerprint, assignment)
    if request.method == 'GET':
        token = signing.dumps(_snapshot(request.user, submission, selected, assignment, existing, fingerprint, permission_request), salt=SALT, compress=True)
        return render(request, 'publishing/review_ai_permission.html', {'kind':'reviewer', 'submission':submission,
            'assignment':assignment, 'selected_round':selected, 'existing':existing, 'active':active,
            'may_grant':may_grant, 'show_title':may_grant, 'token':token,
            'permission_request':permission_request if may_grant else None})
    action = request.POST.get('action')
    if action not in ('grant', 'withdraw', 'decline'):
        return HttpResponse(_('Invalid authorisation action.'), status=400)
    if action == 'grant' and (request.POST.get('confirm') != 'yes' or request.POST.get('synthetic') != 'yes'):
        return HttpResponse(_('Both confirmations are required.'), status=400)
    signed = _form_token(request)
    if not signed:
        return _invalid()
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=submission.pk)
        get_object_or_404(ReviewRound.objects.select_for_update(), pk=selected.pk, submission=submission)
        assignment = _owned_reviewer(pk, actor, locked=True)
        existing = existing_reviewer(assignment, actor, locked=True)
        fingerprint = report_fingerprint(assignment)
        permission_request = current_request(assignment.round, actor, ReviewAIConsent.Subject.REVIEWER, fingerprint, assignment, locked=True)
        if signed != _snapshot(actor, submission, assignment.round, assignment, existing, fingerprint, permission_request):
            return _invalid()
        if action == 'withdraw':
            if not existing:
                return _invalid()
            _end(existing, ReviewAIConsent.EndReason.WITHDRAWN)
            _event(submission, actor, 'review_ai_reviewer_withdrawn')
        elif action == 'decline':
            if (not _reviewer_may_grant(actor, assignment) or not permission_request or
                    permission_request.status != ReviewAIPermissionRequest.Status.PENDING or reviewer_active(existing, assignment)):
                return _invalid()
            permission_request.status = ReviewAIPermissionRequest.Status.DECLINED
            permission_request.responded_at = timezone.now()
            permission_request.save(update_fields=['status', 'responded_at'])
            _event(submission, actor, 'review_ai_reviewer_declined')
        else:
            if not _reviewer_may_grant(actor, assignment):
                return _invalid()
            if existing and not reviewer_active(existing, assignment):
                _end(existing, ReviewAIConsent.EndReason.SUPERSEDED)
                existing = None
            if not existing:
                ReviewAIConsent.objects.create(submission=submission, review_round=assignment.round,
                    version=assignment.round.version, assignment=assignment, granted_by=actor,
                    subject=ReviewAIConsent.Subject.REVIEWER, provider=PROVIDER, purpose=PURPOSE, source_fingerprint=fingerprint)
                _event(submission, actor, 'review_ai_reviewer_granted')
            if permission_request and permission_request.status == ReviewAIPermissionRequest.Status.PENDING:
                permission_request.status = ReviewAIPermissionRequest.Status.GRANTED
                permission_request.responded_at = timezone.now()
                permission_request.save(update_fields=['status', 'responded_at'])
    messages.success(request, _('Your choice was recorded. No AI request was made.'))
    return redirect('reviewer_review_ai_permission', pk=pk)
