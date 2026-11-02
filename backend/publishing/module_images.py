"""Validated, site-scoped image references for page modules."""
import uuid

from django import forms
from django.utils.translation import gettext_lazy as _

from .models import (HERO_FIT_CHOICES, HERO_POSITION_CHOICES, JournalSiteDraft, JournalSiteRelease,
                     OrganisationPortalDraft, OrganisationPortalRelease, SiteModuleImageReference)
from .site_images import asset_is_intact

IMAGE_FIELDS = {"image_asset_id", "image_alt", "image_fit", "image_position"}


def validate_image_fields(item):
    """Accept either no image fields, or the complete, strictly typed set."""
    present = IMAGE_FIELDS.intersection(item)
    if not present:
        return {}
    if present != IMAGE_FIELDS:
        raise ValueError("incomplete image")
    asset_id, alt, fit, position = (item[k] for k in ("image_asset_id", "image_alt", "image_fit", "image_position"))
    try:
        if not isinstance(asset_id, str) or str(uuid.UUID(asset_id)) != asset_id:
            raise ValueError("invalid image id")
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("invalid image id") from exc
    if not isinstance(alt, str) or not alt.strip() or len(alt) > 200:
        raise ValueError("invalid image description")
    if (not isinstance(fit, str) or not isinstance(position, str) or
            fit not in dict(HERO_FIT_CHOICES) or position not in dict(HERO_POSITION_CHOICES)):
        raise ValueError("invalid image display")
    return {"image_asset_id":asset_id, "image_alt":alt.strip(), "image_fit":fit, "image_position":position}


def image_choices(site):
    return [(str(asset.pk), asset.default_alt) for asset in site.images.defer("image_data")]


class SectionImageForm(forms.Form):
    image_asset_id = forms.ChoiceField(label=_("Module image"), required=False, choices=())
    image_alt = forms.CharField(label=_("Module image description"), max_length=200, required=False)
    image_fit = forms.ChoiceField(label=_("Module image display"), required=False,
                                  choices=(("", _("Default: crop to fill")), *HERO_FIT_CHOICES))
    image_position = forms.ChoiceField(label=_("Module image focus"), required=False,
                                       choices=(("", _("Default: centre")), *HERO_POSITION_CHOICES))

    def __init__(self, *args, image_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image_asset_id"].choices = [("", _("No image")), *image_choices]

    def clean(self):
        data = super().clean()
        if data.get("image_asset_id") and not data.get("image_alt", "").strip():
            self.add_error("image_alt", _("Provide a description for the selected module image."))
        return data


def form_image_fields(data):
    if not data.get("image_asset_id"):
        return {}
    return {"image_asset_id":data["image_asset_id"], "image_alt":data["image_alt"],
            "image_fit":data.get("image_fit") or "cover", "image_position":data.get("image_position") or "center"}


def _owner_key(owner):
    keys = {JournalSiteDraft:"journal_draft", JournalSiteRelease:"journal_release",
            OrganisationPortalDraft:"portal_draft", OrganisationPortalRelease:"portal_release"}
    return keys[type(owner)]


def image_ids(sections):
    return {item["image_asset_id"] for item in sections if item.get("image_asset_id")}


def images_valid(site, sections, owner=None, check_references=True, pages=None):
    """Reject foreign, damaged or unprotected images, including hidden content."""
    if pages is None:
        pages = owner.pages_config if owner is not None else site.pages_config
    try:
        if not isinstance(sections, list) or not isinstance(pages, list):
            return False
        ids = image_ids(sections) | image_ids(pages)
    except (TypeError, AttributeError):
        return False
    if check_references:
        if owner is None:
            return False
        referenced = {str(value) for value in owner.module_image_references.values_list("asset_id", flat=True)}
        if ids != referenced:
            return False
    assets = list(site.images.filter(pk__in=ids))
    return len(assets) == len(ids) and all(asset_is_intact(asset) for asset in assets)


def sync_image_references(site, owner, sections, pages=None):
    """Run inside the site lock and transaction that saves the site snapshot."""
    if pages is None:
        pages = owner.pages_config
    if not images_valid(site, sections, check_references=False, pages=pages):
        raise ValueError("unavailable image")
    ids = image_ids(sections) | image_ids(pages)
    owner_key = _owner_key(owner)
    SiteModuleImageReference.objects.filter(**{owner_key:owner}).exclude(asset_id__in=ids).delete()
    existing = {str(value) for value in owner.module_image_references.values_list("asset_id", flat=True)}
    SiteModuleImageReference.objects.bulk_create([
        SiteModuleImageReference(asset_id=asset_id, **{owner_key:owner}) for asset_id in ids - existing
    ])
