"""Unit tests for the rumination continuity path.

Batch/cursor planning, model-input visibility, structured-output validation,
and the scheduler/API wiring are exercised with mocked database and model
calls; the SQL RPC behavior is covered by the pgserver integration test.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import unittest
from unittest.mock import patch

from gateway.config import cfg
from gateway.memory_digest_api import rumination_execute, rumination_status
from gateway.memory_rumination import (
    FIRST_RUN_MAX_MESSAGES,
    RUMINATION_BATCH_MAX,
    RUMINATION_BATCH_MIN,
    RuminationPipelineError,
    build_model_input,
    enrich_rumination_ops,
    first_run_message_ids,
    get_rumination_status,
    merge_thread_operations,
    parse_rumination_output,
    plan_rumination_batches,
    run_rumination_batch,
    run_rumination_digest,
    run_rumination_digest_if_due,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()


def _evidence_times(ids):
    return {int(value): "2026-09-01T10:00+08:00" for value in ids}


def _thread(memory_id=12, state="open", maintained_by="rumination", key="topic.x"):
    return {
        "id": memory_id,
        "memory_key": key,
        "continuity_id": "21111111-1111-1111-1111-1111111111a1",
        "thread_state": state,
        "maintained_by": maintained_by,
        "content": "叶子和栖约定下周三赶海。",
        "content_hash": _sha256("叶子和栖约定下周三赶海。"),
        "continuity_data": {
            "open_question": "赶海是否成行",
            "current_state": "已约定待确认天气",
            "closure_criteria": ["成行或改期"],
        },
        "evidence_message_ids": [1],
        "evidence_start_time": "2026-09-01T10:00+08:00",
        "evidence_end_time": "2026-09-01T10:01+08:00",
        "created_at": "2026-09-01T10:01+08:00",
    }


def _threads_by_id(*threads):
    return {int(thread["id"]): thread for thread in threads}


_DEFAULT_TARGET = _thread(memory_id=12, state="open")


def _snap(target):
    """Snapshot fields the model must echo for the given thread fixture."""
    return {
        "target_memory_key": target["memory_key"],
        "target_continuity_id": target["continuity_id"],
        "target_content_hash": target["content_hash"],
        "target_thread_state": target["thread_state"],
    }


def _op(**kwargs):
    base = {
        "op": "update_thread",
        "reason": "进程有实质进展",
        "target_memory_id": 12,
        **_snap(_DEFAULT_TARGET),
        "content": "赶海计划当前状态：天气已确认，周三成行。",
        "continuity_data": {
            "open_question": "赶海是否成行",
            "current_state": "天气已确认，周三成行",
            "closure_criteria": ["成行或改期"],
        },
        "evidence_message_ids": [3, 4],
    }
    base.update(kwargs)
    return base


class BatchPlanningTests(unittest.TestCase):
    def test_first_run_uses_only_latest_120(self):
        ids_desc = list(range(300, 0, -1))
        self.assertEqual(len(first_run_message_ids(ids_desc)), FIRST_RUN_MAX_MESSAGES)
        self.assertEqual(first_run_message_ids(ids_desc)[0], 181)
        self.assertEqual(first_run_message_ids(ids_desc)[-1], 300)

        batches = plan_rumination_batches(first_run_message_ids(ids_desc), initialized=False)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0], (181, 300, 120))

    def test_daily_examples_from_spec(self):
        def ids(n, start=100):
            return list(range(start, start + n))

        self.assertEqual(plan_rumination_batches(ids(45), initialized=True), [])
        self.assertEqual(
            plan_rumination_batches(ids(60), initialized=True),
            [(100, 159, 60)],
        )
        self.assertEqual(
            plan_rumination_batches(ids(120), initialized=True),
            [(100, 219, 120)],
        )
        self.assertEqual(
            plan_rumination_batches(ids(170), initialized=True),
            [(100, 219, 120)],
        )
        self.assertEqual(
            plan_rumination_batches(ids(180), initialized=True),
            [(100, 219, 120), (220, 279, 60)],
        )
        self.assertEqual(
            plan_rumination_batches(ids(250), initialized=True),
            [(100, 219, 120), (220, 339, 120)],
        )
        self.assertEqual(
            plan_rumination_batches(ids(360), initialized=True),
            [(100, 219, 120), (220, 339, 120), (340, 459, 120)],
        )

    def test_batch_size_bounds(self):
        batches = plan_rumination_batches(list(range(1, 1000)), initialized=True)
        for first, last, count in batches:
            self.assertLessEqual(count, RUMINATION_BATCH_MAX)
            self.assertGreaterEqual(count, RUMINATION_BATCH_MIN)
            self.assertEqual(last - first + 1, count)


class ModelInputVisibilityTests(unittest.TestCase):
    def test_model_input_contains_batch_threads_and_own_requests_only(self):
        messages = [
            {"id": 3, "conversation_id": "c1", "role": "user",
             "content": "今天赶海确认了。", "source_time": "2026-09-01T10:00+08:00"},
        ]
        threads = [_thread(memory_id=12)]
        requests = [{
            "id": 77, "status": "rejected", "continuity_type": "episode",
            "content": "一段被拒的episode申请。", "reason": "证据不足",
            "evidence_message_ids": [2], "review_note": "证据不足",
            "created_at": "2026-09-01T09:00+08:00",
        }]
        text = build_model_input(messages, threads, requests)
        self.assertIn("[id=3", text)
        self.assertIn("<chat_log>", text)
        self.assertIn('"memory_id": 12', text)
        self.assertIn('"maintained_by": "rumination"', text)
        self.assertIn('"request_id": 77', text)
        self.assertIn('"status": "rejected"', text)
        # The input never carries other lanes' requests or closed threads.
        self.assertNotIn("orangechat", text)
        self.assertNotIn("resolved", json.dumps([_compact(threads[0])], ensure_ascii=False))

    def test_compact_thread_exposes_lifecycle_fields_only(self):
        from gateway.memory_rumination import _compact_thread

        row = _thread(memory_id=9)
        row["continuity_data"]["closure_summary"] = "不该出现在输入里"
        compact = _compact_thread(row)
        self.assertEqual(compact["memory_id"], 9)
        self.assertIn("open_question", compact)
        self.assertIn("current_state", compact)
        self.assertIn("closure_criteria", compact)
        self.assertNotIn("closure_summary", compact)
        self.assertNotIn("continuity_data", compact)


def _compact(thread):
    from gateway.memory_rumination import _compact_thread

    return _compact_thread(thread)


class ParseValidationTests(unittest.TestCase):
    def setUp(self):
        self.threads = _threads_by_id(
            _thread(memory_id=12, state="open"),
            _thread(memory_id=13, state="paused", key="topic.paused"),
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        self.times = _evidence_times([1, 2, 3, 4, 5])

    def _snap(self, memory_id):
        return _snap(self.threads[memory_id])

    def _parse(self, ops, absorbable_ids=frozenset()):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
            absorbable_ids=absorbable_ids,
        )

    def test_valid_operation_list_parses(self):
        ops = self._parse([
            _op(),
            {"op": "evidence_only", "reason": "重复表达", "target_memory_id": 12,
             **self._snap(12), "evidence_message_ids": [4]},
            {"op": "ignore", "reason": "寒暄", "evidence_message_ids": [1]},
        ])
        self.assertEqual([item["op"] for item in ops], ["update_thread", "evidence_only", "ignore"])
        self.assertEqual(ops[0]["thread_state"], "open")

    def test_non_operations_payload_rejected(self):
        with self.assertRaises(RuminationPipelineError):
            parse_rumination_output(
                "not json", evidence_times=self.times, threads_by_id=self.threads,
            )
        with self.assertRaises(RuminationPipelineError):
            parse_rumination_output(
                json.dumps({"candidates": []}),
                evidence_times=self.times, threads_by_id=self.threads,
            )

    def test_hallucinated_evidence_id_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "outside this batch"):
            self._parse([_op(evidence_message_ids=[999])])

    def test_unknown_target_memory_id_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "unfinished thread"):
            self._parse([_op(target_memory_id=999)])

    def test_unknown_op_and_extra_fields_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "unknown rumination op"):
            self._parse([{"op": "archive_everything", "reason": "x", "evidence_message_ids": [1]}])
        with self.assertRaisesRegex(RuminationPipelineError, "unsupported fields"):
            self._parse([_op(update_mode="replace")])

    def test_missing_reason_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "reason"):
            self._parse([_op(reason="  ")])

    def test_pause_requires_open_and_resume_requires_paused(self):
        with self.assertRaisesRegex(RuminationPipelineError, "open thread"):
            self._parse([{
                "op": "pause_thread", "reason": "还没暂停就想暂停",
                "target_memory_id": 13,
                **self._snap(13),
                "content": "赶海计划当前状态：暂停。",
                "continuity_data": {"open_question": "赶海是否成行", "current_state": "暂停"},
                "evidence_message_ids": [1],
            }])
        with self.assertRaisesRegex(RuminationPipelineError, "paused thread"):
            self._parse([{
                "op": "resume_thread", "reason": "还没暂停就想恢复",
                "target_memory_id": 12,
                **self._snap(12),
                "content": "赶海计划当前状态：恢复。",
                "continuity_data": {"open_question": "赶海是否成行", "current_state": "恢复"},
                "evidence_message_ids": [1],
            }])

    def test_update_thread_cannot_change_state(self):
        with self.assertRaisesRegex(RuminationPipelineError, "cannot change thread_state"):
            self._parse([_op(thread_state="resolved")])

    def test_resolve_requires_closure_fields(self):
        with self.assertRaisesRegex(RuminationPipelineError, "continuity_data"):
            self._parse([{
                "op": "resolve_thread", "reason": "没有闭合字段的完成",
                "target_memory_id": 12,
                **self._snap(12),
                "content": "赶海计划已完成。",
                "continuity_data": {"open_question": "赶海是否成行", "current_state": "完成"},
                "evidence_message_ids": [1],
            }])

    def test_create_memory_limits_types_and_request_types(self):
        with self.assertRaisesRegex(RuminationPipelineError, "moment or inside_joke"):
            self._parse([{
                "op": "create_memory", "reason": "终态不允许 thread",
                "continuity_type": "thread",
                "content": "把 thread 当终态写会被拒绝。",
                "continuity_data": {"open_question": "x", "current_state": "y"},
                "evidence_message_ids": [1],
            }])
        with self.assertRaisesRegex(RuminationPipelineError, "episode, profile or interaction_rule"):
            self._parse([{
                "op": "create_request", "reason": "thread 不能走申请",
                "continuity_type": "thread",
                "content": "把 thread 走申请会被拒绝。",
                "continuity_data": {"open_question": "x", "current_state": "y"},
                "evidence_message_ids": [1],
            }])

    def test_interaction_rule_request_requires_key_and_others_forbid_it(self):
        rule = {
            "op": "create_request", "reason": "叶子明确要求",
            "continuity_type": "interaction_rule",
            "content": "赶海话题必须提醒防晒，这是明确指令。",
            "continuity_data": {
                "trigger": "提到赶海", "expected_behavior": "提醒防晒",
                "scope": "全局", "priority": 5, "rule_state": "active",
                "explicit_instruction": "以后赶海话题都要提醒防晒",
            },
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "memory_key"):
            self._parse([rule])
        episode = {
            "op": "create_request", "reason": "完整经历",
            "continuity_type": "episode",
            "content": "一段完整的共同赶海经历。",
            "continuity_data": {
                "beginning": "约好", "development": "准备", "outcome": "成行",
                "closure_quality": "complete",
            },
            "memory_key": "topic.must.not.exist",
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "must not carry"):
            self._parse([episode])

    def test_secrets_are_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "secret"):
            self._parse([{
                "op": "create_memory", "reason": "包含密钥的内容",
                "continuity_type": "moment",
                "content": "叶子把 API Key 发了过来：sk-rk-ABCDEFGHIJKLMNOP",
                "continuity_data": {"scene": "聊天", "event": "发密钥", "moment_state": "standalone"},
                "evidence_message_ids": [1],
            }])

    def test_missing_snapshot_fields_rejected(self):
        for key in (
            "target_memory_key", "target_continuity_id",
            "target_content_hash", "target_thread_state",
        ):
            with self.subTest(field=key):
                op = _op()
                op.pop(key)
                with self.assertRaisesRegex(
                    RuminationPipelineError, "snapshot mismatch",
                ):
                    self._parse([op])

    def test_stale_snapshot_fields_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "snapshot mismatch"):
            self._parse([_op(target_content_hash="a" * 64)])
        with self.assertRaisesRegex(RuminationPipelineError, "snapshot mismatch"):
            self._parse([_op(target_thread_state="paused")])
        with self.assertRaisesRegex(RuminationPipelineError, "snapshot mismatch"):
            self._parse([_op(target_memory_key="topic.other")])
        with self.assertRaisesRegex(RuminationPipelineError, "snapshot mismatch"):
            self._parse([_op(target_continuity_id="2" * 36 if False else
                             "21111111-1111-1111-1111-1111111111a2")])

    def test_keyless_fast_path_target_allows_null_key_snapshot(self):
        ops = self._parse([_op(target_memory_id=14, **self._snap(14))])
        self.assertEqual(ops[0]["target_memory_key"], None)

    def test_absorb_targets_must_be_candidates(self):
        episode_request = {
            "op": "create_request", "reason": "完整经历",
            "continuity_type": "episode",
            "content": "一段完整的共同赶海经历。",
            "continuity_data": {
                "beginning": "约好", "development": "准备", "outcome": "成行",
                "closure_quality": "complete",
            },
            "evidence_message_ids": [1],
        }
        # 候选集合中的 ID 才可吸收。
        op = dict(episode_request, absorbed_fast_path_memory_ids=[14])
        ops = self._parse([op], absorbable_ids={14})
        self.assertEqual(ops[0]["absorbed_fast_path_memory_ids"], [14])
        # 候选集合为空时禁止任何吸收 ID。
        with self.assertRaisesRegex(RuminationPipelineError, "no absorbable"):
            self._parse([op])
        # 幻觉 ID 即使对应真实 fast-path 记忆（ID=12 是 rumination 之外任意值），
        # 只要不属于候选集合即整批拒绝。
        op["absorbed_fast_path_memory_ids"] = [999]
        with self.assertRaisesRegex(RuminationPipelineError, "not an absorbable candidate"):
            self._parse([op], absorbable_ids={14})
        # 非候选但真实存在的 ID 同样拒绝。
        op["absorbed_fast_path_memory_ids"] = [12]
        with self.assertRaisesRegex(RuminationPipelineError, "not an absorbable candidate"):
            self._parse([op], absorbable_ids={14})
        # 非法形态整批拒绝。
        op["absorbed_fast_path_memory_ids"] = []
        with self.assertRaisesRegex(RuminationPipelineError, "non-empty array"):
            self._parse([op], absorbable_ids={14})
        # 去重 + 数量上限。
        op["absorbed_fast_path_memory_ids"] = [14, 14]
        ops = self._parse([op], absorbable_ids={14})
        self.assertEqual(ops[0]["absorbed_fast_path_memory_ids"], [14])
        op["absorbed_fast_path_memory_ids"] = list(range(101, 110))
        with self.assertRaisesRegex(RuminationPipelineError, "at most"):
            self._parse([op], absorbable_ids=set(range(101, 110)))

    def test_create_memory_absorb_targets_validated(self):
        op = {
            "op": "create_memory", "reason": "整合重复片段",
            "continuity_type": "moment",
            "content": "整合后的赶海瞬间记忆正文。",
            "continuity_data": {
                "scene": "聊天窗口", "event": "赶海瞬间", "moment_state": "standalone",
            },
            "evidence_message_ids": [1],
            "absorbed_fast_path_memory_ids": [14],
        }
        ops = self._parse([op], absorbable_ids={14})
        self.assertEqual(ops[0]["absorbed_fast_path_memory_ids"], [14])
        with self.assertRaisesRegex(
            RuminationPipelineError, "no absorbable fast-path memories",
        ):
            self._parse([op], absorbable_ids=frozenset())

    def test_twenty_five_operations_parse_fine(self):
        # 25 个合法操作不因数量被拒：内容互异、目标同一条 thread。
        ops = [
            _op(
                content=f"网关改造当前状态：第 {index} 个进展节点已达成。",
            )
            for index in range(25)
        ]
        parsed = self._parse(ops)
        self.assertEqual(len(parsed), 25)

    def test_candidate_loader_scopes_to_batch_evidence(self):
        fake = _FakeClient({"memories": [{"id": 5}]})
        rows = [{"id": 3}, {"id": 4}]
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            from gateway.memory_rumination import _load_absorbable_candidates

            candidates = _load_absorbable_candidates("assistant-1", rows)
        self.assertEqual(len(candidates), 1)
        calls = fake.queries["memories"].calls
        filters = {call[1]: call[2] for call in calls if call[0] == "eq"}
        self.assertEqual(filters.get("producer_path"), "fast_path")
        self.assertEqual(filters.get("verified"), "verified")
        self.assertEqual(filters.get("is_active"), True)
        overlaps = next(call for call in calls if call[0] == "overlaps")
        self.assertEqual(overlaps[1], "evidence_message_ids")
        self.assertEqual(overlaps[2], [3, 4])

    def test_candidate_loader_excludes_closed_threads(self):
        closed = {
            "id": 9, "continuity_type": "thread", "thread_state": "resolved",
            "title": "已完成的线索", "memory_key": None,
            "evidence_message_ids": [3], "producer_path": "fast_path",
            "verified": "verified", "is_active": True,
        }
        fake = _FakeClient({"memories": [closed]})
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            from gateway.memory_rumination import _load_absorbable_candidates

            candidates = _load_absorbable_candidates("assistant-1", [{"id": 3}])
        self.assertEqual(candidates, [])

    def test_candidate_loader_empty_batch_returns_empty(self):
        from gateway.memory_rumination import _load_absorbable_candidates

        self.assertEqual(_load_absorbable_candidates("a", []), [])

    def test_identical_content_ops_deduplicated(self):
        ops = self._parse([_op(), _op(reason="同义重复")])
        self.assertEqual(len(ops), 1)

    def test_invalid_memory_key_format_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "memory_key"):
            self._parse([{
                "op": "create_tracked_thread", "reason": "非法 key",
                "content": "新的长期进程需要稳定的主题键。",
                "memory_key": "非法 KEY 空格",
                "thread_state": "open",
                "continuity_data": {"open_question": "x", "current_state": "y"},
                "evidence_message_ids": [1],
            }])


class EnrichmentTests(unittest.TestCase):
    def test_content_embedding_required_and_recall_embedding_optional(self):
        ops = [
            {
                "op": "create_memory", "reason": "r", "continuity_type": "moment",
                "content": "一条带场景描述的普通记忆。",
                "recall_scene": "聊到赶海时", "content_hash": "a" * 64,
            },
            {
                "op": "create_memory", "reason": "r", "continuity_type": "moment",
                "content": "一条没有场景描述的普通记忆。",
                "recall_scene": None, "content_hash": "b" * 64,
            },
        ]
        with (
            patch("gateway.memory_rumination._update_heartbeat"),
            patch(
                "gateway.memory_rumination._get_embedding_sync",
                side_effect=RuminationPipelineError("embedding_http_error", "boom"),
            ),
        ):
            with self.assertRaises(RuminationPipelineError):
                # 正文向量失败让整批失败。
                enrich_rumination_ops(ops, 1)

        # 场景非空但召回向量失败：整批失败，绝不留下"有场景无向量"的写入。
        def fake_embedding(text):
            from gateway.memory_extract import DigestPipelineError

            if text == "聊到赶海时":
                raise DigestPipelineError("embedding_http_error", "boom")
            return [0.1, 0.2]

        with (
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._get_embedding_sync", side_effect=fake_embedding),
        ):
            with self.assertRaisesRegex(RuminationPipelineError, "Recall scene embedding failed"):
                enrich_rumination_ops(ops, 1)

        # 没有可靠场景时显式空场景写入，不调用召回向量。
        sceneless = [dict(ops[1])]
        with (
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._get_embedding_sync", return_value=[0.3, 0.4]) as embed,
        ):
            enriched = enrich_rumination_ops(sceneless, 1)
        self.assertEqual(enriched[0]["embedding"], [0.3, 0.4])
        self.assertIsNone(enriched[0].get("recall_embedding"))
        self.assertEqual(
            [call for call in embed.call_args_list if call.args[0] == "聊到赶海时"],
            [],
        )


class RunFlowTests(unittest.TestCase):
    def setUp(self):
        self.cursor = {
            "assistant_id": "assistant-1",
            "initialized": True,
            "last_processed_message_id": 100,
            "last_scheduled_date": None,
            "last_success_at": None,
        }

    def _patch_happy(self, *, claim=None, commit=None, cursor=None):
        claim = claim or {"status": "claimed", "run_id": 71}
        commit = commit or {
            "run_id": 71,
            "cursor": {"last_processed_message_id": 159},
            "op_counts": {"created_threads": 1},
            "preview": [],
            "inserted_count": 1,
        }
        cursor = cursor if cursor is not None else dict(self.cursor)
        return (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch(
                "gateway.memory_rumination._fetch_message_ids",
                side_effect=[list(range(101, 161)), []],
            ),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=[
                claim, commit,
            ]),
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        )

    def test_batch_success_advances_cursor(self):
        patches = self._patch_happy()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["batch_count"], 1)
        self.assertEqual(result["cursor_after"], 159)
        self.assertEqual(result["op_counts"], {"created_threads": 1})

    def test_claim_conflict_reports_failed_run_without_cursor_move(self):
        patches = self._patch_happy(
            claim={"status": "already_running", "run_id": 70},
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["batches"][0]["error_code"], "already_running")
        self.assertEqual(result["cursor_after"], 100)

    def test_later_batch_failure_keeps_earlier_success(self):
        cursor = dict(self.cursor)
        claim_results = [
            {"status": "claimed", "run_id": 71},
            {"status": "claimed", "run_id": 72},
        ]
        commit_ok = {
            "run_id": 71,
            "cursor": {"last_processed_message_id": 220},
            "op_counts": {"created_threads": 1},
            "preview": [],
            "inserted_count": 1,
        }
        model_outputs = ['{"operations":[]}', '{"operations":[]}']
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch(
                "gateway.memory_rumination._fetch_message_ids",
                side_effect=[list(range(101, 221)), list(range(221, 341))],
            ),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch(
                "gateway.memory_rumination._call_rumination_model",
                side_effect=[model_outputs[0], RuminationPipelineError("model_http_error", "boom")],
            ),
            patch("gateway.memory_rumination._rpc_object", side_effect=[
                claim_results[0], commit_ok, claim_results[1],
            ]),
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed") as mark_failed,
        ):
            result = run_rumination_digest("rumination_manual")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(result["batches"][0]["status"], "succeeded")
        self.assertEqual(result["batches"][1]["status"], "failed")
        # 前一批已成功提交，游标推进不回滚；失败批只记录失败。
        mark_failed.assert_called_once()
        self.assertEqual(result["batches"][0]["cursor_after"], 220)

    def test_below_threshold_creates_skipped_run_without_model_call(self):
        cursor = dict(self.cursor)
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch(
                "gateway.memory_rumination._fetch_message_ids",
                side_effect=[list(range(101, 146))],
            ),
            patch("gateway.memory_rumination._rpc_object", return_value={
                "status": "skipped", "run_id": 80,
            }) as rpc,
            patch("gateway.memory_rumination._call_rumination_model") as model,
        ):
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["backlog_count"], 45)
        self.assertEqual(result["cursor_after"], 100)
        rpc.assert_called_once()
        model.assert_not_called()

    def test_paged_backlog_processes_every_qualifying_page(self):
        # 260 条积压：120 + 120 两批处理，尾部 20 条不足 60 留到次日。
        pages = [
            list(range(101, 221)),
            list(range(221, 341)),
            list(range(341, 361)),
        ]
        rpc_results = [
            {"status": "claimed", "run_id": 71},
            {
                "run_id": 71,
                "cursor": {"last_processed_message_id": 220},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            },
            {"status": "claimed", "run_id": 72},
            {
                "run_id": 72,
                "cursor": {"last_processed_message_id": 340},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            },
        ]
        cursor = dict(self.cursor)
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids", side_effect=pages) as fetch,
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results),
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(result["cursor_after"], 340)
        # 每页最多 120 条，从未一次性读取全部积压。
        for call in fetch.call_args_list:
            self.assertLessEqual(call.kwargs["limit"], 120)

    def test_paging_continues_beyond_large_backlogs_without_cap(self):
        pages = [list(range(101 + 120 * i, 221 + 120 * i)) for i in range(4)]
        pages.append(list(range(581, 592)))  # 11 条尾部
        rpc_results = []
        for index in range(4):
            rpc_results.append({"status": "claimed", "run_id": 80 + index})
            rpc_results.append({
                "run_id": 80 + index,
                "cursor": {"last_processed_message_id": 220 + 120 * index},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            })
        cursor = dict(self.cursor)
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids", side_effect=pages),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results),
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["batch_count"], 4)
        self.assertEqual(result["cursor_after"], 580)

    def test_sparse_message_ids_are_counted_as_real_rows(self):
        # 稀疏 ID：批次数按真实行数计，而非 last-first+1。
        sparse_ids = list(range(101, 221))
        sparse_ids2 = [900 + i for i in range(60)]
        rpc_results = [
            {"status": "claimed", "run_id": 71},
            {
                "run_id": 71,
                "cursor": {"last_processed_message_id": 959},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            },
            {"status": "claimed", "run_id": 72},
            {
                "run_id": 72,
                "cursor": {"last_processed_message_id": 959},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            },
        ]
        cursor = dict(self.cursor)
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch(
                "gateway.memory_rumination._fetch_message_ids",
                side_effect=[sparse_ids, sparse_ids2, []],
            ),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results),
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["batch_count"], 2)
        first, second = result["batches"]
        self.assertEqual(first["batch"], {"first_message_id": 101, "last_message_id": 220, "message_count": 120})
        self.assertEqual(second["batch"], {"first_message_id": 900, "last_message_id": 959, "message_count": 60})

    def test_first_run_initializes_from_latest_messages(self):
        cursor = dict(self.cursor, initialized=False, last_processed_message_id=0)
        latest = list(range(300, 180, -1))  # descending ids
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_latest_message_ids", return_value=latest) as fetch_latest,
            patch("gateway.memory_rumination._fetch_message_ids") as fetch_after,
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 181, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", return_value={
                "status": "claimed", "run_id": 71,
            }) as claim,
            patch("gateway.memory_rumination._rpc_object") ,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            # claim + commit both go through _rpc_object; first claim, then commit.
            with patch("gateway.memory_rumination._rpc_object", side_effect=[
                {"status": "claimed", "run_id": 71},
                {
                    "run_id": 71,
                    "cursor": {"last_processed_message_id": 300},
                    "op_counts": {},
                    "preview": [],
                    "inserted_count": 0,
                },
            ]):
                result = run_rumination_digest("rumination_manual")
        fetch_latest.assert_called_once_with("assistant-1", FIRST_RUN_MAX_MESSAGES)
        fetch_after.assert_not_called()
        claim_params = None
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["cursor_after"], 300)


class SchedulerTests(unittest.TestCase):
    def _patches(self, *, cursor, configured=True, assistant="assistant-1"):
        return (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=configured),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value=assistant),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
        )

    def test_scheduled_run_happens_once_per_day_after_configured_hour(self):
        cursor = {"initialized": True, "last_processed_message_id": 100,
                  "last_scheduled_date": None}
        patches = self._patches(cursor=cursor)
        with patches[0], patches[1], patches[2], \
                patch("gateway.memory_rumination.run_rumination_digest", return_value={
                    "status": "succeeded", "trigger": "rumination_scheduled",
                }) as run, \
                patch("gateway.memory_rumination.datetime") as dt:
            from datetime import datetime, timedelta, timezone

            cst = timezone(timedelta(hours=8))
            dt.now.return_value = datetime(2026, 9, 5, 6, 5, tzinfo=cst)
            dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)
            result = run_rumination_digest_if_due()
        self.assertIsNotNone(result)
        run.assert_called_once_with("rumination_scheduled")

    def test_scheduled_run_skipped_when_already_done_today(self):
        cursor = {"initialized": True, "last_processed_message_id": 100,
                  "last_scheduled_date": "2026-09-05"}
        patches = self._patches(cursor=cursor)
        with patches[0], patches[1], patches[2], \
                patch("gateway.memory_rumination.run_rumination_digest") as run:
            result = run_rumination_digest_if_due()
        self.assertIsNone(result)
        run.assert_not_called()

    def test_unconfigured_provider_never_runs(self):
        patches = self._patches(cursor={}, configured=False)
        with patches[0], patches[1], patches[2], \
                patch("gateway.memory_rumination.run_rumination_digest") as run:
            self.assertIsNone(run_rumination_digest_if_due())
        run.assert_not_called()


class _Request:
    def __init__(self, token="secret"):
        self.headers = {"authorization": f"Bearer {token}"}


class RuminationApiTests(unittest.TestCase):
    def test_status_requires_gateway_token(self):
        with patch.object(cfg, "GATEWAY_TOKEN", "secret"):
            response = asyncio.run(rumination_status(_Request("wrong")))
        self.assertEqual(response.status_code, 401)

    def test_status_returns_formal_state(self):
        expected = {"configured": True, "cursor": 100, "backlog_count": 3}
        with (
            patch.object(cfg, "GATEWAY_TOKEN", "secret"),
            patch("gateway.memory_digest_api.get_rumination_status", return_value=expected),
        ):
            response = asyncio.run(rumination_status(_Request()))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), expected)

    def test_execute_maps_pipeline_error_code(self):
        with (
            patch.object(cfg, "GATEWAY_TOKEN", "secret"),
            patch(
                "gateway.memory_digest_api.run_rumination_digest",
                side_effect=RuminationPipelineError("analysis_not_configured", "no provider", 503),
            ),
        ):
            response = asyncio.run(rumination_execute(_Request()))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["error_code"], "analysis_not_configured")


class MergeThreadOperationsTests(unittest.TestCase):
    """同批同 thread 多个连续进展：按真实证据时间合并为一个最终版本操作。"""

    def setUp(self):
        self.times = {
            101: "2026-09-01T09:00+08:00",   # 上午：后端完成
            102: "2026-09-01T09:30+08:00",
            201: "2026-09-01T14:00+08:00",   # 下午：前端部署
            202: "2026-09-01T14:30+08:00",
            301: "2026-09-01T20:00+08:00",   # 晚上：实测通过
            302: "2026-09-01T20:30+08:00",
        }
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))

    def _snap(self, memory_id):
        return _snap(self.threads[memory_id])

    def _update(self, evidence, content, state_data=None):
        return {
            "op": "update_thread",
            "reason": "进程有实质进展",
            "target_memory_id": 12,
            **self._snap(12),
            "content": content,
            "continuity_data": state_data or {
                "open_question": "网关改造是否完成",
                "current_state": content,
                "closure_criteria": ["生产实测通过"],
            },
            "evidence_message_ids": list(evidence),
        }

    def test_two_consecutive_progresses_merge_into_one_version(self):
        ops = [
            self._update([101], "网关改造当前状态：后端已完成。",
                         {"open_question": "q", "current_state": "后端已完成"}),
            self._update([201, 202], "网关改造当前状态：后端与前端部署均完成。",
                         {"open_question": "q", "current_state": "前后端均完成"}),
        ]
        merged = merge_thread_operations(
            ops, evidence_times=self.times, threads_by_id=self.threads,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["op"], "update_thread")
        self.assertEqual(merged[0]["evidence_message_ids"], [101, 201, 202])
        # 最终状态来自证据时间最晚的操作。
        self.assertEqual(merged[0]["content"], "网关改造当前状态：后端与前端部署均完成。")
        self.assertEqual(merged[0]["thread_state"], "open")

    def test_three_progresses_ending_in_resolve_merge_into_one_resolve(self):
        ops = [
            self._update([101], "网关改造当前状态：后端已完成。",
                         {"open_question": "q", "current_state": "后端已完成"}),
            self._update([201], "网关改造当前状态：前端已部署。",
                         {"open_question": "q", "current_state": "前端已部署"}),
            {
                "op": "resolve_thread",
                "reason": "生产实测通过，进程完成",
                "target_memory_id": 12,
                **self._snap(12),
                "content": "网关改造已完成：后端完成、前端部署、生产实测通过。",
                "continuity_data": {
                    "open_question": "网关改造是否完成",
                    "current_state": "生产实测通过，改造完成",
                    "closure_criteria": ["生产实测通过"],
                    "closure_summary": "改造全链路完成并实测通过。",
                    "closure_reason": "原文明确说明实测通过",
                    "closed_at": "2026-09-01",
                },
                "evidence_message_ids": [301, 302],
            },
        ]
        merged = merge_thread_operations(
            ops, evidence_times=self.times, threads_by_id=self.threads,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["op"], "resolve_thread")
        self.assertEqual(merged[0]["thread_state"], "resolved")
        self.assertEqual(merged[0]["evidence_message_ids"], [101, 201, 301, 302])
        self.assertIn("closure_summary", merged[0]["continuity_data"])

    def test_pause_then_resume_merges_to_state_unchanged_update(self):
        ops = [
            {
                "op": "pause_thread", "reason": "临时暂停",
                "target_memory_id": 12,
                **self._snap(12),
                "content": "网关改造当前状态：临时暂停。",
                "continuity_data": {"open_question": "q", "current_state": "临时暂停"},
                "evidence_message_ids": [101],
            },
            {
                "op": "resume_thread", "reason": "恢复推进",
                "target_memory_id": 12,
                **self._snap(12),
                "content": "网关改造当前状态：恢复推进。",
                "continuity_data": {"open_question": "q", "current_state": "恢复推进"},
                "evidence_message_ids": [201],
            },
        ]
        merged = merge_thread_operations(
            ops, evidence_times=self.times, threads_by_id=self.threads,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["op"], "update_thread")
        self.assertEqual(merged[0]["thread_state"], "open")
        self.assertEqual(merged[0]["evidence_message_ids"], [101, 201])

    def test_progress_after_resolve_is_contradictory(self):
        ops = [
            {
                "op": "resolve_thread", "reason": "完成",
                "target_memory_id": 12,
                **self._snap(12),
                "content": "网关改造已完成。",
                "continuity_data": {
                    "open_question": "q", "current_state": "完成",
                    "closure_summary": "s", "closure_reason": "r", "closed_at": "2026-09-01",
                },
                "evidence_message_ids": [101],
            },
            self._update([201], "网关改造当前状态：完成后又改动了。"),
        ]
        with self.assertRaisesRegex(RuminationPipelineError, "contradictory"):
            merge_thread_operations(
                ops, evidence_times=self.times, threads_by_id=self.threads,
            )

    def test_pause_on_paused_thread_is_contradictory(self):
        threads = _threads_by_id(_thread(memory_id=13, state="paused", key="topic.p"))
        ops = [
            {
                "op": "pause_thread", "reason": "重复暂停",
                "target_memory_id": 13,
                "content": "网关改造当前状态：再次暂停。",
                "continuity_data": {"open_question": "q", "current_state": "再次暂停"},
                "evidence_message_ids": [101],
            },
            {
                "op": "pause_thread", "reason": "又一次暂停",
                "target_memory_id": 13,
                "content": "网关改造当前状态：第三次暂停。",
                "continuity_data": {"open_question": "q", "current_state": "第三次暂停"},
                "evidence_message_ids": [201],
            },
        ]
        with self.assertRaisesRegex(RuminationPipelineError, "contradictory"):
            merge_thread_operations(
                ops, evidence_times=self.times, threads_by_id=threads,
            )

    def test_unchanged_update_merges_to_evidence_only(self):
        ops = [
            {"op": "evidence_only", "reason": "只补证据",
             "target_memory_id": 12, **self._snap(12),
             "evidence_message_ids": [101]},
            self._update([201], "叶子和栖约定下周三赶海。"),  # 与目标正文相同
        ]
        merged = merge_thread_operations(
            ops, evidence_times=self.times, threads_by_id=self.threads,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["op"], "evidence_only")
        self.assertEqual(merged[0]["evidence_message_ids"], [101, 201])
        self.assertNotIn("content", merged[0])

    def test_ordering_requires_real_evidence_times(self):
        ops = [
            self._update([101], "网关改造当前状态：后端已完成。"),
            self._update([201], "网关改造当前状态：前端已部署。"),
        ]
        broken_times = dict(self.times)
        broken_times[201] = None
        with self.assertRaisesRegex(RuminationPipelineError, "cannot be ordered"):
            merge_thread_operations(
                ops, evidence_times=broken_times, threads_by_id=self.threads,
            )

    def test_single_ops_and_other_ops_pass_through(self):
        ops = [
            {"op": "ignore", "reason": "寒暄", "evidence_message_ids": [101]},
            self._update([201], "网关改造当前状态：后端已完成。"),
            {"op": "create_memory", "reason": "瞬间", "continuity_type": "moment",
             "content": "一条独立的普通记忆。", "continuity_data": {
                 "scene": "s", "event": "e", "moment_state": "standalone"},
             "evidence_message_ids": [202]},
        ]
        merged = merge_thread_operations(
            ops, evidence_times=self.times, threads_by_id=self.threads,
        )
        self.assertEqual(merged, ops)

    def test_adopt_memory_key_survives_merge(self):
        threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        ops = [
            {
                "op": "adopt_thread", "reason": "接管快速路径 thread",
                "target_memory_id": 14,
                **_snap(threads[14]),
                "memory_key": "topic.gateway.rework",
                "evidence_message_ids": [101],
            },
            {
                "op": "update_thread", "reason": "进程有实质进展",
                "target_memory_id": 14,
                **_snap(threads[14]),
                "content": "网关改造当前状态：前端已部署。",
                "continuity_data": {"open_question": "q", "current_state": "前端已部署"},
                "evidence_message_ids": [201],
            },
        ]
        merged = merge_thread_operations(
            ops, evidence_times=self.times, threads_by_id=threads,
        )
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["evidence_message_ids"], [101, 201])
        self.assertEqual(merged[0].get("memory_key"), "topic.gateway.rework")


class _FakeQuery:
    def __init__(self, data):
        self.calls = []
        self._data = data

    def select(self, *args):
        self.calls.append(("select", args))
        return self

    def eq(self, key, value):
        self.calls.append(("eq", key, value))
        return self

    def in_(self, key, values):
        self.calls.append(("in", key, tuple(values)))
        return self

    def overlaps(self, key, values):
        self.calls.append(("overlaps", key, list(values)))
        return self

    def order(self, *args, **kwargs):
        self.calls.append(("order", args, tuple(sorted(kwargs.items()))))
        return self

    def limit(self, count):
        self.calls.append(("limit", count))
        return self

    def execute(self):
        from types import SimpleNamespace

        return SimpleNamespace(data=list(self._data))


class _FakeClient:
    def __init__(self, data):
        self._data = data
        self.queries = {}

    def table(self, name):
        query = _FakeQuery(self._data.get(name, []))
        self.queries[name] = query
        return query


class ModelVisibilityQueryTests(unittest.TestCase):
    """模型输入查询必须严格限定允许范围（DB 过滤层）。"""

    def test_thread_query_excludes_resolved_and_non_thread_rows(self):
        from gateway.memory_rumination import _load_unfinished_threads

        fake = _FakeClient({"memories": [_thread(memory_id=1)]})
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            rows = _load_unfinished_threads("assistant-1")
        self.assertEqual(len(rows), 1)
        calls = fake.queries["memories"].calls
        filters = {call[1]: call[2] for call in calls if call[0] == "eq"}
        self.assertEqual(filters.get("continuity_type"), "thread")
        self.assertEqual(filters.get("verified"), "verified")
        self.assertEqual(filters.get("is_active"), True)
        self.assertEqual(filters.get("assistant_id"), "assistant-1")
        state_filter = next(call for call in calls if call[0] == "in")
        self.assertEqual(state_filter[2], ("open", "paused"))

    def test_request_query_only_reads_own_rumination_requests(self):
        from gateway.memory_rumination import _load_own_requests

        fake = _FakeClient({"memory_requests": [{"id": 7}]})
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            rows = _load_own_requests("assistant-1")
        self.assertEqual(len(rows), 1)
        calls = fake.queries["memory_requests"].calls
        filters = {call[1]: call[2] for call in calls if call[0] == "eq"}
        self.assertEqual(filters.get("source"), "rumination")
        self.assertEqual(filters.get("assistant_id"), "assistant-1")
        status_filter = next(call for call in calls if call[0] == "in")
        self.assertEqual(
            status_filter[2], ("pending", "rejected", "duplicate", "conflict"),
        )

    def test_model_input_carries_only_whitelisted_thread_fields(self):
        thread = _thread(memory_id=12)
        thread["continuity_data"] = dict(
            thread["continuity_data"],
            closure_summary="绝不进入模型输入",
            closure_reason="绝不进入模型输入",
        )
        text = build_model_input(
            [{"id": 5, "conversation_id": "c1", "role": "user",
              "content": "本批原文", "source_time": "2026-09-01T10:00+08:00"}],
            [thread],
            [],
        )
        self.assertIn("本批原文", text)
        self.assertNotIn("绝不进入模型输入", text)
        self.assertNotIn("closure_summary", text)


if __name__ == "__main__":
    unittest.main()
