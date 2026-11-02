import json
import os
import uuid
from unittest.mock import patch

from django.test import Client, TestCase, override_settings
from django.urls import reverse

from publishing.ai_gateway import GatewayRejected, OPENAI, SITE_PAGE_BLOCKS, run_ai_task
from publishing.ai_provider import ProviderError
from publishing.models import (JournalAIPolicy, JournalSiteAIDesignRun, OrganisationAIPolicy, OrganisationPortalAIDesignRun)
from publishing.page_block_ai_provider import parse_response, validate_options
from . import test_page_blocks as helpers

OPTIONS = [{"blocks":[{"kind":"callout", "heading":"Suggested focus", "body":"[Verify policy details]"},
                      {"kind":"image", "heading":"Suggested image area", "body":"[Choose a fictional image]"}]},
           {"blocks":[{"kind":"text", "heading":"Alternative", "body":"[Verify introduction]"}]}]


def response(options=OPTIONS):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options":options})}]}]}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class PageBlockAITests(TestCase):
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
        self.jpolicy = JournalAIPolicy.objects.create(journal=self.journal, site_page_blocks_enabled=True, journal_daily_limit=10)
        self.ppolicy = OrganisationAIPolicy.objects.create(organisation=self.org, portal_page_blocks_enabled=True, portal_daily_limit=10)
        env = patch.dict(os.environ, {"OPENAI_API_KEY":"fictional-key"}); env.start(); self.addCleanup(env.stop)
        transport = patch("publishing.ai_provider._send_request", return_value=response())
        self.transport = transport.start(); self.addCleanup(transport.stop)

    def ai_url(self, kind, action, run_id=None, index=None, page_slug="author-guidelines"):
        site = "journal" if kind == "journal" else "publisher"
        name = f"{action}_{site}_page_block_ai" if action in ("request", "preview", "edit", "apply") else f"{site}_page_block_ai_result"
        args = [self.owner(kind)[1], page_slug if action == "request" else run_id]
        if index is not None:
            args.append(index)
        return reverse(name, args=args)

    def request_data(self, kind, slug="author-guidelines"):
        self.login(kind)
        page = self.client.get(self.block_url(kind, "manage", slug))
        self.assertEqual(page.status_code, 200)
        return {"revision":str(page.context["site"].revision), "fingerprint":page.context["fingerprint"],
                "policy_revision":str(page.context["ai_policy_revision"]), "model":"test-model", "request_id":str(uuid.uuid4()),
                "brief":"Create a fictional author information page", "confirm":"yes", "synthetic":"yes"}

    def preview_data(self, kind, run_id, *, edited=True):
        page = self.client.get(self.ai_url(kind, "result", run_id))
        self.assertEqual(page.status_code, 200)
        site = page.context["site"]
        return {"revision":str(site.revision), "choice_0-TOTAL_FORMS":"2", "choice_0-INITIAL_FORMS":"2",
                "choice_0-MIN_NUM_FORMS":"0", "choice_0-MAX_NUM_FORMS":"20",
                "choice_0-0-kind":"callout", "choice_0-0-heading":"Reviewed focus" if edited else "Suggested focus",
                "choice_0-0-body":"Reviewed fictional text", "choice_0-0-visible":"on", "choice_0-0-ORDER":"2",
                "choice_0-1-kind":"image", "choice_0-1-heading":"Reviewed image area",
                "choice_0-1-body":"Choose an image", "choice_0-1-visible":"on", "choice_0-1-ORDER":"1"}

    def test_generated_blocks_are_reviewable_private_append_only_and_snapshotted(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "PRIVATE IMAGE ALT")
                source = self.page(title="PRIVATE PAGE TITLE"); source["body"] = "PRIVATE PAGE BODY"
                self.assertEqual(self.save_pages(kind, [source]).status_code, 302)
                original = self.block("PRIVATE ORIGINAL BLOCK", asset, url="https://example.org/private-link")
                self.assertEqual(self.save_blocks(kind, [original]).status_code, 302)
                self.assertEqual(self.publish(kind).status_code, 302)
                data = self.request_data(kind)
                request_url = self.ai_url(kind, "request")
                self.assertEqual(self.client.post(request_url, data).status_code, 302)
                model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = model.objects.get(pk=data["request_id"])
                self.assertEqual(run.status, "completed")
                self.assertEqual(self.client.post(request_url, data).status_code, 302)
                wire = self.transport.call_args.args[0].data.decode()
                payload = json.loads(wire)
                self.assertFalse(payload["store"])
                self.assertTrue(payload["text"]["format"]["strict"])
                self.assertEqual(json.loads(payload["input"]), {"brief":data["brief"], "available_slots":19,
                    "site_kind":"journal" if kind == "journal" else "publisher"})
                for secret in ("PRIVATE", "private-link", "author-guidelines", str(asset.pk)):
                    self.assertNotIn(secret, wire)
                self.assertNotIn(data["brief"], str(model.objects.filter(pk=run.pk).values().get()))
                preview_url = self.ai_url(kind, "preview", run.pk, 0)
                self.assertEqual(Client().post(preview_url, self.preview_data(kind, run.pk)).status_code, 302)
                preview = self.client.post(preview_url, self.preview_data(kind, run.pk))
                self.assertContains(preview, "PRIVATE ORIGINAL BLOCK")
                self.assertContains(preview, "Reviewed focus")
                self.assertContains(preview, "Reviewed image area")
                self.assertLess(preview.content.index(b"Reviewed image area"), preview.content.index(b"Reviewed focus"))
                self.assertIn("no-store", preview["Cache-Control"])
                back = self.client.post(self.ai_url(kind, "edit", run.pk, 0),
                    {"revision":data["revision"], "reviewed_blocks":preview.context["block_reviewed"]})
                self.assertEqual(back.status_code, 200)
                self.assertEqual(back.context["choices"][0]["formset"].forms[0].initial["heading"], "Reviewed image area")
                self.assertEqual(back.context["choices"][0]["formset"].forms[1].initial["heading"], "Reviewed focus")
                public = Client()
                self.assertNotContains(public.get(self.page_url(kind, "public", "author-guidelines")), "Reviewed focus")
                site = self.owner(kind)[2]; site.refresh_from_db()
                applied = self.client.post(self.ai_url(kind, "apply", run.pk, 0),
                    {"revision":str(site.revision), "reviewed_blocks":preview.context["block_reviewed"], "confirm":"yes"})
                self.assertEqual(applied.status_code, 302)
                site.refresh_from_db(); run.refresh_from_db()
                self.assertEqual([b["heading"] for b in site.pages_config[0]["blocks"]],
                                 ["PRIVATE ORIGINAL BLOCK", "Reviewed image area", "Reviewed focus"])
                self.assertEqual(site.pages_config[0]["blocks"][0]["image_asset_id"], str(asset.pk))
                self.assertEqual(run.applied_option_content["blocks"][1]["heading"], "Reviewed focus")
                self.assertEqual(self.client.post(self.ai_url(kind, "apply", run.pk, 0),
                    {"revision":str(site.revision), "reviewed_blocks":preview.context["block_reviewed"], "confirm":"yes"}).status_code, 409)
                self.assertNotContains(public.get(self.page_url(kind, "public", "author-guidelines")), "Reviewed focus")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(self.page_url(kind, "public", "author-guidelines")), "Reviewed focus")
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 200)

    def test_independent_policy_confirmation_and_permissions(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                data = self.request_data(kind)
                url = self.ai_url(kind, "request")
                for key in ("confirm", "synthetic"):
                    self.assertEqual(self.client.post(url, {**data, key:""}).status_code, 400)
                policy = self.jpolicy if kind == "journal" else self.ppolicy
                field = "site_page_blocks_enabled" if kind == "journal" else "portal_page_blocks_enabled"
                setattr(policy, field, False); policy.save(update_fields=[field])
                self.assertEqual(self.client.post(url, data).status_code, 409)
                self.assertEqual(self.transport.call_count, 0)
                setattr(policy, field, True); policy.save(update_fields=[field])
                self.assertEqual(self.client.post(url, {**data, "fingerprint":"stale"}).status_code, 409)
                self.assertEqual(self.client.post(url, {**data, "policy_revision":"999"}).status_code, 409)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.post(url, data).status_code, 404)
                csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.owner(kind)[0])
                self.assertEqual(csrf.post(url, data).status_code, 403)

    def test_preview_and_apply_reject_tampering_staleness_and_invalid_provider(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                data = self.request_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                run_id = data["request_id"]
                pv = self.preview_data(kind, run_id)
                preview_url = self.ai_url(kind, "preview", run_id, 0)
                self.assertEqual(self.client.post(preview_url, {**pv, "choice_0-TOTAL_FORMS":"0"}).status_code, 409)
                self.assertEqual(self.client.post(preview_url, {**pv, "choice_0-0-DELETE":"on", "choice_0-1-DELETE":"on"}).status_code, 400)
                self.assertEqual(self.client.post(preview_url, {**pv, "choice_0-0-kind":"<script>"}).status_code, 400)
                reduced = self.client.post(preview_url, {**pv, "choice_0-0-DELETE":"on"})
                self.assertEqual(reduced.status_code, 200)
                back = self.client.post(self.ai_url(kind, "edit", run_id, 0),
                    {"revision":pv["revision"], "reviewed_blocks":reduced.context["block_reviewed"]})
                self.assertEqual(len(back.context["choices"][0]["formset"].forms), 1)
                reduced_data = {**pv, "choice_0-TOTAL_FORMS":"1", "choice_0-INITIAL_FORMS":"1"}
                self.assertEqual(self.client.post(preview_url, reduced_data).status_code, 200)
                preview = self.client.post(preview_url, pv)
                reviewed = json.loads(preview.context["block_reviewed"])
                reviewed[0]["image_asset_id"] = str(uuid.uuid4())
                apply_url = self.ai_url(kind, "apply", run_id, 0)
                self.assertEqual(self.client.post(apply_url, {"revision":pv["revision"], "confirm":"yes", "reviewed_blocks":json.dumps(reviewed)}).status_code, 400)
                self.assertEqual(self.client.post(apply_url, {"revision":pv["revision"], "reviewed_blocks":preview.context["block_reviewed"]}).status_code, 400)
                self.assertEqual(self.save_blocks(kind, [self.block("Later edit")]).status_code, 302)
                self.assertEqual(self.client.post(preview_url, pv).status_code, 409)
                self.assertEqual(self.client.post(apply_url, {"revision":pv["revision"], "confirm":"yes", "reviewed_blocks":preview.context["block_reviewed"]}).status_code, 409)

    def test_provider_validation_and_gateway_allowlist(self):
        self.assertEqual(validate_options(OPTIONS, 2), OPTIONS)
        bad = ([], [OPTIONS[0], OPTIONS[0]], [{"blocks":[]}], [{"blocks":[{"kind":"link", "heading":"x", "body":"y"}]}],
               [{"blocks":[{"kind":"text", "heading":"<script>x</script>", "body":"x"}]}],
               [{"blocks":[{"kind":"text", "heading":"Visit", "body":"https://example.org"}]}],
               [{"blocks":[{"kind":"text", "heading":"x", "body":"x"} for _ in range(3)]}])
        for options in bad:
            with self.subTest(options=options), self.assertRaises(ProviderError):
                validate_options(options, 2)
        with self.assertRaises(ProviderError):
            parse_response({"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[{"type":"refusal", "refusal":"No"}]}]}, 2)
        with self.assertRaises(GatewayRejected):
            run_ai_task(task=SITE_PAGE_BLOCKS, provider=OPENAI, model="test-model", api_key="fake",
                        payload={"brief":"test", "available_slots":2, "page_text":"SECRET"})

    def test_full_page_and_provider_failure_do_not_change_draft(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                entries = [self.block(f"Block {i}", kind="text") for i in range(20)]
                self.assertEqual(self.save_blocks(kind, entries).status_code, 302)
                data = self.request_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 409)
                self.transport.assert_not_called()
                entries[-1]["DELETE"] = True
                self.assertEqual(self.save_blocks(kind, entries).status_code, 302)
                data = self.request_data(kind)
                self.transport.return_value = response(OPTIONS)  # First option exceeds the single available slot.
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = model.objects.get(pk=data["request_id"])
                self.assertEqual((run.status, run.options), ("failed", []))
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(len(site.pages_config[0]["blocks"]), 19)
                self.transport.reset_mock()

    def test_request_history_is_private_and_new_controls_are_translated(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                self.client.post(reverse("set_language"), {"language":"zh-hans", "next":"/"})
                self.assertContains(self.client.get(self.block_url(kind, "manage")), "AI 页面区块建议")
                data = self.request_data(kind)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                run_id = data["request_id"]
                self.assertContains(self.client.get(self.block_url(kind, "manage")), self.ai_url(kind, "result", run_id))
                legacy = "site_ai_result" if kind == "journal" else "publisher_ai_result"
                self.assertRedirects(self.client.get(reverse(legacy, args=[self.owner(kind)[1], run_id])),
                                     self.ai_url(kind, "result", run_id), fetch_redirect_response=False)
                outsider = self.other_journal_manager if kind == "journal" else self.other_portal_manager
                self.client.force_login(outsider)
                self.assertEqual(self.client.get(self.ai_url(kind, "result", run_id)).status_code, 404)
