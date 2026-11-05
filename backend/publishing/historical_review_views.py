"""Editorial rechecks of closed-round fictional reviewer copies."""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .keywords import visible_keywords
from .models import Journal, ReviewRound, Submission, SubmissionVersion
from .review_copy import checked_review_copy_bytes, read_review_copy, review_copy_sha256, review_metadata_sha256
from .views import (REVIEW_COPY_CHECK_SALT, _editor_can_view, _event, _file_exists,
                    _review_copy_check_payload, _submission)


def _checked_material(submission, version):
    if (submission.review_mode != Journal.ReviewMode.SEPARATE or version.submission_id != submission.pk or
            not version.review_copy_confirmed or not _file_exists(version.manuscript)):
        return None
    if ReviewRound.objects.filter(version_id=version.pk, status=ReviewRound.Status.OPEN).exists():
        return None
    data = read_review_copy(version)
    metadata_digest = review_metadata_sha256(version)
    if data is None or not metadata_digest:
        return None
    return data, metadata_digest


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def historical_review_copy_check(request, pk, number):
    """Recheck one closed round without reopening it or granting file access."""
    if not settings.DEBUG:
        raise Http404
    if request.method == 'GET':
        submission = _submission(pk)
        if not _editor_can_view(request.user, submission) or submission.review_mode != Journal.ReviewMode.SEPARATE:
            raise Http404
        review_round = get_object_or_404(ReviewRound.objects.select_related('version'), submission=submission,
            number=number, status=ReviewRound.Status.CLOSED, version__submission=submission)
        version = review_round.version
        material = _checked_material(submission, version)
        token = ''
        if material is not None:
            token = signing.dumps(_review_copy_check_payload(submission, review_round, request.user,
                version, material[0]), salt=REVIEW_COPY_CHECK_SALT)
        return render(request, 'publishing/historical_review_copy_check.html', {
            'submission': submission, 'review_round': review_round, 'version': version,
            'keywords': visible_keywords(version.keywords_snapshot), 'check_token': token,
            'already_checked': checked_review_copy_bytes(submission, version) is not None,
            'version_in_open_round': ReviewRound.objects.filter(version_id=version.pk,
                status=ReviewRound.Status.OPEN).exists(),
        })

    if request.POST.get('confirmed') != 'yes':
        return HttpResponse(_('Confirm the manual checks before continuing.'), status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=pk)
        if not _editor_can_view(request.user, submission) or submission.review_mode != Journal.ReviewMode.SEPARATE:
            raise Http404
        review_round = get_object_or_404(ReviewRound.objects.select_for_update(), submission=submission,
            number=number, status=ReviewRound.Status.CLOSED)
        version = get_object_or_404(SubmissionVersion.objects.select_for_update(),
            pk=review_round.version_id, submission=submission)
        material = _checked_material(submission, version)
        if material is None:
            return HttpResponse(_('A confirmed reviewer copy is required.'), status=409)
        try:
            payload = signing.loads(request.POST.get('check_token', ''), salt=REVIEW_COPY_CHECK_SALT, max_age=3600)
        except signing.BadSignature:
            return HttpResponse(_('The submission or request changed; reload the page.'), status=409)
        data, metadata_digest = material
        if payload != _review_copy_check_payload(submission, review_round, request.user, version, data):
            return HttpResponse(_('The submission or request changed; reload the page.'), status=409)
        digest = review_copy_sha256(data)
        if (not version.review_copy_checked_at or not version.review_copy_checked_by_id or
                version.review_copy_checked_sha256 != digest or version.review_metadata_checked_sha256 != metadata_digest):
            version.review_copy_checked_by = request.user
            version.review_copy_checked_at = timezone.now()
            version.review_copy_checked_sha256 = digest
            version.review_metadata_checked_sha256 = metadata_digest
            version.save(update_fields=['review_copy_checked_by', 'review_copy_checked_at',
                                        'review_copy_checked_sha256', 'review_metadata_checked_sha256'])
            _event(submission, request.user, 'historical_review_copy_checked')
    messages.success(request, _('Manual check recorded for this closed round.'))
    return redirect('historical_review_copy_check', pk=pk, number=number)
