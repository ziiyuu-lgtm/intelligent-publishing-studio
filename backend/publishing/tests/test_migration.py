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
issues_scope = ('publishing', '0008_journalissue_publicationdraft_issue_and_more')
executor = MigrationExecutor(connection)
executor.migrate([issues_scope])
apps = executor.loader.project_state([issues_scope]).apps
Submission8 = apps.get_model('publishing', 'Submission')
Draft8 = apps.get_model('publishing', 'PublicationDraft')
Approval8 = apps.get_model('publishing', 'PublicationApproval')
Issue8 = apps.get_model('publishing', 'JournalIssue')
assert Submission8.objects.filter(pk=submission.pk).exists()
assert Draft8.objects.get(pk=draft6.pk).issue_id is None
assert Approval8.objects.filter(draft_id=draft6.pk, proof_id=proof6.pk, approved_by_id=author.pk).exists()
assert not Issue8.objects.exists(), 'No issue should be inferred for existing manuscripts'
try:
    MigrationExecutor(connection).migrate([production_scope])
except IrreversibleError:
    assert Draft8.objects.filter(pk=draft6.pk).exists()
else:
    raise AssertionError('Issue assignments must not be silently discarded')
issue8 = Issue8.objects.create(journal_id=journal.pk, volume=1, number=1, year=2025, title='Fictional issue', created_by_id=editor.pk)
site_scope = ('publishing', '0009_alter_membership_role_journalsitedraft_and_more')
executor = MigrationExecutor(connection)
executor.migrate([site_scope])
apps = executor.loader.project_state([site_scope]).apps
Issue9 = apps.get_model('publishing', 'JournalIssue')
Site9 = apps.get_model('publishing', 'JournalSiteDraft')
Membership9 = apps.get_model('publishing', 'Membership')
assert Issue9.objects.get(pk=issue8.pk).title == 'Fictional issue'
assert not Site9.objects.exists(), 'No test site should be inferred or published'
assert not Membership9.objects.filter(role='site_manager').exists(), 'No site-manager role should be inferred'
try:
    MigrationExecutor(connection).migrate([issues_scope])
except IrreversibleError:
    assert Issue9.objects.filter(pk=issue8.pk).exists()
else:
    raise AssertionError('Site design history must not be silently discarded')
Journal9 = apps.get_model('publishing', 'Journal')
Journal9.objects.filter(pk=journal.pk).update(allow_openai_metadata=True)
site9 = Site9.objects.create(journal_id=journal.pk, brand_name='Fictional site', headline='Fictional headline',
                            introduction='Fictional introduction', created_by_id=editor.pk)
policy_scope = ('publishing', '0010_aiassistancerun_policy_revision_and_more')
executor = MigrationExecutor(connection)
executor.migrate([policy_scope])
apps = executor.loader.project_state([policy_scope]).apps
Policy10 = apps.get_model('publishing', 'JournalAIPolicy')
Site10 = apps.get_model('publishing', 'JournalSiteDraft')
Membership10 = apps.get_model('publishing', 'Membership')
assert Site10.objects.get(pk=site9.pk).brand_name == 'Fictional site'
assert not Policy10.objects.exists(), 'No AI policy should be inferred, even if the old switch was enabled'
assert not Membership10.objects.filter(role='ai_manager').exists(), 'No AI manager role should be inferred'
try:
    MigrationExecutor(connection).migrate([site_scope])
except IrreversibleError:
    assert Site10.objects.filter(pk=site9.pk).exists()
else:
    raise AssertionError('AI policy history must not be silently discarded')
policy10 = Policy10.objects.create(journal_id=journal.pk, metadata_enabled=True, allow_author=True,
                                  allow_editor=False, journal_daily_limit=4)
design_scope = ('publishing', '0011_journalaipolicy_site_design_enabled_and_more')
executor = MigrationExecutor(connection)
executor.migrate([design_scope])
apps = executor.loader.project_state([design_scope]).apps
Policy11 = apps.get_model('publishing', 'JournalAIPolicy')
Run11 = apps.get_model('publishing', 'JournalSiteAIDesignRun')
policy11 = Policy11.objects.get(pk=policy10.pk)
assert policy11.metadata_enabled and policy11.allow_author and policy11.journal_daily_limit == 4
assert not policy11.site_design_enabled, 'An existing policy must not enable AI site design'
assert not Run11.objects.exists(), 'Existing sites must not create AI requests'
try:
    MigrationExecutor(connection).migrate([policy_scope])
except IrreversibleError:
    assert Policy11.objects.filter(pk=policy10.pk).exists()
else:
    raise AssertionError('AI design history must not be silently discarded')
import uuid
Site11 = apps.get_model('publishing', 'JournalSiteDraft')
Release11 = apps.get_model('publishing', 'JournalSiteRelease')
site11 = Site11.objects.get(pk=site9.pk)
release11 = Release11.objects.create(site_id=site11.pk, revision=site11.revision, brand_name=site11.brand_name,
    headline=site11.headline, tagline=site11.tagline, introduction=site11.introduction, theme=site11.theme,
    layout=site11.layout, published_by_id=editor.pk)
Site11.objects.filter(pk=site11.pk).update(current_release_id=release11.pk)
run11 = Run11.objects.create(id=uuid.uuid4(), site_id=site11.pk, requested_by_id=editor.pk, model_name='legacy',
    prompt_version='site-design-presets-v1', policy_revision=1, site_revision=1, site_fingerprint='a' * 64,
    brief_fingerprint='b' * 64, status='completed', options=[{'theme':'ink', 'layout':'magazine'}])
blueprint_scope = ('publishing', '0012_journalsitedraft_design_config_and_more')
executor = MigrationExecutor(connection)
executor.migrate([blueprint_scope])
apps = executor.loader.project_state([blueprint_scope]).apps
Site12 = apps.get_model('publishing', 'JournalSiteDraft')
Release12 = apps.get_model('publishing', 'JournalSiteRelease')
Run12 = apps.get_model('publishing', 'JournalSiteAIDesignRun')
assert Site12.objects.get(pk=site11.pk).design_config == {}
assert Site12.objects.get(pk=site11.pk).current_release_id == release11.pk
assert Release12.objects.get(pk=release11.pk).design_config == {}
assert Run12.objects.get(pk=run11.pk).options == [{'theme':'ink', 'layout':'magazine'}]
try:
    MigrationExecutor(connection).migrate([design_scope])
except IrreversibleError:
    assert Release12.objects.filter(pk=release11.pk).exists()
else:
    raise AssertionError('Saved design blueprints must not be silently discarded')
print('legacy workflow preserved')
'''
        with tempfile.TemporaryDirectory(prefix="ips-migration-") as temp_dir:
            env = os.environ.copy()
            env["IPS_MIGRATION_TEST_DB"] = str(Path(temp_dir) / "legacy.sqlite3")
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                                    env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("legacy workflow preserved", result.stdout)
