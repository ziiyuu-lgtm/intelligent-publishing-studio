from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .forms import InviteForm, ReportForm, SubmissionForm
from .models import Journal, Membership, ReviewAssignment, Submission, SubmissionEvent


def _journal_is_active(journal):
    return journal.active and journal.organisation.active


def _has_role(user, journal, role):
    return bool(user.is_active and _journal_is_active(journal) and Membership.objects.filter(
        user=user, journal=journal, role=role, active=True,
    ).exists())


def _editor_can_view(user, submission):
    # Editorial role alone does not permit access to one's own submission or to drafts.
    return (submission.author_id != user.pk and submission.status != Submission.Status.DRAFT
            and _has_role(user, submission.journal, Membership.Role.EDITOR))


def _author_can_view(user, submission):
    return bool(user.is_active and submission.author_id == user.pk and _journal_is_active(submission.journal))


def _submission(pk):
    return get_object_or_404(Submission.objects.select_related("journal__organisation", "author"), pk=pk)


def _review(pk, user):
    assignment = get_object_or_404(
        ReviewAssignment.objects.select_related("submission__journal__organisation", "submission__author"),
        pk=pk, reviewer=user,
    )
    if (assignment.submission.author_id == user.pk or
            not _has_role(user, assignment.submission.journal, Membership.Role.REVIEWER)):
        raise Http404
    return assignment


def _event(submission, actor, action):
    # Keep manuscript text, report contents, and passwords out of the audit record.
    SubmissionEvent.objects.create(submission=submission, actor=actor, action=action)


@require_GET
def home(request):
    journals = Journal.objects.filter(active=True, organisation__active=True).select_related("organisation").order_by("name")
    return render(request, "publishing/home.html", {"journals": journals})


@login_required
@never_cache
@require_GET
def workspace(request):
    visible_journals = Journal.objects.filter(active=True, organisation__active=True)
    author_journals = visible_journals.filter(memberships__user=request.user, memberships__role=Membership.Role.AUTHOR, memberships__active=True).distinct()
    authored = Submission.objects.filter(author=request.user, journal__in=visible_journals).select_related("journal")
    editorial = Submission.objects.filter(
        journal__in=visible_journals, journal__memberships__user=request.user,
        journal__memberships__role=Membership.Role.EDITOR, journal__memberships__active=True,
    ).exclude(author=request.user).exclude(status=Submission.Status.DRAFT).select_related("journal", "author").distinct()
    reviews = ReviewAssignment.objects.filter(
        reviewer=request.user, submission__journal__in=visible_journals,
        submission__journal__memberships__user=request.user,
        submission__journal__memberships__role=Membership.Role.REVIEWER,
        submission__journal__memberships__active=True,
    ).exclude(submission__author=request.user).select_related("submission__journal").distinct()
    return render(request, "publishing/workspace.html", {
        "author_journals": author_journals, "authored": authored, "editorial": editorial, "reviews": reviews,
    })


@login_required
@never_cache
def submission_create(request, slug):
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    if not _has_role(request.user, journal, Membership.Role.AUTHOR):
        raise Http404
    form = SubmissionForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        submission = form.save(commit=False)
        submission.journal, submission.author = journal, request.user
        try:
            with transaction.atomic():
                submission.save()
                _event(submission, request.user, "draft_created")
        except Exception:
            # File storage is not transactional; clean up a just-created orphan on failure.
            if submission.manuscript.name and submission.manuscript._committed:
                submission.manuscript.storage.delete(submission.manuscript.name)
            raise
        messages.success(request, _("Draft created. Review it before submitting."))
        return redirect("submission_detail", pk=submission.pk)
    return render(request, "publishing/submission_form.html", {"journal": journal, "form": form})


@login_required
@never_cache
@require_GET
def submission_detail(request, pk):
    submission = _submission(pk)
    author_view = _author_can_view(request.user, submission)
    editor_view = _editor_can_view(request.user, submission)
    if not (author_view or editor_view):
        raise Http404
    context = {"submission": submission, "author_view": author_view, "editor_view": editor_view}
    if editor_view:
        context["invite_form"] = InviteForm(submission=submission, actor=request.user)
        context["reviews"] = submission.reviews.select_related("reviewer").all()
    return render(request, "publishing/submission_detail.html", context)


@login_required
@never_cache
@require_POST
def submission_submit(request, pk):
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(request.user, submission):
            raise Http404
        if submission.status != Submission.Status.DRAFT:
            return HttpResponse(_("This draft has already been submitted."), status=409)
        submission.status = Submission.Status.SUBMITTED
        submission.submitted_at = timezone.now()
        submission.save(update_fields=["status", "submitted_at"])
        _event(submission, request.user, "submitted")
    messages.success(request, _("Submission received for editorial screening."))
    return redirect("submission_detail", pk=submission.pk)


@login_required
@never_cache
@require_GET
def submission_file(request, pk):
    submission = _submission(pk)
    can_read = _author_can_view(request.user, submission) or _editor_can_view(request.user, submission)
    if not can_read and _has_role(request.user, submission.journal, Membership.Role.REVIEWER):
        can_read = ReviewAssignment.objects.filter(
            submission=submission, reviewer=request.user,
            status__in=[ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED],
        ).exclude(submission__author=request.user).exists()
    if not can_read:
        raise Http404
    try:
        stream = submission.manuscript.open("rb")
    except (OSError, ValueError):
        raise Http404
    _event(submission, request.user, "file_downloaded")
    response = FileResponse(stream, as_attachment=True, filename="manuscript.pdf", content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@never_cache
@require_POST
def review_invite(request, pk):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    form = InviteForm(request.POST, submission=submission, actor=request.user)
    if not form.is_valid():
        messages.error(request, _("Choose an eligible reviewer for this journal."))
        return redirect("submission_detail", pk=pk)
    reviewer = form.cleaned_data["reviewer"]
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _editor_can_view(request.user, submission) or reviewer.pk in (submission.author_id, request.user.pk):
            raise Http404
        if not _has_role(reviewer, submission.journal, Membership.Role.REVIEWER):
            raise Http404
        assignment, created = ReviewAssignment.objects.get_or_create(
            submission=submission, reviewer=reviewer, defaults={"invited_by": request.user},
        )
        if created:
            if submission.status == Submission.Status.SUBMITTED:
                submission.status = Submission.Status.UNDER_REVIEW
                submission.save(update_fields=["status"])
            _event(submission, request.user, "review_invited")
    messages.success(request, _("Reviewer invitation recorded; no email was sent.") if created else _("This reviewer was already invited."))
    return redirect("submission_detail", pk=pk)


@login_required
@never_cache
@require_GET
def review_detail(request, pk):
    assignment = _review(pk, request.user)
    form = ReportForm() if assignment.status == ReviewAssignment.Status.ACCEPTED else None
    return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form})


@login_required
@never_cache
@require_POST
def review_respond(request, pk):
    if request.POST.get("response") not in ("accept", "decline"):
        return HttpResponse(_("Choose accept or decline."), status=400)
    with transaction.atomic():
        assignment = _review(pk, request.user)
        assignment = ReviewAssignment.objects.select_for_update().get(pk=assignment.pk)
        if assignment.status != ReviewAssignment.Status.INVITED:
            return HttpResponse(_("This invitation has already been answered."), status=409)
        assignment.status = ReviewAssignment.Status.ACCEPTED if request.POST["response"] == "accept" else ReviewAssignment.Status.DECLINED
        assignment.responded_at = timezone.now()
        assignment.save(update_fields=["status", "responded_at"])
        _event(assignment.submission, request.user, "review_accepted" if assignment.status == ReviewAssignment.Status.ACCEPTED else "review_declined")
    return redirect("review_detail", pk=pk)


@login_required
@never_cache
@require_POST
def review_report(request, pk):
    assignment = _review(pk, request.user)
    form = ReportForm(request.POST)
    if not form.is_valid():
        if assignment.status != ReviewAssignment.Status.ACCEPTED:
            return HttpResponse(_("A report can only be sent after accepting an invitation."), status=409)
        return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form}, status=400)
    with transaction.atomic():
        assignment = ReviewAssignment.objects.select_for_update().get(pk=assignment.pk)
        if assignment.status != ReviewAssignment.Status.ACCEPTED:
            return HttpResponse(_("A report can only be sent after accepting an invitation."), status=409)
        assignment.recommendation = form.cleaned_data["recommendation"]
        assignment.comments = form.cleaned_data["comments"]
        assignment.status = ReviewAssignment.Status.SUBMITTED
        assignment.completed_at = timezone.now()
        assignment.save(update_fields=["recommendation", "comments", "status", "completed_at"])
        _event(assignment.submission, request.user, "review_submitted")
    messages.success(request, _("Report saved for the editor."))
    return redirect("review_detail", pk=pk)
