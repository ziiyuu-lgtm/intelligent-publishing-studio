from unittest.mock import patch

from django.conf import settings
from django.core import signing
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from publishing.models import Membership, ReviewAIConsent, ReviewAssignment, SubmissionEvent
from publishing.review_ai_consent import author_active, existing_author, existing_reviewer, reviewer_active
from publishing.tests import test_review_comparison


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReviewAIPermissionTests(TestCase):
    create_draft = test_review_comparison.ReviewComparisonTests.create_draft
    submit = test_review_comparison.ReviewComparisonTests.submit
    invite = test_review_comparison.ReviewComparisonTests.invite
    report = test_review_comparison.ReviewComparisonTests.report
    add_reviewer = test_review_comparison.ReviewComparisonTests.add_reviewer

    def setUp(self):
        test_review_comparison.ReviewComparisonTests.setUp(self)
        self.report(self.assignment, 'PRIVATE REVIEW TEXT for the editorial office.')
        self.author_url = reverse('author_review_ai_permission', args=[self.submission.pk])
        self.reviewer_url = reverse('reviewer_review_ai_permission', args=[self.assignment.pk])
        self.editor_url = reverse('review_comparison', args=[self.submission.pk])

    def choose(self, user, url, action='grant', token=None, **changes):
        self.client.force_login(user)
        if token is None:
            token = self.client.get(url).context['token']
        data = {'token':token, 'action':action, 'confirm':'yes', 'synthetic':'yes'}
        data.update(changes)
        return self.client.post(url, data)

    def test_author_and_reviewer_own_choices_are_separate_and_never_call_ai(self):
        with patch('publishing.ai_gateway.run_ai_task') as ai:
            self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
            self.assertEqual(self.choose(self.reviewer, self.reviewer_url).status_code, 302)
            self.assertEqual(ReviewAIConsent.objects.count(), 2)
            ai.assert_not_called()
        self.assignment.refresh_from_db()
        self.assertTrue(author_active(existing_author(self.assignment.round, self.author), self.assignment.round))
        self.assertTrue(reviewer_active(existing_reviewer(self.assignment, self.reviewer), self.assignment))
        # Local session keys rotate on restart; the separate consent key stays stable.
        with override_settings(SECRET_KEY='different-synthetic-session-key'):
            self.assertTrue(author_active(existing_author(self.assignment.round, self.author), self.assignment.round))
            self.assertTrue(reviewer_active(existing_reviewer(self.assignment, self.reviewer), self.assignment))
        self.client.force_login(self.editor)
        page = self.client.get(self.editor_url)
        self.assertEqual(page.context['ai_readiness'], {'author':True,'reviewers':1,'total':1})
        self.assertContains(page, 'Permissions and journal policy are checked for every request')
        self.assertFalse(SubmissionEvent.objects.filter(action='editorial_decision').exists())

    def test_author_cannot_see_reviewer_identity_or_report(self):
        self.client.force_login(self.author)
        page = self.client.get(self.author_url)
        self.assertContains(page, 'Reviewer identities and confidential comments are not displayed here')
        self.assertNotContains(page, self.reviewer.username)
        self.assertNotContains(page, 'PRIVATE REVIEW TEXT')
        self.assertNotContains(page, str(self.assignment.pk))
        payload = signing.loads(page.context['token'], salt='publishing.review-ai.permission.v1', max_age=3600)
        self.assertNotIn('PRIVATE REVIEW TEXT', str(payload))
        self.assertNotIn(self.reviewer.username, str(payload))
        self.assertIn('no-store', page['Cache-Control'])
        self.assertContains(self.client.get(reverse('submission_detail', args=[self.submission.pk])), self.author_url)

    def test_roles_and_csrf(self):
        for user, url in [(self.reviewer, self.author_url), (self.editor, self.author_url),
                          (self.author, self.reviewer_url), (self.other_editor, self.reviewer_url),
                          (self.coordinator, self.author_url)]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {'action':'grant'}).status_code, 404)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.author_url, {'action':'grant'}).status_code, 403)
        for key in ('confirm', 'synthetic'):
            self.assertEqual(self.choose(self.author, self.author_url, **{key:''}).status_code, 400)
        self.assertFalse(ReviewAIConsent.objects.exists())

    def test_source_change_invalidates_form_and_recorded_permission(self):
        self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url).status_code, 302)
        self.client.force_login(self.reviewer)
        old_token = self.client.get(self.reviewer_url).context['token']
        self.assignment.comments = 'Changed confidential text'
        self.assignment.save(update_fields=['comments'])
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url, token=old_token).status_code, 409)
        self.assignment.refresh_from_db()
        self.assertFalse(reviewer_active(existing_reviewer(self.assignment, self.reviewer), self.assignment))
        self.assertFalse(author_active(existing_author(self.assignment.round, self.author), self.assignment.round))
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.editor_url).context['ai_readiness'], {'author':False,'reviewers':0,'total':1})
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url).status_code, 302)
        self.assertEqual(ReviewAIConsent.objects.filter(subject='reviewer', end_reason='superseded').count(), 1)
        self.assertEqual(self.choose(self.author, self.author_url).status_code, 302)
        self.assertEqual(ReviewAIConsent.objects.filter(subject='author', end_reason='superseded').count(), 1)

    def test_new_report_and_later_conflict_invalidate_author_scope(self):
        self.choose(self.author, self.author_url)
        self.choose(self.reviewer, self.reviewer_url)
        another = self.add_reviewer('fictional-b', comments='Another fictional report')
        self.assignment.refresh_from_db()
        self.assertFalse(author_active(existing_author(self.assignment.round, self.author), self.assignment.round))
        self.assertTrue(reviewer_active(existing_reviewer(self.assignment, self.reviewer), self.assignment))
        self.choose(self.author, self.author_url)
        another.conflict = 'yes'; another.save(update_fields=['conflict'])
        self.assertFalse(author_active(existing_author(self.assignment.round, self.author), self.assignment.round))
        self.assignment.conflict = 'yes'; self.assignment.save(update_fields=['conflict'])
        self.assertFalse(reviewer_active(existing_reviewer(self.assignment, self.reviewer), self.assignment))
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.editor_url).context['ai_readiness'], {'author':False,'reviewers':0,'total':0})
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url, action='withdraw').status_code, 302)
        self.assertEqual(ReviewAIConsent.objects.get(subject='reviewer').end_reason, 'withdrawn')

    def test_withdrawal_remains_possible_after_access_role_changes(self):
        self.choose(self.author, self.author_url)
        self.choose(self.reviewer, self.reviewer_url)
        Membership.objects.filter(user=self.reviewer, journal=self.journal, role='reviewer').update(active=False)
        self.client.force_login(self.reviewer)
        page = self.client.get(self.reviewer_url)
        self.assertNotContains(page, self.submission.title)
        self.assertNotContains(page, 'PRIVATE REVIEW TEXT')
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url, action='withdraw').status_code, 302)
        self.assertEqual(self.choose(self.reviewer, self.reviewer_url).status_code, 409)
        self.journal.active = False; self.journal.save(update_fields=['active'])
        self.client.force_login(self.author)
        page = self.client.get(self.author_url)
        self.assertNotContains(page, self.submission.title)
        self.assertEqual(self.choose(self.author, self.author_url, action='withdraw').status_code, 302)
        self.assertEqual(ReviewAIConsent.objects.filter(end_reason='withdrawn').count(), 2)

    def test_new_report_and_conflict_cannot_reactivate_an_old_grant(self):
        self.choose(self.author, self.author_url)
        self.choose(self.reviewer, self.reviewer_url)
        second = self.add_reviewer('second-event-reviewer', status='accepted', conflict='none')
        self.client.force_login(second.reviewer)
        response = self.client.post(reverse('review_report', args=[second.pk]),
                                    {'recommendation':'minor', 'comments':'Second fictional report'})
        self.assertEqual(response.status_code, 302)
        original = ReviewAIConsent.objects.get(subject='author')
        self.assertEqual(original.end_reason, 'superseded')
        second_url = reverse('reviewer_review_ai_permission', args=[second.pk])
        self.assertEqual(self.choose(second.reviewer, second_url).status_code, 302)
        self.client.force_login(second.reviewer)
        response = self.client.post(reverse('review_respond', args=[second.pk]),
                                    {'response':'conflict', 'conflict_details':'Fictional conflict'})
        self.assertEqual(response.status_code, 302)
        original.refresh_from_db()
        self.assertIsNotNone(original.ended_at)
        self.assertEqual(ReviewAIConsent.objects.get(assignment=second).end_reason, 'superseded')
        self.assertIsNone(existing_author(self.assignment.round, self.author))
        self.assignment.refresh_from_db()
        self.assertTrue(reviewer_active(existing_reviewer(self.assignment, self.reviewer), self.assignment))

    def test_separate_from_prior_revision_consent_and_disabled_outside_debug(self):
        self.choose(self.author, self.author_url)
        self.choose(self.reviewer, self.reviewer_url)
        from publishing.models import EditorRevisionComparisonConsent
        self.assertFalse(EditorRevisionComparisonConsent.objects.exists())
        with override_settings(DEBUG=False):
            self.client.force_login(self.author)
            self.assertEqual(self.client.get(self.author_url).status_code, 404)
            self.client.force_login(self.reviewer)
            self.assertEqual(self.client.get(self.reviewer_url).status_code, 404)
            self.client.force_login(self.editor)
            self.assertIsNone(self.client.get(self.editor_url).context['ai_readiness'])

    def test_chinese_interface(self):
        self.client.force_login(self.author)
        with translation.override('zh-hans'):
            page = self.client.get(self.author_url, HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(page, '审稿 AI 授权')
