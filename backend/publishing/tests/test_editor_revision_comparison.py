import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import Http404
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import EDITOR_REVISION_COMPARISON, GatewayRejected, OPENAI, run_ai_task
from publishing.ai_provider import ProviderError
from publishing.editor_revision_comparison_provider import _parse_response
from publishing.editor_revision_comparison_views import _source_fingerprint
from publishing.models import (EditorialAssignment, EditorialDecision, EditorRevisionComparisonConsent, EditorRevisionComparisonRun, Journal,
    JournalAIPolicy, Membership, Organisation, ReviewRound, Submission, SubmissionVersion)


def output(decision_quote='clarify the method', response_quote='clarified the method', question='Where can this change be verified?'):
    item = {'decision_quote': decision_quote, 'response_quote': response_quote, 'question': question}
    return {'status': 'completed', 'output': [{'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'output_text', 'text': json.dumps({'items': [item]})}]}]}


class ComparisonAdapterTests(SimpleTestCase):
    def test_quotes_must_be_exact_and_provider_shape_valid(self):
        self.assertEqual(len(_parse_response(output(), 'Please clarify the method.', 'We clarified the method.')), 1)
        for invalid in (output(decision_quote='invented'), output(response_quote='invented'), output(question=''),
                        {'status': 'failed', 'output': []}, {'status': 'completed', 'output': []}):
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                _parse_response(invalid, 'Please clarify the method.', 'We clarified the method.')

    def test_gateway_rejects_extra_fields_before_provider(self):
        with patch('publishing.ai_gateway.request_editor_revision_comparison') as adapter:
            for payload in ({'decision_excerpt': 'x', 'response_excerpt': 'y', 'manuscript': 'private'},
                            {'decision_excerpt': None, 'response_excerpt': 'y'}, {'decision_excerpt': 'x'}, {}):
                with self.subTest(payload=payload), self.assertRaises(GatewayRejected):
                    run_ai_task(task=EDITOR_REVISION_COMPARISON, provider=OPENAI, model='test', api_key='test', payload=payload)
            adapter.assert_not_called()


@override_settings(DEBUG=True, IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL='test-model',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ComparisonWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user('private-author')
        cls.editor = users.create_user('assigned-editor')
        cls.other_editor = users.create_user('other-assigned-editor')
        cls.outsider = users.create_user('outsider')
        cls.manager = users.create_user('ai-manager')
        org = Organisation.objects.create(name='Fictional group', slug='comparison-group')
        cls.journal = Journal.objects.create(organisation=org, name='Sample journal', slug='comparison-journal')
        for user, role in ((cls.editor, Membership.Role.EDITOR), (cls.other_editor, Membership.Role.EDITOR),
                           (cls.manager, Membership.Role.AI_MANAGER)):
            Membership.objects.create(user=user, journal=cls.journal, role=role)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author, title='PRIVATE MANUSCRIPT TITLE',
            abstract='PRIVATE ABSTRACT', manuscript='private.pdf', status=Submission.Status.SUBMITTED)
        for editor in (cls.editor, cls.other_editor):
            EditorialAssignment.objects.create(submission=cls.submission, editor=editor, assigned_by=editor)
        old_version = SubmissionVersion.objects.create(submission=cls.submission, number=1, manuscript='old.pdf', uploaded_by=cls.author)
        old_round = ReviewRound.objects.create(submission=cls.submission, version=old_version, number=1, status=ReviewRound.Status.CLOSED)
        cls.decision = EditorialDecision.objects.create(submission=cls.submission, round=old_round, outcome='major',
            author_message='Please clarify the method. PRIVATE DECISION SUFFIX', decided_by=cls.editor)
        cls.version = SubmissionVersion.objects.create(submission=cls.submission, number=2, manuscript='new.pdf', uploaded_by=cls.author,
            author_response='We clarified the method. PRIVATE RESPONSE SUFFIX')
        cls.round = ReviewRound.objects.create(submission=cls.submission, version=cls.version, number=2)
        cls.policy = JournalAIPolicy.objects.create(journal=cls.journal, editor_revision_comparison_enabled=True, journal_daily_limit=10)
        cls.consent = EditorRevisionComparisonConsent.objects.create(submission=cls.submission, review_round=cls.round,
            decision=cls.decision, version=cls.version, granted_by=cls.author, provider='openai',
            purpose='editor-revision-comparison-v1', source_fingerprint=_source_fingerprint(cls.round, cls.decision))

    def setUp(self):
        env = patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-test-key'})
        env.start(); self.addCleanup(env.stop)
        transport = patch('publishing.editor_revision_comparison_provider._send_request', return_value=output())
        self.mock_transport = transport.start(); self.addCleanup(transport.stop)
        self.client.force_login(self.editor)

    def url(self): return reverse('editor_revision_comparison', args=[self.submission.pk])

    def data(self, **changes):
        current = ReviewRound.objects.select_related('version').get(pk=self.round.pk)
        data = {'request_id': str(uuid.uuid4()), 'decision_id': str(self.decision.pk), 'round_id': str(self.round.pk),
                'version_id': str(self.version.pk), 'policy_revision': str(self.policy.revision), 'model': 'test-model',
                'source_fingerprint': _source_fingerprint(current, self.decision), 'author_consent_id': str(self.consent.pk),
                'decision_excerpt': 'Please clarify the method.', 'response_excerpt': 'We clarified the method.',
                'confirm': 'yes', 'synthetic': 'yes'}
        data.update(changes)
        return data

    def test_scope_requires_current_assigned_editor_and_independent_policy(self):
        self.assertContains(self.client.get(self.url()), 'Compare a revision with AI')
        for user in (self.author, self.outsider, self.manager):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url()).status_code, 404)
            self.assertEqual(self.client.post(self.url(), self.data()).status_code, 404)
        self.client.force_login(self.editor)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(editor_revision_comparison_enabled=False, revision_plan_enabled=True)
        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 409)
        self.mock_transport.assert_not_called()

    def test_confirmations_csrf_and_exact_source_excerpts(self):
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.url(), self.data()).status_code, 403)
        for missing in ('confirm', 'synthetic'):
            self.assertEqual(self.client.post(self.url(), self.data(**{missing: ''})).status_code, 400)
        for field in ('decision_excerpt', 'response_excerpt'):
            self.assertEqual(self.client.post(self.url(), self.data(**{field: 'invented input'})).status_code, 400)
        for field in ('policy_revision', 'decision_id', 'round_id', 'version_id', 'source_fingerprint', 'author_consent_id', 'model'):
            self.assertEqual(self.client.post(self.url(), self.data(**{field: 'changed'})).status_code, 409)
        self.assertFalse(EditorRevisionComparisonRun.objects.exists())
        self.mock_transport.assert_not_called()

    def test_minimal_payload_result_visibility_and_idempotence(self):
        data = self.data()
        response = self.client.post(self.url(), data)
        self.assertEqual(response.status_code, 302)
        run = EditorRevisionComparisonRun.objects.get()
        self.assertEqual(run.status, 'completed')
        raw = self.mock_transport.call_args.args[0].data.decode('utf-8')
        payload = json.loads(raw)
        self.assertEqual(json.loads(payload['input']), {'decision_excerpt': 'Please clarify the method.',
                                                      'response_excerpt': 'We clarified the method.'})
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        for private in ('PRIVATE MANUSCRIPT TITLE', 'PRIVATE ABSTRACT', 'PRIVATE DECISION SUFFIX',
                        'PRIVATE RESPONSE SUFFIX', 'private-author', 'private.pdf'):
            self.assertNotIn(private, raw)
        page = self.client.get(response.url)
        self.assertContains(page, 'clarify the method')
        self.assertNotContains(page, 'synthetic-test-key')
        self.assertEqual(self.client.post(self.url(), data).status_code, 302)
        self.assertEqual(self.mock_transport.call_count, 1)
        for user in (self.other_editor, self.author, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.get(response.url).status_code, 404)

    def test_provider_failure_and_unverifiable_output_are_safe(self):
        self.mock_transport.side_effect = RuntimeError('PRIVATE PROVIDER FAILURE')
        response = self.client.post(self.url(), self.data())
        self.assertEqual(EditorRevisionComparisonRun.objects.get().status, 'failed')
        self.assertNotContains(self.client.get(response.url), 'PRIVATE PROVIDER FAILURE')
        self.mock_transport.side_effect = None
        self.mock_transport.return_value = output(response_quote='invented')
        self.client.post(self.url(), self.data())
        self.assertEqual(EditorRevisionComparisonRun.objects.filter(status='failed').count(), 2)

    def test_result_html_is_escaped(self):
        self.mock_transport.return_value = output(question='<script>alert(1)</script>')
        response = self.client.post(self.url(), self.data())
        page = self.client.get(response.url)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, '<script>alert(1)</script>')

    def test_source_or_assignment_change_rejects_submission_and_hides_results(self):
        data = self.data()
        response = self.client.post(self.url(), data)
        SubmissionVersion.objects.filter(pk=self.version.pk).update(author_response='New response')
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.assertEqual(self.client.post(self.url(), data).status_code, 409)
        self.assertEqual(self.mock_transport.call_count, 1)
        EditorialAssignment.objects.filter(submission=self.submission, editor=self.editor).update(status='recused')
        self.assertEqual(self.client.get(self.url()).status_code, 404)

    def test_pre_dispatch_recheck_cancels_if_access_changes(self):
        from publishing.editor_revision_views import current_revision_package
        original = current_revision_package(self.editor, self.submission.pk)
        with patch('publishing.editor_revision_comparison_views.current_revision_package', side_effect=[original, original, Http404]):
            response = self.client.post(self.url(), self.data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(EditorRevisionComparisonRun.objects.get().status, 'cancelled')
        self.mock_transport.assert_not_called()

    def test_shared_quota_counts_failed_comparisons(self):
        for _ in range(3):
            EditorRevisionComparisonRun.objects.create(id=uuid.uuid4(), submission=self.submission, review_round=self.round,
                version=self.version, decision=self.decision, requested_by=self.editor, model_name='test', prompt_version='test',
                policy_revision=1, source_fingerprint='a'*64, input_fingerprint='b'*64, status='failed')
        self.assertEqual(self.client.post(self.url(), self.data()).status_code, 429)
        self.mock_transport.assert_not_called()

    def test_ai_manager_can_enable_independent_switch(self):
        self.client.force_login(self.manager)
        response = self.client.post(reverse('manage_ai_policy', args=[self.journal.slug]),
            {'revision': '1', 'editor_revision_comparison_enabled': 'on', 'journal_daily_limit': '10',
             'confirm': 'yes', 'synthetic': 'yes'})
        self.assertEqual(response.status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.editor_revision_comparison_enabled)
        self.assertFalse(self.policy.revision_plan_enabled)
        self.assertFalse(self.policy.metadata_enabled)
