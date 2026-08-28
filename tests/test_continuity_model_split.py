"""Contract tests for the independent CONTINUITY_* model provider split.

Continuity text extraction (formal pipeline + Shadow Preview) must use
CONTINUITY_* exclusively, while legacy memory extraction and embedding keep
using ANALYSIS_*.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

from gateway.config import cfg
from gateway.memory_continuity import ContinuityPipelineError, run_continuity_digest, run_continuity_digest_if_due
from gateway.memory_continuity_shadow import (
    ShadowPreviewError,
    _analysis_configured,
    _continuity_analysis_configured,
    extract_continuity_candidates,
    run_shadow_preview,
)
from gateway.memory_extract import DigestPipelineError, _extract_memories, _get_embedding_sync, run_memory_digest

SHADOW_MODULE = "gateway.memory_continuity_shadow"
EXTRACT_MODULE = "gateway.memory_extract"
CONTINUITY_MODULE = "gateway.memory_continuity"

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


def _patch_config(**values):
    return [patch.object(cfg, key, value) for key, value in values.items()]


def _apply(patches, context):
    for item in patches:
        item.__enter__()
        context.append(item)


class ContinuityProviderRequestTests(unittest.TestCase):
    """Requirement 1: a complete CONTINUITY_* config drives the request."""

    def test_shadow_extraction_uses_continuity_provider(self):
        http = _CapturingHttpClient()
        context = []
        _apply(_patch_config(**CONTINUITY_ENV, **ANALYSIS_ENV), context)
        with patch(f"{SHADOW_MODULE}.httpx.Client", http):
            result = extract_continuity_candidates("对话内容", {})
        for item in context:
            item.__exit__(None, None, None)

        self.assertEqual(result, [])
        self.assertEqual(len(http.calls), 1)
        call = http.calls[0]
        self.assertEqual(call["url"], "https://continuity.example/v1/chat/completions")
        self.assertEqual(call["headers"]["Authorization"], "Bearer continuity-key")
        self.assertEqual(call["json"]["model"], "continuity-model")

    def test_analysis_only_values_never_leak_into_continuity_request(self):
        http = _CapturingHttpClient()
        context = []
        _apply(_patch_config(**CONTINUITY_ENV, **ANALYSIS_ENV), context)
        with patch(f"{SHADOW_MODULE}.httpx.Client", http):
            extract_continuity_candidates("对话内容", {})
        for item in context:
            item.__exit__(None, None, None)

        serialized = json.dumps(http.calls)
        self.assertNotIn("analysis.example", serialized)
        self.assertNotIn("analysis-model", serialized)
        self.assertNotIn("analysis-key", serialized)


class SeparateProviderRoutingTests(unittest.TestCase):
    """Requirement 2: distinct values keep both pipelines on their own config."""

    def test_legacy_extraction_and_embedding_still_use_analysis(self):
        extract_http = _CapturingHttpClient(_HttpResponse({
            "choices": [{"message": {"content": '{"memories":[]}'}}],
        }))
        embedding_http = _CapturingHttpClient(_HttpResponse({
            "data": [{"embedding": [0.5, 0.25]}],
        }))
        context = []
        _apply(_patch_config(**CONTINUITY_ENV, **ANALYSIS_ENV), context)
        with patch(f"{EXTRACT_MODULE}.httpx.Client", extract_http):
            memories, output = _extract_memories("对话内容")
        with patch(f"{EXTRACT_MODULE}.httpx.Client", embedding_http):
            embedding = _get_embedding_sync("一段需要向量化的记忆内容")
        for item in context:
            item.__exit__(None, None, None)

        self.assertEqual(memories, [])
        self.assertEqual(len(extract_http.calls), 1)
        extract_call = extract_http.calls[0]
        self.assertEqual(extract_call["url"], "https://analysis.example/v1/chat/completions")
        self.assertEqual(extract_call["headers"]["Authorization"], "Bearer analysis-key")
        self.assertEqual(extract_call["json"]["model"], "analysis-model")
        self.assertIn("analysis-key", json.dumps(extract_http.calls))

        self.assertEqual(embedding, [0.5, 0.25])
        embedding_call = embedding_http.calls[0]
        self.assertEqual(embedding_call["url"], "https://analysis.example/v1/embeddings")
        self.assertEqual(embedding_call["headers"]["Authorization"], "Bearer analysis-key")
        self.assertNotIn("continuity.example", json.dumps(embedding_http.calls))

    def test_continuity_extraction_uses_continuity_when_both_configured(self):
        http = _CapturingHttpClient()
        context = []
        _apply(_patch_config(**CONTINUITY_ENV, **ANALYSIS_ENV), context)
        with patch(f"{SHADOW_MODULE}.httpx.Client", http):
            extract_continuity_candidates("对话内容", {})
        for item in context:
            item.__exit__(None, None, None)

        self.assertEqual(http.calls[0]["url"], "https://continuity.example/v1/chat/completions")


class ContinuityNotConfiguredTests(unittest.TestCase):
    """Requirement 3: any empty CONTINUITY_* value blocks provider requests."""

    def _patch_partially_empty_continuity(self, missing_key):
        values = dict(CONTINUITY_ENV)
        values[missing_key] = ""
        return _patch_config(**values, **ANALYSIS_ENV)

    def test_shadow_extraction_raises_without_provider_request(self):
        for missing_key in ("CONTINUITY_BASE_URL", "CONTINUITY_API_KEY", "CONTINUITY_MODEL"):
            with self.subTest(missing_key=missing_key):
                http = _CapturingHttpClient()
                context = []
                _apply(self._patch_partially_empty_continuity(missing_key), context)
                with patch(f"{SHADOW_MODULE}.httpx.Client", http):
                    with self.assertRaises(ShadowPreviewError) as raised:
                        extract_continuity_candidates("对话内容", {})
                for item in context:
                    item.__exit__(None, None, None)

                self.assertEqual(raised.exception.code, "analysis_not_configured")
                self.assertEqual(http.calls, [])

    def test_shadow_preview_raises_before_any_model_call(self):
        rows = [{
            "id": 11,
            "assistant_id": "assistant-1",
            "conversation_id": "conv-a",
            "role": "user",
            "content": "下次继续这个话题",
            "created_at": "2026-08-28T20:00:00+08:00",
        }]
        extract = Mock()
        context = []
        _apply(self._patch_partially_empty_continuity("CONTINUITY_API_KEY"), context)
        _apply([patch.object(cfg, "MEMORY_ASSISTANT_ID", "assistant-1")], context)
        with (
            patch(f"{SHADOW_MODULE}._client", return_value=SimpleNamespace(table=None)),
            patch(f"{SHADOW_MODULE}.fetch_recent_shadow_sample", return_value=(
                [{"id": 11, "source_time": None}], [],
            )),
            patch(f"{SHADOW_MODULE}._extract_shadow_candidates", extract),
        ):
            with self.assertRaises(ShadowPreviewError) as raised:
                run_shadow_preview(80, 16000)
        for item in context:
            item.__exit__(None, None, None)

        self.assertEqual(raised.exception.code, "analysis_not_configured")
        extract.assert_not_called()

    def test_formal_execution_raises_503_without_model_or_database(self):
        for missing_key in ("CONTINUITY_BASE_URL", "CONTINUITY_API_KEY", "CONTINUITY_MODEL"):
            with self.subTest(missing_key=missing_key):
                extract = Mock()
                database = Mock()
                context = []
                _apply(self._patch_partially_empty_continuity(missing_key), context)
                with (
                    patch(f"{CONTINUITY_MODULE}.extract_continuity_candidates", extract),
                    patch(f"{CONTINUITY_MODULE}._client", database),
                ):
                    with self.assertRaises(ContinuityPipelineError) as raised:
                        run_continuity_digest()
                for item in context:
                    item.__exit__(None, None, None)

                self.assertEqual(raised.exception.code, "analysis_not_configured")
                self.assertEqual(raised.exception.status_code, 503)
                extract.assert_not_called()
                database.assert_not_called()

    def test_automatic_check_returns_none_without_provider(self):
        context = []
        _apply(self._patch_partially_empty_continuity("CONTINUITY_MODEL"), context)
        try:
            self.assertIsNone(run_continuity_digest_if_due())
        finally:
            for item in context:
                item.__exit__(None, None, None)

    def test_continuity_config_check_ignores_analysis_values(self):
        context = []
        _apply(_patch_config(**{**CONTINUITY_ENV, "CONTINUITY_API_KEY": ""}, **ANALYSIS_ENV), context)
        try:
            self.assertFalse(_continuity_analysis_configured())
            self.assertTrue(_analysis_configured())
        finally:
            for item in context:
                item.__exit__(None, None, None)


class AnalysisMissingContinuityStillWorksTests(unittest.TestCase):
    """Requirement 4: ANALYSIS_* absence must not block continuity extraction."""

    def test_shadow_extraction_works_without_analysis_config(self):
        http = _CapturingHttpClient()
        context = []
        _apply(_patch_config(**CONTINUITY_ENV, **{**ANALYSIS_ENV, "ANALYSIS_API_KEY": ""}), context)
        with patch(f"{SHADOW_MODULE}.httpx.Client", http):
            result = extract_continuity_candidates("对话内容", {})
        for item in context:
            item.__exit__(None, None, None)

        self.assertEqual(result, [])
        self.assertEqual(len(http.calls), 1)

    def test_formal_execution_reports_embedding_error_at_embedding_stage(self):
        candidate = {
            "content": "叶子和栖约好下次继续处理网关问题。",
            "continuity_type": "thread",
            "subject": "project",
            "source_type": "natural_chat",
            "thread_state": "open",
            "importance": 6,
            "continuity_value": 9,
            "confidence": 0.9,
            "evidence_message_ids": [178],
            "evidence_start_time": "2026-08-28T10:00+08:00",
            "evidence_end_time": "2026-08-28T10:00+08:00",
            "source_time": "2026-08-28T10:00+08:00",
            "memory_time": None,
            "time_precision": "unknown",
            "title": "网关问题待续",
            "participants": ["yezi", "qi"],
            "reason": "下次需要继续处理。",
            "retention_class": "normal",
        }
        cursor = {
            "status": "ready",
            "last_processed_message_id": 177,
            "manual_cooldown_until": None,
            "auto_cooldown_until": None,
        }
        context = []
        _apply(_patch_config(**CONTINUITY_ENV, **{**ANALYSIS_ENV, "ANALYSIS_API_KEY": ""}), context)
        extract = Mock(return_value=[candidate])
        heartbeat = Mock()
        with (
            patch(f"{CONTINUITY_MODULE}.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch(f"{CONTINUITY_MODULE}._get_cursor", return_value=dict(cursor)),
            patch(f"{CONTINUITY_MODULE}._fetch_rows_after", return_value=[
                {"id": 178, "assistant_id": "assistant-1", "conversation_id": "c1",
                 "role": "user", "content": "消息", "created_at": "2026-08-28T10:00:00+08:00"},
            ]),
            patch(f"{CONTINUITY_MODULE}._claim_slot", return_value={"status": "claimed", "run_id": 91}),
            patch(f"{CONTINUITY_MODULE}._set_running_run", return_value={"id": 91}),
            patch(f"{CONTINUITY_MODULE}.extract_continuity_candidates", extract),
            patch(f"{CONTINUITY_MODULE}._update_heartbeat", heartbeat),
            patch(f"{CONTINUITY_MODULE}._record_failure") as failed,
            patch(f"{CONTINUITY_MODULE}._client", Mock()),
        ):
            with self.assertRaises(ContinuityPipelineError) as raised:
                run_continuity_digest()
        for item in context:
            item.__exit__(None, None, None)

        # Extraction ran on the continuity provider; the failure happened only
        # at the embedding stage, which still belongs to ANALYSIS_*.
        extract.assert_called_once()
        self.assertEqual(raised.exception.code, "embedding_error")
        self.assertEqual(raised.exception.status_code, 422)
        failed.assert_called_once()


class LegacyDigestUnaffectedTests(unittest.TestCase):
    """Requirement 5: legacy digest keeps its ANALYSIS_* contract."""

    def test_legacy_digest_still_requires_analysis_before_database(self):
        context = []
        _apply(_patch_config(**{**ANALYSIS_ENV, "ANALYSIS_API_KEY": ""}, **CONTINUITY_ENV), context)
        database = Mock()
        try:
            with patch(f"{EXTRACT_MODULE}._client", database):
                with self.assertRaises(DigestPipelineError) as raised:
                    run_memory_digest("manual_preview", "preview")
        finally:
            for item in context:
                item.__exit__(None, None, None)

        self.assertEqual(raised.exception.code, "analysis_not_configured")
        database.assert_not_called()

    def test_legacy_analysis_configured_stays_analysis_only(self):
        from gateway.memory_extract import _analysis_configured as legacy_configured

        context = []
        _apply(_patch_config(**ANALYSIS_ENV, **CONTINUITY_ENV), context)
        try:
            self.assertTrue(legacy_configured())
        finally:
            for item in context:
                item.__exit__(None, None, None)

        context = []
        _apply(_patch_config(**{**ANALYSIS_ENV, "ANALYSIS_API_KEY": ""}, **CONTINUITY_ENV), context)
        try:
            self.assertFalse(legacy_configured())
        finally:
            for item in context:
                item.__exit__(None, None, None)


class _ReadOnlyQuery:
    def __init__(self, rows):
        self.rows = rows

    def select(self, _value):
        return self

    def eq(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class ContinuityStatusFieldTests(unittest.TestCase):
    """Requirement section five: status reflects CONTINUITY_* under old names."""

    def _status(self, continuity_overrides):
        values = {**CONTINUITY_ENV, **continuity_overrides}
        context = []
        _apply(_patch_config(**values, **ANALYSIS_ENV), context)
        from gateway.memory_continuity import get_continuity_status

        with (
            patch(f"{CONTINUITY_MODULE}.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch(f"{CONTINUITY_MODULE}._get_cursor", return_value={
                "status": "ready", "last_processed_message_id": 177,
            }),
            patch(f"{CONTINUITY_MODULE}._latest_message", return_value=None),
            patch(f"{CONTINUITY_MODULE}._backlog_count", return_value=0),
            patch(f"{CONTINUITY_MODULE}.list_continuity_runs", return_value=[]),
        ):
            try:
                return get_continuity_status()
            finally:
                for item in context:
                    item.__exit__(None, None, None)

    def test_status_reports_continuity_model_under_legacy_field_names(self):
        result = self._status({})
        self.assertEqual(result["analysis_model"], "continuity-model")
        self.assertTrue(result["analysis_configured"])
        self.assertEqual(result["continuity_model"], "continuity-model")
        self.assertTrue(result["continuity_configured"])

    def test_status_reports_unconfigured_continuity(self):
        result = self._status({"CONTINUITY_API_KEY": ""})
        self.assertFalse(result["analysis_configured"])
        self.assertFalse(result["continuity_configured"])
        self.assertEqual(result["continuity_model"], "continuity-model")


if __name__ == "__main__":
    unittest.main()
