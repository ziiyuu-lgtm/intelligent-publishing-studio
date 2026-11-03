"""Read-only editorial overview of assigned author-list change requests."""
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Exists, OuterRef
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .author_responses import author_responses_ready, conflicting_recipient_ids, response_snapshot_digest
from .contributor_evidence import confirmation_rows
from .contributors import validate_contributors
from .models import ContributorChangeRequest, ContributorConfirmationEvent, ContributorResponseRequest, Membership
from .views import _editorial_submissions

PAGE_SIZE = 12


def _summarize(change, now):
    """Summarise records, without claiming that evidence or identity is verified."""
    tasks = list(change.author_response_requests.exclude(status=ContributorResponseRequest.Status.REVOKED).select_related("recipient"))
    result = {
        "change":change,
        "awaiting":sum(t.status == ContributorResponseRequest.Status.PENDING for t in tasks),
        "objections":sum(t.status == ContributorResponseRequest.Status.DISAGREED for t in tasks),
        "agreed":sum(t.status == ContributorResponseRequest.Status.AGREED for t in tasks),
        "overdue":sum(t.status == ContributorResponseRequest.Status.PENDING and t.expires_at <= now for t in tasks),
        "task_count":len(tasks), "invalid":False, "account_issue":False,
        "manual_checked":0, "manual_total":0, "manual_disputed":0,
    }
    try:
        before = validate_contributors(change.before)
        proposed = validate_contributors(change.proposed)
        rows = confirmation_rows(change, before, proposed)
        result["manual_total"] = len(rows)
        result["manual_checked"] = sum(row["confirmed"] for row in rows)
        result["manual_disputed"] = sum(row["event"] is not None and
            row["event"].state == ContributorConfirmationEvent.State.DISPUTED for row in rows)
        if tasks:
            submission = change.submission
            permitted = set(Membership.objects.filter(user_id__in={t.recipient_id for t in tasks},
                journal=submission.journal, role=Membership.Role.AUTHOR, active=True).values_list("user_id", flat=True))
            excluded = conflicting_recipient_ids(submission)
            snapshots = {"original":before, "proposed":proposed}
            digest = response_snapshot_digest(change)
            result["account_issue"] = any(
                not task.recipient.is_active or task.recipient_id not in permitted or task.recipient_id in excluded or
                task.side not in snapshots or not 0 <= task.position < len(snapshots.get(task.side, [])) or
                task.snapshot_digest != digest for task in tasks)
            # Even eligible accounts can have an inconsistent or missing agreement event.
            if not result["awaiting"] and not result["objections"]:
                result["account_issue"] |= not author_responses_ready(change, before, proposed)
    except ValueError:
        result["invalid"] = True
    return result


@login_required
@never_cache
@require_GET
def author_change_dashboard(request):
    now = timezone.now()
    active_tasks = ContributorResponseRequest.objects.filter(change_id=OuterRef("pk"))
    changes = (ContributorChangeRequest.objects.filter(status=ContributorChangeRequest.Status.PENDING,
        submission__in=_editorial_submissions(request.user).values("pk"))
        .select_related("submission__journal__organisation")
        .annotate(has_waiting=Exists(active_tasks.filter(status=ContributorResponseRequest.Status.PENDING)),
                  has_overdue=Exists(active_tasks.filter(status=ContributorResponseRequest.Status.PENDING, expires_at__lte=now)),
                  has_objection=Exists(active_tasks.filter(status=ContributorResponseRequest.Status.DISAGREED)))
        .order_by("-requested_at", "-pk"))
    # Numbers refer to requests, not the number of people; categories can overlap.
    counts = {"all":changes.count(), "waiting":changes.filter(has_waiting=True).count(),
              "overdue":changes.filter(has_overdue=True).count(),
              "objection":changes.filter(has_objection=True).count()}
    selected = request.GET.get("filter", "all")
    filters = {"waiting":"has_waiting", "overdue":"has_overdue", "objection":"has_objection"}
    if selected not in filters and selected != "all":
        selected = "all"
    if selected in filters:
        changes = changes.filter(**{filters[selected]:True})
    page = Paginator(changes, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(request, "publishing/author_change_dashboard.html", {
        "counts":counts, "selected":selected, "page":page,
        "rows":[_summarize(change, now) for change in page.object_list],
    })
