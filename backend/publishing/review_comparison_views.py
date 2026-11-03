"""Read-only comparison of completed reviews for a single assigned editor."""
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import ReviewAssignment, ReviewRound
from .views import _editor_can_view, _submission


@login_required
@never_cache
@require_GET
def review_comparison(request, pk):
    submission = _submission(pk)
    if not _editor_can_view(request.user, submission):
        raise Http404

    # Ignore damaged cross-submission relationships rather than mixing records.
    rounds = list(submission.rounds.filter(version__submission=submission)
                  .select_related("version").order_by("-number"))
    requested = request.GET.getlist("round")
    if requested:
        raw = requested[0]
        if len(requested) != 1 or not raw.isascii() or not raw.isdigit() or len(raw) > 9:
            raise Http404
        selected = next((item for item in rounds if item.number == int(raw)), None)
        if selected is None:
            raise Http404
    else:
        selected = next((item for item in rounds if item.status == ReviewRound.Status.OPEN), None)
        selected = selected or (rounds[0] if rounds else None)

    assignments = list(ReviewAssignment.objects.filter(submission=submission, round=selected)
                       .select_related("reviewer").order_by("created_at", "pk")) if selected else []
    reports = [item for item in assignments if item.status == ReviewAssignment.Status.SUBMITTED
               and item.conflict == ReviewAssignment.Conflict.NONE]
    counts = {
        "reports": len(reports),
        "accepted": sum(item.status == ReviewAssignment.Status.ACCEPTED and item.conflict != ReviewAssignment.Conflict.YES for item in assignments),
        "invited": sum(item.status == ReviewAssignment.Status.INVITED and item.conflict != ReviewAssignment.Conflict.YES for item in assignments),
        "flagged": sum(item.conflict == ReviewAssignment.Conflict.YES for item in assignments),
        "undeclared": sum(item.status == ReviewAssignment.Status.SUBMITTED and item.conflict == ReviewAssignment.Conflict.UNKNOWN for item in assignments),
    }
    return render(request, "publishing/review_comparison.html", {
        "submission": submission, "rounds": rounds, "selected_round": selected,
        "reports": reports, "counts": counts,
    })
