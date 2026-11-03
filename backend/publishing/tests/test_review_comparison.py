import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import translation

from publishing.models import EditorialAssignment, Membership, ReviewAssignment, ReviewRound, Submission, SubmissionVersion
from publishing.tests import test_workflow


@override_settings(PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class ReviewComparisonTests(TestCase):
    create_draft = test_workflow.WorkflowTests.create_draft
    submit = test_workflow.WorkflowTests.submit
    invite = test_workflow.WorkflowTests.invite

    def setUp(self):
        test_workflow.WorkflowTests.setUp(self)
        directory = tempfile.TemporaryDirectory(prefix='ips-comparison-')
        self.addCleanup(directory.cleanup)
        setting = override_settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.submission = self.create_draft()
        self.submit(self.submission)
        self.assignment = self.invite(self.submission)
        self.url = reverse('review_comparison', args=[self.submission.pk])

    def report(self, assignment, comments, conflict='none', recommendation='major'):
        assignment.status = ReviewAssignment.Status.SUBMITTED
        assignment.conflict = conflict
        assignment.recommendation = recommendation
        assignment.comments = comments
        assignment.save()

    def add_reviewer(self, name, status='submitted', conflict='none', comments=''):
        user = get_user_model().objects.create_user(name)
        Membership.objects.create(user=user, journal=self.journal, role=Membership.Role.REVIEWER)
        return ReviewAssignment.objects.create(submission=self.submission, round=self.assignment.round,
            reviewer=user, invited_by=self.editor, status=status, conflict=conflict,
            recommendation='minor', comments=comments)

    def test_comparison_filters_declared_conflicts_and_escapes_comments(self):
        self.report(self.assignment, 'First fictional report <script>alert(1)</script>')
        self.add_reviewer('second', comments='Second fictional report')
        self.add_reviewer('conflicted', conflict='yes', comments='Conflicted confidential report')
        self.add_reviewer('undeclared', conflict='unknown', comments='Undeclared confidential report')
        self.add_reviewer('pending', status='accepted')
        self.add_reviewer('invited', status='invited', conflict='unknown')
        self.client.force_login(self.editor)
        response = self.client.get(self.url)
        self.assertEqual(response.context['counts'], {'reports':2, 'accepted':1, 'invited':1, 'flagged':1, 'undeclared':1})
        self.assertContains(response, 'First fictional report')
        self.assertContains(response, 'Second fictional report')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        for secret in ('Conflicted confidential report', 'Undeclared confidential report', 'conflicted', 'undeclared'):
            self.assertNotContains(response, secret)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(self.client.get(reverse('submission_detail', args=[self.submission.pk])), self.url)

    def test_author_reviewers_coordinator_and_unassigned_editors_cannot_access(self):
        self.report(self.assignment, 'Private reviewer text')
        for user in (self.author, self.reviewer, self.coordinator, self.other_editor, self.superuser):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url).status_code, 404)
        self.client.force_login(self.author)
        self.assertNotContains(self.client.get(reverse('submission_detail', args=[self.submission.pk])), self.url)
        self.client.force_login(self.editor)
        membership = Membership.objects.get(user=self.editor, journal=self.journal, role='editor')
        membership.active = False
        membership.save()
        self.assertEqual(self.client.get(self.url).status_code, 404)
        membership.active = True
        membership.save()
        EditorialAssignment.objects.filter(submission=self.submission, editor=self.editor).update(status='recused')
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_round_selection_is_scoped_and_view_is_read_only(self):
        self.report(self.assignment, 'Earlier report')
        old = self.assignment.round
        old.status = ReviewRound.Status.CLOSED
        old.save()
        version = SubmissionVersion.objects.create(submission=self.submission, number=2, manuscript='fictional.pdf', uploaded_by=self.author)
        current = ReviewRound.objects.create(submission=self.submission, version=version, number=2)
        other = get_user_model().objects.create_user('new-reviewer')
        ReviewAssignment.objects.create(submission=self.submission, round=current, reviewer=other, invited_by=self.editor,
            status='submitted', conflict='none', comments='Newer report', recommendation='minor')
        self.client.force_login(self.editor)
        before = list(ReviewAssignment.objects.values())
        self.assertContains(self.client.get(self.url), 'Newer report')
        self.assertNotContains(self.client.get(self.url), 'Earlier report')
        old_response = self.client.get(self.url, {'round':'1'})
        self.assertContains(old_response, 'Earlier report')
        self.assertNotContains(old_response, 'Newer report')
        for bad in ('3', '-1', '0', 'x', '９', '1000000000'):
            self.assertEqual(self.client.get(self.url, {'round':bad}).status_code, 404)
        self.assertEqual(self.client.get(self.url + '?round=1&round=2').status_code, 404)
        self.assertEqual(self.client.post(self.url).status_code, 405)
        self.assertEqual(list(ReviewAssignment.objects.values()), before)

    def test_inconsistent_cross_submission_round_and_assignment_are_excluded(self):
        self.report(self.assignment, 'Valid report')
        other = Submission.objects.create(journal=self.journal, author=self.author, title='Other manuscript', abstract='Fictional')
        foreign_version = SubmissionVersion.objects.create(submission=other, number=1, manuscript='other.pdf', uploaded_by=self.author)
        ReviewRound.objects.create(submission=self.submission, version=foreign_version, number=2, status='closed')
        stranger = get_user_model().objects.create_user('cross-submission-reviewer')
        ReviewAssignment.objects.create(submission=other, round=self.assignment.round, reviewer=stranger,
            invited_by=self.editor, status='submitted', conflict='none', comments='Foreign private report')
        self.client.force_login(self.editor)
        response = self.client.get(self.url)
        self.assertContains(response, 'Valid report')
        self.assertNotContains(response, 'Foreign private report')
        self.assertEqual(self.client.get(self.url, {'round':2}).status_code, 404)

    def test_simplified_chinese(self):
        self.client.force_login(self.editor)
        with translation.override('zh-hans'):
            response = self.client.get(self.url, HTTP_ACCEPT_LANGUAGE='zh-hans')
        self.assertContains(response, '比较审稿报告')
