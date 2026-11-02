"""Narrow dispatch boundary for authorised local metadata and site tasks.

This module is not an authorisation boundary. Callers must first enforce
journal or publisher policy, role, applicable consent, quota and per-request confirmation.
Unknown tasks, providers or input fields are rejected instead of forwarded.
"""
from .ai_provider import request_metadata_suggestions
from .site_ai_provider import request_design_suggestions, request_publisher_design_suggestions
from .site_ai_modules import request_module_suggestions, validate_descriptors
from .ai_provider import ProviderError
from .site_design import InvalidDesign, validate_blueprint

METADATA_WRITING = "metadata_writing"
SITE_DESIGN = "site_design"
SITE_MODULES = "site_modules"
PUBLISHER_DESIGN = "publisher_design"
OPENAI = "openai"


class GatewayRejected(ValueError):
    """The proposed task, provider or data shape is not allowlisted."""


def run_ai_task(*, task, provider, model, api_key, payload):
    if provider != OPENAI or task not in (METADATA_WRITING, SITE_DESIGN, SITE_MODULES, PUBLISHER_DESIGN):
        raise GatewayRejected("unsupported_task_or_provider")
    if task == SITE_MODULES:
        if not isinstance(payload, dict) or set(payload) != {"brief", "modules"} or not isinstance(payload["brief"], str):
            raise GatewayRejected("unsupported_input_fields")
        try:
            modules = validate_descriptors(payload["modules"])
        except ProviderError as exc:
            raise GatewayRejected("invalid_modules") from exc
        return request_module_suggestions(api_key=api_key, model=model, brief=payload["brief"], modules=modules)
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
