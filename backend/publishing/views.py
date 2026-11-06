import io

from django.contrib import messages
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Prefetch, Q
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import DecisionForm, InviteForm, ReportForm, RevisionForm, SubmissionForm
from .author_responses import author_responses_ready, is_response_participant
from .author_notices import visible_author_notices
from .contributor_evidence import all_confirmations_intact, confirmation_rows
from .review_notices import visible_review_notices
from .review_copy import checked_review_copy_bytes, read_review_copy, review_copy_sha256, review_metadata_sha256
from .contributors import (ContributorChangeDecisionForm, ContributorChangeForm, ContributorFormSet,
                           MAX_CONTRIBUTORS, ready_to_submit, validate_contributors)
from .models import (AuthorNotice, ConflictDeclaration, ContributorChangeRequest, ContributorResponseRequest, EditorialAssignment, EditorialDecision, Journal, JournalSiteDraft, Membership, Organisation, OrganisationMembership, OrganisationPortalDraft, ProductionAssignment,
                     PublicationDraft, ReviewAssignment, ReviewNotice, ReviewRound, Submission, SubmissionEvent, SubmissionVersion, SupportingFile)

ACTIVE_REVIEW_STATUSES = (Submission.Status.SUBMITTED, Submission.Status.UNDER_REVIEW)
CONTRIBUTOR_CHANGE_STATUSES = (*ACTIVE_REVIEW_STATUSES, Submission.Status.REVISION_REQUESTED)


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
            and not is_response_participant(user, submission)
            and not ProductionAssignment.objects.filter(draft__submission=submission, producer=user, status=ProductionAssignment.Status.RECUSED).exists())


def _can_coordinate(user, submission):
    return (submission.status != Submission.Status.DRAFT and submission.author_id != user.pk
            and _has_role(user, submission.journal, Membership.Role.COORDINATOR)
            and not submission.reviews.filter(reviewer=user).exists()
            and not is_response_participant(user, submission)
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
            is_response_participant(user, assignment.submission) or
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


def _downloadable_review_round(submission):
    """A reviewer may access files only for the active, current review round."""
    if submission.status not in ACTIVE_REVIEW_STATUSES:
        return None
    return _open_round(submission)


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
        return bool(_file_exists(version.manuscript) and checked_review_copy_bytes(submission, version) is not None)
    return False


def _review_copy_check_payload(submission, review_round, editor, version, data):
    return {"submission": str(submission.pk), "round": str(review_round.pk),
            "version": str(review_round.version_id), "editor": str(editor.pk),
            "sha256": review_copy_sha256(data), "metadata_sha256": review_metadata_sha256(version)}


REVIEW_COPY_CHECK_SALT = "publishing.review-copy-check.v1"


@require_GET
def home(request):
    journals = Journal.objects.filter(active=True, organisation__active=True).select_related("organisation").annotate(
        site_is_published=Exists(JournalSiteDraft.objects.filter(journal_id=OuterRef("pk"), current_release__isnull=False,
                                                                 current_release__site_id=F("pk")))).order_by("name")
    local_publication_enabled = settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED
    portals = (OrganisationPortalDraft.objects.filter(organisation__active=True, current_release__isnull=False,
        current_release__site_id=F("pk")).select_related("organisation", "current_release").order_by("current_release__brand_name")
        if local_publication_enabled else OrganisationPortalDraft.objects.none())
    return render(request, "publishing/home.html", {"journals": journals,
        "local_publication_enabled": local_publication_enabled, "publisher_portals":portals})



def _editorial_submissions(user):
    """One shared scope for editorial workspace and private change tracking."""
    visible_journals = Journal.objects.filter(active=True, organisation__active=True)
    return Submission.objects.filter(
        journal__in=visible_journals, journal__memberships__user=user,
        journal__memberships__role=Membership.Role.EDITOR, journal__memberships__active=True,
        editor_assignments__editor=user, editor_assignments__status=EditorialAssignment.Status.ACTIVE,
    ).exclude(author=user).exclude(reviews__reviewer=user).exclude(status=Submission.Status.DRAFT).exclude(
        Exists(ContributorResponseRequest.objects.filter(change__submission_id=OuterRef("pk"), recipient=user))
    ).exclude(
        Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("pk"), producer=user,
                                                    status=ProductionAssignment.Status.RECUSED))
    ).select_related("journal", "author").distinct()

@login_required
@never_cache
@require_GET
def workspace(request):
    from .editor_ai_reply_notices import visible_editor_ai_reply_notices
    from .review_ai_reply_notices import visible_review_ai_replies
    visible_journals = Journal.objects.filter(active=True, organisation__active=True)
    author_journals = visible_journals.filter(memberships__user=request.user, memberships__role=Membership.Role.AUTHOR, memberships__active=True).distinct()
    site_manager_journals = visible_journals.filter(memberships__user=request.user,
        memberships__role=Membership.Role.SITE_MANAGER, memberships__active=True).distinct().order_by("name")
    ai_manager_journals = visible_journals.filter(memberships__user=request.user,
        memberships__role=Membership.Role.AI_MANAGER, memberships__active=True).distinct().order_by("name") if settings.DEBUG else visible_journals.none()
    portal_organisations = Organisation.objects.filter(active=True, memberships__user=request.user,
        memberships__role=OrganisationMembership.Role.PORTAL_MANAGER, memberships__active=True).distinct().order_by("name")
    portal_ai_organisations = Organisation.objects.filter(active=True, memberships__user=request.user,
        memberships__role=OrganisationMembership.Role.AI_MANAGER, memberships__active=True).distinct().order_by("name")
    authored = Submission.objects.filter(author=request.user, journal__in=visible_journals).select_related("journal")
    editorial = _editorial_submissions(request.user).annotate(
        has_reported_conflict=Exists(ReviewAssignment.objects.filter(submission_id=OuterRef("pk"), conflict=ReviewAssignment.Conflict.YES)))
    reviews = ReviewAssignment.objects.filter(
        reviewer=request.user, submission__journal__in=visible_journals,
        submission__journal__memberships__user=request.user,
        submission__journal__memberships__role=Membership.Role.REVIEWER,
        submission__journal__memberships__active=True,
        round__submission_id=F("submission_id"), round__version__submission_id=F("submission_id"),
    ).exclude(submission__author=request.user).exclude(
        Exists(ContributorResponseRequest.objects.filter(change__submission_id=OuterRef("submission_id"), recipient=request.user))
    ).exclude(
        Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("submission_id"), producer=request.user))
    ).select_related("submission__journal", "round__version__submission").distinct()
    production_enabled = settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED
    production = PublicationDraft.objects.none()
    if production_enabled:
        production = PublicationDraft.objects.filter(
            submission__status=Submission.Status.ACCEPTED, submission__journal__in=visible_journals,
            submission__journal__memberships__user=request.user, submission__journal__memberships__role=Membership.Role.PRODUCER,
            submission__journal__memberships__active=True, production_assignments__producer=request.user,
            production_assignments__status=ProductionAssignment.Status.ACTIVE,
        ).exclude(submission__author=request.user).exclude(submission__reviews__reviewer=request.user).exclude(
            Exists(ContributorResponseRequest.objects.filter(change__submission_id=OuterRef("submission_id"), recipient=request.user))
        ).exclude(
            Exists(EditorialAssignment.objects.filter(submission_id=OuterRef("submission_id"), editor=request.user,
                                                      status=EditorialAssignment.Status.RECUSED))
        ).exclude(Exists(ProductionAssignment.objects.filter(draft_id=OuterRef("pk"), producer=request.user,
                                                              status=ProductionAssignment.Status.RECUSED))
        ).select_related("submission__journal").distinct()
    author_response_tasks = ContributorResponseRequest.objects.filter(recipient=request.user, recipient__is_active=True,
        change__submission__journal__in=author_journals
    ).exclude(status=ContributorResponseRequest.Status.REVOKED).exclude(
        Exists(ReviewAssignment.objects.filter(submission_id=OuterRef("change__submission_id"), reviewer=request.user))
    ).exclude(Exists(EditorialAssignment.objects.filter(submission_id=OuterRef("change__submission_id"), editor=request.user))
    ).exclude(Exists(EditorialAssignment.objects.filter(submission_id=OuterRef("change__submission_id"), assigned_by=request.user))
    ).exclude(Exists(EditorialAssignment.objects.filter(submission_id=OuterRef("change__submission_id"), ended_by=request.user))
    ).exclude(Exists(EditorialDecision.objects.filter(submission_id=OuterRef("change__submission_id"), decided_by=request.user))
    ).exclude(Exists(ReviewAssignment.objects.filter(submission_id=OuterRef("change__submission_id"), invited_by=request.user))
    ).exclude(Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("change__submission_id"), producer=request.user))
    ).exclude(Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("change__submission_id"), assigned_by=request.user))
    ).exclude(Exists(ProductionAssignment.objects.filter(draft__submission_id=OuterRef("change__submission_id"), ended_by=request.user))
    ).exclude(~Q(change__submission__author=request.user), Exists(Membership.objects.filter(user=request.user,
        journal_id=OuterRef("change__submission__journal_id"), role=Membership.Role.COORDINATOR, active=True))
    ).select_related("change__submission__journal")[:50]
    return render(request, "publishing/workspace.html", {
        "author_journals": author_journals, "authored": authored, "editorial": editorial, "reviews": reviews,
        "production": production, "local_publication_enabled": production_enabled,
        "author_response_tasks": author_response_tasks,
        "unread_review_notice_count": visible_review_notices(request.user).filter(read_at__isnull=True).count(),
        "unread_author_notice_count": visible_author_notices(request.user).filter(read_at__isnull=True).count(),
        "unread_editor_ai_reply_count": visible_editor_ai_reply_notices(request.user).filter(read_at__isnull=True).count() if settings.DEBUG else 0,
        "unread_review_ai_reply_count": visible_review_ai_replies(request.user).filter(read_at__isnull=True).count() if settings.DEBUG else 0,
        "editor_ai_reply_enabled": settings.DEBUG,
        "review_ai_requests_enabled": settings.DEBUG,
        "site_manager_journals": site_manager_journals,
        "ai_manager_journals": ai_manager_journals,
        "portal_organisations": portal_organisations,
        "portal_ai_organisations": portal_ai_organisations,
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
        Exists(ContributorResponseRequest.objects.filter(change__submission_id=OuterRef("pk"), recipient=request.user))
    ).exclude(
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
                    is_response_participant(editor, submission) or
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
    excluded += list(ContributorResponseRequest.objects.filter(change__submission=submission).values_list("recipient_id", flat=True))
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
        submission.keywords_config = form.cleaned_data["keywords"]
        version = None
        try:
            with transaction.atomic():
                submission.save()
                version = SubmissionVersion(submission=submission, number=1, manuscript=submission.manuscript.name,
                    title_snapshot=submission.title, abstract_snapshot=submission.abstract,
                    keywords_snapshot=submission.keywords_config,
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
    from .keywords import visible_keywords
    review_round = _open_round(submission)
    context = {"submission": submission, "author_view": author_view, "editor_view": editor_view,
               "author_keywords":visible_keywords(submission.keywords_config),
               "versions": submission.versions.all(), "decisions": submission.decisions.select_related("round"),
               "open_round": review_round, "review_copy_ready": bool(review_round and _review_copy_ready(submission, review_round.version)),
               "review_ai_permissions_available": settings.DEBUG and submission.status != Submission.Status.DRAFT,
               "local_publication_enabled": settings.DEBUG and settings.IPS_LOCAL_PUBLICATION_ENABLED}
    if settings.DEBUG:
        from .supporting_files import checked_and_current, reviewer_files_ready, verified_bytes
        active = SupportingFile.objects.filter(submission=submission, removed_at__isnull=True)
        versions = list(submission.versions.prefetch_related(Prefetch('supporting_files', queryset=active, to_attr='active_supporting')))
        for version in versions:
            for item in version.active_supporting:
                item.integrity_ok = verified_bytes(item) is not None
                item.review_check_ok = checked_and_current(item)
        context['versions'] = versions
        context['supporting_enabled'] = True
        context['supporting_reviewer_ready'] = bool(review_round and reviewer_files_ready(review_round.version))
    else:
        from .supporting_files import reviewer_files_ready
        context['supporting_reviewer_ready'] = bool(review_round and reviewer_files_ready(review_round.version))
    try:
        context["contributors"] = validate_contributors(submission.contributors_config)
        context["contributors_ready"] = ready_to_submit(context["contributors"])
    except ValueError:
        context["contributors_invalid"] = True
    if submission.status != Submission.Status.DRAFT:
        context["contributor_changes"] = submission.contributor_change_requests.select_related("requested_by", "closed_by")[:20]
        context["has_pending_contributor_change"] = submission.contributor_change_requests.filter(
            status=ContributorChangeRequest.Status.PENDING).exists()
        context["can_request_contributor_change"] = bool(author_view and
            submission.status in CONTRIBUTOR_CHANGE_STATUSES and not context["has_pending_contributor_change"])
    if context["local_publication_enabled"]:
        from .production_views import _producer_can_manage
        draft = getattr(submission, "publication_draft", None)
        context["production_draft"] = draft
        context["can_open_production"] = bool(draft and (author_view or _producer_can_manage(request.user, draft)))
    if editor_view:
        context["my_editorial_assignment"] = submission.editor_assignments.get(editor=request.user, status=EditorialAssignment.Status.ACTIVE)
        context["has_reported_conflict"] = submission.reviews.filter(conflict=ReviewAssignment.Conflict.YES).exists()
        if settings.DEBUG and submission.review_mode == Journal.ReviewMode.SEPARATE:
            context["historical_review_rounds"] = submission.rounds.filter(
                status=ReviewRound.Status.CLOSED, version__submission=submission).select_related("version")
        if submission.status in ACTIVE_REVIEW_STATUSES and review_round:
            context["invite_form"] = InviteForm(submission=submission, actor=request.user)
            if submission.review_mode == Journal.ReviewMode.SEPARATE:
                context["review_preview_keywords"] = visible_keywords(review_round.version.keywords_snapshot)
            if (submission.review_mode == Journal.ReviewMode.SEPARATE and
                    not context["review_copy_ready"] and review_round.version.review_copy_confirmed and
                    _file_exists(review_round.version.manuscript)):
                data = read_review_copy(review_round.version)
                if data is not None and review_metadata_sha256(review_round.version):
                    payload = _review_copy_check_payload(submission, review_round, request.user, review_round.version, data)
                    context["review_copy_check_token"] = signing.dumps(payload, salt=REVIEW_COPY_CHECK_SALT)
        context["reviews"] = submission.reviews.select_related("reviewer", "round").all()
    return render(request, "publishing/submission_detail.html", context)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def submission_contributors(request, pk):
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    if submission.status != Submission.Status.DRAFT:
        return HttpResponse(_("Direct editing is locked after submission. Request an editorially reviewed change instead."), status=409)
    try:
        initial = validate_contributors(submission.contributors_config)
    except ValueError:
        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
    if request.method == "POST":
        try:
            total = int(request.POST.get("authors-TOTAL_FORMS", "-1"))
        except ValueError:
            total = -1
        if (request.POST.get("authors-INITIAL_FORMS") != str(len(initial)) or
                request.POST.get("authors-TOTAL_FORMS") != str(total) or
                not len(initial) <= total <= MAX_CONTRIBUTORS * 2):
            return HttpResponse(_("The author editor is out of date; reload the page."), status=409)
        formset = ContributorFormSet(request.POST, prefix="authors", initial=initial)
        if formset.is_valid():
            with transaction.atomic():
                current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                if not _author_can_view(request.user, current):
                    raise Http404
                if (current.status != Submission.Status.DRAFT or
                        request.POST.get("revision") != str(current.contributors_revision) or
                        current.contributors_config != initial):
                    return HttpResponse(_("The draft or author list changed; reload the page."), status=409)
                if current.contributors_config != formset.cleaned_contributors:
                    current.contributors_config = formset.cleaned_contributors
                    current.contributors_revision += 1
                    current.save(update_fields=["contributors_config", "contributors_revision"])
                    _event(current, request.user, "draft_contributors_updated")
            messages.success(request, _("Author details saved to the draft."))
            return redirect("submission_detail", pk=pk)
        return render(request, "publishing/submission_contributors.html", {"submission":submission, "formset":formset}, status=400)
    formset = ContributorFormSet(prefix="authors", initial=initial)
    return render(request, "publishing/submission_contributors.html", {"submission":submission, "formset":formset})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def request_contributor_change(request, pk):
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    if submission.status not in CONTRIBUTOR_CHANGE_STATUSES:
        return HttpResponse(_("Author changes can be requested only before the final decision."), status=409)
    if submission.contributor_change_requests.filter(status=ContributorChangeRequest.Status.PENDING).exists():
        return HttpResponse(_("A request is already awaiting review."), status=409)
    try:
        initial = validate_contributors(submission.contributors_config)
    except ValueError:
        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
    form = ContributorChangeForm(request.POST or None)
    if request.method == "POST":
        try:
            total = int(request.POST.get("authors-TOTAL_FORMS", "-1"))
        except ValueError:
            total = -1
        if (request.POST.get("authors-INITIAL_FORMS") != str(len(initial)) or
                request.POST.get("authors-TOTAL_FORMS") != str(total) or
                not len(initial) <= total <= MAX_CONTRIBUTORS * 2):
            return HttpResponse(_("The author editor is out of date; reload the page."), status=409)
        formset = ContributorFormSet(request.POST, prefix="authors", initial=initial)
        form_valid, formset_valid = form.is_valid(), formset.is_valid()
        if form_valid and formset_valid:
            proposed = formset.cleaned_contributors
            if not proposed or not ready_to_submit(proposed):
                form.add_error(None, _("A proposed list needs at least one author and a corresponding author with an email."))
            elif proposed == initial:
                form.add_error(None, _("Change at least one author detail before submitting the request."))
            else:
                with transaction.atomic():
                    current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                    if not _author_can_view(request.user, current):
                        raise Http404
                    if (current.status not in CONTRIBUTOR_CHANGE_STATUSES or
                            request.POST.get("revision") != str(current.contributors_revision) or
                            current.contributors_config != initial or
                            current.contributor_change_requests.filter(status=ContributorChangeRequest.Status.PENDING).exists()):
                        return HttpResponse(_("The submission or request changed; reload the page."), status=409)
                    change = ContributorChangeRequest.objects.create(
                        submission=current, requested_by=request.user, reason=form.cleaned_data["reason"],
                        before=initial, proposed=proposed, base_revision=current.contributors_revision, acknowledged=True)
                    _event(current, request.user, "contributor_change_requested")
                messages.success(request, _("Author change requested. No email was sent."))
                return redirect("contributor_change_detail", pk=pk, change_id=change.pk)
        return render(request, "publishing/contributor_change_request.html",
                      {"submission":submission, "form":form, "formset":formset}, status=400)
    formset = ContributorFormSet(prefix="authors", initial=initial)
    return render(request, "publishing/contributor_change_request.html",
                  {"submission":submission, "form":form, "formset":formset})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def contributor_change_detail(request, pk, change_id):
    submission = _submission(pk)
    author_view = _author_can_view(request.user, submission)
    editor_view = _editor_can_view(request.user, submission)
    if not (author_view or editor_view):
        raise Http404
    change = get_object_or_404(ContributorChangeRequest.objects.select_related("requested_by", "closed_by"),
                               pk=change_id, submission=submission)
    try:
        before, proposed = validate_contributors(change.before), validate_contributors(change.proposed)
    except ValueError:
        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
    pending = change.status == ContributorChangeRequest.Status.PENDING
    try:
        rows = confirmation_rows(change, before, proposed) if editor_view else []
    except ValueError:
        return HttpResponse(_("The saved author checks are invalid; contact the local administrator."), status=409)
    evidence_files = change.evidence_files.all() if editor_view else change.evidence_files.filter(uploaded_by=request.user)
    response_requests = change.author_response_requests.select_related("recipient", "assigned_by", "revoked_by") if editor_view else []
    form = ContributorChangeDecisionForm(request.POST or None)
    if request.method == "POST":
        if not editor_view:
            raise Http404
        if not pending:
            return HttpResponse(_("This request has already been resolved."), status=409)
        if form.is_valid():
            with transaction.atomic():
                current = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                if not _editor_can_view(request.user, current):
                    raise Http404
                locked = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change_id, submission=current)
                if locked.status != ContributorChangeRequest.Status.PENDING:
                    return HttpResponse(_("This request has already been resolved."), status=409)
                outcome = form.cleaned_data["outcome"]
                if outcome == ContributorChangeRequest.Status.APPROVED:
                    try:
                        safe_before = validate_contributors(locked.before)
                        safe_proposed = validate_contributors(locked.proposed)
                        safe_current = validate_contributors(current.contributors_config)
                    except ValueError:
                        return HttpResponse(_("The saved author list is invalid; contact the local administrator."), status=409)
                    if (current.status not in CONTRIBUTOR_CHANGE_STATUSES or locked.requested_by_id != current.author_id or
                            not locked.acknowledged or current.contributors_revision != locked.base_revision or
                            safe_current != safe_before or not safe_proposed or not ready_to_submit(safe_proposed) or
                            safe_proposed == safe_before):
                        return HttpResponse(_("The submission or request changed; reload the page."), status=409)
                    if not all_confirmations_intact(locked, safe_before, safe_proposed):
                        form.add_error(None, _("Record an evidence check for every original and proposed author before approval. Each cited file must remain intact."))
                        return render(request, "publishing/contributor_change_detail.html", {
                            "submission":submission, "change":change, "before":before, "proposed":proposed,
                            "author_view":author_view, "editor_view":editor_view, "pending":pending, "form":form,
                            "evidence_files":evidence_files, "confirmation_rows":rows, "response_requests":response_requests}, status=409)
                    if not author_responses_ready(locked, safe_before, safe_proposed):
                        form.add_error(None, _("Resolve outstanding, disagreeing or ineligible author account responses before approval."))
                        return render(request, "publishing/contributor_change_detail.html", {
                            "submission":submission, "change":change, "before":before, "proposed":proposed,
                            "author_view":author_view, "editor_view":editor_view, "pending":pending, "form":form,
                            "evidence_files":evidence_files, "confirmation_rows":rows, "response_requests":response_requests}, status=409)
                    current.contributors_config = safe_proposed
                    current.contributors_revision += 1
                    current.save(update_fields=["contributors_config", "contributors_revision"])
                locked.status = outcome
                locked.closed_by = request.user
                locked.closed_at = timezone.now()
                locked.decision_note = form.cleaned_data["note"]
                locked.editor_attested = outcome == ContributorChangeRequest.Status.APPROVED and form.cleaned_data["verified"]
                locked.save(update_fields=["status", "closed_by", "closed_at", "decision_note", "editor_attested"])
                if locked.requested_by_id == current.author_id:
                    AuthorNotice.objects.create(submission=current, recipient_id=current.author_id,
                                                kind=AuthorNotice.Kind.AUTHOR_CHANGE, author_change=locked)
                _event(current, request.user, "contributor_change_reviewed")
            messages.success(request, _("Author change decision recorded. No email was sent."))
            return redirect("contributor_change_detail", pk=pk, change_id=change_id)
        return render(request, "publishing/contributor_change_detail.html", {
            "submission":submission, "change":change, "before":before, "proposed":proposed,
            "author_view":author_view, "editor_view":editor_view, "pending":pending, "form":form,
            "evidence_files":evidence_files, "confirmation_rows":rows, "response_requests":response_requests}, status=400)
    return render(request, "publishing/contributor_change_detail.html", {
        "submission":submission, "change":change, "before":before, "proposed":proposed,
        "author_view":author_view, "editor_view":editor_view, "pending":pending, "form":form,
        "evidence_files":evidence_files, "confirmation_rows":rows, "response_requests":response_requests})


@login_required
@never_cache
@require_POST
def withdraw_contributor_change(request, pk, change_id):
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(request.user, submission):
            raise Http404
        change = get_object_or_404(ContributorChangeRequest.objects.select_for_update(), pk=change_id, submission=submission)
        if change.status != ContributorChangeRequest.Status.PENDING:
            return HttpResponse(_("This request has already been resolved."), status=409)
        if request.POST.get("confirm") != "yes":
            return HttpResponse(_("Confirm the withdrawal."), status=400)
        change.status, change.closed_by, change.closed_at = ContributorChangeRequest.Status.WITHDRAWN, request.user, timezone.now()
        change.save(update_fields=["status", "closed_by", "closed_at"])
        _event(submission, request.user, "contributor_change_withdrawn")
    messages.success(request, _("Author change request withdrawn."))
    return redirect("contributor_change_detail", pk=pk, change_id=change_id)


@login_required
@never_cache
@require_GET
def submission_checklist(request, pk):
    from .submission_checklist import review_draft, signed_review
    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    if submission.status != Submission.Status.DRAFT:
        return HttpResponse(_("This draft has already been submitted."), status=409)
    review = review_draft(submission)
    return render(request, "publishing/submission_checklist.html", {"submission": submission, **review,
        "review_token": signed_review(submission, review) if review["ready"] else ""})


@login_required
@never_cache
@require_POST
def submission_submit(request, pk):
    from .submission_checklist import review_draft, review_matches, signed_review
    candidate = _submission(pk)
    if not _author_can_view(request.user, candidate):
        raise Http404
    with transaction.atomic():
        actor = get_object_or_404(get_user_model().objects.select_for_update(), pk=request.user.pk)
        get_object_or_404(Journal.objects.select_for_update(), pk=candidate.journal_id)
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _author_can_view(actor, submission):
            raise Http404
        if submission.status != Submission.Status.DRAFT:
            return HttpResponse(_("This draft has already been submitted."), status=409)
        review = review_draft(submission)
        if (not review["ready"] or request.POST.get("confirm") != "yes" or
                not review_matches(request.POST.get("review_token", ""), submission, review)):
            return render(request, "publishing/submission_checklist.html", {"submission": submission, **review,
                "review_token": signed_review(submission, review) if review["ready"] else "", "review_changed": True}, status=409)
        version = review["version"]
        if not version.metadata_recorded:
            # An unsubmitted legacy draft has no prior submitted metadata to reconstruct.
            version.title_snapshot, version.abstract_snapshot = submission.title, submission.abstract
            version.save(update_fields=["title_snapshot", "abstract_snapshot"])
        ReviewRound.objects.create(submission=submission, version=version, number=1)
        submission.status = Submission.Status.SUBMITTED
        submission.submitted_at = timezone.now()
        submission.save(update_fields=["status", "submitted_at"])
        AuthorNotice.objects.create(submission=submission, recipient_id=submission.author_id,
                                    kind=AuthorNotice.Kind.SUBMITTED)
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
            and not is_response_participant(request.user, submission)
            and not ProductionAssignment.objects.filter(draft__submission=submission, producer=request.user).exists()):
        current_round = _downloadable_review_round(submission)
        reviewer_access = bool(current_round and ReviewAssignment.objects.filter(
            submission=submission, reviewer=request.user, round=current_round, round__version=version,
            conflict=ReviewAssignment.Conflict.NONE,
            status__in=[ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED],
        ).exclude(submission__author=request.user).exists())
    if not (privileged or reviewer_access):
        raise Http404
    return _download_version(request, submission, version, reviewer=reviewer_access)


def _download_version(request, submission, version, reviewer=False, copy=False):
    if version.submission_id != submission.pk:
        raise Http404
    if reviewer and submission.review_mode == Journal.ReviewMode.SEPARATE:
        data = checked_review_copy_bytes(submission, version) if _file_exists(version.manuscript) else None
        if data is None:
            raise Http404
        # Serve precisely the bytes just verified, without reopening the path.
        stream = io.BytesIO(data)
    else:
        if reviewer and not _review_copy_ready(submission, version):
            raise Http404
        field = version.review_copy if copy else version.manuscript
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
    current_round = _downloadable_review_round(assignment.submission)
    if (not current_round or current_round.pk != assignment.round_id or
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
        data = read_review_copy(version)
        metadata_digest = review_metadata_sha256(version)
        if not version.review_copy_confirmed or data is None or not metadata_digest or not _file_exists(version.manuscript):
            return HttpResponse(_("A confirmed reviewer copy is required."), status=409)
        try:
            payload = signing.loads(request.POST.get("check_token", ""), salt=REVIEW_COPY_CHECK_SALT, max_age=3600)
        except signing.BadSignature:
            return HttpResponse(_("The submission or request changed; reload the page."), status=409)
        if payload != _review_copy_check_payload(submission, review_round, request.user, version, data):
            return HttpResponse(_("The submission or request changed; reload the page."), status=409)
        digest = review_copy_sha256(data)
        if (not version.review_copy_checked_at or not version.review_copy_checked_by_id or
                version.review_copy_checked_sha256 != digest or version.review_metadata_checked_sha256 != metadata_digest):
            version.review_copy_checked_by = request.user
            version.review_copy_checked_at = timezone.now()
            version.review_copy_checked_sha256 = digest
            version.review_metadata_checked_sha256 = metadata_digest
            version.save(update_fields=["review_copy_checked_by", "review_copy_checked_at", "review_copy_checked_sha256",
                                        "review_metadata_checked_sha256"])
            _event(submission, request.user, "review_copy_checked")
    messages.success(request, _("Manual check recorded for this version."))
    return redirect("submission_detail", pk=pk)


@login_required
@never_cache
@require_POST
def review_invite(request, pk):
    from .supporting_files import reviewer_files_ready
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404
    initial_round = _open_round(submission)
    if submission.status not in ACTIVE_REVIEW_STATUSES or not initial_round:
        return HttpResponse(_("There is no open review round."), status=409)
    if not _review_copy_ready(submission, initial_round.version):
        return HttpResponse(_("The reviewer file must be available and checked before inviting reviewers."), status=409)
    if not reviewer_files_ready(initial_round.version):
        return HttpResponse(_("Reviewer supporting files must be checked and available before inviting reviewers."), status=409)
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
        if not reviewer_files_ready(review_round.version):
            return HttpResponse(_("Reviewer supporting files must be checked and available before inviting reviewers."), status=409)
        if not _has_role(reviewer, submission.journal, Membership.Role.REVIEWER):
            raise Http404
        if (submission.reviews.filter(invited_by=reviewer).exists() or
                submission.decisions.filter(decided_by=reviewer).exists() or
                submission.editor_assignments.filter(editor=reviewer).exists() or
                is_response_participant(reviewer, submission) or
                ProductionAssignment.objects.filter(draft__submission=submission, producer=reviewer).exists() or
                Membership.objects.filter(user=reviewer, journal=submission.journal, role=Membership.Role.EDITOR).exists()):
            raise Http404
        assignment, created = ReviewAssignment.objects.get_or_create(
            submission=submission, round=review_round, reviewer=reviewer, defaults={"invited_by": request.user},
        )
        if created:
            notice = ReviewNotice.objects.create(assignment=assignment, recipient_id=assignment.reviewer_id,
                                                 kind=ReviewNotice.Kind.INVITATION, revision=0)
            from .local_review_mail import enqueue_invitation
            enqueue_invitation(notice)
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
    from .keywords import visible_keywords
    reviewer_keywords = visible_keywords(assignment.round.version.keywords_snapshot)
    form = ReportForm() if (assignment.status == ReviewAssignment.Status.ACCEPTED and
                            assignment.conflict == ReviewAssignment.Conflict.NONE and
                            assignment.round.status == ReviewRound.Status.OPEN) else None
    files = []
    current = _downloadable_review_round(assignment.submission)
    copy_ready = bool(current and current.pk == assignment.round_id and
                      _review_copy_ready(assignment.submission, assignment.round.version))
    if settings.DEBUG and copy_ready and assignment.status in ('accepted', 'submitted') and assignment.conflict == 'none':
        from .supporting_files import checked_and_current, verified_bytes
        candidates = SupportingFile.objects.filter(submission=assignment.submission, version=assignment.round.version,
            visibility=SupportingFile.Visibility.REVIEWER, removed_at__isnull=True)
        files = [item for item in candidates if checked_and_current(item) and verified_bytes(item) is not None]
    return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form,
        "review_ai_permissions_available": settings.DEBUG,
        "supporting_files": files, "review_copy_ready": copy_ready, "reviewer_keywords": reviewer_keywords})


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
                is_response_participant(request.user, submission) or
                not _has_role(request.user, submission.journal, Membership.Role.REVIEWER)):
            raise Http404
        if not review_round or assignment.round_id != review_round.pk:
            return HttpResponse(_("This review round is closed."), status=409)
        if action == "conflict":
            if assignment.status not in (ReviewAssignment.Status.INVITED, ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED) or assignment.conflict == ReviewAssignment.Conflict.YES:
                return HttpResponse(_("This conflict has already been recorded."), status=409)
            was_submitted = assignment.status == ReviewAssignment.Status.SUBMITTED
            if not was_submitted:
                assignment.status = ReviewAssignment.Status.CONFLICT
            if not assignment.responded_at:
                assignment.responded_at = timezone.now()
            assignment.conflict, assignment.conflict_details, assignment.conflict_declared_at = ReviewAssignment.Conflict.YES, details, timezone.now()
            assignment.save(update_fields=["status", "responded_at", "conflict", "conflict_details", "conflict_declared_at"])
            ConflictDeclaration.objects.create(assignment=assignment, declared_by=request.user, choice=ConflictDeclaration.Choice.YES, details=details)
            if was_submitted:
                from .review_ai_consent import supersede_on_report_change
                supersede_on_report_change(assignment, include_reviewer=True)
            _event(submission, request.user, "review_conflict_declared")
        elif action == "confirm_no_conflict":
            if assignment.status not in (ReviewAssignment.Status.ACCEPTED, ReviewAssignment.Status.SUBMITTED) or assignment.conflict != ReviewAssignment.Conflict.UNKNOWN:
                return HttpResponse(_("A declaration has already been recorded."), status=409)
            assignment.conflict, assignment.conflict_declared_at = ReviewAssignment.Conflict.NONE, timezone.now()
            assignment.save(update_fields=["conflict", "conflict_declared_at"])
            ConflictDeclaration.objects.create(assignment=assignment, declared_by=request.user, choice=ConflictDeclaration.Choice.NONE)
            if assignment.status == ReviewAssignment.Status.SUBMITTED:
                from .review_ai_consent import supersede_on_report_change
                supersede_on_report_change(assignment, include_reviewer=True)
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
        from .keywords import visible_keywords
        return render(request, "publishing/review_detail.html", {"assignment": assignment, "form": form,
            "reviewer_keywords":visible_keywords(assignment.round.version.keywords_snapshot),
            "review_copy_ready": _review_copy_ready(assignment.submission, assignment.round.version)}, status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=assignment.submission_id)
        review_round = _open_round(submission, locked=True)
        assignment = ReviewAssignment.objects.select_for_update().get(pk=pk)
        if (assignment.reviewer_id != request.user.pk or assignment.submission_id != submission.pk or
                is_response_participant(request.user, submission) or
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
        from .review_ai_consent import supersede_on_report_change
        supersede_on_report_change(assignment, include_reviewer=True)
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
            if submission.contributor_change_requests.filter(status=ContributorChangeRequest.Status.PENDING).exists():
                return HttpResponse(_("Resolve the pending author change request before recording an editorial decision."), status=409)
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
                AuthorNotice.objects.create(submission=submission, recipient_id=submission.author_id,
                                            kind=AuthorNotice.Kind.DECISION, decision=decision)
                _event(submission, request.user, "editorial_decision")
                messages.success(request, _("Decision recorded. No email was sent."))
                return redirect("submission_detail", pk=pk)
    pending_change = submission.contributor_change_requests.filter(status=ContributorChangeRequest.Status.PENDING).first()
    return render(request, "publishing/decision_form.html", {"submission": submission, "form": form,
                                                               "pending_contributor_change":pending_change})


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def submission_revision(request, pk):
    from .revision_submission import current_revision_decision, matches_revision_form, sign_revision_form
    def conflict(current):
        return render(request, "publishing/revision_conflict.html", {
            "submission": current, "response_text": request.POST.get("author_response", ""),
            "revised_title": request.POST.get("revised_title", ""),
            "revised_abstract": request.POST.get("revised_abstract", ""),
            "revised_keywords": request.POST.get("revised_keywords", "")[:1100]}, status=409)

    submission = _submission(pk)
    if not _author_can_view(request.user, submission):
        raise Http404
    if submission.status != Submission.Status.REVISION_REQUESTED:
        if request.method == "POST":
            return conflict(submission)
        return HttpResponse(_("A revision has not been requested."), status=409)
    decision = current_revision_decision(submission)
    if not decision:
        if request.method == "POST":
            return conflict(submission)
        return HttpResponse(_("The current revision decision is unavailable."), status=409)
    revision_token = sign_revision_form(submission, decision) if request.method == "GET" else request.POST.get("revision_token", "")
    if request.method == "POST" and not matches_revision_form(revision_token, submission, decision):
        return conflict(submission)
    prefilled_response = None
    if request.method == "GET":
        from .revision_response_views import response_prefill
        try:
            prefilled_response = response_prefill(request, submission)
        except ValueError:
            return HttpResponse(_("The response draft is incomplete or has changed; review it before continuing."), status=409)
    from .keywords import keywords_text
    try:
        current_keywords_text = keywords_text(submission.keywords_config)
    except ValueError:
        return HttpResponse(_("The saved keywords are invalid; contact the local administrator."), status=409)
    initial = {"revised_title": submission.title, "revised_abstract": submission.abstract,
               "revised_keywords": submission.keywords_config}
    if prefilled_response:
        initial["author_response"] = prefilled_response
    form_data = request.POST.copy() if request.method == "POST" else None
    # Older direct test clients did not send metadata; preserve the current values.
    if form_data is not None and "revised_title" not in form_data and "revised_abstract" not in form_data:
        form_data["revised_title"], form_data["revised_abstract"] = submission.title, submission.abstract
    keywords_supplied = form_data is not None and "revised_keywords" in form_data
    if form_data is not None and not keywords_supplied:
        form_data["revised_keywords"] = current_keywords_text
    form = RevisionForm(form_data, request.FILES or None, review_mode=submission.review_mode, initial=initial)
    if request.method == "POST" and form.is_valid():
        version = None
        saved_supporting = []
        try:
            with transaction.atomic():
                submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
                if not _author_can_view(request.user, submission):
                    raise Http404
                decision = current_revision_decision(submission)
                if not matches_revision_form(revision_token, submission, decision):
                    return conflict(submission)
                latest = submission.versions.order_by("-number").first()
                previous_round = submission.rounds.order_by("-number").first()
                if (not latest or not previous_round or previous_round.status != ReviewRound.Status.CLOSED or
                        previous_round.pk != decision.round_id or latest.pk != decision.round.version_id):
                    return conflict(submission)
                version = SubmissionVersion(
                    submission=submission, number=latest.number + 1, uploaded_by=request.user,
                    manuscript=form.cleaned_data["manuscript"], author_response=form.cleaned_data["author_response"],
                    title_snapshot=form.cleaned_data["revised_title"], abstract_snapshot=form.cleaned_data["revised_abstract"],
                    keywords_snapshot=(submission.keywords_config if not keywords_supplied or
                        submission.keywords_config is None and not form.cleaned_data["revised_keywords"]
                        else form.cleaned_data["revised_keywords"]),
                    review_copy=form.cleaned_data.get("review_copy") or "",
                    review_copy_confirmed=form.cleaned_data.get("review_copy_confirmed", False),
                )
                version.save()
                from .supporting_files import save_files
                save_files(submission, version, request.user, form.cleaned_data, saved_supporting)
                ReviewRound.objects.create(submission=submission, version=version, number=previous_round.number + 1)
                submission.status = Submission.Status.SUBMITTED
                submission.title, submission.abstract = version.title_snapshot, version.abstract_snapshot
                submission.keywords_config = version.keywords_snapshot
                submission.save(update_fields=["status", "title", "abstract", "keywords_config"])
                _event(submission, request.user, "revision_submitted")
        except Exception:
            for storage, name in saved_supporting:
                storage.delete(name)
            if version and version.manuscript.name and version.manuscript._committed:
                version.manuscript.storage.delete(version.manuscript.name)
            if version and version.review_copy.name and version.review_copy._committed:
                version.review_copy.storage.delete(version.review_copy.name)
            raise
        messages.success(request, _("Revision submitted for a new review round."))
        return redirect("submission_detail", pk=pk)
    return render(request, "publishing/revision_form.html", {"submission": submission, "form": form,
        "decision": decision, "next_version": decision.round.version.number + 1,
        "next_round": decision.round.number + 1, "revision_token": revision_token,
        "prefilled_response": bool(prefilled_response)})
