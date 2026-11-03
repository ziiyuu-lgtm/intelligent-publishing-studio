"""Author-only editing of unsubmitted synthetic drafts."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from .draft_edit import MAX_REPLACED_DRAFT_FILES, matches_edit, sign_edit
from .forms import DraftEditForm
from .models import Journal, ReplacedDraftFile, Submission, SubmissionVersion
from .submission_checklist import _fingerprint
from .views import _author_can_view, _event, _submission


def _version(submission, *, locked=False):
    if submission.status != Submission.Status.DRAFT or submission.rounds.exists():
        return None
    versions = submission.versions.select_for_update() if locked else submission.versions.all()
    found = list(versions[:2])
    if (len(found) != 1 or found[0].number != 1 or found[0].submission_id != submission.pk or
            found[0].manuscript.name != submission.manuscript.name or
            found[0].review_copy_checked_at or found[0].review_copy_checked_by_id):
        return None
    return found[0]


def _conflict(request, submission):
    return render(request, 'publishing/draft_edit_conflict.html', {'submission':submission,
        'entered_title':request.POST.get('title', '')[:250],
        'entered_abstract':request.POST.get('abstract', '')[:10000]}, status=409)


def _context(submission, version, form, token):
    return {'submission':submission, 'version':version, 'form':form, 'edit_token':token,
        'manuscript_available':bool(_fingerprint(version.manuscript)),
        'review_copy_available':bool(_fingerprint(version.review_copy)),
        'remaining_files':max(0, MAX_REPLACED_DRAFT_FILES - submission.replaced_draft_files.count())}


@login_required
@never_cache
@require_http_methods(['GET', 'POST'])
def edit_draft(request, pk):
    if not settings.DEBUG:
        raise Http404
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    version = _version(submission)
    if not version:
        return HttpResponse(_('This draft cannot be edited in its current state.'), status=409)
    token = sign_edit(submission, version) if request.method == 'GET' else request.POST.get('edit_token', '')
    if request.method == 'POST' and not matches_edit(token, submission, version):
        return _conflict(request, submission)
    available = bool(_fingerprint(version.review_copy))
    form = DraftEditForm(request.POST if request.method == 'POST' else None,
        request.FILES if request.method == 'POST' else None,
        review_mode=submission.review_mode, existing_review_copy_available=available,
        initial={'title':submission.title, 'abstract':submission.abstract})
    if request.method != 'POST' or not form.is_valid():
        return render(request, 'publishing/draft_edit.html', _context(submission, version, form, token))
    saved_files = []
    try:
        with transaction.atomic():
            actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
            get_object_or_404(Journal.objects.select_for_update(), pk=submission.journal_id)
            submission = get_object_or_404(Submission.objects.select_for_update().select_related('journal__organisation'), pk=pk)
            if not _author_can_view(actor, submission):
                raise Http404
            version = _version(submission, locked=True)
            if not version or not matches_edit(token, submission, version):
                return _conflict(request, submission)
            manuscript = form.cleaned_data.get('manuscript')
            reviewer = form.cleaned_data.get('review_copy')
            new_confirmed = (bool(form.cleaned_data.get('review_copy_confirmed')) and
                bool(reviewer or _fingerprint(version.review_copy))) if submission.review_mode == Journal.ReviewMode.SEPARATE else False
            if submission.review_mode not in Journal.ReviewMode.values:
                return HttpResponse(_('The review file policy is unavailable.'), status=409)
            count = int(bool(manuscript and version.manuscript.name)) + int(bool(reviewer and version.review_copy.name))
            if submission.replaced_draft_files.count() + count > MAX_REPLACED_DRAFT_FILES:
                form.add_error(None, _('This draft has reached the retained-file replacement limit.'))
                return render(request, 'publishing/draft_edit.html', _context(submission, version, form, token), status=409)
            changed = bool(manuscript or reviewer or submission.title != form.cleaned_data['title'] or
                submission.abstract != form.cleaned_data['abstract'] or version.title_snapshot != form.cleaned_data['title'] or
                version.abstract_snapshot != form.cleaned_data['abstract'] or version.review_copy_confirmed != new_confirmed)
            if changed:
                if manuscript:
                    old_name = version.manuscript.name
                    version.manuscript.save('manuscript.pdf', manuscript, save=False)
                    saved_files.append((version.manuscript.storage, version.manuscript.name))
                    if old_name:
                        ReplacedDraftFile.objects.create(submission=submission, version=version,
                            replaced_by=actor, kind=ReplacedDraftFile.Kind.MANUSCRIPT, file=old_name)
                    submission.manuscript.name = version.manuscript.name
                if reviewer:
                    old_name = version.review_copy.name
                    version.review_copy.save('reviewer.pdf', reviewer, save=False)
                    saved_files.append((version.review_copy.storage, version.review_copy.name))
                    if old_name:
                        ReplacedDraftFile.objects.create(submission=submission, version=version,
                            replaced_by=actor, kind=ReplacedDraftFile.Kind.REVIEW_COPY, file=old_name)
                submission.title, submission.abstract = form.cleaned_data['title'], form.cleaned_data['abstract']
                version.title_snapshot, version.abstract_snapshot = submission.title, submission.abstract
                version.review_copy_confirmed = new_confirmed
                version.save(update_fields=['manuscript', 'review_copy', 'review_copy_confirmed', 'title_snapshot', 'abstract_snapshot'])
                submission.save(update_fields=['manuscript', 'title', 'abstract'])
                _event(submission, actor, 'draft_content_updated')
    except Exception:
        for storage, name in saved_files:
            storage.delete(name)
        raise
    messages.success(request, _('Draft updated. Review it before submitting.') if changed else _('No draft changes were made.'))
    return redirect('submission_checklist', pk=submission.pk)
