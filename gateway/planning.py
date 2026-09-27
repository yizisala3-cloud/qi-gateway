"""规划管理运行逻辑；Phase 1A 身份与 Phase 1B 刷新模型（Phase 1R 新口径）。

领域原则：过去不重写，已生成实例不追溯，新的事实影响未来。
- 任务规则修改只影响尚未生成的未来实例；已生成实例的身份与实例级数据冻结。
- 刷新边界修改从下一规划周期生效，当前周期按原边界走完，永不重新解释。
- 固定间隔轮次身份由到期事件派生，与边界无关。
新版实例用 round_key、schedule_date 和 display_cycle_date 表达身份；
旧实例不能从 for_date 推断业务轮次或时间所有权。
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from . import db
from .db import get_client
from .planning_domain import (
    BoundaryTransition,
    CALENDAR_FIXED_MODES,
    DEFAULT_REFRESH_BOUNDARY,
    EARLY_CAPABLE_MODES,
    EstimatedTimeOwnership,
    OccurrenceIdentity,
    PlanningCycle,
    calendar_round_key,
    cycle_start_boundary,
    fixed_round_key,
    parse_refresh_boundary,
    planning_cycle_at,
    round_phase_group,
    timed_round_key,
    validate_task_refresh_mode,
)
from .planning_window import (
    ResolvedWindow,
    WindowTemplate,
    hollow_envelope_duration,
    hollow_envelope_minutes,
    resolve_window,
    resolve_window_on_date,
    validate_template_window,
    window_feasible,
)

log = logging.getLogger("gateway.planning")

_CST = timezone(timedelta(hours=8))

TASK_TYPES = ("daily", "interval", "weekly", "monthly", "once", "idle")
# 重复型任务（「此次不执行」仅对这些类型开放；单次待办没有）
REPEATING_TASK_TYPES = ("daily", "interval", "weekly", "monthly", "idle")
TIME_MODES = ("duration", "explicit")
OCCURRENCE_STATUSES = (
    "pending", "in_progress", "deferred", "partial",
    "completed", "discarded_this", "discarded", "timeout",
)
# 部分完成属于开放生命周期：实例仍可继续处理，直到「已全部完成」才真正关闭。
OPEN_STATUSES = ("pending", "in_progress", "deferred", "partial")
CLOSED_STATUSES = ("completed", "discarded_this", "discarded")

RECOMPUTE_WAIT = timedelta(minutes=30)
DISCARD_RETENTION = timedelta(hours=72)
# BF3（第七轮）：after_completion 提前完成的防重复窗口——最近一次已经
# 成功成立的完成事实（服务端持久化处理时间）之后 30 分钟内的再次提前完成，
# 统一视为前一次操作的重复请求；窗口外为新的真实操作。
EARLY_DEDUPE_WINDOW = timedelta(minutes=30)

MAX_CONTENT_LENGTH = 500
MAX_NOTE_LENGTH = 1000
MAX_LIST_ROWS = 500
DEFAULT_LIST_ROWS = 200

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_SHORTHAND_RE = re.compile(r"^(?:(\d+)\s*h)?(?:(\d+)\s*m)?(?:(\d+)\s*s)?$")

_maintenance_lock = threading.Lock()
PLANNING_BOUNDARY_STATE_KEY = "planning.refresh_boundary_state"
PLANNING_DAILY_REFRESH_KEY = "planning.daily_refresh_enabled"
PLANNING_AUTO_RECOMPUTE_ENABLED_KEY = "planning.auto_recompute_enabled"
PLANNING_AUTO_RECOMPUTE_WAIT_KEY = "planning.auto_recompute_wait_minutes"


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


def _default_boundary_state() -> dict[str, Any]:
    return {"boundary": DEFAULT_REFRESH_BOUNDARY.strftime("%H:%M"), "transition": None, "absorbed": []}


def _load_boundary_state(now: datetime) -> tuple[time, BoundaryTransition | None, frozenset[date]]:
    """Load the atomic boundary state: configured boundary, pending transition
    and the registry of transition-absorbed cycle keys.

    The whole state lives in ONE app_settings row so a boundary change is a
    single atomic write — config, transition record and absorbed registry can
    never diverge into a half-updated mix.

    吸收语义（B1）：`absorbed` 只保存**已经完成的过渡**造成的吸收事实；最近
    一次过渡的计划吸收在其等待生效期间同样参与跳过（那时它正被跨越周期覆盖），
    但只有在过渡真正走完后才会在下一次边界修改时转入永久登记——生效前再次
    修改边界会整体重算计划吸收，原计划日期自动恢复为有效周期。
    """
    raw = db.load_app_setting(PLANNING_BOUNDARY_STATE_KEY)
    if raw is db.APP_SETTING_QUERY_FAILED:
        raise PlanningError("database_unavailable", "规划周期配置暂时无法读取", 503)
    if not isinstance(raw, dict):
        raw = _default_boundary_state()
    try:
        boundary = parse_refresh_boundary(
            raw.get("boundary") or DEFAULT_REFRESH_BOUNDARY.strftime("%H:%M")
        )
    except ValueError as exc:
        raise PlanningError("invalid_setting", "规划周期刷新时间配置无效", 500) from exc
    transition = None
    planned_absorbed: frozenset[date] = frozenset()
    info = raw.get("transition")
    if isinstance(info, dict):
        try:
            planned = BoundaryTransition.plan(
                date.fromisoformat(info["spanning_key"]),
                parse_refresh_boundary(info["spanning_boundary"]),
                _parse_dt(info["change_at"], "refresh_boundary_change_at"),
                boundary,
            )
        except (KeyError, TypeError, ValueError):
            log.warning("planning 边界过渡记录无效，按无过渡处理")
        else:
            if planned.active_at(now):
                transition = planned
            # 等待生效与已完成的过渡都参与跳过；只有被新修改取代（未完成）
            # 时其计划吸收才被丢弃（见 set_cycle_settings）。
            planned_absorbed = frozenset(planned.absorbed_cycle_keys())
    absorbed = set()
    for value in raw.get("absorbed") or []:
        try:
            absorbed.add(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    return boundary, transition, frozenset(absorbed | planned_absorbed)


def get_cycle_settings(now: datetime | None = None) -> dict[str, Any]:
    """Read the persisted planning boundary, daily switch and recompute config.

    The cycle reflects a pending boundary transition: the frozen spanning
    cycle keeps its original boundary until the newly configured boundary
    first occurs, no matter how many times the boundary is re-configured.
    """
    now = now or _now()
    boundary, transition, _ = _load_boundary_state(now)
    daily_raw = db.load_app_setting(PLANNING_DAILY_REFRESH_KEY)
    if daily_raw is db.APP_SETTING_QUERY_FAILED:
        raise PlanningError("database_unavailable", "每日刷新配置暂时无法读取", 503)
    # An absent or malformed switch keeps the established enabled default.
    if not isinstance(daily_raw, bool):
        daily_raw = True
    cycle = planning_cycle_at(now, boundary, transition)
    result = {
        "refresh_boundary_time": boundary.strftime("%H:%M"),
        "daily_refresh_enabled": daily_raw,
        "timezone": "Asia/Shanghai",
        "cycle_key": cycle.key.isoformat(),
        "cycle_start": cycle.start.isoformat(),
        "cycle_end": cycle.end.isoformat(),
    }
    if transition is not None:
        result["pending_boundary"] = {
            "spanning_key": transition.spanning_key.isoformat(),
            "previous_time": transition.spanning_boundary.strftime("%H:%M"),
            "change_at": transition.change_at.isoformat(),
            "effective_at": transition.effective_at.isoformat(),
        }
    auto_enabled_raw = db.load_app_setting(PLANNING_AUTO_RECOMPUTE_ENABLED_KEY)
    if auto_enabled_raw is db.APP_SETTING_QUERY_FAILED:
        raise PlanningError("database_unavailable", "自动重算配置暂时无法读取", 503)
    result["auto_recompute_enabled"] = True if not isinstance(auto_enabled_raw, bool) else auto_enabled_raw
    wait_raw = db.load_app_setting(PLANNING_AUTO_RECOMPUTE_WAIT_KEY)
    if wait_raw is db.APP_SETTING_QUERY_FAILED:
        raise PlanningError("database_unavailable", "自动重算配置暂时无法读取", 503)
    result["auto_recompute_wait_minutes"] = (
        wait_raw if isinstance(wait_raw, int) and 1 <= wait_raw <= 1440
        else int(RECOMPUTE_WAIT.total_seconds() // 60)
    )
    return result


def set_cycle_settings(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """Persist one planning setting; a boundary change is a single atomic
    state write that schedules the next-cycle transition instead of
    reinterpreting the cycle already in progress."""
    now = now or _now()
    allowed = {
        "refresh_boundary_time", "daily_refresh_enabled",
        "auto_recompute_enabled", "auto_recompute_wait_minutes",
    }
    if not isinstance(payload, dict) or len(payload) != 1 or set(payload) - allowed:
        raise PlanningError("invalid_payload", f"一次仅接受以下之一：{', '.join(sorted(allowed))}", 400)
    if "refresh_boundary_time" in payload:
        try:
            boundary = parse_refresh_boundary(payload["refresh_boundary_time"])
        except ValueError as exc:
            raise PlanningError("invalid_payload", "刷新时间必须是 00:00 至 23:59", 400) from exc
        # B1：吸收事实只在过渡真正走完时入账。读取原始状态以区分
        # 「已完成过渡的吸收」与「等待生效过渡的计划吸收」。
        raw_state = db.load_app_setting(PLANNING_BOUNDARY_STATE_KEY)
        if raw_state is db.APP_SETTING_QUERY_FAILED:
            raise PlanningError("database_unavailable", "规划周期配置暂时无法读取", 503)
        if not isinstance(raw_state, dict):
            raw_state = _default_boundary_state()
        committed = set()
        for value in raw_state.get("absorbed") or []:
            try:
                committed.add(date.fromisoformat(value))
            except (TypeError, ValueError):
                continue
        info = raw_state.get("transition")
        boundary_now = parse_refresh_boundary(
            raw_state.get("boundary") or DEFAULT_REFRESH_BOUNDARY.strftime("%H:%M")
        )
        stored = None
        if isinstance(info, dict):
            # 旧过渡的有效点用它被规划时的边界（= 写入时的当前配置）评估，
            # 不能用本次要改的新边界，否则会把未完成的过渡误判为已完成。
            try:
                stored = BoundaryTransition.plan(
                    date.fromisoformat(info["spanning_key"]),
                    parse_refresh_boundary(info["spanning_boundary"]),
                    _parse_dt(info["change_at"], "refresh_boundary_change_at"),
                    boundary_now,
                )
            except (KeyError, TypeError, ValueError):
                stored = None
        if stored is not None and stored.active_at(now):
            # 连续修改：跨越周期冻结不变，只重算生效点；旧计划吸收尚未发生，
            # 随新计划整体重算，不入账。
            planned = BoundaryTransition.plan(
                stored.spanning_key, stored.spanning_boundary, now, boundary)
            new_absorbed = committed
        else:
            if stored is not None:
                # 旧过渡已经真正走完：其计划吸收成为事实，转入永久登记。
                committed |= set(stored.absorbed_cycle_keys())
            planned = (
                BoundaryTransition.plan_first(boundary_now, now, boundary)
                if boundary != boundary_now else None
            )
            new_absorbed = committed
        if planned is not None:
            state = {
                "boundary": boundary.strftime("%H:%M"),
                "transition": {
                    "spanning_key": planned.spanning_key.isoformat(),
                    "spanning_boundary": planned.spanning_boundary.strftime("%H:%M"),
                    "change_at": _iso(planned.change_at),
                },
                "absorbed": sorted(d.isoformat() for d in new_absorbed),
            }
            # 一次原子写入：配置、过渡记录与吸收周期登记不会出现半更新。
            if not db.save_app_setting(PLANNING_BOUNDARY_STATE_KEY, state):
                raise PlanningError("database_unavailable", "规划周期配置保存失败", 503)
    if "daily_refresh_enabled" in payload:
        if not isinstance(payload["daily_refresh_enabled"], bool):
            raise PlanningError("invalid_payload", "daily_refresh_enabled 必须是布尔值", 400)
        if not db.save_app_setting(PLANNING_DAILY_REFRESH_KEY, payload["daily_refresh_enabled"]):
            raise PlanningError("database_unavailable", "每日刷新配置保存失败", 503)
    if "auto_recompute_enabled" in payload:
        if not isinstance(payload["auto_recompute_enabled"], bool):
            raise PlanningError("invalid_payload", "auto_recompute_enabled 必须是布尔值", 400)
        if not db.save_app_setting(PLANNING_AUTO_RECOMPUTE_ENABLED_KEY, payload["auto_recompute_enabled"]):
            raise PlanningError("database_unavailable", "自动重算配置保存失败", 503)
    if "auto_recompute_wait_minutes" in payload:
        value = payload["auto_recompute_wait_minutes"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1440:
            raise PlanningError("invalid_payload", "auto_recompute_wait_minutes 必须是 1 至 1440 的整数", 400)
        if not db.save_app_setting(PLANNING_AUTO_RECOMPUTE_WAIT_KEY, value):
            raise PlanningError("database_unavailable", "自动重算配置保存失败", 503)
    return get_cycle_settings(now)


def _current_cycle(now: datetime) -> PlanningCycle:
    boundary, transition, _ = _load_boundary_state(now)
    return planning_cycle_at(now, boundary, transition)


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
    只处理出现的字段。未知字段一律拒绝。2026-09-27 窗口批次：可安排时段
    （``window_start_tod`` / ``window_end_tod``，§6.7）取代显式起止与限时
    截止成为新业务事实来源；旧 explicit / deadline 字段停止新写入（白名单
    移除即拒绝），列与存量行按历史语义保留。创建与编辑白名单分离（Review
    HIGH 修复）：窗口字段仅在创建入口接受，编辑入口在批次 6 前明确拒绝。
    """
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")

    allowed = {
        "content", "task_type", "interval_days", "weekdays", "month_days",
        "target_date", "time_mode", "estimated_minutes",
        "is_hollow", "hollow_start_content", "hollow_start_minutes",
        "hollow_wait_minutes", "hollow_wait_note", "hollow_end_content",
        "hollow_end_minutes",
        "alarm_start", "alarm_end", "timer_minutes", "is_active",
        "refresh_mode", "refresh_anchor_at", "refresh_enabled",
    }
    if not partial:
        # 创建与编辑白名单分离（2026-09-27 Review HIGH）：窗口字段仅在创建
        # 入口接受；编辑（PATCH）在批次 6 的 current/future 语义与完整校验
        # 落地前明确拒绝（见下方专用检查），不得静默忽略或绕过校验写入。
        allowed |= {"window_start_tod", "window_end_tod"}
    if partial and ("window_start_tod" in payload or "window_end_tod" in payload):
        raise PlanningError("invalid_payload", "可安排时段暂不支持编辑", 400)
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

    if "refresh_mode" in payload:
        mode = payload["refresh_mode"]
        if not isinstance(mode, str):
            raise PlanningError("invalid_payload", "refresh_mode must be a string")
        result["refresh_mode"] = mode
    if "refresh_anchor_at" in payload:
        raw = payload["refresh_anchor_at"]
        result["refresh_anchor_at"] = _iso(_parse_dt(raw, "refresh_anchor_at")) if raw else None
    if "refresh_enabled" in payload:
        # 暂停/恢复刷新必须是明确布尔值：null 静默变成 false（暂停）是语义陷阱。
        if payload["refresh_enabled"] is None:
            raise PlanningError("invalid_payload", "refresh_enabled 必须是布尔值", 400)
        result["refresh_enabled"] = _clean_bool(payload["refresh_enabled"], "refresh_enabled")

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
        if partial and not result["estimated_minutes"]:
            # 四轮修复（Review MEDIUM + user 产品事实核验）：预计耗时是
            # 可自动排程待办的必填信息——创建入口已强制（含 hollow / idle），
            # 前端编辑从不提交空值；编辑入口把任务清成无耗时形状属校验
            # 缺失，会使未来轮次生成无 planned_minutes 快照的实例。清空
            # 一律拒绝；排程层对异常缺耗时行另有防御性 skip 兜底。
            raise PlanningError(
                "invalid_payload", "预计耗时不能清空：请填写 1–1440 分钟的有效预计耗时", 400,
            )
    if "window_start_tod" in payload:
        raw = payload.get("window_start_tod")
        result["window_start_tod"] = _tod_str(raw, "window_start_tod")
    if "window_end_tod" in payload:
        raw = payload.get("window_end_tod")
        result["window_end_tod"] = _tod_str(raw, "window_end_tod")
    if result.get("window_start_tod") or result.get("window_end_tod"):
        # 形状校验与批次 1 领域构造同源：start == end 无效（不解释为 24h
        # 窗口）；单侧约束合法。boundary 跨越与可行性在创建入口校验。
        try:
            _task_window_template(result)
        except ValueError as exc:
            raise PlanningError(
                "invalid_payload",
                "可安排时段的开始与结束不能相同（相同时刻不代表 24 小时窗口）", 400,
            ) from exc

    if not partial:
        result.setdefault("time_mode", "duration")
        for flag in ("is_fixed", "is_hollow", "alarm_start", "alarm_end"):
            result.setdefault(flag, False)
        result.setdefault("is_active", True)

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

    if not partial:
        mode = result.get("time_mode", "duration")
        if mode == "explicit":
            # 显式起止随窗口批次停止新写入：新任务一律为耗时（+ 可选时段）。
            raise PlanningError(
                "invalid_payload", "显式起止已停用：请改用预计耗时与可安排时段", 400,
            )
        if not result.get("estimated_minutes"):
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
    if not occ.get("schedule_date"):
        return None  # legacy identity is not guessed from for_date
    schedule_date = date.fromisoformat(occ["schedule_date"])
    tod = time.fromisoformat(end_tod)
    return _iso(_combine(schedule_date, tod))


def schedule_label(occ: dict[str, Any], task: dict[str, Any], now: datetime) -> str:
    """排列状态标签：超时 / 落后 / 前进 / 正常（仅展示；「落后」避开与「延后」状态撞名）。"""
    if occ["status"] == "timeout":
        return "超时"
    if occ["status"] == "deferred":
        return "落后"
    est_start = _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
    if est_start and est_start < now and occ["status"] == "pending":
        return "落后"
    nominal = occ.get("nominal_start")
    if est_start and nominal and est_start < _parse_dt(nominal, "nominal_start") - timedelta(seconds=60):
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
        # 有效耗时单一权威语义（N5）：显式区间优先，与排程同源；任务定义
        # 修改只影响未来轮次。
        "estimated_minutes": _effective_minutes(occ, task),
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
        "cursor_date": task.get("cursor_date"),
        "next_due": next_due,
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


# ── 任务 CRUD ─────────────────────────────────────────────────────

def _task_window_template(task: dict[str, Any]) -> WindowTemplate | None:
    """任务行的可安排时段模板（§6.7）；两端皆空 = 无窗口，正常自动排程。

    行内值与 payload 规范化结果同为 ``HH:MM`` 字符串；数据库返回的
    ``HH:MM:SS`` 同样可解析（秒为 0，与模板分钟精度契约一致）。
    形状非法（start == end）由 :class:`WindowTemplate` 构造拒绝。
    """
    start = task.get("window_start_tod")
    end = task.get("window_end_tod")
    if not start and not end:
        return None
    return WindowTemplate(
        start_tod=time.fromisoformat(start) if start else None,
        end_tod=time.fromisoformat(end) if end else None,
    )


def _window_occupancy_minutes(task: dict[str, Any]) -> int | None:
    """窗口可行性判断的占用跨度（§12.1 / §17.4）：
    普通待办 = 预计耗时；中空待办 = 开始 + 等待 + 结束的整个包络。"""
    if task.get("is_hollow"):
        return hollow_envelope_minutes(
            task["hollow_start_minutes"], task["hollow_wait_minutes"],
            task["hollow_end_minutes"],
        )
    return task.get("estimated_minutes")


def _validate_window_creation(row: dict[str, Any], now: datetime) -> None:
    """创建入口的窗口与产品边界校验（§10 / §12.1 / §30.6 / §32.40 / §32.41）。

    * once 目标日期不得早于当前业务日期（Asia/Shanghai 当日，自然日比较）；
      系统补生成路径不经过本入口，不受此限；
    * 双侧窗口禁止跨越每日刷新 boundary（端点接触合法；单侧约束不校验）；
    * 指定日期 once 按严格自然日解析（不按生成时刻顺延），窗口在创建时
      已经不可容纳占用跨度 → 直接拒绝（不顺延到下一候选）；only-earliest
      保持「只有下界」语义，不凭空补截止；
    * 未指定日期路径按当前周期候选解析后判断剩余空间（§12.1）。
    解析与可行性判断全部调用批次 1 领域函数，与生成冻结共用同一套数学。
    """
    if row.get("task_type") == "once":
        today = _cst_date(now)
        target = _parse_date(row.get("target_date"), "target_date")
        if target < today:
            raise PlanningError(
                "invalid_payload",
                f"目标日期不能早于当前业务日期（{today.isoformat()}）", 400,
            )
    template = _task_window_template(row)
    if template is None:
        return
    occupancy = _window_occupancy_minutes(row)
    if not isinstance(occupancy, int) or occupancy < 1:
        raise PlanningError("invalid_payload", "填写了可安排时段的待办必须提供有效预计耗时", 400)
    boundary, _, _ = _load_boundary_state(now)
    try:
        validate_template_window(template, boundary)
    except ValueError as exc:
        raise PlanningError(
            "invalid_payload",
            f"可安排时段不能跨越每日刷新时间 {boundary.strftime('%H:%M')}，请调整时段", 400,
        ) from exc
    if row["task_type"] == "once":
        # 指定日期 once：user 自然日期 + 时刻组合成固定绝对约束，不做
        # 候选取舍（§32.41）；创建时已不可用即拒绝，绝不顺延。
        resolved = resolve_window_on_date(
            template, _parse_date(row["target_date"], "target_date"))
    else:
        resolved = resolve_window(template, _current_cycle(now).key, now)
    if not window_feasible(resolved, now, occupancy):
        raise PlanningError(
            "invalid_payload",
            f"可安排时段剩余空间不足以容纳预计耗时 {occupancy} 分钟，请调整时段或耗时", 400,
        )


def create_task(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    row = validate_task_payload(payload, partial=False)
    _prepare_refresh_definition(row, now)
    _validate_window_creation(row, now)
    row["created_at"] = _iso(now)
    row["updated_at"] = _iso(now)
    row["is_fixed"] = bool(row.get("is_fixed"))
    client = _require_client()
    response = client.table("planning_task").insert(row).execute()
    created = (response.data or [{}])[0]
    # 即时生成：新建的待办（含 interval 立即到期）不等后台循环，立刻出现在列表。
    _generate_due_quietly(client, now)
    return serialize_task(created, now)


def _prepare_refresh_definition(row: dict[str, Any], now: datetime, current: dict[str, Any] | None = None) -> None:
    """Classify each newly written task; never guess the old interval variant."""
    combined = {**(current or {}), **row}
    task_type = combined["task_type"]
    default = {
        "daily": "daily", "weekly": "fixed_weekday", "monthly": "fixed_monthday",
        "once": "none", "idle": "none",
    }.get(task_type)
    mode = combined.get("refresh_mode")
    if mode is None:
        if task_type == "interval":
            raise PlanningError("invalid_payload", "间歇待办必须明确选择刷新模式", 400)
        mode = default
    try:
        validate_task_refresh_mode(task_type, mode)
    except ValueError as exc:
        raise PlanningError("invalid_payload", "刷新模式与待办类型不匹配", 400) from exc
    if mode == "after_completion" and not 1 <= (combined.get("interval_days") or 0) <= 365:
        raise PlanningError("invalid_payload", "处理后刷新间隔必须为 1 至 365 天", 400)
    row["refresh_mode"] = mode
    mode_changed = current is not None and current.get("refresh_mode") != mode
    if mode_changed:
        row["refresh_generated_through"] = None
    if mode == "fixed_interval":
        row["refresh_anchor_at"] = (
            row.get("refresh_anchor_at")
            or (current.get("refresh_anchor_at") if current and not mode_changed else None)
            or _iso(now)
        )
        if mode_changed:
            row["last_handled_at"] = None
    else:
        if row.get("refresh_anchor_at"):
            raise PlanningError("invalid_payload", "只有固定间隔待办可以设置刷新起点", 400)
        if mode_changed:
            row["refresh_anchor_at"] = None
    if mode_changed and mode != "after_completion":
        row["last_handled_at"] = None
        row["refresh_next_due_at"] = None
    elif mode_changed:
        row["refresh_next_due_at"] = None
    elif mode == "after_completion" and "interval_days" in row:
        handled = combined.get("last_handled_at")
        row["refresh_next_due_at"] = (
            _iso(_parse_dt(handled, "last_handled_at") + timedelta(days=combined["interval_days"]))
            if handled else None
        )
    if combined.get("is_fixed") and not combined.get("est_start_tod"):
        raise PlanningError("invalid_payload", "固定时间必须有有效的预估开始时间", 400)


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
    "refresh_mode", "refresh_anchor_at",
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
    if any(field in row and row[field] != task.get(field) for field in
           ("task_type", "refresh_mode", "refresh_anchor_at")):
        existing = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).limit(1))
        if existing:
            raise PlanningError("round_identity_locked", "已有业务轮次时不能改变刷新模式、类型或首次基准", 409)
    if "target_date" in row and row.get("target_date") is None:
        raise PlanningError("invalid_payload", "target_date cannot be empty for once tasks")
    if task.get("time_mode") == "explicit" and row.get("time_mode") == "duration":
        # The former rule anchor is no longer part of the task definition.
        row["est_start_tod"] = None
        row["est_end_tod"] = None
        row["is_fixed"] = False
    if task.get("refresh_mode") is not None or "refresh_mode" in row or "task_type" in row:
        _prepare_refresh_definition(row, now, task)
    merged = {**task, **row}
    _ensure_type_requirements(merged)
    if (task.get("time_mode") == "explicit" and merged.get("time_mode") == "duration"
            and not merged.get("estimated_minutes")):
        raise PlanningError("invalid_payload", "仅耗时待办必须提供有效预估耗时", 400)

    # 废弃整个任务：终止后续刷新，并关闭所有仍开放的出现实例。
    reactivated = bool(row.get("is_active")) and not task.get("is_active")
    if reactivated and task.get("request_state") == "superseded":
        # H2/I6：被取代的重排请求是终态，不得通过普通启用入口复活。
        raise PlanningError(
            "invalid_transition", "该任务来自已被取代的重排请求，不能重新启用", 409,
        )
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

    row["updated_at"] = _iso(now)
    # 仅当调度规则字段**实际发生变化**时才视为规则编辑：refresh_enabled
    # （暂停/恢复刷新）等内容类 PATCH 不得重置生成游标，否则恢复刷新会把
    # 游标改写到恢复日前一天，跳过暂停期间的固定轴轮次（需求 24B）。
    schedule_touched = any(
        field in row and row[field] != task.get(field) for field in SCHEDULE_FIELDS
    )
    # 恢复刷新（False→True）沿用创建入口的同步补生成先例：恢复后立即进入
    # 现有生成体系，当期应有轮次不等下一个维护周期。
    resume_refresh = row.get("refresh_enabled") is True and task.get("refresh_enabled") is False
    schedule_changed = False
    if reactivated or (schedule_touched and task.get("is_active")):
        # 新轮次由规则与持久身份决定。编辑规则不删除既有业务轮次。
        schedule_changed = True
    if schedule_touched:
        row["refresh_generated_through"] = (_current_cycle(now).key - timedelta(days=1)).isoformat()

    # 已生成实例冻结：任务规则编辑不重建、不删除、不改写任何已生成轮次的
    # 身份或实例级数据。唯一的同步是限时窗口（需求 18.3）：开放实例必须
    # 立即跟随新的有效时间范围（is_limited 与 deadline_at 快照一起更新），
    # 超时判定才不会失真；已关闭历史不得按现在的截止时间重新解释。
    if "deadline_tod" in row or "deadline_end_tod" in row:
        limited = merged.get("deadline_tod") is not None
        end_tod = merged.get("deadline_end_tod") or merged.get("deadline_tod")
        for occ_row in _rows(
            client, "planning_occurrence",
            lambda q: q.eq("task_id", task_id).in_("status", list(OPEN_STATUSES)),
        ):
            if not occ_row.get("round_key"):
                continue
            new_deadline = None
            if limited and end_tod and occ_row.get("schedule_date"):
                new_deadline = _iso(_combine(
                    _parse_date(occ_row["schedule_date"], "schedule_date"),
                    time.fromisoformat(end_tod),
                ))
            if (occ_row.get("is_limited") == limited
                    and occ_row.get("deadline_at") == new_deadline):
                continue
            client.table("planning_occurrence").update({
                "is_limited": limited, "deadline_at": new_deadline,
                "updated_at": _iso(now),
            }).eq("id", occ_row["id"]).execute()

    response = client.table("planning_task").update(row).eq("id", task_id).execute()
    updated = (response.data or [{}])[0]
    if row.get("is_active") is False:
        request_recompute("task_discarded", now)
    elif schedule_changed or resume_refresh:
        # 规则变更后只尝试当前应有轮次；唯一键保护既有轮次。
        _generate_due_quietly(client, now)
    return serialize_task(updated, now)


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
    if task.get("time_mode") == "explicit" and not (
        task.get("est_end_tod") or task.get("estimated_minutes")
    ):
        raise PlanningError("invalid_payload", "显式预估时间必须有结束时间或有效耗时")


def list_tasks(include_inactive: bool = True, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or _now()
    client = _require_client()
    query_fn = None if include_inactive else lambda q: q.eq("is_active", True)
    rows = _rows(client, "planning_task", query_fn)
    # 被取代 / 已收尾的内部重排请求不是用户独立待办，不出现在任务列表。
    rows = [row for row in rows if row.get("request_state") != "superseded"]
    rows.sort(key=lambda r: r["id"])
    return [serialize_task(row, now) for row in rows]


# ── 出现实例生成（只依据任务定义与规则游标） ──────────────────────

def _once_schedule_date(
    task: dict[str, Any], configured: time, transition: BoundaryTransition | None,
) -> date:
    """指定日期 once 的内部规划周期归属（2026-09-27 分离裁决，§32.41）。

    * 双端窗口 / 只有最早开始 → 窗口起点绝对时刻所属规划周期；
    * 只有最晚完成 → 该唯一指定时刻所属规划周期；
    * 无窗口 → target_date（现行规则沿用）。

    boundary 只参与此内部归属换算（时间早于 boundary 自然归属前一天），
    不得改写 target_date 或绝对窗口；结果允许早于 target_date。
    """
    target = _parse_date(task["target_date"], "target_date")
    template = _task_window_template(task)
    if template is None or template.is_empty:
        return target
    resolved = resolve_window_on_date(template, target)
    instant = resolved.start_at if resolved.start_at is not None else resolved.end_at
    return planning_cycle_at(instant, configured, transition).key


def _resolve_generation_window(
    task: dict[str, Any], schedule_date: date, now: datetime,
) -> tuple[ResolvedWindow | None, datetime | None, datetime | None]:
    """把模板窗口解析为本轮冻结的实例窗口，并给出零自由度预锚定 est。

    * 解析全部调用批次 1 领域函数，不在本模块重写窗口数学，并按「是否
      指定日期」二分（§6.7、§32.41，两类语义不得混用）：
      **once（指定日期）**——严格自然日解析（``resolve_window_on_date``，
      锚点 = target_date 而非本轮 schedule_date）：user 日期 + 时刻组合成
      固定绝对约束，不按生成时刻做候选取舍、不顺延、不改写；
      **周期任务（未指定日期）**——候选解析（``resolve_window``，锚点 =
      本轮 schedule_date，参考时刻 = 本轮生成时刻）；
    * 窗口在生成时一次解析并随行写入 ``window_start_at`` / ``window_end_at``
      （§6.7 生成即冻结）；顺延、展示周期变化、模板后续修改均不改写；
    * 零自由度（双侧窗口长恰等于占用跨度：普通 = 预计耗时；中空 = 整个
      包络跨度）时预锚定 est 在窗口起点——沿用原 explicit 分支的 rule 固定
      所有权写入形状（机制继承，§13.2 固定由时间约束涌现，不是独立属性）；
      中空结束阶段经等待链在窗口终点收口（§17.4 包络）；
    * 单侧约束 / 非零自由度 / 无窗口不预锚定，est 留待排程层派生。
    """
    template = _task_window_template(task)
    if template is None:
        return None, None, None
    if task["task_type"] == "once":
        resolved = resolve_window_on_date(
            template, _parse_date(task["target_date"], "target_date"))
    else:
        resolved = resolve_window(template, schedule_date, now)
    occupancy = _window_occupancy_minutes(task)
    if (resolved.start_at is None or resolved.end_at is None
            or not isinstance(occupancy, int) or occupancy < 1):
        return resolved, None, None
    # 绝对瞬间域比较（回拨日钟面差 ≠ 绝对差，见 planning_window._absolute）。
    span = (resolved.end_at.astimezone(timezone.utc)
            - resolved.start_at.astimezone(timezone.utc))
    if span != timedelta(minutes=occupancy):
        return resolved, None, None
    est_start = resolved.start_at
    est_end = est_start + timedelta(
        minutes=task["hollow_start_minutes"] if task.get("is_hollow") else occupancy,
    )
    return resolved, est_start, est_end


def _generation_snapshots(
    task: dict[str, Any], schedule_date: date, phase: str | None,
) -> dict[str, Any]:
    """生成时冻结的展示 / 规则快照（BF5）。

    任务定义后续修改只影响未来实例：已生成实例的名称、中空阶段文案与
    time_mode 一律取自本行快照，序列化绝不回读任务当前定义。

    窗口批次（2026-09-27）：``deadline_at`` 生成期停止写入——新行恒 NULL，
    ``is_limited`` 恒 False（满足 1A 身份 CHECK 形状）；存量限时行按历史
    语义走完生命周期（迁移表「停止新写入但暂时保留兼容」）。窗口事实由
    ``_resolve_generation_window`` 随行写入并冻结。
    """
    if phase == "start":
        display = f"{task.get('hollow_start_content') or task['content']}·开始"
    elif phase == "end":
        display = f"{task.get('hollow_end_content') or task['content']}·结束"
    else:
        display = task["content"]
    return {
        "content_snapshot": task["content"],
        "display_content": display,
        "time_mode_snapshot": task["time_mode"],
        "deadline_at": None,
    }


def _occurrence_row(
    task: dict[str, Any], phase: str | None,
    est_start: datetime | None, est_end: datetime | None, now: datetime,
    identity: OccurrenceIdentity,
    window: ResolvedWindow | None = None,
) -> dict[str, Any]:
    # est 仅来自零自由度窗口预锚定（explicit 分支已退役）：预锚定即 rule
    # 固定所有权（机制继承）；无预锚定时 est 留待排程层派生。
    source = "rule" if est_start is not None else "unassigned"
    fixed = est_start is not None
    ownership = EstimatedTimeOwnership(
        source=source,
        fixed_source="rule" if fixed else None,
        schedule_managed=True,
        estimated_start=est_start,
        estimated_end=est_end,
    )
    return {
        "task_id": task["id"],
        "round_key": identity.round_key,
        "schedule_date": identity.schedule_date.isoformat(),
        "display_cycle_date": identity.display_cycle_date.isoformat(),
        "display_reason": identity.display_reason,
        "fixed_due_at": None,  # set by the caller for fixed-rule rounds
        "phase_group": str(identity.phase_group) if identity.phase_group else None,
        "for_date": identity.schedule_date.isoformat(),  # legacy compatibility mirror only
        "phase": phase,
        "est_start": _iso(est_start) if est_start else None,
        "est_end": _iso(est_end) if est_end else None,
        "nominal_start": _iso(est_start) if est_start else None,
        "status": "pending",
        "planned_minutes": (
            task.get("hollow_start_minutes" if phase == "start" else "hollow_end_minutes")
            if task.get("is_hollow") else task.get("estimated_minutes")
        ),
        "planned_wait_minutes": (
            task.get("hollow_wait_minutes") if task.get("is_hollow") and phase == "end" else None
        ),
        "sort_order": task["id"] * 10 + (1 if phase == "end" else 0) + (
            100_000 if task["task_type"] == "idle" else 0
        ),
        "is_fixed": fixed,
        "estimated_time_source": ownership.source,
        "fixed_source": ownership.fixed_source,
        "schedule_managed": ownership.schedule_managed,
        "is_limited": False,  # 窗口批次：生成期停止写入 deadline 事实
        "window_start_at": _iso(window.start_at) if window and window.start_at else None,
        "window_end_at": _iso(window.end_at) if window and window.end_at else None,
        "source": "schedule",
        **_generation_snapshots(task, identity.schedule_date, phase),
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }


def _create_occurrences(
    client, task: dict[str, Any], schedule_date: date, now: datetime,
    *, due_at: datetime | None = None, display_cycle_date: date | None = None,
    generation_request_key: str | None = None,
) -> int:
    mode = task.get("refresh_mode")
    if mode is None:
        raise PlanningError("unclassified_task", "旧任务定义须在受控迁移中分类", 409)
    try:
        validate_task_refresh_mode(task["task_type"], mode)
    except ValueError as exc:
        raise PlanningError("invalid_task", "任务刷新模式与类型不匹配", 409) from exc
    current_cycle = display_cycle_date or _current_cycle(now).key
    if mode == "none":
        round_key = "once"
    elif mode == "after_completion":
        if due_at is None:
            raise PlanningError("invalid_task", "处理后刷新任务缺少本轮到期基准", 409)
        round_key = timed_round_key("handled", schedule_date, _iso(due_at))
    elif mode == "fixed_interval":
        if due_at is None:
            raise PlanningError("invalid_task", "固定间隔任务缺少本轮到期事件", 409)
        round_key = fixed_round_key(due_at)
    else:
        round_key = calendar_round_key(schedule_date)
    fixed_mode = mode in ("daily", "fixed_interval", "fixed_weekday", "fixed_monthday")
    display_cycle = (current_cycle if fixed_mode and due_at is not None and due_at <= now
                     else max(schedule_date, current_cycle))
    display_reason = "carryover" if display_cycle > schedule_date else "initial"
    window, est_start, est_end = _resolve_generation_window(task, schedule_date, now)
    rows: list[dict[str, Any]] = []
    if task.get("is_hollow"):
        wait = timedelta(minutes=task["hollow_wait_minutes"])
        end_minutes = task["hollow_end_minutes"]
        end_start = (est_end + wait) if est_end else None
        end_end = (end_start + timedelta(minutes=end_minutes)) if end_start else None
        group = round_phase_group(task["id"], round_key)
        for phase, start, end in (("start", est_start, est_end), ("end", end_start, end_end)):
            identity = OccurrenceIdentity(task["id"], round_key, schedule_date, display_cycle,
                                          display_reason, phase, group)
            rows.append(_occurrence_row(task, phase, start, end, now, identity, window=window))
            if fixed_mode and due_at is not None:
                rows[-1]["fixed_due_at"] = _iso(due_at)
    else:
        identity = OccurrenceIdentity(task["id"], round_key, schedule_date, display_cycle,
                                      display_reason)
        rows.append(_occurrence_row(task, None, est_start, est_end, now, identity, window=window))
        if fixed_mode and due_at is not None:
            rows[-1]["fixed_due_at"] = _iso(due_at)
    if generation_request_key:
        # 幂等身份随实例单次插入落库（超时重排等请求），配合部分唯一索引
        # 保证重放 / 并发只产生一份结果。
        for row in rows:
            row["generation_request_key"] = generation_request_key
    try:
        # One PostgREST request: both hollow phases commit or fail together.
        client.table("planning_occurrence").insert(rows if len(rows) > 1 else rows[0]).execute()
    except Exception as exc:
        if "planning_occurrence_round_phase_uq" in str(exc):
            log.info("planning 轮次已存在: task=%s round=%s", task["id"], round_key)
            return 0
        raise
    return len(rows)


def _should_occur(task: dict[str, Any], day: date) -> bool:
    task_type = task["task_type"]
    if task_type == "daily":
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


def _fixed_rounds(
    task: dict[str, Any], cycle: PlanningCycle, now: datetime,
    configured_boundary: time, transition: BoundaryTransition | None,
    absorbed: frozenset[date] = frozenset(),
) -> list[tuple[date, datetime]]:
    """Enumerate rule events ``(progress date, due)`` through now.

    Each event pair is the progress marker checked against
    ``refresh_generated_through`` and the rule due instant that produced the
    round. Fixed-interval events are pure anchor arithmetic; calendar events
    are planning cycles whose start day matches the rule, each mapped through
    the boundary that governed that cycle's start. Days recorded as
    transition-absorbed never name a planning cycle and are never enumerated,
    so a finished transition cannot resurrect them as missed runs.
    """
    mode = task["refresh_mode"]
    if mode == "fixed_interval":
        anchor = _parse_dt(task.get("refresh_anchor_at"), "refresh_anchor_at")
        interval = task.get("interval_days")
        if not isinstance(interval, int) or interval < 1:
            raise PlanningError("invalid_task", "固定间隔任务缺少有效天数", 409)
        events = []
        due = anchor
        while due <= now:
            events.append((due.date(), due))
            due += timedelta(days=interval)
        return events
    created = _parse_dt(task.get("created_at"), "created_at")
    start = planning_cycle_at(created, configured_boundary, transition).key
    events = []
    day = start
    while day <= cycle.key:
        # A boundary transition extends the spanning cycle past its natural
        # end; the absorbed intermediate days are registered when the change
        # is made and stay excluded forever, independent of transition state.
        if day not in absorbed:
            if _should_occur(task, day):
                boundary = cycle_start_boundary(day, configured_boundary, transition)
                due = (max(created, PlanningCycle.for_key(day, boundary).start)
                       if day == start else PlanningCycle.for_key(day, boundary).start)
                if due >= created:
                    events.append((day, due))
        day += timedelta(days=1)
    return events


def _task_open_rows(client, task_id: int) -> list[dict[str, Any]]:
    """Page open rounds so a long outage cannot hide rows behind the API limit."""
    rows: list[dict[str, Any]] = []
    after_id = 0
    while True:
        batch = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id)
                      .in_("status", list(OPEN_STATUSES)).gte("id", after_id + 1)
                      .order("id").limit(500))
        if not batch:
            return rows
        rows.extend(batch)
        after_id = max(row["id"] for row in batch)
        if len(batch) < 500:
            return rows


def _carry_open_rounds(client, task: dict[str, Any], cycle_key: date, now: datetime) -> None:
    """Move open rounds forward into the current cycle; display never moves
    backward, because a boundary change takes effect from the next cycle."""
    open_rows = _task_open_rows(client, task["id"])
    seen: set[str] = set()
    for occ in open_rows:
        round_key = occ.get("round_key")
        if not round_key or round_key in seen:
            continue  # Legacy identity is not inferred from for_date.
        seen.add(round_key)
        display = occ.get("display_cycle_date")
        if not display:
            continue
        if date.fromisoformat(display) >= cycle_key:
            continue
        client.table("planning_occurrence").update({
            "display_cycle_date": cycle_key.isoformat(),
            "display_reason": "carryover", "updated_at": _iso(now),
        }).eq("task_id", task["id"]).eq("round_key", round_key).execute()


def _expire_fixed_rounds(
    client, task: dict[str, Any], events: list[tuple[date, datetime]], now: datetime,
) -> int:
    """A fixed round dies at its next rule event, regardless of handling history."""
    open_rows = _task_open_rows(client, task["id"])
    expired = 0
    expired_rounds: set[str] = set()
    for occ in open_rows:
        round_key = occ.get("round_key")
        if not round_key or round_key in expired_rounds:
            continue
        if occ.get("source") != "schedule" or not occ.get("fixed_due_at"):
            continue  # early rounds are extra completions, not axis rounds
        due = _parse_dt(occ["fixed_due_at"], "fixed_due_at")
        deadline = next((event_due for _, event_due in events if event_due > due), None)
        if deadline is None and events and occ.get("schedule_date"):
            # A rule edit may leave an older open round outside the new axis.
            if _parse_date(occ["schedule_date"], "schedule_date") < events[-1][0]:
                deadline = events[-1][1]
        if deadline is None:
            continue
        client.table("planning_occurrence").update({
            "status": "timeout", "closed_at": _iso(deadline), "updated_at": _iso(now),
        }).eq("task_id", task["id"]).eq("round_key", round_key).in_("status", list(OPEN_STATUSES)).execute()
        expired += sum(item["round_key"] == round_key for item in open_rows)
        expired_rounds.add(round_key)
    return expired


def _after_completion_due(client, task: dict[str, Any]) -> datetime | None:
    """Use the latest persisted round, so a failed task-cache write cannot skip a cycle."""
    rows = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]).order("id", desc=True))
    latest = max((row for row in rows if row.get("round_key")), key=lambda row: row["id"], default=None)
    if latest is None:
        return _parse_dt(task["created_at"], "created_at")
    round_rows = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]).eq("round_key", latest["round_key"]))
    if any(row["status"] in OPEN_STATUSES for row in round_rows):
        return None
    if not all(row["status"] in ("completed", "discarded_this") and row.get("handled_at")
               for row in round_rows):
        return None
    handled = max(_parse_dt(row["handled_at"], "handled_at") for row in round_rows)
    interval = task.get("interval_days")
    if not isinstance(interval, int) or not 1 <= interval <= 365:
        raise PlanningError("invalid_task", "处理后刷新间隔必须为 1 至 365 天", 409)
    return handled + timedelta(days=interval)


def _reconcile_task_rounds(
    client, task: dict[str, Any], cycle: PlanningCycle, now: datetime,
    configured: time, transition: BoundaryTransition | None,
    absorbed: frozenset[date], daily_enabled: bool,
) -> tuple[int, int, list[tuple[date, datetime]]]:
    """Single-task lifecycle reconciliation: generate due rounds, expire dead
    fixed rounds, carry open rounds forward.

    全局 generate_due 与单任务入口（提前完成等）共用同一套规则；单任务调用
    保证一次业务操作的生命周期副作用只作用于该任务自身。
    """
    created = 0
    timed_out = 0
    events: list[tuple[date, datetime]] = []
    mode = task.get("refresh_mode")
    if mode is None:
        # A legacy definition has no trustworthy mode. Phase 5 classifies it.
        return 0, 0, events
    if task.get("request_state") == "superseded":
        # I6：被取代的重排请求永不复活——后台不得为其生成任何实例。
        return 0, 0, events
    try:
        validate_task_refresh_mode(task["task_type"], mode)
    except ValueError as exc:
        raise PlanningError("invalid_task", "任务刷新模式与类型不匹配", 409) from exc
    today = cycle.key
    can_generate = task.get("refresh_enabled") is not False and (mode != "daily" or daily_enabled)
    # 窗口批次收口（2026-09-27 Review MEDIUM + 第二轮 HIGH）：time_mode=
    # 'explicit' 属旧模型任务定义。三类关注点显式分离，不整体跳过维护：
    # * generation（新生成）→ 对 legacy explicit **永久禁止**（不繁殖旧模型
    #   新实例，也不静默生成畸形 unassigned 实例）；
    # * expiration（固定型到期清理）→ 与正常路径一致受 refresh_enabled 控制
    #   （需求 24：暂停时到期清理一并冻结）——由下方各分支既有的
    #   can_generate 门控自然继承，legacy 不另开清理路径；
    # * existing occurrence maintenance（存量实例维护）→ 继续保留（顺延、
    #   到期清理按各自规则照常），任务定义等待受控处置（部署前可清理，
    #   不自动转换，见施工计划 §10.2）。
    legacy_definition = task.get("time_mode") == "explicit"
    if mode == "daily":
        if can_generate and not legacy_definition:
            task_created = _parse_dt(task["created_at"], "created_at")
            first_cycle = planning_cycle_at(task_created, configured, transition).key
            if today >= first_cycle and (today == first_cycle or cycle.start >= task_created):
                due = max(task_created, cycle.start) if today == first_cycle else cycle.start
                created += _create_occurrences(client, task, today, now, due_at=due,
                                               display_cycle_date=today)
    elif mode == "none":
        if task["task_type"] == "once":
            # 日期分离裁决（§32.41）：指定日期 once 的 schedule_date 由严格
            # 自然日窗口反推（内部周期身份，可早于 target_date），生成门随
            # 之提前到该内部周期；无窗口 once 沿用 schedule_date = target_date。
            schedule = _once_schedule_date(task, configured, transition)
        else:
            schedule = planning_cycle_at(
                _parse_dt(task["created_at"], "created_at"),
                configured, transition).key
        if can_generate and not legacy_definition and schedule <= today:
            created += _create_occurrences(client, task, schedule, now, display_cycle_date=today)
    elif mode == "after_completion":
        if can_generate and not legacy_definition:
            due = _after_completion_due(client, task)
            if due is not None and due <= now:
                due_cycle = PlanningCycle.at(due, cycle.start.timetz().replace(tzinfo=None)).key
                created += _create_occurrences(client, task, due_cycle, now, due_at=due,
                                               display_cycle_date=today)
    else:
        events = _fixed_rounds(task, cycle, now, configured, transition, absorbed)
        if can_generate:
            if not legacy_definition:
                through = task.get("refresh_generated_through")
                checked = _parse_date(through, "refresh_generated_through") if through else None
                for day, due in events:
                    if checked is not None and day <= checked:
                        continue
                    # A fixed-interval round is born in the cycle that generates
                    # it; calendar rounds keep their own cycle date as identity.
                    schedule = today if mode == "fixed_interval" else day
                    created += _create_occurrences(client, task, schedule, now, due_at=due,
                                                   display_cycle_date=today)
                    client.table("planning_task").update({
                        "refresh_generated_through": day.isoformat(), "updated_at": _iso(now),
                    }).eq("id", task["id"]).execute()
            # 到期清理只受 refresh_enabled 控制（暂停即冻结，需求 24）——
            # legacy 门禁只禁止新生成，不影响存量轮次的到期死亡。
            timed_out += _expire_fixed_rounds(client, task, events, now)
    _carry_open_rounds(client, task, today, now)
    if legacy_definition:
        log.warning("planning 旧显式任务定义停止生成新轮次: task=%s", task["id"])
    return created, timed_out, events


def generate_due(now: datetime | None = None) -> dict[str, Any]:
    """Generate stable rounds from their own refresh model, never legacy cursors."""
    now = now or _now()
    configured, transition, absorbed = _load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    today = cycle.key
    daily_enabled = get_cycle_settings(now)["daily_refresh_enabled"]
    client = _require_client()
    tasks = _rows(client, "planning_task", lambda q: q.eq("is_active", True))
    created = 0
    timed_out = 0
    for task in tasks:
        task_created, task_timed_out, _ = _reconcile_task_rounds(
            client, task, cycle, now, configured, transition, absorbed, daily_enabled,
        )
        created += task_created
        timed_out += task_timed_out
    if created:
        log.info("planning 生成出现实例: count=%s date=%s", created, today.isoformat())
    return {"created": created, "timed_out": timed_out, "date": today.isoformat()}


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
        if not occ.get("schedule_date"):
            continue  # old instances await the controlled Phase 5 boundary
        deadline = _combine(date.fromisoformat(occ["schedule_date"]), time.fromisoformat(end_tod))
        if deadline < now:
            # closed_at 记录业务死亡时刻（限时截止），与固定型槽次死亡一致；
            # updated_at 才是本行最后修改时间。
            client.table("planning_occurrence").update({
                "status": "timeout", "closed_at": _iso(deadline), "updated_at": _iso(now),
            }).eq("id", occ["id"]).execute()
            timed_out += 1
    if timed_out:
        log.info("planning 超时打标: count=%s", timed_out)
    return {"timed_out": timed_out}


# ── 时间重算 ──────────────────────────────────────────────────────

def _freely_schedulable(occ: dict[str, Any], task: dict[str, Any]) -> bool:
    """未固定 / 未开始、系统可管理的 pending 实例可自动排程。

    可排程性由实例自身的所有权元组决定（H4）：显式规则的实例天然带
    rule 固定锚点被排除，任务定义事后把 time_mode 改为 explicit 不会
    冻结已生成的旧自动实例。
    窗口批次（2026-09-27）：带冻结窗口的实例同样参与重算（§14.2），只是
    排程时必须落在其窗口内；零自由度窗口实例在生成期已预锚定 rule 固定
    （is_fixed），经本谓词天然排除、作为固定槽存在，不进入普通排程流程。
    """
    return (
        occ["status"] == "pending"
        and not occ.get("is_fixed")
        and occ.get("schedule_managed") is True
        and occ.get("fixed_source") is None
        and occ.get("estimated_time_source") in ("unassigned", "automatic", "rule")
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


def _occurrence_window(occ: dict[str, Any]) -> ResolvedWindow | None:
    """实例行上冻结的窗口事实（§6.7，批次 3 生成期写入）；两端皆空 = 无约束。

    只读事实，不是可改写的排程输入：排程层不得改写窗口字段（§十三），
    装不下时报告冲突而不是修正窗口。旧实例（无窗口列值）返回 None，
    继续走既有无窗口排程行为（§十四 兼容）。
    """
    start = occ.get("window_start_at")
    end = occ.get("window_end_at")
    if not start and not end:
        return None
    return ResolvedWindow(
        start_at=_parse_dt(start, "window_start_at") if start else None,
        end_at=_parse_dt(end, "window_end_at") if end else None,
    )


def _window_conflict(
    occ: dict[str, Any], window: ResolvedWindow, quantity: str, *, after_avoidance: bool,
) -> dict[str, Any]:
    """排程冲突的派生结果（§19 三要素：哪个待办 / 哪项约束 / 为什么）。

    派生事实：不落库、不新增生命周期状态；由调用方随响应返回或由
    today 看板读取时派生展示。``quantity`` 描述容纳对象（预计耗时 /
    中空完整包络）。
    """
    end_at = _iso(window.end_at) if window.end_at else None
    if after_avoidance:
        reason = f"固定槽避让后无法在可安排时段内容纳{quantity}（最晚完成 {end_at}）"
    else:
        reason = f"可安排时段剩余空间不足以容纳{quantity}（最晚完成 {end_at}）"
    return {
        "occurrence_id": occ["id"],
        "task_id": occ["task_id"],
        "phase": occ.get("phase"),
        "constraint": "window_end",
        "reason": reason,
    }


def _has_schedulable_duration_source(
    occ: dict[str, Any], task: dict[str, Any],
) -> bool:
    """该实例是否存在真实排程耗时来源（四轮修复 MEDIUM）。

    有效来源按既有权威：有效 est 区间 → planned_minutes 快照 → hollow
    合法阶段耗时 → 任务预计耗时 → 旧显式区间。全部缺失 = 异常缺耗时行
    （如历史/越权写入产物）：保持批次 3 旧 scheduler 的 skip 语义，不得
    使用 :func:`_duration_of` 链尾的默认 30 分钟自动排程——那是人工编辑
    与兼容/防御路径的 fallback，不构成给自动排程新增默认耗时的依据。
    正式产品中预计耗时是创建/编辑边界的必填信息（边界已拒绝清空），
    本判定只是排程层的防御性保护，不为旧数据建设兼容体系。
    """
    return bool(
        (occ.get("est_start") and occ.get("est_end"))
        or occ.get("planned_minutes")
        or (occ.get("phase") == "start" and task.get("hollow_start_minutes"))
        or (occ.get("phase") == "end" and task.get("hollow_end_minutes"))
        or task.get("estimated_minutes")
        or (task.get("est_start_tod") and task.get("est_end_tod"))
    )


def _format_duration(duration: timedelta) -> str:
    """冲突文案用精确时长：整分钟显示「N 分钟」，否则带上秒（四轮修复
    HIGH——文案与真实占用一致，不截断秒级事实）。"""
    total_seconds = int(duration.total_seconds())
    minutes, seconds = divmod(total_seconds, 60)
    if minutes and seconds:
        return f"{minutes} 分 {seconds} 秒"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


def _hollow_sibling(
    occ: dict[str, Any], ordered: list[dict[str, Any]], phase: str,
) -> dict[str, Any] | None:
    """同一业务轮次（task_id + round_key + phase_group）的指定阶段行。"""
    return next(
        (o for o in ordered if o["task_id"] == occ["task_id"]
         and o.get("round_key") == occ.get("round_key")
         and o.get("phase_group") == occ.get("phase_group")
         and o.get("phase") == phase),
        None,
    )


def _hollow_movable_envelope_duration(
    start_duration: timedelta, task: dict[str, Any], end_row: dict[str, Any],
) -> timedelta | None:
    """可重排结束阶段的完整包络精确跨度（开始 + 等待 + 结束有效耗时），
    供开始阶段的包络可行性预判（§17.4）。仅对 movable end 使用——冻结
    end 的位置本身才是权威，不得拿耗时推导一个假位置（二轮修复 MEDIUM）。

    结束阶段有效耗时沿用现有字段权威（:func:`_duration_of`：有效 est 区间
    事实优先，planned_minutes 快照其次——N5/M4 同源，不另立优先级）；等待
    读结束阶段行自带 ``planned_wait_minutes``（H3），缺失回退任务定义。
    五轮修复（Review MEDIUM）：读取前先经 :func:`_has_schedulable_duration_source`
    来源门禁——end 无任何真实耗时来源时返回 ``None``（预判跳过，end 之后
    仍按主循环自身规则 skip），不得让 :func:`_duration_of` 链尾默认 30 分钟
    参与包络、制造假 conflict 或假占位。分量经批次 1
    :func:`hollow_envelope_duration` 精确校验（四轮修复 HIGH：
    预判与落位同用真实 timedelta，不截断秒），非法返回 ``None``——预判
    跳过，由结束阶段真实落位检查兜底。
    """
    if not _has_schedulable_duration_source(end_row, task):
        return None
    wait = end_row.get("planned_wait_minutes")
    if isinstance(wait, bool) or not isinstance(wait, int) or wait < 1:
        wait = task.get("hollow_wait_minutes")
    try:
        return hollow_envelope_duration(
            start_duration, wait, _duration_of(end_row, task, "end"))
    except ValueError:
        return None


def _hollow_start_envelope_conflict(
    occ: dict[str, Any], window: ResolvedWindow | None, envelope: timedelta,
    at_start: datetime, start_floor: datetime, *, after_avoidance: bool,
) -> dict[str, Any] | None:
    """movable end：从 ``at_start`` 起完整包络是否仍可整体落入窗口
    （包络为精确 timedelta，四轮修复 HIGH——预判不截断秒）。"""
    if window is None or window.end_at is None:
        return None
    if window_feasible(window, at_start, envelope):
        return None
    return _window_conflict(
        occ, window, f"中空完整包络 {_format_duration(envelope)}",
        after_avoidance=after_avoidance and window_feasible(window, start_floor, envelope),
    )


def _hollow_start_connection_conflict(
    occ: dict[str, Any], duration: timedelta, wait_minutes: int,
    frozen_end_start: datetime, at_start: datetime, *, after_avoidance: bool,
) -> dict[str, Any] | None:
    """frozen end：开始阶段实际结束 + 等待 是否不晚于冻结结束阶段起点
    （§17.2 结束阶段最早开始 + §19 硬约束）。冻结 end 是真实锚点，不得
    假设其随 start 移动；无法连接即冲突，不回溯、不移动冻结 end。
    """
    start_end = at_start + duration
    if start_end + timedelta(minutes=wait_minutes) <= frozen_end_start:
        return None
    prefix = "固定槽避让后" if after_avoidance else ""
    return {
        "occurrence_id": occ["id"],
        "task_id": occ["task_id"],
        "phase": occ.get("phase"),
        "constraint": "hollow_end_anchor",
        "reason": (
            f"{prefix}中空待办无法连接已固定的结束阶段：开始阶段预计 "
            f"{_iso(start_end)} 结束，加等待 {wait_minutes} 分钟晚于结束阶段"
            f"已固定的开始时刻 {_iso(frozen_end_start)}"
        ),
    }


@dataclass
class ScheduleResult:
    """compute_schedule 的结果：合法放置 + 派生冲突清单。

    冲突是排程的派生事实（§19），不是持久化状态：任何冲突存在时调用方
    必须整体放弃本轮持久化（§19.1），``placed`` 仅在零冲突时有意义。
    """

    placed: dict[int, tuple[datetime, datetime]]
    conflicts: list[dict[str, Any]]


def compute_schedule(
    open_rows: list[dict[str, Any]], tasks: dict[int, dict[str, Any]], now: datetime,
) -> ScheduleResult:
    """按「当前时间 → 排列顺序 → 预估耗时」向后排程。

    * 可自动排程实例（仅填耗时、未固定、未开始）按列表顺序依次装入，
      允许前移；固定时间位（显式起止 / 固定标记 / 进行中 / 已延后）保留
      原位，轮到它们时游标越过其时间槽。
    * 任何可排程实例都不与固定槽重叠：排不下的顺延到槽结束之后。
    * 中空待办结束阶段最早开始 = 开始阶段预计结束 + 中间时长，中间的
      空闲时间允许其他待办按列表顺序排入。
    * 窗口批次（2026-09-27，§14.2 / §17.4 / §18）：实例行冻结窗口是排程
      硬约束——起点 ≥ max(游标, 窗口起点)；普通待办 / 中空结束阶段终点
      ≤ 窗口终点；only-earliest 只有下界、无上界，照常允许跨日（§15），
      不凭空补最晚完成。装不下即冲突：不扩大窗口、不缩短耗时、不拆分、
      不滚到下一日、不回溯搜索。冲突时该实例不放置、游标不动，继续
      排其余条目以收集完整冲突清单；任何冲突由调用方整体放弃本轮持久化。
    * 中空开始阶段按同轮结束阶段的可重排性区分两类依赖（2026-09-28 二轮
      修复）：**movable end**（本轮会由 scheduler 重新安排）→ 以完整包络
      （开始 + 等待 + 结束有效耗时，等待/结束读同轮结束阶段行快照与既有
      耗时权威）做可行性预判——避让前 floor 早期判死 + 避让后实际 start
      写入 placed 前复检；**frozen end**（fixed / in_progress / manual /
      deferred 等依法不参与本轮重排、已有合法 est 区间）→ 不假设其随
      start 移动，按真实锚点校验等待连接（开始阶段预计结束 + 等待 ≤
      冻结结束阶段起点），floor 与避让后各查一次。两类任一确定失败：
      立即冲突——不放置开始阶段、不推进游标、不注册任何槽，后续任务
      继续使用未被污染的游标；同轮可重排结束阶段被依赖阻断。这只是
      下界判死，不是全局回溯；结束阶段的真实落位仍按其自身规则与窗口
      终点检查（等待不占槽不变；冻结 end 自身不被移动或重判）。
    * 同轮开始阶段本轮已判死（记录冲突）时，结束阶段不得回退其历史 est
      伪造本轮依赖：不放置、不推进游标。开始阶段本来就不参与本轮重排
      （fixed / in_progress 等合法冻结事实）时，既有锚点照常供结束阶段
      使用（H3 机制不变）。
    * 窗口不是占位槽（§14.3）：只有最终 est 区间与固定槽进入 slots，
      窗口内未被占用的空间仍按列表顺序安排其他待办。
    * 窗口可行性复用 planning_window.window_feasible（与创建校验同一
      领域函数，仅 effective cursor 不同），不另写第二套窗口数学。
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
    conflicts: list[dict[str, Any]] = []
    # 本轮已判死（记录冲突）的可重排 hollow 开始阶段行 id：同轮结束阶段
    # 的依赖失效标记，仅本轮内存内有效，不落库（修复轮 MEDIUM-3）。
    failed_round_starts: set[int] = set()
    cursor = now
    for occ in ordered:
        task = tasks.get(occ["task_id"], {})
        if not _freely_schedulable(occ, task):
            slot = _slot_range(occ)
            if slot and slot[1] > cursor:
                cursor = slot[1]
            continue
        # 三轮修复（2026-09-28 Review HIGH）：可重排实例的实际占用时长统一经
        # _duration_of 有效耗时权威——有效 est 区间事实优先，planned_minutes
        # 快照其次，再走既有 fallback 链（N5/M4 与字段权威矩阵同源，不在
        # 排程内另立 planned-first 规则）。
        # 四轮修复（Review HIGH）：duration 保持精确 timedelta 贯穿窗口
        # 可行性、包络预判与实际落位——有效 est 区间允许秒级事实
        # （PostgreSQL timestamptz 无整分钟约束），不得 floor/ceil/round
        # 截断后校验再按真实时长落位。
        # 四轮修复（Review MEDIUM）：先判定是否存在真实排程耗时来源——
        # 全部缺失的异常行保持旧 scheduler 的 skip 语义，不得使用
        # _duration_of 链尾默认 30 分钟自动排程（那是人工编辑/兼容路径
        # 的 fallback，不是排程耗时来源）。
        if not _has_schedulable_duration_source(occ, task):
            continue
        duration = _duration_of(occ, task)
        if duration <= timedelta(0):
            continue
        start = cursor
        end_row = None
        if occ.get("phase") == "start":
            end_row = _hollow_sibling(occ, ordered, "end")
        if occ.get("phase") == "end":
            start_occ = _hollow_sibling(occ, ordered, "start")
            if start_occ and start_occ["id"] in failed_round_starts:
                # 同轮开始阶段本轮已判死：结束阶段不放置、不推进游标、
                # 不回退历史 est 伪造本轮依赖；根因冲突已在开始阶段记录。
                continue
            # H3：等待快照由 end 阶段行自带，不回读任务当前定义。
            wait = timedelta(
                minutes=occ.get("planned_wait_minutes")
                or task.get("hollow_wait_minutes") or 0)
            if start_occ:
                anchor = placed.get(start_occ["id"]) or _slot_range(start_occ)
                if anchor:
                    start = max(start, anchor[1] + wait)
        window = _occurrence_window(occ)
        if window is not None and window.start_at is not None:
            # 只有最早开始：不得早于窗口起点；没有上界、不补隐式截止（§15）。
            start = max(start, window.start_at)
        start_floor = start  # 避让前的最早可行起点（游标 / 窗口 / 锚点已并入）
        # 中空开始阶段依赖分类（二轮修复）：movable end → 包络可行性；
        # frozen end（不重排且已有合法 est 区间）→ 真实锚点连接校验。
        hollow_envelope = None
        hollow_frozen = None  # (冻结结束阶段起点, 等待分钟)
        if end_row is not None:
            if _freely_schedulable(end_row, task):
                hollow_envelope = _hollow_movable_envelope_duration(
                    duration, task, end_row)
            else:
                frozen = _slot_range(end_row)
                if frozen is not None:
                    hollow_frozen = (
                        frozen[0],
                        end_row.get("planned_wait_minutes")
                        or task.get("hollow_wait_minutes") or 0,
                    )
        if hollow_envelope is not None:
            conflict = _hollow_start_envelope_conflict(
                occ, window, hollow_envelope, start_floor, start_floor,
                after_avoidance=False)
            if conflict:
                conflicts.append(conflict)
                failed_round_starts.add(occ["id"])
                continue
        if hollow_frozen is not None:
            conflict = _hollow_start_connection_conflict(
                occ, duration, hollow_frozen[1], hollow_frozen[0], start_floor,
                after_avoidance=False)
            if conflict:
                conflicts.append(conflict)
                failed_round_starts.add(occ["id"])
                continue
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
        if hollow_envelope is not None:
            # 避让后复检（二轮修复 MEDIUM）：固定槽可能把开始阶段推到
            # 「自身放得下、完整包络必超窗」的位置——写入 placed 前判死。
            conflict = _hollow_start_envelope_conflict(
                occ, window, hollow_envelope, start, start_floor,
                after_avoidance=True)
            if conflict:
                conflicts.append(conflict)
                failed_round_starts.add(occ["id"])
                continue
        if hollow_frozen is not None:
            conflict = _hollow_start_connection_conflict(
                occ, duration, hollow_frozen[1], hollow_frozen[0], start,
                after_avoidance=True)
            if conflict:
                conflicts.append(conflict)
                failed_round_starts.add(occ["id"])
                continue
        if window is not None and window.end_at is not None and not window_feasible(
                window, start, duration):
            # 终点越界（§18.1）：剩余空间不足，或固定槽避让后越过最晚完成。
            # 冲突派生后不放置、游标不动（§七：不得为救后面的任务搬前面的）。
            conflicts.append(_window_conflict(
                occ, window, f"预计耗时 {_format_duration(duration)}",
                after_avoidance=window_feasible(window, start_floor, duration),
            ))
            if occ.get("phase") == "start":
                failed_round_starts.add(occ["id"])
            continue
        placed[occ["id"]] = (start, end)
        cursor = end
    return ScheduleResult(placed=placed, conflicts=conflicts)


def recompute_today(now: datetime | None = None) -> dict[str, Any]:
    """手动 / 自动重算：只更新当天可自动排程实例的预估起止。

    窗口批次（§19.1 原子性）：任一排程冲突 → 本轮整体不持久化，保留最近
    一次成功排程的既有 est 不清空；冲突清单（派生结果，不落库）随响应
    返回，并由 today 看板按同一纯函数读取时派生展示。
    """
    now = now or _now()
    today = _current_cycle(now).key
    client = _require_client()
    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("display_cycle_date", today.isoformat()).in_("status", list(OPEN_STATUSES)),
    )
    if not open_rows:
        return {"updated": 0, "at": _iso(now), "conflicts": []}
    tasks = _task_map(client, {row["task_id"] for row in open_rows})
    result = compute_schedule(open_rows, tasks, now)
    if result.conflicts:
        log.info("planning 重算冲突: count=%s date=%s",
                 len(result.conflicts), today.isoformat())
        return {"updated": 0, "at": _iso(now), "conflicts": result.conflicts}
    placed = result.placed
    updated = 0
    for occ_id, (start, end) in placed.items():
        occ = next(row for row in open_rows if row["id"] == occ_id)
        old_start = _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
        old_end = _parse_dt(occ["est_end"], "est_end") if occ.get("est_end") else None
        if old_start == start and old_end == end and occ.get("estimated_time_source") == "automatic":
            continue
        patch = _estimate_patch(start, end, source="automatic")
        patch["updated_at"] = _iso(now)
        if not occ.get("nominal_start"):
            patch["nominal_start"] = _iso(start)
        client.table("planning_occurrence").update(patch).eq("id", occ_id).execute()
        updated += 1
    log.info("planning 重算完成: updated=%s date=%s", updated, today.isoformat())
    return {"updated": updated, "at": _iso(now), "conflicts": []}


# ── 重算等待标记 ──────────────────────────────────────────────────

def _auto_recompute_config(now: datetime) -> tuple[bool, timedelta]:
    """自动重算开关与等待时长；读取失败时保持既有默认（开启 / 30 分钟）。"""
    enabled_raw = db.load_app_setting(PLANNING_AUTO_RECOMPUTE_ENABLED_KEY)
    enabled = True if not isinstance(enabled_raw, bool) else enabled_raw
    wait_raw = db.load_app_setting(PLANNING_AUTO_RECOMPUTE_WAIT_KEY)
    minutes = (wait_raw if isinstance(wait_raw, int) and 1 <= wait_raw <= 1440
               else int(RECOMPUTE_WAIT.total_seconds() // 60))
    return enabled, timedelta(minutes=minutes)


def request_recompute(reason: str, now: datetime | None = None) -> None:
    now = now or _now()
    enabled, _ = _auto_recompute_config(now)
    if not enabled:
        # 关闭自动重算（需求 16.3）：顺序仍保存，但不进入「等待自动重算」状态。
        return
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
    enabled, wait = _auto_recompute_config(now)
    client = _require_client()
    rows = _rows(client, "planning_recompute_state", lambda q: q.eq("id", 1).limit(1))
    requested_at = rows[0].get("requested_at") if rows else None
    pending = bool(requested_at) and enabled
    wait_minutes = None
    if pending:
        requested = _parse_dt(requested_at, "requested_at")
        wait_minutes = max(0, round((wait - (now - requested)).total_seconds() / 60))
    return {
        "pending": pending,
        # 自动重算开关随状态返回：关闭时前端不得提示「等待自动重算」（需求 16.3）
        "enabled": enabled,
        "requested_at": requested_at,
        "reason": rows[0].get("reason") if rows else None,
        "wait_minutes": wait_minutes,
    }


def trigger_recompute(now: datetime | None = None) -> dict[str, Any]:
    """手动重算：立即执行；仅零冲突（成功）时清空等待标记（§16.2 / §19.1）。

    成功判定只看 conflicts 是否为空：updated=0（无可修改但合法完成）同样
    属于成功；存在冲突则本轮整体未生效，等待标记保留，不新增状态或重试
    机制——后续触发 / 维护循环按既有语义再次执行。
    """
    now = now or _now()
    result = recompute_today(now)
    if not result.get("conflicts"):
        clear_recompute_mark(now)
    return result


# ── 排列保存 ──────────────────────────────────────────────────────

def save_order(ordered_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    today = _current_cycle(now).key
    if not isinstance(ordered_ids, list) or not all(isinstance(v, int) for v in ordered_ids):
        raise PlanningError("invalid_payload", "order must be an array of occurrence ids")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise PlanningError("invalid_payload", "order contains duplicate ids")
    client = _require_client()
    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("display_cycle_date", today.isoformat()).in_("status", list(OPEN_STATUSES)),
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
                 if row["task_id"] == occ["task_id"]
                 and row.get("round_key") == occ.get("round_key")
                 and row.get("phase_group") == occ.get("phase_group")
                 and row.get("phase") == "start"),
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

def _effective_minutes(occ: dict[str, Any], task: dict[str, Any]) -> int | None:
    """实例有效耗时（单一权威语义，排程与展示同源）：显式起止区间事实
    优先（决策 12：起止与耗时并填以起止为准），其次创建时的耗时快照，
    最后回退任务定义（仅兜底旧数据）。"""
    if occ.get("est_start") and occ.get("est_end"):
        return _minutes_between(
            _parse_dt(occ["est_start"], "est_start"), _parse_dt(occ["est_end"], "est_end"))
    if occ.get("planned_minutes"):
        return occ["planned_minutes"]
    return task.get("estimated_minutes")


def _duration_of(occ: dict[str, Any], task: dict[str, Any],
                 phase: str | None = None) -> timedelta:
    """实例执行时长：显式区间事实优先（决策 12 起止优先），其次创建时的
    耗时快照，最后回退任务定义（仅兜底旧数据，不作为新实例权威来源）。"""
    if phase is None:
        phase = occ.get("phase")
    if occ.get("est_start") and occ.get("est_end"):
        return _parse_dt(occ["est_end"], "est_end") - _parse_dt(occ["est_start"], "est_start")
    if occ.get("planned_minutes"):
        return timedelta(minutes=occ["planned_minutes"])
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


def _estimate_patch(
    start: datetime | None, end: datetime | None, *, source: str,
    fixed_source: str | None = None,
) -> dict[str, Any]:
    """One PATCH carries the complete estimated-time ownership state."""
    ownership = EstimatedTimeOwnership(
        source=source,
        fixed_source=fixed_source,
        schedule_managed=source != "manual",
        estimated_start=start,
        estimated_end=end,
    )
    return {
        "est_start": _iso(start) if start else None,
        "est_end": _iso(end) if end else None,
        "estimated_time_source": ownership.source,
        "fixed_source": ownership.fixed_source,
        "schedule_managed": ownership.schedule_managed,
        "is_fixed": ownership.fixed_source is not None,
    }


def _manual_estimate_patch(
    occ: dict[str, Any], task: dict[str, Any], payload: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """Validate and persist a user time edit or explicit release as one row update."""
    start = _parse_dt(payload["est_start"], "est_start") if payload.get("est_start") else None
    if "est_start" not in payload:
        start = _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
    end = _parse_dt(payload["est_end"], "est_end") if payload.get("est_end") else None
    if "est_end" not in payload:
        if "est_start" in payload:
            end = start + _duration_of(occ, task) if start else None
        else:
            end = _parse_dt(occ["est_end"], "est_end") if occ.get("est_end") else None
    if start is None and end is not None:
        raise PlanningError("invalid_payload", "预估结束时间不能脱离开始时间", 400)
    if start is not None and end is not None and end <= start:
        raise PlanningError("invalid_payload", "预估结束时间必须晚于开始时间", 400)
    explicit_release = payload.get("is_fixed") is False
    if explicit_release and any(key in payload for key in ("est_start", "est_end")):
        raise PlanningError("invalid_payload", "修改预估时间时不能同时取消固定", 400)
    if explicit_release and occ.get("fixed_source") == "rule":
        raise PlanningError("rule_fixed", "规则固定时间须通过修改任务定义调整", 409)
    if payload.get("is_fixed") is True and start is None:
        raise PlanningError("invalid_payload", "人工固定必须提供有效预估时间", 400)
    if explicit_release and occ.get("fixed_source") == "manual":
        patch = _estimate_patch(None, None, source="unassigned")
        patch["nominal_start"] = None
    elif start is None:
        patch = _estimate_patch(None, None, source="unassigned")
    elif explicit_release:
        # An already automatic estimate stays automatic; release never invents
        # scheduler provenance for a rule or manual time.
        patch = _estimate_patch(start, end, source=occ.get("estimated_time_source") or "automatic")
    else:
        patch = _estimate_patch(start, end, source="manual", fixed_source="manual")
        patch["nominal_start"] = _iso(start)
    if "est_start" in payload and start is not None:
        original = date.fromisoformat(occ["schedule_date"])
        cycle = _current_cycle(start).key
        display = max(date.fromisoformat(occ["display_cycle_date"]), _current_cycle(now).key, cycle)
        if display.isoformat() != occ["display_cycle_date"]:
            patch["display_cycle_date"] = display.isoformat()
            patch["display_reason"] = "manual_defer" if display > original else "initial"
    patch["updated_at"] = _iso(now)
    return patch


def _sync_hollow_display(client, occ: dict[str, Any], patch: dict[str, Any], now: datetime) -> None:
    if not occ.get("phase_group") or "display_cycle_date" not in patch:
        return
    client.table("planning_occurrence").update({
        "display_cycle_date": patch["display_cycle_date"],
        "display_reason": patch["display_reason"],
        "updated_at": _iso(now),
    }).eq("task_id", occ["task_id"]).eq("round_key", occ["round_key"]).execute()


def _reschedule_occurrence(
    occ: dict[str, Any], task: dict[str, Any], new_start: datetime, now: datetime,
) -> dict[str, Any]:
    """Manual arrangement changes display/time, never the business round."""
    return _manual_estimate_patch(occ, task, {"est_start": _iso(new_start)}, now)


def _shift_sibling_phase(
    client, occ: dict[str, Any], task: dict[str, Any], old_start: datetime | None,
    new_start: datetime, now: datetime,
) -> None:
    """中空待办单阶段被延后 / 手动改时间时，另一阶段按相同时间差平移，
    避免两阶段日期倒挂；结束阶段的精确锚定随后由重算完成。"""
    if not occ.get("phase"):
        return
    if not occ.get("phase_group") or not occ.get("round_key"):
        raise PlanningError("invalid_round", "中空阶段缺少同轮身份", 409)
    if old_start:
        delta = new_start - old_start
    else:
        # 原本无预估时间（尚未重算的仅耗时实例）：按日期差平移，
        # 保留新时刻的时、分，保证两阶段落在同一天。
        anchor = _combine(date.fromisoformat(occ["display_cycle_date"]), new_start.time())
        delta = new_start - anchor
    if delta == timedelta(0):
        return
    sibling_phase = "end" if occ["phase"] == "start" else "start"
    sibling = next(
        (
            row for row in _rows(
                client, "planning_occurrence",
                lambda q: q.eq("task_id", task["id"]).eq("phase", sibling_phase)
                .eq("round_key", occ["round_key"]).eq("phase_group", occ["phase_group"]),
            )
            if row["id"] != occ["id"]
        ),
        None,
    )
    if not sibling:
        raise PlanningError("invalid_round", "中空待办缺少同轮关联阶段", 409)
    if sibling.get("fixed_source") is not None:
        raise PlanningError("fixed_conflict", "关联阶段已有固定时间，不能自动平移", 409)
    old_sibling_start = _parse_dt(sibling["est_start"], "est_start") if sibling.get("est_start") else None
    old_sibling_end = _parse_dt(sibling["est_end"], "est_end") if sibling.get("est_end") else None
    if old_sibling_start is None:
        return
    shifted_start = old_sibling_start + delta
    shifted_end = (old_sibling_end + delta if old_sibling_end
                   else shifted_start + _duration_of(sibling, task, sibling_phase))
    patch = _estimate_patch(shifted_start, shifted_end, source="automatic")
    patch["updated_at"] = _iso(now)
    client.table("planning_occurrence").update(patch).eq("id", sibling["id"]).execute()


def reschedule_timeout_as_new(
    occurrence_id: int, payload: Any, now: datetime | None = None,
    *, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """超时实例的「重新安排」：不复活旧实例。

    旧超时记录原样保留（状态、closed_at、handled_at 均不改写）。第一次
    重排创建一个全新的单次待办承载后续执行；对该超时记录的再次「修改
    时间」（无论新键旧键、实例是否 partial、是否经过后台恢复）统一收敛到
    **同一个当前业务待办**：同一实例保持业务身份，只移动排程时间
    （BF1/BF2 第七轮语义）。请求身份（模型 A）持久化在**任务行**上，早于
    实例成立：同键重放、实例缺失恢复、并发碰撞都收敛到同一份业务结果。

    用户选择的是**绝对日历日期时间**：实例立即可见于包含该时刻的规划周期
    （9/25 04:00 选 9/25 05:00 → 当前 9/24 周期内立即可见），预估时刻以
    人工锚点恒等于所选时刻，不随周期归属漂移。
    """
    now = now or _now()
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")
    if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                        or not 1 <= len(idempotency_key) <= 200):
        raise PlanningError("invalid_payload", "Idempotency-Key 必须为 1 至 200 字符", 400)
    new_start_raw = payload.get("est_start")
    if not new_start_raw:
        raise PlanningError("invalid_payload", "est_start is required", 422)
    new_start = _parse_dt(new_start_raw, "est_start")
    client = _require_client()
    occ = _fetch_occurrence(client, occurrence_id)
    if not occ:
        raise PlanningError("not_found", "planning occurrence not found", 404)
    if occ.get("status") != "timeout":
        raise PlanningError(
            "invalid_transition", "只有已超时的实例可以重新安排为新的单次待办", 422,
        )
    if not occ.get("round_key"):
        raise PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)

    request_key = (
        f"reschedule:{occurrence_id}:{idempotency_key}" if idempotency_key else None
    )
    if request_key:
        # 收敛路径（BF1/BF2）：同键 = 同一次请求的重放 / 恢复；键已被吸收 =
        # 迟到重放（返回现状，不改写时间）；全新键 = 对当前业务待办的再一次
        # 「修改时间」（接管同一实例）。都不落入首次创建。
        converged = _converge_reschedule_request(
            client, request_key, new_start, occ, task, now,
        )
        if converged is not None:
            return converged
    # 仅新请求执行「目标时间不能早于当前时间」校验（H1）。
    if new_start < now - timedelta(minutes=5):
        raise PlanningError("invalid_payload", "新的执行时间不能早于当前时间", 422)

    today = _current_cycle(now).key
    # 中空阶段超时（口径 2026-09-25）：按 user 操作的那个条目确定内容与
    # 有效耗时（M4：显式区间优先，与排程同源）；内容取生成时冻结的展示
    # 快照（BF5），不回读任务当前名称。
    duration = _effective_minutes(occ, task) or 30
    row = validate_task_payload({
        "content": occ.get("display_content") or _display_content(task, occ),
        "task_type": "once",
        "target_date": today.isoformat(),
        "time_mode": "duration",
        "estimated_minutes": duration,
    }, partial=False)
    _prepare_refresh_definition(row, now)
    row["created_at"] = _iso(now)
    row["updated_at"] = _iso(now)
    if request_key:
        # 请求身份 + 请求内容 + 初始 pending 状态随任务行落库（早于实例）：
        # 部分唯一索引收敛并发；绝对执行时刻在实例缺失 / 被后台重排时仍可
        # 恢复。同一超时实例的当前业务待办唯一性由数据库触发器兜底。
        row["request_key"] = request_key
        row["request_est_start"] = _iso(new_start)
        row["request_state"] = "pending"
        row["request_source_occurrence_id"] = occurrence_id
    try:
        response = client.table("planning_task").insert(row).execute()
    except Exception as exc:
        if request_key and _is_reschedule_convergence_conflict(exc):
            # 并发碰撞（同键唯一索引 / 同源业务待办触发器）→ 收敛到已成立的
            # 结果：接管当前业务待办或按请求身份恢复。
            converged = _converge_reschedule_request(
                client, request_key, new_start, occ, task, now,
            )
            if converged is not None:
                return converged
        raise
    created_task = (response.data or [{}])[0]

    # 实例立即生成于当前周期（target = 当前周期 ≤ today 恒成立），随后把
    # 用户所选绝对时刻以人工锚点写入；生成失败报 503 且任务保留——同键
    # 重试经请求身份 + 内容恢复（B4/M1/N1）。并发下另一请求可能已代为
    # 建立实例（返回 0）：不视为失败，重新读取并核对请求结果（N3）。
    try:
        created = _create_occurrences(
            client, created_task, today, now, display_cycle_date=today,
        )
    except PlanningError:
        raise
    except Exception as exc:
        log.warning("planning 超时重排实例生成失败: error=%s", type(exc).__name__)
        raise PlanningError(
            "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
        ) from exc
    if not created:
        converged = _converge_reschedule_request(
            client, request_key, new_start, occ, task, now,
        ) if request_key else None
        if converged is not None:
            return converged
        raise PlanningError(
            "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
        )
    # H4/F1：副作用提交前重读资格——并发新请求可能已接管本请求的业务待办
    #（接管只改 request_key 不改 request_state，资格必须同时确认两者）。
    if request_key and not _reschedule_still_pending(client, created_task):
        return _converge_reschedule_request(
            client, request_key, new_start, occ, task, now,
        )
    try:
        result = _finalize_reschedule_occurrence(
            client, created_task, new_start, now, old_occurrence_id=occurrence_id,
            request_key=request_key,
        )
    except PlanningError:
        raise
    except Exception as exc:
        if request_key and _is_anchor_rejection(exc):
            # 锚定标记守卫拒绝：检查与写入之间身份已被并发请求接管——本
            # 请求立即 stand-down，收敛到当前最新状态（F1 数据库兜底）。
            return _converge_reschedule_request(
                client, request_key, new_start, occ, task, now,
            )
        raise
    if request_key:
        # 请求生命周期：completed 提交用条件更新（H4/F1）——条件同时包含
        # request_state 与 request_key：并发接管后本方不再落地 completed，
        # 也不会把接管方的请求错误标记为自身完成。
        completed_cas = client.table("planning_task").update({
            "request_state": "completed", "updated_at": _iso(now),
        }).eq("id", created_task["id"]).eq("request_state", "pending").eq(
            "request_key", request_key,
        ).execute()
        if not completed_cas.data:
            return _converge_reschedule_request(
                client, request_key, new_start, occ, task, now,
            )
    log.info(
        "planning 超时重排为新建单次待办: old=%s new_task=%s",
        occurrence_id, created_task["id"],
    )
    return result


def _is_reschedule_convergence_conflict(exc: Exception) -> bool:
    """首次创建命中的两类数据库收敛点：请求键唯一索引 / 同源业务待办守卫。"""
    text = str(exc)
    return (
        "planning_task_request_key_uq" in text
        or "another reschedule todo for this timeout is still current" in text
    )


def _is_anchor_rejection(exc: Exception) -> bool:
    """锚定标记守卫拒绝：写入标记时任务行身份已被并发请求接管（F1）。"""
    return "reschedule anchor marker must match the current request identity" in str(exc)


def _rpc(client, fn: str, params: dict[str, Any]) -> Any:
    """调用数据库函数（PostgREST RPC），返回 .data（布尔函数为 True/False）。"""
    return client.rpc(fn, params).execute().data


def _reschedule_still_pending(client, task_row):
    """H4/F1：重读任务行，核对请求仍处于 pending **且身份未被并发接管**。

    资格必须同时确认 ``request_state == 'pending'`` 与
    ``request_key == 本请求自己的键``——adopt 接管只改 request_key 不改
    request_state，仅看状态的守卫对接管事件是盲的。
    """
    current = _fetch_task(client, task_row["id"])
    return bool(current) and current.get("request_state") == "pending" and (
        current.get("request_key") == task_row.get("request_key")
    )


def _reschedule_request_family(
    client, old_occurrence_id: int,
) -> list[dict[str, Any]]:
    """同一旧超时实例名下的全部重排请求任务（含被吸收身份的任务行）。"""
    prefix = f"reschedule:{old_occurrence_id}:"
    return [
        row for row in _rows(client, "planning_task")
        if str(row.get("request_key") or "").startswith(prefix)
        or any(str(key or "").startswith(prefix)
               for key in (row.get("request_absorbed_keys") or []))
    ]


def _converge_reschedule_request(
    client, request_key: str, new_start: datetime,
    old_occ: dict[str, Any], old_task: dict[str, Any], now: datetime,
) -> dict[str, Any] | None:
    """按「请求身份 + 请求内容」收敛重排结果（重放 / 恢复 / 接管共用）。

    请求身份（含被吸收的旧键）持久化在任务行上：
    - 同 key：同一次请求的重放或恢复。恢复时若实例不存在则补生成；若实例
      的预估时刻不是请求时刻（后台维护在恢复前把它排成了自动时间），强制
      重新应用用户人工锚点——请求未按其内容完成前，后台不得永久覆盖。
    - key 已被吸收：迟到重放。该请求已被更新的「修改时间」操作吸收，返回
      当前状态，不再改写时间、不复活（N1/H5 的用户修改不受影响）。
    - 全新 key：对该超时记录当前业务待办的再一次「修改时间」——接管同一
      实例并把时间移动到新值；当前业务待办尚未收尾时绝不新建第二条
      （BF1/BF2）。仅当之前的重排业务待办都已正常关闭时才走首次创建。
    - 同 key + 不同参数：明确拒绝（409），不静默返回旧结果。
    返回 None 表示尚无该超时记录名下的请求（调用方继续首次创建）。
    """
    family = _reschedule_request_family(client, old_occ["id"])
    if not family:
        return None
    prior = next(
        (row for row in family if row.get("request_key") == request_key), None)
    if prior is not None:
        recorded = prior.get("request_est_start")
        if recorded is not None and _parse_dt(recorded, "request_est_start") != new_start:
            raise PlanningError(
                "request_conflict",
                "同一请求键已绑定不同的执行时间；请使用新的请求提交新的时间", 409,
            )
        return _resume_reschedule_request(client, prior, new_start, old_occ, now)
    absorbed_host = next(
        (row for row in family
         if request_key in (row.get("request_absorbed_keys") or [])), None)
    if absorbed_host is not None:
        # 迟到重放：请求已被后续修改时间操作吸收，返回现状即可。
        return _replay_reschedule_result(
            client, absorbed_host, old_occ, now, superseded=True)
    # 全新的一次「修改时间」：接管当前业务待办（同一实例只移动时间）。
    adoptable = _adoptable_reschedule_tasks(client, family)
    if not adoptable:
        return None  # 之前的重排待办均已正常关闭：本次属于新的业务安排
    target = max(adoptable, key=lambda row: row["id"])
    if new_start < now - timedelta(minutes=5):
        raise PlanningError("invalid_payload", "新的执行时间不能早于当前时间", 422)
    return _adopt_reschedule_task(
        client, target, request_key, new_start, old_occ, now)


def _adoptable_reschedule_tasks(
    client, family: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """仍构成「当前业务待办」的请求任务：pending 请求或名下仍有开放实例。

    与数据库守卫（planning_reschedule_todo_guard）同一判定；数据库保证
    同一来源至多一个，此处冗余防御只取最新。
    """
    adoptable = [
        row for row in family
        if row.get("request_state") != "superseded"
        and row.get("request_state") == "pending"
    ]
    with_open = [
        row for row in family
        if row.get("request_state") != "superseded"
        and any(
            occ.get("status") in OPEN_STATUSES
            for occ in _rows(client, "planning_occurrence",
                             lambda q: q.eq("task_id", row["id"]))
        )
    ]
    seen: dict[int, dict[str, Any]] = {row["id"]: row for row in adoptable}
    for row in with_open:
        seen.setdefault(row["id"], row)
    return list(seen.values())


def _replay_reschedule_result(
    client, task_row: dict[str, Any], old_occ: dict[str, Any], now: datetime,
    *, superseded: bool,
) -> dict[str, Any]:
    occ_rows = _rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", task_row["id"]).limit(1),
    )
    result: dict[str, Any] = {
        "task": serialize_task(task_row, now),
        "occurrence": serialize_occurrence(occ_rows[0], task_row, now) if occ_rows else None,
        "rescheduled_from": old_occ["id"],
        "replayed": True,
    }
    if superseded:
        result["superseded"] = True
    return result


def _resume_reschedule_request(
    client, prior: dict[str, Any], new_start: datetime,
    old_occ: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """同 key 重放 / 恢复：补齐未完成的副作用，不覆盖已确立的用户事实。"""
    state = prior.get("request_state") or "pending"
    occ_rows = _rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", prior["id"]).limit(1),
    )
    today = _current_cycle(now).key
    target = _parse_date(prior["target_date"], "target_date")
    if state == "superseded":
        # 已被新操作取代的旧请求——明确返回，不恢复、不创建。
        return _replay_reschedule_result(client, prior, old_occ, now, superseded=True)
    if not occ_rows:
        if target > today:
            return _replay_reschedule_result(client, prior, old_occ, now, superseded=False)
        try:
            _create_occurrences(client, prior, target, now, display_cycle_date=today)
        except Exception as exc:
            log.warning("planning 超时重排恢复生成失败: error=%s", type(exc).__name__)
            raise PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            ) from exc
        occ_rows = _rows(
            client, "planning_occurrence", lambda q: q.eq("task_id", prior["id"]).limit(1),
        )
        if not occ_rows:
            raise PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            )
    if state == "completed":
        # 请求已成功完成。锚定标记（generation_request_key）区分两种情形：
        # 标记=本请求键 → 本请求锚定已达成，重放只返回结果身份，不重写
        # 锚点（实例之后的合法用户修改不被撤销，H5）；标记≠本请求键 →
        # 身份 CAS 已归属本请求但锚定落库前中断（C-1 崩溃窗口）→ 补应用
        # 本请求所选时刻（仅开放实例；已关闭历史不改写）。
        current = occ_rows[0]
        anchored_by_self = current.get("generation_request_key") == prior.get("request_key")
        if not anchored_by_self and current.get("status") in OPEN_STATUSES:
            _finalize_reschedule_occurrence(
                client, prior, new_start, now, old_occurrence_id=old_occ["id"],
                request_key=prior.get("request_key"),
            )
        return _replay_reschedule_result(client, prior, old_occ, now, superseded=False)
    current = occ_rows[0]
    anchored = (
        current.get("est_start") is not None
        and current.get("fixed_source") == "manual"
    )
    if not anchored:
        # pending：实例缺失，或实例被后台维护排成了自动时间（锚定未达成）
        # ——强制恢复用户所选时刻与人工所有权（N1）。
        try:
            _finalize_reschedule_occurrence(
                client, prior, new_start, now, old_occurrence_id=old_occ["id"],
                request_key=prior.get("request_key"),
            )
        except PlanningError:
            raise
        except Exception as exc:
            if _is_anchor_rejection(exc):
                # 锚定标记守卫拒绝：身份已被并发请求接管——本请求立即
                # stand-down，返回当前最新状态（F1 数据库兜底）。
                return _replay_reschedule_result(
                    client, prior, old_occ, now, superseded=True)
            raise
    else:
        # H5：锚定副作用已达成（manual 所有权即用户已确立的时间事实）——
        # 无论时刻是否等于请求时刻（用户随后可能合法改过时间），只补请求
        # 状态，不重写实例。
        pass
    completed_cas = client.table("planning_task").update({
        "request_state": "completed", "updated_at": _iso(now),
    }).eq("id", prior["id"]).eq("request_state", "pending").eq(
        "request_key", prior.get("request_key"),
    ).execute()
    if not completed_cas.data:
        # 身份已被并发请求接管（F1）：返回当前最新状态，不推进他人生命周期。
        refreshed = _fetch_task(client, prior["id"]) or prior
        return _replay_reschedule_result(
            client, refreshed, old_occ, now, superseded=True)
    return _replay_reschedule_result(client, prior, old_occ, now, superseded=False)


def _adopt_reschedule_task(
    client, target: dict[str, Any], request_key: str, new_start: datetime,
    old_occ: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """BF1/BF2：再次「修改时间」接管当前业务待办——同一实例保持身份。

    C-1（并发 CAS）：接管首先以条件更新（CAS）夺取任务行请求身份——
    ``UPDATE ... WHERE request_key = 本次读取到的当前键``。CAS 失败说明
    读取之后已有并发请求改写身份，本请求随后**不得产生任何实例副作用**
    （不锚定、不建实例），只重读最新状态并把本键登记进
    `request_absorbed_keys`（登记本身同样带 CAS 条件），迟到重放即可收敛
    ——绝不基于旧快照覆盖数据库，也绝不覆盖更晚的用户修改。赢得 CAS 后
    才允许移动同一实例的预估时刻（partial 等用户事实原行保留）；锚定落库
    时写入本请求键标记（`generation_request_key`），崩溃窗口由同键重试经
    恢复路径补应用。不关闭、不删除任何实例。
    """
    for _ in range(5):
        fresh = _fetch_task(client, target["id"])
        if not fresh:
            raise PlanningError("not_found", "planning task not found", 404)
        current_key = fresh.get("request_key")
        if current_key == request_key:
            # 并发窗口内本请求身份已被自己此前的尝试确立：按重放收敛。
            return _resume_reschedule_request(
                client, fresh, new_start, old_occ, now)
        if request_key in (fresh.get("request_absorbed_keys") or []):
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=True)
        # CAS（F2：数据库原子函数）：仅当身份仍等于本次读取值时接管；
        # absorbed 合并在同一条 UPDATE 内于行锁下读取最新数组完成——
        # 并发登记不可能被本写入覆盖，本请求的键也不可能丢失。
        won = _rpc(client, "planning_takeover_reschedule_request", {
            "p_task_id": fresh["id"],
            "p_new_key": request_key,
            "p_new_est_start": _iso(new_start),
            "p_expected_key": current_key,
            "p_now": _iso(now),
        })
        if won:
            return _apply_adopted_intent(
                client, fresh, request_key, new_start, old_occ, now)
        # CAS 失败：身份已被并发请求改写。本请求尚未建立任何事实，重读
        # 最新状态并把本键原子登记为 absorbed（stand-down），保证键可追踪。
        fresh = _fetch_task(client, target["id"])
        if not fresh:
            continue
        current_key = fresh.get("request_key")
        if current_key == request_key:
            return _resume_reschedule_request(
                client, fresh, new_start, old_occ, now)
        if request_key in (fresh.get("request_absorbed_keys") or []):
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=True)
        registered = _rpc(client, "planning_absorb_reschedule_request", {
            "p_task_id": fresh["id"],
            "p_request_key": request_key,
            "p_now": _iso(now),
        })
        if registered:
            # 登记成功：本请求被更新的修改吸收，返回当前状态（不改时间、
            # 不建第二实例、不覆盖更晚的用户修改）。
            refreshed = _fetch_task(client, target["id"]) or fresh
            return _replay_reschedule_result(
                client, refreshed, old_occ, now, superseded=True)
        if request_key in (fresh.get("request_absorbed_keys") or []):
            # 并发窗口内另一路径已完成登记：同样按吸收重放收敛。
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=True)
        # 两次 CAS 都输给并发写 → 重读重试（有限次，耗尽报 503）。
    raise PlanningError(
        "database_unavailable", "当前修改请求并发冲突，请稍后重试", 503,
    )


def _apply_adopted_intent(
    client, fresh: dict[str, Any], request_key: str, new_start: datetime,
    old_occ: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """CAS 赢得身份后的副作用阶段：本请求现在是该业务待办的最新意图。

    顺序：先建缺失实例 → 移动同一实例时刻（仅开放实例；已关闭实例不改写
    est，避免向关闭历史补写旧预估）→ 条件写 completed 终态。任何一步中断
    都由同键重试经恢复路径补齐（pending 未锚定 / completed 标记缺失）。
    """
    occ_rows = _rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", fresh["id"]).limit(1),
    )
    today = _current_cycle(now).key
    if not occ_rows:
        task_target = _parse_date(fresh["target_date"], "target_date")
        if task_target > today:
            # 理论不可达（请求建立时目标周期已到期）；身份已接管，返回现状。
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=False)
        try:
            _create_occurrences(client, fresh, task_target, now, display_cycle_date=today)
        except Exception as exc:
            log.warning("planning 超时重排接管生成失败: error=%s", type(exc).__name__)
            raise PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            ) from exc
        occ_rows = _rows(
            client, "planning_occurrence", lambda q: q.eq("task_id", fresh["id"]).limit(1),
        )
        if not occ_rows:
            raise PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            )
    if occ_rows[0].get("status") in OPEN_STATUSES:
        # 同一实例移动到用户所选时刻（partial 等用户事实保留在原行）。
        try:
            _finalize_reschedule_occurrence(
                client, fresh, new_start, now, old_occurrence_id=old_occ["id"],
                request_key=request_key,
            )
        except PlanningError:
            raise
        except Exception as exc:
            if _is_anchor_rejection(exc):
                # 并发再度接管发生在锚定与身份提交的间隙之外（防御）：本方
                # 立即停止，返回当前最新状态，不覆盖更晚的修改。
                refreshed = _fetch_task(client, fresh["id"]) or fresh
                return _replay_reschedule_result(
                    client, refreshed, old_occ, now, superseded=True)
            raise
    else:
        log.info(
            "planning 接管目标实例已关闭，跳过锚定: task=%s", fresh["id"],
        )
    # pending → completed（条件更新，F1：同时以 request_key 为条件；已
    # completed 的接管保持终态不变；并发再度接管后本方不再推进状态）。
    completed_cas = client.table("planning_task").update({
        "request_state": "completed", "updated_at": _iso(now),
    }).eq("id", fresh["id"]).eq("request_state", "pending").eq(
        "request_key", request_key,
    ).execute()
    refreshed = _fetch_task(client, fresh["id"]) or fresh
    occ_rows = _rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", fresh["id"]).limit(1),
    )
    if not completed_cas.data:
        # 状态提交未命中：身份已被并发请求再度接管或已达终态——返回当前
        # 最新状态（最新意图由其持有者负责完成）。
        return _replay_reschedule_result(
            client, refreshed, old_occ, now, superseded=False)
    log.info(
        "planning 超时重排接管当前业务待办: old=%s task=%s",
        old_occ["id"], fresh["id"],
    )
    return {
        "task": serialize_task(refreshed, now),
        "occurrence": serialize_occurrence(occ_rows[0], refreshed, now) if occ_rows else None,
        "rescheduled_from": old_occ["id"],
        "adopted": True,
    }


def _finalize_reschedule_occurrence(
    client, new_task: dict[str, Any], new_start: datetime, now: datetime,
    old_occurrence_id: int | None = None, request_key: str | None = None,
) -> dict[str, Any]:
    """把用户所选绝对时刻写入新实例（人工锚点）并返回结果。

    展示周期遵循既有「人工改时间」语义：实例出现在包含该执行时刻的规划
    周期（更晚则 manual_defer 顺延），绝不早于其原始周期。

    锚定落库时把请求键标记到实例 `generation_request_key`：同键重放据此
    区分「本请求锚定已达成」（H5：不重写，用户后续修改不被撤销）与「身份
    已接管但锚定尚未落库」（CAS 后锚定前的崩溃窗口：补应用本请求锚点）。
    """
    occ_rows = _rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", new_task["id"]).limit(1),
    )
    if not occ_rows:
        raise PlanningError(
            "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
        )
    occ = occ_rows[0]
    anchored = (
        occ.get("est_start") is not None
        and _parse_dt(occ["est_start"], "est_start") == new_start
        and occ.get("fixed_source") == "manual"
        and (request_key is None or occ.get("generation_request_key") == request_key)
    )
    if not anchored:
        # 首次锚定，或恢复被后台维护改写过的时刻，或身份已接管但锚定尚未
        # 落库：强制回到用户所选绝对时刻与 manual 所有权（请求内容未达成
        # 前，后台不得永久覆盖）。
        patch = _manual_estimate_patch(occ, new_task, {"est_start": _iso(new_start)}, now)
        patch["updated_at"] = _iso(now)
        if request_key:
            patch["generation_request_key"] = request_key
        client.table("planning_occurrence").update(patch).eq("id", occ["id"]).execute()
        occ = {**occ, **patch}
    return {
        "task": serialize_task(new_task, now),
        "occurrence": serialize_occurrence(occ, new_task, now),
        "rescheduled_from": old_occurrence_id,
    }


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
    if not occ.get("round_key"):
        raise PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)
    current = occ["status"]

    new_start_raw = payload.get("est_start")
    new_start = _parse_dt(new_start_raw, "est_start") if new_start_raw else None

    # 状态迁移规则（过去不重写，已关闭实例不复活）：
    # * 已关闭或已超时的历史记录一律不得回到开放生命周期（pending /
    #   in_progress / deferred / partial）；限时超时的出口是「重新安排为新
    #   的单次待办」（reschedule_timeout_as_new），不是改写旧实例状态。
    # * 历史修正仅限关闭态之间的状态标签更正与实际时间 / 说明补改；
    #   handled_at 一经写入不再改变，保持处理后刷新基准的历史事实。
    # * 部分完成属于开放生命周期：记录 partial_at 与说明，不改 handled_at、
    #   不关闭实例；只有「已全部完成」才以最终时间关闭并起算处理后刷新。
    # * 超时后不能再标记完成 / 部分完成，只能废弃或重新安排为新待办。
    if target in OPEN_STATUSES and (current in CLOSED_STATUSES or current == "timeout"):
        raise PlanningError(
            "invalid_transition", "已关闭的历史记录不能恢复为开放待办", 422,
        )
    if target in ("completed", "partial") and current == "timeout":
        raise PlanningError(
            "invalid_transition", "timed-out occurrences can only be rescheduled or discarded", 422,
        )
    if target == "discarded_this":
        if current == "timeout":
            pass  # 超时后此次不执行视为一种废弃处理路径
        elif current not in OPEN_STATUSES and current not in CLOSED_STATUSES:
            raise PlanningError("invalid_transition", f"cannot discard_this from {current}", 422)
    if target == "deferred":
        if current not in ("pending", "in_progress", "partial"):
            raise PlanningError("invalid_transition", f"cannot defer from {current}", 422)
        if not new_start:
            raise PlanningError("invalid_payload", "deferring requires est_start", 422)
    if target == "in_progress" and current not in ("pending", "deferred", "partial"):
        raise PlanningError("invalid_transition", f"cannot start from {current}", 422)
    if target == "partial" and current not in ("pending", "in_progress", "partial", "deferred"):
        raise PlanningError(
            "invalid_transition", f"cannot record partial completion from {current}", 422,
        )
    if target == "completed" and current not in (
        "pending", "in_progress", "partial", "deferred", "completed",
        "discarded", "discarded_this",
    ):
        # discarded → completed 属于关闭态之间的历史标签更正（B5 双向）；
        # 无处理事实的历史以更正时刻记录 handled_at（见 newly_handled）。
        raise PlanningError("invalid_transition", f"cannot complete from {current}", 422)

    row: dict[str, Any] = {"status": target, "updated_at": _iso(now)}

    if target == "partial":
        note = _clean_text(payload.get("partial_note"), "partial_note", required=True, maximum=MAX_NOTE_LENGTH)
        row["partial_note"] = note
        row["partial_at"] = _iso(now)
        # Partial work is a fact inside an open lifecycle, never a baseline.
        row["handled_at"] = None
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
        _sync_hollow_display(client, occ, row, now)

    if target == "in_progress" and not occ.get("actual_start"):
        row["actual_start"] = _iso(now)

    closing = target in CLOSED_STATUSES
    # handled_at 是历史处理事实：完整处理（含把无处理事实的关闭历史更正为
    # 已完成 / 此次不执行）时写入；已有时不得改写（触发器同此约束）。
    newly_handled = target in ("completed", "discarded_this") and (
        current in OPEN_STATUSES or current == "timeout"
        or (current in CLOSED_STATUSES and not occ.get("handled_at"))
    )
    if closing:
        if current in OPEN_STATUSES:
            # closed_at = 第一次真正进入关闭态的事实时间；关闭态之间的标签
            # 更正保留原值（更正时间由 updated_at 表达，B7）。
            row["closed_at"] = _iso(now)
        if newly_handled:
            row["handled_at"] = _iso(now)
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
    # B6：「废弃整个任务」是 user 在开放实例上执行的业务命令；把已关闭历史
    # 的状态标签更正为 discarded 只修改这一条历史记录，不得停用任务定义、
    # 关闭其他开放轮次或停止未来刷新。
    discarding_whole_task = (
        target == "discarded"
        and task["task_type"] in REPEATING_TASK_TYPES
        and task.get("is_active")
        and current not in CLOSED_STATUSES
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

    # Phase 1B: only an explicit full handling of the currently open round
    # starts the next after-completion interval. Partial completion stays open
    # and label corrections that already carry a handled fact do not move it.
    if (newly_handled
            and task["task_type"] == "interval"
            and task.get("refresh_mode") == "after_completion"
            and task.get("is_active") and not discarding_whole_task):
        round_rows = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]).eq("round_key", occ["round_key"]))
        if all(item["status"] in ("completed", "discarded_this") and item.get("handled_at")
               for item in round_rows):
            handled = max(_parse_dt(item["handled_at"], "handled_at") for item in round_rows)
            client.table("planning_task").update({
                "last_handled_at": _iso(handled),
                "refresh_next_due_at": _iso(handled + timedelta(days=task["interval_days"])),
                "updated_at": _iso(now),
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
    if "is_fixed" in payload:
        _clean_bool(payload["is_fixed"], "is_fixed")

    client = _require_client()
    occ = _fetch_occurrence(client, occurrence_id)
    if not occ:
        raise PlanningError("not_found", "planning occurrence not found", 404)
    if not occ.get("round_key"):
        raise PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)

    row: dict[str, Any] = {"updated_at": _iso(now)}
    if any(field in payload for field in ("est_start", "est_end", "is_fixed")):
        row.update(_manual_estimate_patch(occ, task, payload, now))
    if "est_start" in payload and payload["est_start"]:
        _shift_sibling_phase(
            client, occ, task,
            _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
            _parse_dt(row["est_start"], "est_start"), now,
        )
        _sync_hollow_display(client, occ, row, now)
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
    """拆分待办：结束当前这一轮，并把剩余工作拆成 1～10 个新的单次待办。

    拆分是可选辅助功能（partial → 已全部完成才是主流程），业务语义属于
    「本轮已经处理结束」：当前轮以「此次不执行」同级语义合法关闭
    （discarded_this + handled_at 拆分处理时间），处理后刷新型从该处理
    时间推进下一轮，固定刷新型时间轴不变，原任务定义继续正常存在。已有
    partial 说明 / 时间与实际执行事实原样保留（不为记录"已拆分为 N 个
    待办"覆盖用户事实）。只允许开放实例拆分；收口是带开放状态条件的原子
    更新（同轮中空两阶段一起关闭）——已关闭实例（含被并发拆分收口的）
    拒绝再次拆分，重复请求不会产生第二组拆分任务。
    """
    now = now or _now()
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")
    parts = payload.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 10:
        raise PlanningError("invalid_payload", "parts must be an array of 1-10 items")
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
    if not occ.get("round_key"):
        raise PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    if occ.get("status") not in OPEN_STATUSES:
        raise PlanningError(
            "invalid_transition", "只有开放中的待办可以拆分；该待办已关闭或已超时", 422,
        )
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)

    # 先收口当前轮（带开放状态条件的原子更新；同轮中空两阶段一起关闭，
    # 不留半关闭轮次）。并发重复拆分的后到者在同一语句上 0 行命中，不会
    # 创建第二组拆分任务（B2 业务兜底，前端防双击之外的第二层保护）。
    closed = client.table("planning_occurrence").update({
        "status": "discarded_this",
        "closed_at": _iso(now),
        "handled_at": _iso(now),
        "updated_at": _iso(now),
    }).eq("task_id", occ["task_id"]).eq("round_key", occ["round_key"]).in_(
        "status", list(OPEN_STATUSES),
    ).execute()
    if not closed.data:
        raise PlanningError(
            "invalid_transition", "该待办已被并发操作关闭，不能再次拆分", 409,
        )

    created_ids = []
    for part in normalized:
        response = client.table("planning_task").insert({
            "content": part["content"],
            "task_type": "once",
            "time_mode": "duration",
            "estimated_minutes": part["estimated_minutes"],
            "target_date": _current_cycle(now).key.isoformat(),
            "refresh_mode": "none",
            "is_active": True,
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }).execute()
        created = (response.data or [{}])[0]
        created_ids.append(created.get("id"))

    # 拆分处理时间即本轮处理事实：处理后刷新型据此推进下一轮（固定刷新型
    # 时间轴不动）；task 行基准即便写入失败，_after_completion_due 也能从
    # 轮次行的 handled_at 自愈推进。
    if (task["task_type"] == "interval"
            and task.get("refresh_mode") == "after_completion"
            and task.get("is_active")):
        round_rows = _rows(
            client, "planning_occurrence",
            lambda q: q.eq("task_id", task["id"]).eq("round_key", occ["round_key"]),
        )
        interval = task.get("interval_days")
        if (round_rows and all(row.get("handled_at") for row in round_rows)
                and isinstance(interval, int) and 1 <= interval <= 365):
            handled = max(_parse_dt(row["handled_at"], "handled_at") for row in round_rows)
            client.table("planning_task").update({
                "last_handled_at": _iso(handled),
                "refresh_next_due_at": _iso(handled + timedelta(days=interval)),
                "updated_at": _iso(now),
            }).eq("id", task["id"]).execute()

    request_recompute("split", now)
    # 即时生成：拆分出的当日单次待办立刻出现在列表里。
    _generate_due_quietly(client, now)
    return {"created_task_ids": created_ids, "split_from": occurrence_id}


def _after_completion_duplicate(
    client, task: dict[str, Any], now: datetime,
) -> dict[str, Any] | None:
    """BF3：after_completion 提前完成的 30 分钟防重复窗口判重。

    窗口起点 = 最近一次**已经成功成立**的完成事实（服务端持久化）：
    ``max(last_handled_at, 最新 after_completion early 行的 handled_at)``——
    前者覆盖「正常/提前完成按钮完成当前开放轮次」的事实，后者覆盖基准
    字段写失败后仅 early 行落库的漂移场景。窗口内再次提前完成视为前一次
    操作的重复请求：返回此前成功的完成结果并补齐派生基准，不新增事实、
    不再次推进，并附带重复窗口元数据（duplicate_within_window /
    previous_handled_at / elapsed_seconds / retry_after_seconds）供前端
    明确反馈。窗口外（> 30 分钟）返回 None，调用方按新的真实操作处理。
    普通完成、编辑时间、此次不执行、partial 等其他操作不经过本判重。
    """
    rows = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]))
    early_handled = [
        _parse_dt(row["handled_at"], "handled_at")
        for row in rows
        if row.get("source") == "early"
        and row.get("early_period_date") is None
        and row.get("handled_at")
    ]
    candidates = list(early_handled)
    if task.get("last_handled_at"):
        candidates.append(_parse_dt(task["last_handled_at"], "last_handled_at"))
    if not candidates:
        return None  # 尚无任何成功成立的完成事实：不存在窗口
    fact_time = max(candidates)
    if now - fact_time > EARLY_DEDUPE_WINDOW:
        return None  # 窗口外：新的真实提前完成
    fact = max(
        (row for row in rows
         if row.get("handled_at")
         and _parse_dt(row["handled_at"], "handled_at") == fact_time),
        key=lambda row: row["id"],
        default=None,
    )
    if fact is None:
        # 事实时刻找不到对应行（异常漂移）：保守放行，由数据库触发器兜底。
        return None
    _repair_after_completion_baseline(client, task, fact, now)
    result = serialize_occurrence(fact, task, now)
    elapsed = max(0.0, (now - fact_time).total_seconds())
    result.update({
        "duplicate_within_window": True,
        "previous_handled_at": _iso(fact_time),
        "elapsed_seconds": int(elapsed),
        "retry_after_seconds": max(0, int(round(EARLY_DEDUPE_WINDOW.total_seconds() - elapsed))),
    })
    return result


def _repair_after_completion_baseline(
    client, task: dict[str, Any], early_occ: dict[str, Any], now: datetime,
) -> None:
    """M6：after_completion 提前完成的幂等重试，补齐尚未写入的 task 行
    基准字段（last_handled_at / refresh_next_due_at）。按既有 handled 时刻
    计算，不使用重试时刻——下一轮 due 语义不变；fixed 刷新不受影响。"""
    if (task.get("refresh_mode") != "after_completion"
            or not task.get("is_active")
            or early_occ.get("handled_at") is None):
        return
    round_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("task_id", task["id"]).eq("round_key", early_occ["round_key"]),
    )
    if not all(row.get("handled_at") for row in round_rows):
        return
    handled = max(_parse_dt(row["handled_at"], "handled_at") for row in round_rows)
    expected_due = handled + timedelta(days=task.get("interval_days") or 0)
    if (task.get("last_handled_at") == _iso(handled)
            and task.get("refresh_next_due_at") == _iso(expected_due)):
        return
    client.table("planning_task").update({
        "last_handled_at": _iso(handled),
        "refresh_next_due_at": _iso(expected_due),
        "updated_at": _iso(now),
    }).eq("id", task["id"]).execute()


def complete_task_early(
    task_id: int, now: datetime | None = None, *, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """间歇待办提前完成。

    固定刷新型（fixed_interval / fixed_weekday / fixed_monthday）：只新增一条
    额外完成记录，固定时间轴完全不动；存在开放轮次时视为完成该轮本身。
    处理后刷新型（after_completion）：新增完成记录，并以此次完成时间作为
    下一轮刷新基准重新计算。
    """
    now = now or _now()
    if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                        or not 1 <= len(idempotency_key) <= 200):
        raise PlanningError("invalid_payload", "Idempotency-Key 必须为 1 至 200 字符", 400)
    client = _require_client()
    task = _fetch_task(client, task_id)
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)
    if task["task_type"] not in ("interval", "weekly", "monthly"):
        raise PlanningError(
            "invalid_transition", "only refreshable tasks support early completion", 422,
        )
    if not task.get("is_active"):
        raise PlanningError("invalid_transition", "task is discarded", 422)
    if task.get("refresh_mode") not in EARLY_CAPABLE_MODES:
        raise PlanningError("unclassified_task", "旧间歇任务须在受控升级中分类", 409)
    interval = task.get("interval_days")
    if task["refresh_mode"] == "after_completion" and (
        not isinstance(interval, int) or not 1 <= interval <= 365
    ):
        raise PlanningError("invalid_payload", "interval_days is missing", 422)
    if task.get("is_hollow"):
        raise PlanningError("invalid_round", "中空待办提前处理需由完整轮次承载", 409)
    if idempotency_key:
        prior = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).eq("generation_request_key", idempotency_key).limit(1))
        if prior and prior[0]["status"] not in OPEN_STATUSES:
            # M6：幂等重试不仅返回记录——该操作尚未完成的派生状态必须补齐
            # （early 插入成功但 task 基准字段更新失败的场景）；不产生第二条
            # early，下一轮 due 语义不变（按既有 handled 时刻，不用重试时刻）。
            _repair_after_completion_baseline(client, task, prior[0], now)
            return serialize_occurrence(prior[0], task, now)

    # 只对本任务做生命周期校正（生成 / 到期清理 / 顺延），不触碰其他任务：
    # 本请求后续写入失败时，无关任务不得已被改变（B9）。
    configured, transition, absorbed = _load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    daily_enabled = get_cycle_settings(now)["daily_refresh_enabled"]
    _, _, events = _reconcile_task_rounds(
        client, task, cycle, now, configured, transition, absorbed, daily_enabled,
    )

    open_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("task_id", task_id).in_("status", list(OPEN_STATUSES)),
    )
    if open_rows:
        occ = prior[0] if idempotency_key and prior else sorted(open_rows, key=lambda r: r["id"])[0]
        if idempotency_key and occ.get("generation_request_key") not in (None, idempotency_key):
            raise PlanningError("round_busy", "当前轮次正在由其他请求处理", 409)
        if idempotency_key and occ.get("generation_request_key") is None:
            client.table("planning_occurrence").update({
                "generation_request_key": idempotency_key,
            }).eq("id", occ["id"]).execute()
        result = set_occurrence_status(
            occ["id"], {"status": "completed", "actual_end": _iso(now)}, now,
        )
    else:
        if task.get("time_mode") == "explicit":
            # 窗口批次收口（2026-09-27 Review MEDIUM）：额外完成记录继承
            # 生成时快照，旧显式定义不得再产生带 explicit 快照的新行；存量
            # 开放轮次仍可正常完成（走上方分支，不产生新行）。
            raise PlanningError(
                "legacy_task_definition",
                "旧显式起止任务定义已停止产生新的完成记录，请在受控处置中重建任务", 409,
            )
        if task["refresh_mode"] == "after_completion":
            # BF3（第七轮）30 分钟防重复窗口：最近一次已经成功成立的完成
            # 事实（服务端持久化的 last_handled_at / 最新 early 行的 handled
            # 时刻，二者取较晚）之后 30 分钟内的再次提前完成，统一视为前一
            # 次操作的重复请求——不新增完成事实、不再推进基准，收敛返回
            # 此前成功的完成结果。窗口起点必须来自已成功持久化的事实：第一
            # 次实际失败（事实未落库）时不存在窗口，重试不被吞掉（F）；
            # 超过 30 分钟后是新的真实操作，重新计算下一轮（E）。
            dup = _after_completion_duplicate(client, task, now)
            if dup is not None:
                return dup
        early_period_date = None
        if task["refresh_mode"] in ("daily", "fixed_interval", "fixed_weekday", "fixed_monthday"):
            # 同一个固定刷新周期只允许一条额外完成记录：重复点击 / 重试返回
            # 已有结果，不重复新增；到下一固定刷新周期后可再次提前完成。
            # 判重统一依据持久化的 early_period_date（N4）——closed_at 不参与
            # 周期归属推导，修改规则不会让旧周期记录挡住新周期；先查仅为快
            # 速路径，并发窗口由 (task_id, early_period_date) 部分唯一索引兜底。
            period_start = max(
                (due for _, due in events if due <= now),
                default=_parse_dt(task["created_at"], "created_at"),
            )
            early_period_date = period_start.date()
            prior_extra = [
                row for row in _rows(
                    client, "planning_occurrence", lambda q: q.eq("task_id", task_id))
                if row.get("source") == "early"
                and row.get("early_period_date") == early_period_date.isoformat()
            ]
            if prior_extra:
                return serialize_occurrence(prior_extra[-1], task, now)
        today = _current_cycle(now).key
        request_key = idempotency_key or str(uuid.uuid4())
        round_key = timed_round_key("early", today, request_key)
        identity = OccurrenceIdentity(task_id, round_key, today, today)
        row = {
            "task_id": task_id,
            "generation_request_key": request_key,
            "for_date": today.isoformat(),
            "round_key": identity.round_key,
            "schedule_date": identity.schedule_date.isoformat(),
            "display_cycle_date": identity.display_cycle_date.isoformat(),
            "display_reason": identity.display_reason,
            "phase_group": None,
            "phase": None,
            "actual_start": _iso(now),
            "actual_end": _iso(now),
            "actual_minutes": 0,
            "status": "completed",
            "sort_order": task["id"] * 10,
            "is_fixed": False,
            "estimated_time_source": "unassigned",
            "fixed_source": None,
            "schedule_managed": True,
            # 窗口批次：deadline 事实生成期停止写入，与 _generation_snapshots 一致
            "is_limited": False,
            "closed_at": _iso(now),
            "handled_at": _iso(now),
            "source": "early",
            "early_period_date": early_period_date.isoformat() if early_period_date else None,
            **_generation_snapshots(task, identity.schedule_date, None),
            "created_at": _iso(now),
            "updated_at": _iso(now),
        }
        try:
            response = client.table("planning_occurrence").insert(row).execute()
        except Exception as exc:
            if "planning_occurrence_early_period_uq" in str(exc):
                # 并发同周期：数据库唯一约束兜底，收敛到已存在的那条。
                prior = next(
                    (row for row in _rows(
                        client, "planning_occurrence", lambda q: q.eq("task_id", task_id))
                     if row.get("source") == "early"
                     and row.get("early_period_date")
                     and row["early_period_date"]
                     == (early_period_date.isoformat() if early_period_date else None)),
                    None,
                )
                if not prior:
                    raise
                return serialize_occurrence(prior, task, now)
            if "after_completion early completions within the same 30-minute window" in str(exc):
                # BF3/G：真实 PostgreSQL 触发器拒绝同一 30 分钟窗口内的第二条
                # after_completion 提前完成（不同 key 并发）→ 收敛到已成立的
                # 成功事实，并补齐其派生基准字段；基准不再二次推进。附带重复
                # 窗口元数据供前端反馈。
                prior = max(
                    (row for row in _rows(
                        client, "planning_occurrence", lambda q: q.eq("task_id", task_id))
                     if row.get("source") == "early"
                     and row.get("early_period_date") is None
                     and row.get("handled_at")),
                    key=lambda row: row["handled_at"],
                    default=None,
                )
                if not prior:
                    raise
                _repair_after_completion_baseline(client, task, prior, now)
                result = serialize_occurrence(prior, task, now)
                fact_time = _parse_dt(prior["handled_at"], "handled_at")
                elapsed = max(0.0, (now - fact_time).total_seconds())
                result.update({
                    "duplicate_within_window": True,
                    "previous_handled_at": _iso(fact_time),
                    "elapsed_seconds": int(elapsed),
                    "retry_after_seconds": max(
                        0, int(round(EARLY_DEDUPE_WINDOW.total_seconds() - elapsed))),
                })
                return result
            if not any(name in str(exc) for name in (
                "planning_occurrence_generation_request_uq", "planning_occurrence_round_phase_uq",
            )):
                raise
            prior = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).eq("generation_request_key", request_key).limit(1))
            if not prior:
                raise
            return serialize_occurrence(prior[0], task, now)
        created = (response.data or [{}])[0]
        result = serialize_occurrence(created, task, now)
    if task["refresh_mode"] == "after_completion":
        client.table("planning_task").update({
            "last_handled_at": _iso(now),
            "refresh_next_due_at": _iso(now + timedelta(days=interval)),
            "updated_at": _iso(now),
        }).eq("id", task_id).execute()
    return result


# ── 当天 / 全部列表 ───────────────────────────────────────────────

def today_board(now: datetime | None = None) -> dict[str, Any]:
    """当前待办三分区（进度中 / 待处理 / 已完成）+ 重算等待状态。"""
    now = now or _now()
    cycle = _current_cycle(now)
    today = cycle.key
    client = _require_client()
    today_rows = _rows(
        client, "planning_occurrence",
        lambda q: q.eq("display_cycle_date", today.isoformat()),
    )
    timeout_rows = [row for row in _rows(
        client, "planning_occurrence", lambda q: q.eq("status", "timeout"),
    ) if row.get("round_key")]
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
    attention.sort(key=lambda item: (item["schedule_date"], item["id"]))

    # 窗口批次（§19）：排程冲突是读取时只读派生结果（复用 compute_schedule
    # 同一纯函数，不落库、无持久化冲突缓存），仅对当前周期开放实例计算，
    # 与 recompute_today 的排程语义完全同源。
    open_rows = [row for row in today_rows if row["status"] in OPEN_STATUSES]
    conflicts = (
        compute_schedule(open_rows, tasks, now).conflicts if open_rows else []
    )

    return {
        "date": today.isoformat(),
        "cycle_start": cycle.start.isoformat(),
        "cycle_end": cycle.end.isoformat(),
        "now": _iso(now),
        "recompute": get_recompute_state(now),
        "conflicts": conflicts,
        "progress": progress,
        "attention": attention,
        "done": done,
    }


def list_occurrences(
    *,
    task_type: str | None = None,
    status: str | None = None,
    for_date: str | None = None,
    schedule_date: str | None = None,
    display_cycle_date: str | None = None,
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
    if for_date and schedule_date and for_date != schedule_date:
        raise PlanningError("invalid_payload", "for_date 与 schedule_date 筛选条件不一致", 400)
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
        if for_date or schedule_date:
            # for_date is an old parameter name for the immutable schedule date.
            q = q.eq("schedule_date", _parse_date(schedule_date or for_date, "schedule_date").isoformat())
        if display_cycle_date:
            q = q.eq("display_cycle_date", _parse_date(display_cycle_date, "display_cycle_date").isoformat())
        if date_from:
            q = q.gte("schedule_date", _parse_date(date_from, "date_from").isoformat())
        if date_to:
            q = q.lte("schedule_date", _parse_date(date_to, "date_to").isoformat())
        if status:
            q = q.eq("status", status)
        return q.order("schedule_date", desc=True).order("id", desc=True).limit(limit)

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
    """Legacy cleanup only; new business-round closure history is permanent."""
    now = now or _now()
    client = _require_client()
    threshold = _iso(now - DISCARD_RETENTION)
    rows = _rows(
        client, "planning_occurrence",
        lambda q: q.in_("status", ["discarded", "discarded_this"]).lt("closed_at", threshold),
    )
    rows = [row for row in rows if not row.get("round_key")]
    deleted = 0
    by_round: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in rows:
        by_round.setdefault((row["task_id"], row.get("round_key") or f"legacy:{row['id']}"), []).append(row)
    for round_rows in by_round.values():
        first = round_rows[0]
        if first.get("phase_group"):
            all_phases = _rows(client, "planning_occurrence", lambda q: q.eq("task_id", first["task_id"]).eq("round_key", first["round_key"]))
            if len(all_phases) != 2 or len(round_rows) != 2:
                continue
            client.table("planning_occurrence").delete().eq("task_id", first["task_id"]).eq("round_key", first["round_key"]).execute()
            deleted += 2
        else:
            client.table("planning_occurrence").delete().eq("id", first["id"]).execute()
            deleted += 1
    return {"deleted": deleted}


# ── 后台维护循环（约 1 分钟粒度） ─────────────────────────────────

def run_maintenance(now: datetime | None = None) -> dict[str, Any]:
    """At the configured cycle boundary, maintain rounds and legacy services.

    单实例锁防止并发重入；单步失败只记日志，不影响其余步骤。
    """
    if not _maintenance_lock.acquire(blocking=False):
        return {"status": "skipped_busy"}
    try:
        now = now or _now()
        results: dict[str, Any] = {"status": "ok", "at": _iso(now)}
        try:
            results["generation"] = generate_due(now)
            # 新生成的当天实例需要立刻拿到预估起止；
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
            enabled, wait = _auto_recompute_config(now)
            state = get_recompute_state(now)
            requested_at = state.get("requested_at")
            if enabled and requested_at and (now - _parse_dt(requested_at, "requested_at")) >= wait:
                auto = recompute_today(now)
                results["auto_recompute"] = auto
                # 窗口批次修复轮（2026-09-28 Review MEDIUM-1）：仅零冲突（成功）
                # 清空等待标记；冲突本轮整体未生效，标记保留，等待条件改变后
                # 由后续维护循环按既有语义再次执行（不新增状态 / 重试机制）。
                if not auto.get("conflicts"):
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
