"""Narrow dispatch boundary for authorised local metadata and site tasks.

This module is not an authorisation boundary. Callers must first enforce
journal policy, role, applicable consent, quota and per-request confirmation.
Unknown tasks, providers or input fields are rejected instead of forwarded.
"""
from .ai_provider import request_metadata_suggestions
from .site_ai_provider import request_design_suggestions

METADATA_WRITING = "metadata_writing"
SITE_DESIGN = "site_design"
OPENAI = "openai"


class GatewayRejected(ValueError):
    """The proposed task, provider or data shape is not allowlisted."""


def run_ai_task(*, task, provider, model, api_key, payload):
    if provider != OPENAI or task not in (METADATA_WRITING, SITE_DESIGN):
        raise GatewayRejected("unsupported_task_or_provider")
    if task == SITE_DESIGN:
        if not isinstance(payload, dict) or set(payload) != {"brief"} or not isinstance(payload["brief"], str):
            raise GatewayRejected("unsupported_input_fields")
        return request_design_suggestions(api_key=api_key, model=model, brief=payload["brief"])
    if not isinstance(payload, dict) or set(payload) != {"title", "abstract"}:
        raise GatewayRejected("unsupported_input_fields")
    if not all(isinstance(payload[key], str) for key in ("title", "abstract")):
        raise GatewayRejected("invalid_input")
    return request_metadata_suggestions(api_key=api_key, model=model, title=payload["title"], abstract=payload["abstract"])
