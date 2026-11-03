"""Bounded local-test supporting files; callers own transactions and access checks."""
import hashlib
import hmac
import json
import re

from django.core.exceptions import SuspiciousFileOperation
from django.utils.text import get_valid_filename

from .forms import MAX_MANUSCRIPT_BYTES, MAX_SUPPORTING_FILES, MAX_SUPPORTING_TOTAL_BYTES
from .models import SupportingFile

MAX_DRAFT_FILE_RECORDS = 20


def safe_name(value):
    name = str(value).replace('\\', '/').split('/')[-1]
    try:
        name = get_valid_filename(name)
    except SuspiciousFileOperation:
        name = 'supporting.pdf'
    if not name.lower().endswith('.pdf'):
        name = 'supporting.pdf'
    return name[:176] + '.pdf' if len(name) > 180 else name


def within_limits(version, new_files, *, restoring=None):
    records = SupportingFile.objects.filter(submission_id=version.submission_id, version=version)
    active = list(records.filter(removed_at__isnull=True).values_list('size_bytes', flat=True))
    sizes = [file.size for file in new_files]
    if restoring is not None:
        sizes.append(restoring.size_bytes)
    return (len(active) + len(sizes) <= MAX_SUPPORTING_FILES and
            sum(active) + sum(sizes) <= MAX_SUPPORTING_TOTAL_BYTES and
            records.count() + len(new_files) <= MAX_DRAFT_FILE_RECORDS)


def save_files(submission, version, actor, cleaned_data, saved_storage):
    """Call under submission/version locks. Track saved bytes for rollback cleanup."""
    for field, visibility in [('editor_supporting_files', SupportingFile.Visibility.EDITOR),
                              ('reviewer_supporting_files', SupportingFile.Visibility.REVIEWER)]:
        for uploaded in cleaned_data.get(field) or []:
            digest = hashlib.sha256()
            for chunk in uploaded.chunks():
                digest.update(chunk)
            uploaded.seek(0)
            record = SupportingFile(submission=submission, version=version, uploaded_by=actor,
                visibility=visibility, original_name=safe_name(uploaded.name), size_bytes=uploaded.size,
                sha256=digest.hexdigest())
            record.file.save('supporting.pdf', uploaded, save=False)
            saved_storage.append((record.file.storage, record.file.name))
            record.save(force_insert=True)


def verified_bytes(record):
    """Return exactly the checked bytes, or None when missing, changed or oversized."""
    if (not 0 < record.size_bytes <= MAX_MANUSCRIPT_BYTES or not record.file or
            not re.fullmatch(r'[0-9a-f]{64}', record.sha256 or '')):
        return None
    try:
        with record.file.open('rb') as stream:
            data = stream.read(MAX_MANUSCRIPT_BYTES + 1)
    except (OSError, ValueError, SuspiciousFileOperation):
        return None
    if len(data) != record.size_bytes or not data.startswith(b'%PDF-'):
        return None
    return data if hmac.compare_digest(hashlib.sha256(data).hexdigest(), record.sha256) else None


def review_fingerprint(record):
    payload = json.dumps({'id':str(record.pk), 'submission':str(record.submission_id), 'version':record.version_id,
        'name':record.original_name, 'visibility':record.visibility, 'size':record.size_bytes, 'sha256':record.sha256},
        ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(payload.encode()).hexdigest()


def checked_and_current(record):
    return bool(record.checked_at and record.checked_by_id and re.fullmatch(r'[0-9a-f]{64}', record.checked_fingerprint or '') and
                hmac.compare_digest(record.checked_fingerprint, review_fingerprint(record)))


def reviewer_files_ready(version):
    """All current reviewer copies must be checked, associated and still intact."""
    for item in SupportingFile.objects.filter(version=version, removed_at__isnull=True):
        if item.submission_id != version.submission_id:
            return False
        if item.visibility == SupportingFile.Visibility.REVIEWER and (
                not checked_and_current(item) or verified_bytes(item) is None):
            return False
    return True
