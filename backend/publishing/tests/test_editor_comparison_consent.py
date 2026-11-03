import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.editor_comparison_consent import PURPOSE, PROVIDER, active_consent, source_fingerprint
from publishing.editor_revision_views import author_current_revision_package
from publishing.models import (EditorialAssignment, EditorialDecision, EditorRevisionComparisonConsent,
    EditorRevisionComparisonRun, Journal, JournalAIPolicy, Membership, Organisation, ReviewRound, Submission, SubmissionVersion)
from publishing.tests.test_editor_revision_comparison import output


@override_settings(DEBUG=True, IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL='test-model',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class EditorComparisonConsentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user('submitting-author')
        cls.editor = users.create_user('assigned-editor')
        cls.other = users.create_user('other-user')
        cls.journal = Journal.objects.create(organisation=Organisation.objects.create(name='Publisher', slug='consent-publisher'),
                                             name='Journal', slug='consent-journal')
        Membership.objects.create(user=cls.editor, journal=cls.journal, role=Membership.Role.EDITOR)
        cls.sub = Submission.objects.create(journal=cls.journal, author=cls.author, title='Fictional title',
            abstract='Fictional abstract', manuscript='fake.pdf', status=Submission.Status.SUBMITTED)
        EditorialAssignment.objects.create(submission=cls.sub, editor=cls.editor, assigned_by=cls.editor)
        previous = SubmissionVersion.objects.create(submission=cls.sub, number=1, manuscript='old.pdf', uploaded_by=cls.author)
        round1 = ReviewRound.objects.create(submission=cls.sub, version=previous, number=1, status=ReviewRound.Status.CLOSED)
        cls.decision = EditorialDecision.objects.create(submission=cls.sub, round=round1, outcome='major',
            author_message='Please clarify the method.', decided_by=cls.editor)
        cls.version = SubmissionVersion.objects.create(submission=cls.sub, number=2, manuscript='new.pdf', uploaded_by=cls.author,
                                                        author_response='We clarified the method.')
        cls.round = ReviewRound.objects.create(submission=cls.sub, version=cls.version, number=2)
        cls.policy = JournalAIPolicy.objects.create(journal=cls.journal, editor_revision_comparison_enabled=True, journal_daily_limit=10)

    def setUp(self):
        self.env = patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-test-key'})
        self.env.start(); self.addCleanup(self.env.stop)
        transport = patch('publishing.editor_revision_comparison_provider._send_request', return_value=output())
        self.transport = transport.start(); self.addCleanup(transport.stop)
        self.client.force_login(self.author)

    def consent_url(self): return reverse('author_editor_ai_consent', args=[self.sub.pk])
    def compare_url(self): return reverse('editor_revision_comparison', args=[self.sub.pk])
    def token(self): return self.client.get(self.consent_url()).context['consent_token']
    def grant(self, token=None):
        return self.client.post(self.consent_url(), {'consent_token': token or self.token(), 'action':'grant',
                                                      'confirm':'yes', 'synthetic':'yes'})
    def withdraw(self, token=None):
        return self.client.post(self.consent_url(), {'consent_token': token or self.token(), 'action':'withdraw'})
    def editor_data(self, **kwargs):
        self.client.force_login(self.editor)
        page = self.client.get(self.compare_url())
        data = {'request_id':str(uuid.uuid4()), 'decision_id':str(self.decision.pk), 'round_id':str(self.round.pk),
                'version_id':str(self.version.pk), 'policy_revision':str(self.policy.revision), 'model':'test-model',
                'source_fingerprint':page.context['source_fingerprint'],
                'author_consent_id':str(page.context['author_consent'].pk) if page.context['author_consent'] else '',
                'decision_excerpt':'Please clarify the method.', 'response_excerpt':'We clarified the method.',
                'confirm':'yes', 'synthetic':'yes'}
        data.update(kwargs)
        return data

    def test_only_current_author_can_manage_and_open_from_progress(self):
        self.assertContains(self.client.get(reverse('author_submission_progress', args=[self.sub.pk])), 'Manage editor AI sharing permission')
        page = self.client.get(self.consent_url())
        self.assertContains(page, 'Control editor AI sharing')
        self.assertContains(page, self.sub.title)
        self.assertContains(page, self.journal.name)
        self.assertContains(page, 'Not allowed')
        self.assertContains(self.client.get(self.consent_url(), HTTP_ACCEPT_LANGUAGE='zh-hans'), '控制编辑 AI 内容分享')
        for user in (self.editor, self.other):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.consent_url()).status_code, 404)
            self.assertEqual(self.client.post(self.consent_url(), {'action':'withdraw'}).status_code, 404)

    def test_csrf_and_confirmations_are_required_for_grant(self):
        token = self.token()
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.consent_url(), {'action':'grant', 'consent_token':token,
                                                        'confirm':'yes', 'synthetic':'yes'}).status_code, 403)
        for key in ('confirm', 'synthetic'):
            data = {'action':'grant', 'consent_token':token, 'confirm':'yes', 'synthetic':'yes'}; data[key] = ''
            self.assertEqual(self.client.post(self.consent_url(), data).status_code, 400)
        self.assertEqual(self.grant(token + 'tampered').status_code, 409)
        self.assertFalse(EditorRevisionComparisonConsent.objects.exists())
        self.transport.assert_not_called()

    def test_grant_editor_request_and_withdrawal(self):
        data = self.editor_data()
        self.assertFalse(data['author_consent_id'])
        self.assertContains(self.client.get(self.compare_url()), 'Author permission required')
        self.assertEqual(self.client.post(self.compare_url(), data).status_code, 409)
        self.client.force_login(self.author)
        self.assertEqual(self.grant().status_code, 302)
        consent = EditorRevisionComparisonConsent.objects.get()
        self.assertIsNone(consent.ended_at)
        self.assertEqual(consent.granted_by, self.author)
        self.assertEqual((consent.provider, consent.purpose), (PROVIDER, PURPOSE))
        data = self.editor_data()
        self.assertEqual(data['author_consent_id'], str(consent.pk))
        self.assertContains(self.client.get(self.compare_url()), 'Author permission recorded')
        result = self.client.post(self.compare_url(), data)
        self.assertEqual(result.status_code, 302)
        run = EditorRevisionComparisonRun.objects.get()
        self.assertEqual(run.author_consent_id, consent.pk)
        payload = json.loads(self.transport.call_args.args[0].data.decode())
        self.assertEqual(set(json.loads(payload['input'])), {'decision_excerpt', 'response_excerpt'})
        self.client.force_login(self.author)
        self.assertEqual(self.withdraw().status_code, 302)
        consent.refresh_from_db()
        self.assertEqual(consent.end_reason, 'withdrawn')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.compare_url(), {**data, 'request_id':str(uuid.uuid4())}).status_code, 409)
        self.assertEqual(self.client.get(result.url).status_code, 200)
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(len(mail.outbox), 0)
        self.sub.refresh_from_db(); self.assertEqual(self.sub.status, Submission.Status.SUBMITTED)

    def test_current_source_change_invalidates_permission_and_supersedes_old(self):
        stale_token = self.token()
        SubmissionVersion.objects.filter(pk=self.version.pk).update(author_response='A revised response.')
        self.assertEqual(self.grant(stale_token).status_code, 409)
        self.assertEqual(self.grant().status_code, 302)
        old = EditorRevisionComparisonConsent.objects.get()
        SubmissionVersion.objects.filter(pk=self.version.pk).update(author_response='Another response.')
        page = self.client.get(self.consent_url())
        self.assertContains(page, 'Previous permission no longer applies')
        self.assertEqual(self.grant(page.context['consent_token']).status_code, 302)
        old.refresh_from_db(); self.assertEqual(old.end_reason, 'superseded')
        self.assertEqual(EditorRevisionComparisonConsent.objects.filter(ended_at__isnull=True).count(), 1)
        self.assertEqual(EditorRevisionComparisonConsent.objects.count(), 2)

    def test_stale_withdrawal_cannot_revoke_a_new_grant(self):
        self.assertEqual(self.grant().status_code, 302)
        stale = self.token()
        self.assertEqual(self.withdraw(stale).status_code, 302)
        self.assertEqual(self.grant().status_code, 302)
        self.assertEqual(self.withdraw(stale).status_code, 409)
        self.assertEqual(EditorRevisionComparisonConsent.objects.filter(ended_at__isnull=True).count(), 1)

    def test_revocation_remains_available_after_round_closes(self):
        self.assertEqual(self.grant().status_code, 302)
        ReviewRound.objects.filter(pk=self.round.pk).update(status=ReviewRound.Status.CLOSED)
        page = self.client.get(self.consent_url())
        self.assertContains(page, 'A returned revision must be current')
        self.assertContains(page, 'Withdraw permission')
        self.assertEqual(self.withdraw(page.context['consent_token']).status_code, 302)
        self.assertEqual(EditorRevisionComparisonConsent.objects.get().end_reason, 'withdrawn')

    def test_author_transfer_does_not_transfer_permission(self):
        self.assertEqual(self.grant().status_code, 302)
        Submission.objects.filter(pk=self.sub.pk).update(author=self.other)
        self.assertEqual(self.client.get(self.consent_url()).status_code, 404)
        data = self.editor_data()
        self.assertFalse(data['author_consent_id'])
        self.assertEqual(self.client.post(self.compare_url(), data).status_code, 409)
        self.transport.assert_not_called()

    def test_source_binding_rejects_cross_submission_version(self):
        self.assertEqual(self.grant().status_code, 302)
        other = Submission.objects.create(journal=self.journal, author=self.author, title='Other', abstract='x', manuscript='x')
        foreign = SubmissionVersion.objects.create(submission=other, number=1, manuscript='x', uploaded_by=self.author)
        ReviewRound.objects.filter(pk=self.round.pk).update(version=foreign)
        self.assertContains(self.client.get(self.consent_url()), 'No current returned revision')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.compare_url()).status_code, 404)

    def test_withdrawal_immediately_before_dispatch_cancels_request(self):
        self.assertEqual(self.grant().status_code, 302)
        data = self.editor_data()
        consent = EditorRevisionComparisonConsent.objects.get()
        with patch('publishing.editor_revision_comparison_views.active_consent', side_effect=[consent, consent, None]):
            response = self.client.post(self.compare_url(), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EditorRevisionComparisonRun.objects.get().status, 'cancelled')
        self.transport.assert_not_called()

    def test_old_attempts_do_not_infer_or_grant_permission(self):
        EditorRevisionComparisonRun.objects.create(id=uuid.uuid4(), submission=self.sub, review_round=self.round,
            version=self.version, decision=self.decision, requested_by=self.editor, model_name='test', prompt_version='test',
            policy_revision=1, source_fingerprint='a'*64, input_fingerprint='b'*64, status='completed')
        self.assertFalse(EditorRevisionComparisonConsent.objects.exists())
        current, decision = author_current_revision_package(self.author, self.sub.pk)
        self.assertIsNone(active_consent(current, decision))
        data = self.editor_data()
        self.assertEqual(self.client.post(self.compare_url(), data).status_code, 409)

    def test_active_consent_uniqueness_and_end_state_are_enforced(self):
        self.assertEqual(self.grant().status_code, 302)
        original = EditorRevisionComparisonConsent.objects.get()
        with self.assertRaises(IntegrityError), transaction.atomic():
            EditorRevisionComparisonConsent.objects.create(submission=self.sub, review_round=self.round, version=self.version,
                decision=self.decision, granted_by=self.author, provider=PROVIDER, purpose=PURPOSE,
                source_fingerprint=original.source_fingerprint)
        with self.assertRaises(IntegrityError), transaction.atomic():
            EditorRevisionComparisonConsent.objects.filter(pk=original.pk).update(ended_at=timezone.now(), end_reason='')

    @override_settings(DEBUG=False)
    def test_unavailable_outside_debug_prototype(self):
        self.assertEqual(self.client.get(self.consent_url()).status_code, 404)
        self.assertEqual(self.client.post(self.consent_url(), {'action':'withdraw'}).status_code, 404)
