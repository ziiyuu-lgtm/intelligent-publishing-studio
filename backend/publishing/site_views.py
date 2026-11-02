"""Local-only journal site design, preview and versioned test publication."""
import hashlib
import json
import uuid
from types import SimpleNamespace

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .forms import JournalAppearanceForm, JournalSectionFormSet, JournalSiteForm
from .ai_views import _policy, _provider_configured
from .models import HERO_FIT_CHOICES, HERO_POSITION_CHOICES, Journal, JournalSiteDraft, JournalSiteEvent, JournalSiteRelease, Membership
from .production_views import _enabled, _no_index
from .site_design import InvalidDesign, SAMPLES, appearance_for, legacy_to_blueprint, sample_by_id, validate_blueprint
from .site_sections import InvalidSections, default_sections, validate_sections, visible_sections
from .site_pages import InvalidPages, page_navigation, validate_pages
from .site_images import hero_reference_valid
from .module_images import image_choices, images_valid, sync_image_references
from .views import _has_role

TEXT_FIELDS = ("brand_name", "headline", "tagline", "introduction", "theme", "layout")
SITE_FIELDS = (*TEXT_FIELDS, "design_config", "sections_config", "pages_config", "hero_asset_id", "hero_alt", "hero_fit", "hero_position")


def _managed_journal(user, slug, locked=False):
    qs = Journal.objects.select_for_update() if locked else Journal.objects.select_related("organisation")
    journal = get_object_or_404(qs, slug=slug, active=True, organisation__active=True)
    if not _has_role(user, journal, Membership.Role.SITE_MANAGER):
        raise Http404
    return journal


def _site(journal, locked=False):
    qs = JournalSiteDraft.objects.select_for_update() if locked else JournalSiteDraft.objects.select_related("current_release")
    return get_object_or_404(qs, journal=journal)


def _fingerprint(site):
    # Empty configuration preserves fingerprints from the earlier legacy design.
    payload = {"site": site.pk, "revision": site.revision, **{key: getattr(site, key) for key in TEXT_FIELDS}}
    if site.design_config:
        payload["design_config"] = site.design_config
    if site.sections_config:
        payload["sections_config"] = site.sections_config
    if site.pages_config:
        payload["pages_config"] = site.pages_config
    if site.hero_asset_id or site.hero_alt or site.hero_fit != "cover" or site.hero_position != "center":
        payload["hero_asset_id"] = str(site.hero_asset_id) if site.hero_asset_id else None
        payload["hero_alt"] = site.hero_alt
        payload["hero_fit"] = site.hero_fit
        payload["hero_position"] = site.hero_position
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _appearance(site):
    try:
        return appearance_for(site.design_config) if site.design_config else None
    except InvalidDesign as exc:
        raise Http404 from exc


def _render_site(request, journal, design, **context):
    try:
        sections = visible_sections(design.sections_config) if design.sections_config else []
        pages = validate_pages(design.pages_config)
    except (InvalidSections, InvalidPages) as exc:
        raise Http404 from exc
    site = get_object_or_404(JournalSiteDraft, journal=journal)
    owner = site if context.get("preview") else design
    if not images_valid(site, design.sections_config, owner):
        raise Http404
    for section in sections:
        if section.get("image_asset_id"):
            route = "private_journal_image" if context.get("preview") else "public_journal_image"
            section["image_url"] = reverse(route, args=[journal.slug, section["image_asset_id"]])
    hero_image_url = ""
    if design.hero_asset_id or design.hero_alt or design.hero_fit != "cover" or design.hero_position != "center":
        site = get_object_or_404(JournalSiteDraft, journal=journal)
        if not hero_reference_valid(site, design.hero_asset_id, design.hero_alt, design.hero_fit, design.hero_position):
            raise Http404
        if design.hero_asset_id:
            route = "private_journal_image" if context.get("preview") else "public_journal_image"
            hero_image_url = reverse(route, args=[journal.slug, design.hero_asset_id])
    return _no_index(render(request, "publishing/journal_site.html", {"journal": journal, "design": design,
        "appearance": _appearance(design), "sections": sections, "hero_image_url":hero_image_url,
        "page_navigation":page_navigation(pages, "journal", journal.slug, bool(context.get("preview"))),
        "has_about_section": any(item["kind"] == "about" for item in sections), **context}))


def _context(journal, site, form=None, user=None, appearance_form=None, section_formset=None):
    policy = _policy(journal)
    try:
        initial = validate_blueprint(site.design_config) if site.design_config else legacy_to_blueprint(site.theme, site.layout)
        samples = [{**sample, "appearance": appearance_for(sample["config"])} for sample in SAMPLES]
        section_initial = validate_sections(site.sections_config) if site.sections_config else default_sections(site)
    except (InvalidDesign, InvalidSections) as exc:
        raise Http404 from exc
    return {"journal": journal, "site": site, "form": form if form is not None else JournalSiteForm(instance=site),
            "appearance_form": appearance_form if appearance_form is not None else JournalAppearanceForm(initial=initial),
            "design_samples": samples,
            "section_formset": section_formset if section_formset is not None else JournalSectionFormSet(prefix="sections", initial=section_initial, form_kwargs={"image_choices":image_choices(site)}),
            "fingerprint": _fingerprint(site), "releases": site.releases.select_related("published_by"),
            "ai_design_enabled": bool(policy and policy.site_design_enabled),
            "ai_modules_enabled": bool(policy and policy.site_modules_enabled), "ai_provider_configured": _provider_configured(),
            "ai_policy_revision": policy.revision if policy else 0, "ai_model_name": settings.IPS_OPENAI_MODEL,
            "ai_request_id": uuid.uuid4(), "ai_module_request_id": uuid.uuid4(),
            "image_assets":site.images.defer("image_data"),
            "image_slug":journal.slug, "image_upload_route":"upload_journal_image",
            "image_select_route":"select_journal_image", "image_clear_route":"clear_journal_image",
            "image_delete_route":"delete_journal_image", "image_private_route":"private_journal_image",
            "hero_fit_choices":HERO_FIT_CHOICES, "hero_position_choices":HERO_POSITION_CHOICES,
            "ai_design_runs": site.ai_design_runs.filter(requested_by=user)[:10] if user else []}


def _stale(request, site):
    return request.POST.get("revision") != str(site.revision)


@login_required
@never_cache
@require_GET
def manage_journal_site(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = JournalSiteDraft.objects.filter(journal=journal).select_related("current_release").first()
    return render(request, "publishing/manage_journal_site.html", _context(journal, site, user=request.user) if site else {"journal": journal})


@login_required
@never_cache
@require_POST
def start_journal_site(request, slug):
    _enabled()
    if request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Confirm that the materials are fictional."), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site, created = JournalSiteDraft.objects.get_or_create(journal=journal, defaults={"brand_name": journal.name, "headline": journal.name,
            "tagline": "Fictional journal site", "introduction": "A fictional journal for local testing.", "created_by": request.user})
        if created:
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_draft_created", revision=site.revision)
    messages.success(request, _("Journal site draft created.") if created else _("The journal site draft already exists."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def edit_journal_site(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    form = JournalSiteForm(request.POST, instance=site)
    if not form.is_valid():
        return render(request, "publishing/manage_journal_site.html", _context(journal, _site(journal), form, user=request.user), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        changed = any(getattr(site, key) != form.cleaned_data[key] for key in TEXT_FIELDS)
        if changed:
            for key in TEXT_FIELDS:
                setattr(site, key, form.cleaned_data[key])
            site.revision += 1
            site.save(update_fields=[*TEXT_FIELDS, "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_draft_updated", revision=site.revision)
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_GET
def preview_journal_site(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    return _render_site(request, journal, site, preview=True)


@login_required
@never_cache
@require_GET
def preview_site_sample(request, slug, sample_id):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    try:
        sample = sample_by_id(sample_id)
    except InvalidDesign as exc:
        raise Http404 from exc
    design = SimpleNamespace(**{field: getattr(site, field) for field in SITE_FIELDS})
    design.design_config = sample["config"]
    return _render_site(request, journal, design, preview=True, sample_preview=True)


@login_required
@never_cache
@require_POST
def apply_site_sample(request, slug, sample_id):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm applying this option to the draft."), status=400)
    try:
        sample = sample_by_id(sample_id)
    except InvalidDesign as exc:
        raise Http404 from exc
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if site.design_config != sample["config"]:
            site.design_config = sample["config"]
            site.revision += 1
            site.save(update_fields=["design_config", "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_sample_applied", revision=site.revision)
    messages.success(request, _("Template applied to the draft. You can customise it before publishing."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def edit_site_appearance(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    form = JournalAppearanceForm(request.POST)
    if not form.is_valid():
        return render(request, "publishing/manage_journal_site.html", _context(journal, site, user=request.user, appearance_form=form), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if site.design_config != form.cleaned_data:
            site.design_config = form.cleaned_data
            site.revision += 1
            site.save(update_fields=["design_config", "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_appearance_updated", revision=site.revision)
    messages.success(request, _("Design saved to the draft. Preview it before publishing."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def edit_site_sections(request, slug):
    _enabled()
    journal = _managed_journal(request.user, slug)
    site = _site(journal)
    try:
        if site.design_config:
            validate_blueprint(site.design_config)
        initial = validate_sections(site.sections_config) if site.sections_config else default_sections(site)
    except (InvalidDesign, InvalidSections) as exc:
        raise Http404 from exc
    formset = JournalSectionFormSet(request.POST, prefix="sections", initial=initial, form_kwargs={"image_choices":image_choices(site)})
    if not formset.is_valid():
        return render(request, "publishing/manage_journal_site.html", _context(journal, site, user=request.user, section_formset=formset), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if not images_valid(site, formset.cleaned_sections, check_references=False):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if site.sections_config != formset.cleaned_sections:
            sync_image_references(site, site, formset.cleaned_sections)
            site.sections_config = formset.cleaned_sections
            site.revision += 1
            site.save(update_fields=["sections_config", "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_sections_updated", revision=site.revision)
    messages.success(request, _("Page modules saved to the draft. Preview them before publishing."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def publish_journal_site(request, slug):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        if _stale(request, site) or request.POST.get("fingerprint") != _fingerprint(site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if (site.theme not in JournalSiteDraft.Theme.values or site.layout not in JournalSiteDraft.Layout.values or
                not site.brand_name.strip() or not site.headline.strip() or not site.introduction.strip()):
            return HttpResponse(_("The site design is invalid."), status=409)
        if site.design_config:
            try:
                validate_blueprint(site.design_config)
            except InvalidDesign:
                return HttpResponse(_("The site design is invalid."), status=409)
        if site.sections_config:
            try:
                validate_sections(site.sections_config)
            except InvalidSections:
                return HttpResponse(_("The page modules are invalid."), status=409)
        try:
            validate_pages(site.pages_config)
        except InvalidPages:
            return HttpResponse(_("The site pages are invalid."), status=409)
        if not hero_reference_valid(site, site.hero_asset_id, site.hero_alt, site.hero_fit, site.hero_position):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if not images_valid(site, site.sections_config, site):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if site.current_release_id and (site.current_release.site_id != site.pk or site.current_release.revision == site.revision):
            return HttpResponse(_("This version is already published or unavailable."), status=409)
        release = JournalSiteRelease.objects.create(site=site, revision=site.revision, published_by=request.user,
            **{key: getattr(site, key) for key in SITE_FIELDS})
        sync_image_references(site, release, release.sections_config)
        site.current_release = release
        site.save(update_fields=["current_release"])
        JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_test_published", revision=site.revision, source_release=release)
    messages.success(request, _("Journal site published in the local test environment."))
    return redirect("manage_journal_site", slug=slug)


@login_required
@never_cache
@require_POST
def restore_journal_site(request, slug, release_id):
    _enabled()
    with transaction.atomic():
        journal = _managed_journal(request.user, slug, locked=True)
        site = _site(journal, locked=True)
        release = get_object_or_404(JournalSiteRelease, pk=release_id, site=site)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if request.POST.get("confirm") != "yes":
            return HttpResponse(_("Confirm the draft restoration."), status=400)
        if release.theme not in JournalSiteDraft.Theme.values or release.layout not in JournalSiteDraft.Layout.values:
            return HttpResponse(_("The site design is invalid."), status=409)
        if release.design_config:
            try:
                validate_blueprint(release.design_config)
            except InvalidDesign:
                return HttpResponse(_("The site design is invalid."), status=409)
        if release.sections_config:
            try:
                validate_sections(release.sections_config)
            except InvalidSections:
                return HttpResponse(_("The page modules are invalid."), status=409)
        try:
            validate_pages(release.pages_config)
        except InvalidPages:
            return HttpResponse(_("The site pages are invalid."), status=409)
        if not hero_reference_valid(site, release.hero_asset_id, release.hero_alt, release.hero_fit, release.hero_position):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if not images_valid(site, release.sections_config, release):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        changed = any(getattr(site, key) != getattr(release, key) for key in SITE_FIELDS)
        if changed:
            sync_image_references(site, site, release.sections_config, pages=release.pages_config)
            for key in SITE_FIELDS:
                setattr(site, key, getattr(release, key))
            site.revision += 1
            site.save(update_fields=[*SITE_FIELDS, "revision"])
            JournalSiteEvent.objects.create(site=site, actor=request.user, action="site_draft_restored", revision=site.revision, source_release=release)
    messages.success(request, _("Version restored to the draft; publish separately after review."))
    return redirect("manage_journal_site", slug=slug)


@require_GET
def public_journal_site(request, slug):
    _enabled()
    journal = get_object_or_404(Journal.objects.select_related("organisation"), slug=slug, active=True, organisation__active=True)
    site = get_object_or_404(JournalSiteDraft.objects.select_related("current_release"), journal=journal, current_release__isnull=False)
    if (site.current_release.site_id != site.pk or site.current_release.theme not in JournalSiteDraft.Theme.values or
            site.current_release.layout not in JournalSiteDraft.Layout.values):
        raise Http404
    return _render_site(request, journal, site.current_release, preview=False)
