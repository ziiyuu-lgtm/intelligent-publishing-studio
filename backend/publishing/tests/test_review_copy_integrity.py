import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import EditorialAssignment, Membership, ReviewAIConsent, ReviewAIPermissionRequest, ReviewRound, Submission, SubmissionVersion
from publishing.review_ai_consent import PROVIDER, PURPOSE, report_fingerprint
from publishing.review_copy import read_review_copy
from publishing.tests import test_workflow as workflow


@override_settings(DEBUG=True, PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ReviewerCopyIntegrityTests(TestCase):
    setUp = workflow.WorkflowTests.setUp
    create_separate_draft = workflow.WorkflowTests.create_separate_draft
    submit = workflow.WorkflowTests.submit
    invite = workflow.WorkflowTests.invite
    confirm_review_copy = workflow.WorkflowTests.confirm_review_copy

    @classmethod
    def setUpClass(cls):
        cls.media_dir = tempfile.mkdtemp(prefix="ips-review-copy-integrity-")
        cls.media_override = override_settings(MEDIA_ROOT=cls.media_dir)
        cls.media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls.media_override.disable()
        shutil.rmtree(cls.media_dir, ignore_errors=True)

    def prepared_submission(self):
        submission = self.create_separate_draft()
        self.submit(submission)
        self.client.force_login(self.editor)
        return submission

    def test_changed_file_blocks_invites_and_both_downloads_until_rechecked(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]),
            {"response": "accept", "declaration": "none"}).status_code, 302)
        version = submission.versions.get(number=1)
        old_bytes = Path(version.review_copy.path).read_bytes()
        self.assertEqual(b"".join(self.client.get(reverse("review_file", args=[assignment.pk])).streaming_content), old_bytes)

        new_bytes = b"%PDF-1.4\nA changed fictional reviewer copy\n%%EOF"
        Path(version.review_copy.path).write_bytes(new_bytes)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_file", args=[submission.pk])).status_code, 404)
        self.client.force_login(self.editor)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertFalse(page.context["review_copy_ready"])
        self.assertEqual(self.client.post(reverse("review_invite", args=[submission.pk]),
            {"reviewer": self.reviewer.pk}).status_code, 409)
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 2)
        self.client.force_login(self.reviewer)
        response = self.client.get(reverse("review_file", args=[assignment.pk]))
        self.assertEqual(b"".join(response.streaming_content), new_bytes)

    def test_separate_review_copy_is_blocked_when_round_version_is_stale(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]),
            {"response":"accept", "declaration":"none"}).status_code, 302)
        urls = [reverse("review_file", args=[assignment.pk]), reverse("submission_file", args=[submission.pk])]
        for url in urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            response.close()
        version = submission.versions.get(number=1)
        SubmissionVersion.objects.create(submission=submission, number=2,
            manuscript=version.manuscript.name, uploaded_by=self.author)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.assertNotContains(self.client.get(reverse("review_detail", args=[assignment.pk])), urls[0])

    def test_old_page_token_cannot_confirm_replaced_or_invalid_pdf(self):
        submission = self.prepared_submission()
        version = submission.versions.get(number=1)
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        token = page.context["review_copy_check_token"]
        Path(version.review_copy.path).write_bytes(b"%PDF-1.4\nDifferent file\n%%EOF")
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        version.refresh_from_db()
        self.assertFalse(version.review_copy_checked_sha256)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 0)
        Path(version.review_copy.path).write_bytes(b"not a PDF")
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        self.assertNotIn("review_copy_check_token", self.client.get(
            reverse("submission_detail", args=[submission.pk])).context)

    def test_legacy_check_requires_reconfirmation(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        version = submission.versions.get(number=1)
        version.review_copy_checked_sha256 = ""
        version.save(update_fields=["review_copy_checked_sha256"])
        page = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertFalse(page.context["review_copy_ready"])
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        version.refresh_from_db()
        self.assertEqual(len(version.review_copy_checked_sha256), 64)
        self.assertEqual(submission.events.filter(action="review_copy_checked").count(), 2)

    def test_token_is_bound_to_assigned_editor_and_requires_current_access(self):
        submission = self.prepared_submission()
        token = self.client.get(reverse("submission_detail", args=[submission.pk])).context["review_copy_check_token"]
        Membership.objects.create(user=self.other_editor, journal=self.journal, role=Membership.Role.EDITOR)
        EditorialAssignment.objects.create(submission=submission, editor=self.other_editor, assigned_by=self.coordinator)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        self.client.force_login(self.editor)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 404)
        self.assertFalse(submission.events.filter(action="review_copy_checked").exists())

    def test_download_returns_the_verified_bytes_even_if_file_changes_after_read(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.client.post(reverse("review_respond", args=[assignment.pk]), {"response": "accept", "declaration": "none"})
        version = submission.versions.get(number=1)
        expected = Path(version.review_copy.path).read_bytes()

        def replace_after_read(current_version):
            data = read_review_copy(current_version)
            Path(current_version.review_copy.path).write_bytes(b"%PDF-1.4\nchanged after check\n%%EOF")
            return data

        with patch("publishing.review_copy.read_review_copy", side_effect=replace_after_read):
            response = self.client.get(reverse("review_file", args=[assignment.pk]))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), expected)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)

    def test_changed_metadata_is_hidden_across_reviewer_pages_until_rechecked(self):
        submission = self.prepared_submission()
        preview = self.client.get(reverse("submission_detail", args=[submission.pk]))
        self.assertContains(preview, "Fictional separate-copy paper")
        self.assertContains(preview, "Fictional abstract.")
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse("review_respond", args=[assignment.pk]),
            {"response": "accept", "declaration": "none"}).status_code, 302)
        self.assertEqual(self.client.post(reverse("review_report", args=[assignment.pk]),
            {"recommendation": "major", "comments": "A fictional report."}).status_code, 302)
        assignment.refresh_from_db()
        version = submission.versions.get(number=1)
        ReviewAIPermissionRequest.objects.create(submission=submission, review_round=assignment.round,
            version=version, assignment=assignment, requested_by=self.editor, recipient=self.reviewer,
            subject=ReviewAIConsent.Subject.REVIEWER, provider=PROVIDER, purpose=PURPOSE,
            source_fingerprint=report_fingerprint(assignment))
        pages = [reverse("workspace"), reverse("review_detail", args=[assignment.pk]),
                 reverse("review_notice_inbox"), reverse("review_ai_permission_inbox"),
                 reverse("reviewer_review_ai_permission", args=[assignment.pk])]
        for url in pages:
            with self.subTest(url=url, phase="checked"):
                self.assertContains(self.client.get(url), "Fictional separate-copy paper")

        SubmissionVersion.objects.filter(pk=version.pk).update(title_snapshot="Unscreened title",
            abstract_snapshot="Unscreened abstract", keywords_snapshot=["Unscreened keyword"])
        for url in pages:
            with self.subTest(url=url, phase="changed"):
                page = self.client.get(url)
                self.assertEqual(page.status_code, 200)
                for text in ("Unscreened title", "Unscreened abstract", "Unscreened keyword"):
                    self.assertNotContains(page, text)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 404)
        self.client.force_login(self.editor)
        preview = self.client.get(reverse("submission_detail", args=[submission.pk]))
        for text in ("Unscreened title", "Unscreened abstract", "Unscreened keyword"):
            self.assertContains(preview, text)
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        self.client.force_login(self.reviewer)
        page = self.client.get(reverse("review_detail", args=[assignment.pk]))
        for text in ("Unscreened title", "Unscreened abstract", "Unscreened keyword"):
            self.assertContains(page, text)
        self.assertEqual(self.client.get(reverse("review_file", args=[assignment.pk])).status_code, 200)

    def test_old_check_form_does_not_confirm_new_metadata(self):
        submission = self.prepared_submission()
        version = submission.versions.get(number=1)
        token = self.client.get(reverse("submission_detail", args=[submission.pk])).context["review_copy_check_token"]
        SubmissionVersion.objects.filter(pk=version.pk).update(title_snapshot="Changed after opening check")
        self.assertEqual(self.confirm_review_copy(submission, token).status_code, 409)
        version.refresh_from_db()
        self.assertFalse(version.review_metadata_checked_sha256)
        self.assertFalse(submission.events.filter(action="review_copy_checked").exists())
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)

    def test_legacy_metadata_check_is_hidden_and_can_be_redone(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        version = submission.versions.get(number=1)
        version.review_metadata_checked_sha256 = ""
        version.save(update_fields=["review_metadata_checked_sha256"])
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(reverse("review_detail", args=[assignment.pk])),
                               "Fictional separate-copy paper")
        self.client.force_login(self.editor)
        self.assertFalse(self.client.get(reverse("submission_detail", args=[submission.pk])).context["review_copy_ready"])
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(reverse("review_detail", args=[assignment.pk])),
                            "Fictional separate-copy paper")

    def closed_legacy_round(self):
        submission = self.prepared_submission()
        self.assertEqual(self.confirm_review_copy(submission).status_code, 302)
        assignment = self.invite(submission)
        self.client.force_login(self.reviewer)
        self.assertEqual(self.client.post(reverse('review_respond', args=[assignment.pk]),
            {'response':'accept', 'declaration':'none'}).status_code, 302)
        self.assertEqual(self.client.post(reverse('review_report', args=[assignment.pk]),
            {'recommendation':'accept', 'comments':'A fictional review.'}).status_code, 302)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse('editorial_decision', args=[submission.pk]),
            {'outcome':'accept', 'author_message':'Fictional decision.'}).status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.round.status, ReviewRound.Status.CLOSED)
        version = assignment.round.version
        version.review_copy_checked_sha256 = ''
        version.review_metadata_checked_sha256 = ''
        version.save(update_fields=['review_copy_checked_sha256', 'review_metadata_checked_sha256'])
        return submission, assignment, version

    def test_closed_legacy_recheck_restores_metadata_but_not_file_access(self):
        submission, assignment, version = self.closed_legacy_round()
        url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        self.client.force_login(self.reviewer)
        self.assertNotContains(self.client.get(reverse('review_detail', args=[assignment.pk])),
                               'Fictional separate-copy paper')
        self.assertEqual(self.client.get(reverse('review_file', args=[assignment.pk])).status_code, 404)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse('submission_detail', args=[submission.pk])), url)
        page = self.client.get(url)
        self.assertContains(page, 'Fictional separate-copy paper')
        token = page.context['check_token']
        self.assertTrue(token)
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':token}).status_code, 302)
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':token}).status_code, 302)
        self.assertEqual(submission.events.filter(action='historical_review_copy_checked').count(), 1)
        version.refresh_from_db()
        self.assertEqual(len(version.review_copy_checked_sha256), 64)
        self.assertEqual(len(version.review_metadata_checked_sha256), 64)
        self.client.force_login(self.reviewer)
        self.assertContains(self.client.get(reverse('review_detail', args=[assignment.pk])),
                            'Fictional separate-copy paper')
        self.assertEqual(self.client.get(reverse('review_file', args=[assignment.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('submission_file', args=[submission.pk])).status_code, 404)

    def test_closed_recheck_is_translated_when_chinese_is_selected(self):
        submission, assignment, version = self.closed_legacy_round()
        url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        self.client.force_login(self.editor)
        self.client.post(reverse('set_language'), {'language': 'zh-hans', 'next': url})
        page = self.client.get(reverse('submission_detail', args=[submission.pk]))
        self.assertContains(page, '已关闭轮次的审稿副本核查')
        self.assertContains(page, '核查审稿副本和元数据')
        page = self.client.get(url)
        self.assertContains(page, '重新核查历史审稿副本')
        self.assertContains(page, '此项检查不能保证匿名。')
        self.assertContains(page, '不会重新开放该轮审稿或允许审稿人下载文件。')
        token = page.context['check_token']
        response = self.client.post(url, {'confirmed': 'yes', 'check_token': token}, follow=True)
        self.assertContains(response, '已记录该关闭轮次的人工核查。')

    def test_closed_recheck_rejects_stale_file_metadata_and_round_tokens(self):
        submission, assignment, version = self.closed_legacy_round()
        url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        self.client.force_login(self.editor)
        token = self.client.get(url).context['check_token']
        Path(version.review_copy.path).write_bytes(b'%PDF-1.4\nChanged fictional file\n%%EOF')
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':token}).status_code, 409)
        token = self.client.get(url).context['check_token']
        SubmissionVersion.objects.filter(pk=version.pk).update(title_snapshot='Changed fictional title')
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':token}).status_code, 409)
        token = self.client.get(url).context['check_token']
        another = ReviewRound.objects.create(submission=submission, version=version, number=2, status='closed')
        other_url = reverse('historical_review_copy_check', args=[submission.pk, another.number])
        self.assertEqual(self.client.post(other_url, {'confirmed':'yes', 'check_token':token}).status_code, 409)
        self.assertFalse(submission.events.filter(action='historical_review_copy_checked').exists())
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':token}).status_code, 302)

    def test_closed_recheck_requires_assigned_editor_and_csrf(self):
        submission, assignment, version = self.closed_legacy_round()
        url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        for user in (self.author, self.reviewer, self.other_editor, self.superuser):
            with self.subTest(user=user.username):
                self.client.force_login(user)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':'invalid'}).status_code, 404)
        guarded = Client(enforce_csrf_checks=True)
        guarded.force_login(self.editor)
        self.assertEqual(guarded.post(url, {'confirmed':'yes', 'check_token':'invalid'}).status_code, 403)
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertFalse(submission.events.filter(action='historical_review_copy_checked').exists())

    def test_closed_recheck_token_cannot_be_reused_by_another_assigned_editor(self):
        submission, assignment, version = self.closed_legacy_round()
        url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        self.client.force_login(self.editor)
        token = self.client.get(url).context['check_token']
        Membership.objects.create(user=self.other_editor, journal=self.journal, role=Membership.Role.EDITOR)
        EditorialAssignment.objects.create(submission=submission, editor=self.other_editor, assigned_by=self.coordinator)
        self.client.force_login(self.other_editor)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':token}).status_code, 409)
        self.assertFalse(submission.events.filter(action='historical_review_copy_checked').exists())

    def test_closed_recheck_rejects_open_round_and_same_version_in_open_round(self):
        submission, assignment, version = self.closed_legacy_round()
        self.client.force_login(self.editor)
        closed_url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        previously_valid_token = self.client.get(closed_url).context['check_token']
        self.assertTrue(previously_valid_token)
        active = ReviewRound.objects.create(submission=submission, version=version, number=2, status='open')
        open_url = reverse('historical_review_copy_check', args=[submission.pk, active.number])
        self.assertEqual(self.client.get(open_url).status_code, 404)
        page = self.client.get(closed_url)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context['check_token'])
        self.assertEqual(self.client.post(closed_url, {'confirmed':'yes', 'check_token':previously_valid_token}).status_code, 409)
        self.assertFalse(submission.events.filter(action='historical_review_copy_checked').exists())

    def test_closed_recheck_is_unavailable_outside_local_debug(self):
        submission, assignment, version = self.closed_legacy_round()
        url = reverse('historical_review_copy_check', args=[submission.pk, assignment.round.number])
        self.client.force_login(self.editor)
        with self.settings(DEBUG=False):
            self.assertEqual(self.client.get(url).status_code, 404)

    def test_closed_recheck_rejects_round_linked_to_another_submission(self):
        submission, assignment, version = self.closed_legacy_round()
        other = Submission.objects.create(journal=self.other, author=self.author, review_mode='separate',
            title='Another fictional paper', abstract='Another fictional abstract', manuscript=version.manuscript.name,
            status='submitted')
        other_version = SubmissionVersion.objects.create(submission=other, number=1,
            manuscript=version.manuscript.name, uploaded_by=self.author)
        wrong = ReviewRound.objects.create(submission=submission, version=other_version, number=2, status='closed')
        url = reverse('historical_review_copy_check', args=[submission.pk, wrong.number])
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {'confirmed':'yes', 'check_token':'invalid'}).status_code, 404)
        self.assertFalse(submission.events.filter(action='historical_review_copy_checked').exists())
