import uuid
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (EditorialDecision, Journal, Membership, Organisation, ReviewRound, RevisionPlanRun,
                               RevisionResponseDraft, Submission, SubmissionEvent, SubmissionVersion)
from publishing.revision_response_views import compose_response


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RevisionResponseTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user('author-response-owner')
        cls.editor = users.create_user('editor-response')
        cls.other = users.create_user('unrelated-user')
        org = Organisation.objects.create(name='Fictional publisher', slug='response-publisher')
        cls.journal = Journal.objects.create(organisation=org, name='Fictional journal', slug='response-journal')
        cls.submission = Submission.objects.create(journal=cls.journal,author=cls.author,title='Private submission',
            abstract='fictional',manuscript='old.pdf',status='revision_requested')
        cls.version = SubmissionVersion.objects.create(submission=cls.submission,number=1,manuscript='old.pdf',uploaded_by=cls.author)
        cls.round = ReviewRound.objects.create(submission=cls.submission,version=cls.version,number=1,status='closed')
        cls.decision = EditorialDecision.objects.create(submission=cls.submission,round=cls.round,outcome='major',
            author_message='PRIVATE DECISION',decided_by=cls.editor)
        cls.plan = RevisionPlanRun.objects.create(id=uuid.uuid4(),submission=cls.submission,decision=cls.decision,
            requested_by=cls.author,model_name='test',prompt_version='test',policy_revision=1,input_fingerprint='a'*64,
            status='completed',result=[{'evidence':'Clarify the method','revision_action':'INVENTED CLAIM',
                                        'response_prompt':'Question about methods'}])

    def setUp(self):
        self.client.force_login(self.author)

    def url(self): return reverse('revision_response_builder', args=[self.submission.pk])

    def data(self, entries=None, **overrides):
        entries = entries if entries is not None else [{'comment':'Clarify the method','response':'We clarified the study design.',
                                                        'location':'Methods, paragraph 2'}]
        data={'decision_id':str(self.decision.pk),'draft_revision':'0','source_run':'',
              'point-TOTAL_FORMS':str(len(entries)), 'point-INITIAL_FORMS':str(len(entries)),
              'point-MIN_NUM_FORMS':'0','point-MAX_NUM_FORMS':'10','action':'save'}
        for i,item in enumerate(entries):
            for field in ('comment','response','location'):
                data[f'point-{i}-{field}']=item.get(field,'')
        data.update(overrides)
        return data

    def test_selected_ai_result_supplies_only_editable_source_excerpt(self):
        response=self.client.get(self.url(),{'plan':str(self.plan.pk)})
        self.assertContains(response,'Clarify the method')
        self.assertNotContains(response,'INVENTED CLAIM')
        self.assertNotContains(response,'Question about methods')
        self.assertFalse(RevisionResponseDraft.objects.exists())
        self.assertContains(self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE='zh-hans'),'撰写您的回复')

    def test_save_preview_and_prefill_are_explicit_and_private(self):
        response=self.client.post(self.url(),self.data(source_run=str(self.plan.pk)))
        self.assertEqual(response.status_code,302)
        draft=RevisionResponseDraft.objects.get()
        self.assertEqual(draft.source_run_id,self.plan.pk)
        self.assertEqual(draft.entries[0]['response'],'We clarified the study design.')
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status,'revision_requested')
        self.assertEqual(self.submission.versions.count(),1)
        self.assertTrue(SubmissionEvent.objects.filter(submission=self.submission,action='revision_response_draft_saved').exists())
        event=SubmissionEvent.objects.get(submission=self.submission,action='revision_response_draft_saved')
        self.assertNotIn('study design',str(event.action))
        page=self.client.get(self.url())
        self.assertContains(page,'We clarified the study design.')
        prefill=reverse('submission_revision',args=[self.submission.pk])
        page=self.client.get(prefill,{'response_draft':draft.pk,'revision':draft.revision})
        self.assertContains(page,'The saved response draft was copied')
        self.assertContains(page,'Clarify the method')
        self.assertContains(page,'We clarified the study design.')
        self.assertContains(page,'Methods, paragraph 2')
        self.assertNotContains(page,'INVENTED CLAIM')
        self.assertNotContains(page,'PRIVATE DECISION')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(self.url()).status_code,404)
        self.assertEqual(self.client.get(prefill,{'response_draft':draft.pk,'revision':draft.revision}).status_code,404)

    def test_incomplete_draft_cannot_be_used_and_must_be_saved_first(self):
        self.client.post(self.url(),self.data(entries=[{'comment':'Improve clarity','response':'','location':''}]))
        draft=RevisionResponseDraft.objects.get()
        page=self.client.get(self.url())
        self.assertContains(page,'Complete every response')
        prefill=reverse('submission_revision',args=[self.submission.pk])
        self.assertEqual(self.client.get(prefill,{'response_draft':draft.pk,'revision':draft.revision}).status_code,409)
        self.assertEqual(self.client.get(prefill,{'response_draft':draft.pk,'revision':'999'}).status_code,409)
        self.assertEqual(self.client.get(prefill,{'response_draft':'not-uuid','revision':'1'}).status_code,404)

    def test_author_reviews_copied_text_before_explicit_revision_submission(self):
        self.client.post(self.url(),self.data(source_run=str(self.plan.pk)))
        draft=RevisionResponseDraft.objects.get()
        url=reverse('submission_revision',args=[self.submission.pk])
        page=self.client.get(url,{'response_draft':draft.pk,'revision':draft.revision})
        edited=page.context['form'].initial['author_response']+'\n\nAuthor-reviewed addition.'
        with tempfile.TemporaryDirectory(prefix='ips-response-revision-') as media:
            with override_settings(MEDIA_ROOT=media):
                result=self.client.post(url,{'revision_token':page.context['revision_token'],
                                             'manuscript':SimpleUploadedFile('revision.pdf',b'%PDF-1.4\n%%EOF\n'),
                                             'author_response':edited})
        self.assertEqual(result.status_code,302)
        latest=SubmissionVersion.objects.get(submission=self.submission,number=2)
        self.assertEqual(latest.author_response,edited)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status,'submitted')
        self.assertEqual(self.client.get(self.url()).status_code,409)
        self.assertTrue(RevisionResponseDraft.objects.filter(pk=draft.pk).exists())

    def test_stale_writes_and_add_intermediate_do_not_overwrite_newer_draft(self):
        self.client.post(self.url(),self.data())
        draft=RevisionResponseDraft.objects.get()
        first=self.data(draft_revision='1',**{'point-0-response':'Updated response'})
        self.assertEqual(self.client.post(self.url(),first).status_code,302)
        draft.refresh_from_db(); self.assertEqual(draft.revision,2)
        for action in ('save','add'):
            stale=self.data(draft_revision='1',action=action,**{'point-0-response':'Outdated'})
            self.assertEqual(self.client.post(self.url(),stale).status_code,409)
        draft.refresh_from_db(); self.assertEqual(draft.entries[0]['response'],'Updated response')
        page=self.client.post(self.url(),self.data(draft_revision='2',action='add',**{'point-0-response':'Unsaved addition'}))
        self.assertContains(page,'New point added')
        self.assertContains(page,'Unsaved addition')
        draft.refresh_from_db(); self.assertEqual(draft.entries[0]['response'],'Updated response')

    def test_other_plan_cannot_replace_an_existing_draft(self):
        self.client.post(self.url(),self.data())
        draft=RevisionResponseDraft.objects.get()
        page=self.client.get(self.url(),{'plan':self.plan.pk})
        self.assertContains(page,'An existing draft was kept')
        draft.refresh_from_db()
        self.assertIsNone(draft.source_run_id)
        self.assertEqual(self.client.post(self.url(),self.data(draft_revision='1',source_run=str(self.plan.pk))).status_code,409)

    def test_current_owner_and_original_author_are_required(self):
        self.client.post(self.url(),self.data())
        draft=RevisionResponseDraft.objects.get()
        prefill=reverse('submission_revision',args=[self.submission.pk])
        for user in (self.editor,self.other):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url()).status_code,404)
            self.assertEqual(self.client.post(self.url(),self.data()).status_code,404)
        Submission.objects.filter(pk=self.submission.pk).update(author=self.other)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(self.url()).status_code,404)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(self.url()),'We clarified the study design.')
        self.assertEqual(self.client.get(prefill,{'response_draft':draft.pk,'revision':'1'}).status_code,404)
        Journal.objects.filter(pk=self.journal.pk).update(active=False)
        self.assertEqual(self.client.get(self.url()).status_code,404)

    def test_source_and_current_decision_integrity_are_enforced(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(self.url(),{'plan':str(self.plan.pk)}).status_code,404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(self.url(),{'plan':'bogus'}).status_code,404)
        RevisionPlanRun.objects.filter(pk=self.plan.pk).update(status='failed')
        self.assertEqual(self.client.get(self.url(),{'plan':str(self.plan.pk)}).status_code,404)
        RevisionPlanRun.objects.filter(pk=self.plan.pk).update(status='completed')
        other=Submission.objects.create(journal=self.journal,author=self.author,title='other',abstract='x',manuscript='x')
        other_version=SubmissionVersion.objects.create(submission=other,number=1,manuscript='x',uploaded_by=self.author)
        ReviewRound.objects.filter(pk=self.round.pk).update(version=other_version)
        self.assertEqual(self.client.get(self.url()).status_code,409)

    def test_add_delete_limits_validation_and_csrf(self):
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.url(),self.data()).status_code,403)
        self.assertEqual(self.client.put(self.url()).status_code,405)
        self.assertEqual(self.client.post(self.url(),self.data(entries=[{'comment':'','response':'Unfounded'}])).status_code,400)
        self.assertFalse(RevisionResponseDraft.objects.exists())
        self.assertEqual(self.client.post(self.url(),self.data(**{'point-TOTAL_FORMS':'11'})).status_code,400)
        entries=[{'comment':'First','response':'A'}, {'comment':'Second','response':'B'}]
        self.assertEqual(self.client.post(self.url(),self.data(entries=entries,**{'point-0-DELETE':'on'})).status_code,302)
        self.assertEqual(RevisionResponseDraft.objects.get().entries[0]['comment'],'Second')

    def test_escaping_and_combined_length_limit(self):
        self.client.post(self.url(),self.data(entries=[{'comment':'<script>alert(1)</script>', 'response':'Work done'}]))
        page=self.client.get(self.url())
        self.assertContains(page,'&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page,'<script>alert(1)</script>')
        self.assertIsNone(compose_response([{'comment':'x','response':'a'*5000,'location':''} for _ in range(5)]))
        self.assertIsNone(compose_response([{'comment':'x','response':'','location':''}]))
