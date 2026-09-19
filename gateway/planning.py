"""规划管理一期：独立待办体系的核心逻辑。

需求与产品决策的唯一来源：
``前端/前端后续改动方向/规划管理-需求与一期约定.md``；
工程约定：``后端/后端后续改动方向/规划管理-一期-后端实现要点.md``。

红线：
* 与现有 ``public.todos`` 表、``/v1/todos`` 与聊天路径完全无关。
* 出现实例的生成只依据任务定义上的规则游标（``cursor_date`` / ``next_due``），
  绝不以出现记录的存在性为依据——72 小时清理删除已废弃 / 此次废弃记录后，
  后续生成既不会重复也不会遗漏（有回归测试覆盖）。
* 时区一律北京时间（Asia/Shanghai）。
* 出现实例永久保留，仅废弃 / 此次废弃满 72 小时清理；已完成 / 部分完成 /
  延后的记录永久保留。
"""
from __future__ import annotations

import logging
import re
import threading
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .db import get_client

log = logging.getLogger("gateway.planning")

_CST = timezone(timedelta(hours=8))

TASK_TYPES = ("daily", "interval", "weekly", "monthly", "once", "idle")
# 重复型任务（「此次废弃」仅对这些类型开放；单次待办没有）
REPEATING_TASK_TYPES = ("daily", "interval", "weekly", "monthly", "idle")
TIME_MODES = ("duration", "explicit")
OCCURRENCE_STATUSES = (
    "pending", "in_progress", "completed", "partial",
    "deferred", "discarded_this", "discarded", "timeout",
)
# 关闭态记录（当前待办「已完成」分区可见且可修改）
CLOSED_STATUSES = ("completed", "partial", "discarded_this", "discarded")
OPEN_STATUSES = ("pending", "in_progress", "deferred")

RECOMPUTE_WAIT = timedelta(minutes=15)
DISCARD_RETENTION = timedelta(hours=72)

MAX_CONTENT_LENGTH = 500
MAX_NOTE_LENGTH = 1000
MAX_LIST_ROWS = 500
DEFAULT_LIST_ROWS = 200

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SHORTHAND_RE = re.compile(r"^(?:(\d+)\s*h)?(?:(\d+)\s*m)?(?:(\d+)\s*s)?$")

_maintenance_lock = threading.Lock()


class PlanningError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


# ── 时间工具 ──────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(_CST)


def _cst_date(value: datetime) -> date:
    return value.astimezone(_CST).date()


def _iso(value: datetime) -> str:
    return value.astimezone(_CST).isoformat()


def _parse_dt(value: Any, field: str = "datetime") -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PlanningError("invalid_payload", f"{field} must be an ISO datetime") from exc
    else:
        raise PlanningError("invalid_payload", f"{field} is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CST)
    return parsed.astimezone(_CST)


def _parse_date(value: Any, field: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _DATE_RE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise PlanningError("invalid_payload", f"{field} is not a real date") from exc
    raise PlanningError("invalid_payload", f"{field} must be YYYY-MM-DD")


def _parse_tod(value: Any, field: str) -> time:
    if isinstance(value, time):
        return value.replace(second=0, microsecond=0)
    if isinstance(value, str) and _TIME_RE.match(value.strip()):
        hour, minute = value.strip().split(":")
        return time(int(hour), int(minute))
    raise PlanningError("invalid_payload", f"{field} must be HH:MM")


def _tod_str(value: Any, field: str) -> str | None:
    return _parse_tod(value, field).strftime("%H:%M") if value else None


def parse_duration_shorthand(value: Any, field: str) -> int:
    """计时器/耗时简写：整数分钟，或 ``1h`` / ``30m`` / ``1h30m`` / ``1m30s``。

    秒数进位到分钟（最少 1 分钟），与前端展示一致。
    """
    if isinstance(value, bool):
        raise PlanningError("invalid_payload", f"{field} must be minutes or shorthand")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        if text.isdigit():
            minutes = int(text)
        else:
            match = _SHORTHAND_RE.match(text)
            if not match or not any(match.groups()):
                raise PlanningError("invalid_payload", f"{field} shorthand must be like 1h30m")
            hours, mins, secs = (int(g) if g else 0 for g in match.groups())
            minutes = -(-((hours * 60 + mins) * 60 + secs) // 60)
    else:
        raise PlanningError("invalid_payload", f"{field} must be minutes or shorthand")
    if not 1 <= minutes <= 1440:
        raise PlanningError("invalid_payload", f"{field} must be between 1 and 1440 minutes")
    return minutes


def _combine(for_date: date, tod: time) -> datetime:
    return datetime.combine(for_date, tod, tzinfo=_CST)


def _minutes_between(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() / 60))


# ── 数据库访问 ────────────────────────────────────────────────────

def _require_client():
    client = get_client()
    if not client:
        raise PlanningError("database_unavailable", "Supabase is not configured", 503)
    return client


def _rows(client, table: str, query_fn=None) -> list[dict[str, Any]]:
    query = client.table(table).select("*")
    if query_fn:
        query = query_fn(query)
    response = query.execute()
    return response.data or []


def _fetch_task(client, task_id: int) -> dict[str, Any] | None:
    rows = _rows(client, "planning_task", lambda q: q.eq("id", task_id).limit(1))
    return rows[0] if rows else None


def _fetch_occurrence(client, occurrence_id: int) -> dict[str, Any] | None:
    rows = _rows(client, "planning_occurrence", lambda q: q.eq("id", occurrence_id).limit(1))
    return rows[0] if rows else None


def _task_map(client, task_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not task_ids:
        return {}
    rows = _rows(client, "planning_task", lambda q: q.in_("id", sorted(task_ids)))
    return {row["id"]: row for row in rows}


# ── 校验 ──────────────────────────────────────────────────────────

def _clean_text(value: Any, field: str, *, required: bool, maximum: int) -> str | None:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise PlanningError("invalid_payload", f"{field} must be a string")
    if required and not text:
        raise PlanningError("invalid_payload", f"{field} is required")
    if len(text) > maximum:
        raise PlanningError("invalid_payload", f"{field} must not exceed {maximum} characters")
    return text or None


def _clean_int(value: Any, field: str, *, lo: int, hi: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanningError("invalid_payload", f"{field} must be an integer")
    if not lo <= value <= hi:
        raise PlanningError("invalid_payload", f"{field} must be between {lo} and {hi}")
    return value


def _clean_bool(value: Any, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlanningError("invalid_payload", f"{field} must be a boolean")
    return value


def _clean_int_list(value: Any, field: str, *, lo: int, hi: int) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        isinstance(v, bool) or not isinstance(v, int) for v in value
    ):
        raise PlanningError("invalid_payload", f"{field} must be an array of integers")
    if any(not lo <= v <= hi for v in value):
        raise PlanningError("invalid_payload", f"{field} values must be between {lo} and {hi}")
    unique = sorted(set(value))
    if not unique:
        raise PlanningError("invalid_payload", f"{field} must not be empty")
    return unique


def validate_task_payload(payload: Any, *, partial: bool = False) -> dict[str, Any]:
    """校验并规范化任务定义字段。

    ``partial=False``（创建）要求类型必填字段齐全；``partial=True``（编辑）
    只处理出现的字段。未知字段一律拒绝。预估耗时与显式起止同时填写时，
    以起止为准（已确认决策 12）：``time_mode`` 强制为 explicit。
    """
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")

    allowed = {
        "content", "task_type", "interval_days", "weekdays", "month_days",
        "target_date", "time_mode", "estimated_minutes",
        "est_start_tod", "est_end_tod", "is_fixed",
        "deadline_tod", "deadline_end_tod",
        "is_hollow", "hollow_start_content", "hollow_start_minutes",
        "hollow_wait_minutes", "hollow_wait_note", "hollow_end_content",
        "hollow_end_minutes",
        "alarm_start", "alarm_end", "timer_minutes", "is_active",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise PlanningError("invalid_payload", f"unsupported fields: {', '.join(sorted(unknown))}")
    if not payload and not partial:
        raise PlanningError("invalid_payload", "request body is empty")

    result: dict[str, Any] = {}
    if "content" in payload or not partial:
        content = _clean_text(payload.get("content"), "content", required=True, maximum=MAX_CONTENT_LENGTH)
        result["content"] = content

    task_type = payload.get("task_type", result.get("task_type"))
    if task_type is not None or not partial:
        text = str(task_type or "").strip().casefold()
        if text not in TASK_TYPES:
            raise PlanningError("invalid_payload", f"task_type must be one of {', '.join(TASK_TYPES)}")
        result["task_type"] = text
    effective_type: str | None = result.get("task_type")

    if "interval_days" in payload:
        result["interval_days"] = _clean_int(payload.get("interval_days"), "interval_days", lo=1, hi=3650)
    if "weekdays" in payload:
        result["weekdays"] = _clean_int_list(payload.get("weekdays"), "weekdays", lo=0, hi=6)
    if "month_days" in payload:
        result["month_days"] = _clean_int_list(payload.get("month_days"), "month_days", lo=1, hi=31)
    if "target_date" in payload:
        raw = payload.get("target_date")
        result["target_date"] = _parse_date(raw, "target_date").isoformat() if raw is not None else None
    if effective_type and not partial:
        requirements = {
            "interval": ("interval_days",),
            "weekly": ("weekdays",),
            "monthly": ("month_days",),
            "once": ("target_date",),
        }
        for field in requirements.get(effective_type, ()):
            if result.get(field) is None:
                raise PlanningError("invalid_payload", f"{field} is required for {effective_type} tasks")

    if "time_mode" in payload:
        mode = str(payload.get("time_mode") or "").strip().casefold()
        if mode not in TIME_MODES:
            raise PlanningError("invalid_payload", "time_mode must be duration or explicit")
        result["time_mode"] = mode

    if "estimated_minutes" in payload:
        raw = payload.get("estimated_minutes")
        result["estimated_minutes"] = (
            parse_duration_shorthand(raw, "estimated_minutes") if raw is not None else None
        )
    if "est_start_tod" in payload:
        raw = payload.get("est_start_tod")
        result["est_start_tod"] = _tod_str(raw, "est_start_tod")
    if "est_end_tod" in payload:
        raw = payload.get("est_end_tod")
        result["est_end_tod"] = _tod_str(raw, "est_end_tod")

    # 决策 12：起止与耗时同时出现时以起止为准。
    if result.get("est_start_tod"):
        result["time_mode"] = "explicit"
    if not partial:
        result.setdefault("time_mode", "duration")
        for flag in ("is_fixed", "is_hollow", "alarm_start", "alarm_end"):
            result.setdefault(flag, False)
        result.setdefault("is_active", True)

    if "is_fixed" in payload:
        result["is_fixed"] = _clean_bool(payload.get("is_fixed"), "is_fixed")
    if "alarm_start" in payload:
        result["alarm_start"] = _clean_bool(payload.get("alarm_start"), "alarm_start")
    if "alarm_end" in payload:
        result["alarm_end"] = _clean_bool(payload.get("alarm_end"), "alarm_end")
    if "timer_minutes" in payload:
        raw = payload.get("timer_minutes")
        result["timer_minutes"] = (
            parse_duration_shorthand(raw, "timer_minutes") if raw is not None else None
        )
    if "is_active" in payload:
        result["is_active"] = _clean_bool(payload.get("is_active"), "is_active")

    if "deadline_tod" in payload:
        raw = payload.get("deadline_tod")
        result["deadline_tod"] = _tod_str(raw, "deadline_tod")
    if "deadline_end_tod" in payload:
        raw = payload.get("deadline_end_tod")
        result["deadline_end_tod"] = _tod_str(raw, "deadline_end_tod")

    if "is_hollow" in payload:
        result["is_hollow"] = _clean_bool(payload.get("is_hollow"), "is_hollow")
    for field, maximum in (
        ("hollow_start_content", 200),
        ("hollow_wait_note", 200),
        ("hollow_end_content", 200),
    ):
        if field in payload:
            result[field] = _clean_text(payload.get(field), field, required=False, maximum=maximum)
    for field in ("hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes"):
        if field in payload:
            result[field] = _clean_int(payload.get(field), field, lo=1, hi=1440)

    if result.get("deadline_end_tod") and not (
        result.get("deadline_tod")
        or (partial and "deadline_tod" not in payload)
    ):
        raise PlanningError("invalid_payload", "deadline_end_tod requires deadline_tod")

    if not partial:
        mode = result.get("time_mode", "duration")
        if mode == "explicit":
            if not result.get("est_start_tod"):
                raise PlanningError("invalid_payload", "est_start_tod is required for explicit time mode")
            if not result.get("est_end_tod") and not result.get("estimated_minutes"):
                raise PlanningError(
                    "invalid_payload",
                    "explicit tasks need est_end_tod or estimated_minutes",
                )
        elif not result.get("estimated_minutes") and not result.get("est_start_tod"):
            raise PlanningError("invalid_payload", "estimated_minutes is required for duration tasks")
        if result.get("is_hollow"):
            for field in (
                "hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes",
            ):
                if not result.get(field):
                    raise PlanningError("invalid_payload", f"{field} is required for hollow tasks")
            if not (result.get("hollow_start_content") or result.get("content")):
                raise PlanningError("invalid_payload", "hollow_start_content is required for hollow tasks")
            if not (result.get("hollow_end_content") or result.get("content")):
                raise PlanningError("invalid_payload", "hollow_end_content is required for hollow tasks")
        if result.get("deadline_end_tod") and not result.get("deadline_tod"):
            raise PlanningError("invalid_payload", "deadline_end_tod requires deadline_tod")
    else:
        if result.get("time_mode") == "explicit" and "est_start_tod" in result and not result.get("est_start_tod"):
            raise PlanningError("invalid_payload", "est_start_tod cannot be empty for explicit tasks")

    return result


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
    for_date = date.fromisoformat(occ["for_date"])
    tod = time.fromisoformat(end_tod)
    return _iso(_combine(for_date, tod))


def schedule_label(occ: dict[str, Any], task: dict[str, Any], now: datetime) -> str:
    """排列状态标签：超时 / 延后 / 前进 / 正常（仅展示）。"""
    if occ["status"] == "timeout":
        return "超时"
    if occ["status"] == "deferred":
        return "延后"
    est_start = _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
    if est_start and est_start < now and occ["status"] == "pending":
        return "延后"
    nominal = occ.get("nominal_start")
    if est_start and nominal and est_start < _parse_dt(nominal, "nominal_start") - timedelta(seconds=60):
        return "前进"
    return "正常"


def serialize_occurrence(occ: dict[str, Any], task: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "id": occ["id"],
        "task_id": occ["task_id"],
        "for_date": occ["for_date"],
        "phase": occ.get("phase"),
        "content": _display_content(task, occ),
        "task_content": task["content"],
        "task_type": task["task_type"],
        "time_mode": task["time_mode"],
        "task_is_active": task["is_active"],
        "status": occ["status"],
        "est_start": occ.get("est_start"),
        "est_end": occ.get("est_end"),
        "nominal_start": occ.get("nominal_start"),
        "actual_start": occ.get("actual_start"),
        "actual_end": occ.get("actual_end"),
        "actual_minutes": occ.get("actual_minutes"),
        "estimated_minutes": task.get("estimated_minutes"),
        "partial_note": occ.get("partial_note"),
        "sort_order": occ.get("sort_order", 0),
        "is_fixed": occ.get("is_fixed", False),
        "is_limited": occ.get("is_limited", False),
        "is_hollow": task.get("is_hollow", False),
        "deadline_at": _deadline_for(task, occ),
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
        "interval_days": task.get("interval_days"),
        "weekdays": task.get("weekdays"),
        "month_days": task.get("month_days"),
        "target_date": task.get("target_date"),
        "time_mode": task["time_mode"],
        "estimated_minutes": task.get("estimated_minutes"),
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
        "cursor_date": task.get("cursor_date"),
        "next_due": next_due,
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


# ── 任务 CRUD ─────────────────────────────────────────────────────

def create_task(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    row = validate_task_payload(payload, partial=False)
    row["created_at"] = _iso(now)
    row["updated_at"] = _iso(now)
    row["is_fixed"] = bool(row.get("is_fixed"))
    if row["task_type"] == "interval":
        row["next_due"] = _iso(now)
    row["cursor_date"] = None
    client = _require_client()
    response = client.table("planning_task").insert(row).execute()
    created = (response.data or [{}])[0]
    # 即时生成：新建的待办（含 interval 立即到期）不等后台循环，立刻出现在列表。
    _generate_due_quietly(client, now)
    return serialize_task(created, now)


def _generate_due_quietly(client, now: datetime) -> None:
    """写操作后的同步补生成：幂等，失败只记日志，不吞掉已成功的写操作。

    当天有新生成实例时顺带重算一次，让用户立刻看到带起止时间的列表；
    重算以排列顺序与固定槽为准，不会动用户已固定的内容。
    """
    try:
        result = generate_due(now)
    except Exception as exc:
        log.warning(
            "planning 同步生成失败（等待后台循环重试）: error=%s", type(exc).__name__,
        )
        return
    if not result.get("created"):
        return
    try:
        recompute_today(now)
    except Exception as exc:
        log.warning("planning 同步重算失败: error=%s", type(exc).__name__)


SCHEDULE_FIELDS = {
    "task_type", "interval_days", "weekdays", "month_days", "target_date",
    "time_mode", "estimated_minutes", "est_start_tod", "est_end_tod",
    "hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes",
    "hollow_start_content", "hollow_end_content", "hollow_wait_note",
}


def update_task(task_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    client = _require_client()
    task = _fetch_task(client, task_id)
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)
    row = validate_task_payload(payload, partial=True)
    if not row:
        raise PlanningError("invalid_payload", "no writable fields supplied")

    # 废弃整个任务：终止后续刷新，并关闭所有仍开放的出现实例。
    reactivated = bool(row.get("is_active")) and not task.get("is_active")
    if row.get("is_active") is False and task.get("is_active"):
        open_rows = _rows(
            client, "planning_occurrence",
            lambda q: q.eq("task_id", task_id).in_("status", list(OPEN_STATUSES)),
        )
        for occ in open_rows:
            client.table("planning_occurrence").update({
                "status": "discarded",
                "closed_at": _iso(now),
                "updated_at": _iso(now),
            }).eq("id", occ["id"]).execute()

    if "target_date" in row and row.get("target_date") is None:
        raise PlanningError("invalid_payload", "target_date cannot be empty for once tasks")

    merged = {**task, **row}
    _ensure_type_requirements(merged)
    # 类型切换时清掉或初始化间歇游标，避免旧游标驱动新类型。
    if "task_type" in row and row["task_type"] != task.get("task_type"):
        if row["task_type"] == "interval":
            row["next_due"] = _iso(now)
        elif task.get("task_type") == "interval":
            row["next_due"] = None

    row["updated_at"] = _iso(now)
    schedule_changed = False
    if reactivated:
        # 重新启用不回填废弃期间漏掉的日期，从今天按规则继续。
        row["cursor_date"] = _cst_date(now).isoformat()
    elif set(row) & SCHEDULE_FIELDS and task.get("is_active"):
        # 排程规则被修改：删除今天及以后「未开始且未被手动固定」的排程
        # 实例，游标退回昨天，下个周期按新规则重建；已开始 / 已关闭 /
        # 手动固定 / 已延后的实例全部保留，昨日已有实例由唯一索引挡住
        # 不会重复生成。
        schedule_changed = True
        _delete_stale_pending_occurrences(client, task_id, _cst_date(now))
        row["cursor_date"] = (_cst_date(now) - timedelta(days=1)).isoformat()

    # 固定 / 限时冗余标记随任务定义同步到仍开放的实例。
    flag_patch: dict[str, Any] = {}
    if "is_fixed" in row:
        flag_patch["is_fixed"] = row["is_fixed"]
    if "deadline_tod" in row or "deadline_end_tod" in row:
        flag_patch["is_limited"] = merged.get("deadline_tod") is not None
    if flag_patch:
        for occ_row in _rows(
            client, "planning_occurrence",
            lambda q: q.eq("task_id", task_id).in_("status", list(OPEN_STATUSES)),
        ):
            client.table("planning_occurrence").update({
                **flag_patch, "updated_at": _iso(now),
            }).eq("id", occ_row["id"]).execute()

    response = client.table("planning_task").update(row).eq("id", task_id).execute()
    updated = (response.data or [{}])[0]
    if row.get("is_active") is False:
        request_recompute("task_discarded", now)
    elif schedule_changed:
        # 规则变更分支：pending 已删、游标已回退，立刻按新规则重建当日实例。
        _generate_due_quietly(client, now)
    return serialize_task(updated, now)


def _delete_stale_pending_occurrences(client, task_id: int, today: date) -> None:
    """删除今天及以后未开始、未被手动固定的排程实例（规则变更后的重建）。"""
    stale = _rows(
        client, "planning_occurrence",
        lambda q: (
            q.eq("task_id", task_id)
            .eq("status", "pending")
            .eq("is_fixed", False)
            .eq("source", "schedule")
            .gte("for_date", today.isoformat())
        ),
    )
    for occ_row in stale:
        if occ_row.get("actual_start"):
            continue
        client.table("planning_occurrence").delete().eq("id", occ_row["id"]).execute()


def _ensure_type_requirements(task: dict[str, Any]) -> None:
    """编辑合并后的完整任务定义必须仍满足其类型的必填字段。"""
    task_type = task.get("task_type")
    requirements = {
        "interval": ("interval_days",),
        "weekly": ("weekdays",),
        "monthly": ("month_days",),
        "once": ("target_date",),
    }
    for field in requirements.get(task_type, ()):
        value = task.get(field)
        if value is None or (isinstance(value, list) and not value):
            raise PlanningError("invalid_payload", f"{field} is required for {task_type} tasks")
    if task.get("time_mode") == "explicit" and not task.get("est_start_tod"):
        raise PlanningError("invalid_payload", "est_start_tod is required for explicit time mode")


def list_tasks(include_inactive: bool = True, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _now()
    client = _require_client()
    query_fn = None if include_inactive else lambda q: q.eq("is_active", True)
    rows = _rows(client, "planning_task", query_fn)
    rows.sort(key=lambda r: r["id"])
    return [serialize_task(row, now) for row in rows]


# ── 出现实例生成（只依据任务定义与规则游标） ──────────────────────

def _occurrence_est(task: dict[str, Any], for_date: date) -> tuple[datetime | None, datetime | None]:
    """单条目 / 中空开始阶段的预估起止。"""
    if task.get("time_mode") != "explicit" or not task.get("est_start_tod"):
        return None, None
    start = _combine(for_date, time.fromisoformat(task["est_start_tod"]))
    if task.get("est_end_tod"):
        end = _combine(for_date, time.fromisoformat(task["est_end_tod"]))
        if end <= start:
            end += timedelta(days=1)
        return start, end
    minutes = task.get("estimated_minutes")
    return start, (start + timedelta(minutes=minutes)) if minutes else None


def _occurrence_row(
    task: dict[str, Any], for_date: date, phase: str | None,
    est_start: datetime | None, est_end: datetime | None, now: datetime,
) -> dict[str, Any]:
    return {
        "task_id": task["id"],
        "for_date": for_date.isoformat(),
        "phase": phase,
        "est_start": _iso(est_start) if est_start else None,
        "est_end": _iso(est_end) if est_end else None,
        "nominal_start": _iso(est_start) if est_start else None,
        "status": "pending",
        "sort_order": task["id"] * 10 + (1 if phase == "end" else 0) + (
            100_000 if task["task_type"] == "idle" else 0
        ),
        "is_fixed": bool(task.get("is_fixed")) or task.get("time_mode") == "explicit",
        "is_limited": task.get("deadline_tod") is not None,
        "source": "schedule",
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }


def _create_occurrences(
    client, task: dict[str, Any], for_date: date, now: datetime,
) -> int:
    est_start, est_end = _occurrence_est(task, for_date)
    rows: list[dict[str, Any]] = []
    if task.get("is_hollow"):
        wait = timedelta(minutes=task["hollow_wait_minutes"])
        end_minutes = task["hollow_end_minutes"]
        end_start = (est_end + wait) if est_end else None
        end_end = (end_start + timedelta(minutes=end_minutes)) if end_start else None
        rows.append(_occurrence_row(task, for_date, "start", est_start, est_end, now))
        rows.append(_occurrence_row(task, for_date, "end", end_start, end_end, now))
    else:
        rows.append(_occurrence_row(task, for_date, None, est_start, est_end, now))
    inserted = 0
    for row in rows:
        try:
            client.table("planning_occurrence").insert(row).execute()
            inserted += 1
        except Exception as exc:
            # 唯一索引（task_id, for_date, phase, source='schedule'）命中
            # 说明该槽位已生成——静默跳过；其余错误抛回，游标不推进，
            # 下个周期自动补生成。
            if "planning_occurrence_schedule_slot_uq" in str(exc) or "duplicate key" in str(exc).lower():
                log.info("planning 生成跳过已存在槽位: task=%s for_date=%s phase=%s", task["id"], for_date, row.get("phase"))
            else:
                raise
    return inserted


def _should_occur(task: dict[str, Any], day: date) -> bool:
    task_type = task["task_type"]
    if task_type in ("daily", "idle"):
        return True
    if task_type == "once":
        return task.get("target_date") == day.isoformat()
    if task_type == "weekly":
        weekdays = task.get("weekdays") or []
        return day.weekday() in weekdays
    if task_type == "monthly":
        month_days = task.get("month_days") or []
        return day.day in month_days
    return False


def generate_due(now: datetime | None = None) -> dict[str, Any]:
    """按规则游标补齐所有到期任务的出现实例（幂等，可重启补生成）。"""
    now = now or _now()
    today = _cst_date(now)
    client = _require_client()
    tasks = _rows(client, "planning_task", lambda q: q.eq("is_active", True))
    created = 0
    for task in tasks:
        if task["task_type"] == "interval":
            next_due = task.get("next_due")
            if not next_due:
                continue
            due = _parse_dt(next_due, "next_due")
            if due <= now:
                created += _create_occurrences(client, task, _cst_date(due), now)
                client.table("planning_task").update({
                    "next_due": None, "updated_at": _iso(now),
                }).eq("id", task["id"]).execute()
            continue
        cursor = task.get("cursor_date")
        if cursor:
            start = date.fromisoformat(cursor) + timedelta(days=1)
        else:
            created_at = task.get("created_at")
            start = _cst_date(_parse_dt(created_at, "created_at")) if created_at else today
        if task["task_type"] == "once" and task.get("target_date"):
            # 目标日期早于创建日期的单次待办同样视为到期。
            start = min(start, date.fromisoformat(task["target_date"]))
        if start > today:
            continue
        day = start
        while day <= today:
            if _should_occur(task, day):
                created += _create_occurrences(client, task, day, now)
            day += timedelta(days=1)
        client.table("planning_task").update({
            "cursor_date": today.isoformat(), "updated_at": _iso(now),
        }).eq("id", task["id"]).execute()
    if created:
        log.info("planning 生成出现实例: count=%s date=%s", created, today.isoformat())
    return {"created": created, "date": today.isoformat()}


# ── 限时超时判定 ──────────────────────────────────────────────────

def sweep_timeouts(now: datetime | None = None) -> dict[str, int]:
    """限时待办过截止未完成自动标记「已超时」。"""
    now = now or _now()
    client = _require_client()
    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("is_limited", True).in_("status", list(OPEN_STATUSES)),
    )
    if not open_rows:
        return {"timed_out": 0}
    tasks = _task_map(client, {row["task_id"] for row in open_rows})
    timed_out = 0
    for occ in open_rows:
        task = tasks.get(occ["task_id"])
        if not task:
            continue
        end_tod = task.get("deadline_end_tod") or task.get("deadline_tod")
        if not end_tod:
            continue
        deadline = _combine(date.fromisoformat(occ["for_date"]), time.fromisoformat(end_tod))
        if deadline < now:
            client.table("planning_occurrence").update({
                "status": "timeout", "updated_at": _iso(now),
            }).eq("id", occ["id"]).execute()
            timed_out += 1
    if timed_out:
        log.info("planning 超时打标: count=%s", timed_out)
    return {"timed_out": timed_out}


# ── 时间重算 ──────────────────────────────────────────────────────

def _freely_schedulable(occ: dict[str, Any], task: dict[str, Any]) -> bool:
    """仅填耗时（duration）且未固定 / 未开始的 pending 实例可自动排程。"""
    return (
        occ["status"] == "pending"
        and not occ.get("is_fixed")
        and task.get("time_mode") == "duration"
    )


def _slot_range(occ: dict[str, Any]) -> tuple[datetime, datetime] | None:
    est_start = occ.get("est_start")
    est_end = occ.get("est_end")
    if not est_start or not est_end:
        return None
    start = _parse_dt(est_start, "est_start")
    end = _parse_dt(est_end, "est_end")
    if end <= start:
        return None
    return start, end


def compute_schedule(
    open_rows: list[dict[str, Any]], tasks: dict[int, dict[str, Any]], now: datetime,
) -> dict[int, tuple[datetime, datetime]]:
    """按「当前时间 → 排列顺序 → 预估耗时」向后排程。

    * 可自动排程实例（仅填耗时、未固定、未开始）按列表顺序依次装入，
      允许前移；固定时间位（显式起止 / 固定标记 / 进行中 / 已延后）保留
      原位，轮到它们时游标越过其时间槽。
    * 任何可排程实例都不与固定槽重叠：排不下的顺延到槽结束之后。
    * 中空待办结束阶段最早开始 = 开始阶段预计结束 + 中间时长，中间的
      空闲时间允许其他待办按列表顺序排入。
    """
    ordered = sorted(open_rows, key=lambda r: (r.get("sort_order", 0), r["id"]))
    slots: list[tuple[datetime, datetime]] = []
    for occ in ordered:
        if _freely_schedulable(occ, tasks.get(occ["task_id"], {})):
            continue
        slot = _slot_range(occ)
        if slot:
            slots.append(slot)
    slots.sort()

    placed: dict[int, tuple[datetime, datetime]] = {}
    cursor = now
    for occ in ordered:
        task = tasks.get(occ["task_id"], {})
        if not _freely_schedulable(occ, task):
            slot = _slot_range(occ)
            if slot and slot[1] > cursor:
                cursor = slot[1]
            continue
        minutes = task.get("estimated_minutes")
        if occ.get("phase") == "start" and task.get("hollow_start_minutes"):
            minutes = task["hollow_start_minutes"]
        if occ.get("phase") == "end" and task.get("hollow_end_minutes"):
            minutes = task["hollow_end_minutes"]
        if not minutes:
            continue
        duration = timedelta(minutes=minutes)
        start = cursor
        if occ.get("phase") == "end":
            wait = timedelta(minutes=task.get("hollow_wait_minutes") or 0)
            start_occ = next(
                (o for o in ordered if o["task_id"] == occ["task_id"] and o.get("phase") == "start"),
                None,
            )
            if start_occ:
                anchor = placed.get(start_occ["id"]) or _slot_range(start_occ)
                if anchor:
                    start = max(start, anchor[1] + wait)
        moved = True
        while moved:
            moved = False
            for slot_start, slot_end in slots:
                if slot_end <= start:
                    continue
                if slot_start >= start + duration:
                    break
                start = slot_end
                moved = True
                break
        end = start + duration
        placed[occ["id"]] = (start, end)
        cursor = end
    return placed


def recompute_today(now: datetime | None = None) -> dict[str, Any]:
    """手动 / 自动重算：只更新当天可自动排程实例的预估起止。"""
    now = now or _now()
    today = _cst_date(now)
    client = _require_client()
    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("for_date", today.isoformat()).in_("status", list(OPEN_STATUSES)),
    )
    if not open_rows:
        return {"updated": 0, "at": _iso(now)}
    tasks = _task_map(client, {row["task_id"] for row in open_rows})
    placed = compute_schedule(open_rows, tasks, now)
    updated = 0
    for occ_id, (start, end) in placed.items():
        occ = next(row for row in open_rows if row["id"] == occ_id)
        old_start = _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
        old_end = _parse_dt(occ["est_end"], "est_end") if occ.get("est_end") else None
        if old_start == start and old_end == end:
            continue
        patch: dict[str, Any] = {
            "est_start": _iso(start),
            "est_end": _iso(end),
            "updated_at": _iso(now),
        }
        if not occ.get("nominal_start"):
            patch["nominal_start"] = _iso(start)
        client.table("planning_occurrence").update(patch).eq("id", occ_id).execute()
        updated += 1
    log.info("planning 重算完成: updated=%s date=%s", updated, today.isoformat())
    return {"updated": updated, "at": _iso(now)}


# ── 重算等待标记 ──────────────────────────────────────────────────

def request_recompute(reason: str, now: datetime | None = None) -> None:
    now = now or _now()
    client = _require_client()
    client.table("planning_recompute_state").upsert(
        {"id": 1, "requested_at": _iso(now), "reason": reason[:100], "updated_at": _iso(now)},
        ignore_duplicates=False,
    ).execute()


def clear_recompute_mark(now: datetime | None = None) -> None:
    now = now or _now()
    client = _require_client()
    client.table("planning_recompute_state").update({
        "requested_at": None, "reason": None, "updated_at": _iso(now),
    }).eq("id", 1).execute()


def get_recompute_state(now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    client = _require_client()
    rows = _rows(client, "planning_recompute_state", lambda q: q.eq("id", 1).limit(1))
    requested_at = rows[0].get("requested_at") if rows else None
    pending = bool(requested_at)
    wait_minutes = None
    if pending:
        requested = _parse_dt(requested_at, "requested_at")
        wait_minutes = max(0, round((RECOMPUTE_WAIT - (now - requested)).total_seconds() / 60))
    return {
        "pending": pending,
        "requested_at": requested_at,
        "reason": rows[0].get("reason") if rows else None,
        "wait_minutes": wait_minutes,
    }


def trigger_recompute(now: datetime | None = None) -> dict[str, Any]:
    """手动重算：立即执行并清空等待标记。"""
    now = now or _now()
    result = recompute_today(now)
    clear_recompute_mark(now)
    return result


# ── 排列保存 ──────────────────────────────────────────────────────

def save_order(ordered_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    today = _cst_date(now)
    if not isinstance(ordered_ids, list) or not all(isinstance(v, int) for v in ordered_ids):
        raise PlanningError("invalid_payload", "order must be an array of occurrence ids")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise PlanningError("invalid_payload", "order contains duplicate ids")
    client = _require_client()
    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("for_date", today.isoformat()).in_("status", list(OPEN_STATUSES)),
    )
    by_id = {row["id"]: row for row in open_rows}
    unknown = [occ_id for occ_id in ordered_ids if occ_id not in by_id]
    if unknown:
        raise PlanningError("invalid_payload", "order includes occurrences outside today's open list")
    missing = [occ_id for occ_id in by_id if occ_id not in set(ordered_ids)]
    if missing:
        raise PlanningError("invalid_payload", "order must include every open occurrence of today")

    # 中空待办的结束阶段必须排在其开始阶段之后。
    position = {occ_id: index for index, occ_id in enumerate(ordered_ids)}
    for occ_id, occ in by_id.items():
        if occ.get("phase") == "end":
            start_occ = next(
                (row for row in open_rows
                 if row["task_id"] == occ["task_id"] and row.get("phase") == "start"),
                None,
            )
            if start_occ and position[occ_id] < position[start_occ["id"]]:
                raise PlanningError("invalid_payload", "hollow end phase must stay after its start phase")

    for occ_id, index in position.items():
        client.table("planning_occurrence").update({
            "sort_order": index, "updated_at": _iso(now),
        }).eq("id", occ_id).execute()
    request_recompute("reorder", now)
    return {"saved": len(ordered_ids)}


def save_order_from_payload(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or "order" not in payload:
        raise PlanningError("invalid_payload", "order is required")
    return save_order(payload["order"], now)


# ── 出现实例：状态流转 / 打点 / 补填 / 拆分 ───────────────────────

def _duration_of(task: dict[str, Any], phase: str | None = None) -> timedelta:
    """实例执行时长：中空待办按阶段取专用耗时，其余用预估耗时。"""
    if phase == "start" and task.get("hollow_start_minutes"):
        return timedelta(minutes=task["hollow_start_minutes"])
    if phase == "end" and task.get("hollow_end_minutes"):
        return timedelta(minutes=task["hollow_end_minutes"])
    minutes = task.get("estimated_minutes")
    if minutes:
        return timedelta(minutes=minutes)
    if task.get("est_start_tod") and task.get("est_end_tod"):
        start = _combine(date(2000, 1, 1), time.fromisoformat(task["est_start_tod"]))
        end = _combine(date(2000, 1, 1), time.fromisoformat(task["est_end_tod"]))
        if end <= start:
            end += timedelta(days=1)
        return end - start
    return timedelta(minutes=30)


def _compute_actual_minutes(occ: dict[str, Any]) -> int | None:
    actual_start = occ.get("actual_start")
    actual_end = occ.get("actual_end")
    if not actual_start or not actual_end:
        return None
    start = _parse_dt(actual_start, "actual_start")
    end = _parse_dt(actual_end, "actual_end")
    if end < start:
        raise PlanningError("invalid_payload", "actual_end must not precede actual_start")
    return _minutes_between(start, end)


def _reschedule_occurrence(
    occ: dict[str, Any], task: dict[str, Any], new_start: datetime, now: datetime,
) -> dict[str, Any]:
    """延后 / 重新安排：落到新时间并顺延 for_date（实例随日期移动）。"""
    end = new_start + _duration_of(task, occ.get("phase"))
    return {
        "est_start": _iso(new_start),
        "est_end": _iso(end),
        "nominal_start": _iso(new_start),
        "for_date": _cst_date(new_start).isoformat(),
        "updated_at": _iso(now),
    }


def _shift_sibling_phase(
    client, occ: dict[str, Any], task: dict[str, Any], old_start: datetime | None,
    new_start: datetime, now: datetime,
) -> None:
    """中空待办单阶段被延后 / 手动改时间时，另一阶段按相同时间差平移，
    避免两阶段日期倒挂；结束阶段的精确锚定随后由重算完成。"""
    if not task.get("is_hollow") or not occ.get("phase"):
        return
    if old_start:
        delta = new_start - old_start
    else:
        # 原本无预估时间（尚未重算的仅耗时实例）：按日期差平移，
        # 保留新时刻的时、分，保证两阶段落在同一天。
        anchor = _combine(date.fromisoformat(occ["for_date"]), new_start.time())
        delta = new_start - anchor
    if delta == timedelta(0):
        return
    sibling_phase = "end" if occ["phase"] == "start" else "start"
    for_date = occ.get("for_date")
    sibling = next(
        (
            row for row in _rows(
                client, "planning_occurrence",
                lambda q: q.eq("task_id", task["id"]).eq("phase", sibling_phase).eq("for_date", for_date),
            )
            if row["id"] != occ["id"]
        ),
        None,
    )
    if not sibling:
        return
    patch: dict[str, Any] = {"updated_at": _iso(now)}
    for field in ("est_start", "est_end", "nominal_start"):
        if sibling.get(field):
            patch[field] = _iso(_parse_dt(sibling[field], field) + delta)
    patch["for_date"] = (_cst_date(_parse_dt(sibling["for_date"], "for_date")) + delta).isoformat()
    client.table("planning_occurrence").update(patch).eq("id", sibling["id"]).execute()


def set_occurrence_status(occurrence_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")
    target = str(payload.get("status") or "").strip().casefold()
    if target not in OCCURRENCE_STATUSES:
        raise PlanningError("invalid_payload", f"status must be one of {', '.join(OCCURRENCE_STATUSES)}")
    if target == "timeout":
        raise PlanningError("invalid_payload", "timeout is assigned by the system only")

    client = _require_client()
    occ = _fetch_occurrence(client, occurrence_id)
    if not occ:
        raise PlanningError("not_found", "planning occurrence not found", 404)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)
    current = occ["status"]

    new_start_raw = payload.get("est_start")
    new_start = _parse_dt(new_start_raw, "est_start") if new_start_raw else None

    # 状态迁移规则：
    # * 超时后不能再标记完成 / 部分完成（决策 10），只能重新安排（新时间 +
    #   pending）或废弃（整个废弃 / 此次废弃）。
    # * 「此次废弃」仅重复型任务提供。
    # * 关闭态记录可改回 pending / in_progress / deferred（回到进度中）。
    if target in ("completed", "partial") and current == "timeout":
        raise PlanningError(
            "invalid_transition", "timed-out occurrences can only be rescheduled or discarded", 422,
        )
    if target == "discarded_this":
        if task["task_type"] not in REPEATING_TASK_TYPES:
            raise PlanningError(
                "invalid_transition", "discarded_this is only available for repeating tasks", 422,
            )
        if current == "timeout":
            pass  # 超时后此次废弃视为一种废弃处理路径
        elif current not in OPEN_STATUSES and current not in CLOSED_STATUSES:
            raise PlanningError("invalid_transition", f"cannot discard_this from {current}", 422)
    if target == "deferred":
        if current not in ("pending", "in_progress"):
            raise PlanningError("invalid_transition", f"cannot defer from {current}", 422)
        if not new_start:
            raise PlanningError("invalid_payload", "deferring requires est_start", 422)
    if target == "in_progress" and current not in ("pending", "deferred"):
        raise PlanningError("invalid_transition", f"cannot start from {current}", 422)
    if target == "pending" and current == "timeout" and not new_start:
        raise PlanningError(
            "invalid_payload", "rescheduling a timed-out occurrence requires est_start", 422,
        )
    if target in ("completed", "partial") and current not in (
        "pending", "in_progress", "partial", "completed", "deferred",
    ):
        raise PlanningError("invalid_transition", f"cannot complete from {current}", 422)

    row: dict[str, Any] = {"status": target, "updated_at": _iso(now)}

    if target == "partial":
        note = _clean_text(payload.get("partial_note"), "partial_note", required=True, maximum=MAX_NOTE_LENGTH)
        row["partial_note"] = note
    elif target in ("pending", "in_progress", "deferred"):
        if current in CLOSED_STATUSES or current == "timeout":
            row["partial_note"] = None

    if new_start:
        row.update(_reschedule_occurrence(occ, task, new_start, now))
        _shift_sibling_phase(
            client, occ, task,
            _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
            new_start, now,
        )

    if target == "in_progress" and not occ.get("actual_start"):
        row["actual_start"] = _iso(now)

    closing = target in CLOSED_STATUSES
    if closing:
        row["closed_at"] = _iso(now)
        if payload.get("actual_end"):
            row["actual_end"] = _iso(_parse_dt(payload["actual_end"], "actual_end"))
        elif not occ.get("actual_end"):
            row["actual_end"] = _iso(now)
        if payload.get("actual_start"):
            row["actual_start"] = _iso(_parse_dt(payload["actual_start"], "actual_start"))
        merged = {**occ, **row}
        row["actual_minutes"] = _compute_actual_minutes(merged)
    else:
        row["closed_at"] = None
        if payload.get("actual_start"):
            row["actual_start"] = _iso(_parse_dt(payload["actual_start"], "actual_start"))
        if payload.get("actual_end"):
            row["actual_end"] = _iso(_parse_dt(payload["actual_end"], "actual_end"))
        if "actual_start" in row or "actual_end" in row:
            merged = {**occ, **row}
            row["actual_minutes"] = _compute_actual_minutes(merged)

    client.table("planning_occurrence").update(row).eq("id", occurrence_id).execute()

    # 重复型任务的「废弃」= 整个待办不再执行（需求 4d）：停用任务并关闭
    # 其余开放实例，与 update_task(is_active=False) 同效果；单次待办只关
    # 当前实例。前端确认文案「后续不再自动出现」据此成立。
    discarding_whole_task = (
        target == "discarded"
        and task["task_type"] in REPEATING_TASK_TYPES
        and task.get("is_active")
    )
    if discarding_whole_task:
        client.table("planning_task").update({
            "is_active": False, "updated_at": _iso(now),
        }).eq("id", task["id"]).execute()
        for other in _rows(
            client, "planning_occurrence",
            lambda q: q.eq("task_id", task["id"]).in_("status", list(OPEN_STATUSES)),
        ):
            client.table("planning_occurrence").update({
                "status": "discarded", "closed_at": _iso(now), "updated_at": _iso(now),
            }).eq("id", other["id"]).execute()
        task = {**task, "is_active": False}

    # 间歇待办：本轮实例关闭（完成 / 部分完成 / 此次废弃）后，
    # 以关闭时刻 + 间隔重算下一次到期；提前完成同样重置。
    # 整个废弃已终止刷新，无需再排下一次。
    if closing and task["task_type"] == "interval" and task.get("is_active") and not discarding_whole_task:
        interval = task.get("interval_days")
        if interval:
            next_due = now + timedelta(days=interval)
            client.table("planning_task").update({
                "next_due": _iso(next_due), "updated_at": _iso(now),
            }).eq("id", task["id"]).execute()

    # 重算等待标记按约定只在「列表顺序变化 / 有待办完成」时触发；
    # 这里对应关闭态流转（含重新安排与废弃）。
    if closing:
        request_recompute("status_change", now)
    refreshed = _fetch_occurrence(client, occurrence_id) or {**occ, **row}
    return serialize_occurrence(refreshed, task, now)


def start_occurrence(occurrence_id: int, now: datetime | None = None) -> dict[str, Any]:
    return set_occurrence_status(occurrence_id, {"status": "in_progress"}, now)


def finish_occurrence(occurrence_id: int, now: datetime | None = None) -> dict[str, Any]:
    return set_occurrence_status(occurrence_id, {"status": "completed"}, now)


def patch_occurrence(occurrence_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """手动编辑 / 兜底：手动改预估起止、补填或修改实际起止、部分完成说明。"""
    now = now or _now()
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")
    allowed = {"est_start", "est_end", "actual_start", "actual_end", "partial_note", "is_fixed"}
    unknown = set(payload) - allowed
    if unknown:
        raise PlanningError("invalid_payload", f"unsupported fields: {', '.join(sorted(unknown))}")
    if not payload:
        raise PlanningError("invalid_payload", "no writable fields supplied")

    client = _require_client()
    occ = _fetch_occurrence(client, occurrence_id)
    if not occ:
        raise PlanningError("not_found", "planning occurrence not found", 404)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)

    row: dict[str, Any] = {"updated_at": _iso(now)}
    if "est_start" in payload:
        row["est_start"] = _iso(_parse_dt(payload["est_start"], "est_start")) if payload["est_start"] else None
    if "est_end" in payload:
        row["est_end"] = _iso(_parse_dt(payload["est_end"], "est_end")) if payload["est_end"] else None
    if "est_start" in row and row["est_start"]:
        # 手动编辑时间 = 该条目固定此时间位，不再被自动重算移动。
        row.setdefault("is_fixed", True)
        row["nominal_start"] = row["est_start"]
        row["for_date"] = _cst_date(_parse_dt(row["est_start"], "est_start")).isoformat()
        if "est_end" not in row or not row["est_end"]:
            row["est_end"] = _iso(
                _parse_dt(row["est_start"], "est_start") + _duration_of(task, occ.get("phase"))
            )
        _shift_sibling_phase(
            client, occ, task,
            _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
            _parse_dt(row["est_start"], "est_start"), now,
        )
    if "is_fixed" in payload:
        row["is_fixed"] = _clean_bool(payload.get("is_fixed"), "is_fixed")
    if "partial_note" in payload:
        row["partial_note"] = _clean_text(
            payload.get("partial_note"), "partial_note", required=False, maximum=MAX_NOTE_LENGTH,
        )
    if "actual_start" in payload:
        row["actual_start"] = _iso(_parse_dt(payload["actual_start"], "actual_start")) if payload["actual_start"] else None
    if "actual_end" in payload:
        row["actual_end"] = _iso(_parse_dt(payload["actual_end"], "actual_end")) if payload["actual_end"] else None
    if "actual_start" in row or "actual_end" in row:
        row["actual_minutes"] = _compute_actual_minutes({**occ, **row})

    client.table("planning_occurrence").update(row).eq("id", occurrence_id).execute()
    refreshed = _fetch_occurrence(client, occurrence_id) or {**occ, **row}
    return serialize_occurrence(refreshed, task, now)


def split_occurrence(occurrence_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """把一个待办拆分为多个待办：新建 N 个当日单次待办，原实例废弃留痕。"""
    now = now or _now()
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")
    parts = payload.get("parts")
    if not isinstance(parts, list) or not 2 <= len(parts) <= 10:
        raise PlanningError("invalid_payload", "parts must be an array of 2-10 items")
    normalized = []
    for part in parts:
        if not isinstance(part, dict):
            raise PlanningError("invalid_payload", "parts items must be objects")
        content = _clean_text(part.get("content"), "parts.content", required=True, maximum=MAX_CONTENT_LENGTH)
        minutes = parse_duration_shorthand(part.get("estimated_minutes", 30), "parts.estimated_minutes")
        normalized.append({"content": content, "estimated_minutes": minutes})

    client = _require_client()
    occ = _fetch_occurrence(client, occurrence_id)
    if not occ:
        raise PlanningError("not_found", "planning occurrence not found", 404)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)

    created_ids = []
    for part in normalized:
        response = client.table("planning_task").insert({
            "content": part["content"],
            "task_type": "once",
            "time_mode": "duration",
            "estimated_minutes": part["estimated_minutes"],
            "target_date": occ["for_date"],
            "is_active": True,
            "cursor_date": None,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }).execute()
        created = (response.data or [{}])[0]
        created_ids.append(created.get("id"))
    client.table("planning_occurrence").update({
        "status": "discarded",
        "closed_at": _iso(now),
        "partial_note": f"已拆分为 {len(normalized)} 个待办",
        "updated_at": _iso(now),
    }).eq("id", occurrence_id).execute()
    request_recompute("split", now)
    # 即时生成：拆分出的当日单次待办立刻出现在列表里。
    _generate_due_quietly(client, now)
    return {"created_task_ids": created_ids, "split_from": occurrence_id}


def complete_task_early(task_id: int, now: datetime | None = None) -> dict[str, Any]:
    """间歇待办提前完成：以此次完成时间重新计算下一次出现时间。"""
    now = now or _now()
    client = _require_client()
    task = _fetch_task(client, task_id)
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)
    if task["task_type"] != "interval":
        raise PlanningError("invalid_transition", "only interval tasks support early completion", 422)
    if not task.get("is_active"):
        raise PlanningError("invalid_transition", "task is discarded", 422)
    interval = task.get("interval_days")
    if not interval:
        raise PlanningError("invalid_payload", "interval_days is missing", 422)

    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("task_id", task_id).in_("status", list(OPEN_STATUSES)),
    )
    if open_rows:
        occ = sorted(open_rows, key=lambda r: r["id"])[0]
        result = set_occurrence_status(
            occ["id"], {"status": "completed", "actual_end": _iso(now)}, now,
        )
    else:
        today = _cst_date(now)
        response = client.table("planning_occurrence").insert({
            "task_id": task_id,
            "for_date": today.isoformat(),
            "phase": None,
            "actual_start": _iso(now),
            "actual_end": _iso(now),
            "actual_minutes": 0,
            "status": "completed",
            "sort_order": task["id"] * 10,
            "is_fixed": bool(task.get("is_fixed")),
            "is_limited": task.get("deadline_tod") is not None,
            "closed_at": _iso(now),
            "source": "early",
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }).execute()
        created = (response.data or [{}])[0]
        result = serialize_occurrence(created, task, now)
    next_due = now + timedelta(days=interval)
    client.table("planning_task").update({
        "next_due": _iso(next_due), "updated_at": _iso(now),
    }).eq("id", task_id).execute()
    return result


# ── 当天 / 全部列表 ───────────────────────────────────────────────

def today_board(now: datetime | None = None) -> dict[str, Any]:
    """当前待办三分区（进度中 / 待处理 / 已完成）+ 重算等待状态。"""
    now = now or _now()
    today = _cst_date(now)
    client = _require_client()
    today_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("for_date", today.isoformat()),
    )
    timeout_rows = _rows(client, "planning_occurrence", lambda q: q.eq("status", "timeout"))
    merged: dict[int, dict[str, Any]] = {row["id"]: row for row in today_rows + timeout_rows}
    tasks = _task_map(client, {row["task_id"] for row in merged.values()})

    progress, done = [], []
    for row in today_rows:
        task = tasks.get(row["task_id"])
        if not task:
            continue
        item = serialize_occurrence(row, task, now)
        if row["status"] in OPEN_STATUSES:
            progress.append(item)
        elif row["status"] in CLOSED_STATUSES:
            done.append(item)
    progress.sort(key=lambda item: (item["sort_order"], item["id"]))
    done.sort(key=lambda item: (item.get("closed_at") or "", item["id"]), reverse=True)

    attention = []
    for row in timeout_rows:
        task = tasks.get(row["task_id"])
        if not task:
            continue
        attention.append(serialize_occurrence(row, task, now))
    attention.sort(key=lambda item: (item["for_date"], item["id"]))

    return {
        "date": today.isoformat(),
        "now": _iso(now),
        "recompute": get_recompute_state(now),
        "progress": progress,
        "attention": attention,
        "done": done,
    }


def list_occurrences(
    *,
    task_type: str | None = None,
    status: str | None = None,
    for_date: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = DEFAULT_LIST_ROWS,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    now = now or _now()
    if status is not None and status not in OCCURRENCE_STATUSES:
        raise PlanningError("invalid_payload", f"unknown status: {status}")
    if task_type is not None and task_type not in TASK_TYPES:
        raise PlanningError("invalid_payload", f"unknown task_type: {task_type}")
    try:
        limit = max(1, min(MAX_LIST_ROWS, int(limit or DEFAULT_LIST_ROWS)))
    except (TypeError, ValueError) as exc:
        raise PlanningError("invalid_payload", "limit must be an integer") from exc

    client = _require_client()

    # 类型筛选下推到 SQL：先取该类型的 task_id 集合，避免「先 limit 后
    # 内存过滤」把更早的命中记录静默挤掉。
    type_task_ids: list[int] | None = None
    if task_type:
        type_task_ids = [
            row["id"] for row in _rows(client, "planning_task", lambda q: q.eq("task_type", task_type))
        ]
        if not type_task_ids:
            return []

    def query(q):
        if type_task_ids is not None:
            q = q.in_("task_id", type_task_ids)
        if for_date:
            q = q.eq("for_date", for_date)
        if date_from:
            q = q.gte("for_date", _parse_date(date_from, "date_from").isoformat())
        if date_to:
            q = q.lte("for_date", _parse_date(date_to, "date_to").isoformat())
        if status:
            q = q.eq("status", status)
        return q.order("for_date", desc=True).order("id", desc=True).limit(limit)

    rows = _rows(client, "planning_occurrence", query)
    tasks = _task_map(client, {row["task_id"] for row in rows})
    result = []
    for row in rows:
        task = tasks.get(row["task_id"])
        if not task:
            continue
        result.append(serialize_occurrence(row, task, now))
    return result


# ── 72 小时清理 ───────────────────────────────────────────────────

def cleanup_discarded(now: datetime | None = None) -> dict[str, int]:
    """废弃 / 此次废弃的出现记录满 72 小时删除。

    安全前提：生成只依据任务定义与规则游标（cursor_date / next_due），
    删除这些记录不会造成重新生成或漏生成；已完成 / 部分完成 / 延后的
    记录永久保留。
    """
    now = now or _now()
    client = _require_client()
    threshold = _iso(now - DISCARD_RETENTION)
    rows = _rows(
        client, "planning_occurrence",
        lambda q: q.in_("status", ["discarded", "discarded_this"]).lt("closed_at", threshold),
    )
    for row in rows:
        client.table("planning_occurrence").delete().eq("id", row["id"]).execute()
    return {"deleted": len(rows)}


# ── 后台维护循环（约 1 分钟粒度） ─────────────────────────────────

def run_maintenance(now: datetime | None = None) -> dict[str, Any]:
    """0 点补生成 / 间歇到期 / 超时打标 / 15 分钟等待重算 / 72 小时清理。

    单实例锁防止并发重入；单步失败只记日志，不影响其余步骤。
    """
    if not _maintenance_lock.acquire(blocking=False):
        return {"status": "skipped_busy"}
    try:
        now = now or _now()
        results: dict[str, Any] = {"status": "ok", "at": _iso(now)}
        try:
            results["generation"] = generate_due(now)
            # 0 点 / 到期新生成的当天实例需要立刻拿到预估起止；
            # 重算以列表顺序与固定槽为准，不会动用户已固定的内容。
            if isinstance(results["generation"], dict) and results["generation"].get("created"):
                results["generation_recompute"] = recompute_today(now)
        except Exception as exc:
            log.exception("planning 生成失败: %s", type(exc).__name__)
            results["generation"] = {"error": type(exc).__name__}
        try:
            results["timeouts"] = sweep_timeouts(now)
        except Exception as exc:
            log.exception("planning 超时打标失败: %s", type(exc).__name__)
            results["timeouts"] = {"error": type(exc).__name__}
        try:
            state = get_recompute_state(now)
            requested_at = state.get("requested_at")
            if requested_at and (now - _parse_dt(requested_at, "requested_at")) >= RECOMPUTE_WAIT:
                results["auto_recompute"] = recompute_today(now)
                clear_recompute_mark(now)
            else:
                results["auto_recompute"] = {"skipped": True}
        except Exception as exc:
            log.exception("planning 自动重算失败: %s", type(exc).__name__)
            results["auto_recompute"] = {"error": type(exc).__name__}
        try:
            results["cleanup"] = cleanup_discarded(now)
        except Exception as exc:
            log.exception("planning 72 小时清理失败: %s", type(exc).__name__)
            results["cleanup"] = {"error": type(exc).__name__}
        return results
    finally:
        _maintenance_lock.release()
