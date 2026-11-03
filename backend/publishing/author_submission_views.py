"""Private, read-only submission tracking for the submitting account."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import EditorialDecision, ReviewRound, Submission
from .views import _author_can_view


def _owned_submissions(user):
    if not user.is_authenticated or not user.is_active:
        return Submission.objects.none()
    return Submission.objects.filter(author=user, journal__active=True,
                                     journal__organisation__active=True).select_related("journal")


@login_required
@never_cache
@require_GET
def author_submissions(request):
    submissions = _owned_submissions(request.user)
    counts = submissions.aggregate(
        total=Count("pk"),
        revision=Count("pk", filter=Q(status=Submission.Status.REVISION_REQUESTED)),
        review=Count("pk", filter=Q(status=Submission.Status.UNDER_REVIEW)),
        drafts=Count("pk", filter=Q(status=Submission.Status.DRAFT)),
    )
    status = request.GET.get("status", "all")
    if status not in {"all", *Submission.Status.values}:
        status = "all"
    query = request.GET.get("q", "").strip()[:100]
    if status != "all":
        submissions = submissions.filter(status=status)
    if query:
        submissions = submissions.filter(Q(title__icontains=query) | Q(journal__name__icontains=query))
    page = Paginator(submissions.order_by("-created_at", "-pk"), 12).get_page(request.GET.get("page"))
    return render(request, "publishing/author_submissions.html", {
        "counts": counts, "selected": status, "query": query, "page": page,
        "status_choices": Submission.Status.choices,
    })


@login_required
@never_cache
@require_GET
def author_submission_progress(request, pk):
    submission = get_object_or_404(_owned_submissions(request.user).select_related("journal__organisation"), pk=pk)
    if not _author_can_view(request.user, submission):
        raise Http404

    # These entries are derived only from recorded author-visible business objects.
    # Do not use the audit trail or reveal reviewers, review tasks or their activity.
    events = [{"kind": "draft", "when": submission.created_at}]
    if submission.submitted_at:
        events.append({"kind": "submitted", "when": submission.submitted_at})
    rounds = ReviewRound.objects.filter(submission=submission, version__submission=submission,
                                        number__gt=1, version__number__gt=1).select_related("version").order_by("number")
    seen_versions = set()
    for review_round in rounds:
        if review_round.version_id in seen_versions:
            continue
        seen_versions.add(review_round.version_id)
        events.append({"kind": "revision", "when": review_round.version.created_at,
                       "number": review_round.version.number, "round": review_round.number})
    decisions = EditorialDecision.objects.filter(submission=submission, round__submission=submission,
        round__version__submission=submission).select_related("round").order_by("created_at", "pk")
    for decision in decisions:
        events.append({"kind": "decision", "when": decision.created_at, "round": decision.round.number,
                       "outcome": decision.get_outcome_display()})
    events.sort(key=lambda item: item["when"], reverse=True)
    return render(request, "publishing/author_submission_progress.html", {
        "submission": submission, "events": events,
    })
