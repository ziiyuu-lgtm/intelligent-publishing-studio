import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.ai_provider import OPENAI_RESPONSES_URL, ProviderError
from publishing.models import (AIConsent, AIAssistanceRun, Journal, JournalAIPolicy, JournalSiteAIDesignRun,
    JournalSiteDraft, JournalSiteEvent, JournalSiteRelease, Membership, Organisation, Submission, SubmissionVersion)
from publishing.site_ai_provider import LEGACY_PROMPT_VERSION, parse_response
from publishing.site_design import blueprint, legacy_to_blueprint
from publishing.site_views import _fingerprint


FIRST_DESIGN = blueprint("#23468a", "#eee7d9", "#fefcf5", "editorial", "literary", corners="square")
SECOND_DESIGN = legacy_to_blueprint("forest", "classic")


def provider_response(options=None):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options": options if options is not None else [
            FIRST_DESIGN, SECOND_DESIGN]})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SiteAITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("site-ai-manager")
        cls.second_manager = users.create_user("site-ai-second-manager")
        cls.other_manager = users.create_user("site-ai-other-manager")
        cls.author = users.create_user("site-ai-author")
        cls.ai_manager = users.create_user("site-ai-policy-manager")
        org = Organisation.objects.create(name="Fictional publisher", slug="site-ai-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="Fictional Journal A", slug="site-ai-a")
        cls.other = Journal.objects.create(organisation=org, name="Fictional Journal B", slug="site-ai-b")
        for user in (cls.manager, cls.second_manager):
            Membership.objects.create(user=user, journal=cls.journal, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.other_manager, journal=cls.other, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AUTHOR)
        Membership.objects.create(user=cls.ai_manager, journal=cls.journal, role=Membership.Role.AI_MANAGER)
        cls.policy = JournalAIPolicy.objects.create(journal=cls.journal, site_design_enabled=True, journal_daily_limit=10)
        cls.site = JournalSiteDraft.objects.create(journal=cls.journal, brand_name="SECRET SITE NAME",
            headline="SECRET SITE HEADLINE", introduction="SECRET SITE INTRODUCTION", created_by=cls.manager)
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author, title="SECRET MANUSCRIPT TITLE",
            abstract="SECRET MANUSCRIPT ABSTRACT", manuscript="manuscripts/private.pdf", status=Submission.Status.SUBMITTED)
        cls.version = SubmissionVersion.objects.create(submission=cls.submission, number=1,
            manuscript="manuscripts/private.pdf", uploaded_by=cls.author)
        cls.consent = AIConsent.objects.create(version=cls.version, granted_by=cls.author, purpose="metadata-writing-v1",
            data_fingerprint="a" * 64)

    def setUp(self):
        environment = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
        environment.start()
        self.addCleanup(environment.stop)
        transport = patch("publishing.ai_provider._send_request", side_effect=RuntimeError("offline by default"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.client.force_login(self.manager)

    def data(self, **overrides):
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        self.assertEqual(page.status_code, 200)
        data = {"request_id":str(uuid.uuid4()), "revision":str(page.context["site"].revision),
            "fingerprint":page.context["fingerprint"], "policy_revision":str(page.context["ai_policy_revision"]),
            "model":page.context["ai_model_name"], "brief":"A quiet fictional academic site",
            "confirm":"yes", "synthetic":"yes"}
        data.update(overrides)
        return data

    def request_design(self, **overrides):
        return self.client.post(reverse("request_site_ai_design", args=[self.journal.slug]), self.data(**overrides))

    def completed(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        response = self.request_design()
        self.assertEqual(response.status_code, 302)
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.status, JournalSiteAIDesignRun.Status.COMPLETED)
        return run

    def result_url(self, run):
        return reverse("site_ai_result", args=[self.journal.slug, run.pk])

    def preview_url(self, run, index=0):
        return reverse("preview_site_ai_option", args=[self.journal.slug, run.pk, index])

    def apply_url(self, run, index=0):
        return reverse("apply_site_ai_option", args=[self.journal.slug, run.pk, index])

    def test_disabled_controls_block_requests_before_network(self):
        data = self.data()
        url = reverse("request_site_ai_design", args=[self.journal.slug])
        for values in ({"IPS_LOCAL_PUBLICATION_ENABLED":False}, {"DEBUG":False}):
            with self.subTest(values=values), override_settings(**values):
                self.assertEqual(self.client.post(url, data).status_code, 404)
        with override_settings(IPS_OPENAI_ENABLED=False):
            self.assertEqual(self.client.post(url, data).status_code, 409)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_design_enabled=False)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertFalse(JournalSiteAIDesignRun.objects.exists())
        self.transport.assert_not_called()

    def test_only_site_manager_of_this_journal_and_requester_can_access(self):
        run = self.completed()
        data = self.data()
        url = reverse("request_site_ai_design", args=[self.journal.slug])
        for user in (self.author, self.ai_manager, self.other_manager):
            self.client.force_login(user)
            self.assertEqual(self.client.post(url, data).status_code, 404)
            self.assertEqual(self.client.get(self.result_url(run)).status_code, 404)
        self.client.force_login(self.second_manager)
        self.assertEqual(self.client.get(self.result_url(run)).status_code, 404)
        self.assertEqual(self.client.get(self.preview_url(run)).status_code, 404)
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"1", "confirm":"yes"}).status_code, 404)
        self.assertEqual(Client().get(self.preview_url(run)).status_code, 302)
        Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.SITE_MANAGER).update(active=False)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.result_url(run)).status_code, 404)
        self.assertEqual(self.client.post(url, data).status_code, 404)

    def test_confirmation_input_stale_draft_and_policy_validation(self):
        url = reverse("request_site_ai_design", args=[self.journal.slug])
        valid = self.data()
        for override, status in (({"confirm":""}, 400), ({"synthetic":""}, 400), ({"request_id":"bad"}, 400),
                ({"brief":""}, 400), ({"brief":"x" * 601}, 400), ({"model":"different"}, 409),
                ({"policy_revision":"0"}, 409), ({"revision":"999"}, 409), ({"fingerprint":"bad"}, 409)):
            with self.subTest(override=override):
                self.assertEqual(self.client.post(url, {**valid, **override}).status_code, status)
        self.transport.assert_not_called()
        self.assertFalse(JournalSiteAIDesignRun.objects.exists())

    def test_provider_receives_brief_and_visual_settings_request_is_idempotent(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        data = self.data()
        url = reverse("request_site_ai_design", args=[self.journal.slug])
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.transport.call_count, 1)
        req = self.transport.call_args.args[0]
        self.assertEqual(req.full_url, OPENAI_RESPONSES_URL)
        payload = json.loads(req.data)
        self.assertEqual(json.loads(payload["input"]), {"design_brief":data["brief"], "current_appearance":SECOND_DESIGN})
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        for secret in (self.site.brand_name, self.site.headline, self.site.introduction, self.submission.title,
                       self.submission.abstract, "private.pdf", self.manager.username):
            self.assertNotIn(secret, req.data.decode())
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.brief_fingerprint, hashlib.sha256(data["brief"].encode()).hexdigest())
        self.assertNotIn(data["brief"], str(JournalSiteAIDesignRun.objects.values().get()))
        self.assertNotIn(data["brief"], str(list(JournalSiteEvent.objects.values())))
        self.assertEqual(len(run.options), 2)

    def test_ai_receives_saved_visual_settings_for_iterative_design(self):
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(design_config=FIRST_DESIGN)
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        self.assertEqual(self.request_design(brief="Keep these colours but change the layout.").status_code, 302)
        req = self.transport.call_args.args[0]
        sent = json.loads(json.loads(req.data)["input"])
        self.assertEqual(sent["current_appearance"], FIRST_DESIGN)
        self.assertEqual(sent["design_brief"], "Keep these colours but change the layout.")
        self.assertNotIn(self.site.headline, req.data.decode())

    def test_preview_apply_and_separate_publish_leave_existing_release_unchanged(self):
        self.site.refresh_from_db()
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        publish_url = reverse("publish_journal_site", args=[self.journal.slug])
        publication = {"revision":str(self.site.revision), "fingerprint":page.context["fingerprint"], "confirm":"yes", "synthetic":"yes"}
        self.assertEqual(self.client.post(publish_url, publication).status_code, 302)
        public = Client()
        public_url = reverse("public_journal_site", args=[self.journal.slug])
        self.assertContains(public.get(public_url), "theme-forest layout-classic")
        run = self.completed()
        page = self.client.get(self.result_url(run))
        self.assertContains(page, "AI design suggestions")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")
        preview = self.client.get(self.preview_url(run))
        self.assertContains(preview, "composition-editorial font-literary")
        self.assertContains(preview, "--site-accent:#23468a")
        self.assertIn("no-store", preview["Cache-Control"])
        self.assertEqual(preview["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.site.refresh_from_db()
        self.assertEqual((self.site.theme, self.site.layout, self.site.revision), ("forest", "classic", 1))
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"1"}).status_code, 400)
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((self.site.theme, self.site.layout, self.site.revision), ("forest", "classic", 2))
        self.assertEqual(self.site.design_config, FIRST_DESIGN)
        self.assertEqual((run.applied_option, run.applied_revision), (0, 2))
        self.assertContains(self.client.get(self.result_url(run)), "This suggestion has already been applied.")
        self.assertEqual(JournalSiteRelease.objects.filter(site=self.site).count(), 1)
        self.assertContains(public.get(public_url), "theme-forest layout-classic")
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"2", "confirm":"yes"}).status_code, 409)
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        publication.update(revision="2", fingerprint=page.context["fingerprint"])
        self.assertEqual(self.client.post(publish_url, publication).status_code, 302)
        self.assertContains(public.get(public_url), "composition-editorial font-literary")
        self.assertContains(public.get(public_url), "--site-accent:#23468a")

    def test_stale_draft_even_without_revision_change_blocks_preview_and_apply(self):
        run = self.completed()
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(headline="changed without increment")
        self.assertEqual(self.client.get(self.preview_url(run)).status_code, 409)
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"1", "confirm":"yes"}).status_code, 409)
        self.site.refresh_from_db()
        self.assertEqual(self.site.theme, "forest")

    def test_noop_option_is_recorded_without_increment(self):
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(design_config=SECOND_DESIGN)
        run = self.completed()
        self.assertEqual(self.client.post(self.apply_url(run, 1), {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((self.site.revision, run.applied_revision, run.applied_option), (1, 1, 1))
        self.assertEqual(self.client.post(self.apply_url(run, 0), {"revision":"1", "confirm":"yes"}).status_code, 409)

    def test_invalid_output_is_rejected_before_display_or_apply(self):
        invalid = [[], [{"theme":"<script>", "layout":"classic"}], [{"theme":"ink", "layout":"unknown"}],
            [{"theme":"ink", "layout":"classic", "code":"bad"}], [{"theme":"ink", "layout":"classic"}] * 2]
        for options in invalid:
            with self.subTest(options=options), self.assertRaises(ProviderError):
                parse_response(provider_response(options))
        run = self.completed()
        JournalSiteAIDesignRun.objects.filter(pk=run.pk).update(options=[{"theme":"<script>", "layout":"classic"}])
        self.assertEqual(self.client.get(self.result_url(run)).status_code, 404)
        self.assertEqual(self.client.get(self.preview_url(run)).status_code, 404)
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"1", "confirm":"yes"}).status_code, 404)
        self.site.refresh_from_db()
        self.assertEqual(self.site.revision, 1)

    def test_legacy_design_suggestions_remain_available_after_upgrade(self):
        run = JournalSiteAIDesignRun.objects.create(id=uuid.uuid4(), site=self.site, requested_by=self.manager,
            model_name="legacy-model", prompt_version=LEGACY_PROMPT_VERSION, policy_revision=1, site_revision=1,
            site_fingerprint=_fingerprint(self.site), brief_fingerprint="b" * 64, status="completed",
            options=[{"theme":"ink", "layout":"magazine"}])
        self.assertContains(self.client.get(self.preview_url(run)), "theme-ink layout-magazine")
        self.assertEqual(self.client.post(self.apply_url(run), {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual((self.site.theme, self.site.layout, self.site.design_config), ("ink", "magazine", {}))

    def test_new_design_accepts_custom_colours_and_rejects_css_or_extra_fields(self):
        design = {**FIRST_DESIGN, "primary":"#a1b2c3", "background":"#001122"}
        self.assertEqual(parse_response(provider_response([design])), [design])
        for bad in ({**design, "primary":"red; background:url(evil)"}, {**design, "font":"<script>"},
                    {**design, "show_feature":"true"}, {**design, "script":"alert(1)"}):
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                parse_response(provider_response([bad]))

    def test_failure_does_not_leak_or_retry(self):
        response = self.request_design()
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.status, JournalSiteAIDesignRun.Status.FAILED)
        self.assertNotContains(self.client.get(response.url), "offline by default")
        self.assertEqual(self.transport.call_count, 1)
        self.assertEqual(self.client.get(response.url).status_code, 200)
        self.assertEqual(self.transport.call_count, 1)

    def test_design_workflow_supports_chinese_interface(self):
        run = self.completed()
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(reverse("manage_journal_site", args=[self.journal.slug])), "AI 网站设计建议")
        self.assertContains(self.client.get(self.result_url(run)), "AI 设计建议")
        self.assertContains(self.client.get(self.result_url(run)), "预览方案")

    def test_policy_or_role_change_before_dispatch_cancels(self):
        def disable(**kwargs):
            JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_design_enabled=False)
        with patch("publishing.site_ai_views.JournalSiteEvent.objects.create", side_effect=disable):
            self.assertEqual(self.request_design().status_code, 302)
        self.assertEqual(JournalSiteAIDesignRun.objects.get().status, JournalSiteAIDesignRun.Status.CANCELLED)
        self.transport.assert_not_called()
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_design_enabled=True)
        def revoke(**kwargs):
            Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.SITE_MANAGER).update(active=False)
        with patch("publishing.site_ai_views.JournalSiteEvent.objects.create", side_effect=revoke):
            self.assertEqual(self.request_design().status_code, 302)
        self.assertEqual(JournalSiteAIDesignRun.objects.filter(status="cancelled").count(), 2)
        self.transport.assert_not_called()

    def test_metadata_attempts_count_towards_design_journal_and_account_limits(self):
        AIAssistanceRun.objects.create(id=uuid.uuid4(), version=self.version, requested_by=self.manager, consent=self.consent,
            model_name="test-model", prompt_version="test", data_fingerprint="a" * 64, status="failed")
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(journal_daily_limit=1)
        self.assertEqual(self.request_design().status_code, 429)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(journal_daily_limit=10)
        for _ in range(2):
            AIAssistanceRun.objects.create(id=uuid.uuid4(), version=self.version, requested_by=self.manager, consent=self.consent,
                model_name="test-model", prompt_version="test", data_fingerprint="a" * 64, status="cancelled")
        self.assertEqual(self.request_design().status_code, 429)
        self.transport.assert_not_called()

    def test_csrf_is_required_for_request_and_apply(self):
        run = self.completed()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(reverse("request_site_ai_design", args=[self.journal.slug]), {}).status_code, 403)
        self.assertEqual(client.post(self.apply_url(run), {"confirm":"yes", "revision":"1"}).status_code, 403)
        self.assertEqual(client.get(self.apply_url(run)).status_code, 405)
