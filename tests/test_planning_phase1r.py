"""Phase 1R Repair 回归测试（独立 Review 修复项 + 2026-09-25 补充需求）。

覆盖：
- 边界过渡状态模型：跨越周期冻结、连续修改、重启重建、吸收周期不补生成
- 每日待办只做当前周期恢复，不补离线历史轮次（最新确认口径）
- 生命周期：超时实例不复活、重新安排=新建单次待办、closed_at/handled_at 语义
- 提前完成先按正常生命周期补跑生成与到期清理
- 自动重算配置持续生效（默认只在配置缺失时使用）
- 部分完成的排程语义
"""

from datetime import timedelta
from unittest import mock

from gateway import planning
from test_planning_phase1b import Context, at


class RecalcContext(Context):
    """与 Context 相同，但保留真实的 request_recompute（重算等待测试用）。"""

    def __init__(self):
        super().__init__()
        self.patches = [
            p for p in self.patches
            if getattr(p, "attribute", None) != "request_recompute"
        ]


def test_second_boundary_change_during_window_keeps_current_cycle_frozen():
    # HIGH #2：A→B 后、生效前 B→C，当前已成立的周期（9/25）身份不变。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(25, 6))
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(25, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "05:00"}, at(26, 7))
        pending = planning.get_cycle_settings(at(26, 7, 30))["pending_boundary"]
        assert pending["spanning_key"] == "2026-09-25"
        assert pending["previous_time"] == "06:00"
        # 9/26 07:30 仍属于被延长的 9/25 周期（而不是普通算术下的 9/26）
        assert planning.today_board(at(26, 7, 30))["date"] == "2026-09-25"
        assert planning.generate_due(at(26, 7, 30))["created"] == 0
        # 生效点：9/27 05:00（C 的第一次出现）
        assert planning.today_board(at(27, 4))["date"] == "2026-09-25"
        assert planning.today_board(at(27, 5, 30))["date"] == "2026-09-27"


def test_triple_boundary_changes_and_restart_rebuild_same_semantics():
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(25, 6))
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(25, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "05:00"}, at(26, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "07:00"}, at(26, 20))
        pending = planning.get_cycle_settings(at(26, 20, 30))["pending_boundary"]
        assert pending["spanning_key"] == "2026-09-25"
        # 重启（重新读取持久化状态）后语义一致
        again = planning.get_cycle_settings(at(26, 20, 30))["pending_boundary"]
        assert again == pending
        assert again["effective_at"] == "2026-09-27T07:00:00+08:00"
        # 已生成轮次身份不受连续修改影响
        first = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-25")
        assert (first["schedule_date"], first["display_cycle_date"]) == (
            "2026-09-25", "2026-09-25")


def test_generation_during_and_after_transition_keeps_identity():
    # HIGH #1：过渡窗口内生成的实例，过渡结束 / 重启后身份稳定，
    # 不需要重新加载当时的过渡状态才能解释。
    with Context() as c:
        c.create("weekly", at(24, 7), weekdays=[3])  # 9/24 周四
        first = c.rows[0]
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(24, 8))
        assert planning.generate_due(at(24, 9))["created"] == 0
        planning.set_cycle_settings({"refresh_boundary_time": "05:00"}, at(24, 10))
        assert planning.generate_due(at(24, 11))["created"] == 0
        # 过渡结束（9/26 05:00 后）：已生成轮次身份完全不变，仅顺延展示
        planning.generate_due(at(28))
        assert (first["round_key"], first["schedule_date"]) == (
            "cycle:2026-09-24", "2026-09-24")
        assert first["display_cycle_date"] >= first["schedule_date"]
        assert len([row for row in c.rows if row["round_key"] == "cycle:2026-09-24"]) == 1


def test_absorbed_cycle_is_never_backfilled_after_transition_ends():
    # 08:00 → 06:00 主动吸收 9/26（spanning 9/25 延长到 9/27 06:00，9/26 不
    # 再是周期键）：登记后过渡结束也永不按漏跑补生成。
    with Context() as c:
        # 周四周五规则（9/24 周四、9/25 周五）
        c.create("weekly", at(24, 7), weekdays=[3, 4])
        assert {row["schedule_date"] for row in c.rows} == {"2026-09-24"}
        # 9/25 07:00 06:00→08:00：spanning 9/25；当前周期内 9/25（周五）轮正常生成
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(25, 7))
        assert planning.generate_due(at(25, 12))["created"] == 1
        assert {row["schedule_date"] for row in c.rows} == {"2026-09-24", "2026-09-25"}
        # 9/26 07:00 改 08:00→06:00：spanning 仍是 9/25，生效点 9/27 06:00
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(26, 7))
        pending = planning.get_cycle_settings(at(26, 7, 30))["pending_boundary"]
        assert pending["effective_at"] == "2026-09-27T06:00:00+08:00"
        # 9/26（周五）被吸收：过渡结束后（9/27 起）也不补生成 9/26 轮次
        assert planning.generate_due(at(28))["created"] == 0
        assert planning.generate_due(at(29))["created"] == 0
        assert all(row["schedule_date"] != "2026-09-26" for row in c.rows)
        assert {row["schedule_date"] for row in c.rows} == {"2026-09-24", "2026-09-25"}


def test_daily_outage_recovers_current_cycle_without_historical_backfill():
    # 最新确认口径：每日待办只保证当前有效周期存在应有实例；
    # 离线期间完全错过的历史每日轮次不得补生成。
    with Context() as c:
        c.create("daily", at(23))  # 9/23 创建并生成 9/23 轮
        # 系统离线到 9/28
        assert planning.generate_due(at(28))["created"] == 1
        dates = {row["schedule_date"] for row in c.rows}
        assert dates == {"2026-09-23", "2026-09-28"}
        # 不补 9/24–9/27 的历史每日实例；重复维护不重复生成当前周期
        assert planning.generate_due(at(28, 7, 30))["created"] == 0
        assert len(c.rows) == 2


def test_timeout_occurrence_cannot_revive_through_any_entry():
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        occ["closed_at"] = at(24, 6).isoformat()
        for target in ("pending", "in_progress", "deferred", "partial"):
            try:
                planning.set_occurrence_status(
                    occ["id"], {"status": target, "est_start": at(25).isoformat()}, at(25))
            except planning.PlanningError as error:
                assert error.code == "invalid_transition"
            else:
                raise AssertionError(f"timeout must not revive to {target}")
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == at(24, 6).isoformat()


def test_timeout_reschedule_preserves_history_and_creates_new_once_task():
    with Context() as c:
        task = c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        occ["closed_at"] = at(25, 6).isoformat()
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="op-1",
        )
        # 旧超时历史原样保留（状态与关闭时间不被改写）
        assert (occ["status"], occ["closed_at"]) == ("timeout", at(25, 6).isoformat())
        # 模型 A：任务建于当前周期（请求身份在任务行上），实例立即可见；
        # 用户所选绝对时刻作为人工锚点写在实例上
        new_task = next(
            row for row in c.db.rows["planning_task"] if row["id"] == result["task"]["id"]
        )
        assert new_task["task_type"] == "once"
        assert new_task["target_date"] == "2026-09-25"
        assert new_task["request_key"] == "reschedule:1:op-1"
        assert new_task["content"] == task["content"]
        new_occ = next(
            row for row in c.rows if row["task_id"] == new_task["id"]
        )
        assert new_occ["status"] == "pending"
        assert new_occ["est_start"] == at(25, 16).isoformat()
        assert new_occ["display_cycle_date"] == "2026-09-25"
        assert new_occ["is_fixed"] is True
        assert new_occ["fixed_source"] == "manual"
        # 非超时实例不能走该入口
        try:
            planning.reschedule_timeout_as_new(
                new_occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 17),
                idempotency_key="op-2")
        except planning.PlanningError as error:
            assert error.status_code == 422
        else:
            raise AssertionError("only timeout instances can be rescheduled as new")


def test_fixed_death_round_cannot_revive():
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        planning.generate_due(at(27, 6))
        dead = next(row for row in c.rows if row["schedule_date"] == "2026-09-24")
        assert dead["status"] == "timeout"
        try:
            planning.set_occurrence_status(
                dead["id"], {"status": "pending", "est_start": at(27, 7).isoformat()}, at(27, 7))
        except planning.PlanningError as error:
            assert error.code == "invalid_transition"
        else:
            raise AssertionError("fixed-death round must not revive")


def test_sweep_timeout_records_business_death_instant_as_closed_at():
    with Context() as c:
        # 批次 5 换源：判定源 = 生成时冻结的 window_end_at（真实生成路径，
        # 只有最晚完成冻结单一端点）。
        c.create("daily", at(23), window_end_tod="12:00")
        occ = c.rows[0]
        assert occ["window_start_at"] is None
        assert occ["window_end_at"] == at(23, 12).isoformat()
        planning.sweep_timeouts(at(24, 13))
        assert occ["status"] == "timeout"
        # closed_at = 窗口终点（业务死亡时刻），而非 sweep 执行时刻
        assert occ["closed_at"] == at(23, 12).isoformat()


def test_status_correction_keeps_handled_at_as_historical_fact():
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 15))
        handled = occ["handled_at"]
        planning.set_occurrence_status(occ["id"], {"status": "discarded_this"}, at(24, 18))
        # handled_at 是历史业务事实：关闭态之间更正不改写
        assert occ["handled_at"] == handled
        # closed_at 是第一次进入关闭态的事实时间：标签更正保留原值（B7）
        assert occ["closed_at"] == at(24, 15).isoformat()
        # 更正发生时间由 updated_at 表达
        assert occ["updated_at"] == at(24, 18).isoformat()


def test_early_completion_after_stale_backlog_expires_dead_rounds():
    # HIGH #6：提前完成前先按正常生命周期补跑生成与到期清理；应死的旧轮次
    # 被规则杀死，未来固定轮次不受影响。
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        stale = c.rows[0]  # 9/24 轮
        # 模拟维护尚未运行的窗口：9/27 轮已到点但旧轮还未被判死
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(27, 6, 0))
        planning.generate_due.__wrapped__ if False else None
        # 直接制造"旧轮仍开放"的过时视图（不运行 generate_due）
        c.rows[:] = [row for row in c.rows if row["task_id"] == stale["task_id"]]
        stale["status"] = "pending"
        result = planning.complete_task_early(stale["task_id"], at(27, 6, 30), idempotency_key="r1")
        # 补跑后：9/24 旧轮按规则死亡（closed_at = 下一槽 9/27 06:00）
        assert stale["status"] == "timeout"
        assert stale["closed_at"] == at(27, 6).isoformat()
        # 9/27 当前轮被正常完成（不是提前完成的额外记录）
        assert result["source"] == "schedule"
        assert result["schedule_date"] == "2026-09-27"
        # 未来轮次（9/30）不受影响：仍会在到点时正常生成
        planning.generate_due(at(30, 6))
        assert any(row["schedule_date"] == "2026-09-30" and row["status"] == "pending"
                   for row in c.rows)


def test_early_completion_fixed_type_never_closes_future_rounds():
    with Context() as c:
        task = c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 7))
        extra = planning.complete_task_early(task["id"], at(25, 10), idempotency_key="extra-1")
        assert extra["source"] == "early"
        assert extra["status"] == "completed"
        # 固定时间轴不动：下一个槽（9/27）到点正常生成开放轮次
        planning.generate_due(at(27, 6))
        current = next(row for row in c.rows if row["schedule_date"] == "2026-09-27")
        assert current["status"] == "pending"
        assert all(row["status"] != "timeout" or row["schedule_date"] == "2026-09-24"
                   for row in c.rows)


def test_after_completion_early_completion_moves_basis_to_now():
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        task_row = c.db.rows["planning_task"][0]
        planning.complete_task_early(task_row["id"], at(25, 10), idempotency_key="ac-1")
        assert task_row["last_handled_at"] == at(25, 10).isoformat()
        assert task_row["refresh_next_due_at"] == at(28, 10).isoformat()
        # 不残留与新风基准冲突的旧开放轮次
        assert all(row["status"] not in ("pending", "in_progress", "deferred", "partial")
                   for row in c.rows)


def test_auto_recompute_wait_config_is_respected_across_triggers():
    # HIGH #7：默认值只在配置缺失时使用；user 配置 120 分钟后持续生效。
    with RecalcContext() as c:
        c.create("daily", at(23))
        planning.generate_due(at(23, 7, 30))
        planning.set_cycle_settings({"auto_recompute_wait_minutes": 120}, at(23, 8))
        assert planning.get_recompute_state(at(23, 8))["wait_minutes"] is None
        planning.request_recompute("reorder", at(23, 8))
        # +60 分钟：未到期，等待继续且不执行
        assert planning.run_maintenance(at(23, 9))["auto_recompute"].get("skipped")
        state = planning.get_recompute_state(at(23, 9))
        assert state["pending"] and state["wait_minutes"] > 0
        # +121 分钟：到期执行
        result = planning.run_maintenance(at(23, 10, 1))
        assert "updated" in result["auto_recompute"]
        assert planning.get_recompute_state(at(23, 10, 1))["pending"] is False
        # 重新触发仍读 120，而不是默认 30
        planning.request_recompute("status_change", at(23, 11))
        assert planning.run_maintenance(at(23, 12, 29))["auto_recompute"].get("skipped")
        result = planning.run_maintenance(at(23, 13, 1))
        assert "updated" in result["auto_recompute"]


def test_auto_recompute_disabled_stays_disabled_and_manual_works():
    with RecalcContext() as c:
        c.create("daily", at(23))
        planning.generate_due(at(23, 7, 30))
        planning.set_cycle_settings({"auto_recompute_enabled": False}, at(23, 8))
        planning.set_cycle_settings({"daily_refresh_enabled": True}, at(23, 8, 30))
        planning.request_recompute("reorder", at(23, 9))
        assert planning.get_recompute_state(at(23, 9))["pending"] is False
        # 等待三天也不会自动执行
        assert planning.run_maintenance(at(26, 9))["auto_recompute"].get("skipped")
        assert planning.get_recompute_state(at(26, 9))["pending"] is False
        # 手动重算可用，且不改写 user 配置
        planning.trigger_recompute(at(26, 9, 30))
        settings = planning.get_cycle_settings(at(26, 9, 30))
        assert settings["auto_recompute_enabled"] is False


def test_partial_keeps_execution_facts_in_automatic_recompute():
    # MEDIUM #8：部分完成 ≠ 从未开始；自动排程不得从头重排整个实例。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(23, 7, 30))
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "完成一半"}, at(23, 9))
        est_before = (occ["est_start"], occ["est_end"])
        assert est_before[0] is not None  # 已有自动排程结果
        # 列表变化触发重算：partial 实例的时间不被覆盖重排
        planning.recompute_today(at(23, 10))
        assert (occ["est_start"], occ["est_end"]) == est_before
        assert occ["status"] == "partial"
        assert occ["partial_at"] == at(23, 9).isoformat()
        assert occ["partial_note"] == "完成一半"  # 执行事实保留


def test_partial_without_estimate_is_not_rescheduled_from_scratch():
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["est_start"] = None
        occ["est_end"] = None
        occ["estimated_time_source"] = "unassigned"
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "只做了准备"}, at(23, 9))
        planning.recompute_today(at(23, 10))
        # 无剩余耗时信息时不擅自推导：保持无预估，而不是当成新任务排程
        assert occ["est_start"] is None
        assert occ["status"] == "partial"
        # 仍随周期顺延（开放生命周期）
        planning.generate_due(at(25))
        assert occ["display_cycle_date"] == "2026-09-25"


def test_partial_round_flows_through_every_lifecycle_path():
    # 十一节：partial 开放化后的全路径扫描
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(23, 7, 30))
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "一部分"}, at(23, 9))
        # 今日看板：进度中，不在已完成
        board = planning.today_board(at(23, 10))
        assert [item["id"] for item in board["progress"]] == [occ["id"]]
        assert all(item["id"] != occ["id"] for item in board["done"])
        # 到期清理不波及 partial
        planning.cleanup_discarded(at(30))
        assert occ in c.rows
        # 最终「已全部完成」才关闭并起算刷新基准
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(23, 18))
        assert occ["status"] == "completed"
        assert occ["closed_at"] == at(23, 18).isoformat()
        assert occ["handled_at"] == at(23, 18).isoformat()
        assert occ["partial_at"] == at(23, 9).isoformat()
