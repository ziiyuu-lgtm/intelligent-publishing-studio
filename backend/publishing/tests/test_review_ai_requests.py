from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from publishing.models import JournalAIPolicy, Membership, ReviewAIConsent, ReviewAIPermissionRequest
from publishing.tests import test_review_ai_consent


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReviewAIRequestTests(TestCase):
    create_draft = test_review_ai_consent.ReviewAIPermissionTests.create_draft
    submit = test_review_ai_consent.ReviewAIPermissionTests.submit
    invite = test_review_ai_consent.ReviewAIPermissionTests.invite
    report = test_review_ai_consent.ReviewAIPermissionTests.report
    add_reviewer = test_review_ai_consent.ReviewAIPermissionTests.add_reviewer
    choose = test_review_ai_consent.ReviewAIPermissionTests.choose

    def setUp(self):
        test_review_ai_consent.ReviewAIPermissionTests.setUp(self)
        self.second = self.add_reviewer('private-other-reviewer', comments='OTHER CONFIDENTIAL REPORT')
        self.policy = JournalAIPolicy.objects.create(journal=self.journal, review_synthesis_enabled=True, journal_daily_limit=10)
        self.url = reverse('review_ai_request_permissions', args=[self.submission.pk, self.assignment.round.number])
        self.inbox = reverse('review_ai_permission_inbox')
        self.second_url = reverse('reviewer_review_ai_permission', args=[self.second.pk])

    def request_data(self, targets=None):
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertEqual(page.status_code, 200)
        return {'token':page.context['token'], 'selected':targets or ['author', str(self.assignment.pk), str(self.second.pk)],
                'confirm':'yes', 'synthetic':'yes'}

    def send(self, targets=None):
        data = self.request_data(targets)
        return self.client.post(self.url, data)

    def test_selected_in_app_requests_independent_replies_and_no_external_send(self):
        data = self.request_data()
        with patch('publishing.ai_gateway.run_ai_task') as ai, patch('django.core.mail.send_mail') as email:
            self.assertEqual(self.client.post(self.url, data).status_code, 302)
            self.assertEqual(self.client.post(self.url, data).status_code, 302)
            self.assertEqual(ReviewAIPermissionRequest.objects.count(), 3)
            self.assertFalse(ReviewAIConsent.objects.exists())
            self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
            self.assertEqual(self.choose(self.reviewer, self.reviewer_url, action='decline').status_code, 302)
            self.assertEqual(self.choose(self.second.reviewer, self.second_url).status_code, 302)
            ai.assert_not_called(); email.assert_not_called()
        self.assertEqual(ReviewAIConsent.objects.count(), 2)
        self.assertEqual(ReviewAIPermissionRequest.objects.get(recipient=self.author).status, 'granted')
        self.assertEqual(ReviewAIPermissionRequest.objects.get(recipient=self.reviewer).status, 'declined')
        self.assertEqual(ReviewAIPermissionRequest.objects.get(recipient=self.second.reviewer).status, 'granted')
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        self.assertContains(page, 'Declined')
        self.assertContains(page, 'Permission recorded')
        # A declined source is not repeatedly requested, even by reposting an old form.
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertEqual(ReviewAIPermissionRequest.objects.count(), 3)
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url).status_code, 302)
        self.assertEqual(ReviewAIPermissionRequest.objects.get(recipient=self.reviewer).status, 'declined')

    def test_inbox_only_own_entries_no_reviewer_names_or_reports_to_author(self):
        self.send()
        for user in (self.author, self.reviewer, self.second.reviewer):
            self.client.force_login(user)
            page = self.client.get(self.inbox)
            self.assertEqual(page.status_code, 200)
            self.assertEqual(len(page.context['rows']), 1)
            self.assertContains(page, 'Awaiting response')
            self.assertNotContains(page, 'PRIVATE REVIEW TEXT')
            self.assertNotContains(page, 'OTHER CONFIDENTIAL REPORT')
            if user != self.second.reviewer:
                self.assertNotContains(page, 'private-other-reviewer')
            if user != self.reviewer:
                self.assertNotContains(page, self.reviewer.username)
            self.assertContains(self.client.get(reverse('workspace')), self.inbox)
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(self.author_url), self.reviewer.username)
        self.assertNotContains(self.client.get(self.author_url), str(self.assignment.pk))
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(self.reviewer_url), 'private-other-reviewer')
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(self.url).status_code, 404)
        self.assertEqual(len(self.client.get(self.inbox).context['rows']), 0)

    def test_explicit_scope_permissions_validation_and_csrf(self):
        data = self.request_data()
        for key in ('confirm', 'synthetic'):
            invalid = data.copy(); invalid[key] = ''
            self.assertEqual(self.client.post(self.url, invalid).status_code, 400)
        for targets in ([], ['author', 'author'], ['bogus'], [str(self.assignment.pk), 'bogus']):
            invalid = data.copy(); invalid['selected'] = targets
            self.assertIn(self.client.post(self.url, invalid).status_code, (400, 409))
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.url, data).status_code, 403)
        self.assertFalse(ReviewAIPermissionRequest.objects.exists())
        self.policy.review_synthesis_enabled = False; self.policy.save(update_fields=['review_synthesis_enabled'])
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertFalse(ReviewAIPermissionRequest.objects.exists())
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.client.get(self.inbox).status_code, 404)

    def test_changed_scope_cannot_be_answered_and_new_report_supersedes_pending_author_request(self):
        data = self.request_data()
        self.assignment.comments = 'CHANGED SOURCE'; self.assignment.save(update_fields=['comments'])
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertFalse(ReviewAIPermissionRequest.objects.exists())
        self.assertEqual(self.send().status_code, 302)
        author_request = ReviewAIPermissionRequest.objects.get(recipient=self.author)
        self.client.force_login(self.author)
        old_token = self.client.get(self.author_url).context['token']
        extra = self.add_reviewer('new-event-reviewer', status='accepted', comments='')
        self.client.force_login(extra.reviewer)
        posted = self.client.post(reverse('review_report', args=[extra.pk]),
            {'recommendation':'minor', 'comments':'New fictional review'})
        self.assertEqual(posted.status_code, 302)
        author_request.refresh_from_db()
        self.assertEqual(author_request.status, 'outdated')
        self.assertEqual(self.choose(self.author, self.author_url, token=old_token, action='decline').status_code, 409)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(self.inbox), 'Source changed')
        self.assertEqual(self.send(['author']).status_code, 302)
        self.assertEqual(ReviewAIPermissionRequest.objects.filter(recipient=self.author).count(), 2)
        # The first reviewer's own report is unaffected by another reviewer's submission.
        self.assertEqual(ReviewAIPermissionRequest.objects.get(recipient=self.reviewer).status, 'pending')

    def test_revoked_role_hides_reviewer_request_and_stale_reply_fails(self):
        self.send([str(self.assignment.pk)])
        self.client.force_login(self.reviewer)
        token = self.client.get(self.reviewer_url).context['token']
        Membership.objects.filter(user=self.reviewer, journal=self.journal, role='reviewer').update(active=False)
        self.assertEqual(len(self.client.get(self.inbox).context['rows']), 0)
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url, action='decline', token=token).status_code, 409)
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url, token=token).status_code, 409)
        self.assertFalse(ReviewAIConsent.objects.exists())

    def test_withdrawal_is_visible_and_old_requests_do_not_revive(self):
        self.send(['author', str(self.second.pk)])
        self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
        self.assertEqual(self.choose(self.author, self.author_url, action='withdraw').status_code, 302)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(self.inbox), 'Permission inactive')
        self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(self.inbox), 'Permission recorded')
        self.client.force_login(self.second.reviewer)
        self.assertEqual(self.client.post(reverse('review_respond', args=[self.second.pk]),
            {'response':'conflict', 'conflict_details':'Fictional conflict'}).status_code, 302)
        old_request = ReviewAIPermissionRequest.objects.get(recipient=self.second.reviewer)
        self.assertEqual(old_request.status, 'outdated')
        # Even if a source were later restored, the outdated request cannot revive.
        self.second.conflict = 'none'; self.second.save(update_fields=['conflict'])
        self.client.force_login(self.second.reviewer)
        self.assertContains(self.client.get(self.inbox), 'Source changed')
        self.assertEqual(self.send([str(self.second.pk)]).status_code, 302)
        self.assertEqual(ReviewAIPermissionRequest.objects.filter(recipient=self.second.reviewer).count(), 2)

    def test_chinese_interface(self):
        self.client.force_login(self.editor)
        with translation.override('zh-hans'):
            page = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(page, '请求审稿 AI 授权')
