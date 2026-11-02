from unittest.mock import patch

from django.test import SimpleTestCase

from publishing.ai_gateway import GatewayRejected, METADATA_WRITING, OPENAI, run_ai_task


class AIGatewayTests(SimpleTestCase):
    def test_unknown_tasks_providers_and_extra_fields_never_reach_the_adapter(self):
        valid = {"task":METADATA_WRITING, "provider":OPENAI, "model":"test-model", "api_key":"fictional-key",
                 "payload":{"title":"Fictional title", "abstract":"Fictional abstract"}}
        bad_requests = [
            {**valid, "task":"site_design"},
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
