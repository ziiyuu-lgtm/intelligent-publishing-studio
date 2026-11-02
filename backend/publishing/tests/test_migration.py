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
privacy = ('publishing', '0003_journal_review_mode_reviewassignment_conflict_and_more')
executor = MigrationExecutor(connection)
executor.migrate([privacy])
apps = executor.loader.project_state([privacy]).apps
Journal3 = apps.get_model('publishing', 'Journal')
Submission3 = apps.get_model('publishing', 'Submission')
Assignment3 = apps.get_model('publishing', 'ReviewAssignment')
Version3 = apps.get_model('publishing', 'SubmissionVersion')
assert Journal3.objects.get(pk=journal.pk).review_mode == 'direct'
assert Submission3.objects.get(pk=submission.pk).review_mode == 'direct'
assert Assignment3.objects.get(pk=assignment.pk).conflict == 'unknown'
assert not Version3.objects.get(submission_id=submission.pk, number=1).review_copy
new_journal = Journal3.objects.create(organisation_id=org.pk, name='New test journal', slug='new-journal')
assert new_journal.review_mode == 'separate'
try:
    MigrationExecutor(connection).migrate([new])
except IrreversibleError:
    assert Journal3.objects.get(pk=journal.pk).review_mode == 'direct'
else:
    raise AssertionError('Privacy migration must not silently discard declarations')
ai = ('publishing', '0004_journal_allow_openai_metadata_aiconsent_and_more')
executor = MigrationExecutor(connection)
executor.migrate([ai])
apps = executor.loader.project_state([ai]).apps
Journal4 = apps.get_model('publishing', 'Journal')
Consent4 = apps.get_model('publishing', 'AIConsent')
assert Journal4.objects.get(pk=journal.pk).allow_openai_metadata is False
assert not Consent4.objects.exists()
try:
    MigrationExecutor(connection).migrate([privacy])
except IrreversibleError:
    assert Journal4.objects.get(pk=journal.pk).allow_openai_metadata is False
else:
    raise AssertionError('AI consent migration must not silently discard records')
editor_scope = ('publishing', '0005_alter_membership_role_editorialassignment')
executor = MigrationExecutor(connection)
executor.migrate([editor_scope])
apps = executor.loader.project_state([editor_scope]).apps
Submission5 = apps.get_model('publishing', 'Submission')
Assignment5 = apps.get_model('publishing', 'EditorialAssignment')
assert Submission5.objects.get(pk=submission.pk).title == 'Synthetic'
assert not Assignment5.objects.exists(), 'No legacy editor should receive an inferred assignment'
try:
    MigrationExecutor(connection).migrate([ai])
except IrreversibleError:
    assert Submission5.objects.filter(pk=submission.pk).exists()
else:
    raise AssertionError('Editorial assignment history must not be silently discarded')
production = ('publishing', '0006_publicationdraft_publicationproof_and_more')
executor = MigrationExecutor(connection)
executor.migrate([production])
apps = executor.loader.project_state([production]).apps
Submission6 = apps.get_model('publishing', 'Submission')
Draft6 = apps.get_model('publishing', 'PublicationDraft')
assert Submission6.objects.get(pk=submission.pk).title == 'Synthetic'
assert not Draft6.objects.exists(), 'Old submissions must not be published automatically'
try:
    MigrationExecutor(connection).migrate([editor_scope])
except IrreversibleError:
    assert Submission6.objects.filter(pk=submission.pk).exists()
else:
    raise AssertionError('Production history must not be silently discarded')
# Preserve an existing fictional production draft and author confirmation, but
# never infer who may operate it under the new production-role policy.
Version6 = apps.get_model('publishing', 'SubmissionVersion')
Round6 = apps.get_model('publishing', 'ReviewRound')
Decision6 = apps.get_model('publishing', 'EditorialDecision')
Proof6 = apps.get_model('publishing', 'PublicationProof')
Approval6 = apps.get_model('publishing', 'PublicationApproval')
accepted6 = Submission6.objects.create(journal_id=journal.pk, author_id=author.pk, title='Accepted test',
    abstract='Fictional', manuscript='manuscripts/accepted.pdf', status='accepted')
version6 = Version6.objects.create(submission_id=accepted6.pk, number=1,
    manuscript='manuscripts/accepted.pdf', uploaded_by_id=author.pk)
round6 = Round6.objects.create(submission_id=accepted6.pk, version_id=version6.pk, number=1, status='closed')
decision6 = Decision6.objects.create(submission_id=accepted6.pk, round_id=round6.pk, outcome='accept',
    author_message='Fictional', decided_by_id=editor.pk)
draft6 = Draft6.objects.create(submission_id=accepted6.pk, decision_id=decision6.pk, title='Test publication',
    authors='Imaginary author', abstract='Fictional', created_by_id=editor.pk)
proof6 = Proof6.objects.create(draft_id=draft6.pk, file='publication-proofs/old.pdf', sha256='a'*64, uploaded_by_id=editor.pk)
Approval6.objects.create(draft_id=draft6.pk, proof_id=proof6.pk, revision=1, fingerprint='b'*64, approved_by_id=author.pk)
production_scope = ('publishing', '0007_alter_membership_role_productionassignment')
executor = MigrationExecutor(connection)
executor.migrate([production_scope])
apps = executor.loader.project_state([production_scope]).apps
Submission7 = apps.get_model('publishing', 'Submission')
ProductionAssignment7 = apps.get_model('publishing', 'ProductionAssignment')
Draft7 = apps.get_model('publishing', 'PublicationDraft')
Approval7 = apps.get_model('publishing', 'PublicationApproval')
assert Submission7.objects.get(pk=submission.pk).title == 'Synthetic'
assert not ProductionAssignment7.objects.exists(), 'No producer assignment should be inferred'
assert Draft7.objects.get(pk=draft6.pk).title == 'Test publication'
assert Approval7.objects.filter(draft_id=draft6.pk, proof_id=proof6.pk, approved_by_id=author.pk).exists()
try:
    MigrationExecutor(connection).migrate([production])
except IrreversibleError:
    assert Submission7.objects.filter(pk=submission.pk).exists()
else:
    raise AssertionError('Production assignment history must not be silently discarded')
print('legacy workflow preserved')
'''
        with tempfile.TemporaryDirectory(prefix="ips-migration-") as temp_dir:
            env = os.environ.copy()
            env["IPS_MIGRATION_TEST_DB"] = str(Path(temp_dir) / "legacy.sqlite3")
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                                    env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("legacy workflow preserved", result.stdout)
