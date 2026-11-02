import os
from pathlib import Path
import subprocess
import sys
import tempfile
from django.test import SimpleTestCase


class ExistingWorkflowMigrationTests(SimpleTestCase):
    def test_existing_manuscript_and_review_assignment_are_preserved(self):
        # A separate database starts at 0001, so this does not reverse the
        # intentionally irreversible version-history migration in the test DB.
        script = r'''
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
from django.conf import settings
settings.DATABASES['default']['NAME'] = os.environ['IPS_MIGRATION_TEST_DB']
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

old = ('publishing', '0001_initial')
new = ('publishing', '0002_editorialdecision_reviewround_submissionversion_and_more')
executor = MigrationExecutor(connection)
executor.migrate([old])
apps = executor.loader.project_state([old]).apps
User = apps.get_model('auth', 'User')
Organisation = apps.get_model('publishing', 'Organisation')
Journal = apps.get_model('publishing', 'Journal')
Submission = apps.get_model('publishing', 'Submission')
ReviewAssignment = apps.get_model('publishing', 'ReviewAssignment')
author = User.objects.create(username='migration_author', password='!')
editor = User.objects.create(username='migration_editor', password='!')
reviewer = User.objects.create(username='migration_reviewer', password='!')
org = Organisation.objects.create(name='Fictional publisher', slug='migration-publisher')
journal = Journal.objects.create(organisation=org, name='Fictional journal', slug='migration-journal')
submission = Submission.objects.create(journal=journal, author=author, title='Synthetic', abstract='Synthetic', manuscript='manuscripts/original.pdf', status='under_review')
assignment = ReviewAssignment.objects.create(submission=submission, reviewer=reviewer, invited_by=editor, status='accepted')
draft = Submission.objects.create(journal=journal, author=author, title='Draft', abstract='Synthetic', manuscript='manuscripts/draft.pdf', status='draft')
executor = MigrationExecutor(connection)
executor.migrate([new])
apps = executor.loader.project_state([new]).apps
Version = apps.get_model('publishing', 'SubmissionVersion')
Round = apps.get_model('publishing', 'ReviewRound')
Assignment = apps.get_model('publishing', 'ReviewAssignment')
version = Version.objects.get(submission_id=submission.pk, number=1)
round = Round.objects.get(submission_id=submission.pk, number=1)
review = Assignment.objects.get(pk=assignment.pk)
assert version.manuscript.name == 'manuscripts/original.pdf'
assert version.uploaded_by_id == author.pk
assert round.version_id == version.pk and round.status == 'open'
assert review.round_id == round.pk and review.status == 'accepted'
assert Version.objects.get(submission_id=draft.pk, number=1).manuscript.name == 'manuscripts/draft.pdf'
assert not Round.objects.filter(submission_id=draft.pk).exists()
from django.db.migrations.exceptions import IrreversibleError
try:
    MigrationExecutor(connection).migrate([old])
except IrreversibleError:
    assert Version.objects.get(submission_id=submission.pk, number=1).manuscript.name == 'manuscripts/original.pdf'
    assert Assignment.objects.get(pk=assignment.pk).round_id == round.pk
else:
    raise AssertionError('Migration must not silently discard revision history')
print('legacy workflow preserved')
'''
        with tempfile.TemporaryDirectory(prefix="ips-migration-") as temp_dir:
            env = os.environ.copy()
            env["IPS_MIGRATION_TEST_DB"] = str(Path(temp_dir) / "legacy.sqlite3")
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                                    env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("legacy workflow preserved", result.stdout)
