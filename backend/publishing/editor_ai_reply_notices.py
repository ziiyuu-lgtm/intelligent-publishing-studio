"""Scoped access to historical replies to an editor's AI permission request."""
from django.db.models import F

from .editor_comparison_consent import PROVIDER, PURPOSE
from .models import EditorialDecision, EditorAIPermissionReplyNotice, ReviewRound
from .views import _editorial_submissions


def visible_editor_ai_reply_notices(user):
    if not user.is_active:
        return EditorAIPermissionReplyNotice.objects.none()
    return EditorAIPermissionReplyNotice.objects.filter(
        recipient=user, recipient__is_active=True,
        permission_request__requested_by_id=F('recipient_id'),
        permission_request__recipient_id=F('permission_request__submission__author_id'),
        permission_request__submission_id__in=_editorial_submissions(user).values('pk'),
        permission_request__review_round__submission_id=F('permission_request__submission_id'),
        permission_request__version__submission_id=F('permission_request__submission_id'),
        permission_request__review_round__version_id=F('permission_request__version_id'),
        permission_request__decision__submission_id=F('permission_request__submission_id'),
        permission_request__decision__round__submission_id=F('permission_request__submission_id'),
        permission_request__decision__round__version__submission_id=F('permission_request__submission_id'),
        permission_request__decision__round__status=ReviewRound.Status.CLOSED,
        permission_request__decision__round__number=F('permission_request__review_round__number') - 1,
        permission_request__decision__round__version__number=F('permission_request__version__number') - 1,
        permission_request__decision__outcome__in=(EditorialDecision.Outcome.MINOR, EditorialDecision.Outcome.MAJOR),
        permission_request__provider=PROVIDER, permission_request__purpose=PURPOSE,
        permission_request__status=F('outcome'), permission_request__responded_at__isnull=False,
    ).select_related('permission_request__submission__journal__organisation')
