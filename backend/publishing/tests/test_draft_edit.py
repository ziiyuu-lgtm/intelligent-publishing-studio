import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Journal, ReplacedDraftFile, Submission, SubmissionEvent
from publishing.tests import test_workflow as workflow


def pdf(name, text=b'fictional'):
    return SimpleUploadedFile(name, b'%PDF-1.4\n' + text + b'\n%%EOF', content_type='application/pdf')


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class DraftEditTests(TestCase):
    setUp = workflow.WorkflowTests.setUp
    create_draft = workflow.WorkflowTests.create_draft
    submit = workflow.WorkflowTests.submit

    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix='ips-draft-edit-')
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def edit_page(self, submission):
        return self.client.get(reverse('edit_draft', args=[submission.pk]))

    def edit(self, submission, token, **changes):
        data = {'edit_token':token, 'title':submission.title, 'abstract':submission.abstract}
        data.update(changes)
        return self.client.post(reverse('edit_draft', args=[submission.pk]), data)

    def separate_draft(self):
        self.journal.review_mode = Journal.ReviewMode.SEPARATE
        self.journal.save(update_fields=['review_mode'])
        self.client.force_login(self.author)
        response = self.client.post(reverse('submission_create', args=[self.journal.slug]), {
            'title':'Fictional original', 'abstract':'Original abstract.', 'manuscript':pdf('main.pdf',b'old manuscript'),
            'review_copy':pdf('review.pdf',b'old reviewer'), 'review_copy_confirmed':'on'})
        self.assertEqual(response.status_code, 302)
        return Submission.objects.get()

    def test_edit_metadata_and_replace_both_files_then_review(self):
        draft = self.separate_draft()
        version = draft.versions.get(number=1)
        old_manuscript, old_reviewer = version.manuscript.name, version.review_copy.name
        review_token = self.client.get(reverse('submission_checklist', args=[draft.pk])).context['review_token']
        page = self.edit_page(draft)
        self.assertContains(page, 'Edit your draft')
        result = self.edit(draft, page.context['edit_token'], title='Fictional revised title', abstract='Revised abstract.',
            manuscript=pdf('new.pdf',b'new manuscript'), review_copy=pdf('new-review.pdf',b'new reviewer'),
            review_copy_confirmed='on')
        self.assertEqual(result.status_code, 302)
        draft.refresh_from_db();version.refresh_from_db()
        self.assertEqual((draft.title, draft.abstract), ('Fictional revised title','Revised abstract.'))
        self.assertEqual((version.title_snapshot,version.abstract_snapshot), (draft.title,draft.abstract))
        self.assertEqual(draft.manuscript.name, version.manuscript.name)
        self.assertTrue(version.review_copy_confirmed)
        self.assertNotEqual(old_manuscript, version.manuscript.name)
        self.assertNotEqual(old_reviewer, version.review_copy.name)
        self.assertEqual(set(ReplacedDraftFile.objects.values_list('file',flat=True)), {old_manuscript,old_reviewer})
        self.assertTrue(Path(self.media_dir,old_manuscript).exists())
        self.assertTrue(Path(self.media_dir,old_reviewer).exists())
        self.assertEqual(SubmissionEvent.objects.filter(submission=draft, action='draft_content_updated').count(), 1)
        stale = self.client.post(reverse('submission_submit', args=[draft.pk]), {'review_token':review_token,'confirm':'yes'})
        self.assertEqual(stale.status_code, 409)
        fresh = self.client.get(reverse('submission_checklist', args=[draft.pk]))
        self.assertTrue(fresh.context['ready'])
        self.assertEqual(self.client.post(reverse('submission_submit', args=[draft.pk]),
            {'review_token':fresh.context['review_token'],'confirm':'yes'}).status_code, 302)

    def test_metadata_only_update_and_noop(self):
        draft = self.create_draft();page = self.edit_page(draft)
        self.assertEqual(self.edit(draft, page.context['edit_token'], title='A corrected title').status_code, 302)
        draft.refresh_from_db();self.assertEqual(draft.versions.get().title_snapshot,'A corrected title')
        self.assertFalse(ReplacedDraftFile.objects.exists())
        page = self.edit_page(draft)
        self.assertEqual(self.edit(draft, page.context['edit_token']).status_code, 302)
        self.assertEqual(SubmissionEvent.objects.filter(submission=draft, action='draft_content_updated').count(), 1)

    def test_missing_file_can_be_replaced_before_submission(self):
        draft = self.create_draft();version = draft.versions.get()
        old_path = Path(version.manuscript.path);old_path.unlink()
        self.assertFalse(self.client.get(reverse('submission_checklist',args=[draft.pk])).context['ready'])
        page = self.edit_page(draft)
        self.assertFalse(page.context['manuscript_available'])
        self.assertEqual(self.edit(draft, page.context['edit_token'], manuscript=pdf('replacement.pdf')).status_code, 302)
        self.assertTrue(self.client.get(reverse('submission_checklist',args=[draft.pk])).context['ready'])
        self.assertEqual(ReplacedDraftFile.objects.count(),1)
        self.assertFalse(old_path.exists())

    def test_permissions_csrf_and_submitted_state(self):
        draft = self.create_draft();url=reverse('edit_draft',args=[draft.pk])
        token = self.edit_page(draft).context['edit_token']
        for actor in (self.editor, self.reviewer, self.other_editor, self.superuser):
            self.client.force_login(actor)
            self.assertEqual(self.client.get(url).status_code,404)
            self.assertEqual(self.client.post(url, {'edit_token':token,'title':'x','abstract':'y'}).status_code,404)
        self.client.force_login(self.author)
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.author)
        self.assertEqual(csrf.post(url, {'edit_token':token,'title':'x','abstract':'y'}).status_code,403)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(url).status_code,404)
        self.submit(draft)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(url).status_code,409)
        self.assertEqual(self.client.post(url, {'edit_token':token,'title':'x','abstract':'y'}).status_code,409)

    def test_stale_form_preserves_entered_text_without_saving_uploaded_file(self):
        draft = self.create_draft();a=self.edit_page(draft).context['edit_token'];b=self.edit_page(draft).context['edit_token']
        self.assertEqual(self.edit(draft,a,title='Saved from first tab').status_code,302)
        before = set(Path(self.media_dir).rglob('*.pdf'))
        result = self.edit(draft,b,title='<script>unsaved</script>',abstract='Keep these words',manuscript=pdf('unsaved.pdf'))
        self.assertEqual(result.status_code,409)
        self.assertContains(result,'Keep these words',status_code=409)
        self.assertContains(result,'&lt;script&gt;unsaved&lt;/script&gt;',status_code=409)
        self.assertNotContains(result,'<script>unsaved</script>',status_code=409)
        self.assertEqual(before,set(Path(self.media_dir).rglob('*.pdf')))
        draft.refresh_from_db();self.assertEqual(draft.title,'Saved from first tab')

    def test_validation_and_reviewer_confirmation(self):
        draft = self.separate_draft();token=self.edit_page(draft).context['edit_token']
        result=self.edit(draft,token,title='Changed title',review_copy=pdf('new.pdf'))
        self.assertEqual(result.status_code,200)
        self.assertIn('review_copy_confirmed',result.context['form'].errors)
        result=self.edit(draft,token,manuscript=SimpleUploadedFile('bad.pdf',b'not pdf'),review_copy_confirmed='on')
        self.assertEqual(result.status_code,200)
        self.assertIn('manuscript',result.context['form'].errors)
        draft.refresh_from_db();self.assertEqual(draft.title,'Fictional original')
        self.assertFalse(ReplacedDraftFile.objects.exists())

    def test_storage_and_database_roll_back_together(self):
        draft=self.separate_draft();version=draft.versions.get();old_main=version.manuscript.name
        old_review=version.review_copy.name;before=set(Path(self.media_dir).rglob('*.pdf'))
        token=self.edit_page(draft).context['edit_token']
        with patch('publishing.draft_edit_views._event',side_effect=RuntimeError('test rollback')):
            with self.assertRaises(RuntimeError):
                self.edit(draft,token,manuscript=pdf('new.pdf'),review_copy=pdf('review.pdf'),review_copy_confirmed='on')
        draft.refresh_from_db();version.refresh_from_db()
        self.assertEqual((version.manuscript.name,version.review_copy.name),(old_main,old_review))
        self.assertEqual(before,set(Path(self.media_dir).rglob('*.pdf')))
        self.assertFalse(ReplacedDraftFile.objects.exists())

    def test_retained_file_limit_does_not_block_metadata_edits(self):
        draft=self.create_draft();version=draft.versions.get()
        for n in range(20):
            ReplacedDraftFile.objects.create(submission=draft,version=version,replaced_by=self.author,
                kind='manuscript',file=f'manuscripts/old-{n}.pdf')
        token=self.edit_page(draft).context['edit_token']
        result=self.edit(draft,token,manuscript=pdf('new.pdf'))
        self.assertEqual(result.status_code,409)
        self.assertContains(result,'retained-file replacement limit',status_code=409)
        self.assertEqual(ReplacedDraftFile.objects.count(),20)
        self.assertEqual(self.edit(draft,token,title='Metadata is still editable').status_code,302)

    def test_language_switch_and_stale_file_bytes(self):
        draft=self.create_draft();token=self.edit_page(draft).context['edit_token']
        version=draft.versions.get()
        Path(version.manuscript.path).write_bytes(b'%PDF-1.4\nfile changed elsewhere\n%%EOF')
        self.assertEqual(self.edit(draft,token,title='A proposed title').status_code,409)
        draft.refresh_from_db();self.assertNotEqual(draft.title,'A proposed title')
        self.client.post(reverse('set_language'), {'language':'zh-hans','next':'/'})
        self.assertContains(self.edit_page(draft),'编辑草稿')
