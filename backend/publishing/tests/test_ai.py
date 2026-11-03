import json
import os
import uuid
from unittest.mock import patch
from urllib import error

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.ai_provider import OPENAI_RESPONSES_URL, PROMPT_VERSION, ProviderError, _NoRedirect, _parse_response, metadata_fingerprint
from publishing.models import (AIConsent, AIAssistanceRun, EditorialAssignment, Journal, JournalAIPolicy,
    JournalSiteAIDesignRun, JournalSiteDraft, Membership, Organisation, Submission, SubmissionVersion)


def mock_response(evidence="Fictional abstract.", observation="Clarify this sentence.", suggestion="Use a more specific phrase."):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"suggestions":[{"evidence":evidence, "observation":observation, "suggestion":suggestion}]})}
    ]}]}


@override_settings(DEBUG=True, IPS_OPENAI_ENABLED=True, IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AIWorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.author = users.create_user("SECRET_AUTHOR_NAME_4", password="synthetic-password")
        cls.editor = users.create_user("test-editor", password="synthetic-password")
        cls.outsider = users.create_user("test-outsider", password="synthetic-password")
        cls.reviewer = users.create_user("test-reviewer", password="synthetic-password")
        org = Organisation.objects.create(name="Test publisher", slug="ai-test-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Test journal", slug="ai-test-journal", review_mode=Journal.ReviewMode.DIRECT)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AUTHOR)
        Membership.objects.create(user=cls.editor, journal=cls.journal, role=Membership.Role.EDITOR)
        Membership.objects.create(user=cls.reviewer, journal=cls.journal, role=Membership.Role.REVIEWER)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author, title="Fictional title", abstract="Fictional abstract.",
                          manuscript="manuscripts/fake.pdf", status=Submission.Status.SUBMITTED)
        cls.version = SubmissionVersion.objects.create(submission=cls.submission, number=1, manuscript="manuscripts/fake.pdf", uploaded_by=cls.author)
        EditorialAssignment.objects.create(submission=cls.submission, editor=cls.editor, assigned_by=cls.editor)

    def setUp(self):
        self.environment = patch.dict(os.environ, {"OPENAI_API_KEY":"synthetic-key-only"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.transport = patch("publishing.ai_provider._send_request", side_effect=RuntimeError("test transport is offline"))
        self.transport_mock = self.transport.start()
        self.addCleanup(self.transport.stop)

    def allow(self):
        Journal.objects.filter(pk=self.journal.pk).update(allow_openai_metadata=True)
        JournalAIPolicy.objects.update_or_create(journal=self.journal, defaults={"metadata_enabled":True, "allow_author":True,
            "allow_editor":True, "journal_daily_limit":10})

    def grant(self):
        self.allow()
        self.client.force_login(self.author)
        current = Submission.objects.get(pk=self.submission.pk)
        response = self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]),
                                    {"version":str(self.version.pk), "fingerprint":metadata_fingerprint(self.version.pk, current.title, current.abstract), "policy_revision":str(JournalAIPolicy.objects.get(journal=self.journal).revision), "confirm":"yes", "synthetic":"yes"})
        self.assertEqual(response.status_code, 302)
        return AIConsent.objects.get(version=self.version, revoked_at__isnull=True)

    def run_request(self, run_id=None, actor=None, **overrides):
        self.client.force_login(actor or self.author)
        current = Submission.objects.get(pk=self.submission.pk)
        data = {"request_id":str(run_id or uuid.uuid4()), "version":str(self.version.pk), "model":"test-model", "fingerprint":metadata_fingerprint(self.version.pk, current.title, current.abstract), "confirm":"yes", "synthetic":"yes"}
        policy = JournalAIPolicy.objects.filter(journal=self.journal).first()
        data["policy_revision"] = str(policy.revision if policy else 0)
        data.update(overrides)
        return self.client.post(reverse("request_ai_suggestions", args=[self.submission.pk]), data)

    def test_local_checks_are_read_only_and_scoped_to_author_and_editor(self):
        for user in (self.author, self.editor):
            self.client.force_login(user)
            page = self.client.get(reverse("checks_page", args=[self.submission.pk]))
            self.assertContains(page, "Local checks")
            self.assertContains(page, "do not use AI")
            self.assertContains(page, "The journal has not enabled external AI suggestions")
        for user in (self.reviewer, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.get(reverse("checks_page", args=[self.submission.pk])).status_code, 404)
        self.transport_mock.assert_not_called()
        self.assertFalse(AIAssistanceRun.objects.exists())

    def test_site_design_attempts_count_towards_metadata_journal_limit(self):
        self.grant()
        site = JournalSiteDraft.objects.create(journal=self.journal, brand_name="Fictional site", headline="Test",
            introduction="Fictional text", created_by=self.author)
        JournalSiteAIDesignRun.objects.create(id=uuid.uuid4(), site=site, requested_by=self.author, model_name="test-model",
            prompt_version="test", policy_revision=1, site_revision=1, site_fingerprint="a" * 64,
            brief_fingerprint="b" * 64, status="failed")
        JournalAIPolicy.objects.filter(journal=self.journal).update(journal_daily_limit=1)
        self.assertEqual(self.run_request().status_code, 429)
        self.transport_mock.assert_not_called()

    def test_external_provider_is_disabled_by_default_and_no_implicit_consent(self):
        self.client.force_login(self.author)
        response = self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "confirm":"yes", "synthetic":"yes"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.run_request().status_code, 409)
        self.assertFalse(AIConsent.objects.exists())
        self.transport_mock.assert_not_called()
        self.allow()
        with override_settings(IPS_OPENAI_ENABLED=False):
            self.assertEqual(self.run_request().status_code, 409)
        self.transport_mock.assert_not_called()

    def test_only_author_can_grant_and_revoke_and_both_confirmations_are_required(self):
        self.allow()
        for user in (self.editor, self.outsider, self.reviewer):
            self.client.force_login(user)
            self.assertEqual(self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "confirm":"yes", "synthetic":"yes"}).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "confirm":"yes"}).status_code, 400)
        self.assertEqual(self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":"999999", "confirm":"yes", "synthetic":"yes"}).status_code, 409)
        consent = self.grant()
        self.assertEqual(consent.purpose, PROMPT_VERSION)
        self.grant()
        self.assertEqual(AIConsent.objects.filter(version=self.version, revoked_at__isnull=True).count(), 1)
        self.client.force_login(self.editor)
        self.assertEqual(self.client.post(reverse("revoke_ai_consent", args=[consent.pk])).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(reverse("revoke_ai_consent", args=[consent.pk])).status_code, 302)
        consent.refresh_from_db()
        self.assertIsNotNone(consent.revoked_at)
        self.assertEqual(self.run_request(actor=self.editor).status_code, 403)
        self.transport_mock.assert_not_called()

    def test_changing_metadata_or_version_invalidates_authorisation(self):
        old = self.grant()
        Submission.objects.filter(pk=self.submission.pk).update(abstract="Changed fictional abstract.")
        self.assertEqual(self.run_request().status_code, 403)
        self.client.force_login(self.author)
        page = self.client.get(reverse("checks_page", args=[self.submission.pk]))
        self.assertContains(page, "The author has not authorised")
        current = Submission.objects.get(pk=self.submission.pk)
        self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "fingerprint":metadata_fingerprint(self.version.pk, current.title, current.abstract), "policy_revision":str(JournalAIPolicy.objects.get(journal=self.journal).revision), "confirm":"yes", "synthetic":"yes"})
        old.refresh_from_db()
        self.assertIsNotNone(old.revoked_at)
        self.assertEqual(AIConsent.objects.filter(version=self.version).count(), 2)
        SubmissionVersion.objects.create(submission=self.submission, number=2, manuscript="manuscripts/fake2.pdf", uploaded_by=self.author)
        self.assertEqual(self.run_request().status_code, 409)
        self.transport_mock.assert_not_called()

    def test_changed_task_purpose_requires_new_authorisation(self):
        self.grant()
        with patch("publishing.ai_views.PROMPT_VERSION", "metadata-writing-v2"):
            self.assertEqual(self.run_request().status_code, 403)
        self.transport_mock.assert_not_called()

    def test_changed_metadata_requires_fresh_confirmation_for_consent_and_request(self):
        self.allow()
        old_fingerprint = metadata_fingerprint(self.version.pk, self.submission.title, self.submission.abstract)
        Submission.objects.filter(pk=self.submission.pk).update(abstract="Updated fictional abstract.")
        self.client.force_login(self.author)
        response = self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]),
                                    {"version":str(self.version.pk), "fingerprint":old_fingerprint, "policy_revision":str(JournalAIPolicy.objects.get(journal=self.journal).revision), "confirm":"yes", "synthetic":"yes"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(AIConsent.objects.exists())
        self.grant()
        self.assertEqual(self.run_request(fingerprint=old_fingerprint).status_code, 409)
        self.assertFalse(AIAssistanceRun.objects.exists())
        self.transport_mock.assert_not_called()

    def test_request_requires_confirmation_version_model_and_active_consent(self):
        self.allow()
        self.assertEqual(self.run_request().status_code, 403)
        self.grant()
        self.assertEqual(self.run_request(confirm="").status_code, 400)
        self.assertEqual(self.run_request(synthetic="").status_code, 400)
        self.assertEqual(self.run_request(request_id="not-a-uuid").status_code, 400)
        self.assertEqual(self.run_request(model="other-model").status_code, 409)
        self.assertEqual(self.run_request(version="999").status_code, 409)
        self.assertEqual(self.run_request(actor=self.outsider).status_code, 404)
        self.transport_mock.assert_not_called()

    def test_openai_request_is_metadata_only_and_idempotent(self):
        self.grant()
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response(suggestion="<script>alert(1)</script>")
        request_id = uuid.uuid4()
        response = self.run_request(run_id=request_id, actor=self.editor)
        self.assertEqual(response.status_code, 302)
        run = AIAssistanceRun.objects.get(pk=request_id)
        self.assertEqual(run.status, AIAssistanceRun.Status.COMPLETED)
        self.assertEqual(run.prompt_version, PROMPT_VERSION)
        self.assertEqual(run.policy_revision, JournalAIPolicy.objects.get(journal=self.journal).revision)
        provider_request = self.transport_mock.call_args.args[0]
        self.assertEqual(provider_request.full_url, OPENAI_RESPONSES_URL)
        payload = json.loads(provider_request.data.decode("utf-8"))
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(json.loads(payload["input"]), {"title":"Fictional title", "abstract":"Fictional abstract."})
        self.assertNotIn(self.author.username, provider_request.data.decode("utf-8"))
        self.assertNotIn("fake.pdf", provider_request.data.decode("utf-8"))
        result_page = self.client.get(reverse("ai_result", args=[run.pk]))
        self.assertContains(result_page, "&lt;script&gt;alert(1)&lt;/script&gt;")
        self.assertNotContains(result_page, "<script>alert(1)</script>")
        self.assertNotContains(result_page, "synthetic-key-only")
        self.assertEqual(self.run_request(run_id=request_id, actor=self.editor).status_code, 302)
        self.assertEqual(self.transport_mock.call_count, 1)
        self.client.force_login(self.author)
        self.assertEqual(self.client.get(reverse("ai_result", args=[run.pk])).status_code, 404)

    def test_provider_failure_and_unverifiable_evidence_do_not_leak_or_retry(self):
        self.grant()
        self.transport_mock.side_effect = error.URLError("SECRET_PROVIDER_BODY")
        response = self.run_request()
        run = AIAssistanceRun.objects.get()
        self.assertEqual(run.status, AIAssistanceRun.Status.FAILED)
        page = self.client.get(response.url)
        self.assertNotContains(page, "SECRET_PROVIDER_BODY")
        self.assertNotContains(page, "synthetic-key-only")
        self.assertEqual(run.error_code, "provider_unavailable")
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response(evidence="an invented quotation")
        self.run_request()
        self.assertEqual(AIAssistanceRun.objects.filter(status=AIAssistanceRun.Status.FAILED).count(), 2)
        self.assertTrue(all(not item.result for item in AIAssistanceRun.objects.all()))

    def test_revocation_before_send_cancels_without_network(self):
        consent = self.grant()
        def revoke_on_start(submission, actor, action):
            if action == "ai_suggestion_requested":
                AIConsent.objects.filter(pk=consent.pk).update(revoked_at=timezone.now())
        with patch("publishing.ai_views._event", side_effect=revoke_on_start):
            response = self.run_request()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.get().status, AIAssistanceRun.Status.CANCELLED)
        self.transport_mock.assert_not_called()

    def test_policy_or_editor_role_revoked_before_send_cancels_without_network(self):
        self.grant()
        def disable_policy(submission, actor, action):
            if action == "ai_suggestion_requested":
                Journal.objects.filter(pk=self.journal.pk).update(allow_openai_metadata=False)
        with patch("publishing.ai_views._event", side_effect=disable_policy):
            self.assertEqual(self.run_request(actor=self.editor).status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.get().status, AIAssistanceRun.Status.CANCELLED)
        self.transport_mock.assert_not_called()
        self.allow()
        def revoke_role(submission, actor, action):
            if action == "ai_suggestion_requested":
                Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        with patch("publishing.ai_views._event", side_effect=revoke_role):
            self.assertEqual(self.run_request(actor=self.editor).status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.filter(status=AIAssistanceRun.Status.CANCELLED).count(), 2)
        self.transport_mock.assert_not_called()

    def test_missing_key_or_invalid_model_blocks_authorisation(self):
        self.allow()
        self.client.force_login(self.author)
        with patch.dict(os.environ, {"OPENAI_API_KEY":""}):
            result = self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "confirm":"yes", "synthetic":"yes"})
            self.assertEqual(result.status_code, 409)
        with override_settings(IPS_OPENAI_MODEL="bad model\nheader"):
            result = self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "confirm":"yes", "synthetic":"yes"})
            self.assertEqual(result.status_code, 409)
        self.transport_mock.assert_not_called()

    def test_rate_limit_counts_attempts_without_automatic_retry(self):
        self.grant()
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response()
        for _ in range(3):
            self.assertEqual(self.run_request().status_code, 302)
        self.assertEqual(self.run_request().status_code, 429)
        self.assertEqual(self.transport_mock.call_count, 3)

    def test_csrf_guards_consent_and_external_request(self):
        self.allow()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.author)
        self.assertEqual(client.post(reverse("grant_ai_consent", args=[self.submission.pk]), {"version":str(self.version.pk), "confirm":"yes", "synthetic":"yes"}).status_code, 403)
        self.assertEqual(client.post(reverse("request_ai_suggestions", args=[self.submission.pk]), {"request_id":str(uuid.uuid4()), "version":str(self.version.pk), "model":"test-model", "confirm":"yes", "synthetic":"yes"}).status_code, 403)
        self.transport_mock.assert_not_called()

    def test_ai_results_disappear_if_editor_role_is_revoked(self):
        self.grant()
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response()
        self.run_request(actor=self.editor)
        run = AIAssistanceRun.objects.get()
        Membership.objects.filter(user=self.editor, journal=self.journal, role=Membership.Role.EDITOR).update(active=False)
        self.assertEqual(self.client.get(reverse("ai_result", args=[run.pk])).status_code, 404)

    def test_editor_assignment_removed_before_sending_cancels_request(self):
        self.grant()
        def remove_assignment(submission, actor, action):
            if action == "ai_suggestion_requested":
                EditorialAssignment.objects.filter(submission=submission, editor=self.editor).update(status="revoked")
        with patch("publishing.ai_views._event", side_effect=remove_assignment):
            self.assertEqual(self.run_request(actor=self.editor).status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.get().status, AIAssistanceRun.Status.CANCELLED)
        self.transport_mock.assert_not_called()

    def test_ai_result_access_ends_when_editor_assignment_is_removed(self):
        self.grant()
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response()
        self.run_request(actor=self.editor)
        run = AIAssistanceRun.objects.get()
        EditorialAssignment.objects.filter(submission=self.submission, editor=self.editor).update(status="revoked")
        self.assertEqual(self.client.get(reverse("ai_result", args=[run.pk])).status_code, 404)

    def test_missing_policy_and_debug_off_fail_closed(self):
        Journal.objects.filter(pk=self.journal.pk).update(allow_openai_metadata=True)
        self.assertEqual(self.run_request().status_code, 409)
        self.allow()
        with override_settings(DEBUG=False):
            self.assertEqual(self.run_request().status_code, 409)
        self.assertFalse(AIAssistanceRun.objects.exists())
        self.transport_mock.assert_not_called()

    def test_requesting_roles_are_independently_enforced(self):
        self.grant()
        JournalAIPolicy.objects.filter(journal=self.journal).update(allow_editor=False)
        self.assertEqual(self.run_request(actor=self.editor).status_code, 403)
        self.client.force_login(self.editor)
        self.assertContains(self.client.get(reverse("checks_page", args=[self.submission.pk])), "This role is not enabled")
        JournalAIPolicy.objects.filter(journal=self.journal).update(allow_author=False, allow_editor=True)
        self.assertEqual(self.run_request(actor=self.author).status_code, 403)
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response()
        self.assertEqual(self.run_request(actor=self.editor).status_code, 302)
        self.assertEqual(self.transport_mock.call_count, 1)

    def test_policy_change_requires_fresh_confirmation(self):
        self.grant()
        policy = JournalAIPolicy.objects.get(journal=self.journal)
        old_revision = str(policy.revision)
        JournalAIPolicy.objects.filter(pk=policy.pk).update(revision=policy.revision + 1, journal_daily_limit=9)
        self.assertEqual(self.run_request(policy_revision=old_revision).status_code, 409)
        self.client.force_login(self.author)
        current = Submission.objects.get(pk=self.submission.pk)
        data = {"version":str(self.version.pk), "fingerprint":metadata_fingerprint(self.version.pk, current.title, current.abstract),
                "policy_revision":old_revision, "confirm":"yes", "synthetic":"yes"}
        self.assertEqual(self.client.post(reverse("grant_ai_consent", args=[self.submission.pk]), data).status_code, 409)
        self.assertFalse(AIAssistanceRun.objects.exists())
        self.transport_mock.assert_not_called()

    def test_policy_change_before_network_cancels_the_request(self):
        self.grant()
        def restrict_editor(submission, actor, action):
            if action == "ai_suggestion_requested":
                JournalAIPolicy.objects.filter(journal=self.journal).update(allow_editor=False)
        with patch("publishing.ai_views._event", side_effect=restrict_editor):
            self.assertEqual(self.run_request(actor=self.editor).status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.get().status, AIAssistanceRun.Status.CANCELLED)
        self.transport_mock.assert_not_called()

    def test_journal_budget_counts_attempts_across_people_but_not_other_journals(self):
        self.grant()
        JournalAIPolicy.objects.filter(journal=self.journal).update(journal_daily_limit=1)
        other = Journal.objects.create(organisation=self.journal.organisation, name="Other fictional journal", slug="other-ai-budget")
        other_submission = Submission.objects.create(journal=other, author=self.outsider, title="Other title", abstract="Other abstract",
            manuscript="manuscripts/other.pdf", status=Submission.Status.SUBMITTED)
        other_version = SubmissionVersion.objects.create(submission=other_submission, number=1, manuscript="manuscripts/other.pdf", uploaded_by=self.outsider)
        other_consent = AIConsent.objects.create(version=other_version, granted_by=self.outsider, purpose=PROMPT_VERSION, data_fingerprint="other")
        AIAssistanceRun.objects.create(id=uuid.uuid4(), version=other_version, requested_by=self.outsider, consent=other_consent,
            model_name="test-model", prompt_version=PROMPT_VERSION, data_fingerprint="other", status=AIAssistanceRun.Status.FAILED)
        self.transport_mock.side_effect = None
        self.transport_mock.return_value = mock_response()
        self.assertEqual(self.run_request(actor=self.author).status_code, 302)
        self.assertEqual(self.run_request(actor=self.editor).status_code, 429)
        self.assertEqual(self.transport_mock.call_count, 1)

    def test_failed_and_cancelled_attempts_count_towards_journal_limit(self):
        self.grant()
        JournalAIPolicy.objects.filter(journal=self.journal).update(journal_daily_limit=2)
        self.assertEqual(self.run_request(actor=self.author).status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.get().status, AIAssistanceRun.Status.FAILED)
        def restrict_editor(submission, actor, action):
            if action == "ai_suggestion_requested":
                JournalAIPolicy.objects.filter(journal=self.journal).update(allow_editor=False)
        with patch("publishing.ai_views._event", side_effect=restrict_editor):
            self.assertEqual(self.run_request(actor=self.editor).status_code, 302)
        self.assertEqual(AIAssistanceRun.objects.filter(status=AIAssistanceRun.Status.CANCELLED).count(), 1)
        JournalAIPolicy.objects.filter(journal=self.journal).update(allow_editor=True)
        self.assertEqual(self.run_request(actor=self.author).status_code, 429)
        self.assertEqual(self.transport_mock.call_count, 1)

    def test_checks_page_supports_simplified_chinese(self):
        self.client.force_login(self.author)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(reverse("checks_page", args=[self.submission.pk]))
        self.assertContains(page, "本地检查")
        self.assertContains(page, "不使用 AI")


class ProviderParsingTests(TestCase):
    def test_refusals_incomplete_output_and_invented_evidence_fail_closed(self):
        invalid = [
            {"status":"incomplete", "output":[]},
            {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[{"type":"refusal", "refusal":"no"}]}]},
            mock_response(evidence="not in the source"),
        ]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ProviderError):
                _parse_response(item, "title", "Fictional abstract.")

    def test_redirects_are_not_followed(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.invalid"))
