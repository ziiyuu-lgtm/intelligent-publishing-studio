"""Safe, composable journal-site appearance blueprints and sample templates."""
import re
from django.utils.translation import gettext_lazy as _

COMPOSITIONS = (("classic", _("Classic")), ("editorial", _("Editorial")), ("split", _("Split")),
                ("immersive", _("Immersive")), ("minimal", _("Minimal")), ("grid", _("Grid")))
FONTS = (("scholarly", _("Scholarly serif")), ("modern", _("Modern sans")),
         ("literary", _("Literary serif")), ("technical", _("Technical sans")))
HERO_STYLES = (("solid", _("Bold colour")), ("soft", _("Soft background")))
ALIGNMENTS = (("left", _("Left")), ("center", _("Centred")))
DENSITIES = (("spacious", _("Spacious")), ("compact", _("Compact")))
CORNERS = (("soft", _("Rounded")), ("square", _("Square")))
SECTION_ORDERS = (("about_first", _("About first")), ("archive_first", _("Archive first")))
CHOICE_FIELDS = {"composition": COMPOSITIONS, "font": FONTS, "hero_style": HERO_STYLES,
                 "alignment": ALIGNMENTS, "density": DENSITIES, "corners": CORNERS,
                 "section_order": SECTION_ORDERS}
COLOR_FIELDS = ("primary", "surface", "background")
CONFIG_FIELDS = set(COLOR_FIELDS) | set(CHOICE_FIELDS) | {"show_feature"}


def blueprint(primary, surface, background, composition, font, hero_style="solid", alignment="left",
              density="spacious", corners="soft", section_order="about_first", show_feature=True):
    return {"primary": primary, "surface": surface, "background": background, "composition": composition,
            "font": font, "hero_style": hero_style, "alignment": alignment, "density": density,
            "corners": corners, "section_order": section_order, "show_feature": show_feature}


# These are starting points, not limits: colours and design settings can be edited independently.
SAMPLES = (
    {"id":"heritage", "name":_("Heritage Review"), "description":_("A timeless scholarly identity with a structured masthead."),
     "config":blueprint("#17483f", "#eaf2ed", "#fffefa", "classic", "scholarly")},
    {"id":"editorial", "name":_("Editorial Perspective"), "description":_("A bold, editorial presentation for research stories."),
     "config":blueprint("#8c3d35", "#f8ede8", "#fffaf5", "editorial", "literary", section_order="archive_first", corners="square")},
    {"id":"horizon", "name":_("Modern Horizon"), "description":_("A balanced split layout with a contemporary feel."),
     "config":blueprint("#304f8a", "#ecf2fc", "#ffffff", "split", "modern", hero_style="soft")},
    {"id":"frontier", "name":_("Digital Frontier"), "description":_("A confident, immersive style for technology journals."),
     "config":blueprint("#28365d", "#e9edf8", "#fbfbff", "immersive", "technical", alignment="center")},
    {"id":"insight", "name":_("Open Insight"), "description":_("An uncluttered, readable layout for open research."),
     "config":blueprint("#486038", "#eef2e6", "#fffffb", "minimal", "modern", hero_style="soft", show_feature=False)},
    {"id":"discovery", "name":_("Discovery Grid"), "description":_("An organised, modular look for multidisciplinary journals."),
     "config":blueprint("#6d4777", "#f3eaf4", "#fffaff", "grid", "technical", density="compact", section_order="archive_first")},
)


class InvalidDesign(ValueError):
    pass


def validate_blueprint(config):
    """Return a clean copy; reject markup, unknown keys and invalid design tokens."""
    if not isinstance(config, dict) or set(config) != CONFIG_FIELDS:
        raise InvalidDesign("invalid_fields")
    clean = {}
    for field in COLOR_FIELDS:
        value = config[field]
        if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            raise InvalidDesign("invalid_colour")
        clean[field] = value.lower()
    for field, choices in CHOICE_FIELDS.items():
        value = config[field]
        if not isinstance(value, str) or value not in dict(choices):
            raise InvalidDesign("invalid_choice")
        clean[field] = value
    if type(config["show_feature"]) is not bool:
        raise InvalidDesign("invalid_feature")
    clean["show_feature"] = config["show_feature"]
    return clean


def sample_by_id(sample_id):
    for sample in SAMPLES:
        if sample["id"] == sample_id:
            return {**sample, "config": validate_blueprint(sample["config"])}
    raise InvalidDesign("unknown_sample")


def contrast_text(background):
    """Choose black or white text using WCAG relative luminance."""
    values = [int(background[i:i+2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in values]
    luminance = 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
    return "#000000" if (luminance + 0.05) / 0.05 >= 1.05 / (luminance + 0.05) else "#ffffff"


def appearance_for(config):
    """Produce only validated values for CSS and accessible foreground colours."""
    config = validate_blueprint(config)
    hero_bg = config["primary"] if config["hero_style"] == "solid" else config["surface"]
    return {**config, "hero_bg":hero_bg, "hero_text":contrast_text(hero_bg),
            "page_text":contrast_text(config["background"]), "surface_text":contrast_text(config["surface"])}


def legacy_to_blueprint(theme, layout):
    palettes = {"forest": ("#18594e", "#edf5f0", "#ffffff"), "ink": ("#183d68", "#eef3fa", "#ffffff"),
                "terracotta": ("#823d27", "#fbf0e9", "#ffffff")}
    if theme not in palettes or layout not in ("classic", "magazine"):
        raise InvalidDesign("invalid_legacy_design")
    return blueprint(*palettes[theme], "editorial" if layout == "magazine" else "classic", "scholarly",
                     section_order="archive_first" if layout == "magazine" else "about_first")
