"""Verify the exact separate reviewer PDF checked in the local prototype."""
import hashlib
import hmac
import json
import re

from django.core.exceptions import SuspiciousFileOperation

from .forms import MAX_MANUSCRIPT_BYTES
from .models import Journal


def read_review_copy(version):
    """Read a bounded PDF once, or return None if it is invalid or unavailable."""
    if not version.review_copy or not version.review_copy.name:
        return None
    try:
        with version.review_copy.open("rb") as stream:
            data = stream.read(MAX_MANUSCRIPT_BYTES + 1)
    except (OSError, ValueError, SuspiciousFileOperation):
        return None
    if not 0 < len(data) <= MAX_MANUSCRIPT_BYTES or not data.startswith(b"%PDF-"):
        return None
    return data


def review_copy_sha256(data):
    return hashlib.sha256(data).hexdigest()


def review_metadata_sha256(version):
    """Identify the exact version metadata the editor was shown."""
    try:
        payload = json.dumps({"submission": str(version.submission_id), "version": str(version.pk),
            "title": version.title_snapshot, "abstract": version.abstract_snapshot,
            "keywords": version.keywords_snapshot}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError):
        return None
    return hashlib.sha256(payload).hexdigest()


def reviewer_metadata_is_visible(version):
    """Hide changed or legacy metadata from reviewers until checked again."""
    mode = version.submission.review_mode
    if mode == Journal.ReviewMode.DIRECT:
        return True
    if (mode != Journal.ReviewMode.SEPARATE or not version.review_copy_confirmed or
            not version.review_copy_checked_at or not version.review_copy_checked_by_id or
            not re.fullmatch(r"[0-9a-f]{64}", version.review_metadata_checked_sha256 or "")):
        return False
    current = review_metadata_sha256(version)
    return bool(current and hmac.compare_digest(current, version.review_metadata_checked_sha256))


def checked_review_copy_bytes(submission, version):
    """Return the checked bytes, never a later unchecked version of the file."""
    if (submission.review_mode != Journal.ReviewMode.SEPARATE or
            version.submission_id != submission.pk or not version.review_copy_confirmed or
            not version.review_copy_checked_at or not version.review_copy_checked_by_id or
            not re.fullmatch(r"[0-9a-f]{64}", version.review_copy_checked_sha256 or "") or
            not reviewer_metadata_is_visible(version)):
        return None
    data = read_review_copy(version)
    if data is None or not hmac.compare_digest(review_copy_sha256(data), version.review_copy_checked_sha256):
        return None
    return data
