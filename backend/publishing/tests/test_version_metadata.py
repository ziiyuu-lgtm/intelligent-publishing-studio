import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from publishing.models import (EditorialDecision, Membership, Organisation, Journal, ReviewAIPermissionRequest,
    ReviewAssignment, ReviewRound, Submission, SubmissionVersion)
from publishing.review_ai_consent import PROVIDER, PURPOSE, report_fingerprint
from publishing.tests import test_review_comparison

PDF = b'%PDF-1.4\n% fictional metadata test\n%%EOF\n'


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class VersionMetadataTests(TestCase):
    create_draft = test_review_comparison.ReviewComparisonTests.create_draft
    submit = test_review_comparison.ReviewComparisonTests.submit
    invite = test_review_comparison.ReviewComparisonTests.invite
    report = test_review_comparison.ReviewComparisonTests.report
    add_reviewer = test_review_comparison.ReviewComparisonTests.add_reviewer

    def setUp(self):
        test_review_comparison.ReviewComparisonTests.setUp(self)
        self.report(self.assignment, 'PRIVATE FICTIONAL REVIEW')
        self.original = self.submission.versions.get(number=1)
        self.url = reverse('version_metadata_compare', args=[self.submission.pk])
        self.revision_url = reverse('submission_revision', args=[self.submission.pk])
        self.client.force_login(self.editor)
        response = self.client.post(reverse('editorial_decision', args=[self.submission.pk]),
            {'outcome':'major', 'author_message':'Please improve the description.'})
        self.assertEqual(response.status_code, 302)

    def revision_data(self, title='Updated fictional title', abstract='Updated fictional abstract.'):
        self.client.force_login(self.author)
        page = self.client.get(self.revision_url)
        self.assertEqual(page.status_code, 200)
        return {'revision_token':page.context['revision_token'], 'revised_title':title,
            'revised_abstract':abstract, 'author_response':'Fictional response.',
            'manuscript':SimpleUploadedFile('revision.pdf', PDF, content_type='application/pdf')}

    def test_revision_records_metadata_and_preserves_original_review_round(self):
        self.assertEqual((self.original.title_snapshot, self.original.abstract_snapshot),
                         ('An imaginary test manuscript', 'Fictional abstract.'))
        data = self.revision_data()
        self.assertContains(self.client.get(self.revision_url), 'Title and abstract for this version')
        self.assertEqual(self.client.post(self.revision_url, data).status_code, 302)
        self.original.refresh_from_db(); self.submission.refresh_from_db()
        revised = self.submission.versions.get(number=2)
        self.assertEqual((self.original.title_snapshot, self.original.abstract_snapshot),
                         ('An imaginary test manuscript', 'Fictional abstract.'))
        self.assertEqual((revised.title_snapshot, revised.abstract_snapshot),
                         ('Updated fictional title', 'Updated fictional abstract.'))
        self.assertEqual((self.submission.title, self.submission.abstract),
                         (revised.title_snapshot, revised.abstract_snapshot))
        self.client.force_login(self.reviewer)
        page = self.client.get(reverse('review_detail', args=[self.assignment.pk]))
        self.assertContains(page, 'An imaginary test manuscript')
        self.assertContains(page, 'Fictional abstract.')
        self.assertNotContains(page, 'Updated fictional title')
        self.assertNotContains(page, 'Updated fictional abstract.')
        workspace = self.client.get(reverse('workspace'))
        self.assertContains(workspace, 'An imaginary test manuscript')
        self.assertNotContains(workspace, 'Updated fictional title')
        self.client.force_login(self.editor)
        compare = self.client.get(self.url)
        self.assertEqual((compare.context['title_state'], compare.context['abstract_state']), ('changed','changed'))
        self.assertContains(compare, 'An imaginary test manuscript')
        self.assertContains(compare, 'Updated fictional title')
        self.assertNotContains(compare, 'PRIVATE FICTIONAL REVIEW')
        self.assertIn('no-store', compare['Cache-Control'])
        self.assertContains(self.client.get(reverse('returned_revision', args=[self.submission.pk])), self.url)

    def test_legacy_metadata_is_unknown_not_invented(self):
        self.original.title_snapshot = None; self.original.abstract_snapshot = None
        self.original.save(update_fields=['title_snapshot','abstract_snapshot'])
        self.assertEqual(self.client.post(self.revision_url, self.revision_data()).status_code, 302)
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertEqual((page.context['title_state'],page.context['abstract_state']), ('unknown','unknown'))
        self.assertContains(page, 'Version metadata not recorded')
        self.assertContains(page, 'Updated fictional title')
        self.assertNotContains(page, 'An imaginary test manuscript')
        self.client.force_login(self.reviewer)
        page = self.client.get(reverse('review_detail', args=[self.assignment.pk]))
        self.assertContains(page, 'The title and abstract were not recorded for this version.')
        self.assertNotContains(page, 'Updated fictional title')
        self.assertNotContains(page, 'Updated fictional abstract.')
        self.assertNotContains(self.client.get(reverse('workspace')), 'Updated fictional title')
        self.assertNotContains(self.client.get(reverse('review_notice_inbox')), 'Updated fictional title')
        permission = reverse('reviewer_review_ai_permission', args=[self.assignment.pk])
        self.assertNotContains(self.client.get(permission), 'Updated fictional title')
        ReviewAIPermissionRequest.objects.create(submission=self.submission, review_round=self.assignment.round,
            version=self.original, assignment=self.assignment, requested_by=self.editor, recipient=self.reviewer,
            subject='reviewer', provider=PROVIDER, purpose=PURPOSE, source_fingerprint=report_fingerprint(self.assignment))
        self.assertNotContains(self.client.get(reverse('review_ai_permission_inbox')), 'Updated fictional title')

    def test_metadata_change_invalidates_stale_form_and_preserves_author_text(self):
        data = self.revision_data(title='<New title>', abstract='Revised <abstract>')
        Submission.objects.filter(pk=self.submission.pk).update(title='Changed by another session')
        page = self.client.post(self.revision_url, data)
        self.assertContains(page, '&lt;New title&gt;', status_code=409)
        self.assertContains(page, 'Revised &lt;abstract&gt;', status_code=409)
        self.assertContains(page, 'Fictional response.', status_code=409)
        self.assertEqual(self.submission.versions.count(), 1)

    def test_invalid_or_partial_metadata_does_not_submit(self):
        for missing in ('revised_title','revised_abstract'):
            data = self.revision_data(); data.pop(missing)
            self.assertEqual(self.client.post(self.revision_url, data).status_code, 200)
            self.assertEqual(self.submission.versions.count(), 1)
        data = self.revision_data(title='   ')
        self.assertEqual(self.client.post(self.revision_url, data).status_code, 200)
        self.assertEqual(self.submission.versions.count(), 1)

    def test_legacy_clients_can_submit_with_unchanged_current_metadata(self):
        data = self.revision_data(); data.pop('revised_title'); data.pop('revised_abstract')
        self.assertEqual(self.client.post(self.revision_url, data).status_code, 302)
        revised = self.submission.versions.get(number=2)
        self.assertEqual((revised.title_snapshot,revised.abstract_snapshot),
                         ('An imaginary test manuscript', 'Fictional abstract.'))
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url).context['title_state'], 'unchanged')

    def test_compare_is_scoped_read_only_and_escapes_text(self):
        data = self.revision_data(title='<script>alert(1)</script>', abstract='New <b>abstract</b>')
        self.assertEqual(self.client.post(self.revision_url, data).status_code, 302)
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, '<script>alert(1)</script>')
        self.assertContains(page, 'New &lt;b&gt;abstract&lt;/b&gt;')
        self.assertEqual(self.client.post(self.url).status_code, 405)
        for query in ('?before=9','?before=-1','?before=1&before=2','?after=foo','?before=９'):
            self.assertEqual(self.client.get(self.url+query).status_code, 404)
        for user in (self.reviewer,self.other_editor,self.coordinator,self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(self.url, {'before':'2','after':'1'}).status_code, 200)
        with translation.override('zh-hans'):
            self.assertContains(self.client.get(self.url, HTTP_ACCEPT_LANGUAGE='zh-hans'), '比较版本标题与摘要')

    def test_mismatched_review_round_does_not_leak_foreign_snapshot(self):
        other_author = get_user_model().objects.create_user('foreign-author')
        other_submission = Submission.objects.create(journal=self.other, author=other_author, title='Foreign current',
            abstract='Foreign current abstract', manuscript='fictional.pdf', status='submitted')
        foreign_version = SubmissionVersion.objects.create(submission=other_submission, number=1,
            manuscript='foreign.pdf', uploaded_by=other_author, title_snapshot='FOREIGN PRIVATE TITLE',
            abstract_snapshot='FOREIGN PRIVATE ABSTRACT')
        foreign_round = ReviewRound.objects.create(submission=other_submission, version=foreign_version, number=1)
        bad = ReviewAssignment.objects.create(submission=self.submission, round=foreign_round, reviewer=self.reviewer,
            invited_by=self.editor, status='invited', conflict='unknown')
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(reverse('workspace')), 'FOREIGN PRIVATE TITLE')
        self.assertEqual(self.client.get(reverse('review_detail', args=[bad.pk])).status_code, 404)

    def test_database_does_not_allow_half_a_metadata_snapshot(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            SubmissionVersion.objects.filter(pk=self.original.pk).update(title_snapshot=None)


class VersionMetadataMigrationTests(SimpleTestCase):
    def test_upgrade_does_not_invent_historical_metadata_and_is_one_way(self):
        import os
        from pathlib import Path
        import subprocess
        import sys
        script = r'''
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
from django.conf import settings
settings.DATABASES['default']['NAME'] = os.environ['IPS_MIGRATION_TEST_DB']
import django
django.setup()
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.exceptions import IrreversibleError
old=('publishing','0042_reviewaireplynotice')
new=('publishing','0043_submissionversion_abstract_snapshot_and_more')
executor=MigrationExecutor(connection);executor.migrate([old]);apps=executor.loader.project_state([old]).apps
User=apps.get_model('auth','User'); Org=apps.get_model('publishing','Organisation')
Journal=apps.get_model('publishing','Journal'); Submission=apps.get_model('publishing','Submission')
Version=apps.get_model('publishing','SubmissionVersion')
user=User.objects.create(username='metadata-migration',password='!')
org=Org.objects.create(name='Fictional',slug='metadata-migration')
journal=Journal.objects.create(organisation=org,name='Fictional',slug='metadata-migration')
sub=Submission.objects.create(journal=journal,author=user,title='Current title',abstract='Current abstract',manuscript='fictional.pdf')
v=Version.objects.create(submission=sub,number=1,manuscript='fictional.pdf',uploaded_by=user)
executor=MigrationExecutor(connection);executor.migrate([new]);apps=executor.loader.project_state([new]).apps
Version=apps.get_model('publishing','SubmissionVersion')
v=Version.objects.get(pk=v.pk)
assert v.title_snapshot is None and v.abstract_snapshot is None
v.title_snapshot='New title';v.abstract_snapshot='New abstract';v.save()
try: MigrationExecutor(connection).migrate([old])
except IrreversibleError: pass
else: raise AssertionError('Recorded version metadata must not be silently discarded')
assert Version.objects.get(pk=v.pk).title_snapshot=='New title'
print('version metadata migration preserved')
'''
        with tempfile.TemporaryDirectory(prefix='ips-version-migration-') as temp_dir:
            env=os.environ.copy(); env['IPS_MIGRATION_TEST_DB']=str(Path(temp_dir)/'legacy.sqlite3')
            result=subprocess.run([sys.executable,'-c',script],cwd=Path(__file__).resolve().parents[2],
                env=env,text=True,capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('version metadata migration preserved',result.stdout)
