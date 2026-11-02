"""Editable, bounded review forms for AI-proposed page layouts."""
from django import forms
from django.forms import BaseFormSet, formset_factory
from django.utils.translation import gettext_lazy as _

from .ai_provider import ProviderError
from .page_block_layout import GRID_ALIGNMENTS, GRID_COLUMNS
from .page_blocks import MAX_PAGE_BLOCKS
from .page_layout_ai_provider import validate_options


class LayoutSettingsForm(forms.Form):
    gap = forms.IntegerField(label=_("Gap between blocks (px)"), min_value=0, max_value=64)
    alignment = forms.ChoiceField(label=_("Alignment within a row"), choices=GRID_ALIGNMENTS)


class LayoutBlockForm(forms.Form):
    block_id = forms.CharField(widget=forms.HiddenInput, max_length=4)
    order = forms.IntegerField(label=_("Order"), min_value=1, max_value=MAX_PAGE_BLOCKS,
                               widget=forms.NumberInput(attrs={"data-layout-order":""}))
    grid_span = forms.IntegerField(label=_("Grid width (1–12 columns)"), min_value=1, max_value=GRID_COLUMNS,
                                   widget=forms.NumberInput(attrs={"data-layout-span":""}))


class BaseLayoutBlockFormSet(BaseFormSet):
    def __init__(self, *args, descriptors, **kwargs):
        self.descriptors = descriptors
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        if any(self.errors):
            return
        rows = [form.cleaned_data for form in self.forms]
        if (len(rows) != len(self.descriptors) or len({row["order"] for row in rows}) != len(rows) or
                len(self.initial) != len(rows) or
                any(row["block_id"] != initial["block_id"] for row, initial in zip(rows, self.initial))):
            raise forms.ValidationError(_("Keep each block once and use a different order number for each block."))
        placements = [{"id":row["block_id"], "grid_span":row["grid_span"]}
                      for row in sorted(rows, key=lambda row:row["order"])]
        try:
            # Validate the IDs independently of the editable page-level settings.
            self.placements = validate_options([{"gap":0, "alignment":"start", "blocks":placements}], self.descriptors)[0]["blocks"]
        except ProviderError as exc:
            raise forms.ValidationError(_("Keep each block once and use a different order number for each block.")) from exc


LayoutBlockFormSet = formset_factory(LayoutBlockForm, formset=BaseLayoutBlockFormSet, extra=0,
    max_num=MAX_PAGE_BLOCKS, absolute_max=MAX_PAGE_BLOCKS, validate_max=True)
