"""Local-test supplementary PDFs with version and role scoped access."""
import io

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .forms import SupportingUploadForm
from .models import Journal, ReviewAssignment, Submission, SubmissionVersion, SupportingFile
from .supporting_files import checked_and_current, review_fingerprint, save_files, verified_bytes, within_limits
from .views import (_author_can_view, _downloadable_review_round, _editor_can_view, _event, _open_round, _review,
                    _review_copy_ready, _submission)


def _enabled():
    if not settings.DEBUG:
        raise Http404


def _draft(submission, actor):
    if not _author_can_view(actor, submission):
        raise Http404
    if submission.status != Submission.Status.DRAFT or submission.rounds.exists():
        return None
    return submission.versions.filter(number=1, submission=submission).first()


def _context(submission, version, form):
    files = SupportingFile.objects.filter(submission=submission, version=version).select_related('uploaded_by')
    return {'submission':submission, 'version':version, 'form':form,
            'files':files.filter(removed_at__isnull=True), 'removed_files':files.filter(removed_at__isnull=False)}


def _limit_error():
    return HttpResponse(_('The supporting-file limit or draft changed; reload the page.'), status=409)


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def manage_supporting_files(request, pk):
    _enabled()
    submission = _submission(pk)
    version = _draft(submission, request.user)
    if not version:
        return _limit_error()
    form = SupportingUploadForm(request.POST if request.method == 'POST' else None,
                                request.FILES if request.method == 'POST' else None, require_files=True)
    if request.method == 'POST' and form.is_valid():
        saved = []
        try:
            with transaction.atomic():
                actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
                get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
                submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=pk)
                version = _draft(submission, actor)
                if not version:
                    return _limit_error()
                version = get_object_or_404(SubmissionVersion.objects.select_for_update(), pk=version.pk, submission=submission)
                files = (form.cleaned_data.get('editor_supporting_files') or []) + (form.cleaned_data.get('reviewer_supporting_files') or [])
                if not within_limits(version, files):
                    return _limit_error()
                save_files(submission, version, actor, form.cleaned_data, saved)
                _event(submission, actor, 'supporting_files_uploaded')
        except Exception:
            for storage, name in saved:
                storage.delete(name)
            raise
        messages.success(request, _('Supporting files saved in this fictional draft.'))
        return redirect('manage_supporting_files', pk=pk)
    return render(request, 'publishing/supporting_files.html', _context(submission, version, form))


@login_required
@never_cache
@require_POST
def toggle_draft_file(request, pk):
    _enabled()
    if request.POST.get('confirm') != 'yes' or request.POST.get('action') not in ('remove', 'restore'):
        return HttpResponse(_('Confirm the supporting-file action.'), status=400)
    candidate = get_object_or_404(SupportingFile.objects.select_related('submission'), pk=pk)
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=candidate.submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=candidate.submission_id)
        version = _draft(submission, actor)
        if not version:
            return _limit_error()
        version = get_object_or_404(SubmissionVersion.objects.select_for_update(), pk=version.pk, submission=submission)
        record = get_object_or_404(SupportingFile.objects.select_for_update(), pk=pk, submission=submission, version=version)
        if request.POST['action'] == 'remove':
            if record.removed_at or record.checked_at:
                return _limit_error()
            record.removed_at, record.removed_by = timezone.now(), actor
            event = 'supporting_file_removed'
        else:
            if not record.removed_at or not within_limits(version, [], restoring=record) or verified_bytes(record) is None:
                return _limit_error()
            record.removed_at, record.removed_by = None, None
            event = 'supporting_file_restored'
        record.save(update_fields=['removed_at', 'removed_by'])
        _event(submission, actor, event)
    messages.success(request, _('Draft file selection updated.'))
    return redirect('manage_supporting_files', pk=submission.pk)


@login_required
@never_cache
@require_POST
def check_reviewer_file(request, pk):
    _enabled()
    if request.POST.get('confirm') != 'yes':
        return HttpResponse(_('Confirm the manual file and filename check.'), status=400)
    candidate = get_object_or_404(SupportingFile.objects.select_related('submission'), pk=pk)
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=candidate.submission.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=candidate.submission_id)
        if not _editor_can_view(actor, submission):
            raise Http404
        round_ = _open_round(submission, locked=True)
        if not round_:
            return _limit_error()
        record = get_object_or_404(SupportingFile.objects.select_for_update(), pk=pk, submission=submission,
            version=round_.version, visibility=SupportingFile.Visibility.REVIEWER, removed_at__isnull=True)
        if record.checked_at:
            return _limit_error()
        if verified_bytes(record) is None:
            return HttpResponse(_('The supporting file is missing or changed.'), status=409)
        record.checked_by, record.checked_at = actor, timezone.now()
        record.checked_fingerprint = review_fingerprint(record)
        record.save(update_fields=['checked_by', 'checked_at', 'checked_fingerprint'])
        _event(submission, actor, 'supporting_file_checked')
    messages.success(request, _('Manual check recorded. This does not guarantee anonymity.'))
    return redirect('submission_detail', pk=submission.pk)


@login_required
@never_cache
@require_GET
def download_supporting_file(request, pk):
    _enabled()
    record = get_object_or_404(SupportingFile.objects.select_related('submission__journal__organisation', 'version'),
        pk=pk, removed_at__isnull=True)
    submission = record.submission
    if record.version.submission_id != submission.pk:
        raise Http404
    privileged = _author_can_view(request.user, submission) or _editor_can_view(request.user, submission)
    if not privileged:
        round_ = _downloadable_review_round(submission)
        assignment = (ReviewAssignment.objects.filter(submission=submission, round=round_,
            reviewer=request.user, status__in=['accepted', 'submitted'], conflict='none').first() if round_ else None)
        if (record.visibility != SupportingFile.Visibility.REVIEWER or not round_ or
                round_.version_id != record.version_id or not assignment or not checked_and_current(record) or
                not _review_copy_ready(submission, record.version)):
            raise Http404
        _review(assignment.pk, request.user)
    data = verified_bytes(record)
    if data is None:
        raise Http404
    _event(submission, request.user, 'supporting_file_downloaded')
    response = FileResponse(io.BytesIO(data), as_attachment=True, filename=record.original_name,
                            content_type='application/octet-stream')
    response['X-Content-Type-Options'] = 'nosniff'
    response['Cache-Control'] = 'private, no-store'
    return response
