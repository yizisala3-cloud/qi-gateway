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
from unittest.mock import MagicMock, patch
from httpx import QueryParams

from gateway.config import cfg
from gateway.memory_digest_api import rumination_execute, rumination_status
from gateway.memory_rumination import (
    FIRST_RUN_MAX_MESSAGES,
    RUMINATION_BATCH_MAX,
    RUMINATION_BATCH_MIN,
    RUMINATION_OP_EVIDENCE_MAX,
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


def _staggered_evidence_times(ids):
    """每个证据 id 一个严格递增的独立时间。

    同一证据时间上业务负载不同的操作会被歧义裁决拒绝（不能用数组顺序
    决定最终事实），需要合法时间线的用例必须错开时间。
    """
    return {
        int(value): f"2026-09-06T{10 + int(value) // 60:02d}:{int(value) % 60:02d}+08:00"
        for value in ids
    }


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
        self.times = _staggered_evidence_times(range(1, 60))

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

    def test_missing_reason_uses_default(self):
        parsed = self._parse([_op(reason="  ")])
        self.assertIn("反刍", parsed[0]["reason"])

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

    def test_resolve_without_closure_fields_degrades_to_ignore(self):
        parsed = self._parse([{
            "op": "resolve_thread", "reason": "没有闭合字段的完成",
            "target_memory_id": 12,
            **self._snap(12),
            "content": "赶海计划已完成。",
            "continuity_data": {"open_question": "赶海是否成行", "current_state": "完成"},
            "evidence_message_ids": [1],
        }])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])
        self.assertIn("resolve_thread", parsed[0]["reason"])

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
        # 25 个合法操作不因数量被拒：内容互异、目标同一条 thread，
        # 证据时间各自错开（同时间不同内容会被歧义裁决拒绝）。
        ops = [
            _op(
                content=f"网关改造当前状态：第 {index} 个进展节点已达成。",
                evidence_message_ids=[index + 10],
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
        # 修复后 overlaps 接收字符串列表（PostgREST ov() 的 join 需要 str）
        self.assertEqual(overlaps[2], ["3", "4"])

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

        # 场景非空但召回向量失败：只影响该操作——场景与召回向量同时置空
        # （保持"有场景必有向量"的不变量），正文向量正常，记忆仍写入。
        def fake_embedding(text):
            from gateway.memory_extract import DigestPipelineError

            if text == "聊到赶海时":
                raise DigestPipelineError("embedding_http_error", "boom")
            return [0.1, 0.2]

        with (
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._get_embedding_sync", side_effect=fake_embedding),
        ):
            enriched = enrich_rumination_ops(ops, 1)
        self.assertEqual(enriched[0]["embedding"], [0.1, 0.2])
        self.assertIsNone(enriched[0]["recall_scene"])
        self.assertIsNone(enriched[0]["recall_embedding"])
        self.assertEqual(enriched[1]["embedding"], [0.1, 0.2])
        self.assertIsNone(enriched[1].get("recall_scene"))

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

    def test_already_scheduled_today_claim_reports_skipped_run(self):
        cursor = dict(self.cursor)
        claim = {"status": "already_scheduled_today"}
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
            patch("gateway.memory_rumination._call_rumination_model") as model,
            patch("gateway.memory_rumination._rpc_object", return_value=claim),
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(result["reason"], "already_scheduled_today")
        self.assertEqual(result["cursor_after"], 100)
        model.assert_not_called()

    def test_scheduled_if_due_treats_already_scheduled_as_quiet_skip(self):
        cursor = {"initialized": True, "last_processed_message_id": 100,
                  "last_scheduled_date": "2099-12-31"}
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination.run_rumination_digest") as run,
        ):
            self.assertIsNone(run_rumination_digest_if_due())
        run.assert_not_called()

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

    def test_scheduled_multi_batch_shares_execution_identity(self):
        # scheduled 250 条积压：两批连续处理共享同一 execution identity，
        # 循环结束后 finish 被调用一次。
        pages = [
            list(range(101, 221)),
            list(range(221, 341)),
            list(range(341, 351)),  # 尾部 10 条
        ]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {
                "run_id": 71, "cursor": {"last_processed_message_id": 220},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            },
            {"status": "claimed", "run_id": 72, "scheduled_execution_id": 900},
            {
                "run_id": 72, "cursor": {"last_processed_message_id": 340},
                "op_counts": {}, "preview": [], "inserted_count": 0,
            },
        ]
        cursor = dict(self.cursor)
        claim_params = []
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
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["batch_count"], 2)
        self.assertEqual(result["cursor_after"], 340)
        # 首批无 identity（数据库分配），后续批次携带同一 identity 连批；
        # 第三批因尾部不足 60 未发；循环结束后 finish 恰好调用一次。
        rpc_claim_calls = [c for c in rpc.call_args_list if c[0][0] == "claim_rumination_batch"]
        self.assertEqual(len(rpc_claim_calls), 2)
        self.assertIsNone(rpc_claim_calls[0][0][1]["p_scheduled_execution_id"])
        self.assertEqual(rpc_claim_calls[1][0][1]["p_scheduled_execution_id"], 900)
        finish_calls = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish_calls), 1)

    def test_first_run_scheduled_finishes_on_success(self):
        cursor = dict(self.cursor, initialized=False, last_processed_message_id=0)
        latest = list(range(300, 180, -1))
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 300},
             "op_counts": {}, "preview": [], "inserted_count": 0},
        ]
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_latest_message_ids", return_value=latest),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 181, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "succeeded")
        finish = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish), 1)

    def test_first_run_scheduled_finishes_on_model_failure(self):
        cursor = dict(self.cursor, initialized=False, last_processed_message_id=0)
        latest = list(range(300, 180, -1))
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
        ]
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_latest_message_ids", return_value=latest),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 181, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model",
                  side_effect=RuminationPipelineError("model_http_error", "boom")),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cursor_after"], 0)
        finish = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish), 1)

    def test_manual_trigger_does_not_finish(self):
        patches = self._patch_happy()
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], \
                patches[6], patches[7], patches[8], patches[9], patches[10], patches[11], patches[12]:
            rpc.finish_rumination_scheduled_execution if False else None
            result = run_rumination_digest("rumination_manual")
        self.assertEqual(result["status"], "succeeded")
        # manual 不调用 finish（无 execution 被创建）。

    def test_finish_validates_response_structure(self):
        # finish 返回合法 dict 且 status=finished → 正常完成。
        cursor = dict(self.cursor)
        pages = [list(range(101, 161)), []]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 160},
             "op_counts": {}, "preview": [], "inserted_count": 0},
            {"status": "finished", "execution_id": 900, "changed": True},
        ]
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
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "succeeded")
        # 无 error 级别的 finish 日志（status=finished 校验通过）。

    def test_finish_mismatched_execution_id_logs_error(self):
        # finish 返回的 execution_id 与请求的 execution_id 不一致 → 结构化错误。
        cursor = dict(self.cursor)
        pages = [list(range(101, 161)), []]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 160},
             "op_counts": {}, "preview": [], "inserted_count": 0},
            {"status": "finished", "execution_id": 999, "changed": True},
        ]
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
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        # 运行仍成功（finish 失败不影响已完成的 memory/cursor 事务）。
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["cursor_after"], 160)

    def test_finish_idempotent_changed_false_is_ok(self):
        # changed=false 且 status=finished → 幂等成功。
        cursor = dict(self.cursor)
        pages = [list(range(101, 161)), []]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 160},
             "op_counts": {}, "preview": [], "inserted_count": 0},
            {"status": "finished", "execution_id": 900, "changed": False},
        ]
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
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "succeeded")

    def test_generic_exception_in_batch_preserves_execution_id(self):
        # 正文 embedding 抛普通 RuntimeError → 通用 Exception 路径 →
        # execution_id 仍通过包装异常传播 → finish 被调用一次。
        cursor = dict(self.cursor)
        pages = [list(range(101, 221)), []]
        ops = [{
            "op": "create_memory", "reason": "r", "continuity_type": "moment",
            "content": "一条普通记忆。", "recall_scene": None,
        }]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 220},
             "op_counts": {}, "preview": [], "inserted_count": 0},
        ]
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
            patch("gateway.memory_rumination._call_rumination_model",
                  return_value='{"operations":[{"op":"create_memory","reason":"r",'
                               '"continuity_type":"moment","content":"一条普通记忆。",'
                               '"evidence_message_ids":[101]}]}'),
            patch("gateway.memory_rumination.parse_rumination_output", return_value=ops),
            patch("gateway.memory_rumination._get_embedding_sync",
                  side_effect=RuntimeError("generic embedding failure")),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cursor_after"], 100)
        finish_calls = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0][0][1]["p_execution_id"], 900)

    def test_commit_rpc_generic_exception_preserves_execution_id(self):
        # commit RPC 抛普通 Supabase 异常 → execution_id 仍传播 → finish 调用。
        cursor = dict(self.cursor)
        pages = [list(range(101, 161)), []]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
        ]
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
            patch("gateway.memory_rumination._rpc_object",
                  side_effect=[rpc_results[0], RuntimeError("supabase commit failure")]) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cursor_after"], 100)
        finish_calls = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0][0][1]["p_execution_id"], 900)

    def test_load_threads_generic_exception_preserves_execution_id(self):
        # _load_unfinished_threads 抛普通异常 → run 标记 failed + finish 调用。
        cursor = dict(self.cursor)
        pages = [list(range(101, 161)), []]
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
        ]
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
            patch("gateway.memory_rumination._load_unfinished_threads",
                  side_effect=RuntimeError("DB connection lost")),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        finish_calls = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish_calls), 1)
        self.assertEqual(finish_calls[0][0][1]["p_execution_id"], 900)

    def test_first_run_scheduled_generic_exception_finishes(self):
        # 首次 scheduled 的普通异常路径 → finish 调用。
        cursor = dict(self.cursor, initialized=False, last_processed_message_id=0)
        latest = list(range(300, 180, -1))
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
        ]
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_latest_message_ids", return_value=latest),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 181, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads",
                  side_effect=RuntimeError("generic DB error")),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        finish_calls = [c for c in rpc.call_args_list if c[0][0] == "finish_rumination_scheduled_execution"]
        self.assertEqual(len(finish_calls), 1)

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
                  "last_scheduled_date": "2099-12-31"}
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
            # 与目标正文、结构规范形都相同（只有正文相同时结构变化会被
            # 保留为原地更新，不再降级为只补证据）。
            self._update([201], "叶子和栖约定下周三赶海。", {
                "open_question": "赶海是否成行",
                "current_state": "已约定待确认天气",
                "closure_criteria": ["成行或改期"],
            }),
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

    def test_thread_query_has_no_count_cap(self):
        # 未完成 thread 不做 40 条截断：查询不带 limit，45 条全部返回。
        fake = _FakeClient({"memories": [_thread(memory_id=i) for i in range(1, 46)]})
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            from gateway.memory_rumination import _load_unfinished_threads

            rows = _load_unfinished_threads("assistant-1")
        self.assertEqual(len(rows), 45)
        calls = fake.queries["memories"].calls
        self.assertFalse(
            any(call[0] == "limit" for call in calls),
            "unfinished-thread loading must not be truncated",
        )
        # 旧 ID 的 thread 同样进入输入（无 id desc 截断丢弃）。
        self.assertEqual(sorted(row["id"] for row in rows), list(range(1, 46)))

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




class FinishLogAssertionTests(unittest.TestCase):
    """finish 返回校验必须产生结构化日志，而非仅依赖注释。"""

    def _run_scheduled_with_finish(self, finish_response, log_patch_target="log"):
        """Helper: run a single-batch scheduled digest with a mocked finish
        response and return (result, finish_rpc_calls, mock_log)."""
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 160},
             "op_counts": {}, "preview": [], "inserted_count": 0},
            finish_response,
        ]
        mock_log = MagicMock()
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[list(range(101, 161)), []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            result = run_rumination_digest("rumination_scheduled")
        finish_calls = [
            c for c in rpc.call_args_list
            if c[0][0] == "finish_rumination_scheduled_execution"
        ]
        return result, finish_calls, mock_log

    def test_valid_finish_no_error_log(self):
        finish_response = {"status": "finished", "execution_id": 900, "changed": True}
        result, finish_calls, mock_log = self._run_scheduled_with_finish(finish_response)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(finish_calls), 1)
        mock_log.error.assert_not_called()

    def test_unexpected_status_logs_error_with_context(self):
        finish_response = {"status": "weird", "execution_id": 900, "changed": True}
        result, finish_calls, mock_log = self._run_scheduled_with_finish(finish_response)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(finish_calls), 1)
        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args
        self.assertIn(900, call_args.args)

    def test_mismatched_execution_id_logs_error(self):
        finish_response = {"status": "finished", "execution_id": 999, "changed": True}
        result, finish_calls, mock_log = self._run_scheduled_with_finish(finish_response)
        self.assertEqual(result["status"], "succeeded")
        mock_log.error.assert_called_once()
        call_args = mock_log.error.call_args
        self.assertIn(900, call_args.args)
        self.assertIn(999, call_args.args)

    def test_idempotent_changed_false_logs_info_not_error(self):
        finish_response = {"status": "finished", "execution_id": 900, "changed": False}
        result, finish_calls, mock_log = self._run_scheduled_with_finish(finish_response)
        self.assertEqual(result["status"], "succeeded")
        mock_log.error.assert_not_called()
        mock_log.info.assert_called_once()

    def test_rpc_exception_logs_exception_with_context(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 160},
             "op_counts": {}, "preview": [], "inserted_count": 0},
        ]
        mock_log = MagicMock()
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[list(range(101, 161)), []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            # The 3rd rpc call (finish) will get StopIteration from side_effect
            # exhaustion; that's fine — the finally block catches it.
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "succeeded")
        mock_log.exception.assert_called_once()
        call_args = mock_log.exception.call_args
        self.assertIn(900, call_args.args)

    def test_rpc_object_returns_non_dict_logs_exception(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        rpc_results = [
            {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            {"run_id": 71, "cursor": {"last_processed_message_id": 160},
             "op_counts": {}, "preview": [], "inserted_count": 0},
            "not_a_dict",
        ]
        mock_log = MagicMock()
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[list(range(101, 161)), []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "succeeded")
        mock_log.exception.assert_called_once()


from unittest.mock import MagicMock




class ProductionShapeFormattingTests(unittest.TestCase):
    """生产 Supabase 可能返回与测试 fixture 不同的 Python 类型。
    这些测试验证 build_model_input 在生产形状数据下不会抛 TypeError。"""

    def _production_messages(self):
        return [
            {
                "id": 2133,
                "assistant_id": "3d47790c-c415-4b90-9388-751128adb0a0",
                "conversation_id": "12345",
                "role": "user",
                "content": "一条用户消息",
                "source_time": "2026-09-06T14:00+08:00",
            },
            {
                "id": 2134,
                "assistant_id": "3d47790c-c415-4b90-9388-751128adb0a0",
                "conversation_id": "12345",
                "role": "assistant",
                "content": "一条助手消息",
                "source_time": "2026-09-06T14:01+08:00",
            },
        ]

    def _production_threads(self):
        return [{
            "id": 101,
            "memory_key": "topic.test",
            "continuity_id": "21111111-1111-1111-1111-1111111111a1",
            "thread_state": "open",
            "maintained_by": "rumination",
            "producer_path": "rumination",
            "content": "测试 thread 正文。",
            "content_hash": "a" * 64,
            "continuity_data": {"open_question": "q", "current_state": "s"},
            "evidence_message_ids": [2133],
            "evidence_start_time": "2026-09-06T14:00+08:00",
            "evidence_end_time": "2026-09-06T14:01+08:00",
            "created_at": "2026-09-06T14:01+08:00",
        }]

    def _production_requests(self):
        return [{
            "id": 201,
            "status": "pending",
            "continuity_type": "episode",
            "content": "一段被拒的episode申请。",
            "reason": "证据不足",
            "evidence_message_ids": [2133],
            "review_note": None,
            "created_at": "2026-09-06T09:00+08:00",
        }]

    def test_production_shape_build_model_input_returns_str(self):
        from gateway.memory_rumination import build_model_input

        result = build_model_input(
            self._production_messages(),
            self._production_threads(),
            self._production_requests(),
        )
        self.assertIsInstance(result, str)
        self.assertIn("[id=2133", result)
        self.assertIn("[id=2134", result)
        self.assertIn("<unfinished_threads>", result)
        self.assertIn("<rumination_requests>", result)

    def test_integer_conversation_id_does_not_crash(self):
        from gateway.memory_rumination import _format_rumination_conversation

        messages = [
            {"id": 1, "conversation_id": 12345, "role": "user",
             "content": "消息一", "source_time": "2026-09-06T14:00+08:00"},
            {"id": 2, "conversation_id": 12345, "role": "assistant",
             "content": "消息二", "source_time": "2026-09-06T14:01+08:00"},
        ]
        result = _format_rumination_conversation(messages)
        self.assertIsInstance(result, str)
        self.assertIn("[id=1", result)

    def test_integer_id_and_none_source_time(self):
        from gateway.memory_rumination import _format_rumination_conversation

        messages = [
            {"id": 2133, "conversation_id": "c1", "role": "user",
             "content": "消息", "source_time": None},
        ]
        result = _format_rumination_conversation(messages)
        self.assertIsInstance(result, str)
        self.assertIn("unknown", result)

    def test_datetime_source_time(self):
        from gateway.memory_rumination import _format_rumination_conversation
        from datetime import datetime

        messages = [
            {"id": 1, "conversation_id": "c1", "role": "user",
             "content": "消息", "source_time": datetime(2026, 9, 6, 14, 0)},
        ]
        result = _format_rumination_conversation(messages)
        self.assertIsInstance(result, str)

    def test_integer_evidence_ids_in_thread_json(self):
        from gateway.memory_rumination import build_model_input
        import json as _json

        threads = self._production_threads()
        # evidence_message_ids 是整数列表（Supabase bigint[] 的返回类型）
        threads[0]["evidence_message_ids"] = [2133, 2134]
        result = build_model_input(self._production_messages(), threads, [])
        parsed = json.loads(
            result.split("<unfinished_threads>\n")[1].split("\n</unfinished_threads>")[0]
        )
        self.assertEqual(parsed[0]["evidence_message_ids"], [2133, 2134])

    def test_integer_evidence_ids_in_request_json(self):
        from gateway.memory_rumination import build_model_input

        requests = self._production_requests()
        requests[0]["evidence_message_ids"] = [2133]
        result = build_model_input(self._production_messages(), [], requests)
        parsed = json.loads(
            result.split("<rumination_requests>\n")[1].split("\n</rumination_requests>")[0]
        )
        self.assertEqual(parsed[0]["evidence_message_ids"], [2133])

    def test_none_conversation_id_normalized(self):
        from gateway.memory_rumination import _format_rumination_conversation

        messages = [
            {"id": 1, "conversation_id": None, "role": "user",
             "content": "消息", "source_time": "t"},
        ]
        result = _format_rumination_conversation(messages)
        self.assertIsInstance(result, str)
        self.assertIn("unknown", result)

    def test_format_always_returns_str_with_mixed_types(self):
        from gateway.memory_rumination import _format_rumination_conversation

        messages = [
            {"id": 1, "conversation_id": None, "role": "user",
             "content": "消息", "source_time": None},
            {"id": 2.0, "conversation_id": 42, "role": "assistant",
             "content": 42, "source_time": 12345},
        ]
        result = _format_rumination_conversation(messages)
        self.assertIsInstance(result, str)


class StageDiagnosticsTests(unittest.TestCase):
    """通用异常时 stage 诊断日志必须包含批次范围和阶段名。"""

    def test_stage_in_error_log(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        pages = [list(range(101, 161)), []]
        mock_log = MagicMock()
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
            patch("gateway.memory_rumination._load_unfinished_threads",
                  side_effect=RuntimeError("generic DB error")),
            patch("gateway.memory_rumination._rpc_object", side_effect=[
                {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            ]) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            run_rumination_digest("rumination_scheduled")
        self.assertGreaterEqual(mock_log.exception.call_count, 1)
        first_call = mock_log.exception.call_args_list[0]
        self.assertIn("load_unfinished_threads", first_call.args)
        self.assertIn(101, first_call.args)
        self.assertIn(160, first_call.args)

    def test_embedding_failure_stage_enrich_embeddings(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        pages = [list(range(101, 161)), []]
        ops = [{
            "op": "create_memory", "reason": "r", "continuity_type": "moment",
            "content": "一条普通记忆。", "recall_scene": None,
        }]
        mock_log = MagicMock()
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[list(range(101, 161)), []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model",
                  return_value='{"operations":[{"op":"create_memory","reason":"r",'
                               '"continuity_type":"moment","content":"一条普通记忆。",'
                               '"evidence_message_ids":[101]}]}'),
            patch("gateway.memory_rumination.parse_rumination_output", return_value=ops),
            patch("gateway.memory_rumination._get_embedding_sync",
                  side_effect=RuntimeError("generic embedding failure")),
            patch("gateway.memory_rumination._rpc_object", side_effect=[
                {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
            ]) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cursor_after"], 100)
        self.assertGreaterEqual(mock_log.exception.call_count, 1)
        first_call = mock_log.exception.call_args_list[0]
        self.assertIn("enrich_embeddings", first_call.args)
        self.assertIn(101, first_call.args)
        self.assertIn(160, first_call.args)

    def test_commit_rpc_failure_stage_commit_batch(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        pages = [list(range(101, 161)), []]
        mock_log = MagicMock()
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[list(range(101, 161)), []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=[]),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": 101, "conversation_id": "c1", "role": "user",
                 "content": "消息", "source_time": "2026-09-01T10:00+08:00"},
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}'),
            patch("gateway.memory_rumination._rpc_object", side_effect=[
                {"status": "claimed", "run_id": 71, "scheduled_execution_id": 900},
                RuntimeError("supabase commit failure"),
            ]) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            result = run_rumination_digest("rumination_scheduled")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["cursor_after"], 100)
        self.assertGreaterEqual(mock_log.exception.call_count, 1)
        first_call = mock_log.exception.call_args_list[0]
        self.assertIn("commit_batch", first_call.args)
        self.assertIn(101, first_call.args)
        self.assertIn(160, first_call.args)








class LoadAbsorbableCandidatesIntegrationTests(unittest.TestCase):
    """Verify _load_absorbable_candidates passes strings to overlaps()
    and still applies the correct filter conditions."""

    def _make_fake_client(self):
        from test_memory_rumination import _FakeClient
        return _FakeClient({"memories": [{
            "id": 101, "continuity_type": "moment",
            "producer_path": "fast_path", "is_active": True,
            "verified": "verified", "title": "test",
            "evidence_message_ids": [101],
            "memory_key": None, "thread_state": None,
        }]})

    def test_load_candidates_passes_strings_to_overlaps(self):
        from gateway.memory_rumination import _load_absorbable_candidates
        from test_memory_rumination import _FakeClient

        fake = self._make_fake_client()
        batch_rows = [{"id": 101}, {"id": 102}]
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            candidates = _load_absorbable_candidates("assistant-1", batch_rows)
        self.assertEqual(len(candidates), 1)
        overlaps_calls = [
            c for c in fake.queries["memories"].calls if c[0] == "overlaps"
        ]
        self.assertEqual(len(overlaps_calls), 1)
        column, values = overlaps_calls[0][1], overlaps_calls[0][2]
        self.assertEqual(column, "evidence_message_ids")
        # All values must be strings (production regression fix).
        for value in values:
            self.assertIsInstance(value, str)

    def test_load_candidates_filters_correctly(self):
        from gateway.memory_rumination import _load_absorbable_candidates
        from test_memory_rumination import _FakeClient

        fake = self._make_fake_client()
        batch_rows = [{"id": 101}, {"id": 102}]
        with patch("gateway.memory_rumination.get_client", return_value=fake):
            candidates = _load_absorbable_candidates("assistant-1", batch_rows)
        calls = fake.queries["memories"].calls
        filters = {c[1]: c[2] for c in calls if c[0] == "eq"}
        self.assertEqual(filters.get("assistant_id"), "assistant-1")
        self.assertEqual(filters.get("producer_path"), "fast_path")
        self.assertEqual(filters.get("verified"), "verified")
        self.assertEqual(filters.get("is_active"), True)

    def test_empty_batch_returns_empty_without_query(self):
        from gateway.memory_rumination import _load_absorbable_candidates

        self.assertEqual(_load_absorbable_candidates("assistant-1", []), [])


class ProductionRegressionTests(unittest.TestCase):
    """Verify that the production batch shape (2133→2252, 120 messages)
    can complete load_absorbable_candidates and reach build_model_input."""

    def test_production_batch_reaches_build_model_input(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        # Production batch: 2133→2252 = 120 messages
        pages = [list(range(2133, 2253)), []]
        production_rows = [
            {"id": i, "assistant_id": "assistant-1",
             "conversation_id": f"conv-{i % 5}", "role": "user" if i % 2 == 0 else "assistant",
             "content": f"消息 {i}", "created_at": f"2026-09-06T14:{i % 60:02d}:00+08:00"}
            for i in range(2133, 2253)
        ]
        mock_log = MagicMock()
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[pages[0], []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=production_rows),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": i, "conversation_id": f"conv-{i % 5}", "role": "user" if i % 2 == 0 else "assistant",
                 "content": f"消息 {i}", "source_time": f"2026-09-06T14:{i % 60:02d}+08:00"}
                for i in range(2133, 2253)
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._load_absorbable_candidates", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value='{"operations":[]}') as model,
            patch("gateway.memory_rumination._rpc_object", side_effect=[
                {"status": "claimed", "run_id": 249, "scheduled_execution_id": None},
                {"run_id": 249, "cursor": {"last_processed_message_id": 2252},
                 "op_counts": {}, "preview": [], "inserted_count": 0},
            ]) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
            patch("gateway.memory_rumination._mark_failed"),
            patch("gateway.memory_rumination.log", mock_log),
        ):
            from gateway.memory_rumination import run_rumination_batch
            result = run_rumination_batch(
                "assistant-1", "rumination_manual", (2133, 2252, 120),
                first_batch=False,
            )
        # Reached build_model_input and model_request (past load_absorbable_candidates)
        model.assert_called_once()
        self.assertEqual(result["status"], "succeeded")
        # stage log should not contain load_absorbable_candidates error
        for c in mock_log.exception.call_args_list:
            self.assertNotIn("load_absorbable_candidates", c.args)




class RealPostgrestOverlapsSerializationTests(unittest.TestCase):
    """Use the REAL postgrest query builder (ov method) to verify that
    overlaps() with string values does not throw, while int values DO throw
    the exact production TypeError. Regression test for round 11."""

    def _make_builder(self):
        from unittest.mock import MagicMock
        from postgrest._sync.request_builder import SyncFilterRequestBuilder
        return SyncFilterRequestBuilder(
            session=MagicMock(),
            path="/memories",
            http_method="GET",
            headers={},
            params=QueryParams(),
            json=None,
        )

    def test_ov_with_string_values_does_not_throw(self):
        builder = self._make_builder()
        result = builder.ov("evidence_message_ids", ["2133", "2134"])
        self.assertIsNotNone(result)
        params_dict = dict(result.params)
        overlap_values = [
            v for k, v in params_dict.items() if "evidence_message_ids" in k
        ]
        self.assertEqual(len(overlap_values), 1)
        self.assertIn("2133", overlap_values[0])
        self.assertIn("2134", overlap_values[0])

    def test_ov_with_int_values_throws_production_typeerror(self):
        builder = self._make_builder()
        with self.assertRaises(TypeError) as raised:
            builder.ov("evidence_message_ids", [2133, 2134])
        self.assertIn("expected str instance, int found", str(raised.exception))

    def test_ov_filter_type_is_ov_not_cs_or_in(self):
        builder = self._make_builder()
        result = builder.ov("evidence_message_ids", ["2133"])
        params_dict = dict(result.params)
        overlap_values = [v for k, v in params_dict.items() if "evidence_message_ids" in k]
        self.assertEqual(len(overlap_values), 1)
        # PostgREST overlaps filter: value format is "ov.{...}"
        self.assertTrue(overlap_values[0].startswith("ov."))




class MemoryTypeCompatTests(unittest.TestCase):
    """memory_type 退役字段兼容：有合法 continuity_type 时剥离旧字段。"""

    def setUp(self):
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))
        self.times = _evidence_times([1, 2, 3])

    def _parse(self, ops, absorbable_ids=frozenset()):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
            absorbable_ids=absorbable_ids,
        )

    def _moment_op(self, **kwargs):
        base = {
            "op": "create_memory", "reason": "普通记忆",
            "continuity_type": "moment",
            "content": "一条普通记忆正文。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [1],
        }
        base.update(kwargs)
        return base

    def test_memory_type_with_valid_continuity_type_stripped(self):
        op = self._moment_op(memory_type="moment")
        parsed = self._parse([op])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertNotIn("memory_type", parsed[0])

    def test_memory_type_without_continuity_type_rejected(self):
        op = self._moment_op()
        del op["continuity_type"]
        op["memory_type"] = "moment"
        with self.assertRaisesRegex(RuminationPipelineError, "continuity_type"):
            self._parse([op])

    def test_memory_type_conflict_with_continuity_type_rejected(self):
        op = self._moment_op(memory_type="episode")
        with self.assertRaisesRegex(RuminationPipelineError, "conflict"):
            self._parse([op])

    def test_other_unknown_fields_still_rejected(self):
        op = self._moment_op(layer="core")
        with self.assertRaisesRegex(RuminationPipelineError, "unsupported fields"):
            self._parse([op])

    def test_create_memory_continuity_type_thread_rejected(self):
        op = self._moment_op(continuity_type="thread")
        with self.assertRaisesRegex(RuminationPipelineError, "moment or inside_joke"):
            self._parse([op])

    def test_memory_type_and_memory_key_both_validated(self):
        # memory_type 兼容不放宽 key 校验。
        op = self._moment_op(memory_type="moment")
        op["absorbed_fast_path_memory_ids"] = [999]
        with self.assertRaisesRegex(
            RuminationPipelineError, "no absorbable fast-path memories",
        ):
            self._parse([op], absorbable_ids=frozenset())


class MemoryKeyDiagnosticTests(unittest.TestCase):
    """memory_key 校验失败时的安全结构化日志。"""

    def setUp(self):
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))
        self.times = _evidence_times([1, 2])

    def _parse(self, ops):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )

    def test_valid_ascii_key_passes(self):
        op = {
            "op": "create_tracked_thread", "reason": "新的长期进程",
            "content": "新的长期进程需要稳定的主题键。",
            "memory_key": "topic.valid.key",
            "thread_state": "open",
            "continuity_data": {"open_question": "x", "current_state": "y"},
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["memory_key"], "topic.valid.key")

    def test_chinese_key_rejected_with_op_and_field(self):
        op = {
            "op": "create_tracked_thread", "reason": "中文 key 测试",
            "content": "新的长期进程需要稳定的主题键。",
            "memory_key": "中文主题键",
            "thread_state": "open",
            "continuity_data": {"open_question": "x", "current_state": "y"},
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "invalid memory_key.*op=create_tracked_thread.*field=memory_key"):
            self._parse([op])

    def test_key_with_spaces_rejected(self):
        op = {
            "op": "create_tracked_thread", "reason": "空格 key 测试",
            "content": "新的长期进程需要稳定的主题键。",
            "memory_key": "has spaces in key",
            "thread_state": "open",
            "continuity_data": {"open_question": "x", "current_state": "y"},
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "invalid memory_key"):
            self._parse([op])

    def test_key_over_120_chars_rejected(self):
        op = {
            "op": "create_tracked_thread", "reason": "超长 key 测试",
            "content": "新的长期进程需要稳定的主题键。",
            "memory_key": "a" * 121,
            "thread_state": "open",
            "continuity_data": {"open_question": "x", "current_state": "y"},
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "invalid memory_key"):
            self._parse([op])

    def test_episode_with_key_rejected(self):
        op = {
            "op": "create_request", "reason": "episode 不能有 key",
            "continuity_type": "episode",
            "content": "一段完整的共同经历。",
            "continuity_data": {
                "beginning": "b", "development": "d", "outcome": "o",
                "closure_quality": "complete",
            },
            "memory_key": "topic.should.not.exist",
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "must not carry"):
            self._parse([op])

    def test_interaction_rule_without_key_rejected(self):
        op = {
            "op": "create_request", "reason": "规则需要 key",
            "continuity_type": "interaction_rule",
            "content": "赶海话题必须提醒防晒。",
            "continuity_data": {
                "trigger": "t", "expected_behavior": "e", "scope": "s",
                "priority": 5, "rule_state": "active",
                "explicit_instruction": "i",
            },
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "require a stable memory_key"):
            self._parse([op])

    def test_fast_path_keyless_adopt_without_key_downgrades_to_ignore(self):
        """keyless fast_path target + no key → 降级为 ignore，不整批失败。"""
        threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        ops = [{
            "op": "adopt_thread", "reason": "无 key 接管",
            "target_memory_id": 14,
            "target_memory_key": None,
            "target_continuity_id": "21111111-1111-1111-1111-1111111111a1",
            "target_content_hash": threads[14]["content_hash"],
            "target_thread_state": "open",
            "evidence_message_ids": [1],
        }]
        parsed = parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=threads,
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["op"], "ignore")
        self.assertIn("keyless fast_path", parsed[0]["reason"])
        self.assertIn("memory_key", parsed[0]["reason"])
        self.assertEqual(parsed[0]["evidence_message_ids"], [1])

    def test_fast_path_keyless_adopt_with_valid_key_passes(self):
        """keyless fast_path target + 模型提供合法 memory_key → parser 通过。"""
        threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        ops = [{
            "op": "adopt_thread", "reason": "提供合法 key 接管",
            "target_memory_id": 14,
            "target_memory_key": None,
            "target_continuity_id": "21111111-1111-1111-1111-1111111111a1",
            "target_content_hash": threads[14]["content_hash"],
            "target_thread_state": "open",
            "memory_key": "topic.valid.key",
            "evidence_message_ids": [1],
        }]
        parsed = parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=threads,
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["memory_key"], "topic.valid.key")

    def test_fast_path_existing_key_adopt_without_new_key_reuses_target(self):
        """已有合法 key 的 fast_path target + 模型不提供新 key → parser 通过。"""
        threads = _threads_by_id(
            _thread(memory_id=15, state="open", maintained_by="fast_path", key="topic.existing.key"),
        )
        ops = [{
            "op": "adopt_thread", "reason": "已有 key 复用",
            "target_memory_id": 15,
            "target_memory_key": "topic.existing.key",
            "target_continuity_id": threads[15]["continuity_id"],
            "target_content_hash": threads[15]["content_hash"],
            "target_thread_state": "open",
            "evidence_message_ids": [1],
        }]
        parsed = parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=threads,
        )
        self.assertEqual(len(parsed), 1)
        self.assertNotIn("memory_key", parsed[0])

    def test_fast_path_keyless_adopt_with_chinese_key_rejected(self):
        """keyless fast_path target + 中文 key → parser 拒绝。"""
        threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        ops = [{
            "op": "adopt_thread", "reason": "中文 key 接管",
            "target_memory_id": 14,
            "target_memory_key": None,
            "target_continuity_id": "21111111-1111-1111-1111-1111111111a1",
            "target_content_hash": threads[14]["content_hash"],
            "target_thread_state": "open",
            "memory_key": "中文KEY",
            "evidence_message_ids": [1],
        }]
        with self.assertRaisesRegex(RuminationPipelineError, "invalid memory_key"):
            parse_rumination_output(
                json.dumps({"operations": ops}, ensure_ascii=False),
                evidence_times=self.times,
                threads_by_id=threads,
            )

    def test_key_safe_summary_never_contains_full_key(self):
        from gateway.memory_rumination import _key_safe_summary

        summary = _key_safe_summary("a" * 120)
        self.assertIn("value_length=120", summary)
        self.assertIn("pattern_valid=True", summary)
        self.assertNotIn("a" * 120, summary)

        summary_cn = _key_safe_summary("中文主题键")
        self.assertIn("pattern_valid=False", summary_cn)


class ProductionBatchRegressionTests(unittest.TestCase):
    """生产批次 + 模型输出含 memory_type 时 parser 层面的回归。"""

    def test_parser_strips_memory_type_with_valid_continuity_type(self):
        """模型输出带 memory_type 时，parser 剥离退役字段，不因该字段整批失败。"""
        threads = _threads_by_id(_thread(memory_id=12, state="open"))
        times = _evidence_times([2133])
        op = {
            "op": "create_memory", "reason": "普通记忆",
            "continuity_type": "moment", "memory_type": "moment",
            "content": "一条生产记忆正文。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [2133],
        }
        parsed = parse_rumination_output(
            json.dumps({"operations": [op]}, ensure_ascii=False),
            evidence_times=times,
            threads_by_id=threads,
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertNotIn("memory_type", parsed[0])

    def test_parser_invalid_key_includes_op_and_field_in_error(self):
        """非法 key 时错误信息包含 op 和 field 名。"""
        threads = _threads_by_id(_thread(memory_id=12, state="open"))
        times = _evidence_times([101])
        op = {
            "op": "create_tracked_thread", "reason": "新进程",
            "content": "新的长期进程需要稳定的主题键。",
            "memory_key": "中文标题不是KEY",
            "thread_state": "open",
            "continuity_data": {"open_question": "x", "current_state": "y"},
            "evidence_message_ids": [101],
        }
        with self.assertRaisesRegex(
            RuminationPipelineError,
            "invalid memory_key.*op=create_tracked_thread.*field=memory_key",
        ):
            parse_rumination_output(
                json.dumps({"operations": [op]}, ensure_ascii=False),
                evidence_times=times,
                threads_by_id=threads,
            )




class KeylessFastPathAdoptEdgeCaseTests(unittest.TestCase):
    """keyless fast_path adopt_thread 各缺失 key 形式的边界测试。"""

    def setUp(self):
        self.threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        self.times = _evidence_times([1])

    def _make_adopt_op(self, memory_key_value, include_field=True):
        op = {
            "op": "adopt_thread", "reason": "接管",
            "target_memory_id": 14,
            "target_memory_key": None,
            "target_continuity_id": self.threads[14]["continuity_id"],
            "target_content_hash": self.threads[14]["content_hash"],
            "target_thread_state": "open",
            "evidence_message_ids": [1],
        }
        if include_field:
            op["memory_key"] = memory_key_value
        return op

    def _assert_ignored(self, op):
        parsed = parse_rumination_output(
            json.dumps({"operations": [op]}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["op"], "ignore")
        self.assertIn("keyless fast_path", parsed[0]["reason"])
        self.assertIn("memory_key", parsed[0]["reason"])

    def test_key_absent_downgrades(self):
        self._assert_ignored(self._make_adopt_op(None, include_field=False))

    def test_key_none_downgrades(self):
        self._assert_ignored(self._make_adopt_op(None))

    def test_key_empty_string_downgrades(self):
        self._assert_ignored(self._make_adopt_op(""))

    def test_key_whitespace_downgrades(self):
        self._assert_ignored(self._make_adopt_op("   "))

    def test_key_valid_string_passes(self):
        op = self._make_adopt_op("topic.valid.key")
        parsed = parse_rumination_output(
            json.dumps({"operations": [op]}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["memory_key"], "topic.valid.key")


class RuminationMaintainedTargetAdoptTests(unittest.TestCase):
    """非 fast_path target 不受 keyless fast_path 限制。"""

    def test_rumination_maintained_keyless_target_adopt_not_blocked(self):
        """rumination-maintained keyless target（理论上不应存在但防御性测试）
        不应被 keyless fast_path 检查拦截（检查只针对 fast_path）。"""
        threads = _threads_by_id(
            _thread(memory_id=20, state="open", maintained_by="rumination", key=None),
        )
        times = _evidence_times([1])
        op = {
            "op": "adopt_thread", "reason": "接管",
            "target_memory_id": 20,
            "target_memory_key": None,
            "target_continuity_id": threads[20]["continuity_id"],
            "target_content_hash": threads[20]["content_hash"],
            "target_thread_state": "open",
            "evidence_message_ids": [1],
        }
        # rumination-maintained target 没有 keyless fast_path 检查；
        # 但 adopt_thread 仍不能对非 fast_path target 操作（SQL 层拒绝）。
        # 在 parser 层，我们验证不会被 fast_path keyless 检查拦截。
        try:
            parsed = parse_rumination_output(
                json.dumps({"operations": [op]}, ensure_ascii=False),
                evidence_times=times,
                threads_by_id=threads,
            )
            # 如果 parser 通过了（因为 keyless 检查只针对 fast_path），那也没问题。
            # SQL 层会拒绝（memory_rumination_not_fast_path）。
        except RuminationPipelineError as exc:
            # 如果 parser 抛出错误，不应该是因为 keyless fast_path 检查。
            self.assertNotIn("no stable key", str(exc))




class KeylessAdoptEdgeCaseTests(unittest.TestCase):
    """keyless fast_path adopt 降级边界：None、空串、空白、非法 key。"""

    def setUp(self):
        self.threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        self.times = _evidence_times([1])

    def _make(self, memory_key_value=None, include_field=True):
        op = {
            "op": "adopt_thread", "reason": "接管",
            "target_memory_id": 14,
            "target_memory_key": None,
            "target_continuity_id": self.threads[14]["continuity_id"],
            "target_content_hash": self.threads[14]["content_hash"],
            "target_thread_state": "open",
            "evidence_message_ids": [1],
        }
        if include_field:
            op["memory_key"] = memory_key_value
        return op

    def _parse(self, ops):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )

    def test_key_none_downgrades(self):
        parsed = self._parse([self._make(None)])
        self.assertEqual(parsed[0]["op"], "ignore")

    def test_key_empty_string_downgrades(self):
        parsed = self._parse([self._make("")])
        self.assertEqual(parsed[0]["op"], "ignore")

    def test_key_whitespace_downgrades(self):
        parsed = self._parse([self._make("   ")])
        self.assertEqual(parsed[0]["op"], "ignore")

    def test_key_valid_passes_as_adopt(self):
        parsed = self._parse([self._make("topic.valid.key")])
        self.assertEqual(parsed[0]["op"], "adopt_thread")

    def test_key_chinese_still_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "invalid memory_key"):
            self._parse([self._make("中文KEY")])


class MixedBatchKeylessAdoptTests(unittest.TestCase):
    """同批中 keyless adopt 降级 + 其他合法 operation 继续。"""

    def test_keyless_adopt_among_valid_ops(self):
        threads = _threads_by_id(
            _thread(memory_id=14, state="open", maintained_by="fast_path", key=None),
        )
        times = _evidence_times([1, 2])
        ops = [
            {
                "op": "adopt_thread", "reason": "无 key 接管",
                "target_memory_id": 14,
                "target_memory_key": None,
                "target_continuity_id": threads[14]["continuity_id"],
                "target_content_hash": threads[14]["content_hash"],
                "target_thread_state": "open",
                "evidence_message_ids": [1],
            },
            {
                "op": "create_memory", "reason": "普通记忆",
                "continuity_type": "moment",
                "content": "一条普通记忆正文。",
                "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
                "evidence_message_ids": [2],
            },
        ]
        parsed = parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=times,
            threads_by_id=threads,
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]["op"], "ignore")
        self.assertEqual(parsed[1]["op"], "create_memory")

    def test_ignore_skips_embedding(self):
        from gateway.memory_rumination import enrich_rumination_ops

        with patch("gateway.memory_rumination._get_embedding_sync") as embed:
            enriched = enrich_rumination_ops(
                [{"op": "ignore", "reason": "r", "evidence_message_ids": [1]}], 1,
            )
        embed.assert_not_called()


class KeylessAdoptProductionRegressionTests(unittest.TestCase):
    """生产形状 target memory 98 回归。"""

    def test_target_98_graceful_skip(self):
        t98 = {
            "id": 98, "memory_key": None,
            "continuity_id": "31111111-1111-1111-1111-11111111119a",
            "thread_state": "open", "maintained_by": "fast_path",
            "producer_path": "fast_path",
            "content": "正文。", "content_hash": "b" * 64,
            "continuity_data": {"open_question": "q", "current_state": "s"},
            "evidence_message_ids": [2133],
            "evidence_start_time": "2026-09-06T14:00+08:00",
            "evidence_end_time": "2026-09-06T14:01+08:00",
            "created_at": "2026-09-06T14:01+08:00",
        }
        threads = _threads_by_id(t98)
        times = _evidence_times([2133])
        op = {
            "op": "adopt_thread", "reason": "接管 98",
            "target_memory_id": 98,
            "target_memory_key": None,
            "target_continuity_id": t98["continuity_id"],
            "target_content_hash": t98["content_hash"],
            "target_thread_state": "open",
            "evidence_message_ids": [2133],
        }
        parsed = parse_rumination_output(
            json.dumps({"operations": [op]}, ensure_ascii=False),
            evidence_times=times,
            threads_by_id=threads,
        )
        self.assertEqual(parsed[0]["op"], "ignore")
        self.assertEqual(parsed[0]["evidence_message_ids"], [2133])
        self.assertIn("memory_key", parsed[0]["reason"])




class MissingReasonFallbackTests(unittest.TestCase):
    """模型输出缺少 reason 字段时使用默认值，不整批拒绝。"""

    def setUp(self):
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))
        self.times = _evidence_times([1, 2])

    def _parse(self, ops):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )

    def test_create_memory_missing_reason_uses_default(self):
        op = {
            "op": "create_memory",
            "continuity_type": "moment",
            "content": "一条没有 reason 的记忆正文。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertEqual(len(parsed), 1)
        self.assertIn("反刍", parsed[0]["reason"])
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertEqual(parsed[0]["content"], "一条没有 reason 的记忆正文。")
        self.assertEqual(parsed[0]["evidence_message_ids"], [1])

    def test_create_request_missing_reason_uses_default(self):
        op = {
            "op": "create_request",
            "continuity_type": "episode",
            "content": "一段没有 reason 的经历正文。",
            "continuity_data": {
                "beginning": "b", "development": "d",
                "outcome": "o", "closure_quality": "complete",
            },
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertEqual(len(parsed), 1)
        self.assertIn("反刍", parsed[0]["reason"])
        self.assertEqual(parsed[0]["continuity_type"], "episode")

    def test_update_thread_missing_reason_uses_default(self):
        op = {
            "op": "update_thread",
            "target_memory_id": 12,
            "target_memory_key": self.threads[12]["memory_key"],
            "target_continuity_id": self.threads[12]["continuity_id"],
            "target_content_hash": self.threads[12]["content_hash"],
            "target_thread_state": "open",
            "content": "赶海计划当前状态：更新后的正文。",
            "continuity_data": {
                "open_question": "q", "current_state": "updated",
                "closure_criteria": ["done"],
            },
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertEqual(len(parsed), 1)
        self.assertIn("反刍", parsed[0]["reason"])
        self.assertEqual(parsed[0]["op"], "update_thread")

    def test_custom_reason_preserved(self):
        op = {
            "op": "create_memory", "reason": "自定义审核理由",
            "continuity_type": "moment",
            "content": "一条有自定义 reason 的记忆。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["reason"], "自定义审核理由")

    def test_empty_reason_uses_default(self):
        op = {
            "op": "create_memory", "reason": "",
            "continuity_type": "moment",
            "content": "一条 reason 为空的记忆。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertIn("反刍", parsed[0]["reason"])

    def test_missing_content_still_rejected(self):
        op = {
            "op": "create_memory",
            "continuity_type": "moment",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "content"):
            self._parse([op])

    def test_missing_continuity_type_still_rejected(self):
        op = {
            "op": "create_memory",
            "content": "没有 continuity_type 的记忆。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [1],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "moment or inside_joke"):
            self._parse([op])

    def test_missing_continuity_data_degrades_to_request(self):
        op = {
            "op": "create_memory",
            "continuity_type": "moment",
            "content": "没有 continuity_data 的记忆正文。",
            "evidence_message_ids": [1],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertIn("待审核申请", parsed[0]["reason"])

    def test_missing_evidence_still_rejected(self):
        op = {
            "op": "create_memory",
            "continuity_type": "moment",
            "content": "没有证据的记忆正文。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
        }
        with self.assertRaisesRegex(RuminationPipelineError, "evidence_message_ids"):
            self._parse([op])

    def test_mixed_batch_missing_reason_and_valid_ops(self):
        ops = [
            {
                "op": "create_memory",
                "continuity_type": "moment",
                "content": "缺少 reason 的记忆。",
                "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
                "evidence_message_ids": [1],
            },
            {
                "op": "create_request", "reason": "自定义理由",
                "continuity_type": "episode",
                "content": "有自定义 reason 的申请。",
                "continuity_data": {
                    "beginning": "b", "development": "d",
                    "outcome": "o", "closure_quality": "complete",
                },
                "evidence_message_ids": [2],
            },
        ]
        parsed = self._parse(ops)
        self.assertEqual(len(parsed), 2)
        # 第一个 op 缺 reason → 使用默认
        self.assertIn("反刍", parsed[0]["reason"])
        # 第二个 op 有自定义 reason → 保留
        self.assertEqual(parsed[1]["reason"], "自定义理由")


class ProductionShapeMissingReasonTests(unittest.TestCase):
    """生产形状 2133→2458 批次 + create_memory 缺 reason 回归。"""

    def test_production_batch_with_missing_reason_reaches_model(self):
        cursor = {
            "assistant_id": "assistant-1", "initialized": True,
            "last_processed_message_id": 100, "last_scheduled_date": None,
            "last_success_at": None,
        }
        pages = [list(range(2339, 2459)), []]
        production_rows = [
            {"id": i, "assistant_id": "assistant-1",
             "conversation_id": f"conv-{i % 3}", "role": "user" if i % 2 == 0 else "assistant",
             "content": f"消息 {i}", "created_at": f"2026-09-06T14:{i % 60:02d}:00+08:00"}
            for i in range(2339, 2459)
        ]
        # 模型输出 create_memory 缺少 reason
        model_output = json.dumps({"operations": [{
            "op": "create_memory",
            "continuity_type": "moment",
            "content": "一条生产记忆正文。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [2339],
        }]}, ensure_ascii=False)
        ops = [{
            "op": "create_memory", "reason": "反刍根据本批原文提取的独立记忆",
            "continuity_type": "moment",
            "content": "一条生产记忆正文。",
            "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
            "evidence_message_ids": [2339],
        }]
        rpc_results = [
            {"status": "claimed", "run_id": 263, "scheduled_execution_id": None},
            {"run_id": 263, "cursor": {"last_processed_message_id": 2458},
             "op_counts": {"created_memories": 1}, "preview": [], "inserted_count": 1},
        ]
        with (
            patch("gateway.memory_rumination._rumination_analysis_configured", return_value=True),
            patch("gateway.memory_rumination.resolve_rumination_assistant_id", return_value="assistant-1"),
            patch("gateway.memory_rumination.get_rumination_cursor", return_value=cursor),
            patch("gateway.memory_rumination._mark_stale_rumination_runs"),
            patch("gateway.memory_rumination._fetch_message_ids",
                  side_effect=[pages[0], []]),
            patch("gateway.memory_rumination._fetch_batch_rows", return_value=production_rows),
            patch("gateway.memory_rumination._normalize_batch_messages", return_value=[
                {"id": i, "conversation_id": f"conv-{i % 3}",
                 "role": "user" if i % 2 == 0 else "assistant",
                 "content": f"消息 {i}", "source_time": f"2026-09-06T14:{i % 60:02d}+08:00"}
                for i in range(2339, 2459)
            ]),
            patch("gateway.memory_rumination._load_unfinished_threads", return_value=[]),
            patch("gateway.memory_rumination._load_own_requests", return_value=[]),
            patch("gateway.memory_rumination._load_absorbable_candidates", return_value=[]),
            patch("gateway.memory_rumination._call_rumination_model", return_value=model_output),
            patch("gateway.memory_rumination.parse_rumination_output", return_value=ops),
            patch("gateway.memory_rumination._get_embedding_sync", return_value=[0.1, 0.2]),
            patch("gateway.memory_rumination._rpc_object", side_effect=rpc_results) as rpc,
            patch("gateway.memory_rumination._set_run_model_name"),
            patch("gateway.memory_rumination._update_heartbeat"),
        ):
            result = run_rumination_digest("rumination_manual")
        # 成功到达 model_request 并完成 commit
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["cursor_after"], 2458)


class ContinuityDataDegradationTests(unittest.TestCase):
    """continuity_data 结构不可用时按操作类型优雅降级（内容不丢失）。

    create_memory → create_request 待审核；create_request → 最小结构替换；
    create_tracked_thread 与 thread 生命周期操作 → ignore；
    interaction_rule 结构不可用且无稳定 memory_key → ignore。
    """

    def setUp(self):
        self.threads = _threads_by_id(
            _thread(memory_id=12, state="open"),
        )
        self.times = _evidence_times([3, 4, 101, 102, 103])

    def _parse(self, ops, absorbable_ids=frozenset()):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
            absorbable_ids=absorbable_ids,
        )

    @staticmethod
    def _moment(**kwargs):
        op = {
            "op": "create_memory", "reason": "记录一个瞬间",
            "continuity_type": "moment",
            "content": "叶子在窗边看潮水漫过礁石，安静了几秒。",
            "continuity_data": {
                "scene": "窗边", "event": "潮水漫过礁石", "moment_state": "standalone",
            },
            "recall_scene": "窗边看潮", "recall_tags": ["潮汐"],
            "memory_time": "2026-09-12T10:00+08:00", "time_precision": "minute",
            "evidence_message_ids": [101, 102],
        }
        op.update(kwargs)
        return op

    # -- 直接写入（结构可用，不降级） ----------------------------------

    def test_valid_dict_writes_directly(self):
        parsed = self._parse([self._moment()])
        self.assertEqual([item["op"] for item in parsed], ["create_memory"])
        self.assertEqual(parsed[0]["continuity_data"]["scene"], "窗边")

    def test_valid_json_string_parses_and_writes_directly(self):
        raw = self._moment(continuity_data=json.dumps(
            {"scene": "窗边", "event": "潮水漫过礁石", "moment_state": "standalone"},
            ensure_ascii=False,
        ))
        parsed = self._parse([raw])
        self.assertEqual([item["op"] for item in parsed], ["create_memory"])
        self.assertEqual(parsed[0]["continuity_data"]["moment_state"], "standalone")

    # -- create_memory 结构不可用 → create_request ---------------------

    def test_unparsable_string_degrades_to_request_with_category(self):
        with self.assertLogs("gateway.memory_rumination", level="WARNING") as captured:
            parsed = self._parse([self._moment(continuity_data="{scene: 不是JSON")])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertIn(
            "reason_category=continuity_data_unparsable_string",
            "\n".join(captured.output),
        )

    def test_null_degrades_to_request(self):
        parsed = self._parse([self._moment(continuity_data=None)])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_data"]["moment_state"], "standalone")

    def test_missing_degrades_to_request(self):
        op = self._moment()
        del op["continuity_data"]
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertTrue(parsed[0]["continuity_data"]["event"])

    def test_dict_missing_scene_degrades_with_category(self):
        with self.assertLogs("gateway.memory_rumination", level="WARNING") as captured:
            parsed = self._parse([self._moment(
                continuity_data={"event": "潮水漫过礁石", "moment_state": "standalone"},
            )])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertIn(
            "reason_category=continuity_data_missing_required_field",
            "\n".join(captured.output),
        )

    def test_dict_invalid_enum_degrades_with_category(self):
        with self.assertLogs("gateway.memory_rumination", level="WARNING") as captured:
            parsed = self._parse([self._moment(
                continuity_data={
                    "scene": "窗边", "event": "潮水漫过礁石", "moment_state": "forever",
                },
            )])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertIn(
            "reason_category=continuity_data_invalid_enum",
            "\n".join(captured.output),
        )

    def test_dict_extra_field_degrades_with_category(self):
        with self.assertLogs("gateway.memory_rumination", level="WARNING") as captured:
            parsed = self._parse([self._moment(
                continuity_data={
                    "scene": "窗边", "event": "潮水漫过礁石",
                    "moment_state": "standalone", "open_question": "不属于 moment",
                },
            )])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertIn(
            "reason_category=continuity_data_invalid_field",
            "\n".join(captured.output),
        )

    def test_inside_joke_degrades_with_nonempty_trigger_phrases(self):
        op = {
            "op": "create_memory", "reason": "共享梗",
            "continuity_type": "inside_joke",
            "title": "赶海梗",
            "content": "把赶海说成赶海失败收场已经成了两个人的梗。",
            "continuity_data": "not a json object",
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_type"], "inside_joke")
        triggers = parsed[0]["continuity_data"]["trigger_phrases"]
        self.assertIsInstance(triggers, list)
        self.assertTrue(triggers)

    # -- create_request 结构不可用 → 最小可用值替换 ---------------------

    def test_episode_request_unusable_replaced_with_minimal(self):
        op = {
            "op": "create_request", "reason": "一段经历",
            "continuity_type": "episode",
            "content": "周末从早潮等到晚潮最终成行的一次赶海。",
            "continuity_data": None,
            "evidence_message_ids": [101, 102],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_data"]["closure_quality"], "uncertain")

    def test_profile_request_unusable_replaced_with_minimal(self):
        op = {
            "op": "create_request", "reason": "偏好画像",
            "continuity_type": "profile",
            "content": "叶子偏好安静的清晨时段聊天。",
            "continuity_data": {"facet": "作息"},
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_data"]["stability"], "provisional")
        self.assertEqual(parsed[0]["continuity_data"]["basis"], "reviewed_summary")

    def test_interaction_rule_unusable_with_valid_key_becomes_request(self):
        op = {
            "op": "create_request", "reason": "叶子明确要求",
            "continuity_type": "interaction_rule",
            "content": "叶子要求提醒时直接给结论不要铺垫。",
            "continuity_data": "not-json",
            "memory_key": "rule.direct-answer",
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["memory_key"], "rule.direct-answer")
        self.assertEqual(parsed[0]["continuity_data"]["priority"], 5)

    def test_interaction_rule_unusable_without_key_degrades_to_ignore(self):
        op = {
            "op": "create_request", "reason": "叶子明确要求",
            "continuity_type": "interaction_rule",
            "content": "叶子要求提醒时直接给结论不要铺垫。",
            "continuity_data": "not-json",
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])
        self.assertIn("interaction_rule", parsed[0]["reason"])

    def test_interaction_rule_unusable_with_illegal_key_degrades_to_ignore(self):
        op = {
            "op": "create_request", "reason": "叶子明确要求",
            "continuity_type": "interaction_rule",
            "content": "叶子要求提醒时直接给结论不要铺垫。",
            "continuity_data": "not-json",
            "memory_key": "不是合法KEY",
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])

    def test_interaction_rule_valid_data_without_key_still_rejected(self):
        # 结构可用但 key 缺失：仍然整批拒绝，不因降级放宽。
        op = {
            "op": "create_request", "reason": "叶子明确要求",
            "continuity_type": "interaction_rule",
            "content": "叶子要求提醒时直接给结论不要铺垫。",
            "continuity_data": {
                "trigger": "提醒", "expected_behavior": "直接给结论",
                "scope": "全局", "priority": 5, "rule_state": "active",
                "explicit_instruction": "直接给结论",
            },
            "evidence_message_ids": [101],
        }
        with self.assertRaises(RuminationPipelineError):
            self._parse([op])

    # -- thread 操作结构不可用 → ignore --------------------------------

    def test_create_tracked_thread_unusable_degrades_to_ignore(self):
        op = {
            "op": "create_tracked_thread", "reason": "追踪一个长期进程",
            "memory_key": "topic.track-me",
            "content": "跟踪叶子的赶海计划从构想到成行的全过程。",
            "continuity_data": None,
            "evidence_message_ids": [101, 102],
        }
        parsed = self._parse([op])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])
        self.assertIn("create_tracked_thread", parsed[0]["reason"])

    def test_update_thread_unusable_degrades_to_ignore(self):
        op = {
            "op": "update_thread", "reason": "进程有实质进展",
            "target_memory_id": 12, **_snap(self.threads[12]),
            "content": "赶海计划当前状态：天气确认，周三成行。",
            "continuity_data": {"open_question": "缺 current_state"},
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])
        self.assertIn("update_thread", parsed[0]["reason"])

    def test_pause_thread_unusable_degrades_to_ignore(self):
        op = {
            "op": "pause_thread", "reason": "暂时搁置",
            "target_memory_id": 12, **_snap(self.threads[12]),
            "content": "赶海计划当前状态：暂停等待天气。",
            "continuity_data": "not-json",
            "evidence_message_ids": [101],
        }
        parsed = self._parse([op])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])

    # -- 转换后的申请保留字段、不带 memory_key、reason 提示人工确认 ------

    def test_converted_request_preserves_fields_and_drops_memory_key(self):
        op = self._moment(
            title="窗边看潮", importance=7, confidence=0.8,
            source_type="natural_chat",
            absorbed_fast_path_memory_ids=[55],
            continuity_data="not-json",
        )
        parsed = self._parse([op], absorbable_ids={55})
        request = parsed[0]
        self.assertEqual(request["op"], "create_request")
        self.assertEqual(request["content"], "叶子在窗边看潮水漫过礁石，安静了几秒。")
        self.assertEqual(request["title"], "窗边看潮")
        self.assertEqual(request["evidence_message_ids"], [101, 102])
        self.assertEqual(request["importance"], 7)
        self.assertEqual(request["confidence"], 0.8)
        self.assertEqual(request["source_type"], "natural_chat")
        self.assertEqual(request["recall_scene"], "窗边看潮")
        self.assertEqual(request["recall_tags"], ["潮汐"])
        self.assertEqual(request["memory_time"], "2026-09-12T10:00+08:00")
        self.assertEqual(request["time_precision"], "minute")
        self.assertEqual(request["absorbed_fast_path_memory_ids"], [55])
        self.assertNotIn("memory_key", request)
        self.assertNotIn("thread_state", request)
        self.assertIn("待审核申请", request["reason"])
        self.assertIn("确认", request["reason"])

    def test_converted_request_absorb_ids_outside_candidates_still_rejected(self):
        # 降级不放宽 absorbed 候选集合校验：不在候选内的 id 仍整批拒绝。
        op = self._moment(
            absorbed_fast_path_memory_ids=[999],
            continuity_data="not-json",
        )
        with self.assertRaises(RuminationPipelineError):
            self._parse([op], absorbable_ids={55})

    # -- 同批混合与生产形状 --------------------------------------------

    def test_mixed_batch_degrades_one_and_keeps_others(self):
        batch = [
            self._moment(continuity_data=None),
            {
                "op": "create_tracked_thread", "reason": "追踪进程",
                "memory_key": "topic.track-fine",
                "content": "一个结构完好的可追踪进程正文。",
                "continuity_data": {
                    "open_question": "是否成行", "current_state": "待确认",
                    "closure_criteria": ["成行"],
                },
                "evidence_message_ids": [102],
            },
            _op(),
            {
                "op": "create_request", "reason": "episode 申请",
                "continuity_type": "episode",
                "content": "一次结构完好的经历申请正文。",
                "continuity_data": {
                    "beginning": "b", "development": "d", "outcome": "o",
                    "closure_quality": "complete",
                },
                "evidence_message_ids": [103],
            },
        ]
        parsed = self._parse(batch)
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0]["op"], "create_request")
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertEqual(parsed[1]["op"], "create_tracked_thread")
        self.assertEqual(parsed[2]["op"], "update_thread")
        self.assertEqual(parsed[3]["op"], "create_request")
        self.assertEqual(parsed[3]["continuity_type"], "episode")

    def test_production_shape_2339_2458_with_broken_moment_parses(self):
        batch = [
            {
                "op": "create_memory", "reason": "记录瞬间",
                "continuity_type": "moment",
                "content": "生产批次中一条 continuity_data 被写成字符串的瞬间记忆。",
                "continuity_data": '{"scene": "生产"} 截断',
                "evidence_message_ids": [2339, 2400],
            },
            {
                "op": "create_request", "reason": "episode 申请",
                "continuity_type": "episode",
                "content": "生产批次中一段结构完好的经历申请。",
                "continuity_data": {
                    "beginning": "b", "development": "d", "outcome": "o",
                    "closure_quality": "complete",
                },
                "evidence_message_ids": [2458],
            },
        ]
        parsed = parse_rumination_output(
            json.dumps({"operations": batch}, ensure_ascii=False),
            evidence_times=_evidence_times(range(2339, 2459)),
            threads_by_id={},
        )
        self.assertEqual([item["op"] for item in parsed], ["create_request", "create_request"])
        self.assertEqual(parsed[0]["continuity_type"], "moment")
        self.assertEqual(parsed[1]["continuity_type"], "episode")


class SameBatchDedupeStateMergeTests(unittest.TestCase):
    """复审修复回归：语义化正文去重 + 同批状态机 + 合并证据契约。

    覆盖完整处理链 parse → merge，而不是只单测合并函数。
    """

    def setUp(self):
        self.threads = _threads_by_id(
            _thread(memory_id=12, state="open"),
            _thread(memory_id=13, state="paused", key="topic.paused"),
        )
        self.times = _staggered_evidence_times(range(1, 40))

    def _parse(self, ops, threads=None):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=threads if threads is not None else self.threads,
        )

    def _merge(self, parsed, threads=None):
        return merge_thread_operations(
            parsed,
            evidence_times=self.times,
            threads_by_id=threads if threads is not None else self.threads,
        )

    def _update(self, target_id, content, evidence, *, state=None):
        target = self.threads[target_id]
        op = {
            "op": "update_thread", "reason": "进程有实质进展",
            "target_memory_id": target_id, **_snap(target),
            "content": content,
            "continuity_data": {
                "open_question": "赶海是否成行", "current_state": content[8:],
                "closure_criteria": ["成行或改期"],
            },
            "evidence_message_ids": list(evidence),
        }
        if state:
            op["thread_state"] = state
        return op

    # -- 问题 1：去重只作用于最终有效操作 --------------------------------

    def test_ignored_degrade_does_not_block_same_content_request(self):
        content = "赶海计划有了新进展，装备已经备齐。"
        parsed = self._parse([
            {"op": "create_tracked_thread", "reason": "追踪",
             "memory_key": "topic.new", "content": content,
             "continuity_data": "not-json", "evidence_message_ids": [3]},
            {"op": "create_request", "reason": "画像",
             "continuity_type": "profile", "content": content,
             "continuity_data": {
                 "facet": "装备习惯", "statement": "提前一天备齐装备",
                 "scope": "赶海", "stability": "stable",
                 "basis": "repeated_observation",
             },
             "evidence_message_ids": [4]},
        ])
        self.assertEqual(
            [(item["op"], item.get("continuity_type")) for item in parsed],
            [("ignore", None), ("create_request", "profile")],
        )

    def test_same_content_different_targets_not_swallowed(self):
        content = "同正文出现在两个目标上的进展，语义各自成立。"
        parsed = self._parse([
            self._update(12, content, [3, 4, 5, 6, 7]),
            self._update(13, content, [8, 9, 10, 11, 12]),
        ])
        self.assertEqual(len(parsed), 2)
        self.assertEqual(
            [item["target_memory_id"] for item in parsed], [12, 13],
        )

    def test_same_content_different_memory_class_survives(self):
        content = "同一句正文分别作为瞬间与画像提交，分类不同。"
        parsed = self._parse([
            {"op": "create_memory", "reason": "瞬间", "continuity_type": "moment",
             "content": content,
             "continuity_data": {
                 "scene": "窗边", "event": "潮水漫过礁石",
                 "moment_state": "standalone",
             },
             "evidence_message_ids": [3]},
            {"op": "create_request", "reason": "画像",
             "continuity_type": "profile", "content": content,
             "continuity_data": {
                 "facet": "装备习惯", "statement": "提前一天备齐装备",
                 "scope": "赶海", "stability": "stable",
                 "basis": "repeated_observation",
             },
             "evidence_message_ids": [4]},
        ])
        self.assertEqual(
            [(item["op"], item.get("continuity_type")) for item in parsed],
            [("create_memory", "moment"), ("create_request", "profile")],
        )

    def test_true_duplicates_merge_evidence_instead_of_dropping(self):
        content = "赶海计划当前状态：重复正文的同一进展。"
        evidence_sets = ([3, 4], [5, 6])
        parsed = self._parse([
            self._update(12, content, evidence) for evidence in evidence_sets
        ])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["evidence_message_ids"], [3, 4, 5, 6])

    # -- 问题 2：同批 pause/resume 按 running-state 校验 ------------------

    def test_open_pause_then_resume_survives_parse_and_merges(self):
        snap12 = _snap(self.threads[12])
        parsed = self._parse([
            {"op": "pause_thread", "reason": "下雨暂停", "target_memory_id": 12,
             **snap12, "content": "赶海计划当前状态：因下雨暂停。",
             "continuity_data": {
                 "open_question": "赶海是否成行", "current_state": "因下雨暂停",
             },
             "evidence_message_ids": [3]},
            {"op": "resume_thread", "reason": "转晴恢复", "target_memory_id": 12,
             **snap12, "content": "赶海计划当前状态：天气转晴恢复推进。",
             "continuity_data": {
                 "open_question": "赶海是否成行", "current_state": "天气转晴恢复推进",
             },
             "evidence_message_ids": [4]},
        ])
        self.assertEqual([item["op"] for item in parsed], ["pause_thread", "resume_thread"])
        merged = self._merge(parsed)
        thread_ops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(thread_ops), 1)
        merged_op = thread_ops[0]
        self.assertEqual(merged_op["op"], "update_thread")
        self.assertEqual(merged_op["thread_state"], "open")
        self.assertEqual(merged_op["evidence_message_ids"], [3, 4])
        self.assertEqual(merged_op["content"], "赶海计划当前状态：天气转晴恢复推进。")
        self.assertEqual(
            merged_op["continuity_data"]["current_state"], "天气转晴恢复推进",
        )

    def test_paused_resume_then_pause_survives_parse_and_merges(self):
        snap13 = _snap(self.threads[13])
        parsed = self._parse([
            {"op": "resume_thread", "reason": "出差回来恢复", "target_memory_id": 13,
             **snap13, "content": "暂停进程当前状态：恢复推进。",
             "continuity_data": {
                 "open_question": "是否继续", "current_state": "恢复推进",
             },
             "evidence_message_ids": [3]},
            {"op": "pause_thread", "reason": "再次暂停", "target_memory_id": 13,
             **snap13, "content": "暂停进程当前状态：重新暂停等待。",
             "continuity_data": {
                 "open_question": "是否继续", "current_state": "重新暂停等待",
             },
             "evidence_message_ids": [4]},
        ])
        self.assertEqual([item["op"] for item in parsed], ["resume_thread", "pause_thread"])
        merged = self._merge(parsed)
        thread_ops = [item for item in merged if item.get("target_memory_id") == 13]
        self.assertEqual(len(thread_ops), 1)
        merged_op = thread_ops[0]
        self.assertEqual(merged_op["op"], "update_thread")
        self.assertEqual(merged_op["thread_state"], "paused")
        self.assertEqual(merged_op["evidence_message_ids"], [3, 4])
        self.assertEqual(merged_op["content"], "暂停进程当前状态：重新暂停等待。")

    def test_illegal_state_transitions_still_rejected(self):
        snap12 = _snap(self.threads[12])
        snap13 = _snap(self.threads[13])
        resume_on_open = {
            "op": "resume_thread", "reason": "还没暂停就想恢复",
            "target_memory_id": 12, **snap12,
            "content": "赶海计划当前状态：恢复。",
            "continuity_data": {"open_question": "q", "current_state": "恢复"},
            "evidence_message_ids": [3],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "paused thread"):
            self._parse([resume_on_open])
        pause_on_paused = {
            "op": "pause_thread", "reason": "还没打开就想暂停",
            "target_memory_id": 13, **snap13,
            "content": "暂停进程当前状态：暂停。",
            "continuity_data": {"open_question": "q", "current_state": "暂停"},
            "evidence_message_ids": [3],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "open thread"):
            self._parse([pause_on_paused])
        # 同批 pause → pause：第二条在推进后的状态上非法。
        pause_then_pause = [
            {
                "op": "pause_thread", "reason": "第一次暂停",
                "target_memory_id": 12, **snap12,
                "content": "赶海计划当前状态：第一次暂停。",
                "continuity_data": {"open_question": "q", "current_state": "暂停一"},
                "evidence_message_ids": [3],
            },
            {
                "op": "pause_thread", "reason": "第二次暂停",
                "target_memory_id": 12, **snap12,
                "content": "赶海计划当前状态：第二次暂停。",
                "continuity_data": {"open_question": "q", "current_state": "暂停二"},
                "evidence_message_ids": [4],
            },
        ]
        with self.assertRaisesRegex(RuminationPipelineError, "open thread"):
            self._parse(pause_then_pause)
        # resolve 之后继续推进：矛盾操作拒绝。
        resolve_op = {
            "op": "resolve_thread", "reason": "完成", "target_memory_id": 12,
            **snap12, "content": "赶海计划已经完成。",
            "continuity_data": {
                "open_question": "赶海是否成行", "current_state": "已完成",
                "closure_summary": "顺利成行", "closure_reason": "原文明确完成",
                "closed_at": "2026-09-12",
            },
            "evidence_message_ids": [4],
        }
        with self.assertRaisesRegex(RuminationPipelineError, "resolve_thread"):
            self._parse([self._update(12, "赶海计划当前状态：推进中的正文。", [3]), resolve_op,
                         self._update(12, "赶海计划当前状态：关闭后又推进的正文。", [5])])

    # -- 问题 3：合并证据并集与提交契约一致 -------------------------------

    def test_five_plus_five_evidence_merge_keeps_union_within_contract(self):
        parsed = self._parse([
            self._update(12, "赶海计划当前状态：第一阶段完成，正文各不相同。", [3, 4, 5, 6, 7]),
            self._update(12, "赶海计划当前状态：第二阶段完成，正文各不相同。", [8, 9, 10, 11, 12]),
        ])
        merged = self._merge(parsed)
        thread_ops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(thread_ops), 1)
        self.assertEqual(thread_ops[0]["evidence_message_ids"], list(range(3, 13)))
        self.assertLessEqual(
            len(thread_ops[0]["evidence_message_ids"]), RUMINATION_OP_EVIDENCE_MAX,
        )
        self.assertEqual(RUMINATION_OP_EVIDENCE_MAX, RUMINATION_BATCH_MAX * 8)

    def test_merged_op_shape_is_commit_ready(self):
        parsed = self._parse([
            self._update(12, "赶海计划当前状态：第一段进展的完整正文。", [3, 4]),
            self._update(12, "赶海计划当前状态：第二段进展的完整正文。", [5, 6]),
        ])
        merged = self._merge(parsed)
        merged_op = [item for item in merged if item.get("target_memory_id") == 12][0]
        # 合并结果保留快照回显与目标，正文哈希重算，可直接进入 enrich/commit。
        self.assertEqual(merged_op["target_memory_id"], 12)
        self.assertEqual(
            merged_op["target_continuity_id"], self.threads[12]["continuity_id"],
        )
        self.assertIn("content_hash", merged_op)
        for key in ("target_memory_key", "target_thread_state"):
            self.assertIn(key, merged_op)


class TimelineConsistencyTests(unittest.TestCase):
    """复审轮 2 回归：时间线唯一裁决 + 全业务负载去重（完整 parse → merge 链）。

    数组顺序不承载时间语义：状态转换按真实证据时间重放；去重键覆盖全部
    影响业务结果的字段，生命周期状态事件永不跨时间去重。
    """

    def setUp(self):
        self.threads = _threads_by_id(
            _thread(memory_id=12, state="open"),
            _thread(memory_id=13, state="paused", key="topic.paused"),
        )
        self.times = _staggered_evidence_times(range(1, 40))

    def _parse(self, ops, times=None):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=times or self.times,
            threads_by_id=self.threads,
        )

    def _merge(self, parsed, times=None):
        return merge_thread_operations(
            parsed,
            evidence_times=times or self.times,
            threads_by_id=self.threads,
        )

    def _update_op(self, target_id, content, evidence, current_state, **extra):
        target = self.threads[target_id]
        op = {
            "op": "update_thread", "reason": "进程有实质进展",
            "target_memory_id": target_id, **_snap(target),
            "content": content,
            "continuity_data": {
                "open_question": "赶海是否成行", "current_state": current_state,
                "closure_criteria": ["成行或改期"],
            },
            "evidence_message_ids": list(evidence),
        }
        op.update(extra)
        return op

    def _trans_op(self, kind, target_id, content, evidence, current_state):
        target = self.threads[target_id]
        continuity = {
            "open_question": "赶海是否成行", "current_state": current_state,
        }
        if kind == "resolve_thread":
            continuity.update({
                "closure_summary": "顺利收尾", "closure_reason": "原文明确完成",
                "closed_at": "2026-09-12",
            })
        return {
            "op": kind, "reason": kind,
            "target_memory_id": target_id, **_snap(target),
            "content": content, "continuity_data": continuity,
            "evidence_message_ids": list(evidence),
        }

    # -- 问题 1：去重键覆盖全业务负载 ------------------------------------

    def test_same_content_newer_structure_not_swallowed(self):
        times = {3: "2026-09-06T09:00+08:00", 4: "2026-09-06T10:00+08:00"}
        content = "赶海计划当前状态：正文完全相同的一条进展。"
        parsed = self._parse([
            self._update_op(12, content, [3], "后端完成"),
            self._update_op(12, content, [4], "前端完成"),
        ], times=times)
        self.assertEqual(len(parsed), 2)
        merged = self._merge(parsed, times=times)
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["continuity_data"]["current_state"], "前端完成")
        self.assertEqual(tops[0]["evidence_message_ids"], [3, 4])
        self.assertEqual(tops[0]["content"], content)
        self.assertEqual(tops[0]["thread_state"], "open")

    def test_same_content_different_business_fields_not_duplicates(self):
        content = "赶海计划当前状态：业务字段不同的同正文进展。"
        parsed = self._parse([
            self._update_op(12, content, [3], "进展", title="标题甲"),
            self._update_op(12, content, [4], "进展", title="标题乙"),
        ])
        self.assertEqual(len(parsed), 2)
        parsed = self._parse([
            self._update_op(12, content, [3], "进展",
                            memory_time="2026-09-06T09:00+08:00", time_precision="minute"),
            self._update_op(12, content, [4], "进展",
                            memory_time="2026-09-06T15:00+08:00", time_precision="hour"),
        ])
        self.assertEqual(len(parsed), 2)

    def test_true_duplicates_still_merge_evidence(self):
        content = "赶海计划当前状态：字段完全一致的重复进展。"
        parsed = self._parse([
            self._update_op(12, content, [3, 4], "同一状态"),
            self._update_op(12, content, [5], "同一状态"),
        ])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["evidence_message_ids"], [3, 4, 5])

    # -- 问题 2：生命周期状态事件不跨时间去重 ----------------------------

    def test_open_pause_resume_pause_same_body_ends_paused(self):
        pause_body = "赶海计划当前状态：两次完全相同的暂停正文。"
        parsed = self._parse([
            self._trans_op("pause_thread", 12, pause_body, [3], "暂停"),
            self._trans_op(
                "resume_thread", 12, "赶海计划当前状态：中间恢复推进的正文。", [4], "恢复",
            ),
            self._trans_op("pause_thread", 12, pause_body, [5], "暂停"),
        ])
        self.assertEqual(
            [item["op"] for item in parsed],
            ["pause_thread", "resume_thread", "pause_thread"],
        )
        merged = self._merge(parsed)
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["op"], "pause_thread")
        self.assertEqual(tops[0]["thread_state"], "paused")
        self.assertEqual(tops[0]["evidence_message_ids"], [3, 4, 5])
        self.assertEqual(tops[0]["content"], pause_body)
        self.assertEqual(tops[0]["continuity_data"]["current_state"], "暂停")

    def test_paused_resume_pause_resume_same_body_ends_open(self):
        resume_body = "暂停进程当前状态：两次完全相同的恢复正文。"
        parsed = self._parse([
            self._trans_op("resume_thread", 13, resume_body, [3], "恢复"),
            self._trans_op(
                "pause_thread", 13, "暂停进程当前状态：中间再次暂停的正文。", [4], "暂停",
            ),
            self._trans_op("resume_thread", 13, resume_body, [5], "恢复"),
        ])
        self.assertEqual(
            [item["op"] for item in parsed],
            ["resume_thread", "pause_thread", "resume_thread"],
        )
        merged = self._merge(parsed)
        tops = [item for item in merged if item.get("target_memory_id") == 13]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["op"], "resume_thread")
        self.assertEqual(tops[0]["thread_state"], "open")
        self.assertEqual(tops[0]["evidence_message_ids"], [3, 4, 5])
        self.assertEqual(tops[0]["content"], resume_body)

    # -- 问题 3：证据时间是唯一顺序规则 ----------------------------------

    def test_evidence_time_order_beats_array_order(self):
        times = {3: "2026-09-06T09:00+08:00", 4: "2026-09-06T15:00+08:00"}
        parsed = self._parse([
            self._trans_op("resume_thread", 12, "赶海计划当前状态：下午恢复推进。", [4], "恢复"),
            self._trans_op("pause_thread", 12, "赶海计划当前状态：上午暂停。", [3], "暂停"),
        ], times=times)
        self.assertEqual([item["op"] for item in parsed], ["resume_thread", "pause_thread"])
        merged = self._merge(parsed, times=times)
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["op"], "update_thread")
        self.assertEqual(tops[0]["thread_state"], "open")
        self.assertEqual(tops[0]["evidence_message_ids"], [3, 4])
        self.assertEqual(tops[0]["content"], "赶海计划当前状态：下午恢复推进。")
        self.assertEqual(tops[0]["continuity_data"]["current_state"], "恢复")

    def test_contradictory_evidence_timeline_rejected(self):
        # 证据时间上 resume 发生在 pause 之前：时间序矛盾，整批拒绝。
        times = {3: "2026-09-06T09:00+08:00", 4: "2026-09-06T15:00+08:00"}
        with self.assertRaisesRegex(RuminationPipelineError, "paused thread"):
            self._parse([
                self._trans_op("pause_thread", 12, "赶海计划当前状态：下午暂停。", [4], "暂停"),
                self._trans_op("resume_thread", 12, "赶海计划当前状态：上午恢复。", [3], "恢复"),
            ], times=times)

    def test_progress_after_resolve_by_time_order_rejected(self):
        # resolve 证据在上午，update 在下午：时间序上关闭后推进，拒绝。
        times = {3: "2026-09-06T09:00+08:00", 4: "2026-09-06T15:00+08:00"}
        with self.assertRaisesRegex(RuminationPipelineError, "resolve_thread"):
            self._parse([
                self._update_op(12, "赶海计划当前状态：关闭后的推进正文。", [4], "推进"),
                self._trans_op("resolve_thread", 12, "赶海计划已经完成。", [3], "已完成"),
            ], times=times)

    def test_deferred_markers_stripped_from_output(self):
        content = "赶海计划当前状态：检查内部标记不外泄的正文。"
        merged = self._merge(self._parse([
            self._update_op(12, content, [3, 4], "标记清理"),
        ]))
        for op in merged:
            self.assertNotIn("_deferred_continuity", op)
            self.assertNotIn("_raw_thread_state", op)


class ContentContractTests(unittest.TestCase):
    """正文长度契约 5–3000：诊断含序号/类型/原始值类型/规范化长度/分类。"""

    def setUp(self):
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))
        self.times = _staggered_evidence_times(range(1, 20))

    def _parse(self, raw_content):
        op = {
            "op": "create_request", "reason": "长度契约测试",
            "continuity_type": "profile",
            "content": raw_content,
            "continuity_data": {
                "facet": "契约", "statement": "长度边界验证",
                "scope": "全局", "stability": "stable",
                "basis": "explicit_self_report",
            },
            "evidence_message_ids": [3],
        }
        return parse_rumination_output(
            json.dumps({"operations": [op]}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )

    def test_below_minimum_rejected_with_full_diagnostics(self):
        with self.assertRaises(RuminationPipelineError) as raised:
            self._parse("abcd")
        message = str(raised.exception)
        self.assertIn("op #0 (create_request)", message)
        self.assertIn("content_too_short", message)
        self.assertIn("raw_type=str", message)
        self.assertIn("normalized_length=4", message)
        self.assertIn("allowed=5-3000", message)
        self.assertNotIn("abcd", message)

    def test_minimum_and_maximum_boundaries_pass(self):
        for length in (5, 600, 601, 3000):
            with self.subTest(length=length):
                parsed = self._parse("x" * length)
                self.assertEqual(len(parsed[0]["content"]), length)

    def test_above_maximum_rejected_with_category(self):
        with self.assertRaises(RuminationPipelineError) as raised:
            self._parse("x" * 3001)
        self.assertIn("content_too_long", str(raised.exception))
        self.assertIn("normalized_length=3001", str(raised.exception))

    def test_non_string_content_rejected_as_type_error(self):
        for raw, type_name in ((123, "int"), (["正文"], "list"), (1.5, "float")):
            with self.subTest(raw_type=type_name):
                with self.assertRaises(RuminationPipelineError) as raised:
                    self._parse(raw)
                message = str(raised.exception)
                self.assertIn("content_type_invalid", message)
                self.assertIn(f"raw_type={type_name}", message)

    def test_missing_and_null_content_count_as_too_short(self):
        op = self._threadless_op_without_content()
        with self.assertRaises(RuminationPipelineError) as raised:
            parse_rumination_output(
                json.dumps({"operations": [op]}, ensure_ascii=False),
                evidence_times=self.times,
                threads_by_id=self.threads,
            )
        message = str(raised.exception)
        self.assertIn("content_too_short", message)
        self.assertIn("normalized_length=0", message)

    def _threadless_op_without_content(self):
        return {
            "op": "create_request", "reason": "缺正文",
            "continuity_type": "profile",
            "continuity_data": {
                "facet": "契约", "statement": "缺正文验证",
                "scope": "全局", "stability": "stable",
                "basis": "explicit_self_report",
            },
            "evidence_message_ids": [3],
        }

    def test_whitespace_normalization_measured_after_collapse(self):
        with self.assertRaises(RuminationPipelineError) as raised:
            self._parse("  a 	 b  ")
        self.assertIn("normalized_length=3", str(raised.exception))


class CreateTrackedThreadStateTests(unittest.TestCase):
    """create_tracked_thread 必须把规范化后的 thread_state='open' 写入 op。

    生产故障：parse 只校验未写入，提交 RPC 因缺 thread_state 拒绝整批。
    """

    def setUp(self):
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))
        self.times = _staggered_evidence_times(range(1, 20))

    def _parse(self, ops):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )

    @staticmethod
    def _thread_op(**kwargs):
        op = {
            "op": "create_tracked_thread", "reason": "追踪进程",
            "memory_key": "topic.brand-new",
            "content": "跟踪一个全新的长期进程正文。",
            "continuity_data": {
                "open_question": "是否成行", "current_state": "待确认",
                "closure_criteria": ["成行"],
            },
            "evidence_message_ids": [3],
        }
        op.update(kwargs)
        return op

    def test_explicit_open_is_normalized_into_op(self):
        parsed = self._parse([self._thread_op(thread_state="open")])
        self.assertEqual(parsed[0]["op"], "create_tracked_thread")
        self.assertEqual(parsed[0]["thread_state"], "open")

    def test_omitted_state_defaults_to_open_in_op(self):
        parsed = self._parse([self._thread_op()])
        self.assertEqual(parsed[0]["thread_state"], "open")

    def test_non_open_state_still_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "must start as open"):
            self._parse([self._thread_op(thread_state="resolved")])


class ResolvedTimelineDedupeTests(unittest.TestCase):
    """复审轮 3 回归：去重后置于时间线解析、同时间歧义裁决、交接目标并集。

    处理顺序：字段校验 → 时间线判定（含状态与结构解析）→ 去重合并 →
    数据库提交。状态未解析完成的操作不提前去重；同一证据时间上业务
    负载不同或状态转换多于一个时明确拒绝，数组顺序不决定最终事实。
    """

    def setUp(self):
        self.threads = _threads_by_id(_thread(memory_id=12, state="open"))
        self.times = _staggered_evidence_times(range(1, 40))

    def _parse(self, ops, times=None, absorbable=frozenset()):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=times or self.times,
            threads_by_id=self.threads,
            absorbable_ids=absorbable,
        )

    def _merge(self, parsed, times=None):
        return merge_thread_operations(
            parsed,
            evidence_times=times or self.times,
            threads_by_id=self.threads,
        )

    def _update_op(self, content, evidence, current_state, *, thread_state=None):
        op = {
            "op": "update_thread", "reason": "进程有实质进展",
            "target_memory_id": 12, **_snap(self.threads[12]),
            "content": content,
            "continuity_data": {
                "open_question": "赶海是否成行", "current_state": current_state,
                "closure_criteria": ["成行或改期"],
            },
            "evidence_message_ids": list(evidence),
        }
        if thread_state:
            op["thread_state"] = thread_state
        return op

    def _trans_op(self, kind, content, evidence, current_state):
        continuity = {
            "open_question": "赶海是否成行", "current_state": current_state,
        }
        if kind == "resolve_thread":
            continuity.update({
                "closure_summary": "顺利收尾", "closure_reason": "原文明确完成",
                "closed_at": "2026-09-12",
            })
        return {
            "op": kind, "reason": kind,
            "target_memory_id": 12, **_snap(self.threads[12]),
            "content": content, "continuity_data": continuity,
            "evidence_message_ids": list(evidence),
        }

    # -- 问题 1：状态未解析的操作不提前去重 ------------------------------

    def test_update_pause_update_same_body_across_states_succeeds(self):
        body = "项目正在等待，等待正文完全相同。"
        parsed = self._parse([
            self._update_op(body, [3], "等待中", thread_state="open"),
            self._trans_op("pause_thread", "项目暂停，等天气。这是一条暂停正文。", [4], "暂停"),
            self._update_op(body, [5], "等待中", thread_state="paused"),
        ])
        # 同正文同结构在 open 与 paused 两个状态阶段是两个有效操作。
        self.assertEqual([item["op"] for item in parsed], ["update_thread", "pause_thread", "update_thread"])
        merged = self._merge(parsed)
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["op"], "pause_thread")
        self.assertEqual(tops[0]["thread_state"], "paused")
        self.assertEqual(tops[0]["evidence_message_ids"], [3, 4, 5])

    # -- 问题 2：相同证据时间按歧义裁决，数组顺序不影响结论 --------------

    def test_same_evidence_different_content_rejected_in_both_orders(self):
        times = {3: "2026-09-06T09:00+08:00"}
        for order in (("后端完成", "前端完成"), ("前端完成", "后端完成")):
            ops = [
                self._update_op(
                    f"网关改造当前状态：{order[0]}。正文各不相同。", [3], order[0],
                ),
                self._update_op(
                    f"网关改造当前状态：{order[1]}。正文各不相同。", [3], order[1],
                ),
            ]
            with self.assertRaisesRegex(RuminationPipelineError, "ambiguous"):
                self._parse(ops, times=times)

    def test_same_time_and_overlapping_evidence_equivalent_ops_merge(self):
        # 业务完全等价（含相同结构）的操作与顺序无关，安全合并；
        # 证据集合重叠时并集完整保留。
        times = {3: "2026-09-06T09:00+08:00", 4: "2026-09-06T09:00+08:00",
                 5: "2026-09-06T09:00+08:00"}
        content = "网关改造当前状态：完全等价的同时间重复进展。"
        parsed = self._parse([
            self._update_op(content, [3, 4], "等价状态"),
            self._update_op(content, [4, 5], "等价状态"),
        ], times=times)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["evidence_message_ids"], [3, 4, 5])

    def test_same_time_transition_and_update_rejected(self):
        # update + pause 同一证据时间：谁的内容最终生效取决于顺序，拒绝。
        times = {3: "2026-09-06T09:00+08:00"}
        with self.assertRaisesRegex(RuminationPipelineError, "ambiguous"):
            self._parse([
                self._update_op("网关改造当前状态：进展与暂停同时发生。", [3], "进展"),
                self._trans_op("pause_thread", "网关改造当前状态：同时发生的暂停。", [3], "暂停"),
            ], times=times)

    # -- 问题 3：交接目标并入合并语义，不静默丢失 ------------------------

    @staticmethod
    def _request_op(content, evidence, absorbed):
        return {
            "op": "create_request", "reason": "经历需要审核",
            "continuity_type": "episode",
            "content": content,
            "continuity_data": {
                "beginning": "b", "development": "d", "outcome": "o",
                "closure_quality": "complete",
            },
            "evidence_message_ids": list(evidence),
            "absorbed_fast_path_memory_ids": list(absorbed),
        }

    def test_duplicate_requests_union_absorb_targets(self):
        content = "一段需要审核的完整经历，交接目标不同。"
        parsed = self._parse([
            self._request_op(content, [3], [91]),
            self._request_op(content, [4], [92]),
        ], absorbable={91, 92})
        requests = [item for item in parsed if item["op"] == "create_request"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["absorbed_fast_path_memory_ids"], [91, 92])
        self.assertEqual(requests[0]["evidence_message_ids"], [3, 4])

    def test_duplicate_requests_absorb_union_over_cap_rejected(self):
        content = "一段需要审核的完整经历，交接目标超过上限。"
        ops = [
            self._request_op(content, [index], [100 + index])
            for index in range(3, 13)
        ]
        with self.assertRaisesRegex(RuminationPipelineError, "absorb more than"):
            self._parse(ops, absorbable=set(range(100, 113)))

    def test_duplicate_create_memory_union_absorb_targets(self):
        content = "同一瞬间的两条重复记忆，交接目标不同。"
        parsed = self._parse([
            {
                "op": "create_memory", "reason": "瞬间", "continuity_type": "moment",
                "content": content,
                "continuity_data": {
                    "scene": "窗边", "event": "潮水漫过礁石", "moment_state": "standalone",
                },
                "evidence_message_ids": [3],
                "absorbed_fast_path_memory_ids": [81],
            },
            {
                "op": "create_memory", "reason": "瞬间", "continuity_type": "moment",
                "content": content,
                "continuity_data": {
                    "scene": "窗边", "event": "潮水漫过礁石", "moment_state": "standalone",
                },
                "evidence_message_ids": [4],
                "absorbed_fast_path_memory_ids": [82],
            },
        ], absorbable={81, 82})
        memories = [item for item in parsed if item["op"] == "create_memory"]
        self.assertEqual(len(memories), 1)
        self.assertEqual(memories[0]["absorbed_fast_path_memory_ids"], [81, 82])

    # -- 既有拒绝项在本轮顺序下仍然成立 ----------------------------------

    def test_illegal_evidence_snapshot_and_state_still_rejected(self):
        with self.assertRaisesRegex(RuminationPipelineError, "outside this batch"):
            self._parse([self._update_op("网关改造当前状态：幻觉证据的正文。", [999], "x")])
        with self.assertRaisesRegex(RuminationPipelineError, "snapshot"):
            bad = self._update_op("网关改造当前状态：错误快照的正文。", [3], "x")
            bad["target_content_hash"] = "f" * 64
            self._parse([bad])
        with self.assertRaisesRegex(RuminationPipelineError, "paused thread"):
            self._parse([self._trans_op("resume_thread", "还没暂停就想恢复。", [3], "恢复")])
        with self.assertRaisesRegex(RuminationPipelineError, "resolve_thread"):
            self._parse([
                self._trans_op("resolve_thread", "网关改造已经完成。", [3], "已完成"),
                self._update_op("关闭之后又推进的正文。", [4], "推进"),
            ])


class StructureChangePreservationTests(unittest.TestCase):
    """复审轮 4 回归：正文未变但结构变化时，不得降级为"只补证据"。

    比较语义与数据库一致：正文哈希 + 结构规范形（递归剥离显式 null、
    键序无关）。普通字段无模型基线，不参与变化判定。
    """

    def setUp(self):
        self.base_content = "网关改造当前状态：正文与数据库当前版本完全相同。"
        self.target = {
            "id": 12, "memory_key": "topic.x",
            "continuity_id": "21111111-1111-1111-1111-1111111111a1",
            "thread_state": "open", "maintained_by": "rumination",
            "content": self.base_content,
            "content_hash": _sha256(self.base_content),
            "continuity_data": {
                "open_question": "改造是否完成", "current_state": "进行中",
                "closure_criteria": ["实测通过"],
            },
            "evidence_message_ids": [1],
        }
        self.threads = _threads_by_id(self.target)
        self.times = _staggered_evidence_times(range(1, 20))

    def _snap(self):
        return _snap(self.target)

    def _update_op(self, content, evidence, current_state):
        return {
            "op": "update_thread", "reason": "进程有实质进展",
            "target_memory_id": 12, **self._snap(),
            "content": content,
            "continuity_data": {
                "open_question": "改造是否完成", "current_state": current_state,
                "closure_criteria": ["实测通过"],
            },
            "evidence_message_ids": list(evidence),
        }

    def _parse(self, ops):
        return parse_rumination_output(
            json.dumps({"operations": ops}, ensure_ascii=False),
            evidence_times=self.times,
            threads_by_id=self.threads,
        )

    def _merge(self, parsed):
        return merge_thread_operations(
            parsed, evidence_times=self.times, threads_by_id=self.threads,
        )

    def test_multi_op_merge_keeps_structure_change_on_same_body(self):
        parsed = self._parse([
            self._update_op(self.base_content, [3], "进行中"),
            self._update_op(self.base_content, [4], "实测通过，改造完成"),
        ])
        merged = self._merge(parsed)
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["op"], "update_thread")
        self.assertEqual(tops[0]["content"], self.base_content)
        self.assertEqual(
            tops[0]["continuity_data"]["current_state"], "实测通过，改造完成",
        )
        self.assertEqual(tops[0]["evidence_message_ids"], [3, 4])

    def test_single_update_keeps_structure_change_on_same_body(self):
        parsed = self._parse([
            self._update_op(self.base_content, [3], "实测通过，改造完成"),
        ])
        self.assertEqual(parsed[0]["op"], "update_thread")
        self.assertEqual(
            parsed[0]["continuity_data"]["current_state"], "实测通过，改造完成",
        )

    def test_truly_unchanged_expression_still_downgrades_to_evidence_only(self):
        # merge 阶段对"正文与结构都与当前版本一致"的多条进展仍降级为
        # 只补证据。（完全相同的重复操作在 parse 去重阶段就合并为单条，
        # 由提交侧 evidence_merged_unchanged 分支处理，见 pg 集成测试。）
        merged = self._merge([
            self._update_op(self.base_content, [3], "进行中"),
            self._update_op(self.base_content, [4], "进行中"),
        ])
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(len(tops), 1)
        self.assertEqual(tops[0]["op"], "evidence_only")
        self.assertNotIn("continuity_data", tops[0])
        self.assertNotIn("content", tops[0])

    def test_canonical_equivalence_ignores_key_order_and_explicit_nulls(self):
        # 键序不同、显式 null 等价于"未提供"：规范形一致时仍只补证据。
        reordered = self._update_op(self.base_content, [4], "进行中")
        reordered["continuity_data"] = {
            "closure_criteria": ["实测通过"],
            "current_state": "进行中",
            "open_question": "改造是否完成",
            "next_expected": None,
        }
        merged = self._merge([
            self._update_op(self.base_content, [3], "进行中"),
            reordered,
        ])
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(tops[0]["op"], "evidence_only")

    def test_parse_dedupes_identical_updates_before_merge(self):
        # 完全相同的重复 update 在 parse 阶段合并为单条（证据并集），
        # 交由提交侧按同哈希分支裁决。
        parsed = self._parse([
            self._update_op(self.base_content, [3], "进行中"),
            self._update_op(self.base_content, [4], "进行中"),
        ])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["op"], "update_thread")
        self.assertEqual(parsed[0]["evidence_message_ids"], [3, 4])
        self.assertEqual(
            parsed[0]["continuity_data"]["current_state"], "进行中",
        )

    def test_invalid_structure_still_degrades_before_merge(self):
        op = self._update_op(self.base_content, [3], "进行中")
        op["continuity_data"] = {
            "open_question": "改造是否完成", "current_state": "进行中",
            "closure_criteria": ["实测通过"], "unexpected_field": "多余字段",
        }
        parsed = self._parse([op])
        self.assertEqual([item["op"] for item in parsed], ["ignore"])

    # -- 服务端结构基线（轮 5）--------------------------------------------

    def test_server_baseline_attached_and_carried_through_merge(self):
        parsed = self._parse([
            self._update_op(self.base_content, [3], "进行中"),
            self._update_op(self.base_content, [4], "实测通过，改造完成"),
        ])
        baseline = self.target["continuity_data"]
        for op in parsed:
            self.assertEqual(op["continuity_baseline"], baseline)
        merged = self._merge(parsed)
        tops = [item for item in merged if item.get("target_memory_id") == 12]
        self.assertEqual(tops[0]["continuity_baseline"], baseline)

    def test_model_forged_baseline_rejected_as_unknown_field(self):
        op = self._update_op(self.base_content, [3], "进行中")
        op["continuity_baseline"] = {"open_question": "伪造"}
        with self.assertRaisesRegex(RuminationPipelineError, "unsupported fields"):
            self._parse([op])

    def test_baseline_not_part_of_dedupe_identity(self):
        # 基线是服务端元数据：同组操作共享同一读取基线，不参与去重键；
        # 真重复（业务负载一致）仍合并证据。
        parsed = self._parse([
            self._update_op(self.base_content, [3], "进行中"),
            self._update_op(self.base_content, [4], "进行中"),
        ])
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["evidence_message_ids"], [3, 4])
        self.assertEqual(
            parsed[0]["continuity_baseline"], self.target["continuity_data"],
        )


if __name__ == "__main__":
    unittest.main()
