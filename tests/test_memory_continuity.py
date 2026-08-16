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
    def __init__(self, *, commit_data=1, fail_commit=False):
        self.commit_data = commit_data
        self.fail_commit = fail_commit
        self.rpc_names = []

    def rpc(self, name, params):
        self.rpc_names.append(name)
        if self.fail_commit and name == "commit_memory_continuity_run":
            raise RuntimeError("commit failed")
        data = {} if name == "pause_memory_continuity_empty" else self.commit_data
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=data))


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
            patch("gateway.memory_continuity._analysis_configured", return_value=True),
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
            patch("gateway.memory_continuity._analysis_configured", return_value=True),
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
            patch("gateway.memory_continuity._analysis_configured", return_value=True),
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
            patch("gateway.memory_continuity._analysis_configured", return_value=True),
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
            patch("gateway.memory_continuity._analysis_configured", return_value=True),
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


def _now():
    return datetime.now(timezone.utc)


if __name__ == "__main__":
    unittest.main()
