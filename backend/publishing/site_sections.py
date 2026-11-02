"""Validated page modules for fictional local journal-site drafts."""
from django.utils.translation import gettext_lazy as _

MAX_SECTIONS = 12
MAX_HEADING_LENGTH = 120
MAX_BODY_LENGTH = 2000
SECTION_CHOICES = (
    ("about", _("About the journal")),
    ("archive", _("Articles and issues")),
    ("scope", _("Aims and scope")),
    ("editorial_board", _("Editorial board")),
    ("policies", _("Editorial policies")),
    ("authors", _("For authors")),
    ("special_issues", _("Special issues")),
    ("announcement", _("News and announcements")),
    ("contact", _("Contact information")),
    ("custom", _("Custom section")),
)
SECTION_FIELDS = {"kind", "heading", "body", "visible"}
UNIQUE_KINDS = {"about", "archive"}


class InvalidSections(ValueError):
    pass


def validate_sections(sections):
    """Validate the complete snapshot before storing or rendering it."""
    if not isinstance(sections, list) or not 1 <= len(sections) <= MAX_SECTIONS:
        raise InvalidSections("invalid_count")
    result, seen = [], set()
    for item in sections:
        if not isinstance(item, dict) or set(item) != SECTION_FIELDS:
            raise InvalidSections("invalid_fields")
        kind, heading, body, visible = (item[k] for k in ("kind", "heading", "body", "visible"))
        if not isinstance(kind, str) or kind not in dict(SECTION_CHOICES):
            raise InvalidSections("invalid_kind")
        if kind in UNIQUE_KINDS:
            if kind in seen:
                raise InvalidSections("duplicate_kind")
            seen.add(kind)
        if not isinstance(heading, str) or len(heading) > MAX_HEADING_LENGTH or not isinstance(body, str) or len(body) > MAX_BODY_LENGTH:
            raise InvalidSections("invalid_text")
        heading, body = heading.strip(), body.strip()
        if kind == "custom" and not heading:
            raise InvalidSections("custom_heading_required")
        if type(visible) is not bool:
            raise InvalidSections("invalid_visibility")
        result.append({"kind":kind, "heading":heading, "body":body, "visible":visible})
    if not any(item["visible"] for item in result):
        raise InvalidSections("no_visible_sections")
    return result


def default_sections(site):
    """Represent an older two-section site without changing its stored draft."""
    sections = [{"kind":"about", "heading":"", "body":"", "visible":True},
                {"kind":"archive", "heading":"", "body":"", "visible":True}]
    archive_first = (site.design_config.get("section_order") == "archive_first" if site.design_config
                     else site.layout == "magazine")
    return list(reversed(sections)) if archive_first else sections


def visible_sections(sections):
    return [{**item, "title":item["heading"] or dict(SECTION_CHOICES)[item["kind"]]}
            for item in validate_sections(sections) if item["visible"]]
