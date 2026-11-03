"""Scope and fingerprints for source-bound review-report AI permissions."""
import json
from django.conf import settings
from django.utils.crypto import salted_hmac
from django.utils import timezone
from .models import ReviewAIConsent, ReviewAssignment

PROVIDER = 'openai'
PURPOSE = 'review-report-synthesis-v1'


def _digest(value):
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return salted_hmac('publishing.review-ai.consent.v1', payload,
                       secret=settings.IPS_REVIEW_AI_FINGERPRINT_KEY, algorithm='sha256').hexdigest()


def eligible_reports(round_):
    if not round_ or round_.version.submission_id != round_.submission_id:
        return []
    return list(ReviewAssignment.objects.filter(submission_id=round_.submission_id, round=round_,
        status=ReviewAssignment.Status.SUBMITTED, conflict=ReviewAssignment.Conflict.NONE)
        .select_related('reviewer', 'round__version').order_by('pk'))


def report_fingerprint(assignment):
    if (not assignment or assignment.round.submission_id != assignment.submission_id or
            assignment.round.version.submission_id != assignment.submission_id or
            assignment.status != ReviewAssignment.Status.SUBMITTED or
            assignment.conflict != ReviewAssignment.Conflict.NONE):
        return None
    return _digest({'purpose': PURPOSE, 'provider': PROVIDER, 'submission': str(assignment.submission_id),
        'round': assignment.round_id, 'version': assignment.round.version_id, 'assignment': str(assignment.pk),
        'reviewer': assignment.reviewer_id, 'status': assignment.status, 'conflict': assignment.conflict,
        'recommendation': assignment.recommendation, 'comments': assignment.comments})


def round_fingerprint(round_, reports=None):
    reports = eligible_reports(round_) if reports is None else reports
    if not round_ or not reports:
        return None
    fingerprints = []
    for report in sorted(reports, key=lambda item: str(item.pk)):
        if report.round_id != round_.pk or report.submission_id != round_.submission_id:
            return None
        value = report_fingerprint(report)
        if value is None:
            return None
        fingerprints.append(value)
    return _digest({'purpose': PURPOSE, 'provider': PROVIDER, 'submission': str(round_.submission_id),
                    'round': round_.pk, 'version': round_.version_id, 'reports': fingerprints})


def existing_author(round_, author, locked=False):
    query = ReviewAIConsent.objects.select_for_update() if locked else ReviewAIConsent.objects
    return query.filter(review_round=round_, subject=ReviewAIConsent.Subject.AUTHOR, granted_by=author,
                        provider=PROVIDER, purpose=PURPOSE, ended_at__isnull=True).first()


def existing_reviewer(assignment, reviewer, locked=False):
    query = ReviewAIConsent.objects.select_for_update() if locked else ReviewAIConsent.objects
    return query.filter(assignment=assignment, subject=ReviewAIConsent.Subject.REVIEWER, granted_by=reviewer,
                        provider=PROVIDER, purpose=PURPOSE, ended_at__isnull=True).first()


def supersede_on_report_change(assignment, *, include_reviewer=False):
    """Permanently end affected grants inside the existing submission transaction.

    Call when a report enters or leaves the eligible set or its source changes.
    Fingerprint checks additionally fail closed on unexpected source changes.
    """
    base = ReviewAIConsent.objects.filter(submission_id=assignment.submission_id,
        review_round_id=assignment.round_id, provider=PROVIDER, purpose=PURPOSE, ended_at__isnull=True)
    now = timezone.now()
    base.filter(subject=ReviewAIConsent.Subject.AUTHOR).update(
        ended_at=now, end_reason=ReviewAIConsent.EndReason.SUPERSEDED)
    if include_reviewer:
        base.filter(subject=ReviewAIConsent.Subject.REVIEWER, assignment=assignment).update(
            ended_at=now, end_reason=ReviewAIConsent.EndReason.SUPERSEDED)


def author_active(consent, round_, reports=None):
    current = round_fingerprint(round_, reports)
    return bool(consent and current and consent.subject == ReviewAIConsent.Subject.AUTHOR and
        consent.provider == PROVIDER and consent.purpose == PURPOSE and not consent.ended_at and
        consent.assignment_id is None and consent.submission_id == round_.submission_id and
        consent.review_round_id == round_.pk and consent.version_id == round_.version_id and
        consent.granted_by_id == round_.submission.author_id and consent.source_fingerprint == current)


def reviewer_active(consent, assignment):
    current = report_fingerprint(assignment)
    return bool(consent and current and consent.subject == ReviewAIConsent.Subject.REVIEWER and
        consent.provider == PROVIDER and consent.purpose == PURPOSE and not consent.ended_at and
        consent.assignment_id == assignment.pk and consent.submission_id == assignment.submission_id and
        consent.review_round_id == assignment.round_id and consent.version_id == assignment.round.version_id and
        consent.granted_by_id == assignment.reviewer_id and consent.source_fingerprint == current)
