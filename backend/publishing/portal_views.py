"""Fictional local publisher portals with scoped permissions and versioned releases."""
import hashlib
import json
import re
from types import SimpleNamespace

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import F
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import (Journal, JournalSiteDraft, Organisation, OrganisationMembership,
                     OrganisationPortalDraft, OrganisationPortalEvent, OrganisationPortalRelease)
from .portal_forms import MAX_JOURNALS, PortalAppearanceForm, PortalContentForm
from .production_views import _enabled, _no_index
from .site_design import InvalidDesign, SAMPLES, appearance_for, sample_by_id, validate_blueprint
from .site_sections import InvalidSections, visible_sections

CONTENT_FIELDS = ("brand_name", "headline", "tagline", "introduction")


def _organisation(user, slug, locked=False):
    qs = Organisation.objects.select_for_update() if locked else Organisation.objects.all()
    organisation = get_object_or_404(qs, slug=slug, active=True)
    if not user.is_active or not OrganisationMembership.objects.filter(
            organisation=organisation, user=user, role=OrganisationMembership.Role.PORTAL_MANAGER, active=True).exists():
        raise Http404
    return organisation


def _site(organisation, locked=False):
    qs = OrganisationPortalDraft.objects.select_for_update() if locked else OrganisationPortalDraft.objects.select_related("current_release")
    return get_object_or_404(qs, organisation=organisation)


def _valid_ids(ids):
    return (isinstance(ids, list) and len(ids) <= MAX_JOURNALS and
            all(type(item) is int and item > 0 for item in ids) and len(ids) == len(set(ids)))


def _journal_queryset(organisation):
    return Journal.objects.filter(organisation=organisation, active=True)


def _linked_journal_ids(organisation, journal_ids):
    """Only link to journal releases that the existing public view can render."""
    sites = JournalSiteDraft.objects.filter(journal__organisation=organisation, journal__active=True,
        journal_id__in=journal_ids, current_release__isnull=False, current_release__site_id=F("pk")
        ).select_related("current_release")
    available = set()
    for site in sites:
        release = site.current_release
        if release.theme not in JournalSiteDraft.Theme.values or release.layout not in JournalSiteDraft.Layout.values:
            continue
        try:
            if release.design_config:
                validate_blueprint(release.design_config)
            if release.sections_config:
                visible_sections(release.sections_config)
        except (InvalidDesign, InvalidSections):
            continue
        available.add(site.journal_id)
    return available


def _selected(site, locked=False):
    if not _valid_ids(site.journal_ids):
        raise ValueError("invalid journal selection")
    queryset = _journal_queryset(site.organisation)
    if locked:
        # Lock journals without an outer join; journal publication also locks these rows.
        list(Journal.objects.select_for_update().filter(organisation=site.organisation, pk__in=site.journal_ids).order_by("pk"))
    found = {j.pk: j for j in queryset.filter(pk__in=site.journal_ids)}
    if len(found) != len(site.journal_ids):
        raise ValueError("selected journal is unavailable")
    linked = _linked_journal_ids(site.organisation, site.journal_ids)
    for journal in found.values():
        journal.site_published = journal.pk in linked
    return [found[pk] for pk in site.journal_ids]


def _fingerprint(site):
    # Bind publication confirmation to the chosen journal names, slugs and link availability too.
    try:
        journals = [(j.pk, j.slug, j.name, j.site_published) for j in _selected(site)]
    except ValueError:
        journals = None
    payload = {"site":site.pk, "revision":site.revision, "design":site.design_config, "ids":site.journal_ids,
               "journals":journals, **{key:getattr(site, key) for key in CONTENT_FIELDS}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _valid_snapshot(journals):
    if not isinstance(journals, list) or len(journals) > MAX_JOURNALS:
        return False
    ids = []
    for item in journals:
        if (not isinstance(item, dict) or set(item) != {"id", "slug", "name"} or type(item["id"]) is not int
                or item["id"] <= 0 or not isinstance(item["slug"], str)
                or not re.fullmatch(r"[-a-zA-Z0-9_]{1,50}", item["slug"])
                or not isinstance(item["name"], str) or not item["name"].strip() or len(item["name"]) > 200):
            return False
        ids.append(item["id"])
    return len(ids) == len(set(ids))


def _snapshot_journals(organisation, snapshots):
    if not _valid_snapshot(snapshots):
        raise Http404
    current = {j.pk:j for j in _journal_queryset(organisation).filter(pk__in=[item["id"] for item in snapshots])}
    linked = _linked_journal_ids(organisation, list(current))
    # A removed, moved, deactivated or renamed slug is not exposed or linked.
    return [{"name":item["name"], "slug":item["slug"], "can_link":item["id"] in linked}
            for item in snapshots if item["id"] in current and current[item["id"]].slug == item["slug"]]


def _draft_journals(site):
    try:
        return [{"name":j.name, "slug":j.slug, "can_link":j.site_published} for j in _selected(site)]
    except ValueError as exc:
        raise Http404 from exc


def _render(request, organisation, design, journals, **extra):
    try:
        appearance = appearance_for(design.design_config)
    except InvalidDesign as exc:
        raise Http404 from exc
    return _no_index(render(request, "publishing/publisher_portal.html", {
        "organisation":organisation, "design":design, "appearance":appearance, "journals":journals, **extra}))


def _context(organisation, site, content_form=None, appearance_form=None):
    try:
        design = validate_blueprint(site.design_config)
    except InvalidDesign as exc:
        raise Http404 from exc
    initial = {key:getattr(site, key) for key in CONTENT_FIELDS}
    initial["journals"] = site.journal_ids if _valid_ids(site.journal_ids) else []
    return {"organisation":organisation, "site":site, "fingerprint":_fingerprint(site),
        "form":content_form if content_form is not None else PortalContentForm(organisation=organisation, initial=initial),
        "appearance_form":appearance_form if appearance_form is not None else PortalAppearanceForm(initial=design),
        "samples":[{**sample, "appearance":appearance_for(sample["config"])} for sample in SAMPLES],
        "releases":site.releases.select_related("published_by")}


def _stale(request, site):
    return request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site)


def _event(site, user, action, release=None):
    OrganisationPortalEvent.objects.create(site=site, actor=user, action=action, revision=site.revision, source_release=release)


@login_required
@never_cache
@require_GET
def manage_portal(request, slug):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = OrganisationPortalDraft.objects.filter(organisation=organisation).select_related("current_release").first()
    return render(request, "publishing/manage_publisher_portal.html",
                  _context(organisation, site) if site else {"organisation":organisation})


@login_required
@never_cache
@require_POST
def start_portal(request, slug):
    _enabled()
    if request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Confirm that the materials are fictional."), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site, created = OrganisationPortalDraft.objects.get_or_create(organisation=organisation, defaults={
            "brand_name":organisation.name, "headline":organisation.name,
            "tagline":"Fictional publisher portal", "introduction":"A fictional publisher for local testing.",
            "design_config":sample_by_id("heritage")["config"], "created_by":request.user})
        if created:
            _event(site, request.user, "portal_draft_created")
    return redirect("manage_publisher_portal", slug=slug)


@login_required
@never_cache
@require_POST
def edit_portal(request, slug):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    form = PortalContentForm(request.POST, organisation=organisation)
    if not form.is_valid():
        return render(request, "publishing/manage_publisher_portal.html", _context(organisation, site, content_form=form), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        # Re-check membership of the selected journals after acquiring the organisation lock.
        ids = list(form.cleaned_data["journals"].values_list("pk", flat=True))
        if Journal.objects.filter(organisation=organisation, active=True, pk__in=ids).count() != len(ids):
            return HttpResponse(_("The journal selection changed; reload the page."), status=409)
        changed = any(getattr(site, k) != form.cleaned_data[k] for k in CONTENT_FIELDS) or site.journal_ids != ids
        if changed:
            for key in CONTENT_FIELDS:
                setattr(site, key, form.cleaned_data[key])
            site.journal_ids = ids
            site.revision += 1
            site.save(update_fields=[*CONTENT_FIELDS, "journal_ids", "revision"])
            _event(site, request.user, "portal_draft_updated")
    return redirect("manage_publisher_portal", slug=slug)


@login_required
@never_cache
@require_GET
def preview_portal(request, slug):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    return _render(request, organisation, site, _draft_journals(site), preview=True)


@login_required
@never_cache
@require_GET
def preview_sample(request, slug, sample_id):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    try:
        sample = sample_by_id(sample_id)
    except InvalidDesign as exc:
        raise Http404 from exc
    design = SimpleNamespace(**{field:getattr(site, field) for field in CONTENT_FIELDS}, design_config=sample["config"])
    return _render(request, organisation, design, _draft_journals(site), preview=True, sample_preview=True)


@login_required
@never_cache
@require_POST
def apply_sample(request, slug, sample_id):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm applying this option to the draft."), status=400)
    try:
        config = sample_by_id(sample_id)["config"]
    except InvalidDesign as exc:
        raise Http404 from exc
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if site.design_config != config:
            site.design_config = config
            site.revision += 1
            site.save(update_fields=["design_config", "revision"])
            _event(site, request.user, "portal_sample_applied")
    messages.success(request, _("Template applied to the draft. You can customise it before publishing."))
    return redirect("manage_publisher_portal", slug=slug)


@login_required
@never_cache
@require_POST
def edit_appearance(request, slug):
    _enabled()
    organisation = _organisation(request.user, slug)
    site = _site(organisation)
    form = PortalAppearanceForm(request.POST)
    if not form.is_valid():
        return render(request, "publishing/manage_publisher_portal.html", _context(organisation, site, appearance_form=form), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if site.design_config != form.cleaned_data:
            site.design_config = form.cleaned_data
            site.revision += 1
            site.save(update_fields=["design_config", "revision"])
            _event(site, request.user, "portal_appearance_updated")
    return redirect("manage_publisher_portal", slug=slug)


@login_required
@never_cache
@require_POST
def publish_portal(request, slug):
    _enabled()
    if request.POST.get("confirm") != "yes" or request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Both confirmations are required."), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        try:
            journals = _selected(site, locked=True)
            config = validate_blueprint(site.design_config)
        except (ValueError, InvalidDesign):
            return HttpResponse(_("The portal draft or journal selection is invalid."), status=409)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if any(not getattr(site, key).strip() for key in ("brand_name", "headline", "introduction")):
            return HttpResponse(_("The portal draft or journal selection is invalid."), status=409)
        if site.current_release_id and (site.current_release.site_id != site.pk or site.current_release.revision == site.revision):
            return HttpResponse(_("This version is already published or unavailable."), status=409)
        snapshots = [{"id":j.pk, "slug":j.slug, "name":j.name} for j in journals]
        release = OrganisationPortalRelease.objects.create(site=site, revision=site.revision,
            published_by=request.user, design_config=config, journals=snapshots,
            **{key:getattr(site, key) for key in CONTENT_FIELDS})
        site.current_release = release
        site.save(update_fields=["current_release"])
        _event(site, request.user, "portal_test_published", release)
    messages.success(request, _("Publisher portal published in the local test environment."))
    return redirect("manage_publisher_portal", slug=slug)


@login_required
@never_cache
@require_POST
def restore_portal(request, slug, release_id):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm the draft restoration."), status=400)
    with transaction.atomic():
        organisation = _organisation(request.user, slug, locked=True)
        site = _site(organisation, locked=True)
        release = get_object_or_404(OrganisationPortalRelease, pk=release_id, site=site)
        if _stale(request, site):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        try:
            config = validate_blueprint(release.design_config)
            if not _valid_snapshot(release.journals):
                raise ValueError
            ids = [item["id"] for item in release.journals]
            available = {j.pk:j for j in _journal_queryset(organisation).filter(pk__in=ids)}
            if len(available) != len(ids) or any(available[item["id"]].slug != item["slug"] for item in release.journals):
                raise ValueError
        except (ValueError, InvalidDesign):
            return HttpResponse(_("Some journals in this version are unavailable. Update the draft manually."), status=409)
        changed = any(getattr(site, key) != getattr(release, key) for key in CONTENT_FIELDS)
        changed = changed or site.design_config != config or site.journal_ids != ids
        if changed:
            for key in CONTENT_FIELDS:
                setattr(site, key, getattr(release, key))
            site.design_config = config
            site.journal_ids = ids
            site.revision += 1
            site.save(update_fields=[*CONTENT_FIELDS, "design_config", "journal_ids", "revision"])
            _event(site, request.user, "portal_draft_restored", release)
    messages.success(request, _("Version restored to the draft; publish separately after review."))
    return redirect("manage_publisher_portal", slug=slug)


@require_GET
def public_portal(request, slug):
    _enabled()
    organisation = get_object_or_404(Organisation, slug=slug, active=True)
    site = get_object_or_404(OrganisationPortalDraft.objects.select_related("current_release"),
                             organisation=organisation, current_release__isnull=False)
    if site.current_release.site_id != site.pk:
        raise Http404
    return _render(request, organisation, site.current_release, _snapshot_journals(organisation, site.current_release.journals))
