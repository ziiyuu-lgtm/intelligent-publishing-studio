import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.ai_gateway import GatewayRejected, OPENAI, REVIEW_SYNTHESIS, run_ai_task
from publishing.ai_provider import ProviderError
from publishing.models import EditorialAssignment, JournalAIPolicy, Membership, ReviewAIConsent, ReviewSynthesisRun
from publishing.review_ai_consent import existing_reviewer
from publishing.review_synthesis_provider import parse_response
from publishing.tests import test_review_ai_consent


def model_output(a='Please clarify the methods.', b='The methods need clarification.', kind='shared'):
    item = {'kind':kind, 'summary':'Both reports discuss the methods.',
            'evidence':[{'source':'A','quote':a}, {'source':'B','quote':b}]}
    return {'status':'completed','output':[{'type':'message','role':'assistant','content':[
        {'type':'output_text','text':json.dumps({'items':[item]})}]}]}


class ReviewSynthesisAdapterTests(SimpleTestCase):
    def setUp(self):
        self.sources = [{'source':'A','excerpt':'Please clarify the methods.'},
                        {'source':'B','excerpt':'The methods need clarification.'}]

    def test_quotes_must_match_sources_and_two_sources_support_comparisons(self):
        self.assertEqual(len(parse_response(model_output(), self.sources)), 1)
        examples = [model_output(a='fabricated'), model_output(b='fabricated'),
            {'status':'failed','output':[]}, {'status':'completed','output':[]},
            {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'refusal','refusal':'no'}]}]}]
        same_source = model_output()
        raw = json.loads(same_source['output'][0]['content'][0]['text'])
        raw['items'][0]['evidence'][1] = {'source':'A','quote':'Please clarify'}
        same_source['output'][0]['content'][0]['text'] = json.dumps(raw)
        examples.append(same_source)
        for invalid in examples:
            with self.subTest(invalid=invalid), self.assertRaises(ProviderError):
                parse_response(invalid, self.sources)
        empty = {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"items":[]}'}]}]}
        self.assertEqual(parse_response(empty, self.sources), [])

    def test_gateway_never_forwards_extra_fields_or_bad_labels(self):
        with patch('publishing.ai_gateway.request_review_synthesis') as provider:
            for payload in ({'sources':self.sources, 'title':'private'}, {'sources':self.sources[:1]},
                            {'sources':[{'source':'A','excerpt':'a'}, {'source':'A','excerpt':'b'}]}, {}):
                with self.subTest(payload=payload), self.assertRaises(GatewayRejected):
                    run_ai_task(task=REVIEW_SYNTHESIS, provider=OPENAI, model='test', api_key='test', payload=payload)
            provider.assert_not_called()


@override_settings(DEBUG=True, IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL='test-model',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReviewSynthesisWorkflowTests(TestCase):
    create_draft = test_review_ai_consent.ReviewAIPermissionTests.create_draft
    submit = test_review_ai_consent.ReviewAIPermissionTests.submit
    invite = test_review_ai_consent.ReviewAIPermissionTests.invite
    report = test_review_ai_consent.ReviewAIPermissionTests.report
    add_reviewer = test_review_ai_consent.ReviewAIPermissionTests.add_reviewer
    choose = test_review_ai_consent.ReviewAIPermissionTests.choose

    def setUp(self):
        test_review_ai_consent.ReviewAIPermissionTests.setUp(self)
        self.report(self.assignment, 'Please clarify the methods. PRIVATE FIRST SUFFIX')
        self.second = self.add_reviewer('second-reviewer-private', comments='The methods need clarification. PRIVATE SECOND SUFFIX')
        self.third = self.add_reviewer('third-reviewer-private', comments='UNSELECTED PRIVATE REPORT')
        self.choose(self.author, self.author_url)
        self.choose(self.reviewer, self.reviewer_url)
        self.choose(self.second.reviewer, reverse('reviewer_review_ai_permission', args=[self.second.pk]))
        self.policy = JournalAIPolicy.objects.create(journal=self.journal, review_synthesis_enabled=True, journal_daily_limit=10)
        env = patch.dict(os.environ, {'OPENAI_API_KEY':'synthetic-api-key'})
        env.start(); self.addCleanup(env.stop)
        transport = patch('publishing.review_synthesis_provider._send_request')
        self.transport = transport.start(); self.addCleanup(transport.stop)
        self.client.force_login(self.editor)
        self.url = reverse('review_synthesis_request', args=[self.submission.pk, self.assignment.round.number])

    def data(self):
        self.client.force_login(self.editor)
        page = self.client.get(self.url)
        rows = [row for row in page.context['rows'] if row['permission']]
        excerpts = [row['assignment'].comments.split(' PRIVATE')[0] for row in rows]
        self.transport.return_value = model_output(a=excerpts[0], b=excerpts[1])
        data = {'request_id':str(page.context['request_id']), 'scope_token':page.context['scope_token'],
                'selected':[str(row['assignment'].pk) for row in rows], 'confirm':'yes', 'synthetic':'yes'}
        for row, excerpt in zip(rows, excerpts):
            data['excerpt_'+str(row['assignment'].pk)] = excerpt
        return data

    def test_selected_excerpts_only_and_verified_private_result(self):
        data = self.data()
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        run = ReviewSynthesisRun.objects.get()
        self.assertEqual(run.status, 'completed')
        self.assertEqual(len(run.result), 1)
        body = self.transport.call_args.args[0].data.decode('utf-8')
        payload = json.loads(body)
        sent = json.loads(payload['input'])
        self.assertEqual(len(sent['sources']), 2)
        self.assertEqual([s['source'] for s in sent['sources']], ['A','B'])
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        for private in ('PRIVATE FIRST SUFFIX','PRIVATE SECOND SUFFIX','UNSELECTED PRIVATE REPORT',
                        self.submission.title,self.author.username,self.reviewer.username,'synthetic-api-key'):
            self.assertNotIn(private, body)
        self.assertNotIn('Please clarify the methods.', json.dumps(run.sources))
        result = self.client.get(response.url)
        self.assertContains(result, 'Review comparison draft')
        self.assertContains(result, 'Both reports discuss the methods.')
        self.assertContains(result, 'Please clarify the methods.')
        self.assertIn('no-store', result['Cache-Control'])
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.transport.call_count, 1)
        first = 'excerpt_'+data['selected'][0]
        data[first] = data[first][:12]
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertEqual(self.transport.call_count, 1)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.get(response.url).status_code, 404)

    def test_role_policy_consent_csrf_and_invalid_selection_fail_closed(self):
        data = self.data()
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.editor)
        self.assertEqual(csrf.post(self.url, data).status_code, 403)
        for user in (self.author,self.reviewer,self.coordinator,self.other_editor,self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.client.post(self.url, data).status_code, 404)
        self.client.force_login(self.editor)
        for missing in ('confirm','synthetic'):
            bad = dict(data); bad[missing]=''
            self.assertEqual(self.client.post(self.url, bad).status_code, 400)
        bad = dict(data); bad['selected']=[str(self.third.pk),data['selected'][0]]
        self.assertEqual(self.client.post(self.url, bad).status_code, 400)
        bad = dict(data); bad['selected']=[data['selected'][0],data['selected'][0]]
        self.assertEqual(self.client.post(self.url, bad).status_code, 400)
        bad = dict(data); bad['excerpt_'+data['selected'][0]]='not in this report'
        self.assertEqual(self.client.post(self.url, bad).status_code, 400)
        self.policy.review_synthesis_enabled=False; self.policy.save(update_fields=['review_synthesis_enabled'])
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertFalse(ReviewSynthesisRun.objects.exists())
        self.transport.assert_not_called()

    def test_withdrawal_before_and_during_request(self):
        data = self.data()
        self.choose(self.author, self.author_url, action='withdraw')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.transport.assert_not_called()
        self.choose(self.author, self.author_url)
        data = self.data()
        consent = existing_reviewer(self.assignment, self.reviewer)
        output = self.transport.return_value
        def withdraw_during(_request):
            ReviewAIConsent.objects.filter(pk=consent.pk).update(ended_at=timezone.now(), end_reason='withdrawn')
            return output
        self.transport.side_effect = withdraw_during
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 409)
        run = ReviewSynthesisRun.objects.get()
        self.assertEqual((run.status, run.result), ('cancelled', []))
        self.assertEqual(self.transport.call_count, 1)

    def test_source_or_reviewer_access_change_invalidates_results(self):
        data = self.data()
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        self.assignment.comments = 'Replaced fictional report.'
        self.assignment.save(update_fields=['comments'])
        self.assertEqual(self.client.get(response.url).status_code, 404)
        self.assertEqual(self.client.post(self.url, data).status_code, 409)
        self.assertEqual(self.transport.call_count, 1)

    def test_unverifiable_provider_output_and_errors_are_safe(self):
        data = self.data()
        self.transport.return_value = model_output(a='invented quotation')
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ReviewSynthesisRun.objects.get().status, 'failed')
        self.assertContains(self.client.get(response.url), 'could not be generated')
        self.transport.side_effect = RuntimeError('SECRET PROVIDER DETAILS')
        response = self.client.post(self.url, self.data())
        self.assertNotContains(self.client.get(response.url), 'SECRET PROVIDER DETAILS')
        self.assertEqual(ReviewSynthesisRun.objects.filter(status='failed').count(), 2)

    def test_shared_quota_counts_attempts(self):
        data = self.data()
        self.policy.journal_daily_limit=1; self.policy.save(update_fields=['journal_daily_limit'])
        # Policy data changed without a new form; acquire a current form and use it.
        data = self.data()
        self.assertEqual(self.client.post(self.url, data).status_code, 302)
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 429)
        self.assertEqual(self.transport.call_count, 1)

    def test_result_access_requires_the_requester_even_for_another_assigned_editor(self):
        response = self.client.post(self.url, self.data())
        users = get_user_model().objects
        another = users.create_user('another-assigned-editor')
        Membership.objects.create(user=another, journal=self.journal, role='editor')
        EditorialAssignment.objects.create(submission=self.submission, editor=another, assigned_by=self.coordinator)
        self.client.force_login(another)
        self.assertEqual(self.client.get(response.url).status_code, 404)

    def test_withdrawn_permission_hides_an_existing_result(self):
        response = self.client.post(self.url, self.data())
        self.choose(self.reviewer, self.reviewer_url, action='withdraw')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(response.url).status_code, 404)

    def test_model_text_is_escaped(self):
        data = self.data()
        result = self.transport.return_value
        payload = json.loads(result['output'][0]['content'][0]['text'])
        payload['items'][0]['summary'] = '<script>alert(1)</script>'
        result['output'][0]['content'][0]['text'] = json.dumps(payload)
        response = self.client.post(self.url, data)
        page = self.client.get(response.url)
        self.assertContains(page, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page, '<script>alert(1)</script>')

    def test_ai_manager_controls_an_independent_default_off_switch(self):
        self.assertFalse(JournalAIPolicy(journal=self.journal).review_synthesis_enabled)
        self.policy.review_synthesis_enabled = False
        self.policy.save(update_fields=['review_synthesis_enabled'])
        manager = get_user_model().objects.create_user('fictional-ai-manager')
        Membership.objects.create(user=manager, journal=self.journal, role='ai_manager')
        self.client.force_login(manager)
        response = self.client.post(reverse('manage_ai_policy', args=[self.journal.slug]),
            {'revision':str(self.policy.revision), 'review_synthesis_enabled':'on', 'journal_daily_limit':'10',
             'confirm':'yes', 'synthetic':'yes'})
        self.assertEqual(response.status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.review_synthesis_enabled)
        self.assertFalse(self.policy.editor_revision_comparison_enabled)

    def test_unconfigured_or_non_debug_environment_blocks_the_task(self):
        data = self.data()
        with override_settings(IPS_OPENAI_ENABLED=False):
            self.assertEqual(self.client.post(self.url, data).status_code, 409)
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get(self.url).status_code, 404)
            self.assertEqual(self.client.post(self.url, data).status_code, 404)
        self.transport.assert_not_called()
