"""Place fictional online-first articles into issues with author confirmation."""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .models import JournalIssue, PublicationApproval, PublishedIssueProposal
from .production_views import (_draft, _enabled, _fingerprint, _latest_proof, _producer_can_manage,
                               _proof_intact, _source_valid, _stale)
from .published_issue import issue_snapshot, proposal_token
from .views import _author_can_view, _event


def _published_source_valid(draft):
    """Require the same already-published, author-confirmed source and PDF."""
    proof = draft.published_proof
    if (not draft.published_at or draft.issue_id or not proof or proof.draft_id != draft.pk or
            not _source_valid(draft.submission, draft.decision)):
        return False
    latest = _latest_proof(draft)
    if not latest or latest.pk != proof.pk or not _proof_intact(proof):
        return False
    return PublicationApproval.objects.filter(draft=draft, proof=proof, revision=draft.revision,
        fingerprint=_fingerprint(draft, proof), approved_by=draft.submission.author).exists()


def _resolve(proposal, status, actor):
    proposal.status, proposal.resolved_at, proposal.resolved_by = status, timezone.now(), actor
    proposal.save(update_fields=["status", "resolved_at", "resolved_by"])


@login_required
@never_cache
@require_POST
def propose_published_issue(request, pk):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        issue_id = uuid.UUID(request.POST.get("issue", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Choose an available issue from this journal."), status=400)
    with transaction.atomic():
        draft = _draft(pk, locked=True)
        if not _producer_can_manage(request.user, draft):
            raise Http404
        if (_stale(request, draft) or not _published_source_valid(draft) or
                request.POST.get("fingerprint") != _fingerprint(draft, draft.published_proof)):
            return HttpResponse(_("Production changed or is incomplete; reload the page."), status=409)
        # This lock also serializes assignment against release_test_issue().
        issue = JournalIssue.objects.select_for_update().filter(pk=issue_id, journal_id=draft.submission.journal_id).first()
        if not issue or issue.released_at:
            return HttpResponse(_("Choose an available issue from this journal."), status=400)
        pending = PublishedIssueProposal.objects.select_for_update().filter(draft=draft, status="pending").first()
        if (request.POST.get("current_proposal", "") != (str(pending.pk) if pending else "") or
                request.POST.get("proposal_token", "") != (proposal_token(pending) if pending else "")):
            return HttpResponse(_("The issue proposal changed; reload the page."), status=409)
        snapshot = issue_snapshot(issue)
        source = _fingerprint(draft, draft.published_proof)
        if pending and (pending.issue_id == issue.pk and pending.proposed_by_id == request.user.pk and
                        pending.source_fingerprint == source and pending.issue_snapshot == snapshot):
            return redirect("production_detail", pk=pk)
        if pending:
            _resolve(pending, PublishedIssueProposal.Status.WITHDRAWN, request.user)
            _event(draft.submission, request.user, "published_issue_withdrawn")
        PublishedIssueProposal.objects.create(draft=draft, issue=issue, proposed_by=request.user,
                                              source_fingerprint=source, issue_snapshot=snapshot)
        _event(draft.submission, request.user, "published_issue_proposed")
    messages.success(request, _("Issue proposal saved for the submitting author's review."))
    return redirect("production_detail", pk=pk)


@login_required
@never_cache
@require_POST
def resolve_published_issue(request, pk, proposal_id):
    _enabled()
    choice = request.POST.get("choice")
    if choice not in ("accept", "decline", "withdraw"):
        return HttpResponse(_("Choose an action for this issue proposal."), status=400)
    with transaction.atomic():
        draft = _draft(pk, locked=True)
        if choice in ("accept", "decline"):
            if not _author_can_view(request.user, draft.submission):
                raise Http404
        elif not _producer_can_manage(request.user, draft):
            raise Http404
        proposal = get_object_or_404(PublishedIssueProposal.objects.select_for_update(), pk=proposal_id, draft=draft)
        if proposal.status != PublishedIssueProposal.Status.PENDING or request.POST.get("proposal_token") != proposal_token(proposal):
            return HttpResponse(_("The issue proposal changed; reload the page."), status=409)
        if choice != "accept":
            status = PublishedIssueProposal.Status.DECLINED if choice == "decline" else PublishedIssueProposal.Status.WITHDRAWN
            _resolve(proposal, status, request.user)
            _event(draft.submission, request.user, "published_issue_" + status)
            messages.success(request, _("Issue proposal closed."))
            return redirect("production_detail", pk=pk)
        if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
            return HttpResponse(_("Both confirmations are required."), status=400)
        issue = JournalIssue.objects.select_for_update().filter(pk=proposal.issue_id, journal_id=draft.submission.journal_id).first()
        if (not issue or issue.released_at or issue_snapshot(issue) != proposal.issue_snapshot or
                not _producer_can_manage(proposal.proposed_by, draft) or not _published_source_valid(draft) or
                proposal.source_fingerprint != _fingerprint(draft, draft.published_proof)):
            return HttpResponse(_("The article or issue changed; ask for a new proposal."), status=409)
        # Retain the previous approval as evidence and add a new approval for the
        # exact new issue, without modifying the published proof or public text.
        draft.issue = issue
        draft.revision += 1
        draft.save(update_fields=["issue", "revision"])
        PublicationApproval.objects.create(draft=draft, proof=draft.published_proof, revision=draft.revision,
                                           fingerprint=_fingerprint(draft, draft.published_proof), approved_by=request.user)
        _resolve(proposal, PublishedIssueProposal.Status.ACCEPTED, request.user)
        _event(draft.submission, request.user, "published_issue_author_accepted")
    messages.success(request, _("The fictional article has been assigned to this test issue."))
    return redirect("production_detail", pk=pk)
