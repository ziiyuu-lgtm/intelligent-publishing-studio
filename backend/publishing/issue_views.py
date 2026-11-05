"""Fictional, opt-in local issue management; no external publication."""
import hashlib
import json
import re
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core import signing
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
from .production_views import _draft, _enabled, _fingerprint, _no_index, _producer_can_manage, _proof_intact, _source_valid, _stale
from .published_issue import issue_snapshot
from .views import _event, _has_role


def _coordinator_journal(user, slug):
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    if not _has_role(user, journal, Membership.Role.COORDINATOR):
        raise Http404
    return journal


def _independent_coordinator(user, articles):
    submission_ids = articles.values("submission_id")
    return not (
        articles.filter(submission__author=user).exists() or
        ContributorResponseRequest.objects.filter(change__submission_id__in=submission_ids, recipient=user).exists() or
        ReviewAssignment.objects.filter(submission_id__in=submission_ids, reviewer=user).exists() or
        EditorialAssignment.objects.filter(submission_id__in=submission_ids, editor=user, status=EditorialAssignment.Status.RECUSED).exists() or
        ProductionAssignment.objects.filter(draft__submission_id__in=submission_ids, producer=user, status=ProductionAssignment.Status.RECUSED).exists()
    )


def _articles_in_issue_order(issue, articles):
    """Use saved positions; append newly assigned articles in publication order."""
    saved = [] if issue.article_order is None else issue.article_order
    by_id = {str(article.pk): article for article in articles}
    if (not isinstance(saved, list) or any(not isinstance(value, str) for value in saved) or
            len(saved) != len(set(saved)) or any(value not in by_id for value in saved)):
        raise ValueError("Invalid saved issue order")
    selected = set(saved)
    return [by_id[value] for value in saved] + [article for article in articles if str(article.pk) not in selected]


def _issue_order_state(issue, articles):
    payload = {"issue": str(issue.pk), "saved": issue.article_order,
               "articles": [str(article.pk) for article in articles]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _invalid_issue_articles(articles, issue):
    return articles.filter(Q(published_at__isnull=True) | Q(published_proof__isnull=True) |
                           ~Q(submission__journal_id=issue.journal_id) |
                           ~Q(published_proof__draft_id=F("pk"))).exists()


ISSUE_RELEASE_SALT = "publishing.test-issue-release.v1"
ISSUE_RELEASE_MAX_AGE = 60 * 60


def _issue_release_contents(issue, articles):
    """Inspect public article metadata only; never expose unfinished drafts."""
    blockers = []
    if not articles.exists() or _invalid_issue_articles(articles, issue):
        blockers.append(_("The issue needs at least one published article and no unpublished assignments."))
    chronological = list(articles.filter(published_at__isnull=False, published_proof__isnull=False,
        published_proof__draft_id=F("pk"), submission__journal_id=issue.journal_id
    ).select_related("published_proof", "issue").order_by("published_at", "pk"))
    for article in chronological:
        article.proof_intact = _proof_intact(article.published_proof)
    if any(not article.proof_intact for article in chronological):
        blockers.append(_("A published test PDF is missing or changed; the issue cannot be released."))
    try:
        ordered = _articles_in_issue_order(issue, chronological)
    except ValueError:
        ordered = []
        blockers.append(_("The saved issue order is invalid; an administrator must review it."))
    return ordered, blockers


def _issue_release_state(issue, articles):
    payload = {"issue": issue_snapshot(issue), "journal": str(issue.journal_id),
        "journal_name": issue.journal.name, "saved_order": issue.article_order,
        "articles": [{"fingerprint": _fingerprint(article, article.published_proof),
                      "published_at": article.published_at.isoformat(),
                      "file": article.published_proof.file.name} for article in articles]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


@login_required
@never_cache
@require_GET
def review_test_issue_release(request, pk):
    _enabled()
    issue = get_object_or_404(JournalIssue.objects.select_related("journal__organisation"), pk=pk)
    if not _has_role(request.user, issue.journal, Membership.Role.COORDINATOR):
        raise Http404
    if issue.released_at:
        return HttpResponse(_("This test issue is already released."), status=409)
    articles = PublicationDraft.objects.filter(issue=issue)
    if not _independent_coordinator(request.user, articles):
        return HttpResponse(_("An independent coordinator must release this issue."), status=403)
    ordered, blockers = _issue_release_contents(issue, articles)
    token = "" if blockers else signing.dumps({"actor": str(request.user.pk),
        "state": _issue_release_state(issue, ordered)}, salt=ISSUE_RELEASE_SALT)
    return _no_index(render(request, "publishing/review_test_issue_release.html", {
        "issue": issue, "articles": ordered, "blockers": blockers, "release_token": token}))


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
@require_http_methods(["GET", "POST"])
def order_test_issue(request, pk):
    _enabled()
    with transaction.atomic():
        issues = JournalIssue.objects.select_for_update() if request.method == "POST" else JournalIssue.objects.all()
        issue = get_object_or_404(issues, pk=pk)
        if not _has_role(request.user, issue.journal, Membership.Role.COORDINATOR):
            raise Http404
        if issue.released_at:
            return HttpResponse(_("The order of a released test issue cannot be changed."), status=409)
        articles = PublicationDraft.objects.filter(issue=issue)
        if not _independent_coordinator(request.user, articles):
            return HttpResponse(_("An independent coordinator must manage this issue."), status=403)
        incomplete = _invalid_issue_articles(articles, issue)
        if incomplete and request.method == "POST":
            return HttpResponse(_("Publish all assigned test articles before setting their order."), status=409)
        chronological = list(articles.filter(published_at__isnull=False, published_proof__isnull=False,
                                             published_proof__draft_id=F("pk"),
                                             submission__journal_id=issue.journal_id).order_by("published_at", "pk"))
        try:
            ordered = _articles_in_issue_order(issue, chronological)
        except ValueError:
            return HttpResponse(_("The saved issue order is invalid; an administrator must review it."), status=409)
        state = _issue_order_state(issue, ordered)
        if request.method == "POST":
            if request.POST.get("synthetic") != "yes":
                return HttpResponse(_("Confirm this is a fictional test issue."), status=400)
            if request.POST.get("state") != state:
                return HttpResponse(_("The issue contents or order changed; reload the page."), status=409)
            if not ordered:
                return HttpResponse(_("Add published test articles before setting their order."), status=409)
            expected = {"position_" + str(article.pk) for article in ordered}
            provided = {key for key in request.POST if key.startswith("position_")}
            if provided != expected:
                return HttpResponse(_("Enter each position once, from 1 to the number of articles."), status=400)
            positions = []
            for article in ordered:
                values = request.POST.getlist("position_" + str(article.pk))
                if len(values) != 1 or not re.fullmatch(r"[0-9]{1,9}", values[0]):
                    return HttpResponse(_("Enter each position once, from 1 to the number of articles."), status=400)
                positions.append((int(values[0]), article))
            if sorted(position for position, _ in positions) != list(range(1, len(ordered) + 1)):
                return HttpResponse(_("Enter each position once, from 1 to the number of articles."), status=400)
            positions.sort(key=lambda item: item[0])
            selected = [str(article.pk) for _, article in positions]
            # Persist even an unchanged display order: this freezes those
            # positions so a later online-first article is appended.
            if selected != issue.article_order:
                issue.article_order = selected
                issue.save(update_fields=["article_order"])
                for article in ordered:
                    _event(article.submission, request.user, "test_issue_order_saved")
            messages.success(request, _("Test issue order saved."))
            return redirect("order_test_issue", pk=pk)
        return render(request, "publishing/order_test_issue.html", {"issue": issue, "articles": ordered,
                      "order_state": state, "incomplete": incomplete})


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
        if not _independent_coordinator(request.user, articles):
            return HttpResponse(_("An independent coordinator must release this issue."), status=403)
        ordered, blockers = _issue_release_contents(issue, articles)
        if blockers:
            return HttpResponse(blockers[0], status=409)
        try:
            reviewed = signing.loads(request.POST.get("release_token", ""),
                                     salt=ISSUE_RELEASE_SALT, max_age=ISSUE_RELEASE_MAX_AGE)
        except signing.BadSignature:
            reviewed = None
        if reviewed != {"actor": str(request.user.pk), "state": _issue_release_state(issue, ordered)}:
            return HttpResponse(_("The release review expired or the issue changed. Open a fresh release review before confirming."), status=409)
        issue.article_order = [str(article.pk) for article in ordered]
        issue.released_at, issue.released_by = timezone.now(), request.user
        issue.save(update_fields=["article_order", "released_at", "released_by"])
    messages.success(request, _("Fictional issue released in the local test environment."))
    return redirect("manage_test_issues", slug=journal.slug)


@require_GET
def test_issue(request, slug, pk):
    _enabled()
    issue = get_object_or_404(JournalIssue.objects.select_related("journal__organisation"), pk=pk,
        journal__slug=slug, journal__active=True, journal__organisation__active=True, released_at__isnull=False)
    chronological = list(issue.articles.filter(submission__journal_id=issue.journal_id, published_at__isnull=False,
        published_proof__isnull=False, published_proof__draft_id=F("pk")).order_by("published_at", "pk"))
    try:
        articles = _articles_in_issue_order(issue, chronological)
    except ValueError:
        raise Http404
    return _no_index(render(request, "publishing/test_issue.html", {"issue": issue, "journal": issue.journal, "articles": articles}))
