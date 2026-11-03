import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import GatewayRejected, KEYWORD_SUGGESTIONS, OPENAI, run_ai_task
from publishing.ai_provider import ProviderError
from publishing.ai_usage import account_requests_since, journal_requests_since
from publishing.keyword_ai_provider import _parse_response, input_fingerprint
from publishing.models import Journal, JournalAIPolicy, KeywordSuggestionRun, Organisation, Submission, SubmissionEvent, SubmissionVersion
from django.utils import timezone


def output(items=None):
    if items is None:
        items=[{'keyword':'Research collaboration','evidence':'research collaboration'}, {'keyword':'Governance','evidence':'governance'}]
    return {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps({'items':items})}]}]}


class KeywordAdapterTests(SimpleTestCase):
    def test_response_is_bounded_and_evidence_is_an_exact_source_quote(self):
        title='AI and research collaboration'; abstract='Responsible governance for teams.'
        self.assertEqual(len(_parse_response(output(),title,abstract)),2)
        bad=[output([{'keyword':'Invented','evidence':'not in text'}]), output([{'keyword':'x','evidence':''}]),
             output([{'keyword':'X','evidence':'governance'},{'keyword':'x','evidence':'governance'}]),
             output([{'keyword':'x\nattack','evidence':'governance'}]), output([{'keyword':'x'*101,'evidence':'governance'}]),
             output([{'keyword':str(i),'evidence':'governance'} for i in range(9)]),
             {'status':'failed','output':[]}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                _parse_response(value,title,abstract)

    def test_gateway_rejects_unauthorised_input_fields(self):
        with patch('publishing.ai_gateway.request_keyword_suggestions') as adapter:
            for payload in ({'title':'T','abstract':'A','file':'secret.pdf'}, {'title':None,'abstract':'A'}, {'title':'T'}, {}):
                with self.subTest(payload=payload), self.assertRaises(GatewayRejected):
                    run_ai_task(task=KEYWORD_SUGGESTIONS,provider=OPENAI,model='test',api_key='key',payload=payload)
            adapter.assert_not_called()


@override_settings(DEBUG=True, IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL='test-model',
                   PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class KeywordWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users=get_user_model().objects
        cls.author=users.create_user('author')
        cls.outsider=users.create_user('outsider')
        org=Organisation.objects.create(name='Fictional publisher',slug='keyword-publisher')
        cls.journal=Journal.objects.create(organisation=org,name='Test journal',slug='keyword-journal')
        cls.submission=Submission.objects.create(journal=cls.journal,author=cls.author,title='AI and research collaboration',
            abstract='Responsible governance for teams.',keywords_config=['Responsible AI'],manuscript='private-file.pdf')
        cls.version=SubmissionVersion.objects.create(submission=cls.submission,number=1,uploaded_by=cls.author,
            manuscript='private-file.pdf',title_snapshot=cls.submission.title,abstract_snapshot=cls.submission.abstract,
            keywords_snapshot=['Responsible AI'])
        cls.policy=JournalAIPolicy.objects.create(journal=cls.journal,keyword_suggestions_enabled=True,journal_daily_limit=10)

    def setUp(self):
        env=patch.dict(os.environ, {'OPENAI_API_KEY':'synthetic-test-key'}); env.start(); self.addCleanup(env.stop)
        transport=patch('publishing.keyword_ai_provider._send_request',return_value=output()); self.mock=transport.start(); self.addCleanup(transport.stop)
        self.client.force_login(self.author)

    def url(self): return reverse('keyword_suggestions',args=[self.submission.pk])
    def data(self,**kwargs):
        data={'request_id':str(uuid.uuid4()),'policy_revision':str(self.policy.revision),'model':'test-model',
            'fingerprint':input_fingerprint(self.version.pk,self.submission.title,self.submission.abstract,self.submission.keywords_config),
            'confirm':'yes','synthetic':'yes'}
        data.update(kwargs); return data
    def make_request(self):
        response=self.client.post(self.url(),self.data()); self.assertEqual(response.status_code,302)
        return KeywordSuggestionRun.objects.latest('created_at')
    def apply(self,run,**kwargs):
        data={'confirm':'yes','keyword':['Governance']}; data.update(kwargs)
        return self.client.post(reverse('apply_keywords',args=[run.pk]),data)

    def test_role_configuration_and_consent_gate_every_request(self):
        self.assertContains(self.client.get(self.url()), 'Find the right keywords')
        self.assertContains(self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE='zh-hans'),'找到合适的关键词')
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.url(),self.data()).status_code,403)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.url()).status_code,404)
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,404)
        self.client.force_login(self.author)
        for missing in ('confirm','synthetic'):
            self.assertEqual(self.client.post(self.url(),self.data(**{missing:''})).status_code,400)
        for bad in ({'model':'other'},{'policy_revision':'999'},{'fingerprint':'stale'}):
            self.assertEqual(self.client.post(self.url(),self.data(**bad)).status_code,409)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(keyword_suggestions_enabled=False)
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,409)
        self.assertFalse(KeywordSuggestionRun.objects.exists()); self.mock.assert_not_called()
        with self.settings(DEBUG=False): self.assertEqual(self.client.get(self.url()).status_code,404)

    def test_request_uses_only_title_and_abstract_and_is_idempotent(self):
        data=self.data();response=self.client.post(self.url(),data)
        run=KeywordSuggestionRun.objects.get(); self.assertEqual(run.status,'completed')
        request=self.mock.call_args.args[0];payload=json.loads(request.data.decode())
        self.assertEqual(json.loads(payload['input']),{'title':self.submission.title,'abstract':self.submission.abstract})
        self.assertFalse(payload['store'])
        for private in ('private-file.pdf','Responsible AI','synthetic-test-key','author'):
            self.assertNotIn(private,payload['input'])
        self.assertContains(self.client.get(response.url),'Source excerpt')
        self.client.post(self.url(),data);self.assertEqual(self.mock.call_count,1)
        self.assertEqual(account_requests_since(self.author,timezone.now()-timezone.timedelta(days=1)),1)
        self.assertEqual(journal_requests_since(self.journal,timezone.now()-timezone.timedelta(days=1)),1)
        self.client.force_login(self.outsider);self.assertEqual(self.client.get(response.url).status_code,404)

    def test_author_selects_and_existing_keywords_are_preserved_without_submission(self):
        run=self.make_request(); self.assertEqual(self.apply(run).status_code,302)
        self.submission.refresh_from_db();self.version.refresh_from_db();run.refresh_from_db()
        self.assertEqual(self.submission.keywords_config,['Responsible AI','Governance'])
        self.assertEqual(self.version.keywords_snapshot,self.submission.keywords_config)
        self.assertEqual(self.submission.status,Submission.Status.DRAFT);self.assertIsNotNone(run.applied_at)
        self.assertEqual(self.apply(run).status_code,302)
        self.assertEqual(SubmissionEvent.objects.filter(action='keyword_ai_selection_applied').count(),1)

    def test_forged_duplicate_or_excessive_selections_do_not_modify_draft(self):
        run=self.make_request()
        for selected in (['invented'],['Governance','Governance'],[],['x']*9):
            self.assertEqual(self.apply(run,keyword=selected).status_code,400)
        self.assertEqual(self.apply(run,confirm='').status_code,400)
        self.submission.refresh_from_db();self.assertEqual(self.submission.keywords_config,['Responsible AI'])

    def test_draft_or_policy_change_invalidates_selection(self):
        run=self.make_request()
        Submission.objects.filter(pk=self.submission.pk).update(title='Changed')
        page=self.client.get(reverse('keyword_result',args=[run.pk]))
        self.assertContains(page,'Request fresh suggestions')
        self.assertEqual(self.apply(run).status_code,409)
        Submission.objects.filter(pk=self.submission.pk).update(title=self.submission.title)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(revision=2)
        self.assertEqual(self.apply(run).status_code,409)

    def test_changes_during_external_call_discard_the_response(self):
        def changed(_request):
            Submission.objects.filter(pk=self.submission.pk).update(abstract='Changed while awaiting result.')
            return output()
        self.mock.side_effect=changed
        run=self.make_request();self.assertEqual(run.status,'cancelled');self.assertEqual(run.result,[])

    def test_provider_failure_is_generic_and_does_not_save_details(self):
        self.mock.side_effect=RuntimeError('PRIVATE provider detail')
        run=self.make_request();self.assertEqual(run.status,'failed');self.assertEqual(run.result,[])
        page=self.client.get(reverse('keyword_result',args=[run.pk]));self.assertNotContains(page,'PRIVATE provider detail')

    def test_quota_counts_attempts(self):
        for _ in range(3): self.make_request()
        self.assertEqual(self.client.post(self.url(),self.data()).status_code,429)
        self.assertEqual(self.mock.call_count,3)
