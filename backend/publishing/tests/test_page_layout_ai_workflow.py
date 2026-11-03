import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.models import JournalAIPolicy, JournalSiteAIDesignRun, OrganisationAIPolicy, OrganisationPortalAIDesignRun
from . import test_page_blocks as helpers

OPTIONS = [{"gap":18, "alignment":"stretch", "blocks":[
    {"id":"b2", "grid_span":4}, {"id":"b0", "grid_span":8}, {"id":"b1", "grid_span":12}]}]


def response(options=OPTIONS):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options":options})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PageLayoutWorkflowTests(TestCase):
    setUpTestData = helpers.PageBlockTests.__dict__["setUpTestData"]
    owner = helpers.PageBlockTests.owner
    route = helpers.PageBlockTests.route
    login = helpers.PageBlockTests.login
    token = helpers.PageBlockTests.token
    upload = helpers.PageBlockTests.upload
    publish = helpers.PageBlockTests.publish
    add_image = helpers.PageBlockTests.add_image
    page_url = helpers.PageBlockTests.page_url
    pages_token = helpers.PageBlockTests.pages_token
    payload = helpers.PageBlockTests.payload
    save_pages = helpers.PageBlockTests.save_pages
    page = staticmethod(helpers.PageBlockTests.page)
    block = staticmethod(helpers.PageBlockTests.block)
    block_url = helpers.PageBlockTests.block_url
    block_payload = helpers.PageBlockTests.block_payload
    save_blocks = helpers.PageBlockTests.save_blocks

    def setUp(self):
        self.jpolicy = JournalAIPolicy.objects.create(journal=self.journal, site_page_layout_enabled=True, journal_daily_limit=10)
        self.ppolicy = OrganisationAIPolicy.objects.create(organisation=self.org, portal_page_layout_enabled=True, portal_daily_limit=10)
        env = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"}); env.start(); self.addCleanup(env.stop)
        transport = patch("publishing.ai_provider._send_request", return_value=response())
        self.transport = transport.start(); self.addCleanup(transport.stop)

    def ai_url(self, kind, action, run_id=None, index=None):
        site = "journal" if kind == "journal" else "publisher"
        name = f"{action}_{site}_page_layout_ai" if action != "result" else f"{site}_page_layout_ai_result"
        args = [self.owner(kind)[1], "author-guidelines" if action == "request" else run_id]
        if index is not None:
            args.append(index)
        return reverse(name, args=args)

    def seed(self, kind):
        asset = self.add_image(kind, "PRIVATE ALT")
        self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
        blocks = [self.block("PRIVATE first", asset, True, url="https://example.org/secret"),
                  self.block("PRIVATE hidden", asset, False), self.block("PRIVATE last", kind="text")]
        self.assertEqual(self.save_blocks(kind, blocks).status_code, 302)
        return asset

    def request_data(self, kind):
        self.login(kind)
        page = self.client.get(self.block_url(kind, "manage"))
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"],
                "policy_revision":str(page.context["ai_policy_revision"]), "model":"test-model", "request_id":str(uuid.uuid4()),
                "brief":"Fictional scholarly layout", "requested_options":"2", "confirm":"yes", "synthetic":"yes"}

    def review_data(self, revision):
        return {"revision":revision, "settings_0-gap":"22", "settings_0-alignment":"start",
                "blocks_0-TOTAL_FORMS":"3", "blocks_0-INITIAL_FORMS":"3", "blocks_0-MIN_NUM_FORMS":"0", "blocks_0-MAX_NUM_FORMS":"20",
                "blocks_0-0-block_id":"b2", "blocks_0-0-grid_span":"4", "blocks_0-0-order":"2",
                "blocks_0-1-block_id":"b0", "blocks_0-1-grid_span":"8", "blocks_0-1-order":"1",
                "blocks_0-2-block_id":"b1", "blocks_0-2-grid_span":"12", "blocks_0-2-order":"3"}

    def test_layout_review_preview_apply_and_snapshot_for_both_scopes(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.seed(kind)
                self.assertEqual(self.publish(kind).status_code, 302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                original = site.pages_config[0]["blocks"]
                data = self.request_data(kind)
                url = self.ai_url(kind, "request")
                self.assertEqual(self.client.post(url, data).status_code, 302)
                self.assertEqual(self.client.post(url, data).status_code, 302)
                model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = model.objects.get(pk=data["request_id"])
                self.assertEqual(run.status, "completed")
                payload = json.loads(self.transport.call_args.args[0].data)
                wire = json.dumps(payload)
                for secret in ("PRIVATE", "secret", str(asset.pk), "author-guidelines"):
                    self.assertNotIn(secret, wire)
                sent = json.loads(payload["input"])
                self.assertEqual(sent["blocks"][1]["visible"], False)
                self.assertTrue(sent["blocks"][1]["has_image"])
                self.assertNotIn(data["brief"], str(model.objects.filter(pk=run.pk).values().get()))
                result = self.client.get(self.ai_url(kind, "result", run.pk))
                self.assertContains(result, "PRIVATE hidden")
                review = self.review_data(data["revision"])
                preview_url = self.ai_url(kind, "preview", run.pk, 0)
                preview = self.client.post(preview_url, review)
                self.assertEqual(preview.status_code, 200)
                self.assertContains(preview, "PRIVATE first")
                self.assertNotContains(preview, "PRIVATE hidden")
                self.assertContains(preview, "--block-grid-span:8")
                self.assertIn("no-store", preview["Cache-Control"])
                self.assertLess(preview.content.index(b"PRIVATE first"), preview.content.index(b"PRIVATE last"))
                token = preview.context["layout_reviewed"]
                edited = self.client.post(self.ai_url(kind, "edit", run.pk, 0), {"revision":data["revision"], "reviewed_layout":token})
                self.assertEqual(edited.status_code, 200)
                self.assertEqual(edited.context["choices"][0]["settings"].initial["gap"], 22)
                self.assertEqual(edited.context["choices"][0]["formset"].forms[0].initial["block_id"], "b2")
                self.assertEqual(edited.context["choices"][0]["formset"].forms[0].initial["order"], 2)
                again = self.client.post(preview_url, review)
                self.assertEqual(again.status_code, 200)
                self.assertEqual(again.context["page"]["block_layout"], {"gap":22, "alignment":"start"})
                public = Client()
                self.assertNotContains(public.get(self.page_url(kind, "public", "author-guidelines")), "--block-grid-span:8")
                apply_url = self.ai_url(kind, "apply", run.pk, 0)
                self.assertEqual(self.client.post(apply_url, {"revision":data["revision"], "reviewed_layout":token}).status_code, 400)
                self.assertEqual(self.client.post(apply_url, {"revision":data["revision"], "reviewed_layout":token, "confirm":"yes"}).status_code, 302)
                site.refresh_from_db(); run.refresh_from_db()
                self.assertEqual(site.pages_config[0]["block_layout"], {"gap":22, "alignment":"start"})
                self.assertEqual([b["heading"] for b in site.pages_config[0]["blocks"]], ["PRIVATE first", "PRIVATE last", "PRIVATE hidden"])
                self.assertEqual(site.pages_config[0]["blocks"][2], original[1])
                self.assertEqual(site.pages_config[0]["blocks"][0]["image_asset_id"], str(asset.pk))
                self.assertEqual(site.pages_config[0]["blocks"][0]["link_url"], "https://example.org/secret")
                self.assertEqual(run.applied_option_content["gap"], 22)
                self.assertEqual(self.client.post(apply_url, {"revision":str(site.revision), "reviewed_layout":token, "confirm":"yes"}).status_code, 409)
                self.assertNotContains(public.get(self.page_url(kind, "public", "author-guidelines")), "--block-grid-span:8")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(self.page_url(kind, "public", "author-guidelines")), "--block-grid-span:8")
                self.transport.reset_mock()

    def test_ai_canvas_uses_scoped_saved_content_without_another_request(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Private example image")
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                visible = self.block("Visible <title>", asset, True, url="https://example.org/private")
                visible["body"] = "Saved <script>alert(1)</script> text."
                visible["design"] = {"layout":"image_left", "width":100, "background":"#e2f1e7",
                    "accent":"#234f40", "alignment":"left", "padding":20, "radius":12, "gap":16}
                hidden = self.block("Hidden block", asset, False)
                hidden["body"] = "HIDDEN_CANVAS_BODY"
                last = self.block("Last block", kind="text")
                self.assertEqual(self.save_blocks(kind, [visible, hidden, last]).status_code, 302)
                data = self.request_data(kind)
                second = {"gap":12, "alignment":"start", "blocks":[
                    {"id":"b0", "grid_span":12}, {"id":"b2", "grid_span":6},
                    {"id":"b1", "grid_span":6}]}
                self.transport.return_value = response([OPTIONS[0], second])
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                self.assertEqual(self.transport.call_count, 1)
                wire = self.transport.call_args.args[0].data.decode()
                for value in ("Saved", "Visible", "example.org", str(asset.pk), "HIDDEN_CANVAS_BODY"):
                    self.assertNotIn(value, wire)
                result_url = self.ai_url(kind, "result", data["request_id"])
                result = self.client.get(result_url)
                self.assertContains(result, "Compare your suggestions")
                self.assertContains(result, 'href="#ai-layout-option-0"')
                self.assertContains(result, 'id="ai-layout-comparison-1"')
                self.assertContains(result, 'data-layout-content-mode="yes"', count=2)
                self.assertContains(result, 'data-layout-content')
                self.assertEqual(result.context["choices"][0]["rows"][0]["block"]["heading"], "Last block")
                self.assertEqual(result.context["choices"][1]["rows"][0]["block"]["heading"], "Visible <title>")
                self.assertContains(result, "Saved &lt;script&gt;alert(1)&lt;/script&gt; text.")
                self.assertNotContains(result, "<script>alert(1)</script>")
                self.assertContains(result, "--block-bg:#e2f1e7")
                self.assertContains(result, "block-layout-image_left")
                self.assertContains(result, self.route(kind, "private", asset.pk))
                self.assertNotContains(result, "HIDDEN_CANVAS_BODY")
                self.assertNotContains(result, "https://example.org/private")
                self.assertEqual(self.client.get(self.route(kind, "private", asset.pk)).status_code, 200)
                self.assertNotEqual(Client().get(self.route(kind, "private", asset.pk)).status_code, 200)
                self.assertIn("no-store", result["Cache-Control"])
                bad = self.client.post(self.ai_url(kind, "preview", data["request_id"], 0),
                    {**self.review_data(data["revision"]), "blocks_0-0-grid_span":"13"})
                self.assertEqual(bad.status_code, 400)
                self.assertContains(bad, "Saved &lt;script&gt;alert(1)&lt;/script&gt; text.", status_code=400)
                self.assertEqual(self.transport.call_count, 1)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(result_url).status_code, 404)
                self.transport.reset_mock()

    def test_access_consent_tampering_staleness_and_policy_revocation(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.seed(kind)
                data = self.request_data(kind)
                request_url = self.ai_url(kind, "request")
                for key in ("confirm", "synthetic"):
                    self.assertEqual(self.client.post(request_url, {**data, key:""}).status_code, 400)
                policy = self.jpolicy if kind == "journal" else self.ppolicy
                field = "site_page_layout_enabled" if kind == "journal" else "portal_page_layout_enabled"
                setattr(policy, field, False); policy.save(update_fields=[field])
                self.assertEqual(self.client.post(request_url, data).status_code, 409)
                self.transport.assert_not_called()
                setattr(policy, field, True); policy.save(update_fields=[field])
                self.assertEqual(self.client.post(request_url, {**data, "fingerprint":"stale"}).status_code, 409)
                self.assertEqual(self.client.post(request_url, data).status_code, 302)
                preview_url = self.ai_url(kind, "preview", data["request_id"], 0)
                edits = self.review_data(data["revision"])
                self.assertEqual(self.client.post(preview_url, {**edits, "blocks_0-TOTAL_FORMS":"2"}).status_code, 409)
                self.assertEqual(self.client.post(preview_url, {**edits, "blocks_0-2-block_id":"b2"}).status_code, 400)
                self.assertEqual(self.client.post(preview_url, {**edits, "blocks_0-2-order":"1"}).status_code, 400)
                self.assertEqual(self.client.post(preview_url, {**edits, "blocks_0-2-grid_span":"13"}).status_code, 400)
                pv = self.client.post(preview_url, edits)
                token = pv.context["layout_reviewed"]
                apply_url = self.ai_url(kind, "apply", data["request_id"], 0)
                self.assertEqual(self.client.post(apply_url, {"revision":data["revision"], "reviewed_layout":token+"x", "confirm":"yes"}).status_code, 400)
                self.assertEqual(self.client.post(apply_url, {"revision":data["revision"], "reviewed_layout":"", "confirm":"yes"}).status_code, 400)
                csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(apply_url, {"revision":data["revision"], "reviewed_layout":token, "confirm":"yes"}).status_code, 403)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(self.ai_url(kind, "result", data["request_id"])).status_code, 404)
                self.login(kind)
                setattr(policy, field, False); policy.revision += 1; policy.save(update_fields=[field, "revision"])
                self.assertEqual(self.client.post(apply_url, {"revision":data["revision"], "reviewed_layout":token, "confirm":"yes"}).status_code, 409)
                self.transport.reset_mock()

    def test_changed_draft_invalidates_a_previously_reviewed_layout(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.seed(kind)
                data = self.request_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                preview = self.client.post(self.ai_url(kind, "preview", data["request_id"], 0), self.review_data(data["revision"]))
                self.assertEqual(preview.status_code, 200)
                token = preview.context["layout_reviewed"]
                current = self.owner(kind)[2]; current.refresh_from_db()
                blocks = current.pages_config[0]["blocks"]
                blocks[0]["heading"] = "Newer draft"
                self.assertEqual(self.save_blocks(kind, blocks).status_code, 302)
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", data["request_id"], 0),
                    {"revision":data["revision"], "reviewed_layout":token, "confirm":"yes"}).status_code, 409)
                current.refresh_from_db()
                self.assertEqual(current.pages_config[0]["blocks"][0]["heading"], "Newer draft")
                self.transport.reset_mock()

    def test_empty_pages_and_bad_ai_responses_fail_closed(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                data = self.request_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 409)
                self.transport.assert_not_called()
                self.seed(kind)
                data = self.request_data(kind)
                self.transport.return_value = response([{"gap":1, "alignment":"start", "blocks":[{"id":"b0", "grid_span":12}]}])
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                self.assertEqual(model.objects.get(pk=data["request_id"]).status, "failed")
                self.transport.reset_mock()
