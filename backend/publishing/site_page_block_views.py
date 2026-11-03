"""Build independent pages from scoped, snapshotted content blocks."""
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import JournalSiteEvent
from .module_images import image_choices, images_valid, sync_image_references
from .page_blocks import MAX_PAGE_BLOCKS, PageBlockFormSet
from .page_block_layout import BlockLayoutForm
from .page_block_ai_provider import LEGACY_PROMPT_VERSION, MAX_OPTIONS, PROMPT_VERSION
from .page_layout_ai_provider import PROMPT_VERSION as LAYOUT_PROMPT_VERSION
from .portal_views import _event as portal_event
from .portal_policy_views import _policy as publisher_policy
from .ai_views import _policy as journal_policy, _provider_configured
from .production_views import _enabled, _no_index
from .site_page_views import _fingerprint, _managed, _valid_design


def _page(site, kind, page_slug):
    pages = _valid_design(site, site, kind, site)
    if pages is None:
        raise Http404
    page = next((entry for entry in pages if entry["slug"] == page_slug), None)
    if page is None:
        raise Http404
    return pages, page


def _editor(request, slug, page_slug, kind, formset=None, layout_form=None, status=200):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    pages, page = _page(site, kind, page_slug)
    if formset is None:
        formset = PageBlockFormSet(prefix="sections", initial=page.get("blocks", []),
                                   form_kwargs={"image_choices":image_choices(site)})
    if layout_form is None:
        layout_form = BlockLayoutForm(prefix="layout", saved_layout=page.get("block_layout"))
    policy = journal_policy(owner) if kind == "journal" else publisher_policy(owner)
    enabled = bool(policy and (policy.site_page_blocks_enabled if kind == "journal" else policy.portal_page_blocks_enabled))
    return _no_index(render(request, "publishing/manage_page_blocks.html", {
        "owner":owner, "site":site, "page":page, "block_formset":formset, "layout_form":layout_form,
        "fingerprint":_fingerprint(site, kind),
        "save_route":"save_journal_page_blocks" if kind == "journal" else "save_publisher_page_blocks",
        "pages_route":"manage_journal_pages" if kind == "journal" else "manage_publisher_pages",
        "preview_route":"preview_journal_page" if kind == "journal" else "preview_publisher_page",
        "manual_layout_route":"journal_manual_layout" if kind == "journal" else "publisher_manual_layout",
        "ai_enabled":enabled, "ai_provider_configured":_provider_configured(),
        "ai_policy_revision":policy.revision if policy else 0, "ai_model_name":settings.IPS_OPENAI_MODEL,
        "ai_request_id":uuid.uuid4(), "available_slots":MAX_PAGE_BLOCKS - len(page.get("blocks", [])),
        "layout_ai_enabled":bool(policy and (policy.site_page_layout_enabled if kind == "journal" else policy.portal_page_layout_enabled)),
        "layout_request_id":uuid.uuid4(), "layout_block_count":len(page.get("blocks", [])),
        "layout_runs":site.ai_design_runs.filter(requested_by=request.user, target_page_slug=page_slug,
                                                  prompt_version=LAYOUT_PROMPT_VERSION)[:10],
        "layout_request_route":"request_journal_page_layout_ai" if kind == "journal" else "request_publisher_page_layout_ai",
        "layout_result_route":"journal_page_layout_ai_result" if kind == "journal" else "publisher_page_layout_ai_result",
        "ai_option_counts":range(1, MAX_OPTIONS + 1),
        "ai_runs":site.ai_design_runs.filter(requested_by=request.user, target_page_slug=page_slug,
                                              prompt_version__in=(LEGACY_PROMPT_VERSION, PROMPT_VERSION))[:10],
        "ai_request_route":"request_journal_page_block_ai" if kind == "journal" else "request_publisher_page_block_ai",
        "ai_result_route":"journal_page_block_ai_result" if kind == "journal" else "publisher_page_block_ai_result",
    }, status=status))


def _save(request, slug, page_slug, kind):
    _enabled()
    owner, site = _managed(request.user, slug, kind)
    pages, page = _page(site, kind, page_slug)
    initial = page.get("blocks", [])
    try:
        total = int(request.POST.get("sections-TOTAL_FORMS", "-1"))
    except ValueError:
        total = -1
    if request.POST.get("sections-INITIAL_FORMS") != str(len(initial)) or total < len(initial):
        return HttpResponse(_("The page editor is out of date; reload the page."), status=409)
    formset = PageBlockFormSet(request.POST, prefix="sections", initial=initial,
                               form_kwargs={"image_choices":image_choices(site)})
    layout_form = BlockLayoutForm(request.POST, prefix="layout", saved_layout=page.get("block_layout"))
    blocks_valid, layout_valid = formset.is_valid(), layout_form.is_valid()
    if not blocks_valid or not layout_valid:
        return _editor(request, slug, page_slug, kind, formset=formset, layout_form=layout_form, status=400)
    with transaction.atomic():
        owner, site = _managed(request.user, slug, kind, locked=True)
        if request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site, kind):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        pages, page = _page(site, kind, page_slug)
        updated = dict(page)
        if formset.cleaned_blocks:
            updated["blocks"] = formset.cleaned_blocks
        else:
            updated.pop("blocks", None)
        if layout_form.layout is not None:
            updated["block_layout"] = layout_form.layout
        else:
            updated.pop("block_layout", None)
        new_pages = [updated if entry["slug"] == page_slug else entry for entry in pages]
        if not images_valid(site, site.sections_config, pages=new_pages, check_references=False):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if new_pages != site.pages_config:
            sync_image_references(site, site, site.sections_config, pages=new_pages)
            site.pages_config = new_pages
            site.revision += 1
            site.save(update_fields=["pages_config", "revision"])
            if kind == "journal":
                JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_page_blocks_updated", revision=site.revision)
            else:
                portal_event(site, request.user, "portal_page_blocks_updated")
    messages.success(request, _("Content blocks saved to the draft. Preview the page before publishing."))
    return redirect("manage_journal_page_blocks" if kind == "journal" else "manage_publisher_page_blocks",
                    slug=slug, page_slug=page_slug)


@login_required
@never_cache
@require_GET
def manage_journal_page_blocks(request, slug, page_slug):
    return _editor(request, slug, page_slug, "journal")


@login_required
@never_cache
@require_POST
def save_journal_page_blocks(request, slug, page_slug):
    return _save(request, slug, page_slug, "journal")


@login_required
@never_cache
@require_GET
def manage_publisher_page_blocks(request, slug, page_slug):
    return _editor(request, slug, page_slug, "portal")


@login_required
@never_cache
@require_POST
def save_publisher_page_blocks(request, slug, page_slug):
    return _save(request, slug, page_slug, "portal")
