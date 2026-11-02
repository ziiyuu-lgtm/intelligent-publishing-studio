import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import GatewayRejected, OPENAI, PUBLISHER_PAGE, SITE_PAGE, run_ai_task
from publishing.ai_provider import ProviderError
from publishing.models import (JournalAIPolicy, JournalSiteAIDesignRun, JournalSiteRelease, Membership,
                               OrganisationAIPolicy, OrganisationMembership, OrganisationPortalAIDesignRun,
                               OrganisationPortalRelease)
from publishing.page_ai_provider import PROMPT_VERSION, parse_response, validate_options
from publishing.page_design import InvalidPageDesign, validate_page_design
from . import test_site_pages as page_helpers


STYLE = {"layout":"image_left", "width":72, "background":"#f4f8f2", "accent":"#245a67", "alignment":"left"}
OPTION = {"title":"Suggested fictional page", "body":"[Verify the journal scope]\nReview all factual details.", "design":STYLE}
OPTION_TWO = {"title":"Alternative page", "body":"[Confirm editorial information]", "design":{
    "layout":"image_right", "width":90, "background":"#fff9ef", "accent":"#763f42", "alignment":"center"}}


def provider_response(options=None):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options":options if options is not None else [OPTION, OPTION_TWO]})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class IndependentPageAITests(TestCase):
    setUpTestData = page_helpers.SitePageTests.__dict__["setUpTestData"]
    owner = page_helpers.SitePageTests.owner
    route = page_helpers.SitePageTests.route
    login = page_helpers.SitePageTests.login
    token = page_helpers.SitePageTests.token
    upload = page_helpers.SitePageTests.upload
    publish = page_helpers.SitePageTests.publish
    add_image = page_helpers.SitePageTests.add_image
    page_url = page_helpers.SitePageTests.page_url
    pages_token = page_helpers.SitePageTests.pages_token
    payload = page_helpers.SitePageTests.payload
    save_pages = page_helpers.SitePageTests.save_pages
    page = staticmethod(page_helpers.SitePageTests.page)

    def setUp(self):
        self.jpolicy = JournalAIPolicy.objects.create(journal=self.journal, site_pages_enabled=True, journal_daily_limit=10)
        self.ppolicy = OrganisationAIPolicy.objects.create(organisation=self.org, portal_pages_enabled=True, portal_daily_limit=10)
        env = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"})
        env.start(); self.addCleanup(env.stop)
        transport = patch("publishing.ai_provider._send_request", return_value=provider_response())
        self.transport = transport.start(); self.addCleanup(transport.stop)

    def ai_url(self, kind, action, run_id=None, index=None):
        names = {("journal", "request"):"request_journal_page_ai", ("journal", "result"):"journal_page_ai_result",
                 ("journal", "preview"):"preview_journal_page_ai", ("journal", "apply"):"apply_journal_page_ai",
                 ("journal", "preview_edited"):"preview_edited_journal_page_ai",
                 ("portal", "request"):"request_publisher_page_ai", ("portal", "result"):"publisher_page_ai_result",
                 ("portal", "preview"):"preview_publisher_page_ai", ("portal", "apply"):"apply_publisher_page_ai",
                 ("portal", "preview_edited"):"preview_edited_publisher_page_ai"}
        args = [self.owner(kind)[1]]
        if run_id is not None:
            args.append(run_id)
        if index is not None:
            args.append(index)
        return reverse(names[(kind, action)], args=args)

    def ai_data(self, kind, **changes):
        self.login(kind)
        page = self.client.get(self.page_url(kind, "manage"))
        self.assertEqual(page.status_code, 200)
        data = {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"],
            "policy_revision":str(page.context["ai_policy_revision"]), "model":"test-model", "request_id":str(uuid.uuid4()),
            "page_slug":"private-page-name", "brief":"A fictional author guide in a calm style", "confirm":"yes", "synthetic":"yes"}
        data.update(changes)
        return data

    def make_page(self, kind, with_image=True):
        asset = self.add_image(kind, "SECRET IMAGE DESCRIPTION") if with_image else None
        entry = self.page(asset, slug="private-page-name", title="SECRET PAGE TITLE")
        entry["body"] = "SECRET PAGE BODY"
        self.assertEqual(self.save_pages(kind, [entry]).status_code, 302)
        return asset

    def test_preview_apply_and_publish_keep_source_private_until_confirmed(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.make_page(kind)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]
                release_model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                original = release_model.objects.get(site=site)
                before_calls = self.transport.call_count
                data = self.ai_data(kind)
                response = self.client.post(self.ai_url(kind, "request"), data)
                self.assertEqual(response.status_code, 302)
                run_model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = run_model.objects.get(pk=data["request_id"])
                self.assertEqual(run.status, "completed")
                self.assertEqual(run.target_page_slug, "private-page-name")
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                self.assertEqual(self.transport.call_count, before_calls + 1)
                wire = self.transport.call_args.args[0].data.decode()
                payload = json.loads(wire)
                self.assertFalse(payload["store"])
                self.assertTrue(payload["text"]["format"]["strict"])
                self.assertEqual(json.loads(payload["input"]), {"brief":data["brief"], "has_image":True,
                    "site_kind":"journal" if kind == "journal" else "publisher"})
                for secret in ("SECRET PAGE TITLE", "SECRET PAGE BODY", "SECRET IMAGE DESCRIPTION", "private-page-name", str(asset.pk)):
                    self.assertNotIn(secret, wire)
                self.assertNotIn(data["brief"], str(run_model.objects.filter(pk=run.pk).values().get()))
                legacy_result_name = "site_ai_result" if kind == "journal" else "publisher_ai_result"
                legacy_result = reverse(legacy_result_name, args=[self.owner(kind)[1], run.pk])
                self.assertRedirects(self.client.get(legacy_result), self.ai_url(kind, "result", run.pk), fetch_redirect_response=False)
                self.assertContains(self.client.get(self.route(kind, "manage")), self.ai_url(kind, "result", run.pk))
                self.assertContains(self.client.get(self.ai_url(kind, "result", run.pk)), "SECRET PAGE BODY")
                preview = self.client.get(self.ai_url(kind, "preview", run.pk, 0))
                self.assertContains(preview, "Suggested fictional page")
                self.assertContains(preview, "--page-bg:#f4f8f2")
                self.assertContains(preview, self.route(kind, "private", asset.pk))
                self.assertEqual(preview["X-Robots-Tag"], "noindex, nofollow, noarchive")
                public = Client()
                public_page = self.page_url(kind, "public", "private-page-name")
                self.assertContains(public.get(public_page), "SECRET PAGE BODY")
                site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["body"], "SECRET PAGE BODY")
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run.pk, 0), {"revision":site.revision}).status_code, 400)
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run.pk, 0),
                    {"revision":site.revision, "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                saved = site.pages_config[0]
                self.assertEqual((saved["slug"], saved["title"], saved["body"], saved["design"]),
                                 ("private-page-name", OPTION["title"], OPTION["body"], STYLE))
                self.assertEqual(saved["image_asset_id"], str(asset.pk))
                self.assertTrue(saved["visible"])
                self.assertContains(public.get(public_page), "SECRET PAGE BODY")
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run.pk, 0),
                    {"revision":site.revision, "confirm":"yes"}).status_code, 409)
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(public_page), "Suggested fictional page")
                self.assertContains(public.get(public_page), "--page-bg:#f4f8f2")
                self.assertEqual(self.client.post(self.route(kind, "restore", original.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertContains(self.client.get(self.page_url(kind, "preview", "private-page-name")), "SECRET PAGE BODY")
                self.assertContains(public.get(public_page), "Suggested fictional page")

    def test_manual_design_edit_and_old_form_protection(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.make_page(kind, with_image=False)
                data = self.ai_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                run_id = data["request_id"]
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run_id, 0),
                    {"revision":site.revision, "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                entry = dict(site.pages_config[0])
                self.assertEqual(self.save_pages(kind, [entry]).status_code, 400)
                data = self.payload(kind, [entry])
                data.update({"pages-0-design_form_present":"yes", "pages-0-design_enabled":"on",
                    "pages-0-design_layout":"image_right", "pages-0-design_width":"87",
                    "pages-0-design_background":"#fefefe", "pages-0-design_accent":"#123abc",
                    "pages-0-design_alignment":"center"})
                self.assertEqual(self.client.post(self.page_url(kind, "save"), data).status_code, 302)
                site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["design"], {"layout":"image_right", "width":87,
                    "background":"#fefefe", "accent":"#123abc", "alignment":"center"})
                preview = self.client.get(self.page_url(kind, "preview", "private-page-name"))
                self.assertContains(preview, "--page-width:87%")
                self.assertContains(preview, "--page-accent:#123abc")

    def test_edit_and_preview_proposal_before_applying(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.make_page(kind)
                data = self.ai_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                edited = {"revision":str(site.revision), "title":"Reviewed page title", "body":"Edited by the manager.",
                    "layout":"image_right", "width":"83", "background":"#eafafa", "accent":"#285472", "alignment":"center"}
                response = self.client.post(self.ai_url(kind, "preview_edited", data["request_id"], 0), edited)
                self.assertContains(response, "Reviewed page title")
                self.assertContains(response, "Edited by the manager.")
                self.assertContains(response, "--page-width:83%")
                self.assertContains(response, 'name="confirm"')
                site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["title"], "SECRET PAGE TITLE")
                safe_text = {**edited, "body":"Contact https://example.test and <script>bad</script>"}
                safe_preview = self.client.post(self.ai_url(kind, "preview_edited", data["request_id"], 0), safe_text)
                self.assertContains(safe_preview, "&lt;script&gt;bad&lt;/script&gt;")
                self.assertNotContains(safe_preview, "<script>bad</script>")
                invalid = {**edited, "width":"40"}
                self.assertEqual(self.client.post(self.ai_url(kind, "preview_edited", data["request_id"], 0), invalid).status_code, 400)
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", data["request_id"], 0),
                    {**invalid, "confirm":"yes"}).status_code, 400)
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", data["request_id"], 0),
                    {**edited, "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                self.assertEqual((site.pages_config[0]["title"], site.pages_config[0]["body"]),
                                 ("Reviewed page title", "Edited by the manager."))
                self.assertEqual(site.pages_config[0]["image_asset_id"], str(asset.pk))
                run_model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = run_model.objects.get(pk=data["request_id"])
                self.assertEqual(run.applied_option_content["title"], "Reviewed page title")
                self.assertEqual(run.applied_option_content["design"]["width"], 83)
                self.assertContains(self.client.get(self.ai_url(kind, "result", run.pk)), "Reviewed page title")

    def test_policy_confirmation_staleness_roles_and_limits(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.make_page(kind, with_image=False)
                data = self.ai_data(kind)
                url = self.ai_url(kind, "request")
                for change, status in (({"confirm":""},400), ({"synthetic":""},400), ({"brief":""},400),
                    ({"brief":"x"*801},400), ({"model":"other"},409), ({"policy_revision":"0"},409),
                    ({"fingerprint":"stale"},409), ({"revision":"999"},409)):
                    self.assertEqual(self.client.post(url, {**data, **change}).status_code, status)
                policy = self.jpolicy if kind == "journal" else self.ppolicy
                field = "site_pages_enabled" if kind == "journal" else "portal_pages_enabled"
                type(policy).objects.filter(pk=policy.pk).update(**{field:False})
                self.assertEqual(self.client.post(url, data).status_code, 409)
                type(policy).objects.filter(pk=policy.pk).update(**{field:True})
                self.assertEqual(self.transport.call_count, 0)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.post(url, data).status_code, 404)
                self.login(kind)
                csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(url, data).status_code, 403)
                self.assertEqual(self.client.post(url, data).status_code, 302)
                run_id = data["request_id"]
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(self.ai_url(kind, "result", run_id)).status_code, 404)
                self.assertEqual(self.client.get(self.ai_url(kind, "preview", run_id, 0)).status_code, 404)
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run_id, 0), {"confirm":"yes", "revision":data["revision"]}).status_code, 404)
                self.login(kind)
                self.assertEqual(self.client.post(url, {**data, "page_slug":"different-page"}).status_code, 409)
                # A later manual edit invalidates preview and application of the old suggestion.
                entry = self.page(slug="private-page-name", title="Changed by manager")
                self.assertEqual(self.save_pages(kind, [entry]).status_code, 302)
                self.assertEqual(self.client.get(self.ai_url(kind, "preview", run_id, 0)).status_code, 409)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run_id, 0),
                    {"revision":site.revision, "confirm":"yes"}).status_code, 409)
                self.transport.reset_mock()

    def test_revoked_policy_before_dispatch_cancels_without_network(self):
        self.make_page("journal", with_image=False)
        data = self.ai_data("journal")
        with patch("publishing.site_page_ai_views._provider_configured", side_effect=[True, True, False]):
            response = self.client.post(self.ai_url("journal", "request"), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(JournalSiteAIDesignRun.objects.get(pk=data["request_id"]).status, "cancelled")
        self.transport.assert_not_called()

    def test_ai_application_does_not_make_a_hidden_page_public(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Hidden page image")
                entry = self.page(asset, slug="private-page-name", visible=False, title="Private page")
                self.assertEqual(self.save_pages(kind, [entry]).status_code, 302)
                data = self.ai_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                self.assertEqual(self.client.get(self.ai_url(kind, "preview", data["request_id"], 0)).status_code, 200)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", data["request_id"], 0),
                    {"revision":site.revision, "confirm":"yes"}).status_code, 302)
                site.refresh_from_db()
                self.assertFalse(site.pages_config[0]["visible"])
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertEqual(Client().get(self.page_url(kind, "public", "private-page-name")).status_code, 404)
                self.assertEqual(Client().get(self.route(kind, "public_image", asset.pk)).status_code, 404)

    def test_page_attempts_share_the_account_limit(self):
        self.make_page("journal", with_image=False)
        for _ in range(3):
            self.assertEqual(self.client.post(self.ai_url("journal", "request"), self.ai_data("journal")).status_code, 302)
        self.assertEqual(self.transport.call_count, 3)
        self.assertEqual(self.client.post(self.ai_url("journal", "request"), self.ai_data("journal")).status_code, 429)
        self.assertEqual(self.transport.call_count, 3)

    def test_results_are_requester_scoped_and_role_revocation_is_enforced(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.make_page(kind, with_image=False)
                data = self.ai_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                if kind == "journal":
                    other = self.other_journal_manager
                    Membership.objects.create(user=other, journal=self.journal, role=Membership.Role.SITE_MANAGER)
                    revoke = Membership.objects.filter(user=self.journal_manager, journal=self.journal, role=Membership.Role.SITE_MANAGER)
                else:
                    other = self.other_portal_manager
                    OrganisationMembership.objects.create(user=other, organisation=self.org, role=OrganisationMembership.Role.PORTAL_MANAGER)
                    revoke = OrganisationMembership.objects.filter(user=self.portal_manager, organisation=self.org, role=OrganisationMembership.Role.PORTAL_MANAGER)
                self.client.force_login(other)
                self.assertEqual(self.client.get(self.page_url(kind, "manage")).status_code, 200)
                self.assertEqual(self.client.get(self.ai_url(kind, "result", data["request_id"])).status_code, 404)
                self.assertEqual(self.client.get(self.ai_url(kind, "preview", data["request_id"], 0)).status_code, 404)
                revoke.update(active=False)
                self.login(kind)
                self.assertEqual(self.client.get(self.ai_url(kind, "result", data["request_id"])).status_code, 404)
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", data["request_id"], 0),
                    {"revision":data["revision"], "confirm":"yes"}).status_code, 404)
    def test_new_policy_is_independent_of_existing_ai_tasks(self):
        self.assertTrue(self.jpolicy.site_pages_enabled)
        self.assertFalse(self.jpolicy.site_design_enabled)
        self.assertFalse(self.jpolicy.site_modules_enabled)
        self.assertFalse(self.jpolicy.metadata_enabled)
        self.assertTrue(self.ppolicy.portal_pages_enabled)
        self.assertFalse(self.ppolicy.portal_design_enabled)
        self.assertFalse(self.ppolicy.portal_modules_enabled)

    def test_response_validation_and_gateway_reject_extra_input(self):
        self.assertEqual(parse_response(provider_response()), [OPTION, OPTION_TWO])
        bad_options = [[], [OPTION, OPTION], [{**OPTION, "body":"<script>alert(1)</script>"}],
            [{**OPTION, "body":"Visit https://example.test"}], [{**OPTION, "title":" "}],
            [{**OPTION, "body":"x"*2501}], [{**OPTION, "unknown":"private"}],
            [{**OPTION, "design":{**STYLE, "width":True}}], [{**OPTION, "design":{**STYLE, "background":"red;url(x)"}}]]
        for value in bad_options:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_options(value)
        with self.assertRaises(ProviderError):
            parse_response({"status":"incomplete", "output":[]})
        with self.assertRaises(ProviderError):
            parse_response({"status":"completed", "output":[{"type":"message", "role":"assistant",
                "content":[{"type":"refusal", "refusal":"No"}]}]})
        for invalid in (None, {}, {**STYLE, "width":40}, {**STYLE, "layout":"javascript"}):
            with self.subTest(style=invalid), self.assertRaises(InvalidPageDesign):
                validate_page_design(invalid)
        for task in (SITE_PAGE, PUBLISHER_PAGE):
            with patch("publishing.ai_gateway.request_page_suggestions", return_value=[]) as adapter:
                self.assertEqual(run_ai_task(task=task, provider=OPENAI, model="test-model", api_key="key",
                    payload={"brief":"fictional", "has_image":False}), [])
                with self.assertRaises(GatewayRejected):
                    run_ai_task(task=task, provider=OPENAI, model="test-model", api_key="key",
                        payload={"brief":"fictional", "has_image":False, "body":"private"})
                adapter.assert_called_once()

    def test_invalid_provider_response_is_recorded_as_failure(self):
        self.make_page("portal", with_image=False)
        self.transport.return_value = provider_response([{**OPTION, "body":"Contact admin@example.com"}])
        data = self.ai_data("portal")
        self.assertEqual(self.client.post(self.ai_url("portal", "request"), data).status_code, 302)
        run = OrganisationPortalAIDesignRun.objects.get(pk=data["request_id"])
        self.assertEqual((run.status, run.error_code, run.options), ("failed", "provider_unavailable", []))
        self.assertNotIn("admin@example.com", str(run.options))
        self.assertEqual(self.client.get(self.ai_url("portal", "preview", run.pk, 0)).status_code, 404)

    def test_page_ai_interface_in_simplified_chinese(self):
        self.make_page("journal", with_image=False)
        self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
        page = self.client.get(self.page_url("journal", "manage"))
        self.assertContains(page, "AI 页面建议")
        self.assertContains(page, "页面强调颜色")
        data = self.ai_data("journal")
        self.assertEqual(self.client.post(self.ai_url("journal", "request"), data).status_code, 302)
        result = self.client.get(self.ai_url("journal", "result", data["request_id"]))
        self.assertContains(result, "预览修改后的建议")
