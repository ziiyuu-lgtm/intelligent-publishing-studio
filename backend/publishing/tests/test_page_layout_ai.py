import json
from copy import deepcopy
from unittest.mock import patch

from django.test import SimpleTestCase

from publishing.ai_gateway import (GatewayRejected, OPENAI, PUBLISHER_PAGE_LAYOUT, SITE_PAGE_LAYOUT,
                                   run_ai_task)
from publishing.ai_provider import ProviderError
from publishing.page_layout_ai_provider import (describe_blocks, materialize_layout, parse_response,
    request_layout_suggestions, validate_descriptors, validate_options)


def block(heading, *, kind="text", visible=True, **kwargs):
    return {"kind":kind, "heading":heading, "body":"Secret body", "visible":visible,
            "link_label":"", "link_url":"", **kwargs}


BLOCKS = [
    block("Private first", grid_span=6),
    block("Private hidden", kind="image", visible=False,
          image_asset_id="01345678-1234-4234-8234-123456789abc", image_alt="Private alt",
          image_fit="cover", image_position="center"),
    block("Private link", kind="link", link_label="Private label", link_url="https://example.org/secret"),
]
OPTIONS = [{"gap":24, "alignment":"stretch", "blocks":[
    {"id":"b2", "grid_span":8}, {"id":"b0", "grid_span":4}, {"id":"b1", "grid_span":12}]}]


def response(options=OPTIONS):
    return {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[
        {"type":"output_text", "text":json.dumps({"options":options})}]}]}


class PageLayoutAITests(SimpleTestCase):
    def test_only_minimal_structure_is_described_and_sent(self):
        descriptors = describe_blocks(BLOCKS)
        self.assertEqual(descriptors, [
            {"id":"b0", "kind":"text", "visible":True, "has_image":False, "grid_span":6},
            {"id":"b1", "kind":"image", "visible":False, "has_image":True, "grid_span":12},
            {"id":"b2", "kind":"link", "visible":True, "has_image":False, "grid_span":12},
        ])
        with patch("publishing.ai_provider._send_request", return_value=response()) as transport:
            result = request_layout_suggestions(api_key="fictional-key", model="test-model", brief="Fictional brief",
                blocks=descriptors, requested_options=2, site_kind="journal")
        self.assertEqual(result, OPTIONS)
        payload = json.loads(transport.call_args.args[0].data)
        self.assertFalse(payload["store"])
        self.assertTrue(payload["text"]["format"]["strict"])
        self.assertEqual(json.loads(payload["input"]), {"brief":"Fictional brief", "blocks":descriptors,
                                                       "requested_options":2, "site_kind":"journal"})
        wire = json.dumps(payload)
        for secret in ("Private", "Secret", "01345678", "example.org", "secret"):
            self.assertNotIn(secret, wire)

    def test_materialization_keeps_every_field_including_hidden_blocks_images_and_links(self):
        page = {"title":"Private title", "body":"Private page body", "blocks":deepcopy(BLOCKS),
                "block_layout":{"gap":10, "alignment":"start"}, "other":"preserved"}
        original = deepcopy(page)
        result = materialize_layout(page, OPTIONS[0])
        self.assertEqual(page, original)
        self.assertEqual(result["block_layout"], {"gap":24, "alignment":"stretch"})
        self.assertEqual(result["blocks"], [{**BLOCKS[2], "grid_span":8}, {**BLOCKS[0], "grid_span":4}, BLOCKS[1]])
        self.assertEqual(result["title"], page["title"])
        self.assertEqual(result["other"], page["other"])
        self.assertIsNot(result["blocks"][0], page["blocks"][2])

    def test_rejects_missing_duplicate_extra_or_invalid_placements(self):
        descriptors = describe_blocks(BLOCKS)
        self.assertEqual(validate_options(OPTIONS, descriptors), OPTIONS)
        original = OPTIONS[0]
        bad_options = [[], [original, original],
            [{**original, "blocks":original["blocks"][:2]}],
            [{**original, "blocks":[original["blocks"][0]] * 3}],
            [{**original, "blocks":[*original["blocks"][:2], {"id":"b3", "grid_span":4}]}],
            [{**original, "blocks":[*original["blocks"][:2], {"id":"b1", "grid_span":13}]}],
            [{**original, "blocks":[*original["blocks"][:2], {"id":"b1", "grid_span":True}]}],
            [{**original, "gap":65}], [{**original, "alignment":"<script>"}],
            [{**original, "text":"injected"}]]
        for bad in bad_options:
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                validate_options(bad, descriptors)
        with self.assertRaises(ProviderError):
            materialize_layout({"blocks":BLOCKS}, bad_options[2][0])

    def test_gateway_rejects_unapproved_fields_and_bad_descriptors(self):
        descriptors = describe_blocks(BLOCKS)
        base = {"provider":OPENAI, "model":"test-model", "api_key":"fictional-key"}
        with patch("publishing.ai_gateway.request_layout_suggestions", return_value=OPTIONS) as adapter:
            for task, site_kind in ((SITE_PAGE_LAYOUT, "journal"), (PUBLISHER_PAGE_LAYOUT, "publisher")):
                payload = {"brief":"Fictional brief", "blocks":descriptors, "requested_options":2}
                self.assertEqual(run_ai_task(task=task, **base, payload=payload), OPTIONS)
                self.assertEqual(adapter.call_args.kwargs["site_kind"], site_kind)
                for bad in ({**payload, "page_body":"secret"}, {**payload, "requested_options":True},
                            {**payload, "blocks":[{**descriptors[0], "heading":"secret"}]},
                            {**payload, "blocks":[{**descriptors[0], "id":"secret"}]},
                            {**payload, "blocks":[{**descriptors[0], "visible":1}]}):
                    with self.subTest(task=task, bad=bad), self.assertRaises(GatewayRejected):
                        run_ai_task(task=task, **base, payload=bad)
            self.assertEqual(adapter.call_count, 2)

    def test_provider_and_descriptor_failures_are_not_accepted(self):
        descriptors = describe_blocks(BLOCKS)
        for bad in ([], [{**descriptors[0], "id":"b1"}],
                    [{**descriptors[0], "grid_span":False}], [{**descriptors[0], "has_image":"yes"}]):
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                validate_descriptors(bad)
        with self.assertRaises(ProviderError):
            describe_blocks([])
        for bad in ({"status":"incomplete", "output":[]},
                    {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[{"type":"refusal"}]}]},
                    response([OPTIONS[0], OPTIONS[0]]),
                    {"status":"completed", "output":[{"type":"message", "role":"assistant", "content":[{"type":"output_text", "text":"not json"}]}]}):
            with self.subTest(bad=bad), self.assertRaises(ProviderError):
                parse_response(bad, descriptors)
