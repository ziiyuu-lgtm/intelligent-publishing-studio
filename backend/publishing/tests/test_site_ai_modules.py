import hashlib
import json
import os
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import GatewayRejected, OPENAI, SITE_MODULES, run_ai_task
from publishing.ai_provider import OPENAI_RESPONSES_URL, ProviderError
from publishing.models import (Journal, JournalAIPolicy, JournalSiteAIDesignRun, JournalSiteDraft, JournalSiteEvent,
                               JournalSiteRelease, Membership, Organisation)
from publishing.site_ai_modules import describe_sections, materialize_option, parse_response, validate_options


SECTIONS = [
    {"kind":"about", "heading":"CONFIDENTIAL HEADING A", "body":"CONFIDENTIAL BODY A", "visible":True},
    {"kind":"archive", "heading":"", "body":"", "visible":True},
    {"kind":"custom", "heading":"CONFIDENTIAL CUSTOM", "body":"CONFIDENTIAL CUSTOM BODY", "visible":False},
]
DESCRIPTORS = [{"id":"m0", "kind":"about", "visible":True}, {"id":"m1", "kind":"archive", "visible":True},
               {"id":"m2", "kind":"custom", "visible":False}]
ARRANGEMENT = {"modules":[{"id":"m1", "kind":"archive", "visible":True},
    {"id":"m0", "kind":"about", "visible":True}, {"id":"new", "kind":"authors", "visible":True},
    {"id":"m2", "kind":"custom", "visible":False}]}
SECOND = {"modules":[{"id":"m2", "kind":"custom", "visible":True}, {"id":"m0", "kind":"about", "visible":False},
    {"id":"m1", "kind":"archive", "visible":True}]}


def provider_response(options=None):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options": options if options is not None else [ARRANGEMENT, SECOND]})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class SiteAIModuleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        users = get_user_model().objects
        cls.manager = users.create_user("module-ai-manager")
        cls.other_same = users.create_user("module-ai-second-manager")
        cls.other = users.create_user("module-ai-other-manager")
        cls.author = users.create_user("module-ai-author")
        org = Organisation.objects.create(name="Fictional publisher", slug="module-ai-publisher")
        cls.journal = Journal.objects.create(organisation=org, name="SECRET JOURNAL NAME", slug="module-ai-a")
        cls.other_journal = Journal.objects.create(organisation=org, name="Other", slug="module-ai-b")
        for user in (cls.manager, cls.other_same):
            Membership.objects.create(user=user, journal=cls.journal, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.other, journal=cls.other_journal, role=Membership.Role.SITE_MANAGER)
        Membership.objects.create(user=cls.author, journal=cls.journal, role=Membership.Role.AUTHOR)
        cls.policy = JournalAIPolicy.objects.create(journal=cls.journal, site_design_enabled=True, site_modules_enabled=True, journal_daily_limit=10)
        cls.site = JournalSiteDraft.objects.create(journal=cls.journal, brand_name="PRIVATE BRAND",
            headline="PRIVATE HEADLINE", introduction="PRIVATE INTRO", created_by=cls.manager, sections_config=SECTIONS)

    def setUp(self):
        env = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
        env.start(); self.addCleanup(env.stop)
        transport = patch("publishing.ai_provider._send_request", side_effect=RuntimeError("offline"))
        self.transport = transport.start(); self.addCleanup(transport.stop)
        self.client.force_login(self.manager)

    def data(self, **overrides):
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        self.assertEqual(page.status_code, 200)
        data = {"request_id":str(uuid.uuid4()), "revision":str(page.context["site"].revision),
            "fingerprint":page.context["fingerprint"], "policy_revision":str(page.context["ai_policy_revision"]),
            "model":"test-model", "brief":"Make the fictional journal easier for authors to navigate",
            "confirm":"yes", "synthetic":"yes"}
        data.update(overrides)
        return data

    def request(self, **overrides):
        return self.client.post(reverse("request_site_ai_modules", args=[self.journal.slug]), self.data(**overrides))

    def completed(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        response = self.request()
        self.assertEqual(response.status_code, 302)
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.status, "completed")
        return run

    def result(self, run):
        return reverse("site_ai_module_result", args=[self.journal.slug, run.pk])

    def preview(self, run, index=0):
        return reverse("preview_site_ai_modules", args=[self.journal.slug, run.pk, index])

    def apply(self, run, index=0):
        return reverse("apply_site_ai_modules", args=[self.journal.slug, run.pk, index])

    def publish_data(self):
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"],
                "confirm":"yes", "synthetic":"yes"}

    def test_request_discloses_only_fictional_brief_and_module_metadata(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response()
        data = self.data()
        url = reverse("request_site_ai_modules", args=[self.journal.slug])
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(self.transport.call_count, 1)
        req = self.transport.call_args.args[0]
        self.assertEqual(req.full_url, OPENAI_RESPONSES_URL)
        payload = json.loads(req.data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(json.loads(payload["input"]), {"design_brief":data["brief"], "current_modules":DESCRIPTORS})
        for secret in ("CONFIDENTIAL HEADING A", "CONFIDENTIAL BODY A", "CONFIDENTIAL CUSTOM", "PRIVATE BRAND",
                       "PRIVATE HEADLINE", "PRIVATE INTRO", "SECRET JOURNAL NAME", self.manager.username):
            self.assertNotIn(secret, req.data.decode())
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.brief_fingerprint, hashlib.sha256(data["brief"].encode()).hexdigest())
        self.assertNotIn(data["brief"], str(JournalSiteAIDesignRun.objects.values().get()))
        self.assertNotIn("CONFIDENTIAL BODY A", str(run.options))
        self.assertEqual(run.options, [ARRANGEMENT, SECOND])
        self.assertEqual(self.client.get(reverse("site_ai_result", args=[self.journal.slug, run.pk])).url, self.result(run))
        result_page = self.client.get(self.result(run))
        self.assertContains(result_page, "New empty module")
        self.assertEqual(result_page["X-Robots-Tag"], "noindex, nofollow, noarchive")

    def test_preview_apply_preserves_text_and_published_snapshot(self):
        publish_url = reverse("publish_journal_site", args=[self.journal.slug])
        self.assertEqual(self.client.post(publish_url, self.publish_data()).status_code, 302)
        public = Client()
        public_url = reverse("public_journal_site", args=[self.journal.slug])
        first_public = public.get(public_url).content.decode()
        self.assertNotIn("For authors", first_public)
        run = self.completed()
        page = self.client.get(self.preview(run))
        html = page.content.decode()
        self.assertEqual(page["X-Robots-Tag"], "noindex, nofollow, noarchive")
        self.assertIn("no-store", page["Cache-Control"])
        self.assertLess(html.index("module-archive"), html.index("module-about"))
        self.assertIn("For authors", html)
        self.assertNotIn("CONFIDENTIAL CUSTOM BODY", html)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, SECTIONS)
        self.assertEqual(self.client.post(self.apply(run), {"revision":"1"}).status_code, 400)
        self.assertEqual(self.client.post(self.apply(run), {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual([x["kind"] for x in self.site.sections_config], ["archive", "about", "authors", "custom"])
        self.assertEqual(self.site.sections_config[1]["body"], "CONFIDENTIAL BODY A")
        self.assertEqual(self.site.sections_config[3]["body"], "CONFIDENTIAL CUSTOM BODY")
        self.assertEqual((self.site.sections_config[2]["heading"], self.site.sections_config[2]["body"]), ("", ""))
        self.assertEqual((run.applied_option, run.applied_revision), (0, 2))
        self.assertEqual(JournalSiteRelease.objects.filter(site=self.site).count(), 1)
        self.assertNotContains(public.get(public_url), "For authors")
        self.assertContains(self.client.get(self.result(run)), "This suggestion has already been applied.")
        self.assertEqual(self.client.post(self.apply(run), {"revision":"2", "confirm":"yes"}).status_code, 409)
        self.assertEqual(self.client.post(publish_url, self.publish_data()).status_code, 302)
        self.assertContains(public.get(public_url), "For authors")

    def test_disabled_policy_environment_confirmation_and_stale_data(self):
        data = self.data()
        url = reverse("request_site_ai_modules", args=[self.journal.slug])
        for override, status in (({"confirm":""},400), ({"synthetic":""},400), ({"brief":""},400),
            ({"brief":"a"*601},400), ({"request_id":"bad"},400), ({"model":"bad"},409),
            ({"policy_revision":"0"},409), ({"fingerprint":"bad"},409), ({"revision":"999"},409)):
            with self.subTest(override=override):
                self.assertEqual(self.client.post(url, {**data, **override}).status_code, status)
        with override_settings(IPS_OPENAI_ENABLED=False):
            self.assertEqual(self.client.post(url, data).status_code, 409)
        for setting in ({"DEBUG":False}, {"IPS_LOCAL_PUBLICATION_ENABLED":False}):
            with override_settings(**setting):
                self.assertEqual(self.client.post(url, data).status_code, 404)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_modules_enabled=False)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        self.transport.assert_not_called()
        self.assertFalse(JournalSiteAIDesignRun.objects.exists())

    def test_module_and_visual_policy_switches_are_independent(self):
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_design_enabled=False, site_modules_enabled=True)
        self.assertEqual(self.request().status_code, 302)
        self.assertEqual(JournalSiteAIDesignRun.objects.get().status, "failed")
        self.assertEqual(self.transport.call_count, 1)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_design_enabled=True, site_modules_enabled=False)
        self.assertEqual(self.request().status_code, 409)
        self.assertEqual(self.transport.call_count, 1)

    def test_only_authorised_requester_can_view_preview_or_apply(self):
        run = self.completed()
        for user in (self.other_same, self.other, self.author):
            self.client.force_login(user)
            self.assertEqual(self.client.get(self.result(run)).status_code, 404)
            self.assertEqual(self.client.get(self.preview(run)).status_code, 404)
            self.assertEqual(self.client.post(self.apply(run), {"revision":"1", "confirm":"yes"}).status_code, 404)
        self.assertEqual(Client().get(self.result(run)).status_code, 302)
        Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.SITE_MANAGER).update(active=False)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(self.result(run)).status_code, 404)

    def test_stale_or_tampered_suggestions_never_change_draft(self):
        run = self.completed()
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(headline="Changed without revision")
        self.assertContains(self.client.get(self.result(run)), "The site draft changed")
        self.assertEqual(self.client.get(self.preview(run)).status_code, 409)
        self.assertEqual(self.client.post(self.apply(run), {"revision":"1", "confirm":"yes"}).status_code, 409)
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(headline="PRIVATE HEADLINE")
        JournalSiteAIDesignRun.objects.filter(pk=run.pk).update(options=[{"modules":[{"id":"m0", "kind":"<script>", "visible":True}]}])
        self.assertEqual(self.client.get(self.result(run)).status_code, 404)
        self.assertEqual(self.client.get(self.preview(run)).status_code, 404)
        self.assertEqual(self.client.post(self.apply(run), {"revision":"1", "confirm":"yes"}).status_code, 404)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, SECTIONS)

    def test_module_text_change_without_revision_invalidates_preview(self):
        run = self.completed()
        changed = [{**SECTIONS[0], "body":"changed"}, *SECTIONS[1:]]
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(sections_config=changed)
        self.assertEqual(self.client.get(self.preview(run)).status_code, 409)
        self.assertEqual(self.client.post(self.apply(run), {"revision":"1", "confirm":"yes"}).status_code, 409)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config, changed)

    def test_visibility_changes_retain_hidden_module_content(self):
        run = self.completed()
        self.assertEqual(self.client.post(self.apply(run, 1), {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db()
        self.assertEqual(self.site.sections_config[0]["body"], "CONFIDENTIAL CUSTOM BODY")
        self.assertTrue(self.site.sections_config[0]["visible"])
        self.assertFalse(self.site.sections_config[1]["visible"])
        self.assertEqual(self.site.sections_config[1]["body"], "CONFIDENTIAL BODY A")
        preview = self.client.get(reverse("preview_journal_site", args=[self.journal.slug]))
        self.assertContains(preview, "CONFIDENTIAL CUSTOM BODY")
        self.assertNotContains(preview, "CONFIDENTIAL BODY A")

    def test_legacy_site_uses_two_default_modules_without_sending_site_text(self):
        JournalSiteDraft.objects.filter(pk=self.site.pk).update(sections_config=[])
        self.transport.side_effect = None
        self.transport.return_value = provider_response([{"modules":[
            {"id":"m1", "kind":"archive", "visible":True}, {"id":"m0", "kind":"about", "visible":True},
            {"id":"new", "kind":"scope", "visible":True}]}])
        self.assertEqual(self.request().status_code, 302)
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.status, "completed")
        sent = json.loads(json.loads(self.transport.call_args.args[0].data)["input"])
        self.assertEqual(sent["current_modules"], DESCRIPTORS[:2])
        self.assertNotIn("PRIVATE INTRO", str(sent))

    def test_noop_suggestion_does_not_increment_site_revision(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response([{"modules":DESCRIPTORS}])
        self.assertEqual(self.request().status_code, 302)
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(self.client.post(self.apply(run), {"revision":"1", "confirm":"yes"}).status_code, 302)
        self.site.refresh_from_db(); run.refresh_from_db()
        self.assertEqual(self.site.revision, 1)
        self.assertEqual(run.applied_revision, 1)
        self.assertEqual(self.site.sections_config, SECTIONS)

    def test_policy_or_role_change_before_dispatch_cancels(self):
        def disable(**kwargs):
            JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_modules_enabled=False)
        with patch("publishing.site_ai_module_views.JournalSiteEvent.objects.create", side_effect=disable):
            self.assertEqual(self.request().status_code, 302)
        self.assertEqual(JournalSiteAIDesignRun.objects.get().status, "cancelled")
        self.transport.assert_not_called()
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(site_modules_enabled=True)
        def revoke(**kwargs):
            Membership.objects.filter(user=self.manager, journal=self.journal, role=Membership.Role.SITE_MANAGER).update(active=False)
        with patch("publishing.site_ai_module_views.JournalSiteEvent.objects.create", side_effect=revoke):
            self.assertEqual(self.request().status_code, 302)
        self.assertEqual(JournalSiteAIDesignRun.objects.filter(status="cancelled").count(), 2)
        self.transport.assert_not_called()

    def test_quota_shared_with_visual_design_and_identifier_not_reused(self):
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        visual_data = {"request_id":str(uuid.uuid4()), "revision":"1", "fingerprint":page.context["fingerprint"],
            "policy_revision":"1", "model":"test-model", "brief":"Visual design", "confirm":"yes", "synthetic":"yes"}
        visual_url = reverse("request_site_ai_design", args=[self.journal.slug])
        self.assertEqual(self.client.post(visual_url, visual_data).status_code, 302)
        self.assertEqual(self.request(request_id=visual_data["request_id"]).status_code, 409)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(journal_daily_limit=1)
        self.assertEqual(self.request().status_code, 429)
        self.assertEqual(self.transport.call_count, 1)

    def test_module_attempt_counts_against_visual_quota_and_cannot_reuse_identifier(self):
        response = self.request()
        self.assertEqual(response.status_code, 302)
        run = JournalSiteAIDesignRun.objects.get()
        page = self.client.get(reverse("manage_journal_site", args=[self.journal.slug]))
        data = {"request_id":str(run.pk), "revision":"1", "fingerprint":page.context["fingerprint"],
                "policy_revision":"1", "model":"test-model", "brief":"Visual design", "confirm":"yes", "synthetic":"yes"}
        url = reverse("request_site_ai_design", args=[self.journal.slug])
        self.assertEqual(self.client.post(url, data).status_code, 409)
        JournalAIPolicy.objects.filter(pk=self.policy.pk).update(journal_daily_limit=1)
        data["request_id"] = str(uuid.uuid4())
        self.assertEqual(self.client.post(url, data).status_code, 429)
        self.assertEqual(self.transport.call_count, 1)

    def test_failed_response_not_retried_or_exposed(self):
        self.transport.side_effect = None
        self.transport.return_value = provider_response([{"modules":[{"id":"m0", "kind":"about", "visible":True}]}])
        response = self.request()
        run = JournalSiteAIDesignRun.objects.get()
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.options, [])
        self.assertContains(self.client.get(response.url), "No automatic retry")
        self.assertEqual(self.transport.call_count, 1)

    def test_csrf_and_method_restrictions(self):
        run = self.completed()
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.manager)
        self.assertEqual(client.post(reverse("request_site_ai_modules", args=[self.journal.slug]), {}).status_code, 403)
        self.assertEqual(client.post(self.apply(run), {"confirm":"yes", "revision":"1"}).status_code, 403)
        self.assertEqual(client.get(self.apply(run)).status_code, 405)

    def test_chinese_interface(self):
        run = self.completed()
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        self.assertContains(self.client.get(reverse("manage_journal_site", args=[self.journal.slug])), "AI 模块编排")
        self.assertContains(self.client.get(self.result(run)), "AI 模块建议")


class ModuleProposalValidationTests(TestCase):
    def test_existing_content_is_preserved_and_new_modules_are_empty(self):
        self.assertEqual(describe_sections(SECTIONS), DESCRIPTORS)
        result = materialize_option(ARRANGEMENT, SECTIONS)
        self.assertEqual(result[1], SECTIONS[0])
        self.assertEqual(result[3], SECTIONS[2])
        self.assertEqual(result[2], {"kind":"authors", "heading":"", "body":"", "visible":True})
        source = [{**SECTIONS[0], "heading":"  spaced heading  ", "body":"\n exact body \n"}, *SECTIONS[1:]]
        preserved = materialize_option(ARRANGEMENT, source)
        self.assertEqual(preserved[1]["heading"], source[0]["heading"])
        self.assertEqual(preserved[1]["body"], source[0]["body"])

    def test_invalid_proposals_are_rejected(self):
        bad = [[], [{"modules":[]}], [{"modules":ARRANGEMENT["modules"][:-1]}],
            [{"modules":ARRANGEMENT["modules"] + [ARRANGEMENT["modules"][0]]}],
            [{"modules":[{**x, "visible":False} for x in ARRANGEMENT["modules"]]}],
            [{"modules":[{**x, "kind":"scope"} if x["id"] == "m0" else x for x in ARRANGEMENT["modules"]]}],
            [{"modules":ARRANGEMENT["modules"] + [{"id":"new", "kind":"custom", "visible":True}]}],
            [{"modules":ARRANGEMENT["modules"] + [{"id":"new", "kind":"authors", "visible":True}]}],
            [{"modules":ARRANGEMENT["modules"] + [{"id":"new", "kind":"about", "visible":True}]}],
            [{"modules":ARRANGEMENT["modules"] + [{"id":"m7", "kind":"scope", "visible":True}]}],
            [{"modules":[{**x, "script":"bad"} for x in ARRANGEMENT["modules"]]}],
            [ARRANGEMENT, ARRANGEMENT]]
        for options in bad:
            with self.subTest(options=options), self.assertRaises(ProviderError):
                parse_response(provider_response(options), DESCRIPTORS)

    def test_gateway_rejects_extra_fields_and_unvalidated_module_details(self):
        valid = {"task":SITE_MODULES, "provider":OPENAI, "model":"test-model", "api_key":"fictional-key"}
        with patch("publishing.ai_gateway.request_module_suggestions", return_value=[]) as adapter:
            self.assertEqual(run_ai_task(**valid, payload={"brief":"Fictional", "modules":DESCRIPTORS}), [])
            adapter.assert_called_once_with(api_key="fictional-key", model="test-model", brief="Fictional", modules=DESCRIPTORS)
            for payload in ({"brief":"Fictional", "modules":DESCRIPTORS, "manuscript":"private"},
                {"brief":"Fictional", "modules":[{**DESCRIPTORS[0], "body":"private"}]},
                {"brief":"Fictional", "modules":[{"id":"m9", "kind":"about", "visible":True}]}, {}):
                with self.subTest(payload=payload), self.assertRaises(GatewayRejected):
                    run_ai_task(**valid, payload=payload)
            self.assertEqual(adapter.call_count, 1)
