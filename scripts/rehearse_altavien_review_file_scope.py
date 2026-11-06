"""Exercise the reviewer file-scope installer only on a disposable GitHub Actions runner."""
import os
from pathlib import Path
import sys

if os.environ.get("GITHUB_ACTIONS") != "true" or os.environ.get("IPS_INSTALLER_REHEARSAL") != "1":
    raise SystemExit("This rehearsal is restricted to the disposable CI fixture.")
if sys.argv[1:] not in (["seed"], ["rehearse"]):
    raise SystemExit("Choose seed or rehearse.")

if sys.argv[1] == "seed":
    import hashlib
    from datetime import timedelta
    sys.path.insert(0, "/srv/altavien-test/backend")
    os.environ["DJANGO_SETTINGS_MODULE"] = "config.settings"
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.utils import timezone
    from publishing.models import (
        Organisation, Journal, JournalIssue, Submission, SubmissionVersion, ReviewRound,
        EditorialDecision, PublicationDraft, PublicationProof, PublicationApproval, Membership,
    )
    from publishing.production_views import _fingerprint
    users = get_user_model().objects
    assert not users.exists(), "The rehearsal requires an empty disposable database."
    author = users.create_user("fictional-installer-author")
    editor = users.create_user("fictional-installer-editor")
    coordinator = users.create_user("fictional-installer-coordinator")
    organisation = Organisation.objects.create(name="Fictional publisher", slug="fictional-publisher")
    journal = Journal.objects.create(organisation=organisation, name="Fictional journal", slug="fictional-journal")
    Membership.objects.create(user=coordinator, journal=journal, role="coordinator")
    released = JournalIssue.objects.create(journal=journal, volume=1, number=1, year=2026,
        title="Fictional released issue", created_by=coordinator, released_by=coordinator,
        released_at=timezone.now())
    pending = JournalIssue.objects.create(journal=journal, volume=1, number=2, year=2026,
        title="Fictional pending issue", created_by=coordinator)
    drafts = []
    for number in range(1, 5):
        submission = Submission.objects.create(journal=journal, author=author,
            title=f"Fictional paper {number}", abstract="Fictional abstract",
            manuscript="fictional.pdf", status="accepted")
        version = SubmissionVersion.objects.create(submission=submission, number=1,
            manuscript="fictional.pdf", uploaded_by=author)
        review = ReviewRound.objects.create(submission=submission, version=version, number=1, status="closed")
        decision = EditorialDecision.objects.create(submission=submission, round=review,
            outcome="accept", author_message="Fictional acceptance", decided_by=editor)
        draft = PublicationDraft.objects.create(submission=submission, decision=decision,
            issue=released if number <= 2 else pending, title=submission.title,
            authors="Fictional Author", abstract="Fictional public abstract",
            created_by=editor, published_by=editor, published_at=timezone.now() - timedelta(days=number))
        data = b"%PDF-1.4\nFictional installer proof\n%%EOF\n"
        proof = PublicationProof.objects.create(draft=draft, file=SimpleUploadedFile("proof.pdf", data),
            sha256=hashlib.sha256(data).hexdigest(), uploaded_by=editor)
        draft.published_proof = proof
        draft.save(update_fields=["published_proof"])
        PublicationApproval.objects.create(draft=draft, proof=proof, revision=draft.revision,
            fingerprint=_fingerprint(draft, proof), approved_by=author)
        drafts.append(draft)
    if hasattr(released, "article_order"):
        released.article_order = [str(draft.pk) for draft in drafts[:2]]
        released.save(update_fields=["article_order"])
    raise SystemExit(0)

import fcntl
import importlib.util
import json
import sqlite3

assert os.geteuid() == 0
spec = importlib.util.spec_from_file_location("issue_installer",
    Path(__file__).with_name("install_altavien_review_file_scope.py"))
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
m = installer.build_installer()
db_path = m.BASE / ".runtime/dev.sqlite3"
with sqlite3.connect(db_path) as db:
    assert db.execute("SELECT COUNT(*) FROM auth_user WHERE username LIKE 'fictional-installer-%'").fetchone()[0] == 3
initial = m.database_state(db_path)
initial_media = m.media_manifest()
initial_pid = m.service()["MainPID"]
assert m.main(check_only=True) == 0

# Unknown edits and symlinks stop before service or database changes.
changed = m.BASE / "publishing/issue_views.py"
original = changed.read_bytes()
try:
    changed.write_bytes(original + b"\n# Unknown local edit for CI.\n")
    assert m.main() == 1
finally:
    changed.write_bytes(original)
link = m.BASE / "publishing/supporting_views.py"
held = link.with_name("supporting_views.ci-held")
assert link.is_file() and not held.exists()
link.rename(held)
try:
    link.symlink_to(m.BASE / "publishing/issue_views.py")
    assert m.main() == 1
finally:
    link.unlink()
    held.rename(link)
with m.LOCK.open("r+") as locked:
    fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert m.main() == 1
assert m.service()["MainPID"] == initial_pid
assert m.database_state(db_path) == initial
assert m.media_manifest() == initial_media
print("PREFLIGHT_REJECTION_AND_CONCURRENT_RUN_GUARDS_PASSED", flush=True)


def assert_original_code():
    for rel, expected in m.OLD.items():
        assert m.digest((m.BASE / rel).read_bytes()) == expected
    for rel in m.NEW.keys() - m.OLD.keys():
        assert not (m.BASE / rel).exists()
    m.homepage()


apply_original = m.apply_schema_and_check
def fail_before_restart(env, before):
    apply_original(env, before)
    raise RuntimeError("CI_FAILURE_AFTER_MIGRATION")
m.apply_schema_and_check = fail_before_restart
assert m.main() == 1
m.apply_schema_and_check = apply_original
assert_original_code()
m.preserved(initial, m.database_state(db_path))
assert m.media_manifest() == initial_media
print("PRESTART_FAILURE_RECOVERED_WITH_RECORDS_AND_SCHEMA_PRESERVED", flush=True)

live_original = m.live_check
def fail_after_live_write(paths):
    live_original(paths)
    env = m.service_environment(int(m.service()["MainPID"]))
    code = """from pathlib import Path
from django.conf import settings
from django.contrib.auth import get_user_model
from publishing.models import JournalIssue
get_user_model().objects.create_user('fictional-post-start-write')
issue = JournalIssue.objects.get(number=2)
issue.article_order = [str(value) for value in issue.articles.order_by('-published_at', '-pk').values_list('pk', flat=True)]
issue.save(update_fields=['article_order'])
Path(settings.MEDIA_ROOT, 'fictional-post-start-write.txt').write_bytes(b'New fictional file after restart.')
"""
    m.app_command(m.BASE, env, "shell", "-c", code)
    raise RuntimeError("CI_FAILURE_AFTER_LIVE_WRITE")
m.live_check = fail_after_live_write
assert m.main() == 1
m.live_check = live_original
assert_original_code()
with sqlite3.connect(db_path) as db:
    assert db.execute("SELECT COUNT(*) FROM auth_user WHERE username='fictional-post-start-write'").fetchone()[0] == 1
    order = json.loads(db.execute("SELECT article_order FROM publishing_journalissue WHERE number=2").fetchone()[0])
    assert len(order) == 2
assert (m.BASE / ".runtime/private-media/fictional-post-start-write.txt").read_bytes() == b"New fictional file after restart."
for name, value in initial_media.items():
    assert m.media_manifest()[name] == value

# Older code must still be able to create issues with the retained nullable column.
env = m.service_environment(int(m.service()["MainPID"]))
m.app_command(m.BASE, env, "shell", "-c",
    "from publishing.models import JournalIssue; original=JournalIssue.objects.get(number=2); "
    "JournalIssue.objects.create(journal=original.journal,volume=1,number=3,year=2026,"
    "title='Fictional issue after recovery',created_by=original.created_by)")
print("POSTSTART_FAILURE_RECOVERED_WITH_NEW_RECORDS_ORDER_AND_MEDIA_PRESERVED", flush=True)

before_final = m.database_state(db_path)
media_final = m.media_manifest()
assert m.main() == 0
m.preserved(before_final, m.database_state(db_path))
assert m.media_manifest() == media_final
for rel, expected in m.NEW.items():
    assert m.digest((m.BASE / rel).read_bytes()) == expected
# Verify the installed recovery page with the disposable fictional records.
env = m.service_environment(int(m.service()["MainPID"]))
m.app_command(m.BASE, env, "shell", "-c", '''
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from publishing.models import JournalIssue
coordinator = get_user_model().objects.get(username="fictional-installer-coordinator")
issue = JournalIssue.objects.get(number=2)
assert issue.released_at is None
client = Client()
client.force_login(coordinator)
response = client.post(reverse("release_test_issue", args=[issue.pk]),
    {"confirm":"yes", "synthetic":"yes", "release_token":"invalid-test-token"})
assert response.status_code == 409
assert reverse("review_test_issue_release", args=[issue.pk]).encode() in response.content
assert b'name="release_token"' not in response.content
issue.refresh_from_db()
assert issue.released_at is None
fresh = client.get(reverse("review_test_issue_release", args=[issue.pk]))
assert fresh.status_code == 200 and b'name="release_token"' in fresh.content
issue.refresh_from_db()
assert issue.released_at is None
''')
print("INSTALLED_RECOVERY_LINK_AND_NO_RELEASE_PASSED", flush=True)
# Verify the new reviewer boundary against the disposable database and PDF.
m.app_command(m.BASE, env, "shell", "-c", '''
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import Client
from django.urls import reverse
from publishing.models import Membership, ReviewAssignment, Submission
users = get_user_model().objects
reviewer = users.create_user("fictional-scope-reviewer")
editor = users.get(username="fictional-installer-editor")
submission = Submission.objects.order_by("pk").first()
Membership.objects.create(user=reviewer, journal=submission.journal, role="reviewer")
round_ = submission.rounds.get(number=1)
round_.status = "open"
round_.save(update_fields=["status"])
version = round_.version
version.manuscript.save("scope-test.pdf", ContentFile(b"%PDF-1.4\\nfictional scope\\n%%EOF"), save=True)
submission.status = "under_review"
submission.save(update_fields=["status"])
assignment = ReviewAssignment.objects.create(submission=submission, round=round_, reviewer=reviewer,
    invited_by=editor, status="accepted", conflict="none")
client = Client()
client.force_login(reviewer)
url = reverse("review_file", args=[assignment.pk])
response = client.get(url)
assert response.status_code == 200
assert b"".join(response.streaming_content).startswith(b"%PDF-")
submission.status = "accepted"
submission.save(update_fields=["status"])
assert client.get(url).status_code == 404
assert url.encode() not in client.get(reverse("review_detail", args=[assignment.pk])).content
''')
print("INSTALLED_REVIEWER_FILE_BOUNDARY_PASSED", flush=True)
pid = m.service()["MainPID"]
backups = set(Path("/srv").glob("altavien-issue-release-backup-*"))
assert m.main() == 0
assert m.service()["MainPID"] == pid
assert set(Path("/srv").glob("altavien-issue-release-backup-*")) == backups
print("SUCCESSFUL_UPGRADE_AND_IDEMPOTENT_RERUN_PASSED", flush=True)
