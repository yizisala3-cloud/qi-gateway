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
    window_at_crosses_boundary,
    window_crosses_boundary,
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
# 窗口超时扫描的显式分页大小（批次 5 Review MEDIUM）：不依赖服务端默认行数
# 上限；固定按 id 排序持续取「当前第一页到期开放行」直到取空，已处理行离开
# 开放结果集后自然前移，不用 offset 分页（避免处理中途结果集缩小导致跳行）。
SWEEP_PAGE_SIZE = 1000
# 携带「到达下一规则点死亡」生命周期的刷新模式（每日轮无固定到期死亡，
# 不参与双死亡边界裁决；批次 5 二轮 Review HIGH）。
_FIXED_EXPIRING_MODES = ("fixed_interval", "fixed_weekday", "fixed_monthday")
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
# 真实 PostgREST 把 time 列序列化为 ``HH:MM:SS``（批次 8 HIGH #1）：编辑
# 表单把未触碰的窗口端原样回传，业务契约是分钟精度（秒恒为 0），语义同值
# 必须接受——与 _canonical_template_value 的 fromisoformat 规范化同口径。
_TIME_WITH_SECONDS_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d):([0-5]\d)$")
_SHORTHAND_RE = re.compile(r"^(?:(\d+)\s*h)?(?:(\d+)\s*m)?(?:(\d+)\s*s)?$")

# 可重入锁（最终修复问题 3）：既做维护循环互斥，也做 once 身份编辑与
# 全部生成入口的任务级互斥——update_task 持锁期间调用
# _generate_due_quietly 靠可重入避免自锁。
_maintenance_lock = threading.RLock()
PLANNING_BOUNDARY_STATE_KEY = "planning.refresh_boundary_state"
PLANNING_DAILY_REFRESH_KEY = "planning.daily_refresh_enabled"
PLANNING_AUTO_RECOMPUTE_ENABLED_KEY = "planning.auto_recompute_enabled"
PLANNING_AUTO_RECOMPUTE_WAIT_KEY = "planning.auto_recompute_wait_minutes"


class PlanningError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400,
                 details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        # 结构化附加数据（如 boundary 冲突清单）：API 层随错误响应返回
        self.details = details


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
    if isinstance(value, str):
        text = value.strip()
        match = _TIME_RE.match(text)
        if match:
            hour, minute = text.split(":")
            return time(int(hour), int(minute))
        # ``HH:MM:SS``（真实 PostgREST time 列形状）：分钟精度契约下秒恒为 0，
        # 截断接受；秒非 0 不是合法模板时刻。
        match = _TIME_WITH_SECONDS_RE.match(text)
        if match:
            hour, minute, second = text.split(":")
            if int(second) != 0:
                raise PlanningError("invalid_payload", f"{field} must be HH:MM")
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


def _load_raw_boundary_state() -> dict[str, Any]:
    """boundary 状态行的原始存储（批次 7）：不做过渡活跃性解释。

    dry-run 预计算、最终保存的 CAS 基准都必须基于同一份原始状态；读取
    失败 503（与 _load_boundary_state 同一语义）。
    """
    raw = db.load_app_setting(PLANNING_BOUNDARY_STATE_KEY)
    if raw is db.APP_SETTING_QUERY_FAILED:
        raise PlanningError("database_unavailable", "规划周期配置暂时无法读取", 503)
    if not isinstance(raw, dict):
        raw = _default_boundary_state()
    return raw


def _parse_boundary_adjustments(value: Any) -> list[dict[str, Any]]:
    """boundary 修改流程中的关联任务窗口调整项（§5.2.2）。

    每一项必须携带完整双端窗口（window_start_tod / window_end_tod，各自
    可空——允许调整成单侧或无窗口以避开新 boundary）；start == end 无效；
    重复 task_id 与未知字段拒绝。返回 RPC 调整项 payload。
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise PlanningError("invalid_payload", "task_adjustments 必须是数组", 400)
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise PlanningError("invalid_payload", "task_adjustments 项必须是对象", 400)
        if (set(item) - {"task_id", "window_start_tod", "window_end_tod"}
                or not {"task_id", "window_start_tod", "window_end_tod"} <= set(item)):
            raise PlanningError(
                "invalid_payload",
                "task_adjustments 项必须包含 task_id 与完整的双端可安排时段", 400)
        task_id = item["task_id"]
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise PlanningError("invalid_payload", "task_adjustments 的 task_id 必须是整数", 400)
        if task_id in seen:
            raise PlanningError("invalid_payload", "task_adjustments 中存在重复的待办", 400)
        seen.add(task_id)
        try:
            start = (time.fromisoformat(str(item["window_start_tod"]))
                     if item["window_start_tod"] is not None else None)
            end = (time.fromisoformat(str(item["window_end_tod"]))
                   if item["window_end_tod"] is not None else None)
        except (TypeError, ValueError) as exc:
            raise PlanningError(
                "invalid_payload",
                "可安排时段的时间格式无效：请使用 HH:MM（例如 09:00）", 400,
            ) from exc
        if start is not None:
            start = start.replace(second=0, microsecond=0)
        if end is not None:
            end = end.replace(second=0, microsecond=0)
        if start is not None and end is not None and start == end:
            raise PlanningError(
                "invalid_payload",
                "可安排时段的开始与结束不能相同（相同时刻不代表 24 小时窗口）", 400)
        out.append({
            "task_id": task_id,
            "window_start_tod": start.strftime("%H:%M") if start else None,
            "window_end_tod": end.strftime("%H:%M") if end else None,
        })
    return out


def _boundary_window_conflicts(
    new_boundary: time,
    adjustments: dict[int, tuple[str | None, str | None]],
    tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """以准备生效的新 boundary 校验全部启用中任务（§5.2.2；dry-run 与
    最终保存共用同一预计算）。

    本请求调整的待办使用提交的新值，未调整者使用数据库当前值；双侧窗口
    禁止跨越（boundary 落在开始/结束时刻的顺时针开区间内即非法，端点
    接触合法）；单侧约束 / 无窗口不构成区间、不做跨越校验（§6.7）。
    暂停刷新（refresh_enabled=false）不豁免——调用方只按 is_active 过滤。
    """
    conflicts: list[dict[str, Any]] = []
    for task in tasks:
        task_id = task.get("id")
        if task_id in adjustments:
            start, end = adjustments[task_id]
        else:
            start = (_canonical_template_value("window_start_tod", task.get("window_start_tod"))
                     if task.get("window_start_tod") else None)
            end = (_canonical_template_value("window_end_tod", task.get("window_end_tod"))
                   if task.get("window_end_tod") else None)
        if not start or not end:
            continue
        template = WindowTemplate(
            start_tod=time.fromisoformat(start), end_tod=time.fromisoformat(end))
        if window_crosses_boundary(template, new_boundary):
            conflicts.append({
                "task_id": task_id,
                "content": task.get("content"),
                "window_start_tod": start,
                "window_end_tod": end,
                "reason": (
                    f"新刷新时间 {new_boundary.strftime('%H:%M')} 落在该待办"
                    "可安排时段的起止时刻之间（时段不能跨越每日刷新时间）"
                ),
            })
    return conflicts


def _save_cycle_boundary(
    boundary: time, raw_state: dict[str, Any],
    adjustments_payload: list[dict[str, Any]], now: datetime,
) -> dict[str, Any]:
    """最终保存（批次 7）：boundary + 关联调整在一个数据库事务内原子生效。

    Python 先按当前状态预计算过渡（复用 BoundaryTransition.plan 语义），
    RPC 内重新全量校验全部启用中模板（modal 打开期间因时间流逝或他人
    操作产生的新冲突同样被拒绝）并原子写入；状态已被其他 worker 改变时
    CAS 未命中 → 409 要求重新计划；冲突 → 409 + 冲突清单。
    """
    client = _require_client()
    committed = set()
    for value in raw_state.get("absorbed") or []:
        try:
            committed.add(date.fromisoformat(value))
        except (TypeError, ValueError):
            continue
    boundary_now = parse_refresh_boundary(
        raw_state.get("boundary") or DEFAULT_REFRESH_BOUNDARY.strftime("%H:%M"))
    info = raw_state.get("transition")
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
    transition_payload = None
    if planned is not None:
        transition_payload = {
            "spanning_key": planned.spanning_key.isoformat(),
            "spanning_boundary": planned.spanning_boundary.strftime("%H:%M"),
            "change_at": _iso(planned.change_at),
        }
    # CAS 基准 = 预计算所依据的原始状态（boundary 文本 + 原始过渡记录）。
    expected_state = {
        "boundary": raw_state.get("boundary"),
        "transition": info if isinstance(info, dict) else None,
    }
    try:
        resp = client.rpc("planning_update_cycle_boundary", {
            "p_new_boundary": boundary.strftime("%H:%M"),
            "p_expected_state": expected_state,
            "p_transition": transition_payload,
            "p_absorbed": sorted(d.isoformat() for d in new_absorbed),
            "p_adjustments": adjustments_payload,
        }).execute()
    except Exception as exc:
        log.exception("boundary 原子保存失败: %s", type(exc).__name__)
        raise PlanningError("database_unavailable", "规划周期配置保存失败", 503) from exc
    data = getattr(resp, "data", None)
    if isinstance(data, dict) and data.get("status") == "stale_state":
        raise PlanningError(
            "boundary_state_conflict",
            "规划周期配置已被其他修改更新，请重新加载后再试", 409)
    if isinstance(data, dict) and data.get("status") == "conflicts":
        conflicts = data.get("conflicts") or []
        raise PlanningError(
            "boundary_window_conflicts",
            "存在跨越新刷新时间的待办，请先调整其可安排时段", 409,
            details={"conflicts": conflicts})
    if not (isinstance(data, dict) and data.get("status") == "ok"):
        raise PlanningError("database_unavailable", "规划周期配置保存失败", 503)
    result = get_cycle_settings(now)
    result["adjusted_tasks"] = int(data.get("updated_tasks") or 0)
    return result


def set_cycle_settings(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """Persist one planning setting; a boundary change is an atomic
    transaction (dry-run precheck + task adjustments + transition state,
    批次 7 §5.2.2) that schedules the next-cycle transition instead of
    reinterpreting the cycle already in progress."""
    now = now or _now()
    allowed = {
        "refresh_boundary_time", "daily_refresh_enabled",
        "auto_recompute_enabled", "auto_recompute_wait_minutes",
        "dry_run", "task_adjustments",
    }
    if not isinstance(payload, dict) or not payload or set(payload) - allowed:
        raise PlanningError("invalid_payload", f"一次仅接受以下之一：refresh_boundary_time, daily_refresh_enabled, auto_recompute_enabled, auto_recompute_wait_minutes", 400)
    if "refresh_boundary_time" not in payload and ("dry_run" in payload or "task_adjustments" in payload):
        raise PlanningError(
            "invalid_payload", "boundary 试算与关联调整只能与 refresh_boundary_time 一同提交", 400)
    if set(payload) & {"daily_refresh_enabled", "auto_recompute_enabled", "auto_recompute_wait_minutes"} and len(payload) != 1:
        raise PlanningError("invalid_payload", f"一次仅接受以下之一：refresh_boundary_time, daily_refresh_enabled, auto_recompute_enabled, auto_recompute_wait_minutes", 400)
    if "refresh_boundary_time" in payload:
        try:
            boundary = parse_refresh_boundary(payload["refresh_boundary_time"])
        except ValueError as exc:
            raise PlanningError("invalid_payload", "刷新时间必须是 00:00 至 23:59", 400) from exc
        dry_run = payload.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise PlanningError("invalid_payload", "dry_run 必须是布尔值", 400)
        adjustments_payload = _parse_boundary_adjustments(payload.get("task_adjustments"))
        adjustments = {
            item["task_id"]: (item["window_start_tod"], item["window_end_tod"])
            for item in adjustments_payload
        }
        # dry-run（§5.2.2）：以准备生效的新 boundary 校验全部启用中任务，
        # 绝对零写入；冲突清单（待办 / 现窗口 / 原因）随响应返回。
        tasks = _rows(_require_client(), "planning_task", lambda q: q.eq("is_active", True))
        conflicts = _boundary_window_conflicts(boundary, adjustments, tasks)
        if dry_run:
            return {
                "dry_run": True,
                "boundary_time": boundary.strftime("%H:%M"),
                "conflicts": conflicts,
            }
        if conflicts:
            raise PlanningError(
                "boundary_window_conflicts",
                "存在跨越新刷新时间的待办，请先调整其可安排时段", 409,
                details={"conflicts": conflicts})
        raw_state = _load_raw_boundary_state()
        return _save_cycle_boundary(boundary, raw_state, adjustments_payload, now)
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
    移除即拒绝），列与存量行按历史语义保留。批次 6（2026-09-28）：窗口
    字段正式开放编辑（current/future 分流）——模板窗口编辑属「未来轮次」
    语义（§18.3、§28.1），只影响尚未生成的实例，已生成实例的冻结窗口
    不受影响；保存校验见 :func:`_validate_template_window_constraints`。
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
        "window_start_tod", "window_end_tod",
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
        # 2026-10-01（§32.45）：单次目标日期为可选项——once 不再要求
        # target_date；空日期表达「未指定日期、常驻显示」，不得自动补今天。
        requirements = {
            "interval": ("interval_days",),
            "weekly": ("weekdays",),
            "monthly": ("month_days",),
        }
        for field in requirements.get(effective_type, ()):
            if result.get(field) is None:
                raise PlanningError("invalid_payload", f"{field} is required for {effective_type} tasks")
        if effective_type == "once":
            # 省略与显式 NULL 等价：落库显式 NULL（不依赖列默认值、不补今天）。
            result.setdefault("target_date", None)

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
    if "window_start_tod" in payload or "window_end_tod" in payload:
        # 批次 6 二轮 MEDIUM：模板编辑入口的时间格式错误统一为项目中文
        # PlanningError（"abc" / 非法时间类型等），不向外暴露英文解析文案。
        try:
            if "window_start_tod" in payload:
                result["window_start_tod"] = _tod_str(
                    payload.get("window_start_tod"), "window_start_tod")
            if "window_end_tod" in payload:
                result["window_end_tod"] = _tod_str(
                    payload.get("window_end_tod"), "window_end_tod")
        except PlanningError as exc:
            raise PlanningError(
                "invalid_payload",
                "可安排时段的时间格式无效：请使用 HH:MM（例如 09:00）", 400,
            ) from exc
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
        # once 已生成标记（§28.3；list_tasks 聚合填充，其它入口缺省 False）
        "has_generated_occurrence": bool(task.get("has_generated_occurrence")),
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


def _validate_template_window_constraints(row: dict[str, Any], now: datetime) -> None:
    """模板窗口的保存校验（§30.6；创建与规则编辑共用同一套领域约束）。

    * 任何非空模板都要求有效占用跨度（预计耗时；中空 = 完整包络）；
    * 双侧窗口禁止跨越每日刷新 boundary（端点接触合法；单侧约束不构成
      区间，不做跨越校验）——复用批次 1 ``validate_template_window``；
    * 形状非法（start == end）经批次 1 领域构造拒绝，并在本入口统一转换
      为项目中文 PlanningError（2026-09-28 一轮 Review MEDIUM：单字段
      PATCH 的合并形状错误不得向 API 泄漏原始 ValueError）；
    * 剩余空间可行性是**创建入口**的拒绝项（§12.1/§30.6），不属于模板
      保存校验：未来轮次装不下时由生成后的排程冲突派生呈现（§18.1）。
    """
    try:
        template = _task_window_template(row)
    except ValueError as exc:
        raise PlanningError(
            "invalid_payload",
            "可安排时段的开始与结束不能相同（相同时刻不代表 24 小时窗口）", 400,
        ) from exc
    if template is None:
        return
    occupancy = _window_occupancy_minutes(row)
    if not isinstance(occupancy, int) or occupancy < 1:
        raise PlanningError("invalid_payload", "填写了可安排时段的待办必须提供有效预计耗时", 400)
    if not template.is_bounded:
        return
    boundary, _, _ = _load_boundary_state(now)
    try:
        validate_template_window(template, boundary)
    except ValueError as exc:
        raise PlanningError(
            "invalid_payload",
            f"可安排时段不能跨越每日刷新时间 {boundary.strftime('%H:%M')}，请调整时段", 400,
        ) from exc


def _validate_window_creation(row: dict[str, Any], now: datetime) -> None:
    """创建入口的窗口与产品边界校验（§10 / §12.1 / §30.6 / §32.40 / §32.41 / §32.45）。

    * once 目标日期为可选项（2026-10-01）：非空时不得早于当前业务日期
      （Asia/Shanghai 当日，自然日比较）；空日期合法、不自动补今天；
      系统补生成路径不经过本入口，不受此限；
    * 无日期 once 不能携带任何窗口端（§30.6：常驻语义、不设最早开始 /
      最晚完成）；
    * 模板保存校验（占用跨度 + boundary 跨越）见
      :func:`_validate_template_window_constraints`（批次 6 起与规则编辑共用）；
    * 创建允许与排程可行性分离（§12.1 / §32.45，2026-10-01）：
      **once** 仅在其解析后的绝对最晚完成已到或越过（当前时刻 ≥ 截止）
      时按时间过期拒绝——窗口起点已过、只填最早开始都不是过期；尚未
      截止但剩余空间不足允许创建并呈现排程冲突（§18.1），不截短耗时；
      **重复待办**不再受创建时当前剩余时间限制——已截止首轮在创建时刻
      经 :func:`_first_round_settlement` 一次性结算（结算游标随任务行
      落库，生成侧按游标跳过），剩余不足由排程冲突派生呈现。
    解析与可行性判断全部调用批次 1 领域函数，与生成冻结共用同一套数学。
    """
    if row.get("task_type") == "once":
        if row.get("target_date") is not None:
            today = _cst_date(now)
            target = _parse_date(row["target_date"], "target_date")
            if target < today:
                raise PlanningError(
                    "invalid_payload",
                    f"目标日期不能早于当前业务日期（{today.isoformat()}）", 400,
                )
        if row.get("window_start_tod") or row.get("window_end_tod"):
            _validate_once_date_window_pair(row)
    _validate_template_window_constraints(row, now)
    template = _task_window_template(row)
    if template is None:
        return
    if row["task_type"] == "once":
        # 指定日期 once：user 自然日期 + 时刻组合成固定绝对约束，不做
        # 候选取舍（§32.41）；仅最晚完成已到或越过时按时间过期拒绝，
        # 绝不顺延（§12.1：2026-10-01 剩余不足不再代替「已过期」）。
        resolved = resolve_window_on_date(
            template, _parse_date(row["target_date"], "target_date"))
        if resolved.end_at is not None and now >= resolved.end_at:
            raise PlanningError(
                "invalid_payload",
                f"单次待办的最晚完成（{resolved.end_at.astimezone(_CST).strftime('%m-%d %H:%M')}）"
                "已到或已过，不能按已过期的时间创建", 400,
            )


def _validate_once_date_window_pair(merged: dict[str, Any]) -> None:
    """无日期单次不设窗口（§30.6 / §28.3 / §32.45，2026-10-01）。

    单次目标日期省略或为 NULL 时不得携带最早开始 / 最晚完成——空日期表达
    「未指定日期、常驻显示」，不是「默认今天」；创建与编辑合并视图共用
    本校验，违反即拒绝且零写入。
    """
    if merged.get("task_type") != "once" or merged.get("target_date"):
        return
    if merged.get("window_start_tod") or merged.get("window_end_tod"):
        raise PlanningError(
            "invalid_payload",
            "未指定日期的单次待办不能设置可安排时段：无日期单次常驻显示，"
            "不设最早开始或最晚完成", 400,
        )


# 2026-10-01 新建首轮裁决（§6.7 / §12.1 / §32.45）适用的固定刷新模式：
# 任务创建时刻当前轮的最晚完成已到或越过时跳过该轮（不生成实例、不制造
# 超时记录），从次日起按原重复规则生效。after_completion（处理后刷新型）
# 没有日历轴、其轮次链依赖首个实例启动，跳过会使它永远等不到首个有效
# 实例——因此不适用本裁决；once 属指定/常驻单次，不在此列。首轮裁决在
# 创建时刻**一次性结算**（R3 审查修复）：跳过经生成游标随任务行落库，
# 生成侧不再按当下时间重判——模板编辑、暂停恢复、重复维护不复活被跳过
# 的首轮，即时生成的暂时失败也不被误判成跳过（首轮 DUE 保留恢复资格）。
_ROUND_SKIP_REFRESH_MODES = ("daily", "fixed_interval", "fixed_weekday", "fixed_monthday")


def _round_deadline_passed(
    task: dict[str, Any], round_day: date, now: datetime,
    configured: time, transition: BoundaryTransition | None,
) -> bool:
    """当前轮（按其规划周期解析的本轮窗口）最晚完成是否已到或越过。

    ``round_day`` 必须是**当前轮的规划周期键**——本轮窗口以该周期起点为
    参考解析（候选取舍只看是否已结束，等价于取该周期内的窗口出现），
    与生成冻结（``_resolve_generation_window`` 按 born 周期解析）同一
    归属，绝不把下一周期的窗口当成当前轮截止（R1 审查修复：fixed_interval
    的到期事件自然日只是事件身份，不得用于截止解析）。当前时刻 ≥ 解析后
    的绝对截止即已到或越过。无窗口或只有最早开始没有最晚完成，不存在
    截止；早于每日刷新 boundary 的清晨窗口解析到该周期内的下一次出现，
    正常生成不受影响。
    """
    if task.get("refresh_mode") not in _ROUND_SKIP_REFRESH_MODES:
        return False
    template = _task_window_template(task)
    if template is None or template.end_tod is None:
        return False  # 无窗口 / 只有最早开始：没有最晚完成，不存在截止
    boundary = cycle_start_boundary(round_day, configured, transition)
    cycle_start = PlanningCycle.for_key(round_day, boundary).start
    resolved = resolve_window(template, round_day, cycle_start)
    if resolved.end_at is None:
        return False
    return now >= resolved.end_at  # 已到或越过（§12.1：当前时刻 ≥ 绝对截止）


def _fixed_interval_anchor_due(row: dict[str, Any], now: datetime) -> bool:
    """固定间隔首个轴点是否已到期（due ≤ now；§8.1 显式首次基准）。

    首个轴点 = ``refresh_anchor_at``（默认锚点 = 创建时刻）。轴点未到时
    首轮尚未成为「当前轮」：既不能提前结算游标（R7 审查修复——否则首个
    事件被游标吞掉、第一次任务丢失），也不按创建时刻的窗口剩余编造当前
    轮冲突反馈（轮次将在到期时按候选解析生成）。结算与创建反馈两个入口
    共用本判定，不复制第二套轴点数学。
    """
    anchor = row.get("refresh_anchor_at")
    if not anchor:
        return False
    return _parse_dt(anchor, "refresh_anchor_at") <= now


def _first_round_settlement(
    row: dict[str, Any], now: datetime,
) -> tuple[bool, date | None]:
    """创建时刻的首轮裁决（R1 / R3 / R6 / R7 审查修复）：一次性判定并给出结算。

    返回 ``(skipped, settle_day)``：

    * 当前轮不存在（weekly / monthly 的当前规划周期键不是规则日，R6；
      fixed_interval 的显式首次基准尚未到期，R7）→ ``(False, None)``——
      没有「当前轮」可跳过，也不为「次日起生效」在非法日期强造轮次；
      daily 与默认锚点（锚点 = 创建时刻）的 fixed_interval 当前轮恒存在；
    * 当前轮存在、最晚完成尚未到 → ``(False, None)``——首轮 DUE：生成
      资格保留，即时生成的暂时失败由后续维护按候选解析恢复，不因时间
      流逝被重判为跳过（R3-A）；
    * 当前轮存在且最晚完成已到或越过 → ``(True, 首轮事件日)``——创建时
      即结算：结算游标随任务行原子落库，生成侧只按游标跳过；模板编辑、
      暂停恢复、重复维护不得复活被跳过的首轮（R3-B）。

    截止解析统一用当前轮的规划周期（R1）；结算日 = 被跳过首轮的**事件日
    **（daily / weekly / monthly = 当前周期键；fixed_interval = 首个轴点
    ``refresh_anchor_at`` 的自然日，与固定轴枚举的 ``day`` 同一语义，R7
    审查修复——不得把未到期事件或整段历史轴统一结算为创建自然日），不
    改变轮次键与固定轴。
    """
    if row.get("refresh_mode") not in _ROUND_SKIP_REFRESH_MODES:
        return False, None
    template = _task_window_template(row)
    if template is None or template.end_tod is None:
        return False, None  # 无窗口 / 只有最早开始：没有最晚完成，不存在截止
    mode = row["refresh_mode"]
    if mode == "fixed_interval" and not _fixed_interval_anchor_due(row, now):
        return False, None  # 显式未来首次基准未到期：首轮尚未成为当前轮（R7）
    configured, transition, _ = _load_boundary_state(now)
    cycle_key = _current_cycle(now).key
    if mode in ("fixed_weekday", "fixed_monthday") and not _should_occur(row, cycle_key):
        return False, None  # 当前周期无合法轮次：无「当前轮」可跳过（R6）
    if not _round_deadline_passed(row, cycle_key, now, configured, transition):
        return False, None  # 首轮 DUE：保留生成资格（R3-A 恢复语义）
    settle_day = (
        _parse_dt(row["refresh_anchor_at"], "refresh_anchor_at").date()
        if mode == "fixed_interval" else cycle_key)
    return True, settle_day


def _creation_window_outcome(
    row: dict[str, Any], now: datetime,
) -> tuple[bool, bool, date | None]:
    """创建反馈与首轮结算（§30.6 / §18.1 / §32.45，2026-10-01）。

    返回 ``(first_round_skipped, schedule_conflict, settle_day)``：判定与
    生成侧共享同一套周期归属（当前轮 = 当前规划周期），保证「创建响应
    提示」「结算游标」「实际生成行为」一致，不伪造排程成功。

    * 重复待办：当前轮最晚完成已到或越过 → 首轮跳过并结算（任务保存、
      零实例、游标承载）；未截止但剩余空间不足 → 当前轮照常生成并呈现
      排程冲突；当前周期无合法轮次（weekly / monthly 非规则日，R10）或
      固定间隔首个轴点尚未到期（R7）→ 无当前轮，跳过与冲突反馈均为否；
    * after_completion（R9）：没有首轮跳过资格（跳过会让轮次链永远等不
      到首个有效实例），但冲突反馈资格照常——按当前轮实际冻结窗口检查
      剩余空间，与看板同一派生口径；
    * once：无窗口 / 只有最早开始不存在截止；有最晚完成时（创建校验已
      保证未过期）剩余空间不足 → 排程冲突。
    """
    template = _task_window_template(row)
    if template is None or template.end_tod is None:
        return False, False, None
    occupancy = _window_occupancy_minutes(row)
    if row.get("task_type") == "once":
        resolved = resolve_window_on_date(
            template, _parse_date(row["target_date"], "target_date"))
        return False, not window_feasible(resolved, now, occupancy), None
    mode = row.get("refresh_mode")
    # 跳过资格（固定刷新型）与冲突反馈资格（固定刷新型 + 处理后刷新型）
    # 分开（R9 审查修复）：after_completion 不跳过首轮，不代表它不需要
    # 检查创建时冲突。
    skip_eligible = mode in _ROUND_SKIP_REFRESH_MODES
    if not skip_eligible and mode != "after_completion":
        return False, False, None
    if not isinstance(occupancy, int) or occupancy < 1:
        return False, False, None
    if mode == "fixed_interval" and not _fixed_interval_anchor_due(row, now):
        return False, False, None  # 首个轴点未到期：尚无当前轮（R7）
    configured, transition, _ = _load_boundary_state(now)
    cycle_key = _current_cycle(now).key
    if (mode in ("fixed_weekday", "fixed_monthday")
            and not _should_occur(row, cycle_key)):
        return False, False, None  # 非规则日没有当前轮（R6 / R10）
    if skip_eligible:
        skipped, settle_day = _first_round_settlement(row, now)
        if skipped:
            return True, False, settle_day
    # 未跳过时本轮冻结窗口 = 以当前时刻为参考的候选解析（与
    # _resolve_generation_window 同一归属与参考）；剩余装不下即排程冲突。
    resolved = resolve_window(template, cycle_key, now)
    return False, not window_feasible(resolved, now, occupancy), None


def create_task(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    row = validate_task_payload(payload, partial=False)
    _prepare_refresh_definition(row, now)
    _validate_window_creation(row, now)
    row["created_at"] = _iso(now)
    row["updated_at"] = _iso(now)
    row["is_fixed"] = bool(row.get("is_fixed"))
    # 创建反馈 + 首轮结算（§30.6 / §18.1 / §32.45）：判定、提示与结算在
    # 写入前一次完成；跳过经生成游标随任务行原子落库（R3 稳定裁决），
    # 生成侧不再按当下时间重判。
    first_round_skipped, schedule_conflict, settle_day = _creation_window_outcome(row, now)
    if settle_day is not None:
        row["refresh_generated_through"] = settle_day.isoformat()
    client = _require_client()
    response = client.table("planning_task").insert(row).execute()
    created = (response.data or [{}])[0]
    # 即时生成：新建的待办（含 interval 立即到期）不等后台循环，立刻出现在列表。
    _generate_due_quietly(client, now)
    serialized = serialize_task(created, now)
    # 创建反馈（§30.6 / §18.1）：区分「本轮已截止、次日起生效」与
    # 「已创建但存在排程冲突」，两者都不改变任务已保存的事实。
    serialized["first_round_skipped"] = first_round_skipped
    serialized["schedule_conflict"] = schedule_conflict
    return serialized


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


def _should_recompute_after_generation(result: Any) -> bool:
    """generation 之后是否应触发一次保守的幂等重算（五轮 / 六轮 Review）。

    正式条件：``created > 0`` **或** ``errors 非空``——partial create（INSERT
    成功后 cursor 等后续写失败）的 created 计数会丢失，但新行已真实持久化；
    task-level failure 同样可能发生在 INSERT 之后。多跑一次幂等重算的代价
    低于新 occurrence 永久漏掉自动排程。run_maintenance 与
    _generate_due_quietly 两个调用入口共用本判断，不得各自漂移。
    """
    return isinstance(result, dict) and bool(
        result.get("created") or result.get("errors"))


def _generate_due_quietly(client, now: datetime) -> None:
    """写操作后的同步补生成：幂等，失败只记日志，不吞掉已成功的写操作。

    当天有新生成实例、或 generation 报告了 task-level failure（partial
    create 的 created 计数会丢失，见 `_should_recompute_after_generation`）
    时，顺带重算一次，让用户立刻看到带起止时间的列表；重算以排列顺序与
    固定槽为准，不会动用户已固定的内容。
    最终修复（问题 3）：与 once 身份编辑共享 _maintenance_lock——生成
    （含本入口）不得与 once 编辑的「检查 + 保存」交错产生半状态。
    """
    with _maintenance_lock:
        try:
            result = generate_due(now)
        except Exception as exc:
            log.warning(
                "planning 同步生成失败（等待后台循环重试）: error=%s", type(exc).__name__,
            )
            return
        if not _should_recompute_after_generation(result):
            return
        try:
            recompute_today(now)
        except Exception as exc:
            log.warning("planning 同步重算失败: error=%s", type(exc).__name__)


SCHEDULE_FIELDS = {
    "task_type", "interval_days", "weekdays", "month_days", "target_date",
    "refresh_mode", "refresh_anchor_at",
    "time_mode", "estimated_minutes",
    "window_start_tod", "window_end_tod",
    "hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes",
    "hollow_start_content", "hollow_end_content", "hollow_wait_note",
}
# 批次 6 一轮 Review 裁决（2026-09-28）：模板窗口不是 recurrence 字段——
# window_start_tod / window_end_tod 只是未来 occurrence 的窗口模板 snapshot
# 来源，不改变周期事件轴，编辑它们不得重置 recurrence 生成游标；未来尚未
# 生成的 occurrence 自然从任务当前模板冻结新值。窗口字段保留在
# SCHEDULE_FIELDS 只为触发写后的幂等补生成（无游标副作用）。
# 旧 est_start_tod / est_end_tod 已停止接受写入（批次 3 白名单移除），作为
# 调度规则字段一并退役。存量行读取兼容不受影响。
# 真正改变 recurrence 事件轴的字段：interval_days / weekdays / month_days
# （refresh_mode / refresh_anchor_at / task_type 在已有轮次时被 409 锁定；
# 无轮次时经 _prepare_refresh_definition 的 mode_changed 分支重置游标）。
_RECURRENCE_SWITCH_FIELDS = {"interval_days", "weekdays", "month_days"}
_ONCE_LOCKED_TEMPLATE_FIELDS = ("target_date", "window_start_tod", "window_end_tod")


def _canonical_template_value(field: str, value: Any) -> Any:
    """模板字段的语义规范值（批次 6 二轮 HIGH：同值比较不得用原始字符串）。

    数据库返回 ``09:00:00`` 而编辑入口规范化为 ``09:00``——二者语义相同；
    比较前统一经 ``time.fromisoformat``（现有 TOD 解析）规范化，避免
    「DB 09:00:00 + PATCH 09:00 被误判为实际修改」。target_date 经
    ``_parse_date().isoformat()`` 规范化；其它字段原样返回。
    """
    if value is None:
        return None
    if field in ("window_start_tod", "window_end_tod"):
        return time.fromisoformat(value).strftime("%H:%M")
    if field == "target_date":
        return _parse_date(value, field).isoformat()
    return value


def _close_out_recurrence_before_switch(client, old_task: dict[str, Any], now: datetime) -> None:
    """规则切换 Phase A（2026-09-28 一轮 Review BLOCKER 1 裁决）：旧规则收尾。

    rule_switch_at = 本次规则编辑生效时刻（= now）。旧规则负责全部
    ``due_at <= rule_switch_at`` 的轮次——用**修改前的任务快照**按既有
    reconcile 语义补齐旧轴已到期但尚未生成的漏轮、执行既有的固定到期
    清理与开放轮次顺延。收尾失败（生成异常等）时异常向上传播：新规则
    一律不保存（Phase B 不执行），已补齐的旧轴轮次是旧规则欠下的合法
    事实，重试整个编辑时 Phase A 幂等（轮次唯一键）。
    """
    configured, transition, absorbed = _load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    daily_enabled = get_cycle_settings(now)["daily_refresh_enabled"]
    _reconcile_task_rounds(
        client, old_task, cycle, now, configured, transition, absorbed, daily_enabled,
    )


def _first_rule_event_after(
    task: dict[str, Any], switch_at: datetime,
    configured: time, transition: BoundaryTransition | None,
    absorbed: frozenset[date],
) -> tuple[date, datetime] | None:
    """规则切换 Phase C 的下界定位：该任务**当前规则**事件轴上
    ``due`` 严格晚于 ``switch_at`` 的第一个事件 ``(day, due)``。

    现有按日游标 ``refresh_generated_through`` 借助本计算**精确**表达
    切换点：把游标写成「首个新事件日期的前一天」，生成循环的
    ``day <= cursor`` 跳过恰好吞掉切换日上位于 switch_at 之前的同日
    事件，且不丢失其后任何事件——无需近似「昨天/今天」，也无需新增
    持久化字段。事件数学与 ``_fixed_rounds`` / ``_following_fixed_event``
    完全同源（不复制第二套 recurrence 数学）。返回 None 表示规则数据
    异常无法定位（调用方拒绝本次编辑，绝不静默丢轮）。
    """
    mode = task.get("refresh_mode")
    if mode == "fixed_interval":
        anchor = _parse_dt(task.get("refresh_anchor_at"), "refresh_anchor_at")
        interval = task.get("interval_days")
        if not isinstance(interval, int) or interval < 1:
            return None
        due = anchor
        switch_abs = switch_at.astimezone(timezone.utc)
        for _ in range(4000):  # 防御上限；锚点到切换点的轴步数远小于此
            if due.astimezone(timezone.utc) > switch_abs:
                return due.date(), due
            due += timedelta(days=interval)
        return None
    created = _parse_dt(task.get("created_at"), "created_at")
    start = planning_cycle_at(created, configured, transition).key
    day = start
    first_due: datetime | None = None
    for _ in range(400):
        if day not in absorbed and _should_occur(task, day):
            boundary = cycle_start_boundary(day, configured, transition)
            due = (max(created, PlanningCycle.for_key(day, boundary).start)
                   if day == start else PlanningCycle.for_key(day, boundary).start)
            if due >= created:
                first_due = due
                break
        day += timedelta(days=1)
    if first_due is None:
        return None
    due, event_day = first_due, day
    switch_abs = switch_at.astimezone(timezone.utc)
    while due.astimezone(timezone.utc) <= switch_abs:
        nxt = _following_fixed_event(task, event_day, due, configured, transition, absorbed)
        if nxt is None:
            return None
        event_day, due = nxt.date(), nxt
    return event_day, due


def _recurrence_switch_cursor(
    new_task: dict[str, Any], switch_at: datetime,
) -> str:
    """规则切换 Phase C：新规则生成游标下界（只枚举 due > switch_at）。

    cursor = 首个新事件日期的前一天。生成循环随后按 ``day > cursor``
    枚举：切换日上早于切换时刻的新轴同日事件被精确跳过（不追溯），
    其后事件正常生成（旧轴漏轮已在 Phase A 按旧规则补齐，不依赖游标
    重置）。无法定位首个新事件时拒绝本次编辑（409），绝不静默丢轮。
    """
    configured, transition, absorbed = _load_boundary_state(switch_at)
    first = _first_rule_event_after(new_task, switch_at, configured, transition, absorbed)
    if first is None:
        raise PlanningError(
            "invalid_task", "无法确定新规则在编辑时刻之后的首个轮次，规则编辑未保存", 409)
    day, _ = first
    return (day - timedelta(days=1)).isoformat()


def update_task(task_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """任务编辑入口（最终修复问题 3）：与全部生成入口共享
    ``_maintenance_lock``——once 身份编辑的「检查无实例 → 保存新日期」与
    生成创建 once 实例互斥，杜绝「任务日期 ≠ 唯一实例」的交错半状态。
    不改 once 轮次唯一键语义、不新增状态字段。
    """
    with _maintenance_lock:
        return _update_task(task_id, payload, now)


def _update_task(task_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
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
    if task.get("time_mode") == "explicit" and row.get("time_mode") == "duration":
        # The former rule anchor is no longer part of the task definition.
        row["est_start_tod"] = None
        row["est_end_tod"] = None
        row["is_fixed"] = False
    if task.get("refresh_mode") is not None or "refresh_mode" in row or "task_type" in row:
        _prepare_refresh_definition(row, now, task)
    merged = {**task, **row}
    _ensure_type_requirements(merged)
    # 2026-10-01（§30.6 / §32.45）：编辑合并视图同样执行「无日期单次不设
    # 窗口」——清除尚未生成单次的日期时，窗口也须由 user 明确清空后才能
    # 保存，不能只清日期而残留窗口值。
    _validate_once_date_window_pair(merged)
    if (task.get("time_mode") == "explicit" and merged.get("time_mode") == "duration"
            and not merged.get("estimated_minutes")):
        raise PlanningError("invalid_payload", "仅耗时待办必须提供有效预估耗时", 400)
    # once 已生成后的任务级排程身份锁定（2026-09-28 一轮 Review HIGH 裁决；
    # 二轮 HIGH：「实际变化」判定先做语义规范化——DB ``09:00:00`` ≡ PATCH
    # ``09:00``，不用原始字符串比较）。once 没有「未来轮次」可消费新模板，
    # 已生成实例存在时禁止实际变化地修改 target_date / 未来窗口模板——
    # 否则形成「任务显示新日期、唯一实例仍属旧日期、新日期永不生成」的半
    # 重定向状态。仅语义无变化的幂等 PATCH 按现有语义放行；调整已生成的
    # 这一次走当前实例窗口编辑。身份锁定先于值校验。
    once_identity_edit = merged.get("task_type") == "once" and any(
            field in row
            and _canonical_template_value(field, row.get(field))
            != _canonical_template_value(field, task.get(field))
            for field in _ONCE_LOCKED_TEMPLATE_FIELDS)
    if once_identity_edit:
        # 预检（友好错误；权威复核在写入阶段的锁内 RPC——最终修复问题 3）：
        # once 没有「未来轮次」可消费新模板，已生成实例存在时禁止实际变化
        # 地修改任务日期 / 未来窗口模板。
        existing_once = _rows(
            client, "planning_occurrence", lambda q: q.eq("task_id", task_id).limit(1))
        if existing_once:
            raise PlanningError(
                "invalid_payload",
                "单次待办已生成当前实例，请编辑当前实例，不可再修改任务日期或未来窗口模板", 400,
            )
    # 批次 6（§18.3 未来轮次 / §28.1）：模板窗口正式开放编辑——保存校验
    # 复用创建入口同一套领域约束（占用跨度有效性 + 双侧 boundary 跨越）。
    # 语义是修改「未来模板」：已生成实例的冻结窗口不做任何同步、不改写、
    # 不按新模板重新解析（§6.7 冻结事实、不变量 36）。单字段 PATCH 的
    # 合并形状错误（如 09:00–12:00 仅改 start=12:00 → 12:00–12:00）在
    # 这里以项目中文 PlanningError 规范化拒绝，不泄漏领域 ValueError。
    if "window_start_tod" in row or "window_end_tod" in row:
        _validate_template_window_constraints(merged, now)
    # 产品边界（§10/§30.6/§32.40）：编辑重定向 target_date 不得早于当前
    # 业务日期（Asia/Shanghai 当日，自然日比较）；仅当 target_date 实际
    # 变化时校验（仅未生成 once 可达——已生成已被上方锁定拒绝）。空日期
    # 本身合法（2026-10-01 日期可选），不进入日期下界比较。
    if ("target_date" in row and row.get("target_date") is not None
            and row.get("target_date") != task.get("target_date")
            and merged.get("task_type") == "once"):
        today = _cst_date(now)
        if _parse_date(row["target_date"], "target_date") < today:
            raise PlanningError(
                "invalid_payload",
                f"目标日期不能早于当前业务日期（{today.isoformat()}）", 400,
            )

    # 废弃整个任务：终止后续刷新，并关闭所有仍开放的出现实例。
    reactivated = bool(row.get("is_active")) and not task.get("is_active")
    if reactivated and task.get("request_state") == "superseded":
        # H2/I6：被取代的重排请求是终态，不得通过普通启用入口复活。
        raise PlanningError(
            "invalid_transition", "该任务来自已被取代的重排请求，不能重新启用", 409,
        )
    if reactivated:
        # 批次 9 Review HIGH #4：重新启用必须按「重新启用时的当前正式
        # boundary」重新验证模板窗口——inactive 任务不参与 boundary 修改
        # 流程的全量扫描（§5.2.2），停用期间 boundary 可能已变。预检给
        # 中文错误；并发下由 planning_boundary_window_guard 守卫在 advisory
        # lock 内权威复核（PostgREST / RPC / 直接 SQL 全覆盖，HIGH #3）。
        _validate_template_window_constraints(merged, now)
    # 最终 Debug（明日可用 HIGH）：is_active=false 与其它实际修改字段同请求
    # 会形成「RPC 已提交停用废弃、后续字段保存失败」的半成功——方案 A：
    # 写入前明确拒绝，要求停用操作单独提交（validation-before-write，
    # 零写入）。幂等无变化字段不触发本限制。
    if (row.get("is_active") is False and task.get("is_active")
            and any(key not in ("is_active", "updated_at")
                    and _canonical_template_value(key, row[key])
                    != _canonical_template_value(key, task.get(key))
                    for key in row)):
        raise PlanningError(
            "invalid_payload",
            "停用待办不能与其它修改同时提交：请单独执行停用操作", 400,
        )
    if row.get("is_active") is False and task.get("is_active"):
        # 最终 Debug（问题 1A）：停用/废弃整个任务 = 单事务命令——复用
        # planning_discard_task（锁任务行 → 单语句关闭全部开放 occurrence
        # → 单语句停用任务，任一失败整体回滚）。occurrence 关闭不再发生于
        # 事务之外；is_active 由 RPC 写入，主任务更新不再重复该字段。
        _discard_task_atomically(client, task_id, now)
        # 停用命令已在 RPC 内完成全部写入。这里不得再发普通 UPDATE：即使
        # 只写 updated_at，失败也会造成 API 报错而任务实际已停用。
        # 批次 6 收尾（BUG B）：停用关闭了开放实例、从排程释放时间槽——
        # 成功后必须登记重算请求，让后续实例填补释放的槽位；登记是
        # post-commit side effect，失败不伪装成停用失败（quiet）。
        _request_recompute_quietly("task_discarded", now)
        updated = {**task, "is_active": False, "updated_at": _iso(now)}
        return serialize_task(updated, now)

    row["updated_at"] = _iso(now)
    # 批次 6 一轮 Review BLOCKER 1（2026-09-28 user 裁决）：recurrence 规则
    # 编辑采用「切换时刻」语义——rule_switch_at = 本次编辑生效时刻；旧规则
    # 负责 due <= rule_switch_at 的全部轮次，新规则只负责 due > rule_switch_at。
    # 正确顺序：Phase A 用修改前快照把旧规则截至切换时刻欠下的漏轮补齐（含
    # 既有固定到期清理与顺延）→ Phase B 保存新规则 → Phase C 把生成游标
    # 写成「新规则首个合法事件（due > switch_at）日期的前一天」，精确表达
    # 切换下界。绝不把新规则用于编辑前时刻，也绝不因游标重置跳掉旧轴漏轮
    # （轮次唯一键只防重复，不定义切换语义）。
    # 批次 6 二轮 Review BLOCKER 2（user 批准继续）：**窗口模板实际变化同样
    # 先结清旧模板漏轮**——window-only / 组合编辑都用完整修改前快照做一次
    # Phase A（旧漏轮冻结旧 recurrence + 旧窗口模板），Phase B 一次保存全部
    # 新值；window-only 不触碰生成游标（Phase C 仅 recurrence 变化时执行），
    # 后续真正未来轮自然从当前模板冻结新窗口。同请求 recurrence + window
    # 只执行一次 closeout，绝不重复收尾两遍。模板窗口 / 耗时等内容类编辑
    # 不重置生成游标（旧「重置到昨天」设计已废除）。
    recurrence_changed = any(
        field in row and row[field] != task.get(field)
        for field in _RECURRENCE_SWITCH_FIELDS)
    window_changed = any(
        field in row
        and _canonical_template_value(field, row.get(field))
        != _canonical_template_value(field, task.get(field))
        for field in ("window_start_tod", "window_end_tod"))
    switching = (
        (recurrence_changed or window_changed)
        and task.get("refresh_mode") in _FIXED_EXPIRING_MODES
        and task.get("is_active")
        and row.get("is_active") is not False
    )
    if switching:
        # Phase A：旧定义（旧 recurrence + 旧窗口模板）收尾——失败即异常
        # 上抛，新值一律不保存，任务行保持旧定义；已补齐的旧轴轮次是合法
        # 事实，重试幂等。同请求 recurrence + window 只收尾这一次。
        _close_out_recurrence_before_switch(client, task, now)
        if recurrence_changed:
            # Phase C：仅规则变化时写新规则下界（基于合并后的新规则计算）。
            row["refresh_generated_through"] = _recurrence_switch_cursor(merged, now)
    # 恢复刷新（False→True）沿用创建入口的同步补生成先例：恢复后立即进入
    # 现有生成体系，当期应有轮次不等下一个维护周期。
    resume_refresh = row.get("refresh_enabled") is True and task.get("refresh_enabled") is False
    schedule_touched = any(
        field in row
        and _canonical_template_value(field, row[field])
        != _canonical_template_value(field, task.get(field))
        for field in SCHEDULE_FIELDS
    )
    schedule_changed = False
    if reactivated or (schedule_touched and task.get("is_active")):
        # 新轮次由规则与持久身份决定。编辑规则不删除既有业务轮次。
        schedule_changed = True

    # 批次 6：需求 18.3「限时修改同步开放实例」块正式退役（迁移表「最终
    # 退役」）。写入侧自批次 3 起不再接受 deadline_tod / deadline_end_tod
    # （白名单拒绝），该同步块已不可达；旧 is_limited / deadline_at 为存量
    # 行历史兼容字段，仅序列化兼容读取，不构成任何业务判定来源（超时唯一
    # 权威自批次 5 起为实例行 window_end_at）。任务模板编辑对已生成实例
    # 无任何同步（§18.3 / §28.1：已生成即已生成，不因模板编辑重新出生）。

    if once_identity_edit:
        # 最终修复（问题 3）：once 身份编辑的「检查无实例 → 保存新值」在
        # 数据库事务内以任务行 FOR UPDATE 锁原子完成（与生成的锁内插入
        # 互斥）——跨进程 / 多 worker 下不再可能产生「任务日期 ≠ 唯一
        # 实例」。锁内复核发现实例已存在 → 与预检同一中文错误、零写入。
        try:
            client.rpc("planning_update_once_task_guarded", {
                "p_task_id": task_id, "p_patch": row,
            }).execute()
        except PlanningError:
            raise
        except Exception as exc:
            if "once identity locked" in str(exc):
                raise PlanningError(
                    "invalid_payload",
                    "单次待办已生成当前实例，请编辑当前实例，不可再修改任务日期或未来窗口模板", 400,
                ) from exc
            raise
        updated = {**task, **row}
    else:
        response = client.table("planning_task").update(row).eq("id", task_id).execute()
        updated = (response.data or [{}])[0]
    deactivated = row.get("is_active") is False or task.get("is_active") is False
    if row.get("is_active") is False:
        request_recompute("task_discarded", now)
    elif (schedule_changed or resume_refresh) and not deactivated:
        # 停用命令（is_active=False）不得触发补生成——废弃后新轮次必须
        # 不存在（最终 Debug 问题 1A：single-transaction discard）。
        # 规则变更后只尝试当前应有轮次；唯一键保护既有轮次。
        _generate_due_quietly(client, now)
    return serialize_task(updated, now)


def _ensure_type_requirements(task: dict[str, Any]) -> None:
    """编辑合并后的完整任务定义必须仍满足其类型的必填字段。

    2026-10-01（§32.45）：once 的 target_date 为可选项，不再属于必填；
    无日期 once 的窗口组合约束由 :func:`_validate_once_date_window_pair`
    在合并视图上单独执行。"""
    task_type = task.get("task_type")
    requirements = {
        "interval": ("interval_days",),
        "weekly": ("weekdays",),
        "monthly": ("month_days",),
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
    # once 已生成的锁定标记（§28.3，批次 9 UI #1）：前端编辑表单必须在不
    # 依赖 occurrences 列表加载状态的前提下可靠禁用 target_date / 未来窗
    # 口模板。只查 once 任务的实例存在性（每条 once 至多一个实例，集合有界）。
    once_ids = [row["id"] for row in rows if row.get("task_type") == "once"]
    generated: set[int] = set()
    if once_ids:
        # 过滤链尾不得再接 .select(...)：requirements 锁定的 supabase 2.15.1
        # 的 postgrest builder 不支持 in_→select 链序（AttributeError，生产
        # 2026-10-01 smoke 复现）；列裁剪交给 _rows 既有 select("*")，集合有界。
        occ_rows = _rows(
            client, "planning_occurrence", lambda q: q.in_("task_id", once_ids))
        generated = {occ["task_id"] for occ in occ_rows}
    return [
        serialize_task({**row, "has_generated_occurrence": row["id"] in generated}, now)
        for row in rows
    ]


# ── 出现实例生成（只依据任务定义与规则游标） ──────────────────────

def _once_schedule_date(
    task: dict[str, Any], configured: time, transition: BoundaryTransition | None,
) -> date:
    """once 的内部规划周期归属（2026-09-27 分离裁决，§32.41；2026-10-01 日期可选）。

    * 无日期 once（2026-10-01）：不解析目标日窗口，内部原始周期按创建
      时刻确定（§6.3 / §10 常驻）——target_date 保持为空，不得把内部
      周期日期回填成 user 指定日期；
    * 指定日期：双端窗口 / 只有最早开始 → 窗口起点绝对时刻所属规划周期；
      只有最晚完成 → 该唯一指定时刻所属规划周期；无窗口 → target_date
      （现行规则沿用）。

    boundary 只参与此内部归属换算（时间早于 boundary 自然归属前一天），
    不得改写 target_date 或绝对窗口；结果允许早于 target_date。
    """
    if not task.get("target_date"):
        return planning_cycle_at(
            _parse_dt(task["created_at"], "created_at"), configured, transition).key
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
    fixed_expires_at: datetime | None = None,
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
        # 固定轮次生成时冻结的到期死亡边界（三轮 Review 裁决，20260928010000）：
        # 仅固定轴轮由调用方传入；中空两阶段共享同值；其余恒 NULL。
        "fixed_expires_at": _iso(fixed_expires_at) if fixed_expires_at else None,
        "source": "schedule",
        **_generation_snapshots(task, identity.schedule_date, phase),
        "created_at": _iso(now),
        "updated_at": _iso(now),
    }


def _create_occurrences(
    client, task: dict[str, Any], schedule_date: date, now: datetime,
    *, due_at: datetime | None = None, display_cycle_date: date | None = None,
    generation_request_key: str | None = None,
    fixed_expires_at: datetime | None = None,
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
            rows.append(_occurrence_row(task, phase, start, end, now, identity, window=window,
                                        fixed_expires_at=fixed_expires_at))
            if fixed_mode and due_at is not None:
                rows[-1]["fixed_due_at"] = _iso(due_at)
    else:
        identity = OccurrenceIdentity(task["id"], round_key, schedule_date, display_cycle,
                                      display_reason)
        rows.append(_occurrence_row(task, None, est_start, est_end, now, identity, window=window,
                                    fixed_expires_at=fixed_expires_at))
        if fixed_mode and due_at is not None:
            rows[-1]["fixed_due_at"] = _iso(due_at)
    if generation_request_key:
        # 幂等身份随实例单次插入落库（超时重排等请求），配合部分唯一索引
        # 保证重放 / 并发只产生一份结果。
        for row in rows:
            row["generation_request_key"] = generation_request_key
    if task.get("task_type") == "once" and task.get("refresh_mode") == "none":
        # 最终修复（问题 3）：once 生成在锁内复核任务定义未漂移——编辑与
        # 生成的交错使按旧定义预构造的行作废（下一次维护按新定义重新
        # 生成），「任务日期 ≠ 唯一实例」不可能落库。
        try:
            client.rpc("planning_insert_once_occurrence", {
                "p_task_id": task["id"],
                "p_expected_target_date": task.get("target_date"),
                "p_expected_window_start_tod": task.get("window_start_tod"),
                "p_expected_window_end_tod": task.get("window_end_tod"),
                "p_rows": rows,
                # 最终 Debug（问题 2）：全部会冻结进 occurrence 的任务输入
                # 快照——锁内逐一复核，任一漂移即放弃旧 snapshot 生成。
                "p_expected_task": {
                    "content": task.get("content"),
                    "estimated_minutes": task.get("estimated_minutes"),
                    "time_mode": task.get("time_mode"),
                    "is_hollow": bool(task.get("is_hollow")),
                    "hollow_start_minutes": task.get("hollow_start_minutes"),
                    "hollow_wait_minutes": task.get("hollow_wait_minutes"),
                    "hollow_end_minutes": task.get("hollow_end_minutes"),
                },
            }).execute()
        except Exception as exc:
            if "planning_occurrence_round_phase_uq" in str(exc):
                log.info("planning 轮次已存在: task=%s round=%s", task["id"], round_key)
                return 0
            if "once definition changed during generation" in str(exc):
                log.info("planning once 定义在生成期间被编辑，本轮作废: task=%s", task["id"])
                return 0
            raise
        return len(rows)
    # 最终 Debug（明日可用 BLOCKER 2）：非 once 生成同样在任务行锁内复核
    # 定义未漂移（active / refresh_enabled / content / 耗时 / 窗口模板）——
    # 停用或模板修改与生成的交错使旧 snapshot 行作废（0 行），后续维护按
    # 新定义重新生成；不先 INSERT 再补救删除。
    try:
        client.rpc("planning_insert_round_occurrence", {
            "p_task_id": task["id"], "p_rows": rows,
            "p_expected_task": {field: task.get(field) for field in (
                "task_type", "refresh_mode", "refresh_enabled", "is_active",
                "request_state", "content", "time_mode", "estimated_minutes",
                "is_hollow", "hollow_start_minutes", "hollow_wait_minutes",
                "hollow_end_minutes", "hollow_start_content", "hollow_end_content",
                "interval_days", "weekdays", "month_days", "target_date",
                "created_at", "refresh_anchor_at",
                "last_handled_at", "refresh_next_due_at", "window_start_tod",
                "window_end_tod",
            )},
        }).execute()
    except Exception as exc:
        if "planning_occurrence_round_phase_uq" in str(exc):
            log.info("planning 轮次已存在: task=%s round=%s", task["id"], round_key)
            return 0
        if getattr(exc, "code", None) == CONCURRENCY_ERRCODE                 or "task no longer active" in str(exc)                 or "task definition changed during generation" in str(exc):
            # 固定轮生成外层随后会推进 refresh_generated_through。定义漂移
            # 不能作为「0 个新行」返回，否则游标跳过尚未出生的事件。
            raise ConcurrencyRejected(
                "concurrent_modified", "规划任务定义在生成期间变化，本轮稍后重试", 409,
            ) from exc
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


def _following_fixed_event(
    task: dict[str, Any], day: date, due: datetime,
    configured: time, transition: BoundaryTransition | None,
    absorbed: frozenset[date],
) -> datetime | None:
    """规则序列中**晚于本轮事件**的下一个事件（生成时冻结死亡边界用）。

    ``_fixed_rounds`` 只枚举到 now——本轮若位于序列末尾，其死亡边界在将来
    尚未入列，须按同一规则向前多看一个事件。与 ``_fixed_rounds`` 共用
    ``_should_occur`` 与周期边界映射，不复制第二套 recurrence 数学；
    fixed_interval 轴步长恒定，下一事件即 ``due + interval``。规则集合非空
    由任务校验保证，逐日上限仅为防御。返回 None 表示无法确定（异常定义，
    调用方按无冻结边界落库）。
    """
    mode = task.get("refresh_mode")
    if mode == "fixed_interval":
        interval = task.get("interval_days")
        if not isinstance(interval, int) or interval < 1:
            return None
        return due + timedelta(days=interval)
    next_day = day + timedelta(days=1)
    for _ in range(400):
        if next_day not in absorbed and _should_occur(task, next_day):
            boundary = cycle_start_boundary(next_day, configured, transition)
            candidate = PlanningCycle.for_key(next_day, boundary).start
            if candidate > due:
                return candidate
        next_day += timedelta(days=1)
    return None


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


def _fixed_death_boundary(occ: dict[str, Any]) -> datetime | None:
    """该固定轮次生成时冻结的到期死亡边界（§8.4 的唯一 fixed 生命周期权威）。

    唯一来源是实例行生成时随行冻结的 ``fixed_expires_at``（20260928010000，
    三轮 Review 裁决）——**不得**再从 task 当前 interval/weekdays/month_days
    重算历史：规则编辑只影响未来未生成实例，已生成轮次按生成时的轴活完
    生命周期。返回 None 表示边界不存在或未冻结：非 schedule 轴轮（提前完成
    的额外完成记录无 ``fixed_due_at``）、非固定型行、存量行（NULL，不按当前
    规则回算）。「边界是否已成立」（冻结值 ≤ now）由调用方按各自语义判定
    ——固定到期沿用「到达即死」的 ≤ 语义，窗口 sweep 沿用严格越过语义。
    产品门控（固定型模式、refresh_enabled 暂停、request_state、is_active）
    同样由调用方决定。供固定到期清理与窗口 sweep 两个关闭入口共用，不得
    复制第二套裁决。
    """
    if occ.get("source") != "schedule" or not occ.get("fixed_due_at"):
        return None  # early rounds are extra completions, not axis rounds
    expires = occ.get("fixed_expires_at")
    if not expires:
        return None  # 存量行 / 非固定行：不按当前规则回算历史
    return _parse_dt(expires, "fixed_expires_at")


def _expire_fixed_rounds(client, task: dict[str, Any], now: datetime) -> int:
    """A fixed round dies at its frozen next-rule-event boundary, regardless of
    handling history.

    双死亡边界裁决（批次 5 Review HIGH）：该轮若同时携带已成立的窗口最晚
    完成边界（``window_end_at`` 早于本固定到期边界；固定边界沿用「到达即
    死」的 ≤ now 语义，故更早的窗口边界必然也已按 sweep 的严格越过语义成
    立），``closed_at`` 取两者更早者——业务死亡时刻由生成时冻结的事实决定，
    不由两个关闭入口（固定到期 vs 窗口 sweep）的执行顺序、generation 是否
    临时失败或 task 规则后续编辑决定；窗口边界尚未越过（未成立）时不影响
    固定到期。窗口 sweep 侧的同一裁决见 ``sweep_timeouts``（共用
    ``_fixed_death_boundary``）。
    """
    open_rows = _task_open_rows(client, task["id"])
    expired = 0
    expired_rounds: set[str] = set()
    for occ in open_rows:
        round_key = occ.get("round_key")
        if not round_key or round_key in expired_rounds:
            continue
        deadline = _fixed_death_boundary(occ)
        if deadline is None or deadline > now:
            continue  # 边界未冻结（存量行）或尚未到达（下一规则点未到）
        end_at = occ.get("window_end_at")
        if end_at:
            window_end = _parse_dt(end_at, "window_end_at")
            if window_end < deadline:
                deadline = window_end
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
                # 首轮结算（创建时刻落库的游标，R3 审查修复）：today ==
                # first_cycle 且游标已覆盖首轮周期 → 已结算跳过，模板编辑 /
                # 暂停恢复 / 重复维护不复活；游标为空 = 首轮 DUE——即时生成
                # 的暂时失败按候选解析恢复（R3-A），不因时间流逝被重判为
                # 跳过。非首轮周期照常生成（§7.1.1）。
                settled_through = task.get("refresh_generated_through")
                first_round_settled = (
                    today == first_cycle and settled_through is not None
                    and _parse_date(settled_through, "refresh_generated_through")
                    >= first_cycle
                )
                if not first_round_settled:
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
            # 批次 5 三轮 Review HIGH 1：固定到期清理与新轮创建**解耦**——
            # 旧轮生命周期结束不依赖下一轮 INSERT 成功，下一轮暂时缺失时
            # 故障解除后按既有补生成语义恢复。清理先于创建执行。
            # 到期清理只受 refresh_enabled 控制（暂停即冻结，需求 24）——
            # legacy 门禁只禁止新生成，不影响存量轮次的到期死亡。
            timed_out += _expire_fixed_rounds(client, task, now)
            if not legacy_definition:
                through = task.get("refresh_generated_through")
                checked = _parse_date(through, "refresh_generated_through") if through else None
                # 五轮 Review HIGH：进入收尾清理前**先冻结**是否已存在原始
                # 生成异常——不得在 cleanup 自己的 except 内用 sys.exc_info()
                # 推断（它只看得到 cleanup 异常自身，无法区分双重失败与
                # cleanup 单独失败）。
                generation_error: Exception | None = None
                try:
                    for day, due in events:
                        if checked is not None and day <= checked:
                            continue
                        # 生成该轮时按**当时**规则序列冻结它自己的死亡边界：
                        # 序列中晚于本轮 due 的下一个事件；本轮位于序列末尾时
                        # 其边界在将来，按同一规则向前多看一个事件。一次 reconcile
                        # 补生成多个历史轮次时每轮各取自己的下一个事件，不按
                        # 扫描时刻推算；规则后续编辑不得改写该冻结值（§28.1）。
                        expires = next(
                            (event_due for _, event_due in events if event_due > due), None)
                        if expires is None:
                            expires = _following_fixed_event(
                                task, day, due, configured, transition, absorbed)
                        # A fixed-interval round is born in the cycle that generates
                        # it; calendar rounds keep their own cycle date as identity.
                        schedule = today if mode == "fixed_interval" else day
                        # 首轮已截止的跳过在创建时刻一次性结算（游标随任务行
                        # 落库，R3 审查修复）：本循环只按游标跳过（day <=
                        # checked），不按当下时间重判——即时生成的暂时失败
                        # 保留恢复资格（R3-A），模板编辑 / 暂停恢复 / 重复
                        # 维护不复活被跳过的首轮（R3-B）。创建后各轮按候选
                        # 解析冻结窗口（§6.7 正常轮次语义）。
                        created += _create_occurrences(
                            client, task, schedule, now, due_at=due,
                            display_cycle_date=today,
                            fixed_expires_at=expires)
                        cursor_update = client.table("planning_task").update({
                            "refresh_generated_through": day.isoformat(), "updated_at": _iso(now),
                        }).eq("id", task["id"])
                        old_updated_at = task.get("updated_at")
                        cursor_update = (cursor_update.eq("updated_at", old_updated_at)
                                         if old_updated_at is not None else
                                         cursor_update.is_("updated_at", None))
                        old_cursor = task.get("refresh_generated_through")
                        cursor_update = (cursor_update.eq("refresh_generated_through", old_cursor)
                                         if old_cursor is not None else
                                         cursor_update.is_("refresh_generated_through", None))
                        if not cursor_update.execute().data:
                            # 插入已先提交，可由唯一键幂等重放；并发编辑已更改
                            # 任务定义/游标，本次旧生成器不能覆盖新游标。
                            raise ConcurrencyRejected(
                                "concurrent_modified", "规划任务在生成期间变化，游标未推进", 409,
                            )
                        task["refresh_generated_through"] = day.isoformat()
                        task["updated_at"] = _iso(now)
                except Exception as exc:
                    generation_error = exc
                    raise
                finally:
                    # 批次 5 三轮/四轮 Review HIGH 1 + 五轮 Review HIGH：**任何**
                    # 已成功写入数据库的轮次（含本轮中途失败前已创建的部分）
                    # 都必须在本轮 reconcile 退出前经过同一固定到期清理——后续
                    # INSERT / 游标更新失败不得让已生成的过期轮次漏关（否则其
                    # 在 closed_at 之前仍可被 completed）。清理幂等（已关闭行
                    # 跳过），重复调用不重复计数、不重复终态写入。清理自身失败
                    # 不得静默：
                    # * 无原始异常 → 清理异常正常向上抛出（整体调用失败，后续
                    #   完成 / 提前完成操作不得继续）；
                    # * generation 已失败 → 记录清理失败完整日志，原始异常作为
                    #   主异常继续传播（不覆盖、不吞、不伪装成功）。
                    try:
                        timed_out += _expire_fixed_rounds(client, task, now)
                    except Exception:
                        if generation_error is None:
                            raise
                        log.exception(
                            "planning 固定到期清理失败（原始生成异常继续传播）: task=%s",
                            task["id"])
    _carry_open_rounds(client, task, today, now)
    if legacy_definition:
        log.warning("planning 旧显式任务定义停止生成新轮次: task=%s", task["id"])
    return created, timed_out, events


def generate_due(now: datetime | None = None) -> dict[str, Any]:
    """Generate stable rounds from their own refresh model, never legacy cursors.

    批次 5 四轮 Review HIGH 2：失败隔离在**单 task reconcile 粒度**——单个
    task 的生成失败（含其内部已完成的到期清理）只记录该 task 的错误并继续
    其余任务的生命周期维护，不得终止整个任务循环，也不全局吞掉异常。每个
    失败 task 的错误经 log（完整堆栈）与返回值 ``errors`` 列表（最小形态：
    task_id + 异常类型名）保留可观测性，不伪装成成功；``created`` /
    ``timed_out`` 汇总只计成功完成的任务。
    """
    now = now or _now()
    configured, transition, absorbed = _load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    today = cycle.key
    daily_enabled = get_cycle_settings(now)["daily_refresh_enabled"]
    client = _require_client()
    tasks = _rows(client, "planning_task", lambda q: q.eq("is_active", True))
    created = 0
    timed_out = 0
    errors: list[dict[str, Any]] = []
    for task in tasks:
        try:
            task_created, task_timed_out, _ = _reconcile_task_rounds(
                client, task, cycle, now, configured, transition, absorbed, daily_enabled,
            )
        except Exception as exc:
            log.exception("planning 单任务生成失败: task=%s", task["id"])
            errors.append({"task_id": task["id"], "error": type(exc).__name__})
            continue
        created += task_created
        timed_out += task_timed_out
    if created:
        log.info("planning 生成出现实例: count=%s date=%s", created, today.isoformat())
    result = {"created": created, "timed_out": timed_out, "date": today.isoformat()}
    if errors:
        result["errors"] = errors
    return result


# ── 最晚完成超时判定（窗口换源） ──────────────────────────────────

def sweep_timeouts(now: datetime | None = None) -> dict[str, int]:
    """开放实例越过冻结实例窗口的最晚完成自动标记「已超时」（§18.2、§22.5）。

    判定源是实例行生成时冻结的 ``window_end_at``（唯一超时权威）：状态开放
    （含执行中 in_progress 与 partial）且真实时间**越过**（严格大于，恰等
    不超时）窗口终点仍未合法关闭即打标，``closed_at`` = 窗口终点——业务
    死亡时刻，与固定型槽次死亡同模式，不写扫描执行时刻。超时是异常关闭：
    不写完成 / 结束 / 处理事实，不推进任何刷新基准（§18.2）。无窗口待办
    不因此超时；单次 / 处理后刷新型不因跨周期超时的既有规则不变（本函数
    不读任何日期字段）。旧 deadline 判定源（``is_limited`` + 任务 tod +
    ``schedule_date`` 现算）已随窗口批次退役，不再构成第二超时权威；存量
    限时行仅按序列化兼容读取。

    双死亡边界裁决（批次 5 Review HIGH / 三轮 Review 收口）：固定刷新型
    「到达下一规则点死亡」（``_expire_fixed_rounds``）是另一套独立生命周期；
    同一开放轮次同时存在已成立的固定到期边界与窗口最晚完成边界时，两个
    关闭入口对 ``closed_at`` 的裁决一致——都取两者更早者，且边界同源：固定
    边界唯一来自实例行生成时冻结的 ``fixed_expires_at``（共用
    ``_fixed_death_boundary``），**绝不**从 task 当前 recurrence rule 重算
    历史。maintenance 生成步先于本 sweep 运行且到期清理已与新轮创建解耦；
    generation 整体失败或单任务失败时，本 sweep 对固定型轮次读同一冻结
    边界参与 min，保证 ``closed_at`` 与调用顺序、generation 成败、scanner
    迟到时长、规则编辑无关。门控与到期清理一致（固定型模式、refresh_enabled
    未暂停、请求未被取代、任务启用中——inactive task 的固定边界不成立，
    仅窗口边界生效）；暂停 / 被取代任务的固定边界不参与 min，窗口超时仍
    独立生效。

    查询与分页（批次 5 Review MEDIUM）：查询侧直接过滤 ``status ∈ 开放`` 且
    ``window_end_at < now``（SQL 语义下同时排除 NULL——无窗口 / legacy
    deadline / est_end 行不进入本轮查询、无法占满结果页），按 id 稳定排序、
    显式 ``SWEEP_PAGE_SIZE`` 分页，持续取「当前第一页到期开放行」直到取空；
    已处理行离开开放结果集后下一轮自然前移，不用 offset 分页，不存在处理
    中途结果集缩小导致的跳行，也不依赖服务端默认行数上限。更新带开放状态
    条件（与固定到期清理同形状）：并发下已终态的行不得被二次改写。
    """
    now = now or _now()
    client = _require_client()
    now_iso = _iso(now)
    timed_out = 0
    seen: set[int] = set()
    tasks: dict[int, dict[str, Any]] = {}
    round_closures: dict[tuple[int, str | None, str], list[dict[str, Any]]] = {}
    while True:
        page = _rows(
            client, "planning_occurrence",
            lambda q: q.in_("status", list(OPEN_STATUSES))
            .lt("window_end_at", now_iso).order("id").limit(SWEEP_PAGE_SIZE),
        )
        if not page or all(row["id"] in seen for row in page):
            break  # 取空即完毕；全页停滞（更新未生效的异常情形）防死循环
        missing = {row["task_id"] for row in page} - tasks.keys()
        if missing:
            tasks.update(_task_map(client, missing))
        for occ in page:
            end_at = occ.get("window_end_at")
            if not end_at:
                continue  # 防御：timestamptz 列不产生空值以下的异常行
            closed_at = _parse_dt(end_at, "window_end_at")
            # 双死亡边界裁决：该轮生成时冻结的固定到期边界若已成立（到达
            # 即死的 ≤ 语义）且固定到期机制当前有效（固定型模式、未暂停
            # 刷新、请求未被取代、任务启用中——与 _expire_fixed_rounds 的
            # 门控一致），closed_at 取两者更早者。generation 失败时清理虽
            # 未执行，这里读同一冻结事实防止死亡时刻漂移到较晚的窗口边界；
            # 暂停 / 被取代 / 停用任务的固定边界不成立，不参与 min。
            task = tasks.get(occ["task_id"])
            if (task and task.get("refresh_mode") in _FIXED_EXPIRING_MODES
                    and task.get("refresh_enabled") is not False
                    and task.get("request_state") != "superseded"
                    and task.get("is_active") is not False):
                boundary = _fixed_death_boundary(occ)
                if boundary is not None and boundary <= now and boundary < closed_at:
                    closed_at = boundary
            scanned_window = _iso(_parse_dt(end_at, "window_end_at"))
            key = (occ["task_id"], occ.get("round_key"), _iso(closed_at), scanned_window)
            round_closures.setdefault(key, []).append(occ)
        # 按轮分组提交（最终验收修复问题 4）；最终 Debug（明日可用 BLOCKER 1）
        # 增加**写时复核**：读取与写入之间用户可能修改窗口（陈旧 12:00 不得
        # 关闭已改为 18:00 的实例）或产生生命周期事实——每行 UPDATE 内联
        # ``window_end_at`` 等值 + 状态及生命周期事实与扫描值一致、当前
        # 时间仍超过窗口；未命中的行放弃本次旧扫描结果，且不得让
        # 同轮其它命中行保持半关闭——round 级语句先以 SQL 复核
        # ``window_end_at`` 一致后才关闭，未命中即整轮放弃。
        for (task_id, round_key, closed_at_iso, scanned_window), group in                 round_closures.items():
            if round_key is None:
                for representative in group:
                    query = client.table("planning_occurrence").update({
                        "status": "timeout", "closed_at": closed_at_iso,
                        "updated_at": _iso(now),
                    }).eq("id", representative["id"]).eq(
                        "window_end_at", scanned_window,
                    ).eq("status", representative["status"])
                    query = query.lt("window_end_at", now_iso)
                    for field in LIFECYCLE_FACT_FIELDS:
                        value = representative.get(field)
                        query = query.is_(field, None) if value is None else query.eq(field, value)
                    result = query.execute()
                    timed_out += len(result.data or [])
            else:
                # round 级复核：仅当该轮全部开放阶段的 window_end_at 仍等于
                # 扫描值时才关闭；任一漂移 → SQL 内直接 0 行（整轮放弃）。
                ids = [row["id"] for row in group]
                result = client.table("planning_occurrence").update({
                    "status": "timeout", "closed_at": closed_at_iso,
                    "updated_at": _iso(now),
                }).eq("task_id", task_id).eq("round_key", round_key).in_(
                    "id", ids,
                ).eq("window_end_at", scanned_window).lt("window_end_at", now_iso)
                if len(group) == 1:
                    result = result.eq("status", group[0]["status"])
                    for field in LIFECYCLE_FACT_FIELDS:
                        value = group[0].get(field)
                        result = result.is_(field, None) if value is None else result.eq(field, value)
                else:
                    result = result.in_("status", list(OPEN_STATUSES))
                result = result.execute()
                timed_out += len(result.data or [])
        seen.update(row["id"] for row in page)
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
    最终修复（2026-09-28 问题 5）：可排程实例必须同时满足「状态允许 + 无
    任何生命周期事实」（`_has_lifecycle_fact` 单一定义，与实例编辑门控
    同源）——pending + actual_end / pending + partial_at 等脏状态行不得
    进入重新排程，不复制第二套判断。
    """
    return (
        occ["status"] == "pending"
        and not _has_lifecycle_fact(occ)
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


def _frozen_slot_window_conflict(
    occ: dict[str, Any], task: dict[str, Any], now: datetime,
    ordered: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """尚未开始的固定锚点实例在冻结窗口剩余空间不足时派生排程冲突
    （R2 审查修复；§18.1 / §12.1）。

    固定锚点实例（零自由度窗口预锚定的 rule 固定 / 人工钉住的 manual
    固定）不参与重排，其 est 在生成或钉住时写入——时间流逝使冻结窗口
    的剩余空间装不下完整占用跨度时，原创建 / 钉住时刻的可行性不再成立，
    必须以派生冲突呈现，不得把过去时间包装成可执行的成功排程；固定位置
    语义不变（est 不移动、耗时不变，冲突不落库）。

    豁免（§18.1）：已开始执行（actual_start）、执行中 / partial、已带任何
    生命周期事实（含 deferred）的实例不因剩余窗口缩小被重判冲突（超时另
    由 sweep 处理）——本判定只覆盖「尚未开始且仍开放、未触动」的固定锚点
    行，即本次放开创建拒绝后新出现的剩余不足人口；无最晚完成（only-
    earliest / 无窗口 / 旧 explicit 行）不存在剩余空间约束。中空按 §17.4
    以完整包络在**开始阶段**上报一次，结束阶段不重复——但仅当开始阶段
    仍在开放行集合中实际承担该检查（R8 审查修复）：开始阶段已完成 /
    已关闭（退出开放集合）或已触动（§18.1 豁免、不承担）时无人代为上报，
    尚未开始的结束阶段必须按自身所需时间自行检查剩余空间。
    """
    if not _is_untouched_open(occ):
        return None  # 执行中 / partial / 已开始 / 已延期：豁免剩余不足重判
    if not occ.get("is_fixed") and occ.get("fixed_source") is None:
        return None  # 非固定锚点实例由主循环 / 中空包络预判覆盖
    if occ.get("phase") == "end":
        start_row = _hollow_sibling(occ, ordered, "start")
        if start_row is not None and _is_untouched_open(start_row):
            return None  # 开始阶段仍开放且未触动：整轮剩余不足由它按包络上报一次
        # 开始阶段不在开放集合（已完成 / 已关闭）或已豁免重判：结束阶段
        # 自行检查（R8），否则无人上报剩余不足。
    window = _occurrence_window(occ)
    if window is None or window.end_at is None:
        return None  # 只有最早开始 / 无窗口：没有最晚完成，无剩余空间约束
    if not _has_schedulable_duration_source(occ, task):
        return None
    if occ.get("phase") == "start":
        start_duration = _duration_of(occ, task, "start")
        end_row = _hollow_sibling(occ, ordered, "end")
        envelope = (
            _hollow_movable_envelope_duration(start_duration, task, end_row)
            if end_row is not None else None)
        span = envelope if envelope is not None else start_duration
        quantity = (
            f"中空完整包络 {_format_duration(span)}" if envelope is not None
            else f"预计耗时 {_format_duration(span)}")
    else:
        span = _duration_of(occ, task)
        quantity = f"预计耗时 {_format_duration(span)}"
    if window_feasible(window, now, span):
        return None
    return _window_conflict(occ, window, quantity, after_avoidance=False)


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
            # R2 审查修复（§18.1）：尚未开始且未触动的固定锚点实例（零自由
            # 度预锚定 / 人工钉住）在冻结窗口剩余空间不足时同样派生排程
            # 冲突——本次放开创建拒绝后，创建时合法的零自由度输入会随时间
            # 流逝变成「剩余不足」，固定位置语义保留（est 不移动、不截短
            # 耗时），冲突由读取时派生呈现。已开始 / 执行中 / partial /
            # deferred 等已触动实例豁免重判（§18.1，超时另由 sweep）。
            conflict = _frozen_slot_window_conflict(occ, task, now, ordered)
            if conflict:
                conflicts.append(conflict)
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


# 已知的乐观并发拒绝标识（最终修复问题 6）：仅这两类数据库错误可被
# 重算安全吞掉（skip）；RPC 缺失 / 连接故障 / 非预期约束违反 / 未知错误
# 一律向上传播，使 maintenance / 手动重算失败可见，且不错误清除等待标记。
# 标识 = 迁移内固定的 ERRCODE 'PC001' + 固定消息常量（双保险，不模糊匹配）。
CONCURRENCY_ERRCODE = "PC001"
_CONCURRENCY_REJECTION_MESSAGES = (
    "round is no longer editable",
    "schedule inputs drifted",
    "invalid expected snapshot",
    "round already closed",
)


class ConcurrencyRejected(PlanningError):
    """已知的乐观并发拒绝：锁内生命周期门 / expected snapshot 漂移。"""


def _is_rpc_concurrency_rejection(exc: BaseException) -> bool:
    if getattr(exc, "sqlstate", None) == CONCURRENCY_ERRCODE             or getattr(exc, "code", None) == CONCURRENCY_ERRCODE:
        return True
    message = str(exc)
    return any(const in message for const in _CONCURRENCY_REJECTION_MESSAGES)


def _conditional_lifecycle_update(client, occ: dict[str, Any], patch: dict[str, Any],
                                  current_status: str) -> bool:
    """编辑写入的条件 UPDATE（最终修复问题 1）：状态等值 + 生命周期事实
    集合内联 WHERE——检查与写入之间的并发变化使条件未命中 → 0 行 → 调用方
    以 409 拒绝。返回是否实际写入。"""
    query = client.table("planning_occurrence").update(patch).eq("id", occ["id"])
    query = query.eq("status", current_status)
    for field in LIFECYCLE_FACT_FIELDS:
        query = query.is_(field, None)
    result = query.execute()
    return bool(result.data)


def _recompute_expected_snapshot(occ: dict[str, Any]) -> dict[str, Any]:
    """重算某行的 expected snapshot（最终验收修复问题 3；#6 扩为全部参与
    计算行；#25 补回生命周期事实）：compute_schedule 实际读取并决定「可排 /
    可覆盖 / 窗口」的输入字段——状态（含 in_progress / partial 等冻结槽的
    真实开放状态，#6 前硬编码 pending 会误判参与行漂移）、生命周期事实
    （#25：actual_start 等事实读取后并发补录使可排程谓词失效——旧
    _conditional_schedulable_update 的内联门在批量路径的承接）、所有权
    元组、冻结窗口、既有 est 预态与排序（#13：sort_order 是遍历顺序输入，
    读取后 save_order 改序即旧结果作废，与单行条件 UPDATE 的内联等值守卫
    同源）。NULL 显式参与复核。"""
    snapshot: dict[str, Any] = {
        "id": occ["id"],
        "status": occ.get("status"),
        "window_start_at": occ.get("window_start_at"),
        "window_end_at": occ.get("window_end_at"),
        "est_start": occ.get("est_start"),
        "est_end": occ.get("est_end"),
        "estimated_time_source": occ.get("estimated_time_source"),
        "fixed_source": occ.get("fixed_source"),
        "is_fixed": bool(occ.get("is_fixed")),
        "schedule_managed": bool(occ.get("schedule_managed")),
        "sort_order": occ["sort_order"],
    }
    # #25：生命周期事实字段集合单一来源（LIFECYCLE_FACT_FIELDS），与
    # _has_lifecycle_fact / 实例编辑门控同一集合，不复制第二份字段清单。
    for field in LIFECYCLE_FACT_FIELDS:
        snapshot[field] = occ.get(field)
    return snapshot


def _atomic_round_write(client, target: dict[str, Any], main_patch: dict[str, Any],
                        sibling: dict[str, Any] | None,
                        sibling_patch: dict[str, Any] | None,
                        expected: list[dict[str, Any]] | None = None) -> None:
    """同轮编辑的原子提交（批次 6 二轮 user 批准 RPC；最终修复问题 1/6）。

    * 单行实例 = 条件 UPDATE（状态 + 生命周期事实集合内联 WHERE）——检查
      与写入之间的并发变化使条件未命中 → 409、零写入（编辑路径不携带
      expected：严格门后 minutes 必为请求自洽）；
    * 中空同轮两阶段 = 一次 ``planning_patch_occurrence_round`` RPC——函数
      内完成行锁、round 身份确认、硬白名单、锁内生命周期二次校验（问题 5：
      窗口字段永远严格门）、expected snapshot 复核（最终验收修复问题 3）、
      窗口一致性、两行各自补丁与整体 rollback；
    * 错误分类（最终验收修复问题 6）：仅固定 ERRCODE 'PC001' / 已知拒绝
      消息映射为并发跳过（``ConcurrencyRejected``）；RPC 缺失 / 连接故障 /
      非预期约束违反 / 未知错误一律向上传播。``expected`` 非 None（重算
      路径）时基础设施失败也直接传播——由 maintenance / 手动重算如实失败；
      编辑路径（None）包装为用户可见 503。
    * 调用方必须在进入本函数前完成全部 payload 校验（validation-before-write）。
    """
    if sibling is None or sibling_patch is None:
        if not _conditional_lifecycle_update(client, target, main_patch,
                                             current_status=target["status"]):
            raise ConcurrencyRejected(
                "concurrent_modified",
                "该待办已被并发操作改变，本次编辑未执行，请刷新后重试", 409,
            )
        return
    try:
        client.rpc("planning_patch_occurrence_round", {
            "p_target_id": target["id"],
            "p_sibling_id": sibling["id"],
            "p_target_patch": main_patch,
            "p_sibling_patch": sibling_patch,
            "p_expected": expected,
        }).execute()
    except PlanningError:
        raise
    except Exception as exc:
        if _is_rpc_concurrency_rejection(exc):
            raise ConcurrencyRejected(
                "concurrent_modified",
                "同轮原子编辑因并发状态变化被数据库拒绝，本次编辑未执行", 409,
            ) from exc
        if expected is not None:
            # 重算路径：基础设施失败向上传播——maintenance / 手动重算如实
            # 失败（保留等待标记），绝不伪装成并发跳过（最终修复问题 6）。
            raise
        raise PlanningError(
            "database_unavailable",
            "同轮原子编辑暂时无法完成，请稍后重试", 503,
        ) from exc


def recompute_today(now: datetime | None = None) -> dict[str, Any]:
    """手动 / 自动重算：只更新当天可自动排程实例的预估起止。

    窗口批次（§19.1 原子性）：任一排程冲突 → 本轮整体不持久化，保留最近
    一次成功排程的既有 est 不清空；冲突清单（派生结果，不落库）随响应
    返回，并由 today 看板按同一纯函数读取时派生展示。
    最终修复（问题 1）：写入阶段以条件 UPDATE 复核生命周期事实集合——
    读取后实例被并发完成 / 开始 / 关闭时放弃该行排程结果（静默跳过）。
    最终修复（问题 2）：中空同轮两阶段同时被重排时经原子 RPC 一次提交
    （锁内复核 + 任意失败整体回滚），不存在「A 新时间、B 旧时间」半提交。
    #6（2026-10-02，迁移 20261002040000）：普通行与中空轮的全部写集合在
    **同一事务**内一次提交，expected 快照覆盖全部参与计算行（不只待写行）
    ——任一行漂移整批放弃（updated=0、stale_skipped=待写行数、等待标记
    保留），不留「部分行新排程、部分行旧排程」的混合状态。
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
    pending_rows: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
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
        pending_rows[occ_id] = (occ, patch)
    # 按轮分组：中空同轮两阶段一起经原子 RPC；其余单行条件 UPDATE。
    rounds: dict[tuple[int, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    singles: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for occ, patch in pending_rows.values():
        if occ.get("phase") and occ.get("round_key"):
            rounds.setdefault((occ["task_id"], occ["round_key"]), []).append((occ, patch))
        else:
            singles.append((occ, patch))
    stale_skipped = 0
    updated = 0
    if pending_rows:
        # 整批事务 + 完整计算输入复核（#6，迁移 20261002040000 RPC）：
        # * expected 快照覆盖**全部参与计算行**（不只待写行）——固定槽、其它
        #   行状态 / 排序、成员集合任一在读取后漂移 = 整次计算输入失效；
        # * 普通行与中空轮补丁在同一事务内应用（中空轮复用
        #   planning_patch_occurrence_round 的白名单 / 严格门 / 窗口一致性）；
        # * 任一漂移或失败 → PC001 整批放弃：updated=0、stale_skipped=待写
        #   行数、重算等待标记保留，下一次重算按最新输入重新执行（§19.1：
        #   不留「部分行新排程、部分行旧排程」的混合状态）；
        # * 基础设施失败如实向上传播，不伪装成功（最终修复问题 6）。
        expected = [_recompute_expected_snapshot(row) for row in open_rows]
        singles_payload: list[dict[str, Any]] = []
        rounds_payload: list[dict[str, Any]] = []
        for occ, patch in singles:
            singles_payload.append({"id": occ["id"], **patch})
        for entries in rounds.values():
            if len(entries) >= 2:
                (target, target_patch), (sibling, sibling_patch) = entries[0], entries[1]
                rounds_payload.append({
                    "target_id": target["id"], "sibling_id": sibling["id"],
                    "target_patch": target_patch, "sibling_patch": sibling_patch,
                })
            else:
                occ, patch = entries[0]
                singles_payload.append({"id": occ["id"], **patch})
        try:
            response = client.rpc("planning_apply_recompute_batch", {
                "p_expected": expected,
                "p_singles": singles_payload,
                "p_rounds": rounds_payload,
            }).execute()
        except PlanningError:
            raise
        except Exception as exc:
            if _is_rpc_concurrency_rejection(exc):
                log.info("planning 重算因并发状态变化整批放弃: %s", exc)
                stale_skipped = len(pending_rows)
            else:
                raise
        else:
            updated = len(response.data or [])
    log.info("planning 重算完成: updated=%s stale_skipped=%s date=%s",
             updated, stale_skipped, today.isoformat())
    result = {"updated": updated, "at": _iso(now), "conflicts": []}
    if stale_skipped:
        # 最终 Debug（问题 3）：本次结果不完整，调用方不得据此清掉重算等待
        # 标记；下一次重算按最新输入重新执行。
        result["stale_skipped"] = stale_skipped
    return result


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
    # 批次 6 收尾（A1）：消费身份由数据库原子生成（request_token uuid）。
    # requested_at 来自业务 now，两次业务操作可能捕获同一时间戳 T——它不能
    # 充当请求身份；token 在 RPC 函数体内生成，同一时间戳的两次登记必然
    # 得到不同的消费身份。
    client.rpc("planning_request_recompute", {
        "p_reason": reason[:100],
        "p_requested_at": _iso(now),
    }).execute()


def _request_recompute_quietly(reason: str, now: datetime) -> None:
    """停用 / 废弃成功后的排程请求登记（批次 6 收尾 BUG B）。

    主业务结果（任务已停用、开放实例已关闭）在 RPC 事务内提交成功后，
    ``request_recompute`` 属 post-commit side effect：登记失败不得伪装成
    停用失败（用户已看到废弃成功），但也不得静默假装后续工作完成——记
    警告日志保留可观测性，失败时由用户手动重算 / 后续维护循环兜底
    （与 ``_generate_due_quietly`` 同一 quiet 语义）。
    """
    try:
        request_recompute(reason, now)
    except Exception as exc:
        log.warning(
            "planning 停用后重算请求登记失败（等待手动重算或后续触发兜底）: reason=%s error=%s",
            reason, type(exc).__name__,
        )


def clear_recompute_mark(now: datetime | None = None,
                         expected_request_token: str | None = None) -> None:
    """清除重算等待标记；提供 ``expected_request_token`` 时仅条件清除。

    批次 6 收尾（BUG A → A1 升级）：一次 recompute 只能消费它**开始时**
    捕获的那一版请求。消费身份是数据库原子生成的 ``request_token``——
    requested_at 来自业务 now，两次业务操作可能捕获同一时间戳 T，等值
    条件会把执行期间并发登记的同 T 新请求一并清掉；token 等值条件命中
    0 行，新请求保留给下一轮消费。
    ``expected_request_token`` 为 None（捕获时本无待处理请求）时不清除：
    执行期间到达的新请求同样必须保留。
    """
    now = now or _now()
    if expected_request_token is None:
        return
    client = _require_client()
    client.rpc("planning_clear_recompute_mark", {
        "p_request_token": expected_request_token,
    }).execute()


def get_recompute_state(now: datetime | None = None) -> dict[str, Any]:
    now = now or _now()
    enabled, wait = _auto_recompute_config(now)
    client = _require_client()
    rows = _rows(client, "planning_recompute_state", lambda q: q.eq("id", 1).limit(1))
    requested_at = rows[0].get("requested_at") if rows else None
    request_token = rows[0].get("request_token") if rows else None
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
        # 消费身份（A1）：内部条件清除使用，不进入前端展示契约
        "request_token": request_token,
        "reason": rows[0].get("reason") if rows else None,
        "wait_minutes": wait_minutes,
    }


def trigger_recompute(now: datetime | None = None) -> dict[str, Any]:
    """手动重算：立即执行；仅零冲突（成功）时清空等待标记（§16.2 / §19.1）。

    成功判定只看 conflicts 是否为空：updated=0（无可修改但合法完成）同样
    属于成功；存在冲突则本轮整体未生效，等待标记保留，不新增状态或重试
    机制——后续触发 / 维护循环按既有语义再次执行。
    批次 6 收尾（BUG A → A1 升级）：清除以开始时捕获的 request_token 为
    条件——手动重算执行期间并发产生的新请求（如拖动排序，即使其业务时间
    与捕获值相同）不由本次消费，保留给下一轮。
    """
    now = now or _now()
    request_token = get_recompute_state(now).get("request_token")
    result = recompute_today(now)
    # 最终 Debug（问题 3）：stale_skipped = 本次计算基于旧快照、结果被部分
    # 放弃（如并发 save_order 改序）——并发产生的新重算请求必须保留。
    if not result.get("conflicts") and not result.get("stale_skipped"):
        clear_recompute_mark(now, expected_request_token=request_token)
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


def _add_actual_staleness_guards(query, occ: dict[str, Any], row: dict[str, Any]):
    """actual_minutes 的读派生输入带乐观条件（最终修复问题 4）：由读取值
    （而非本次请求值）参与分钟计算的 actual_start / actual_end，其旧值——
    **包括 NULL**——必须作为等值 / IS NULL 条件内联 WHERE；并发的实际时间
    写入使条件未命中 → 0 行 → 拒绝旧请求，三字段不一致不可能落库。"""
    if "actual_minutes" not in row:
        return query
    for field in ("actual_start", "actual_end"):
        if field in row:
            continue  # 本次请求自带该值：分钟与其自洽，无需守卫
        if occ.get(field):
            query = query.eq(field, occ[field])
        else:
            query = query.is_(field, None)
    return query


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
    if start is not None and end is None and "est_end" in payload:
        # 显式置空结束的半区间（#3）：与上一条互为镜像的中文业务拒绝——
        # 省略 est_end 的请求不受影响（仍按预计耗时自动补终点）；不放开
        # 半区间，也不把领域 ValueError 泄漏成英文技术错误。
        raise PlanningError(
            "invalid_payload",
            "预估开始时间不能脱离结束时间：只修改开始时间时请省略结束时间，"
            "由系统按预计耗时自动补齐", 400,
        )
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


def _hollow_display_patch(occ: dict[str, Any], patch: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """同轮两阶段的展示周期字段（批次 6 一轮 Review BLOCKER 3：写前计算，
    不再单独写库——由调用方并入原子写入）。"""
    if not occ.get("phase_group") or "display_cycle_date" not in patch:
        return None
    return {
        "display_cycle_date": patch["display_cycle_date"],
        "display_reason": patch["display_reason"],
        "updated_at": _iso(now),
    }


# ── 当前实例窗口编辑（§18.3 / §12.1 / §13.2，批次 6 接线） ─────────

def _hollow_sibling_of(client, occ: dict[str, Any]) -> dict[str, Any] | None:
    """同轮另一阶段行（只读查找；缺失返回 None，由调用方决定语义）。"""
    if not occ.get("phase"):
        return None
    sibling_phase = "end" if occ["phase"] == "start" else "start"
    return next(
        (
            row for row in _rows(
                client, "planning_occurrence",
                lambda q: q.eq("task_id", occ["task_id"]).eq("phase", sibling_phase)
                .eq("round_key", occ["round_key"]).eq("phase_group", occ["phase_group"]),
            )
            if row["id"] != occ["id"]
        ),
        None,
    )


def _occurrence_round_rows(client, occ: dict[str, Any]) -> list[dict[str, Any]]:
    """窗口编辑涉及的轮次行：普通实例只有本行；中空实例是同轮两阶段。

    实例窗口是轮次级约束（生成时两阶段随行写入同值，§17.4 包络语义），
    编辑必须对同轮两阶段一致生效，不得留下两阶段窗口分叉的轮次。
    """
    if not occ.get("phase"):
        return [occ]
    sibling = _hollow_sibling_of(client, occ)
    if not sibling:
        raise PlanningError("invalid_round", "中空待办缺少同轮关联阶段", 409)
    return [occ, sibling]


def _occurrence_window_occupancy(rows: list[dict[str, Any]], task: dict[str, Any]) -> timedelta:
    """实例窗口可行性判断的占用跨度（§12.1 / §17.4）：普通 = 有效耗时
    （est 区间事实优先、耗时快照其次，:func:`_duration_of` 单一权威）；
    中空 = 开始 + 等待 + 结束的完整包络（等待读结束阶段行自带的
    ``planned_wait_minutes``，缺失回退任务定义）。"""
    if len(rows) == 1:
        return _duration_of(rows[0], task)
    start_row = next(row for row in rows if row.get("phase") == "start")
    end_row = next(row for row in rows if row.get("phase") == "end")
    wait = end_row.get("planned_wait_minutes")
    if isinstance(wait, bool) or not isinstance(wait, int) or not 1 <= wait <= 1440:
        wait = task.get("hollow_wait_minutes")
    try:
        return hollow_envelope_duration(
            _duration_of(start_row, task, "start"), wait, _duration_of(end_row, task, "end"))
    except ValueError as exc:
        raise PlanningError("invalid_payload", "中空待办的阶段耗时或等待时长无效，无法调整时段", 400) from exc


# 生命周期事实字段（批次 6 最终修复问题 4/5 的「统一生命周期门控」单一
# 定义）：任何开始 / 处理 / 关闭事实的存在都使实例不再是「尚未开始」。
LIFECYCLE_FACT_FIELDS = ("actual_start", "actual_end", "partial_at", "handled_at", "closed_at")
# 尚未开始且仍开放的状态集合（与实例窗口编辑门控一致）。
UNTOUCHED_OPEN_STATUSES = ("pending", "deferred")
# 开放生命周期状态集合（延后等状态流转仍可触达）。
OPEN_ONLY_STATUSES = ("pending", "in_progress", "deferred", "partial")


def _has_lifecycle_fact(row: dict[str, Any]) -> bool:
    """实例是否携带任何开始 / 处理 / 关闭事实（单一判定，禁止复制）。"""
    return any(row.get(field) for field in LIFECYCLE_FACT_FIELDS)


def _is_untouched_open(row: dict[str, Any]) -> bool:
    """实例是否「尚未开始且仍开放」：状态允许 + 无任何生命周期事实。"""
    return row.get("status") in UNTOUCHED_OPEN_STATUSES and not _has_lifecycle_fact(row)


def _window_edit_gate(
    rows: list[dict[str, Any]], task: dict[str, Any],
    action: str = "调整可安排时段",
) -> None:
    """当前实例编辑的统一生命周期门控（2026-09-28 一轮 Review 裁决 4/5；
    最终修复问题 4：预估时间编辑复用同一门控）。

    只允许**尚未开始且仍开放**的实例（`_is_untouched_open`：状态 ∈
    pending/deferred 且无 actual_start / actual_end / partial_at /
    handled_at / closed_at 任何事实）；in_progress、partial、completed、
    timeout 等已开始 / 终态一律拒绝；被取代重排请求（superseded）的任务
    实例同样拒绝。**中空按整轮判断**：同轮任一阶段不再可编辑则整轮
    拒绝。本门控先于 zero-slack 分支执行，钉住不得绕过。
    """
    if task.get("request_state") == "superseded":
        raise PlanningError(
            "invalid_transition", f"该待办来自已被取代的重排请求，不能{action}", 409,
        )
    for row in rows:
        if not _is_untouched_open(row):
            raise PlanningError(
                "invalid_transition",
                f"只有尚未开始且开放的待办可以{action}；执行中、部分完成或已关闭的记录是历史事实", 422,
            )


def _occurrence_window_edit(
    client, occ: dict[str, Any], task: dict[str, Any], payload: dict[str, Any], now: datetime,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """当前实例窗口编辑：绝对时间收窄 / 平移 / 改单边 / 钉住（§18.3，批次 6）。

    **纯校验 + 补丁计算，零数据库写入**（2026-09-28 一轮 Review BLOCKER 3：
    调用方在全部 payload 校验完成后统一原子写入）。语义与校验：

    * 只修改当前轮的实例窗口约束——**绝不**回写任务模板；模板编辑走任务
      PATCH（未来轮次），两条路径无任何同步（§18.3 / §28）；
    * 生命周期门控（:func:`_window_edit_gate`）先于一切形状 / 可行性判断，
      zero-slack 钉住不得绕过（裁决 4/5：仅尚未开始的开放实例；中空整轮
      共同判断）；
    * 中空轮次对同轮两阶段一致生效（轮次级约束，§17.4）；
    * 双端时终点晚于起点（绝对瞬间域比较）；禁止跨越每日刷新 boundary
      （端点接触合法，批次 1 ``window_at_crosses_boundary`` 同源）；有最晚
      完成时剩余空间须容纳占用跨度——与创建校验 / 排程冲突共用
      ``window_feasible``（§4.1 单一领域逻辑，§12.1 剩余空间按编辑时刻）；
    * **不允许清空既有窗口**（裁决 6）：已带任一窗口约束的当前轮不得
      PATCH 成双 NULL——不得通过当前编辑取消这一轮既有的窗口 / 超时
      约束；可收窄、平移、改为单边、零自由度钉住。本来就无窗口的双 NULL
      是 no-op；
    * 不可移动锚点守卫（裁决：门控先于 zero-slack）：重算不会移动的
      est（manual / rule 固定锚点）落在新窗口之外 → 拒绝，不偷偷搬锚点；
    * 收窄至恰等占用跨度（零自由度）→ manual 锚点钉住（§13.2：复用
      ``_manual_estimate_patch`` 完整所有权元组；中空两阶段一起钉住，开始
      阶段锚在窗口起点、结束阶段经等待链在窗口终点收口；已有锚点恰好
      等于窗口本身时保持原所有权，不改写为 manual）；
    * 其余情形 est 不动。

    返回 ``(目标行补丁, 同轮另一阶段补丁 | None)``；两补丁均已含窗口字段。
    """
    rows = _occurrence_round_rows(client, occ)
    _window_edit_gate(rows, task)
    current = rows[0]
    start_raw = (payload["window_start_at"] if "window_start_at" in payload
                 else current.get("window_start_at"))
    end_raw = (payload["window_end_at"] if "window_end_at" in payload
               else current.get("window_end_at"))
    start = _parse_dt(start_raw, "window_start_at") if start_raw else None
    end = _parse_dt(end_raw, "window_end_at") if end_raw else None
    # §28.3（2026-10-01）：无日期单次常驻显示、不设时间窗口——不能通过
    # 当前实例编辑为无日期常驻实例新增窗口端，借编辑引入截止会改变常驻
    # 语义；任务级模板对已生成 once 一律锁定，本守卫封住实例级旁路。
    if (task.get("task_type") == "once" and not task.get("target_date")
            and (start is not None or end is not None)):
        raise PlanningError(
            "invalid_payload",
            "未指定日期的单次待办常驻显示、不设可安排时段：不能为它的当前实例新增时间窗口", 400,
        )
    start_abs = start.astimezone(timezone.utc) if start else None
    end_abs = end.astimezone(timezone.utc) if end else None
    if start_abs and end_abs and end_abs <= start_abs:
        raise PlanningError("invalid_payload", "可安排时段的结束必须晚于开始", 400)
    if start_abs and end_abs:
        boundary, _, _ = _load_boundary_state(now)
        if window_at_crosses_boundary(start, end, boundary):
            raise PlanningError(
                "invalid_payload",
                f"可安排时段不能跨越每日刷新时间 {boundary.strftime('%H:%M')}，请调整时段", 400,
            )
    # 裁决 6：不允许通过当前编辑整轮清空既有窗口约束（单边保留合法）。
    had_window = any(row.get("window_start_at") or row.get("window_end_at") for row in rows)
    if had_window and start is None and end is None:
        raise PlanningError(
            "invalid_payload",
            "不能清空已生成待办的既有可安排时段约束；可以收窄、平移或改为单边时段", 400,
        )
    occupancy = _occurrence_window_occupancy(rows, task)
    if end_abs and not window_feasible(
            ResolvedWindow(start_at=start, end_at=end), now, occupancy):
        raise PlanningError(
            "invalid_payload",
            f"可安排时段剩余空间不足以容纳执行耗时 {_format_duration(occupancy)}，请调整时段或耗时", 400,
        )
    zero_freedom = start_abs is not None and end_abs is not None and (end_abs - start_abs) == occupancy
    # 不可移动锚点守卫：先于 zero-slack 分支（裁决：不得借钉住搬运既有
    # 固定 est；锚点在新窗口外一律拒绝）。
    anchored_equal_window = False
    for row in rows:
        slot = _slot_range(row)
        if slot is None or _freely_schedulable(row, task):
            continue
        slot_start, slot_end = slot
        slot_start_abs = slot_start.astimezone(timezone.utc)
        slot_end_abs = slot_end.astimezone(timezone.utc)
        inside = ((start_abs is None or slot_start_abs >= start_abs)
                  and (end_abs is None or slot_end_abs <= end_abs))
        if not inside:
            raise PlanningError(
                "invalid_payload",
                "该待办已有固定的预估时间在新的可安排时段之外，请先调整预估时间或扩大时段", 400,
            )
        if zero_freedom and slot_start_abs == start_abs and slot_end_abs == end_abs:
            anchored_equal_window = True  # 锚点已是唯一合法位置：保持原所有权
    patch: dict[str, Any] = {
        "window_start_at": _iso(start) if start else None,
        "window_end_at": _iso(end) if end else None,
    }
    sibling_row = rows[1] if len(rows) > 1 else None
    if zero_freedom and not anchored_equal_window:
        # 零自由度窗口 → 钉住（§13.2）：est = 窗口本身，manual 所有权元组。
        # 中空两阶段一起钉住：开始阶段锚在窗口起点，结束阶段经等待链在窗口
        # 终点收口（开始 + 等待 + 结束 = 窗口长，位置由此唯一确定）。
        if sibling_row is None:
            patch.update(_manual_estimate_patch(
                occ, task, {"est_start": _iso(start), "est_end": _iso(end)}, now))
        else:
            start_row = next(row for row in rows if row.get("phase") == "start")
            end_row = next(row for row in rows if row.get("phase") == "end")
            start_duration = _duration_of(start_row, task, "start")
            end_duration = _duration_of(end_row, task, "end")
            phase_patches = {
                start_row["id"]: _manual_estimate_patch(
                    start_row, task,
                    {"est_start": _iso(start), "est_end": _iso(start + start_duration)}, now),
                end_row["id"]: _manual_estimate_patch(
                    end_row, task,
                    {"est_start": _iso(end - end_duration), "est_end": _iso(end)}, now),
            }
            for phase_patch in phase_patches.values():
                phase_patch["window_start_at"] = patch["window_start_at"]
                phase_patch["window_end_at"] = patch["window_end_at"]
                phase_patch["updated_at"] = _iso(now)
            main_patch = dict(phase_patches[occ["id"]])
            sibling_patch = (
                dict(phase_patches[sibling_row["id"]]) if sibling_row["id"] in phase_patches
                else {**patch, "updated_at": _iso(now)})
            return main_patch, sibling_patch
    elif sibling_row is not None:
        sibling_patch = {**patch, "updated_at": _iso(now)}
        patch["updated_at"] = _iso(now)
        return patch, sibling_patch
    patch["updated_at"] = _iso(now)
    return patch, None


def _reschedule_occurrence(
    occ: dict[str, Any], task: dict[str, Any], new_start: datetime, now: datetime,
) -> dict[str, Any]:
    """Manual arrangement changes display/time, never the business round."""
    return _manual_estimate_patch(occ, task, {"est_start": _iso(new_start)}, now)


def _shift_sibling_phase(
    client, occ: dict[str, Any], task: dict[str, Any], old_start: datetime | None,
    new_start: datetime, now: datetime,
) -> dict[str, Any] | None:
    """中空待办单阶段被延后 / 手动改时间时，另一阶段按相同时间差平移，
    避免两阶段日期倒挂；结束阶段的精确锚定随后由重算完成。

    批次 6 一轮 Review BLOCKER 3：改为**纯补丁计算**（零数据库写入）——
    返回关联阶段的写入字段，由调用方并入写前完整校验后的原子写入。
    返回 None 表示无需平移。"""
    if not occ.get("phase"):
        return None
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
        return None
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
        return None
    shifted_start = old_sibling_start + delta
    shifted_end = (old_sibling_end + delta if old_sibling_end
                   else shifted_start + _duration_of(sibling, task, sibling_phase))
    patch = _estimate_patch(shifted_start, shifted_end, source="automatic")
    patch["updated_at"] = _iso(now)
    return patch


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


def _discard_task_atomically(client, task_id: int, now: datetime,
                             target_id: int | None = None,
                             target_patch: dict[str, Any] | None = None) -> None:
    """废弃整个任务 = 跨 task + occurrence 的原子命令（最终验收修复问题 5）：
    `planning_discard_task` 在单个数据库事务内锁任务行 → 单语句关闭全部
    开放 occurrence（中空同轮两阶段同语句命中）→ 单语句停用任务；任一
    失败整体回滚。仅已知的并发停用拒绝映射为 409；数据库故障向上传播。"""
    try:
        client.rpc("planning_discard_task", {
            "p_task_id": task_id, "p_now": _iso(now),
            "p_target_id": target_id,
            "p_target_patch": target_patch,
        }).execute()
    except PlanningError:
        raise
    except Exception as exc:
        if getattr(exc, "code", None) == CONCURRENCY_ERRCODE                 or "task already inactive" in str(exc):
            raise PlanningError(
                "concurrent_modified",
                "该待办已被并发操作废弃，本次操作未执行", 409,
            ) from exc
        # 基础设施失败（连接 / 非预期约束 / RPC 缺失）必须用户可见，不伪装成功。
        raise PlanningError(
            "database_unavailable",
            "停用待办暂时无法完成，请稍后重试", 503,
        ) from exc


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

    # 重复型任务的「废弃」= 整个待办不再执行（需求 4d）。判定前移——
    # 废弃命令（跨 task + occurrence）在主写入前以原子 RPC 执行
    #（最终验收修复问题 5）。
    discarding_whole_task = (
        target == "discarded"
        and task["task_type"] in REPEATING_TASK_TYPES
        and task.get("is_active")
        and current not in CLOSED_STATUSES
    )

    row: dict[str, Any] = {"status": target, "updated_at": _iso(now)}
    # 中空同轮两阶段联动（最终修复问题 2）：延后等带时间的开放状态流转会
    # 同时修改同轮兄弟行（est 平移 + 展示一致）——两行补丁统一经原子 RPC
    # 提交，禁止「update A; update B」顺序写；终态流转只写目标行（单行
    # 条件 UPDATE 本身原子），不经 RPC（白名单永不携带终态）。
    sibling: dict[str, Any] | None = None
    sibling_row: dict[str, Any] | None = None

    if target == "partial":
        note = _clean_text(payload.get("partial_note"), "partial_note", required=True, maximum=MAX_NOTE_LENGTH)
        row["partial_note"] = note
        row["partial_at"] = _iso(now)
        # Partial work is a fact inside an open lifecycle, never a baseline.
        row["handled_at"] = None
    elif target in ("pending", "in_progress", "deferred"):
        if current in CLOSED_STATUSES or current == "timeout":
            row["partial_note"] = None

    if new_start and not discarding_whole_task:
        # 废弃整个任务（不再执行）与「指定新执行时间」矛盾：废弃路径不做
        # 时间平移（原子命令覆盖全部开放实例）。
        row.update(_reschedule_occurrence(occ, task, new_start, now))
        shift_patch = _shift_sibling_phase(
            client, occ, task,
            _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
            new_start, now,
        )
        display_patch = _hollow_display_patch(occ, row, now)
        if display_patch:
            row.update(display_patch)
        if shift_patch is not None:
            sibling = _hollow_sibling_of(client, occ)
            sibling_row = {**shift_patch}
            if display_patch:
                sibling_row.update(display_patch)

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

    if discarding_whole_task:
        # 最终 Debug（问题 1B）：废弃整个任务 = 单事务命令。目标行的关闭
        # 事实（actual_end / actual_minutes / actual_start——废弃命令成功
        # 必须产生的结果）作为 RPC 输入在同一事务内写入；RPC commit 后
        # 不再有事务外补写（fact 更新失败 = 整个废弃回滚，task 不会已被
        # 停用）。closed_at / status 由 RPC 的批量关闭覆盖。
        target_facts = {key: row[key] for key in
                        ("actual_start", "actual_end", "actual_minutes")
                        if key in row}
        target_facts["updated_at"] = _iso(now)
        _discard_task_atomically(client, task["id"], now,
                                 target_id=occ["id"], target_patch=target_facts)
        # 批次 6 收尾（BUG B）：整任务废弃提前 return，绕过了下方 closing
        # 分支——废弃释放的时间槽必须由一次重算重新分配；登记为 post-commit
        # side effect，失败不伪装成废弃失败（quiet，可观测日志兜底）。
        _request_recompute_quietly("task_discarded", now)
        task = {**task, "is_active": False}
        refreshed = _fetch_occurrence(client, occurrence_id) or {**occ, **row}
        return serialize_occurrence(refreshed, task, now)

    if sibling_row is not None:
        # 最终修复（问题 2）：中空同轮两阶段（目标行状态流转 + 兄弟行时间
        # 联动 + 展示一致）经原子 RPC 一次提交；注入失败两行整体回滚。
        # RPC 锁内以宽松门（开放且无关闭事实）复核生命周期——延后自
        # in_progress / partial 仍合法（既有语义），并发完成 / 关闭则拒绝。
        _atomic_round_write(client, occ, row, sibling, sibling_row)
    else:
        # 单行流转：条件 UPDATE（状态等值条件 + actual_minutes 读派生输入
        # 的等值 / IS NULL 条件内联——乐观并发：并发状态或实际时间变化使
        # 条件未命中 → 409、零写入）。
        query = client.table("planning_occurrence").update(
            row).eq("id", occurrence_id).eq(
            "status", "discarded" if discarding_whole_task else current)
        query = _add_actual_staleness_guards(query, occ, row)
        result = query.execute()
        if not result.data:
            raise PlanningError(
                "concurrent_modified",
                "该待办已被并发操作改变，状态修改未执行，请刷新后重试", 409,
            )

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
    """手动编辑 / 兜底：手动改预估起止、调整当前实例窗口（§18.3）、补填或
    修改实际起止、部分完成说明。

    批次 6 一轮 Review BLOCKER 3：**写前完整校验**——payload 的全部字段
    （窗口 / 生命周期门控 / partial_note / 实际时间 / 混合字段限制 / 中空
    轮次一致性 / 固定锚点 / 可行性）都在第一个数据库写入之前校验完成；
    之后中空同轮多行修改经 :func:`_atomic_round_write` 单语句原子提交，
    任何失败下两行都保持修改前状态（无半写）。
    """
    now = now or _now()
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "request body must be a JSON object")
    allowed = {"est_start", "est_end", "actual_start", "actual_end", "partial_note", "is_fixed",
               "window_start_at", "window_end_at"}
    unknown = set(payload) - allowed
    if unknown:
        raise PlanningError("invalid_payload", f"unsupported fields: {', '.join(sorted(unknown))}")
    if not payload:
        raise PlanningError("invalid_payload", "no writable fields supplied")
    if "is_fixed" in payload:
        _clean_bool(payload["is_fixed"], "is_fixed")
    window_edited = any(field in payload for field in ("window_start_at", "window_end_at"))
    if window_edited and any(field in payload for field in ("est_start", "est_end", "is_fixed")):
        # 两条编辑路径语义不同（实例窗口 = 排程约束；est = 排程结果 / 人工
        # 锚点），不提供同请求混合语义（校验先行，零写入）。
        raise PlanningError(
            "invalid_payload", "可安排时段与预估时间不能在同一次请求中同时修改", 400,
        )

    client = _require_client()
    occ = _fetch_occurrence(client, occurrence_id)
    if not occ:
        raise PlanningError("not_found", "planning occurrence not found", 404)
    if not occ.get("round_key"):
        raise PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = _fetch_task(client, occ["task_id"])
    if not task:
        raise PlanningError("not_found", "planning task not found", 404)

    # ── 校验与补丁计算（零写入） ──────────────────────────────────
    main_row: dict[str, Any] = {"updated_at": _iso(now)}
    sibling: dict[str, Any] | None = None
    sibling_row: dict[str, Any] | None = None
    est_edited = any(field in payload for field in ("est_start", "est_end", "is_fixed"))
    if est_edited:
        # 最终修复（问题 4）：预估时间编辑复用统一生命周期门控（与窗口编辑
        # 同一谓词）——已发生事实实例（completed / timeout / in_progress /
        # partial）不得重新排程；实际时间 / 说明的事实修正走下方专用路径、
        # 不受此限（§23 历史修正）。中空按整轮判断（平移会触及同轮兄弟行）。
        _window_edit_gate(_occurrence_round_rows(client, occ), task, action="修改预估时间")
        main_row.update(_manual_estimate_patch(occ, task, payload, now))
    if window_edited:
        window_patch, sibling_window_patch = _occurrence_window_edit(
            client, occ, task, payload, now)
        main_row.update(window_patch)
        if sibling_window_patch is not None:
            sibling = _occurrence_round_rows(client, occ)[1]
            sibling_row = {**sibling_window_patch}
    if "est_start" in payload and payload["est_start"]:
        shift_patch = _shift_sibling_phase(
            client, occ, task,
            _parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
            _parse_dt(main_row["est_start"], "est_start"), now,
        )
        display_patch = _hollow_display_patch(occ, main_row, now)
        if shift_patch is not None:
            if display_patch:
                shift_patch.update(display_patch)
            if sibling_row is not None:
                sibling_row.update(shift_patch)
            else:
                sibling = _hollow_sibling_of(client, occ)
                sibling_row = shift_patch
        elif display_patch:
            # 无需平移但展示周期变化：中空同轮两阶段展示字段一致生效。
            if sibling_row is not None:
                sibling_row.update(display_patch)
                main_row.update(display_patch)
            elif occ.get("phase"):
                sibling = _hollow_sibling_of(client, occ)
                sibling_row = display_patch
    if "partial_note" in payload:
        main_row["partial_note"] = _clean_text(
            payload.get("partial_note"), "partial_note", required=False, maximum=MAX_NOTE_LENGTH,
        )
    if "actual_start" in payload:
        main_row["actual_start"] = _iso(_parse_dt(payload["actual_start"], "actual_start")) if payload["actual_start"] else None
    if "actual_end" in payload:
        main_row["actual_end"] = _iso(_parse_dt(payload["actual_end"], "actual_end")) if payload["actual_end"] else None
    if "actual_start" in main_row or "actual_end" in main_row:
        main_row["actual_minutes"] = _compute_actual_minutes({**occ, **main_row})

    # ── 写入阶段（全部校验已通过） ────────────────────────────────
    if est_edited or window_edited:
        # 窗口 / 预估编辑：单行 = 条件 UPDATE（生命周期门控条件内联——
        # 最终修复问题 1，普通单行写不绕过锁内保护）；中空同轮两阶段统一
        # 经 _atomic_round_write 的 RPC 原子完成——不存在「同轮两行顺序写」
        # 路径（批次 6 二轮 十一）。
        _atomic_round_write(client, occ, main_row, sibling, sibling_row)
        if window_edited:
            # 窗口是排程约束：约束变化后可重排实例的 est 由下一次重算在窗口内
            # 重新派生（§16.2「其他明确要求重新排程的状态变化」；钉住实例的
            # est 已随编辑确定，重算把其视为固定槽，行为不变）。
            request_recompute("occurrence_window_edit", now)
    else:
        # 实际时间 / 说明的事实修正（§23 历史修正，允许发生于已发生事实
        # 实例）：单行乐观条件写——actual_minutes 的读派生输入（含 NULL
        # 旧值）携带等值 / IS NULL 条件，并发修改使条件未命中 → 409、
        # 零写入（最终修复问题 6 / 4）。
        query = client.table("planning_occurrence").update(
            main_row).eq("id", occurrence_id)
        query = _add_actual_staleness_guards(query, occ, main_row)
        result = query.execute()
        if not result.data:
            raise PlanningError(
                "concurrent_modified",
                "该待办的实际时间已被并发修改，本次补填未执行，请刷新后重试", 409,
            )
    refreshed = _fetch_occurrence(client, occurrence_id) or {**occ, **main_row}
    return serialize_occurrence(refreshed, task, now)


def split_occurrence(occurrence_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """拆分待办：结束当前这一轮，并把剩余工作拆成 1～10 个新的单次待办。

    拆分是可选辅助功能（partial → 已全部完成才是主流程），业务语义属于
    「本轮已经处理结束」：当前轮以「此次不执行」同级语义合法关闭
    （discarded_this + handled_at 拆分处理时间），处理后刷新型从该处理
    时间推进下一轮，固定刷新型时间轴不变，原任务定义继续正常存在。已有
    partial 说明 / 时间与实际执行事实原样保留（不为记录"已拆分为 N 个
    待办"覆盖用户事实）。只允许开放实例拆分；已关闭实例（含被并发拆分
    收口的）拒绝再次拆分，重复请求不会产生第二组拆分任务。

    #1（2026-10-02，迁移 20261002030000）：关闭原轮、创建 1～10 个单次
    待办与 after_completion 基准推进在同一数据库事务内原子完成——任一
    失败整体回滚（原轮保持开放、零任务创建，重试可完整重放），不再留下
    「原轮已关闭 + 部分任务」的不可重试半状态。
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

    # 原子拆分（#1，迁移 20261002030000 RPC）：条件关闭、1～10 个单次待办
    # 创建与 after_completion 基准推进在**同一数据库事务**内完成，任一失败
    # 整体回滚——原轮保持开放、零任务创建，重试可完整重放。此前关闭先落库、
    # 中途 INSERT 失败会留下「原轮已关闭 + 部分任务」的不可重试半状态。
    # 锁内基准推进（语句 6）与旧「关闭后读行再写」同语义：全部轮次行均有
    # handled_at 时按 max(handled_at) 推进，基准缺失时 _after_completion_due
    # 仍可从轮次行自愈。
    after_completion_days = None
    if (task["task_type"] == "interval"
            and task.get("refresh_mode") == "after_completion"
            and task.get("is_active")):
        interval = task.get("interval_days")
        if isinstance(interval, int) and 1 <= interval <= 365:
            after_completion_days = interval
    try:
        response = client.rpc("planning_split_occurrence", {
            "p_task_id": task["id"],
            "p_round_key": occ["round_key"],
            "p_target_date": _current_cycle(now).key.isoformat(),
            "p_now": _iso(now),
            "p_parts": [
                {"content": part["content"],
                 "estimated_minutes": part["estimated_minutes"]}
                for part in normalized
            ],
            "p_after_completion_days": after_completion_days,
        }).execute()
    except PlanningError:
        raise
    except Exception as exc:
        if _is_rpc_concurrency_rejection(exc):
            # 0 行命中（已关闭 / 已拆分收口 / 已超时）：并发重复拆分请求
            # 不产生第二组拆分任务（B2 业务兜底，语义与原条件关闭一致）。
            raise PlanningError(
                "invalid_transition", "该待办已被并发操作关闭，不能再次拆分", 409,
            ) from exc
        raise PlanningError(
            "database_unavailable", "拆分暂时无法完成，请稍后重试", 503,
        ) from exc
    created_ids = [int(item) for item in (response.data or [])]

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
    try:
        _, _, events = _reconcile_task_rounds(
            client, task, cycle, now, configured, transition, absorbed, daily_enabled,
        )
    except Exception:
        # 批次 5 七轮 Review MEDIUM：本入口直接调用 reconcile、完全绕过
        # _generate_due_quietly 的共享重算判断——partial create（occurrence
        # INSERT 成功后 cursor 等后续写失败）会留下已持久化但未排程的开放
        # occurrence，后续 maintenance 因 created=0 + no errors 不再触发
        # generation recompute。保守执行一次幂等 recompute 作为**恢复动作**
        # （不改变失败语义、不判断创建数量），然后重新抛出原异常：提前完成
        # 流程停止、不写 completed / handled_at / 完成事实、不创建 early 行。
        # 恢复自身失败不得覆盖原异常：记录完整日志后让原 reconcile 异常继续
        # 传播（与 generation + cleanup 双重失败同一原则）。
        try:
            recompute_today(now)
        except Exception:
            log.exception("planning 提前完成恢复重算失败: task=%s", task_id)
        raise

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
            # 五轮 / 六轮 Review：触发条件与 _generate_due_quietly 共用
            # `_should_recompute_after_generation`（created > 0 或 errors
            # 非空——partial create 的 created 计数会丢失，保守幂等重算）。
            if _should_recompute_after_generation(results["generation"]):
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
            request_token = state.get("request_token")
            if enabled and requested_at and (now - _parse_dt(requested_at, "requested_at")) >= wait:
                auto = recompute_today(now)
                results["auto_recompute"] = auto
                # 窗口批次修复轮（2026-09-28 Review MEDIUM-1）：仅零冲突（成功）
                # 清空等待标记；冲突本轮整体未生效，标记保留，等待条件改变后
                # 由后续维护循环按既有语义再次执行（不新增状态 / 重试机制）。
                # 批次 6 收尾（BUG A → A1 升级）：清除以本次消费的
                # request_token 为条件——执行期间并发写入的新请求（即使其
                # requested_at 与捕获值相同）不被本次清除，保留给下一轮
                # 维护循环消费。
                if not auto.get("conflicts") and not auto.get("stale_skipped"):
                    clear_recompute_mark(now, expected_request_token=request_token)
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
