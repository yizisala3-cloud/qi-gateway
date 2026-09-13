from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from gateway.memory_continuity import (
    AUTO_THRESHOLD,
    INITIAL_CURSOR,
    ContinuityPipelineError,
    _enrich_candidates,
    prepare_continuity_batch,
    run_continuity_digest,
    run_continuity_digest_if_due,
    skip_blocked_continuity_batch,
)
from gateway.config import cfg
from gateway.memory_digest_api import (
    continuity_execute,
    continuity_skip_blocked,
    continuity_status,
)
from gateway.memory_extract import DigestPipelineError


def _row(message_id: int, role: str = "user", content: str = "消息", conversation: str = "c1"):
    return {
        "id": message_id,
        "assistant_id": "assistant-1",
        "conversation_id": conversation,
        "role": role,
        "content": content,
        "created_at": "2026-08-16T10:00:00+08:00",
    }


def _candidate():
    return {
        "content": "叶子和栖约好下次继续处理网关问题。",
        "continuity_type": "thread",
        "subject": "project",
        "source_type": "natural_chat",
        "thread_state": "open",
        "importance": 6,
        "continuity_value": 9,
        "confidence": 0.9,
        "evidence_message_ids": [178],
        "evidence_start_time": "2026-08-16T10:00+08:00",
        "evidence_end_time": "2026-08-16T10:00+08:00",
        "source_time": "2026-08-16T10:00+08:00",
        "memory_time": None,
        "time_precision": "unknown",
        "title": "网关问题待续",
        "participants": ["yezi", "qi"],
        "reason": "下次需要继续处理。",
        "retention_class": "normal",
    }


class _RpcClient:
    def __init__(self, *, commit_data=1, fail_commit=False, fail_message="commit failed",
                 pause_data=None):
        self.commit_data = commit_data
        self.fail_commit = fail_commit
        self.fail_message = fail_message
        self.pause_data = pause_data
        self.rpc_names = []
        self.table_updates = []

    def rpc(self, name, params):
        self.rpc_names.append(name)
        if self.fail_commit and name == "commit_memory_continuity_run":
            raise RuntimeError(self.fail_message)
        if name == "pause_memory_continuity_empty" and self.pause_data is not None:
            data = self.pause_data
        elif name == "pause_memory_continuity_empty":
            data = {}
        else:
            data = self.commit_data
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))

    def table(self, name):
        table = Mock()
        table.update.side_effect = lambda payload: (
            self.table_updates.append((name, payload)),
            table.update.return_value,
        )[1]
        return table


class ContinuityBatchTests(unittest.TestCase):
    def test_initial_cursor_and_threshold_contract(self):
        self.assertEqual(INITIAL_CURSOR, 177)
        self.assertEqual(AUTO_THRESHOLD, 80)

    def test_character_budget_keeps_complete_turn_and_cursor_range(self):
        rows = [
            _row(178, "user", "a" * 400, "c1"),
            _row(179, "assistant", "b" * 400, "c1"),
            _row(180, "user", "c" * 400, "c2"),
            _row(181, "assistant", "d" * 400, "c2"),
        ]
        raw, messages = prepare_continuity_batch(rows, max_chars=1100)
        self.assertEqual([row["id"] for row in raw], [178, 179])
        self.assertEqual([row["id"] for row in messages], [178, 179])

    def test_oversized_first_turn_is_complete_but_model_text_is_trimmed(self):
        rows = [
            _row(178, "user", "用户粘贴" + "a" * 900, "c1"),
            _row(179, "assistant", "最终回复" + "b" * 900, "c1"),
            _row(180, "user", "下一个 turn 不应进入", "c2"),
        ]

        raw, messages = prepare_continuity_batch(rows, max_chars=500)

        self.assertEqual([row["id"] for row in raw], [178, 179])
        self.assertEqual([row["id"] for row in messages], [178, 179])
        self.assertLessEqual(sum(len(row["content"]) + 100 for row in messages), 500)
        self.assertTrue(messages[0]["content"])
        self.assertTrue(messages[1]["content"])
        for message in messages:
            for field in ("id", "role", "conversation_id", "source_time"):
                self.assertIn(field, message)

    def test_assistant_retries_fold_only_within_same_conversation(self):
        rows = [
            _row(178, "user", "问题", "c1"),
            _row(179, "assistant", "旧回复", "c1"),
            _row(180, "assistant", "最终回复", "c1"),
            _row(181, "assistant", "另一窗口回复", "c2"),
        ]
        raw, messages = prepare_continuity_batch(rows)
        self.assertEqual(len(raw), 4)
        self.assertEqual([row["id"] for row in messages], [178, 180, 181])


class ContinuityExecutionTests(unittest.TestCase):
    def setUp(self):
        self.cursor = {
            "status": "ready",
            "last_processed_message_id": 177,
            "manual_cooldown_until": None,
            "auto_cooldown_until": None,
        }
        self.rows = [_row(178), _row(179, "assistant", "回应")]
        self.run = {
            "id": 91,
            "pipeline": "continuity",
            "trigger": "continuity_manual",
            "mode": "execute",
            "status": "succeeded",
            "source_first_message_id": 178,
            "source_last_message_id": 179,
            "message_count": 2,
            "extracted_count": 1,
            "inserted_count": 1,
            "preview_memories": [_candidate()],
        }

    def _patch_success(self, client, *, candidates=None):
        return (
            patch("gateway.memory_continuity._continuity_analysis_configured", return_value=True),
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value=dict(self.cursor)),
            patch("gateway.memory_continuity._fetch_rows_after", return_value=list(self.rows)),
            patch("gateway.memory_continuity._claim_slot", return_value={"status": "claimed", "run_id": 91}),
            patch("gateway.memory_continuity._set_running_run", return_value={"id": 91}),
            patch("gateway.memory_continuity.extract_continuity_candidates", return_value=[_candidate()] if candidates is None else candidates),
            patch("gateway.memory_continuity._enrich_candidates", return_value=[{**_candidate(), "embedding": [0.1], "content_hash": "a" * 64}]),
            patch("gateway.memory_continuity._load_run", return_value=dict(self.run)),
            patch("gateway.memory_continuity._update_heartbeat"),
            patch("gateway.memory_continuity._client", return_value=client),
        )

    def test_automatic_below_threshold_does_not_claim_or_call_model(self):
        with (
            patch("gateway.memory_continuity._continuity_analysis_configured", return_value=True),
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value=dict(self.cursor)),
            patch("gateway.memory_continuity._backlog_count", return_value=79),
            patch("gateway.memory_continuity._claim_slot") as claim,
            patch("gateway.memory_continuity.extract_continuity_candidates") as model,
        ):
            with self.assertRaisesRegex(ContinuityPipelineError, "below the automatic threshold"):
                run_continuity_digest(automatic=True)
        claim.assert_not_called()
        model.assert_not_called()

    def test_oversized_first_turn_advances_only_to_that_turns_last_raw_message(self):
        self.rows = [
            _row(178, "user", "x" * 17000, "c1"),
            _row(179, "assistant", "y" * 1000, "c1"),
            _row(180, "user", "next turn", "c2"),
        ]
        client = _RpcClient()
        patches = self._patch_success(client)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            result = run_continuity_digest()

        self.assertEqual(result["cursor_after"], 179)

    def test_automatic_threshold_executes_and_manual_ignores_auto_cooldown(self):
        client = _RpcClient()
        self.cursor["auto_cooldown_until"] = (_now() + timedelta(hours=1)).isoformat()
        patches = self._patch_success(client)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            result = run_continuity_digest(automatic=False)
        self.assertEqual(result["cursor_after"], 179)
        self.assertIn("commit_memory_continuity_run", client.rpc_names)

        self.cursor["auto_cooldown_until"] = None
        with (
            patch("gateway.memory_continuity._continuity_analysis_configured", return_value=True),
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value=dict(self.cursor)),
            patch("gateway.memory_continuity._backlog_count", return_value=80),
            patch("gateway.memory_continuity._fetch_rows_after", return_value=list(self.rows)),
            patch("gateway.memory_continuity._claim_slot", return_value={"status": "already_running"}),
        ):
            with self.assertRaisesRegex(ContinuityPipelineError, "already running"):
                run_continuity_digest(automatic=True)

    def test_manual_cooldown_rejects_repeat_before_model(self):
        self.cursor["manual_cooldown_until"] = (_now() + timedelta(seconds=10)).isoformat()
        with (
            patch("gateway.memory_continuity._continuity_analysis_configured", return_value=True),
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value=dict(self.cursor)),
            patch("gateway.memory_continuity.extract_continuity_candidates") as model,
        ):
            with self.assertRaises(ContinuityPipelineError) as raised:
                run_continuity_digest()
        self.assertEqual(raised.exception.code, "manual_cooldown")
        model.assert_not_called()

    def test_zero_candidates_pauses_without_commit_or_cursor_advance(self):
        client = _RpcClient()
        patches = self._patch_success(client, candidates=[])
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            result = run_continuity_digest()
        self.assertTrue(result["paused_empty"])
        self.assertEqual(result["cursor_before"], result["cursor_after"])
        self.assertIn("pause_memory_continuity_empty", client.rpc_names)
        self.assertNotIn("commit_memory_continuity_run", client.rpc_names)

    def test_stale_empty_pause_does_not_regress_processed_batch(self):
        # 陈旧请求的空结果：批次已被其它运行处理 → 不重新 paused_empty，
        # 游标保持真实推进位置。
        client = _RpcClient(pause_data={
            "status": "already_processed",
            "last_processed_message_id": 2504,
        })
        patches = self._patch_success(client, candidates=[])
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            result = run_continuity_digest()
        self.assertFalse(result["paused_empty"])
        self.assertEqual(result["cursor_after"], 2504)
        self.assertEqual(result["status"], "succeeded")

    def test_stale_commit_finalizes_run_without_write(self):
        # 陈旧请求带候选提交：RPC 拒绝（already_processed）→ run 按空成功
        # 收束，不产生写入、不暂停。
        client = _RpcClient(
            fail_commit=True,
            fail_message="psycopg.errors.RaiseException: "
                         "memory_continuity_batch_already_processed",
        )
        patches = self._patch_success(client)
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10]:
            result = run_continuity_digest()
        self.assertTrue(result["already_processed"])
        self.assertFalse(result["paused_empty"])
        self.assertEqual(result["inserted_count"], 0)
        finalize = [
            payload for name, payload in client.table_updates
            if payload.get("status") == "succeeded" and payload.get("inserted_count") == 0
        ]
        self.assertEqual(len(finalize), 1)
        self.assertIsNone(finalize[0]["heartbeat_at"])

    def test_paused_retry_uses_fixed_batch_even_when_new_messages_exist(self):
        self.cursor.update({
            "status": "paused_empty",
            "blocked_first_message_id": 178,
            "blocked_last_message_id": 179,
            "blocked_message_count": 2,
        })
        client = _RpcClient()
        patches = self._patch_success(client)
        with (
            patches[0], patches[1], patches[2],
            patch("gateway.memory_continuity._fetch_blocked_rows", return_value=list(self.rows)) as blocked,
            patch("gateway.memory_continuity._fetch_rows_after") as latest,
            patches[4], patches[5], patches[6], patches[7], patches[8], patches[9], patches[10],
        ):
            result = run_continuity_digest()
        blocked.assert_called_once()
        latest.assert_not_called()
        self.assertFalse(result["paused_empty"])

    def test_embedding_failure_marks_failed_and_does_not_commit(self):
        client = _RpcClient()
        patches = self._patch_success(client)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6],
            patch("gateway.memory_continuity._enrich_candidates", side_effect=ContinuityPipelineError("embedding_error", "failed")),
            patches[8], patches[9],
            patch("gateway.memory_continuity._record_failure") as failed,
            patches[10],
        ):
            with self.assertRaises(ContinuityPipelineError) as raised:
                run_continuity_digest()
        self.assertEqual(raised.exception.code, "embedding_error")
        failed.assert_called_once()
        self.assertNotIn("commit_memory_continuity_run", client.rpc_names)

    def test_commit_failure_does_not_report_cursor_advance(self):
        client = _RpcClient(fail_commit=True)
        patches = self._patch_success(client)
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9],
            patch("gateway.memory_continuity._record_failure") as failed,
            patches[10],
        ):
            with self.assertRaises(ContinuityPipelineError) as raised:
                run_continuity_digest()
        self.assertEqual(raised.exception.code, "commit_failed")
        failed.assert_called_once()

    def test_paused_automatic_check_and_skip_do_not_call_model(self):
        self.cursor.update({
            "status": "paused_empty",
            "blocked_first_message_id": 178,
            "blocked_last_message_id": 179,
            "blocked_message_count": 2,
        })
        with (
            patch("gateway.memory_continuity._continuity_analysis_configured", return_value=True),
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value=dict(self.cursor)),
            patch("gateway.memory_continuity.extract_continuity_candidates") as model,
        ):
            self.assertIsNone(run_continuity_digest_if_due())
        model.assert_not_called()

        with (
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value=dict(self.cursor)),
            patch("gateway.memory_continuity._rpc_object", return_value={"run_id": 7, "cursor": {"status": "ready"}}),
            patch("gateway.memory_continuity.extract_continuity_candidates") as model,
        ):
            result = skip_blocked_continuity_batch()
        self.assertEqual(result["inserted_count"], 0)
        model.assert_not_called()


class ContinuityRecallEmbeddingTests(unittest.TestCase):
    def test_enrich_candidates_embed_recall_scene_only_and_skip_blank_scene(self):
        sceneful = {
            **_candidate(),
            "recall_scene": "当下次继续处理网关问题时",
            "recall_tags": ["网关", "待续"],
        }
        sceneless = dict(_candidate())
        embedded_texts = []

        def fake_embedding(text):
            embedded_texts.append(text)
            return [0.1, 0.2]

        with (
            patch("gateway.memory_continuity._update_heartbeat"),
            patch("gateway.memory_continuity._get_embedding_sync", side_effect=fake_embedding),
        ):
            enriched = _enrich_candidates([sceneful, sceneless], 91)

        self.assertEqual(
            embedded_texts,
            [
                "叶子和栖约好下次继续处理网关问题。",  # 正文 embedding（去重用）
                "当下次继续处理网关问题时",  # 召回场景 embedding
                "叶子和栖约好下次继续处理网关问题。",  # 无场景候选仅正文
            ],
        )
        self.assertEqual(enriched[0]["recall_embedding"], [0.1, 0.2])
        self.assertIsNone(enriched[1]["recall_embedding"])
        # 正文 embedding 仍按原文计算，供去重使用。
        self.assertEqual(enriched[0]["embedding"], [0.1, 0.2])

    def test_recall_embedding_failure_sends_candidate_to_pending(self):
        sceneful = {
            **_candidate(),
            "recall_scene": "当下次继续处理网关问题时",
            "recall_tags": ["网关", "待续"],
        }

        def fake_embedding(text):
            if text == "当下次继续处理网关问题时":
                raise DigestPipelineError("embedding_http_error", "boom")
            return [0.1, 0.2]

        with (
            patch("gateway.memory_continuity._update_heartbeat"),
            patch("gateway.memory_continuity._get_embedding_sync", side_effect=fake_embedding),
        ):
            enriched = _enrich_candidates([sceneful], 91)

        # 正文 embedding 成功；召回向量失败不让整批失败，该候选以
        # NULL 向量进入 pending，由叶子补场景后再通过。
        self.assertEqual(enriched[0]["embedding"], [0.1, 0.2])
        self.assertEqual(enriched[0]["recall_scene"], "当下次继续处理网关问题时")
        self.assertIsNone(enriched[0]["recall_embedding"])


class _Request:
    def __init__(self, token="secret"):
        self.headers = {"authorization": f"Bearer {token}"}


class ContinuityApiTests(unittest.TestCase):
    def test_status_requires_gateway_token(self):
        with patch.object(cfg, "GATEWAY_TOKEN", "secret"):
            response = asyncio.run(continuity_status(_Request("wrong")))
        self.assertEqual(response.status_code, 401)

    def test_status_returns_formal_state(self):
        expected = {"status": "ready", "cursor": 177, "backlog_count": 3}
        with (
            patch.object(cfg, "GATEWAY_TOKEN", "secret"),
            patch("gateway.memory_digest_api.get_continuity_status", return_value=expected),
        ):
            response = asyncio.run(continuity_status(_Request()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), expected)

    def test_execute_returns_stable_error_code(self):
        with (
            patch.object(cfg, "GATEWAY_TOKEN", "secret"),
            patch(
                "gateway.memory_digest_api.run_continuity_digest",
                side_effect=ContinuityPipelineError("manual_cooldown", "cooling down", 429),
            ),
        ):
            response = asyncio.run(continuity_execute(_Request()))
        self.assertEqual(response.status_code, 429)
        self.assertEqual(json.loads(response.body)["error_code"], "manual_cooldown")

    def test_skip_returns_result(self):
        expected = {"status": "succeeded", "inserted_count": 0}
        with (
            patch.object(cfg, "GATEWAY_TOKEN", "secret"),
            patch("gateway.memory_digest_api.skip_blocked_continuity_batch", return_value=expected),
        ):
            response = asyncio.run(continuity_skip_blocked(_Request()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), expected)


class ContinuityInitFailureTests(unittest.TestCase):
    """领取成功后的批次初始化失败：记失败、结束占用、不残留 claimed。

    生产事故：run 267 succeeded 占据窗口（旧唯一索引含 succeeded），
    run 268 重试初始化撞 memory_digest_runs_active_batch_uidx，
    异常发生在 try 之外导致残留 claimed。
    """

    def test_init_failure_records_failure_and_raises(self):
        with (
            patch("gateway.memory_continuity._continuity_analysis_configured", return_value=True),
            patch("gateway.memory_continuity.resolve_continuity_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_continuity._get_cursor", return_value={
                "status": "paused_empty",
                "last_processed_message_id": 2460,
                "manual_cooldown_until": None,
                "auto_cooldown_until": None,
            }),
            patch(
                "gateway.memory_continuity._fetch_blocked_rows",
                return_value=[_row(2461), _row(2462, "assistant", "回应")],
            ),
            patch(
                "gateway.memory_continuity._claim_slot",
                return_value={"status": "claimed", "run_id": 268},
            ),
            patch(
                "gateway.memory_continuity._set_running_run",
                side_effect=Exception(
                    'duplicate key value violates unique constraint '
                    '"memory_digest_runs_active_batch_uidx"'
                ),
            ),
            patch("gateway.memory_continuity._record_failure") as record_failure,
        ):
            with self.assertRaises(ContinuityPipelineError) as raised:
                run_continuity_digest(automatic=False)
        self.assertEqual(raised.exception.code, "batch_init_failed")
        record_failure.assert_called_once()
        self.assertEqual(record_failure.call_args.args[0], 268)
        self.assertEqual(record_failure.call_args.args[1], "batch_init_failed")
        self.assertIn("memory_digest_runs_active_batch_uidx", record_failure.call_args.args[2])

    def test_record_failure_marks_run_failed_and_clears_heartbeat(self):
        client = Mock()
        client.table.return_value.update.return_value.eq.return_value.execute.return_value = (
            SimpleNamespace(data=[{"id": 268}])
        )
        from gateway.memory_continuity import _record_failure

        with patch("gateway.memory_continuity._client", return_value=client):
            _record_failure(268, "batch_init_failed", "boom")
        payload = client.table.return_value.update.call_args.args[0]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_code"], "batch_init_failed")
        self.assertIsNone(payload["heartbeat_at"])


def _now():
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    unittest.main()
