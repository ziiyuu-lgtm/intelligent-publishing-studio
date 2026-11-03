"""Only the requesting editor, with current manuscript access, sees these replies."""
from django.db.models import F, Q

from .models import ReviewAIReplyNotice
from .review_ai_consent import PROVIDER, PURPOSE
from .views import _editorial_submissions


def visible_review_ai_replies(user):
    if not user.is_active:
        return ReviewAIReplyNotice.objects.none()
    return ReviewAIReplyNotice.objects.filter(
        recipient=user, recipient__is_active=True,
        permission_request__requested_by_id=F('recipient_id'),
        permission_request__submission_id__in=_editorial_submissions(user).values('pk'),
        permission_request__review_round__submission_id=F('permission_request__submission_id'),
        permission_request__version__submission_id=F('permission_request__submission_id'),
        permission_request__review_round__version_id=F('permission_request__version_id'),
        permission_request__provider=PROVIDER, permission_request__purpose=PURPOSE,
        permission_request__status=F('outcome'), permission_request__responded_at__isnull=False,
    ).filter(
        Q(permission_request__subject='author', permission_request__assignment__isnull=True,
          permission_request__recipient_id=F('permission_request__submission__author_id')) |
        Q(permission_request__subject='reviewer', permission_request__assignment__isnull=False,
          permission_request__assignment__reviewer_id=F('permission_request__recipient_id'),
          permission_request__assignment__submission_id=F('permission_request__submission_id'),
          permission_request__assignment__round_id=F('permission_request__review_round_id'))
    ).select_related('permission_request__submission__journal__organisation',
        'permission_request__submission__author', 'permission_request__review_round__version',
        'permission_request__version', 'permission_request__recipient',
        'permission_request__assignment__round__version')
