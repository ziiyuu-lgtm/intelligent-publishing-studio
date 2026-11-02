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
modular_scope = ('publishing', '0013_journalsitedraft_sections_config_and_more')
executor = MigrationExecutor(connection)
executor.migrate([modular_scope])
apps = executor.loader.project_state([modular_scope]).apps
Site13 = apps.get_model('publishing', 'JournalSiteDraft')
Release13 = apps.get_model('publishing', 'JournalSiteRelease')
Run13 = apps.get_model('publishing', 'JournalSiteAIDesignRun')
assert Site13.objects.get(pk=site11.pk).sections_config == []
assert Site13.objects.get(pk=site11.pk).current_release_id == release11.pk
assert Release13.objects.get(pk=release11.pk).sections_config == []
assert Run13.objects.get(pk=run11.pk).options == [{'theme':'ink', 'layout':'magazine'}]
try:
    MigrationExecutor(connection).migrate([blueprint_scope])
except IrreversibleError:
    assert Release13.objects.filter(pk=release11.pk).exists()
else:
    raise AssertionError('Saved page modules must not be silently discarded')
Policy13 = apps.get_model('publishing', 'JournalAIPolicy')
Policy13.objects.filter(pk=policy10.pk).update(site_design_enabled=True)
module_policy_scope = ('publishing', '0014_journalaipolicy_site_modules_enabled')
executor = MigrationExecutor(connection)
executor.migrate([module_policy_scope])
apps = executor.loader.project_state([module_policy_scope]).apps
Policy14 = apps.get_model('publishing', 'JournalAIPolicy')
policy14 = Policy14.objects.get(pk=policy10.pk)
assert policy14.site_design_enabled and not policy14.site_modules_enabled
assert policy14.metadata_enabled and policy14.allow_author and policy14.journal_daily_limit == 4
try:
    MigrationExecutor(connection).migrate([modular_scope])
except IrreversibleError:
    assert Policy14.objects.filter(pk=policy10.pk).exists()
else:
    raise AssertionError('AI module policy must not be silently discarded')
portal_scope = ('publishing', '0015_organisation_portal')
executor = MigrationExecutor(connection)
executor.migrate([portal_scope])
apps = executor.loader.project_state([portal_scope]).apps
assert not apps.get_model('publishing', 'OrganisationMembership').objects.exists()
assert not apps.get_model('publishing', 'OrganisationPortalDraft').objects.exists()
assert apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site11.pk).current_release_id == release11.pk
assert not apps.get_model('publishing', 'JournalAIPolicy').objects.get(pk=policy10.pk).site_modules_enabled
try:
    MigrationExecutor(connection).migrate([module_policy_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'JournalSiteRelease').objects.filter(pk=release11.pk).exists()
else:
    raise AssertionError('Portal versions must not be silently discarded')
OrgMembership15 = apps.get_model('publishing', 'OrganisationMembership')
OrgMembership15.objects.create(organisation_id=org.pk, user_id=editor.pk, role='portal_manager')
Portal15 = apps.get_model('publishing', 'OrganisationPortalDraft')
Release15 = apps.get_model('publishing', 'OrganisationPortalRelease')
portal15 = Portal15.objects.create(organisation_id=org.pk, created_by_id=editor.pk, brand_name='Fictional',
    headline='Portal', introduction='Test', design_config={}, journal_ids=[journal.pk])
release15 = Release15.objects.create(site_id=portal15.pk, revision=1, published_by_id=editor.pk,
    brand_name='Fictional', headline='Portal', introduction='Test', design_config={}, journals=[])
Portal15.objects.filter(pk=portal15.pk).update(current_release_id=release15.pk)
portal_ai_scope = ('publishing', '0016_organisation_portal_ai')
executor = MigrationExecutor(connection)
executor.migrate([portal_ai_scope])
apps = executor.loader.project_state([portal_ai_scope]).apps
assert not apps.get_model('publishing', 'OrganisationAIPolicy').objects.exists()
assert not apps.get_model('publishing', 'OrganisationMembership').objects.filter(role='ai_manager').exists()
assert apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal15.pk).current_release_id == release15.pk
assert apps.get_model('publishing', 'OrganisationPortalRelease').objects.filter(pk=release15.pk).exists()
assert not apps.get_model('publishing', 'JournalAIPolicy').objects.get(pk=policy10.pk).site_modules_enabled
try:
    MigrationExecutor(connection).migrate([portal_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'OrganisationPortalRelease').objects.filter(pk=release15.pk).exists()
else:
    raise AssertionError('Publisher AI history must not be silently discarded')
Policy16 = apps.get_model('publishing', 'OrganisationAIPolicy')
policy16 = Policy16.objects.create(organisation_id=org.pk, portal_design_enabled=True, portal_daily_limit=6)
PortalRun16 = apps.get_model('publishing', 'OrganisationPortalAIDesignRun')
portal_run16 = PortalRun16.objects.create(id=uuid.uuid4(), site_id=portal15.pk, requested_by_id=editor.pk,
    model_name='legacy', prompt_version='publisher-site-blueprints-v1', policy_revision=1, site_revision=1,
    site_fingerprint='c'*64, brief_fingerprint='d'*64, status='completed', options=[])
portal_modules_scope = ('publishing', '0017_publisher_portal_modules')
executor = MigrationExecutor(connection)
executor.migrate([portal_modules_scope])
apps = executor.loader.project_state([portal_modules_scope]).apps
policy17 = apps.get_model('publishing', 'OrganisationAIPolicy').objects.get(pk=policy16.pk)
assert policy17.portal_design_enabled and not policy17.portal_modules_enabled and policy17.portal_daily_limit == 6
assert apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal15.pk).sections_config == []
assert apps.get_model('publishing', 'OrganisationPortalRelease').objects.get(pk=release15.pk).sections_config == []
assert apps.get_model('publishing', 'OrganisationPortalAIDesignRun').objects.get(pk=portal_run16.pk).site_fingerprint == 'c'*64
try:
    MigrationExecutor(connection).migrate([portal_ai_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'OrganisationPortalRelease').objects.filter(pk=release15.pk).exists()
else:
    raise AssertionError('Publisher modules must not be silently discarded')
image_scope = ('publishing', '0018_site_images')
executor = MigrationExecutor(connection)
executor.migrate([image_scope])
apps = executor.loader.project_state([image_scope]).apps
assert not apps.get_model('publishing', 'SiteImageAsset').objects.exists()
site18 = apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site11.pk)
portal18 = apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal15.pk)
assert site18.hero_asset_id is None and site18.hero_alt == '' and site18.current_release_id == release11.pk
assert portal18.hero_asset_id is None and portal18.hero_alt == '' and portal18.current_release_id == release15.pk
assert (site18.hero_fit, site18.hero_position, portal18.hero_fit, portal18.hero_position) == ('cover', 'center', 'cover', 'center')
assert apps.get_model('publishing', 'OrganisationPortalAIDesignRun').objects.get(pk=portal_run16.pk).site_fingerprint == 'c'*64
try:
    MigrationExecutor(connection).migrate([portal_modules_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'OrganisationPortalRelease').objects.filter(pk=release15.pk).exists()
else:
    raise AssertionError('Published site images must not be silently discarded')
Asset18 = apps.get_model('publishing', 'SiteImageAsset')
legacy_asset = Asset18.objects.create(journal_site_id=site18.pk, image_data=b'legacy', media_type='image/jpeg',
    width=10, height=10, sha256='a'*64, default_alt='Legacy hero', uploaded_by_id=editor.pk)
legacy_sections = [{'kind':'about', 'heading':'Old heading', 'body':'Old body', 'visible':True}]
apps.get_model('publishing', 'JournalSiteDraft').objects.filter(pk=site18.pk).update(
    hero_asset_id=legacy_asset.pk, hero_alt='Legacy hero', sections_config=legacy_sections)
apps.get_model('publishing', 'OrganisationPortalDraft').objects.filter(pk=portal18.pk).update(sections_config=legacy_sections)
module_images_scope = ('publishing', '0019_sitemoduleimagereference')
executor = MigrationExecutor(connection)
executor.migrate([module_images_scope])
apps = executor.loader.project_state([module_images_scope]).apps
assert not apps.get_model('publishing', 'SiteModuleImageReference').objects.exists()
site19 = apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site18.pk)
portal19 = apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal18.pk)
assert site19.hero_asset_id == legacy_asset.pk and site19.hero_alt == 'Legacy hero'
assert site19.sections_config == legacy_sections and portal19.sections_config == legacy_sections
assert site19.current_release_id == release11.pk and portal19.current_release_id == release15.pk
assert apps.get_model('publishing', 'OrganisationPortalAIDesignRun').objects.get(pk=portal_run16.pk).site_fingerprint == 'c'*64
try:
    MigrationExecutor(connection).migrate([image_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'SiteImageAsset').objects.filter(pk=legacy_asset.pk).exists()
else:
    raise AssertionError('Module image references must not be silently discarded')
Reference19 = apps.get_model('publishing', 'SiteModuleImageReference')
reference19 = Reference19.objects.create(asset_id=legacy_asset.pk, journal_draft_id=site19.pk)
pages_scope = ('publishing', '0020_journalsitedraft_pages_config_and_more')
executor = MigrationExecutor(connection)
executor.migrate([pages_scope])
apps = executor.loader.project_state([pages_scope]).apps
site20 = apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site19.pk)
portal20 = apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal19.pk)
assert site20.pages_config == [] and portal20.pages_config == []
assert apps.get_model('publishing', 'JournalSiteRelease').objects.get(pk=release11.pk).pages_config == []
assert apps.get_model('publishing', 'OrganisationPortalRelease').objects.get(pk=release15.pk).pages_config == []
assert site20.sections_config == legacy_sections and portal20.sections_config == legacy_sections
assert site20.hero_asset_id == legacy_asset.pk and site20.hero_alt == 'Legacy hero'
assert apps.get_model('publishing', 'SiteModuleImageReference').objects.filter(pk=reference19.pk).exists()
assert apps.get_model('publishing', 'OrganisationPortalAIDesignRun').objects.get(pk=portal_run16.pk).site_fingerprint == 'c'*64
try:
    MigrationExecutor(connection).migrate([module_images_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'SiteModuleImageReference').objects.filter(pk=reference19.pk).exists()
else:
    raise AssertionError('Independent page snapshots must not be silently discarded')
legacy_pages = [{'slug':'about', 'title':'Old page', 'body':'Old text', 'visible':True}]
apps.get_model('publishing', 'JournalSiteDraft').objects.filter(pk=site20.pk).update(pages_config=legacy_pages)
apps.get_model('publishing', 'OrganisationPortalDraft').objects.filter(pk=portal20.pk).update(pages_config=legacy_pages)
page_ai_scope = ('publishing', '0021_journalaipolicy_site_pages_enabled_and_more')
executor = MigrationExecutor(connection)
executor.migrate([page_ai_scope])
apps = executor.loader.project_state([page_ai_scope]).apps
assert not apps.get_model('publishing', 'JournalAIPolicy').objects.get(pk=policy10.pk).site_pages_enabled
assert not apps.get_model('publishing', 'OrganisationAIPolicy').objects.get(pk=policy16.pk).portal_pages_enabled
assert apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site20.pk).pages_config == legacy_pages
assert apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal20.pk).pages_config == legacy_pages
run21 = apps.get_model('publishing', 'JournalSiteAIDesignRun').objects.get(pk=run11.pk)
portal_run21 = apps.get_model('publishing', 'OrganisationPortalAIDesignRun').objects.get(pk=portal_run16.pk)
assert run21.target_page_slug == '' and run21.applied_option_content == {}
assert portal_run21.target_page_slug == '' and portal_run21.applied_option_content == {}
assert portal_run21.site_fingerprint == 'c'*64
assert apps.get_model('publishing', 'SiteModuleImageReference').objects.filter(pk=reference19.pk).exists()
try:
    MigrationExecutor(connection).migrate([pages_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'JournalSiteAIDesignRun').objects.filter(pk=run11.pk).exists()
else:
    raise AssertionError('Page AI history must not be silently discarded')
block_ai_scope = ('publishing', '0022_journalaipolicy_site_page_blocks_enabled_and_more')
executor = MigrationExecutor(connection)
executor.migrate([block_ai_scope])
apps = executor.loader.project_state([block_ai_scope]).apps
assert not apps.get_model('publishing', 'JournalAIPolicy').objects.get(pk=policy10.pk).site_page_blocks_enabled
assert not apps.get_model('publishing', 'OrganisationAIPolicy').objects.get(pk=policy16.pk).portal_page_blocks_enabled
assert apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site20.pk).pages_config == legacy_pages
assert apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal20.pk).pages_config == legacy_pages
try:
    MigrationExecutor(connection).migrate([page_ai_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'JournalSiteAIDesignRun').objects.filter(pk=run11.pk).exists()
else:
    raise AssertionError('Page block AI policy must not be silently discarded')
layout_ai_scope = ('publishing', '0023_page_layout_ai_policy')
executor = MigrationExecutor(connection)
executor.migrate([layout_ai_scope])
apps = executor.loader.project_state([layout_ai_scope]).apps
assert not apps.get_model('publishing', 'JournalAIPolicy').objects.get(pk=policy10.pk).site_page_layout_enabled
assert not apps.get_model('publishing', 'OrganisationAIPolicy').objects.get(pk=policy16.pk).portal_page_layout_enabled
assert apps.get_model('publishing', 'JournalSiteDraft').objects.get(pk=site20.pk).pages_config == legacy_pages
assert apps.get_model('publishing', 'OrganisationPortalDraft').objects.get(pk=portal20.pk).pages_config == legacy_pages
try:
    MigrationExecutor(connection).migrate([block_ai_scope])
except IrreversibleError:
    assert apps.get_model('publishing', 'JournalSiteAIDesignRun').objects.filter(pk=run11.pk).exists()
else:
    raise AssertionError('Page layout AI policy must not be silently discarded')
contributors_scope = ('publishing', '0024_submission_contributors')
executor = MigrationExecutor(connection)
executor.migrate([contributors_scope])
apps = executor.loader.project_state([contributors_scope]).apps
Submission24 = apps.get_model('publishing', 'Submission')
legacy_submission = Submission24.objects.get(pk=submission.pk)
assert legacy_submission.title == 'Synthetic'
assert legacy_submission.contributors_config == [] and legacy_submission.contributors_revision == 0
assert Submission24.objects.get(pk=accepted6.pk).contributors_config == []
try:
    MigrationExecutor(connection).migrate([layout_ai_scope])
except IrreversibleError:
    assert Submission24.objects.filter(pk=submission.pk).exists()
else:
    raise AssertionError('Contributor metadata must not be silently discarded')
change_scope = ('publishing', '0025_contributor_change_requests')
executor = MigrationExecutor(connection)
executor.migrate([change_scope])
apps = executor.loader.project_state([change_scope]).apps
Submission25 = apps.get_model('publishing', 'Submission')
Change25 = apps.get_model('publishing', 'ContributorChangeRequest')
assert Submission25.objects.get(pk=submission.pk).contributors_config == []
assert not Change25.objects.exists(), 'No author change requests should be inferred'
change25 = Change25.objects.create(submission_id=submission.pk, requested_by_id=author.pk,
    reason='Fictional migration test', before=[], proposed=[], base_revision=0, acknowledged=True)
old_approved25 = Change25.objects.create(submission_id=accepted6.pk, requested_by_id=author.pk,
    reason='Previous approval', before=[], proposed=[], base_revision=0, acknowledged=True,
    status='approved', editor_attested=True, closed_by_id=editor.pk)
try:
    MigrationExecutor(connection).migrate([contributors_scope])
except IrreversibleError:
    assert Change25.objects.filter(pk=change25.pk).exists()
else:
    raise AssertionError('Author change history must not be silently discarded')
evidence_scope = ('publishing', '0026_contributor_evidence_and_checks')
executor = MigrationExecutor(connection)
executor.migrate([evidence_scope])
apps = executor.loader.project_state([evidence_scope]).apps
Change26 = apps.get_model('publishing', 'ContributorChangeRequest')
Evidence26 = apps.get_model('publishing', 'ContributorEvidence')
Confirmation26 = apps.get_model('publishing', 'ContributorConfirmationEvent')
assert Change26.objects.filter(pk=change25.pk, reason='Fictional migration test').exists()
assert Change26.objects.filter(pk=old_approved25.pk, status='approved', editor_attested=True).exists()
assert not Evidence26.objects.exists() and not Confirmation26.objects.exists(), 'No evidence or consent may be inferred'
try:
    MigrationExecutor(connection).migrate([change_scope])
except IrreversibleError:
    assert Change26.objects.filter(pk=change25.pk).exists()
else:
    raise AssertionError('Stored evidence and manual checks must not be silently discarded')
response_scope = ('publishing', '0027_account_author_responses')
executor = MigrationExecutor(connection)
executor.migrate([response_scope])
apps = executor.loader.project_state([response_scope]).apps
Change27 = apps.get_model('publishing', 'ContributorChangeRequest')
Response27 = apps.get_model('publishing', 'ContributorResponseRequest')
ResponseEvent27 = apps.get_model('publishing', 'ContributorResponseEvent')
assert Change27.objects.filter(pk=old_approved25.pk, status='approved').exists()
assert not Response27.objects.exists() and not ResponseEvent27.objects.exists(), 'No account response may be inferred'
try:
    MigrationExecutor(connection).migrate([evidence_scope])
except IrreversibleError:
    assert Change27.objects.filter(pk=old_approved25.pk).exists()
else:
    raise AssertionError('Author response history must not be silently discarded')
deadline_scope = ('publishing', '0028_review_deadlines')
executor = MigrationExecutor(connection)
executor.migrate([deadline_scope])
apps = executor.loader.project_state([deadline_scope]).apps
Assignment28 = apps.get_model('publishing', 'ReviewAssignment')
Deadline28 = apps.get_model('publishing', 'ReviewDeadlineEvent')
legacy_review28 = Assignment28.objects.get(pk=assignment.pk)
assert legacy_review28.due_on is None and legacy_review28.deadline_revision == 0
assert not Deadline28.objects.exists(), 'No deadlines or events may be inferred'
try:
    MigrationExecutor(connection).migrate([response_scope])
except IrreversibleError:
    assert Assignment28.objects.filter(pk=assignment.pk).exists()
else:
    raise AssertionError('Review deadline history must not be silently discarded')
notice_scope = ('publishing', '0029_review_notices')
executor = MigrationExecutor(connection)
executor.migrate([notice_scope])
apps = executor.loader.project_state([notice_scope]).apps
Assignment29 = apps.get_model('publishing', 'ReviewAssignment')
Notice29 = apps.get_model('publishing', 'ReviewNotice')
assert Assignment29.objects.filter(pk=assignment.pk).exists()
assert not Notice29.objects.exists(), 'No historical notices may be inferred'
try:
    MigrationExecutor(connection).migrate([deadline_scope])
except IrreversibleError:
    assert Assignment29.objects.filter(pk=assignment.pk).exists()
else:
    raise AssertionError('Review notice history must not be silently discarded')
print('legacy workflow preserved')
'''
        with tempfile.TemporaryDirectory(prefix="ips-migration-") as temp_dir:
            env = os.environ.copy()
            env["IPS_MIGRATION_TEST_DB"] = str(Path(temp_dir) / "legacy.sqlite3")
            result = subprocess.run([sys.executable, "-c", script], cwd=Path(__file__).resolve().parents[2],
                                    env=env, text=True, capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("legacy workflow preserved", result.stdout)
