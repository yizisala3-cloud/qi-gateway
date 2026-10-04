"""每日旧轮周期收场（清单 #32，2026-10-04 user 口裁决）。

口径：新每日轮照常生成的周期里，旧未处理每日轮（pending / in_progress /
deferred / partial）自动关闭标记「已超时」，历史与已有事实保留；这类
「周期死亡」不进「待处理」，作为关闭记录出现在收场周期「已完成」与
全部待办。暂停刷新 / 关闭每日刷新期间不生成新轮、旧轮保持开放可继续
处理。窗口 sweep 超时（用户明确的最晚完成）保持既有待处理展示。
"""

from gateway import planning

from gateway.planning_common import PLANNING_BOUNDARY_STATE_KEY

from tests.support.planning_context import Context, at

OPEN = ("pending", "in_progress", "deferred", "partial")


def _open_rows(c):
    return [row for row in c.rows if row["status"] in OPEN]


def test_three_consecutive_cycles_keep_single_open_round():
    # 连续三个有效规划周期：同一普通每日任务任意时刻只有一条开放轮次，
    # 旧轮在下一周期开始时按其所属周期终点收场。
    with Context() as c:
        c.create("daily", at(22, 21))
        planning.generate_due(at(23, 6))
        planning.generate_due(at(24, 6))
        planning.generate_due(at(25, 6))
        open_rows = _open_rows(c)
        assert [row["round_key"] for row in open_rows] == ["cycle:2026-09-25"]
        assert open_rows[0]["status"] == "pending"
        closed = {row["round_key"]: row for row in c.rows
                  if row["round_key"] != "cycle:2026-09-25"}
        # closed_at = 旧轮所属周期的自然终点（死亡时刻，非扫描执行时刻）
        assert closed["cycle:2026-09-22"]["status"] == "timeout"
        assert closed["cycle:2026-09-22"]["closed_at"] == at(23, 6).isoformat()
        assert closed["cycle:2026-09-23"]["closed_at"] == at(24, 6).isoformat()
        assert closed["cycle:2026-09-24"]["closed_at"] == at(25, 6).isoformat()
        # 收场周期归属：display 随关闭顺延到收场周期（carryover）
        assert closed["cycle:2026-09-22"]["display_cycle_date"] == "2026-09-23"
        assert closed["cycle:2026-09-22"]["display_reason"] == "carryover"
        # 关闭是异常收场：不写处理 / 完成事实
        assert all(row.get("handled_at") is None for row in closed.values())
        assert all(row.get("actual_end") is None for row in closed.values())


def test_old_rounds_close_in_every_open_state_with_facts_preserved():
    with Context() as c:
        tasks = [c.create("daily", at(22, 21)) for _ in range(4)]
        first_by_task = {}
        for task in tasks:
            row = next(row for row in c.rows if row["task_id"] == task["id"])
            first_by_task[task["id"]] = row
        planning.set_occurrence_status(
            first_by_task[tasks[0]["id"]]["id"], {"status": "in_progress"}, at(22, 22))
        planning.set_occurrence_status(
            first_by_task[tasks[1]["id"]]["id"],
            {"status": "deferred", "est_start": planning._iso(at(22, 22, 30))}, at(22, 22))
        planning.set_occurrence_status(
            first_by_task[tasks[2]["id"]]["id"],
            {"status": "partial", "partial_note": "做了一半"}, at(22, 22))
        # tasks[3] 保持 pending
        planning.set_occurrence_status(
            first_by_task[tasks[0]["id"]]["id"], {"status": "completed"}, at(22, 23))
        planning.generate_due(at(23, 6))
        by_key = {}
        for task in tasks:
            rows = [row for row in c.rows if row["task_id"] == task["id"]]
            assert len(rows) == 2, "每任务一条旧轮 + 一条新轮"
            old, new = rows
            assert old["schedule_date"] == "2026-09-22"
            assert new["schedule_date"] == "2026-09-23" and new["status"] == "pending"
            by_key[task["id"]] = old
        # 已完成的旧轮保持既有事实，不被周期收场改写
        assert by_key[tasks[0]["id"]]["status"] == "completed"
        # in_progress / deferred / partial 的旧轮全部收场，已有事实保留
        assert by_key[tasks[1]["id"]]["status"] == "timeout"
        assert by_key[tasks[2]["id"]]["status"] == "timeout"
        assert by_key[tasks[2]["id"]]["partial_at"] is not None
        assert by_key[tasks[2]["id"]]["partial_note"] == "做了一半"
        assert by_key[tasks[2]["id"]]["handled_at"] is None
        assert by_key[tasks[3]["id"]]["status"] == "timeout"


def test_hollow_daily_round_closes_both_phases_together():
    with Context() as c:
        c.create("daily", at(22, 21), is_hollow=True, hollow_start_minutes=10,
                 hollow_wait_minutes=30, hollow_end_minutes=10)
        planning.generate_due(at(23, 6))
        open_rows = _open_rows(c)
        assert len(open_rows) == 2
        assert {row["phase"] for row in open_rows} == {"start", "end"}
        assert all(row["round_key"] == "cycle:2026-09-23" for row in open_rows)
        closed = [row for row in c.rows if row["status"] == "timeout"]
        assert {row["phase"] for row in closed} == {"start", "end"}
        assert all(row["closed_at"] == at(23, 6).isoformat() for row in closed)


def test_paused_or_disabled_refresh_keeps_old_round_open():
    with Context() as c:
        c.create("daily", at(22, 21))
        # 关闭每日刷新：不生成新轮、旧轮保持开放并继续顺延展示
        planning.set_cycle_settings({"daily_refresh_enabled": False}, at(22, 22))
        planning.generate_due(at(23, 6))
        assert len(_open_rows(c)) == 1
        assert _open_rows(c)[0]["status"] == "pending"
        assert _open_rows(c)[0]["display_cycle_date"] == "2026-09-23"
        # 任务级暂停刷新：同样不收场、不生成
        planning.set_cycle_settings({"daily_refresh_enabled": True}, at(23, 7))
        c.db.rows["planning_task"][0]["refresh_enabled"] = False
        planning.generate_due(at(24, 6))
        assert len(_open_rows(c)) == 1
        assert _open_rows(c)[0]["schedule_date"] == "2026-09-22"
        # 恢复刷新：新轮生成，旧轮按其周期终点收场
        c.db.rows["planning_task"][0]["refresh_enabled"] = True
        planning.generate_due(at(24, 7))
        open_rows = _open_rows(c)
        assert [row["round_key"] for row in open_rows] == ["cycle:2026-09-24"]
        old = next(row for row in c.rows if row["schedule_date"] == "2026-09-22")
        assert old["status"] == "timeout"
        assert old["closed_at"] == at(23, 6).isoformat()


def test_cycle_death_enters_done_not_attention_and_scheduling():
    with Context() as c:
        c.create("daily", at(22, 21))
        planning.generate_due(at(23, 6))
        board = planning.today_board(at(23, 12))
        # 进度中只有当前轮；待处理不含周期死亡；已完成含收场记录
        assert [item["round_key"] for item in board["progress"]] == ["cycle:2026-09-23"]
        assert board["attention"] == []
        assert [item["schedule_date"] for item in board["done"]] == ["2026-09-22"]
        # 排程输入一致：重算只覆盖开放实例（收场行保留其开放期间的历史
        # est，不被收场后的重算改写或重排）
        planning.recompute_today(at(23, 12))
        scheduled = [row for row in c.rows
                     if row["schedule_date"] == "2026-09-23" and row.get("est_start")]
        assert len(scheduled) == 1
        assert scheduled[0]["est_start"] == at(23, 12).isoformat()
        closed = next(row for row in c.rows if row["schedule_date"] == "2026-09-22")
        assert closed["est_start"] == at(22, 21).isoformat()


def test_window_timeout_daily_round_keeps_attention_entry():
    # 窗口 sweep 超时（用户明确的最晚完成）保持既有待处理展示，与周期
    # 死亡区分：closed_at == window_end_at。
    with Context() as c:
        c.create("daily", at(21, 7), window_start_tod="08:00", window_end_tod="09:00")
        planning.sweep_timeouts(at(21, 9, 1))
        board = planning.today_board(at(21, 10))
        assert len(board["attention"]) == 1
        assert board["attention"][0]["schedule_date"] == "2026-09-21"
        # 下一周期新轮照常生成；被 sweep 的旧轮已在窗口终点收场
        result = planning.generate_due(at(22, 6))
        assert result["created"] == 1
        assert result["timed_out"] == 0
        board = planning.today_board(at(22, 12))
        assert [item["round_key"] for item in board["progress"]] == ["cycle:2026-09-22"]
        assert len(board["attention"]) == 1
        assert board["attention"][0]["status"] == "timeout"


def test_legacy_accumulated_overlap_converges_on_next_maintenance():
    # 存量 bug 状态：旧轮已顺延进当前周期（display=今天）且仍开放，与新轮
    # 并存。下一次维护按同一口径收场旧轮，看板恢复单实例。
    with Context() as c:
        c.create("daily", at(22, 21))
        planning.generate_due(at(23, 6))
        old = next(row for row in c.rows if row["schedule_date"] == "2026-09-22")
        # 手工还原修复前的重叠状态（开放 + 已顺延到当前周期）
        old["status"] = "pending"
        old["closed_at"] = None
        old["display_cycle_date"] = "2026-09-23"
        old["display_reason"] = "carryover"
        board = planning.today_board(at(23, 12))
        assert len(board["progress"]) == 2  # 复现重叠
        planning.generate_due(at(23, 13))
        board = planning.today_board(at(23, 14))
        assert len(board["progress"]) == 1
        assert board["progress"][0]["round_key"] == "cycle:2026-09-23"
        assert old["status"] == "timeout"
        assert old["closed_at"] == at(23, 6).isoformat()


def test_after_completion_and_once_carryover_unaffected():
    # once / after_completion 的合法顺延不回归：非 daily 模式无周期收场。
    with Context() as c:
        c.create("once", at(22), target_date=None)
        c.create("interval", at(22), refresh_mode="after_completion", interval_days=3)
        planning.generate_due(at(25))
        open_rows = _open_rows(c)
        assert len(open_rows) == 2
        assert all(row["status"] == "pending" for row in open_rows)
        assert open_rows[0]["display_reason"] == "carryover"

# ── R2（残留 B）：离线恢复的窗口截止保持窗口超时口径，与入口顺序无关 ──

def test_offline_recovery_window_death_identical_both_orders():
    # 9/21 07:00 创建（冻结窗口 08:00–09:00），窗口越过前离线，9/23 06:00
    # 恢复：无论先生成后 sweep，还是先 sweep 后生成，旧轮都必须在窗口
    # 终点（9/21 09:00）收场并保留「待处理」入口，closed_at 不随入口顺序。
    for sweep_first in (False, True):
        with Context() as c:
            c.create("daily", at(21, 7), window_start_tod="08:00", window_end_tod="09:00")
            if sweep_first:
                planning.sweep_timeouts(at(23, 6))
                planning.generate_due(at(23, 6, 1))
            else:
                planning.generate_due(at(23, 6))
                planning.sweep_timeouts(at(23, 6, 1))
            old = next(row for row in c.rows if row["schedule_date"] == "2026-09-21")
            assert old["status"] == "timeout"
            assert old["closed_at"] == at(21, 9).isoformat()
            board = planning.today_board(at(23, 12))
            assert [item["round_key"] for item in board["progress"]] == ["cycle:2026-09-23"]
            assert [item["schedule_date"] for item in board["attention"]] == ["2026-09-21"]
            assert board["done"] == []


def test_repeated_maintenance_window_death_stable():
    # 重复维护对窗口死亡幂等：closed_at 不被二次改写、不重复计数。
    with Context() as c:
        c.create("daily", at(21, 7), window_start_tod="08:00", window_end_tod="09:00")
        first = planning.generate_due(at(23, 6))
        assert first["timed_out"] == 1
        second = planning.generate_due(at(23, 6, 30))
        assert second["created"] == 0 and second["timed_out"] == 0
        old = next(row for row in c.rows if row["schedule_date"] == "2026-09-21")
        assert old["closed_at"] == at(21, 9).isoformat()


def test_window_end_equal_cycle_end_keeps_attention():
    # 窗口终点恰为周期终点（跨午夜 22:00–06:00，boundary 06:00）：保持
    # 既有保守口径——按窗口超时进「待处理」。
    with Context() as c:
        c.create("daily", at(21, 7), window_start_tod="22:00", window_end_tod="06:00")
        old = c.rows[0]
        assert old["window_end_at"] == at(22, 6).isoformat()  # 周期 9/21 的自然终点
        planning.generate_due(at(23, 6))
        assert old["closed_at"] == at(22, 6).isoformat()
        board = planning.today_board(at(23, 12))
        assert [item["schedule_date"] for item in board["attention"]] == ["2026-09-21"]


# ── R5（残留 C）：过渡结束后的收场按持久化记录还原实际周期终点 ──

def test_absorbed_transition_death_uses_actual_cycle_end():
    # 边界 06:00 → 9/23 05:00 改为 02:00：9/22 冻结周期延伸到 9/24 02:00，
    # 9/23 被吸收。9/24 02:00 收场时 transition 已不再生效，死亡时刻必须
    # 仍还原为实际周期终点 9/24 02:00，而不是按新边界回算的 9/23 02:00。
    with Context() as c:
        c.create("daily", at(22, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "02:00"}, at(23, 5))
        state = planning.get_cycle_settings(at(23, 6))
        assert state["pending_boundary"]["spanning_key"] == "2026-09-22"
        assert state["cycle_end"] == at(24, 2).isoformat()  # 权威周期终点
        result = planning.generate_due(at(24, 2))
        assert result["created"] == 1  # 9/24 新周期正常生成
        old = next(row for row in c.rows if row["schedule_date"] == "2026-09-22")
        assert old["status"] == "timeout"
        assert old["closed_at"] == at(24, 2).isoformat()


def test_consecutive_boundary_changes_death_uses_actual_end():
    # 连续两次边界修改（02:00 未生效前再改 04:00）：冻结段 9/22 的实际
    # 终点随最新修改延伸到 9/24 04:00，收场 closed_at 与之一致。
    with Context() as c:
        c.create("daily", at(22, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "02:00"}, at(23, 5))
        planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(23, 10))
        state = planning.get_cycle_settings(at(23, 11))
        assert state["pending_boundary"]["spanning_key"] == "2026-09-22"
        assert state["cycle_end"] == at(24, 4).isoformat()
        result = planning.generate_due(at(24, 4))
        assert result["created"] == 1
        old = next(row for row in c.rows if row["schedule_date"] == "2026-09-22")
        assert old["status"] == "timeout"
        assert old["closed_at"] == at(24, 4).isoformat()


def test_pause_across_transition_closes_at_actual_end_after_resume():
    # 暂停穿越过渡、恢复后第一次生成收场：死亡时刻同样是实际周期终点。
    with Context() as c:
        task = c.create("daily", at(22, 7))
        planning.update_task(task["id"], {"refresh_enabled": False}, at(22, 8))
        planning.set_cycle_settings({"refresh_boundary_time": "02:00"}, at(23, 5))
        planning.generate_due(at(23, 12))  # 暂停期间：不生成、不收场
        assert len(c.rows) == 1
        assert c.rows[0]["status"] == "pending"
        planning.update_task(task["id"], {"refresh_enabled": True}, at(24, 1))
        planning.generate_due(at(24, 2))
        old = c.rows[0]
        assert old["status"] == "timeout"
        assert old["closed_at"] == at(24, 2).isoformat()


def test_absorbed_day_skip_after_completed_transition_then_new_change():
    # 第一次过渡（06:00→02:00，9/22 冻结段延伸到 9/24 02:00，9/23 吸收）
    # 真正走完且吸收转入永久登记后，再次修改（02:00→04:00，冻结段变为
    # 9/25）：旧 9/22 轮的名义终点落在被吸收的 9/23 上，死亡时刻必须按
    # 吸收登记前移到下一个真实周期起点 9/24 02:00。
    with Context() as c:
        task = c.create("daily", at(22, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "02:00"}, at(23, 5))
        planning.update_task(task["id"], {"refresh_enabled": False}, at(23, 6))
        planning.generate_due(at(24, 2))  # 第一次过渡走完（暂停期间旧轮保持开放）
        planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(25, 10))
        state = planning.get_cycle_settings(at(25, 11))
        assert state["pending_boundary"]["spanning_key"] == "2026-09-25"
        # 第一次过渡走完后，其吸收事实在下一次修改时转入永久登记（B1）
        raw_state = c.settings[PLANNING_BOUNDARY_STATE_KEY]
        assert "2026-09-23" in (raw_state.get("absorbed") or [])
        planning.update_task(task["id"], {"refresh_enabled": True}, at(26, 3))
        planning.generate_due(at(26, 4))
        old = next(row for row in c.rows if row["schedule_date"] == "2026-09-22")
        assert old["status"] == "timeout"
        assert old["closed_at"] == at(24, 2).isoformat()
