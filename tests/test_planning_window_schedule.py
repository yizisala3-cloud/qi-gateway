"""批次 4 定向测试：自动排程窗口约束 + 排程冲突（一期规范 §14 / §15 / §17.4 / §18 / §19）。

覆盖：双端窗口（A）/ only-earliest（B）/ only-latest（C）/ 无窗口兼容（D）/
固定槽避让 + 窗口（E）/ 列表顺序权威（F）/ 窗口不整体占槽（G）/ 零自由度
预锚定不重排（H）/ 中空包络（I）/ in_progress 免疫（J）/ 历史实例兼容（K）/
创建校验与排程同源（§十二）/ today 看板读取时派生冲突。

冲突是派生结果（哪个待办 / 哪项约束 / 为什么）：不落库、无生命周期状态；
任一冲突 → 本轮重算整体不持久化，既有 est 保留不清空。窗口可行性全部经
planning_window.window_feasible（与创建校验同源，仅 effective cursor 不同）。
"""

from datetime import datetime, timezone, timedelta

import pytest

from gateway import planning
from test_planning import _setup
from test_planning_phase1b import Context, at

CST = timezone(timedelta(hours=8))


def _cst(day, hour, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=CST)


def iso(dt):
    return planning._iso(dt)


def cycle_of(now):
    return planning._current_cycle(now).key.isoformat()


def seed_task(client, task_id, *, estimated_minutes=30, **kw):
    client.rows["planning_task"].append({
        "id": task_id, "content": kw.get("content", "任务"),
        "task_type": kw.get("task_type", "daily"), "refresh_mode": "daily",
        "refresh_enabled": True, "time_mode": "duration",
        "estimated_minutes": estimated_minutes,
        "window_start_tod": kw.get("window_start_tod"),
        "window_end_tod": kw.get("window_end_tod"),
        "est_start_tod": None, "est_end_tod": None, "is_fixed": False,
        "deadline_tod": None, "deadline_end_tod": None,
        "is_hollow": kw.get("is_hollow", False),
        "hollow_start_minutes": kw.get("hollow_start_minutes"),
        "hollow_wait_minutes": kw.get("hollow_wait_minutes"),
        "hollow_end_minutes": kw.get("hollow_end_minutes"),
        "is_active": True, "cursor_date": None, "next_due": None,
    })


def seed_occ(client, occ_id, task_id, *, now, sort_order=10, status="pending",
             phase=None, round_key=None, phase_group=None, planned_minutes=30,
             planned_wait_minutes=None, window_start_at=None, window_end_at=None,
             est_start=None, est_end=None, is_fixed=False,
             estimated_time_source="unassigned", fixed_source=None,
             schedule_managed=True):
    cycle = cycle_of(now)
    client.rows["planning_occurrence"].append({
        "id": occ_id, "task_id": task_id, "for_date": cycle,
        "round_key": round_key or f"cycle:{cycle}", "schedule_date": cycle,
        "display_cycle_date": cycle, "display_reason": "initial",
        "phase": phase, "phase_group": phase_group,
        "est_start": est_start, "est_end": est_end, "nominal_start": None,
        "actual_start": None, "actual_end": None, "status": status,
        "planned_minutes": planned_minutes,
        "planned_wait_minutes": planned_wait_minutes,
        "sort_order": sort_order, "is_fixed": is_fixed, "is_limited": False,
        "estimated_time_source": estimated_time_source,
        "fixed_source": fixed_source, "schedule_managed": schedule_managed,
        "window_start_at": window_start_at, "window_end_at": window_end_at,
        "source": "schedule", "closed_at": None,
    })
    return client.rows["planning_occurrence"][-1]


def assert_single_conflict(result, occ_id, *, phase=None, constraint="window_end"):
    """任一冲突 → 整体不持久化；冲突三要素齐全（§19）。"""
    assert result["updated"] == 0
    assert len(result["conflicts"]) == 1
    conflict = result["conflicts"][0]
    assert set(conflict) == {"occurrence_id", "task_id", "phase", "constraint", "reason"}
    assert conflict["occurrence_id"] == occ_id
    assert conflict["constraint"] == constraint
    assert conflict["phase"] == phase
    assert conflict["reason"]
    return conflict


# ── A. 双端基本窗口（window 03:00→05:00，duration 60） ────────────────

def _seed_both_ends(client, now, *, est_start=None, est_end=None, minutes=60):
    seed_task(client, 1, estimated_minutes=minutes)
    return seed_occ(
        client, 1, 1, now=now, planned_minutes=minutes,
        window_start_at=iso(_cst(20, 3)), window_end_at=iso(_cst(20, 5)),
        est_start=est_start, est_end=est_end,
        estimated_time_source="automatic" if est_start else "unassigned",
    )


def test_both_end_window_cursor_before_window_pulls_to_window_start():
    now = _cst(20, 2)
    client, ctx = _setup()
    with ctx():
        _seed_both_ends(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert result["updated"] == 1
        row = client.rows["planning_occurrence"][0]
        assert row["est_start"] == iso(_cst(20, 3))
        assert row["est_end"] == iso(_cst(20, 4))


def test_both_end_window_cursor_inside_window_schedules_from_cursor():
    now = _cst(20, 3, 30)
    client, ctx = _setup()
    with ctx():
        _seed_both_ends(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 3, 30)), iso(_cst(20, 4, 30)))


def test_both_end_window_end_touch_is_legal():
    now = _cst(20, 4)
    client, ctx = _setup()
    with ctx():
        _seed_both_ends(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 4)), iso(_cst(20, 5)))


def test_both_end_window_cursor_too_late_conflicts_and_keeps_previous_est():
    now = _cst(20, 4, 1)
    client, ctx = _setup()
    with ctx():
        _seed_both_ends(client, now)
        result = planning.recompute_today(now)
        conflict = assert_single_conflict(result, 1)
        assert "剩余空间不足" in conflict["reason"]
        # 冲突零写入：est 保持未排程状态，不写违反窗口的起止。
        row = client.rows["planning_occurrence"][0]
        assert row["est_start"] is None and row["est_end"] is None


def test_conflict_preserves_last_successful_schedule_and_window_facts():
    now = _cst(20, 4, 31)
    client, ctx = _setup()
    with ctx():
        row = _seed_both_ends(
            client, now,
            est_start=iso(_cst(20, 3)), est_end=iso(_cst(20, 4)),
        )
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1)
        # 最近一次成功排程结果保留不清空；窗口冻结事实不得被排程改写。
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 3)), iso(_cst(20, 4)))
        assert (row["window_start_at"], row["window_end_at"]) == (
            iso(_cst(20, 3)), iso(_cst(20, 5)))
        assert row["estimated_time_source"] == "automatic"


# ── B. only-earliest（只有最早开始 03:00） ────────────────────────────

def _seed_earliest_only(client, now, minutes=60):
    seed_task(client, 1, estimated_minutes=minutes)
    return seed_occ(client, 1, 1, now=now, planned_minutes=minutes,
                    window_start_at=iso(_cst(20, 3)))


def test_only_earliest_pulls_start_to_window_start():
    now = _cst(20, 2)
    client, ctx = _setup()
    with ctx():
        _seed_earliest_only(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 3)), iso(_cst(20, 4)))


def test_only_earliest_after_start_schedules_from_cursor():
    now = _cst(20, 4)
    client, ctx = _setup()
    with ctx():
        _seed_earliest_only(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 4)), iso(_cst(20, 5)))


def test_only_earliest_has_no_implicit_end_and_may_cross_day():
    # 只有下界：不存在隐式 boundary 截止，不凭空补最晚完成（§15）。
    now = _cst(20, 4, 30)
    client, ctx = _setup()
    with ctx():
        _seed_earliest_only(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 4, 30)), iso(_cst(20, 5, 30)))


# ── C. only-latest（只有最晚完成 05:00） ──────────────────────────────

def _seed_latest_only(client, now, minutes=60):
    seed_task(client, 1, estimated_minutes=minutes)
    return seed_occ(client, 1, 1, now=now, planned_minutes=minutes,
                    window_end_at=iso(_cst(20, 5)))


def test_only_latest_schedules_from_cursor_when_it_fits():
    now = _cst(20, 3)
    client, ctx = _setup()
    with ctx():
        _seed_latest_only(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 3)), iso(_cst(20, 4)))


def test_only_latest_end_touch_is_legal():
    now = _cst(20, 4)
    client, ctx = _setup()
    with ctx():
        _seed_latest_only(client, now)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 4)), iso(_cst(20, 5)))


def test_only_latest_cursor_too_late_conflicts():
    now = _cst(20, 4, 1)
    client, ctx = _setup()
    with ctx():
        _seed_latest_only(client, now)
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1)
        row = client.rows["planning_occurrence"][0]
        assert row["est_start"] is None and row["est_end"] is None


# ── D. 无窗口：既有自动排程行为不变（§15 跨日照旧） ───────────────────

def test_no_window_keeps_existing_behavior():
    now = _cst(20, 14, 7)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 14, 7)), iso(_cst(20, 15, 7)))


def test_no_window_still_crosses_day_without_conflict():
    now = _cst(20, 23, 30)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 23, 30)), iso(_cst(21, 0, 30)))


# ── E. 固定槽避让 + 窗口（§14.3 / §六） ───────────────────────────────

def _seed_with_fixed_slot(client, now, *, window_end, minutes=60, sort_fixed=20):
    seed_task(client, 1, estimated_minutes=minutes)
    seed_task(client, 2, estimated_minutes=minutes, time_mode="explicit")
    seed_occ(client, 1, 1, now=now, sort_order=10, planned_minutes=minutes,
             window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, window_end)))
    seed_occ(client, 2, 2, now=now, sort_order=sort_fixed, planned_minutes=120,
             est_start=iso(_cst(20, 18, 30)), est_end=iso(_cst(20, 19, 30)),
             is_fixed=True, estimated_time_source="rule", fixed_source="rule")


def test_windowed_task_avoids_fixed_slot_inside_window():
    # §六示例：window 18:00→22:00，固定槽 18:30→19:30，cursor 18:00 → 19:30→20:30。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        _seed_with_fixed_slot(client, now, window_end=22)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 19, 30)), iso(_cst(20, 20, 30)))
        # 固定槽不动。
        assert (rows[2]["est_start"], rows[2]["est_end"]) == (
            iso(_cst(20, 18, 30)), iso(_cst(20, 19, 30)))


def test_windowed_task_conflicts_when_avoidance_leaves_no_room():
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        _seed_with_fixed_slot(client, now, window_end=20)
        result = planning.recompute_today(now)
        conflict = assert_single_conflict(result, 1)
        # 避让前 18:00 起剩余 120 分钟本可容纳 → 冲突原因是避让后越界。
        assert "固定槽避让后" in conflict["reason"]
        rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
        assert rows[1]["est_start"] is None
        assert (rows[2]["est_start"], rows[2]["est_end"]) == (
            iso(_cst(20, 18, 30)), iso(_cst(20, 19, 30)))


# ── F. 列表顺序仍然是权威（§14.1 / §七，禁止回溯与自动重排） ──────────

def test_list_order_is_authoritative_late_task_conflicts():
    now = _cst(20, 14)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=120)
        seed_task(client, 2, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, sort_order=10, planned_minutes=120)
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=60,
                 window_start_at=iso(_cst(20, 15)), window_end_at=iso(_cst(20, 16)))
        result = planning.recompute_today(now)
        # A 在前宽松、占掉空间；B 窗口紧 → B 冲突，系统不得交换 A/B。
        assert_single_conflict(result, 2)
        rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
        assert rows[1]["est_start"] is None  # 整体不持久化
        assert rows[2]["est_start"] is None


def test_swapped_list_order_resolves_without_conflict():
    now = _cst(20, 14)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=120)
        seed_task(client, 2, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, sort_order=20, planned_minutes=120)
        seed_occ(client, 2, 2, now=now, sort_order=10, planned_minutes=60,
                 window_start_at=iso(_cst(20, 15)), window_end_at=iso(_cst(20, 16)))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
        assert (rows[2]["est_start"], rows[2]["est_end"]) == (
            iso(_cst(20, 15)), iso(_cst(20, 16)))
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 16)), iso(_cst(20, 18)))


# ── G. 窗口不是占位槽（§14.3：只有最终 est 区间进槽） ─────────────────

def test_window_is_not_a_placeholder_slot():
    now = _cst(20, 14)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_task(client, 2, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, sort_order=10, planned_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=60)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 19)))
        # B 排在 A 的窗口内空隙（19:00–20:00）：整个窗口若被当作槽，
        # B 会被推到 22:00 之后。
        assert (rows[2]["est_start"], rows[2]["est_end"]) == (
            iso(_cst(20, 19)), iso(_cst(20, 20)))


# ── H. 零自由度窗口：批次 3 预锚定 rule 固定，不重排、不作冲突（§四） ──

def test_zero_degree_preanchored_round_stays_fixed_and_blocks_slot():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=60,
                 window_start_tod="20:00", window_end_tod="21:00")
        c.create("daily", at(24, 10, 5), estimated_minutes=60)
        fixed = next(row for row in c.rows if row.get("is_fixed"))
        flex = next(row for row in c.rows if not row.get("is_fixed"))
        assert fixed["estimated_time_source"] == "rule"
        assert fixed["est_start"] == iso(_cst(24, 20))
        flex["sort_order"] = 5
        fixed["sort_order"] = 10
        result = planning.recompute_today(at(24, 10, 10))
        assert result["conflicts"] == []
        # 预锚定实例不重排、窗口事实不改写；灵活待办按顺序排在其前。
        assert (fixed["est_start"], fixed["est_end"]) == (iso(_cst(24, 20)), iso(_cst(24, 21)))
        assert (fixed["window_start_at"], fixed["window_end_at"]) == (
            iso(_cst(24, 20)), iso(_cst(24, 21)))
        assert (flex["est_start"], flex["est_end"]) == (iso(_cst(24, 10, 10)), iso(_cst(24, 11, 10)))


def test_zero_degree_preanchored_round_keeps_slot_for_later_items():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=60,
                 window_start_tod="20:00", window_end_tod="21:00")
        c.create("daily", at(24, 10, 5), estimated_minutes=60)
        fixed = next(row for row in c.rows if row.get("is_fixed"))
        flex = next(row for row in c.rows if not row.get("is_fixed"))
        # 灵活待办在固定槽之后：游标越过固定位置（既有 greedy 语义）。
        fixed["sort_order"] = 5
        flex["sort_order"] = 10
        result = planning.recompute_today(at(24, 10, 10))
        assert result["conflicts"] == []
        assert (fixed["est_start"], fixed["est_end"]) == (iso(_cst(24, 20)), iso(_cst(24, 21)))
        assert (flex["est_start"], flex["est_end"]) == (iso(_cst(24, 21)), iso(_cst(24, 22)))


# ── I. 中空待办：包络约束、两阶段各占槽、等待不占槽（§17.4） ──────────

def _seed_hollow(client, now, *, window_end=(20, 30), wait_minutes=60,
                 extra=None, start_sort=10, normal_sort=20, end_sort=30):
    """中空任务：开始 30 + 等待 60 + 结束 30，窗口 18:00→20:30（150 分钟）。"""
    seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
              hollow_wait_minutes=wait_minutes, hollow_end_minutes=30)
    seed_occ(client, 1, 1, now=now, sort_order=start_sort, phase="start",
             round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
             window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, *window_end)))
    if extra:
        seed_task(client, 2, estimated_minutes=extra[0])
        seed_occ(client, 2, 2, now=now, sort_order=normal_sort,
                 planned_minutes=extra[0])
    seed_occ(client, 3, 1, now=now, sort_order=end_sort, phase="end",
             round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
             planned_wait_minutes=wait_minutes,
             window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, *window_end)))
    return client.rows["planning_occurrence"]


def test_hollow_envelope_fits_and_stages_anchor_within_window():
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        rows = {row["id"]: row for row in _seed_hollow(client, now)}
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        start_row, end_row = rows[1], rows[3]
        assert (start_row["est_start"], start_row["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 30)))
        # 结束阶段锚定 = 开始阶段预计结束 + 等待（结束阶段行自带快照）。
        assert (end_row["est_start"], end_row["est_end"]) == (
            iso(_cst(20, 19, 30)), iso(_cst(20, 20)))


def test_hollow_wait_gap_is_usable_by_other_tasks():
    # 等待不占普通排程槽：中间普通待办排入 18:30–19:30，结束阶段仍按锚点落位。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        rows = {row["id"]: row for row in _seed_hollow(client, now, extra=(60,))}
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        start_row, normal, end_row = rows[1], rows[2], rows[3]
        assert (start_row["est_start"], start_row["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 30)))
        assert (normal["est_start"], normal["est_end"]) == (
            iso(_cst(20, 18, 30)), iso(_cst(20, 19, 30)))
        assert (end_row["est_start"], end_row["est_end"]) == (
            iso(_cst(20, 19, 30)), iso(_cst(20, 20)))


def test_hollow_end_phase_shifted_by_wait_stays_within_window():
    # 中间普通待办把结束阶段顶到锚点之后（cursor 20:00），恰好触及窗口终点。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        rows = {row["id"]: row for row in _seed_hollow(client, now, extra=(90,))}
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        end_row = rows[3]
        assert (end_row["est_start"], end_row["est_end"]) == (
            iso(_cst(20, 20)), iso(_cst(20, 20, 30)))


def test_fixed_slot_inside_hollow_wait_is_allowed():
    # 固定槽落在等待区间内：中空包络不占用等待，固定槽照常存在，无冲突。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=60, hollow_end_minutes=30)
        seed_task(client, 2, estimated_minutes=30)
        rows = client.rows["planning_occurrence"]
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 20, 30)))
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=30,
                 est_start=iso(_cst(20, 18, 30)), est_end=iso(_cst(20, 19)),
                 is_fixed=True, estimated_time_source="rule", fixed_source="rule")
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 20, 30)))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        start_row, fixed, end_row = rows[0], rows[1], rows[2]
        assert (start_row["est_start"], start_row["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 30)))
        assert (fixed["est_start"], fixed["est_end"]) == (
            iso(_cst(20, 18, 30)), iso(_cst(20, 19)))
        assert (end_row["est_start"], end_row["est_end"]) == (
            iso(_cst(20, 19, 30)), iso(_cst(20, 20)))


def test_hollow_envelope_precheck_conflicts_at_start_stage():
    # 修复轮 MEDIUM-2：cursor 19:30——开始阶段放置前，包络预判已确定
    # 19:30 + 120 > 20:30 → 开始阶段立即冲突，不放置、游标不动；
    # 结束阶段不因开始阶段失败而回退旧锚点继续排（修复轮 MEDIUM-3）。
    now = _cst(20, 19, 30)
    client, ctx = _setup()
    with ctx():
        rows = {row["id"]: row for row in _seed_hollow(client, now)}
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1, phase="start")
        assert "包络" in result["conflicts"][0]["reason"]
        # 整体不持久化：两个阶段均不写入 est。
        for row in rows.values():
            assert row["est_start"] is None


# ── J. in_progress 免疫：已开始实例不因重算移动（§十一 / 不变量 38） ──

def test_in_progress_instance_is_not_rescheduled_or_conflicted():
    # 剩余窗口 30 分钟 < 耗时 60 分钟：若重排将冲突；执行中实例豁免，
    # est 保留为既有执行事实并继续作为固定槽。
    now = _cst(20, 21, 30)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        started = seed_occ(client, 1, 1, now=now, sort_order=10, status="in_progress",
                           planned_minutes=60,
                           window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)),
                           est_start=iso(_cst(20, 18)), est_end=iso(_cst(20, 19)))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert result["updated"] == 0
        assert (started["est_start"], started["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 19)))


# ── K. 历史 / 兼容：无窗口旧实例与 legacy explicit 保持既有行为（§十四） ──

def test_legacy_row_without_window_keeps_old_scheduling():
    now = _cst(20, 14, 7)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60, estimated_time_source="automatic")
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (iso(_cst(20, 14, 7)), iso(_cst(20, 15, 7)))


def test_legacy_explicit_occurrence_keeps_fixed_slot_behavior():
    now = _cst(20, 14)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_task(client, 2, estimated_minutes=120)
        seed_occ(client, 1, 1, now=now, sort_order=10, planned_minutes=60)
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=120,
                 est_start=iso(_cst(20, 20)), est_end=iso(_cst(20, 22)),
                 is_fixed=True, estimated_time_source="rule", fixed_source="rule")
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        rows = {row["id"]: row for row in client.rows["planning_occurrence"]}
        # legacy explicit 固定实例按自己的历史时间事实生活，不被重排、不判冲突。
        assert (rows[2]["est_start"], rows[2]["est_end"]) == (
            iso(_cst(20, 20)), iso(_cst(20, 22)))
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 14)), iso(_cst(20, 15)))


# ── §十二：创建校验与排程同源 —— 同一可行性函数、不同 effective cursor ──

def test_creation_feasible_but_later_cursor_conflicts_same_domain_math():
    with Context() as c:
        c.create("daily", at(20, 2), estimated_minutes=60,
                 window_start_tod="03:00", window_end_tod="05:00")
        occ = c.rows[0]
        # 创建校验（reference 02:00）：窗口剩余 120 分钟 ≥ 60 → 接受并冻结。
        assert (occ["window_start_at"], occ["window_end_at"]) == (
            iso(_cst(20, 3)), iso(_cst(20, 5)))
        # 排程 cursor 02:00：合法放置 03:00–04:00。
        result = planning.recompute_today(at(20, 2))
        assert result["conflicts"] == []
        assert occ["est_start"] == iso(_cst(20, 3))
        # cursor 推进到 04:31：同一领域数学回答「现在塞不下了」→ 冲突，非矛盾。
        conflict = planning.recompute_today(at(20, 4, 31))
        assert conflict["updated"] == 0
        assert conflict["conflicts"][0]["occurrence_id"] == occ["id"]
        assert "剩余空间不足" in conflict["conflicts"][0]["reason"]
        # 既有 est 保留，窗口事实未被「修正」。
        assert occ["est_start"] == iso(_cst(20, 3))
        assert (occ["window_start_at"], occ["window_end_at"]) == (
            iso(_cst(20, 3)), iso(_cst(20, 5)))


# ── today 看板：读取时只读派生冲突（不落库、无持久化冲突缓存） ────────

def test_today_board_derives_conflicts_readonly():
    now = _cst(20, 4, 1)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60,
                 window_start_at=iso(_cst(20, 3)), window_end_at=iso(_cst(20, 5)))
        board = planning.today_board(now)
        assert len(board["conflicts"]) == 1
        assert board["conflicts"][0]["occurrence_id"] == 1
        assert board["conflicts"][0]["constraint"] == "window_end"
        # 只读派生：实例行零写入。
        row = client.rows["planning_occurrence"][0]
        assert row["est_start"] is None and row["est_end"] is None


def test_today_board_without_conflicts_returns_empty_list():
    now = _cst(20, 14)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60)
        board = planning.today_board(now)
        assert board["conflicts"] == []


# ── 修复轮（2026-09-28 Review MEDIUM-1）：等待标记成功判定 ────────────
# 成功 = conflicts 为空（与 updated 无关：updated=0 的合法完成同属成功）；
# 存在冲突则整体未生效，等待标记保留。不新增状态、不新增重试机制。

def _seed_conflicting_window(client, now):
    seed_task(client, 1, estimated_minutes=60)
    seed_occ(client, 1, 1, now=now, planned_minutes=60,
             window_start_at=iso(_cst(20, 3)), window_end_at=iso(_cst(20, 5)))


def test_manual_recompute_conflict_keeps_waiting_mark():
    now = _cst(20, 4, 1)
    client, ctx = _setup()
    with ctx():
        _seed_conflicting_window(client, now)
        planning.request_recompute("reorder", now)
        assert planning.get_recompute_state(now)["pending"]
        result = planning.trigger_recompute(now)
        assert result["conflicts"]
        # 冲突 = 重算未成功：等待标记保留（requested_at 原样）。
        state = planning.get_recompute_state(now)
        assert state["pending"]
        assert state["requested_at"] == planning._iso(now)


def test_maintenance_auto_recompute_conflict_keeps_waiting_mark():
    now = _cst(20, 4, 1)
    client, ctx = _setup()
    with ctx():
        _seed_conflicting_window(client, now)
        requested = now - timedelta(minutes=31)
        client.rows["planning_recompute_state"][0].update({
            "requested_at": planning._iso(requested), "reason": "reorder"})
        results = planning.run_maintenance(now)
        assert results["auto_recompute"]["conflicts"]
        row = client.rows["planning_recompute_state"][0]
        assert row["requested_at"] == planning._iso(requested)


def test_recompute_without_conflict_clears_mark_even_when_nothing_changed():
    # 无冲突且 updated=0（合法完成、无条目需要修改）也属于成功：标记清除。
    now = _cst(20, 2)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60,
                 window_start_at=iso(_cst(20, 3)), window_end_at=iso(_cst(20, 5)),
                 est_start=iso(_cst(20, 3)), est_end=iso(_cst(20, 4)),
                 estimated_time_source="automatic")
        planning.request_recompute("reorder", now - timedelta(minutes=31))
        result = planning.trigger_recompute(now)
        assert result["updated"] == 0
        assert result["conflicts"] == []
        assert not planning.get_recompute_state(now)["pending"]


def test_recompute_with_updates_and_no_conflict_clears_mark():
    now = _cst(20, 2)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60,
                 window_start_at=iso(_cst(20, 3)), window_end_at=iso(_cst(20, 5)))
        planning.request_recompute("reorder", now - timedelta(minutes=31))
        result = planning.trigger_recompute(now)
        assert result["updated"] == 1
        assert result["conflicts"] == []
        assert not planning.get_recompute_state(now)["pending"]


# ── 修复轮（2026-09-28 Review MEDIUM-2/3）：hollow 包络预判与失败依赖 ──

def test_hollow_start_envelope_precheck_conflicts_without_polluting_cursor():
    # Codex 场景：hollow 30+120+30，window 18:00→22:00，cursor 19:30；
    # B（30min，only-latest 20:00）排在 hollow 之后。
    # 开始阶段放置前包络预判：19:30 + 180 > 22:00 → 立即冲突：不放置、
    # 不推进游标、不注册槽；B 不被制造假冲突，可排 19:30→20:00。
    now = _cst(20, 19, 30)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=120, hollow_end_minutes=30)
        seed_task(client, 2, estimated_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=30,
                 window_end_at=iso(_cst(20, 20)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 planned_wait_minutes=120,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        # 唯一根因冲突在开始阶段；B 不是冲突且拿到未污染的合法位置。
        assert [c["occurrence_id"] for c in result.conflicts] == [1]
        assert result.conflicts[0]["phase"] == "start"
        assert "包络" in result.conflicts[0]["reason"]
        assert 1 not in result.placed and 3 not in result.placed
        assert result.placed[2] == (_cst(20, 19, 30), _cst(20, 20))
        # 重算级：任一冲突整轮零写入，B 不落库（§19.1）。
        result = planning.recompute_today(now)
        assert result["updated"] == 0
        assert [c["occurrence_id"] for c in result["conflicts"]] == [1]
        assert occs[2]["est_start"] is None and occs[1]["est_start"] is None


def test_hollow_start_failure_blocks_end_from_falling_back_to_old_est():
    # Codex 场景（修复 3）：hollow 30+120+10，window 18:00→22:00；
    # start 带旧 est 18:00→18:30；cursor 21:45；B 10min latest 22:00。
    # start 包络预判判死（21:45 + 160 > 22:00）→ end 不得回退旧 start est
    # 伪造本轮依赖；cursor 不被 hollow 推进；B 正常排 21:45→21:55；
    # 数据库整轮零写入，start 旧 est 原样保留。
    now = _cst(20, 21, 45)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=120, hollow_end_minutes=10)
        seed_task(client, 2, estimated_minutes=10)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)),
                 est_start=iso(_cst(20, 18)), est_end=iso(_cst(20, 18, 30)),
                 estimated_time_source="automatic")
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=10,
                 window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 planned_wait_minutes=120,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        assert [c["occurrence_id"] for c in result.conflicts] == [1]
        assert 1 not in result.placed and 3 not in result.placed
        assert result.placed[2] == (_cst(20, 21, 45), _cst(20, 21, 55))
        result = planning.recompute_today(now)
        assert result["updated"] == 0
        assert occs[1]["est_start"] == iso(_cst(20, 18))  # 旧 est 保留不清空
        assert occs[3]["est_start"] is None
        assert occs[2]["est_start"] is None


def test_hollow_start_own_check_failure_also_blocks_end():
    # 修复 3 另一路径：包络预判通过（floor 18:00 + 100 ≤ 22:00），但固定槽
    # 18:00–21:40 把开始阶段避让到 21:40，自身 30min 越过 22:00 → 开始阶段
    # 冲突；结束阶段不得回退旧 est；B 使用未被 hollow 污染的游标。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=60, hollow_end_minutes=10)
        seed_task(client, 2, estimated_minutes=220)
        seed_task(client, 3, estimated_minutes=10)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=220,
                 est_start=iso(_cst(20, 18)), est_end=iso(_cst(20, 21, 40)),
                 is_fixed=True, estimated_time_source="rule", fixed_source="rule")
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 4, 3, now=now, sort_order=40, planned_minutes=10)
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        assert [c["occurrence_id"] for c in result.conflicts] == [1]
        assert "固定槽避让后" in result.conflicts[0]["reason"]
        assert 1 not in result.placed and 3 not in result.placed
        # B 在固定槽之后正常落位（游标推进来自固定槽，而非失败的 hollow）。
        assert result.placed[4] == (_cst(20, 21, 40), _cst(20, 21, 50))
        result = planning.recompute_today(now)
        assert result["updated"] == 0
        assert occs[3]["est_start"] is None


def test_hollow_end_still_uses_legitimate_frozen_start_anchor():
    # 区分场景：start 不参与本轮重排（in_progress，est 为合法冻结事实）→
    # 结束阶段锚定照常使用其 est（H3 机制不变），本轮无冲突。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=60, hollow_end_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 status="in_progress",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)),
                 est_start=iso(_cst(20, 18)), est_end=iso(_cst(20, 18, 30)),
                 estimated_time_source="automatic")
        seed_occ(client, 3, 1, now=now, sort_order=20, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        assert result.conflicts == []
        assert result.placed[3] == (_cst(20, 19, 30), _cst(20, 20))
        result = planning.recompute_today(now)
        assert result["updated"] == 1
        # in_progress 的 est 保持既有执行事实，不被重排、不被清除。
        assert occs[1]["est_start"] == iso(_cst(20, 18))
        assert occs[3]["est_start"] == iso(_cst(20, 19, 30))


# ── 二轮修复（2026-09-28 Review HIGH + 2 MEDIUM）：frozen end 区分 ─────
# hollow start 按同轮 end 的可重排性区分依赖：
#   movable end → 完整包络可行性（floor 早期判死 + 避让后 placed 前复检）；
#   frozen end（fixed / in_progress / manual / deferred）→ 真实锚点连接校验
#   （开始阶段预计结束 + 等待 ≤ 冻结结束阶段起点，§17.2/§19），不假设其移动。

def _seed_hollow_with_end(client, now, *, wait_minutes, end_status="pending",
                          end_is_fixed=False, end_fixed_source=None,
                          end_est=None, end_planned_minutes=30):
    """hollow 30 + wait + 30，双端窗口 18:00→22:00；end 可为冻结事实。"""
    seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
              hollow_wait_minutes=wait_minutes, hollow_end_minutes=30)
    seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
             round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
             window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
    seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
             round_key="cycle:2026-09-20", phase_group="g1",
             planned_minutes=end_planned_minutes, planned_wait_minutes=wait_minutes,
             status=end_status, is_fixed=end_is_fixed, fixed_source=end_fixed_source,
             estimated_time_source="automatic" if end_est else "unassigned",
             window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)),
             est_start=end_est[0] if end_est else None,
             est_end=end_est[1] if end_est else None)
    return {row["id"]: row for row in client.rows["planning_occurrence"]}


def test_frozen_fixed_end_wait_insufficient_conflicts():
    # HIGH 场景：hollow 30+120+30，window 18:00→22:00；end 已固定 20:30→21:00；
    # cursor 19:00。start 19:00→19:30 后仅剩 60 分钟等待（需 120）→ 冲突；
    # 不持久化 start，不移动 frozen end。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120, end_is_fixed=True,
            end_fixed_source="rule",
            end_est=(iso(_cst(20, 20, 30)), iso(_cst(20, 21))))
        result = planning.recompute_today(now)
        conflict = assert_single_conflict(result, 1, phase="start",
                                          constraint="hollow_end_anchor")
        assert "等待" in conflict["reason"]
        assert rows[1]["est_start"] is None
        assert rows[3]["est_start"] == iso(_cst(20, 20, 30))


def test_frozen_fixed_end_exact_connection_succeeds():
    # 合法对照：end 已固定 21:30→21:40，wait 120。start 19:00→19:30 后恰好
    # 21:30 连接（等号合法，§17.2）→ start 正常排入，frozen end 不动。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120, end_is_fixed=True,
            end_fixed_source="rule",
            end_est=(iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40))))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert result["updated"] == 1
        assert rows[1]["est_start"] == iso(_cst(20, 19))
        assert rows[3]["est_start"] == iso(_cst(20, 21, 30))


def test_frozen_in_progress_end_wait_insufficient_conflicts():
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        _seed_hollow_with_end(
            client, now, wait_minutes=120, end_status="in_progress",
            end_est=(iso(_cst(20, 20, 30)), iso(_cst(20, 21))))
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1, phase="start",
                               constraint="hollow_end_anchor")


def test_frozen_in_progress_end_connection_succeeds():
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120, end_status="in_progress",
            end_est=(iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40))))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert rows[1]["est_start"] == iso(_cst(20, 19))
        assert rows[3]["est_start"] == iso(_cst(20, 21, 30))


def test_frozen_manual_end_connects():
    # 抽样（manual）：人工固定 end 合法连接 → start 正常排入。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120, end_is_fixed=True,
            end_fixed_source="manual",
            end_est=(iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40))))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert rows[1]["est_start"] == iso(_cst(20, 19))
        assert rows[3]["est_start"] == iso(_cst(20, 21, 30))


def test_frozen_deferred_end_wait_insufficient_conflicts():
    # 抽样（deferred）：延后 end 为合法冻结事实，等待不足同样冲突。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        _seed_hollow_with_end(
            client, now, wait_minutes=120, end_status="deferred",
            end_est=(iso(_cst(20, 20, 30)), iso(_cst(20, 21))))
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1, phase="start",
                               constraint="hollow_end_anchor")


def test_frozen_end_connection_checked_even_without_window():
    # 冻结 end 连接校验不依赖窗口存在（§17.2 无窗口同样适用）。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=120, hollow_end_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30)
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 planned_wait_minutes=120, is_fixed=True, fixed_source="rule",
                 estimated_time_source="automatic",
                 est_start=iso(_cst(20, 20, 30)), est_end=iso(_cst(20, 21)))
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1, phase="start",
                               constraint="hollow_end_anchor")


def test_frozen_end_real_interval_wins_over_snapshot():
    # MEDIUM：end 冻结区间 21:30→21:40（有效耗时 10 分钟），planned_minutes=60。
    # 冻结 end 以真实锚点为权威：start 19:00→19:30 + 等待 120 恰好连接、
    # 整体 21:40 前结束；不得按 60 分钟快照预测假包络（30+120+60=210 →
    # 22:30 超窗）制造假冲突。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120, end_is_fixed=True,
            end_fixed_source="manual",
            end_est=(iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40))),
            end_planned_minutes=60)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert rows[1]["est_start"] == iso(_cst(20, 19))
        assert rows[3]["est_start"] == iso(_cst(20, 21, 30))


def test_post_avoidance_envelope_recheck_blocks_cursor_pollution():
    # MEDIUM：hollow 30+120+30，window 18:00→22:00，cursor 18:00，固定槽
    # 18:00→20:00。floor 预判通过（18:00+180=21:00 ≤ 22:00），但避让后
    # start=20:00、包络最早 23:00 必然超窗 → 开始阶段冲突，不得占
    # 20:00→20:30；B（30min，latest 20:30）正常排 20:00→20:30。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=120, hollow_end_minutes=30)
        seed_task(client, 2, estimated_minutes=120)
        seed_task(client, 3, estimated_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=120,
                 est_start=iso(_cst(20, 18)), est_end=iso(_cst(20, 20)),
                 is_fixed=True, estimated_time_source="rule", fixed_source="rule")
        seed_occ(client, 4, 3, now=now, sort_order=30, planned_minutes=30,
                 window_end_at=iso(_cst(20, 20, 30)))
        seed_occ(client, 3, 1, now=now, sort_order=40, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 planned_wait_minutes=120,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        assert [c["occurrence_id"] for c in result.conflicts] == [1]
        assert "固定槽避让后" in result.conflicts[0]["reason"]
        assert "包络" in result.conflicts[0]["reason"]
        assert 1 not in result.placed and 3 not in result.placed
        assert result.placed[4] == (_cst(20, 20), _cst(20, 20, 30))
        result = planning.recompute_today(now)
        assert result["updated"] == 0
        assert occs[4]["est_start"] is None and occs[1]["est_start"] is None


def test_movable_end_envelope_still_works_after_recheck_addition():
    # 回归确认：movable start/end 正常中空排程（等待可被普通待办填入）
    # 不受二次复检影响。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        rows = {row["id"]: row for row in _seed_hollow(client, now, extra=(60,))}
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 30)))
        assert (rows[2]["est_start"], rows[2]["est_end"]) == (
            iso(_cst(20, 18, 30)), iso(_cst(20, 19, 30)))
        assert (rows[3]["est_start"], rows[3]["est_end"]) == (
            iso(_cst(20, 19, 30)), iso(_cst(20, 20)))


# ── 三轮修复（2026-09-28 Review HIGH）：有效耗时单一权威统一 ──────────
# 预判（窗口可行性 / 包络）与实际落位必须使用同一耗时来源：
# 有效 est 区间事实优先 → planned_minutes 快照 → 既有 fallback（_duration_of）。

def test_normal_movable_row_places_by_effective_est_interval():
    # 普通可重排实例：有效 est 区间 60 分钟优先于 planned_minutes=30，
    # 重算后仍按 60 分钟安排（不得按快照缩成 30 分钟）。
    now = _cst(20, 14, 7)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=30)
        seed_occ(client, 1, 1, now=now, planned_minutes=30,
                 est_start=iso(_cst(20, 14)), est_end=iso(_cst(20, 15)),
                 estimated_time_source="automatic")
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (
            iso(_cst(20, 14, 7)), iso(_cst(20, 15, 7)))


def test_normal_movable_row_places_by_effective_est_interval_reverse():
    # 反向：有效 est 区间 30 分钟优先于 planned_minutes=60。
    now = _cst(20, 14, 7)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=60)
        seed_occ(client, 1, 1, now=now, planned_minutes=60,
                 est_start=iso(_cst(20, 14)), est_end=iso(_cst(20, 14, 30)),
                 estimated_time_source="automatic")
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert (row["est_start"], row["est_end"]) == (
            iso(_cst(20, 14, 7)), iso(_cst(20, 14, 37)))


def test_hollow_movable_end_effective_interval_10_wins_over_planned_60():
    # Codex 场景 A：movable end 有效 est 区间 = 10 分钟（21:30→21:40），
    # planned_minutes = 60。end 有效耗时 = 10：start 19:00→19:30 + 等待
    # 120 → end 21:30→21:40 合法落位；不得按 planned=60 误报越窗冲突。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120,
            end_est=(iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40))),
            end_planned_minutes=60)
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert result["updated"] == 1  # end 新位置与旧 est 相同 → 不重复写
        assert rows[1]["est_start"] == iso(_cst(20, 19))
        assert (rows[3]["est_start"], rows[3]["est_end"]) == (
            iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40)))


def test_hollow_movable_end_effective_interval_60_conflicts_over_window():
    # Codex 场景 B：movable end 有效 est 区间 = 60 分钟，planned_minutes = 10。
    # start 19:00→19:30 + 等待 120 → end 最早 21:30，需 60 分钟 →
    # 21:30→22:30 越过 22:00 → 冲突；不得偷偷按 planned=10 缩成 21:40，
    # 既有有效耗时事实（60 分钟区间）不被 updated 覆盖。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        rows = _seed_hollow_with_end(
            client, now, wait_minutes=120,
            end_est=(iso(_cst(20, 21, 30)), iso(_cst(20, 22, 30))),
            end_planned_minutes=10)
        result = planning.recompute_today(now)
        assert_single_conflict(result, 1, phase="start", constraint="window_end")
        assert "包络" in result["conflicts"][0]["reason"]
        # 整轮零写入：60 分钟有效区间原样保留，未被缩短覆盖。
        assert (rows[3]["est_start"], rows[3]["est_end"]) == (
            iso(_cst(20, 21, 30)), iso(_cst(20, 22, 30)))
        assert rows[1]["est_start"] is None


# ── 四轮修复（2026-09-28 Review HIGH + MEDIUM）：精确耗时与无耗时 skip ──

def test_second_precision_window_check_uses_exact_duration():
    # Codex 场景 A：普通 movable 实例有效 est 区间 = 10 分 30 秒，
    # cursor 19:00，latest 19:10。实际结束 19:10:30 越过最晚完成 → 冲突；
    # 不得把检查截成 10 分钟而误判可行、写入 19:10:30。
    now = _cst(20, 19)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=10)
        seed_occ(client, 1, 1, now=now, planned_minutes=10,
                 window_end_at=iso(_cst(20, 19, 10)),
                 est_start=iso(_cst(20, 19)),
                 est_end=iso(_cst(20, 19, 10, second=30)),
                 estimated_time_source="automatic")
        result = planning.recompute_today(now)
        conflict = assert_single_conflict(result, 1, constraint="window_end")
        assert "10 分 30 秒" in conflict["reason"]
        row = client.rows["planning_occurrence"][0]
        assert row["est_start"] == iso(_cst(20, 19))  # 旧 est 零覆盖


def test_hollow_second_precision_envelope_conflicts_before_placement():
    # Codex 场景 B：hollow start 30 + wait 120 + movable end 有效耗时
    # 10 分 30 秒，cursor 19:20，window_end 22:00。完整包络 160 分 30 秒 →
    # 19:20 起最早 22:00:30 必然超窗 → 开始阶段真正占位前判死，
    # 不推进 cursor、不注册槽。
    now = _cst(20, 19, 20)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=120, hollow_end_minutes=10)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 planned_wait_minutes=120,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)),
                 est_start=iso(_cst(20, 21, 30)),
                 est_end=iso(_cst(20, 21, 40, second=30)),
                 estimated_time_source="automatic")
        result = planning.recompute_today(now)
        conflict = assert_single_conflict(result, 1, phase="start",
                                          constraint="window_end")
        assert "包络" in conflict["reason"]
        assert "30 秒" in conflict["reason"]
        rows = client.rows["planning_occurrence"]
        # 整轮零写入：开始阶段从未持久化；end 的既有 10 分 30 秒有效区间原样保留。
        assert rows[0]["est_start"] is None
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 21, 30)), iso(_cst(20, 21, 40, second=30)))


def test_second_precision_hollow_failure_does_not_pollute_b():
    # Codex 场景 C：同场景后接 B（30 分钟，latest 19:50）。hollow 在真正
    # 占位前已判死、cursor 未被推进 → B 正常排 19:20→19:50。
    now = _cst(20, 19, 20)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, is_hollow=True, hollow_start_minutes=30,
                  hollow_wait_minutes=120, hollow_end_minutes=10)
        seed_task(client, 2, estimated_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=30,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)))
        seed_occ(client, 4, 2, now=now, sort_order=20, planned_minutes=30,
                 window_end_at=iso(_cst(20, 19, 50)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 planned_wait_minutes=120,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 22)),
                 est_start=iso(_cst(20, 21, 30)),
                 est_end=iso(_cst(20, 21, 40, second=30)),
                 estimated_time_source="automatic")
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        assert [c["occurrence_id"] for c in result.conflicts] == [1]
        assert 1 not in result.placed and 3 not in result.placed
        assert result.placed[4] == (_cst(20, 19, 20), _cst(20, 19, 50))
        result = planning.recompute_today(now)
        assert result["updated"] == 0
        assert occs[4]["est_start"] is None


def test_duration_less_row_is_skipped_not_defaulted():
    # 四轮修复 MEDIUM：无任何真实耗时来源（无 est 区间、无 planned_minutes、
    # 任务无 estimated_minutes）→ 保持旧 scheduler skip 语义：不排、不
    # 脑补 30 分钟。正式产品中该形状已被创建/编辑边界拒绝（预计耗时必填），
    # 此为排程层防御性保护。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=None)
        seed_occ(client, 1, 1, now=now, planned_minutes=None)
        result = planning.recompute_today(now)
        assert result["updated"] == 0
        assert result["conflicts"] == []
        row = client.rows["planning_occurrence"][0]
        assert row["est_start"] is None and row["est_end"] is None


def test_duration_less_row_does_not_pollute_cursor_for_b():
    # 无耗时 A 被 skip；B（30 分钟，latest 18:30）正常排 18:00→18:30，
    # A 不得凭空占掉这半小时制造 B 的假冲突。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=None)
        seed_task(client, 2, estimated_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, planned_minutes=None)
        seed_occ(client, 2, 2, now=now, sort_order=20, planned_minutes=30,
                 window_end_at=iso(_cst(20, 18, 30)))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert result["updated"] == 1
        rows = client.rows["planning_occurrence"]
        assert rows[0]["est_start"] is None
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 30)))


# ── 四轮修复：边界不变量——预计耗时不可经编辑入口清空（user 产品事实：
# 预计耗时是可自动排程待办的必填信息；前端从不提交空值，null PATCH 属
# 后端校验缺失）──

def test_patch_cannot_clear_estimated_minutes_on_daily():
    # Codex 复现路径 1（daily）：PATCH estimated_minutes=null 曾绕过创建
    # 必填检查，随后生成无 planned_minutes 快照的轮次。边界修复后拒绝。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"estimated_minutes": None}, at(24, 11))
        assert error.value.status_code == 400
        assert "预计耗时不能清空" in str(error.value)
        stored = next(row for row in c.db.rows["planning_task"] if row["id"] == task["id"])
        assert stored["estimated_minutes"] == 30


def test_patch_cannot_clear_estimated_minutes_on_future_once():
    # Codex 复现路径 2（未来 once 首次生成前清空耗时）：同样在边界拒绝。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-30",
                        estimated_minutes=30)
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"estimated_minutes": None}, at(24, 11))
        assert error.value.status_code == 400
        stored = next(row for row in c.db.rows["planning_task"] if row["id"] == task["id"])
        assert stored["estimated_minutes"] == 30


def test_patch_cannot_clear_estimated_minutes_on_idle_reactivate():
    # Codex 复现路径 3（inactive idle 清空耗时后重新启用）：同样拒绝；
    # 有效值更新不受影响。
    with Context() as c:
        task = c.create("idle", at(24, 10), estimated_minutes=30)
        planning.update_task(task["id"], {"is_active": False}, at(24, 10, 30))
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(
                task["id"], {"is_active": True, "estimated_minutes": None}, at(24, 11))
        assert error.value.status_code == 400
        assert "预计耗时不能清空" in str(error.value)
        # 有效值更新照常。
        updated = planning.update_task(task["id"], {"is_active": True}, at(24, 11, 30))
        assert updated["is_active"] is True


def test_patch_keeps_accepting_valid_estimated_minutes_update():
    # 边界修复只堵「清空」：合法新值（任务规则编辑，影响未来轮次）照常。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        updated = planning.update_task(task["id"], {"estimated_minutes": 45}, at(24, 11))
        assert updated["estimated_minutes"] == 45


# ── 五轮修复（2026-09-28 Review MEDIUM）：hollow 包络预判同守来源门禁 ──

def test_hollow_movable_end_without_duration_source_skips_envelope_precheck():
    # Codex 场景：hollow start 10min、wait 60min；end 无 est、无
    # planned_minutes、任务无 estimated_minutes、无 hollow_end_minutes；
    # window 18:00→19:20。end 无来源 → 包络预判不得经 _duration_of 默认
    # 30 分钟脑补成 100 分钟提前 conflict：start 正常 18:00→18:10，
    # end 在主循环阶段自行 skip。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=None, is_hollow=True,
                  hollow_start_minutes=10, hollow_wait_minutes=60,
                  hollow_end_minutes=None)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=None,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert result["updated"] == 1
        rows = client.rows["planning_occurrence"]
        assert (rows[0]["est_start"], rows[0]["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 10)))
        assert rows[1]["est_start"] is None  # end 无来源 → 主循环 skip


def test_hollow_movable_end_without_source_does_not_pollute_b():
    # B 对照：同场景后接 B（30 分钟，latest 18:40）。假包络若存在会提前
    # conflict 且 start 不放置，B 位置不受影响；修复后 B 正常 18:10→18:40。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=None, is_hollow=True,
                  hollow_start_minutes=10, hollow_wait_minutes=60,
                  hollow_end_minutes=None)
        seed_task(client, 2, estimated_minutes=30)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        seed_occ(client, 4, 2, now=now, sort_order=20, planned_minutes=30,
                 window_end_at=iso(_cst(20, 18, 40)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=None,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        occs = {row["id"]: row for row in client.rows["planning_occurrence"]}
        tasks = {row["id"]: row for row in client.rows["planning_task"]}
        result = planning.compute_schedule(list(occs.values()), tasks, now)
        assert result.conflicts == []
        assert result.placed[4] == (_cst(20, 18, 10), _cst(20, 18, 40))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        assert occs[1]["est_start"] == iso(_cst(20, 18))
        assert occs[4]["est_start"] == iso(_cst(20, 18, 10))


def test_hollow_movable_end_with_real_est_still_joins_envelope():
    # 正向对照：end 有真实 est 区间（10 分钟）→ 来源门禁放行，包络预判
    # 照常生效（80 分钟恰满窗口 18:00→19:20，等号合法）：start 18:00→18:10、
    # end 19:10→19:20。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=None, is_hollow=True,
                  hollow_start_minutes=10, hollow_wait_minutes=60,
                  hollow_end_minutes=10)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)),
                 est_start=iso(_cst(20, 19, 10)), est_end=iso(_cst(20, 19, 20)),
                 estimated_time_source="automatic")
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        rows = client.rows["planning_occurrence"]
        assert (rows[0]["est_start"], rows[0]["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 10)))
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 19, 10)), iso(_cst(20, 19, 20)))


def test_hollow_start_without_duration_source_still_skipped():
    # 额外确认：movable start 自己无耗时来源时同样在主循环 skip
    #（来源门禁对全部可重排行生效，hollow 阶段不豁免）；end 仍按自身
    # 来源落位——start 无锚点可回退时按旧语义从 cursor 排程。
    now = _cst(20, 18)
    client, ctx = _setup()
    with ctx():
        seed_task(client, 1, estimated_minutes=None, is_hollow=True,
                  hollow_start_minutes=None, hollow_wait_minutes=60,
                  hollow_end_minutes=10)
        seed_occ(client, 1, 1, now=now, sort_order=10, phase="start",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=None,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        seed_occ(client, 3, 1, now=now, sort_order=30, phase="end",
                 round_key="cycle:2026-09-20", phase_group="g1", planned_minutes=10,
                 planned_wait_minutes=60,
                 window_start_at=iso(_cst(20, 18)), window_end_at=iso(_cst(20, 19, 20)))
        result = planning.recompute_today(now)
        assert result["conflicts"] == []
        rows = client.rows["planning_occurrence"]
        assert rows[0]["est_start"] is None  # start 无来源 → skip
        assert (rows[1]["est_start"], rows[1]["est_end"]) == (
            iso(_cst(20, 18)), iso(_cst(20, 18, 10)))


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__]))
