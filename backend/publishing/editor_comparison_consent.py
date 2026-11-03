"""Shared source-bound permission checks for editor AI comparison."""
from .editor_revision_comparison_provider import PROMPT_VERSION, fingerprint
from .models import EditorRevisionComparisonConsent

PROVIDER = "openai"
PURPOSE = PROMPT_VERSION


def source_fingerprint(current, decision):
    return fingerprint(round_id=current.pk, version_id=current.version_id, decision_id=decision.pk,
                       decision_text=decision.author_message, response_text=current.version.author_response)


def active_consent(current, decision):
    if not current or not decision:
        return None
    return EditorRevisionComparisonConsent.objects.filter(
        submission_id=current.submission_id, review_round=current, version_id=current.version_id, decision=decision,
        granted_by_id=current.submission.author_id, granted_by__is_active=True, provider=PROVIDER, purpose=PURPOSE,
        source_fingerprint=source_fingerprint(current, decision), ended_at__isnull=True,
    ).first()
