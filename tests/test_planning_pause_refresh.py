"""Phase 1R 暂停刷新 / 恢复刷新（需求 24）定向场景。

只验证既有 refresh_enabled 后端能力端到端成立：暂停只阻止未来周期轮次
生成，不关闭/修改当前实例，不改任务定义、周期规则、历史事实与固定时间
轴；恢复后完全沿用现有生成/补生成规则——daily 不补历史日，fixed 沿原
固定轴（漏跑语义不变），after_completion 从持久化处理时间继续。暂停本
身不写入任何完成事实，也不经 is_active 模拟。

场景覆盖需求验收 A–H：每日、固定间隔、处理后刷新、weekly/monthly、
开放实例保护、历史事实隔离、连续操作收敛、服务端持久化。
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from test_planning_phase1a import _Database


CST = timezone(timedelta(hours=8))


def at(day, hour=7, minute=0, month=9):
    return datetime(2026, month, day, hour, minute, tzinfo=CST)


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


def test_daily_pause_blocks_rounds_and_resume_continues_without_backfill():
    # A：暂停后不生成下一周期；当前实例保留；恢复后按 daily 规则继续，
    # 不补暂停期间历史日。
    with Context() as c:
        task = c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        first = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-24")
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8))
        assert c.db.rows["planning_task"][0]["is_active"] is True
        assert c.db.rows["planning_task"][0].get("last_handled_at") is None
        assert planning.generate_due(at(25, 6))["created"] == 0
        assert planning.generate_due(at(26, 6))["created"] == 0
        # 当前实例保留、未关闭、仍随周期顺延展示（暂停不动它）
        assert first["status"] == "pending" and first.get("closed_at") is None
        assert (first["round_key"], first["schedule_date"]) == (
            "cycle:2026-09-24", "2026-09-24")
        assert first["display_cycle_date"] == "2026-09-26"
        planning.update_task(task["id"], {"refresh_enabled": True}, at(26, 8))
        planning.generate_due(at(26, 8, 30))
        keys = [row["round_key"] for row in c.rows]
        assert "cycle:2026-09-25" not in keys  # 不补暂停期间历史日
        assert keys.count("cycle:2026-09-26") == 1  # 当期轮恰一轮，不重复
        assert planning.generate_due(at(27, 6))["created"] == 1
        assert c.rows[-1]["round_key"] == "cycle:2026-09-27"


def test_fixed_interval_pause_keeps_anchor_and_resume_continues_on_axis():
    # B：暂停不改 refresh_anchor_at 与固定时间轴；恢复后沿原固定轴继续，
    # 恢复时间不成为新 anchor。
    with Context() as c:
        task = c.create("interval", at(24, 7), refresh_mode="fixed_interval", interval_days=3)
        anchor = task["refresh_anchor_at"]
        planning.generate_due(at(25, 7))
        planning.update_task(task["id"], {"refresh_enabled": False}, at(25, 8))
        # 原轴 9/27、9/30 到期事件在暂停期间都不生成
        assert planning.generate_due(at(27, 7))["created"] == 0
        assert planning.generate_due(at(29, 7))["created"] == 0
        # 暂停期间既有开放轮完全冻结：不被关闭、不被清理（需求 24E）
        axis_round = next(row for row in c.rows if row["round_key"].startswith("fixed:2026-09-24:"))
        assert axis_round["status"] == "pending"
        assert axis_round.get("closed_at") is None
        fresh = c.db.rows["planning_task"][0]
        assert fresh["refresh_anchor_at"] == anchor
        assert fresh["refresh_generated_through"] == "2026-09-24"
        planning.update_task(task["id"], {"refresh_enabled": True}, at(29, 8))
        planning.generate_due(at(29, 8, 30))
        resumed = [row for row in c.rows if row["round_key"].startswith("fixed:2026-09-27:")]
        assert len(resumed) == 1  # 沿原固定轴补上当期轮，且不重复
        resumed = resumed[0]
        assert resumed["schedule_date"] == "2026-09-29"  # fixed_interval 出生在当前周期
        assert resumed["fixed_due_at"] == at(27, 7).isoformat()
        assert resumed["status"] == "pending"  # 最新轴轮开放可处理
        # 此后仍按原 3 天节奏走（9/30），而不是从恢复时间另起一条轴
        assert planning.generate_due(at(30, 7))["created"] == 1
        assert c.rows[-1]["round_key"].startswith("fixed:2026-09-30:")
        # 恢复补生成后，错过的旧轮按固定型「到期死亡」关闭（需求 8.4）
        assert axis_round["status"] == "timeout"
        assert axis_round["closed_at"] == at(27, 7).isoformat()


def test_after_completion_pause_keeps_facts_and_resume_continues_from_baseline():
    # C：暂停不改 last_handled_at / refresh_next_due_at；暂停期间完成当前
    # 实例时完成事实照常保存；恢复后按现有 after_completion 规则继续。
    with Context() as c:
        task = c.create("interval", at(24, 7), refresh_mode="after_completion", interval_days=3)
        first = c.rows[0]
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8))
        fresh = c.db.rows["planning_task"][0]
        assert fresh["is_active"] is True
        assert fresh.get("last_handled_at") is None  # 暂停不是一次完成
        # 暂停期间完成当前实例：完成事实与基准照常落库
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 9))
        assert first["status"] == "completed"
        assert first["handled_at"] == at(24, 9).isoformat()
        fresh = c.db.rows["planning_task"][0]
        assert fresh["last_handled_at"] == at(24, 9).isoformat()
        assert fresh["refresh_next_due_at"] == at(27, 9).isoformat()
        # 暂停期间即使到期也不生成新轮
        assert planning.generate_due(at(27, 10))["created"] == 0
        planning.update_task(task["id"], {"refresh_enabled": True}, at(27, 11))
        planning.generate_due(at(27, 11, 30))
        resumed = [row for row in c.rows if row["round_key"].startswith("handled:2026-09-27:")]
        assert len(resumed) == 1  # 从持久化处理时间推进的下一轮，恰一轮
        assert resumed[0]["status"] == "pending"
        # 历史完成事实未被恢复操作改写
        assert first["handled_at"] == at(24, 9).isoformat()
        assert c.db.rows["planning_task"][0]["last_handled_at"] == at(24, 9).isoformat()


def test_weekly_pause_and_resume_continues_on_calendar_axis():
    # D：9/24（周四）创建并已生成当轮，暂停覆盖 10/1（周四）：期间不生成；
    # 恢复后按现有固定型漏跑语义继续（需求 7.1.1：星期刷新漏跑语义不变），
    # 轮次身份仍是原周期 10/1，不是恢复日。
    with Context() as c:
        task = c.create("weekly", at(24, 7), weekdays=[3])
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8))
        assert planning.generate_due(at(26, 7))["created"] == 0
        assert planning.generate_due(at(30, 7))["created"] == 0
        planning.update_task(task["id"], {"refresh_enabled": True}, at(1, 16, month=10))
        planning.generate_due(at(1, 16, 30, month=10))
        resumed = [row for row in c.rows if row["round_key"] == "cycle:2026-10-01"]
        assert len(resumed) == 1
        resumed = resumed[0]
        assert resumed["schedule_date"] == "2026-10-01"
        assert resumed["status"] == "pending"
        assert planning.generate_due(at(8, 7, month=10))["created"] == 1
        assert c.rows[-1]["round_key"] == "cycle:2026-10-08"


def test_monthly_pause_and_resume_continues_on_calendar_axis():
    with Context() as c:
        task = c.create("monthly", at(24, 7), month_days=[15])
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8))
        assert planning.generate_due(at(15, 7, month=10))["created"] == 0
        planning.update_task(task["id"], {"refresh_enabled": True}, at(15, 9, month=10))
        planning.generate_due(at(15, 9, 30, month=10))
        assert [row["round_key"] for row in c.rows].count("cycle:2026-10-15") == 1
        assert c.rows[-1]["schedule_date"] == "2026-10-15"


def test_pause_never_closes_or_alters_open_occurrences():
    # E：pending / in_progress / partial 实例不因暂停被关闭、删除或改状态。
    with Context() as c:
        daily = c.create("daily", at(23))
        weekly = c.create("weekly", at(23, 8), weekdays=[3])
        planning.generate_due(at(24, 6))
        pending_row = next(row for row in c.rows if row["task_id"] == weekly["id"])
        started = next(row for row in c.rows if row["task_id"] == daily["id"])
        planning.set_occurrence_status(started["id"], {"status": "in_progress"}, at(24, 7))
        planning.set_occurrence_status(
            pending_row["id"],
            {"status": "partial", "partial_note": "进行了一半"}, at(24, 7, 30),
        )
        planning.update_task(daily["id"], {"refresh_enabled": False}, at(24, 8))
        planning.update_task(weekly["id"], {"refresh_enabled": False}, at(24, 8))
        snapshot = {
            row["id"]: (row["status"], row.get("closed_at"), row.get("handled_at"),
                        row.get("partial_note"), row["round_key"], row["schedule_date"])
            for row in c.rows
        }
        assert {row["status"] for row in c.rows} <= {"in_progress", "partial", "pending"}
        for _ in range(3):
            planning.generate_due(at(26, 6))
        planning.update_task(daily["id"], {"refresh_enabled": True}, at(26, 7))
        planning.update_task(weekly["id"], {"refresh_enabled": True}, at(26, 7))
        planning.generate_due(at(27, 6))
        for row in c.rows:
            if row["id"] in snapshot:
                assert (row["status"], row.get("closed_at"), row.get("handled_at"),
                        row.get("partial_note"), row["round_key"], row["schedule_date"]) \
                    == snapshot[row["id"]]
        assert c.db.rows["planning_task"][0]["is_active"] is True


def test_pause_and_resume_leave_closed_history_untouched():
    # F：completed / discarded_this 历史实例完全不被修改。
    with Context() as c:
        task = c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 7))
        planning.generate_due(at(25, 6))
        second = c.rows[-1]
        planning.set_occurrence_status(second["id"], {"status": "discarded_this"}, at(25, 8))
        pending_row = next(row for row in c.rows if row["status"] == "pending")
        history = {
            row["id"]: (row["status"], row.get("handled_at"), row.get("closed_at"),
                        row.get("updated_at"))
            for row in c.rows if row["status"] in ("completed", "discarded_this")
        }
        assert len(history) == 2
        planning.update_task(task["id"], {"refresh_enabled": False}, at(25, 9))
        for _ in range(2):
            planning.generate_due(at(27, 6))
        planning.update_task(task["id"], {"refresh_enabled": True}, at(27, 7))
        planning.generate_due(at(28, 6))
        for row in c.rows:
            if row["id"] in history:
                assert (row["status"], row.get("handled_at"), row.get("closed_at"),
                        row.get("updated_at")) == history[row["id"]]
        # 暂停前已开放的实例不被关闭（其顺延展示更新属正常行为）
        assert next(row for row in c.rows if row["id"] == pending_row["id"])["status"] == "pending"


def test_consecutive_pause_resume_toggles_converge_without_new_rounds():
    # G：暂停→暂停、恢复→恢复、暂停→恢复→暂停 稳定幂等，不制造新实例。
    with Context() as c:
        task = c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        before = len(c.rows)
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8))
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8, 30))
        assert c.db.rows["planning_task"][0]["refresh_enabled"] is False
        assert planning.generate_due(at(25, 6))["created"] == 0
        planning.update_task(task["id"], {"refresh_enabled": True}, at(25, 7))
        planning.update_task(task["id"], {"refresh_enabled": True}, at(25, 7, 30))
        planning.generate_due(at(25, 8))
        keys = [row["round_key"] for row in c.rows]
        assert keys.count("cycle:2026-09-25") == 1  # 恢复即生成当期轮，重复恢复不加倍
        assert len(c.rows) == before + 1
        planning.update_task(task["id"], {"refresh_enabled": False}, at(25, 9))
        assert planning.generate_due(at(26, 6))["created"] == 0
        assert len(c.rows) == before + 1
        assert c.db.rows["planning_task"][0]["refresh_enabled"] is False


def test_refresh_enabled_is_persisted_server_state_and_validated_bool():
    # H：状态来自服务端持久化；非布尔值拒绝；is_active 与时间轴不受影响。
    with Context() as c:
        task = c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        paused = planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 8))
        assert paused["refresh_enabled"] is False
        assert paused["is_active"] is True
        stored = planning.list_tasks(include_inactive=True, now=at(24, 8, 30))
        assert [row["refresh_enabled"] for row in stored if row["id"] == task["id"]] == [False]
        with pytest.raises(planning.PlanningError):
            planning.update_task(task["id"], {"refresh_enabled": "false"}, at(24, 9))
        with pytest.raises(planning.PlanningError):
            planning.update_task(task["id"], {"refresh_enabled": 0}, at(24, 9))
        resumed = planning.update_task(task["id"], {"refresh_enabled": True}, at(24, 10))
        assert resumed["refresh_enabled"] is True


def test_pause_api_patch_round_trip_and_validation():
    # API 层：PATCH /tasks/{id} 的 refresh_enabled 走通，非法值 400。
    with Context() as c, mock.patch.object(cfg, "GATEWAY_TOKEN", "pause-token"):
        task = c.create("daily", at(23))
        http = TestClient(Starlette(routes=list(planning_api_routes)))
        auth = {"Authorization": "Bearer pause-token",
                "Content-Type": "application/json"}
        url = f"/admin/api/planning/tasks/{task['id']}"
        assert http.patch(url, headers=auth, json={"refresh_enabled": False}).status_code == 200
        assert http.patch(url, headers=auth, json={"refresh_enabled": False}).json()[
            "refresh_enabled"] is False
        assert c.db.rows["planning_task"][0]["refresh_enabled"] is False
        assert http.patch(url, headers=auth, json={"refresh_enabled": "no"}).status_code == 400
        # 暂停/恢复必须是明确布尔值：null 静默暂停是语义陷阱，明确拒绝
        assert http.patch(url, headers=auth, json={"refresh_enabled": None}).status_code == 400
        assert http.patch(url, headers=auth, json={"refresh_enabled": True}).json()[
            "refresh_enabled"] is True
        assert c.db.rows["planning_task"][0]["is_active"] is True
