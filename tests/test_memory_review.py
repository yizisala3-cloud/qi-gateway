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

from gateway.memory_requests import MemoryRequestError
from gateway.memory_review import review_ai_memory_request, review_memory_request, validate_review


MODULE = "gateway.memory_review"


class _RpcQuery:
    def __init__(self, client):
        self.client = client

    def execute(self):
        if self.client.error:
            raise self.client.error
        return SimpleNamespace(data=self.client.result)


class _RequestRowQuery:
    def __init__(self, client):
        self.client = client

    def select(self, fields):
        self.client.request_select = fields
        return self

    def eq(self, *_args):
        return self

    def limit(self, *_args):
        return self

    def execute(self):
        return SimpleNamespace(data=self.client.request_rows)


class _Client:
    def __init__(self, result=None, error=None, request_rows=None):
        self.result = result
        self.error = error
        self.request_rows = request_rows or []
        self.request_select = ""
        self.rpc_name = None
        self.rpc_payload = None

    def table(self, name):
        if name != "memory_requests":
            raise AssertionError(f"unexpected table access: {name}")
        return _RequestRowQuery(self)

    def rpc(self, name, payload):
        self.rpc_name = name
        self.rpc_payload = payload
        return _RpcQuery(self)


class ValidationTests(unittest.TestCase):
    def test_approve_normalizes_edits_and_hashes_content(self):
        review = validate_review(12, {
            "action": "APPROVE",
            "content": "  用户喜欢清晨散步。  ",
            "title": " 清晨偏好 ",
            "tags": "散步，清晨,散步",
            "importance": "8",
            "review_note": " 已核对 ",
        })

        self.assertEqual(review["request_id"], 12)
        self.assertEqual(review["action"], "approve")
        self.assertEqual(review["content"], "用户喜欢清晨散步。")
        self.assertEqual(review["tags"], ["散步", "清晨"])
        self.assertEqual(review["importance"], 8)
        self.assertEqual(len(review["content_hash"]), 64)
        self.assertEqual(review["review_note"], "已核对")
        self.assertIsNone(review["update_mode"])
        self.assertIsNone(review["memory_key"])
        self.assertIsNone(review["related_memory_id"])

    def test_replace_review_requires_a_stable_key(self):
        review = validate_review(12, {
            "action": "approve",
            "content": "qi-gateway 当前代码进度为 60%。",
            "importance": 7,
            "update_mode": "replace",
            "memory_key": "Project.QI-Gateway.Progress",
        })
        self.assertEqual(review["update_mode"], "replace")
        self.assertEqual(review["memory_key"], "project.qi-gateway.progress")

        with self.assertRaises(MemoryRequestError):
            validate_review(12, {
                "action": "approve",
                "content": "有效记忆内容",
                "update_mode": "replace",
            })

    def test_reject_does_not_accept_memory_edits(self):
        review = validate_review("9", {"action": "reject", "review_note": "不够稳定"})
        self.assertEqual(review["request_id"], 9)
        self.assertIsNone(review["content"])

        with self.assertRaises(MemoryRequestError) as raised:
            validate_review(9, {"action": "reject", "content": "不应提交"})
        self.assertEqual(raised.exception.code, "invalid_review")

    def test_rejects_invalid_action_and_importance(self):
        with self.assertRaises(MemoryRequestError):
            validate_review(1, {"action": "archive"})
        with self.assertRaises(MemoryRequestError):
            validate_review(1, {"action": "approve", "content": "有效记忆内容", "importance": 11})

    def test_duplicate_and_conflict_require_target_without_edits(self):
        for action in ("duplicate", "conflict"):
            with self.subTest(action=action):
                review = validate_review(3, {
                    "action": action,
                    "related_memory_id": "27",
                    "review_note": "人工核对",
                })
                self.assertEqual(review["related_memory_id"], 27)
                self.assertIsNone(review["content"])

        with self.assertRaises(MemoryRequestError):
            validate_review(3, {"action": "duplicate"})
        with self.assertRaises(MemoryRequestError):
            validate_review(3, {
                "action": "conflict",
                "related_memory_id": 27,
                "content": "不允许修改",
            })

    def test_merge_requires_target_and_edited_result(self):
        review = validate_review(4, {
            "action": "merge",
            "related_memory_id": 18,
            "content": "用户喜欢清晨在公园散步。",
            "title": "清晨散步偏好",
            "tags": ["清晨", "散步"],
            "importance": 8,
        })
        self.assertEqual(review["related_memory_id"], 18)
        self.assertEqual(review["action"], "merge")
        self.assertEqual(len(review["content_hash"]), 64)

        with self.assertRaises(MemoryRequestError):
            validate_review(4, {
                "action": "merge",
                "related_memory_id": 18,
                "content": "有效合并内容",
                "update_mode": "append",
            })


class PersistenceTests(unittest.TestCase):
    def test_ai_review_scope_and_reviewer_are_server_controlled(self):
        selected = {
            "id": 42,
            "continuity_type": "moment",
            "status": "pending",
            "content": "用户喜欢清晨散步。",
            "title": "清晨",
            "tags": ["清晨"],
            "importance": 7,
        }
        with (
            patch(f"{MODULE}.list_reviewable_memory_requests", return_value=[selected]),
            patch(f"{MODULE}.review_memory_request", return_value={"status": "approved"}) as review,
        ):
            result = review_ai_memory_request("assistant-1", 42, {"action": "approve"})
        self.assertEqual(result["status"], "approved")
        self.assertEqual(review.call_args.kwargs["reviewed_by"], "orangechat_ai")
        self.assertEqual(review.call_args.kwargs["allowed_types"], ("moment", "thread", "inside_joke"))
        self.assertEqual(review.call_args.args[1]["content"], selected["content"])

        with patch(f"{MODULE}.list_reviewable_memory_requests", return_value=[]):
            with self.assertRaises(MemoryRequestError) as raised:
                review_ai_memory_request("assistant-1", 99, {"action": "approve"})
        self.assertEqual(raised.exception.code, "request_not_reviewable")

    def test_approve_calls_atomic_rpc_and_returns_memory(self):
        client = _Client({
            "changed": True,
            "request": {
                "id": 42,
                "status": "approved",
                "memory_id": 77,
                "reviewed_at": "2026-08-02T02:00:00+00:00",
            },
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = review_memory_request(42, {
                "action": "approve",
                "content": "用户喜欢清晨散步。",
                "title": "清晨偏好",
                "tags": ["清晨"],
                "importance": 8,
            })

        self.assertEqual(result["memory_id"], 77)
        self.assertEqual(result["status"], "approved")
        self.assertTrue(result["changed"])
        self.assertEqual(client.rpc_name, "review_memory_request_v5")
        self.assertEqual(client.rpc_payload["p_request_id"], 42)
        self.assertEqual(client.rpc_payload["p_action"], "approve")
        self.assertEqual(client.rpc_payload["p_reviewed_by"], "gateway_admin")
        self.assertIsNone(client.rpc_payload["p_update_mode"])
        self.assertIsNone(client.rpc_payload["p_memory_key"])
        self.assertIsNone(client.rpc_payload["p_related_memory_id"])

    def test_reject_calls_same_atomic_rpc_without_memory_fields(self):
        client = _Client({
            "changed": True,
            "request": {"id": 8, "status": "rejected", "memory_id": None, "reviewed_at": "now"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = review_memory_request(8, {"action": "reject", "review_note": "不够稳定"})

        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(client.rpc_payload["p_content"])
        self.assertIsNone(client.rpc_payload["p_content_hash"])
        self.assertIsNone(client.rpc_payload["p_recall_embedding"])

    def test_approve_embeds_the_request_recall_scene_for_vector_recall(self):
        client = _Client(
            {
                "changed": True,
                "request": {"id": 42, "status": "approved", "memory_id": 77, "reviewed_at": "now"},
            },
            request_rows=[{"recall_scene": "当叶子再问部署进度时"}],
        )
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch("gateway.memory_requests._get_embedding_sync", return_value=[0.3, 0.4]) as embed,
        ):
            result = review_memory_request(42, {"action": "approve", "content": "用户喜欢清晨散步。"})

        embed.assert_called_once_with("当叶子再问部署进度时")
        self.assertEqual(client.rpc_payload["p_recall_embedding"], [0.3, 0.4])
        self.assertEqual(result["memory_id"], 77)

    def test_scene_less_request_keeps_null_recall_embedding(self):
        client = _Client(
            {
                "changed": True,
                "request": {"id": 43, "status": "approved", "memory_id": 78, "reviewed_at": "now"},
            },
            request_rows=[{"recall_scene": None}],
        )
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch("gateway.memory_extract._get_embedding_sync") as embed,
        ):
            review_memory_request(43, {"action": "approve", "content": "有效记忆内容。"})

        embed.assert_not_called()
        self.assertIsNone(client.rpc_payload["p_recall_embedding"])

    def test_merge_also_embeds_the_request_recall_scene(self):
        client = _Client(
            {
                "changed": True,
                "related_memory_id": 18,
                "request": {"id": 44, "status": "merged", "memory_id": 94, "reviewed_at": "now"},
            },
            request_rows=[{"recall_scene": "当旅行计划被提起时"}],
        )
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch("gateway.memory_requests._get_embedding_sync", return_value=[0.5]) as embed,
        ):
            review_memory_request(44, {
                "action": "merge",
                "related_memory_id": 18,
                "content": "合并后的旅行计划内容。",
            })

        embed.assert_called_once_with("当旅行计划被提起时")
        self.assertEqual(client.rpc_payload["p_recall_embedding"], [0.5])

    def test_recall_scene_read_failure_blocks_the_review(self):
        class _BrokenRequestClient(_Client):
            def table(self, name):
                raise RuntimeError("db down")

        client = _BrokenRequestClient({
            "changed": True,
            "request": {"id": 45, "status": "approved", "memory_id": 79, "reviewed_at": "now"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                review_memory_request(45, {"action": "approve", "content": "有效记忆内容。"})

        self.assertEqual(raised.exception.code, "recall_embedding_failed")
        # 审核 RPC 未被调用，申请保持原状态。
        self.assertIsNone(client.rpc_name)

    def test_recall_embedding_failure_blocks_the_approve(self):
        client = _Client(
            {
                "changed": True,
                "request": {"id": 46, "status": "approved", "memory_id": 81, "reviewed_at": "now"},
            },
            request_rows=[{"recall_scene": "当叶子再问部署进度时"}],
        )
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch(
                "gateway.memory_requests._get_embedding_sync",
                side_effect=RuntimeError("provider down"),
            ),
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                review_memory_request(46, {"action": "approve", "content": "有效记忆内容。"})

        self.assertEqual(raised.exception.code, "recall_embedding_failed")
        self.assertEqual(client.rpc_name, None)

    def test_recall_embedding_failure_blocks_the_merge(self):
        client = _Client(
            {
                "changed": True,
                "related_memory_id": 18,
                "request": {"id": 47, "status": "merged", "memory_id": 95, "reviewed_at": "now"},
            },
            request_rows=[{"recall_scene": "当旅行计划被提起时"}],
        )
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
            patch(
                "gateway.memory_requests._get_embedding_sync",
                side_effect=RuntimeError("provider down"),
            ),
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                review_memory_request(47, {
                    "action": "merge",
                    "related_memory_id": 18,
                    "content": "合并后的旅行计划内容。",
                })

        self.assertEqual(raised.exception.code, "recall_embedding_failed")
        self.assertEqual(client.rpc_name, None)

    def test_duplicate_calls_v4_with_selected_memory(self):
        client = _Client({
            "changed": True,
            "related_memory_id": 91,
            "request": {"id": 8, "status": "duplicate", "memory_id": 91, "reviewed_at": "now"},
        })
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            result = review_memory_request(8, {
                "action": "duplicate",
                "related_memory_id": 91,
            })

        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(result["related_memory_id"], 91)
        self.assertEqual(client.rpc_payload["p_related_memory_id"], 91)

    def test_requires_elevated_key_before_database_access(self):
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=False),
            patch(f"{MODULE}.get_client") as get_client,
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                review_memory_request(8, {"action": "reject"})

        self.assertEqual(raised.exception.code, "database_permissions_unavailable")
        get_client.assert_not_called()

    def test_maps_not_found_and_already_reviewed_errors(self):
        for message, code, status in (
            ("memory_request_not_found", "request_not_found", 404),
            ("memory_request_not_pending", "request_not_pending", 409),
        ):
            with self.subTest(message=message):
                client = _Client(error=RuntimeError(message))
                with (
                    patch(f"{MODULE}._server_writes_allowed", return_value=True),
                    patch(f"{MODULE}.get_client", return_value=client),
                ):
                    with self.assertRaises(MemoryRequestError) as raised:
                        review_memory_request(8, {"action": "reject"})
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.status_code, status)

    def test_maps_stale_mutable_fact_update_to_conflict(self):
        client = _Client(error=RuntimeError("memory_request_stale_update"))
        with (
            patch(f"{MODULE}._server_writes_allowed", return_value=True),
            patch(f"{MODULE}.get_client", return_value=client),
        ):
            with self.assertRaises(MemoryRequestError) as raised:
                review_memory_request(8, {
                    "action": "approve",
                    "content": "qi-gateway 当前代码进度为 50%。",
                    "importance": 7,
                    "update_mode": "replace",
                    "memory_key": "project.qi-gateway.progress",
                })

        self.assertEqual(raised.exception.code, "stale_update")
        self.assertEqual(raised.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()

