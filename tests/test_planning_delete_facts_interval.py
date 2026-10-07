"""规划调整批次（2026-10-07 执行文档）验收测试。

四组行为：
* C——创建防重：已删除操作登记（tombstone）阻止旧请求复建；旧快照
  语义等价归一重放；同键不同内容 409。
* D——删除按执行事实分流：有完成 / 部分完成 / 中空阶段完成 / 提前完成
  事实者保留全部历史；从无事实者物理删除任务与实例；重复删除稳定；
  已删除不得恢复；事实门槛不因标签更正消失。
* A——实际耗时来源：用户开始 / 结束 / 补填 = 'user'；系统收口与提前完成
  合成 = 'system'；序列化携带来源供三层展示。
* R——after_completion 分钟间隔：d/h/m 解析、完成 / 拆分 / 编辑 / 生成
  全路径按分钟推进；旧 interval_days 天数等价兼容；fixed_interval 不变。
"""

import re
import unittest
from datetime import timedelta
from unittest import mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import (
    planning, planning_generation, planning_occurrences, planning_runtime,
    planning_tasks)
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from gateway.planning_common import (
    MAX_AFTER_COMPLETION_MINUTES, MIN_AFTER_COMPLETION_MINUTES,
    format_interval_shorthand, parse_interval_shorthand)
from tests.support.planning_context import Context, at

GATEWAY_TOKEN = "gateway-token-test"


def _task_row(c, task_id):
    return next(r for r in c.db.rows["planning_task"] if r["id"] == task_id)


def _facts(c):
    return c.db.rows.setdefault("planning_task_completion_fact", [])


def _tombstones(c):
    return c.db.rows.setdefault("planning_creation_request", [])


# ── C 组：创建防重与已删除登记 ─────────────────────────────────────


class CreationDeletedRegistryTests(unittest.TestCase):
    def test_physical_delete_registers_tombstone_and_blocks_old_key(self):
        # C06（物理删除分支）：删除后网络重发原创建请求 → 已删除结果、
        # 零复建；新键创建同内容正常。
        with Context() as c:
            created = planning.create_task({
                "content": "喵喵喵", "task_type": "daily",
                "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-1")
            task_id = created["id"]
            result = planning.update_task(task_id, {"is_active": False}, at(24, 9))
            self.assertTrue(result["deleted"])
            self.assertFalse(result["history_preserved"])
            self.assertEqual(c.db.rows["planning_task"], [])
            self.assertEqual([t["request_key"] for t in _tombstones(c)], ["op-1"])
            # 旧键重发：已删除结果，不复建
            replay = planning.create_task({
                "content": "喵喵喵", "task_type": "daily",
                "estimated_minutes": 30,
            }, at(24, 10), idempotency_key="op-1")
            self.assertTrue(replay["creation_request_deleted"])
            self.assertTrue(replay["idempotent_replay"])
            self.assertEqual(c.db.rows["planning_task"], [])
            # 新键：正常创建同内容
            fresh = planning.create_task({
                "content": "喵喵喵", "task_type": "daily",
                "estimated_minutes": 30,
            }, at(24, 11), idempotency_key="op-2")
            self.assertNotIn("creation_request_deleted", fresh)
            self.assertEqual(len(c.db.rows["planning_task"]), 1)

    def test_history_delete_replay_returns_deleted_result(self):
        # C06（历史保留分支）：任务行保留为归档载体（deleted_at），同键
        # 重放按已删除收敛；新键正常创建。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 8, 10))
            planning.update_task(created["id"], {"is_active": False}, at(24, 9))
            row = _task_row(c, created["id"])
            self.assertTrue(row["deleted_at"])
            self.assertFalse(row["is_active"])
            replay = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 10), idempotency_key=row["creation_request_key"])
            self.assertTrue(replay["creation_request_deleted"])
            fresh = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 11), idempotency_key="op-new")
            self.assertNotIn("creation_request_deleted", fresh)

    def test_tombstone_same_key_different_content_conflicts(self):
        # 同键不同内容（已删除登记）：409，不悄悄覆盖。
        with Context() as c:
            planning.create_task({
                "content": "A", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-1")
            planning.update_task(
                next(r["id"] for r in c.db.rows["planning_task"]), {"is_active": False},
                at(24, 9))
            with pytest.raises(planning.PlanningError) as error:
                planning.create_task({
                    "content": "B", "task_type": "daily", "estimated_minutes": 30,
                }, at(24, 10), idempotency_key="op-1")
            assert error.value.status_code == 409
            assert error.value.code == "request_conflict"

    def test_live_same_key_different_content_conflicts(self):
        with Context() as c:
            planning.create_task({
                "content": "A", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-1")
            with pytest.raises(planning.PlanningError) as error:
                planning.create_task({
                    "content": "A2", "task_type": "daily", "estimated_minutes": 30,
                }, at(24, 9), idempotency_key="op-1")
            assert error.value.status_code == 409

    def test_legacy_snapshot_replays_with_semantic_equivalence(self):
        # C08 / C09（#9 旧快照兼容）：旧版快照缺 refresh_enabled、
        # refresh_mode=null、以 interval_days 天数承载间隔——同语义重放
        # 成功；`1` 与 `1d` 等价；真实差异（显式 false）拒绝。
        with Context() as c:
            planning.create_task({
                "content": "X", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion", "interval_days": 3,
            }, at(24, 8), idempotency_key="op-1")
            row = _task_row(c, next(r["id"] for r in c.db.rows["planning_task"]))
            # 改写为旧版快照形状（缺省字段未存、间隔以天数承载）。
            row["creation_request_content"] = {
                "content": "X", "task_type": "interval", "time_mode": "duration",
                "estimated_minutes": 30, "window_start_tod": None,
                "window_end_tod": None, "interval_days": 3, "weekdays": None,
                "month_days": None, "target_date": None,
                "refresh_mode": "after_completion", "refresh_anchor_at": None,
                "is_hollow": False, "hollow_start_content": None,
                "hollow_start_minutes": None, "hollow_wait_minutes": None,
                "hollow_wait_note": None, "hollow_end_content": None,
                "hollow_end_minutes": None, "alarm_start": False,
                "alarm_end": False, "timer_minutes": None, "is_active": True,
            }
            # 同语义重放（缺省 refresh_enabled ≙ true；3d ≙ 旧 3 天 ≙ 4320m）。
            replay = planning.create_task({
                "content": "X", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion",
                "after_completion_interval": "3d",
            }, at(24, 9), idempotency_key="op-1")
            self.assertTrue(replay["idempotent_replay"])
            # 纯数字天数与 d 后缀等价。
            replay2 = planning.create_task({
                "content": "X", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion",
                "after_completion_interval": "3",
            }, at(24, 9, 30), idempotency_key="op-1")
            self.assertTrue(replay2["idempotent_replay"])
            # 真实差异（间隔变化）：409。
            with pytest.raises(planning.PlanningError) as error:
                planning.create_task({
                    "content": "X", "task_type": "interval", "estimated_minutes": 30,
                    "refresh_mode": "after_completion",
                    "after_completion_interval": "2h",
                }, at(24, 10), idempotency_key="op-1")
            assert error.value.status_code == 409

    def test_new_snapshot_one_day_and_one_d_equivalent(self):
        with Context() as c:
            planning.create_task({
                "content": "Y", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion",
                "after_completion_interval": "1",
            }, at(24, 8), idempotency_key="op-1")
            replay = planning.create_task({
                "content": "Y", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion",
                "after_completion_interval": "1d",
            }, at(24, 9), idempotency_key="op-1")
            self.assertTrue(replay["idempotent_replay"])

    def test_history_deleted_same_key_different_content_conflicts(self):
        # R14（2026-10-07 复审 #14）：有历史的已删除任务（deleted_at 非空、
        # 任务行留存为归档载体）同键不同内容 → 409——先做原创建内容语义
        # 比较，再对同内容返回已删除结果（与物理删除登记分支同一收敛顺序，
        # 旧实现该分支直接返回已删除结果、跳过内容核对）。
        with Context() as c:
            created = planning.create_task({
                "content": "A", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            planning.update_task(created["id"], {"is_active": False}, at(24, 10))
            row = _task_row(c, created["id"])
            self.assertTrue(row["deleted_at"])
            # 同键同内容 → 已删除结果
            replay = planning.create_task({
                "content": "A", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 11), idempotency_key="op-hist")
            self.assertTrue(replay["creation_request_deleted"])
            # 同键不同内容 → 409（旧实现直接返回已删除结果）
            with pytest.raises(planning.PlanningError) as error:
                planning.create_task({
                    "content": "B", "task_type": "daily", "estimated_minutes": 30,
                }, at(24, 12), idempotency_key="op-hist")
            assert error.value.status_code == 409
            assert error.value.code == "request_conflict"

    def test_legacy_snapshot_all_types_replay_after_upgrade(self):
        # R05（低优先级修复，2026-10-07 复审 #5）：旧版快照缺
        # after_completion_minutes 键——投影到当前字段集合后与新格式同形，
        # daily / once / fixed_interval 原键原内容重放不再误报 409；升级后
        # 编辑任务再重放仍返回同任务（不能用任务当前定义还原旧请求）。
        for payload in (
            {"content": "D", "task_type": "daily", "estimated_minutes": 30},
            {"content": "O", "task_type": "once", "estimated_minutes": 30,
             "target_date": "2026-09-25"},
            {"content": "F", "task_type": "interval", "estimated_minutes": 30,
             "refresh_mode": "fixed_interval", "interval_days": 3},
        ):
            with self.subTest(task_type=payload["task_type"]):
                with Context() as c:
                    created = planning.create_task(
                        dict(payload), at(24, 8), idempotency_key="op-1")
                    row = _task_row(c, created["id"])
                    # 改写为旧版快照形状（缺 after_completion_minutes 键）。
                    stored = dict(row["creation_request_content"])
                    stored.pop("after_completion_minutes", None)
                    row["creation_request_content"] = stored
                    replay = planning.create_task(
                        dict(payload), at(24, 9), idempotency_key="op-1")
                    self.assertTrue(replay["idempotent_replay"])
                    self.assertEqual(replay["id"], created["id"])
                    # 升级后编辑任务，再原样重放：仍按旧请求内容命中
                    #（不能用任务当前定义还原旧请求）。
                    planning.update_task(created["id"], {"content": "已改名"}, at(24, 9, 30))
                    replay2 = planning.create_task(
                        dict(payload), at(24, 9, 40), idempotency_key="op-1")
                    self.assertTrue(replay2["idempotent_replay"])
                    # 真实差异仍拒绝
                    changed = dict(payload)
                    changed["content"] = payload["content"] + "!"
                    with pytest.raises(planning.PlanningError) as error:
                        planning.create_task(changed, at(24, 10), idempotency_key="op-1")
                    assert error.value.status_code == 409

    def test_tombstone_keeps_only_digest_identity(self):
        # R09（2026-10-07 复审 #9）：物理删除后登记表只保留请求键 + 语义
        # 摘要 + task_id + 删除时刻——业务正文不落登记表（随任务行真正
        # 删除，不是移到别表永久保留）；摘要非空且可复算。
        from gateway import planning_tasks
        with Context() as c:
            planning.create_task({
                "content": "喵喵喵", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-1")
            task_row = _task_row(c, next(r["id"] for r in c.db.rows["planning_task"]))
            expected_digest = planning_tasks._creation_content_digest(
                task_row["creation_request_content"])
            planning.update_task(task_row["id"], {"is_active": False}, at(24, 9))
            tombstones = _tombstones(c)
            self.assertEqual(len(tombstones), 1)
            tomb = tombstones[0]
            self.assertEqual(tomb["request_key"], "op-1")
            self.assertNotIn("content", tomb)
            self.assertEqual(tomb["content_digest"], expected_digest)
            self.assertTrue(tomb["content_digest"])
            # 完成事实门槛行同事务清理（无外键设计）
            self.assertEqual(_facts(c), [])


# ── D 组：删除按执行事实分流 ───────────────────────────────────────


class DeleteByFactTests(unittest.TestCase):
    def test_delete_without_facts_physical_across_open_statuses(self):
        # D01：pending / in_progress（仅开始）/ deferred / timeout /
        # discarded_this 都不构成保留依据——物理删除，页面无残留。
        for status, setup in (
            ("pending", None),
            ("in_progress", lambda c, occ: planning.start_occurrence(occ["id"], at(24, 9))),
            ("deferred", lambda c, occ: planning.set_occurrence_status(
                occ["id"], {"status": "deferred", "est_start": planning._iso(at(25, 9))}, at(24, 9))),
            ("timeout", lambda c, occ: occ.update({"status": "timeout", "closed_at": planning._iso(at(24, 9))})),
        ):
            with self.subTest(status=status):
                with Context() as c:
                    created = c.create("daily", at(24, 8))
                    planning.generate_due(at(24, 8, 5))
                    occ = c.rows[0]
                    if setup:
                        setup(c, occ)
                    self.assertEqual(_facts(c), [])
                    result = planning.update_task(
                        created["id"], {"is_active": False}, at(24, 10))
                    self.assertTrue(result["deleted"])
                    self.assertFalse(result["history_preserved"])
                    self.assertEqual(c.db.rows["planning_task"], [])
                    self.assertEqual(c.db.rows["planning_occurrence"], [])
                    # 次日不再生成
                    self.assertEqual(planning.generate_due(at(25, 7))["created"], 0)

    def test_delete_after_completion_fact_preserves_history(self):
        # D02：任一历史轮 completed → 保留全部历史、停止生成、原完成
        # 内容与时间不改。
        with Context() as c:
            created = c.create("daily", at(23, 8))
            planning.generate_due(at(24, 6, 5))
            done = next(r for r in c.rows if r["schedule_date"] == "2026-09-24")
            planning.set_occurrence_status(done["id"], {"status": "completed"}, at(24, 9))
            handled = done["handled_at"]
            planning.generate_due(at(25, 6, 5))
            current = c.rows[-1]
            self.assertEqual(current["status"], "pending")
            result = planning.update_task(created["id"], {"is_active": False}, at(25, 10))
            self.assertTrue(result["deleted"])
            self.assertTrue(result["history_preserved"])
            row = _task_row(c, created["id"])
            self.assertFalse(row["is_active"])
            self.assertTrue(row["deleted_at"])
            # 完成历史原样保留；开放轮按删除语义收口
            self.assertEqual(done["status"], "completed")
            self.assertEqual(done["handled_at"], handled)
            self.assertEqual(current["status"], "discarded")
            # 停止生成
            self.assertEqual(planning.generate_due(at(26, 6, 5))["created"], 0)
            # 重复删除：稳定结果
            again = planning.update_task(created["id"], {"is_active": False}, at(25, 11))
            self.assertTrue(again["deleted"])
            self.assertTrue(again["history_preserved"])

    def test_partial_fact_preserves_even_after_label_correction(self):
        # D03：partial_at 事实触发保留；状态标签后续更正（timeout）后
        # 事实门槛仍在（触发器登记不随更正消失）。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(
                occ["id"], {"status": "partial", "partial_note": "做了一半"}, at(24, 9))
            self.assertTrue(any(f["task_id"] == created["id"] for f in _facts(c)))
            # 标签更正为 timeout（模拟后续超时收场）
            occ.update({"status": "timeout", "closed_at": planning._iso(at(24, 12))})
            result = planning.update_task(created["id"], {"is_active": False}, at(25, 8))
            self.assertTrue(result["history_preserved"])
            self.assertEqual(occ["partial_at"], planning._iso(at(24, 9)))

    def test_early_completion_fact_preserves(self):
        # D04：有效提前完成记录 → 保留全部历史。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="1d")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            # 下一轮 1 天后才到期：此刻无开放轮 → 提前完成产生额外记录。
            planning.complete_task_early(created["id"], at(24, 10), idempotency_key="early-1")
            early = next(r for r in c.rows if r["source"] == "early")
            self.assertEqual(early["status"], "completed")
            result = planning.update_task(created["id"], {"is_active": False}, at(24, 11))
            self.assertTrue(result["history_preserved"])
            self.assertEqual(len(c.rows), 2)

    def test_hollow_start_completed_preserves_whole_round(self):
        # D05：中空 start completed、end pending → 两阶段及全部历史保留。
        with Context() as c:
            created = c.create("daily", at(24, 8), estimated_minutes=30,
                               is_hollow=True, hollow_start_minutes=10,
                               hollow_wait_minutes=30, hollow_end_minutes=10)
            planning.generate_due(at(24, 8, 5))
            start = next(r for r in c.rows if r["phase"] == "start")
            end = next(r for r in c.rows if r["phase"] == "end")
            planning.set_occurrence_status(start["id"], {"status": "completed"}, at(24, 9))
            result = planning.update_task(created["id"], {"is_active": False}, at(24, 10))
            self.assertTrue(result["history_preserved"])
            self.assertEqual(start["status"], "completed")
            self.assertEqual(end["status"], "discarded")

    def test_deleted_task_cannot_be_restored(self):
        # D09：已删除任务不得经旧 is_active=true 恢复。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            planning.update_task(created["id"], {"is_active": False}, at(24, 10))
            with pytest.raises(planning.PlanningError) as error:
                planning.update_task(created["id"], {"is_active": True}, at(24, 11))
            assert error.value.status_code == 409
            assert "已删除" in str(error.value)

    def test_occurrence_entry_delete_matches_task_entry(self):
        # 两个旧删除入口统一进入同一事务规则：实例入口 status='discarded'
        # 与任务入口 PATCH is_active=false 结果一致。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            result = planning.set_occurrence_status(
                c.rows[0]["id"], {"status": "discarded"}, at(24, 9))
            self.assertTrue(result["deleted"])
            self.assertFalse(result["history_preserved"])
            self.assertEqual(c.db.rows["planning_task"], [])

    def test_completion_fact_registered_on_status_write(self):
        # 触发器语义：completed / partial 写入的同一语句登记事实门槛。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            self.assertEqual(_facts(c), [])
            planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 9))
            self.assertEqual([f["task_id"] for f in _facts(c)], [created["id"]])
            # 幂等：重复完成更正不重复登记
            planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 10))
            self.assertEqual(len(_facts(c)), 1)


# ── A 组：实际耗时来源 ─────────────────────────────────────────────


class ActualTimeSourceTests(unittest.TestCase):
    def test_start_and_finish_mark_user_source(self):
        # A01：10:00 开始、10:12 结束 → 自动实际 12m，来源 user。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10))
            self.assertEqual(occ["actual_time_source"], "user")
            planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 10, 12))
            self.assertEqual(occ["actual_time_source"], "user")
            self.assertEqual(occ["actual_minutes"], 12)
            serialized = planning.get_occurrence(occ["id"], at(24, 13))
            self.assertEqual(serialized["actual_time_source"], "user")
            self.assertEqual(serialized["actual_minutes"], 12)

    def test_manual_logged_duration_keeps_auto_facts(self):
        # A02：手填 8m 存独立字段；自动起止 / 12m 不被覆盖。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10))
            planning.finish_occurrence(
                occ["id"], {"actual_logged_duration": "8m"}, at(24, 10, 12))
            self.assertEqual(occ["actual_logged_seconds"], 480)
            self.assertEqual(occ["actual_minutes"], 12)
            self.assertEqual(occ["actual_time_source"], "user")

    def test_complete_without_start_has_no_auto_pair(self):
        # A03：没点开始直接完成 → 只有结束事实，无起止对，不构成自动实际。
        with Context() as c:
            c.create("daily", at(24, 8))
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 9))
            self.assertIsNone(occ.get("actual_start"))
            self.assertEqual(occ.get("actual_time_source"), "user")
            self.assertIsNone(occ.get("actual_minutes"))

    def test_early_completion_synthesizes_system_source(self):
        # A05：提前完成合成同刻起止 → 来源 system，不冒充用户计时。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="1d")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            # 下一轮 1 天后才到期：此刻无开放轮 → 提前完成合成同刻起止行。
            planning.complete_task_early(created["id"], at(24, 10), idempotency_key="early-1")
            occ = next(r for r in c.rows if r["source"] == "early")
            self.assertEqual(occ.get("actual_time_source"), "system")
            self.assertEqual(occ.get("actual_minutes"), 0)

    def test_discard_closure_marks_system_source(self):
        # A05：删除收口 in_progress 的结束时间 → system。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 9))
            _facts(c).append({"task_id": created["id"]})
            planning.update_task(created["id"], {"is_active": False}, at(24, 11))
            self.assertEqual(occ["actual_time_source"], "system")
            self.assertEqual(occ["status"], "discarded")

    def test_backfill_patch_marks_user_and_clearing_recomputes(self):
        # A04：补填完整起止 → user；清除一端 → 分钟回 None。
        with Context() as c:
            created = planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.patch_occurrence(occ["id"], {
                "actual_start": planning._iso(at(24, 10)),
                "actual_end": planning._iso(at(24, 10, 25)),
            }, at(24, 12))
            self.assertEqual(occ.get("actual_time_source"), "user")
            self.assertEqual(occ.get("actual_minutes"), 25)
            planning.patch_occurrence(occ["id"], {"actual_end": None}, at(24, 13))
            self.assertIsNone(occ.get("actual_minutes"))
            self.assertIsNone(occ.get("actual_end"))

    def test_single_endpoint_patch_keeps_system_source(self):
        # R07（低优先级修复，2026-10-07 复审 #7）：08:00 开始、08:12 此次
        # 不执行由系统补 end（source=system）；只改 start 为 08:01 → 混合
        # 配对不整体提升为 user（旧实现单端补丁把整对端点改标 user，展示
        # 冒充「实际耗时 11m」）。双端同请求补填仍为 user。
        with Context() as c:
            planning.create_task({
                "content": "daily", "task_type": "daily", "estimated_minutes": 30,
            }, at(24, 8), idempotency_key="op-hist")
            planning.generate_due(at(24, 8, 5))
            occ = c.rows[0]
            planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 8))
            planning.set_occurrence_status(
                occ["id"], {"status": "discarded_this"}, at(24, 8, 12))
            self.assertEqual(occ["actual_time_source"], "system")
            self.assertEqual(occ["actual_start"], planning._iso(at(24, 8)))
            self.assertEqual(occ["actual_end"], planning._iso(at(24, 8, 12)))
            # 只改一端：保留 system 来源（混合配对不冒充实测）
            planning.patch_occurrence(
                occ["id"], {"actual_start": planning._iso(at(24, 8, 1))}, at(24, 9))
            self.assertEqual(occ["actual_start"], planning._iso(at(24, 8, 1)))
            self.assertEqual(occ["actual_time_source"], "system")
            # 双端同请求补填 → 完整用户配对 → user
            planning.patch_occurrence(occ["id"], {
                "actual_start": planning._iso(at(24, 8, 1)),
                "actual_end": planning._iso(at(24, 8, 12)),
            }, at(24, 9, 30))
            self.assertEqual(occ["actual_time_source"], "user")


# ── R 组：after_completion 分钟间隔 ─────────────────────────────────


class IntervalMinutesTests(unittest.TestCase):
    def test_shorthand_parsing_and_roundtrip(self):
        # R01 / R02：解析与回显互逆；非法输入拒绝。
        cases = {
            "1": 1440, "1d": 1440, "2h": 120, "30m": 30,
            "1d1h1m": 1501, "1h30m": 90, " 1D1H1M ": 1501, "365d": 525600,
        }
        for raw, minutes in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_interval_shorthand(raw), minutes)
                # 回显 → 再解析互逆（规范简写是稳定不动点）。
                self.assertEqual(
                    parse_interval_shorthand(format_interval_shorthand(minutes)), minutes)
                if minutes % 1440 == 0:
                    # 整数（天）入口与文本入口等价。
                    self.assertEqual(parse_interval_shorthand(minutes // 1440), minutes)
        for bad in ("", "0", "0m", "-1", "1.5h", "1m1d", "1d1d", "1s", "1w",
                    "365d1m", "525601m", None, True, 0, -5, 1.5):
            with self.subTest(bad=bad):
                with pytest.raises(planning.PlanningError):
                    parse_interval_shorthand(bad)
        self.assertEqual(format_interval_shorthand(1440), "1d")
        self.assertEqual(format_interval_shorthand(1501), "1d1h1m")
        self.assertEqual(format_interval_shorthand(30), "30m")
        self.assertEqual(format_interval_shorthand(None), "")

    def test_create_stores_minutes_and_nulls_days(self):
        # R01：创建解析为分钟；interval_days 在 after_completion 行为空。
        with Context() as c:
            created = planning.create_task({
                "content": "R", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion",
                "after_completion_interval": "1d1h1m",
            }, at(24, 8))
            row = _task_row(c, created["id"])
            self.assertEqual(row["after_completion_minutes"], 1501)
            self.assertIsNone(row["interval_days"])
            self.assertEqual(created["after_completion_minutes"], 1501)
            # 旧调用以天数表达：等价换算。
            legacy = planning.create_task({
                "content": "R2", "task_type": "interval", "estimated_minutes": 30,
                "refresh_mode": "after_completion", "interval_days": 3,
            }, at(24, 9))
            self.assertEqual(_task_row(c, legacy["id"])["after_completion_minutes"], 4320)
            self.assertIsNone(_task_row(c, legacy["id"])["interval_days"])

    def test_completion_advances_due_by_exact_minutes(self):
        # R03 / R07：完成 + 30m → 到期 = 处理时刻 + 30 分钟（不取整到天）；
        # 同一周期内多个短间隔轮次各自独立。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="30m")
            planning.generate_due(at(24, 8, 5))
            first = c.rows[0]
            planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 9))
            row = _task_row(c, created["id"])
            self.assertEqual(row["refresh_next_due_at"], planning._iso(at(24, 9, 30)))
            # 30 分钟后生成下一轮（round_key 内嵌精确 due，身份独立）
            self.assertEqual(planning.generate_due(at(24, 9, 31))["created"], 1)
            second = c.rows[-1]
            self.assertNotEqual(second["round_key"], first["round_key"])
            planning.set_occurrence_status(second["id"], {"status": "completed"}, at(24, 10))
            self.assertEqual(
                _task_row(c, created["id"])["refresh_next_due_at"],
                planning._iso(at(24, 10, 30)))
            self.assertEqual(planning.generate_due(at(24, 10, 31))["created"], 1)
            self.assertEqual(len({r["round_key"] for r in c.rows}), 3)

    def test_partial_does_not_advance(self):
        # R04：部分完成不启动新间隔。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="30m")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(
                c.rows[0]["id"], {"status": "partial", "partial_note": "一半"}, at(24, 9))
            self.assertIsNone(_task_row(c, created["id"])["refresh_next_due_at"])
            self.assertEqual(planning.generate_due(at(25, 9))["created"], 0)

    def test_split_advances_due_by_minutes(self):
        # R05：拆分以分钟推进 after_completion 基准。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="90m")
            planning.generate_due(at(24, 8, 5))
            planning.split_occurrence(c.rows[0]["id"], {
                "parts": [{"content": "剩余", "estimated_minutes": 20}],
            }, at(24, 9))
            row = _task_row(c, created["id"])
            self.assertEqual(row["refresh_next_due_at"], planning._iso(at(24, 10, 30)))

    def test_edit_interval_recomputes_due_from_handled(self):
        # R06：编辑间隔后基于既有合法处理基准重算下一到期。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="1d")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            planning.update_task(created["id"], {
                "after_completion_interval": "2h"}, at(24, 10))
            row = _task_row(c, created["id"])
            self.assertEqual(row["after_completion_minutes"], 120)
            self.assertEqual(row["refresh_next_due_at"], planning._iso(at(24, 11)))
            self.assertEqual(row["last_handled_at"], planning._iso(at(24, 9)))

    def test_legacy_days_edit_updates_minutes_and_due(self):
        # R06（2026-10-07 复审 #6）：旧天数字段修改不再被现有分钟值吞掉——
        # PATCH interval_days:3 在 after_completion_minutes=1440 的任务上
        # 真正换算为 4320（旧实现先合并旧任务再取分钟，返回成功却仍是
        # 1440、interval_days 被置空）；下一次 due 按新间隔从既有处理基准
        # 重算。两键同请求且不等价 → 明确拒绝。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="1d")
            planning.generate_due(at(24, 8, 5))
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            row = _task_row(c, created["id"])
            self.assertEqual(row["after_completion_minutes"], 1440)
            planning.update_task(created["id"], {"interval_days": 3}, at(24, 10))
            row = _task_row(c, created["id"])
            self.assertEqual(row["after_completion_minutes"], 4320)
            self.assertIsNone(row["interval_days"])
            self.assertEqual(row["refresh_next_due_at"], planning._iso(at(27, 9)))
            self.assertEqual(row["last_handled_at"], planning._iso(at(24, 9)))
            # 冲突输入：两键同请求且不等价 → 400，零写入
            with pytest.raises(planning.PlanningError) as error:
                planning.update_task(created["id"], {
                    "after_completion_minutes": 60, "interval_days": 2}, at(24, 11))
            assert error.value.status_code == 400
            self.assertEqual(_task_row(c, created["id"])["after_completion_minutes"], 4320)
            # 等价双写（3d ≙ 4320m）放行
            planning.update_task(created["id"], {
                "after_completion_minutes": 4320, "interval_days": 3}, at(24, 12))
            self.assertEqual(_task_row(c, created["id"])["after_completion_minutes"], 4320)

    def test_legacy_days_row_reads_equivalent_minutes(self):
        # R08：迁移回填前的旧任务行（interval_days=3、无分钟）按等价分钟
        # 读取：due = handled + 3 天；提前完成可用。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="after_completion",
                               after_completion_interval="1d")
            row = _task_row(c, created["id"])
            row["after_completion_minutes"] = None
            row["interval_days"] = 3
            planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 9))
            self.assertEqual(row["refresh_next_due_at"], planning._iso(at(27, 9)))
            due = planning_generation._after_completion_due(c.db, row)
            self.assertEqual(due, at(27, 9))

    def test_fixed_interval_keeps_days_axis(self):
        # R08：固定间隔模式仍使用 interval_days 天数轴。
        with Context() as c:
            created = c.create("interval", at(24, 8), refresh_mode="fixed_interval",
                               interval_days=3)
            row = _task_row(c, created["id"])
            self.assertEqual(row["interval_days"], 3)
            self.assertIsNone(row.get("after_completion_minutes"))
            planning.generate_due(at(24, 8, 5))
            self.assertTrue(c.rows[0]["round_key"].startswith("fixed:2026-09-24"))


# ── C11：中文错误与 _dispatch 分类 ──────────────────────────────────


class DispatchChineseErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(planning_api_routes))

    def setUp(self):
        self.client = None

    def _http(self):
        return TestClient(self.app, raise_server_exceptions=False)

    def test_unknown_exception_returns_chinese_internal_error(self):
        # C11：未知异常 → 中文提示 + 稳定错误码；不泄漏异常类名。
        with mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN), \
                mock.patch.object(
                    planning, "today_board",
                    side_effect=RuntimeError("APIError: secret detail")):
            http = self._http()
            response = http.get(
                "/admin/api/planning/today",
                headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"})
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error_code"], "internal_error")
        self.assertEqual(body["error"], "操作失败：服务暂时出现异常，请稍后重试")
        self.assertNotIn("APIError", body["error"])
        self.assertNotIn("secret", body["error"])

    def test_schema_mismatch_returns_upgrade_hint(self):
        # C11 / C01：可可靠分类的缺列故障 → 「数据库尚未完成升级」。
        for message in (
            'column planning_task.creation_request_key does not exist',
            "Could not find the 'creation_request_key' column",
        ):
            with self.subTest(message=message):
                with mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN), \
                        mock.patch.object(
                            planning, "today_board",
                            side_effect=RuntimeError(message)):
                    http = self._http()
                    response = http.get(
                        "/admin/api/planning/today",
                        headers={"Authorization": f"Bearer {GATEWAY_TOKEN}"})
                self.assertEqual(response.status_code, 500)
                body = response.json()
                self.assertEqual(body["error_code"], "schema_not_migrated")
                self.assertEqual(body["error"], "操作失败：数据库尚未完成升级，请联系管理员")

    def test_invalid_json_returns_chinese_message(self):
        with mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN):
            http = self._http()
            response = http.post(
                "/admin/api/planning/tasks",
                headers={"Authorization": f"Bearer {GATEWAY_TOKEN}",
                         "Content-Type": "application/json"},
                content=b"{not json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "请求内容必须是合法的 JSON")

    def test_optional_body_and_auth_errors_are_chinese(self):
        # R13（低优先级修复，2026-10-07 复审 #13）：可选 body 端点（/finish）
        # 的非法 JSON 与两个独立鉴权分支（complete-early / reschedule-
        # timeout）与 _dispatch 同口径中文提示；HTTP 状态与稳定错误码不变，
        # 无 body 的 finish 原可用契约保持。
        with mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN):
            http = self._http()
            response = http.post(
                "/admin/api/planning/occurrences/1/finish",
                headers={"Authorization": f"Bearer {GATEWAY_TOKEN}",
                         "Content-Type": "application/json"},
                content=b"{not json")
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error"], "请求内容必须是合法的 JSON")
        self.assertEqual(body["error_code"], "invalid_json")
        # 独立鉴权分支：中文 + unauthorized 错误码
        for path in ("/admin/api/planning/tasks/1/complete-early",
                     "/admin/api/planning/occurrences/1/reschedule-timeout"):
            with self.subTest(path=path):
                with mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN):
                    http = self._http()
                    response = http.post(path)
                self.assertEqual(response.status_code, 401)
                body = response.json()
                self.assertEqual(body["error"], "未登录或令牌无效")
                self.assertEqual(body["error_code"], "unauthorized")


# ── 前端：三层耗时展示与间隔格式化（quickjs 行为级） ────────────────

DISPLAY_SOURCE = None
try:
    from pathlib import Path
    DISPLAY_SOURCE = (
        Path(__file__).resolve().parents[1]
        / "admin" / "js" / "lib" / "planning_display.js"
    ).read_text(encoding="utf-8")
except OSError:
    pass


def _try_import_quickjs():
    try:
        import quickjs  # noqa: F401
    except ImportError:
        return None
    return quickjs


@unittest.skipIf(_try_import_quickjs() is None or DISPLAY_SOURCE is None,
                 "quickjs 或前端源码不可用")
class FrontendDurationDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        quickjs = _try_import_quickjs()
        ctx = quickjs.Context()
        # planning_display.js 只依赖 ui.js 的 tag/icon/esc——mock 后整模块
        # 可在裸环境执行。
        ctx.eval("""
            globalThis.tag = (label) => '<tag>' + label + '</tag>';
            globalThis.icon = (name) => '<i>' + name + '</i>';
            globalThis.esc = (v) => String(v == null ? '' : v);
        """)
        source = re.sub(r"import\s[^;]*?;", "", DISPLAY_SOURCE, flags=re.S)
        source = re.sub(r"\bexport\s+(?=(async\s+)?(function|const|let|class|var)\b)",
                        "", source)
        ctx.eval(source)
        # quickjs 不能直接转换 Python dict：经 JSON 桥接调用纯函数。
        ctx.eval("""
            globalThis.__durationText = (json) => durationText(JSON.parse(json));
            globalThis.__durationDetailRows = (json) => durationDetailRows(JSON.parse(json));
            globalThis.__summary = (json) => taskTypeSummary(JSON.parse(json));
        """)
        cls.ctx = ctx

    def test_closed_duration_three_tier_priority(self):
        # A 组前端口径：手填 → user 起止自动 → 预估；system / 来源不明
        # 不冒充实测；0 分钟是有效实际值。
        import json as _json
        duration = lambda occ: self.ctx.eval("__durationText")(_json.dumps(occ))
        rows = lambda occ: self.ctx.eval("__durationDetailRows")(_json.dumps(occ))
        cases = [
            ({"status": "completed", "actual_logged_seconds": 480,
              "estimated_minutes": 30}, "实际耗时 8m"),
            ({"status": "completed", "actual_logged_seconds": None,
              "actual_time_source": "user", "actual_start": "a", "actual_end": "b",
              "actual_minutes": 12, "estimated_minutes": 30}, "实际耗时 12m"),
            ({"status": "completed", "actual_logged_seconds": None,
              "actual_time_source": "user", "actual_start": "a", "actual_end": "b",
              "actual_minutes": 0, "estimated_minutes": 30}, "实际耗时 0m"),
            ({"status": "completed", "actual_logged_seconds": None,
              "actual_time_source": "system", "actual_start": "a", "actual_end": "b",
              "actual_minutes": 0, "estimated_minutes": 30}, "预估耗时 30m"),
            ({"status": "completed", "actual_logged_seconds": None,
              "actual_time_source": None, "actual_start": "a", "actual_end": "b",
              "actual_minutes": 45, "estimated_minutes": 30}, "预估耗时 30m"),
            ({"status": "completed", "actual_logged_seconds": None,
              "actual_time_source": "user", "actual_start": None,
              "actual_minutes": 45, "estimated_minutes": 30}, "预估耗时 30m"),
            ({"status": "timeout", "actual_logged_seconds": None,
              "actual_time_source": None, "estimated_minutes": 30}, "预估耗时 30m"),
        ]
        for occ, expected in cases:
            with self.subTest(occ=occ):
                self.assertEqual(duration(occ), expected)
                html = rows(occ)
                # 详情行 label 与 value 分属两个 span：分别断言
                self.assertIn("实际耗时" if expected.startswith("实际") else "预估耗时", html)
                self.assertIn(expected.split(" ", 1)[1] + "</span>", html)

    def test_interval_summary_uses_minutes(self):
        import json as _json
        summary = lambda task: self.ctx.eval("__summary")(_json.dumps(task))
        self.assertEqual(
            summary({"task_type": "interval", "refresh_mode": "after_completion",
                     "after_completion_minutes": 1501}),
            "完成后 1d1h1m 刷新")
        self.assertEqual(
            summary({"task_type": "interval", "refresh_mode": "fixed_interval",
                     "interval_days": 3}),
            "每 3 天（固定时间轴）")
        fmt = self.ctx.eval("formatIntervalMinutes")
        self.assertEqual(fmt(1440), "1d")
        self.assertEqual(fmt(90), "1h30m")
        self.assertEqual(fmt(30), "30m")
        self.assertEqual(fmt(None), "?")

    def test_form_has_interval_input_and_hint(self):
        # R01 前端：间隔输入为时长文本 + 默认单位提示 + 就近错误区。
        from pathlib import Path
        form = (Path(__file__).resolve().parents[1]
                / "admin" / "js" / "lib" / "planning_task_form.js"
                ).read_text(encoding="utf-8")
        for marker in (
            'id="pf-ac-interval"',
            "无单位按天",
            "after_completion_interval",
            'id="pf-interval-error"',
            'id="pf-ac-block"',
            'id="pf-fi-block"',
            "未收到保存结果，请重试确认",
            "creation_request_deleted",
            # R11（2026-10-07 复审 #11）：清空间隔的保存前确认弹窗——
            # 明确「未填写刷新间隔，保存后将保留原间隔」；取消停留补填。
            "未填写刷新间隔，保存后将保留原间隔",
            "返回补填",
            # R12（2026-10-07 复审 #12）：2xx 正文损坏按结果未知分类。
            "ResultUnknownError",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, form)
        # R12：共享 gw() 对 2xx 不可解析正文以 ResultUnknownError 上抛
        #（结果未知，不是确定失败）。
        api = (Path(__file__).resolve().parents[1]
               / "admin" / "js" / "api.js").read_text(encoding="utf-8")
        self.assertIn("resultUnknown = true", api)


# ── R15：旧数据 dry-run 工具的分页读取（quickjs 之外的纯 Python 行为） ─


def _load_dryrun_tool():
    import importlib.util
    from pathlib import Path
    path = (Path(__file__).resolve().parents[1]
            / "tools" / "planning_old_data_dryrun.py")
    spec = importlib.util.spec_from_file_location(
        "planning_old_data_dryrun_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _PostgrestPageCapClient:
    """R15：模拟 PostgREST 单页行数上限的只读 client。

    select → eq → gt → order → limit → execute 链与 supabase-py 同形；
    execute 只按当前过滤 / 排序 / 游标条件返回一页——超过一页的行必须经
    分页读取才能拿到（旧行为：单次 select 只拿到第一页，返回数量被当
    全量总数）。"""

    def __init__(self, tables, page_size):
        self._tables = tables
        self._page_size = page_size

    def table(self, name):
        rows = self._tables.get(name, [])
        client = self

        class _Query:
            def __init__(self):
                self._eq = {}
                self._gt = None
                self._order_col = None
                self._limit = None

            def select(self, *_args, **_kwargs):
                return self

            def eq(self, col, value):
                self._eq[col] = value
                return self

            def gt(self, col, value):
                self._gt = (col, value)
                return self

            def order(self, col, **_kwargs):
                self._order_col = col
                return self

            def limit(self, size, **_kwargs):
                self._limit = size
                return self

            def execute(self):
                data = [r for r in rows
                        if all(r.get(c) == v for c, v in self._eq.items())]
                if self._order_col:
                    data = sorted(data, key=lambda r: r.get(self._order_col))
                if self._gt:
                    col, value = self._gt
                    data = [r for r in data if r.get(col) > value]
                size = (self._limit if self._limit is not None
                        else client._page_size)
                from types import SimpleNamespace
                return SimpleNamespace(data=list(data[:size]))

        return _Query()


class DryRunPaginationTests(unittest.TestCase):
    def test_beyond_first_page_facts_are_read(self):
        # R15（低优先级修复，2026-10-07 复审 #15）：单次 select 受服务端
        # 行数上限约束——第 N+1 行之后的完成事实读不到时，目标任务被误判
        # delete（报告统计事实 2、目标判 delete，预期 keep）。分页读取
        # （主键键集游标）后事实完整，verdict 与统计正确。
        tool = _load_dryrun_tool()
        inactive = {
            "content": "旧待办", "task_type": "daily", "is_active": False,
            "deleted_at": "2026-10-01T08:00:00+08:00",
            "refresh_enabled": True, "request_state": None,
            "creation_request_key": None, "request_source_occurrence_id": None,
        }
        tables = {
            "planning_task": [
                {**inactive, "id": 10}, {**inactive, "id": 20}, {**inactive, "id": 30},
            ],
            # 事实行按 task_id 升序分页：第 1 页只有 10 / 20，30 的事实
            # 在第 2 页——旧实现读不到 → task 30 被误判 delete。
            "planning_task_completion_fact": [
                {"task_id": 10}, {"task_id": 20}, {"task_id": 30},
            ],
            "planning_occurrence": [],
            "planning_creation_request": [],
        }
        client = _PostgrestPageCapClient(tables, page_size=2)
        tool.PAGE_SIZE = 2  # 缩小页验证分页（真实服务端上限 1000）
        try:
            report, orphans, totals = tool._collect(client, 500)
        finally:
            tool.PAGE_SIZE = 1000
        self.assertEqual(totals["fact_rows"], 3)
        verdicts = {item["task_id"]: item["verdict"] for item in report}
        self.assertEqual(verdicts, {10: "keep", 20: "keep", 30: "keep"})
        self.assertEqual(orphans, [])
        self.assertFalse(totals.get("truncated_by_limit"))

    def test_limit_truncation_is_reported(self):
        # --limit 截断候选时报告显式标记，不得把不完整输出当全量清理依据。
        tool = _load_dryrun_tool()
        inactive = {
            "content": "旧待办", "task_type": "daily", "is_active": False,
            "deleted_at": "2026-10-01T08:00:00+08:00",
            "refresh_enabled": True, "request_state": None,
            "creation_request_key": None, "request_source_occurrence_id": None,
        }
        tables = {
            "planning_task": [{**inactive, "id": 1}, {**inactive, "id": 2},
                              {**inactive, "id": 3}],
            "planning_task_completion_fact": [],
            "planning_occurrence": [],
            "planning_creation_request": [],
        }
        client = _PostgrestPageCapClient(tables, page_size=2)
        report, _orphans, totals = tool._collect(client, 2)
        self.assertEqual(len(report), 2)
        self.assertTrue(totals["truncated_by_limit"])
        self.assertEqual(totals["total_tasks"], 3)


if __name__ == "__main__":
    unittest.main()
