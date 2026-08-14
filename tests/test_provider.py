"""Anthropic response-shape and continuation handling."""

import json
import unittest
from unittest.mock import patch

from strategy_os.provider import (
    AnthropicProvider,
    DEFAULT_STAGE_MAX_TOKENS,
    DEFAULT_XAI_MODEL,
    LiveStrategyProvider,
    ProviderError,
    REQUEST_TIMEOUT_SECONDS,
    RESEARCH_EXCERPT_CHARS,
    RESEARCH_SOURCE_LIMIT,
    SOURCE_COLLECTION_MAX_TOKENS,
    STAGE_MAX_TOKENS,
    XaiProvider,
)


class _Response:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def read(self):
        return self.body


class AnthropicProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = AnthropicProvider("test-key")

    def request(self, *payloads):
        with patch("strategy_os.provider.urlopen", side_effect=[_Response(item) for item in payloads]) as mocked:
            result = self.provider._request("original prompt", 100, use_web_search=True)
        return result, mocked

    def test_normal_json_text(self):
        result, mocked = self.request({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": '{"sources": []}'}]})
        self.assertEqual(result, {"sources": []})
        self.assertEqual(mocked.call_args.kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)

    def test_fenced_json_text(self):
        result, _ = self.request({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": '```json\n{"sources": []}\n```'}]})
        self.assertEqual(result, {"sources": []})

    def test_search_preamble_before_final_json(self):
        result, _ = self.request({
            "type": "message", "stop_reason": "end_turn", "content": [
                {"type": "text", "text": "I'll search for current sources."},
                {"type": "server_tool_use", "id": "srv-1", "name": "web_search", "input": {"query": "current sources"}},
                {"type": "web_search_tool_result", "tool_use_id": "srv-1", "content": []},
                {"type": "text", "text": '```json\n{"sources": []}\n```'},
            ],
        })
        self.assertEqual(result, {"sources": []})

    def test_preamble_selects_outer_stage_object_not_nested_object(self):
        expected = {"stage": "research", "market_position": {"summary": "Position"}}
        result, _ = self.request({
            "type": "message", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": "Research complete.\n" + json.dumps(expected)}],
        })
        self.assertEqual(result, expected)

    def test_pause_turn_then_completion_preserves_original_turn(self):
        paused_content = [
            {"type": "server_tool_use", "id": "srv-1", "name": "web_search", "input": {"query": "example"}},
            {"type": "web_search_tool_result", "tool_use_id": "srv-1", "content": []},
        ]
        result, mocked = self.request(
            {"type": "message", "stop_reason": "pause_turn", "content": paused_content},
            {"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": '{"sources": []}'}]},
        )
        self.assertEqual(result, {"sources": []})
        continued = json.loads(mocked.call_args_list[1].args[0].data.decode())
        self.assertEqual(continued["messages"], [
            {"role": "user", "content": "original prompt"},
            {"role": "assistant", "content": paused_content},
        ])
        self.assertEqual(continued["tools"], [{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}])

    def test_empty_output(self):
        with self.assertRaisesRegex(ProviderError, "without returning structured text"):
            self.request({"type": "message", "stop_reason": "end_turn", "content": [{"type": "text", "text": "  "}]})

    def test_refusal_and_error_payloads(self):
        cases = [
            ({"type": "message", "stop_reason": "refusal", "content": []}, "declined"),
            ({"type": "error", "error": {"type": "overloaded_error", "message": "internal detail"}}, "overloaded_error"),
        ]
        for payload, reason in cases:
            with self.subTest(reason=reason), self.assertRaisesRegex(ProviderError, reason):
                self.request(payload)

    def test_max_tokens_and_unexpected_shape(self):
        with self.assertRaisesRegex(ProviderError, "cut off"):
            self.request({"type": "message", "stop_reason": "max_tokens", "content": [{"type": "text", "text": "{"}]})
        with self.assertRaisesRegex(ProviderError, "content list"):
            self.request({"type": "message", "stop_reason": "end_turn", "content": {}})

    def test_research_prompt_is_compact_and_uses_stage_token_limit(self):
        captured = {}

        def fake_request(prompt, max_tokens, use_web_search):
            captured["prompt"] = json.loads(prompt)
            captured["max_tokens"] = max_tokens
            captured["use_web_search"] = use_web_search
            return {"stage": "research"}

        self.provider._request = fake_request
        long_excerpt = "verified excerpt " * 40
        sources = []
        for index in range(RESEARCH_SOURCE_LIMIT + 5):
            sources.append({
                "id": "source-%s" % index,
                "label": "Label %s" % index,
                "title": "Title %s" % index,
                "publisher": "Publisher",
                "url": "https://example.com/source/%s/with/a/long/path" % index,
                "publication_date": "2026-08-01",
                "accessed_date": "2026-08-14",
                "type": "report",
                "excerpt": long_excerpt,
            })
        result = self.provider.generate("research", "A working brief.", {
            "cycle_id": "cycle-1",
            "brand_slug": "pragmatic-play",
            "source_collection": {"stage": "source_collection", "sources": sources},
            "unrelated": "must-not-be-sent",
        }, 1)
        self.assertEqual(result, {"stage": "research"})
        self.assertEqual(captured["max_tokens"], STAGE_MAX_TOKENS["research"])
        self.assertGreater(captured["max_tokens"], DEFAULT_STAGE_MAX_TOKENS)
        self.assertFalse(captured["use_web_search"])
        prompt = captured["prompt"]
        self.assertEqual(prompt["stage"], "research")
        self.assertNotIn("unrelated", prompt["context"])
        self.assertNotIn("source_collection", prompt["context"])
        collected = prompt["context"]["collected_sources"]
        self.assertEqual(len(collected), RESEARCH_SOURCE_LIMIT)
        self.assertNotIn("url", collected[0])
        self.assertLessEqual(len(collected[0]["excerpt"]), RESEARCH_EXCERPT_CHARS + 1)
        joined_rules = " ".join(prompt["rules"]).lower()
        self.assertIn("concise", joined_rules)
        self.assertIn("do not copy collected_sources wholesale", joined_rules)
        self.assertIn("at most 180 words", joined_rules)
        self.assertEqual(prompt["output_schema"]["body"], "<=180 words")
        self.assertEqual(prompt["output_schema"]["sources"][0]["type"].split("|")[0], "report")

    def test_non_research_stage_keeps_default_token_limit(self):
        captured = {}

        def fake_request(prompt, max_tokens, use_web_search):
            captured["prompt"] = json.loads(prompt)
            captured["max_tokens"] = max_tokens
            return {"stage": "competitive"}

        self.provider._request = fake_request
        context = {"cycle_id": "cycle-1", "brand_slug": "pragmatic-play", "research": {"body": "kept"}}
        self.provider.generate("competitive", "A working brief.", context, 1)
        self.assertEqual(captured["max_tokens"], DEFAULT_STAGE_MAX_TOKENS)
        self.assertEqual(captured["prompt"]["context"], context)
        self.assertNotIn("output_schema", captured["prompt"])

    def test_source_collection_keeps_its_token_limit(self):
        captured = {}

        def fake_request(prompt, max_tokens, use_web_search):
            captured["max_tokens"] = max_tokens
            captured["use_web_search"] = use_web_search
            return {"sources": [{"id": "one"}, {"id": "two"}, {"id": "three"}]}

        self.provider._request = fake_request
        sources = self.provider.collect_sources("brief", {})
        self.assertEqual(len(sources), 3)
        self.assertEqual(captured["max_tokens"], SOURCE_COLLECTION_MAX_TOKENS)
        self.assertTrue(captured["use_web_search"])

    def test_web_search_error_block(self):
        with self.assertRaisesRegex(ProviderError, "search limit"):
            self.request({
                "type": "message", "stop_reason": "end_turn", "content": [{
                    "type": "web_search_tool_result", "tool_use_id": "srv-1",
                    "content": {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"},
                }],
            })


class XaiProviderTests(unittest.TestCase):
    def setUp(self):
        self.provider = XaiProvider("test-key")

    def request(self, payload):
        with patch("strategy_os.provider.urlopen", return_value=_Response(payload)) as mocked:
            result = self.provider._request("original prompt", 8000)
        return result, mocked

    def test_json_object_and_research_token_limit(self):
        captured = {}

        def fake_request(prompt, max_tokens):
            captured["prompt"] = json.loads(prompt)
            captured["max_tokens"] = max_tokens
            return {"stage": "research"}

        self.provider._request = fake_request
        result = self.provider.generate("research", "A working brief.", {
            "cycle_id": "cycle-1",
            "brand_slug": "pragmatic-play",
            "source_collection": {"sources": [{"id": "source-1", "label": "Report", "type": "report", "publication_date": "2026-08-01", "accessed_date": "2026-08-14", "excerpt": "short"}]},
        }, 1)
        self.assertEqual(result, {"stage": "research"})
        self.assertEqual(captured["max_tokens"], STAGE_MAX_TOKENS["research"])
        self.assertEqual(captured["prompt"]["stage"], "research")
        self.assertIn("concise", " ".join(captured["prompt"]["rules"]).lower())

    def test_posts_json_mode_to_grok(self):
        result, mocked = self.request({
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"stage": "research"}'}}],
        })
        self.assertEqual(result, {"stage": "research"})
        body = json.loads(mocked.call_args.args[0].data.decode())
        self.assertEqual(body["model"], DEFAULT_XAI_MODEL)
        self.assertEqual(body["max_completion_tokens"], 8000)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(mocked.call_args.kwargs["timeout"], REQUEST_TIMEOUT_SECONDS)

    def test_length_cutoff_uses_safe_reason(self):
        with self.assertRaisesRegex(ProviderError, "cut off"):
            self.request({"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})

    def test_refusal(self):
        with self.assertRaisesRegex(ProviderError, "declined"):
            self.request({"choices": [{"finish_reason": "stop", "message": {"content": "", "refusal": "no"}}]})


class LiveStrategyProviderTests(unittest.TestCase):
    def test_routes_sources_to_anthropic_and_stages_to_grok(self):
        anthropic = AnthropicProvider("anthropic-key")
        xai = XaiProvider("xai-key")
        anthropic.collect_sources = lambda brief, context: [{"id": "source-1"}]
        xai.generate = lambda stage_id, brief, context, revision: {"stage": stage_id, "revision": revision}
        routed = LiveStrategyProvider(anthropic, xai)
        self.assertEqual(routed.collect_sources("brief", {}), [{"id": "source-1"}])
        self.assertEqual(routed.generate("research", "brief", {}, 1), {"stage": "research", "revision": 1})


if __name__ == "__main__":
    unittest.main()
