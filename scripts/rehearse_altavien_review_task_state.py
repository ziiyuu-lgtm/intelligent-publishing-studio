"""Exercise the task-page installer only on a disposable CI runner."""
import os
from pathlib import Path
import sys

if os.environ.get('GITHUB_ACTIONS') != 'true' or os.environ.get('IPS_INSTALLER_REHEARSAL') != '1':
    raise SystemExit('This rehearsal is restricted to the disposable CI fixture.')
if sys.argv[1:] != ['rehearse'] or os.geteuid() != 0:
    raise SystemExit('Choose rehearse as root on the disposable fixture.')

import fcntl
import importlib.util
import sqlite3

spec = importlib.util.spec_from_file_location('task_state_installer',
    Path(__file__).with_name('install_altavien_review_task_state.py'))
wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wrapper)
m = wrapper.build_installer()
db_path = m.BASE / '.runtime/dev.sqlite3'
with sqlite3.connect(db_path) as db:
    assert db.execute("SELECT COUNT(*) FROM auth_user WHERE username LIKE 'fictional-installer-%'").fetchone()[0] == 3
initial = m.database_state(db_path)
initial_media = m.media_manifest()
initial_pid = m.service()['MainPID']
assert m.main(check_only=True) == 0

# Unknown changes to either updated file, symlinks and concurrent installs stop.
for rel in (wrapper.VIEWS, wrapper.TEMPLATE):
    path = m.BASE / rel
    original = path.read_bytes()
    try:
        path.write_bytes(original + b'\n# Unknown local CI edit.\n')
        assert m.main() == 1
    finally:
        path.write_bytes(original)
path = m.BASE / wrapper.TEMPLATE
held = path.with_name('review_detail.ci-held')
assert path.is_file() and not held.exists()
path.rename(held)
try:
    path.symlink_to(m.BASE / wrapper.VIEWS)
    assert m.main() == 1
finally:
    path.unlink()
    held.rename(path)
with m.LOCK.open('r+') as locked:
    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert m.main() == 1
assert m.service()['MainPID'] == initial_pid
assert m.database_state(db_path) == initial and m.media_manifest() == initial_media
print('PREFLIGHT_REJECTION_AND_CONCURRENT_RUN_GUARDS_PASSED', flush=True)


def assert_original_code():
    for rel, expected in m.OLD.items():
        assert m.digest((m.BASE / rel).read_bytes()) == expected
    m.homepage()


original_apply = m.apply_schema_and_check
def fail_before_restart(env, before):
    original_apply(env, before)
    raise RuntimeError('CI_FAILURE_AFTER_CHECKS')
m.apply_schema_and_check = fail_before_restart
assert m.main() == 1
m.apply_schema_and_check = original_apply
assert_original_code()
m.preserved(initial, m.database_state(db_path))
assert m.media_manifest() == initial_media
print('PRESTART_FAILURE_RECOVERED_WITH_RECORDS_AND_MEDIA_PRESERVED', flush=True)

original_live_check = m.live_check
def fail_after_live_write(paths):
    original_live_check(paths)
    env = m.service_environment(int(m.service()['MainPID']))
    code = """from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
get_user_model().objects.create_user('fictional-task-post-start-write')
Path(settings.MEDIA_ROOT, 'fictional-task-post-start-write.txt').write_bytes(b'Fictional post-start file.')
"""
    m.app_command(m.BASE, env, 'shell', '-c', code)
    raise RuntimeError('CI_FAILURE_AFTER_LIVE_WRITE')
m.live_check = fail_after_live_write
assert m.main() == 1
m.live_check = original_live_check
assert_original_code()
with sqlite3.connect(db_path) as db:
    assert db.execute("SELECT COUNT(*) FROM auth_user WHERE username='fictional-task-post-start-write'").fetchone()[0] == 1
assert (m.BASE / '.runtime/private-media/fictional-task-post-start-write.txt').read_bytes() == b'Fictional post-start file.'
for name, value in initial_media.items():
    assert m.media_manifest()[name] == value
print('POSTSTART_FAILURE_RECOVERED_WITH_NEW_RECORDS_AND_MEDIA_PRESERVED', flush=True)

before = m.database_state(db_path)
media = m.media_manifest()
assert m.main() == 0
m.preserved(before, m.database_state(db_path))
assert m.media_manifest() == media
for rel, expected in m.NEW.items():
    assert m.digest((m.BASE / rel).read_bytes()) == expected

# Exercise the installed page using fictional records, without releasing the issue.
env = m.service_environment(int(m.service()['MainPID']))
m.app_command(m.BASE, env, 'shell', '-c', '''
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client
from django.urls import reverse
from publishing.models import JournalIssue, Membership, ReviewAssignment, Submission, SubmissionVersion
users = get_user_model().objects
reviewer = users.create_user("fictional-task-reviewer")
editor = users.get(username="fictional-installer-editor")
submission = Submission.objects.order_by("pk").first()
Membership.objects.create(user=reviewer, journal=submission.journal, role="reviewer")
round_ = submission.rounds.get(number=1)
round_.status = "open"
round_.save(update_fields=["status"])
version = round_.version
version.manuscript.save("task-test.pdf", ContentFile(b"%PDF-1.4\\nfictional task\\n%%EOF"), save=True)
submission.status = "under_review"
submission.save(update_fields=["status"])
assignment = ReviewAssignment.objects.create(submission=submission, round=round_, reviewer=reviewer,
    invited_by=editor, status="invited", conflict="unknown")
client = Client()
client.force_login(reviewer)
detail = reverse("review_detail", args=[assignment.pk])
respond = reverse("review_respond", args=[assignment.pk])
report = reverse("review_report", args=[assignment.pk])
download = reverse("review_file", args=[assignment.pk])
assert b'value="accept"' in client.get(detail).content
later = SubmissionVersion.objects.create(submission=submission, number=2,
    manuscript=version.manuscript.name, uploaded_by=submission.author)
assert b'value="accept"' not in client.get(detail).content
assert client.post(respond, {"response":"accept", "declaration":"none"}).status_code == 409
later.delete()
assert client.post(respond, {"response":"accept", "declaration":"none"}).status_code == 302
assert report.encode() in client.get(detail).content
invalid = client.post(report, {"recommendation":"major"})
assert invalid.status_code == 400 and download.encode() in invalid.content
submission.status = "accepted"
submission.save(update_fields=["status"])
inactive = client.get(detail)
assert report.encode() not in inactive.content and download.encode() not in inactive.content
assert b'value="conflict"' in inactive.content
assert client.post(report, {}).status_code == 409
assert JournalIssue.objects.get(number=2).released_at is None
''')
print('INSTALLED_TASK_ACTIONS_AND_NO_RELEASE_PASSED', flush=True)
pid = m.service()['MainPID']
backups = set(Path('/srv').glob('altavien-issue-release-backup-*'))
assert m.main() == 0
assert m.service()['MainPID'] == pid
assert set(Path('/srv').glob('altavien-issue-release-backup-*')) == backups
print('SUCCESSFUL_UPGRADE_AND_IDEMPOTENT_RERUN_PASSED', flush=True)
