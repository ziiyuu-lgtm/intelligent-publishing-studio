import hashlib

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .contributor_evidence import (ContributorConfirmationForm, ContributorEvidenceForm, MAX_EVIDENCE_BYTES,
                                   MAX_EVIDENCE_FILES, open_intact_evidence)
from .contributors import validate_contributors
from .models import ContributorChangeRequest, ContributorConfirmationEvent, ContributorEvidence, Submission
from .views import _author_can_view, _editor_can_view, _event, _submission


def _access(user, submission):
    author = _author_can_view(user, submission)
    editor = _editor_can_view(user, submission)
    if not (author or editor):
        raise Http404
    return author, editor


def _snapshot(change, side, position):
    if side not in ContributorConfirmationEvent.Side.values:
        raise Http404
    try:
        people = validate_contributors(change.before if side == "original" else change.proposed)
    except ValueError:
        return None
    if not 0 <= position < len(people):
        raise Http404
    return people[position]


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def upload_contributor_evidence(request, pk, change_id):
    submission = _submission(pk)
    _access(request.user, submission)
    change = get_object_or_404(ContributorChangeRequest, pk=change_id, submission=submission)
    if change.status != ContributorChangeRequest.Status.PENDING:
        return HttpResponse(_("Evidence can be added only while the request is pending."), status=409)
    form = ContributorEvidenceForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        document = form.cleaned_data["file"]
        digest, size = hashlib.sha256(), 0
        for chunk in document.chunks():
            size += len(chunk)
            if size > MAX_EVIDENCE_BYTES:
                return HttpResponse(_("Choose a PDF file no larger than 5 MB."), status=400)
            digest.update(chunk)
        document.seek(0)
        if size != document.size:
            return HttpResponse(_("The evidence file could not be read safely."), status=400)
        evidence = None
        try:
            with transaction.atomic():
                current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                _access(request.user, current)
                locked = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change_id, submission=current)
                if locked.status != ContributorChangeRequest.Status.PENDING:
                    return HttpResponse(_("Evidence can be added only while the request is pending."), status=409)
                if locked.evidence_files.count() >= MAX_EVIDENCE_FILES:
                    return HttpResponse(_("This request has reached the evidence file limit."), status=409)
                evidence = ContributorEvidence(change=locked, label=form.cleaned_data["label"], file=document,
                                               size=size, sha256=digest.hexdigest(), uploaded_by=request.user)
                evidence.save()
                _event(current, request.user, "contributor_evidence_uploaded")
        except Exception:
            if evidence and evidence.file.name and evidence.file._committed:
                evidence.file.storage.delete(evidence.file.name)
            raise
        messages.success(request, _("Fictional evidence saved privately. No email was sent."))
        return redirect("contributor_change_detail", pk=pk, change_id=change_id)
    return render(request, "publishing/contributor_evidence_upload.html", {"submission":submission, "change":change,
        "form":form}, status=400 if request.method == "POST" else 200)


@login_required
@never_cache
@require_GET
def download_contributor_evidence(request, pk, change_id, evidence_id):
    submission = _submission(pk)
    author, editor = _access(request.user, submission)
    change = get_object_or_404(ContributorChangeRequest, pk=change_id, submission=submission)
    evidence = get_object_or_404(ContributorEvidence, pk=evidence_id, change=change)
    if not editor and not (author and evidence.uploaded_by_id == request.user.pk):
        raise Http404
    stream = open_intact_evidence(evidence)
    if not stream:
        raise Http404
    response = FileResponse(stream, as_attachment=True, filename="fictional-evidence.pdf", content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def record_contributor_confirmation(request, pk, change_id, side, position):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    change = get_object_or_404(ContributorChangeRequest, pk=change_id, submission=submission)
    if change.status != ContributorChangeRequest.Status.PENDING:
        return HttpResponse(_("This request has already been resolved."), status=409)
    person = _snapshot(change, side, position)
    if person is None:
        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
    history = change.confirmation_events.filter(side=side, position=position).select_related("evidence", "recorded_by").order_by("-pk")
    latest = history.first()
    form = ContributorConfirmationForm(request.POST or None, change=change,
        initial={"state":latest.state if latest else ContributorConfirmationEvent.State.UNCHECKED,
                 "evidence":latest.evidence_id if latest else None})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
            if not _editor_can_view(request.user, current):
                raise Http404
            locked = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change_id, submission=current)
            if locked.status != ContributorChangeRequest.Status.PENDING:
                return HttpResponse(_("This request has already been resolved."), status=409)
            locked_person = _snapshot(locked, side, position)
            if locked_person is None or locked_person != person:
                return HttpResponse(_("The submission or request changed; reload the page."), status=409)
            last = locked.confirmation_events.filter(side=side, position=position).order_by("-pk").first()
            if request.POST.get("last_event") != str(last.pk if last else 0):
                return HttpResponse(_("The author check changed; reload the page."), status=409)
            evidence = form.cleaned_data["evidence"]
            state = form.cleaned_data["state"]
            if evidence and evidence.change_id != locked.pk:
                raise Http404
            if state == ContributorConfirmationEvent.State.CONFIRMED:
                stream = open_intact_evidence(evidence)
                if not stream:
                    return HttpResponse(_("The selected evidence is unavailable or has changed."), status=409)
                stream.close()
            if not last or last.state != state or last.evidence_id != (evidence.pk if evidence else None):
                ContributorConfirmationEvent.objects.create(change=locked, side=side, position=position, state=state,
                    evidence=evidence, recorded_by=request.user)
                _event(current, request.user, "contributor_consent_checked")
        messages.success(request, _("Manual author check recorded."))
        return redirect("contributor_change_detail", pk=pk, change_id=change_id)
    return render(request, "publishing/contributor_confirmation.html", {"submission":submission, "change":change,
        "side":side, "position":position, "person":person, "history":history[:50], "latest":latest, "form":form},
        status=400 if request.method == "POST" else 200)
