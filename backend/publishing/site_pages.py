"""Validated independent pages for local fictional journal and publisher sites."""
import re

from django import forms
from django.forms import BaseFormSet, formset_factory
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from .module_images import IMAGE_FIELDS, SectionImageForm, form_image_fields, validate_image_fields
from .page_blocks import validate_blocks
from .page_block_layout import validate_block_layout
from .page_design import ALIGNMENTS, LAYOUTS, InvalidPageDesign, validate_page_design

MAX_PAGES = 20
MAX_BODY_LENGTH = 20000
PAGE_FIELDS = {"slug", "title", "body", "visible"}
SLUG_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


class InvalidPages(ValueError):
    pass


def validate_pages(pages):
    if not isinstance(pages, list) or len(pages) > MAX_PAGES:
        raise InvalidPages("invalid_count")
    result, slugs = [], set()
    for item in pages:
        if (not isinstance(item, dict) or not PAGE_FIELDS.issubset(item) or
                set(item) - PAGE_FIELDS - IMAGE_FIELDS - {"design", "blocks", "block_layout"}):
            raise InvalidPages("invalid_fields")
        slug, title, body, visible = (item[key] for key in ("slug", "title", "body", "visible"))
        if not isinstance(slug, str) or len(slug) > 60 or not SLUG_PATTERN.fullmatch(slug) or slug in slugs:
            raise InvalidPages("invalid_slug")
        if (not isinstance(title, str) or not title.strip() or len(title) > 120 or
                not isinstance(body, str) or len(body) > MAX_BODY_LENGTH or type(visible) is not bool):
            raise InvalidPages("invalid_content")
        try:
            image = validate_image_fields(item)
        except ValueError as exc:
            raise InvalidPages("invalid_image") from exc
        try:
            design = {"design":validate_page_design(item["design"])} if "design" in item else {}
        except InvalidPageDesign as exc:
            raise InvalidPages("invalid_design") from exc
        try:
            blocks = {"blocks":validate_blocks(item["blocks"])} if "blocks" in item else {}
        except ValueError as exc:
            raise InvalidPages("invalid_blocks") from exc
        try:
            layout = {"block_layout":validate_block_layout(item["block_layout"])} if "block_layout" in item else {}
        except ValueError as exc:
            raise InvalidPages("invalid_block_layout") from exc
        slugs.add(slug)
        result.append({"slug":slug, "title":title.strip(), "body":body.strip(), "visible":visible, **image, **design, **blocks, **layout})
    return result


class SitePageForm(SectionImageForm):
    slug = forms.RegexField(label=_("Page URL name"), regex=SLUG_PATTERN, max_length=60,
        help_text=_("Use lowercase letters, numbers and hyphens, for example author-guidelines."))
    title = forms.CharField(label=_("Page title"), max_length=120)
    body = forms.CharField(label=_("Page text"), max_length=MAX_BODY_LENGTH, required=False,
                           widget=forms.Textarea(attrs={"rows":6}))
    visible = forms.BooleanField(label=_("Show this page publicly after publishing"), required=False)
    design_enabled = forms.BooleanField(label=_("Customise this page's design"), required=False)
    design_layout = forms.ChoiceField(label=_("Page layout"), required=False, choices=(("", _("Choose a layout")), *LAYOUTS))
    design_width = forms.IntegerField(label=_("Content width (%)"), required=False, min_value=55, max_value=100)
    design_background = forms.RegexField(label=_("Page background colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7, required=False,
                                         widget=forms.TextInput(attrs={"placeholder":"#ffffff"}))
    design_accent = forms.RegexField(label=_("Page accent colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7, required=False,
                                     widget=forms.TextInput(attrs={"placeholder":"#17483f"}))
    design_alignment = forms.ChoiceField(label=_("Page text alignment"), required=False, choices=(("", _("Choose alignment")), *ALIGNMENTS))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image_asset_id"].label = _("Page image")
        self.fields["image_alt"].label = _("Page image description")
        self.fields["image_fit"].label = _("Page image display")
        self.fields["image_position"].label = _("Page image focus")
        design = self.initial.get("design")
        if design:
            self.initial.update({"design_enabled":True, **{f"design_{key}":value for key, value in design.items()}})

    def clean(self):
        data = super().clean()
        if self.initial.get("design") and self.add_prefix("design_form_present") not in self.data:
            raise forms.ValidationError(_("Reload the page editor before changing a styled page."))
        if data.get("design_enabled"):
            try:
                data["design"] = validate_page_design({key:data.get(f"design_{key}")
                    for key in ("layout", "width", "background", "accent", "alignment")})
            except InvalidPageDesign:
                raise forms.ValidationError(_("Complete every custom page design setting."))
        return data


class BaseSitePageFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active = []
        for index, form in enumerate(self.forms):
            data = getattr(form, "cleaned_data", {})
            if not data or data.get("DELETE"):
                continue
            order = data.get("ORDER")
            page = {**{key:data[key] for key in ("slug", "title", "body", "visible")}, **form_image_fields(data)}
            if data.get("design_enabled"):
                page["design"] = data["design"]
            if "blocks" in form.initial:
                page["blocks"] = form.initial["blocks"]
            if "block_layout" in form.initial:
                page["block_layout"] = form.initial["block_layout"]
            active.append(((order is None, order if order is not None else 0, index), page))
        try:
            self.cleaned_pages = validate_pages([page for _, page in sorted(active, key=lambda row:row[0])])
        except InvalidPages as exc:
            raise forms.ValidationError(_("Each page needs a unique URL name and a title.")) from exc


SitePageFormSet = formset_factory(SitePageForm, formset=BaseSitePageFormSet, extra=1,
    can_order=True, can_delete=True, max_num=MAX_PAGES, absolute_max=MAX_PAGES * 2, validate_max=True)


def page_navigation(pages, kind, slug, preview):
    route = ("preview_journal_page" if kind == "journal" else "preview_publisher_page") if preview else (
        "public_journal_page" if kind == "journal" else "public_publisher_page")
    return [{"title":page["title"], "url":reverse(route, args=[slug, page["slug"]])}
            for page in pages if page["visible"]]
