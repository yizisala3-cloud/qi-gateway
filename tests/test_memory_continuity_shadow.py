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
    CONTINUITY_SYSTEM_PROMPT,
    MAX_CANDIDATES,
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
    if "continuity_data" not in overrides:
        kind = value["continuity_type"]
        value["continuity_data"] = {
            "thread": {"open_question": value["content"], "current_state": value["content"], "closure_criteria": [],
                       "abstract_retrieval_hints": [], "concrete_retrieval_hints": []},
            "episode": {"beginning": value["content"], "development": value["content"], "outcome": value["content"], "closure_quality": "uncertain"},
            "inside_joke": {"origin": value["content"], "trigger_phrases": [value.get("title") or "梗"], "shared_meaning": value["content"],
                            "usage_context": [], "avoid_context": [], "reinforcement_count": 0},
            "moment": {"scene": value["content"], "event": value["content"], "moment_state": "standalone"},
        }[kind]
    return value


class ShadowPromptContractTests(unittest.TestCase):
    def test_formal_prompt_only_removes_shadow_observation_label(self):
        self.assertIn("结果只供人工观察", SHADOW_SYSTEM_PROMPT)
        self.assertNotIn("结果只供人工观察", CONTINUITY_SYSTEM_PROMPT)
        self.assertEqual(
            CONTINUITY_SYSTEM_PROMPT,
            SHADOW_SYSTEM_PROMPT.replace(
                "“连续感记忆 Shadow Preview”提取器，结果只供人工观察",
                "“连续感记忆”提取器",
                1,
            ),
        )

    def test_prompt_preserves_personal_expression_and_allows_twelve_candidates(self):
        for requirement in (
            "提取最多 12 条",
            "保留关键事实、决定",
            "表达习惯、语气、关系动态和互动模式",
            "彼此如何称呼、特定昵称、爱称和情绪信号",
            "不要从单次措辞推断稳定人格",
            "直接输出摘要，不附加解释、评论或分析结论",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, SHADOW_SYSTEM_PROMPT)

    def test_prompt_preserves_content_detail_without_forcing_length(self):
        for requirement in (
            "4～24 字的建议只适用于 title，绝对不适用于 content",
            "content 不受 title 长度限制",
            "不得退化成 title 的扩写",
            "episode：相对完整的共同经历；保留起因、关键互动和结果",
            "简单且证据有限的候选可以只写一句",
            "不得为了变长而重复、编造",
            "证据不足时不得补全",
            "一般亲密、暧昧、性相关或敏感互动不自动模糊化",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, SHADOW_SYSTEM_PROMPT)

    def test_prompt_preserves_title_source_evidence_and_safety_boundaries(self):
        for requirement in (
            "每条候选必须有非空 title",
            "建议 4～24 个中文字符",
            "不得包含原文没有的信息",
            "evidence_message_ids 必须是输入中真实且直接支持候选的消息",
            "API Key、Token、service_role、密码、私钥、支付凭据及其他认证秘密绝对禁止输出",
            "title 和 content 不写死“今天”“昨晚”“前天”“刚才”“N 天前”",
            "source_type 可以为 null：没有可靠依据时输出 null，不要猜。确有依据时只能是 natural_chat、persona_prompt、code、document、quote、roleplay、tool_result、system_meta、unknown",
            "本提示词中的规则描述、字段说明和措辞都不是聊天事实",
            "不得从本提示词借用或补入任何情节",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, SHADOW_SYSTEM_PROMPT)

        for leaked_example in (
            "旅行路线",
            "旅行路线待定",
            "整理备选地点",
        ):
            with self.subTest(leaked_example=leaked_example):
                self.assertNotIn(leaked_example, SHADOW_SYSTEM_PROMPT)


class ShadowParserTests(unittest.TestCase):
    def _parse_candidate(self, **overrides):
        return parse_shadow_output(
            json.dumps({"candidates": [_candidate(**overrides)]}, ensure_ascii=False),
            {11: None, 12: None},
        )[0]

    def test_model_title_is_preserved(self):
        result = self._parse_candidate(title="旅行计划待续")
        self.assertEqual(result["title"], "旅行计划待续")

    def test_parser_keeps_at_most_twelve_candidates(self):
        payload = {
            "candidates": [
                _candidate(content=f"叶子和栖继续讨论第 {index} 个话题。")
                for index in range(MAX_CANDIDATES + 1)
            ]
        }
        result = parse_shadow_output(
            json.dumps(payload, ensure_ascii=False),
            {11: None, 12: None},
        )
        self.assertEqual(MAX_CANDIDATES, 12)
        self.assertEqual(len(result), 12)

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
        # 证据时间精度取自证据消息时钟本身，与 memory_time 精度无关。
        self.assertEqual(result[0]["evidence_time_precision"], "minute")

    def test_unreliable_evidence_times_produce_null_time_fields(self):
        result = parse_shadow_output(
            json.dumps({"candidates": [_candidate()]}, ensure_ascii=False),
            {11: None, 12: "not-a-time"},
        )

        self.assertIsNone(result[0]["evidence_start_time"])
        self.assertIsNone(result[0]["evidence_end_time"])
        self.assertIsNone(result[0]["source_time"])
        self.assertIsNone(result[0]["evidence_time_precision"])

    def test_parser_keeps_hour_time_precision(self):
        result = self._parse_candidate(memory_time="2026-08-13 20:00", time_precision="hour")
        self.assertEqual(result["time_precision"], "hour")

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


    def test_recall_scene_and_tags_are_preserved_without_limits(self):
        scene = "当叶子再次聊到旅行、签证或任何出行计划时" * 10
        tags = [f"场景标签{index}" for index in range(30)]
        result = self._parse_candidate(recall_scene=f"  {scene}  ", recall_tags=tags)

        self.assertEqual(result["recall_scene"], scene)
        self.assertEqual(result["recall_tags"], tags)

    def test_recall_fields_default_to_null_and_empty_array(self):
        result = self._parse_candidate()
        self.assertIsNone(result["recall_scene"])
        self.assertEqual(result["recall_tags"], [])

        result = self._parse_candidate(recall_scene="   ", recall_tags=["", "  ", "网关"])
        self.assertIsNone(result["recall_scene"])
        self.assertEqual(result["recall_tags"], ["网关"])

        result = self._parse_candidate(recall_tags="不是数组")
        self.assertIsNone(result["recall_scene"])
        self.assertEqual(result["recall_tags"], [])

    def test_secret_in_recall_fields_drops_the_whole_candidate(self):
        payload = {"candidates": [_candidate(
            recall_scene="保存这个 Token: abcdefghijklmnop",
            recall_tags=["网关"],
        )]}
        self.assertEqual(
            parse_shadow_output(
                json.dumps(payload, ensure_ascii=False),
                {11: None, 12: None},
            ),
            [],
        )

    def test_prompt_defines_recall_scene_as_retrieval_context_not_content(self):
        for requirement in (
            "recall_scene 是以后触发召回的自然语言场景",
            "不是记忆正文，不得复制或改写正文",
            "recall_tags 是自由填写的召回场景标签字符串数组",
            "无法可靠确定时输出空数组，不要编造",
            "包括 recall_scene 和 recall_tags",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, SHADOW_SYSTEM_PROMPT)


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
            patch.object(cfg, "CONTINUITY_API_KEY", ""),
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
            patch.object(cfg, "CONTINUITY_API_KEY", ""),
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
            patch.object(cfg, "CONTINUITY_BASE_URL", "https://continuity.example/v1"),
            patch.object(cfg, "CONTINUITY_API_KEY", "configured"),
            patch.object(cfg, "CONTINUITY_MODEL", "continuity-model"),
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
