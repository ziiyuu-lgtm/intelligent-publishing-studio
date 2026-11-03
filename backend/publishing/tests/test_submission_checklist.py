import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Journal, Submission, SubmissionEvent, SupportingFile
from publishing.tests import test_workflow as workflow


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class SubmissionChecklistTests(TestCase):
    setUp = workflow.WorkflowTests.setUp
    create_draft = workflow.WorkflowTests.create_draft

    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix='ips-checklist-')
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def review(self, draft):
        return self.client.get(reverse('submission_checklist', args=[draft.pk]))

    def post_review(self, draft, token='', confirm='yes'):
        return self.client.post(reverse('submission_submit', args=[draft.pk]), {'review_token':token, 'confirm':confirm})

    def test_review_is_private_and_confirmation_is_required(self):
        draft = self.create_draft()
        page = self.review(draft)
        self.assertContains(page, 'Review your submission')
        self.assertTrue(page.context['ready'])
        token = page.context['review_token']
        self.assertEqual(self.post_review(draft).status_code, 409)
        self.assertEqual(self.post_review(draft, token, '').status_code, 409)
        self.client.force_login(self.editor)
        self.assertEqual(self.review(draft).status_code, 404)
        self.assertEqual(self.post_review(draft, token).status_code, 404)
        self.client.force_login(self.author)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.author)
        self.assertEqual(csrf_client.post(reverse('submission_submit', args=[draft.pk]), {'review_token':token,'confirm':'yes'}).status_code, 403)
        self.assertEqual(self.post_review(draft, token).status_code, 302)
        draft.refresh_from_db()
        self.assertEqual(draft.status, Submission.Status.SUBMITTED)
        self.assertEqual(self.post_review(draft, token).status_code, 409)
        self.assertEqual(SubmissionEvent.objects.filter(submission=draft, action='submitted').count(), 1)

    def test_contributor_edits_invalidate_the_review(self):
        draft = self.create_draft()
        token = self.review(draft).context['review_token']
        data = {'revision':'0', 'authors-TOTAL_FORMS':'1', 'authors-INITIAL_FORMS':'0',
            'authors-MIN_NUM_FORMS':'0', 'authors-MAX_NUM_FORMS':'100',
            'authors-0-name':'Fictional Author', 'authors-0-email':'author@example.org',
            'authors-0-corresponding':'on', 'authors-0-ORDER':'1'}
        self.assertEqual(self.client.post(reverse('submission_contributors', args=[draft.pk]), data).status_code, 302)
        stale = self.post_review(draft, token)
        self.assertEqual(stale.status_code, 409)
        self.assertContains(stale, 'Fictional Author', status_code=409)
        self.assertTrue(stale.context['ready'])
        self.assertEqual(self.post_review(draft, stale.context['review_token']).status_code, 302)

    def test_primary_file_change_or_missing_file_prevents_stale_submit(self):
        draft = self.create_draft()
        token = self.review(draft).context['review_token']
        version = draft.versions.get(number=1)
        Path(version.manuscript.path).write_bytes(b'%PDF-1.4\nchanged draft\n%%EOF')
        response = self.post_review(draft, token)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.context['ready'])
        Path(version.manuscript.path).unlink()
        missing = self.post_review(draft, response.context['review_token'])
        self.assertEqual(missing.status_code, 409)
        self.assertFalse(missing.context['ready'])
        self.assertIn('manuscript_unavailable', missing.context['errors'])
        draft.refresh_from_db()
        self.assertEqual(draft.status, Submission.Status.DRAFT)

    def test_supporting_file_change_blocks_submission_and_can_be_removed(self):
        draft = self.create_draft()
        upload = reverse('manage_supporting_files', args=[draft.pk])
        response = self.client.post(upload, {'reviewer_supporting_files':[SimpleUploadedFile('appendix.pdf', workflow.PDF)],
            'reviewer_files_confirmed':'on'})
        self.assertEqual(response.status_code, 302)
        item = SupportingFile.objects.get()
        page = self.review(draft)
        self.assertContains(page, 'appendix.pdf')
        token = page.context['review_token']
        Path(item.file.path).write_bytes(b'%PDF-1.4\ncorrupt\n')
        blocked = self.post_review(draft, token)
        self.assertEqual(blocked.status_code, 409)
        self.assertIn('supporting_unavailable', blocked.context['errors'])
        self.assertFalse(blocked.context['review_token'])
        remove = reverse('toggle_draft_file', args=[item.pk])
        self.assertEqual(self.client.post(remove, {'action':'remove','confirm':'yes'}).status_code, 302)
        updated = self.review(draft)
        self.assertTrue(updated.context['ready'])
        self.assertEqual(self.post_review(draft, updated.context['review_token']).status_code, 302)

    def test_review_tokens_are_submission_specific_and_expire(self):
        draft = self.create_draft()
        with patch('django.core.signing.time.time', return_value=1000):
            token = self.review(draft).context['review_token']
        with patch('django.core.signing.time.time', return_value=1000 + 7201):
            expired = self.post_review(draft, token)
        self.assertEqual(expired.status_code, 409)
        self.assertEqual(draft.rounds.count(), 0)
        self.client.force_login(self.author)
        response = self.client.post(reverse('submission_create', args=[self.journal.slug]), {
            'title':'A second fictional draft', 'abstract':'A fictional abstract.',
            'manuscript':SimpleUploadedFile('second.pdf', workflow.PDF)})
        self.assertEqual(response.status_code, 302)
        second = Submission.objects.exclude(pk=draft.pk).get()
        self.assertEqual(self.post_review(second, self.review(draft).context['review_token']).status_code, 409)

    def test_separate_reviewer_copy_is_displayed_and_must_exist(self):
        self.journal.review_mode = Journal.ReviewMode.SEPARATE
        self.journal.save(update_fields=['review_mode'])
        self.client.force_login(self.author)
        response = self.client.post(reverse('submission_create', args=[self.journal.slug]), {
            'title':'Fictional separate review', 'abstract':'A fictional abstract.',
            'manuscript':SimpleUploadedFile('main.pdf', workflow.PDF),
            'review_copy':SimpleUploadedFile('review.pdf', workflow.PDF), 'review_copy_confirmed':'on'})
        self.assertEqual(response.status_code, 302)
        draft = Submission.objects.get()
        page = self.review(draft)
        self.assertContains(page, 'Separate reviewer PDF')
        self.assertTrue(page.context['ready'])
        Path(draft.versions.get(number=1).review_copy.path).unlink()
        response = self.post_review(draft, page.context['review_token'])
        self.assertEqual(response.status_code, 409)
        self.assertIn('review_copy_unavailable', response.context['errors'])

    def test_empty_legacy_author_list_is_clearly_optional_and_locale_switches(self):
        draft = self.create_draft()
        page = self.review(draft)
        self.assertContains(page, 'No authors are listed.')
        self.assertContains(page, 'Existing test drafts may be submitted without a list')
        self.client.post(reverse('set_language'), {'language':'zh-hans','next':'/'})
        self.assertContains(self.review(draft), '提交前核对')
