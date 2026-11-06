import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import Membership, ReviewAssignment, Submission, SubmissionVersion, SupportingFile
from publishing.tests import test_workflow as workflow

PDF = workflow.PDF


def pdf(name, data=PDF):
    return SimpleUploadedFile(name, data, content_type='application/pdf')


@override_settings(DEBUG=True)
class SupportingFileTests(TestCase):
    setUp = workflow.WorkflowTests.setUp
    create_draft = workflow.WorkflowTests.create_draft
    submit = workflow.WorkflowTests.submit
    invite = workflow.WorkflowTests.invite
    finish_review = workflow.WorkflowTests.finish_review
    decide = workflow.WorkflowTests.decide

    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix='ips-support-')
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def upload(self, submission, *, editor=True, reviewer=True):
        self.client.force_login(self.author)
        data = {}
        if editor:
            data['editor_supporting_files'] = [pdf('editor-private.pdf')]
        if reviewer:
            data['reviewer_supporting_files'] = [pdf('review-data.pdf', b'%PDF-1.4\nreview data\n%%EOF')]
            data['reviewer_files_confirmed'] = 'on'
        return self.client.post(reverse('manage_supporting_files', args=[submission.pk]), data)

    def check(self, item):
        self.client.force_login(self.editor)
        return self.client.post(reverse('check_reviewer_file', args=[item.pk]), {'confirm':'yes'})

    def test_roles_manual_check_and_current_round_scope(self):
        submission = self.create_draft()
        self.assertEqual(self.upload(submission).status_code, 302)
        editorial = SupportingFile.objects.get(visibility='editor')
        reviewer_file = SupportingFile.objects.get(visibility='reviewer')
        url = lambda item: reverse('download_supporting_file', args=[item.pk])
        self.assertContains(self.client.get(reverse('manage_supporting_files', args=[submission.pk])), 'editor-private.pdf')
        self.assertEqual(self.client.get(url(editorial)).status_code, 200)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(url(reviewer_file)).status_code, 404)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(reverse('manage_supporting_files', args=[submission.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse('check_reviewer_file', args=[reviewer_file.pk]), {'confirm':'yes'}).status_code, 404)
        self.submit(submission)
        self.assertEqual(self.client.get(reverse('manage_supporting_files', args=[submission.pk])).status_code, 409)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse('review_invite', args=[submission.pk]), {'reviewer':self.reviewer.pk}).status_code, 409)
        self.assertEqual(self.client.post(reverse('check_reviewer_file', args=[reviewer_file.pk]), {}).status_code, 400)
        self.assertEqual(self.check(reviewer_file).status_code, 302)
        self.assertEqual(self.client.get(url(editorial)).status_code, 200)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(url(reviewer_file)).status_code, 404)
        self.assertEqual(self.client.post(reverse('review_respond', args=[assignment.pk]), {'response':'accept','declaration':'none'}).status_code, 302)
        response = self.client.get(url(reviewer_file))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), b'%PDF-1.4\nreview data\n%%EOF')
        self.assertEqual(response['X-Content-Type-Options'], 'nosniff')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.get(url(editorial)).status_code, 404)
        page = self.client.get(reverse('review_detail', args=[assignment.pk]))
        self.assertContains(page, 'review-data.pdf')
        self.assertNotContains(page, 'editor-private.pdf')
        self.client.force_login(self.other_reviewer)
        self.assertEqual(self.client.get(url(reviewer_file)).status_code, 404)
        self.client.force_login(self.reviewer)
        Membership.objects.filter(user=self.reviewer, journal=self.journal).update(active=False)
        self.assertEqual(self.client.get(url(reviewer_file)).status_code, 404)

    def test_reviewer_supporting_file_requires_active_submission(self):
        submission = self.create_draft()
        self.upload(submission, editor=False)
        item = SupportingFile.objects.get()
        self.submit(submission)
        self.check(item)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse('review_respond', args=[assignment.pk]),
            {'response':'accept', 'declaration':'none'}).status_code, 302)
        url = reverse('download_supporting_file', args=[item.pk])
        for active_status in (Submission.Status.SUBMITTED, Submission.Status.UNDER_REVIEW):
            Submission.objects.filter(pk=submission.pk).update(status=active_status)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF-'))
        version = submission.versions.get(number=1)
        later = SubmissionVersion.objects.create(submission=submission, number=2,
            manuscript=version.manuscript.name, uploaded_by=self.author)
        before = submission.events.filter(action='supporting_file_downloaded').count()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertNotContains(self.client.get(reverse('review_detail', args=[assignment.pk])), 'review-data.pdf')
        later.delete()
        for status in (Submission.Status.DRAFT, Submission.Status.REVISION_REQUESTED,
                       Submission.Status.ACCEPTED, Submission.Status.REJECTED):
            with self.subTest(status=status):
                Submission.objects.filter(pk=submission.pk).update(status=status)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertNotContains(self.client.get(reverse('review_detail', args=[assignment.pk])), 'review-data.pdf')
        self.assertEqual(submission.events.filter(action='supporting_file_downloaded').count(), before)
        for actor in (self.author, self.editor):
            self.client.force_login(actor)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF-'))

    def test_invalid_review_report_preserves_only_current_file_links(self):
        submission = self.create_draft()
        self.upload(submission, editor=False)
        item = SupportingFile.objects.get()
        self.submit(submission)
        self.check(item)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse('review_respond', args=[assignment.pk]),
            {'response':'accept', 'declaration':'none'}).status_code, 302)
        report = reverse('review_report', args=[assignment.pk])
        manuscript = reverse('review_file', args=[assignment.pk])
        supporting = reverse('download_supporting_file', args=[item.pk])
        page = self.client.post(report, {'recommendation':'major'})
        self.assertEqual(page.status_code, 400)
        self.assertContains(page, manuscript, status_code=400)
        self.assertContains(page, supporting, status_code=400)
        self.assertTrue(page.context['form'].errors)

        Path(item.file.path).write_bytes(b'%PDF-1.4\nchanged fictional bytes')
        page = self.client.post(report, {'recommendation':'major'})
        self.assertEqual(page.status_code, 400)
        self.assertContains(page, manuscript, status_code=400)
        self.assertNotContains(page, supporting, status_code=400)
        self.assertEqual(self.client.get(supporting).status_code, 404)

    def test_validation_limits_removal_restore_and_methods(self):
        submission = self.create_draft()
        manage = reverse('manage_supporting_files', args=[submission.pk])
        response = self.client.post(manage, {'reviewer_supporting_files':[pdf('test.pdf')]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        response = self.client.post(manage, {'editor_supporting_files':[pdf('fake.pdf', b'not pdf')]})
        self.assertTrue(response.context['form'].errors)
        response = self.client.post(manage, {'editor_supporting_files':[pdf(f'{i}.pdf') for i in range(6)]})
        self.assertTrue(response.context['form'].errors)
        self.assertFalse(SupportingFile.objects.exists())
        self.assertEqual(self.upload(submission, reviewer=False).status_code, 302)
        item = SupportingFile.objects.get()
        toggle = reverse('toggle_draft_file', args=[item.pk])
        self.assertEqual(self.client.get(toggle).status_code, 405)
        self.assertEqual(self.client.post(toggle, {'action':'remove'}).status_code, 400)
        self.assertEqual(self.client.post(toggle, {'action':'remove', 'confirm':'yes'}).status_code, 302)
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[item.pk])).status_code, 404)
        self.assertEqual(self.client.post(toggle, {'action':'restore', 'confirm':'yes'}).status_code, 302)
        item.refresh_from_db()
        self.assertIsNone(item.removed_at)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.author)
        self.assertEqual(csrf.post(toggle, {'action':'remove','confirm':'yes'}).status_code, 403)
        self.submit(submission)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(toggle, {'action':'remove','confirm':'yes'}).status_code, 409)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(reverse('download_supporting_file', args=[item.pk])).status_code, 404)

    def test_changed_bytes_and_metadata_are_never_given_to_reviewer(self):
        submission = self.create_draft()
        self.upload(submission, editor=False)
        item = SupportingFile.objects.get()
        self.submit(submission)
        self.assertEqual(self.check(item).status_code, 302)
        item.original_name = 'changed-name.pdf'
        item.save(update_fields=['original_name'])
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse('review_invite', args=[submission.pk]), {'reviewer':self.reviewer.pk}).status_code, 409)
        item.original_name = 'review-data.pdf'
        item.save(update_fields=['original_name'])
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.client.post(reverse('review_respond', args=[assignment.pk]), {'response':'accept','declaration':'none'})
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[item.pk])).status_code, 200)
        Path(item.file.path).write_bytes(b'%PDF-1.4\nchanged bytes')
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[item.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('review_detail', args=[assignment.pk])), 'review-data.pdf')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[item.pk])).status_code, 404)

    def test_revision_files_are_version_bound(self):
        submission = self.create_draft()
        self.upload(submission, editor=False)
        first_file = SupportingFile.objects.get()
        self.submit(submission)
        self.check(first_file)
        first = self.invite(submission)
        self.finish_review(first)
        self.assertEqual(self.decide(submission).status_code, 302)
        self.client.force_login(self.author)
        page = self.client.get(reverse('submission_revision', args=[submission.pk]))
        response = self.client.post(reverse('submission_revision', args=[submission.pk]), {
            'revision_token':page.context['revision_token'], 'manuscript':pdf('revision.pdf'),
            'author_response':'We revised the fictional paper.',
            'reviewer_supporting_files':[pdf('new-supplement.pdf')], 'reviewer_files_confirmed':'on',
        })
        self.assertEqual(response.status_code, 302)
        second_file = SupportingFile.objects.get(original_name='new-supplement.pdf')
        self.assertNotEqual(first_file.version_id, second_file.version_id)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[first_file.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[second_file.pk])).status_code, 404)
        self.assertEqual(self.check(second_file).status_code, 302)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse('review_invite', args=[submission.pk]), {'reviewer':self.reviewer.pk}).status_code, 302)
        second = ReviewAssignment.objects.get(submission=submission, reviewer=self.reviewer, round__number=2)
        self.client.force_login(self.reviewer)
        self.client.post(reverse('review_respond', args=[second.pk]), {'response':'accept','declaration':'none'})
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[second_file.pk])).status_code, 200)
        self.assertEqual(self.client.get(reverse('download_supporting_file', args=[first_file.pk])).status_code, 404)

    def test_upload_exception_cleans_saved_bytes(self):
        submission = self.create_draft()
        before = set(Path(self.media_dir).rglob('*.pdf'))
        with patch('publishing.supporting_files.SupportingFile.save', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.upload(submission, reviewer=False)
        self.assertFalse(SupportingFile.objects.exists())
        self.assertEqual(before, set(Path(self.media_dir).rglob('*.pdf')))

    def test_limit_applies_across_existing_uploads_and_restore(self):
        submission = self.create_draft()
        manage = reverse('manage_supporting_files', args=[submission.pk])
        self.assertEqual(self.client.post(manage, {'editor_supporting_files':[pdf(f'file-{i}.pdf') for i in range(5)]}).status_code, 302)
        first = SupportingFile.objects.first()
        self.assertEqual(self.client.post(manage, {'editor_supporting_files':[pdf('sixth.pdf')]}).status_code, 409)
        toggle = reverse('toggle_draft_file', args=[first.pk])
        self.assertEqual(self.client.post(toggle, {'action':'remove','confirm':'yes'}).status_code, 302)
        self.assertEqual(self.client.post(manage, {'editor_supporting_files':[pdf('sixth.pdf')]}).status_code, 302)
        self.assertEqual(self.client.post(toggle, {'action':'restore','confirm':'yes'}).status_code, 409)
        self.assertEqual(SupportingFile.objects.filter(removed_at__isnull=True).count(), 5)

    def test_revision_exception_cleans_supporting_and_manuscript_files(self):
        submission = self.create_draft()
        self.submit(submission)
        first = self.invite(submission)
        self.finish_review(first)
        self.assertEqual(self.decide(submission).status_code, 302)
        self.client.force_login(self.author)
        url = reverse('submission_revision', args=[submission.pk])
        token = self.client.get(url).context['revision_token']
        before = set(Path(self.media_dir).rglob('*.pdf'))
        with patch('publishing.views.ReviewRound.objects.create', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.client.post(url, {'revision_token':token, 'manuscript':pdf('revision.pdf'),
                    'author_response':'Revised.', 'editor_supporting_files':[pdf('supplement.pdf')]})
        self.assertEqual(submission.versions.count(), 1)
        self.assertFalse(SupportingFile.objects.exists())
        self.assertEqual(before, set(Path(self.media_dir).rglob('*.pdf')))
