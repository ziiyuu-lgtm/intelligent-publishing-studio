"""Editable page styles with bounded layout tokens and arbitrary colours."""
import re

from django.utils.translation import gettext_lazy as _

from .site_design import contrast_text

LAYOUTS = (("stack", _("Image above text")), ("image_left", _("Image to the left")),
           ("image_right", _("Image to the right")))
ALIGNMENTS = (("left", _("Left")), ("center", _("Centred")))
DESIGN_FIELDS = {"layout", "width", "background", "accent", "alignment"}


class InvalidPageDesign(ValueError):
    pass


def validate_page_design(value):
    if not isinstance(value, dict) or set(value) != DESIGN_FIELDS:
        raise InvalidPageDesign("invalid_fields")
    if (not isinstance(value["layout"], str) or value["layout"] not in dict(LAYOUTS) or
            not isinstance(value["alignment"], str) or value["alignment"] not in dict(ALIGNMENTS) or
            type(value["width"]) is not int or not 55 <= value["width"] <= 100):
        raise InvalidPageDesign("invalid_layout")
    for key in ("background", "accent"):
        if not isinstance(value[key], str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value[key]):
            raise InvalidPageDesign("invalid_colour")
    return {**value, "background":value["background"].lower(), "accent":value["accent"].lower()}


def page_appearance(value):
    clean = validate_page_design(value)
    return {**clean, "text":contrast_text(clean["background"]), "accent_text":contrast_text(clean["accent"])}
