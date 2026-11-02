"""Private author notices are visible only through current submission ownership."""
from django.db.models import F, Q
from .models import AuthorNotice, ContributorChangeRequest


def visible_author_notices(user):
    if not user.is_authenticated or not user.is_active:
        return AuthorNotice.objects.none()
    valid_sources = (Q(kind=AuthorNotice.Kind.SUBMITTED) |
        Q(kind=AuthorNotice.Kind.DECISION, decision__submission_id=F("submission_id"),
          decision__round__submission_id=F("submission_id"),
          decision__round__version__submission_id=F("submission_id")) |
        Q(kind=AuthorNotice.Kind.AUTHOR_CHANGE, author_change__submission_id=F("submission_id"),
          author_change__requested_by_id=F("recipient_id"),
          author_change__status__in=[ContributorChangeRequest.Status.APPROVED, ContributorChangeRequest.Status.DECLINED]))
    return (AuthorNotice.objects.filter(valid_sources, recipient=user, submission__author=user,
        submission__journal__active=True, submission__journal__organisation__active=True)
        .exclude(submission__status="draft").select_related("submission__journal"))
