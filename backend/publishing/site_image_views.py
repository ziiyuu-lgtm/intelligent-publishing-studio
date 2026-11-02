"""Scoped local image upload, selection and serving for journal and publisher sites."""
import uuid

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.deletion import ProtectedError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from .models import (Journal, JournalSiteDraft, JournalSiteEvent, Organisation, OrganisationPortalDraft,
                     SiteImageAsset)
from .module_images import images_valid
from .portal_views import (_event as portal_event, _fingerprint as portal_fingerprint, _organisation,
                           _site as portal_site, _valid_snapshot)
from .portal_sections import InvalidSections as InvalidPortalSections, validate_sections as validate_portal_sections
from .production_views import _enabled, _no_index
from .site_design import InvalidDesign, validate_blueprint
from .site_images import MAX_SITE_IMAGES, InvalidImage, asset_is_intact, hero_reference_valid, normalise_image, valid_display
from .site_sections import InvalidSections as InvalidJournalSections, validate_sections as validate_journal_sections
from .site_pages import InvalidPages, validate_pages
from .site_views import _fingerprint as journal_fingerprint, _managed_journal, _site as journal_site


def _managed(user, slug, kind, locked=False):
    if kind == "journal":
        owner = _managed_journal(user, slug, locked=locked)
        return journal_site(owner, locked=locked)
    owner = _organisation(user, slug, locked=locked)
    return portal_site(owner, locked=locked)


def _fingerprint(site, kind):
    return journal_fingerprint(site) if kind == "journal" else portal_fingerprint(site)


def _event(site, actor, kind, action):
    if kind == "journal":
        JournalSiteEvent.objects.create(site=site, actor=actor, action=action, revision=site.revision)
    else:
        portal_event(site, actor, action)


def _redirect(slug, kind):
    return redirect("manage_journal_site" if kind == "journal" else "manage_publisher_portal", slug=slug)


def _stale(request, site, kind):
    return request.POST.get("revision") != str(site.revision) or request.POST.get("fingerprint") != _fingerprint(site, kind)


def _alt(request):
    value = request.POST.get("alt", "").strip()
    if not value or len(value) > 200:
        raise ValidationError("invalid alt")
    return value


def _asset_id(request):
    try:
        return uuid.UUID(request.POST.get("asset_id", ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise Http404 from exc


def _display(request):
    fit = request.POST.get("fit", "cover")
    position = request.POST.get("position", "center")
    if not valid_display(fit, position):
        raise ValidationError("invalid display")
    return fit, position


def _upload(request, slug, kind):
    _enabled()
    _managed(request.user, slug, kind)
    if request.POST.get("synthetic") != "yes":
        return HttpResponse(_("Confirm that the image is fictional test data."), status=400)
    use_as = request.POST.get("use_as", "hero")
    if use_as not in ("hero", "library"):
        return HttpResponse(_("Invalid image upload option."), status=400)
    try:
        alt = _alt(request)
        fit, position = _display(request)
        fields = normalise_image(request.FILES.get("image"))
    except (ValidationError, InvalidImage):
        return HttpResponse(_("Choose a single-frame JPEG, PNG or WebP image up to 5 MB, 4096 pixels per side and 12 megapixels, and provide an image description."), status=400)
    with transaction.atomic():
        site = _managed(request.user, slug, kind, locked=True)
        if _stale(request, site, kind):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if site.images.count() >= MAX_SITE_IMAGES:
            return HttpResponse(_("This site has reached its local image-library limit."), status=409)
        owner_field = "journal_site" if kind == "journal" else "portal_site"
        asset = SiteImageAsset.objects.create(**{owner_field:site}, **fields, default_alt=alt, uploaded_by=request.user)
        if use_as == "hero":
            site.hero_asset, site.hero_alt, site.hero_fit, site.hero_position = asset, alt, fit, position
            site.revision += 1
            site.save(update_fields=["hero_asset", "hero_alt", "hero_fit", "hero_position", "revision"])
            _event(site, request.user, kind, "site_hero_uploaded" if kind == "journal" else "portal_hero_uploaded")
        else:
            _event(site, request.user, kind, "site_image_uploaded" if kind == "journal" else "portal_image_uploaded")
    messages.success(request, _("Image added to the library. Select it in a page module or as the hero image."))
    return _redirect(slug, kind)


def _select(request, slug, kind):
    _enabled()
    _managed(request.user, slug, kind)
    try:
        alt = _alt(request)
        fit, position = _display(request)
    except ValidationError:
        return HttpResponse(_("Provide a valid image description and display setting."), status=400)
    asset_id = _asset_id(request)
    with transaction.atomic():
        site = _managed(request.user, slug, kind, locked=True)
        if _stale(request, site, kind):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        asset = get_object_or_404(site.images, pk=asset_id)
        if not asset_is_intact(asset):
            return HttpResponse(_("The selected image is unavailable or damaged."), status=409)
        if (site.hero_asset_id != asset.pk or site.hero_alt != alt or site.hero_fit != fit or
                site.hero_position != position):
            site.hero_asset, site.hero_alt, site.hero_fit, site.hero_position = asset, alt, fit, position
            site.revision += 1
            site.save(update_fields=["hero_asset", "hero_alt", "hero_fit", "hero_position", "revision"])
            _event(site, request.user, kind, "site_hero_selected" if kind == "journal" else "portal_hero_selected")
    messages.success(request, _("Image added to the draft. Preview it before publishing."))
    return _redirect(slug, kind)


def _clear(request, slug, kind):
    _enabled()
    with transaction.atomic():
        site = _managed(request.user, slug, kind, locked=True)
        if _stale(request, site, kind):
            return HttpResponse(_("The site draft changed; reload the page."), status=409)
        if site.hero_asset_id or site.hero_alt or site.hero_fit != "cover" or site.hero_position != "center":
            site.hero_asset, site.hero_alt, site.hero_fit, site.hero_position = None, "", "cover", "center"
            site.revision += 1
            site.save(update_fields=["hero_asset", "hero_alt", "hero_fit", "hero_position", "revision"])
            _event(site, request.user, kind, "site_hero_cleared" if kind == "journal" else "portal_hero_cleared")
    return _redirect(slug, kind)


def _delete(request, slug, kind):
    _enabled()
    if request.POST.get("confirm") != "yes":
        return HttpResponse(_("Confirm deletion of the unused image."), status=400)
    asset_id = _asset_id(request)
    with transaction.atomic():
        site = _managed(request.user, slug, kind, locked=True)
        asset = get_object_or_404(site.images.select_for_update(), pk=asset_id)
        try:
            asset.delete()
        except ProtectedError:
            return HttpResponse(_("This image is used by a draft or published version and cannot be deleted."), status=409)
        _event(site, request.user, kind, "site_image_deleted" if kind == "journal" else "portal_image_deleted")
    return _redirect(slug, kind)


def _image_response(asset):
    if not asset_is_intact(asset):
        raise Http404
    response = HttpResponse(bytes(asset.image_data), content_type=asset.media_type)
    response["Content-Security-Policy"] = "default-src 'none'"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return _no_index(response)


def _private_image(request, slug, asset_id, kind):
    _enabled()
    site = _managed(request.user, slug, kind)
    asset = get_object_or_404(site.images, pk=asset_id)
    return _image_response(asset)


def _public_image(slug, asset_id, kind):
    _enabled()
    if kind == "journal":
        owner = get_object_or_404(Journal, slug=slug, active=True, organisation__active=True)
        site = get_object_or_404(JournalSiteDraft.objects.select_related("current_release"), journal=owner,
                                 current_release__isnull=False)
    else:
        owner = get_object_or_404(Organisation, slug=slug, active=True)
        site = get_object_or_404(OrganisationPortalDraft.objects.select_related("current_release"), organisation=owner,
                                 current_release__isnull=False)
    release = site.current_release
    if release.site_id != site.pk:
        raise Http404
    sections = []
    try:
        pages = validate_pages(release.pages_config)
        if kind == "journal":
            if release.theme not in JournalSiteDraft.Theme.values or release.layout not in JournalSiteDraft.Layout.values:
                raise ValueError
            if release.design_config:
                validate_blueprint(release.design_config)
            if release.sections_config:
                sections = validate_journal_sections(release.sections_config)
        else:
            validate_blueprint(release.design_config)
            if release.sections_config:
                sections = validate_portal_sections(release.sections_config)
            if not _valid_snapshot(release.journals):
                raise ValueError
    except (ValueError, InvalidDesign, InvalidPortalSections, InvalidJournalSections, InvalidPages) as exc:
        raise Http404 from exc
    if not hero_reference_valid(site, release.hero_asset_id, release.hero_alt, release.hero_fit, release.hero_position):
        raise Http404
    if not images_valid(site, sections, release):
        raise Http404
    visible_images = {section["image_asset_id"] for section in sections
                      if section["visible"] and section.get("image_asset_id")}
    visible_images.update(page["image_asset_id"] for page in pages
                          if page["visible"] and page.get("image_asset_id"))
    if release.hero_asset_id != asset_id and str(asset_id) not in visible_images:
        raise Http404
    asset = get_object_or_404(site.images, pk=asset_id)
    return _image_response(asset)


@login_required
@never_cache
@require_POST
def upload_journal_image(request, slug):
    return _upload(request, slug, "journal")


@login_required
@never_cache
@require_POST
def select_journal_image(request, slug):
    return _select(request, slug, "journal")


@login_required
@never_cache
@require_POST
def clear_journal_image(request, slug):
    return _clear(request, slug, "journal")


@login_required
@never_cache
@require_POST
def delete_journal_image(request, slug):
    return _delete(request, slug, "journal")


@login_required
@never_cache
@require_GET
def private_journal_image(request, slug, asset_id):
    return _private_image(request, slug, asset_id, "journal")


@require_GET
def public_journal_image(request, slug, asset_id):
    return _public_image(slug, asset_id, "journal")


@login_required
@never_cache
@require_POST
def upload_portal_image(request, slug):
    return _upload(request, slug, "portal")


@login_required
@never_cache
@require_POST
def select_portal_image(request, slug):
    return _select(request, slug, "portal")


@login_required
@never_cache
@require_POST
def clear_portal_image(request, slug):
    return _clear(request, slug, "portal")


@login_required
@never_cache
@require_POST
def delete_portal_image(request, slug):
    return _delete(request, slug, "portal")


@login_required
@never_cache
@require_GET
def private_portal_image(request, slug, asset_id):
    return _private_image(request, slug, asset_id, "portal")


@require_GET
def public_portal_image(request, slug, asset_id):
    return _public_image(slug, asset_id, "portal")
