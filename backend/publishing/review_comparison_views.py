"""Read-only comparison of completed reviews for a single assigned editor."""
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .models import ReviewAIConsent, ReviewAssignment, ReviewRound
from .review_ai_consent import PROVIDER, PURPOSE, author_active, existing_author, reviewer_active
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
                       .select_related("reviewer", "round__version").order_by("created_at", "pk")) if selected else []
    reports = [item for item in assignments if item.status == ReviewAssignment.Status.SUBMITTED
               and item.conflict == ReviewAssignment.Conflict.NONE]
    counts = {
        "reports": len(reports),
        "accepted": sum(item.status == ReviewAssignment.Status.ACCEPTED and item.conflict != ReviewAssignment.Conflict.YES for item in assignments),
        "invited": sum(item.status == ReviewAssignment.Status.INVITED and item.conflict != ReviewAssignment.Conflict.YES for item in assignments),
        "flagged": sum(item.conflict == ReviewAssignment.Conflict.YES for item in assignments),
        "undeclared": sum(item.status == ReviewAssignment.Status.SUBMITTED and item.conflict == ReviewAssignment.Conflict.UNKNOWN for item in assignments),
    }
    ai_readiness = None
    if settings.DEBUG and selected:
        author_consent = existing_author(selected, submission.author)
        consents = ReviewAIConsent.objects.filter(assignment__in=reports, subject=ReviewAIConsent.Subject.REVIEWER,
            provider=PROVIDER, purpose=PURPOSE, ended_at__isnull=True)
        by_assignment = {item.assignment_id: item for item in consents}
        ai_readiness = {"author": author_active(author_consent, selected, reports),
                        "reviewers": sum(reviewer_active(by_assignment.get(item.pk), item) for item in reports),
                        "total": len(reports)}
    return render(request, "publishing/review_comparison.html", {
        "submission": submission, "rounds": rounds, "selected_round": selected,
        "reports": reports, "counts": counts, "ai_readiness": ai_readiness,
    })
