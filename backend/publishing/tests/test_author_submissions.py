from datetime import datetime, timezone as utc_timezone
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone, translation

from publishing.models import (EditorialDecision, Membership, ReviewRound, Submission,
                               SubmissionVersion)
from publishing.tests import test_workflow


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class AuthorSubmissionTrackingTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    def setUp(self):
        super().setUp()
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix='ips-author-progress-')
        self.addCleanup(directory.cleanup)
        setting = override_settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)

    def test_overview_filters_search_pagination_and_safe_html(self):
        mine = self.create_draft(title='<script>Fictional research</script>')
        for i in range(13):
            Submission.objects.create(author=self.author, journal=self.other, title=f'Sample topic {i:02}',
                                      abstract='test', manuscript='test.pdf', status='revision_requested')
        self.client.force_login(self.author)
        url = reverse('author_submissions')
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context['counts']['total'], 14)
        self.assertEqual(page.context['counts']['revision'], 13)
        self.assertEqual(len(page.context['page'].object_list), 12)
        self.assertEqual(self.client.get(url, {'page': 2}).context['page'].number, 2)
        page = self.client.get(url, {'q': 'FICTIONAL', 'status': 'draft'})
        self.assertContains(page, '&lt;script&gt;Fictional research&lt;/script&gt;')
        self.assertNotContains(page, '<script>Fictional research</script>')
        self.assertNotContains(page, 'Sample topic')
        self.assertContains(page, reverse('author_submission_progress', args=[mine.pk]))
        self.assertEqual(self.client.get(url, {'status': 'bogus'}).context['selected'], 'all')
        self.assertContains(self.client.get(url, {'q': 'journal b'}), 'Sample topic')
        self.assertContains(self.client.get(reverse('workspace')), url)
        self.assertEqual(self.client.post(url).status_code, 405)
        self.assertIn('no-store', self.client.get(url)['Cache-Control'])

    def test_scope_current_owner_active_organisation_and_no_extra_role_required(self):
        mine = self.create_draft()
        url = reverse('author_submissions')
        detail = reverse('author_submission_progress', args=[mine.pk])
        self.client.logout()
        self.assertEqual(self.client.get(detail).status_code, 302)
        for user in (self.editor, self.reviewer, self.coordinator, self.other_editor, self.superuser):
            self.client.force_login(user)
            self.assertNotContains(self.client.get(url), mine.title)
            self.assertEqual(self.client.get(detail).status_code, 404)
        Membership.objects.filter(user=self.author, journal=self.journal, role='author').update(active=False)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(url), mine.title)
        self.assertEqual(self.client.get(detail).status_code, 200)
        self.journal.active = False
        self.journal.save(update_fields=['active'])
        self.assertNotContains(self.client.get(url), mine.title)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.journal.active = True
        self.journal.save(update_fields=['active'])
        self.journal.organisation.active = False
        self.journal.organisation.save(update_fields=['active'])
        self.assertEqual(self.client.get(detail).status_code, 404)

    def test_owner_reassignment_changes_access(self):
        mine = self.create_draft()
        detail = reverse('author_submission_progress', args=[mine.pk])
        next_owner = get_user_model().objects.create_user('another-fictional-owner')
        mine.author = next_owner
        mine.save(update_fields=['author'])
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.client.force_login(next_owner)
        self.assertEqual(self.client.get(detail).status_code, 200)

    def test_timeline_shows_recorded_workflow_without_private_review_data(self):
        mine = self.create_draft()
        url = reverse('author_submission_progress', args=[mine.pk])
        page = self.client.get(url)
        self.assertContains(page, 'Draft created')
        self.assertNotContains(page, 'Submission received')
        self.submit(mine)
        assignment = self.invite(mine)
        self.client.force_login(self.reviewer)
        self.client.post(reverse('review_respond', args=[assignment.pk]), {'response': 'accept', 'declaration': 'none'})
        self.client.post(reverse('review_report', args=[assignment.pk]),
                         {'recommendation': 'major', 'comments': 'Private report XYZ'})
        self.client.force_login(self.editor)
        self.client.post(reverse('editorial_decision', args=[mine.pk]),
                         {'outcome': 'major', 'author_message': 'Decision letter ABC'})
        self.client.force_login(self.author)
        page = self.client.get(url)
        self.assertContains(page, 'Revision requested')
        self.assertContains(page, reverse('submission_revision', args=[mine.pk]))
        self.assertContains(page, 'Editorial decision recorded')
        self.assertNotContains(page, self.reviewer.username)
        self.assertNotContains(page, 'Private report XYZ')
        self.assertNotContains(page, 'Decision letter ABC')
        self.assertEqual(self.client.post(url).status_code, 405)
        revision_url = reverse('submission_revision', args=[mine.pk])
        revision_token = self.client.get(revision_url).context['revision_token']
        response = self.client.post(revision_url, {
            'revision_token': revision_token,
            'manuscript': SimpleUploadedFile('revision.pdf', test_workflow.PDF, content_type='application/pdf'),
            'author_response': 'Fictional response to editor',
        })
        self.assertEqual(response.status_code, 302)
        page = self.client.get(url)
        kinds = [event['kind'] for event in page.context['events']]
        self.assertEqual(kinds, ['revision', 'decision', 'submitted', 'draft'])
        self.assertContains(page, 'Revised version submitted')
        self.assertContains(page, 'Version 2')
        self.assertContains(page, 'Round 2')
        self.assertNotContains(page, 'Fictional response to editor')
        self.assertNotContains(page, reverse('submission_revision', args=[mine.pk]))

    def test_invalid_cross_submission_links_are_not_included(self):
        mine = self.create_draft()
        self.submit(mine)
        foreign = Submission.objects.create(author=self.author, journal=self.other, title='Other', abstract='x', manuscript='a.pdf')
        foreign_version = SubmissionVersion.objects.create(submission=foreign, number=5, manuscript='b.pdf', uploaded_by=self.author)
        invalid = ReviewRound.objects.create(submission=mine, version=foreign_version, number=5, status='closed')
        EditorialDecision.objects.create(submission=mine, round=invalid, outcome='reject', author_message='not mine', decided_by=self.editor)
        self.client.force_login(self.author)
        page = self.client.get(reverse('author_submission_progress', args=[mine.pk]))
        self.assertEqual([event['kind'] for event in page.context['events']], ['submitted', 'draft'])
        self.assertNotContains(page, 'not mine')
        self.assertNotContains(page, 'Round 5')

    def test_utc_and_chinese_translation(self):
        mine = self.create_draft()
        Submission.objects.filter(pk=mine.pk).update(created_at=datetime(2026, 11, 2, 22, 15, tzinfo=utc_timezone.utc))
        self.client.force_login(self.author)
        with timezone.override('Asia/Shanghai'), translation.override('zh-hans'):
            page = self.client.get(reverse('author_submission_progress', args=[mine.pk]), HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(page, '2026-11-02 22:15')
        self.assertContains(page, '投稿时间线')
