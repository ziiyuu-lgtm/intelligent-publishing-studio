import json
import uuid

from django.test import Client, TestCase, override_settings

from publishing.ai_provider import ProviderError
from publishing.block_design import block_appearance, validate_block_design
from publishing.models import JournalSiteAIDesignRun, JournalSiteRelease, OrganisationPortalAIDesignRun, OrganisationPortalRelease
from publishing.page_block_ai_provider import LEGACY_PROMPT_VERSION, PROMPT_VERSION, validate_options
from . import test_page_block_ai as ai_helpers
from . import test_page_blocks as page_helpers

STYLE = {"layout":"image_right", "width":74, "background":"#202340", "accent":"#E4B36B",
         "alignment":"center", "padding":32, "radius":18, "gap":26}
NORMAL_STYLE = {**STYLE, "accent":"#e4b36b"}


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True,
                   PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class ManualBlockDesignTests(TestCase):
    setUpTestData = page_helpers.PageBlockTests.__dict__["setUpTestData"]
    owner = page_helpers.PageBlockTests.owner
    route = page_helpers.PageBlockTests.route
    login = page_helpers.PageBlockTests.login
    token = page_helpers.PageBlockTests.token
    upload = page_helpers.PageBlockTests.upload
    publish = page_helpers.PageBlockTests.publish
    add_image = page_helpers.PageBlockTests.add_image
    page_url = page_helpers.PageBlockTests.page_url
    pages_token = page_helpers.PageBlockTests.pages_token
    payload = page_helpers.PageBlockTests.payload
    save_pages = page_helpers.PageBlockTests.save_pages
    page = staticmethod(page_helpers.PageBlockTests.page)
    block = staticmethod(page_helpers.PageBlockTests.block)
    block_url = page_helpers.PageBlockTests.block_url
    block_payload = page_helpers.PageBlockTests.block_payload
    save_blocks = page_helpers.PageBlockTests.save_blocks

    def test_arbitrary_colours_validated_dimensions_and_contrast(self):
        self.assertEqual(validate_block_design(STYLE), NORMAL_STYLE)
        self.assertEqual(block_appearance(STYLE)["text"], "#ffffff")
        self.assertEqual(block_appearance({**STYLE, "background":"#f7faf5"})["text"], "#000000")
        bad = [{**STYLE, "background":"#000000;display:none"}, {**STYLE, "accent":"url(x)"},
               {**STYLE, "layout":"evil"}, {**STYLE, "alignment":"justify;display:none"},
               {**STYLE, "width":True}, {**STYLE, "width":39}, {**STYLE, "width":101},
               {**STYLE, "padding":65}, {**STYLE, "radius":-1}, {**STYLE, "gap":65},
               {**STYLE, "extra":"x"}, {key:value for key,value in STYLE.items() if key != "gap"}]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_block_design(value)

    def test_manual_style_preview_published_snapshots_restore_and_legacy_form(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                asset = self.add_image(kind, "Fictional visual")
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                entry = {**self.block("Individually designed", asset), "design":STYLE}
                self.assertEqual(self.save_blocks(kind, [entry]).status_code, 302)
                site = self.owner(kind)[2]; site.refresh_from_db()
                self.assertEqual(site.pages_config[0]["blocks"][0]["design"], NORMAL_STYLE)
                preview = self.client.get(self.page_url(kind, "preview", "author-guidelines"))
                self.assertContains(preview, "block-layout-image_right")
                self.assertContains(preview, "--block-width:74%;--block-bg:#202340;--block-text:#ffffff;--block-accent:#e4b36b")
                self.assertContains(preview, "--block-padding:32px;--block-radius:18px;--block-gap:26px")
                self.assertEqual(self.publish(kind).status_code, 302)
                model = JournalSiteRelease if kind == "journal" else OrganisationPortalRelease
                release = model.objects.get(site=site)
                public = Client(); public_url = self.page_url(kind, "public", "author-guidelines")
                self.assertContains(public.get(public_url), "--block-bg:#202340")

                data = self.block_payload(kind, [entry])
                data.pop("sections-0-design_form_present")
                self.assertEqual(self.client.post(self.block_url(kind, "save"), data).status_code, 400)
                invalid = self.block_payload(kind, [entry]); invalid["sections-0-design_background"] = "#000000;display:none"
                self.assertEqual(self.client.post(self.block_url(kind, "save"), invalid).status_code, 400)
                site.refresh_from_db(); self.assertEqual(site.pages_config[0]["blocks"][0]["design"], NORMAL_STYLE)

                changed = {**entry, "design":{**STYLE, "background":"#f7faf5", "layout":"image_left", "width":93}}
                self.assertEqual(self.save_blocks(kind, [changed]).status_code, 302)
                self.assertContains(public.get(public_url), "--block-bg:#202340")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(public_url), "--block-bg:#f7faf5")
                self.assertContains(public.get(public_url), "block-layout-image_left")
                self.assertEqual(self.client.post(self.route(kind, "restore", release.pk),
                    {**self.token(kind), "confirm":"yes"}).status_code, 302)
                self.assertContains(self.client.get(self.page_url(kind, "preview", "author-guidelines")), "--block-bg:#202340")
                self.assertContains(public.get(public_url), "--block-bg:#f7faf5")
                self.assertEqual(self.publish(kind).status_code, 302)
                self.assertContains(public.get(public_url), "--block-bg:#202340")
                self.assertEqual(public.get(self.route(kind, "public_image", asset.pk)).status_code, 200)


@override_settings(DEBUG=True, IPS_LOCAL_PUBLICATION_ENABLED=True, IPS_OPENAI_ENABLED=True,
                   IPS_OPENAI_MODEL="test-model", PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
class AIBlockDesignTests(TestCase):
    setUpTestData = ai_helpers.PageBlockAITests.__dict__["setUpTestData"]
    setUp = ai_helpers.PageBlockAITests.setUp
    owner = ai_helpers.PageBlockAITests.owner
    route = ai_helpers.PageBlockAITests.route
    login = ai_helpers.PageBlockAITests.login
    token = ai_helpers.PageBlockAITests.token
    upload = ai_helpers.PageBlockAITests.upload
    publish = ai_helpers.PageBlockAITests.publish
    add_image = ai_helpers.PageBlockAITests.add_image
    page_url = ai_helpers.PageBlockAITests.page_url
    pages_token = ai_helpers.PageBlockAITests.pages_token
    payload = ai_helpers.PageBlockAITests.payload
    save_pages = ai_helpers.PageBlockAITests.save_pages
    page = staticmethod(ai_helpers.PageBlockAITests.page)
    block = staticmethod(ai_helpers.PageBlockAITests.block)
    block_url = ai_helpers.PageBlockAITests.block_url
    block_payload = ai_helpers.PageBlockAITests.block_payload
    save_blocks = ai_helpers.PageBlockAITests.save_blocks
    ai_url = ai_helpers.PageBlockAITests.ai_url
    request_data = ai_helpers.PageBlockAITests.request_data
    preview_data = ai_helpers.PageBlockAITests.preview_data

    def test_generate_up_to_requested_count_and_reject_invalid_styles(self):
        variants = [{"blocks":[{"kind":"text", "heading":f"Option {i}", "body":"[Verify]", "design":STYLE}]} for i in range(6)]
        self.transport.return_value = ai_helpers.response(variants)
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                data = self.request_data(kind); data["requested_options"] = "6"
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), {**data, "requested_options":"7"}).status_code, 400)
                self.assertEqual(self.client.post(self.ai_url(kind, "request"), data).status_code, 302)
                model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = model.objects.get(pk=data["request_id"])
                self.assertEqual(run.prompt_version, PROMPT_VERSION)
                self.assertEqual(len(run.options), 6)
                wire = json.loads(self.transport.call_args.args[0].data.decode())
                self.assertEqual(json.loads(wire["input"])["requested_options"], 6)
                design_schema = wire["text"]["format"]["schema"]["properties"]["options"]["items"]["properties"]["blocks"]["items"]["properties"]["design"]
                self.assertFalse(design_schema["additionalProperties"])
                self.assertEqual(set(design_schema["required"]), set(STYLE))
                self.assertEqual(len(self.client.get(self.ai_url(kind, "result", run.pk)).context["choices"]), 6)
                self.assertEqual(run.options[0]["blocks"][0]["design"], NORMAL_STYLE)
        invalid = [{"blocks":[{"kind":"text", "heading":"Bad", "body":"Text", "design":{**STYLE, "background":"#fff;display:none"}}]}]
        with self.assertRaises(ProviderError):
            validate_options(invalid, 5)
        with self.assertRaises(ProviderError):
            validate_options(variants, 5, max_options=5)

    def test_previous_v1_suggestions_remain_viewable_and_applicable(self):
        for kind in ("journal", "portal"):
            with self.subTest(kind=kind):
                self.assertEqual(self.save_pages(kind, [self.page()]).status_code, 302)
                data = self.request_data(kind)
                site = self.owner(kind)[2]; site.refresh_from_db()
                model = JournalSiteAIDesignRun if kind == "journal" else OrganisationPortalAIDesignRun
                run = model.objects.create(id=uuid.uuid4(), site=site, requested_by=self.owner(kind)[0],
                    model_name="test-model", prompt_version=LEGACY_PROMPT_VERSION, policy_revision=1,
                    site_revision=site.revision, site_fingerprint=data["fingerprint"], brief_fingerprint="a"*64,
                    target_page_slug="author-guidelines", status="completed",
                    options=[{"blocks":[{"kind":"text", "heading":"Old proposal", "body":"Old content"}]}])
                result = self.client.get(self.ai_url(kind, "result", run.pk))
                self.assertContains(result, "Old proposal")
                self.assertContains(self.client.get(self.block_url(kind, "manage")), self.ai_url(kind, "result", run.pk))
                form = {"revision":str(site.revision), "choice_0-TOTAL_FORMS":"1", "choice_0-INITIAL_FORMS":"1",
                        "choice_0-MIN_NUM_FORMS":"0", "choice_0-MAX_NUM_FORMS":"20",
                        "choice_0-0-kind":"text", "choice_0-0-heading":"Old proposal", "choice_0-0-body":"Old content",
                        "choice_0-0-visible":"on", "choice_0-0-ORDER":"1"}
                preview = self.client.post(self.ai_url(kind, "preview", run.pk, 0), form)
                self.assertContains(preview, "Old proposal")
                applied = self.client.post(self.ai_url(kind, "apply", run.pk, 0),
                    {"revision":str(site.revision), "reviewed_blocks":preview.context["block_reviewed"], "confirm":"yes"})
                self.assertEqual(applied.status_code, 302)
                site.refresh_from_db()
                self.assertNotIn("design", site.pages_config[0]["blocks"][0])
