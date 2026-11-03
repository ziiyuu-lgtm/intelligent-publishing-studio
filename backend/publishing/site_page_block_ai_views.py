"""Opt-in AI draft blocks, previewed and appended only after human review."""
import hashlib
import json
import os
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .ai_gateway import OPENAI, PUBLISHER_PAGE_BLOCKS, SITE_PAGE_BLOCKS, run_ai_task
from .ai_provider import ProviderError
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since, portal_requests_since
from .ai_views import _provider_configured
from .models import JournalSiteEvent
from .module_images import images_valid
from .page_block_ai_forms import ReviewedBlockFormSet, validate_reviewed_blocks
from .page_block_ai_provider import LEGACY_PROMPT_VERSION, MAX_BRIEF_LENGTH, MAX_OPTIONS, PROMPT_VERSION, validate_options
from .page_blocks import MAX_PAGE_BLOCKS
from .portal_views import _event as portal_event
from .production_views import _enabled, _no_index
from .site_page_ai_views import _current, _model, _page_for, _policy
from .site_page_views import _fingerprint, _managed, _page


def _active(policy, kind):
    if not policy:
        return False
    enabled = policy.site_page_blocks_enabled if kind == "journal" else policy.portal_page_blocks_enabled
    limit = policy.journal_daily_limit if kind == "journal" else policy.portal_daily_limit
    return bool(enabled and 1 <= limit <= 100)


def _snapshot(policy, kind):
    if not policy:
        return None
    return (policy.pk, policy.revision,
            policy.site_page_blocks_enabled if kind == "journal" else policy.portal_page_blocks_enabled,
            policy.journal_daily_limit if kind == "journal" else policy.portal_daily_limit)


def _run(site, user, run_id, kind, locked=False):
    qs = _model(kind).objects.select_for_update() if locked else _model(kind).objects
    return get_object_or_404(qs, pk=run_id, site=site, requested_by=user,
                             prompt_version__in=(LEGACY_PROMPT_VERSION, PROMPT_VERSION))


def _previous(run_id, site, user, page_slug, kind):
    previous = _model(kind).objects.filter(pk=run_id).first()
    if previous:
        if previous.site_id != site.pk or previous.requested_by_id != user.pk:
            raise Http404
        if previous.prompt_version not in (LEGACY_PROMPT_VERSION, PROMPT_VERSION) or previous.target_page_slug != page_slug:
            return False
    return previous


def _slots(page):
    return MAX_PAGE_BLOCKS - len(page.get("blocks", []))


def _options(run, page):
    if run.status != type(run).Status.COMPLETED:
        raise Http404
    try:
        return validate_options(run.options, _slots(page), legacy=run.prompt_version == LEGACY_PROMPT_VERSION)
    except ProviderError as exc:
        raise Http404 from exc


def _result_route(kind):
    return "journal_page_block_ai_result" if kind == "journal" else "publisher_page_block_ai_result"


def _request(request, slug, page_slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        run_id = uuid.UUID(request.POST.get("request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Invalid request identifier."), status=400)
    previous = _previous(run_id, site, request.user, page_slug, kind)
    if previous is False:
        return HttpResponse(_("This request identifier belongs to a different AI task or page."), status=409)
    if previous:
        return redirect(_result_route(kind), slug=slug, run_id=previous.pk)
    brief = request.POST.get("brief", "").strip()
    if not brief or len(brief) > MAX_BRIEF_LENGTH:
        return HttpResponse(_("Provide a fictional page brief of at most 800 characters."), status=400)
    try:
        requested_options = int(request.POST.get("requested_options", "3"))
    except ValueError:
        requested_options = 0
    if not 1 <= requested_options <= MAX_OPTIONS:
        return HttpResponse(_("Choose between 1 and 6 suggested designs."), status=400)
    policy = _policy(owner, kind)
    if not _provider_configured() or not _active(policy, kind):
        return HttpResponse(_("AI page-block suggestions are not enabled for this site and test environment."), status=409)
    if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
        return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        owner, site = _managed(request.user, slug, kind, locked=True)
        policy = _policy(owner, kind, locked=True)
        if not _provider_configured() or not _active(policy, kind):
            return HttpResponse(_("AI page-block suggestions are not enabled for this site and test environment."), status=409)
        if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
            return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
        previous = _previous(run_id, site, request.user, page_slug, kind)
        if previous is False:
            return HttpResponse(_("This request identifier belongs to a different AI task or page."), status=409)
        if previous:
            return redirect(_result_route(kind), slug=slug, run_id=previous.pk)
        if request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site, kind):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        page = _page_for(site, page_slug, kind)
        available_slots = _slots(page)
        if available_slots < 1:
            return HttpResponse(_("This page has reached the block limit. Remove a block before requesting suggestions."), status=409)
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        used = journal_requests_since(owner, since) if kind == "journal" else portal_requests_since(owner, since)
        limit = policy.journal_daily_limit if kind == "journal" else policy.portal_daily_limit
        if used >= limit:
            return HttpResponse(_("The site's local AI request limit has been reached."), status=429)
        run = _model(kind).objects.create(id=run_id, site=site, requested_by=request.user,
            model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION, policy_revision=policy.revision,
            site_revision=site.revision, site_fingerprint=_fingerprint(site, kind), target_page_slug=page_slug,
            brief_fingerprint=hashlib.sha256(brief.encode("utf-8")).hexdigest())
        snapshot = _snapshot(policy, kind)
        if kind == "journal":
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_ai_blocks_requested", revision=site.revision)
        else:
            portal_event(site, request.user, "portal_ai_blocks_requested")

    fresh_actor = get_user_model().objects.filter(pk=request.user.pk).first()
    try:
        if fresh_actor is None:
            raise Http404
        fresh_owner, fresh_site = _managed(fresh_actor, slug, kind)
        fresh_policy = _policy(fresh_owner, kind)
        allowed = (_provider_configured() and settings.IPS_OPENAI_MODEL == run.model_name and
                   _active(fresh_policy, kind) and _snapshot(fresh_policy, kind) == snapshot and
                   _current(fresh_site, run, kind))
    except Http404:
        allowed = False
    if not allowed:
        run.status, run.error_code, run.finished_at = _model(kind).Status.CANCELLED, "authorization_changed", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
        return redirect(_result_route(kind), slug=slug, run_id=run.pk)
    try:
        result = run_ai_task(task=SITE_PAGE_BLOCKS if kind == "journal" else PUBLISHER_PAGE_BLOCKS, provider=OPENAI,
            api_key=os.environ.get("OPENAI_API_KEY", ""), model=run.model_name,
            payload={"brief":brief, "available_slots":available_slots, "requested_options":requested_options})
        result = validate_options(result, available_slots, max_options=requested_options)
    except Exception:
        run.status, run.error_code, run.finished_at = _model(kind).Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.options, run.finished_at = _model(kind).Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "options", "finished_at"])
    return redirect(_result_route(kind), slug=slug, run_id=run.pk)


def _initial(option):
    return [{**block, "visible":True} for block in option["blocks"]]


def _result(request, slug, run_id, kind, edit_error=None, status=200):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    current = _current(site, run, kind)
    page, choices = None, []
    if run.status == _model(kind).Status.COMPLETED and current and run.applied_revision is None:
        page = _page_for(site, run.target_page_slug, kind)
        for i, option in enumerate(_options(run, page)):
            formset = edit_error[1] if edit_error and edit_error[0] == i else ReviewedBlockFormSet(
                prefix=f"choice_{i}", initial=_initial(option))
            choices.append({"index":i, "formset":formset})
    return _no_index(render(request, "publishing/page_block_ai_result.html", {
        "owner":owner, "site":site, "run":run, "page":page, "current":current, "choices":choices,
        "manage_route":"manage_journal_page_blocks" if kind == "journal" else "manage_publisher_page_blocks",
        "preview_route":"preview_journal_page_block_ai" if kind == "journal" else "preview_publisher_page_block_ai",
    }, status=status))


def _preview(request, slug, run_id, index, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    if request.POST.get("revision") != str(site.revision) or not _current(site, run, kind) or run.applied_revision is not None:
        return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
    page = _page_for(site, run.target_page_slug, kind)
    options = _options(run, page)
    if index >= len(options):
        raise Http404
    initial = _initial(options[index])
    prefix = f"choice_{index}"
    try:
        count = int(request.POST.get(f"{prefix}-INITIAL_FORMS", "-1"))
    except ValueError:
        count = -1
    if (not 1 <= count <= len(initial) or request.POST.get(f"{prefix}-INITIAL_FORMS") != str(count) or
            request.POST.get(f"{prefix}-TOTAL_FORMS") != str(count)):
        return HttpResponse(_("The suggestion form is out of date; reload the page."), status=409)
    formset = ReviewedBlockFormSet(request.POST, prefix=prefix, initial=initial)
    if not formset.is_valid():
        return _result(request, slug, run_id, kind, edit_error=(index, formset), status=400)
    try:
        blocks = validate_reviewed_blocks(formset.reviewed_blocks, _slots(page))
    except ValueError:
        return HttpResponse(_("The suggested blocks are invalid or exceed the page limit."), status=400)
    apply_route = "apply_journal_page_block_ai" if kind == "journal" else "apply_publisher_page_block_ai"
    edit_route = "edit_journal_page_block_ai" if kind == "journal" else "edit_publisher_page_block_ai"
    return _page(request, slug, run.target_page_slug, kind, True, block_suggestion=blocks,
        expected=(run.site_revision, run.site_fingerprint), block_apply_url=reverse(apply_route, args=[slug, run.pk, index]),
        block_edit_url=reverse(edit_route, args=[slug, run.pk, index]),
        block_reviewed=json.dumps(blocks, ensure_ascii=False, separators=(",", ":")), ai_revision=run.site_revision)


def _edit(request, slug, run_id, index, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    run = _run(site, request.user, run_id, kind)
    if request.POST.get("revision") != str(site.revision) or not _current(site, run, kind) or run.applied_revision is not None:
        return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
    page = _page_for(site, run.target_page_slug, kind)
    options = _options(run, page)
    if index >= len(options):
        raise Http404
    raw = request.POST.get("reviewed_blocks", "")
    if len(raw) > 220000:
        return HttpResponse(_("The suggested blocks are invalid or exceed the page limit."), status=400)
    try:
        blocks = validate_reviewed_blocks(json.loads(raw), _slots(page))
        if len(blocks) > len(options[index]["blocks"]):
            raise ValueError("too many reviewed blocks")
    except (ValueError, TypeError):
        return HttpResponse(_("The suggested blocks are invalid or exceed the page limit."), status=400)
    initial = [{**{key:block[key] for key in ("kind", "heading", "body", "visible")},
                **({"design":block["design"]} if "design" in block else {}),
                **({"grid_span":block["grid_span"]} if "grid_span" in block else {})} for block in blocks]
    formset = ReviewedBlockFormSet(prefix=f"choice_{index}", initial=initial)
    return _result(request, slug, run_id, kind, edit_error=(index, formset))


def _apply(request, slug, run_id, index, kind):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm applying this option to the draft."), status=400)
    raw = request.POST.get("reviewed_blocks", "")
    if len(raw) > 220000:
        return HttpResponse(_("The suggested blocks are invalid or exceed the page limit."), status=400)
    try:
        supplied = json.loads(raw)
    except (ValueError, TypeError):
        return HttpResponse(_("The suggested blocks are invalid or exceed the page limit."), status=400)
    with transaction.atomic():
        owner, site = _managed(request.user, slug, kind, locked=True)
        run = _run(site, request.user, run_id, kind, locked=True)
        if run.applied_revision is not None:
            return HttpResponse(_("This suggestion has already been applied."), status=409)
        if request.POST.get("revision") != str(site.revision) or not _current(site, run, kind):
            return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
        page = _page_for(site, run.target_page_slug, kind)
        options = _options(run, page)
        if index >= len(options):
            raise Http404
        try:
            blocks = validate_reviewed_blocks(supplied, _slots(page))
            if len(blocks) > len(options[index]["blocks"]):
                raise ValueError("too many reviewed blocks")
        except (ValueError, TypeError):
            return HttpResponse(_("The suggested blocks are invalid or exceed the page limit."), status=400)
        candidate = {**page, "blocks":[*page.get("blocks", []), *blocks]}
        pages = [candidate if entry["slug"] == run.target_page_slug else entry for entry in site.pages_config]
        if not images_valid(site, site.sections_config, owner=site, pages=pages):
            return HttpResponse(_("The site draft or its images are invalid."), status=409)
        site.pages_config = pages
        site.revision += 1
        site.save(update_fields=["pages_config", "revision"])
        run.applied_option, run.applied_revision, run.applied_option_content = index, site.revision, {"blocks":blocks}
        run.save(update_fields=["applied_option", "applied_revision", "applied_option_content"])
        if kind == "journal":
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_ai_blocks_applied", revision=site.revision)
        else:
            portal_event(site, request.user, "portal_ai_blocks_applied")
    messages.success(request, _("Suggested blocks added to the draft. Edit them before publishing."))
    return redirect("manage_journal_page_blocks" if kind == "journal" else "manage_publisher_page_blocks",
                    slug=slug, page_slug=run.target_page_slug)


@login_required
@never_cache
@require_POST
def request_journal_page_block_ai(request, slug, page_slug):
    return _request(request, slug, page_slug, "journal")


@login_required
@never_cache
@require_GET
def journal_page_block_ai_result(request, slug, run_id):
    return _result(request, slug, run_id, "journal")


@login_required
@never_cache
@require_POST
def preview_journal_page_block_ai(request, slug, run_id, index):
    return _preview(request, slug, run_id, index, "journal")


@login_required
@never_cache
@require_POST
def edit_journal_page_block_ai(request, slug, run_id, index):
    return _edit(request, slug, run_id, index, "journal")


@login_required
@never_cache
@require_POST
def apply_journal_page_block_ai(request, slug, run_id, index):
    return _apply(request, slug, run_id, index, "journal")


@login_required
@never_cache
@require_POST
def request_publisher_page_block_ai(request, slug, page_slug):
    return _request(request, slug, page_slug, "portal")


@login_required
@never_cache
@require_GET
def publisher_page_block_ai_result(request, slug, run_id):
    return _result(request, slug, run_id, "portal")


@login_required
@never_cache
@require_POST
def preview_publisher_page_block_ai(request, slug, run_id, index):
    return _preview(request, slug, run_id, index, "portal")


@login_required
@never_cache
@require_POST
def edit_publisher_page_block_ai(request, slug, run_id, index):
    return _edit(request, slug, run_id, index, "portal")


@login_required
@never_cache
@require_POST
def apply_publisher_page_block_ai(request, slug, run_id, index):
    return _apply(request, slug, run_id, index, "portal")
