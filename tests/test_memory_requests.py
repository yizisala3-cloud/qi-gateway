import importlib.util
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


if "dotenv" not in sys.modules and importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from gateway.config import cfg
from gateway.memory_requests import (
    MemoryRequestError,
    create_memory_request,
    validate_memory_request,
)


MODULE = "gateway.memory_requests"


class _SourceQuery:
    def __init__(self, client):
        self.client = client

    def select(self, fields):
        self.client.source_select = fields
        return self

    def eq(self, field, value):
        self.client.source_filters.append((field, value))
        return self

    def limit(self, value):
        self.client.source_limit = value
        return self

    def execute(self):
        return SimpleNamespace(data=self.client.source_rows)


class _RpcQuery:
    def __init__(self, client):
        self.client = client

    def execute(self):
        if self.client.rpc_error:
            raise self.client.rpc_error
        return SimpleNamespace(data=self.client.rpc_result)


class _Client:
    def __init__(self, *, source_rows=None, rpc_result=None, rpc_error=None):
        self.source_rows = source_rows or []
        self.rpc_result = rpc_result
        self.rpc_error = rpc_error
        self.table_names = []
        self.source_filters = []
        self.source_select = ""
        self.source_limit = None
        self.rpc_name = ""
        self.rpc_payload = None

    def table(self, name):
        self.table_names.append(name)
        if name != "chat_messages":
            raise AssertionError(f"unexpected table access: {name}")
        return _SourceQuery(self)

    def rpc(self, name, payload):
        self.rpc_name = name
        self.rpc_payload = payload
        return _RpcQuery(self)


def _payload(**overrides):
    payload = {
        "assistant_id": "assistant-1",
        "content": "用户希望以后尽量安排安静的清晨活动。",
        "reason": "这是长期稳定的生活偏好。",
        "title": "清晨偏好",
        "tags": "偏好，清晨, 安静,偏好",
        "importance": 7,
        "continuity_type": "profile",
        "continuity_data": {
            "facet": "daily_rhythm", "statement": "用户偏好安静的清晨活动", "scope": "daily_life",
            "stability": "stable", "exceptions": [], "basis": "explicit_preference",
        },
    }
    payload.update(overrides)
    return payload


class ValidationTests(unittest.TestCase):
    def test_normalizes_request_and_builds_deterministic_idempotency_key(self):
        first = validate_memory_request(_payload())
        second = validate_memory_request(_payload(content="  用户希望以后尽量安排安静的清晨活动。  "))

        self.assertEqual(first["content"], "用户希望以后尽量安排安静的清晨活动。")
        self.assertEqual(first["tags"], ["偏好", "清晨", "安静"])
        self.assertEqual(first["importance"], 7)
        self.assertEqual(first["update_mode"], "append")
        self.assertIsNone(first["memory_key"])
        self.assertEqual(len(first["content_hash"]), 64)
        self.assertEqual(first["content_hash"], second["content_hash"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])

    def test_accepts_a_safe_explicit_idempotency_key(self):
        result = validate_memory_request(_payload(), "orangechat:message:123")
        self.assertEqual(result["idempotency_key"], "orangechat:message:123")

    def test_rejects_unknown_fields_and_invalid_importance(self):
        with self.assertRaises(MemoryRequestError) as unknown:
            validate_memory_request(_payload(service_role_key="secret"))
        self.assertEqual(unknown.exception.code, "invalid_payload")

        with self.assertRaises(MemoryRequestError) as importance:
            validate_memory_request(_payload(importance=11))
        self.assertEqual(importance.exception.code, "invalid_payload")

    def test_rejects_an_unsafe_idempotency_key(self):
        with self.assertRaises(MemoryRequestError) as raised:
            validate_memory_request(_payload(), "bad key")
        self.assertEqual(raised.exception.code, "invalid_idempotency_key")

    def test_replace_mode_requires_and_normalizes_a_stable_memory_key(self):
        result = validate_memory_request(_payload(
            content="qi-gateway 当前代码进度为 60%。",
            update_mode="REPLACE",
            memory_key=" Project.QI-Gateway.Progress ",
        ))

        self.assertEqual(result["update_mode"], "replace")
        self.assertEqual(result["memory_key"], "project.qi-gateway.progress")

        with self.assertRaises(MemoryRequestError):
            validate_memory_request(_payload(update_mode="replace"))
        with self.assertRaises(MemoryRequestError):
            validate_memory_request(_payload(memory_key="project.progress"))
        with self.assertRaises(MemoryRequestError):
            validate_memory_request(_payload(
                update_mode="replace",
                memory_key="包含中文的键",
            ))

    def test_recall_scene_and_tags_normalize_without_business_limits(self):
        long_scene = "当叶子再次聊到网关部署" * 100
        many_tags = [f"场景{index}" for index in range(50)]
        result = validate_memory_request(_payload(
            recall_scene=f"  {long_scene}  ",
            recall_tags=[" 当", "当", "网关", "", many_tags[0], *many_tags],
        ))

        self.assertEqual(result["recall_scene"], long_scene)
        self.assertEqual(result["recall_tags"], ["当", "网关", *many_tags])

        self.assertIsNone(validate_memory_request(_payload())["recall_scene"])
        self.assertIsNone(validate_memory_request(_payload())["recall_tags"])
        self.assertIsNone(validate_memory_request(_payload(recall_scene="   "))["recall_scene"])
        self.assertIsNone(validate_memory_request(_payload(recall_tags=[]))["recall_tags"])

        with self.assertRaises(MemoryRequestError) as scene_type:
            validate_memory_request(_payload(recall_scene=123))
        self.assertEqual(scene_type.exception.code, "invalid_payload")
        with self.assertRaises(MemoryRequestError) as tags_type:
            validate_memory_request(_payload(recall_tags="网关,部署"))
        self.assertEqual(tags_type.exception.code, "invalid_payload")
        with self.assertRaises(MemoryRequestError) as tag_entry:
            validate_memory_request(_payload(recall_tags=[42]))
        self.assertEqual(tag_entry.exception.code, "invalid_payload")


class PersistenceTests(unittest.TestCase):
    def test_creates_pending_request_through_atomic_rpc(self):
        client = _Client(rpc_result={
            "created": True,
            "request": {
                "id": 42,
                "status": "pending",
                "created_at": "2026-08-02T01:00:00+00:00",
            },
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch.object(cfg, "MEMORY_REQUEST_RATE_LIMIT", 6),
        ):
            result = create_memory_request(_payload())

        self.assertEqual(result["request_id"], 42)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result["created"])
        self.assertFalse(result["deduplicated"])
        self.assertEqual(client.rpc_name, "create_memory_request_v4")
        self.assertEqual(client.rpc_payload["p_rate_limit"], 6)
        self.assertEqual(client.rpc_payload["p_update_mode"], "append")
        self.assertIsNone(client.rpc_payload["p_memory_key"])
        self.assertEqual(client.rpc_payload["p_source"], "orangechat_plugin")
        self.assertNotIn("plugin_token", client.rpc_payload)
        self.assertNotIn("service_role", " ".join(client.rpc_payload))
        self.assertEqual(client.table_names, [])

    def test_low_risk_type_uses_atomic_direct_writer(self):
        client = _Client(rpc_result={
            "created": True,
            "request": {"id": 44, "status": "approved", "memory_id": 88, "continuity_type": "moment"},
        })
        payload = _payload(
            continuity_type="moment",
            continuity_data={"scene": "聊天窗口", "event": "确认计划", "moment_state": "standalone"},
        )
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = create_memory_request(payload, source="mcp_memory")
        self.assertEqual(client.rpc_name, "write_memory_direct_v1")
        self.assertEqual(client.rpc_payload["p_source"], "mcp_memory")
        self.assertEqual(client.rpc_payload["p_reviewed_by"], "orangechat_ai")
        self.assertFalse(result["requires_user_review"])
        self.assertEqual(result["memory_id"], 88)

    def test_all_six_types_follow_server_controlled_business_split(self):
        payloads = {
            "moment": (None, {"scene": "聊天", "event": "确认", "moment_state": "standalone"}),
            "thread": ("open", {"open_question": "下一步", "current_state": "待继续", "closure_criteria": []}),
            "inside_joke": (None, {"origin": "口误", "trigger_phrases": ["小橘子"], "shared_meaning": "共同玩笑"}),
            "episode": (None, {"beginning": "开始", "development": "讨论", "outcome": "确认", "closure_quality": "complete"}),
            "profile": (None, {"facet": "偏好", "statement": "喜欢清晨", "scope": "日常", "stability": "stable", "basis": "explicit_preference"}),
            "interaction_rule": (None, {"trigger": "求助", "expected_behavior": "先给结论", "scope": "对话", "priority": 8, "rule_state": "active", "explicit_instruction": "用户明确要求先给结论"}),
        }
        for kind, (state, data) in payloads.items():
            with self.subTest(kind=kind):
                automatic = kind in {"moment", "thread", "inside_joke"}
                client = _Client(rpc_result={
                    "created": True,
                    "request": {
                        "id": 50,
                        "status": "approved" if automatic else "pending",
                        "memory_id": 90 if automatic else None,
                        "continuity_type": kind,
                    },
                })
                extra = {}
                if kind == "interaction_rule":
                    extra = {"update_mode": "replace", "memory_key": "rule.reply-order"}
                with (
                    patch(f"{MODULE}._server_writes_allowed", return_value=True),
                    patch(f"{MODULE}.get_client", return_value=client),
                ):
                    result = create_memory_request(_payload(
                        continuity_type=kind,
                        thread_state=state,
                        continuity_data=data,
                        **extra,
                    ))
                self.assertEqual(
                    client.rpc_name,
                    "write_memory_direct_v1" if automatic else "create_memory_request_v4",
                )
                self.assertEqual(result["requires_user_review"], not automatic)
                self.assertNotIn("p_proposed_relations", client.rpc_payload)

    def test_server_controlled_assistant_cannot_be_overridden(self):
        with self.assertRaises(MemoryRequestError) as raised:
            create_memory_request(_payload(), source="mcp_memory", assistant_id="server-assistant")
        self.assertEqual(raised.exception.code, "invalid_payload")

    def test_recall_fields_reach_the_pending_request_rpc_without_embedding(self):
        client = _Client(rpc_result={
            "created": True,
            "request": {"id": 45, "status": "pending", "created_at": "now"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            create_memory_request(_payload(
                recall_scene="当叶子再问部署进度时",
                recall_tags=["网关", "部署"],
            ))

        self.assertEqual(client.rpc_payload["p_recall_scene"], "当叶子再问部署进度时")
        self.assertEqual(client.rpc_payload["p_recall_tags"], ["网关", "部署"])
        self.assertNotIn("p_recall_embedding", client.rpc_payload)

    def test_direct_writer_embeds_recall_scene_for_the_vector_channel(self):
        client = _Client(rpc_result={
            "created": True,
            "request": {"id": 46, "status": "approved", "memory_id": 91, "continuity_type": "moment"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch(f"{MODULE}._get_embedding_sync", return_value=[0.1, 0.2]) as embed,
        ):
            create_memory_request(_payload(
                continuity_type="moment",
                continuity_data={"scene": "聊天", "event": "确认", "moment_state": "standalone"},
                recall_scene="当叶子提到那天的约定时",
                recall_tags=["约定"],
            ), source="mcp_memory")

        embed.assert_called_once_with("当叶子提到那天的约定时")
        self.assertEqual(client.rpc_payload["p_recall_embedding"], [0.1, 0.2])

    def test_blank_recall_scene_never_generates_a_recall_embedding(self):
        client = _Client(rpc_result={
            "created": True,
            "request": {"id": 47, "status": "approved", "memory_id": 92, "continuity_type": "moment"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch(f"{MODULE}._get_embedding_sync") as embed,
        ):
            create_memory_request(_payload(
                continuity_type="moment",
                continuity_data={"scene": "聊天", "event": "确认", "moment_state": "standalone"},
            ), source="mcp_memory")

        embed.assert_not_called()
        self.assertIsNone(client.rpc_payload["p_recall_embedding"])

    def test_recall_embedding_failure_degrades_without_failing_the_request(self):
        client = _Client(rpc_result={
            "created": True,
            "request": {"id": 48, "status": "approved", "memory_id": 93, "continuity_type": "moment"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch(f"{MODULE}._get_embedding_sync", side_effect=RuntimeError("provider down")),
        ):
            result = create_memory_request(_payload(
                continuity_type="moment",
                continuity_data={"scene": "聊天", "event": "确认", "moment_state": "standalone"},
                recall_scene="当叶子提到那天的约定时",
            ), source="mcp_memory")

        self.assertEqual(result["memory_id"], 93)
        self.assertIsNone(client.rpc_payload["p_recall_embedding"])

    def test_duplicate_request_returns_existing_pending_application(self):
        client = _Client(rpc_result={
            "created": False,
            "request": {"id": 42, "status": "pending", "created_at": "now"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = create_memory_request(_payload())

        self.assertFalse(result["created"])
        self.assertTrue(result["deduplicated"])
        self.assertEqual(result["request_id"], 42)

    def test_optional_source_is_verified_with_read_only_chat_query(self):
        client = _Client(
            source_rows=[{
                "id": 99,
                "assistant_id": "assistant-1",
                "conversation_id": "conversation-1",
            }],
            rpc_result={
                "created": True,
                "request": {"id": 43, "status": "pending", "created_at": "now"},
            },
        )
        payload = _payload(conversation_id="conversation-1", source_message_id=99)
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = create_memory_request(payload)

        self.assertEqual(result["request_id"], 43)
        self.assertEqual(client.table_names, ["chat_messages"])
        self.assertEqual(client.source_select, "id,assistant_id,conversation_id")
        self.assertEqual(client.source_filters, [("id", 99)])

    def test_source_from_another_assistant_is_rejected_before_rpc(self):
        client = _Client(source_rows=[{
            "id": 99,
            "assistant_id": "assistant-2",
            "conversation_id": "conversation-1",
        }])
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                create_memory_request(_payload(source_message_id=99))

        self.assertEqual(raised.exception.code, "source_mismatch")
        self.assertEqual(client.rpc_name, "")

    def test_requires_elevated_server_key_before_database_access(self):
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=False),
            patch(f"{MODULE}.get_client") as get_client,
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                create_memory_request(_payload())

        self.assertEqual(raised.exception.code, "database_permissions_unavailable")
        self.assertEqual(raised.exception.status_code, 503)
        get_client.assert_not_called()

    def test_database_rate_limit_is_returned_as_http_429_error(self):
        client = _Client(rpc_error=RuntimeError("memory_request_rate_limited"))
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                create_memory_request(_payload())

        self.assertEqual(raised.exception.code, "rate_limited")
        self.assertEqual(raised.exception.status_code, 429)


if __name__ == "__main__":
    unittest.main()

