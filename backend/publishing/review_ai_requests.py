"""Personal inbox entries and current-scope checks for review AI permission."""
from django.http import Http404
from .models import ReviewAIConsent, ReviewAIPermissionRequest, Submission
from .review_ai_consent import PROVIDER, PURPOSE, report_fingerprint, round_fingerprint
from .views import _author_can_view, _review


def current_request(round_, actor, subject, fingerprint, assignment=None, locked=False):
    if not round_ or not fingerprint:
        return None
    query = ReviewAIPermissionRequest.objects.select_for_update() if locked else ReviewAIPermissionRequest.objects
    return query.filter(submission_id=round_.submission_id, review_round=round_, version_id=round_.version_id,
        recipient=actor, subject=subject, assignment=assignment, provider=PROVIDER, purpose=PURPOSE,
        source_fingerprint=fingerprint).exclude(status=ReviewAIPermissionRequest.Status.OUTDATED).first()


def visible_entry(entry, actor):
    """Only expose an entry when its recipient still has the relevant role and scope."""
    if (not actor.is_active or entry.recipient_id != actor.pk or entry.provider != PROVIDER or entry.purpose != PURPOSE
            or entry.review_round.submission_id != entry.submission_id
            or entry.version.submission_id != entry.submission_id or entry.review_round.version_id != entry.version_id
            or entry.submission.status == Submission.Status.DRAFT):
        return False
    if entry.subject == ReviewAIConsent.Subject.AUTHOR:
        return not entry.assignment_id and _author_can_view(actor, entry.submission)
    if entry.subject != ReviewAIConsent.Subject.REVIEWER or not entry.assignment_id:
        return False
    if (entry.assignment.submission_id != entry.submission_id or entry.assignment.round_id != entry.review_round_id
            or entry.assignment.reviewer_id != actor.pk):
        return False
    try:
        _review(entry.assignment_id, actor)
    except Http404:
        return False
    return True


def entry_current(entry):
    if entry.status == ReviewAIPermissionRequest.Status.OUTDATED:
        return False
    if entry.subject == ReviewAIConsent.Subject.AUTHOR:
        fingerprint = round_fingerprint(entry.review_round)
    else:
        fingerprint = report_fingerprint(entry.assignment)
    return bool(fingerprint and fingerprint == entry.source_fingerprint)


def visible_entries(actor):
    if not actor.is_active:
        return []
    candidates = ReviewAIPermissionRequest.objects.filter(recipient=actor).select_related(
        'submission__journal__organisation', 'review_round__version', 'version__submission', 'assignment__round__version')
    return [entry for entry in candidates if visible_entry(entry, actor)]
