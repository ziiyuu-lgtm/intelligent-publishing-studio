"""A complete, isolated acceptance journey using fictional records only."""
import tempfile

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import (EditorialAssignment, EditorialDecision, Journal, JournalIssue,
    LocalReviewMailJob, Membership, Organisation, ProductionAssignment, PublicationApproval,
    PublicationDraft, ReviewAssignment, ReviewNotice, ReviewRound, Submission, SubmissionEvent)


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_LOCAL_REVIEW_MAIL_ENABLED=False,
    EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend',
    PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class FictionalTwoRoundPublicationTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='ips-end-to-end-')
        self.addCleanup(directory.cleanup)
        media = override_settings(MEDIA_ROOT=directory.name)
        media.enable()
        self.addCleanup(media.disable)
        users = get_user_model().objects
        self.actors = {role: users.create_user('fictional-e2e-' + role, email=role + '@example.invalid') for role in
            ('author', 'editor', 'coordinator', 'reviewer1', 'reviewer2', 'producer', 'foreign')}
        org = Organisation.objects.create(name='Fictional end-to-end publisher', slug='e2e-publisher')
        self.journal = Journal.objects.create(organisation=org, name='Fictional end-to-end journal',
            slug='e2e-journal', review_mode=Journal.ReviewMode.SEPARATE)
        other_org = Organisation.objects.create(name='Another fictional publisher', slug='e2e-other-publisher')
        self.other_journal = Journal.objects.create(organisation=other_org, name='Other journal', slug='e2e-other')
        for role, membership_role in [('author', 'author'), ('editor', 'editor'),
                ('coordinator', 'coordinator'), ('reviewer1', 'reviewer'),
                ('reviewer2', 'reviewer'), ('producer', 'producer')]:
            Membership.objects.create(user=self.actors[role], journal=self.journal, role=membership_role)
        Membership.objects.create(user=self.actors['foreign'], journal=self.other_journal, role='editor')
        self.clients = {}
        for role, actor in self.actors.items():
            client = Client(enforce_csrf_checks=True)
            client.force_login(actor)
            self.assertEqual(client.get(reverse('workspace')).status_code, 200)
            self.assertIn('csrftoken', client.cookies)
            self.clients[role] = client

    def post(self, role, name, args, data, expected=302):
        client = self.clients[role]
        result = client.post(reverse(name, args=args), data, HTTP_X_CSRFTOKEN=client.cookies['csrftoken'].value)
        self.assertEqual(result.status_code, expected, msg=f'{role}: {name}')
        return result

    @staticmethod
    def pdf(name, data):
        return SimpleUploadedFile(name, data, content_type='application/pdf')

    def check_review_copy(self, submission):
        page = self.clients['editor'].get(reverse('submission_detail', args=[submission.pk]))
        self.assertEqual(page.status_code, 200)
        return self.post('editor', 'review_copy_screen', [submission.pk],
            {'confirmed':'yes', 'check_token':page.context['review_copy_check_token']})

    def confirmation_data(self, role, draft):
        page = self.clients[role].get(reverse('production_detail', args=[draft.pk]))
        self.assertEqual(page.status_code, 200)
        return {'revision':str(page.context['draft'].revision), 'proof':str(page.context['proof'].pk),
            'fingerprint':page.context['fingerprint'], 'confirm':'yes', 'synthetic':'yes'}

    def test_two_rounds_to_released_issue_with_private_review_copies(self):
        original = b'%PDF-1.4\nfictional full manuscript v1\n%%EOF\n'
        anonymous_v1 = b'%PDF-1.4\nfictional reviewer copy v1\n%%EOF\n'
        revised = b'%PDF-1.4\nfictional full manuscript v2\n%%EOF\n'
        anonymous_v2 = b'%PDF-1.4\nfictional reviewer copy v2\n%%EOF\n'
        proof = b'%PDF-1.4\nfictional final proof\n%%EOF\n'
        report1 = 'Fictional confidential first-round report'
        report2 = 'Fictional confidential second-round report'

        # The draft and its files are private until the author explicitly submits.
        self.post('author', 'submission_create', [self.journal.slug], {
            'title':'Fictional two-round manuscript', 'abstract':'Fictional original abstract.',
            'manuscript':self.pdf('full-v1.pdf', original),
            'review_copy':self.pdf('review-v1.pdf', anonymous_v1), 'review_copy_confirmed':'on'})
        submission = Submission.objects.get(author=self.actors['author'])
        detail = reverse('submission_detail', args=[submission.pk])
        self.assertEqual(self.clients['editor'].get(detail).status_code, 404)
        self.assertEqual(self.clients['foreign'].get(detail).status_code, 404)
        checklist = self.clients['author'].get(reverse('submission_checklist', args=[submission.pk]))
        self.assertTrue(checklist.context['ready'])
        submitted = {'review_token':checklist.context['review_token'], 'confirm':'yes'}
        self.assertEqual(self.clients['author'].post(reverse('submission_submit', args=[submission.pk]), submitted).status_code, 403)
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.DRAFT)
        self.post('author', 'submission_submit', [submission.pk], submitted)
        self.post('author', 'submission_submit', [submission.pk], submitted, expected=409)
        self.post('coordinator', 'submission_editors', [submission.pk], {'editor':self.actors['editor'].pk})
        self.assertEqual(EditorialAssignment.objects.filter(submission=submission, status='active').count(), 1)
        self.assertEqual(self.clients['foreign'].get(detail).status_code, 404)

        # Each round requires its own editorial file check and acceptance.
        self.post('editor', 'review_invite', [submission.pk], {'reviewer':self.actors['reviewer1'].pk}, expected=409)
        self.check_review_copy(submission)
        self.post('editor', 'review_invite', [submission.pk], {'reviewer':self.actors['reviewer1'].pk})
        self.post('editor', 'review_invite', [submission.pk], {'reviewer':self.actors['reviewer1'].pk})
        first = ReviewAssignment.objects.get(submission=submission, round__number=1, reviewer=self.actors['reviewer1'])
        first_file = reverse('review_file', args=[first.pk])
        self.assertEqual(self.clients['reviewer1'].get(first_file).status_code, 404)
        self.assertEqual(self.clients['reviewer2'].get(reverse('review_detail', args=[first.pk])).status_code, 404)
        self.post('reviewer1', 'review_respond', [first.pk], {'response':'accept', 'declaration':'none'})
        response = self.clients['reviewer1'].get(first_file)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), anonymous_v1)
        self.assertEqual(self.clients['reviewer1'].get(reverse('submission_version_file', args=[submission.pk, 1])).status_code, 404)
        self.post('reviewer1', 'review_report', [first.pk], {'recommendation':'major', 'comments':report1})
        self.post('editor', 'editorial_decision', [submission.pk],
            {'outcome':'major', 'author_message':'Fictional revision requested.'})
        author_page = self.clients['author'].get(detail)
        self.assertContains(author_page, 'Fictional revision requested.')
        self.assertNotContains(author_page, report1)
        self.assertNotContains(author_page, self.actors['reviewer1'].username)
        self.assertEqual(self.clients['reviewer1'].get(first_file).status_code, 404)

        revision = self.clients['author'].get(reverse('submission_revision', args=[submission.pk]))
        token = revision.context['revision_token']
        revision_data = {'revision_token':token, 'author_response':'Fictional response to the editor.',
            'manuscript':self.pdf('full-v2.pdf', revised), 'review_copy':self.pdf('review-v2.pdf', anonymous_v2),
            'review_copy_confirmed':'on'}
        self.post('author', 'submission_revision', [submission.pk], revision_data)
        self.post('author', 'submission_revision', [submission.pk], {'revision_token':token}, expected=409)
        self.assertEqual(list(submission.versions.values_list('number', flat=True)), [1, 2])
        self.post('editor', 'review_invite', [submission.pk], {'reviewer':self.actors['reviewer2'].pk}, expected=409)
        self.check_review_copy(submission)
        self.post('editor', 'review_invite', [submission.pk], {'reviewer':self.actors['reviewer2'].pk})
        second = ReviewAssignment.objects.get(submission=submission, round__number=2, reviewer=self.actors['reviewer2'])
        self.assertEqual(second.round.version.number, 2)
        self.assertEqual(self.clients['reviewer1'].get(reverse('review_detail', args=[second.pk])).status_code, 404)
        self.post('reviewer2', 'review_respond', [second.pk], {'response':'accept', 'declaration':'none'})
        response = self.clients['reviewer2'].get(reverse('review_file', args=[second.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), anonymous_v2)
        self.post('reviewer2', 'review_report', [second.pk], {'recommendation':'accept', 'comments':report2})
        self.post('editor', 'editorial_decision', [submission.pk],
            {'outcome':'accept', 'author_message':'Fictional acceptance after revision.'})
        submission.refresh_from_db()
        self.assertEqual(submission.status, Submission.Status.ACCEPTED)
        self.assertEqual(list(submission.rounds.values_list('status', flat=True)),
            [ReviewRound.Status.CLOSED, ReviewRound.Status.CLOSED])
        self.assertEqual(EditorialDecision.objects.filter(submission=submission).count(), 2)
        for number, data in [(1, original), (2, revised)]:
            response = self.clients['author'].get(reverse('submission_version_file', args=[submission.pk, number]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b''.join(response.streaming_content), data)

        # Production, approval and publication are separate authorised actions.
        self.post('editor', 'start_production', [submission.pk], {'synthetic':'yes'})
        draft = PublicationDraft.objects.get(submission=submission)
        self.assertEqual(draft.decision.round.version.number, 2)
        self.post('coordinator', 'production_team', [submission.pk], {'producer':self.actors['producer'].pk})
        self.assertTrue(ProductionAssignment.objects.filter(draft=draft, producer=self.actors['producer'], status='active').exists())
        self.assertEqual(self.clients['reviewer1'].get(reverse('production_detail', args=[draft.pk])).status_code, 404)
        producer_page = self.clients['producer'].get(reverse('production_detail', args=[draft.pk]))
        for private in (report1, report2, self.actors['reviewer1'].username, self.actors['reviewer2'].username):
            self.assertNotContains(producer_page, private)
        draft.refresh_from_db()
        self.post('producer', 'edit_production_metadata', [draft.pk], {
            'revision':str(draft.revision), 'title':'Fictional public title', 'authors':'Fictional Author',
            'abstract':'Fictional public abstract.'})
        draft.refresh_from_db()
        self.post('producer', 'upload_publication_proof', [draft.pk],
            {'revision':str(draft.revision), 'synthetic':'on', 'file':self.pdf('proof.pdf', proof)})
        self.post('coordinator', 'manage_test_issues', [self.journal.slug],
            {'volume':1, 'number':7, 'year':2026, 'title':'Fictional test issue', 'synthetic':'on'})
        issue = JournalIssue.objects.get(journal=self.journal, volume=1, number=7)
        draft.refresh_from_db()
        self.post('producer', 'set_production_issue', [draft.pk], {'revision':str(draft.revision), 'issue':str(issue.pk)})
        public = Client()
        article_url = reverse('test_article', args=[self.journal.slug, draft.pk])
        issue_url = reverse('test_issue', args=[self.journal.slug, issue.pk])
        self.assertEqual(public.get(article_url).status_code, 404)
        self.post('producer', 'publish_test_article', [draft.pk], self.confirmation_data('producer', draft), expected=409)
        author_confirmation = self.confirmation_data('author', draft)
        self.post('author', 'approve_publication_proof', [draft.pk], author_confirmation)
        self.post('author', 'approve_publication_proof', [draft.pk], author_confirmation)
        self.assertEqual(PublicationApproval.objects.filter(draft=draft).count(), 1)
        self.post('producer', 'publish_test_article', [draft.pk], self.confirmation_data('producer', draft))
        self.assertEqual(public.get(issue_url).status_code, 404)
        review = self.clients['coordinator'].get(reverse('review_test_issue_release', args=[issue.pk]))
        self.assertEqual(review.status_code, 200)
        self.assertContains(review, 'Fictional public title')
        self.assertNotContains(review, report1)
        issue.refresh_from_db()
        self.assertIsNone(issue.released_at)
        release = {'release_token':review.context['release_token'], 'confirm':'yes', 'synthetic':'yes'}
        self.post('coordinator', 'release_test_issue', [issue.pk], release)
        self.post('coordinator', 'release_test_issue', [issue.pk], release, expected=409)

        # Public pages contain only publication metadata; review reports remain private.
        for url in (article_url, issue_url):
            page = public.get(url)
            self.assertContains(page, 'Fictional public title')
            for private in (report1, report2, self.actors['reviewer1'].username, self.actors['reviewer2'].username):
                self.assertNotContains(page, private)
        article = public.get(article_url)
        self.assertContains(article, 'Fictional Author')
        self.assertContains(article, 'Fictional public abstract.')
        self.assertEqual(public.get(reverse('test_article', args=[self.other_journal.slug, draft.pk])).status_code, 404)
        response = public.get(reverse('test_article_pdf', args=[self.journal.slug, draft.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b''.join(response.streaming_content), proof)
        self.assertEqual(ReviewNotice.objects.filter(assignment__submission=submission, kind='invitation').count(), 2)
        self.assertFalse(LocalReviewMailJob.objects.exists())
        self.assertEqual(getattr(mail, 'outbox', []), [])
        self.assertEqual(SubmissionEvent.objects.filter(submission=submission, action='review_invited').count(), 2)
        self.assertEqual(SubmissionEvent.objects.filter(submission=submission, action='test_article_published').count(), 1)
