"""Manage and render independent pages in the isolated local site simulator."""
from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import (Journal, JournalSiteDraft, JournalSiteEvent, Organisation, OrganisationPortalDraft)
from .module_images import image_choices, images_valid, sync_image_references
from .portal_sections import InvalidSections as InvalidPortalSections, validate_sections as validate_portal_sections
from .portal_views import (_event as portal_event, _fingerprint as portal_fingerprint, _organisation,
                           _site as portal_site, _valid_snapshot)
from .page_design import page_appearance
from .block_design import block_appearance
from .page_block_ai_forms import validate_reviewed_blocks
from .page_blocks import MAX_PAGE_BLOCKS
from .page_ai_provider import PROMPT_VERSION
from .production_views import _enabled, _no_index
from .site_design import InvalidDesign, appearance_for, validate_blueprint
from .site_images import hero_reference_valid
from .site_pages import InvalidPages, SitePageFormSet, page_navigation, validate_pages
from .site_sections import InvalidSections as InvalidJournalSections, validate_sections as validate_journal_sections
from .site_views import _fingerprint as journal_fingerprint, _managed_journal, _site as journal_site


def _managed(user, slug, kind, locked=False):
    if kind == "journal":
        owner = _managed_journal(user, slug, locked=locked)
        return owner, journal_site(owner, locked=locked)
    owner = _organisation(user, slug, locked=locked)
    return owner, portal_site(owner, locked=locked)


def _fingerprint(site, kind):
    return journal_fingerprint(site) if kind == "journal" else portal_fingerprint(site)


def _valid_design(site, design, kind, owner):
    try:
        pages = validate_pages(design.pages_config)
        if kind == "journal":
            if design.theme not in JournalSiteDraft.Theme.values or design.layout not in JournalSiteDraft.Layout.values:
                return None
            if design.design_config:
                validate_blueprint(design.design_config)
            if design.sections_config:
                validate_journal_sections(design.sections_config)
        else:
            validate_blueprint(design.design_config)
            if design.sections_config:
                validate_portal_sections(design.sections_config)
            if owner is not site and not _valid_snapshot(design.journals):
                return None
        if not hero_reference_valid(site, design.hero_asset_id, design.hero_alt, design.hero_fit, design.hero_position):
            return None
        if not images_valid(site, design.sections_config, owner, pages=pages):
            return None
        return pages
    except (ValueError, InvalidDesign, InvalidPages, InvalidJournalSections, InvalidPortalSections):
        return None


def _editor(request, slug, kind, formset=None, status=200):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    try:
        initial = validate_pages(site.pages_config)
    except InvalidPages as exc:
        raise Http404 from exc
    if formset is None:
        formset = SitePageFormSet(prefix="pages", initial=initial, form_kwargs={"image_choices":image_choices(site)})
    from .ai_views import _policy as journal_policy, _provider_configured
    from .portal_policy_views import _policy as publisher_policy
    import uuid
    policy = journal_policy(owner) if kind == "journal" else publisher_policy(owner)
    enabled = bool(policy and (policy.site_pages_enabled if kind == "journal" else policy.portal_pages_enabled))
    return _no_index(render(request, "publishing/manage_site_pages.html", {
        "owner":owner, "site":site, "kind":kind, "page_formset":formset,
        "fingerprint":_fingerprint(site, kind),
        "save_route":"save_journal_pages" if kind == "journal" else "save_publisher_pages",
        "manage_route":"manage_journal_site" if kind == "journal" else "manage_publisher_portal",
        "preview_route":"preview_journal_page" if kind == "journal" else "preview_publisher_page",
        "blocks_route":"manage_journal_page_blocks" if kind == "journal" else "manage_publisher_page_blocks",
        "page_options":initial, "ai_page_enabled":enabled, "ai_provider_configured":_provider_configured(),
        "ai_policy_revision":policy.revision if policy else 0, "ai_model_name":settings.IPS_OPENAI_MODEL,
        "ai_request_id":uuid.uuid4(), "ai_page_runs":site.ai_design_runs.filter(requested_by=request.user, prompt_version=PROMPT_VERSION)[:10],
        "ai_request_route":"request_journal_page_ai" if kind == "journal" else "request_publisher_page_ai",
        "ai_result_route":"journal_page_ai_result" if kind == "journal" else "publisher_page_ai_result",
    }, status=status))


def _save(request, slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    try:
        initial = validate_pages(site.pages_config)
    except InvalidPages as exc:
        raise Http404 from exc
    try:
        total = int(request.POST.get("pages-TOTAL_FORMS", "-1"))
    except ValueError:
        total = -1
    if request.POST.get("pages-INITIAL_FORMS") != str(len(initial)) or total < len(initial):
        return HttpResponse(_("The page editor is out of date; reload the page."), status=409)
    formset = SitePageFormSet(request.POST, prefix="pages", initial=initial, form_kwargs={"image_choices":image_choices(site)})
    if not formset.is_valid():
        return _editor(request, slug, kind, formset=formset, status=400)
    with transaction.atomic():
        owner, site = _managed(request.user, slug, kind, locked=True)
        if request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site, kind):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if _valid_design(site, site, kind, site) is None:
            return HttpResponse(_("The site draft or its images are invalid."), status=409)
        if not images_valid(site, site.sections_config, check_references=False, pages=formset.cleaned_pages):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if site.pages_config != formset.cleaned_pages:
            sync_image_references(site, site, site.sections_config, pages=formset.cleaned_pages)
            site.pages_config = formset.cleaned_pages
            site.revision += 1
            site.save(update_fields=["pages_config", "revision"])
            if kind == "journal":
                JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_pages_updated", revision=site.revision)
            else:
                portal_event(site, request.user, "portal_pages_updated")
    messages.success(request, _("Pages saved to the draft. Preview them before publishing."))
    return redirect("manage_journal_pages" if kind == "journal" else "manage_publisher_pages", slug=slug)


def _page(request, slug, page_slug, kind, preview, page_override=None, expected=None, ai_apply_url="", ai_revision=None,
          block_suggestion=None, block_apply_url="", block_edit_url="", block_reviewed=""):
    _enabled()
    if preview:
        owner, site = _managed(request.user, slug, kind)
        design = site
    elif kind == "journal":
        owner = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
        site = get_object_or_404(JournalSiteDraft.objects.select_related("current_release"), journal=owner, current_release__isnull=False)
        design = site.current_release
    else:
        owner = get_object_or_404(Organisation, slug=slug, active=True)
        site = get_object_or_404(OrganisationPortalDraft.objects.select_related("current_release"), organisation=owner, current_release__isnull=False)
        design = site.current_release
    if not preview and design.site_id != site.pk:
        raise Http404
    if expected is not None and (site.revision, _fingerprint(site, kind)) != expected:
        return HttpResponse(_("The site draft changed; request new suggestions."), status=409)
    pages = _valid_design(site, design, kind, site if preview else design)
    if pages is None:
        raise Http404
    page = next((entry for entry in pages if entry["slug"] == page_slug and (preview or entry["visible"])), None)
    if page is None:
        raise Http404
    if page_override is not None:
        if not preview:
            raise Http404
        try:
            candidate = validate_pages([page_override])[0]
        except InvalidPages as exc:
            raise Http404 from exc
        protected = ("slug", "visible", "image_asset_id", "image_alt", "image_fit", "image_position", "blocks")
        if any(candidate.get(key) != page.get(key) for key in protected):
            raise Http404
        page = candidate
        pages = [candidate if entry["slug"] == page_slug else entry for entry in pages]
    if block_suggestion is not None:
        if not preview or page_override is not None:
            raise Http404
        try:
            additions = validate_reviewed_blocks(block_suggestion, MAX_PAGE_BLOCKS - len(page.get("blocks", [])))
        except (ValueError, TypeError) as exc:
            raise Http404 from exc
        page = {**page, "blocks":[*page.get("blocks", []), *additions]}
    image_route = ("private_journal_image" if kind == "journal" else "private_portal_image") if preview else (
        "public_journal_image" if kind == "journal" else "public_portal_image")
    image_url = ""
    if page.get("image_asset_id"):
        image_url = reverse(image_route, args=[slug, page["image_asset_id"]])
    blocks = []
    for block in page.get("blocks", []):
        if block["visible"]:
            block = dict(block)
            block["image_url"] = reverse(image_route, args=[slug, block["image_asset_id"]]) if block.get("image_asset_id") else ""
            block["appearance"] = block_appearance(block["design"]) if block.get("design") else None
            blocks.append(block)
    home_route = ("preview_journal_site" if kind == "journal" else "preview_publisher_portal") if preview else (
        "public_journal_site" if kind == "journal" else "public_publisher_portal")
    return _no_index(render(request, "publishing/site_page.html", {
        "owner":owner, "design":design, "page":page, "preview":preview, "kind":kind,
        "ai_page_preview":page_override is not None,
        "ai_block_preview":block_suggestion is not None,
        "ai_apply_url":ai_apply_url, "ai_revision":ai_revision,
        "block_apply_url":block_apply_url, "block_edit_url":block_edit_url, "block_reviewed":block_reviewed,
        "page_appearance":page_appearance(page["design"]) if page.get("design") else None,
        "appearance":appearance_for(design.design_config) if design.design_config else None,
        "home_url":reverse(home_route, args=[slug]), "image_url":image_url,
        "page_blocks":blocks,
        "page_navigation":page_navigation(pages, kind, slug, preview),
    }))


@login_required
@never_cache
@require_GET
def manage_journal_pages(request, slug):
    return _editor(request, slug, "journal")


@login_required
@never_cache
@require_POST
def save_journal_pages(request, slug):
    return _save(request, slug, "journal")


@login_required
@never_cache
@require_GET
def preview_journal_page(request, slug, page_slug):
    return _page(request, slug, page_slug, "journal", True)


@require_GET
def public_journal_page(request, slug, page_slug):
    return _page(request, slug, page_slug, "journal", False)


@login_required
@never_cache
@require_GET
def manage_publisher_pages(request, slug):
    return _editor(request, slug, "portal")


@login_required
@never_cache
@require_POST
def save_publisher_pages(request, slug):
    return _save(request, slug, "portal")


@login_required
@never_cache
@require_GET
def preview_publisher_page(request, slug, page_slug):
    return _page(request, slug, page_slug, "portal", True)


@require_GET
def public_publisher_page(request, slug, page_slug):
    return _page(request, slug, page_slug, "portal", False)
