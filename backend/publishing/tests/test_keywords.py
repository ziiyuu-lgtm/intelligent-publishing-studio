import hashlib
import json
import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse

from publishing.keywords import validate_keywords
from publishing.models import PublicationDraft, ReviewAssignment, Submission, SubmissionVersion
from publishing.tests import test_production as production_tests, test_workflow as workflow


def pdf(name='test.pdf'):
    return SimpleUploadedFile(name, workflow.PDF, content_type='application/pdf')


@override_settings(DEBUG=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class KeywordWorkflowTests(TestCase):
    setUp = workflow.WorkflowTests.setUp
    submit = workflow.WorkflowTests.submit
    invite = workflow.WorkflowTests.invite
    finish_review = workflow.WorkflowTests.finish_review
    decide = workflow.WorkflowTests.decide

    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix='ips-keywords-')
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def draft(self, keywords='Responsible AI\nData governance'):
        self.client.force_login(self.author)
        result = self.client.post(reverse('submission_create',args=[self.journal.slug]), {
            'title':'Fictional research', 'abstract':'Fictional abstract.', 'keywords':keywords, 'manuscript':pdf()})
        self.assertEqual(result.status_code,302)
        return Submission.objects.get()

    def test_chinese_interface_and_multilingual_terms(self):
        draft=self.draft('科研诚信\nArtificial intelligence\n研究治理')
        self.client.post(reverse('set_language'), {'language':'zh-hans','next':'/'})
        page=self.client.get(reverse('submission_checklist',args=[draft.pk]))
        self.assertContains(page,'关键词');self.assertContains(page,'科研诚信')
        edit=self.client.get(reverse('edit_draft',args=[draft.pk]))
        self.assertContains(edit,'每行一个关键词')

    def test_author_create_edit_and_checklist_require_current_keyword_snapshot(self):
        draft=self.draft('Responsible AI\n学术出版\nData governance')
        version=draft.versions.get()
        self.assertEqual(draft.keywords_config,['Responsible AI','学术出版','Data governance'])
        self.assertEqual(version.keywords_snapshot,draft.keywords_config)
        page=self.client.get(reverse('submission_checklist',args=[draft.pk]))
        self.assertContains(page,'学术出版')
        old=page.context['review_token']
        edit=self.client.get(reverse('edit_draft',args=[draft.pk]))
        result=self.client.post(reverse('edit_draft',args=[draft.pk]), {
            'edit_token':edit.context['edit_token'], 'title':draft.title, 'abstract':draft.abstract,
            'keywords':'Research integrity\nHuman oversight'})
        self.assertEqual(result.status_code,302)
        draft.refresh_from_db();version.refresh_from_db()
        self.assertEqual(draft.keywords_config,['Research integrity','Human oversight'])
        self.assertEqual(version.keywords_snapshot,draft.keywords_config)
        stale=self.client.post(reverse('submission_submit',args=[draft.pk]), {'review_token':old,'confirm':'yes'})
        self.assertEqual(stale.status_code,409)
        self.assertContains(stale,'Research integrity',status_code=409)
        self.assertEqual(self.client.post(reverse('submission_submit',args=[draft.pk]),
            {'review_token':stale.context['review_token'],'confirm':'yes'}).status_code,302)

    def test_validation_rejects_duplicates_too_many_and_long_values(self):
        self.client.force_login(self.author);url=reverse('submission_create',args=[self.journal.slug])
        for text in ['AI\nai', 'Café\nCafe\u0301', '\n'.join(f'keyword {i}' for i in range(11)), 'a'*101]:
            with self.subTest(text=text):
                result=self.client.post(url, {'title':'Fictional','abstract':'Fictional', 'keywords':text,'manuscript':pdf()})
                self.assertEqual(result.status_code,200)
                self.assertIn('keywords',result.context['form'].errors)
        self.assertFalse(Submission.objects.exists())
        for bad in [{'bad':'data'},['ok',42],['duplicated','Duplicated'],['two\nlines']]:
            with self.assertRaises(ValueError):validate_keywords(bad)

    def test_revisions_and_reviewers_see_only_their_recorded_version(self):
        draft=self.draft('Initial topic\nFirst method');self.submit(draft)
        first=self.invite(draft);self.finish_review(first)
        self.assertEqual(self.decide(draft).status_code,302)
        self.client.force_login(self.author);url=reverse('submission_revision',args=[draft.pk]);page=self.client.get(url)
        result=self.client.post(url, {'revision_token':page.context['revision_token'],'manuscript':pdf('revision.pdf'),
            'author_response':'We revised the fictional paper.', 'revised_title':'Fictional research',
            'revised_abstract':'Fictional abstract.', 'revised_keywords':'Updated topic\nSecond method'})
        self.assertEqual(result.status_code,302)
        draft.refresh_from_db();self.assertEqual(draft.keywords_config,['Updated topic','Second method'])
        self.assertEqual(draft.versions.get(number=1).keywords_snapshot,['Initial topic','First method'])
        self.assertEqual(draft.versions.get(number=2).keywords_snapshot,['Updated topic','Second method'])
        comparison=self.client.get(reverse('version_metadata_compare',args=[draft.pk]))
        self.assertEqual(comparison.context['keywords_state'],'changed')
        self.assertContains(comparison,'Initial topic');self.assertContains(comparison,'Updated topic')
        self.client.force_login(self.reviewer)
        prior=self.client.get(reverse('review_detail',args=[first.pk]))
        self.assertContains(prior,'Initial topic');self.assertNotContains(prior,'Updated topic')
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse('review_invite',args=[draft.pk]),{'reviewer':self.reviewer.pk}).status_code,302)
        second=ReviewAssignment.objects.get(submission=draft,reviewer=self.reviewer,round__number=2)
        self.client.force_login(self.reviewer);current=self.client.get(reverse('review_detail',args=[second.pk]))
        self.assertContains(current,'Updated topic');self.assertNotContains(current,'Initial topic')

    def test_legacy_unknown_keywords_are_not_invented(self):
        draft=self.draft();Submission.objects.filter(pk=draft.pk).update(keywords_config=None)
        SubmissionVersion.objects.filter(submission=draft).update(keywords_snapshot=None)
        page=self.client.get(reverse('submission_checklist',args=[draft.pk]))
        self.assertIsNone(page.context['keywords']);self.assertContains(page,'Not recorded or unavailable')
        self.assertEqual(self.client.post(reverse('submission_submit',args=[draft.pk]),
            {'review_token':page.context['review_token'],'confirm':'yes'}).status_code,302)
        draft.refresh_from_db();self.assertIsNone(draft.keywords_config)
        self.assertIsNone(draft.versions.get().keywords_snapshot)

    def test_legacy_unknown_stays_unknown_when_editing_other_fields(self):
        draft=self.draft();Submission.objects.filter(pk=draft.pk).update(keywords_config=None)
        SubmissionVersion.objects.filter(submission=draft).update(keywords_snapshot=None)
        draft.refresh_from_db();page=self.client.get(reverse('edit_draft',args=[draft.pk]))
        result=self.client.post(reverse('edit_draft',args=[draft.pk]), {'edit_token':page.context['edit_token'],
            'title':'Edited legacy draft','abstract':draft.abstract,'keywords':''})
        self.assertEqual(result.status_code,302)
        draft.refresh_from_db();self.assertIsNone(draft.keywords_config);self.assertIsNone(draft.versions.get().keywords_snapshot)

    def test_revision_omitting_keyword_field_preserves_current_values(self):
        draft=self.draft('Keep this topic\nExisting method');self.submit(draft)
        first=self.invite(draft);self.finish_review(first);self.assertEqual(self.decide(draft).status_code,302)
        self.client.force_login(self.author);url=reverse('submission_revision',args=[draft.pk]);page=self.client.get(url)
        result=self.client.post(url, {'revision_token':page.context['revision_token'], 'manuscript':pdf('revision.pdf'),
            'author_response':'Fictional revision.'})
        self.assertEqual(result.status_code,302)
        draft.refresh_from_db();self.assertEqual(draft.keywords_config,['Keep this topic','Existing method'])
        self.assertEqual(draft.versions.get(number=2).keywords_snapshot,draft.keywords_config)

    def test_keywords_changed_after_revision_review_preserve_entered_text(self):
        draft=self.draft('Before');self.submit(draft);first=self.invite(draft);self.finish_review(first)
        self.assertEqual(self.decide(draft).status_code,302)
        self.client.force_login(self.author);url=reverse('submission_revision',args=[draft.pk]);page=self.client.get(url)
        Submission.objects.filter(pk=draft.pk).update(keywords_config=['Changed concurrently'])
        result=self.client.post(url, {'revision_token':page.context['revision_token'], 'manuscript':pdf('revision.pdf'),
            'author_response':'Fictional revision.', 'revised_keywords':'Unsaved topic'})
        self.assertEqual(result.status_code,409);self.assertContains(result,'Unsaved topic',status_code=409)
        self.assertEqual(draft.versions.count(),1)

    def test_invalid_or_changed_data_blocks_submission_and_escaped_keywords(self):
        draft=self.draft('<script>alert(1)</script>\nGovernance')
        page=self.client.get(reverse('submission_checklist',args=[draft.pk]))
        self.assertContains(page,'&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(page,'<script>alert(1)</script>')
        token=page.context['review_token']
        Submission.objects.filter(pk=draft.pk).update(keywords_config=['Changed'])
        result=self.client.post(reverse('submission_submit',args=[draft.pk]), {'review_token':token,'confirm':'yes'})
        self.assertEqual(result.status_code,409);self.assertIn('keywords_changed',result.context['errors'])
        Submission.objects.filter(pk=draft.pk).update(keywords_config={'invalid':True})
        result=self.client.get(reverse('submission_checklist',args=[draft.pk]))
        self.assertIn('keywords_invalid',result.context['errors']);self.assertFalse(result.context['ready'])


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class KeywordProductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        production_tests.ProductionTests.setUpTestData.__func__(cls)

    @classmethod
    def setUpClass(cls):
        cls.media_dir=tempfile.mkdtemp(prefix='ips-keywords-production-')
        cls.media_override=override_settings(MEDIA_ROOT=cls.media_dir);cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass();cls.media_override.disable();shutil.rmtree(cls.media_dir,ignore_errors=True)

    start=production_tests.ProductionTests.start
    edit=production_tests.ProductionTests.edit
    upload=production_tests.ProductionTests.upload
    confirmation_data=production_tests.ProductionTests.confirmation_data
    approve=production_tests.ProductionTests.approve
    publish=production_tests.ProductionTests.publish

    def test_accepted_version_keywords_reach_publication_and_change_requires_new_approval(self):
        self.version.keywords_snapshot=['Accepted keyword','方法'];self.version.save(update_fields=['keywords_snapshot'])
        self.submission.keywords_config=['Different current value'];self.submission.save(update_fields=['keywords_config'])
        draft=self.start();self.assertEqual(draft.keywords_config,['Accepted keyword','方法'])
        self.assertEqual(self.edit(draft).status_code,302)
        draft.refresh_from_db();self.assertEqual(draft.keywords_config,['Accepted keyword','方法'])
        self.assertEqual(self.upload(draft).status_code,302)
        self.assertEqual(self.approve(draft).status_code,302)
        self.assertEqual(self.edit(draft,keywords='Final keyword\n研究治理').status_code,302)
        self.assertEqual(self.publish(draft).status_code,409)
        self.assertEqual(self.approve(draft).status_code,302)
        self.assertEqual(self.publish(draft).status_code,302)
        public=self.client.get(reverse('test_article',args=[self.journal.slug,draft.pk]))
        self.assertContains(public,'Final keyword');self.assertContains(public,'研究治理')
        self.assertNotContains(public,'Different current value')

    def test_invalid_public_keywords_block_author_confirmation(self):
        draft=self.start();self.assertEqual(self.edit(draft).status_code,302);self.assertEqual(self.upload(draft).status_code,302)
        PublicationDraft.objects.filter(pk=draft.pk).update(keywords_config=['Duplicate','duplicate'])
        data=self.confirmation_data(draft,self.author)
        self.assertEqual(self.client.post(reverse('approve_publication_proof',args=[draft.pk]),data).status_code,409)

    def test_legacy_empty_metadata_does_not_change_existing_fingerprint(self):
        from publishing.production_views import _fingerprint
        draft=self.start();self.assertIsNone(draft.keywords_config)
        self.assertEqual(self.edit(draft).status_code,302);self.assertEqual(self.upload(draft).status_code,302)
        draft.refresh_from_db();proof=draft.proofs.first()
        payload={'draft':str(draft.pk),'revision':draft.revision,'title':draft.title,'authors':draft.authors,
                 'abstract':draft.abstract,'proof':proof.pk,'sha256':proof.sha256}
        legacy=hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        self.assertEqual(_fingerprint(draft,proof),legacy)
