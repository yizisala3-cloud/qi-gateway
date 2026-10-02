"""Shared planning row factories and frozen-fact snapshots."""

from gateway import planning
from gateway.planning_domain import BUSINESS_TIMEZONE
from .planning_context import at


HOLLOW = dict(is_hollow=True, hollow_start_content="准备", hollow_start_minutes=30,
              hollow_wait_minutes=60, hollow_end_content="收尾", hollow_end_minutes=30)


def datetime_tz(day, hour, minute=0):
    from datetime import datetime
    return datetime(2026, 9, day, hour, minute, tzinfo=BUSINESS_TIMEZONE)


def iso(day, hour, minute=0):
    return planning._iso(datetime_tz(day, hour, minute))


def iso_dt(y, m, d, hour, minute=0):
    from datetime import datetime
    return planning._iso(datetime(y, m, d, hour, minute, tzinfo=BUSINESS_TIMEZONE))


def _fields(occ):
    """已生成 occurrence 的冻结事实快照（身份 + 窗口 + fixed 生命周期 + est）。

    ``display_cycle_date`` 不在内：合法顺延使其前进（§6.3），不属于冻结事实；
    冻结不变量针对窗口、身份与 fixed 生命周期事实（§28.1、不变量 36）。"""
    return {key: occ.get(key) for key in (
        "id", "task_id", "round_key", "schedule_date",
        "window_start_at", "window_end_at", "fixed_due_at", "fixed_expires_at",
        "planned_minutes", "status", "est_start", "est_end",
        "estimated_time_source", "fixed_source", "is_fixed",
    )}



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



def _timeout_occ(c):
    occ = c.rows[0]
    occ["status"] = "timeout"
    return occ


def _once_tasks(c):
    return [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]


def _once_occs(c):
    once_ids = {row["id"] for row in _once_tasks(c)}
    return [row for row in c.rows if row["task_id"] in once_ids]



def _after_completion_task(c, created=at(24, 7)):
    return c.create("interval", created, refresh_mode="after_completion", interval_days=3)
