"""Phase 1B lifecycle scenarios against the sealed Phase 1A identity paths.

Phase 1R 口径：过去不重写，已生成实例不追溯，新的事实影响未来。
边界变更从下一规划周期生效，不重新解释当前周期；固定间隔轮次身份由
到期事件派生；部分完成属于开放生命周期。
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from test_planning_phase1a import _Database


CST = timezone(timedelta(hours=8))


def at(day, hour=7, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=CST)


class Context:
    def __init__(self):
        self.db = _Database()
        self.settings = {}
        self.patches = [
            mock.patch.object(planning, "get_client", return_value=self.db),
            mock.patch.object(planning.db, "load_app_setting", side_effect=self.settings.get),
            mock.patch.object(planning.db, "save_app_setting", side_effect=self.save),
            mock.patch.object(planning, "request_recompute"),
        ]

    def save(self, key, value):
        self.settings[key] = value
        return True

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *_):
        for patch in reversed(self.patches):
            patch.stop()

    @property
    def rows(self):
        return self.db.rows["planning_occurrence"]

    def create(self, kind, now=at(24), **kwargs):
        return planning.create_task({
            "content": kind, "task_type": kind, "estimated_minutes": 30, **kwargs,
        }, now)


def test_boundary_change_keeps_current_cycle_and_takes_effect_next_cycle():
    # 核心新需求：06:00 → 08:00，9/25 07:00 修改。当前周期（9/25）按原边界
    # 继续走完，已生成轮次不动；新边界从 9/26 08:00 的周期开始生效。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        planning.generate_due(at(25, 6))
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(25, 7))
        assert planning.get_cycle_settings(at(25, 12))["pending_boundary"]["previous_time"] == "06:00"
        # 过渡窗口内「今天」仍是 9/25：不生成新轮、不移动任何展示。
        assert planning.generate_due(at(25, 12))["created"] == 0
        assert planning.today_board(at(25, 12))["date"] == "2026-09-25"
        # 9/26 07:00 仍属于被延长的 9/25 周期。
        assert planning.today_board(at(26, 7))["date"] == "2026-09-25"
        assert planning.generate_due(at(26, 7))["created"] == 0
        # 9/26 08:00 新周期开始：9/26 轮按新边界生成，未完成的 9/25 轮顺延。
        assert planning.generate_due(at(26, 8, 30))["created"] == 1
        assert c.rows[-1]["round_key"] == "cycle:2026-09-26"
        assert c.rows[-1]["schedule_date"] == "2026-09-26"
        current = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-25")
        assert (current["display_cycle_date"], current["display_reason"]) == (
            "2026-09-26", "carryover")
        assert all(row["display_cycle_date"] >= row["schedule_date"] for row in c.rows)


def test_boundary_change_back_keeps_current_cycle_frozen_until_natural_end():
    # 06:00 → 08:00（9/25 07:00）→ 06:00（9/25 12:00）（HIGH #2）：跨越周期
    # 9/25 冻结不变，只延长到 A 的下一个自然边界点 9/26 06:00；没有轮次需要
    # 恢复或重新解释。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(25, 6))
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(25, 7))
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(25, 12))
        pending = planning.get_cycle_settings(at(25, 12))["pending_boundary"]
        assert pending["spanning_key"] == "2026-09-25"
        assert pending["previous_time"] == "06:00"
        # 9/26 05:00 仍属于被延长的 9/25 周期
        assert planning.today_board(at(26, 5))["date"] == "2026-09-25"
        assert planning.generate_due(at(26, 5))["created"] == 0
        # 9/26 06:00 起新周期正常开始
        assert planning.today_board(at(26, 7))["date"] == "2026-09-26"
        assert planning.generate_due(at(26, 7))["created"] == 1
        assert c.rows[-1]["round_key"] == "cycle:2026-09-26"
        assert len(c.rows) == 3


def test_consecutive_boundary_changes_keep_single_round_without_reinterpretation():
    # 边界反复变化只影响「下一周期从几点开始」，绝不重新安置已生成轮次，
    # 重复维护保持幂等。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        first = c.rows[0]
        assert first["round_key"] == "cycle:2026-09-23"
        planning.set_cycle_settings({"refresh_boundary_time": "08:00"}, at(24, 7))
        assert planning.generate_due(at(24, 8))["created"] == 0
        planning.set_cycle_settings({"refresh_boundary_time": "06:00"}, at(24, 9))
        for _ in range(2):
            assert planning.generate_due(at(24, 9))["created"] == 0
        assert len(c.rows) == 2
        assert (first["round_key"], first["schedule_date"]) == (
            "cycle:2026-09-23", "2026-09-23")
        assert (first["display_cycle_date"], first["display_reason"]) == (
            "2026-09-24", "carryover")
        assert planning.generate_due(at(25, 7))["created"] == 1


def test_boundary_change_never_reinterprets_generated_rounds():
    # 任何边界变更都不产生 boundary_shift：展示只前进（顺延），不后退。
    with Context() as c:
        c.create("weekly", at(24, 7), weekdays=[3])  # 9/24 周四
        first = c.rows[0]
        assert (first["round_key"], first["schedule_date"]) == (
            "cycle:2026-09-24", "2026-09-24")
        planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(24, 7, 10))
        assert planning.generate_due(at(24, 7, 10))["created"] == 0
        assert (first["display_cycle_date"], first["display_reason"]) == (
            "2026-09-24", "initial")
        # 新周期（9/25 起）里未处理的轮次正常顺延。
        assert planning.generate_due(at(25, 5))["created"] == 0
        assert (first["display_cycle_date"], first["display_reason"]) == (
            "2026-09-25", "carryover")
        assert len(c.rows) == 1
        assert all(row.get("boundary_shift_at") is None for row in c.rows)


def test_fixed_interval_round_key_survives_boundary_change_and_progress_loss():
    # 固定间隔轮次身份由到期事件派生：边界变更、进度写丢失后重放，同一到期
    # 事件仍映射到同一轮次，绝不产生第二轮。
    with Context() as c:
        c.create("interval", at(24, 7), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        assert first["round_key"].startswith("fixed:2026-09-24:")
        assert first["schedule_date"] == "2026-09-24"
        assert first["fixed_due_at"] == at(24, 7).isoformat()
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 7, 10))
        planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(24, 7, 20))
        c.db.rows["planning_task"][0]["refresh_generated_through"] = None
        assert planning.generate_due(at(24, 7, 20))["created"] == 0
        assert planning.generate_due(at(24, 7, 20))["created"] == 0
        assert len(c.rows) == 1
        assert first["round_key"].startswith("fixed:2026-09-24:")
        assert planning.generate_due(at(27, 7))["created"] == 1
        assert c.rows[-1]["round_key"].startswith("fixed:2026-09-27:")
        assert c.rows[-1]["schedule_date"] == "2026-09-27"
        assert planning.generate_due(at(27, 7))["created"] == 0


def test_early_round_is_not_an_axis_round_and_survives_expiry_sweeps():
    # 额外完成记录不属于固定时间轴：到期清理只作用于规则轮次。
    with Context() as c:
        task = c.create("interval", at(24, 7), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 7, 10))
        early = planning.complete_task_early(task["id"], at(24, 7, 20), idempotency_key="k1")
        row = next(item for item in c.rows if item["id"] == early["id"])
        assert row["source"] == "early"
        assert row.get("fixed_due_at") is None
        assert row["status"] == "completed"
        assert row["round_key"].startswith("early:2026-09-24:")
        planning.generate_due(at(27, 7))
        assert row in c.rows and row["status"] == "completed"


def test_daily_boundary_switch_replay_and_identity_carryover():
    with Context() as c:
        before = at(24, 5, 59)
        c.create("daily", before)
        first = c.rows[0]
        assert first["round_key"] == "cycle:2026-09-23"
        assert planning.generate_due(at(24, 5, 59))["created"] == 0
        assert planning.generate_due(at(24, 6))["created"] == 1
        assert first["round_key"] == "cycle:2026-09-23"
        assert first["schedule_date"] == first["for_date"] == "2026-09-23"
        assert (first["display_cycle_date"], first["display_reason"]) == ("2026-09-24", "carryover")
        assert planning.generate_due(at(24, 6))["created"] == 0
        planning.set_cycle_settings({"daily_refresh_enabled": False}, at(25))
        assert planning.generate_due(at(25))["created"] == 0
        assert len(c.rows) == 2
        planning.set_cycle_settings({"daily_refresh_enabled": True}, at(25))
        assert planning.generate_due(at(25))["created"] == 1
        assert planning.generate_due(at(25))["created"] == 0
        assert {row["round_key"] for row in c.rows} == {
            "cycle:2026-09-23", "cycle:2026-09-24", "cycle:2026-09-25",
        }


def test_daily_outage_resumes_current_cycle_without_stale_new_rounds():
    with Context() as c:
        c.create("daily", at(24))
        assert planning.generate_due(at(27))["created"] == 1
        assert {row["schedule_date"] for row in c.rows} == {"2026-09-24", "2026-09-27"}
        assert all(row["display_cycle_date"] == "2026-09-27" for row in c.rows)


def test_concurrent_replay_cannot_create_two_rows_for_one_round():
    with Context() as c:
        c.create("once", at(24), target_date="2026-09-25")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(planning.generate_due, [at(25)] * 8))
        assert sum(result["created"] for result in results) == 1
        assert len(c.rows) == 1
        assert c.rows[0]["round_key"] == "once"


def test_once_and_idle_are_single_persistent_rounds():
    with Context() as c:
        c.create("once", at(24), target_date="2026-09-25")
        c.create("idle", at(24))
        assert len(c.rows) == 1
        idle = c.rows[0]
        assert idle["round_key"] == "once"
        planning.generate_due(at(25))
        assert len(c.rows) == 2
        once = next(row for row in c.rows if row["task_id"] != idle["task_id"])
        assert once["schedule_date"] == "2026-09-25"
        planning.generate_due(at(27))
        assert len(c.rows) == 2
        assert (idle["round_key"], idle["schedule_date"], idle["display_cycle_date"]) == (
            "once", "2026-09-24", "2026-09-27",
        )
        assert once["display_reason"] == "carryover"
        assert all(row["status"] == "pending" for row in c.rows)
        planning.set_occurrence_status(once["id"], {"status": "discarded_this"}, at(27, 15))
        planning.set_occurrence_status(idle["id"], {"status": "discarded_this"}, at(27, 15))
        planning.generate_due(at(30))
        assert len(c.rows) == 2


def test_fixed_interval_catches_up_and_expires_only_at_next_rule_round():
    with Context() as c:
        c.create("interval", at(1, 6), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        assert first["round_key"].startswith("fixed:2026-09-01:")
        planning.generate_due(at(3))
        assert first["status"] == "pending"
        assert first["display_cycle_date"] == "2026-09-03"
        result = planning.generate_due(at(7, 6))
        assert result["created"] == 2
        assert first["status"] == "timeout"
        assert first["closed_at"] == at(4, 6).isoformat()
        # 轮次按到期事件区分；错过的 9/4 轮出生于当前周期（9/7），照常死亡。
        status_by_due = {row["fixed_due_at"][:10]: row["status"] for row in c.rows}
        assert status_by_due == {
            "2026-09-01": "timeout", "2026-09-04": "timeout", "2026-09-07": "pending",
        }
        assert sum(row["schedule_date"] == "2026-09-07" for row in c.rows) == 2
        assert planning.generate_due(at(7, 6))["created"] == 0


@pytest.mark.parametrize("kind,rule,next_due,next_key,expected", [
    ("daily", {}, at(25, 4), "cycle:2026-09-25", 1),
    # weekly/monthly：过渡期被吸收的 9/24 周期已登记，过渡结束后也永不按
    # 「漏跑补生成」补回（边界过渡主动吸收 ≠ 真正漏跑）。
    ("weekly", {"weekdays": [2, 3]}, at(30, 4), "cycle:2026-09-30", 1),
    ("monthly", {"month_days": [23, 24]}, datetime(2026, 10, 23, 4, tzinfo=CST), "cycle:2026-10-23", 1),
])
def test_calendar_fixed_replay_does_not_retroactively_create_a_second_initial_round(
    kind, rule, next_due, next_key, expected,
):
    with Context() as c:
        c.create(kind, at(24, 5), **rule)
        first = c.rows[0]
        assert first["round_key"] == "cycle:2026-09-23"
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 5) + timedelta(seconds=30))
        planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(24, 5, 1))
        c.db.rows["planning_task"][0]["refresh_generated_through"] = None
        assert planning.generate_due(at(24, 5, 1))["created"] == 0
        assert planning.generate_due(at(24, 5, 1))["created"] == 0
        assert {row["round_key"] for row in c.rows} == {"cycle:2026-09-23"}
        assert first["schedule_date"] == first["for_date"] == "2026-09-23"
        assert planning.generate_due(next_due)["created"] == expected
        assert c.rows[-1]["round_key"] == next_key
        assert planning.generate_due(next_due)["created"] == 0
        assert all(row["display_cycle_date"] >= row["schedule_date"] for row in c.rows)


def test_fixed_completion_and_discard_do_not_move_anchor():
    with Context() as c:
        task = c.create("interval", at(1, 6), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "discarded_this"}, at(2, 15))
        assert c.db.rows["planning_task"][0]["refresh_anchor_at"] == at(1, 6).isoformat()
        assert planning.generate_due(at(4, 5, 59))["created"] == 0
        assert planning.generate_due(at(4, 6))["created"] == 1
        assert c.rows[-1]["round_key"].startswith("fixed:2026-09-04:")
        assert c.rows[-1]["task_id"] == task["id"]


def test_existing_round_locks_refresh_mode_and_fixed_first_anchor():
    with Context() as c:
        task = c.create("interval", at(24), refresh_mode="fixed_interval", interval_days=3)
        for patch in ({"refresh_mode": "after_completion"},
                      {"refresh_anchor_at": at(25, 6).isoformat()}):
            try:
                planning.update_task(task["id"], patch, at(24, 9))
            except planning.PlanningError as error:
                assert error.code == "round_identity_locked"
            else:
                raise AssertionError("existing round identity must remain stable")
        assert len(c.rows) == 1


def test_after_completion_carries_then_uses_confirmation_time():
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        first = c.rows[0]
        assert first["round_key"].startswith("handled:2026-09-24:")
        assert planning.generate_due(at(26))["created"] == 0
        assert len(c.rows) == 1
        assert first["display_cycle_date"] == "2026-09-26"
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(26, 15))
        assert c.db.rows["planning_task"][0]["last_handled_at"] == at(26, 15).isoformat()
        assert c.db.rows["planning_task"][0]["refresh_next_due_at"] == at(29, 15).isoformat()
        assert planning.generate_due(at(29, 14, 59))["created"] == 0
        assert planning.generate_due(at(29, 15))["created"] == 1
        assert c.rows[-1]["schedule_date"] == "2026-09-29"
        assert c.rows[-1]["round_key"] != first["round_key"]
        assert planning.generate_due(at(29, 15))["created"] == 0


def test_after_completion_missed_due_generates_one_original_due_round():
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 15))
        assert planning.generate_due(at(30))["created"] == 1
        new = c.rows[-1]
        assert new["schedule_date"] == new["for_date"] == "2026-09-27"
        assert (new["display_cycle_date"], new["display_reason"]) == ("2026-09-30", "carryover")
        assert planning.generate_due(at(30))["created"] == 0


def test_discard_this_starts_after_completion_cycle_and_history_survives_cleanup():
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "discarded_this"}, at(24, 15))
        assert first["handled_at"] == at(24, 15).isoformat()
        assert planning.generate_due(at(27, 14, 59))["created"] == 0
        assert planning.generate_due(at(27, 15))["created"] == 1
        with mock.patch.object(planning, "_rows", return_value=c.rows):
            planning.cleanup_discarded(at(29, 16))
        assert first in c.rows


def test_partial_keeps_round_open_and_does_not_start_after_completion_interval():
    # Phase 1R：部分完成是开放生命周期里的一次事实记录——不关闭、不关闭时间、
    # 不作为刷新基准。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(
            first["id"], {"status": "partial", "partial_note": "一部分"}, at(24, 15))
        assert first["status"] == "partial"
        assert first["closed_at"] is None
        assert first["partial_at"] == at(24, 15).isoformat()
        assert first.get("handled_at") is None
        # 仍然开放：维护顺延后出现在当前看板进度中。
        planning.generate_due(at(25))
        board = planning.today_board(at(25))
        assert [item["id"] for item in board["progress"]] == [first["id"]]
        assert first["display_cycle_date"] == "2026-09-25"
        assert first["display_reason"] == "carryover"
        assert planning.generate_due(at(30))["created"] == 0
        assert len(c.rows) == 1


@pytest.mark.parametrize("final_status", ["completed", "discarded_this"])
def test_partial_then_final_handling_uses_final_confirmation_time(final_status):
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "partial", "partial_note": "部分完成"}, at(24, 15))
        assert first.get("handled_at") is None
        assert first["partial_at"] == at(24, 15).isoformat()
        assert c.db.rows["planning_task"][0].get("refresh_next_due_at") is None
        assert planning.generate_due(at(26, 15, 59))["created"] == 0
        planning.set_occurrence_status(first["id"], {"status": final_status}, at(26, 16))
        assert first["handled_at"] == at(26, 16).isoformat()
        assert first["actual_end"] == at(26, 16).isoformat()
        assert first["closed_at"] == at(26, 16).isoformat()
        # 部分完成时间作为历史保留，最终完成时间单独记录。
        assert first["partial_at"] == at(24, 15).isoformat()
        task = c.db.rows["planning_task"][0]
        assert task["last_handled_at"] == at(26, 16).isoformat()
        assert task["refresh_next_due_at"] == at(29, 16).isoformat()
        planning.set_occurrence_status(first["id"], {"status": final_status}, at(26, 17))
        assert first["handled_at"] == at(26, 16).isoformat()
        assert task["refresh_next_due_at"] == at(29, 16).isoformat()
        assert planning.generate_due(at(27, 15))["created"] == 0
        assert planning.generate_due(at(29, 15, 59))["created"] == 0
        assert planning.generate_due(at(29, 16))["created"] == 1
        assert planning.generate_due(at(29, 16))["created"] == 0
        assert len(c.rows) == 2
        assert c.rows[-1]["schedule_date"] == "2026-09-29"


def test_weekday_and_monthday_use_calendar_and_skip_invalid_date():
    with Context() as c:
        c.create("weekly", at(24), weekdays=[3])  # Thursday
        c.create("monthly", at(24), month_days=[31])
        result = planning.generate_due(at(30))
        assert result["created"] == 0  # September 24 already exists; no September 31.
        weekly = [row for row in c.rows if row["task_id"] == 1]
        monthly = [row for row in c.rows if row["task_id"] == 2]
        assert {row["schedule_date"] for row in weekly} == {"2026-09-24"}
        assert monthly == []
        planning.generate_due(datetime(2026, 10, 31, 7, tzinfo=CST))
        assert [row["schedule_date"] for row in monthly] == []  # local list is a snapshot
        assert {row["schedule_date"] for row in c.rows if row["task_id"] == 2} == {"2026-10-31"}


def test_early_action_retry_uses_request_identity_for_existing_and_new_rounds():
    with Context() as c:
        task = c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        first = planning.complete_task_early(task["id"], at(24, 15), idempotency_key="operation-1")
        repeated = planning.complete_task_early(task["id"], at(25), idempotency_key="operation-1")
        assert repeated["id"] == first["id"]
        assert len(c.rows) == 1
        second = planning.complete_task_early(task["id"], at(25, 15), idempotency_key="operation-2")
        again = planning.complete_task_early(task["id"], at(26), idempotency_key="operation-2")
        assert second["id"] == again["id"]
        assert len(c.rows) == 2


def test_early_completion_resets_after_completion_baseline_but_not_fixed_axis():
    with Context() as c:
        fixed = c.create("interval", at(24, 7), refresh_mode="fixed_interval", interval_days=3)
        planning.complete_task_early(fixed["id"], at(24, 8), idempotency_key="fixed-1")
        assert c.db.rows["planning_task"][0].get("refresh_next_due_at") is None
        assert c.db.rows["planning_task"][0]["refresh_anchor_at"] == at(24, 7).isoformat()
        after = c.create("interval", at(24, 7), refresh_mode="after_completion", interval_days=3)
        planning.complete_task_early(after["id"], at(24, 8), idempotency_key="after-1")
        task = c.db.rows["planning_task"][1]
        assert task["last_handled_at"] == at(24, 8).isoformat()
        assert task["refresh_next_due_at"] == at(27, 8).isoformat()


def test_early_api_requires_idempotency_key_and_preserves_auth_order():
    with Context() as c, mock.patch.object(cfg, "GATEWAY_TOKEN", "phase1b-token"):
        task = c.create("interval", at(24), refresh_mode="fixed_interval", interval_days=3)
        http = TestClient(Starlette(routes=list(planning_api_routes)))
        url = f"/admin/api/planning/tasks/{task['id']}/complete-early"
        assert http.post(url).status_code == 401
        auth = {"Authorization": "Bearer phase1b-token"}
        assert http.post(url, headers=auth).status_code == 400
        headers = {**auth, "Idempotency-Key": "api-early-1"}
        first = http.post(url, headers=headers)
        second = http.post(url, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json()["id"] == second.json()["id"]
        assert len(c.rows) == 1


def test_hollow_generation_has_one_round_and_replay_cannot_duplicate_phases():
    with Context() as c:
        c.create("once", at(24), target_date="2026-09-24", is_hollow=True,
                 hollow_start_minutes=10, hollow_wait_minutes=30, hollow_end_minutes=5)
        assert len(c.rows) == 2
        assert {row["round_key"] for row in c.rows} == {"once"}
        assert len({row["phase_group"] for row in c.rows}) == 1
        assert planning.generate_due(at(25))["created"] == 0
        assert {row["display_cycle_date"] for row in c.rows} == {"2026-09-25"}


def test_fixed_hollow_timeout_updates_both_phases_of_old_round():
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3,
                 is_hollow=True, hollow_start_minutes=10, hollow_wait_minutes=30,
                 hollow_end_minutes=5)
        planning.generate_due(at(27, 6))
        old = [row for row in c.rows if row["schedule_date"] == "2026-09-24"]
        new = [row for row in c.rows if row["schedule_date"] == "2026-09-27"]
        assert len(old) == len(new) == 2
        assert {row["status"] for row in old} == {"timeout"}
        assert {row["status"] for row in new} == {"pending"}


def test_phase1b_migration_keeps_round_uniqueness_and_separate_refresh_fields():
    root = Path(__file__).resolve().parents[1]
    phase1a = (root / "supabase/migrations/20260924010000_planning_phase1a_domain_identity.sql").read_text(encoding="utf-8")
    phase1b = (root / "supabase/migrations/20260924020000_planning_phase1b_refresh.sql").read_text(encoding="utf-8")
    assert "planning_occurrence_round_phase_uq" in phase1a
    assert "planning.daily_refresh_enabled" in phase1b
    assert "refresh_generated_through" in phase1b
    assert "refresh_next_due_at" in phase1b
    assert "planning_occurrence_generation_request_uq" in phase1b
    assert "cursor_date" not in phase1b and "next_due " not in phase1b
    # 任务级边界快照随证据机制一并退役：轮次身份不依赖边界。
    assert "refresh_round_boundary_time" not in phase1b
    assert "boundary_shift" not in phase1a.lower()
