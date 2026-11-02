"""Human review of generated blocks before a private preview and application."""
from django import forms
from django.forms import BaseFormSet, formset_factory
from django.utils.translation import gettext_lazy as _

from .page_block_ai_provider import AI_KINDS
from .page_blocks import KINDS, MAX_PAGE_BLOCKS, validate_blocks
from .block_design import BlockDesignForm
from .page_block_layout import GridSpanForm


def validate_reviewed_blocks(blocks, available_slots):
    if type(available_slots) is not int or not 1 <= available_slots <= MAX_PAGE_BLOCKS:
        raise ValueError("invalid block capacity")
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= available_slots:
        raise ValueError("invalid block count")
    for block in blocks:
        if (not isinstance(block, dict) or set(block) - {"design", "grid_span"} != {"kind", "heading", "body", "visible", "link_label", "link_url"} or
                block.get("kind") not in AI_KINDS or block.get("link_label") != "" or block.get("link_url") != ""):
            raise ValueError("invalid reviewed block")
    return validate_blocks(blocks)


class ReviewedBlockForm(BlockDesignForm, GridSpanForm):
    kind = forms.ChoiceField(label=_("Block style"), choices=tuple(item for item in KINDS if item[0] in AI_KINDS))
    heading = forms.CharField(label=_("Block heading"), max_length=120, required=False)
    body = forms.CharField(label=_("Block text"), max_length=10000, required=False, widget=forms.Textarea(attrs={"rows":4}))
    visible = forms.BooleanField(label=_("Show this block when the page is published"), required=False)

    def clean(self):
        data = super().clean()
        if not self.errors and not (data.get("heading") or data.get("body")):
            raise forms.ValidationError(_("Add a heading or text to this block."))
        return data


class BaseReviewedBlockFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = []
        for index, form in enumerate(self.forms):
            data = getattr(form, "cleaned_data", {})
            if not data or data.get("DELETE"):
                continue
            order = data.get("ORDER")
            block = {key:data[key] for key in ("kind", "heading", "body", "visible")}
            block.update(link_label="", link_url="")
            if data.get("design_enabled"):
                block["design"] = data["design"]
            if data.get("grid_span") not in (None, 12):
                block["grid_span"] = data["grid_span"]
            rows.append(((order is None, order if order is not None else 0, index), block))
        try:
            self.reviewed_blocks = validate_reviewed_blocks([block for _, block in sorted(rows, key=lambda row:row[0])],
                                                            self.max_num)
        except ValueError as exc:
            raise forms.ValidationError(_("Keep at least one valid suggested block.")) from exc


ReviewedBlockFormSet = formset_factory(ReviewedBlockForm, formset=BaseReviewedBlockFormSet, extra=0,
    can_order=True, can_delete=True, max_num=MAX_PAGE_BLOCKS, absolute_max=MAX_PAGE_BLOCKS, validate_max=True)
