"""Planning round generation, frozen snapshots and timeout reconciliation.

Existing occurrences keep their original identity and window facts. Generation
continues to use guarded database RPCs and isolates failures per task."""
from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .planning_domain import (
    BUSINESS_TIMEZONE,
    BoundaryTransition,
    EstimatedTimeOwnership,
    OccurrenceIdentity,
    PlanningCycle,
    calendar_round_key,
    cycle_start_boundary,
    fixed_round_key,
    planning_cycle_at,
    round_phase_group,
    timed_round_key,
    validate_task_refresh_mode,
)
from .planning_window import ResolvedWindow, resolve_window, resolve_window_on_date
from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_cycles as cycles

log = logging.getLogger("gateway.planning")


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
            common._parse_dt(task["created_at"], "created_at"), configured, transition).key
    target = common._parse_date(task["target_date"], "target_date")
    template = common._task_window_template(task)
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
    template = common._task_window_template(task)
    if template is None:
        return None, None, None
    if task["task_type"] == "once":
        resolved = resolve_window_on_date(
            template, common._parse_date(task["target_date"], "target_date"))
    else:
        resolved = resolve_window(template, schedule_date, now)
    occupancy = common._window_occupancy_minutes(task)
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
        "est_start": common._iso(est_start) if est_start else None,
        "est_end": common._iso(est_end) if est_end else None,
        "nominal_start": common._iso(est_start) if est_start else None,
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
        "window_start_at": common._iso(window.start_at) if window and window.start_at else None,
        "window_end_at": common._iso(window.end_at) if window and window.end_at else None,
        # 固定轮次生成时冻结的到期死亡边界（三轮 Review 裁决，20260928010000）：
        # 仅固定轴轮由调用方传入；中空两阶段共享同值；其余恒 NULL。
        "fixed_expires_at": common._iso(fixed_expires_at) if fixed_expires_at else None,
        "source": "schedule",
        **_generation_snapshots(task, identity.schedule_date, phase),
        "created_at": common._iso(now),
        "updated_at": common._iso(now),
    }


def _create_occurrences(
    client, task: dict[str, Any], schedule_date: date, now: datetime,
    *, due_at: datetime | None = None, display_cycle_date: date | None = None,
    generation_request_key: str | None = None,
    fixed_expires_at: datetime | None = None,
) -> int:
    mode = task.get("refresh_mode")
    if mode is None:
        raise common.PlanningError("unclassified_task", "旧任务定义须在受控迁移中分类", 409)
    try:
        validate_task_refresh_mode(task["task_type"], mode)
    except ValueError as exc:
        raise common.PlanningError("invalid_task", "任务刷新模式与类型不匹配", 409) from exc
    current_cycle = display_cycle_date or cycles._current_cycle(now).key
    if mode == "none":
        round_key = "once"
    elif mode == "after_completion":
        if due_at is None:
            raise common.PlanningError("invalid_task", "处理后刷新任务缺少本轮到期基准", 409)
        round_key = timed_round_key("handled", schedule_date, common._iso(due_at))
    elif mode == "fixed_interval":
        if due_at is None:
            raise common.PlanningError("invalid_task", "固定间隔任务缺少本轮到期事件", 409)
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
                rows[-1]["fixed_due_at"] = common._iso(due_at)
    else:
        identity = OccurrenceIdentity(task["id"], round_key, schedule_date, display_cycle,
                                      display_reason)
        rows.append(_occurrence_row(task, None, est_start, est_end, now, identity, window=window,
                                    fixed_expires_at=fixed_expires_at))
        if fixed_mode and due_at is not None:
            rows[-1]["fixed_due_at"] = common._iso(due_at)
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
        if getattr(exc, "code", None) == common.CONCURRENCY_ERRCODE                 or "task no longer active" in str(exc)                 or "task definition changed during generation" in str(exc):
            # 固定轮生成外层随后会推进 refresh_generated_through。定义漂移
            # 不能作为「0 个新行」返回，否则游标跳过尚未出生的事件。
            raise common.ConcurrencyRejected(
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
        anchor = common._parse_dt(task.get("refresh_anchor_at"), "refresh_anchor_at")
        interval = task.get("interval_days")
        if not isinstance(interval, int) or interval < 1:
            raise common.PlanningError("invalid_task", "固定间隔任务缺少有效天数", 409)
        events = []
        due = anchor
        while due <= now:
            events.append((due.date(), due))
            due += timedelta(days=interval)
        return events
    created = common._parse_dt(task.get("created_at"), "created_at")
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
        batch = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id)
                      .in_("status", list(common.OPEN_STATUSES)).gte("id", after_id + 1)
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
            "display_reason": "carryover", "updated_at": common._iso(now),
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
    return common._parse_dt(expires, "fixed_expires_at")


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
            window_end = common._parse_dt(end_at, "window_end_at")
            if window_end < deadline:
                deadline = window_end
        client.table("planning_occurrence").update({
            "status": "timeout", "closed_at": common._iso(deadline), "updated_at": common._iso(now),
        }).eq("task_id", task["id"]).eq("round_key", round_key).in_("status", list(common.OPEN_STATUSES)).execute()
        expired += sum(item["round_key"] == round_key for item in open_rows)
        expired_rounds.add(round_key)
    return expired


def _daily_round_death_boundary(
    schedule_date: date, configured: time, transition: BoundaryTransition | None,
    absorbed: frozenset[date],
) -> datetime:
    """每日轮次的死亡边界 = 其所属**实际**规划周期的自然终点（下一真实周期开始）。

    复用既有周期原语重建，不复制第二套周期数学：以该轮周期自身的起始
    边界（``cycle_start_boundary``，过渡期冻结段沿用 spanning 边界）构造
    周期内时刻，再取 ``planning_cycle_at`` 的周期终点——跨周期冻结段
    （spanning cycle）的终点已被权威函数替换为 ``effective_at``，因此
    过渡生效期间与过渡结束后的第一次收场（R5，配合调用方传入的持久化
    记录重建 transition）都还原真实终点。当名义终点落在**更早的已完成
    过渡**所吸收的日期上时（吸收日不命名规划周期，B1 语义），按吸收
    登记逐日前移到下一个真实周期起点；已知局限：早于当前保留过渡记录
    冻结段、且其所属周期边界又被更早过渡改变过的存量轮次，只能按现有
    记录近似（与 ``cycle_start_boundary`` 的模型简化一致）。
    """
    start_boundary = cycle_start_boundary(schedule_date, configured, transition)
    moment = datetime.combine(schedule_date, start_boundary, BUSINESS_TIMEZONE)
    cycle = planning_cycle_at(moment, configured, transition)
    nominal_end_day = cycle.end.astimezone(BUSINESS_TIMEZONE).date()
    end_day = nominal_end_day
    while end_day in absorbed:
        end_day += timedelta(days=1)
    if end_day == nominal_end_day:
        return cycle.end
    return datetime.combine(
        end_day, cycle_start_boundary(end_day, configured, transition), BUSINESS_TIMEZONE)


def _expire_daily_rounds(
    client, task: dict[str, Any], today: date, now: datetime,
    configured: time, transition: BoundaryTransition | None,
    absorbed: frozenset[date],
) -> int:
    """每日旧轮在新轮照常生成的周期收场（清单 #32，2026-10-04 user 口裁决）。

    口径（user 确认）：新轮生成时，旧未处理每日轮自动关闭标记「已超时」
    （§8.4「到期死亡」同型生命周期；含执行中 / partial，已有事实原样保留，
    不写完成 / 处理事实、不推进任何刷新基准）。死亡时刻 = 旧轮所属实际
    周期的自然终点，与已成立的冻结窗口截止共用**双死亡边界裁决**（R2，
    与固定到期 ``_expire_fixed_rounds`` 同形）：窗口终点更早时取窗口终点，
    收场原因随之归窗口超时（``is_daily_cycle_death`` 按 ``closed_at ==
    window_end_at`` 识别），不再依赖 sweep 与收场的执行顺序；恰等时保持
    既有保守口径（按窗口超时进「待处理」）。过渡完成后仍按持久化记录
    重建历史周期（R5：``_parse_boundary_state`` 在 ``effective_at`` 之后
    不再暴露 transition，按需读取一次记录重建）。仅 ``can_generate``
    （生成门禁）时执行：暂停刷新 / 关闭每日刷新不生成新轮，旧轮保持
    开放继续顺延展示（需求 24 / §5.3）。与固定到期清理同序且解耦：清理
    先于新轮创建，新轮 INSERT 失败不阻止已越界旧轮关闭（否则其在
    ``closed_at`` 之前仍可被 completed）；清理幂等，重复调用不重复计数、
    不重复终态写入。展示随关闭顺延到当前周期（``display_reason=carryover``，
    不得早于 ``schedule_date``），使关闭记录出现在收场周期「已完成」；
    「待处理」分区由 :func:`is_daily_cycle_death` 按同一口径排除这类
    周期死亡（user 裁决：不出现在待处理）。
    """
    open_rows = _task_open_rows(client, task["id"])
    expired = 0
    expired_rounds: set[str] = set()
    record_transition = transition
    record_resolved = transition is not None
    for occ in open_rows:
        round_key = occ.get("round_key")
        if not round_key or round_key in expired_rounds:
            continue  # 旧实例身份不受控，属受控迁移范围
        raw = occ.get("schedule_date")
        if not raw:
            continue
        schedule_date = date.fromisoformat(raw)
        if schedule_date >= today:
            continue  # 当前周期的轮次（含本轮新轮）保持开放
        if not record_resolved:
            # R5：无生效过渡 ≠ 无过渡历史——过渡完成后按持久化记录重建。
            record_transition = cycles._load_boundary_record_transition()
            record_resolved = True
        death = _daily_round_death_boundary(
            schedule_date, configured, record_transition, absorbed)
        end_at = occ.get("window_end_at")
        if end_at:
            window_end = common._parse_dt(end_at, "window_end_at")
            if window_end < death:
                death = window_end  # 更早成立的窗口截止保持窗口超时口径（R2）
        client.table("planning_occurrence").update({
            "status": "timeout", "closed_at": common._iso(death),
            "display_cycle_date": today.isoformat(),
            "display_reason": "carryover", "updated_at": common._iso(now),
        }).eq("task_id", task["id"]).eq("round_key", round_key).in_(
            "status", list(common.OPEN_STATUSES)).execute()
        expired += sum(item["round_key"] == round_key for item in open_rows)
        expired_rounds.add(round_key)
    return expired


def is_daily_cycle_death(row: dict[str, Any], task: dict[str, Any]) -> bool:
    """识别「每日轮周期死亡」超时行（#32 user 裁决：不进「待处理」分区）。

    识别口径与 :func:`_expire_daily_rounds` 的写入同源：每日刷新任务的
    超时行，且该关闭不是窗口 sweep——窗口死亡以 ``closed_at ==
    window_end_at`` 为特征（用户明确设置的最晚完成，§18.2，保持既有
    待处理展示）；周期死亡写入周期终点，与窗口终点（必然在周期内）不同。
    两入口竞争同一行时谁先提交谁定 ``closed_at``，识别跟随实际关闭者；
    唯一歧义角落（窗口终点恰为边界时刻且周期死亡先落库）落在「展示」
    一侧，属保守方向。
    """
    if task.get("refresh_mode") != "daily":
        return False
    end_at = row.get("window_end_at")
    closed_at = row.get("closed_at")
    if not end_at or not closed_at:
        return True
    return (common._parse_dt(closed_at, "closed_at")
            != common._parse_dt(end_at, "window_end_at"))


def _after_completion_due(client, task: dict[str, Any]) -> datetime | None:
    """Use the latest persisted round, so a failed task-cache write cannot skip a cycle."""
    rows = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]).order("id", desc=True))
    latest = max((row for row in rows if row.get("round_key")), key=lambda row: row["id"], default=None)
    if latest is None:
        return common._parse_dt(task["created_at"], "created_at")
    round_rows = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]).eq("round_key", latest["round_key"]))
    if any(row["status"] in common.OPEN_STATUSES for row in round_rows):
        return None
    if not all(row["status"] in ("completed", "discarded_this") and row.get("handled_at")
               for row in round_rows):
        return None
    handled = max(common._parse_dt(row["handled_at"], "handled_at") for row in round_rows)
    interval = task.get("interval_days")
    if not isinstance(interval, int) or not 1 <= interval <= 365:
        raise common.PlanningError("invalid_task", "处理后刷新间隔必须为 1 至 365 天", 409)
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
        raise common.PlanningError("invalid_task", "任务刷新模式与类型不匹配", 409) from exc
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
        if can_generate:
            # 清单 #32（2026-10-04 user 口裁决）：先收场旧轮——与固定到期
            # 清理同序且解耦，旧每日轮生命周期结束不依赖本轮新轮 INSERT
            # 成功；暂停刷新 / 关闭每日刷新（can_generate=False）不收场，
            # 旧轮保持开放继续顺延展示。legacy 门禁只禁止新生成，不影响
            # 存量轮次的到期死亡（与固定分支同一口径）。
            timed_out += _expire_daily_rounds(
                client, task, today, now, configured, transition, absorbed)
        if can_generate and not legacy_definition:
            task_created = common._parse_dt(task["created_at"], "created_at")
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
                    and common._parse_date(settled_through, "refresh_generated_through")
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
                common._parse_dt(task["created_at"], "created_at"),
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
                checked = common._parse_date(through, "refresh_generated_through") if through else None
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
                            "refresh_generated_through": day.isoformat(), "updated_at": common._iso(now),
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
                            raise common.ConcurrencyRejected(
                                "concurrent_modified", "规划任务在生成期间变化，游标未推进", 409,
                            )
                        task["refresh_generated_through"] = day.isoformat()
                        task["updated_at"] = common._iso(now)
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


def generate_task(
    task_id: int, now: datetime | None = None, *,
    context: cycles.PlanningRequestContext | None = None,
) -> dict[str, Any]:
    """Reconcile only a newly saved task through the shared generation rules.

    The caller holds the maintenance lock. Re-read by identity after acquiring
    it so a task edited or deactivated while waiting is not generated from the
    insert response. Database generation guards remain authoritative.
    """
    now = now or runtime._now()
    context = context or cycles.PlanningRequestContext(now)
    configured, transition, absorbed = context.boundary_state()
    cycle = context.cycle
    daily_enabled = context.daily_refresh_enabled
    client = runtime._require_client()
    task = runtime._fetch_task(client, task_id)
    result = {"created": 0, "timed_out": 0, "date": cycle.key.isoformat()}
    if task is None or task.get("is_active") is False:
        return result
    try:
        created, timed_out, _ = _reconcile_task_rounds(
            client, task, cycle, now, configured, transition, absorbed, daily_enabled,
        )
    except Exception as exc:
        # A task may have committed occurrences before a later step failed.
        # Preserve the error signal so the caller still recomputes those rows.
        log.exception("planning 新任务生成失败: task=%s", task_id)
        result["errors"] = [{"task_id": task_id, "error": type(exc).__name__}]
    else:
        result.update(created=created, timed_out=timed_out)
    return result


def generate_due(now: datetime | None = None) -> dict[str, Any]:
    """Generate stable rounds from their own refresh model, never legacy cursors.

    批次 5 四轮 Review HIGH 2：失败隔离在**单 task reconcile 粒度**——单个
    task 的生成失败（含其内部已完成的到期清理）只记录该 task 的错误并继续
    其余任务的生命周期维护，不得终止整个任务循环，也不全局吞掉异常。每个
    失败 task 的错误经 log（完整堆栈）与返回值 ``errors`` 列表（最小形态：
    task_id + 异常类型名）保留可观测性，不伪装成成功；``created`` /
    ``timed_out`` 汇总只计成功完成的任务。
    """
    now = now or runtime._now()
    configured, transition, absorbed = cycles._load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    today = cycle.key
    daily_enabled = cycles.get_cycle_settings(now)["daily_refresh_enabled"]
    client = runtime._require_client()
    tasks = runtime._rows(client, "planning_task", lambda q: q.eq("is_active", True))
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
    now = now or runtime._now()
    client = runtime._require_client()
    now_iso = common._iso(now)
    timed_out = 0
    seen: set[int] = set()
    tasks: dict[int, dict[str, Any]] = {}
    round_closures: dict[tuple[int, str | None, str], list[dict[str, Any]]] = {}
    while True:
        page = runtime._rows(
            client, "planning_occurrence",
            lambda q: q.in_("status", list(common.OPEN_STATUSES))
            .lt("window_end_at", now_iso).order("id").limit(common.SWEEP_PAGE_SIZE),
        )
        if not page or all(row["id"] in seen for row in page):
            break  # 取空即完毕；全页停滞（更新未生效的异常情形）防死循环
        missing = {row["task_id"] for row in page} - tasks.keys()
        if missing:
            tasks.update(runtime._task_map(client, missing))
        for occ in page:
            end_at = occ.get("window_end_at")
            if not end_at:
                continue  # 防御：timestamptz 列不产生空值以下的异常行
            closed_at = common._parse_dt(end_at, "window_end_at")
            # 双死亡边界裁决：该轮生成时冻结的固定到期边界若已成立（到达
            # 即死的 ≤ 语义）且固定到期机制当前有效（固定型模式、未暂停
            # 刷新、请求未被取代、任务启用中——与 _expire_fixed_rounds 的
            # 门控一致），closed_at 取两者更早者。generation 失败时清理虽
            # 未执行，这里读同一冻结事实防止死亡时刻漂移到较晚的窗口边界；
            # 暂停 / 被取代 / 停用任务的固定边界不成立，不参与 min。
            task = tasks.get(occ["task_id"])
            if (task and task.get("refresh_mode") in common._FIXED_EXPIRING_MODES
                    and task.get("refresh_enabled") is not False
                    and task.get("request_state") != "superseded"
                    and task.get("is_active") is not False):
                boundary = _fixed_death_boundary(occ)
                if boundary is not None and boundary <= now and boundary < closed_at:
                    closed_at = boundary
            scanned_window = common._iso(common._parse_dt(end_at, "window_end_at"))
            key = (occ["task_id"], occ.get("round_key"), common._iso(closed_at), scanned_window)
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
                        "updated_at": common._iso(now),
                    }).eq("id", representative["id"]).eq(
                        "window_end_at", scanned_window,
                    ).eq("status", representative["status"])
                    query = query.lt("window_end_at", now_iso)
                    for field in common.LIFECYCLE_FACT_FIELDS:
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
                    "updated_at": common._iso(now),
                }).eq("task_id", task_id).eq("round_key", round_key).in_(
                    "id", ids,
                ).eq("window_end_at", scanned_window).lt("window_end_at", now_iso)
                if len(group) == 1:
                    result = result.eq("status", group[0]["status"])
                    for field in common.LIFECYCLE_FACT_FIELDS:
                        value = group[0].get(field)
                        result = result.is_(field, None) if value is None else result.eq(field, value)
                else:
                    result = result.in_("status", list(common.OPEN_STATUSES))
                result = result.execute()
                timed_out += len(result.data or [])
        seen.update(row["id"] for row in page)
    if timed_out:
        log.info("planning 超时打标: count=%s", timed_out)
    return {"timed_out": timed_out}
