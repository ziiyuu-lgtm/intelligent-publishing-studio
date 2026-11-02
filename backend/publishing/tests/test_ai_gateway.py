from unittest.mock import patch

from django.test import SimpleTestCase

from publishing.ai_gateway import GatewayRejected, METADATA_WRITING, OPENAI, SITE_DESIGN, run_ai_task
from publishing.site_design import legacy_to_blueprint


class AIGatewayTests(SimpleTestCase):
    def test_unknown_tasks_providers_and_extra_fields_never_reach_the_adapter(self):
        valid = {"task":METADATA_WRITING, "provider":OPENAI, "model":"test-model", "api_key":"fictional-key",
                 "payload":{"title":"Fictional title", "abstract":"Fictional abstract"}}
        bad_requests = [
            {**valid, "task":"unregistered_task"},
            {**valid, "provider":"unregistered-provider"},
            {**valid, "payload":{"title":"Fictional title", "abstract":"Fictional abstract", "review":"private"}},
            {**valid, "payload":{"title":"Fictional title", "file":"private.pdf"}},
            {**valid, "payload":{"title":None, "abstract":"Fictional abstract"}},
        ]
        with patch("publishing.ai_gateway.request_metadata_suggestions") as adapter:
            for args in bad_requests:
                with self.subTest(args=args), self.assertRaises(GatewayRejected):
                    run_ai_task(**args)
            adapter.assert_not_called()

    def test_registered_task_passes_only_title_and_abstract(self):
        with patch("publishing.ai_gateway.request_metadata_suggestions", return_value=[]) as adapter:
            result = run_ai_task(task=METADATA_WRITING, provider=OPENAI, model="test-model", api_key="fictional-key",
                                 payload={"title":"Fictional title", "abstract":"Fictional abstract"})
        self.assertEqual(result, [])
        adapter.assert_called_once_with(api_key="fictional-key", model="test-model", title="Fictional title", abstract="Fictional abstract")

    def test_site_task_accepts_only_brief_and_validated_visual_settings(self):
        valid = {"task":SITE_DESIGN, "provider":OPENAI, "model":"test-model", "api_key":"fictional-key"}
        config = legacy_to_blueprint("forest", "classic")
        with patch("publishing.ai_gateway.request_design_suggestions", return_value=[]) as adapter:
            self.assertEqual(run_ai_task(**valid, payload={"brief":"Fictional visual style", "appearance":config}), [])
            adapter.assert_called_once_with(api_key="fictional-key", model="test-model", brief="Fictional visual style", appearance=config)
            for payload in ({"brief":"Style", "appearance":config, "manuscript":"private"},
                            {"brief":None, "appearance":config}, {"brief":"Style", "appearance":{"script":"bad"}}, {}):
                with self.subTest(payload=payload), self.assertRaises(GatewayRejected):
                    run_ai_task(**valid, payload=payload)
            self.assertEqual(adapter.call_count, 1)
