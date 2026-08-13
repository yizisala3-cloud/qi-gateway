import asyncio
import importlib.util
import json
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Match the existing memory extraction suite: production installs python-dotenv,
# while unit tests only need an import-time placeholder in a bare environment.
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
from gateway.memory_continuity_shadow import (
    SHADOW_SYSTEM_PROMPT,
    _normalize_messages,
    parse_shadow_output,
    run_shadow_preview,
)
from gateway.memory_digest_api import continuity_shadow_preview


MODULE = "gateway.memory_continuity_shadow"
API_MODULE = "gateway.memory_digest_api"


def _candidate(**overrides):
    value = {
        "content": "叶子和栖约好下次继续讨论旅行计划。",
        "continuity_type": "thread",
        "subject": "shared",
        "source_type": "natural_chat",
        "thread_state": "open",
        "importance": 5,
        "continuity_value": 9,
        "confidence": 0.9,
        "evidence_message_ids": [11, 12],
        "memory_time": None,
        "time_precision": "unknown",
        "title": "旅行计划待续",
        "participants": ["yezi", "qi"],
        "reason": "下个窗口需要继续这个计划。",
        "retention_class": "normal",
    }
    value.update(overrides)
    return value


class ShadowPromptContractTests(unittest.TestCase):
    def test_prompt_requires_concrete_evidence_bounded_summaries(self):
        self.assertIn(
            "不得用“某种方式”“特殊的方式”“极端的方式”",
            SHADOW_SYSTEM_PROMPT,
        )
        for vague_phrase in (
            "某种方式",
            "特殊的方式",
            "极端的方式",
            "发生了一些事情",
            "进行了一些互动",
        ):
            with self.subTest(vague_phrase=vague_phrase):
                self.assertIn(vague_phrase, SHADOW_SYSTEM_PROMPT)

        for requirement in (
            "直白、具体、客观",
            "必须保留理解候选所需的关键动作",
            "不得自行补全",
            "不要仅因敏感而排除或自动模糊化",
            "内部梗、具体称呼、共同玩法、承诺和约定",
            "绝不能当作输入事实",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, SHADOW_SYSTEM_PROMPT)

    def test_prompt_requires_a_specific_title_for_every_candidate(self):
        for requirement in (
            "每条 candidate 都必须包含非空 title 字段",
            "简短、具体、便于一眼识别的中文标题",
            "建议 4～24 个中文字符",
            "不得包含原文没有的信息",
            "原神至冬地图讨论",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, SHADOW_SYSTEM_PROMPT)


class ShadowParserTests(unittest.TestCase):
    def _parse_candidate(self, **overrides):
        return parse_shadow_output(
            json.dumps({"candidates": [_candidate(**overrides)]}, ensure_ascii=False),
            {11: None, 12: None},
        )[0]

    def test_model_title_is_preserved(self):
        result = self._parse_candidate(title="旅行计划待续")
        self.assertEqual(result["title"], "旅行计划待续")

    def test_missing_title_uses_content_fallback(self):
        candidate = _candidate()
        candidate.pop("title")
        result = parse_shadow_output(
            json.dumps({"candidates": [candidate]}, ensure_ascii=False),
            {11: None, 12: None},
        )[0]
        self.assertEqual(result["title"], "叶子和栖约好下次继续讨论旅行计划")

    def test_blank_title_uses_content_fallback(self):
        result = self._parse_candidate(title="   ")
        self.assertEqual(result["title"], "叶子和栖约好下次继续讨论旅行计划")

    def test_placeholder_titles_use_content_fallback(self):
        for title in ("（无标题）", "untitled"):
            with self.subTest(title=title):
                result = self._parse_candidate(title=title)
                self.assertEqual(result["title"], "叶子和栖约好下次继续讨论旅行计划")

    def test_fallback_title_is_at_most_twenty_four_characters(self):
        result = self._parse_candidate(
            title=None,
            content="叶子和栖约好下次继续讨论一个需要反复确认细节的很长旅行计划。后续说明。",
        )
        self.assertLessEqual(len(result["title"]), 24)
        self.assertTrue(result["title"].endswith("…"))

    def test_parser_accepts_core_candidate_types_and_normalizes_thread_state(self):
        payload = {
            "candidates": [
                _candidate(),
                _candidate(
                    content="叶子和栖一起完成了一次深夜排障。",
                    continuity_type="episode",
                    thread_state="open",
                    evidence_message_ids=[12],
                ),
                _candidate(
                    content="叶子和栖把‘小灯塔’当作内部称呼。",
                    continuity_type="inside_joke",
                    thread_state="resolved",
                    evidence_message_ids=[11],
                ),
            ]
        }
        result = parse_shadow_output(
            json.dumps(payload, ensure_ascii=False),
            {11: "2026-08-13T20:00+08:00", 12: "2026-08-13T20:05+08:00"},
        )

        self.assertEqual([item["continuity_type"] for item in result], [
            "thread", "episode", "inside_joke",
        ])
        self.assertEqual(result[0]["thread_state"], "open")
        self.assertIsNone(result[1]["thread_state"])
        self.assertIsNone(result[2]["thread_state"])

    def test_invalid_evidence_is_removed_and_candidate_without_evidence_is_dropped(self):
        payload = {"candidates": [
            _candidate(evidence_message_ids=[11, 999]),
            _candidate(content="没有真实证据的候选。", evidence_message_ids=[999]),
        ]}
        result = parse_shadow_output(
            json.dumps(payload, ensure_ascii=False),
            {11: "2026-08-13T20:00+08:00"},
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["evidence_message_ids"], [11])

    def test_evidence_ids_are_limited_to_eight(self):
        evidence_times = {message_id: None for message_id in range(1, 11)}
        result = parse_shadow_output(
            json.dumps({"candidates": [_candidate(evidence_message_ids=list(range(1, 11)))]}, ensure_ascii=False),
            evidence_times,
        )

        self.assertEqual(result[0]["evidence_message_ids"], list(range(1, 9)))

    def test_evidence_time_range_and_source_time_are_computed_from_valid_evidence(self):
        result = parse_shadow_output(
            json.dumps({"candidates": [_candidate(
                evidence_start_time="2099-01-01T00:00Z",
                evidence_end_time="2099-01-02T00:00Z",
                source_time="2099-01-02T00:00Z",
            )]}, ensure_ascii=False),
            {11: "2026-08-13T20:00+08:00", 12: "2026-08-13T20:05+08:00"},
        )

        self.assertEqual(result[0]["evidence_start_time"], "2026-08-13T20:00+08:00")
        self.assertEqual(result[0]["evidence_end_time"], "2026-08-13T20:05+08:00")
        self.assertEqual(result[0]["source_time"], "2026-08-13T20:05+08:00")

    def test_unreliable_evidence_times_produce_null_time_fields(self):
        result = parse_shadow_output(
            json.dumps({"candidates": [_candidate()]}, ensure_ascii=False),
            {11: None, 12: "not-a-time"},
        )

        self.assertIsNone(result[0]["evidence_start_time"])
        self.assertIsNone(result[0]["evidence_end_time"])
        self.assertIsNone(result[0]["source_time"])

    def test_obvious_credentials_drop_entire_candidate(self):
        for secret in (
            "API Key: sk-live_1234567890123456",
            "Token: abcdefghijklmnop",
            "密码: correct-horse-battery-staple",
        ):
            with self.subTest(secret=secret):
                payload = {"candidates": [_candidate(content=f"叶子保存了 {secret}")]}
                self.assertEqual(
                    parse_shadow_output(
                        json.dumps(payload, ensure_ascii=False),
                        {11: None, 12: None},
                    ),
                    [],
                )


class ShadowSamplingTests(unittest.TestCase):
    def test_assistant_retries_fold_only_within_same_conversation(self):
        messages, _ = _normalize_messages([
            {"id": 1, "conversation_id": "a", "role": "assistant", "content": "retry"},
            {"id": 2, "conversation_id": "a", "role": "assistant", "content": "final"},
            {"id": 3, "conversation_id": "b", "role": "assistant", "content": "other"},
        ], 16000)

        self.assertEqual([item["id"] for item in messages], [2, 3])

    def test_latest_unanswered_user_message_is_retained(self):
        messages, _ = _normalize_messages([
            {"id": 1, "conversation_id": "a", "role": "user", "content": "first"},
            {"id": 2, "conversation_id": "a", "role": "assistant", "content": "answer"},
            {"id": 3, "conversation_id": "a", "role": "user", "content": "next time continue"},
        ], 16000)

        self.assertEqual(messages[-1]["id"], 3)
        self.assertEqual(messages[-1]["role"], "user")


class _ReadOnlyQuery:
    def __init__(self, client):
        self.client = client

    def select(self, value):
        self.client.selects.append(value)
        return self

    def eq(self, *_args):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=self.client.rows)

    def __getattr__(self, name):
        if name in {"insert", "update", "delete", "upsert"}:
            raise AssertionError(f"database write attempted: {name}")
        raise AttributeError(name)


class _ReadOnlyClient:
    def __init__(self, rows):
        self.rows = rows
        self.tables = []
        self.selects = []

    def table(self, name):
        if name != "chat_messages":
            raise AssertionError(f"unexpected table access: {name}")
        self.tables.append(name)
        return _ReadOnlyQuery(self)

    def rpc(self, name, *_args, **_kwargs):
        raise AssertionError(f"RPC attempted: {name}")


class ShadowBoundaryTests(unittest.TestCase):
    def test_completely_empty_source_returns_success_without_configured_assistant(self):
        client = _ReadOnlyClient([])
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", ""),
            patch.object(cfg, "MEMORY_ASSISTANT_ID", ""),
            patch(f"{MODULE}._client", return_value=client),
        ):
            result = run_shadow_preview(80, 16000)

        self.assertEqual(result["assistant_id"], "")
        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["candidates"], [])

    def test_no_chat_returns_empty_success_without_model_call(self):
        client = _ReadOnlyClient([])
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", ""),
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "assistant-1"),
            patch(f"{MODULE}._client", return_value=client),
            patch(f"{MODULE}._extract_shadow_candidates") as extract,
        ):
            result = run_shadow_preview(80, 16000)

        self.assertEqual(result["message_count"], 0)
        self.assertEqual(result["candidates"], [])
        extract.assert_not_called()

    def test_shadow_only_selects_chat_messages_and_has_no_production_side_effects(self):
        client = _ReadOnlyClient([
            {
                "id": 11,
                "assistant_id": "assistant-1",
                "conversation_id": "conv-a",
                "role": "user",
                "content": "下次继续这个话题",
                "created_at": "2026-08-13T20:00:00+08:00",
            },
        ])
        with (
            patch.object(cfg, "ANALYSIS_API_KEY", "configured"),
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "assistant-1"),
            patch(f"{MODULE}._client", return_value=client),
            patch(f"{MODULE}._extract_shadow_candidates", return_value=[]),
            patch("gateway.memory_extract._get_embedding_sync") as embedding,
            patch("gateway.memory_extract._get_cursor") as cursor,
            patch("gateway.memory_extract._claim_slot") as claim,
            patch("gateway.memory_extract._update_heartbeat") as heartbeat,
        ):
            result = run_shadow_preview(80, 16000)

        self.assertEqual(result["persistence"], "none")
        self.assertEqual(result["message_count"], 1)
        self.assertEqual(client.tables, ["chat_messages"])
        self.assertEqual(len(client.selects), 1)
        embedding.assert_not_called()
        cursor.assert_not_called()
        claim.assert_not_called()
        heartbeat.assert_not_called()


class _Request:
    def __init__(self, body=None, token="secret"):
        self._body = {} if body is None else body
        self.headers = {"authorization": f"Bearer {token}"}

    async def json(self):
        return self._body


class ShadowApiTests(unittest.TestCase):
    def _call(self, request):
        return asyncio.run(continuity_shadow_preview(request))

    def test_api_requires_gateway_token(self):
        with patch.object(cfg, "GATEWAY_TOKEN", "secret"):
            response = self._call(_Request(token="wrong"))
        self.assertEqual(response.status_code, 401)

    def test_api_rejects_invalid_parameters(self):
        with patch.object(cfg, "GATEWAY_TOKEN", "secret"):
            response = self._call(_Request({"max_messages": 0, "max_chars": 16000}))
        self.assertEqual(response.status_code, 400)

    def test_api_returns_shadow_result(self):
        expected = {
            "mode": "shadow_preview",
            "persistence": "none",
            "assistant_id": "assistant-1",
            "source_first_message_id": 11,
            "source_last_message_id": 12,
            "message_count": 2,
            "candidates": [],
            "warnings": [],
        }
        with (
            patch.object(cfg, "GATEWAY_TOKEN", "secret"),
            patch(f"{API_MODULE}.run_shadow_preview", return_value=expected) as runner,
        ):
            response = self._call(_Request({"max_messages": 80, "max_chars": 16000}))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), expected)
        runner.assert_called_once_with(80, 16000)


if __name__ == "__main__":
    unittest.main()
