"""Opt-in, fictional local production and publication workflow.

There is deliberately no DOI, notification, external submission or deployment.
"""
import hashlib
import json

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .forms import PublicationMetadataForm, PublicationProofForm
from .models import (EditorialAssignment, EditorialDecision, Journal, Membership, ProductionAssignment,
                     PublicationApproval, PublicationDraft, PublicationProof, ReviewRound, Submission)
from .views import _author_can_view, _can_coordinate, _download_version, _editor_can_view, _event, _has_role, _submission


def _enabled():
    if not settings.DEBUG or not settings.IPS_LOCAL_PUBLICATION_ENABLED:
        raise Http404


def _source_valid(submission, decision):
    latest_version = submission.versions.order_by("-number").first()
    latest_round = submission.rounds.select_related("version").order_by("-number").first()
    return bool(submission.status == Submission.Status.ACCEPTED and decision and latest_version and latest_round and
                decision.submission_id == submission.pk and decision.outcome == EditorialDecision.Outcome.ACCEPT and
                decision.round_id == latest_round.pk and latest_round.submission_id == submission.pk and
                latest_round.status == ReviewRound.Status.CLOSED and latest_round.version_id == latest_version.pk and
                latest_version.submission_id == submission.pk)


def _draft(pk, locked=False):
    qs = PublicationDraft.objects.select_related("submission__journal__organisation", "decision__round__version", "published_proof")
    if locked:
        qs = qs.select_for_update()
    return get_object_or_404(qs, pk=pk)


def _producer_can_manage(user, draft):
    submission = draft.submission
    return (submission.status == Submission.Status.ACCEPTED and submission.author_id != user.pk
            and _has_role(user, submission.journal, Membership.Role.PRODUCER)
            and draft.production_assignments.filter(producer=user, status=ProductionAssignment.Status.ACTIVE).exists()
            and not draft.production_assignments.filter(producer=user, status=ProductionAssignment.Status.RECUSED).exists()
            and not submission.reviews.filter(reviewer=user).exists()
            and not submission.editor_assignments.filter(editor=user, status=EditorialAssignment.Status.RECUSED).exists())


def _permitted(user, draft):
    return _author_can_view(user, draft.submission) or _producer_can_manage(user, draft)


def _latest_proof(draft):
    return draft.proofs.order_by("-pk").first()


def _fingerprint(draft, proof):
    payload = {"draft": str(draft.pk), "revision": draft.revision, "title": draft.title,
               "authors": draft.authors, "abstract": draft.abstract, "proof": proof.pk, "sha256": proof.sha256}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _proof_intact(proof):
    if not proof or not proof.file:
        return False
    digest = hashlib.sha256()
    try:
        with proof.file.open("rb") as stream:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    break
                digest.update(chunk)
    except (OSError, ValueError):
        return False
    return digest.hexdigest() == proof.sha256


def _context(request, draft, metadata_form=None, proof_form=None):
    proof = _latest_proof(draft)
    fingerprint = _fingerprint(draft, proof) if proof else ""
    approval = (PublicationApproval.objects.filter(draft=draft, proof=proof, revision=draft.revision,
                fingerprint=fingerprint, approved_by=draft.submission.author).first() if proof else None)
    producer_view = _producer_can_manage(request.user, draft)
    assignment = (draft.production_assignments.filter(producer=request.user, status=ProductionAssignment.Status.ACTIVE).first()
                  if producer_view else None)
    return {"draft": draft, "submission": draft.submission, "proof": proof, "fingerprint": fingerprint,
            "approval": approval, "author_view": _author_can_view(request.user, draft.submission),
            "producer_view": producer_view, "my_production_assignment": assignment,
            "can_view_submission": (_author_can_view(request.user, draft.submission) or _editor_can_view(request.user, draft.submission)),
            "metadata_form": metadata_form or PublicationMetadataForm(instance=draft),
            "proof_form": proof_form or PublicationProofForm()}


def _stale(request, draft):
    return request.POST.get("revision") != str(draft.revision)


@login_required
@never_cache
@require_POST
def start_production(request, pk):
    _enabled()
    if request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Confirm that the materials are fictional."), status=400)
    with transaction.atomic():
        submission = get_object_or_404(Submission.objects.select_for_update().select_related("journal__organisation"), pk=pk)
        if not _editor_can_view(request.user, submission):
            raise Http404
        decision = submission.decisions.select_related("round__version").order_by("-round__number").first()
        if not _source_valid(submission, decision):
            return HttpResponse(_("A valid acceptance for the latest version is required."), status=409)
        draft, created = PublicationDraft.objects.get_or_create(submission=submission, defaults={"decision": decision,
            "title": submission.title, "abstract": submission.abstract, "created_by": request.user})
        if created:
            _event(submission, request.user, "production_started")
    messages.success(request, _("Production started. A coordinator must assign a production editor.") if created
                     else _("Production has already started."))
    return redirect("submission_detail", pk=submission.pk)


@login_required
@never_cache
@require_http_methods(["GET", "POST"])
def production_team(request, pk):
    _enabled()
    submission = _submission(pk)
    if not _can_coordinate(request.user, submission):
        raise Http404
    draft = get_object_or_404(PublicationDraft.objects.select_related("decision__round__version"), submission=submission)
    if not _source_valid(submission, draft.decision):
        return HttpResponse(_("A valid acceptance for the latest version is required."), status=409)
    if request.method == "POST":
        try:
            producer_id = int(request.POST.get("producer", ""))
            if not 1 <= producer_id <= 9223372036854775807:
                raise ValueError
        except (TypeError, ValueError):
            return HttpResponse(_("Choose an eligible production editor."), status=400)
        with transaction.atomic():
            draft = _draft(draft.pk, locked=True)
            submission = draft.submission
            if not _can_coordinate(request.user, submission):
                raise Http404
            if draft.published_at or not _source_valid(submission, draft.decision):
                return HttpResponse(_("Production is closed or unavailable."), status=409)
            producer = get_user_model().objects.filter(pk=producer_id, is_active=True).first()
            if (not producer or producer.pk in (submission.author_id, request.user.pk) or
                    not _has_role(producer, submission.journal, Membership.Role.PRODUCER) or
                    submission.reviews.filter(reviewer=producer).exists() or
                    submission.editor_assignments.filter(editor=producer, status=EditorialAssignment.Status.RECUSED).exists() or
                    draft.production_assignments.filter(producer=producer, status=ProductionAssignment.Status.RECUSED).exists()):
                return HttpResponse(_("Choose an eligible production editor."), status=400)
            assignment, created = ProductionAssignment.objects.get_or_create(draft=draft, producer=producer,
                status=ProductionAssignment.Status.ACTIVE, defaults={"assigned_by": request.user})
            if created:
                _event(submission, request.user, "production_staff_assigned")
        messages.success(request, _("Production assignment recorded.") if created else _("This production editor is already assigned."))
        return redirect("production_team", pk=pk)
    excluded = [submission.author_id, request.user.pk]
    excluded += list(submission.reviews.values_list("reviewer_id", flat=True))
    excluded += list(submission.editor_assignments.filter(status=EditorialAssignment.Status.RECUSED).values_list("editor_id", flat=True))
    excluded += list(draft.production_assignments.filter(status__in=[ProductionAssignment.Status.ACTIVE,
                          ProductionAssignment.Status.RECUSED]).values_list("producer_id", flat=True))
    candidates = get_user_model().objects.filter(is_active=True, journal_memberships__journal=submission.journal,
        journal_memberships__role=Membership.Role.PRODUCER, journal_memberships__active=True).exclude(pk__in=excluded).distinct().order_by("username")
    return render(request, "publishing/production_team.html", {"submission": submission, "production_draft": draft,
        "candidates": candidates, "assignments": draft.production_assignments.select_related("producer", "assigned_by", "ended_by")})


@login_required
@never_cache
@require_POST
def remove_production_assignment(request, pk):
    _enabled()
    with transaction.atomic():
        assignment = get_object_or_404(ProductionAssignment.objects.select_for_update().select_related(
            "draft__submission__journal__organisation"), pk=pk)
        submission = assignment.draft.submission
        if not _can_coordinate(request.user, submission):
            raise Http404
        if assignment.status != ProductionAssignment.Status.ACTIVE:
            return HttpResponse(_("This assignment is no longer active."), status=409)
        assignment.status, assignment.ended_at, assignment.ended_by = ProductionAssignment.Status.REVOKED, timezone.now(), request.user
        assignment.save(update_fields=["status", "ended_at", "ended_by"])
        _event(submission, request.user, "production_assignment_removed")
    return redirect("production_team", pk=submission.pk)


@login_required
@never_cache
@require_POST
def recuse_producer(request, pk):
    _enabled()
    with transaction.atomic():
        assignment = get_object_or_404(ProductionAssignment.objects.select_for_update().select_related(
            "draft__submission__journal__organisation"), pk=pk, producer=request.user)
        draft = assignment.draft
        if assignment.status != ProductionAssignment.Status.ACTIVE or not _producer_can_manage(request.user, draft):
            raise Http404
        if request.POST.get("confirm") != "yes":
            return HttpResponse(_("Confirm your recusal."), status=400)
        assignment.status, assignment.ended_at, assignment.ended_by = ProductionAssignment.Status.RECUSED, timezone.now(), request.user
        assignment.save(update_fields=["status", "ended_at", "ended_by"])
        _event(draft.submission, request.user, "production_staff_recused")
    messages.success(request, _("Your recusal was recorded and production access ended."))
    return redirect("workspace")


@login_required
@never_cache
@require_GET
def production_detail(request, pk):
    _enabled()
    draft = _draft(pk)
    if not _permitted(request.user, draft):
        raise Http404
    return render(request, "publishing/production_detail.html", _context(request, draft))


@login_required
@never_cache
@require_GET
def accepted_production_source(request, pk):
    _enabled()
    draft = _draft(pk)
    if not _producer_can_manage(request.user, draft) or not _source_valid(draft.submission, draft.decision):
        raise Http404
    return _download_version(request, draft.submission, draft.decision.round.version)


@login_required
@never_cache
@require_POST
def edit_production_metadata(request, pk):
    _enabled()
    draft = _draft(pk)
    if not _producer_can_manage(request.user, draft):
        raise Http404
    form = PublicationMetadataForm(request.POST, instance=draft)
    if not form.is_valid():
        return render(request, "publishing/production_detail.html", _context(request, _draft(pk), metadata_form=form), status=400)
    with transaction.atomic():
        draft = _draft(pk, locked=True)
        if not _producer_can_manage(request.user, draft):
            raise Http404
        if draft.published_at or _stale(request, draft) or not _source_valid(draft.submission, draft.decision):
            return HttpResponse(_("Production changed or is closed; reload the page."), status=409)
        changed = any(getattr(draft, k) != form.cleaned_data[k] for k in ("title", "authors", "abstract"))
        if changed:
            for key in ("title", "authors", "abstract"):
                setattr(draft, key, form.cleaned_data[key])
            draft.revision += 1
            draft.save(update_fields=["title", "authors", "abstract", "revision"])
            _event(draft.submission, request.user, "production_metadata_updated")
    return redirect("production_detail", pk=pk)


@login_required
@never_cache
@require_POST
def upload_publication_proof(request, pk):
    _enabled()
    draft = _draft(pk)
    if not _producer_can_manage(request.user, draft):
        raise Http404
    form = PublicationProofForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(request, "publishing/production_detail.html", _context(request, draft, proof_form=form), status=400)
    document = form.cleaned_data["file"]
    digest = hashlib.sha256()
    for chunk in document.chunks():
        digest.update(chunk)
    document.seek(0)
    proof = None
    try:
        with transaction.atomic():
            draft = _draft(pk, locked=True)
            if not _producer_can_manage(request.user, draft):
                raise Http404
            if draft.published_at or _stale(request, draft) or not _source_valid(draft.submission, draft.decision):
                return HttpResponse(_("Production changed or is closed; reload the page."), status=409)
            proof = PublicationProof(draft=draft, file=document, sha256=digest.hexdigest(), uploaded_by=request.user)
            proof.save()
            draft.revision += 1
            draft.save(update_fields=["revision"])
            _event(draft.submission, request.user, "publication_proof_uploaded")
    except Exception:
        if proof and proof.file.name and proof.file._committed:
            proof.file.storage.delete(proof.file.name)
        raise
    return redirect("production_detail", pk=pk)


@login_required
@never_cache
@require_GET
def private_publication_proof(request, pk, proof_id):
    _enabled()
    draft = _draft(pk)
    if not _permitted(request.user, draft):
        raise Http404
    proof = get_object_or_404(PublicationProof, pk=proof_id, draft=draft)
    if not _producer_can_manage(request.user, draft) and proof.pk != getattr(_latest_proof(draft), "pk", None):
        raise Http404
    if not _proof_intact(proof):
        raise Http404
    try:
        stream = proof.file.open("rb")
    except (OSError, ValueError):
        raise Http404
    response = FileResponse(stream, as_attachment=True, filename="fictional-proof.pdf", content_type="application/octet-stream")
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


@login_required
@never_cache
@require_POST
def approve_publication_proof(request, pk):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        draft = _draft(pk, locked=True)
        if not _author_can_view(request.user, draft.submission):
            raise Http404
        proof = _latest_proof(draft)
        if (draft.published_at or _stale(request, draft) or not proof or request.POST.get("proof") != str(proof.pk) or
                not _source_valid(draft.submission, draft.decision) or not draft.title.strip() or not draft.authors.strip() or not draft.abstract.strip()):
            return HttpResponse(_("Production changed or is incomplete; reload the page."), status=409)
        fingerprint = _fingerprint(draft, proof)
        if request.POST.get("fingerprint") != fingerprint or not _proof_intact(proof):
            return HttpResponse(_("The proof or metadata changed; review it again."), status=409)
        approval, created = PublicationApproval.objects.get_or_create(draft=draft, proof=proof,
            revision=draft.revision, approved_by=request.user, defaults={"fingerprint": fingerprint})
        if not created and approval.fingerprint != fingerprint:
            return HttpResponse(_("The proof or metadata changed; review it again."), status=409)
        if created:
            _event(draft.submission, request.user, "publication_author_approved")
    return redirect("production_detail", pk=pk)


@login_required
@never_cache
@require_POST
def publish_test_article(request, pk):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        draft = _draft(pk, locked=True)
        if not _producer_can_manage(request.user, draft):
            raise Http404
        proof = _latest_proof(draft)
        if (draft.published_at or _stale(request, draft) or not proof or request.POST.get("proof") != str(proof.pk) or
                not _source_valid(draft.submission, draft.decision) or not draft.title.strip() or not draft.authors.strip() or not draft.abstract.strip()):
            return HttpResponse(_("Production changed or is incomplete; reload the page."), status=409)
        fingerprint = _fingerprint(draft, proof)
        if (request.POST.get("fingerprint") != fingerprint or not _proof_intact(proof) or
                not PublicationApproval.objects.filter(draft=draft, proof=proof, revision=draft.revision,
                    fingerprint=fingerprint, approved_by=draft.submission.author).exists()):
            return HttpResponse(_("The current proof needs a valid author confirmation."), status=409)
        draft.published_at, draft.published_by, draft.published_proof = timezone.now(), request.user, proof
        draft.save(update_fields=["published_at", "published_by", "published_proof"])
        _event(draft.submission, request.user, "test_article_published")
    messages.success(request, _("Fictional article published in the local test environment."))
    return redirect("production_detail", pk=pk)


def _public_article(slug, pk):
    draft = get_object_or_404(PublicationDraft.objects.select_related("submission__journal__organisation", "published_proof"),
        pk=pk, submission__journal__slug=slug, submission__journal__active=True,
        submission__journal__organisation__active=True, published_at__isnull=False, published_proof__isnull=False)
    if draft.published_proof.draft_id != draft.pk:
        raise Http404
    return draft


def _no_index(response):
    response["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response["Cache-Control"] = "no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_GET
def journal_test_articles(request, slug):
    _enabled()
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    articles = PublicationDraft.objects.filter(submission__journal=journal, published_at__isnull=False,
                                               published_proof__isnull=False, published_proof__draft_id=F("pk")).order_by("-published_at")
    return _no_index(render(request, "publishing/journal_test_articles.html", {"journal": journal, "articles": articles}))


@require_GET
def test_article(request, slug, pk):
    _enabled()
    draft = _public_article(slug, pk)
    return _no_index(render(request, "publishing/test_article.html", {"draft": draft, "journal": draft.submission.journal}))


@require_GET
def test_article_pdf(request, slug, pk):
    _enabled()
    draft = _public_article(slug, pk)
    if not _proof_intact(draft.published_proof):
        raise Http404
    try:
        stream = draft.published_proof.file.open("rb")
    except (OSError, ValueError):
        raise Http404
    return _no_index(FileResponse(stream, as_attachment=True,
                     filename="fictional-article.pdf", content_type="application/octet-stream"))
