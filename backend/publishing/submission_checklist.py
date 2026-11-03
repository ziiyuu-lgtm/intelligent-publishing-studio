"""Build and sign a current, author-only pre-submission review for local testing."""
import hashlib
import json

from django.conf import settings
from django.core import signing
from django.core.exceptions import SuspiciousFileOperation
from django.db.models import Q
from django.utils.crypto import constant_time_compare

from .contributors import ready_to_submit, validate_contributors
from .forms import MAX_MANUSCRIPT_BYTES, MAX_SUPPORTING_FILES, MAX_SUPPORTING_TOTAL_BYTES
from .models import Journal, SupportingFile
from .keywords import validate_keywords
from .supporting_files import verified_bytes

SALT = 'publishing.draft-checklist.v1'
TOKEN_AGE_SECONDS = 2 * 60 * 60


def _fingerprint(file):
    """Check the PDF header and bound reads; this is neither parsing nor virus scanning."""
    if not file or not file.name:
        return None
    try:
        with file.open('rb') as stream:
            data = stream.read(MAX_MANUSCRIPT_BYTES + 1)
    except (OSError, ValueError, SuspiciousFileOperation):
        return None
    if not data.startswith(b'%PDF-') or len(data) > MAX_MANUSCRIPT_BYTES:
        return None
    return {'path': file.name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def review_draft(submission):
    """Return display state and an opaque digest; no source text is stored in the token."""
    version = submission.versions.filter(number=1).first()
    errors = []
    try:
        contributors = validate_contributors(submission.contributors_config)
    except ValueError:
        contributors = []
        errors.append('authors_invalid')
    else:
        if not ready_to_submit(contributors):
            errors.append('authors_incomplete')
    if not version or submission.rounds.exists():
        errors.append('version_unavailable')
    if not submission.title.strip() or not submission.abstract.strip():
        errors.append('metadata_missing')
    if version and version.metadata_recorded and (version.title_snapshot != submission.title or version.abstract_snapshot != submission.abstract):
        errors.append('metadata_changed')
    try:
        keywords = validate_keywords(submission.keywords_config)
        version_keywords = validate_keywords(version.keywords_snapshot) if version else None
    except ValueError:
        keywords, version_keywords = None, None
        errors.append('keywords_invalid')
    else:
        if version and keywords != version_keywords:
            errors.append('keywords_changed')
    manuscript = _fingerprint(version.manuscript) if version else None
    if not manuscript:
        errors.append('manuscript_unavailable')
    review_copy = None
    if submission.review_mode == Journal.ReviewMode.SEPARATE:
        if version and version.review_copy_confirmed:
            review_copy = _fingerprint(version.review_copy)
        if not review_copy or (manuscript and review_copy['path'] == manuscript['path']):
            errors.append('review_copy_unavailable')
    elif submission.review_mode != Journal.ReviewMode.DIRECT:
        errors.append('policy_unavailable')

    attachments = []
    if version:
        # Include both sides to fail closed on an inconsistent association.
        records = list(SupportingFile.objects.filter(Q(submission=submission) | Q(version=version),
            removed_at__isnull=True).select_related('version').order_by('pk'))
        if any(item.submission_id != submission.pk or item.version_id != version.pk for item in records):
            errors.append('supporting_unavailable')
        elif len(records) > MAX_SUPPORTING_FILES or sum(item.size_bytes for item in records) > MAX_SUPPORTING_TOTAL_BYTES:
            errors.append('supporting_unavailable')
        else:
            for item in records:
                item.available = verified_bytes(item) is not None
                if not item.available:
                    errors.append('supporting_unavailable')
                attachments.append(item)
    payload = {'submission': str(submission.pk), 'author': submission.author_id, 'journal': submission.journal_id,
        'title': submission.title, 'abstract': submission.abstract, 'keywords':keywords, 'version_keywords':version_keywords,
        'contributors_revision': submission.contributors_revision,
        'contributors': contributors, 'version': version.pk if version else None,
        'review_mode': submission.review_mode, 'review_confirmed': version.review_copy_confirmed if version else None,
        'manuscript': manuscript, 'review_copy': review_copy,
        'attachments': [{'id':str(item.pk), 'name':item.original_name, 'path':item.file.name,
            'visibility':item.visibility, 'size':item.size_bytes, 'sha256':item.sha256} for item in attachments]}
    digest = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {'version':version, 'contributors':contributors, 'attachments':attachments,
        'errors':list(dict.fromkeys(errors)), 'ready':not errors, 'digest':digest, 'keywords':keywords,
        'manuscript_available':bool(manuscript), 'review_copy_available':bool(review_copy),
        'supporting_enabled':settings.DEBUG}


def signed_review(submission, review):
    return signing.dumps({'submission':str(submission.pk), 'author':submission.author_id, 'digest':review['digest']},
                         salt=SALT, compress=True)


def review_matches(token, submission, review):
    try:
        value = signing.loads(token, salt=SALT, max_age=TOKEN_AGE_SECONDS)
        return bool(isinstance(value, dict) and value.get('submission') == str(submission.pk) and
                    value.get('author') == submission.author_id and
                    isinstance(value.get('digest'), str) and constant_time_compare(value['digest'], review['digest']))
    except (signing.BadSignature, TypeError, ValueError):
        return False
