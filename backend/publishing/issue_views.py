"""Fictional, opt-in local issue management; no external publication."""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import JournalIssueForm
from .models import ContributorResponseRequest, EditorialAssignment, Journal, JournalIssue, Membership, ProductionAssignment, PublicationDraft, ReviewAssignment
from .production_views import _draft, _enabled, _no_index, _producer_can_manage, _proof_intact, _source_valid, _stale
from .views import _event, _has_role


def _coordinator_journal(user, slug):
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    if not _has_role(user, journal, Membership.Role.COORDINATOR):
        raise Http404
    return journal


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def manage_test_issues(request, slug):
    _enabled()
    journal = _coordinator_journal(request.user, slug)
    form = JournalIssueForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        with transaction.atomic():
            journal = get_object_or_404(Journal.objects.select_for_update().select_related("organisation"), pk=journal.pk)
            if not _has_role(request.user, journal, Membership.Role.COORDINATOR):
                raise Http404
            issue, created = JournalIssue.objects.get_or_create(journal=journal, volume=data["volume"], number=data["number"],
                defaults={"year": data["year"], "title": data["title"], "created_by": request.user})
            if not created and (issue.year != data["year"] or issue.title != data["title"]):
                return HttpResponse(_("This volume and issue already exist with different details."), status=409)
        messages.success(request, _("Test issue created.") if created else _("This test issue already exists."))
        return redirect("manage_test_issues", slug=slug)
    return render(request, "publishing/manage_test_issues.html", {"journal": journal, "form": form,
        "issues": journal.test_issues.all()}, status=400 if request.method == "POST" else 200)


@login_required
@never_cache
@require_POST
def set_production_issue(request, pk):
    _enabled()
    with transaction.atomic():
        draft = _draft(pk, locked=True)
        if not _producer_can_manage(request.user, draft):
            raise Http404
        if draft.published_at or _stale(request, draft) or not _source_valid(draft.submission, draft.decision):
            return HttpResponse(_("Production changed or is closed; reload the page."), status=409)
        if "issue" not in request.POST:
            return HttpResponse(_("Choose an available issue from this journal."), status=400)
        raw = request.POST.get("issue", "")
        issue = None
        if raw:
            try:
                issue_id = uuid.UUID(raw)
            except (ValueError, TypeError, AttributeError):
                return HttpResponse(_("Choose an available issue from this journal."), status=400)
            issue = JournalIssue.objects.select_for_update().filter(pk=issue_id, journal_id=draft.submission.journal_id).first()
            if not issue or issue.released_at:
                return HttpResponse(_("Choose an available issue from this journal."), status=400)
        if draft.issue_id != (issue.pk if issue else None):
            draft.issue = issue
            draft.revision += 1
            draft.save(update_fields=["issue", "revision"])
            _event(draft.submission, request.user, "test_issue_selection_changed")
    return redirect("production_detail", pk=pk)


@login_required
@never_cache
@require_POST
def release_test_issue(request, pk):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        issue = get_object_or_404(JournalIssue.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        journal = issue.journal
        if not _has_role(request.user, journal, Membership.Role.COORDINATOR):
            raise Http404
        if issue.released_at:
            return HttpResponse(_("This test issue is already released."), status=409)
        articles = PublicationDraft.objects.filter(issue=issue)
        if (not articles.exists() or articles.filter(Q(published_at__isnull=True) | Q(published_proof__isnull=True) |
             ~Q(submission__journal_id=issue.journal_id) | ~Q(published_proof__draft_id=F("pk"))).exists()):
            return HttpResponse(_("The issue needs at least one published article and no unpublished assignments."), status=409)
        submission_ids = articles.values("submission_id")
        if (articles.filter(submission__author=request.user).exists() or
                ContributorResponseRequest.objects.filter(change__submission_id__in=submission_ids, recipient=request.user).exists() or
                ReviewAssignment.objects.filter(submission_id__in=submission_ids, reviewer=request.user).exists() or
                EditorialAssignment.objects.filter(submission_id__in=submission_ids, editor=request.user, status=EditorialAssignment.Status.RECUSED).exists() or
                ProductionAssignment.objects.filter(draft__submission_id__in=submission_ids, producer=request.user, status=ProductionAssignment.Status.RECUSED).exists()):
            return HttpResponse(_("An independent coordinator must release this issue."), status=403)
        if any(not _proof_intact(article.published_proof) for article in articles.select_related("published_proof").iterator()):
            return HttpResponse(_("A published test PDF is missing or changed; the issue cannot be released."), status=409)
        issue.released_at, issue.released_by = timezone.now(), request.user
        issue.save(update_fields=["released_at", "released_by"])
    messages.success(request, _("Fictional issue released in the local test environment."))
    return redirect("manage_test_issues", slug=journal.slug)


@require_GET
def test_issue(request, slug, pk):
    _enabled()
    issue = get_object_or_404(JournalIssue.objects.select_related("journal__organisation"), pk=pk,
        journal__slug=slug, journal__active=True, journal__organisation__active=True, released_at__isnull=False)
    articles = issue.articles.filter(submission__journal_id=issue.journal_id, published_at__isnull=False,
        published_proof__isnull=False, published_proof__draft_id=F("pk")).order_by("published_at", "pk")
    return _no_index(render(request, "publishing/test_issue.html", {"issue": issue, "journal": issue.journal, "articles": articles}))
