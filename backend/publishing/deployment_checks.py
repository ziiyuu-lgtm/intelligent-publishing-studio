"""Prevent the incomplete candidate from being mistaken for a deployable app."""
from django.conf import settings
from django.core.checks import Error, Tags, register


@register(Tags.security, deploy=True)
def release_candidate_is_blocked(app_configs, **kwargs):
    if getattr(settings, "IPS_RELEASE_CANDIDATE_BLOCKED", False):
        return [Error(
            "The release candidate is deliberately blocked and is not ready for real users or data.",
            hint="Complete and independently verify the release requirements before removing the block.",
            id="publishing.E001",
        )]
    return []
