"""批次 5 定向测试：超时换源 window_end_at（一期规范 §18.2 / §22.5）。

核心裁决：超时来源是用户冻结实例窗口的 window_end_at（唯一权威）——
不是 est_end / 系统排程结果 / planned_minutes / 任务 tod 现算 / 旧
deadline；now > window_end_at 才超时（恰等不超时）；closed_at = 窗口
终点（业务死亡时刻）；超时是异常关闭，不写完成 / 结束 / 处理事实、
不推进任何刷新基准；固定刷新型「到达下一规则点死亡」是另一套独立
生命周期，不与本 sweep 揉合；旧 deadline 判定源退役，不构成第二权威。

测试全部走真实 sweep_timeouts / run_maintenance / set_occurrence_status /
reschedule_timeout_as_new 路径与真实 occurrence 行，不在测试内复制判定
算法；判定源边界（est 与 window 分叉、恰等等号）由定向场景锁定，供
mutation 变异验证杀死错误实现。
"""

import logging
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from gateway import planning
from test_planning import _setup
from test_planning_phase1b import Context, at
from test_planning_window_schedule import seed_occ, seed_task

CST = timezone(timedelta(hours=8))


def ats(day, hour=7, minute=0, second=0):
    """秒级精度时刻（窗口终点恰等 / 越过边界矩阵用）。"""
    return datetime(2026, 9, day, hour, minute, second, tzinfo=CST)


def cstm(month, day, hour=6, minute=0):
    """跨月时刻（fixed 轴延伸到 10 月的冻结边界断言用）。"""
    return datetime(2026, month, day, hour, minute, tzinfo=CST)


def iso(day, hour=0, minute=0):
    return planning._iso(ats(day, hour, minute))


# ── A. 双端窗口：恰等不超时，越过才超时 ───────────────────────────

def test_both_end_window_boundary_matrix():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="20:00")
        occ = c.rows[0]
        assert occ["window_start_at"] == iso(24, 18)
        assert occ["window_end_at"] == iso(24, 20)
        # now 19:59:59 → 不超时
        assert planning.sweep_timeouts(ats(24, 19, 59, 59))["timed_out"] == 0
        assert occ["status"] == "pending"
        # now 20:00:00（恰等最晚完成）→ 仍不超时
        assert planning.sweep_timeouts(ats(24, 20, 0, 0))["timed_out"] == 0
        assert occ["status"] == "pending"
        assert occ.get("closed_at") is None
        # now 20:00:01（真实越过）→ timeout
        assert planning.sweep_timeouts(ats(24, 20, 0, 1))["timed_out"] == 1
        assert occ["status"] == "timeout"
        # closed_at = 窗口终点 20:00:00，不是扫描时刻 20:00:01
        assert occ["closed_at"] == iso(24, 20)
        assert occ["updated_at"] == planning._iso(ats(24, 20, 0, 1))
        assert occ["window_start_at"] == iso(24, 18)
        assert occ["window_end_at"] == iso(24, 20)


# ── B. only-latest：只有最晚完成也是完整超时约束 ──────────────────

def test_only_latest_window_times_out_after_passing():
    with Context() as c:
        # 指定日期 once + 只有最晚完成：内部周期 9/25，9/25 刷新生成冻结
        c.create("once", at(24, 10), estimated_minutes=30,
                 target_date="2026-09-25", window_end_tod="20:00")
        assert c.rows == []
        planning.generate_due(at(25, 6))
        occ = c.rows[0]
        assert occ["window_start_at"] is None
        assert occ["window_end_at"] == iso(25, 20)
        assert planning.sweep_timeouts(ats(25, 20, 0, 0))["timed_out"] == 0
        assert planning.sweep_timeouts(ats(25, 20, 0, 1))["timed_out"] == 1
        assert (occ["status"], occ["closed_at"]) == ("timeout", iso(25, 20))


# ── C. only-earliest：只有最早开始不存在最晚完成超时 ──────────────

def test_only_earliest_window_never_times_out_via_window_model():
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, window_start_tod="08:00")
        occ = c.rows[0]
        assert occ["window_start_at"] == iso(24, 8)
        assert occ["window_end_at"] is None
        # 过去很多小时：不因窗口模型超时，也不自动补最晚完成
        assert planning.sweep_timeouts(at(25, 20))["timed_out"] == 0
        assert planning.sweep_timeouts(at(26, 12))["timed_out"] == 0
        assert occ["status"] == "pending"
        assert occ["window_end_at"] is None


# ── D. 无窗口：不因新窗口模型自动超时 ─────────────────────────────

def test_no_window_open_instances_never_time_out():
    with Context() as c:
        c.create("daily", at(23), estimated_minutes=30)
        planning.generate_due(at(25, 6))  # 顺延 + 新轮，均无窗口
        assert all(row["window_end_at"] is None for row in c.rows)
        assert planning.sweep_timeouts(at(26, 12))["timed_out"] == 0
        assert all(row["status"] == "pending" for row in c.rows)
    with Context() as c:
        # 无窗口 once：指定日期过去后仍持续顺延，不因日期过去自动超时（§10）
        c.create("once", at(23), estimated_minutes=30, target_date="2026-09-23")
        planning.generate_due(at(26, 6))
        assert planning.sweep_timeouts(at(27, 12))["timed_out"] == 0
        assert c.rows[0]["status"] == "pending"


# ── E. 固定刷新到期死亡与窗口超时是两种独立触发来源 ────────────────

def _seed_fixed_with_anchor(c, *, window_end_tod, anchor_day=21):
    """固定间隔任务（锚点 9/21 20:00、3 天间隔）：当前轮 9/21 20:00 到期、
    下一规则点 9/24 20:00，可与窗口终点构成双死亡边界。"""
    c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3,
             refresh_anchor_at=planning._iso(at(anchor_day, 20)),
             window_end_tod=window_end_tod)
    return c.rows[0]


def _fail_fixed_round_inserts(c, task_id, fail_from=1):
    """注入：指定任务的 planning_occurrence INSERT 从第 fail_from 次起临时
    失败（fail_from=1 即全部失败；fail_from=2 模拟批量补生成中途故障）。
    返回还原函数。"""
    original_table = c.db.table
    state = {"attempts": 0}

    def failing_table(name):
        query = original_table(name)
        if name == "planning_occurrence":
            original_execute = query.execute

            def execute():
                payload = getattr(query, "payload", None)
                items = payload if isinstance(payload, list) else [payload]
                if getattr(query, "action", None) == "insert" and any(
                        isinstance(item, dict) and item.get("task_id") == task_id
                        for item in items):
                    state["attempts"] += 1
                    if state["attempts"] >= fail_from:
                        raise RuntimeError("simulated transient insert failure")
                return original_execute()

            query.execute = execute
        return query

    c.db.table = failing_table
    return lambda: setattr(c.db, "table", original_table)


def test_dual_death_boundary_fixed_expiration_earlier_wins():
    # Review HIGH 场景 A：fixed 到期 9/24 20:00 早于 window_end_at 22:00，
    # scanner 20:30 → closed_at = 20:00（更早的业务死亡边界，不由执行顺序决定）
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        assert stale["fixed_due_at"] == planning._iso(at(21, 20))
        assert stale["window_end_at"] == iso(24, 22)
        result = planning.run_maintenance(ats(24, 20, 30))
        assert result["generation"]["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        # 已被固定到期关闭：窗口 sweep 不重复处理（终态不二次改写）
        assert result["timeouts"]["timed_out"] == 0


def test_dual_death_boundary_window_earlier_wins():
    # Review HIGH 场景 B：window_end_at 9/24 19:00 早于 fixed 到期 20:00，
    # scanner 23:00（generate_due 先于 sweep 运行）→ closed_at = 19:00。
    # 修复前固定到期先执行会写 20:00（谁先执行谁写）；修复后取更早者。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="19:00")
        assert stale["window_end_at"] == iso(24, 19)
        result = planning.run_maintenance(ats(24, 23))
        assert result["generation"]["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 19)))
        assert result["timeouts"]["timed_out"] == 0


def test_dual_death_boundary_window_boundary_not_yet_due_keeps_fixed():
    # 窗口边界尚未越过（未成立）时不影响固定到期：fixed 20:00 关闭，窗口 22:00 不参与
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="23:00")
        result = planning.run_maintenance(ats(24, 20, 30))
        assert result["generation"]["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))


def test_dual_death_boundary_paused_fixed_still_window_times_out():
    # 暂停刷新冻结固定到期清理，但窗口超时独立于 refresh_enabled：
    # 双边界并存时唯一成立的死亡边界是窗口终点
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="19:00")
        c.db.rows["planning_task"][0]["refresh_enabled"] = False
        result = planning.run_maintenance(ats(24, 23))
        assert result["generation"]["timed_out"] == 0
        assert result["timeouts"]["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 19)))
        # 暂停语义不回退：不繁殖新轮次
        assert len(c.rows) == 1


def test_dual_death_boundary_paused_fixed_excluded_from_min():
    # Review 二轮 HIGH 场景 E：refresh_enabled=false 时固定到期不成立——
    # 即使历史规则点已过，fixed 20:00 也不参与 min；唯一成立的死亡边界
    # 是窗口终点 22:00（不得写 20:00）。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        c.db.rows["planning_task"][0]["refresh_enabled"] = False
        result = planning.run_maintenance(ats(24, 23))
        assert result["generation"]["timed_out"] == 0
        assert result["timeouts"]["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 22)))
        # 暂停语义不回退：不繁殖新轮次
        assert len(c.rows) == 1


def test_dual_death_boundary_normal_order_late_scanner():
    # Review 二轮补充：场景 A 的 23:00 双触发（generation 正常）——固定到期
    # 先按 min 关闭旧轮（20:00），窗口 sweep 不重复处理；新一轮窗口在
    # scanner 之后才到期，保持开放。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        result = planning.run_maintenance(ats(24, 23))
        assert result["generation"]["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        assert result["timeouts"]["timed_out"] == 0
        fresh = c.rows[-1]
        assert fresh["status"] == "pending"
        assert fresh["window_end_at"] == iso(25, 22)


def test_generation_failure_keeps_earliest_fixed_boundary():
    # Review 二轮 HIGH 场景 C：fixed 20:00 / window 22:00 / scanner 23:00，
    # 生成下一固定轮的 INSERT 临时失败 → _expire_fixed_rounds 本轮未执行，
    # 但 sweep 不得把旧轮永久关成 22:00；最早业务死亡边界 20:00 与调用顺序、
    # generation 成败、scanner 迟到时长无关。故障解除后再次 maintenance
    # 结果保持正确（终态不二次改写）。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        result = planning.run_maintenance(ats(24, 23))
        restore()
        assert [e["task_id"] for e in result["generation"]["errors"]] == [stale["task_id"]]
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        assert stale.get("handled_at") is None and stale.get("actual_end") is None
        # 故障解除：下一固定轮照常生成；旧轮终态与死亡边界不被改写
        result2 = planning.run_maintenance(ats(24, 23, 30))
        assert result2["generation"]["created"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        assert result2["timeouts"]["timed_out"] == 0


def test_generation_failure_window_earlier_still_wins():
    # Review 二轮 HIGH 场景 F：window 19:00 < fixed 20:00 且 generation 失败
    # → closed_at 仍必须 19:00（min 方向不因失败改变）。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="19:00")
        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        result = planning.run_maintenance(ats(24, 23))
        restore()
        assert [e["task_id"] for e in result["generation"]["errors"]] == [stale["task_id"]]
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 19)))


def test_failing_task_does_not_starve_other_tasks_window_timeout():
    # Review 二轮 HIGH 场景 D：Task A（固定型）generation 持续失败，
    # Task B（普通窗口已超时）仍必须正常 timeout——单任务失败不拖累其他任务。
    with Context() as c:
        failing = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="19:00")
        normal = next(row for row in c.rows if row["task_id"] != failing["task_id"])
        _fail_fixed_round_inserts(c, failing["task_id"])  # 持续失败，不还原
        result = planning.run_maintenance(ats(24, 23))
        assert [e["task_id"] for e in result["generation"]["errors"]] == [failing["task_id"]]
        # B：普通窗口超时照常生效，不被故障任务饿死
        assert (normal["status"], normal["closed_at"]) == ("timeout", planning._iso(at(24, 19)))
        # A：sweep 侧同一裁决，仍取最早死亡边界
        assert (failing["status"], failing["closed_at"]) == ("timeout", planning._iso(at(24, 20)))


def test_sweep_applies_frozen_fixed_boundary_min():
    # generate_due 失败中止 / 独立调用时 reconcile 未执行的固定轮（播种模拟
    # 该中间态）：sweep 读该轮自己生成时冻结的 fixed 边界参与 min——
    # fixed 9/24 07:00 早于 window 9/24 22:00 → closed_at 必须是 07:00，
    # 不是窗口终点（与 _expire_fixed_rounds 同一裁决、同一冻结事实）。
    client, ctx = _setup()
    with ctx():
        client.rows["planning_task"].append({
            "id": 1, "refresh_mode": "fixed_interval", "refresh_enabled": True,
            "is_active": True, "interval_days": 3,
        })
        client.rows["planning_occurrence"].append({
            "id": 1, "task_id": 1, "status": "pending", "source": "schedule",
            "fixed_due_at": planning._iso(ats(21, 7)),
            "fixed_expires_at": planning._iso(ats(24, 7)),
            "window_end_at": planning._iso(ats(24, 22)),
        })
        assert planning.sweep_timeouts(ats(24, 23))["timed_out"] == 1
        row = client.rows["planning_occurrence"][0]
        assert (row["status"], row["closed_at"]) == ("timeout", planning._iso(ats(24, 7)))


def test_fixed_refresh_expiration_survives_without_window():
    with Context() as c:
        # 无 window_end_at 的固定刷新型：到达下一规则点旧轮照常到期死亡
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        assert stale["window_end_at"] is None
        planning.generate_due(at(27, 6))
        assert stale["status"] == "timeout"
        assert stale["closed_at"] == at(27, 6).isoformat()
        assert any(row["schedule_date"] == "2026-09-27" and row["status"] == "pending"
                   for row in c.rows)


def test_fixed_refresh_pause_still_freezes_expiration():
    with Context() as c:
        # 批次 3 暂停修复不被批次 5 误伤：refresh_enabled=false 冻结到期清理
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        c.db.rows["planning_task"][0]["refresh_enabled"] = False
        stale = c.rows[0]
        planning.generate_due(at(27, 6))
        assert stale["status"] == "pending"
        assert stale.get("closed_at") is None
        assert not any(row["schedule_date"] == "2026-09-27" for row in c.rows)
        # sweep 也不得绕过暂停语义波及该实例
        assert planning.sweep_timeouts(at(27, 12))["timed_out"] == 0


def test_window_timeout_fires_independently_of_fixed_axis_death():
    with Context() as c:
        # 带窗口的固定刷新型：窗口已过但下一规则点未到 → 窗口超时独立触发
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3,
                 window_end_tod="12:00")
        stale = c.rows[0]
        assert stale["window_end_at"] == iso(24, 12)
        assert planning.sweep_timeouts(at(25, 13))["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", iso(24, 12))
        # 固定时间轴不动：9/27 到点正常生成新轮
        planning.generate_due(at(27, 6))
        assert any(row["schedule_date"] == "2026-09-27" and row["status"] == "pending"
                   for row in c.rows)


# ── F. in_progress：超时 ≠ 自动完成，执行事实保留 ─────────────────

def test_in_progress_times_out_preserving_actual_start():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 15))
        assert occ["actual_start"] == planning._iso(at(24, 15))
        assert planning.sweep_timeouts(ats(24, 20, 0, 1))["timed_out"] == 1
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == iso(24, 20)
        # 已有真实执行事实保留；不得伪造用户完成事实
        assert occ["actual_start"] == planning._iso(at(24, 15))
        assert occ.get("actual_end") is None
        assert occ.get("handled_at") is None
        assert occ.get("actual_minutes") is None


# ── G. partial：开放生命周期实例照常被窗口超时，partial 事实保留 ──

def test_partial_open_instance_times_out_keeping_partial_facts():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "做了一半"}, at(24, 16))
        assert occ["partial_at"] == planning._iso(at(24, 16))
        assert planning.sweep_timeouts(ats(24, 20, 0, 1))["timed_out"] == 1
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == iso(24, 20)
        assert occ["partial_note"] == "做了一半"
        assert occ["partial_at"] == planning._iso(at(24, 16))
        assert occ.get("handled_at") is None


# ── H. est_end 是排程结果，绝不成为超时来源 ───────────────────────

def test_est_end_earlier_than_window_never_triggers_early_timeout():
    # 窗口 18:00→22:00，系统排程结果 18:00→19:00：19:00:01 不得提前超时
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        occ["est_start"] = iso(24, 18)
        occ["est_end"] = iso(24, 19)
        assert planning.sweep_timeouts(ats(24, 19, 0, 1))["timed_out"] == 0
        assert occ["status"] == "pending"
        # 真正最晚完成仍是 22:00
        assert planning.sweep_timeouts(ats(24, 22, 0, 1))["timed_out"] == 1
        assert (occ["status"], occ["closed_at"]) == ("timeout", iso(24, 22))


def test_est_end_later_than_window_still_times_out_at_window_end():
    # 历史/异常 est_end 写成 23:00，但 window_end_at=22:00：22:00 后必须超时
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        occ["est_start"] = iso(24, 18)
        occ["est_end"] = iso(24, 23)
        assert planning.sweep_timeouts(ats(24, 22, 0, 1))["timed_out"] == 1
        assert (occ["status"], occ["closed_at"]) == ("timeout", iso(24, 22))


# ── I. 排程 conflict ≠ timeout ───────────────────────────────────

def test_scheduling_conflict_keeps_status_open_until_window_end():
    # 批次 4 conflict 是「现在按列表顺序排不进去」的派生结果，不落库、
    # 不是超时；sweep 不读冲突列表，只有真实时间越过 window_end_at 才超时。
    now = ats(20, 4, 1)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        row = seed_occ(client, 1, 1, now=now, planned_minutes=60,
                       window_start_at=iso(20, 3), window_end_at=iso(20, 5))
        result = planning.recompute_today(now)
        assert len(result["conflicts"]) == 1  # 排程冲突（窗口内装不下）
        assert row["status"] == "pending"
        # now 仍未越过窗口终点：sweep 不打标
        assert planning.sweep_timeouts(ats(20, 4, 30))["timed_out"] == 0
        assert row["status"] == "pending"
        # 真实时间越过窗口终点才超时
        assert planning.sweep_timeouts(ats(20, 5, 0, 1))["timed_out"] == 1
        assert (row["status"], row["closed_at"]) == ("timeout", iso(20, 5))


# ── J. 终态实例不得再次扫描 ──────────────────────────────────────

def test_terminal_rows_are_never_reswept():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        completed = c.rows[0]
        planning.set_occurrence_status(completed["id"], {"status": "completed"}, at(24, 19))
        facts = (completed["closed_at"], completed["handled_at"], completed["actual_end"])
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        discarded = next(row for row in c.rows if row["task_id"] != completed["task_id"])
        planning.set_occurrence_status(discarded["id"], {"status": "discarded_this"}, at(24, 19))
        discarded_facts = (discarded["closed_at"], discarded["handled_at"])
        # 越过窗口终点扫描：关闭行不重复写
        assert planning.sweep_timeouts(at(24, 21))["timed_out"] == 0
        assert completed["status"] == "completed"
        assert (completed["closed_at"], completed["handled_at"], completed["actual_end"]) == facts
        assert discarded["status"] == "discarded_this"
        assert (discarded["closed_at"], discarded["handled_at"]) == discarded_facts
    with Context() as c:
        # timeout 行幂等：重复扫描不重复写（closed_at / updated_at 不变）
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        occ = c.rows[0]
        assert planning.sweep_timeouts(at(24, 21))["timed_out"] == 1
        first = (occ["closed_at"], occ["updated_at"])
        assert planning.sweep_timeouts(at(24, 22))["timed_out"] == 0
        assert (occ["closed_at"], occ["updated_at"]) == first


# ── K. 窗口超时不推进刷新基准；task baseline 直接断言；adopt 照常 ──

def test_window_timeout_does_not_advance_after_completion_baseline():
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="after_completion", interval_days=3,
                 window_end_tod="12:00")
        first = c.rows[0]
        # 第一轮正常完成：写出真实的任务级刷新基准（9/24 09:00 处理 → 下一轮 9/27 09:00）
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 9))
        task_row = c.db.rows["planning_task"][0]
        baseline = (task_row["last_handled_at"], task_row["refresh_next_due_at"])
        assert baseline == (planning._iso(at(24, 9)), planning._iso(at(27, 9)))
        # 第二轮（9/27 12:00 窗口）到期生成后窗口超时：异常关闭
        planning.generate_due(ats(27, 9, 0, 1))
        second = c.rows[1]
        assert second["window_end_at"] == iso(27, 12)
        assert planning.sweep_timeouts(ats(27, 12, 0, 1))["timed_out"] == 1
        assert (second["status"], second["closed_at"]) == ("timeout", iso(27, 12))
        assert second.get("handled_at") is None
        # 任务级 baseline 直接断言：last_handled_at / refresh_next_due_at 完全不变
        assert task_row["last_handled_at"] == planning._iso(at(24, 9))
        assert task_row["refresh_next_due_at"] == planning._iso(at(27, 9))
        # 异常关闭不是处理事实：不推进 after_completion 下一轮
        assert planning.generate_due(at(30, 6))["created"] == 0
        assert len(c.rows) == 2
        # 既有合法恢复动作照常消费 timeout 实例（adopt 接管重排）
        result = planning.reschedule_timeout_as_new(
            second["id"], {"est_start": at(30, 10).isoformat()}, at(30, 9),
            idempotency_key="adopt-1")
        new_occ = next(row for row in c.rows if row["task_id"] == result["task"]["id"])
        assert new_occ["status"] == "pending"
        assert new_occ["window_end_at"] is None  # 重排产出的无窗口 once
        assert (second["status"], second["closed_at"]) == ("timeout", iso(27, 12))


# ── L. 中空待办：共享窗口终点，只关闭真正开放的阶段 ───────────────

def _seed_hollow(c, **window_kw):
    c.create("daily", at(24, 9), estimated_minutes=30, is_hollow=True,
             hollow_start_content="炖汤", hollow_start_minutes=10,
             hollow_wait_minutes=60, hollow_end_content="盛汤", hollow_end_minutes=10,
             **window_kw)
    start_row = next(row for row in c.rows if row["phase"] == "start")
    end_row = next(row for row in c.rows if row["phase"] == "end")
    return start_row, end_row


def test_hollow_end_phase_times_out_completed_start_untouched():
    with Context() as c:
        start_row, end_row = _seed_hollow(c, window_start_tod="10:00", window_end_tod="20:00")
        # 两阶段共享同一冻结窗口终点
        assert start_row["window_end_at"] == end_row["window_end_at"] == iso(24, 20)
        planning.set_occurrence_status(start_row["id"], {"status": "completed"}, at(24, 10))
        assert planning.sweep_timeouts(ats(24, 20, 0, 1))["timed_out"] == 1
        # 只关闭真正开放的结束阶段；已完成开始阶段不被再次扫描 / 改写
        assert end_row["status"] == "timeout"
        assert end_row["closed_at"] == iso(24, 20)
        assert start_row["status"] == "completed"
        assert start_row["closed_at"] == planning._iso(at(24, 10))
        # 不伪造结束阶段完成事实
        assert end_row.get("handled_at") is None
        assert end_row.get("actual_end") is None


def test_hollow_both_open_phases_time_out_once_each():
    with Context() as c:
        start_row, end_row = _seed_hollow(c, window_start_tod="10:00", window_end_tod="20:00")
        assert planning.sweep_timeouts(ats(24, 20, 0, 1))["timed_out"] == 2
        assert start_row["status"] == end_row["status"] == "timeout"
        assert start_row["closed_at"] == end_row["closed_at"] == iso(24, 20)


# ── M. 旧 deadline 判定源退役：window_end_at 是唯一超时权威 ────────

def test_legacy_deadline_alone_never_triggers_window_sweep():
    with Context() as c:
        # 存量限时快照（deadline_at 已过 + window_end_at = null）：
        # 不应仅因旧 deadline 触发新 sweep（双权威禁止，退役不兼容回潮）
        c.create("daily", at(23), estimated_minutes=30)
        occ = c.rows[0]
        occ["is_limited"] = True
        occ["deadline_at"] = planning._iso(at(23, 12))
        assert planning.sweep_timeouts(at(24, 13))["timed_out"] == 0
        assert occ["status"] == "pending"


def test_window_end_at_is_sole_timeout_authority():
    with Context() as c:
        # deadline_at 未过 + window_end_at 已过 → 按 window_end_at 超时
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        occ = c.rows[0]
        occ["is_limited"] = True
        occ["deadline_at"] = planning._iso(at(24, 23))
        assert planning.sweep_timeouts(ats(24, 20, 0, 1))["timed_out"] == 1
        assert (occ["status"], occ["closed_at"]) == ("timeout", iso(24, 20))
        # 超时不写处理事实（deadline 快照本身也不被改写）
        assert occ.get("handled_at") is None
        assert occ["deadline_at"] == planning._iso(at(24, 23))


# ── 查询侧过滤与稳定分页（Review MEDIUM） ─────────────────────────

def _bulk_rows(c, rows):
    """直接播种最小 occurrence 行（sweep 只消费 id / status / window_end_at）。"""
    for row in rows:
        c.db.rows["planning_occurrence"].append({
            "id": row["id"], "task_id": row.get("task_id", 1),
            "status": row.get("status", "pending"),
            "window_end_at": row.get("window_end_at"),
            "is_limited": False, "source": "schedule",
        })


def test_windowless_open_rows_cannot_fill_sweep_page():
    # 模拟服务端页上限：1000 条无窗口开放行 + 第 1001 条窗口已过期。
    # 查询侧 window_end_at < now 过滤使无窗口 / legacy 行不进入结果页，
    # 过期行必须被扫到（页大小压到 2 也不受 1000 条无窗口行阻挡）。
    with Context() as c:
        _bulk_rows(c, [{"id": i, "status": "pending", "window_end_at": None}
                       for i in range(1, 1001)])
        _bulk_rows(c, [{"id": 1001, "status": "pending",
                        "window_end_at": planning._iso(ats(24, 12))}])
        with mock.patch.object(planning, "SWEEP_PAGE_SIZE", 2):
            result = planning.sweep_timeouts(ats(24, 13))
        assert result["timed_out"] == 1
        due_row = c.db.rows["planning_occurrence"][1000]
        assert due_row["status"] == "timeout"
        assert due_row["closed_at"] == planning._iso(ats(24, 12))
        assert all(row["status"] == "pending"
                   for row in c.db.rows["planning_occurrence"][:1000])


def test_all_due_rows_across_pages_all_time_out():
    # 1002 条全部到期的窗口行、页大小 1000：两页全部处理，不得只处理前 1000；
    # closed_at 一律 = 窗口终点；重复 sweep 无额外写入（updated_at 不变）。
    frozen_end = planning._iso(ats(24, 12))
    with Context() as c:
        _bulk_rows(c, [{"id": i, "status": "pending", "window_end_at": frozen_end}
                       for i in range(1, 1003)])
        with mock.patch.object(planning, "SWEEP_PAGE_SIZE", 1000):
            result = planning.sweep_timeouts(ats(24, 13))
        assert result["timed_out"] == 1002
        rows = c.db.rows["planning_occurrence"]
        assert all(row["status"] == "timeout" for row in rows)
        assert all(row["closed_at"] == frozen_end for row in rows)
        with mock.patch.object(planning, "SWEEP_PAGE_SIZE", 1000):
            assert planning.sweep_timeouts(ats(24, 14))["timed_out"] == 0
        assert all(row["updated_at"] == planning._iso(ats(24, 13)) for row in rows)


def test_multi_page_sweep_never_skips_rows_after_first_page_update():
    # 页大小 3、7 条全部到期：3+3+1 三页全部处理——第一页 update 使开放
    # 结果集缩小后，后续行仍被逐页处理（first-page-until-empty，不用 offset）。
    with Context() as c:
        _bulk_rows(c, [{"id": i, "status": "pending",
                        "window_end_at": planning._iso(ats(24, 12))}
                       for i in range(1, 8)])
        with mock.patch.object(planning, "SWEEP_PAGE_SIZE", 3):
            result = planning.sweep_timeouts(ats(24, 13))
        assert result["timed_out"] == 7
        assert all(row["status"] == "timeout"
                   for row in c.db.rows["planning_occurrence"])


# ── run_maintenance 真实维护路径 ─────────────────────────────────

def test_run_maintenance_sweeps_window_timeout_and_skips_conflict_free_recompute():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="20:00")
        occ = c.rows[0]
        result = planning.run_maintenance(ats(24, 20, 0, 1))
        assert result["timeouts"]["timed_out"] == 1
        assert (occ["status"], occ["closed_at"]) == ("timeout", iso(24, 20))
        # 窗口超时不推进刷新基准：后续维护不再产生本轮的新副本
        planning.run_maintenance(at(25, 6))
        assert len([row for row in c.rows if row["schedule_date"] == "2026-09-24"]) == 1


# ── 三轮 Review：fixed_expires_at 生成冻结与规则编辑不可追溯 ──────

def test_generation_freezes_own_next_rule_event_per_round():
    # 约束 3/4：fixed_interval 一次 reconcile 补生成多个历史轮次时，每轮
    # 各冻结规则序列中属于自己的下一个事件（不按扫描时刻推算）；轴末轮
    # （下一个事件在将来）按同一规则向前多看一个事件。
    with Context() as c:
        c.create("interval", at(20, 6), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        assert (first["fixed_due_at"], first["fixed_expires_at"]) == (
            planning._iso(at(20, 6)), planning._iso(at(23, 6)))
        # 离线跨三个轴点：9/23、9/26、9/29 一次补生成
        planning.generate_due(at(29, 6, 30))
        rounds = {row["fixed_due_at"]: row["fixed_expires_at"] for row in c.rows}
        assert rounds[planning._iso(at(23, 6))] == planning._iso(at(26, 6))
        assert rounds[planning._iso(at(26, 6))] == planning._iso(at(29, 6))
        assert rounds[planning._iso(at(29, 6))] == planning._iso(cstm(10, 2))


def test_generation_freezes_weekday_and_monthday_boundaries():
    with Context() as c:
        # 2026-09-17 周四、9/18 周五（weekdays [3,4] = 周四、周五）
        c.create("weekly", at(17, 6), weekdays=[3, 4])
        stale = c.rows[0]
        assert (stale["fixed_due_at"], stale["fixed_expires_at"]) == (
            planning._iso(at(17, 6)), planning._iso(at(18, 6)))
    with Context() as c:
        c.create("monthly", at(10, 6), month_days=[10, 20])
        stale = c.rows[0]
        assert (stale["fixed_due_at"], stale["fixed_expires_at"]) == (
            planning._iso(at(10, 6)), planning._iso(at(20, 6)))


def test_non_fixed_rounds_and_early_rows_keep_null_fixed_expires_at():
    # 约束 6：不使用固定到期死亡的实例保持 NULL（daily 有 fixed_due_at——
    # 本轮自己的事件——但无死亡边界；once / after_completion 全 NULL）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        c.create("once", at(24, 10), estimated_minutes=30, target_date="2026-09-24")
        c.create("interval", at(24, 10), refresh_mode="after_completion", interval_days=3)
        daily = c.rows[0]
        assert daily["fixed_due_at"] == planning._iso(at(24, 10))  # 本轮自身事件 = 创建时刻
        assert all(row["fixed_expires_at"] is None for row in c.rows)
    with Context() as c:
        # 提前完成的额外完成记录行（source='early'）不携带固定死亡边界
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        planning.set_occurrence_status(stale["id"], {"status": "completed"}, at(24, 7))
        result = planning.complete_task_early(stale["task_id"], at(24, 8),
                                              idempotency_key="freeze-e1")
        early = next(row for row in c.rows if row["source"] == "early")
        assert result["source"] == "early"
        # 额外完成记录行不经 _occurrence_row 构建，无固定轴事实键
        assert early.get("fixed_due_at") is None
        assert early.get("fixed_expires_at") is None


def test_hollow_fixed_round_shares_frozen_expires_at():
    # 约束 5：中空同轮 start / end 两阶段共享同一个 fixed_expires_at。
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3,
                 is_hollow=True, hollow_start_content="开始", hollow_start_minutes=10,
                 hollow_wait_minutes=30, hollow_end_content="结束", hollow_end_minutes=10)
        start_row = next(row for row in c.rows if row["phase"] == "start")
        end_row = next(row for row in c.rows if row["phase"] == "end")
        assert start_row["fixed_expires_at"] == end_row["fixed_expires_at"] == planning._iso(at(27, 6))


def test_rule_edit_interval_shortened_does_not_shorten_old_round_life():
    # 必测：3天 → 1天。旧轮死亡边界保持生成时冻结的 9/24 20:00，
    # 不得被新轴提前到 9/22 20:00（历史不重解释）。
    with Context() as c:
        c.create("interval", at(21, 20), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(24, 20))
        planning.update_task(stale["task_id"], {"interval_days": 1}, at(24, 10))
        # 约束 8：规则编辑不改写任何已生成 occurrence 的冻结值
        assert stale["fixed_expires_at"] == planning._iso(at(24, 20))
        planning.generate_due(at(24, 23))
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        # 新轴轮按新规则生成并冻结自己的边界（9/24 20:00 → 9/25 20:00）
        fresh = c.rows[-1]
        assert fresh["fixed_due_at"] == planning._iso(at(24, 20))
        assert fresh["fixed_expires_at"] == planning._iso(cstm(9, 25, 20))


def test_rule_edit_interval_shortened_failure_path_same_boundary():
    # 必测（INSERT 失败路径）：新轴轮创建失败时，清理已先于创建执行，
    # 旧轮同样按冻结边界 9/24 20:00 关闭——两条入口同一历史边界。
    with Context() as c:
        c.create("interval", at(21, 20), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        planning.update_task(stale["task_id"], {"interval_days": 1}, at(24, 10))
        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        result = planning.run_maintenance(at(24, 23))
        restore()
        assert [e["task_id"] for e in result["generation"]["errors"]] == [stale["task_id"]]
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))


def test_rule_edit_interval_lengthened_does_not_lengthen_old_round_life():
    # 必测：1天 → 5天。旧轮（9/23 轮）死亡边界保持生成时冻结的 9/24 20:00，
    # 不得延后到新轴 9/26 20:00。
    with Context() as c:
        c.create("interval", at(21, 20), refresh_mode="fixed_interval", interval_days=1)
        planning.generate_due(at(23, 7))   # 轴轮 9/22 到点生成；9/21 轮按冻结边界死亡
        planning.generate_due(at(24, 12))  # 轴轮 9/23 到点生成；9/22 轮按冻结边界死亡
        by_due = {row["fixed_due_at"]: row for row in c.rows}
        assert by_due[planning._iso(at(22, 20))]["fixed_expires_at"] == planning._iso(at(23, 20))
        assert by_due[planning._iso(at(23, 20))]["fixed_expires_at"] == planning._iso(at(24, 20))
        planning.update_task(by_due[planning._iso(at(23, 20))]["task_id"],
                             {"interval_days": 5}, at(24, 13))
        planning.generate_due(at(24, 21))
        # 各轮均按生成时冻结的边界死亡，不随新轴（9/26）延后
        assert by_due[planning._iso(at(21, 20))]["closed_at"] == planning._iso(at(22, 20))
        assert by_due[planning._iso(at(22, 20))]["closed_at"] == planning._iso(at(23, 20))
        assert (by_due[planning._iso(at(23, 20))]["status"],
                by_due[planning._iso(at(23, 20))]["closed_at"]) == (
            "timeout", planning._iso(at(24, 20)))
        assert all(row["fixed_expires_at"] == planning._iso(at(24, 20))
                   for row in c.rows if row["fixed_due_at"] == planning._iso(at(23, 20)))


def test_rule_edit_weekdays_does_not_move_old_round_boundary():
    # 必测：weekday 集合编辑后旧轮死亡边界不漂移。
    with Context() as c:
        c.create("weekly", at(17, 6), weekdays=[3, 4])  # 9/17 周四、9/18 周五
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(18, 6))
        planning.update_task(stale["task_id"], {"weekdays": [1]}, at(18, 7))  # 改成周二
        assert stale["fixed_expires_at"] == planning._iso(at(18, 6))
        planning.generate_due(at(18, 8))
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(18, 6)))


def test_rule_edit_weekdays_failure_path_same_boundary():
    # 失败路径：注入保持到新轴首个事件（9/22 周二 06:00）到点的维护——
    # 新轴轮 INSERT 失败，但旧轮已在清理步（先于创建）按冻结边界关闭。
    with Context() as c:
        c.create("weekly", at(17, 6), weekdays=[3, 4])
        stale = c.rows[0]
        planning.update_task(stale["task_id"], {"weekdays": [1]}, at(18, 7))  # 改成周二
        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        result = planning.run_maintenance(at(22, 6, 30))
        restore()
        assert [e["task_id"] for e in result["generation"]["errors"]] == [stale["task_id"]]
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(18, 6)))


def test_rule_edit_month_days_does_not_move_old_round_boundary():
    # 必测：monthday 集合编辑后旧轮死亡边界不漂移。
    with Context() as c:
        c.create("monthly", at(10, 6), month_days=[10, 20])
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(20, 6))
        planning.update_task(stale["task_id"], {"month_days": [15]}, at(20, 7))  # 改成每月 15 日
        assert stale["fixed_expires_at"] == planning._iso(at(20, 6))
        planning.generate_due(at(20, 8))
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(20, 6)))


def test_rule_edit_month_days_failure_path_same_boundary():
    # 失败路径：注入保持到新轴首个事件（10/15 06:00）到点的维护——
    # 新轴轮 INSERT 失败，但旧轮已在清理步（先于创建）按冻结边界关闭。
    with Context() as c:
        c.create("monthly", at(10, 6), month_days=[10, 20])
        stale = c.rows[0]
        planning.update_task(stale["task_id"], {"month_days": [15]}, at(20, 7))  # 改成每月 15 日
        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        result = planning.run_maintenance(cstm(10, 15, 6, 30))
        restore()
        assert [e["task_id"] for e in result["generation"]["errors"]] == [stale["task_id"]]
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(20, 6)))


def test_generation_failure_at_fixed_boundary_closes_old_round_immediately():
    # 三轮 HIGH 1 必测：fixed 20:00 / window 22:00、scanner 20:30、下一轮
    # INSERT 失败——到期清理已与新轮创建解耦，旧轮立即按冻结边界 20:00
    # timeout（不保持 pending、不等 22:00 由窗口补关、20:31 起不可 completed）；
    # 故障解除后下一轮补生成，旧轮 closed_at 不被改写。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        result = planning.run_maintenance(ats(24, 20, 30))
        restore()
        assert [e["task_id"] for e in result["generation"]["errors"]] == [stale["task_id"]]
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        assert stale.get("handled_at") is None and stale.get("actual_end") is None
        result2 = planning.run_maintenance(ats(24, 21))
        assert result2["generation"]["created"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        try:
            planning.set_occurrence_status(stale["id"], {"status": "completed"}, at(24, 21, 30))
        except planning.PlanningError as error:
            assert error.code == "invalid_transition"
        else:
            raise AssertionError("expired fixed round must not be completed after death")


def test_pause_keeps_frozen_expires_at_untouched_and_resumes():
    # 约束 7 必测：refresh_enabled=false 冻结存在但不执行；恢复刷新后按
    # 既有补生成语义恢复，旧轮按冻结边界到期死亡，冻结值全程不删不改。
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(27, 6))
        c.db.rows["planning_task"][0]["refresh_enabled"] = False
        planning.generate_due(at(27, 6))
        assert stale["status"] == "pending"
        assert stale["fixed_expires_at"] == planning._iso(at(27, 6))
        c.db.rows["planning_task"][0]["refresh_enabled"] = True
        planning.generate_due(at(27, 6, 30))
        assert stale["status"] == "timeout"
        assert stale["closed_at"] == planning._iso(at(27, 6))
        assert stale["fixed_expires_at"] == planning._iso(at(27, 6))


def test_inactive_task_fixed_boundary_excluded_from_sweep():
    # 三轮 LOW 必测：is_active=false 时固定到期不参与——唯一有效边界是
    # 窗口终点。正常停用入口会先关闭开放实例，此为异常存量的防御性兼容。
    with Context() as c:
        stale = _seed_fixed_with_anchor(c, window_end_tod="22:00")
        c.db.rows["planning_task"][0]["is_active"] = False
        assert stale["fixed_expires_at"] == planning._iso(at(24, 20))
        assert planning.sweep_timeouts(ats(24, 23))["timed_out"] == 1
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 22)))


def test_legacy_null_fixed_expires_at_never_recomputed_from_current_rule():
    # 必测：旧 NULL 行不按当前规则回算——规则点已过也不因 fixed 边界关闭
    #（存量数据受控部署清理，不建 backfill）。
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        stale["fixed_expires_at"] = None
        planning.update_task(stale["task_id"], {"interval_days": 1}, at(25, 9))
        planning.generate_due(at(26, 12))
        assert stale["status"] == "pending"
        assert planning.sweep_timeouts(at(26, 12))["timed_out"] == 0
        assert stale["status"] == "pending"


# ── 四轮 Review：批量失败清理保证 + 单 task 失败隔离 ───────────────

def test_batch_backfill_partial_failure_still_expires_created_rounds():
    # 四轮 HIGH 1 必测（A）：旧轮 9/20、maintenance 9/29 需补生成 9/23/9/26/9/29；
    # 9/23 INSERT 成功、9/26 失败 → 9/23 轮（expires 9/26 06:00 已过期）必须
    # 在本轮 reconcile 退出前被清理（timeout、closed_at=9/26 06:00、不可
    # completed）；9/26 / 9/29 缺失待恢复；不回滚已成功的前序轮次。
    with Context() as c:
        c.create("interval", at(20, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(23, 6))
        restore = _fail_fixed_round_inserts(c, stale["task_id"], fail_from=2)
        result = planning.generate_due(at(29, 6, 30))
        restore()
        assert [e["task_id"] for e in result["errors"]] == [stale["task_id"]]
        by_due = {row["fixed_due_at"]: row for row in c.rows}
        assert set(by_due) == {planning._iso(at(20, 6)), planning._iso(at(23, 6))}
        created = by_due[planning._iso(at(23, 6))]
        assert (created["status"], created["closed_at"]) == ("timeout", planning._iso(at(26, 6)))
        # 旧轮 9/20 按自己的冻结边界关闭
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(23, 6)))
        try:
            planning.set_occurrence_status(created["id"], {"status": "completed"}, at(29, 7))
        except planning.PlanningError as error:
            assert error.code == "invalid_transition"
        else:
            raise AssertionError("expired backfilled round must not be completed")
        # 故障解除：剩余轮次按既有补生成语义恢复
        result2 = planning.generate_due(at(29, 7))
        assert result2["created"] == 2
        assert "errors" not in result2
        by_due2 = {row["fixed_due_at"]: row for row in c.rows}
        assert by_due2[planning._iso(at(26, 6))]["fixed_expires_at"] == planning._iso(at(29, 6))
        assert by_due2[planning._iso(at(29, 6))]["fixed_expires_at"] == planning._iso(cstm(10, 2))


def test_batch_backfill_last_insert_failure_expires_all_created():
    # 必测（C）：最后一条 INSERT 失败——前面所有成功生成且已到期的轮次
    # 全部按各自冻结边界正确关闭。
    with Context() as c:
        c.create("interval", at(20, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        restore = _fail_fixed_round_inserts(c, stale["task_id"], fail_from=3)
        result = planning.generate_due(at(29, 6, 30))
        restore()
        assert result["errors"]
        by_due = {row["fixed_due_at"]: row for row in c.rows}
        assert (by_due[planning._iso(at(23, 6))]["status"],
                by_due[planning._iso(at(23, 6))]["closed_at"]) == (
            "timeout", planning._iso(at(26, 6)))
        assert (by_due[planning._iso(at(26, 6))]["status"],
                by_due[planning._iso(at(26, 6))]["closed_at"]) == (
            "timeout", planning._iso(at(29, 6)))
        assert not any(row["fixed_due_at"] == planning._iso(at(29, 6)) for row in c.rows)


def test_batch_backfill_success_counts_expirations_once():
    # 必测（D）：generation 全成功——漏跑轮出生即死亡、最新合法轮保持开放、
    # timed_out 计数不重复（旧轮 + 两个出生即死轮 = 3，不因多次清理而翻倍）。
    with Context() as c:
        c.create("interval", at(20, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        result = planning.generate_due(at(29, 6, 30))
        assert "errors" not in result
        assert result["created"] == 3
        assert result["timed_out"] == 3
        by_due = {row["fixed_due_at"]: row for row in c.rows}
        assert (by_due[planning._iso(at(23, 6))]["status"],
                by_due[planning._iso(at(23, 6))]["closed_at"]) == (
            "timeout", planning._iso(at(26, 6)))
        assert (by_due[planning._iso(at(26, 6))]["status"],
                by_due[planning._iso(at(26, 6))]["closed_at"]) == (
            "timeout", planning._iso(at(29, 6)))
        assert by_due[planning._iso(at(29, 6))]["status"] == "pending"
        assert stale["closed_at"] == planning._iso(at(23, 6))


def test_single_task_failure_does_not_block_other_fixed_task_expiry():
    # 必测（E）：A 下一轮 INSERT 失败，B（固定型、已到期）仍必须正常
    # timeout——generate_due 继续处理 B，B 自己的到期清理照常执行。
    with Context() as c:
        failing = _seed_fixed_with_anchor(c, window_end_tod="22:00")  # 任务 A（先创建）
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        other = c.rows[-1]
        assert other["fixed_expires_at"] == planning._iso(at(24, 7))
        restore = _fail_fixed_round_inserts(c, failing["task_id"])
        result = planning.run_maintenance(ats(24, 23))
        restore()
        gen = result["generation"]
        assert [e["task_id"] for e in gen["errors"]] == [failing["task_id"]]
        # A：清理步已先行关闭（解耦）
        assert (failing["status"], failing["closed_at"]) == ("timeout", planning._iso(at(24, 20)))
        # B：A 失败后 reconcile 继续执行，B 的到期清理照常关闭
        assert (other["status"], other["closed_at"]) == ("timeout", planning._iso(at(24, 7)))


def test_three_task_loop_isolates_failure_and_keeps_errors_observable():
    # 必测（F）：A 正常、B generation 失败、C 正常 → A/C 完整处理、
    # B 错误可观测（不伪装成功）、C 不因 B 被跳过。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)  # A
        _seed_fixed_with_anchor(c, window_end_tod="22:00")                               # B
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)  # C
        b_round = c.rows[1]
        a_round = c.rows[0]
        c_round = c.rows[-1]
        restore = _fail_fixed_round_inserts(c, b_round["task_id"])
        result = planning.run_maintenance(ats(24, 23))
        restore()
        gen = result["generation"]
        assert [e["task_id"] for e in gen["errors"]] == [b_round["task_id"]]
        assert gen["errors"][0]["error"]
        # A / C 均完整处理：旧轮按冻结边界关闭、新轮正常生成
        for round_row in (a_round, c_round):
            assert (round_row["status"], round_row["closed_at"]) == (
                "timeout", planning._iso(at(24, 7)))
        fresh = [row for row in c.rows
                 if row["fixed_due_at"] == planning._iso(at(24, 7))]
        assert len(fresh) == 2
        assert all(row["status"] == "pending" for row in fresh)
        assert gen["created"] == 2
        # B 自己的轮按冻结边界关闭（清理在其 reconcile 内已先行执行）
        assert (b_round["status"], b_round["closed_at"]) == ("timeout", planning._iso(at(24, 20)))


# ── 五轮 Review：cleanup 失败语义 + partial create 排程兜底 ───────

def _fail_task_cursor_updates(c, task_id):
    """注入：指定任务的 planning_task cursor（generated_through）更新失败。
    返回还原函数。"""
    original_table = c.db.table

    def failing_table(name):
        query = original_table(name)
        if name == "planning_task":
            original_execute = query.execute

            def execute():
                if getattr(query, "action", None) == "update" and any(
                        key == "id" and value == task_id
                        for key, value in getattr(query, "filters", [])):
                    raise RuntimeError("cursor update failure")
                return original_execute()

            query.execute = execute
        return query

    c.db.table = failing_table
    return lambda: setattr(c.db, "table", original_table)


def test_cleanup_only_failure_propagates_and_blocks_completion():
    # 五轮 HIGH 必测 1/4（Codex 复现场景）：generation 成功、**finally 收尾
    # 清理**单独失败 → 整体调用失败（cleanup 异常可观察），后续完成操作
    # 不得继续——已过期 occurrence（fixed_expires_at = 9/26 06:00，now =
    # 9/29 07:00）不得被写成 completed、不写完成事实。
    with Context() as c:
        c.create("interval", at(23, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(26, 6))
        real_expire = planning._expire_fixed_rounds
        calls = {"n": 0}

        def flaky_expire(client, task, now):
            calls["n"] += 1
            if calls["n"] >= 2:  # 第一次（创建前）成功，第二次（finally）失败
                raise RuntimeError("cleanup boom")
            return real_expire(client, task, now)

        with mock.patch.object(planning, "_expire_fixed_rounds", side_effect=flaky_expire):
            with pytest.raises(RuntimeError):
                planning.complete_task_early(stale["task_id"], at(29, 7),
                                             idempotency_key="cleanup-fail")
        # 已过期 occurrence 未被写成 completed、未写完成事实
        by_due = {row["fixed_due_at"]: row for row in c.rows}
        expired = by_due[planning._iso(at(26, 6))]  # 出生即过期轮（cleanup 失败未关闭）
        assert expired["status"] == "pending"
        assert expired.get("handled_at") is None
        assert expired.get("closed_at") is None
        assert not any(row["source"] == "early" for row in c.rows)
        # 旧轮 9/23 由创建前清理正常关闭
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(26, 6)))


def test_cleanup_only_failure_observable_via_generate_due():
    # finally 收尾清理失败经单 task 隔离进入 errors（可观察）；该 task 的
    # 清理事实缺失不伪装成成功（出生即过期轮未被关闭也未伪造完成）。
    with Context() as c:
        c.create("interval", at(23, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        real_expire = planning._expire_fixed_rounds
        calls = {"n": 0}

        def flaky_expire(client, task, now):
            calls["n"] += 1
            if calls["n"] >= 2:  # 第一次（创建前）成功，第二次（finally）失败
                raise RuntimeError("cleanup boom")
            return real_expire(client, task, now)

        with mock.patch.object(planning, "_expire_fixed_rounds", side_effect=flaky_expire):
            result = planning.generate_due(at(29, 7))
        assert [e["task_id"] for e in result["errors"]] == [stale["task_id"]]
        assert result["errors"][0]["error"] == "RuntimeError"
        by_due = {row["fixed_due_at"]: row for row in c.rows}
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(26, 6)))
        assert by_due[planning._iso(at(26, 6))]["status"] == "pending"


def test_double_failure_keeps_generation_exception_primary(caplog):
    # 五轮 HIGH 必测 3：generation 失败 + cleanup 失败 → generation 异常
    # 为主异常继续传播（不被 cleanup 覆盖），cleanup 失败有完整日志，
    # 第一次（创建前）清理的成功结果保留。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        real_expire = planning._expire_fixed_rounds
        calls = {"n": 0}

        def flaky_expire(client, task, now):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise ValueError("cleanup boom")
            return real_expire(client, task, now)

        restore = _fail_fixed_round_inserts(c, stale["task_id"])
        with mock.patch.object(planning, "_expire_fixed_rounds", side_effect=flaky_expire):
            with caplog.at_level(logging.ERROR, logger="gateway.planning"):
                result = planning.generate_due(at(24, 23))
        restore()
        # generation 异常（RuntimeError）为主异常进入 errors，未被 cleanup
        #（ValueError）覆盖；cleanup 失败有完整日志。
        assert [e["task_id"] for e in result["errors"]] == [stale["task_id"]]
        assert result["errors"][0]["error"] == "RuntimeError"
        assert "固定到期清理失败" in caplog.text
        # 第一次（创建前）清理成功：旧轮已按冻结边界关闭
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 7)))


def test_partial_create_cursor_failure_still_triggers_recompute():
    # 五轮 MEDIUM 必测 A/D：INSERT 成功 → cursor 更新失败 → created 计数
    # 丢失但新行已真实持久化；errors 触发一次保守幂等 recompute——新
    # occurrence 不得永久保持 unassigned；对照任务不受影响。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        c.create("interval", at(23, 23), refresh_mode="fixed_interval",
                 interval_days=3)  # 对照任务：轴 9/23，失败 pass 无新事件
        control = c.rows[-1]
        assert control["fixed_expires_at"] == planning._iso(at(26, 23))
        restore = _fail_task_cursor_updates(c, stale["task_id"])
        result = planning.run_maintenance(at(24, 23))
        restore()
        gen = result["generation"]
        assert [e["task_id"] for e in gen["errors"]] == [stale["task_id"]]
        assert gen.get("created", 0) == 0  # partial create 计数丢失（诊断偏差）
        assert "generation_recompute" in result  # errors 触发保守幂等重算
        # 新 occurrence 真实存在且已得到排程（不永久 unassigned）
        fresh = next(row for row in c.rows
                     if row["fixed_due_at"] == planning._iso(at(24, 7)))
        assert fresh["status"] == "pending"
        assert fresh["est_start"] is not None and fresh["est_end"] is not None
        # 对照任务不受影响：无错误、旧轮保持开放
        assert all(e["task_id"] != control["task_id"] for e in gen["errors"])
        assert control["status"] == "pending"


def test_partial_create_second_maintenance_idempotent():
    # 五轮 MEDIUM 必测 B：故障解除后第二次 maintenance 幂等重试——不重复
    # 创建、已存在实例不因 created=0 再次漏排、排程结果保持。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        restore = _fail_task_cursor_updates(c, stale["task_id"])
        result = planning.run_maintenance(at(24, 23))
        restore()
        fresh = next(row for row in c.rows
                     if row["fixed_due_at"] == planning._iso(at(24, 7)))
        est = (fresh["est_start"], fresh["est_end"])
        assert fresh["est_start"] is not None
        result2 = planning.run_maintenance(at(24, 23, 30))
        assert result2["generation"]["created"] == 0
        assert "errors" not in result2["generation"]
        assert len(c.rows) == 2  # 不重复创建
        assert (fresh["est_start"], fresh["est_end"]) == est


def test_error_triggered_recompute_safe_without_new_rows(caplog):
    # 五轮 MEDIUM 必测 C：generation error 但实际没有新行（cleanup 单独
    # 失败）→ 保守 recompute 触发一次且无副作用——既有实例生命周期不被
    # 破坏。
    with Context() as c:
        c.create("interval", at(23, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        c.create("daily", at(24, 10), estimated_minutes=30)
        normal = c.rows[-1]
        with mock.patch.object(
                planning, "_expire_fixed_rounds",
                side_effect=RuntimeError("cleanup boom")):
            with caplog.at_level(logging.ERROR, logger="gateway.planning"):
                result = planning.run_maintenance(at(29, 7))
        gen = result["generation"]
        assert [e["task_id"] for e in gen["errors"]] == [stale["task_id"]]
        assert "generation_recompute" in result
        # 既有实例生命周期不被破坏：正常实例仍开放且排程完好
        assert normal["status"] == "pending"
        assert normal["est_start"] is not None
        assert stale["status"] == "pending"  # 清理失败的轮不被伪造关闭


# ── 六轮 Review：create_task 入口 recompute 语义统一 ──────────────

def test_create_task_partial_create_recompute_via_entry():
    # 六轮 MEDIUM 主测：create_task 同步补生成——9/22 首轮 INSERT 成功、
    # cursor 更新失败（其冻结边界 9/25 07:00 在未来 → 轮保持 pending）→
    # _generate_due_quietly 共享判断（errors 非空）触发同步幂等重算——
    # 首轮获得正常 est，不永久 unassigned。
    with Context() as c:
        restore = _fail_task_cursor_updates(c, 1)
        task = planning.create_task({
            "content": "喝中药", "task_type": "interval",
            "refresh_mode": "fixed_interval", "interval_days": 3,
            "estimated_minutes": 30,
            "refresh_anchor_at": planning._iso(at(22, 7)),
        }, at(24, 8))
        restore()
        assert task["id"] == 1
        rows = [row for row in c.rows if row["task_id"] == 1]
        # partial create：首轮 INSERT 成功（cursor 更新失败中止），9/25 等未来轮缺失
        assert len(rows) == 1
        fresh = rows[0]
        assert fresh["fixed_due_at"] == planning._iso(at(22, 7))
        assert fresh["fixed_expires_at"] == planning._iso(at(25, 7))
        assert fresh["status"] == "pending"
        # 共享判断触发同步重算：首轮获得正常 est（不永久 unassigned）
        assert fresh["est_start"] is not None and fresh["est_end"] is not None
        # 故障解除后 maintenance：幂等（不重复创建、排程保持）
        result2 = planning.run_maintenance(at(24, 8, 30))
        assert result2["generation"]["created"] == 0
        assert "errors" not in result2["generation"]
        assert len(c.rows) == 1
        assert (fresh["est_start"], fresh["est_end"]) == (
            rows[0]["est_start"], rows[0]["est_end"])


def test_create_task_normal_path_still_recomputes_once():
    # 正常 create_task：created>0 时仍正常排程、无重复 recompute 副作用。
    with Context() as c:
        task = planning.create_task({
            "content": "背单词", "task_type": "daily",
            "estimated_minutes": 30,
        }, at(24, 10))
        occ = c.rows[0]
        assert occ["task_id"] == task["id"]
        assert occ["est_start"] == planning._iso(at(24, 10))
        assert occ["est_end"] == planning._iso(at(24, 10, 30))
        assert len(c.rows) == 1


# ── 七轮 Review：complete_task_early 直接 reconcile 路径的排程恢复 ──

def test_complete_task_early_partial_create_recovers_scheduling():
    # 七轮 MEDIUM 必测（Codex 场景）：旧轮 9/21、下一轮 due 9/24 07:00、
    # 9/24 08:00 调用 complete_task_early；新轮 INSERT 成功、cursor 更新
    # 失败 → 原 reconcile 异常继续传播、不产生提前完成事实；新轮保持 open
    # 并经恢复 recompute 获得正常 est（不永久 unassigned）；故障解除后
    # maintenance 幂等、cursor 最终恢复。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        assert stale["fixed_expires_at"] == planning._iso(at(24, 7))
        restore = _fail_task_cursor_updates(c, stale["task_id"])
        with pytest.raises(RuntimeError):
            planning.complete_task_early(stale["task_id"], at(24, 8),
                                         idempotency_key="recover-1")
        restore()
        # 提前完成流程停止：不写 completed / handled_at / 完成事实、无 early 行
        assert not any(row["source"] == "early" for row in c.rows)
        assert not any(row["status"] == "completed" for row in c.rows)
        assert not any(row.get("handled_at") for row in c.rows)
        # 旧轮由创建前清理按冻结边界关闭（异常关闭，非 completed）
        assert (stale["status"], stale["closed_at"]) == ("timeout", planning._iso(at(24, 7)))
        # 新轮保持 open；恢复 recompute 使其获得正常 est
        fresh = next(row for row in c.rows
                     if row["fixed_due_at"] == planning._iso(at(24, 7)))
        assert fresh["status"] == "pending"
        assert fresh["est_start"] is not None and fresh["est_end"] is not None
        # 故障解除后 maintenance：幂等（created=0）、cursor 最终恢复
        result2 = planning.run_maintenance(at(24, 9))
        assert result2["generation"]["created"] == 0
        assert "errors" not in result2["generation"]
        assert len(c.rows) == 2
        task_row = c.db.rows["planning_task"][0]
        assert task_row["refresh_generated_through"] == "2026-09-24"


def test_complete_task_early_recovery_recompute_safe_without_new_rows():
    # reconcile 很早失败（无新行）→ 恢复 recompute 仍允许一次且无副作用：
    # in_progress / fixed 所有权实例不乱动，异常继续传播、不写完成事实。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        planning.set_occurrence_status(stale["id"], {"status": "in_progress"}, at(22, 8))
        est_before = stale["est_start"]
        with mock.patch.object(
                planning, "_expire_fixed_rounds",
                side_effect=RuntimeError("cleanup boom")):
            with pytest.raises(RuntimeError):
                planning.complete_task_early(stale["task_id"], at(24, 8),
                                             idempotency_key="recover-2")
        # 无新行、无完成事实；in_progress 实例不被乱动（不重排、est 保留）
        assert stale["status"] == "in_progress"
        assert stale["est_start"] == est_before
        assert stale.get("handled_at") is None
        assert not any(row["source"] == "early" for row in c.rows)
        assert len(c.rows) == 1


def test_complete_task_early_double_failure_keeps_reconcile_exception_primary(caplog):
    # 七轮 MEDIUM 双重失败：partial create（cursor 失败）+ 恢复 recompute
    # 也失败 → 原 reconcile 异常（RuntimeError）保持主异常，恢复失败有完整
    # 日志（不覆盖、不静默）；新轮保持 open、不写完成事实。
    with Context() as c:
        c.create("interval", at(21, 7), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]
        restore = _fail_task_cursor_updates(c, stale["task_id"])
        with mock.patch.object(planning, "recompute_today",
                               side_effect=ValueError("recompute boom")):
            with caplog.at_level(logging.ERROR, logger="gateway.planning"):
                with pytest.raises(RuntimeError) as caught:
                    planning.complete_task_early(stale["task_id"], at(24, 8),
                                                 idempotency_key="recover-3")
        restore()
        assert type(caught.value).__name__ == "RuntimeError"
        assert "提前完成恢复重算失败" in caplog.text
        fresh = next(row for row in c.rows
                     if row["fixed_due_at"] == planning._iso(at(24, 7)))
        assert fresh["status"] == "pending"
        assert fresh.get("est_start") is None  # 恢复也失败：排程恢复未达成（如实）
        assert not any(row["source"] == "early" for row in c.rows)
