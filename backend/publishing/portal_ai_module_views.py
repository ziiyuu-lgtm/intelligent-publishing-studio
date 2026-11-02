"""Opt-in AI module arrangements for a fictional publisher portal."""
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
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .ai_gateway import OPENAI, PUBLISHER_MODULES, run_ai_task
from .ai_provider import ProviderError
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, portal_requests_since
from .ai_views import _provider_configured
from .models import OrganisationPortalAIDesignRun
from .portal_ai_modules import MAX_BRIEF_LENGTH, PROMPT_VERSION, describe_sections, materialize_option, validate_options
from .portal_ai_views import _current, _run
from .portal_policy_views import _policy
from .portal_sections import SECTION_CHOICES, InvalidSections, default_sections, validate_sections
from .portal_views import CONTENT_FIELDS, _draft_journals, _event, _fingerprint, _organisation, _render, _site
from .production_views import _enabled, _no_index
from .site_design import InvalidDesign, validate_blueprint


def _active(policy):
    return bool(policy and policy.portal_modules_enabled and 1 <= policy.portal_daily_limit <= 100)


def _snapshot(policy):
    return (policy.pk, policy.revision, policy.portal_modules_enabled, policy.portal_daily_limit) if policy else None


def _sections(site):
    try:
        validate_blueprint(site.design_config)
        if site.sections_config:
            validate_sections(site.sections_config)
            return [dict(item) for item in site.sections_config]
        return default_sections(site)
    except (InvalidDesign, InvalidSections) as exc:
        raise Http404 from exc


def _module_run(organisation, user, run_id, locked=False):
    run = _run(organisation, user, run_id, locked=locked)
    if run.prompt_version != PROMPT_VERSION:
        raise Http404
    return run


def _options(site, run):
    if run.status != OrganisationPortalAIDesignRun.Status.COMPLETED:
        raise Http404
    try:
        return validate_options(run.options, describe_sections(_sections(site)))
    except (ProviderError, InvalidSections) as exc:
        raise Http404 from exc


@login_required
@never_cache
@require_POST
def request_modules(request, slug):
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
        if previous.prompt_version != PROMPT_VERSION:
            return HttpResponse(_("This request identifier belongs to a different AI task."), status=409)
        return redirect("publisher_ai_module_result", slug=slug, run_id=previous.pk)
    brief = request.POST.get("brief", "").strip()
    if not brief or len(brief) > MAX_BRIEF_LENGTH:
        return HttpResponse(_("Provide a fictional design brief of at most 600 characters."), status=400)
    policy = _policy(organisation)
    if not _provider_configured() or not _active(policy):
        return HttpResponse(_("AI module arrangement is not enabled for this publisher and test environment."), status=409)
    if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
        return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        policy = _policy(organisation, locked=True)
        if not _provider_configured() or not _active(policy):
            return HttpResponse(_("AI module arrangement is not enabled for this publisher and test environment."), status=409)
        if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
            return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
        previous = OrganisationPortalAIDesignRun.objects.filter(pk=run_id).first()
        if previous:
            if previous.site_id != site.pk or previous.requested_by_id != request.user.pk:
                raise Http404
            if previous.prompt_version != PROMPT_VERSION:
                return HttpResponse(_("This request identifier belongs to a different AI task."), status=409)
            return redirect("publisher_ai_module_result", slug=slug, run_id=previous.pk)
        if request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        modules = describe_sections(_sections(site))
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        if portal_requests_since(organisation, since) >= policy.portal_daily_limit:
            return HttpResponse(_("The publisher portal's local AI request limit has been reached."), status=429)
        run = OrganisationPortalAIDesignRun.objects.create(id=run_id, site=site, requested_by=request.user,
            model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION, policy_revision=policy.revision,
            site_revision=site.revision, site_fingerprint=_fingerprint(site),
            brief_fingerprint=hashlib.sha256(brief.encode("utf-8")).hexdigest())
        snapshot = _snapshot(policy)
        _event(site, request.user, "portal_ai_modules_requested")

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
        return redirect("publisher_ai_module_result", slug=slug, run_id=run.pk)
    try:
        result = run_ai_task(task=PUBLISHER_MODULES, provider=OPENAI, api_key=os.environ.get("OPENAI_API_KEY", ""),
                             model=run.model_name, payload={"brief":brief, "modules":modules})
        result = validate_options(result, modules)
    except Exception:
        run.status, run.error_code, run.finished_at = OrganisationPortalAIDesignRun.Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.options, run.finished_at = OrganisationPortalAIDesignRun.Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "options", "finished_at"])
    return redirect("publisher_ai_module_result", slug=slug, run_id=run.pk)


@login_required
@never_cache
@require_GET
def module_result(request, slug, run_id):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    run = _module_run(organisation, request.user, run_id)
    current = _current(site, run)
    choices = []
    if run.status == OrganisationPortalAIDesignRun.Status.COMPLETED and current and run.applied_revision is None:
        source, labels = _sections(site), dict(SECTION_CHOICES)
        for index, option in enumerate(_options(site, run)):
            items = []
            for item in option["modules"]:
                new = item["id"] == "new"
                label = labels[item["kind"]] if new else (source[int(item["id"][1:])]["heading"] or labels[item["kind"]])
                items.append({"label":label, "visible":item["visible"], "is_new":new})
            choices.append({"index":index, "items":items})
    return _no_index(render(request, "publishing/publisher_ai_module_result.html",
        {"organisation":organisation, "site":site, "run":run, "current":current, "choices":choices}))


@login_required
@never_cache
@require_GET
def preview_modules(request, slug, run_id, index):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    run = _module_run(organisation, request.user, run_id)
    if not _current(site, run):
        return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
    options = _options(site, run)
    if index >= len(options):
        raise Http404
    design = SimpleNamespace(**{field:getattr(site, field) for field in CONTENT_FIELDS},
        design_config=site.design_config, sections_config=materialize_option(options[index], _sections(site)))
    return _render(request, organisation, design, _draft_journals(site), preview=True, ai_proposal_preview=True)


@login_required
@never_cache
@require_POST
def apply_modules(request, slug, run_id, index):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm applying this option to the draft."), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        run = _module_run(organisation, request.user, run_id, locked=True)
        if run.applied_revision is not None:
            return HttpResponse(_("This suggestion has already been applied."), status=409)
        if request.POST.get("revision") != str(site.revision) or not _current(site, run):
            return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
        options = _options(site, run)
        if index >= len(options):
            raise Http404
        sections = materialize_option(options[index], _sections(site))
        if site.sections_config != sections:
            site.sections_config = sections
            site.revision += 1
            site.save(update_fields=["sections_config", "revision"])
        run.applied_option, run.applied_revision = index, site.revision
        run.save(update_fields=["applied_option", "applied_revision"])
        _event(site, request.user, "portal_ai_modules_applied")
    messages.success(request, _("Module suggestion applied to the draft. Review new empty modules before publishing."))
    return redirect("manage_publisher_portal", slug=slug)
