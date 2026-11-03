from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (AuthorNotice, EditorialAssignment, EditorialDecision, EditorComparisonPermissionRequest,
    EditorRevisionComparisonConsent, Journal, JournalAIPolicy, Membership, Organisation, ReviewRound, Submission, SubmissionVersion)


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class EditorAIPermissionRequestTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user('fictional-author')
        cls.editor = users.create_user('private-editor-identity')
        cls.editor2 = users.create_user('second-editor')
        cls.outsider = users.create_user('outside-user')
        cls.journal = Journal.objects.create(organisation=Organisation.objects.create(name='Fictional publisher', slug='request-publisher'),
                                             name='Fictional journal', slug='request-journal')
        for editor in (cls.editor, cls.editor2):
            Membership.objects.create(user=editor, journal=cls.journal, role=Membership.Role.EDITOR)
        cls.sub = Submission.objects.create(journal=cls.journal, author=cls.author, title='Fictional submission',
            abstract='PRIVATE ABSTRACT', manuscript='fake.pdf', status=Submission.Status.SUBMITTED)
        for editor in (cls.editor, cls.editor2):
            EditorialAssignment.objects.create(submission=cls.sub, editor=editor, assigned_by=editor)
        old = SubmissionVersion.objects.create(submission=cls.sub, number=1, manuscript='old.pdf', uploaded_by=cls.author)
        prior = ReviewRound.objects.create(submission=cls.sub, version=old, number=1, status=ReviewRound.Status.CLOSED)
        cls.decision = EditorialDecision.objects.create(submission=cls.sub, round=prior, outcome='major',
            author_message='PRIVATE AUTHOR DECISION TEXT', decided_by=cls.editor)
        cls.version = SubmissionVersion.objects.create(submission=cls.sub, number=2, manuscript='new.pdf',
            uploaded_by=cls.author, author_response='PRIVATE AUTHOR RESPONSE TEXT')
        cls.round = ReviewRound.objects.create(submission=cls.sub, version=cls.version, number=2)
        cls.policy = JournalAIPolicy.objects.create(journal=cls.journal, editor_revision_comparison_enabled=True)

    def setUp(self): self.client.force_login(self.editor)
    def editor_url(self): return reverse('request_author_editor_ai_permission', args=[self.sub.pk])
    def author_url(self): return reverse('author_editor_ai_consent', args=[self.sub.pk])
    def token(self): return self.client.get(self.editor_url()).context['request_token']
    def send_request(self, token=None):
        return self.client.post(self.editor_url(), {'request_token':token or self.token(), 'confirm':'yes', 'synthetic':'yes'})
    def author_token(self): return self.client.get(self.author_url()).context['consent_token']
    def author_grant(self):
        return self.client.post(self.author_url(), {'consent_token':self.author_token(), 'action':'grant', 'confirm':'yes', 'synthetic':'yes'})
    def author_decline(self, req, token=None):
        return self.client.post(self.author_url(), {'consent_token':token or self.author_token(), 'action':'decline',
                                                   'permission_request_id':str(req.pk)})

    def test_explicit_request_creates_private_inbox_notice_without_messages(self):
        page = self.client.get(self.editor_url())
        self.assertContains(page, self.sub.title)
        self.assertContains(page, 'Create in-app request')
        self.assertContains(self.client.get(self.editor_url(), HTTP_ACCEPT_LANGUAGE='zh-hans'), '请求作者 AI 授权')
        with patch('publishing.editor_revision_comparison_provider._send_request') as ai:
            self.assertEqual(self.send_request(page.context['request_token']).status_code, 302)
        ai.assert_not_called()
        req = EditorComparisonPermissionRequest.objects.get()
        notice = AuthorNotice.objects.get(kind='ai_permission')
        self.assertEqual((req.recipient, req.requested_by, notice.recipient), (self.author, self.editor, self.author))
        self.assertEqual(notice.ai_permission_request, req)
        self.assertFalse(EditorRevisionComparisonConsent.objects.exists())
        self.assertEqual(len(mail.outbox), 0)
        self.client.force_login(self.author)
        inbox = self.client.get(reverse('author_notice_inbox'))
        self.assertContains(inbox, 'AI permission requested')
        self.assertNotContains(inbox, self.editor.username)
        self.assertNotContains(inbox, 'PRIVATE AUTHOR DECISION TEXT')
        self.assertNotContains(inbox, 'PRIVATE AUTHOR RESPONSE TEXT')
        detail = self.client.get(reverse('author_editor_ai_request', args=[notice.pk]))
        self.assertContains(detail, 'AI sharing request')
        self.assertContains(detail, 'This request refers to the current revision')
        self.assertEqual(self.client.post(reverse('read_author_notice', args=[notice.pk])).status_code, 302)
        notice.refresh_from_db(); self.assertIsNotNone(notice.read_at)

    def test_editor_scope_confirmation_csrf_and_policy(self):
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.editor_url(), {'request_token':self.token(), 'confirm':'yes', 'synthetic':'yes'}).status_code, 403)
        for field in ('confirm', 'synthetic'):
            data={'request_token':self.token(), 'confirm':'yes', 'synthetic':'yes'}; data[field]=''
            self.assertEqual(self.client.post(self.editor_url(), data).status_code, 400)
        self.assertEqual(self.send_request(self.token()+'tampered').status_code, 409)
        for user in (self.author, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.editor_url()).status_code, 404)
        self.client.force_login(self.editor)
        old = self.token()
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(revision=2)
        self.assertEqual(self.send_request(old).status_code, 409)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(editor_revision_comparison_enabled=False)
        self.assertEqual(self.send_request().status_code, 409)
        self.assertFalse(EditorComparisonPermissionRequest.objects.exists())
        self.assertFalse(AuthorNotice.objects.exists())

    def test_duplicate_requests_are_idempotent_across_editors_and_after_decline(self):
        token = self.token()
        self.assertEqual(self.send_request(token).status_code, 302)
        self.assertEqual(self.send_request(token).status_code, 302)
        self.client.force_login(self.editor2)
        self.assertEqual(self.send_request().status_code, 302)
        self.assertEqual(EditorComparisonPermissionRequest.objects.count(), 1)
        self.assertEqual(AuthorNotice.objects.count(), 1)
        req = EditorComparisonPermissionRequest.objects.get()
        self.client.force_login(self.author)
        self.assertEqual(self.author_decline(req).status_code, 302)
        req.refresh_from_db(); self.assertEqual(req.status, 'declined'); self.assertIsNotNone(req.responded_at)
        self.assertFalse(EditorRevisionComparisonConsent.objects.exists())
        self.client.force_login(self.editor)
        self.assertEqual(self.send_request().status_code, 302)
        self.assertEqual(EditorComparisonPermissionRequest.objects.count(), 1)
        self.client.force_login(self.author)
        self.assertEqual(self.author_grant().status_code, 302)
        req.refresh_from_db(); self.assertEqual(req.status, 'declined')
        self.assertTrue(EditorRevisionComparisonConsent.objects.filter(ended_at__isnull=True).exists())

    def test_author_grant_resolves_pending_request(self):
        self.assertEqual(self.send_request().status_code, 302)
        req = EditorComparisonPermissionRequest.objects.get()
        self.client.force_login(self.author)
        page = self.client.get(self.author_url())
        self.assertContains(page, 'The editorial office requested your choice')
        self.assertContains(page, 'Decline this request')
        self.assertEqual(self.author_grant().status_code, 302)
        req.refresh_from_db()
        self.assertEqual(req.status, 'granted')
        self.assertIsNotNone(req.responded_at)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.editor_url()), 'Author permission recorded')

    def test_stale_source_notice_is_historical_and_new_source_can_be_requested(self):
        self.assertEqual(self.send_request().status_code, 302)
        old_req = EditorComparisonPermissionRequest.objects.get(); old_notice = AuthorNotice.objects.get()
        SubmissionVersion.objects.filter(pk=self.version.pk).update(author_response='Changed response')
        self.client.force_login(self.author)
        detail = self.client.get(reverse('author_editor_ai_request', args=[old_notice.pk]))
        self.assertContains(detail, 'This request is no longer current')
        self.assertEqual(self.author_decline(old_req).status_code, 409)
        self.client.force_login(self.editor)
        self.assertEqual(self.send_request().status_code, 302)
        self.assertEqual(EditorComparisonPermissionRequest.objects.count(), 2)
        self.assertEqual(AuthorNotice.objects.count(), 2)

    def test_recipient_change_and_invalid_source_hide_the_notice(self):
        self.assertEqual(self.send_request().status_code, 302)
        notice = AuthorNotice.objects.get()
        detail = reverse('author_editor_ai_request', args=[notice.pk])
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(detail).status_code, 404)
        Submission.objects.filter(pk=self.sub.pk).update(author=self.outsider)
        self.assertNotContains(self.client.get(reverse('author_notice_inbox')), self.sub.title)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(detail).status_code, 404)
        Submission.objects.filter(pk=self.sub.pk).update(author=self.author)
        other = Submission.objects.create(journal=self.journal, author=self.author, title='Other', abstract='x', manuscript='x')
        foreign = SubmissionVersion.objects.create(submission=other, number=1, manuscript='x', uploaded_by=self.author)
        ReviewRound.objects.filter(pk=self.round.pk).update(version=foreign)
        self.assertNotContains(self.client.get(reverse('author_notice_inbox')), self.sub.title)
        self.assertEqual(self.client.get(detail).status_code, 404)

    def test_notification_failure_rolls_back_request(self):
        token = self.token()
        with patch('publishing.editor_comparison_request_views.AuthorNotice.objects.create', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError):
                self.send_request(token)
        self.assertFalse(EditorComparisonPermissionRequest.objects.exists())
        self.assertFalse(AuthorNotice.objects.exists())

    def test_author_consent_or_inactive_account_blocks_new_request(self):
        token = self.token()
        self.client.force_login(self.author); self.assertEqual(self.author_grant().status_code, 302)
        self.client.force_login(self.editor)
        self.assertEqual(self.send_request(token).status_code, 409)
        self.assertFalse(EditorComparisonPermissionRequest.objects.exists())
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(self.author_url(), {'action':'withdraw', 'consent_token':self.author_token()}).status_code, 302)
        get_user_model().objects.filter(pk=self.author.pk).update(is_active=False)
        self.client.force_login(self.editor)
        self.assertEqual(self.send_request().status_code, 409)
        self.assertFalse(AuthorNotice.objects.exists())

    def test_request_and_notice_integrity_constraints(self):
        self.assertEqual(self.send_request().status_code, 302)
        req = EditorComparisonPermissionRequest.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            AuthorNotice.objects.create(submission=self.sub, recipient=self.author, kind='decision', ai_permission_request=req)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EditorComparisonPermissionRequest.objects.filter(pk=req.pk).update(status='declined', responded_at=None)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EditorComparisonPermissionRequest.objects.create(submission=self.sub, review_round=self.round,
                decision=self.decision, version=self.version, requested_by=self.editor2, recipient=self.author,
                provider=req.provider, purpose=req.purpose, source_fingerprint=req.source_fingerprint)

    def test_request_is_historical_after_round_closes(self):
        self.assertEqual(self.send_request().status_code, 302)
        notice = AuthorNotice.objects.get()
        ReviewRound.objects.filter(pk=self.round.pk).update(status=ReviewRound.Status.CLOSED)
        self.client.force_login(self.author)
        self.assertContains(self.client.get(reverse('author_editor_ai_request', args=[notice.pk])), 'This request is no longer current')

    def test_editor_deactivated_during_request_cannot_create_notice(self):
        from publishing.editor_revision_views import current_revision_package
        token = self.token()
        calls = []
        def disable_after_first_check(user, pk):
            result = current_revision_package(user, pk)
            calls.append(True)
            if len(calls) == 1:
                get_user_model().objects.filter(pk=user.pk).update(is_active=False)
            return result
        with patch('publishing.editor_comparison_request_views.current_revision_package', side_effect=disable_after_first_check):
            self.assertEqual(self.send_request(token).status_code, 404)
        self.assertFalse(EditorComparisonPermissionRequest.objects.exists())
        self.assertFalse(AuthorNotice.objects.exists())

    def test_author_deactivated_during_grant_cannot_record_permission(self):
        from publishing.editor_revision_views import author_current_revision_package
        self.client.force_login(self.author)
        token = self.author_token()
        calls = []
        def disable_after_first_check(user, pk):
            result = author_current_revision_package(user, pk)
            calls.append(True)
            if len(calls) == 1:
                get_user_model().objects.filter(pk=user.pk).update(is_active=False)
            return result
        with patch('publishing.editor_comparison_consent_views.author_current_revision_package', side_effect=disable_after_first_check):
            response = self.client.post(self.author_url(), {'consent_token':token, 'action':'grant', 'confirm':'yes', 'synthetic':'yes'})
        self.assertEqual(response.status_code, 404)
        self.assertFalse(EditorRevisionComparisonConsent.objects.exists())

    @override_settings(DEBUG=False)
    def test_editor_request_endpoint_is_disabled_outside_debug(self):
        self.assertEqual(self.client.get(self.editor_url()).status_code, 404)
        self.assertEqual(self.client.post(self.editor_url()).status_code, 404)
