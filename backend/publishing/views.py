from django.contrib import messages
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import DecisionForm, InviteForm, ReportForm, RevisionForm, SubmissionForm
from .models import (ConflictDeclaration, EditorialAssignment, EditorialDecision, Journal, Membership, ProductionAssignment,
                     PublicationDraft, ReviewAssignment, ReviewRound, Submission, SubmissionEvent, SubmissionVersion)

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
            and submission.editor_assignments.filter(editor=user, status=EditorialAssignment.Status.ACTIVE).exists()
            and not submission.reviews.filter(reviewer=user).exists()
            and not ProductionAssignment.objects.filter(draft__submission=submission, producer=user, status=ProductionAssignment.Status.RECUSED).exists())


def _can_coordinate(user, submission):
    return (submission.status != Submission.Status.DRAFT and submission.author_id != user.pk
            and _has_role(user, submission.journal, Membership.Role.COORDINATOR)
            and not submission.reviews.filter(reviewer=user).exists()
            and not submission.editor_assignments.filter(editor=user, status=EditorialAssignment.Status.RECUSED).exists()
            and not ProductionAssignment.objects.filter(draft__submission=submission, producer=user, status=ProductionAssignment.Status.RECUSED).exists())


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
            ProductionAssignment.objects.filter(draft__submission=assignment.submission, producer=user).exists() or
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


def _file_exists(field):
    try:
        return bool(field and field.name and field.storage.exists(field.name))
    except OSError:
        return False


def _review_copy_ready(submission, version):
    if version.submission_id != submission.pk:
        return False
    if submission.review_mode == Journal.ReviewMode.DIRECT:
        return _file_exists(version.manuscript)
    if submission.review_mode == Journal.ReviewMode.SEPARATE:
        return bool(_file_exists(version.manuscript) and version.review_copy_confirmed and version.review_copy_checked_at and
                    version.review_copy_checked_by_id and _file_exists(version.review_copy))
    return False


@require_GET
def home(request):
    journals = Journal.objects.filter(active=True, organisation__active=True).select_related("organisation").order_by("name")
    return render(request, "publishing/home.html", {"journals": journals,
        "local_publication_enabled": settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED})


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
        editor_assignments__editor=request.user, editor_assignments__status=EditorialAssignment.Status.ACTIVE,
    ).exclude(author=request.user).exclude(reviews__reviewer=request.user).exclude(status=Submission.Status.DRAFT).exclude(
        Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("pk"), producer=request.user,
                                                    status=ProductionAssignment.Status.RECUSED))
    ).select_related("journal", "author").distinct().annotate(
        has_reported_conflict=Exists(ReviewAssignment.objects.filter(submission_id=OuterRef("pk"), conflict=ReviewAssignment.Conflict.YES)))
    reviews = ReviewAssignment.objects.filter(
        reviewer=request.user, submission__journal__in=visible_journals,
        submission__journal__memberships__user=request.user,
        submission__journal__memberships__role=Membership.Role.REVIEWER,
        submission__journal__memberships__active=True,
    ).exclude(submission__author=request.user).exclude(
        Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("submission_id"), producer=request.user))
    ).select_related("submission__journal", "round").distinct()
    production_enabled = settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED
    production = PublicationDraft.objects.none()
    if production_enabled:
        production = PublicationDraft.objects.filter(
            submission__status=Submission.Status.ACCEPTED, submission__journal__in=visible_journals,
            submission__journal__memberships__user=request.user, submission__journal__memberships__role=Membership.Role.PRODUCER,
            submission__journal__memberships__active=True, production_assignments__producer=request.user,
            production_assignments__status=ProductionAssignment.Status.ACTIVE,
        ).exclude(submission__author=request.user).exclude(submission__reviews__reviewer=request.user).exclude(
            Exists(EditorialAssignment.objects.filter(submission_id=OuterRef("submission_id"), editor=request.user,
                                                      status=EditorialAssignment.Status.RECUSED))
        ).exclude(Exists(ProductionAssignment.objects.filter(draft_id=OuterRef("pk"), producer=request.user,
                                                              status=ProductionAssignment.Status.RECUSED))
        ).select_related("submission__journal").distinct()
    return render(request, "publishing/workspace.html", {
        "author_journals": author_journals, "authored": authored, "editorial": editorial, "reviews": reviews,
        "production": production, "local_publication_enabled": production_enabled,
        "is_coordinator": Membership.objects.filter(user=request.user, role=Membership.Role.COORDINATOR,
             active=True, journal__active=True, journal__organisation__active=True).exists(),
    })


@login_required
@never_cache
@require_GET
def coordination_workspace(request):
    submissions = Submission.objects.filter(
        journal__active=True, journal__organisation__active=True,
        journal__memberships__user=request.user, journal__memberships__role=Membership.Role.COORDINATOR,
        journal__memberships__active=True,
    ).exclude(status=Submission.Status.DRAFT).exclude(author=request.user).exclude(
        reviews__reviewer=request.user).exclude(
        Exists(EditorialAssignment.objects.filter(submission_id=OuterRef("pk"), editor=request.user,
                                                  status=EditorialAssignment.Status.RECUSED))
    ).exclude(
        Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("pk"), producer=request.user,
                                                    status=ProductionAssignment.Status.RECUSED))
    ).select_related("journal").distinct().order_by("-submitted_at")
    journals = Journal.objects.filter(active=True, organisation__active=True, memberships__user=request.user,
        memberships__role=Membership.Role.COORDINATOR, memberships__active=True).distinct().order_by("name")
    return render(request, "publishing/coordination_workspace.html", {"submissions": submissions,
        "coordinator_journals": journals, "local_publication_enabled": settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def submission_editors(request, pk):
    submission = _submission(pk)
    if not _can_coordinate(request.user, submission):
        raise Http404
    if request.method == "POST":
        try:
            editor_id = int(request.POST.get("editor", ""))
            if not 1 <= editor_id <= 9223372036854775807:
                raise ValueError
        except (TypeError, ValueError):
            return HttpResponse(_("Choose an eligible editor."), status=400)
        with transaction.atomic():
            submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
            if not _can_coordinate(request.user, submission):
                raise Http404
            editor = get_user_model().objects.filter(pk=editor_id, is_active=True).first()
            if (not editor or editor.pk in (submission.author_id, request.user.pk) or
                    not _has_role(editor, submission.journal, Membership.Role.EDITOR) or
                    submission.reviews.filter(reviewer=editor).exists() or
                    submission.editor_assignments.filter(editor=editor, status=EditorialAssignment.Status.RECUSED).exists() or
                    ProductionAssignment.objects.filter(draft__submission=submission, producer=editor, status=ProductionAssignment.Status.RECUSED).exists()):
                return HttpResponse(_("Choose an eligible editor."), status=400)
            assignment, created = EditorialAssignment.objects.get_or_create(submission=submission, editor=editor,
                status=EditorialAssignment.Status.ACTIVE, defaults={"assigned_by": request.user})
            if created:
                _event(submission, request.user, "editor_assigned")
        messages.success(request, _("Editor assignment recorded.") if created else _("This editor is already assigned."))
        return redirect("submission_editors", pk=pk)
    excluded = [submission.author_id, request.user.pk]
    excluded += list(submission.reviews.values_list("reviewer_id", flat=True))
    excluded += list(submission.editor_assignments.filter(status__in=[EditorialAssignment.Status.ACTIVE,
                              EditorialAssignment.Status.RECUSED]).values_list("editor_id", flat=True))
    excluded += list(ProductionAssignment.objects.filter(draft__submission=submission, status=ProductionAssignment.Status.RECUSED).values_list("producer_id", flat=True))
    candidates = get_user_model().objects.filter(is_active=True, journal_memberships__journal=submission.journal,
        journal_memberships__role=Membership.Role.EDITOR, journal_memberships__active=True).exclude(pk__in=excluded).distinct().order_by("username")
    history = submission.editor_assignments.select_related("editor", "assigned_by", "ended_by")
    production_draft = getattr(submission, "publication_draft", None) if settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED else None
    return render(request, "publishing/submission_editors.html", {"submission": submission, "candidates": candidates,
        "assignments": history, "production_draft": production_draft})


@login_required
@never_cache
@require_POST
def remove_editor_assignment(request, pk):
    with transaction.atomic():
        assignment = get_object_or_404(EditorialAssignment.objects.select_for_update().select_related(
            "submission__journal__organisation"), pk=pk)
        submission = assignment.submission
        if not _can_coordinate(request.user, submission):
            raise Http404
        if assignment.status != EditorialAssignment.Status.ACTIVE:
            return HttpResponse(_("This assignment is no longer active."), status=409)
        assignment.status, assignment.ended_at, assignment.ended_by = EditorialAssignment.Status.REVOKED, timezone.now(), request.user
        assignment.save(update_fields=["status", "ended_at", "ended_by"])
        _event(submission, request.user, "editor_assignment_removed")
    return redirect("submission_editors", pk=submission.pk)


@login_required
@never_cache
@require_POST
def recuse_editor(request, pk):
    with transaction.atomic():
        assignment = get_object_or_404(EditorialAssignment.objects.select_for_update().select_related(
            "submission__journal__organisation"), pk=pk, editor=request.user)
        submission = assignment.submission
        if assignment.status != EditorialAssignment.Status.ACTIVE or not _editor_can_view(request.user, submission):
            raise Http404
        if request.POST.get("confirm") != "yes":
            return HttpResponse(_("Confirm your recusal."), status=400)
        assignment.status, assignment.ended_at, assignment.ended_by = EditorialAssignment.Status.RECUSED, timezone.now(), request.user
        assignment.save(update_fields=["status", "ended_at", "ended_by"])
        _event(submission, request.user, "editor_recused")
    messages.success(request, _("Your recusal was recorded and editorial access ended."))
    return redirect("workspace")


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def submission_create(request, slug):
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    if not _has_role(request.user, journal, Membership.Role.AUTHOR):
        raise Http404
    form = SubmissionForm(request.POST or None, request.FILES or None, review_mode=journal.review_mode)
    if request.method == "POST" and form.is_valid():
        submission = form.save(commit=False)
        submission.journal, submission.author, submission.review_mode = journal, request.user, journal.review_mode
        version = None
        try:
            with transaction.atomic():
                submission.save()
                version = SubmissionVersion(submission=submission, number=1, manuscript=submission.manuscript.name,
                    review_copy=form.cleaned_data.get("review_copy") or "",
                    review_copy_confirmed=form.cleaned_data.get("review_copy_confirmed", False), uploaded_by=request.user)
                version.save()
                _event(submission, request.user, "draft_created")
        except Exception:
            # File storage is not transactional; clean up a just-created orphan on failure.
            if submission.manuscript.name and submission.manuscript._committed:
                submission.manuscript.storage.delete(submission.manuscript.name)
            if version and version.review_copy.name and version.review_copy._committed:
                version.review_copy.storage.delete(version.review_copy.name)
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
               "open_round": review_round, "review_copy_ready": bool(review_round and _review_copy_ready(submission, review_round.version)),
               "local_publication_enabled": settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED}
    if context["local_publication_enabled"]:
        from .production_views import _producer_can_manage
        draft = getattr(submission, "publication_draft", None)
        context["production_draft"] = draft
        context["can_open_production"] = bool(draft and (author_view or _producer_can_manage(request.user, draft)))
    if editor_view:
        context["my_editorial_assignment"] = submission.editor_assignments.get(editor=request.user, status=EditorialAssignment.Status.ACTIVE)
        context["has_reported_conflict"] = submission.reviews.filter(conflict=ReviewAssignment.Conflict.YES).exists()
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
        if not _file_exists(version.manuscript):
            return HttpResponse(_("The manuscript version is unavailable."), status=409)
        if submission.review_mode not in Journal.ReviewMode.values:
            return HttpResponse(_("The review file policy is unavailable."), status=409)
        if submission.review_mode == Journal.ReviewMode.SEPARATE and not (version.review_copy_confirmed and _file_exists(version.review_copy)):
            return HttpResponse(_("A confirmed reviewer copy is required."), status=409)
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
    privileged = _author_can_view(request.user, submission) or _editor_can_view(request.user, submission)
    reviewer_access = False
    if (not privileged and _has_role(request.user, submission.journal, Membership.Role.REVIEWER)
            and not ProductionAssignment.objects.filter(draft__submission=submission, producer=request.user).exists()):
        reviewer_access = ReviewAssignment.objects.filter(
            submission=submission, reviewer=request.user, round__submission=submission,
            round__version=version, round__status=ReviewRound.Status.OPEN,
            conflict=ReviewAssignment.Conflict.NONE,
            status__in=[ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED],
        ).exclude(submission__author=request.user).exists()
    if not (privileged or reviewer_access):
        raise Http404
    return _download_version(request, submission, version, reviewer=reviewer_access)


def _download_version(request, submission, version, reviewer=False, copy=False):
    if version.submission_id != submission.pk:
        raise Http404
    if reviewer and not _review_copy_ready(submission, version):
        raise Http404
    field = version.review_copy if (copy or (reviewer and submission.review_mode == Journal.ReviewMode.SEPARATE)) else version.manuscript
    try:
        stream = field.open("rb")
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
def submission_review_copy_file(request, pk, number):
    submission = _submission(pk)
    if not (_author_can_view(request.user, submission) or _editor_can_view(request.user, submission)):
        raise Http404
    version = get_object_or_404(SubmissionVersion, submission=submission, number=number)
    if submission.review_mode != Journal.ReviewMode.SEPARATE or not version.review_copy:
        raise Http404
    return _download_version(request, submission, version, copy=True)


@login_required
@never_cache
@require_GET
def review_file(request, pk):
    assignment = _review(pk, request.user)
    if (assignment.round.status != ReviewRound.Status.OPEN or
            assignment.conflict != ReviewAssignment.Conflict.NONE or
            assignment.status not in (ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED)):
        raise Http404
    return _download_version(request, assignment.submission, assignment.round.version, reviewer=True)


@login_required
@never_cache
@require_POST
def review_copy_screen(request, pk):
    if request.POST.get("confirmed") != "yes":
        return HttpResponse(_("Confirm the manual checks before continuing."), status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _editor_can_view(request.user, submission):
            raise Http404
        review_round = _open_round(submission, locked=True)
        if not review_round or submission.status not in ACTIVE_REVIEW_STATUSES or submission.review_mode != Journal.ReviewMode.SEPARATE:
            return HttpResponse(_("There is no reviewer copy awaiting checks."), status=409)
        version = SubmissionVersion.objects.select_for_update().get(pk=review_round.version_id)
        if not version.review_copy_confirmed or not _file_exists(version.review_copy) or not _file_exists(version.manuscript):
            return HttpResponse(_("A confirmed reviewer copy is required."), status=409)
        if not version.review_copy_checked_at:
            version.review_copy_checked_by = request.user
            version.review_copy_checked_at = timezone.now()
            version.save(update_fields=["review_copy_checked_by", "review_copy_checked_at"])
            _event(submission, request.user, "review_copy_checked")
    messages.success(request, _("Manual check recorded for this version."))
    return redirect("submission_detail", pk=pk)


@login_required
@never_cache
@require_POST
def review_invite(request, pk):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    initial_round = _open_round(submission)
    if submission.status not in ACTIVE_REVIEW_STATUSES or not initial_round:
        return HttpResponse(_("There is no open review round."), status=409)
    if not _review_copy_ready(submission, initial_round.version):
        return HttpResponse(_("The reviewer file must be available and checked before inviting reviewers."), status=409)
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
        if not _review_copy_ready(submission, review_round.version):
            return HttpResponse(_("The reviewer file must be available and checked before inviting reviewers."), status=409)
        if not _has_role(reviewer, submission.journal, Membership.Role.REVIEWER):
            raise Http404
        if (submission.reviews.filter(invited_by=reviewer).exists() or
                submission.decisions.filter(decided_by=reviewer).exists() or
                submission.editor_assignments.filter(editor=reviewer).exists() or
                ProductionAssignment.objects.filter(draft__submission=submission, producer=reviewer).exists() or
                Membership.objects.filter(user=reviewer, journal=submission.journal, role=Membership.Role.EDITOR).exists()):
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
    form = ReportForm() if (assignment.status == ReviewAssignment.Status.ACCEPTED and
                            assignment.conflict == ReviewAssignment.Conflict.NONE and
                            assignment.round.status == ReviewRound.Status.OPEN) else None
    return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form,
        "review_copy_ready": _review_copy_ready(assignment.submission, assignment.round.version)})


@login_required
@never_cache
@require_POST
def review_respond(request, pk):
    action = request.POST.get("response")
    if action not in ("accept", "decline", "confirm_no_conflict", "conflict"):
        return HttpResponse(_("Choose an available response."), status=400)
    if action in ("accept", "confirm_no_conflict") and request.POST.get("declaration") != "none":
        return HttpResponse(_("Confirm that you have no conflict before continuing."), status=400)
    details = request.POST.get("conflict_details", "").strip() if action == "conflict" else ""
    if action == "conflict" and not 0 < len(details) <= 2000:
        return HttpResponse(_("Provide a conflict explanation of up to 2000 characters."), status=400)
    candidate = _review(pk, request.user)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=candidate.submission_id)
        if action in ("conflict", "confirm_no_conflict"):
            review_round = get_object_or_404(ReviewRound.objects.select_for_update(), pk=candidate.round_id, submission=submission)
        else:
            review_round = _open_round(submission, locked=True)
        assignment = ReviewAssignment.objects.select_for_update().get(pk=pk)
        if (assignment.reviewer_id != request.user.pk or assignment.submission_id != submission.pk or
                not _has_role(request.user, submission.journal, Membership.Role.REVIEWER)):
            raise Http404
        if not review_round or assignment.round_id != review_round.pk:
            return HttpResponse(_("This review round is closed."), status=409)
        if action == "conflict":
            if assignment.status not in (ReviewAssignment.Status.INVITED, ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED) or assignment.conflict == ReviewAssignment.Conflict.YES:
                return HttpResponse(_("This conflict has already been recorded."), status=409)
            if assignment.status != ReviewAssignment.Status.SUBMITTED:
                assignment.status = ReviewAssignment.Status.CONFLICT
            if not assignment.responded_at:
                assignment.responded_at = timezone.now()
            assignment.conflict, assignment.conflict_details, assignment.conflict_declared_at = ReviewAssignment.Conflict.YES, details, timezone.now()
            assignment.save(update_fields=["status", "responded_at", "conflict", "conflict_details", "conflict_declared_at"])
            ConflictDeclaration.objects.create(assignment=assignment, declared_by=request.user, choice=ConflictDeclaration.Choice.YES, details=details)
            _event(submission, request.user, "review_conflict_declared")
        elif action == "confirm_no_conflict":
            if assignment.status not in (ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED) or assignment.conflict != ReviewAssignment.Conflict.UNKNOWN:
                return HttpResponse(_("A declaration has already been recorded."), status=409)
            assignment.conflict, assignment.conflict_declared_at = ReviewAssignment.Conflict.NONE, timezone.now()
            assignment.save(update_fields=["conflict", "conflict_declared_at"])
            ConflictDeclaration.objects.create(assignment=assignment, declared_by=request.user, choice=ConflictDeclaration.Choice.NONE)
            _event(submission, request.user, "review_no_conflict_declared")
        else:
            if review_round.status != ReviewRound.Status.OPEN or submission.status not in ACTIVE_REVIEW_STATUSES:
                return HttpResponse(_("This review round is closed."), status=409)
            if assignment.status != ReviewAssignment.Status.INVITED:
                return HttpResponse(_("This invitation has already been answered."), status=409)
            assignment.status = ReviewAssignment.Status.ACCEPTED if action == "accept" else ReviewAssignment.Status.DECLINED
            assignment.responded_at = timezone.now()
            fields = ["status", "responded_at"]
            if action == "accept":
                assignment.conflict, assignment.conflict_declared_at = ReviewAssignment.Conflict.NONE, timezone.now()
                fields += ["conflict", "conflict_declared_at"]
                ConflictDeclaration.objects.create(assignment=assignment, declared_by=request.user, choice=ConflictDeclaration.Choice.NONE)
            assignment.save(update_fields=fields)
            _event(submission, request.user, "review_accepted" if action == "accept" else "review_declined")
    return redirect("review_detail", pk=pk)


@login_required
@never_cache
@require_POST
def review_report(request, pk):
    assignment = _review(pk, request.user)
    form = ReportForm(request.POST)
    if not form.is_valid():
        if (assignment.status != ReviewAssignment.Status.ACCEPTED or assignment.round.status != ReviewRound.Status.OPEN or
                assignment.conflict != ReviewAssignment.Conflict.NONE):
            return HttpResponse(_("A report can only be sent after accepting an invitation."), status=409)
        return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form,
            "review_copy_ready": _review_copy_ready(assignment.submission, assignment.round.version)}, status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=assignment.submission_id)
        review_round = _open_round(submission, locked=True)
        assignment = ReviewAssignment.objects.select_for_update().get(pk=pk)
        if (assignment.reviewer_id != request.user.pk or assignment.submission_id != submission.pk or
                not _has_role(request.user, submission.journal, Membership.Role.REVIEWER)):
            raise Http404
        if (not review_round or assignment.round_id != review_round.pk or
                submission.status not in ACTIVE_REVIEW_STATUSES or assignment.status != ReviewAssignment.Status.ACCEPTED or
                assignment.conflict != ReviewAssignment.Conflict.NONE):
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
            if outcome != EditorialDecision.Outcome.REJECT and not review_round.assignments.filter(status=ReviewAssignment.Status.SUBMITTED, conflict=ReviewAssignment.Conflict.NONE).exists():
                form.add_error("outcome", _("This decision requires a submitted review with no conflict declared."))
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
    form = RevisionForm(request.POST or None, request.FILES or None, review_mode=submission.review_mode)
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
                    review_copy=form.cleaned_data.get("review_copy") or "",
                    review_copy_confirmed=form.cleaned_data.get("review_copy_confirmed", False),
                )
                version.save()
                ReviewRound.objects.create(submission=submission, version=version, number=previous_round.number + 1)
                submission.status = Submission.Status.SUBMITTED
                submission.save(update_fields=["status"])
                _event(submission, request.user, "revision_submitted")
        except Exception:
            if version and version.manuscript.name and version.manuscript._committed:
                version.manuscript.storage.delete(version.manuscript.name)
            if version and version.review_copy.name and version.review_copy._committed:
                version.review_copy.storage.delete(version.review_copy.name)
            raise
        messages.success(request, _("Revision submitted for a new review round."))
        return redirect("submission_detail", pk=pk)
    return render(request, "publishing/revision_form.html", {"submission": submission, "form": form})
