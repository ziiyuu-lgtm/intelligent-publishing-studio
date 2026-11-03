from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (EditorialAssignment, EditorialDecision, EditorAIPermissionReplyNotice,
    EditorComparisonPermissionRequest, EditorRevisionComparisonConsent, Journal, JournalAIPolicy,
    Membership, Organisation, ReviewRound, Submission, SubmissionVersion)


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class EditorAIPermissionReplyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user('fictional-author')
        cls.editor = users.create_user('original-editor')
        cls.editor2 = users.create_user('other-editor')
        cls.outsider = users.create_superuser('outsider', 'outside@example.test', 'x')
        cls.journal = Journal.objects.create(organisation=Organisation.objects.create(name='Fictional publisher', slug='reply-publisher'),
                                             name='Fictional journal', slug='reply-journal')
        for editor in (cls.editor, cls.editor2):
            Membership.objects.create(user=editor, journal=cls.journal, role=Membership.Role.EDITOR)
        cls.sub = Submission.objects.create(journal=cls.journal, author=cls.author, title='Fictional submission',
            abstract='PRIVATE ABSTRACT', manuscript='fake.pdf', status=Submission.Status.SUBMITTED)
        for editor in (cls.editor, cls.editor2):
            EditorialAssignment.objects.create(submission=cls.sub, editor=editor, assigned_by=editor)
        old = SubmissionVersion.objects.create(submission=cls.sub, number=1, manuscript='old.pdf', uploaded_by=cls.author)
        prior = ReviewRound.objects.create(submission=cls.sub, version=old, number=1, status=ReviewRound.Status.CLOSED)
        cls.decision = EditorialDecision.objects.create(submission=cls.sub, round=prior, outcome='major',
            author_message='PRIVATE DECISION TEXT', decided_by=cls.editor)
        cls.version = SubmissionVersion.objects.create(submission=cls.sub, number=2, manuscript='new.pdf',
            uploaded_by=cls.author, author_response='PRIVATE RESPONSE TEXT')
        cls.round = ReviewRound.objects.create(submission=cls.sub, version=cls.version, number=2)
        JournalAIPolicy.objects.create(journal=cls.journal, editor_revision_comparison_enabled=True)

    def setUp(self):
        self.client.force_login(self.editor)

    def create_request(self):
        url = reverse('request_author_editor_ai_permission', args=[self.sub.pk])
        token = self.client.get(url).context['request_token']
        self.assertEqual(self.client.post(url, {'request_token':token, 'confirm':'yes', 'synthetic':'yes'}).status_code, 302)
        return EditorComparisonPermissionRequest.objects.get()

    def author_action(self, action, req=None):
        self.client.force_login(self.author)
        url = reverse('author_editor_ai_consent', args=[self.sub.pk])
        token = self.client.get(url).context['consent_token']
        data = {'consent_token':token, 'action':action, 'confirm':'yes', 'synthetic':'yes'}
        if req:
            data['permission_request_id'] = str(req.pk)
        return self.client.post(url, data)

    def reply(self, action='grant'):
        req = self.create_request()
        self.assertEqual(self.author_action(action, req).status_code, 302)
        self.client.force_login(self.editor)
        return EditorAIPermissionReplyNotice.objects.get()

    def detail(self, notice): return reverse('editor_ai_reply_detail', args=[notice.pk])
    def read(self, notice): return reverse('read_editor_ai_reply', args=[notice.pk])
    def inbox(self): return reverse('editor_ai_reply_inbox')

    def test_grant_creates_private_reply_without_email_or_ai(self):
        with patch('publishing.editor_revision_comparison_provider._send_request') as ai:
            notice = self.reply()
        ai.assert_not_called()
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual((notice.recipient, notice.outcome), (self.editor, 'granted'))
        response = self.client.get(self.inbox())
        self.assertContains(response, 'Author allowed this request')
        self.assertContains(response, self.sub.title)
        self.assertNotContains(response, 'PRIVATE DECISION TEXT')
        self.assertNotContains(response, 'PRIVATE RESPONSE TEXT')
        self.assertContains(self.client.get(self.detail(notice)), 'Currently allowed')
        self.assertContains(self.client.get(reverse('workspace')), 'Author AI replies · 1 unread')

    def test_decline_then_later_grant_keeps_history_but_updates_current_state(self):
        notice = self.reply('decline')
        self.assertContains(self.client.get(self.detail(notice)), 'Author declined this request')
        self.assertContains(self.client.get(self.detail(notice)), 'Not currently allowed')
        self.assertEqual(self.author_action('grant').status_code, 302)
        self.client.force_login(self.editor)
        detail = self.client.get(self.detail(notice))
        self.assertContains(detail, 'Author declined this request')
        self.assertContains(detail, 'Currently allowed')
        self.assertEqual(EditorAIPermissionReplyNotice.objects.count(), 1)

    def test_withdrawal_and_changed_source_do_not_misrepresent_consent(self):
        notice = self.reply()
        self.assertEqual(self.author_action('withdraw').status_code, 302)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.detail(notice)), 'Not currently allowed')
        SubmissionVersion.objects.filter(pk=self.version.pk).update(author_response='different source')
        detail = self.client.get(self.detail(notice))
        self.assertContains(detail, 'This request is no longer current')
        self.assertNotContains(detail, 'Open AI comparison')
        self.assertContains(self.client.get(self.inbox()), 'Author allowed this request')

    def test_author_replacement_malformed_sources_and_role_changes_hide_reply(self):
        notice = self.reply()
        detail = self.detail(notice)
        for user in (self.author, self.editor2, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.get(detail).status_code, 404)
            self.assertEqual(self.client.post(self.read(notice)).status_code, 404)
        self.client.force_login(self.editor)
        Submission.objects.filter(pk=self.sub.pk).update(author=self.outsider)
        self.assertEqual(self.client.get(detail).status_code, 404)
        Submission.objects.filter(pk=self.sub.pk).update(author=self.author)
        EditorialAssignment.objects.filter(submission=self.sub, editor=self.editor).update(status='revoked')
        self.assertEqual(self.client.get(detail).status_code, 404)
        EditorialAssignment.objects.filter(submission=self.sub, editor=self.editor).update(status='active')
        Membership.objects.filter(user=self.editor, journal=self.journal).update(active=False)
        self.assertEqual(self.client.get(detail).status_code, 404)
        Membership.objects.filter(user=self.editor, journal=self.journal).update(active=True)
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(self.client.get(detail).status_code, 404)
        Journal.objects.filter(pk=self.journal.pk).update(active=True)
        other = Submission.objects.create(journal=self.journal, author=self.author, title='Other', abstract='x', manuscript='x')
        foreign = SubmissionVersion.objects.create(submission=other, number=1, manuscript='x', uploaded_by=self.author)
        ReviewRound.objects.filter(pk=self.round.pk).update(version=foreign)
        self.assertEqual(self.client.get(detail).status_code, 404)
        self.assertNotContains(self.client.get(self.inbox()), self.sub.title)

    def test_read_is_csrf_protected_idempotent_and_filterable(self):
        notice = self.reply('decline')
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.read(notice)).status_code, 403)
        self.assertEqual(self.client.get(self.read(notice)).status_code, 405)
        self.assertContains(self.client.get(self.inbox()+'?filter=unread'), self.sub.title)
        self.assertEqual(self.client.post(self.read(notice)).status_code, 302)
        notice.refresh_from_db(); when = notice.read_at
        self.assertIsNotNone(when)
        self.assertEqual(self.client.post(self.read(notice)).status_code, 302)
        notice.refresh_from_db(); self.assertEqual(notice.read_at, when)
        self.assertNotContains(self.client.get(self.inbox()+'?filter=unread'), self.sub.title)
        self.assertContains(self.client.get(self.inbox()), self.sub.title)

    def test_response_notice_failure_rolls_back_grant_and_decline(self):
        req = self.create_request()
        for action in ('grant', 'decline'):
            with patch('publishing.editor_comparison_consent_views.EditorAIPermissionReplyNotice.objects.create', side_effect=RuntimeError('test')):
                with self.assertRaises(RuntimeError): self.author_action(action, req)
            req.refresh_from_db()
            self.assertEqual(req.status, 'pending')
            self.assertFalse(EditorRevisionComparisonConsent.objects.exists())
            self.assertFalse(EditorAIPermissionReplyNotice.objects.exists())

    def test_no_notice_for_proactive_grant_or_old_requests(self):
        self.assertEqual(self.author_action('grant').status_code, 302)
        self.assertFalse(EditorAIPermissionReplyNotice.objects.exists())
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(self.inbox()), 'No author AI replies match this filter.')

    def test_invalid_outcome_and_duplicate_request_notice_rejected(self):
        notice = self.reply()
        with self.assertRaises(IntegrityError), transaction.atomic():
            EditorAIPermissionReplyNotice.objects.create(permission_request=notice.permission_request, recipient=self.editor, outcome='invalid')
        with self.assertRaises(IntegrityError), transaction.atomic():
            EditorAIPermissionReplyNotice.objects.create(permission_request=notice.permission_request, recipient=self.editor, outcome='granted')
        EditorAIPermissionReplyNotice.objects.filter(pk=notice.pk).update(outcome='declined')
        self.assertEqual(self.client.get(self.detail(notice)).status_code, 404)

    def test_historical_when_round_closed_and_local_only(self):
        notice = self.reply()
        ReviewRound.objects.filter(pk=self.round.pk).update(status=ReviewRound.Status.CLOSED)
        self.assertContains(self.client.get(self.detail(notice)), 'This request is no longer current')
        with self.settings(DEBUG=False):
            self.assertEqual(self.client.get(self.inbox()).status_code, 404)
            self.assertEqual(self.client.get(self.detail(notice)).status_code, 404)
            self.assertEqual(self.client.post(self.read(notice)).status_code, 404)

    def test_chinese_interface_and_requester_deactivation(self):
        notice = self.reply()
        page = self.client.get(self.inbox(), HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(page, '作者 AI 授权回复')
        self.assertContains(page, '作者已同意此请求')
        page = self.client.get(self.detail(notice), HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(page, '当前已授权')
        get_user_model().objects.filter(pk=self.editor.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.detail(notice)).status_code, 302)

    def test_multiple_replies_paginate_and_unread_filter(self):
        notice = self.reply()
        req = notice.permission_request
        for i in range(20):
            req.pk = None
            req.source_fingerprint = f'{i:064x}'
            req.save(force_insert=True)
            EditorAIPermissionReplyNotice.objects.create(permission_request=req, recipient=self.editor, outcome='granted')
        page = self.client.get(self.inbox())
        self.assertEqual(page.context['page'].paginator.count, 21)
        self.assertEqual(len(page.context['page'].object_list), 20)
        self.assertEqual(self.client.get(self.inbox()+'?page=2').context['page'].number, 2)
        self.assertEqual(self.client.post(self.read(notice)).status_code, 302)
        page = self.client.get(self.inbox()+'?filter=unread')
        self.assertEqual(page.context['page'].paginator.count, 20)
