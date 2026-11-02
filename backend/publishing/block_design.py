"""Constrained but combinable visual settings for independent page blocks."""
import re

from django import forms
from django.utils.translation import gettext_lazy as _

from .page_design import LAYOUTS
from .site_design import contrast_text

ALIGNMENTS = (("left", _("Left")), ("center", _("Centred")), ("right", _("Right")))
DESIGN_FIELDS = {"layout", "width", "background", "accent", "alignment", "padding", "radius", "gap"}


def validate_block_design(value):
    if not isinstance(value, dict) or set(value) != DESIGN_FIELDS:
        raise ValueError("invalid design fields")
    if (not isinstance(value["layout"], str) or value["layout"] not in dict(LAYOUTS) or
            not isinstance(value["alignment"], str) or value["alignment"] not in dict(ALIGNMENTS)):
        raise ValueError("invalid design layout")
    for key, minimum, maximum in (("width", 40, 100), ("padding", 0, 64), ("radius", 0, 40), ("gap", 0, 64)):
        if type(value[key]) is not int or not minimum <= value[key] <= maximum:
            raise ValueError("invalid design dimension")
    for key in ("background", "accent"):
        if not isinstance(value[key], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value[key]):
            raise ValueError("invalid design colour")
    return {**value, "background":value["background"].lower(), "accent":value["accent"].lower()}


def block_appearance(value):
    clean = validate_block_design(value)
    return {**clean, "text":contrast_text(clean["background"])}


class BlockDesignForm(forms.Form):
    design_enabled = forms.BooleanField(label=_("Customise this block's visual design"), required=False)
    design_layout = forms.ChoiceField(label=_("Block image layout"), required=False, choices=(("", _("Choose a layout")), *LAYOUTS))
    design_width = forms.IntegerField(label=_("Block width (%)"), required=False, min_value=40, max_value=100,
        help_text=_("When the grid is enabled, this is a percentage of the block's assigned grid area."))
    design_background = forms.RegexField(label=_("Block background colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7, required=False,
                                         widget=forms.TextInput(attrs={"placeholder":"#ffffff", "data-block-colour-input":"true"}))
    design_accent = forms.RegexField(label=_("Block accent colour"), regex=r"^#[0-9a-fA-F]{6}$", max_length=7, required=False,
                                     widget=forms.TextInput(attrs={"placeholder":"#17483f", "data-block-colour-input":"true"}))
    design_alignment = forms.ChoiceField(label=_("Block text alignment"), required=False, choices=(("", _("Choose alignment")), *ALIGNMENTS))
    design_padding = forms.IntegerField(label=_("Inner spacing (px)"), required=False, min_value=0, max_value=64)
    design_radius = forms.IntegerField(label=_("Corner radius (px)"), required=False, min_value=0, max_value=40)
    design_gap = forms.IntegerField(label=_("Image and text gap (px)"), required=False, min_value=0, max_value=64)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        design = self.initial.get("design")
        if design:
            self.initial.update({"design_enabled":True, **{f"design_{key}":value for key, value in design.items()}})

    def clean(self):
        data = super().clean()
        if self.initial.get("design") and self.add_prefix("design_form_present") not in self.data:
            raise forms.ValidationError(_("Reload the block editor before changing a styled block."))
        if data.get("design_enabled"):
            try:
                data["design"] = validate_block_design({key:data.get(f"design_{key}") for key in DESIGN_FIELDS})
            except ValueError as exc:
                raise forms.ValidationError(_("Complete every custom block design setting.")) from exc
        return data
