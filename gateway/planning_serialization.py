"""Read-only planning task and occurrence serialization.

Generated rows use frozen display/rule snapshots; effective duration shares
its authority with scheduling through planning_common."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from . import planning_common as common


# ── 序列化 ────────────────────────────────────────────────────────

def _display_content(task: dict[str, Any], occ: dict[str, Any]) -> str:
    if occ.get("phase") == "start":
        return f"{task.get('hollow_start_content') or task['content']}·开始"
    if occ.get("phase") == "end":
        return f"{task.get('hollow_end_content') or task['content']}·结束"
    return task["content"]


def _deadline_for(task: dict[str, Any], occ: dict[str, Any]) -> str | None:
    end_tod = task.get("deadline_end_tod") or task.get("deadline_tod")
    if not occ.get("is_limited") or not end_tod:
        return None
    if not occ.get("schedule_date"):
        return None  # legacy identity is not guessed from for_date
    schedule_date = date.fromisoformat(occ["schedule_date"])
    tod = time.fromisoformat(end_tod)
    return common._iso(common._combine(schedule_date, tod))


def schedule_label(occ: dict[str, Any], task: dict[str, Any], now: datetime) -> str:
    """排列状态标签：超时 / 落后 / 前进 / 正常（仅展示；「落后」避开与「延后」状态撞名）。"""
    if occ["status"] == "timeout":
        return "超时"
    if occ["status"] == "deferred":
        return "落后"
    est_start = common._parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
    if est_start and est_start < now and occ["status"] == "pending":
        return "落后"
    nominal = occ.get("nominal_start")
    if est_start and nominal and est_start < common._parse_dt(nominal, "nominal_start") - timedelta(seconds=60):
        return "前进"
    return "正常"


def serialize_occurrence(occ: dict[str, Any], task: dict[str, Any], now: datetime) -> dict[str, Any]:
    # BF5：已生成实例（round_key 非空）的展示内容、规则语义与截止时刻一律
    # 来自生成时快照，任务定义后续修改不重新解释历史；round_key 为空的旧
    # 实例保持既有兜底（按任务定义计算，等待受控升级）。
    generated = bool(occ.get("round_key"))
    if generated:
        is_hollow = occ.get("phase") is not None
        deadline_at = occ.get("deadline_at")
        if deadline_at is None and occ.get("is_limited"):
            deadline_at = _deadline_for(task, occ)  # 防御：缺快照时按任务计算
    else:
        is_hollow = task.get("is_hollow", False)
        deadline_at = _deadline_for(task, occ)
    return {
        "id": occ["id"],
        "task_id": occ["task_id"],
        "for_date": occ["for_date"],
        # Null means an old instance whose identity cannot be safely inferred.
        "round_key": occ.get("round_key"),
        "schedule_date": occ.get("schedule_date"),
        "display_cycle_date": occ.get("display_cycle_date"),
        "display_reason": occ.get("display_reason"),
        "phase_group": occ.get("phase_group"),
        "phase": occ.get("phase"),
        "content": occ.get("display_content") or _display_content(task, occ),
        "task_content": occ.get("content_snapshot") or task["content"],
        "task_type": task["task_type"],
        "time_mode": occ.get("time_mode_snapshot") or task["time_mode"],
        "task_is_active": task["is_active"],
        "status": occ["status"],
        "est_start": occ.get("est_start"),
        "est_end": occ.get("est_end"),
        "nominal_start": occ.get("nominal_start"),
        "actual_start": occ.get("actual_start"),
        "actual_end": occ.get("actual_end"),
        "handled_at": occ.get("handled_at"),
        "partial_at": occ.get("partial_at"),
        "actual_minutes": occ.get("actual_minutes"),
        # 完成耗时手填（2026-10-01 确认，§12.3）：独立秒粒度字段，与自动
        # actual_* 并存互不覆盖；NULL = 未手填（展示回退预估并标注预估）。
        "actual_logged_seconds": occ.get("actual_logged_seconds"),
        # 有效耗时单一权威语义（N5）：显式区间优先，与排程同源；任务定义
        # 修改只影响未来轮次。
        "estimated_minutes": common._effective_minutes(occ, task),
        "partial_note": occ.get("partial_note"),
        "sort_order": occ.get("sort_order", 0),
        "is_fixed": occ.get("is_fixed", False),
        "estimated_time_source": occ.get("estimated_time_source"),
        "fixed_source": occ.get("fixed_source"),
        "schedule_managed": occ.get("schedule_managed"),
        "is_limited": occ.get("is_limited", False),
        # 生成时冻结的实例窗口（§6.7）；NULL = 无该端约束。
        "window_start_at": occ.get("window_start_at"),
        "window_end_at": occ.get("window_end_at"),
        "is_hollow": is_hollow,
        "deadline_at": deadline_at,
        "alarm_start": task.get("alarm_start", False),
        "alarm_end": task.get("alarm_end", False),
        "timer_minutes": task.get("timer_minutes"),
        "source": occ.get("source", "schedule"),
        "closed_at": occ.get("closed_at"),
        "schedule_label": schedule_label(occ, task, now),
    }


def serialize_task(task: dict[str, Any], now: datetime) -> dict[str, Any]:
    next_due = task.get("next_due")
    return {
        "id": task["id"],
        "content": task["content"],
        "task_type": task["task_type"],
        "refresh_mode": task.get("refresh_mode"),
        "refresh_anchor_at": task.get("refresh_anchor_at"),
        "last_handled_at": task.get("last_handled_at"),
        "refresh_next_due_at": task.get("refresh_next_due_at"),
        "refresh_enabled": task.get("refresh_enabled"),
        "refresh_generated_through": task.get("refresh_generated_through"),
        "request_state": task.get("request_state"),
        "request_est_start": task.get("request_est_start"),
        "interval_days": task.get("interval_days"),
        "weekdays": task.get("weekdays"),
        "month_days": task.get("month_days"),
        "target_date": task.get("target_date"),
        "time_mode": task["time_mode"],
        "estimated_minutes": task.get("estimated_minutes"),
        # 模板窗口（2026-09-27 窗口批次的新业务事实来源）；旧 explicit /
        # deadline 字段仅为存量行兼容保留，停止新写入。
        "window_start_tod": task.get("window_start_tod"),
        "window_end_tod": task.get("window_end_tod"),
        "est_start_tod": task.get("est_start_tod"),
        "est_end_tod": task.get("est_end_tod"),
        "is_fixed": task.get("is_fixed", False),
        "is_limited": task.get("deadline_tod") is not None,
        "deadline_tod": task.get("deadline_tod"),
        "deadline_end_tod": task.get("deadline_end_tod"),
        "is_hollow": task.get("is_hollow", False),
        "hollow_start_content": task.get("hollow_start_content"),
        "hollow_start_minutes": task.get("hollow_start_minutes"),
        "hollow_wait_minutes": task.get("hollow_wait_minutes"),
        "hollow_wait_note": task.get("hollow_wait_note"),
        "hollow_end_content": task.get("hollow_end_content"),
        "hollow_end_minutes": task.get("hollow_end_minutes"),
        "alarm_start": task.get("alarm_start", False),
        "alarm_end": task.get("alarm_end", False),
        "timer_minutes": task.get("timer_minutes"),
        "is_active": task["is_active"],
        # once 已生成标记（§28.3；list_tasks 聚合填充，其它入口缺省 False）
        "has_generated_occurrence": bool(task.get("has_generated_occurrence")),
        "cursor_date": task.get("cursor_date"),
        "next_due": next_due,
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }
