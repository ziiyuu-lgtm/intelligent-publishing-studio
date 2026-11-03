from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta

from publishing.models import (EditorialAssignment, EditorialDecision, Journal, Membership, Organisation,
                               ReviewAssignment, ReviewRound, Submission, SubmissionEvent, SubmissionVersion)


class ReturnedRevisionsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.author = User.objects.create_user('writer')
        self.editor = User.objects.create_user('assigned-editor')
        self.other = User.objects.create_user('unassigned-editor')
        self.reviewer = User.objects.create_user('private-reviewer-identity')
        self.organisation = Organisation.objects.create(name='Fictional Group', slug='fictional')
        self.journal = Journal.objects.create(organisation=self.organisation, name='Test Journal', slug='test')
        for user, role in [(self.editor, Membership.Role.EDITOR), (self.other, Membership.Role.EDITOR),
                           (self.reviewer, Membership.Role.REVIEWER), (self.author, Membership.Role.AUTHOR)]:
            Membership.objects.create(user=user, journal=self.journal, role=role)
        self.sub = self.create_revision('Synthetic climate manuscript')
        self.client.force_login(self.editor)

    def create_revision(self, title, status=Submission.Status.SUBMITTED):
        sub = Submission.objects.create(journal=self.journal, author=self.author, title=title, abstract='test',
                                        manuscript='fake/manuscript.pdf', status=status)
        EditorialAssignment.objects.create(submission=sub, editor=self.editor, assigned_by=self.editor)
        first = SubmissionVersion.objects.create(submission=sub, number=1, manuscript='fake/first.pdf', uploaded_by=self.author)
        round1 = ReviewRound.objects.create(submission=sub, version=first, number=1, status=ReviewRound.Status.CLOSED)
        EditorialDecision.objects.create(submission=sub, round=round1, outcome=EditorialDecision.Outcome.MAJOR,
                                         author_message='Please clarify the method.', decided_by=self.editor)
        second = SubmissionVersion.objects.create(submission=sub, number=2, manuscript='fake/second.pdf',
                                                  author_response='We clarified the method.', uploaded_by=self.author)
        ReviewRound.objects.create(submission=sub, version=second, number=2)
        return sub

    def detail(self, sub=None):
        return reverse('returned_revision', args=[(sub or self.sub).pk])

    def test_dashboard_and_package_show_current_record_only(self):
        round1 = self.sub.rounds.get(number=1)
        ReviewAssignment.objects.create(submission=self.sub, round=round1, reviewer=self.reviewer, invited_by=self.editor,
                                        status=ReviewAssignment.Status.SUBMITTED, comments='secret-review-report')
        page = self.client.get(reverse('returned_revisions'))
        self.assertContains(page, self.sub.title)
        self.assertContains(page, 'Current revisions')
        self.assertEqual(page.context['counts']['all'], 1)
        response = self.client.get(self.detail())
        for text in ['Please clarify the method.', 'We clarified the method.', 'Major revision', 'Version 1', 'Version 2']:
            self.assertContains(response, text)
        self.assertNotContains(response, 'secret-review-report')
        self.assertNotContains(response, 'private-reviewer-identity')
        self.assertContains(response, reverse('submission_version_file', args=[self.sub.pk, 1]))
        self.assertContains(response, reverse('submission_version_file', args=[self.sub.pk, 2]))
        self.assertEqual(response['Cache-Control'].find('no-store') >= 0, True)

    def test_only_assigned_active_editor_can_access(self):
        for user in [self.author, self.other, self.reviewer]:
            self.client.force_login(user)
            self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)
            self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.client.force_login(self.editor)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=True)
        EditorialAssignment.objects.filter(submission=self.sub, editor=self.editor).update(status=EditorialAssignment.Status.RECUSED)
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)

    def test_reviewer_conflict_and_inactive_journal_block_access(self):
        ReviewAssignment.objects.create(submission=self.sub, round=self.sub.rounds.get(number=1),
                                        reviewer=self.editor, invited_by=self.other)
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)
        ReviewAssignment.objects.filter(reviewer=self.editor).delete()
        self.organisation.active = False
        self.organisation.save(update_fields=['active'])
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)

    def test_inconsistent_prior_decision_or_newer_version_is_excluded(self):
        decision = self.sub.decisions.get()
        decision.outcome = EditorialDecision.Outcome.ACCEPT
        decision.save(update_fields=['outcome'])
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        decision.outcome = EditorialDecision.Outcome.MAJOR
        decision.save(update_fields=['outcome'])
        SubmissionVersion.objects.create(submission=self.sub, number=3, manuscript='fake/new.pdf', uploaded_by=self.author)
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)

    def test_cross_submission_round_version_is_excluded(self):
        other = self.create_revision('Different manuscript')
        prior = self.sub.rounds.get(number=1)
        prior.version = other.versions.get(number=1)
        prior.save(update_fields=['version'])
        self.assertEqual(self.client.get(self.detail()).status_code, 404)
        self.assertNotContains(self.client.get(reverse('returned_revisions')), self.sub.title)
        self.assertContains(self.client.get(reverse('returned_revisions')), other.title)

    def test_filters_search_pagination_and_safe_text(self):
        self.create_revision('A second manuscript', status=Submission.Status.UNDER_REVIEW)
        for i in range(12):
            self.create_revision('Fictional paper %02d' % i)
        response = self.client.get(reverse('returned_revisions'), {'filter': 'under_review', 'q': 'second'})
        self.assertContains(response, 'A second manuscript')
        self.assertEqual(response.context['counts']['all'], 14)
        self.assertEqual(response.context['counts']['under_review'], 1)
        self.assertNotContains(response, self.sub.title)
        self.assertTrue(self.client.get(reverse('returned_revisions')).context['page'].has_next())
        version = self.sub.versions.get(number=2)
        version.author_response = '<script>unsafe()</script>'
        version.save(update_fields=['author_response'])
        response = self.client.get(self.detail())
        self.assertContains(response, '&lt;script&gt;unsafe()&lt;/script&gt;', html=False)
        self.assertNotContains(response, '<script>unsafe()</script>')

    def test_read_only_and_utc_rendering(self):
        version = self.sub.versions.get(number=2)
        SubmissionVersion.objects.filter(pk=version.pk).update(created_at=timezone.now() - timedelta(hours=3))
        before = SubmissionEvent.objects.count()
        self.assertEqual(self.client.get(self.detail()).status_code, 200)
        self.assertEqual(self.client.post(self.detail()).status_code, 405)
        self.assertEqual(self.client.post(reverse('returned_revisions')).status_code, 405)
        self.assertEqual(SubmissionEvent.objects.count(), before)
        self.assertEqual(len(mail.outbox), 0)
