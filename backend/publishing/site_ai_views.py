"""Private, opt-in AI suggestions for a fictional journal site draft."""
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

from .ai_gateway import OPENAI, SITE_DESIGN, run_ai_task
from .ai_provider import ProviderError
from .ai_usage import DAILY_REQUEST_LIMIT, account_requests_since, journal_requests_since
from .ai_views import _policy, _provider_configured
from .models import JournalSiteAIDesignRun, JournalSiteEvent
from .production_views import _enabled, _no_index
from .site_ai_provider import LEGACY_PROMPT_VERSION, MAX_BRIEF_LENGTH, PROMPT_VERSION, validate_options
from .site_ai_modules import PROMPT_VERSION as MODULE_PROMPT_VERSION
from .site_design import COMPOSITIONS, FONTS, InvalidDesign, appearance_for, legacy_to_blueprint, validate_blueprint
from .site_views import SITE_FIELDS, _fingerprint, _managed_journal, _render_site, _site


def _active(policy):
    return bool(policy and policy.site_design_enabled and 1 <= policy.journal_daily_limit <= 100)


def _snapshot(policy):
    return (policy.pk, policy.revision, policy.site_design_enabled, policy.journal_daily_limit) if policy else None


def _run(journal, user, run_id, locked=False):
    runs = JournalSiteAIDesignRun.objects.select_for_update() if locked else JournalSiteAIDesignRun.objects
    return get_object_or_404(runs, pk=run_id, site__journal=journal, requested_by=user)


def _options(run):
    if run.status != JournalSiteAIDesignRun.Status.COMPLETED:
        raise Http404
    if run.prompt_version not in (LEGACY_PROMPT_VERSION, PROMPT_VERSION):
        raise Http404
    try:
        return validate_options(run.options, legacy=run.prompt_version == LEGACY_PROMPT_VERSION)
    except ProviderError as exc:
        raise Http404 from exc


def _current(site, run):
    return site.revision == run.site_revision and _fingerprint(site) == run.site_fingerprint


@login_required
@never_cache
@require_POST
def request_site_ai_design(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    try:
        run_id = uuid.UUID(request.POST.get("request_id", ""))
    except (ValueError, TypeError, AttributeError):
        return HttpResponse(_("Invalid request identifier."), status=400)
    previous = JournalSiteAIDesignRun.objects.filter(pk=run_id).first()
    if previous:
        if previous.site_id != site.pk or previous.requested_by_id != request.user.pk:
            raise Http404
        if previous.prompt_version not in (LEGACY_PROMPT_VERSION, PROMPT_VERSION):
            return HttpResponse(_("This request identifier belongs to a different AI task."), status=409)
        return redirect("site_ai_result", slug=slug, run_id=previous.pk)
    brief = request.POST.get("brief", "").strip()
    if not brief or len(brief) > MAX_BRIEF_LENGTH:
        return HttpResponse(_("Provide a fictional design brief of at most 600 characters."), status=400)
    policy = _policy(journal)
    if not _provider_configured() or not _active(policy):
        return HttpResponse(_("AI site design is not enabled for this journal and test environment."), status=409)
    if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
        return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
    with transaction.atomic():
        get_user_model().objects.select_for_update().get(pk=request.user.pk)
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        policy = _policy(journal, locked=True)
        if not _provider_configured() or not _active(policy):
            return HttpResponse(_("AI site design is not enabled for this journal and test environment."), status=409)
        if request.POST.get("policy_revision") != str(policy.revision) or request.POST.get("model") != settings.IPS_OPENAI_MODEL:
            return HttpResponse(_("The AI policy or model changed; reload the page."), status=409)
        previous = JournalSiteAIDesignRun.objects.filter(pk=run_id).first()
        if previous:
            if previous.site_id != site.pk or previous.requested_by_id != request.user.pk:
                raise Http404
            if previous.prompt_version not in (LEGACY_PROMPT_VERSION, PROMPT_VERSION):
                return HttpResponse(_("This request identifier belongs to a different AI task."), status=409)
            return redirect("site_ai_result", slug=slug, run_id=previous.pk)
        if request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        try:
            current_appearance = validate_blueprint(site.design_config) if site.design_config else legacy_to_blueprint(site.theme, site.layout)
        except InvalidDesign:
            return HttpResponse(_("The site design is invalid."), status=409)
        since = timezone.now() - timedelta(hours=24)
        if account_requests_since(request.user, since) >= DAILY_REQUEST_LIMIT:
            return HttpResponse(_("The local test request limit has been reached."), status=429)
        if journal_requests_since(journal, since) >= policy.journal_daily_limit:
            return HttpResponse(_("The journal's local test request limit has been reached."), status=429)
        run = JournalSiteAIDesignRun.objects.create(id=run_id, site=site, requested_by=request.user,
            model_name=settings.IPS_OPENAI_MODEL, prompt_version=PROMPT_VERSION, policy_revision=policy.revision,
            site_revision=site.revision, site_fingerprint=_fingerprint(site),
            brief_fingerprint=hashlib.sha256(brief.encode("utf-8")).hexdigest())
        snapshot = _snapshot(policy)
        JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_ai_requested", revision=site.revision)

    # The brief is held only for this request; it is not stored in the test database.
    fresh_actor = get_user_model().objects.get(pk=request.user.pk)
    try:
        fresh_journal = _managed_journal(fresh_actor, slug)
        fresh_site = _site(fresh_journal)
        fresh_policy = _policy(fresh_journal)
        allowed = (_provider_configured() and settings.IPS_OPENAI_MODEL == run.model_name and
                   _active(fresh_policy) and _snapshot(fresh_policy) == snapshot and _current(fresh_site, run))
    except Http404:
        allowed = False
    if not allowed:
        run.status, run.error_code, run.finished_at = JournalSiteAIDesignRun.Status.CANCELLED, "authorization_changed", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
        return redirect("site_ai_result", slug=slug, run_id=run.pk)
    try:
        result = run_ai_task(task=SITE_DESIGN, provider=OPENAI, api_key=os.environ.get("OPENAI_API_KEY", ""),
                             model=run.model_name, payload={"brief": brief, "appearance":current_appearance})
    except Exception:
        run.status, run.error_code, run.finished_at = JournalSiteAIDesignRun.Status.FAILED, "provider_unavailable", timezone.now()
        run.save(update_fields=["status", "error_code", "finished_at"])
    else:
        run.status, run.options, run.finished_at = JournalSiteAIDesignRun.Status.COMPLETED, result, timezone.now()
        run.save(update_fields=["status", "options", "finished_at"])
    return redirect("site_ai_result", slug=slug, run_id=run.pk)


@login_required
@never_cache
@require_GET
def site_ai_result(request, slug, run_id):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    run = _run(journal, request.user, run_id)
    if run.prompt_version == MODULE_PROMPT_VERSION:
        return redirect("site_ai_module_result", slug=slug, run_id=run.pk)
    choices = []
    if run.status == JournalSiteAIDesignRun.Status.COMPLETED:
        for i, option in enumerate(_options(run)):
            config = legacy_to_blueprint(option["theme"], option["layout"]) if run.prompt_version == LEGACY_PROMPT_VERSION else option
            choices.append({"index": i, "appearance": appearance_for(config),
                "composition_label": dict(COMPOSITIONS)[config["composition"]], "font_label": dict(FONTS)[config["font"]]})
    return _no_index(render(request, "publishing/site_ai_result.html", {"journal": journal, "site": site, "run": run,
        "choices": choices, "current": _current(site, run)}))


@login_required
@never_cache
@require_GET
def preview_site_ai_option(request, slug, run_id, index):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    run = _run(journal, request.user, run_id)
    if not _current(site, run):
        return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
    options = _options(run)
    if index >= len(options):
        raise Http404
    design = SimpleNamespace(**{field: getattr(site, field) for field in SITE_FIELDS})
    if run.prompt_version == LEGACY_PROMPT_VERSION:
        design.theme, design.layout = options[index]["theme"], options[index]["layout"]
    else:
        design.design_config = options[index]
    return _render_site(request, journal, design, preview=True, ai_proposal_preview=True)


@login_required
@never_cache
@require_POST
def apply_site_ai_option(request, slug, run_id, index):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm applying this option to the draft."), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        run = _run(journal, request.user, run_id, locked=True)
        options = _options(run)
        if index >= len(options):
            raise Http404
        if run.applied_revision is not None:
            return HttpResponse(_("This suggestion has already been applied."), status=409)
        if request.POST.get("revision") != str(site.revision) or not _current(site, run):
            return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
        option = options[index]
        if run.prompt_version == LEGACY_PROMPT_VERSION:
            changed = site.theme != option["theme"] or site.layout != option["layout"]
            site.theme, site.layout = option["theme"], option["layout"]
            changed_fields = ["theme", "layout"]
        else:
            changed = site.design_config != option
            site.design_config = option
            changed_fields = ["design_config"]
        if changed:
            site.revision += 1
            site.save(update_fields=[*changed_fields, "revision"])
        run.applied_option, run.applied_revision = index, site.revision
        run.save(update_fields=["applied_option", "applied_revision"])
        JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_ai_option_applied", revision=site.revision)
    messages.success(request, _("Suggestion applied to the draft. Publish separately after review."))
    return redirect("manage_journal_site", slug=slug)
