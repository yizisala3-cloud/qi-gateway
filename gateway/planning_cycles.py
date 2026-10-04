"""Persisted planning cycle settings and atomic boundary transitions.

Domain mathematics remain in planning_domain and planning_window; this service
retains each setting's established missing-value and query-failure semantics."""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Any

from . import db
from .planning_domain import (
    BoundaryTransition,
    DEFAULT_REFRESH_BOUNDARY,
    PlanningCycle,
    parse_refresh_boundary,
    planning_cycle_at,
)
from .planning_window import WindowTemplate, window_crosses_boundary
from . import planning_common as common
from . import planning_runtime as runtime

log = logging.getLogger("gateway.planning")


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
    return _parse_boundary_state(db.load_app_setting(common.PLANNING_BOUNDARY_STATE_KEY), now)


def _parse_boundary_state(raw: Any, now: datetime) -> tuple[time, BoundaryTransition | None, frozenset[date]]:
    """Interpret a stored boundary snapshot using the existing transition rules."""
    if raw is db.APP_SETTING_QUERY_FAILED:
        raise common.PlanningError("database_unavailable", "规划周期配置暂时无法读取", 503)
    if not isinstance(raw, dict):
        raw = _default_boundary_state()
    try:
        boundary = parse_refresh_boundary(
            raw.get("boundary") or DEFAULT_REFRESH_BOUNDARY.strftime("%H:%M")
        )
    except ValueError as exc:
        raise common.PlanningError("invalid_setting", "规划周期刷新时间配置无效", 500) from exc
    transition = None
    planned_absorbed: frozenset[date] = frozenset()
    info = raw.get("transition")
    if isinstance(info, dict):
        try:
            planned = BoundaryTransition.plan(
                date.fromisoformat(info["spanning_key"]),
                parse_refresh_boundary(info["spanning_boundary"]),
                common._parse_dt(info["change_at"], "refresh_boundary_change_at"),
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


def _load_boundary_record_transition() -> BoundaryTransition | None:
    """从持久化边界快照重建过渡对象，无论其当前是否仍在生效（清单 #32 R5）。

    ``_parse_boundary_state`` 只在过渡仍生效（``active_at(now)``）时返回
    transition；过渡真正走完后记录仍持久保留在同一 app_settings 行，但
    不再作为 transition 暴露。每日旧轮的**实际**周期终点重建需要这段
    过渡历史（冻结段的终点 = ``effective_at``，晚于过渡完成时刻的扫描
    仍须还原），本入口按同一 ``BoundaryTransition.plan`` 语义重建，不
    复制第二套解析。快照缺失 / 无过渡记录 / 记录无效一律返回 None
    （与既有「无效记录按无过渡处理」口径一致）；设置读取失败按既有
    三态语义抛 503，不静默降级。
    """
    raw = db.load_app_setting(common.PLANNING_BOUNDARY_STATE_KEY)
    if raw is db.APP_SETTING_QUERY_FAILED:
        raise common.PlanningError("database_unavailable", "规划周期配置暂时无法读取", 503)
    if not isinstance(raw, dict):
        return None
    info = raw.get("transition")
    if not isinstance(info, dict):
        return None
    try:
        return BoundaryTransition.plan(
            date.fromisoformat(info["spanning_key"]),
            parse_refresh_boundary(info["spanning_boundary"]),
            common._parse_dt(info["change_at"], "refresh_boundary_change_at"),
            parse_refresh_boundary(
                raw.get("boundary") or DEFAULT_REFRESH_BOUNDARY.strftime("%H:%M")
            ),
        )
    except (KeyError, TypeError, ValueError):
        log.warning("planning 边界过渡记录无效，历史周期重建按无过渡处理")
        return None


class PlanningRequestContext:
    """Lazy request-local settings, with failures interpreted at their original stage.

    Immediate creation uses only boundary and daily-refresh settings. Loading
    their raw values together must not turn a generation failure into a failed
    task insert; validation only interprets the boundary when it needs it.
    """

    def __init__(self, now: datetime):
        self.now = now
        self._values: dict[str, Any] | None = None
        self._boundary: tuple[time, BoundaryTransition | None, frozenset[date]] | None = None

    def _setting(self, key: str) -> Any:
        if self._values is None:
            self._values = db.load_app_settings((
                common.PLANNING_BOUNDARY_STATE_KEY, common.PLANNING_DAILY_REFRESH_KEY,
            ))
        return self._values.get(key)

    def boundary_state(self) -> tuple[time, BoundaryTransition | None, frozenset[date]]:
        if self._boundary is None:
            self._boundary = _parse_boundary_state(
                self._setting(common.PLANNING_BOUNDARY_STATE_KEY), self.now,
            )
        return self._boundary

    @property
    def cycle(self) -> PlanningCycle:
        configured, transition, _ = self.boundary_state()
        return planning_cycle_at(self.now, configured, transition)

    @property
    def daily_refresh_enabled(self) -> bool:
        value = self._setting(common.PLANNING_DAILY_REFRESH_KEY)
        if value is db.APP_SETTING_QUERY_FAILED:
            raise common.PlanningError("database_unavailable", "每日刷新配置暂时无法读取", 503)
        return value if isinstance(value, bool) else True


def get_cycle_settings(now: datetime | None = None) -> dict[str, Any]:
    """Read the persisted planning boundary, daily switch and recompute config.

    The cycle reflects a pending boundary transition: the frozen spanning
    cycle keeps its original boundary until the newly configured boundary
    first occurs, no matter how many times the boundary is re-configured.
    """
    now = now or runtime._now()
    boundary, transition, _ = _load_boundary_state(now)
    daily_raw = db.load_app_setting(common.PLANNING_DAILY_REFRESH_KEY)
    if daily_raw is db.APP_SETTING_QUERY_FAILED:
        raise common.PlanningError("database_unavailable", "每日刷新配置暂时无法读取", 503)
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
    auto_enabled_raw = db.load_app_setting(common.PLANNING_AUTO_RECOMPUTE_ENABLED_KEY)
    if auto_enabled_raw is db.APP_SETTING_QUERY_FAILED:
        raise common.PlanningError("database_unavailable", "自动重算配置暂时无法读取", 503)
    result["auto_recompute_enabled"] = True if not isinstance(auto_enabled_raw, bool) else auto_enabled_raw
    wait_raw = db.load_app_setting(common.PLANNING_AUTO_RECOMPUTE_WAIT_KEY)
    if wait_raw is db.APP_SETTING_QUERY_FAILED:
        raise common.PlanningError("database_unavailable", "自动重算配置暂时无法读取", 503)
    result["auto_recompute_wait_minutes"] = (
        wait_raw if isinstance(wait_raw, int) and 1 <= wait_raw <= 1440
        else int(common.RECOMPUTE_WAIT.total_seconds() // 60)
    )
    return result


def _load_raw_boundary_state() -> dict[str, Any]:
    """boundary 状态行的原始存储（批次 7）：不做过渡活跃性解释。

    dry-run 预计算、最终保存的 CAS 基准都必须基于同一份原始状态；读取
    失败 503（与 _load_boundary_state 同一语义）。
    """
    raw = db.load_app_setting(common.PLANNING_BOUNDARY_STATE_KEY)
    if raw is db.APP_SETTING_QUERY_FAILED:
        raise common.PlanningError("database_unavailable", "规划周期配置暂时无法读取", 503)
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
        raise common.PlanningError("invalid_payload", "task_adjustments 必须是数组", 400)
    seen: set[int] = set()
    out: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise common.PlanningError("invalid_payload", "task_adjustments 项必须是对象", 400)
        if (set(item) - {"task_id", "window_start_tod", "window_end_tod"}
                or not {"task_id", "window_start_tod", "window_end_tod"} <= set(item)):
            raise common.PlanningError(
                "invalid_payload",
                "task_adjustments 项必须包含 task_id 与完整的双端可安排时段", 400)
        task_id = item["task_id"]
        if isinstance(task_id, bool) or not isinstance(task_id, int):
            raise common.PlanningError("invalid_payload", "task_adjustments 的 task_id 必须是整数", 400)
        if task_id in seen:
            raise common.PlanningError("invalid_payload", "task_adjustments 中存在重复的待办", 400)
        seen.add(task_id)
        try:
            start = (time.fromisoformat(str(item["window_start_tod"]))
                     if item["window_start_tod"] is not None else None)
            end = (time.fromisoformat(str(item["window_end_tod"]))
                   if item["window_end_tod"] is not None else None)
        except (TypeError, ValueError) as exc:
            raise common.PlanningError(
                "invalid_payload",
                "可安排时段的时间格式无效：请使用 HH:MM（例如 09:00）", 400,
            ) from exc
        if start is not None:
            start = start.replace(second=0, microsecond=0)
        if end is not None:
            end = end.replace(second=0, microsecond=0)
        if start is not None and end is not None and start == end:
            raise common.PlanningError(
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
            start = (common._canonical_template_value("window_start_tod", task.get("window_start_tod"))
                     if task.get("window_start_tod") else None)
            end = (common._canonical_template_value("window_end_tod", task.get("window_end_tod"))
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
    client = runtime._require_client()
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
                common._parse_dt(info["change_at"], "refresh_boundary_change_at"),
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
            "change_at": common._iso(planned.change_at),
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
        raise common.PlanningError("database_unavailable", "规划周期配置保存失败", 503) from exc
    data = getattr(resp, "data", None)
    if isinstance(data, dict) and data.get("status") == "stale_state":
        raise common.PlanningError(
            "boundary_state_conflict",
            "规划周期配置已被其他修改更新，请重新加载后再试", 409)
    if isinstance(data, dict) and data.get("status") == "conflicts":
        conflicts = data.get("conflicts") or []
        raise common.PlanningError(
            "boundary_window_conflicts",
            "存在跨越新刷新时间的待办，请先调整其可安排时段", 409,
            details={"conflicts": conflicts})
    if not (isinstance(data, dict) and data.get("status") == "ok"):
        raise common.PlanningError("database_unavailable", "规划周期配置保存失败", 503)
    result = get_cycle_settings(now)
    result["adjusted_tasks"] = int(data.get("updated_tasks") or 0)
    return result


def set_cycle_settings(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """Persist one planning setting; a boundary change is an atomic
    transaction (dry-run precheck + task adjustments + transition state,
    批次 7 §5.2.2) that schedules the next-cycle transition instead of
    reinterpreting the cycle already in progress."""
    now = now or runtime._now()
    allowed = {
        "refresh_boundary_time", "daily_refresh_enabled",
        "auto_recompute_enabled", "auto_recompute_wait_minutes",
        "dry_run", "task_adjustments",
    }
    if not isinstance(payload, dict) or not payload or set(payload) - allowed:
        raise common.PlanningError("invalid_payload", f"一次仅接受以下之一：refresh_boundary_time, daily_refresh_enabled, auto_recompute_enabled, auto_recompute_wait_minutes", 400)
    if "refresh_boundary_time" not in payload and ("dry_run" in payload or "task_adjustments" in payload):
        raise common.PlanningError(
            "invalid_payload", "boundary 试算与关联调整只能与 refresh_boundary_time 一同提交", 400)
    if set(payload) & {"daily_refresh_enabled", "auto_recompute_enabled", "auto_recompute_wait_minutes"} and len(payload) != 1:
        raise common.PlanningError("invalid_payload", f"一次仅接受以下之一：refresh_boundary_time, daily_refresh_enabled, auto_recompute_enabled, auto_recompute_wait_minutes", 400)
    if "refresh_boundary_time" in payload:
        try:
            boundary = parse_refresh_boundary(payload["refresh_boundary_time"])
        except ValueError as exc:
            raise common.PlanningError("invalid_payload", "刷新时间必须是 00:00 至 23:59", 400) from exc
        dry_run = payload.get("dry_run", False)
        if not isinstance(dry_run, bool):
            raise common.PlanningError("invalid_payload", "dry_run 必须是布尔值", 400)
        adjustments_payload = _parse_boundary_adjustments(payload.get("task_adjustments"))
        adjustments = {
            item["task_id"]: (item["window_start_tod"], item["window_end_tod"])
            for item in adjustments_payload
        }
        # dry-run（§5.2.2）：以准备生效的新 boundary 校验全部启用中任务，
        # 绝对零写入；冲突清单（待办 / 现窗口 / 原因）随响应返回。
        tasks = runtime._rows(runtime._require_client(), "planning_task", lambda q: q.eq("is_active", True))
        conflicts = _boundary_window_conflicts(boundary, adjustments, tasks)
        if dry_run:
            return {
                "dry_run": True,
                "boundary_time": boundary.strftime("%H:%M"),
                "conflicts": conflicts,
            }
        if conflicts:
            raise common.PlanningError(
                "boundary_window_conflicts",
                "存在跨越新刷新时间的待办，请先调整其可安排时段", 409,
                details={"conflicts": conflicts})
        raw_state = _load_raw_boundary_state()
        return _save_cycle_boundary(boundary, raw_state, adjustments_payload, now)
    if "daily_refresh_enabled" in payload:
        if not isinstance(payload["daily_refresh_enabled"], bool):
            raise common.PlanningError("invalid_payload", "daily_refresh_enabled 必须是布尔值", 400)
        if not db.save_app_setting(common.PLANNING_DAILY_REFRESH_KEY, payload["daily_refresh_enabled"]):
            raise common.PlanningError("database_unavailable", "每日刷新配置保存失败", 503)
    if "auto_recompute_enabled" in payload:
        if not isinstance(payload["auto_recompute_enabled"], bool):
            raise common.PlanningError("invalid_payload", "auto_recompute_enabled 必须是布尔值", 400)
        if not db.save_app_setting(common.PLANNING_AUTO_RECOMPUTE_ENABLED_KEY, payload["auto_recompute_enabled"]):
            raise common.PlanningError("database_unavailable", "自动重算配置保存失败", 503)
    if "auto_recompute_wait_minutes" in payload:
        value = payload["auto_recompute_wait_minutes"]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1440:
            raise common.PlanningError("invalid_payload", "auto_recompute_wait_minutes 必须是 1 至 1440 的整数", 400)
        if not db.save_app_setting(common.PLANNING_AUTO_RECOMPUTE_WAIT_KEY, value):
            raise common.PlanningError("database_unavailable", "自动重算配置保存失败", 503)
    return get_cycle_settings(now)


def _current_cycle(now: datetime) -> PlanningCycle:
    boundary, transition, _ = _load_boundary_state(now)
    return planning_cycle_at(now, boundary, transition)
