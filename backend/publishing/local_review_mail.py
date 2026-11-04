"""Fictional in-memory invitation simulations; this code never uses SMTP.

The transaction and recipient checks are a test foundation, not a complete
production delivery system. No real email address or manuscript may be used.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.core.mail.backends.locmem import EmailBackend
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .models import LocalReviewMailJob, ReviewAssignment, ReviewNotice, ReviewRound, Submission
from .review_notices import visible_review_notices


def _enabled():
    return bool(settings.DEBUG and getattr(settings, "IPS_LOCAL_REVIEW_MAIL_ENABLED", False))


def _fictional_address(value):
    if not isinstance(value, str) or len(value) > 254 or value != value.strip():
        return None
    try:
        validate_email(value)
    except ValidationError:
        return None
    domain = value.rsplit("@", 1)[-1].lower()
    if not domain.endswith(".invalid"):
        return None
    return value


def _recipient_digest(address):
    return salted_hmac("ips.local_review_mail.recipient", address,
                       secret=settings.IPS_LOCAL_REVIEW_MAIL_KEY, algorithm="sha256").hexdigest()


def enqueue_invitation(notice):
    """Called inside the invitation transaction. Invalid addresses are ignored."""
    if not _enabled() or notice.kind != ReviewNotice.Kind.INVITATION:
        return None
    if notice.recipient_id != notice.assignment.reviewer_id:
        return None
    address = _fictional_address(notice.recipient.email)
    if not address:
        return None
    job, _ = LocalReviewMailJob.objects.get_or_create(
        notice=notice, defaults={"recipient_digest": _recipient_digest(address)},
    )
    return job


def simulate_invitation(job_id):
    """Simulate one job at most once; never contacts an external mail server."""
    if not _enabled():
        return "disabled"
    with transaction.atomic():
        job = LocalReviewMailJob.objects.select_for_update().get(pk=job_id)
        if job.status != LocalReviewMailJob.Status.PENDING:
            return "unchanged"
        notice = ReviewNotice.objects.select_related(
            "recipient", "assignment__round", "assignment__submission",
        ).get(pk=job.notice_id)
        assignment = notice.assignment
        recipient = notice.recipient
        address = _fictional_address(recipient.email)
        eligible = (
            notice.kind == ReviewNotice.Kind.INVITATION
            and recipient.is_active and notice.recipient_id == assignment.reviewer_id
            and assignment.status == ReviewAssignment.Status.INVITED
            and assignment.round.status == ReviewRound.Status.OPEN
            and assignment.submission.status in (Submission.Status.SUBMITTED, Submission.Status.UNDER_REVIEW)
            and address is not None
            and constant_time_compare(job.recipient_digest, _recipient_digest(address))
            and visible_review_notices(recipient).filter(pk=notice.pk).exists()
        )
        if eligible:
            # Instantiate the in-memory backend directly, independent of any
            # configured mail backend. The address is always under .invalid.
            backend = EmailBackend()
            message = EmailMessage(
                subject="Fictional review invitation",
                body="A fictional review invitation is available in the local test workspace. No actual email was sent.",
                from_email="no-reply@example.invalid", to=[address], connection=backend,
            )
            if backend.send_messages([message]) != 1:
                raise RuntimeError("The local email simulation did not complete.")
            job.status = LocalReviewMailJob.Status.SIMULATED
        else:
            job.status = LocalReviewMailJob.Status.SUPPRESSED
        job.processed_at = timezone.now()
        job.save(update_fields=["status", "processed_at"])
        return job.status
