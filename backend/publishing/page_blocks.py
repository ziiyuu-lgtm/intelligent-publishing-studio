"""Validated, reorderable content blocks for independent site pages."""
from urllib.parse import urlsplit

from django import forms
from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.forms import BaseFormSet, formset_factory
from django.utils.translation import gettext_lazy as _

from .module_images import IMAGE_FIELDS, SectionImageForm, form_image_fields, validate_image_fields

MAX_PAGE_BLOCKS = 20
KINDS = (("text", _("Text")), ("image", _("Image and text")),
         ("callout", _("Highlight")), ("quote", _("Quotation")), ("link", _("Link")))
BLOCK_FIELDS = {"kind", "heading", "body", "visible", "link_label", "link_url"}


def validate_link(value):
    """Allow HTTPS or a local path; never fetch links on the server."""
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("invalid link")
    if not value:
        return ""
    if any(ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value):
        raise ValueError("invalid link")
    if value.startswith("/") and not value.startswith("//"):
        return value
    try:
        parsed = urlsplit(value)
        if (parsed.scheme.lower() != "https" or not parsed.hostname or
                parsed.username is not None or parsed.password is not None):
            raise ValueError("invalid link")
        URLValidator(schemes=["https"])(value)
    except (ValueError, ValidationError) as exc:
        raise ValueError("invalid link") from exc
    return value


def validate_blocks(blocks):
    if not isinstance(blocks, list) or len(blocks) > MAX_PAGE_BLOCKS:
        raise ValueError("invalid block count")
    result = []
    for item in blocks:
        if (not isinstance(item, dict) or not BLOCK_FIELDS.issubset(item) or
                set(item) - BLOCK_FIELDS - IMAGE_FIELDS):
            raise ValueError("invalid block fields")
        kind, heading, body, visible, label, url = (item[key] for key in
            ("kind", "heading", "body", "visible", "link_label", "link_url"))
        if (not isinstance(kind, str) or kind not in dict(KINDS) or
                not isinstance(heading, str) or len(heading) > 120 or
                not isinstance(body, str) or len(body) > 10000 or type(visible) is not bool or
                not isinstance(label, str) or len(label) > 120 or not isinstance(url, str)):
            raise ValueError("invalid block content")
        heading, body, label, url = heading.strip(), body.strip(), label.strip(), url.strip()
        image = validate_image_fields(item)
        if bool(label) != bool(url) or (kind == "link" and not url):
            raise ValueError("incomplete link")
        url = validate_link(url)
        if not (heading or body or image or url):
            raise ValueError("empty block")
        result.append({"kind":kind, "heading":heading, "body":body, "visible":visible,
                       "link_label":label, "link_url":url, **image})
    return result


class PageBlockForm(SectionImageForm):
    kind = forms.ChoiceField(label=_("Block style"), choices=(("", _("Choose a style")), *KINDS))
    heading = forms.CharField(label=_("Block heading"), max_length=120, required=False)
    body = forms.CharField(label=_("Block text"), max_length=10000, required=False,
                           widget=forms.Textarea(attrs={"rows":4}))
    link_label = forms.CharField(label=_("Link label"), max_length=120, required=False)
    link_url = forms.CharField(label=_("Link URL"), max_length=2048, required=False,
                               help_text=_("Use an HTTPS address or a site path beginning with / ."))
    visible = forms.BooleanField(label=_("Show this block when the page is published"), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["image_asset_id"].label = _("Block image")
        self.fields["image_alt"].label = _("Block image description")
        self.fields["image_fit"].label = _("Block image display")
        self.fields["image_position"].label = _("Block image focus")

    def clean(self):
        data = super().clean()
        label, url = data.get("link_label", ""), data.get("link_url", "")
        if bool(label) != bool(url) or (data.get("kind") == "link" and not url):
            raise forms.ValidationError(_("Enter both a link label and URL."))
        if url:
            try:
                validate_link(url)
            except ValueError:
                self.add_error("link_url", _("Use a valid HTTPS address or local site path."))
        if data.get("kind") and not (data.get("heading") or data.get("body") or data.get("image_asset_id") or url):
            raise forms.ValidationError(_("Add text, an image or a link to this block."))
        return data


class BasePageBlockFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        active = []
        for index, form in enumerate(self.forms):
            data = getattr(form, "cleaned_data", {})
            if not data or data.get("DELETE"):
                continue
            block = {**{key:data[key] for key in ("kind", "heading", "body", "visible", "link_label", "link_url")},
                     **form_image_fields(data)}
            order = data.get("ORDER")
            active.append(((order is None, order if order is not None else 0, index), block))
        try:
            self.cleaned_blocks = validate_blocks([block for _, block in sorted(active, key=lambda row:row[0])])
        except ValueError as exc:
            raise forms.ValidationError(_("Check the content and links in each block.")) from exc


PageBlockFormSet = formset_factory(PageBlockForm, formset=BasePageBlockFormSet, extra=1,
    can_order=True, can_delete=True, max_num=MAX_PAGE_BLOCKS, absolute_max=MAX_PAGE_BLOCKS * 2, validate_max=True)
