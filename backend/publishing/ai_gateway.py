"""Narrow dispatch boundary for the already-authorised local metadata task.

This module is not an authorisation boundary. Callers must first enforce
journal policy, role, exact author consent, quota and per-request confirmation.
Unknown tasks, providers or input fields are rejected instead of forwarded.
"""
from .ai_provider import request_metadata_suggestions

METADATA_WRITING = "metadata_writing"
OPENAI = "openai"


class GatewayRejected(ValueError):
    """The proposed task, provider or data shape is not allowlisted."""


def run_ai_task(*, task, provider, model, api_key, payload):
    if task != METADATA_WRITING or provider != OPENAI:
        raise GatewayRejected("unsupported_task_or_provider")
    if not isinstance(payload, dict) or set(payload) != {"title", "abstract"}:
        raise GatewayRejected("unsupported_input_fields")
    if not all(isinstance(payload[key], str) for key in ("title", "abstract")):
        raise GatewayRejected("invalid_input")
    return request_metadata_suggestions(api_key=api_key, model=model, title=payload["title"], abstract=payload["abstract"])
