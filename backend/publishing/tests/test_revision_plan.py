import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import GatewayRejected, OPENAI, REVISION_PLAN, run_ai_task
from publishing.ai_provider import ProviderError
from publishing.models import (EditorialDecision, Journal, JournalAIPolicy, Membership, Organisation,
                               ReviewRound, RevisionPlanRun, Submission, SubmissionVersion)
from publishing.revision_plan_provider import _parse_response


def provider_output(evidence='clarify the method', action='Describe the method in more detail.', prompt='What did you clarify?'):
    return {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text',
        'text':json.dumps({'items':[{'evidence':evidence,'revision_action':action,'response_prompt':prompt}]})}]}]}


class RevisionPlanAdapterTests(SimpleTestCase):
    def test_provider_rejects_unverifiable_or_invalid_output(self):
        self.assertEqual(len(_parse_response(provider_output(), 'Please clarify the method.')), 1)
        for value in [provider_output(evidence='not present'), provider_output(evidence=''),
                      {'status':'failed','output':[]}, {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'refusal','text':'no'}]}]}]:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                _parse_response(value, 'Please clarify the method.')

    def test_gateway_rejects_extra_inputs_before_provider(self):
        with patch('publishing.ai_gateway.request_revision_plan') as adapter:
            for payload in ({'comment':'x','notes':'y','manuscript':'secret'}, {'comment':None,'notes':''}, {'comment':'x'}, {}):
                with self.subTest(payload=payload), self.assertRaises(GatewayRejected):
                    run_ai_task(task=REVISION_PLAN,provider=OPENAI,api_key='test',model='test',payload=payload)
            adapter.assert_not_called()


@override_settings(DEBUG=True, IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL='test-model',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RevisionPlanWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users=get_user_model().objects
        cls.author=users.create_user('secret-fake-author')
        cls.editor=users.create_user('editor')
        cls.outsider=users.create_user('outsider')
        cls.manager=users.create_user('ai-manager')
        org=Organisation.objects.create(name='Fictional publisher',slug='plan-publisher')
        cls.journal=Journal.objects.create(organisation=org,name='Sample journal',slug='plan-journal')
        Membership.objects.create(user=cls.manager,journal=cls.journal,role=Membership.Role.AI_MANAGER)
        cls.submission=Submission.objects.create(journal=cls.journal,author=cls.author,title='PRIVATE TEST TITLE',
            abstract='PRIVATE TEST ABSTRACT',manuscript='private.pdf',status=Submission.Status.REVISION_REQUESTED)
        cls.version=SubmissionVersion.objects.create(submission=cls.submission,number=1,manuscript='private.pdf',uploaded_by=cls.author)
        cls.round=ReviewRound.objects.create(submission=cls.submission,version=cls.version,number=1,status=ReviewRound.Status.CLOSED)
        cls.decision=EditorialDecision.objects.create(submission=cls.submission,round=cls.round,outcome='major',
            author_message='PRIVATE DECISION LETTER',decided_by=cls.editor)
        cls.policy=JournalAIPolicy.objects.create(journal=cls.journal,revision_plan_enabled=True,journal_daily_limit=10)

    def setUp(self):
        self.env=patch.dict(os.environ, {'OPENAI_API_KEY':'synthetic-test-key'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.transport=patch('publishing.revision_plan_provider._send_request',return_value=provider_output())
        self.mock_transport=self.transport.start(); self.addCleanup(self.transport.stop)
        self.client.force_login(self.author)

    def url(self): return reverse('revision_plan', args=[self.submission.pk])

    def data(self, **overrides):
        data={'request_id':str(uuid.uuid4()),'decision_id':str(self.decision.pk),'policy_revision':str(self.policy.revision),
              'model':'test-model','comment':'Please clarify the method.','notes':'I plan to check the method section.',
              'confirm':'yes','synthetic':'yes'}
        data.update(overrides)
        return data

    def test_author_only_requires_current_decision_policy_configuration_and_consent(self):
        page=self.client.get(self.url())
        self.assertContains(page, 'Plan your revision')
        self.assertNotContains(page, 'PRIVATE DECISION LETTER')
        self.assertContains(self.client.get(self.url(), HTTP_ACCEPT_LANGUAGE='zh-hans'), '规划您的修回')
        for user in (self.editor,self.outsider,self.manager):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url()).status_code,404)
            self.assertEqual(self.client.post(self.url(),self.data()).status_code,404)
        self.client.force_login(self.author)
        for missing in ('confirm','synthetic'):
            self.assertEqual(self.client.post(self.url(),self.data(**{missing:''})).status_code,400)
        self.assertEqual(self.client.post(self.url(),self.data(decision_id='999')).status_code,409)
        self.assertEqual(self.client.post(self.url(),self.data(model='other-model')).status_code,409)
        self.assertEqual(self.client.post(self.url(),self.data(policy_revision='99')).status_code,409)
        self.assertEqual(self.client.post(self.url(),self.data(request_id='bad')).status_code,400)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(revision_plan_enabled=False)
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,409)
        self.assertFalse(RevisionPlanRun.objects.exists())
        self.mock_transport.assert_not_called()

    def test_consent_csrf_privacy_and_idempotent_request(self):
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.url(),self.data()).status_code,403)
        request_id=uuid.uuid4()
        response=self.client.post(self.url(),self.data(request_id=str(request_id)))
        self.assertEqual(response.status_code,302)
        run=RevisionPlanRun.objects.get(pk=request_id)
        self.assertEqual(run.status,'completed')
        self.assertEqual(run.decision_id,self.decision.pk)
        request=self.mock_transport.call_args.args[0]
        payload=json.loads(request.data.decode('utf-8'))
        self.assertEqual(json.loads(payload['input']), {'comment':'Please clarify the method.',
                                                       'author_notes':'I plan to check the method section.'})
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        for secret in ('PRIVATE TEST TITLE','PRIVATE TEST ABSTRACT','PRIVATE DECISION LETTER','secret-fake-author','private.pdf'):
            self.assertNotIn(secret,request.data.decode('utf-8'))
        page=self.client.get(response.url)
        self.assertContains(page,'clarify the method')
        self.assertNotContains(page,'synthetic-test-key')
        self.assertNotContains(page,'PRIVATE DECISION LETTER')
        self.assertEqual(self.client.post(self.url(),self.data(request_id=str(request_id))).status_code,302)
        self.assertEqual(self.mock_transport.call_count,1)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(response.url).status_code,404)

    def test_provider_failure_and_invalid_evidence_never_expose_details(self):
        self.mock_transport.side_effect=RuntimeError('PRIVATE PROVIDER DETAIL')
        response=self.client.post(self.url(),self.data())
        run=RevisionPlanRun.objects.get()
        self.assertEqual(run.status,'failed')
        self.assertEqual(run.result,[])
        self.assertNotContains(self.client.get(response.url),'PRIVATE PROVIDER DETAIL')
        self.mock_transport.side_effect=None
        self.mock_transport.return_value=provider_output(evidence='invented quote')
        self.client.post(self.url(),self.data())
        self.assertEqual(RevisionPlanRun.objects.filter(status='failed').count(),2)

    def test_provider_content_is_escaped_and_result_checks_source_integrity(self):
        self.mock_transport.return_value=provider_output(action='<script>alert(1)</script>')
        response=self.client.post(self.url(),self.data())
        page=self.client.get(response.url)
        self.assertContains(page,'&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page,'<script>alert(1)</script>')
        other=Submission.objects.create(journal=self.journal,author=self.author,title='Other',abstract='x',manuscript='x')
        other_version=SubmissionVersion.objects.create(submission=other,number=1,manuscript='x',uploaded_by=self.author)
        ReviewRound.objects.filter(pk=self.round.pk).update(version=other_version)
        self.assertEqual(self.client.get(response.url).status_code,404)

    def test_recheck_cancels_when_decision_or_policy_changes_before_sending(self):
        with patch('publishing.revision_plan_views._current_decision', side_effect=[self.decision,self.decision,None]):
            response=self.client.post(self.url(),self.data())
        self.assertEqual(response.status_code,302)
        self.assertEqual(RevisionPlanRun.objects.get().status,'cancelled')
        self.mock_transport.assert_not_called()

    def test_old_result_stays_bound_to_original_author_and_source(self):
        response=self.client.post(self.url(),self.data())
        self.assertEqual(self.client.get(response.url).status_code,200)
        Submission.objects.filter(pk=self.submission.pk).update(author=self.outsider)
        self.assertEqual(self.client.get(response.url).status_code,404)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(response.url).status_code,404)

    def test_shared_limits_include_revision_attempts(self):
        for _ in range(3):
            RevisionPlanRun.objects.create(id=uuid.uuid4(),submission=self.submission,decision=self.decision,
                requested_by=self.author,model_name='test',prompt_version='test',policy_revision=1,input_fingerprint='a'*64,status='failed')
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,429)
        self.mock_transport.assert_not_called()

    def test_wrong_status_closed_journal_and_cross_link_block_access(self):
        Submission.objects.filter(pk=self.submission.pk).update(status='submitted')
        self.assertEqual(self.client.get(self.url()).status_code,409)
        Submission.objects.filter(pk=self.submission.pk).update(status='revision_requested')
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(self.client.get(self.url()).status_code,404)
        Journal.objects.filter(pk=self.journal.pk).update(active=True)
        other=Submission.objects.create(journal=self.journal,author=self.author,title='Other',abstract='x',manuscript='x')
        foreign=SubmissionVersion.objects.create(submission=other,number=1,manuscript='x',uploaded_by=self.author)
        ReviewRound.objects.filter(pk=self.round.pk).update(version=foreign)
        self.assertEqual(self.client.get(self.url()).status_code,409)
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,409)
        self.mock_transport.assert_not_called()

    def test_ai_policy_switch_is_independent(self):
        self.client.force_login(self.manager)
        policy_url=reverse('manage_ai_policy',args=[self.journal.slug])
        response=self.client.post(policy_url, {'revision':'1','revision_plan_enabled':'on','journal_daily_limit':'10',
                                               'confirm':'yes','synthetic':'yes'})
        self.assertEqual(response.status_code,302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.revision_plan_enabled)
        self.assertFalse(self.policy.metadata_enabled)
