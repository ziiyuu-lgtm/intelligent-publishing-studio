import hashlib
import json
import os
import uuid
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from publishing.ai_gateway import GatewayRejected, OPENAI, PUBLISHER_DESIGN, run_ai_task
from publishing.ai_provider import OPENAI_RESPONSES_URL
from publishing.ai_usage import account_requests_since
from publishing.models import (Journal, JournalAIPolicy, JournalSiteAIDesignRun, JournalSiteDraft, Membership,
    Organisation, OrganisationAIPolicy, OrganisationAIPolicyEvent, OrganisationMembership,
    OrganisationPortalAIDesignRun, OrganisationPortalDraft, OrganisationPortalEvent, OrganisationPortalRelease, Submission)
from publishing.site_design import blueprint, sample_by_id

FIRST = blueprint("#314a89", "#f4e8c5", "#fffcf4", "editorial", "literary", corners="square")
SECOND = blueprint("#f0547a", "#faf4ed", "#eeeeee", "immersive", "modern", alignment="center")


def provider_response(options=None):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options":options if options is not None else [FIRST, SECOND]})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PublisherPortalAITests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("publisher-portal-manager")
        cls.second = users.create_user("publisher-portal-second")
        cls.ai_manager = users.create_user("publisher-policy-manager")
        cls.outsider = users.create_user("other-publisher-manager")
        cls.author = users.create_user("publisher-author")
        cls.admin = users.create_superuser("publisher-admin", password="fictional-password")
        cls.org = Organisation.objects.create(name="PRIVATE PUBLISHER", slug="publisher-ai")
        cls.other = Organisation.objects.create(name="Other fictional publisher", slug="publisher-ai-other")
        cls.journal = Journal.objects.create(organisation=cls.org, name="PRIVATE JOURNAL", slug="private-journal")
        cls.submission = Submission.objects.create(journal=cls.journal, author=cls.author,
            title="PRIVATE MANUSCRIPT", abstract="PRIVATE ABSTRACT", manuscript="manuscripts/private.pdf")
        for user in (cls.manager, cls.second):
            OrganisationMembership.objects.create(user=user, organisation=cls.org, role=OrganisationMembership.Role.PORTAL_MANAGER)
        OrganisationMembership.objects.create(user=cls.ai_manager, organisation=cls.org, role=OrganisationMembership.Role.AI_MANAGER)
        OrganisationMembership.objects.create(user=cls.outsider, organisation=cls.other, role=OrganisationMembership.Role.PORTAL_MANAGER)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AI_MANAGER)
        cls.site = OrganisationPortalDraft.objects.create(organisation=cls.org, created_by=cls.manager,
            brand_name="SECRET PORTAL NAME", headline="SECRET PORTAL HEADLINE", tagline="SECRET PORTAL TAGLINE",
            introduction="SECRET PORTAL INTRODUCTION", design_config=sample_by_id("heritage")["config"], journal_ids=[cls.journal.pk])
        cls.policy = OrganisationAIPolicy.objects.create(organisation=cls.org, portal_design_enabled=True, portal_daily_limit=10)

    def setUp(self):
        environment = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
        environment.start()
        self.addCleanup(environment.stop)
        transport = patch("publishing.ai_provider._send_request", side_effect=RuntimeError("private transport error"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.client.force_login(self.manager)

    def url(self, name, *args, org=None):
        return reverse(name, args=[(org or self.org).slug, *args])

    def data(self, **changes):
        page = self.client.get(self.url("manage_publisher_portal"))
        self.assertEqual(page.status_code, 200)
        data = {"request_id":str(uuid.uuid4()), "revision":str(page.context["site"].revision),
            "fingerprint":page.context["fingerprint"], "policy_revision":str(page.context["ai_policy_revision"]),
            "model":page.context["ai_model_name"], "brief":"A welcoming fictional research publisher",
            "confirm":"yes", "synthetic":"yes"}
        data.update(changes)
        return data

    def request_design(self, **changes):
        return self.client.post(self.url("request_publisher_ai_design"), self.data(**changes))

    def completed(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        response = self.request_design()
        self.assertEqual(response.status_code, 302)
        run = OrganisationPortalAIDesignRun.objects.latest("created_at")
        self.assertEqual(run.status, "completed")
        return run

    def test_policy_roles_are_separate_scoped_and_audited(self):
        url = self.url("manage_publisher_ai_policy")
        for user in (self.manager, self.author, self.outsider, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.ai_manager)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertContains(self.client.get(reverse("workspace")), "Publisher AI policy")
        self.assertEqual(self.client.get(self.url("manage_publisher_portal")).status_code, 404)
        self.assertEqual(self.client.get(reverse("submission_detail", args=[self.submission.pk])).status_code, 404)
        data = {"revision":"1", "portal_design_enabled":"on", "portal_daily_limit":"4", "confirm":"yes", "synthetic":"yes"}
        self.assertEqual(self.client.post(url, {**data, "confirm":""}).status_code, 400)
        self.assertEqual(self.client.post(url, {**data, "portal_daily_limit":"101"}).status_code, 400)
        self.assertEqual(self.client.post(url, {**data, "revision":"0"}).status_code, 409)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.policy.refresh_from_db()
        self.assertEqual((self.policy.revision, self.policy.portal_daily_limit), (2, 4))
        self.assertEqual(OrganisationAIPolicyEvent.objects.get(policy=self.policy).configuration,
                         {"portal_design_enabled":True, "portal_modules_enabled":False, "portal_pages_enabled":False, "portal_page_blocks_enabled":False, "portal_daily_limit":4})
        self.assertFalse(JournalAIPolicy.objects.exists())
        self.assertEqual(self.client.post(url, {**data, "revision":"2"}).status_code, 302)
        self.assertEqual(OrganisationAIPolicyEvent.objects.count(), 1)
        OrganisationMembership.objects.filter(user=self.ai_manager, organisation=self.org).update(active=False)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_default_off_and_each_gate_prevents_network(self):
        data = self.data()
        url = self.url("request_publisher_ai_design")
        for values in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with self.subTest(values=values), override_settings(**values):
                self.assertEqual(self.client.post(url, data).status_code, 404)
        with override_settings(IPS_OPENAI_ENABLED=False):
            self.assertEqual(self.client.post(url, data).status_code, 409)
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_design_enabled=False)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).delete()
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.assertFalse(OrganisationPortalAIDesignRun.objects.exists())
        self.transport.assert_not_called()

    def test_new_publisher_policy_defaults_to_disabled(self):
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).delete()
        self.client.force_login(self.ai_manager)
        response = self.client.post(self.url("manage_publisher_ai_policy"),
            {"revision":"0", "portal_daily_limit":"5", "confirm":"yes", "synthetic":"yes"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(OrganisationAIPolicy.objects.get(organisation=self.org).portal_design_enabled)
        self.assertFalse(OrganisationAIPolicy.objects.get(organisation=self.org).portal_pages_enabled)
        self.assertFalse(OrganisationAIPolicy.objects.get(organisation=self.org).portal_page_blocks_enabled)
        self.client.force_login(self.manager)
        self.assertContains(self.client.get(self.url("manage_publisher_portal")),
                            "The publisher has not enabled AI portal design")
        self.assertEqual(self.request_design().status_code, 409)
        self.transport.assert_not_called()

    def test_page_suggestions_have_their_own_publisher_policy_switch(self):
        self.client.force_login(self.ai_manager)
        data = {"revision":"1", "portal_pages_enabled":"on", "portal_daily_limit":"5", "confirm":"yes", "synthetic":"yes"}
        self.assertEqual(self.client.post(self.url("manage_publisher_ai_policy"), data).status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.portal_pages_enabled)
        self.assertFalse(self.policy.portal_design_enabled)
        self.assertFalse(self.policy.portal_modules_enabled)
        self.assertTrue(OrganisationAIPolicyEvent.objects.get(policy=self.policy).configuration["portal_pages_enabled"])
        self.client.force_login(self.manager)
        self.assertEqual(self.request_design().status_code, 409)
        self.transport.assert_not_called()

    def test_page_block_suggestions_have_a_separate_publisher_policy_switch(self):
        self.client.force_login(self.ai_manager)
        data = {"revision":"1", "portal_page_blocks_enabled":"on", "portal_daily_limit":"5", "confirm":"yes", "synthetic":"yes"}
        self.assertEqual(self.client.post(self.url("manage_publisher_ai_policy"), data).status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.portal_page_blocks_enabled)
        self.assertFalse(self.policy.portal_pages_enabled)
        self.assertTrue(OrganisationAIPolicyEvent.objects.get(policy=self.policy).configuration["portal_page_blocks_enabled"])

    def test_confirmation_staleness_and_model_are_checked_before_network(self):
        url = self.url("request_publisher_ai_design")
        valid = self.data()
        for override, status in (({"confirm":""}, 400), ({"synthetic":""}, 400), ({"request_id":"bad"}, 400),
            ({"brief":""}, 400), ({"brief":"x"*601}, 400), ({"model":"wrong"}, 409),
            ({"policy_revision":"0"}, 409), ({"revision":"99"}, 409), ({"fingerprint":"bad"}, 409)):
            with self.subTest(override=override):
                self.assertEqual(self.client.post(url, {**valid, **override}).status_code, status)
        self.transport.assert_not_called()
        self.assertFalse(OrganisationPortalAIDesignRun.objects.exists())

    def test_only_brief_and_visual_settings_are_sent_and_request_is_idempotent(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        data = self.data()
        url = self.url("request_publisher_ai_design")
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.transport.call_count, 1)
        req = self.transport.call_args.args[0]
        self.assertEqual(req.full_url, OPENAI_RESPONSES_URL)
        payload = json.loads(req.data)
        self.assertEqual(json.loads(payload["input"]), {"design_brief":data["brief"], "current_appearance":self.site.design_config})
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["name"], "publisher_site_blueprints")
        self.assertTrue(payload["text"]["format"]["strict"])
        for secret in (self.org.name, self.journal.name, self.site.brand_name, self.site.headline, self.site.introduction,
                       self.submission.title, self.submission.abstract, self.manager.username, "private.pdf"):
            self.assertNotIn(secret, req.data.decode())
        run = OrganisationPortalAIDesignRun.objects.get()
        self.assertEqual(run.brief_fingerprint, hashlib.sha256(data["brief"].encode()).hexdigest())
        self.assertNotIn(data["brief"], str(OrganisationPortalAIDesignRun.objects.values().get()))
        self.assertNotIn(data["brief"], str(list(OrganisationPortalEvent.objects.values())))

    def test_results_are_private_and_only_requester_can_access(self):
        run = self.completed()
        for user in (self.ai_manager, self.second, self.outsider, self.author, self.admin):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url("publisher_ai_result", run.pk)).status_code, 404)
            self.assertEqual(self.client.get(self.url("preview_publisher_ai_option", run.pk, 0)).status_code, 404)
            self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0),
                                              {"confirm":"yes", "revision":"1"}).status_code, 404)
        self.client.force_login(self.manager)
        page = self.client.get(self.url("publisher_ai_result", run.pk))
        self.assertContains(page, "Design option 1")
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertIn("no-store", page["Cache-Control"])
        self.assertEqual(Client().get(self.url("preview_publisher_ai_option", run.pk, 0)).status_code, 302)
        OrganisationMembership.objects.filter(user=self.manager, organisation=self.org).update(active=False)
        self.assertEqual(self.client.get(self.url("publisher_ai_result", run.pk)).status_code, 404)

    def test_preview_and_apply_do_not_publish_and_can_be_refined(self):
        release = OrganisationPortalRelease.objects.create(site=self.site, revision=1, published_by=self.manager,
            brand_name=self.site.brand_name, headline=self.site.headline, tagline=self.site.tagline,
            introduction=self.site.introduction, design_config=self.site.design_config, journals=[])
        OrganisationPortalDraft.objects.filter(pk=self.site.pk).update(current_release=release)
        run = self.completed()
        page = self.client.get(self.url("preview_publisher_ai_option", run.pk, 0))
        self.assertContains(page, "Private AI suggestion preview")
        self.assertContains(page, "--site-accent:#314a89")
        self.site.refresh_from_db()
        self.assertNotEqual(self.site.design_config, FIRST)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0), {"revision":"1"}).status_code, 400)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0),
            {"confirm":"yes", "revision":"1"}).status_code, 302)
        self.site.refresh_from_db()
        release.refresh_from_db()
        self.assertEqual(self.site.design_config, FIRST)
        self.assertNotEqual(release.design_config, FIRST)
        self.assertEqual(self.site.journal_ids, [self.journal.pk])
        self.assertEqual(self.site.current_release_id, release.pk)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0),
            {"confirm":"yes", "revision":"2"}).status_code, 409)
        self.transport.return_value = provider_response([SECOND])
        self.assertEqual(self.request_design(brief="Keep the fictional style, but use a different accent").status_code, 302)
        payload = json.loads(self.transport.call_args.args[0].data)
        self.assertEqual(json.loads(payload["input"])["current_appearance"], FIRST)

    def test_visual_design_preview_and_apply_preserve_portal_modules(self):
        sections = [
            {"kind":"about", "heading":"Our fictional press", "body":"Fictional content", "visible":True},
            {"kind":"journals", "heading":"", "body":"", "visible":True},
            {"kind":"custom", "heading":"Hidden title", "body":"Hidden fictional text", "visible":False},
        ]
        OrganisationPortalDraft.objects.filter(pk=self.site.pk).update(sections_config=sections)
        run = self.completed()
        page = self.client.get(self.url("preview_publisher_ai_option", run.pk, 0))
        self.assertContains(page, "Fictional content")
        self.assertContains(page, self.journal.name)
        self.assertNotContains(page, "Hidden fictional text")
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0),
            {"confirm":"yes", "revision":"1"}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, sections)
        self.assertEqual(self.site.journal_ids, [self.journal.pk])

    def test_stale_drafts_or_journal_change_prevent_preview_and_apply(self):
        run = self.completed()
        OrganisationPortalDraft.objects.filter(pk=self.site.pk).update(headline="Changed outside the revision")
        self.assertEqual(self.client.get(self.url("preview_publisher_ai_option", run.pk, 0)).status_code, 409)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0),
                                          {"confirm":"yes", "revision":"1"}).status_code, 409)
        self.assertFalse(OrganisationPortalAIDesignRun.objects.get(pk=run.pk).applied_revision)

    def test_saved_options_are_validated_again_when_viewed(self):
        run = self.completed()
        OrganisationPortalAIDesignRun.objects.filter(pk=run.pk).update(options=[{**FIRST, "primary":"<script>"}])
        self.assertEqual(self.client.get(self.url("publisher_ai_result", run.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url("preview_publisher_ai_option", run.pk, 0)).status_code, 404)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_option", run.pk, 0),
                                          {"confirm":"yes", "revision":"1"}).status_code, 404)

    def test_untrusted_or_duplicate_outputs_are_rejected_without_retry(self):
        self.transport.side_effect = None
        for options in ([{**FIRST, "primary":"red; background:url(evil)"}], [FIRST, FIRST],
                        [{**FIRST, "script":"<script>"}]):
            self.transport.return_value = provider_response(options)
            response = self.request_design()
            run = OrganisationPortalAIDesignRun.objects.latest("created_at")
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.options, [])
            self.assertEqual(self.client.get(response.url).status_code, 200)
        self.transport.return_value = provider_response([])
        self.assertEqual(self.request_design().status_code, 429)
        self.assertEqual(self.transport.call_count, 3)  # 24-hour account limit blocks the fourth attempt

    def test_failure_does_not_expose_errors_or_retry(self):
        response = self.request_design()
        run = OrganisationPortalAIDesignRun.objects.get()
        self.assertEqual(run.status, "failed")
        self.assertNotContains(self.client.get(response.url), "private transport error")
        self.assertEqual(self.transport.call_count, 1)
        self.client.get(response.url)
        self.assertEqual(self.transport.call_count, 1)

    def test_policy_or_role_change_before_dispatch_cancels(self):
        def disable(*args, **kwargs):
            OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_design_enabled=False)
        with patch("publishing.portal_ai_views._event", side_effect=disable):
            self.assertEqual(self.request_design().status_code, 302)
        self.assertEqual(OrganisationPortalAIDesignRun.objects.get().status, "cancelled")
        self.transport.assert_not_called()
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_design_enabled=True)
        def revoke(*args, **kwargs):
            OrganisationMembership.objects.filter(user=self.manager, organisation=self.org).update(active=False)
        with patch("publishing.portal_ai_views._event", side_effect=revoke):
            self.assertEqual(self.request_design().status_code, 302)
        self.assertEqual(OrganisationPortalAIDesignRun.objects.filter(status="cancelled").count(), 2)
        self.transport.assert_not_called()

    def test_portal_and_account_limits_include_failed_and_journal_attempts(self):
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_daily_limit=1)
        self.request_design()
        self.client.force_login(self.second)
        self.assertEqual(self.request_design().status_code, 429)
        self.assertEqual(self.transport.call_count, 1)
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_daily_limit=10)
        self.client.force_login(self.manager)
        journal_site = JournalSiteDraft.objects.create(journal=self.journal, brand_name="fictional", headline="fictional",
            introduction="fictional", created_by=self.manager)
        for _ in range(2):
            JournalSiteAIDesignRun.objects.create(id=uuid.uuid4(), site=journal_site, requested_by=self.manager,
                model_name="test-model", prompt_version="test", policy_revision=1, site_revision=1,
                site_fingerprint="a"*64, brief_fingerprint="b"*64, status="cancelled")
        self.assertEqual(account_requests_since(self.manager, timezone.now()-timedelta(hours=24)), 3)
        self.assertEqual(self.request_design().status_code, 429)
        self.assertEqual(self.transport.call_count, 1)

    def test_gateway_rejects_extra_data_and_invalid_blueprints(self):
        args = {"task":PUBLISHER_DESIGN, "provider":OPENAI, "model":"test-model", "api_key":"test-key"}
        with patch("publishing.ai_gateway.request_publisher_design_suggestions", return_value=[FIRST]) as adapter:
            self.assertEqual(run_ai_task(**args, payload={"brief":"fictional", "appearance":FIRST}), [FIRST])
            for payload in ({"brief":"fictional", "appearance":FIRST, "manuscript":"private"},
                            {"brief":None, "appearance":FIRST}, {"brief":"test", "appearance":{"script":"bad"}}):
                with self.assertRaises(GatewayRejected):
                    run_ai_task(**args, payload=payload)
            adapter.assert_called_once()

    def test_csrf_and_chinese_interface(self):
        run = self.completed()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(self.url("request_publisher_ai_design"), {}).status_code, 403)
        self.assertEqual(client.post(self.url("apply_publisher_ai_option", run.pk, 0), {}).status_code, 403)
        client.force_login(self.ai_manager)
        self.assertEqual(client.post(self.url("manage_publisher_ai_policy"), {}).status_code, 403)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(self.url("manage_publisher_portal")), "AI 出版社门户设计")
        self.assertContains(self.client.get(self.url("publisher_ai_result", run.pk)), "AI 设计建议")
        self.client.force_login(self.ai_manager)
        self.assertContains(self.client.get(self.url("manage_publisher_ai_policy")), "出版社 AI 策略")
