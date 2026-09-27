"""Phase 1R 第三轮修复（Repair Round 2）回归测试。

对应独立复审 B1–B9：
- B1 边界过渡的「计划吸收」与「已发生吸收」分离；
- B2/B3/B4 超时重排的绝对时间、幂等与无半成功；
- B5/B6/B7 关闭态历史更正语义；
- B8 实例级排程耗时快照；
- B9 单任务生命周期校正边界；
- 两个已确认口径（同期单条额外完成 / 中空阶段重排）。
"""

from datetime import date, datetime, timedelta, timezone
from unittest import mock

import pytest

from gateway import planning
from test_planning_phase1b import Context, at


CST = timezone(timedelta(hours=8))


def test_boundary_replan_before_effect_restores_absorbed_cycle():
    # B1：08→06（计划吸收 9/25）→ 生效前再改 09：9/25 恢复为有效周期，
    # 当月规则任务不得漏生成。
    with Context() as c:
        c.create("monthly", at(24, 7), month_days=[25], refresh_mode="fixed_monthday")
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(23, 9))
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(25, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "09:00"}, at(25, 7, 30))
        # 新计划：9/25 09:00 即新周期，9/25 不再被吸收
        pending = planning.get_cycle_settings(at(25, 8))["pending_boundary"]
        assert pending["spanning_key"] == "2026-09-24"
        assert pending["effective_at"] == "2026-09-25T09:00:00+08:00"
        result = planning.generate_due(at(25, 9, 10))
        assert result["created"] == 1
        assert any(row["schedule_date"] == "2026-09-25" for row in c.rows)


def test_boundary_replan_back_to_original_restores_absorbed_cycle():
    # 08→06→08：9/25 从 08:00 起恢复为有效周期。
    with Context() as c:
        c.create("monthly", at(24, 7), month_days=[25], refresh_mode="fixed_monthday")
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(23, 9))
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(25, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(25, 7, 30))
        assert planning.generate_due(at(25, 8, 30))["created"] == 1
        assert any(row["schedule_date"] == "2026-09-25" for row in c.rows)


def test_realized_absorption_survives_later_boundary_changes():
    # 已真正走完的过渡：吸收事实永久保留，后续改边界也不得补生成。
    with Context() as c:
        c.create("monthly", at(24, 7), month_days=[25], refresh_mode="fixed_monthday")
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(23, 9))
        # 9/25 07:00 08→06：跨越 9/24，生效 9/26 06:00，9/25 被吸收
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(25, 7))
        # 9/26 07:00 过渡已走完，此时再改：吸收事实入账
        planning.set_cycle_settings({"refresh_boundary_time": "09:00"}, at(26, 7))
        state = c.settings[planning.PLANNING_BOUNDARY_STATE_KEY]
        assert "2026-09-25" in state["absorbed"]
        assert planning.generate_due(at(27, 9, 30))["created"] == 0
        assert all(row["schedule_date"] != "2026-09-25" for row in c.rows)


def test_boundary_transition_restart_keeps_absorption_semantics():
    # 重启（重新读取持久化状态）后语义一致：pending 吸收仍可被重算，
    # 已完成吸收保持跳过。
    with Context() as c:
        c.create("monthly", at(24, 7), month_days=[25], refresh_mode="fixed_monthday")
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(23, 9))
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(25, 7))
        # 未生效前重启两次，再改 09:00 → 9/25 恢复有效
        assert planning.get_cycle_settings(at(25, 7, 10))["pending_boundary"]
        assert planning.get_cycle_settings(at(25, 7, 20))["pending_boundary"]
        planning.set_cycle_settings({"refresh_boundary_time": "09:00"}, at(25, 7, 30))
        assert planning.generate_due(at(25, 9, 10))["created"] == 1


def test_absorbed_day_skip_covers_weekday_rules():
    # weekly 规则在吸收日期上的行为与 monthly 一致（08→06 吸收 9/25 周四）。
    with Context() as c:
        c.create("weekly", at(24, 9), weekdays=[3], refresh_mode="fixed_weekday")
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(23, 9))
        # 9/25 07:00 08→06：spanning 9/24，生效 9/26 06:00，9/25（周四）被吸收
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(25, 7))
        planning.generate_due(at(26, 7))
        assert all(row["schedule_date"] != "2026-09-25" for row in c.rows)
        # 下一周四（10/1）正常生成
        planning.generate_due(datetime(2026, 10, 1, 7, tzinfo=CST))
        assert any(row["schedule_date"] == "2026-10-01" for row in c.rows)


def test_timeout_reschedule_keeps_user_absolute_time_before_boundary():
    # B2/H1：边界 06:00，9/25 04:00 重排到 9/25 05:00——绝对时刻原样保留，
    # 且实例立即出现在当前周期看板（不等 06:00）。
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 5).isoformat()}, at(25, 4),
            idempotency_key="op-1",
        )
        new_task = next(
            row for row in c.db.rows["planning_task"] if row["id"] == result["task"]["id"]
        )
        assert new_task["target_date"] == "2026-09-24"  # 建于当前周期
        assert new_task["request_key"] == "reschedule:1:op-1"
        new_occ = next(row for row in c.rows if row["task_id"] == new_task["id"])
        # 绝对执行时刻恒等于用户所选，不因周期归属漂移
        assert new_occ["est_start"] == at(25, 5).isoformat()
        assert new_occ["est_end"] == at(25, 5, 30).isoformat()
        assert new_occ["display_cycle_date"] == "2026-09-24"  # 立即可见
        board = planning.today_board(at(25, 4, 30))
        assert any(item["id"] == new_occ["id"] for item in board["progress"])
        assert new_occ["is_fixed"] is True and new_occ["fixed_source"] == "manual"


def test_timeout_reschedule_future_time_defers_to_its_cycle():
    # H1 未来时间：实例立即创建，预估为用户所选绝对时刻，展示顺延到包含
    # 该时刻的周期（manual_defer）。
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(28, 10).isoformat()}, at(25, 4),
            idempotency_key="op-f1",
        )
        new_occ = next(row for row in c.rows if row["task_id"] == result["task"]["id"])
        assert new_occ["est_start"] == at(28, 10).isoformat()
        assert (new_occ["display_cycle_date"], new_occ["display_reason"]) == (
            "2026-09-28", "manual_defer")


def test_timeout_reschedule_is_idempotent_per_request():
    # B3（第七轮语义）：同幂等键重放 / 双击收敛到同一结果；对该超时记录的
    # 再次「修改时间」（新键）接管同一业务待办——最终只有一个待办。
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        first = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="op-1",
        )
        replay = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="op-1",
        )
        assert replay["task"]["id"] == first["task"]["id"]
        assert replay["occurrence"]["id"] == first["occurrence"]["id"]
        assert replay.get("replayed") is True
        second = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 17).isoformat()}, at(25, 15, 30),
            idempotency_key="op-2",
        )
        # 同一业务待办被接管：任务与实例身份保持，时间移动到 17:00
        assert second["occurrence"]["id"] == first["occurrence"]["id"]
        assert second["occurrence"]["est_start"] == at(25, 17).isoformat()
        tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(tasks) == 1
        assert tasks[0]["request_key"] == "reschedule:1:op-2"
        assert tasks[0]["request_absorbed_keys"] == ["reschedule:1:op-1"]


def test_timeout_reschedule_generation_failure_is_not_silent_success():
    # B4/M1（模型 A）：实例生成失败 → 报 503，不报成功；任务行携带请求身份
    # 保留（不依赖补偿删除），同键重试经身份恢复收敛为一份完整结果。
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("generation down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
                    idempotency_key="op-1",
                )
            except planning.PlanningError as error:
                assert error.status_code == 503
            else:
                raise AssertionError("generation failure must not report success")
        # 任务行按请求身份保留（可恢复），不存在第二份任务
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert once_tasks[0]["request_key"] == "reschedule:1:op-1"
        # 同键重试：经请求身份恢复，收敛为一份带实例的结果
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="op-1",
        )
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert result["occurrence"] is not None
        assert result["occurrence"]["est_start"] == at(25, 16).isoformat()


def test_timeout_reschedule_recovery_survives_repeated_generation_failures():
    # M1 极端路径：恢复生成也失败 → 仍 503 且任务不重复；最终恢复成功。
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        occ["status"] = "timeout"
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            for _ in range(2):
                try:
                    planning.reschedule_timeout_as_new(
                        occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
                        idempotency_key="op-1",
                    )
                except planning.PlanningError as error:
                    assert error.status_code == 503
        assert len([row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]) == 1
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="op-1",
        )
        assert result["occurrence"] is not None
        assert len([row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]) == 1


def test_timeout_reschedule_hollow_phase_uses_phase_content_and_duration():
    # 歧义口径 2：中空阶段条目超时重排按该条目的展示内容与实例级耗时。
    with Context() as c:
        c.create("once", at(24), target_date="2026-09-24", is_hollow=True,
                 hollow_start_minutes=10, hollow_wait_minutes=30,
                 hollow_end_minutes=5)
        start_occ = next(row for row in c.rows if row["phase"] == "start")
        start_occ["status"] = "timeout"
        result = planning.reschedule_timeout_as_new(
            start_occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="hollow-1",
        )
        new_task = next(
            row for row in c.db.rows["planning_task"] if row["id"] == result["task"]["id"]
        )
        assert new_task["task_type"] == "once"
        assert "开始" in new_task["content"]  # 阶段展示内容
        assert new_task["estimated_minutes"] == 10  # 阶段耗时，而非任务主耗时


def test_task_duration_edit_does_not_reinterpret_generated_instances():
    # B8：任务耗时 30→120 后，已生成实例仍按 30 排程（手动/自动重算）；
    # 未来新实例使用 120；自动时间仍可移动。
    with Context() as c:
        task = c.create("daily", at(23), estimated_minutes=30)
        occ = c.rows[0]
        planning.recompute_today(at(23, 8))
        planning.update_task(task["id"], {"estimated_minutes": 120}, at(23, 9))
        # 手动重算：仍 30 分钟
        planning.recompute_today(at(23, 10))
        dur = (datetime.fromisoformat(occ["est_end"])
               - datetime.fromisoformat(occ["est_start"])).total_seconds() / 60
        assert dur == 30
        assert occ["estimated_time_source"] == "automatic"
        # 自动时间可移动（重算把开始推到重算时刻），但耗时仍是实例自己的 30
        planning.recompute_today(at(23, 11))
        assert datetime.fromisoformat(occ["est_start"]) == at(23, 11)
        dur = (datetime.fromisoformat(occ["est_end"])
               - datetime.fromisoformat(occ["est_start"])).total_seconds() / 60
        assert dur == 30
        # 未来新实例使用 120
        fresh_task = next(row for row in c.db.rows["planning_task"] if row["id"] == task["id"])
        planning._create_occurrences(c.db, fresh_task, date(2026, 9, 24), at(24))
        future = next(row for row in c.rows if row["schedule_date"] == "2026-09-24")
        assert future["planned_minutes"] == 120
        planning.recompute_today(at(24, 7))
        dur = (datetime.fromisoformat(future["est_end"])
               - datetime.fromisoformat(future["est_start"])).total_seconds() / 60
        assert dur == 120


def test_api_and_display_use_instance_duration_snapshot():
    # M3：任务耗时 30→120 后，旧实例的 API/展示耗时为实例值 30。
    with Context() as c:
        c.create("daily", at(23), estimated_minutes=30)
        occ = c.rows[0]
        planning.update_task(1, {"estimated_minutes": 120}, at(23, 9))
        task_row = c.db.rows["planning_task"][0]
        serialized = planning.serialize_occurrence(occ, task_row, at(23, 10))
        assert serialized["estimated_minutes"] == 30
        # 新实例展示 120
        fresh = planning._create_occurrences(c.db, task_row, date(2026, 9, 24), at(24))
        assert fresh == 1
        fresh_occ = next(row for row in c.rows if row["schedule_date"] == "2026-09-24")
        assert planning.serialize_occurrence(
            fresh_occ, task_row, at(24, 7))["estimated_minutes"] == 120


def test_closed_history_correction_to_discarded_is_row_only():
    # B6：把已完成历史更正为 discarded 只改这一条记录，不停用任务、
    # 不关闭其他开放轮次、不停止未来刷新。
    with Context() as c:
        c.create("daily", at(23))
        first = c.rows[0]  # 9/23 轮（已完成历史）
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(23, 15))
        planning.generate_due(at(25))
        second = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        planning.set_occurrence_status(first["id"], {"status": "discarded"}, at(24, 9))
        assert c.db.rows["planning_task"][0]["is_active"] is True
        assert second["status"] == "pending"
        # 任务未来仍按规则刷新
        assert planning.generate_due(at(26))["created"] == 1


def test_closed_correction_completed_discarded_both_directions():
    # B5：completed ↔ discarded 双向更正（Python 层），closed_at 与
    # handled_at 语义不变（数据库侧由真实 PostgreSQL 测试覆盖）。
    with Context() as c:
        c.create("daily", at(23))
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(23, 15))
        planning.set_occurrence_status(occ["id"], {"status": "discarded"}, at(24, 9))
        assert occ["status"] == "discarded"
        assert occ.get("handled_at") == at(23, 15).isoformat()
        assert occ["closed_at"] == at(23, 15).isoformat()
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(25, 9))
        assert occ["status"] == "completed"
        assert occ["handled_at"] == at(23, 15).isoformat()
        assert occ["closed_at"] == at(23, 15).isoformat()


def test_correction_from_unhandled_discard_records_handling_at_correction_time():
    # 整任务废弃产生无 handled_at 的关闭历史（任务已按命令停用）；该历史
    # 更正为已完成时，以更正时刻补记处理事实。任务保持停用（B6 行内更正），
    # 不再排未来的刷新基准。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "discarded"}, at(24, 9))
        assert occ.get("handled_at") is None
        assert c.db.rows["planning_task"][0]["is_active"] is False
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(26, 10))
        assert occ["handled_at"] == at(26, 10).isoformat()
        task_row = c.db.rows["planning_task"][0]
        assert task_row["is_active"] is False
        assert task_row["refresh_next_due_at"] is None


def test_early_completion_failure_does_not_touch_unrelated_tasks():
    # B9：Task A 提前完成失败时，Task B 的待生成轮次不受影响；A 重试后正常。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="fixed_interval", interval_days=3)
        c.create("daily", at(23))  # Task B：9/25 轮待生成
        b_before = len([row for row in c.rows if row["task_id"] == 2])
        def failing(*args, **kwargs):
            raise RuntimeError("injected A write failure")
        with mock.patch.object(planning, "set_occurrence_status", side_effect=failing):
            try:
                planning.complete_task_early(1, at(25, 10), idempotency_key="a-1")
            except RuntimeError:
                pass
            else:
                raise AssertionError("injected failure must propagate")
        assert len([row for row in c.rows if row["task_id"] == 2]) == b_before
        # A 重试成功；B 的轮次随后由正常维护生成
        result = planning.complete_task_early(1, at(25, 11), idempotency_key="a-1")
        assert result["source"] in ("early", "schedule")
        planning.generate_due(at(25, 12))
        assert any(row["task_id"] == 2 and row["schedule_date"] == "2026-09-25"
                   for row in c.rows)
