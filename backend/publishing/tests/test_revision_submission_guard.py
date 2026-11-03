import tempfile
import time
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import EditorialDecision, Journal, Organisation, ReviewRound, Submission, SubmissionVersion

PDF=b'%PDF-1.4\n% fictional revision\n%%EOF\n'


@override_settings(DEBUG=True,PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class RevisionSubmissionGuardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users=get_user_model().objects
        cls.author=users.create_user('revision-author')
        cls.editor=users.create_user('revision-editor')
        cls.other=users.create_user('revision-other')
        org=Organisation.objects.create(name='Fictional publisher',slug='revguard-publisher')
        cls.journal=Journal.objects.create(organisation=org,name='Fictional journal',slug='revguard-journal',review_mode='direct')
        cls.submission=Submission.objects.create(journal=cls.journal,author=cls.author,title='Fictional manuscript',
            abstract='Fictional',manuscript='original.pdf',status='revision_requested',review_mode='direct')
        cls.version=SubmissionVersion.objects.create(submission=cls.submission,number=1,manuscript='original.pdf',uploaded_by=cls.author)
        cls.round=ReviewRound.objects.create(submission=cls.submission,version=cls.version,number=1,status='closed')
        cls.decision=EditorialDecision.objects.create(submission=cls.submission,round=cls.round,outcome='major',
            author_message='Private decision for author',decided_by=cls.editor)

    def setUp(self):
        self.media=tempfile.TemporaryDirectory(prefix='ips-revision-guard-')
        self.addCleanup(self.media.cleanup)
        setting=override_settings(MEDIA_ROOT=self.media.name);setting.enable();self.addCleanup(setting.disable)
        self.client.force_login(self.author)

    def url(self): return reverse('submission_revision',args=[self.submission.pk])
    def token(self):
        page=self.client.get(self.url())
        self.assertEqual(page.status_code,200)
        return page.context['revision_token']
    def submit(self, token, response='Fictional author response'):
        return self.client.post(self.url(),{'revision_token':token,'author_response':response,
            'manuscript':SimpleUploadedFile('revision.pdf',PDF,content_type='application/pdf')})

    def test_current_form_shows_revision_details_and_submits_explicitly(self):
        page=self.client.get(self.url())
        self.assertContains(page,'Files and response')
        self.assertContains(page,'Next review round')
        self.assertEqual(page.context['next_version'],2)
        self.assertEqual(page.context['next_round'],2)
        self.assertContains(self.client.get(self.url(),HTTP_ACCEPT_LANGUAGE='zh-hans'),'文件与回复')
        token=page.context['revision_token']
        self.assertEqual(SubmissionVersion.objects.filter(submission=self.submission).count(),1)
        self.assertEqual(self.submit(token).status_code,302)
        self.submission.refresh_from_db()
        self.assertEqual(self.submission.status,'submitted')
        self.assertEqual(SubmissionVersion.objects.get(submission=self.submission,number=2).author_response,'Fictional author response')
        self.assertEqual(ReviewRound.objects.get(submission=self.submission,number=2).status,'open')

    def test_old_form_cannot_submit_to_a_later_revision_round(self):
        old=self.token()
        version2=SubmissionVersion.objects.create(submission=self.submission,number=2,manuscript='v2.pdf',uploaded_by=self.author)
        round2=ReviewRound.objects.create(submission=self.submission,version=version2,number=2,status='closed')
        EditorialDecision.objects.create(submission=self.submission,round=round2,outcome='minor',author_message='New decision',decided_by=self.editor)
        result=self.submit(old,response='<script>Keep my response</script>')
        self.assertEqual(result.status_code,409)
        self.assertContains(result,'&lt;script&gt;Keep my response&lt;/script&gt;',status_code=409)
        self.assertNotContains(result,'<script>Keep my response</script>',status_code=409)
        self.assertContains(result,'This attempt was not submitted',status_code=409)
        self.assertFalse(SubmissionVersion.objects.filter(submission=self.submission,number=3).exists())
        self.assertEqual(self.submit(self.token()).status_code,302)
        self.assertTrue(SubmissionVersion.objects.filter(submission=self.submission,number=3).exists())

    def test_replayed_tampered_or_missing_token_preserves_response(self):
        token=self.token()
        for bad in ('',token+'x','not-signed'):
            page=self.submit(bad,response='Please preserve this text')
            self.assertContains(page,'Please preserve this text',status_code=409)
        self.assertEqual(SubmissionVersion.objects.filter(submission=self.submission).count(),1)
        self.assertEqual(self.submit(token).status_code,302)
        page=self.submit(token,response='Second attempt')
        self.assertContains(page,'Second attempt',status_code=409)
        self.assertEqual(SubmissionVersion.objects.filter(submission=self.submission).count(),2)

    def test_expired_form_preserves_response(self):
        with patch('django.core.signing.time.time',return_value=time.time()-8*24*60*60):
            old=self.token()
        page=self.submit(old,response='Response to retain')
        self.assertContains(page,'Response to retain',status_code=409)
        self.assertEqual(SubmissionVersion.objects.filter(submission=self.submission).count(),1)

    def test_owner_or_file_policy_change_invalidates_form(self):
        token=self.token()
        Submission.objects.filter(pk=self.submission.pk).update(review_mode='separate')
        self.assertEqual(self.submit(token).status_code,409)
        Submission.objects.filter(pk=self.submission.pk).update(review_mode='direct',author=self.other)
        self.client.force_login(self.other)
        self.assertEqual(self.submit(token).status_code,409)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(self.url()).status_code,404)

    def test_recheck_inside_transaction_blocks_changed_decision(self):
        token=self.token()
        with patch('publishing.revision_submission.current_revision_decision',side_effect=[self.decision,None]):
            result=self.submit(token,response='Text to copy')
        self.assertContains(result,'Text to copy',status_code=409)
        self.assertEqual(SubmissionVersion.objects.filter(submission=self.submission).count(),1)

    def test_changed_status_and_csrf_are_enforced(self):
        token=self.token()
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.author)
        self.assertEqual(csrf.post(self.url(),{'revision_token':token}).status_code,403)
        Submission.objects.filter(pk=self.submission.pk).update(status='under_review')
        result=self.submit(token,response='Unsaved response')
        self.assertContains(result,'Unsaved response',status_code=409)
        self.assertEqual(self.client.get(self.url()).status_code,409)
