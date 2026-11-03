"""Read-only revision packages for currently assigned editors."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import F, OuterRef, Q, Subquery, Value
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import EditorialDecision, Journal, ReviewRound, Submission, SubmissionVersion
from .views import ACTIVE_REVIEW_STATUSES, _editor_can_view, _editorial_submissions, _file_exists, _review_copy_ready


def _returned_rounds(user):
    """Only link a current round to its actual preceding revision decision."""
    if not user.is_active:
        return ReviewRound.objects.none()
    latest_version = SubmissionVersion.objects.filter(submission_id=OuterRef("submission_id")).order_by("-number").values("pk")[:1]
    prior = EditorialDecision.objects.filter(
        submission_id=OuterRef("submission_id"),
        round__submission_id=OuterRef("submission_id"),
        round__status=ReviewRound.Status.CLOSED,
        round__number=OuterRef("number") - Value(1),
        round__version__submission_id=OuterRef("submission_id"),
        round__version__number=OuterRef("version__number") - Value(1),
        outcome__in=(EditorialDecision.Outcome.MINOR, EditorialDecision.Outcome.MAJOR),
    ).order_by("pk")
    return ReviewRound.objects.filter(
        submission__in=_editorial_submissions(user).values("pk"),
        submission__status__in=ACTIVE_REVIEW_STATUSES,
        status=ReviewRound.Status.OPEN, number__gt=1,
        version__submission_id=F("submission_id"), version__number__gt=1,
        version_id=Subquery(latest_version),
    ).annotate(prior_decision_id=Subquery(prior.values("pk")[:1])).filter(
        prior_decision_id__isnull=False,
    ).select_related("submission__journal", "submission__journal__organisation", "version").order_by("-version__created_at", "-pk")


@login_required
@never_cache
@require_GET
def returned_revisions(request):
    rounds = _returned_rounds(request.user)
    counts = {"all": rounds.count(),
              "submitted": rounds.filter(submission__status=Submission.Status.SUBMITTED).count(),
              "under_review": rounds.filter(submission__status=Submission.Status.UNDER_REVIEW).count()}
    selected = request.GET.get("filter", "all")
    if selected not in counts:
        selected = "all"
    if selected != "all":
        rounds = rounds.filter(submission__status=selected)
    query = request.GET.get("q", "").strip()[:120]
    if query:
        rounds = rounds.filter(Q(submission__title__icontains=query) | Q(submission__journal__name__icontains=query))
    page = Paginator(rounds, 12).get_page(request.GET.get("page"))
    return render(request, "publishing/returned_revisions.html", {"counts": counts, "selected": selected, "query": query, "page": page})


@login_required
@never_cache
@require_GET
def returned_revision(request, pk):
    current = get_object_or_404(_returned_rounds(request.user), submission_id=pk)
    submission = current.submission
    if not _editor_can_view(request.user, submission):
        raise Http404
    decision = get_object_or_404(EditorialDecision.objects.select_related("round__version"), pk=current.prior_decision_id,
        submission=submission, round__submission=submission, round__status=ReviewRound.Status.CLOSED,
        round__number=current.number - 1, round__version__submission=submission,
        round__version__number=current.version.number - 1,
        outcome__in=(EditorialDecision.Outcome.MINOR, EditorialDecision.Outcome.MAJOR))
    if submission.review_mode == Journal.ReviewMode.DIRECT:
        review_copy_state = "direct" if _file_exists(current.version.manuscript) else "unavailable"
    elif _review_copy_ready(submission, current.version):
        review_copy_state = "checked"
    else:
        review_copy_state = "needed"
    return render(request, "publishing/returned_revision.html", {
        "submission": submission, "current": current, "decision": decision,
        "previous_version": decision.round.version, "review_copy_state": review_copy_state,
        "review_copy_available": _file_exists(current.version.review_copy),
    })
