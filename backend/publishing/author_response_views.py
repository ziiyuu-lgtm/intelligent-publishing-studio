"""Local account responses for fictional author-change requests; no email is sent."""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_http_methods

from .author_responses import AssignAuthorResponseForm, AuthorResponseForm, eligible_recipient, response_snapshot_digest
from .contributor_evidence_views import _snapshot
from .contributors import validate_contributors
from .models import ContributorChangeRequest, ContributorResponseEvent, ContributorResponseRequest, Submission
from .views import CONTRIBUTOR_CHANGE_STATUSES, _editor_can_view, _event, _submission


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def manage_author_response(request, pk, change_id, side, position):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    change = get_object_or_404(ContributorChangeRequest, pk=change_id, submission=submission)
    closed = change.status != ContributorChangeRequest.Status.PENDING or submission.status not in CONTRIBUTOR_CHANGE_STATUSES
    if request.method == "POST" and closed:
        return HttpResponse(_("This request has already been resolved."), status=409)
    person = _snapshot(change, side, position)
    if person is None:
        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
    history = change.author_response_requests.filter(side=side, position=position).select_related("recipient", "assigned_by", "revoked_by").prefetch_related("events__actor")
    current = history.exclude(status=ContributorResponseRequest.Status.REVOKED).first()
    form = AssignAuthorResponseForm(request.POST or None, change=change, side=side)
    if request.method == "POST":
        if current:
            return HttpResponse(_("This author entry already has an active account task."), status=409)
        if form.is_valid():
            with transaction.atomic():
                locked_submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                if not _editor_can_view(request.user, locked_submission):
                    raise Http404
                locked = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change_id, submission=locked_submission)
                if (locked.status != ContributorChangeRequest.Status.PENDING or
                        locked_submission.status not in CONTRIBUTOR_CHANGE_STATUSES or
                        locked_submission.contributors_revision != locked.base_revision or
                        locked_submission.contributors_config != locked.before or
                        _snapshot(locked, side, position) != person):
                    return HttpResponse(_("The submission or request changed; reload the page."), status=409)
                try:
                    validate_contributors(locked.before)
                    validate_contributors(locked.proposed)
                except ValueError:
                    return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
                recipient = get_user_model().objects.filter(pk=form.cleaned_data["recipient"].pk, is_active=True).first()
                if (not eligible_recipient(recipient, locked_submission) or
                        locked.author_response_requests.filter(side=side, position=position).exclude(status="revoked").exists() or
                        locked.author_response_requests.filter(side=side, recipient=recipient).exclude(status="revoked").exists()):
                    return HttpResponse(_("The selected account is no longer eligible or already assigned."), status=409)
                task = ContributorResponseRequest.objects.create(change=locked, side=side, position=position,
                    recipient=recipient, assigned_by=request.user, snapshot_digest=response_snapshot_digest(locked),
                    expires_at=timezone.now() + timedelta(days=30))
                ContributorResponseEvent.objects.create(response_request=task, actor=request.user, action="issued")
                _event(locked_submission, request.user, "author_response_assigned")
            messages.success(request, _("Account task created locally. No email was sent."))
            return redirect("manage_author_response", pk=pk, change_id=change_id, side=side, position=position)
        return render(request, "publishing/manage_author_response.html", {"submission":submission, "change":change,
            "side":side, "position":position, "person":person, "current":current, "history":history, "form":form, "closed":closed}, status=400)
    return render(request, "publishing/manage_author_response.html", {"submission":submission, "change":change,
        "side":side, "position":position, "person":person, "current":current, "history":history, "form":form, "closed":closed})


@login_required
@never_cache
@require_POST
def revoke_author_response(request, pk, change_id, response_id):
    reason = request.POST.get("reason", "").strip()
    if not 0 < len(reason) <= 500:
        return HttpResponse(_("Provide a cancellation reason of up to 500 characters."), status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _editor_can_view(request.user, submission):
            raise Http404
        change = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change_id, submission=submission)
        task = get_object_or_404(ContributorResponseRequest.objects.select_for_update(), pk=response_id, change=change)
        if change.status != ContributorChangeRequest.Status.PENDING or task.status == ContributorResponseRequest.Status.REVOKED:
            return HttpResponse(_("This request has already been resolved."), status=409)
        if task.status != ContributorResponseRequest.Status.PENDING:
            return HttpResponse(_("An answered task cannot be cancelled. The account holder may update the response, or the editor may decline the change."), status=409)
        task.status, task.revoked_at, task.revoked_by = ContributorResponseRequest.Status.REVOKED, timezone.now(), request.user
        task.revocation_reason = reason
        task.revision += 1
        task.save(update_fields=["status", "revoked_at", "revoked_by", "revocation_reason", "revision"])
        ContributorResponseEvent.objects.create(response_request=task, actor=request.user, action="revoked")
        _event(submission, request.user, "author_response_cancelled")
    messages.success(request, _("Account task cancelled; its history remains."))
    return redirect("manage_author_response", pk=pk, change_id=change_id, side=task.side, position=task.position)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def respond_author_change(request, response_id):
    task = get_object_or_404(ContributorResponseRequest.objects.select_related("change__submission__journal__organisation"),
                             pk=response_id, recipient=request.user)
    change, submission = task.change, task.change.submission
    if task.status == ContributorResponseRequest.Status.REVOKED or not eligible_recipient(request.user, submission):
        raise Http404
    closed = change.status != ContributorChangeRequest.Status.PENDING or submission.status not in CONTRIBUTOR_CHANGE_STATUSES
    if request.method == "POST" and closed:
        return HttpResponse(_("This request has already been resolved."), status=409)
    if task.snapshot_digest != response_snapshot_digest(change):
        return HttpResponse(_("The submission or request changed; reload the page."), status=409)
    try:
        before, proposed = validate_contributors(change.before), validate_contributors(change.proposed)
        person = _snapshot(change, task.side, task.position)
    except ValueError:
        person = None
    if person is None:
        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
    form = AuthorResponseForm(request.POST or None, initial={"response":task.status if task.status in ("agreed", "disagreed") else None})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=submission.pk)
            locked_change = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change.pk, submission=current)
            locked_task = get_object_or_404(ContributorResponseRequest.objects.select_for_update(), pk=task.pk, change=locked_change, recipient=request.user)
            if locked_task.status == ContributorResponseRequest.Status.REVOKED or not eligible_recipient(request.user, current):
                raise Http404
            if (locked_change.status != ContributorChangeRequest.Status.PENDING or current.status not in CONTRIBUTOR_CHANGE_STATUSES or
                    current.contributors_revision != locked_change.base_revision or
                    current.contributors_config != locked_change.before or
                    locked_task.snapshot_digest != response_snapshot_digest(locked_change) or
                    _snapshot(locked_change, locked_task.side, locked_task.position) != person):
                return HttpResponse(_("The submission or request changed; reload the page."), status=409)
            if request.POST.get("revision") != str(locked_task.revision):
                return HttpResponse(_("Your response changed; reload the page."), status=409)
            outcome = form.cleaned_data["response"]
            if outcome == ContributorResponseRequest.Status.AGREED and locked_task.status != outcome and timezone.now() >= locked_task.expires_at:
                return HttpResponse(_("This account task has expired. Contact the editor."), status=409)
            if locked_task.status != outcome:
                locked_task.status, locked_task.responded_at = outcome, timezone.now()
                locked_task.revision += 1
                locked_task.save(update_fields=["status", "responded_at", "revision"])
                ContributorResponseEvent.objects.create(response_request=locked_task, actor=request.user, action=outcome)
                _event(current, request.user, "author_response_recorded")
        messages.success(request, _("Your response was recorded locally. No email was sent."))
        return redirect("respond_author_change", response_id=response_id)
    return render(request, "publishing/author_change_response.html", {"submission":submission, "change":change,
        "task":task, "person":person, "before":before, "proposed":proposed, "form":form,
        "expired":timezone.now() >= task.expires_at, "closed":closed,
        "response_history":task.events.filter(action__in=["agreed", "disagreed"]).order_by("-created_at", "-pk")[:50]},
        status=400 if request.method == "POST" else 200)
