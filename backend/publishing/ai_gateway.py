"""Narrow dispatch boundary for authorised local metadata and site tasks.

This module is not an authorisation boundary. Callers must first enforce
journal or publisher policy, role, applicable consent, quota and per-request confirmation.
Unknown tasks, providers or input fields are rejected instead of forwarded.
"""
from .ai_provider import request_metadata_suggestions
from .site_ai_provider import request_design_suggestions, request_publisher_design_suggestions
from .site_ai_modules import request_module_suggestions, validate_descriptors
from .portal_ai_modules import (request_module_suggestions as request_publisher_module_suggestions,
                                validate_descriptors as validate_publisher_descriptors)
from .ai_provider import ProviderError
from .site_design import InvalidDesign, validate_blueprint
from .page_ai_provider import request_page_suggestions
from .page_block_ai_provider import MAX_OPTIONS as MAX_BLOCK_OPTIONS, request_block_suggestions
from .page_layout_ai_provider import (MAX_OPTIONS as MAX_LAYOUT_OPTIONS, request_layout_suggestions,
                                      validate_descriptors as validate_layout_descriptors)
from .page_blocks import MAX_PAGE_BLOCKS
from .revision_plan_provider import request_revision_plan

METADATA_WRITING = "metadata_writing"
REVISION_PLAN = "revision_plan"
SITE_DESIGN = "site_design"
SITE_MODULES = "site_modules"
PUBLISHER_DESIGN = "publisher_design"
PUBLISHER_MODULES = "publisher_modules"
SITE_PAGE = "site_page"
PUBLISHER_PAGE = "publisher_page"
SITE_PAGE_BLOCKS = "site_page_blocks"
PUBLISHER_PAGE_BLOCKS = "publisher_page_blocks"
SITE_PAGE_LAYOUT = "site_page_layout"
PUBLISHER_PAGE_LAYOUT = "publisher_page_layout"
OPENAI = "openai"


class GatewayRejected(ValueError):
    """The proposed task, provider or data shape is not allowlisted."""


def run_ai_task(*, task, provider, model, api_key, payload):
    if provider != OPENAI or task not in (METADATA_WRITING, REVISION_PLAN, SITE_DESIGN, SITE_MODULES, PUBLISHER_DESIGN, PUBLISHER_MODULES, SITE_PAGE, PUBLISHER_PAGE, SITE_PAGE_BLOCKS, PUBLISHER_PAGE_BLOCKS, SITE_PAGE_LAYOUT, PUBLISHER_PAGE_LAYOUT):
        raise GatewayRejected("unsupported_task_or_provider")
    if task == REVISION_PLAN:
        if (not isinstance(payload, dict) or set(payload) != {"comment", "notes"} or
                not all(isinstance(payload[key], str) for key in ("comment", "notes"))):
            raise GatewayRejected("unsupported_input_fields")
        return request_revision_plan(api_key=api_key, model=model, comment=payload["comment"], notes=payload["notes"])
    if task in (SITE_PAGE_LAYOUT, PUBLISHER_PAGE_LAYOUT):
        if (not isinstance(payload, dict) or set(payload) != {"brief", "blocks", "requested_options"} or
                not isinstance(payload["brief"], str) or type(payload["requested_options"]) is not int or
                not 1 <= payload["requested_options"] <= MAX_LAYOUT_OPTIONS):
            raise GatewayRejected("unsupported_input_fields")
        try:
            descriptors = validate_layout_descriptors(payload["blocks"])
        except ProviderError as exc:
            raise GatewayRejected("invalid_blocks") from exc
        return request_layout_suggestions(api_key=api_key, model=model, brief=payload["brief"],
            blocks=descriptors, requested_options=payload["requested_options"],
            site_kind="journal" if task == SITE_PAGE_LAYOUT else "publisher")
    if task in (SITE_PAGE_BLOCKS, PUBLISHER_PAGE_BLOCKS):
        if (not isinstance(payload, dict) or set(payload) != {"brief", "available_slots", "requested_options"} or
                not isinstance(payload["brief"], str) or type(payload["available_slots"]) is not int or
                not 1 <= payload["available_slots"] <= MAX_PAGE_BLOCKS or
                type(payload["requested_options"]) is not int or not 1 <= payload["requested_options"] <= MAX_BLOCK_OPTIONS):
            raise GatewayRejected("unsupported_input_fields")
        return request_block_suggestions(api_key=api_key, model=model, brief=payload["brief"],
            available_slots=payload["available_slots"], requested_options=payload["requested_options"],
            site_kind="journal" if task == SITE_PAGE_BLOCKS else "publisher")
    if task in (SITE_PAGE, PUBLISHER_PAGE):
        if (not isinstance(payload, dict) or set(payload) != {"brief", "has_image"} or
                not isinstance(payload["brief"], str) or type(payload["has_image"]) is not bool):
            raise GatewayRejected("unsupported_input_fields")
        return request_page_suggestions(api_key=api_key, model=model, brief=payload["brief"],
            has_image=payload["has_image"], site_kind="journal" if task == SITE_PAGE else "publisher")
    if task in (SITE_MODULES, PUBLISHER_MODULES):
        if not isinstance(payload, dict) or set(payload) != {"brief", "modules"} or not isinstance(payload["brief"], str):
            raise GatewayRejected("unsupported_input_fields")
        try:
            validator = validate_descriptors if task == SITE_MODULES else validate_publisher_descriptors
            modules = validator(payload["modules"])
        except ProviderError as exc:
            raise GatewayRejected("invalid_modules") from exc
        adapter = request_module_suggestions if task == SITE_MODULES else request_publisher_module_suggestions
        return adapter(api_key=api_key, model=model, brief=payload["brief"], modules=modules)
    if task in (SITE_DESIGN, PUBLISHER_DESIGN):
        if not isinstance(payload, dict) or set(payload) != {"brief", "appearance"} or not isinstance(payload["brief"], str):
            raise GatewayRejected("unsupported_input_fields")
        try:
            appearance = validate_blueprint(payload["appearance"])
        except InvalidDesign as exc:
            raise GatewayRejected("invalid_appearance") from exc
        adapter = request_design_suggestions if task == SITE_DESIGN else request_publisher_design_suggestions
        return adapter(api_key=api_key, model=model, brief=payload["brief"], appearance=appearance)
    if not isinstance(payload, dict) or set(payload) != {"title", "abstract"}:
        raise GatewayRejected("unsupported_input_fields")
    if not all(isinstance(payload[key], str) for key in ("title", "abstract")):
        raise GatewayRejected("invalid_input")
    return request_metadata_suggestions(api_key=api_key, model=model, title=payload["title"], abstract=payload["abstract"])
