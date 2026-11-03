"""Current, source-bound editor requests for an author's AI choice."""
from .editor_comparison_consent import PROVIDER, PURPOSE, source_fingerprint
from .models import EditorComparisonPermissionRequest


def current_permission_request(current, decision):
    if not current or not decision:
        return None
    return EditorComparisonPermissionRequest.objects.filter(
        submission_id=current.submission_id, review_round=current, version_id=current.version_id, decision=decision,
        recipient_id=current.submission.author_id, provider=PROVIDER, purpose=PURPOSE,
        source_fingerprint=source_fingerprint(current, decision),
    ).first()
