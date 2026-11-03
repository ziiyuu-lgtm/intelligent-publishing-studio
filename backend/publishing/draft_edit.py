"""Signed state for editing an unsubmitted fictional draft."""
import hashlib
import json

from django.core import signing
from django.utils.crypto import constant_time_compare

from .submission_checklist import _fingerprint

SALT = 'publishing.draft-edit.v1'
MAX_AGE_SECONDS = 2 * 60 * 60
MAX_REPLACED_DRAFT_FILES = 20


def snapshot(submission, version):
    """Bind editable fields, file bytes and the version association, without exposing text."""
    payload = {'id':str(submission.pk), 'author':submission.author_id, 'journal':submission.journal_id,
        'status':submission.status, 'review_mode':submission.review_mode, 'title':submission.title,
        'abstract':submission.abstract, 'submission_file':submission.manuscript.name,
        'version':version.pk, 'version_title':version.title_snapshot, 'version_abstract':version.abstract_snapshot,
        'manuscript_name':version.manuscript.name, 'manuscript':_fingerprint(version.manuscript),
        'review_name':version.review_copy.name, 'review_copy':_fingerprint(version.review_copy),
        'review_confirmed':version.review_copy_confirmed,
        'review_checked':version.review_copy_checked_at.isoformat() if version.review_copy_checked_at else None,
        'review_checked_by':version.review_copy_checked_by_id}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sign_edit(submission, version):
    return signing.dumps({'submission':str(submission.pk), 'author':submission.author_id, 'digest':snapshot(submission, version)},
                         salt=SALT, compress=True)


def matches_edit(token, submission, version):
    try:
        value = signing.loads(token, salt=SALT, max_age=MAX_AGE_SECONDS)
        return bool(isinstance(value, dict) and value.get('submission') == str(submission.pk) and
                    value.get('author') == submission.author_id and isinstance(value.get('digest'), str) and
                    constant_time_compare(value['digest'], snapshot(submission, version)))
    except (signing.BadSignature, TypeError, ValueError):
        return False
