"""Contract tests for the dedicated continuity output token budget."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import types
import unittest
from unittest.mock import patch

# Match the existing memory suites: production installs python-dotenv and
# httpx, while unit tests only need import-time placeholders in a bare env.
if importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.__spec__ = importlib.util.spec_from_loader("dotenv", loader=None)
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

if importlib.util.find_spec("httpx") is None:
    httpx = types.ModuleType("httpx")
    httpx.__spec__ = importlib.util.spec_from_loader("httpx", loader=None)
    httpx.Client = object
    sys.modules["httpx"] = httpx

from gateway.config import _clamped_env_int, cfg
from gateway.memory_continuity_shadow import ShadowPreviewError, extract_continuity_candidates
from gateway.memory_extract import EMBEDDING_MODEL, _extract_memories, _get_embedding_sync

SHADOW = "gateway.memory_continuity_shadow"
EXTRACT = "gateway.memory_extract"

REASONING_MARKER = "SECRET-REASONING-MARKER-internal-model-thoughts"

CONTINUITY_ENV = {
    "CONTINUITY_BASE_URL": "https://continuity.example/v1",
    "CONTINUITY_API_KEY": "continuity-key",
    "CONTINUITY_MODEL": "continuity-model",
}
ANALYSIS_ENV = {
    "ANALYSIS_BASE_URL": "https://analysis.example/v1",
    "ANALYSIS_API_KEY": "analysis-key",
    "ANALYSIS_MODEL": "analysis-model",
}


class _HttpResponse:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload or "")

    def json(self):
        return self._payload


class _CapturingHttpClient:
    """Stand-in for httpx.Client that records every post() call."""

    def __init__(self, response=None):
        self.response = response or _HttpResponse({
            "choices": [{"message": {"content": json.dumps({"candidates": []})}}],
        })
        self.calls = []

    def __call__(self, timeout=None):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, url, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        return self.response


def _apply(patches):
    entered = []
    for item in patches:
        item.__enter__()
        entered.append(item)
    return entered


def _exit_all(entered):
    for item in reversed(entered):
        item.__exit__(None, None, None)


class ContinuityTokenBudgetConfigTests(unittest.TestCase):
    """Boundary parsing of CONTINUITY_MAX_TOKENS itself."""

    def _parse(self, raw):
        with patch.dict(os.environ):
            os.environ.pop("CONTINUITY_MAX_TOKENS", None)
            if raw is not None:
                os.environ["CONTINUITY_MAX_TOKENS"] = raw
            return _clamped_env_int("CONTINUITY_MAX_TOKENS", 8192, 1024, 120000)

    def test_unset_uses_default_8192(self):
        self.assertEqual(self._parse(None), 8192)

    def test_configured_value_is_used(self):
        self.assertEqual(self._parse("4096"), 4096)

    def test_below_minimum_clamps_to_1024(self):
        self.assertEqual(self._parse("512"), 1024)
        self.assertEqual(self._parse("0"), 1024)
        self.assertEqual(self._parse("-100"), 1024)

    def test_above_maximum_clamps_to_120000(self):
        self.assertEqual(self._parse("120001"), 120000)
        self.assertEqual(self._parse("999999"), 120000)

    def test_values_within_range_pass_through(self):
        self.assertEqual(self._parse("99999"), 99999)
        self.assertEqual(self._parse("120000"), 120000)

    def test_unparsable_value_uses_default(self):
        self.assertEqual(self._parse("abc"), 8192)
        self.assertEqual(self._parse("4096.5"), 8192)
        self.assertEqual(self._parse(""), 8192)

    def test_module_config_is_an_integer_and_does_not_break_import(self):
        self.assertIsInstance(cfg.CONTINUITY_MAX_TOKENS, int)
        self.assertGreaterEqual(cfg.CONTINUITY_MAX_TOKENS, 1024)
        self.assertLessEqual(cfg.CONTINUITY_MAX_TOKENS, 120000)


class ContinuityRequestBudgetTests(unittest.TestCase):
    """The extraction request body must use cfg.CONTINUITY_MAX_TOKENS."""

    def _extract_with_budget(self, budget):
        http = _CapturingHttpClient()
        patches = [patch.object(cfg, name, value) for name, value in CONTINUITY_ENV.items()]
        patches.append(patch.object(cfg, "CONTINUITY_MAX_TOKENS", budget))
        entered = _apply(patches)
        try:
            with patch(f"{SHADOW}.httpx.Client", http):
                result = extract_continuity_candidates("对话内容", {})
        finally:
            _exit_all(entered)
        return result, http

    def test_default_budget_8192_is_sent(self):
        result, http = self._extract_with_budget(8192)
        self.assertEqual(result, [])
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 8192)

    def test_configured_budget_4096_is_sent(self):
        _, http = self._extract_with_budget(4096)
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 4096)

    def test_configured_budget_120000_is_sent(self):
        _, http = self._extract_with_budget(120000)
        self.assertEqual(http.calls[0]["json"]["max_tokens"], 120000)

    def test_request_shape_is_unchanged_except_max_tokens(self):
        _, http = self._extract_with_budget(8192)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://continuity.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer continuity-key")
        body = call["json"]
        self.assertEqual(body["model"], "continuity-model")
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertEqual(body["temperature"], 0.1)
        self.assertEqual(body["messages"][0]["role"], "system")


class LegacyPipelineUnchangedTests(unittest.TestCase):
    """The budget must not leak into legacy extraction or embedding."""

    def test_legacy_extraction_keeps_max_tokens_1200(self):
        http = _CapturingHttpClient(_HttpResponse({
            "choices": [{"message": {"content": '{"memories":[]}'}}],
        }))
        patches = [patch.object(cfg, name, value) for name, value in ANALYSIS_ENV.items()]
        patches.append(patch.object(cfg, "CONTINUITY_MAX_TOKENS", 4096))
        entered = _apply(patches)
        try:
            with patch(f"{EXTRACT}.httpx.Client", http):
                _extract_memories("对话内容")
        finally:
            _exit_all(entered)

        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://analysis.example/v1/chat/completions")
        self.assertEqual(call["json"]["max_tokens"], 1200)

    def test_embedding_still_uses_analysis_without_max_tokens(self):
        http = _CapturingHttpClient(_HttpResponse({
            "data": [{"embedding": [0.5, 0.25]}],
        }))
        patches = [patch.object(cfg, name, value) for name, value in ANALYSIS_ENV.items()]
        patches.append(patch.object(cfg, "CONTINUITY_MAX_TOKENS", 4096))
        entered = _apply(patches)
        try:
            with patch(f"{EXTRACT}.httpx.Client", http):
                embedding = _get_embedding_sync("一段需要向量化的记忆内容")
        finally:
            _exit_all(entered)

        self.assertEqual(embedding, [0.5, 0.25])
        call = http.calls[0]
        self.assertEqual(call["url"], "https://analysis.example/v1/embeddings")
        self.assertEqual(call["headers"]["Authorization"], "Bearer analysis-key")
        self.assertEqual(call["json"]["model"], EMBEDDING_MODEL)
        self.assertNotIn("max_tokens", call["json"])


class EmptyContentObservabilityTests(unittest.TestCase):
    """HTTP 200 with empty content stays a failure and stays explainable."""

    def _respond(self, payload):
        http = _CapturingHttpClient(_HttpResponse(payload))
        patches = [patch.object(cfg, name, value) for name, value in CONTINUITY_ENV.items()]
        patches.append(patch.object(cfg, "CONTINUITY_MAX_TOKENS", 8192))
        entered = _apply(patches)
        try:
            with patch(f"{SHADOW}.httpx.Client", http):
                extract_continuity_candidates("对话内容", {})
        except ShadowPreviewError as exc:
            return exc
        finally:
            _exit_all(entered)
        raise AssertionError("expected ShadowPreviewError")

    def test_length_finish_reason_reports_budget_exhaustion(self):
        payload = {
            "choices": [{
                "finish_reason": "length",
                "message": {"content": "", "reasoning_content": REASONING_MARKER},
            }],
            "usage": {
                "completion_tokens": 8192,
                "completion_tokens_details": {"reasoning_tokens": 8000},
            },
        }
        with self.assertLogs("gateway.memory_continuity_shadow", level="ERROR") as captured:
            error = self._respond(payload)

        self.assertEqual(error.code, "model_response_error")
        self.assertIn("budget exhausted", str(error))
        self.assertNotIn(REASONING_MARKER, str(error))
        joined = "\n".join(captured.output)
        self.assertIn("finish_reason='length'", joined)
        self.assertIn("reasoning_content=present", joined)
        self.assertIn("completion_tokens=8192", joined)
        self.assertIn("reasoning_tokens=8000", joined)
        # The model's internal reasoning must never reach the logs.
        self.assertNotIn(REASONING_MARKER, joined)

    def test_plain_empty_content_keeps_original_code_and_message(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": ""}}]}
        error = self._respond(payload)
        self.assertEqual(error.code, "model_response_error")
        self.assertIn("Shadow model response shape is invalid", str(error))
        self.assertIn("finish_reason='stop'", str(error))

    def test_malformed_response_keeps_original_error(self):
        with patch.object(cfg, "CONTINUITY_BASE_URL", "https://continuity.example/v1"), \
             patch.object(cfg, "CONTINUITY_API_KEY", "continuity-key"), \
             patch.object(cfg, "CONTINUITY_MODEL", "continuity-model"):
            http = _CapturingHttpClient(_HttpResponse({"error": "not openai shape"}))
            with patch(f"{SHADOW}.httpx.Client", http):
                with self.assertRaises(ShadowPreviewError) as raised:
                    extract_continuity_candidates("对话内容", {})
        self.assertEqual(raised.exception.code, "model_response_error")
        self.assertEqual(
            str(raised.exception), "Shadow model response shape is invalid",
        )

    def test_valid_strict_json_still_parses_normally(self):
        candidate = {
            "content": "叶子和栖约好下次继续讨论旅行计划。",
            "continuity_type": "thread",
            "continuity_data": {
                "open_question": "旅行计划", "current_state": "讨论中",
                "closure_criteria": [], "abstract_retrieval_hints": [],
                "concrete_retrieval_hints": [],
            },
            "subject": "shared",
            "source_type": "natural_chat",
            "thread_state": "open",
            "importance": 5,
            "continuity_value": 9,
            "confidence": 0.9,
            "evidence_message_ids": [11],
            "memory_time": None,
            "time_precision": "unknown",
            "title": "旅行计划待续",
            "participants": ["yezi", "qi"],
            "reason": "下个窗口需要继续。",
            "retention_class": "normal",
        }
        http = _CapturingHttpClient(_HttpResponse({
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps({"candidates": [candidate]}, ensure_ascii=False)},
            }],
        }))
        patches = [patch.object(cfg, name, value) for name, value in CONTINUITY_ENV.items()]
        entered = _apply(patches)
        try:
            with patch(f"{SHADOW}.httpx.Client", http):
                result = extract_continuity_candidates("对话内容", {11: None})
        finally:
            _exit_all(entered)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["title"], "旅行计划待续")
        self.assertEqual(result[0]["continuity_type"], "thread")
        self.assertEqual(result[0]["evidence_message_ids"], [11])


if __name__ == "__main__":
    unittest.main()
