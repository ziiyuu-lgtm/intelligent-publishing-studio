from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import DecisionForm, InviteForm, ReportForm, RevisionForm, SubmissionForm
from .models import (EditorialDecision, Journal, Membership, ReviewAssignment, ReviewRound,
                     Submission, SubmissionEvent, SubmissionVersion)

ACTIVE_REVIEW_STATUSES = (Submission.Status.SUBMITTED, Submission.Status.UNDER_REVIEW)


def _journal_is_active(journal):
    return journal.active and journal.organisation.active


def _has_role(user, journal, role):
    return bool(user.is_active and _journal_is_active(journal) and Membership.objects.filter(
        user=user, journal=journal, role=role, active=True,
    ).exists())


def _editor_can_view(user, submission):
    # Authors and any assigned reviewer cannot act as editor for this submission.
    return (submission.author_id != user.pk and submission.status != Submission.Status.DRAFT
            and _has_role(user, submission.journal, Membership.Role.EDITOR)
            and not submission.reviews.filter(reviewer=user).exists())


def _author_can_view(user, submission):
    return bool(user.is_active and submission.author_id == user.pk and _journal_is_active(submission.journal))


def _submission(pk):
    return get_object_or_404(Submission.objects.select_related("journal__organisation", "author"), pk=pk)


def _review(pk, user):
    assignment = get_object_or_404(
        ReviewAssignment.objects.select_related("submission__journal__organisation", "submission__author", "round__version"),
        pk=pk, reviewer=user,
    )
    if (assignment.round.submission_id != assignment.submission_id or
            assignment.round.version.submission_id != assignment.submission_id or
            assignment.submission.author_id == user.pk or
            not _has_role(user, assignment.submission.journal, Membership.Role.REVIEWER)):
        raise Http404
    return assignment


def _event(submission, actor, action):
    # Keep manuscript text, report contents, and passwords out of the audit record.
    SubmissionEvent.objects.create(submission=submission, actor=actor, action=action)


def _open_round(submission, locked=False):
    queryset = submission.rounds.select_related("version").filter(status=ReviewRound.Status.OPEN)
    if locked:
        queryset = queryset.select_for_update()
    review_round = queryset.first()
    latest = submission.versions.order_by("-number").first()
    if not review_round or not latest or review_round.version_id != latest.pk or latest.submission_id != submission.pk:
        return None
    return review_round


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
    ).exclude(author=request.user).exclude(reviews__reviewer=request.user).exclude(status=Submission.Status.DRAFT).select_related("journal", "author").distinct()
    reviews = ReviewAssignment.objects.filter(
        reviewer=request.user, submission__journal__in=visible_journals,
        submission__journal__memberships__user=request.user,
        submission__journal__memberships__role=Membership.Role.REVIEWER,
        submission__journal__memberships__active=True,
    ).exclude(submission__author=request.user).select_related("submission__journal", "round").distinct()
    return render(request, "publishing/workspace.html", {
        "author_journals": author_journals, "authored": authored, "editorial": editorial, "reviews": reviews,
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
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
                SubmissionVersion.objects.create(submission=submission, number=1,
                    manuscript=submission.manuscript.name, uploaded_by=request.user)
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
    review_round = _open_round(submission)
    context = {"submission": submission, "author_view": author_view, "editor_view": editor_view,
               "versions": submission.versions.all(), "decisions": submission.decisions.select_related("round"),
               "open_round": review_round}
    if editor_view:
        if submission.status in ACTIVE_REVIEW_STATUSES and review_round:
            context["invite_form"] = InviteForm(submission=submission, actor=request.user)
        context["reviews"] = submission.reviews.select_related("reviewer", "round").all()
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
        version = submission.versions.filter(number=1).first()
        if not version or submission.rounds.exists():
            return HttpResponse(_("The manuscript version is unavailable."), status=409)
        ReviewRound.objects.create(submission=submission, version=version, number=1)
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
    version = submission.versions.order_by("-number").first()
    if not version:
        raise Http404
    can_read = _author_can_view(request.user, submission) or _editor_can_view(request.user, submission)
    if not can_read and _has_role(request.user, submission.journal, Membership.Role.REVIEWER):
        can_read = ReviewAssignment.objects.filter(
            submission=submission, reviewer=request.user, round__submission=submission,
            round__version=version, round__status=ReviewRound.Status.OPEN,
            status__in=[ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED],
        ).exclude(submission__author=request.user).exists()
    if not can_read:
        raise Http404
    return _download_version(request, submission, version)


def _download_version(request, submission, version):
    if version.submission_id != submission.pk:
        raise Http404
    try:
        stream = version.manuscript.open("rb")
    except (OSError, ValueError):
        raise Http404
    _event(submission, request.user, "file_downloaded")
    response = FileResponse(stream, as_attachment=True, filename=f"manuscript-v{version.number}.pdf", content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@never_cache
@require_GET
def submission_version_file(request, pk, number):
    submission = _submission(pk)
    if not (_author_can_view(request.user, submission) or _editor_can_view(request.user, submission)):
        raise Http404
    version = get_object_or_404(SubmissionVersion, submission=submission, number=number)
    return _download_version(request, submission, version)


@login_required
@never_cache
@require_GET
def review_file(request, pk):
    assignment = _review(pk, request.user)
    if (assignment.round.status != ReviewRound.Status.OPEN or
            assignment.status not in (ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED)):
        raise Http404
    return _download_version(request, assignment.submission, assignment.round.version)


@login_required
@never_cache
@require_POST
def review_invite(request, pk):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    if submission.status not in ACTIVE_REVIEW_STATUSES or not _open_round(submission):
        return HttpResponse(_("There is no open review round."), status=409)
    form = InviteForm(request.POST, submission=submission, actor=request.user)
    if not form.is_valid():
        messages.error(request, _("Choose an eligible reviewer for this journal."))
        return redirect("submission_detail", pk=pk)
    reviewer = form.cleaned_data["reviewer"]
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _editor_can_view(request.user, submission) or reviewer.pk in (submission.author_id, request.user.pk):
            raise Http404
        review_round = _open_round(submission, locked=True)
        if submission.status not in ACTIVE_REVIEW_STATUSES or not review_round:
            return HttpResponse(_("There is no open review round."), status=409)
        if not _has_role(reviewer, submission.journal, Membership.Role.REVIEWER):
            raise Http404
        if (submission.reviews.filter(invited_by=reviewer).exists() or
                submission.decisions.filter(decided_by=reviewer).exists()):
            raise Http404
        assignment, created = ReviewAssignment.objects.get_or_create(
            submission=submission, round=review_round, reviewer=reviewer, defaults={"invited_by": request.user},
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
    form = ReportForm() if assignment.status == ReviewAssignment.Status.ACCEPTED and assignment.round.status == ReviewRound.Status.OPEN else None
    return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form})


@login_required
@never_cache
@require_POST
def review_respond(request, pk):
    if request.POST.get("response") not in ("accept", "decline"):
        return HttpResponse(_("Choose accept or decline."), status=400)
    candidate = _review(pk, request.user)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=candidate.submission_id)
        review_round = _open_round(submission, locked=True)
        assignment = ReviewAssignment.objects.select_for_update().get(pk=pk)
        if (assignment.reviewer_id != request.user.pk or assignment.submission_id != submission.pk or
                not _has_role(request.user, submission.journal, Membership.Role.REVIEWER)):
            raise Http404
        if (not review_round or assignment.round_id != review_round.pk or
                submission.status not in ACTIVE_REVIEW_STATUSES):
            return HttpResponse(_("This review round is closed."), status=409)
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
        if assignment.status != ReviewAssignment.Status.ACCEPTED or assignment.round.status != ReviewRound.Status.OPEN:
            return HttpResponse(_("A report can only be sent after accepting an invitation."), status=409)
        return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form}, status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=assignment.submission_id)
        review_round = _open_round(submission, locked=True)
        assignment = ReviewAssignment.objects.select_for_update().get(pk=pk)
        if (assignment.reviewer_id != request.user.pk or assignment.submission_id != submission.pk or
                not _has_role(request.user, submission.journal, Membership.Role.REVIEWER)):
            raise Http404
        if (not review_round or assignment.round_id != review_round.pk or
                submission.status not in ACTIVE_REVIEW_STATUSES or assignment.status != ReviewAssignment.Status.ACCEPTED):
            return HttpResponse(_("A report can only be sent after accepting an invitation."), status=409)
        assignment.recommendation = form.cleaned_data["recommendation"]
        assignment.comments = form.cleaned_data["comments"]
        assignment.status = ReviewAssignment.Status.SUBMITTED
        assignment.completed_at = timezone.now()
        assignment.save(update_fields=["recommendation", "comments", "status", "completed_at"])
        _event(assignment.submission, request.user, "review_submitted")
    messages.success(request, _("Report saved for the editor."))
    return redirect("review_detail", pk=pk)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def editorial_decision(request, pk):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    if submission.status not in ACTIVE_REVIEW_STATUSES or not _open_round(submission):
        return HttpResponse(_("There is no open review round."), status=409)
    form = DecisionForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
            if not _editor_can_view(request.user, submission):
                raise Http404
            review_round = _open_round(submission, locked=True)
            if not review_round or submission.status not in ACTIVE_REVIEW_STATUSES:
                return HttpResponse(_("There is no open review round."), status=409)
            outcome = form.cleaned_data["outcome"]
            if outcome != EditorialDecision.Outcome.REJECT and not review_round.assignments.filter(status=ReviewAssignment.Status.SUBMITTED).exists():
                form.add_error("outcome", _("This decision requires at least one submitted review."))
            else:
                decision = form.save(commit=False)
                decision.submission, decision.round, decision.decided_by = submission, review_round, request.user
                decision.save()
                review_round.status, review_round.closed_at = ReviewRound.Status.CLOSED, timezone.now()
                review_round.save(update_fields=["status", "closed_at"])
                submission.status = {
                    EditorialDecision.Outcome.MINOR: Submission.Status.REVISION_REQUESTED,
                    EditorialDecision.Outcome.MAJOR: Submission.Status.REVISION_REQUESTED,
                    EditorialDecision.Outcome.ACCEPT: Submission.Status.ACCEPTED,
                    EditorialDecision.Outcome.REJECT: Submission.Status.REJECTED,
                }[outcome]
                submission.save(update_fields=["status"])
                _event(submission, request.user, "editorial_decision")
                messages.success(request, _("Decision recorded. No email was sent."))
                return redirect("submission_detail", pk=pk)
    return render(request, "publishing/decision_form.html", {"submission": submission, "form": form})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def submission_revision(request, pk):
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    if submission.status != Submission.Status.REVISION_REQUESTED:
        return HttpResponse(_("A revision has not been requested."), status=409)
    form = RevisionForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        version = None
        try:
            with transaction.atomic():
                submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                if not _author_can_view(request.user, submission):
                    raise Http404
                if submission.status != Submission.Status.REVISION_REQUESTED or submission.rounds.filter(status=ReviewRound.Status.OPEN).exists():
                    return HttpResponse(_("A revision has already been submitted."), status=409)
                latest = submission.versions.order_by("-number").first()
                previous_round = submission.rounds.order_by("-number").first()
                if not latest or not previous_round or previous_round.status != ReviewRound.Status.CLOSED:
                    return HttpResponse(_("The previous version is unavailable."), status=409)
                version = SubmissionVersion(
                    submission=submission, number=latest.number + 1, uploaded_by=request.user,
                    manuscript=form.cleaned_data["manuscript"], author_response=form.cleaned_data["author_response"],
                )
                version.save()
                ReviewRound.objects.create(submission=submission, version=version, number=previous_round.number + 1)
                submission.status = Submission.Status.SUBMITTED
                submission.save(update_fields=["status"])
                _event(submission, request.user, "revision_submitted")
        except Exception:
            if version and version.manuscript.name and version.manuscript._committed:
                version.manuscript.storage.delete(version.manuscript.name)
            raise
        messages.success(request, _("Revision submitted for a new review round."))
        return redirect("submission_detail", pk=pk)
    return render(request, "publishing/revision_form.html", {"submission": submission, "form": form})
