"""Private, local-only review deadline tracking for currently assigned editors."""
from datetime import timezone as datetime_timezone
from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Subquery
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from .models import Membership, ReviewAssignment, ReviewDeadlineEvent, ReviewRound, Submission, SubmissionVersion
from .views import ACTIVE_REVIEW_STATUSES, _editor_can_view, _editorial_submissions, _event, _open_round, _submission

OPEN_TASK_STATUSES = (ReviewAssignment.Status.INVITED, ReviewAssignment.Status.ACCEPTED)


def utc_today():
    """Use the promised UTC date even if a user or server selects another timezone."""
    return timezone.now().astimezone(datetime_timezone.utc).date()


class ReviewDeadlineForm(forms.Form):
    due_on = forms.DateField(label=gettext_lazy("Review deadline (UTC)"), required=False,
        widget=forms.DateInput(format="%Y-%m-%d", attrs={"type":"date"}),
        help_text=gettext_lazy("Leave blank to remove the deadline. The date lasts through the end of the UTC day."))
    reason = forms.CharField(label=gettext_lazy("Reason for setting or changing the deadline"), max_length=500,
                             widget=forms.Textarea(attrs={"rows":3}))
    revision = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    def clean_due_on(self):
        value = self.cleaned_data["due_on"]
        if value and value < utc_today():
            raise forms.ValidationError(gettext_lazy("Choose today or a future UTC date."))
        return value

    def clean_reason(self):
        value = self.cleaned_data["reason"].strip()
        if not value:
            raise forms.ValidationError(gettext_lazy("Please provide a reason."))
        return value


def _can_manage(assignment, submission):
    current = _open_round(submission)
    return bool(current and assignment.round_id == current.pk and assignment.submission_id == submission.pk and
        submission.status in ACTIVE_REVIEW_STATUSES and assignment.status in OPEN_TASK_STATUSES and
        assignment.conflict != ReviewAssignment.Conflict.YES)


@login_required
@never_cache
@require_GET
def review_progress(request):
    today = utc_today()
    latest_version = SubmissionVersion.objects.filter(submission_id=OuterRef("submission_id")).order_by("-number").values("pk")[:1]
    active_role = Membership.objects.filter(
        journal_id=OuterRef("submission__journal_id"), user_id=OuterRef("reviewer_id"), role=Membership.Role.REVIEWER, active=True)
    assignments = ReviewAssignment.objects.filter(
        submission__in=_editorial_submissions(request.user).values("pk"),
        submission__status__in=ACTIVE_REVIEW_STATUSES, status__in=OPEN_TASK_STATUSES,
        round__status=ReviewRound.Status.OPEN, round__submission_id=F("submission_id"),
        round__version__submission_id=F("submission_id"), round__version_id=Subquery(latest_version),
    ).exclude(conflict=ReviewAssignment.Conflict.YES).select_related("submission__journal", "reviewer", "round").annotate(
        reviewer_role_active=Exists(active_role)).order_by(F("due_on").asc(nulls_last=True), "created_at", "pk")
    counts = {"all":assignments.count(), "invited":assignments.filter(status=ReviewAssignment.Status.INVITED).count(),
              "accepted":assignments.filter(status=ReviewAssignment.Status.ACCEPTED).count(),
              "overdue":assignments.filter(due_on__lt=today).count(),
              "no_deadline":assignments.filter(due_on__isnull=True).count()}
    selected = request.GET.get("filter", "all")
    filters = {"invited":{"status":ReviewAssignment.Status.INVITED}, "accepted":{"status":ReviewAssignment.Status.ACCEPTED},
               "overdue":{"due_on__lt":today}, "no_deadline":{"due_on__isnull":True}}
    if selected not in filters and selected != "all":
        selected = "all"
    if selected in filters:
        assignments = assignments.filter(**filters[selected])
    page = Paginator(assignments, 12).get_page(request.GET.get("page"))
    return render(request, "publishing/review_progress.html", {
        "counts":counts, "selected":selected, "page":page, "today":today,
    })


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def manage_review_deadline(request, pk):
    candidate = get_object_or_404(ReviewAssignment.objects.select_related("reviewer", "round"), pk=pk)
    submission = _submission(candidate.submission_id)
    if not _editor_can_view(request.user, submission) or candidate.round.submission_id != submission.pk:
        raise Http404
    can_manage = _can_manage(candidate, submission)
    form = ReviewDeadlineForm(request.POST or None, initial={"due_on":candidate.due_on, "revision":candidate.deadline_revision})
    history = candidate.deadline_events.select_related("recorded_by")[:50]
    context = {"submission":submission, "assignment":candidate, "can_manage":can_manage, "form":form, "history":history}
    if request.method == "POST":
        if not can_manage:
            return HttpResponse(_("This review task is no longer open for deadline changes."), status=409)
        if not form.is_valid():
            return render(request, "publishing/manage_review_deadline.html", context, status=400)
        with transaction.atomic():
            submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=candidate.submission_id)
            if not _editor_can_view(request.user, submission):
                raise Http404
            current_round = _open_round(submission, locked=True)
            assignment = get_object_or_404(ReviewAssignment.objects.select_for_update(), pk=pk, submission=submission)
            if (not current_round or assignment.round_id != current_round.pk or submission.status not in ACTIVE_REVIEW_STATUSES or
                    assignment.status not in OPEN_TASK_STATUSES or assignment.conflict == ReviewAssignment.Conflict.YES):
                return HttpResponse(_("This review task is no longer open for deadline changes."), status=409)
            if form.cleaned_data["revision"] != assignment.deadline_revision:
                return HttpResponse(_("The deadline changed; reload the page before saving."), status=409)
            due_on = form.cleaned_data["due_on"]
            if due_on and due_on < utc_today():
                return HttpResponse(_("Choose today or a future UTC date."), status=400)
            if due_on != assignment.due_on:
                old_date = assignment.due_on
                assignment.due_on, assignment.deadline_revision = due_on, assignment.deadline_revision + 1
                assignment.save(update_fields=["due_on", "deadline_revision"])
                ReviewDeadlineEvent.objects.create(assignment=assignment, previous_date=old_date, new_date=due_on,
                    revision=assignment.deadline_revision, reason=form.cleaned_data["reason"], recorded_by=request.user)
                _event(submission, request.user, "review_deadline_changed")
        messages.success(request, _("Deadline saved locally. No email was sent."))
        return redirect("manage_review_deadline", pk=pk)
    return render(request, "publishing/manage_review_deadline.html", context)
