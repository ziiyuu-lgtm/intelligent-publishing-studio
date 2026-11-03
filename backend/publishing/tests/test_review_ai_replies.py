from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.http import Http404
from django.urls import reverse
from django.utils import translation

from publishing.models import EditorialAssignment, Membership, ReviewAIConsent, ReviewAIReplyNotice
from publishing.tests import test_review_ai_requests


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReviewAIReplyTests(TestCase):
    create_draft = test_review_ai_requests.ReviewAIRequestTests.create_draft
    submit = test_review_ai_requests.ReviewAIRequestTests.submit
    invite = test_review_ai_requests.ReviewAIRequestTests.invite
    report = test_review_ai_requests.ReviewAIRequestTests.report
    add_reviewer = test_review_ai_requests.ReviewAIRequestTests.add_reviewer
    choose = test_review_ai_requests.ReviewAIRequestTests.choose
    request_data = test_review_ai_requests.ReviewAIRequestTests.request_data
    send = test_review_ai_requests.ReviewAIRequestTests.send

    def setUp(self):
        test_review_ai_requests.ReviewAIRequestTests.setUp(self)
        self.reply_url = reverse('review_ai_reply_inbox')

    def reply(self):
        self.assertEqual(self.send().status_code, 302)
        self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url, action='decline').status_code, 302)
        self.assertEqual(self.choose(self.second.reviewer, self.second_url).status_code, 302)

    def test_reply_notices_are_created_once_and_shown_as_unread(self):
        with patch('publishing.ai_gateway.run_ai_task') as ai, patch('django.core.mail.send_mail') as email:
            self.reply()
            self.assertEqual(ReviewAIReplyNotice.objects.count(), 3)
            ai.assert_not_called(); email.assert_not_called()
        self.client.force_login(self.editor)
        page = self.client.get(self.reply_url)
        self.assertEqual(page.context['unread_count'], 3)
        self.assertEqual(len(page.context['rows']), 3)
        self.assertContains(page, 'Permission granted')
        self.assertContains(page, 'Permission declined')
        self.assertContains(page, 'Currently active')
        self.assertContains(page, 'Not currently active')
        self.assertNotContains(page, 'PRIVATE REVIEW TEXT')
        self.assertNotContains(page, 'OTHER CONFIDENTIAL REPORT')
        self.assertIn('no-store', page['Cache-Control'])
        self.assertEqual(self.client.get(reverse('workspace')).context['unread_review_ai_reply_count'], 3)
        self.assertContains(self.client.get(reverse('workspace')), self.reply_url)
        # GET, refresh and voluntary later changes do not create duplicate reply events.
        self.client.get(self.reply_url); self.client.get(self.reply_url)
        self.choose(self.reviewer, self.reviewer_url)
        self.assertEqual(ReviewAIReplyNotice.objects.count(), 3)

    def test_mark_read_is_explicit_scoped_and_csrf_protected(self):
        self.reply()
        notice = ReviewAIReplyNotice.objects.get(permission_request__recipient=self.author)
        mark = reverse('read_review_ai_reply', args=[notice.pk])
        detail = reverse('review_ai_reply_detail', args=[notice.pk])
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(detail).status_code, 200)
        notice.refresh_from_db(); self.assertIsNone(notice.read_at)
        self.assertEqual(self.client.get(mark).status_code, 405)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.editor)
        self.assertEqual(csrf.post(mark).status_code, 403)
        self.assertEqual(self.client.post(mark).status_code, 302)
        notice.refresh_from_db(); first = notice.read_at
        self.assertIsNotNone(first)
        self.assertEqual(self.client.post(mark).status_code, 302)
        notice.refresh_from_db(); self.assertEqual(notice.read_at, first)
        page = self.client.get(self.reply_url, {'filter':'unread'})
        self.assertEqual(page.context['unread_count'], 2)
        self.assertEqual(len(page.context['rows']), 2)
        self.assertEqual(self.client.get(reverse('workspace')).context['unread_review_ai_reply_count'], 2)

    def test_privacy_and_current_editor_authorisation(self):
        self.reply()
        notice = ReviewAIReplyNotice.objects.first()
        detail = reverse('review_ai_reply_detail', args=[notice.pk])
        mark = reverse('read_review_ai_reply', args=[notice.pk])
        # Another assigned editor can manage the submission but not these personal notices.
        EditorialAssignment.objects.create(submission=self.submission, editor=self.other_editor,
            assigned_by=self.coordinator, status='active')
        for user in (self.other_editor, self.author, self.reviewer, self.coordinator, self.superuser):
            self.client.force_login(user)
            self.assertEqual(len(self.client.get(self.reply_url).context['rows']), 0)
            self.assertEqual(self.client.get(detail).status_code, 404)
            self.assertEqual(self.client.post(mark).status_code, 404)
        Membership.objects.filter(user=self.editor, journal=self.journal, role='editor').update(active=False)
        self.client.force_login(self.editor)
        self.assertEqual(len(self.client.get(self.reply_url).context['rows']), 0)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertEqual(self.client.post(mark).status_code, 404)

    def test_past_reply_and_current_authorisation_are_separate(self):
        self.reply()
        notice = ReviewAIReplyNotice.objects.get(permission_request__recipient=self.author)
        detail = reverse('review_ai_reply_detail', args=[notice.pk])
        self.choose(self.author, self.author_url, action='withdraw')
        self.client.force_login(self.editor)
        page = self.client.get(detail)
        self.assertContains(page, 'Permission granted')
        self.assertContains(page, 'Not currently active')
        self.choose(self.author, self.author_url)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(detail), 'Currently active')
        self.assignment.comments = 'Changed fictional report'; self.assignment.save(update_fields=['comments'])
        self.assertContains(self.client.get(detail), 'Source changed')
        self.assertFalse(self.client.get(detail).context['current'])

    def test_reviewer_access_loss_disables_permission_until_restored(self):
        self.reply()
        notice = ReviewAIReplyNotice.objects.get(permission_request__recipient=self.second.reviewer)
        detail = reverse('review_ai_reply_detail', args=[notice.pk])
        membership = Membership.objects.get(user=self.second.reviewer, journal=self.journal, role='reviewer')
        membership.active = False; membership.save(update_fields=['active'])
        self.client.force_login(self.editor)
        page = self.client.get(detail)
        self.assertTrue(page.context['current'])
        self.assertFalse(page.context['allowed'])
        synthesis = reverse('review_synthesis_request', args=[self.submission.pk, self.assignment.round.number])
        self.assertEqual(self.client.get(synthesis).context['reviewer_count'], 0)
        membership.active = True; membership.save(update_fields=['active'])
        self.assertTrue(self.client.get(detail).context['allowed'])
        self.assertEqual(self.client.get(synthesis).context['reviewer_count'], 1)

    def test_inactive_author_cannot_be_used_for_a_new_comparison(self):
        self.reply()
        self.author.is_active = False; self.author.save(update_fields=['is_active'])
        self.client.force_login(self.editor)
        notice = ReviewAIReplyNotice.objects.get(permission_request__recipient=self.author)
        page = self.client.get(reverse('review_ai_reply_detail', args=[notice.pk]))
        self.assertTrue(page.context['current'])
        self.assertFalse(page.context['allowed'])
        synthesis = reverse('review_synthesis_request', args=[self.submission.pk, self.assignment.round.number])
        self.assertFalse(self.client.get(synthesis).context['author_permission'])
        self.assertTrue(ReviewAIConsent.objects.filter(subject='author', ended_at__isnull=True).exists())

    def test_unrelated_or_malformed_notice_is_hidden(self):
        self.reply()
        notice = ReviewAIReplyNotice.objects.get(permission_request__recipient=self.author)
        self.client.force_login(self.editor)
        self.assertEqual(len(self.client.get(self.reply_url).context['rows']), 3)
        notice.outcome = 'declined'; notice.save(update_fields=['outcome'])
        self.assertEqual(len(self.client.get(self.reply_url).context['rows']), 2)
        self.assertEqual(self.client.get(reverse('review_ai_reply_detail', args=[notice.pk])).status_code, 404)

    def test_access_removed_after_inbox_query_hides_reply_content(self):
        self.reply(); self.client.force_login(self.editor)
        notice = ReviewAIReplyNotice.objects.first()
        detail = reverse('review_ai_reply_detail', args=[notice.pk])
        with patch('publishing.review_ai_reply_views._state', side_effect=Http404):
            page = self.client.get(self.reply_url)
            self.assertEqual(page.context['rows'], [])
            self.assertNotContains(page, self.submission.title)
            self.assertEqual(self.client.get(detail).status_code, 404)

    def test_chinese_and_non_debug_boundary(self):
        self.reply(); self.client.force_login(self.editor)
        with translation.override('zh-hans'):
            page = self.client.get(self.reply_url, HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(page, '审稿 AI 授权回复')
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(self.reply_url).status_code, 404)
            self.assertEqual(self.client.get(reverse('review_ai_reply_detail', args=[ReviewAIReplyNotice.objects.first().pk])).status_code, 404)
