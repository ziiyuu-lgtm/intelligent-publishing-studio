"""Reviewed AI appearance suggestions for a fictional publisher portal draft."""
import hashlib
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .ai_gateway import OPENAI, PUBLISHER_DESIGN, run_ai_task
from .ai_provider import ProviderError
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, portal_requests_since
from .ai_views import _provider_configured
from .models import OrganisationPortalAIDesignRun
from .portal_policy_views import _policy
from .portal_views import CONTENT_FIELDS, _draft_journals, _event, _fingerprint, _organisation, _render, _site
from .production_views import _enabled, _no_index
from .site_ai_provider import MAX_BRIEF_LENGTH, PUBLISHER_PROMPT_VERSION, validate_options
from .page_ai_provider import PROMPT_VERSION as PAGE_PROMPT_VERSION
from .page_block_ai_provider import LEGACY_PROMPT_VERSION as LEGACY_BLOCK_PROMPT_VERSION, PROMPT_VERSION as BLOCK_PROMPT_VERSION
from .site_design import COMPOSITIONS, FONTS, InvalidDesign, appearance_for, validate_blueprint


def _active(policy):
    return bool(policy and policy.portal_design_enabled and 1 <= policy.portal_daily_limit <= 100)


def _snapshot(policy):
    return (policy.pk, policy.revision, policy.portal_design_enabled, policy.portal_daily_limit) if policy else None


def _run(organisation, user, run_id, locked=False):
    qs = OrganisationPortalAIDesignRun.objects.select_for_update() if locked else OrganisationPortalAIDesignRun.objects
    return get_object_or_404(qs, pk=run_id, site__organisation=organisation, requested_by=user)


def _options(run):
    if run.status != OrganisationPortalAIDesignRun.Status.COMPLETED or run.prompt_version != PUBLISHER_PROMPT_VERSION:
        raise Http404
    try:
        return validate_options(run.options)
    except ProviderError as exc:
        raise Http404 from exc


def _current(site, run):
    return site.revision == run.site_revision and _fingerprint(site) == run.site_fingerprint


@login_required
@never_cache
@require_POST
def request_design(request, slug):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        run_id = uuid.UUID(request.POST.get("request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Invalid request identifier."), status=400)
    previous = OrganisationPortalAIDesignRun.objects.filter(pk=run_id).first()
    if previous:
        if previous.site_id != site.pk or previous.requested_by_id != request.user.pk:
            raise Http404
        if previous.prompt_version != PUBLISHER_PROMPT_VERSION:
            return HttpResponse(_("This request identifier belongs to a different AI task."), status=409)
        return redirect("publisher_ai_result", slug=slug, run_id=previous.pk)
    brief = request.POST.get("brief", "").strip()
    if not brief or len(brief) > MAX_BRIEF_LENGTH:
        return HttpResponse(_("Provide a fictional design brief of at most 600 characters."), status=400)
    policy = _policy(organisation)
    if not _provider_configured() or not _active(policy):
        return HttpResponse(_("AI portal design is not enabled for this publisher and test environment."), status=409)
    if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
        return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        policy = _policy(organisation, locked=True)
        if not _provider_configured() or not _active(policy):
            return HttpResponse(_("AI portal design is not enabled for this publisher and test environment."), status=409)
        if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
            return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
        previous = OrganisationPortalAIDesignRun.objects.filter(pk=run_id).first()
        if previous:
            if previous.site_id != site.pk or previous.requested_by_id != request.user.pk:
                raise Http404
            if previous.prompt_version != PUBLISHER_PROMPT_VERSION:
                return HttpResponse(_("This request identifier belongs to a different AI task."), status=409)
            return redirect("publisher_ai_result", slug=slug, run_id=previous.pk)
        if request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        try:
            appearance = validate_blueprint(site.design_config)
        except InvalidDesign:
            return HttpResponse(_("The site design is invalid."), status=409)
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        if portal_requests_since(organisation, since) >= policy.portal_daily_limit:
            return HttpResponse(_("The publisher portal's local AI request limit has been reached."), status=429)
        run = OrganisationPortalAIDesignRun.objects.create(id=run_id, site=site, requested_by=request.user,
            model_name=settings.IPS_OPENAI_MODEL, prompt_version=PUBLISHER_PROMPT_VERSION, policy_revision=policy.revision,
            site_revision=site.revision, site_fingerprint=_fingerprint(site),
            brief_fingerprint=hashlib.sha256(brief.encode("utf-8")).hexdigest())
        snapshot = _snapshot(policy)
        _event(site, request.user, "portal_ai_requested")

    # The brief exists only in this request, not in database records.
    fresh_actor = get_user_model().objects.get(pk=request.user.pk)
    try:
        fresh_org = _organisation(fresh_actor, slug)
        fresh_site = _site(fresh_org)
        fresh_policy = _policy(fresh_org)
        allowed = (_provider_configured() and settings.IPS_OPENAI_MODEL == run.model_name and
                   _active(fresh_policy) and _snapshot(fresh_policy) == snapshot and _current(fresh_site, run))
    except Http404:
        allowed = False
    if not allowed:
        run.status, run.error_code, run.finished_at = OrganisationPortalAIDesignRun.Status.CANCELLED, "authorization_changed", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
        return redirect("publisher_ai_result", slug=slug, run_id=run.pk)
    try:
        result = run_ai_task(task=PUBLISHER_DESIGN, provider=OPENAI, api_key=os.environ.get("OPENAI_API_KEY", ""),
                             model=run.model_name, payload={"brief":brief, "appearance":appearance})
        result = validate_options(result)
    except Exception:
        run.status, run.error_code, run.finished_at = OrganisationPortalAIDesignRun.Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.options, run.finished_at = OrganisationPortalAIDesignRun.Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "options", "finished_at"])
    return redirect("publisher_ai_result", slug=slug, run_id=run.pk)


@login_required
@never_cache
@require_GET
def result(request, slug, run_id):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    run = _run(organisation, request.user, run_id)
    if run.prompt_version == PAGE_PROMPT_VERSION:
        return redirect("publisher_page_ai_result", slug=slug, run_id=run.pk)
    if run.prompt_version in (LEGACY_BLOCK_PROMPT_VERSION, BLOCK_PROMPT_VERSION):
        return redirect("publisher_page_block_ai_result", slug=slug, run_id=run.pk)
    if run.prompt_version != PUBLISHER_PROMPT_VERSION:
        raise Http404
    choices = []
    if run.status == OrganisationPortalAIDesignRun.Status.COMPLETED:
        for index, option in enumerate(_options(run)):
            choices.append({"index":index, "appearance":appearance_for(option),
                "composition_label":dict(COMPOSITIONS)[option["composition"]], "font_label":dict(FONTS)[option["font"]]})
    return _no_index(render(request, "publishing/publisher_ai_result.html", {
        "organisation":organisation, "site":site, "run":run, "choices":choices, "current":_current(site, run)}))


@login_required
@never_cache
@require_GET
def preview_option(request, slug, run_id, index):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    run = _run(organisation, request.user, run_id)
    if not _current(site, run):
        return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
    options = _options(run)
    if index >= len(options):
        raise Http404
    design = SimpleNamespace(**{field:getattr(site, field) for field in CONTENT_FIELDS},
                             design_config=options[index], sections_config=site.sections_config, pages_config=site.pages_config,
                             hero_asset_id=site.hero_asset_id, hero_alt=site.hero_alt,
                             hero_fit=site.hero_fit, hero_position=site.hero_position)
    return _render(request, organisation, design, _draft_journals(site), preview=True, ai_proposal_preview=True)


@login_required
@never_cache
@require_POST
def apply_option(request, slug, run_id, index):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm applying this option to the draft."), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        run = _run(organisation, request.user, run_id, locked=True)
        options = _options(run)
        if index >= len(options):
            raise Http404
        if run.applied_revision is not None:
            return HttpResponse(_("This suggestion has already been applied."), status=409)
        if request.POST.get("revision") != str(site.revision) or not _current(site, run):
            return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
        if site.design_config != options[index]:
            site.design_config = options[index]
            site.revision += 1
            site.save(update_fields=["design_config", "revision"])
        run.applied_option, run.applied_revision = index, site.revision
        run.save(update_fields=["applied_option", "applied_revision"])
        _event(site, request.user, "portal_ai_option_applied")
    messages.success(request, _("Suggestion applied to the draft. Publish separately after review."))
    return redirect("manage_publisher_portal", slug=slug)
