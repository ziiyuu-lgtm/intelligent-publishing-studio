import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import GatewayRejected, OPENAI, PUBLISHER_MODULES, run_ai_task
from publishing.ai_provider import OPENAI_RESPONSES_URL, ProviderError
from publishing.models import (Journal, Organisation, OrganisationAIPolicy, OrganisationAIPolicyEvent, OrganisationMembership,
    OrganisationPortalAIDesignRun, OrganisationPortalDraft, OrganisationPortalRelease)
from publishing.portal_ai_modules import PROMPT_VERSION, describe_sections, materialize_option, validate_options
from publishing.site_ai_provider import PUBLISHER_PROMPT_VERSION
from publishing.site_design import sample_by_id


SECTIONS = [
    {"kind":"about", "heading":"PRIVATE INTRO HEADING", "body":"PRIVATE INTRO BODY", "visible":True},
    {"kind":"journals", "heading":"", "body":"", "visible":True},
    {"kind":"custom", "heading":"PRIVATE CUSTOM HEADING", "body":"PRIVATE CUSTOM BODY", "visible":False},
]
DESCRIPTORS = [{"id":"m0", "kind":"about", "visible":True}, {"id":"m1", "kind":"journals", "visible":True},
               {"id":"m2", "kind":"custom", "visible":False}]
ARRANGEMENT = {"modules":[{"id":"m1", "kind":"journals", "visible":True},
    {"id":"m0", "kind":"about", "visible":True}, {"id":"new", "kind":"news", "visible":True},
    {"id":"m2", "kind":"custom", "visible":False}]}
SECOND = {"modules":[{"id":"m2", "kind":"custom", "visible":True}, {"id":"m0", "kind":"about", "visible":False},
    {"id":"m1", "kind":"journals", "visible":True}]}


def provider_response(options=None):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options":options if options is not None else [ARRANGEMENT, SECOND]})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PublisherPortalAIModuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("portal-modules-manager")
        cls.second_manager = users.create_user("portal-modules-second")
        cls.policy_manager = users.create_user("portal-modules-policy")
        cls.outsider = users.create_user("portal-modules-outsider")
        cls.org = Organisation.objects.create(name="PRIVATE PUBLISHER", slug="portal-module-ai")
        cls.other = Organisation.objects.create(name="Other publisher", slug="portal-module-ai-other")
        cls.journal = Journal.objects.create(organisation=cls.org, name="PRIVATE JOURNAL", slug="portal-module-journal")
        for user in (cls.manager, cls.second_manager):
            OrganisationMembership.objects.create(organisation=cls.org, user=user, role=OrganisationMembership.Role.PORTAL_MANAGER)
        OrganisationMembership.objects.create(organisation=cls.org, user=cls.policy_manager, role=OrganisationMembership.Role.AI_MANAGER)
        OrganisationMembership.objects.create(organisation=cls.other, user=cls.outsider, role=OrganisationMembership.Role.PORTAL_MANAGER)
        cls.policy = OrganisationAIPolicy.objects.create(organisation=cls.org, portal_design_enabled=True,
                                                         portal_modules_enabled=True, portal_daily_limit=10)
        cls.site = OrganisationPortalDraft.objects.create(organisation=cls.org, created_by=cls.manager,
            brand_name="PRIVATE BRAND", headline="PRIVATE HEADLINE", introduction="PRIVATE INTRO",
            design_config=sample_by_id("heritage")["config"], sections_config=SECTIONS, journal_ids=[cls.journal.pk])

    def setUp(self):
        env = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
        env.start()
        self.addCleanup(env.stop)
        transport = patch("publishing.ai_provider._send_request", side_effect=RuntimeError("offline"))
        self.transport = transport.start()
        self.addCleanup(transport.stop)
        self.client.force_login(self.manager)

    def url(self, name, *args):
        return reverse(name, args=[self.org.slug, *args])

    def data(self, **overrides):
        page = self.client.get(self.url("manage_publisher_portal"))
        self.assertEqual(page.status_code, 200)
        data = {"request_id":str(uuid.uuid4()), "revision":str(page.context["site"].revision),
            "fingerprint":page.context["fingerprint"], "policy_revision":str(page.context["ai_policy_revision"]),
            "model":"test-model", "brief":"Organise a fictional publisher portal", "confirm":"yes", "synthetic":"yes"}
        data.update(overrides)
        return data

    def request(self, **overrides):
        return self.client.post(self.url("request_publisher_ai_modules"), self.data(**overrides))

    def completed(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        response = self.request()
        self.assertEqual(response.status_code, 302)
        run = OrganisationPortalAIDesignRun.objects.latest("created_at")
        self.assertEqual(run.status, "completed")
        return run

    def publish(self):
        page = self.client.get(self.url("manage_publisher_portal"))
        return self.client.post(self.url("publish_publisher_portal"),
            {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"],
             "confirm":"yes", "synthetic":"yes"})

    def test_only_brief_and_module_descriptors_are_sent_and_request_is_idempotent(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        data = self.data()
        url = self.url("request_publisher_ai_modules")
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.transport.call_count, 1)
        req = self.transport.call_args.args[0]
        self.assertEqual(req.full_url, OPENAI_RESPONSES_URL)
        payload = json.loads(req.data)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["text"]["format"]["name"], "publisher_module_options")
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(json.loads(payload["input"]), {"design_brief":data["brief"], "current_modules":DESCRIPTORS})
        for secret in ("PRIVATE INTRO HEADING", "PRIVATE INTRO BODY", "PRIVATE CUSTOM HEADING", "PRIVATE CUSTOM BODY",
                       self.org.name, self.journal.name, self.site.brand_name, self.site.headline,
                       self.site.introduction, self.manager.username):
            self.assertNotIn(secret, req.data.decode())
        run = OrganisationPortalAIDesignRun.objects.get()
        self.assertEqual(run.brief_fingerprint, hashlib.sha256(data["brief"].encode()).hexdigest())
        self.assertNotIn(data["brief"], str(OrganisationPortalAIDesignRun.objects.values().get()))
        self.assertNotIn("PRIVATE INTRO BODY", str(run.options))
        self.assertEqual(run.options, [ARRANGEMENT, SECOND])
        result = self.client.get(self.url("publisher_ai_module_result", run.pk))
        self.assertContains(result, "New empty module")
        self.assertContains(self.client.get(self.url("manage_publisher_portal")), "Module arrangement")
        self.assertEqual(result["X-Robots-Tag"], "noindex, nofollow, noarchive")

    def test_preview_apply_preserve_text_and_public_snapshot(self):
        self.assertEqual(self.publish().status_code, 302)
        public = Client()
        public_url = self.url("public_publisher_portal")
        self.assertNotContains(public.get(public_url), "News and announcements")
        run = self.completed()
        page = self.client.get(self.url("preview_publisher_ai_modules", run.pk, 0))
        html = page.content.decode()
        self.assertIn("no-store", page["Cache-Control"])
        self.assertLess(html.index("publisher-module-journals"), html.index("publisher-module-about"))
        self.assertIn("News and announcements", html)
        self.assertNotIn("PRIVATE CUSTOM BODY", html)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, SECTIONS)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_modules", run.pk, 0), {"revision":"1"}).status_code, 400)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_modules", run.pk, 0),
                                          {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual([x["kind"] for x in self.site.sections_config], ["journals", "about", "news", "custom"])
        self.assertEqual(self.site.sections_config[1]["body"], "PRIVATE INTRO BODY")
        self.assertEqual(self.site.sections_config[3]["body"], "PRIVATE CUSTOM BODY")
        self.assertEqual((self.site.sections_config[2]["heading"], self.site.sections_config[2]["body"]), ("", ""))
        self.assertEqual(self.site.journal_ids, [self.journal.pk])
        self.assertEqual((run.applied_option, run.applied_revision), (0, 2))
        self.assertEqual(OrganisationPortalRelease.objects.filter(site=self.site).count(), 1)
        self.assertNotContains(public.get(public_url), "News and announcements")
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_modules", run.pk, 0),
                                          {"revision":"2", "confirm":"yes"}).status_code, 409)
        self.assertEqual(self.publish().status_code, 302)
        self.assertContains(public.get(public_url), "News and announcements")

    def test_module_policy_is_independent_of_visual_design(self):
        self.client.force_login(self.policy_manager)
        url = self.url("manage_publisher_ai_policy")
        self.assertEqual(self.client.post(url, {"revision":"1", "portal_modules_enabled":"on", "portal_daily_limit":"4",
                                                "confirm":"yes", "synthetic":"yes"}).status_code, 302)
        self.policy.refresh_from_db()
        self.assertTrue(self.policy.portal_modules_enabled)
        self.assertFalse(self.policy.portal_design_enabled)
        self.assertEqual(OrganisationAIPolicyEvent.objects.get().configuration,
                         {"portal_design_enabled":False, "portal_modules_enabled":True, "portal_pages_enabled":False, "portal_page_blocks_enabled":False, "portal_page_layout_enabled":False, "portal_daily_limit":4})
        self.client.force_login(self.manager)
        self.assertEqual(self.request().status_code, 302)
        self.assertEqual(self.transport.call_count, 1)
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_design_enabled=True, portal_modules_enabled=False)
        self.assertEqual(self.request().status_code, 409)
        self.assertEqual(self.transport.call_count, 1)

    def test_gates_confirmation_and_stale_data_block_network(self):
        data = self.data()
        url = self.url("request_publisher_ai_modules")
        for override, status in (({"confirm":""},400), ({"synthetic":""},400), ({"brief":""},400),
                ({"brief":"x"*601},400), ({"request_id":"bad"},400), ({"model":"bad"},409),
                ({"policy_revision":"0"},409), ({"fingerprint":"bad"},409), ({"revision":"99"},409)):
            with self.subTest(override=override):
                self.assertEqual(self.client.post(url, {**data, **override}).status_code, status)
        with override_settings(IPS_OPENAI_ENABLED=False):
            self.assertEqual(self.client.post(url, data).status_code, 409)
        for config in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with override_settings(**config):
                self.assertEqual(self.client.post(url, data).status_code, 404)
        OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_modules_enabled=False)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.transport.assert_not_called()
        self.assertFalse(OrganisationPortalAIDesignRun.objects.exists())

    def test_request_and_result_are_limited_to_authorised_requester(self):
        run = self.completed()
        for user in (self.second_manager, self.policy_manager, self.outsider):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.url("publisher_ai_module_result", run.pk)).status_code, 404)
            self.assertEqual(self.client.get(self.url("preview_publisher_ai_modules", run.pk, 0)).status_code, 404)
            self.assertEqual(self.client.post(self.url("apply_publisher_ai_modules", run.pk, 0),
                                              {"revision":"1", "confirm":"yes"}).status_code, 404)
        self.assertEqual(Client().get(self.url("publisher_ai_module_result", run.pk)).status_code, 302)
        OrganisationMembership.objects.filter(user=self.manager, organisation=self.org).update(active=False)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.url("publisher_ai_module_result", run.pk)).status_code, 404)

    def test_stale_or_tampered_suggestions_do_not_change_draft(self):
        run = self.completed()
        changed = [{**SECTIONS[0], "body":"changed"}, *SECTIONS[1:]]
        OrganisationPortalDraft.objects.filter(pk=self.site.pk).update(sections_config=changed)
        self.assertEqual(self.client.get(self.url("preview_publisher_ai_modules", run.pk, 0)).status_code, 409)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_modules", run.pk, 0),
                                          {"revision":"1", "confirm":"yes"}).status_code, 409)
        OrganisationPortalDraft.objects.filter(pk=self.site.pk).update(sections_config=SECTIONS)
        OrganisationPortalAIDesignRun.objects.filter(pk=run.pk).update(options=[{"modules":[{"id":"m0", "kind":"<script>", "visible":True}]}])
        self.assertEqual(self.client.get(self.url("publisher_ai_module_result", run.pk)).status_code, 404)
        self.assertEqual(self.client.get(self.url("preview_publisher_ai_modules", run.pk, 0)).status_code, 404)
        self.assertEqual(self.client.post(self.url("apply_publisher_ai_modules", run.pk, 0),
                                          {"revision":"1", "confirm":"yes"}).status_code, 404)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, SECTIONS)

    def test_contract_preserves_existing_modules_and_rejects_injected_content(self):
        self.assertEqual(describe_sections(SECTIONS), DESCRIPTORS)
        materialised = materialize_option(ARRANGEMENT, SECTIONS)
        self.assertEqual(materialised[1]["body"], "PRIVATE INTRO BODY")
        self.assertEqual(materialised[3]["body"], "PRIVATE CUSTOM BODY")
        bad_options = [
            {"modules":ARRANGEMENT["modules"][:-1]},
            {"modules":ARRANGEMENT["modules"] + [ARRANGEMENT["modules"][0]]},
            {"modules":[{**x, "visible":False} for x in ARRANGEMENT["modules"]]},
            {"modules":[*ARRANGEMENT["modules"], {"id":"new", "kind":"custom", "visible":True}]},
            {"modules":[*ARRANGEMENT["modules"], {"id":"new", "kind":"news", "visible":True}]},
            {"modules":[{**x, "body":"injected"} for x in ARRANGEMENT["modules"]]},
            {"modules":[{**ARRANGEMENT["modules"][0], "kind":"about"}, *ARRANGEMENT["modules"][1:]]},
        ]
        for bad in bad_options:
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                validate_options([bad], DESCRIPTORS)

    def test_invalid_provider_response_is_not_stored_or_retried(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response([{"modules":ARRANGEMENT["modules"][:-1]}])
        data = self.data()
        url = self.url("request_publisher_ai_modules")
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        run = OrganisationPortalAIDesignRun.objects.get()
        self.assertEqual((run.status, run.options), ("failed", []))
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.get(response.url).status_code, 200)
        self.assertEqual(self.transport.call_count, 1)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, SECTIONS)

    def test_gateway_rejects_extra_fields_and_unknown_descriptors(self):
        args = {"task":PUBLISHER_MODULES, "provider":OPENAI, "api_key":"test-key", "model":"test-model"}
        with patch("publishing.ai_gateway.request_publisher_module_suggestions", return_value=[]) as adapter:
            self.assertEqual(run_ai_task(**args, payload={"brief":"fictional", "modules":DESCRIPTORS}), [])
            for payload in ({"brief":"x", "modules":DESCRIPTORS, "body":"secret"},
                            {"brief":"x", "modules":[{"id":"m0", "kind":"custom", "visible":True, "text":"secret"}]}):
                with self.assertRaises(GatewayRejected):
                    run_ai_task(**args, payload=payload)
            adapter.assert_called_once()

    def test_policy_revocation_before_dispatch_cancels(self):
        def disable(*args, **kwargs):
            OrganisationAIPolicy.objects.filter(pk=self.policy.pk).update(portal_modules_enabled=False)
        with patch("publishing.portal_ai_module_views._event", side_effect=disable):
            self.assertEqual(self.request().status_code, 302)
        self.assertEqual(OrganisationPortalAIDesignRun.objects.get().status, "cancelled")
        self.transport.assert_not_called()

    def test_module_and_visual_requests_share_limit_and_cannot_reuse_ids(self):
        for _ in range(3):
            OrganisationPortalAIDesignRun.objects.create(id=uuid.uuid4(), site=self.site, requested_by=self.manager,
                model_name="test-model", prompt_version=PUBLISHER_PROMPT_VERSION, policy_revision=1, site_revision=1,
                site_fingerprint="a"*64, brief_fingerprint="b"*64, status="failed")
        visual = OrganisationPortalAIDesignRun.objects.first()
        self.assertEqual(self.request(request_id=str(visual.pk)).status_code, 409)
        self.assertEqual(self.request().status_code, 429)
        self.transport.assert_not_called()

    def test_module_request_id_cannot_be_reused_for_visual_task(self):
        run = self.completed()
        data = self.data(request_id=str(run.pk))
        self.assertEqual(self.client.post(self.url("request_publisher_ai_design"), data).status_code, 409)
        self.assertEqual(self.client.get(self.url("publisher_ai_result", run.pk)).status_code, 404)
        self.assertEqual(self.transport.call_count, 1)

    def test_csrf_and_chinese_ui(self):
        run = self.completed()
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.manager)
        self.assertEqual(csrf.post(self.url("request_publisher_ai_modules"), {}).status_code, 403)
        self.assertEqual(csrf.post(self.url("apply_publisher_ai_modules", run.pk, 0), {}).status_code, 403)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(self.url("manage_publisher_portal")), "AI 出版社门户模块编排")
        self.assertContains(self.client.get(self.url("publisher_ai_module_result", run.pk)), "AI 模块建议")
