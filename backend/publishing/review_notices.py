"""Visibility of in-app review notices mirrors the private review-task boundary."""
from django.db.models import Exists, F, OuterRef

from .models import ContributorResponseRequest, Membership, ProductionAssignment, ReviewNotice


def visible_review_notices(user):
    if not user.is_authenticated or not user.is_active:
        return ReviewNotice.objects.none()
    active_role = Membership.objects.filter(journal_id=OuterRef("assignment__submission__journal_id"),
        user=user, role=Membership.Role.REVIEWER, active=True)
    return (ReviewNotice.objects.filter(
        Exists(active_role),
        recipient=user,
        assignment__reviewer=user,
        recipient_id=F("assignment__reviewer_id"),
        assignment__submission__journal__active=True,
        assignment__submission__journal__organisation__active=True,
        assignment__round__submission_id=F("assignment__submission_id"),
        assignment__round__version__submission_id=F("assignment__submission_id"),
    ).exclude(assignment__submission__author=user)
     .exclude(Exists(ContributorResponseRequest.objects.filter(
        change__submission_id=OuterRef("assignment__submission_id"), recipient=user)))
     .exclude(Exists(ProductionAssignment.objects.filter(
        draft__submission_id=OuterRef("assignment__submission_id"), producer=user)))
     .select_related("assignment__submission__journal", "assignment__round"))
