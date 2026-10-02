"""Planning occurrence edits, lifecycle transitions, split and early completion.

Validation precedes writes. User facts, atomic round edits and split transactions
retain their existing contracts; task definitions are not rewritten by edits."""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from .planning_domain import (
    EARLY_CAPABLE_MODES,
    OccurrenceIdentity,
    planning_cycle_at,
    timed_round_key,
)
from .planning_window import (
    ResolvedWindow,
    hollow_envelope_duration,
    window_at_crosses_boundary,
    window_feasible,
)
from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_cycles as cycles
from . import planning_schedule as scheduler
from . import planning_recompute as recompute
from . import planning_generation as generation
from . import planning_tasks as task_service
from . import planning_serialization as presentation

log = logging.getLogger("gateway.planning")


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


def _hollow_display_patch(occ: dict[str, Any], patch: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """同轮两阶段的展示周期字段（批次 6 一轮 Review BLOCKER 3：写前计算，
    不再单独写库——由调用方并入原子写入）。"""
    if not occ.get("phase_group") or "display_cycle_date" not in patch:
        return None
    return {
        "display_cycle_date": patch["display_cycle_date"],
        "display_reason": patch["display_reason"],
        "updated_at": common._iso(now),
    }


# ── 当前实例窗口编辑（§18.3 / §12.1 / §13.2，批次 6 接线） ─────────

def _hollow_sibling_of(client, occ: dict[str, Any]) -> dict[str, Any] | None:
    """同轮另一阶段行（只读查找；缺失返回 None，由调用方决定语义）。"""
    if not occ.get("phase"):
        return None
    sibling_phase = "end" if occ["phase"] == "start" else "start"
    return next(
        (
            row for row in runtime._rows(
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
        raise common.PlanningError("invalid_round", "中空待办缺少同轮关联阶段", 409)
    return [occ, sibling]


def _occurrence_window_occupancy(rows: list[dict[str, Any]], task: dict[str, Any]) -> timedelta:
    """实例窗口可行性判断的占用跨度（§12.1 / §17.4）：普通 = 有效耗时
    （est 区间事实优先、耗时快照其次，:func:`_duration_of` 单一权威）；
    中空 = 开始 + 等待 + 结束的完整包络（等待读结束阶段行自带的
    ``planned_wait_minutes``，缺失回退任务定义）。"""
    if len(rows) == 1:
        return common._duration_of(rows[0], task)
    start_row = next(row for row in rows if row.get("phase") == "start")
    end_row = next(row for row in rows if row.get("phase") == "end")
    wait = end_row.get("planned_wait_minutes")
    if isinstance(wait, bool) or not isinstance(wait, int) or not 1 <= wait <= 1440:
        wait = task.get("hollow_wait_minutes")
    try:
        return hollow_envelope_duration(
            common._duration_of(start_row, task, "start"), wait, common._duration_of(end_row, task, "end"))
    except ValueError as exc:
        raise common.PlanningError("invalid_payload", "中空待办的阶段耗时或等待时长无效，无法调整时段", 400) from exc


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
        raise common.PlanningError(
            "invalid_transition", f"该待办来自已被取代的重排请求，不能{action}", 409,
        )
    for row in rows:
        if not common._is_untouched_open(row):
            raise common.PlanningError(
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
    start = common._parse_dt(start_raw, "window_start_at") if start_raw else None
    end = common._parse_dt(end_raw, "window_end_at") if end_raw else None
    # §28.3（2026-10-01）：无日期单次常驻显示、不设时间窗口——不能通过
    # 当前实例编辑为无日期常驻实例新增窗口端，借编辑引入截止会改变常驻
    # 语义；任务级模板对已生成 once 一律锁定，本守卫封住实例级旁路。
    if (task.get("task_type") == "once" and not task.get("target_date")
            and (start is not None or end is not None)):
        raise common.PlanningError(
            "invalid_payload",
            "未指定日期的单次待办常驻显示、不设可安排时段：不能为它的当前实例新增时间窗口", 400,
        )
    start_abs = start.astimezone(timezone.utc) if start else None
    end_abs = end.astimezone(timezone.utc) if end else None
    if start_abs and end_abs and end_abs <= start_abs:
        raise common.PlanningError("invalid_payload", "可安排时段的结束必须晚于开始", 400)
    if start_abs and end_abs:
        boundary, _, _ = cycles._load_boundary_state(now)
        if window_at_crosses_boundary(start, end, boundary):
            raise common.PlanningError(
                "invalid_payload",
                f"可安排时段不能跨越每日刷新时间 {boundary.strftime('%H:%M')}，请调整时段", 400,
            )
    # 裁决 6：不允许通过当前编辑整轮清空既有窗口约束（单边保留合法）。
    had_window = any(row.get("window_start_at") or row.get("window_end_at") for row in rows)
    if had_window and start is None and end is None:
        raise common.PlanningError(
            "invalid_payload",
            "不能清空已生成待办的既有可安排时段约束；可以收窄、平移或改为单边时段", 400,
        )
    occupancy = _occurrence_window_occupancy(rows, task)
    if end_abs and not window_feasible(
            ResolvedWindow(start_at=start, end_at=end), now, occupancy):
        raise common.PlanningError(
            "invalid_payload",
            f"可安排时段剩余空间不足以容纳执行耗时 {common._format_duration(occupancy)}，请调整时段或耗时", 400,
        )
    zero_freedom = start_abs is not None and end_abs is not None and (end_abs - start_abs) == occupancy
    # 不可移动锚点守卫：先于 zero-slack 分支（裁决：不得借钉住搬运既有
    # 固定 est；锚点在新窗口外一律拒绝）。
    anchored_equal_window = False
    for row in rows:
        slot = scheduler._slot_range(row)
        if slot is None or scheduler._freely_schedulable(row, task):
            continue
        slot_start, slot_end = slot
        slot_start_abs = slot_start.astimezone(timezone.utc)
        slot_end_abs = slot_end.astimezone(timezone.utc)
        inside = ((start_abs is None or slot_start_abs >= start_abs)
                  and (end_abs is None or slot_end_abs <= end_abs))
        if not inside:
            raise common.PlanningError(
                "invalid_payload",
                "该待办已有固定的预估时间在新的可安排时段之外，请先调整预估时间或扩大时段", 400,
            )
        if zero_freedom and slot_start_abs == start_abs and slot_end_abs == end_abs:
            anchored_equal_window = True  # 锚点已是唯一合法位置：保持原所有权
    patch: dict[str, Any] = {
        "window_start_at": common._iso(start) if start else None,
        "window_end_at": common._iso(end) if end else None,
    }
    sibling_row = rows[1] if len(rows) > 1 else None
    if zero_freedom and not anchored_equal_window:
        # 零自由度窗口 → 钉住（§13.2）：est = 窗口本身，manual 所有权元组。
        # 中空两阶段一起钉住：开始阶段锚在窗口起点，结束阶段经等待链在窗口
        # 终点收口（开始 + 等待 + 结束 = 窗口长，位置由此唯一确定）。
        if sibling_row is None:
            patch.update(recompute._manual_estimate_patch(
                occ, task, {"est_start": common._iso(start), "est_end": common._iso(end)}, now))
        else:
            start_row = next(row for row in rows if row.get("phase") == "start")
            end_row = next(row for row in rows if row.get("phase") == "end")
            start_duration = common._duration_of(start_row, task, "start")
            end_duration = common._duration_of(end_row, task, "end")
            phase_patches = {
                start_row["id"]: recompute._manual_estimate_patch(
                    start_row, task,
                    {"est_start": common._iso(start), "est_end": common._iso(start + start_duration)}, now),
                end_row["id"]: recompute._manual_estimate_patch(
                    end_row, task,
                    {"est_start": common._iso(end - end_duration), "est_end": common._iso(end)}, now),
            }
            for phase_patch in phase_patches.values():
                phase_patch["window_start_at"] = patch["window_start_at"]
                phase_patch["window_end_at"] = patch["window_end_at"]
                phase_patch["updated_at"] = common._iso(now)
            main_patch = dict(phase_patches[occ["id"]])
            sibling_patch = (
                dict(phase_patches[sibling_row["id"]]) if sibling_row["id"] in phase_patches
                else {**patch, "updated_at": common._iso(now)})
            return main_patch, sibling_patch
    elif sibling_row is not None:
        sibling_patch = {**patch, "updated_at": common._iso(now)}
        patch["updated_at"] = common._iso(now)
        return patch, sibling_patch
    patch["updated_at"] = common._iso(now)
    return patch, None


def _reschedule_occurrence(
    occ: dict[str, Any], task: dict[str, Any], new_start: datetime, now: datetime,
) -> dict[str, Any]:
    """Manual arrangement changes display/time, never the business round."""
    return recompute._manual_estimate_patch(occ, task, {"est_start": common._iso(new_start)}, now)


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
        raise common.PlanningError("invalid_round", "中空阶段缺少同轮身份", 409)
    if old_start:
        delta = new_start - old_start
    else:
        # 原本无预估时间（尚未重算的仅耗时实例）：按日期差平移，
        # 保留新时刻的时、分，保证两阶段落在同一天。
        anchor = common._combine(date.fromisoformat(occ["display_cycle_date"]), new_start.time())
        delta = new_start - anchor
    if delta == timedelta(0):
        return None
    sibling_phase = "end" if occ["phase"] == "start" else "start"
    sibling = next(
        (
            row for row in runtime._rows(
                client, "planning_occurrence",
                lambda q: q.eq("task_id", task["id"]).eq("phase", sibling_phase)
                .eq("round_key", occ["round_key"]).eq("phase_group", occ["phase_group"]),
            )
            if row["id"] != occ["id"]
        ),
        None,
    )
    if not sibling:
        raise common.PlanningError("invalid_round", "中空待办缺少同轮关联阶段", 409)
    if sibling.get("fixed_source") is not None:
        raise common.PlanningError("fixed_conflict", "关联阶段已有固定时间，不能自动平移", 409)
    old_sibling_start = common._parse_dt(sibling["est_start"], "est_start") if sibling.get("est_start") else None
    old_sibling_end = common._parse_dt(sibling["est_end"], "est_end") if sibling.get("est_end") else None
    if old_sibling_start is None:
        return None
    shifted_start = old_sibling_start + delta
    shifted_end = (old_sibling_end + delta if old_sibling_end
                   else shifted_start + common._duration_of(sibling, task, sibling_phase))
    patch = common._estimate_patch(shifted_start, shifted_end, source="automatic")
    patch["updated_at"] = common._iso(now)
    return patch


def set_occurrence_status(occurrence_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or runtime._now()
    if not isinstance(payload, dict):
        raise common.PlanningError("invalid_payload", "request body must be a JSON object")
    target = str(payload.get("status") or "").strip().casefold()
    if target not in common.OCCURRENCE_STATUSES:
        raise common.PlanningError("invalid_payload", f"status must be one of {', '.join(common.OCCURRENCE_STATUSES)}")
    if target == "timeout":
        raise common.PlanningError("invalid_payload", "timeout is assigned by the system only")
    # 完成耗时手填（2026-10-01 确认，§12.3）：校验前置（写前完整校验纪律），
    # 手填值存独立 actual_logged_seconds，不覆盖自动 actual_* 事实。
    logged_seconds = common.parse_logged_duration_seconds(
        payload.get("actual_logged_duration"), "actual_logged_duration",
    ) if target == "completed" else None

    client = runtime._require_client()
    occ = runtime._fetch_occurrence(client, occurrence_id)
    if not occ:
        raise common.PlanningError("not_found", "planning occurrence not found", 404)
    if not occ.get("round_key"):
        raise common.PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = runtime._fetch_task(client, occ["task_id"])
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)
    current = occ["status"]

    new_start_raw = payload.get("est_start")
    new_start = common._parse_dt(new_start_raw, "est_start") if new_start_raw else None

    # 状态迁移规则（过去不重写，已关闭实例不复活）：
    # * 已关闭或已超时的历史记录一律不得回到开放生命周期（pending /
    #   in_progress / deferred / partial）；限时超时的出口是「重新安排为新
    #   的单次待办」（reschedule_timeout_as_new），不是改写旧实例状态。
    # * 历史修正仅限关闭态之间的状态标签更正与实际时间 / 说明补改；
    #   handled_at 一经写入不再改变，保持处理后刷新基准的历史事实。
    # * 部分完成属于开放生命周期：记录 partial_at 与说明，不改 handled_at、
    #   不关闭实例；只有「已全部完成」才以最终时间关闭并起算处理后刷新。
    # * 超时后不能再标记完成 / 部分完成，只能废弃或重新安排为新待办。
    if target in common.OPEN_STATUSES and (current in common.CLOSED_STATUSES or current == "timeout"):
        raise common.PlanningError(
            "invalid_transition", "已关闭的历史记录不能恢复为开放待办", 422,
        )
    if target in ("completed", "partial") and current == "timeout":
        raise common.PlanningError(
            "invalid_transition", "timed-out occurrences can only be rescheduled or discarded", 422,
        )
    if target == "discarded_this":
        if current == "timeout":
            pass  # 超时后此次不执行视为一种废弃处理路径
        elif current not in common.OPEN_STATUSES and current not in common.CLOSED_STATUSES:
            raise common.PlanningError("invalid_transition", f"cannot discard_this from {current}", 422)
    if target == "deferred":
        if current not in ("pending", "in_progress", "partial"):
            raise common.PlanningError("invalid_transition", f"cannot defer from {current}", 422)
        if not new_start:
            raise common.PlanningError("invalid_payload", "deferring requires est_start", 422)
    if target == "in_progress" and current not in ("pending", "deferred", "partial"):
        raise common.PlanningError("invalid_transition", f"cannot start from {current}", 422)
    if target == "partial" and current not in ("pending", "in_progress", "partial", "deferred"):
        raise common.PlanningError(
            "invalid_transition", f"cannot record partial completion from {current}", 422,
        )
    if target == "completed" and current not in (
        "pending", "in_progress", "partial", "deferred", "completed",
        "discarded", "discarded_this",
    ):
        # discarded → completed 属于关闭态之间的历史标签更正（B5 双向）；
        # 无处理事实的历史以更正时刻记录 handled_at（见 newly_handled）。
        raise common.PlanningError("invalid_transition", f"cannot complete from {current}", 422)

    # 重复型任务的「废弃」= 整个待办不再执行（需求 4d）。判定前移——
    # 废弃命令（跨 task + occurrence）在主写入前以原子 RPC 执行
    #（最终验收修复问题 5）。
    discarding_whole_task = (
        target == "discarded"
        and task["task_type"] in common.REPEATING_TASK_TYPES
        and task.get("is_active")
        and current not in common.CLOSED_STATUSES
    )

    row: dict[str, Any] = {"status": target, "updated_at": common._iso(now)}
    # 中空同轮两阶段联动（最终修复问题 2）：延后等带时间的开放状态流转会
    # 同时修改同轮兄弟行（est 平移 + 展示一致）——两行补丁统一经原子 RPC
    # 提交，禁止「update A; update B」顺序写；终态流转只写目标行（单行
    # 条件 UPDATE 本身原子），不经 RPC（白名单永不携带终态）。
    sibling: dict[str, Any] | None = None
    sibling_row: dict[str, Any] | None = None

    if target == "partial":
        note = common._clean_text(payload.get("partial_note"), "partial_note", required=True, maximum=common.MAX_NOTE_LENGTH)
        row["partial_note"] = note
        row["partial_at"] = common._iso(now)
        # Partial work is a fact inside an open lifecycle, never a baseline.
        row["handled_at"] = None
    elif target in ("pending", "in_progress", "deferred"):
        if current in common.CLOSED_STATUSES or current == "timeout":
            row["partial_note"] = None

    if new_start and not discarding_whole_task:
        # 废弃整个任务（不再执行）与「指定新执行时间」矛盾：废弃路径不做
        # 时间平移（原子命令覆盖全部开放实例）。
        row.update(_reschedule_occurrence(occ, task, new_start, now))
        shift_patch = _shift_sibling_phase(
            client, occ, task,
            common._parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
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
        row["actual_start"] = common._iso(now)

    closing = target in common.CLOSED_STATUSES
    # handled_at 是历史处理事实：完整处理（含把无处理事实的关闭历史更正为
    # 已完成 / 此次不执行）时写入；已有时不得改写（触发器同此约束）。
    newly_handled = target in ("completed", "discarded_this") and (
        current in common.OPEN_STATUSES or current == "timeout"
        or (current in common.CLOSED_STATUSES and not occ.get("handled_at"))
    )
    if closing:
        if current in common.OPEN_STATUSES:
            # closed_at = 第一次真正进入关闭态的事实时间；关闭态之间的标签
            # 更正保留原值（更正时间由 updated_at 表达，B7）。
            row["closed_at"] = common._iso(now)
        if newly_handled:
            row["handled_at"] = common._iso(now)
        if payload.get("actual_end"):
            row["actual_end"] = common._iso(common._parse_dt(payload["actual_end"], "actual_end"))
        elif not occ.get("actual_end"):
            row["actual_end"] = common._iso(now)
        if payload.get("actual_start"):
            row["actual_start"] = common._iso(common._parse_dt(payload["actual_start"], "actual_start"))
        merged = {**occ, **row}
        row["actual_minutes"] = common._compute_actual_minutes(merged)
        if logged_seconds is not None:
            if sibling_row is not None:
                # 中空同轮两阶段写经 round RPC 硬白名单（无本字段）——手填
                # 耗时与时间平移不提供同请求混合语义，显式中文拒绝、零写入。
                raise common.PlanningError(
                    "invalid_payload",
                    "手填实际耗时不能与时间调整同请求提交：请先完成，再单独调整时间",
                )
            row["actual_logged_seconds"] = logged_seconds
    else:
        row["closed_at"] = None
        if payload.get("actual_start"):
            row["actual_start"] = common._iso(common._parse_dt(payload["actual_start"], "actual_start"))
        if payload.get("actual_end"):
            row["actual_end"] = common._iso(common._parse_dt(payload["actual_end"], "actual_end"))
        if "actual_start" in row or "actual_end" in row:
            merged = {**occ, **row}
            row["actual_minutes"] = common._compute_actual_minutes(merged)

    if discarding_whole_task:
        # 最终 Debug（问题 1B）：废弃整个任务 = 单事务命令。目标行的关闭
        # 事实（actual_end / actual_minutes / actual_start——废弃命令成功
        # 必须产生的结果）作为 RPC 输入在同一事务内写入；RPC commit 后
        # 不再有事务外补写（fact 更新失败 = 整个废弃回滚，task 不会已被
        # 停用）。closed_at / status 由 RPC 的批量关闭覆盖。
        target_facts = {key: row[key] for key in
                        ("actual_start", "actual_end", "actual_minutes")
                        if key in row}
        target_facts["updated_at"] = common._iso(now)
        runtime._discard_task_atomically(client, task["id"], now,
                                 target_id=occ["id"], target_patch=target_facts)
        # 批次 6 收尾（BUG B）：整任务废弃提前 return，绕过了下方 closing
        # 分支——废弃释放的时间槽必须由一次重算重新分配；登记为 post-commit
        # side effect，失败不伪装成废弃失败（quiet，可观测日志兜底）。
        recompute._request_recompute_quietly("task_discarded", now)
        task = {**task, "is_active": False}
        refreshed = runtime._fetch_occurrence(client, occurrence_id) or {**occ, **row}
        return presentation.serialize_occurrence(refreshed, task, now)

    if sibling_row is not None:
        # 最终修复（问题 2）：中空同轮两阶段（目标行状态流转 + 兄弟行时间
        # 联动 + 展示一致）经原子 RPC 一次提交；注入失败两行整体回滚。
        # RPC 锁内以宽松门（开放且无关闭事实）复核生命周期——延后自
        # in_progress / partial 仍合法（既有语义），并发完成 / 关闭则拒绝。
        recompute._atomic_round_write(client, occ, row, sibling, sibling_row)
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
            raise common.PlanningError(
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
        round_rows = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]).eq("round_key", occ["round_key"]))
        if all(item["status"] in ("completed", "discarded_this") and item.get("handled_at")
               for item in round_rows):
            handled = max(common._parse_dt(item["handled_at"], "handled_at") for item in round_rows)
            client.table("planning_task").update({
                "last_handled_at": common._iso(handled),
                "refresh_next_due_at": common._iso(handled + timedelta(days=task["interval_days"])),
                "updated_at": common._iso(now),
            }).eq("id", task["id"]).execute()

    # 重算等待标记按约定只在「列表顺序变化 / 有待办完成」时触发；
    # 这里对应关闭态流转（含重新安排与废弃）。
    if closing:
        recompute.request_recompute("status_change", now)
    refreshed = runtime._fetch_occurrence(client, occurrence_id) or {**occ, **row}
    return presentation.serialize_occurrence(refreshed, task, now)


def start_occurrence(occurrence_id: int, now: datetime | None = None) -> dict[str, Any]:
    return set_occurrence_status(occurrence_id, {"status": "in_progress"}, now)


def finish_occurrence(occurrence_id: int, payload: Any = None,
                      now: datetime | None = None) -> dict[str, Any]:
    """完成当前实例（2026-10-01 确认 §12.3）：可选 ``payload`` 携带 user
    手填实际耗时的原始文本（``actual_logged_duration``），由
    set_occurrence_status 统一解析；既有 ``finish_occurrence(id, now)``
    位置传参（第二参数为 datetime）保持兼容。"""
    if isinstance(payload, datetime):
        payload, now = None, payload
    body: dict[str, Any] = {"status": "completed"}
    if isinstance(payload, dict) and "actual_logged_duration" in payload:
        body["actual_logged_duration"] = payload["actual_logged_duration"]
    return set_occurrence_status(occurrence_id, body, now)


def patch_occurrence(occurrence_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """手动编辑 / 兜底：手动改预估起止、调整当前实例窗口（§18.3）、补填或
    修改实际起止、部分完成说明。

    批次 6 一轮 Review BLOCKER 3：**写前完整校验**——payload 的全部字段
    （窗口 / 生命周期门控 / partial_note / 实际时间 / 混合字段限制 / 中空
    轮次一致性 / 固定锚点 / 可行性）都在第一个数据库写入之前校验完成；
    之后中空同轮多行修改经 :func:`_atomic_round_write` 单语句原子提交，
    任何失败下两行都保持修改前状态（无半写）。
    """
    now = now or runtime._now()
    if not isinstance(payload, dict):
        raise common.PlanningError("invalid_payload", "request body must be a JSON object")
    allowed = {"est_start", "est_end", "actual_start", "actual_end", "partial_note", "is_fixed",
               "window_start_at", "window_end_at"}
    unknown = set(payload) - allowed
    if unknown:
        raise common.PlanningError("invalid_payload", f"unsupported fields: {', '.join(sorted(unknown))}")
    if not payload:
        raise common.PlanningError("invalid_payload", "no writable fields supplied")
    if "is_fixed" in payload:
        common._clean_bool(payload["is_fixed"], "is_fixed")
    window_edited = any(field in payload for field in ("window_start_at", "window_end_at"))
    if window_edited and any(field in payload for field in ("est_start", "est_end", "is_fixed")):
        # 两条编辑路径语义不同（实例窗口 = 排程约束；est = 排程结果 / 人工
        # 锚点），不提供同请求混合语义（校验先行，零写入）。
        raise common.PlanningError(
            "invalid_payload", "可安排时段与预估时间不能在同一次请求中同时修改", 400,
        )

    client = runtime._require_client()
    occ = runtime._fetch_occurrence(client, occurrence_id)
    if not occ:
        raise common.PlanningError("not_found", "planning occurrence not found", 404)
    if not occ.get("round_key"):
        raise common.PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = runtime._fetch_task(client, occ["task_id"])
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)

    # ── 校验与补丁计算（零写入） ──────────────────────────────────
    main_row: dict[str, Any] = {"updated_at": common._iso(now)}
    sibling: dict[str, Any] | None = None
    sibling_row: dict[str, Any] | None = None
    est_edited = any(field in payload for field in ("est_start", "est_end", "is_fixed"))
    if est_edited:
        # 最终修复（问题 4）：预估时间编辑复用统一生命周期门控（与窗口编辑
        # 同一谓词）——已发生事实实例（completed / timeout / in_progress /
        # partial）不得重新排程；实际时间 / 说明的事实修正走下方专用路径、
        # 不受此限（§23 历史修正）。中空按整轮判断（平移会触及同轮兄弟行）。
        _window_edit_gate(_occurrence_round_rows(client, occ), task, action="修改预估时间")
        main_row.update(recompute._manual_estimate_patch(occ, task, payload, now))
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
            common._parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None,
            common._parse_dt(main_row["est_start"], "est_start"), now,
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
        main_row["partial_note"] = common._clean_text(
            payload.get("partial_note"), "partial_note", required=False, maximum=common.MAX_NOTE_LENGTH,
        )
    if "actual_start" in payload:
        main_row["actual_start"] = common._iso(common._parse_dt(payload["actual_start"], "actual_start")) if payload["actual_start"] else None
    if "actual_end" in payload:
        main_row["actual_end"] = common._iso(common._parse_dt(payload["actual_end"], "actual_end")) if payload["actual_end"] else None
    if "actual_start" in main_row or "actual_end" in main_row:
        main_row["actual_minutes"] = common._compute_actual_minutes({**occ, **main_row})

    # ── 写入阶段（全部校验已通过） ────────────────────────────────
    if est_edited or window_edited:
        # 窗口 / 预估编辑：单行 = 条件 UPDATE（生命周期门控条件内联——
        # 最终修复问题 1，普通单行写不绕过锁内保护）；中空同轮两阶段统一
        # 经 _atomic_round_write 的 RPC 原子完成——不存在「同轮两行顺序写」
        # 路径（批次 6 二轮 十一）。
        recompute._atomic_round_write(client, occ, main_row, sibling, sibling_row)
        if window_edited:
            # 窗口是排程约束：约束变化后可重排实例的 est 由下一次重算在窗口内
            # 重新派生（§16.2「其他明确要求重新排程的状态变化」；钉住实例的
            # est 已随编辑确定，重算把其视为固定槽，行为不变）。
            recompute.request_recompute("occurrence_window_edit", now)
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
            raise common.PlanningError(
                "concurrent_modified",
                "该待办的实际时间已被并发修改，本次补填未执行，请刷新后重试", 409,
            )
    refreshed = runtime._fetch_occurrence(client, occurrence_id) or {**occ, **main_row}
    return presentation.serialize_occurrence(refreshed, task, now)


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
    now = now or runtime._now()
    if not isinstance(payload, dict):
        raise common.PlanningError("invalid_payload", "request body must be a JSON object")
    parts = payload.get("parts")
    if not isinstance(parts, list) or not 1 <= len(parts) <= 10:
        raise common.PlanningError("invalid_payload", "parts must be an array of 1-10 items")
    normalized = []
    for part in parts:
        if not isinstance(part, dict):
            raise common.PlanningError("invalid_payload", "parts items must be objects")
        content = common._clean_text(part.get("content"), "parts.content", required=True, maximum=common.MAX_CONTENT_LENGTH)
        minutes = common.parse_duration_shorthand(part.get("estimated_minutes", 30), "parts.estimated_minutes")
        normalized.append({"content": content, "estimated_minutes": minutes})

    client = runtime._require_client()
    occ = runtime._fetch_occurrence(client, occurrence_id)
    if not occ:
        raise common.PlanningError("not_found", "planning occurrence not found", 404)
    if not occ.get("round_key"):
        raise common.PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    if occ.get("status") not in common.OPEN_STATUSES:
        raise common.PlanningError(
            "invalid_transition", "只有开放中的待办可以拆分；该待办已关闭或已超时", 422,
        )
    task = runtime._fetch_task(client, occ["task_id"])
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)

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
            "p_target_date": cycles._current_cycle(now).key.isoformat(),
            "p_now": common._iso(now),
            "p_parts": [
                {"content": part["content"],
                 "estimated_minutes": part["estimated_minutes"]}
                for part in normalized
            ],
            "p_after_completion_days": after_completion_days,
        }).execute()
    except common.PlanningError:
        raise
    except Exception as exc:
        if common._is_rpc_concurrency_rejection(exc):
            # 0 行命中（已关闭 / 已拆分收口 / 已超时）：并发重复拆分请求
            # 不产生第二组拆分任务（B2 业务兜底，语义与原条件关闭一致）。
            raise common.PlanningError(
                "invalid_transition", "该待办已被并发操作关闭，不能再次拆分", 409,
            ) from exc
        raise common.PlanningError(
            "database_unavailable", "拆分暂时无法完成，请稍后重试", 503,
        ) from exc
    created_ids = [int(item) for item in (response.data or [])]

    # 复审 R3（清单 #27）：主事务已完整提交（原轮收口 + 任务创建 + 基准
    # 推进）之后，重算请求登记属于 post-commit side effect——登记失败不得
    # 把完整成功误报为 500、也不得短路下面的即时生成（与停用路径同一
    # quiet 语义；失败由用户手动重算 / 后续维护循环兜底）。
    recompute._request_recompute_quietly("split", now)
    # 即时生成：拆分出的当日单次待办立刻出现在列表里。
    task_service._generate_due_quietly(client, now)
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
    rows = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task["id"]))
    early_handled = [
        common._parse_dt(row["handled_at"], "handled_at")
        for row in rows
        if row.get("source") == "early"
        and row.get("early_period_date") is None
        and row.get("handled_at")
    ]
    candidates = list(early_handled)
    if task.get("last_handled_at"):
        candidates.append(common._parse_dt(task["last_handled_at"], "last_handled_at"))
    if not candidates:
        return None  # 尚无任何成功成立的完成事实：不存在窗口
    fact_time = max(candidates)
    if now - fact_time > common.EARLY_DEDUPE_WINDOW:
        return None  # 窗口外：新的真实提前完成
    fact = max(
        (row for row in rows
         if row.get("handled_at")
         and common._parse_dt(row["handled_at"], "handled_at") == fact_time),
        key=lambda row: row["id"],
        default=None,
    )
    if fact is None:
        # 事实时刻找不到对应行（异常漂移）：保守放行，由数据库触发器兜底。
        return None
    _repair_after_completion_baseline(client, task, fact, now)
    result = presentation.serialize_occurrence(fact, task, now)
    elapsed = max(0.0, (now - fact_time).total_seconds())
    result.update({
        "duplicate_within_window": True,
        "previous_handled_at": common._iso(fact_time),
        "elapsed_seconds": int(elapsed),
        "retry_after_seconds": max(0, int(round(common.EARLY_DEDUPE_WINDOW.total_seconds() - elapsed))),
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
    round_rows = runtime._rows(
        client, "planning_occurrence",
        lambda q: q.eq("task_id", task["id"]).eq("round_key", early_occ["round_key"]),
    )
    if not all(row.get("handled_at") for row in round_rows):
        return
    handled = max(common._parse_dt(row["handled_at"], "handled_at") for row in round_rows)
    expected_due = handled + timedelta(days=task.get("interval_days") or 0)
    if (task.get("last_handled_at") == common._iso(handled)
            and task.get("refresh_next_due_at") == common._iso(expected_due)):
        return
    client.table("planning_task").update({
        "last_handled_at": common._iso(handled),
        "refresh_next_due_at": common._iso(expected_due),
        "updated_at": common._iso(now),
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
    now = now or runtime._now()
    if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                        or not 1 <= len(idempotency_key) <= 200):
        raise common.PlanningError("invalid_payload", "Idempotency-Key 必须为 1 至 200 字符", 400)
    client = runtime._require_client()
    task = runtime._fetch_task(client, task_id)
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)
    if task["task_type"] not in ("interval", "weekly", "monthly"):
        raise common.PlanningError(
            "invalid_transition", "only refreshable tasks support early completion", 422,
        )
    if not task.get("is_active"):
        raise common.PlanningError("invalid_transition", "task is discarded", 422)
    if task.get("refresh_mode") not in EARLY_CAPABLE_MODES:
        raise common.PlanningError("unclassified_task", "旧间歇任务须在受控升级中分类", 409)
    interval = task.get("interval_days")
    if task["refresh_mode"] == "after_completion" and (
        not isinstance(interval, int) or not 1 <= interval <= 365
    ):
        raise common.PlanningError("invalid_payload", "interval_days is missing", 422)
    if task.get("is_hollow"):
        raise common.PlanningError("invalid_round", "中空待办提前处理需由完整轮次承载", 409)
    if idempotency_key:
        prior = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).eq("generation_request_key", idempotency_key).limit(1))
        if prior and prior[0]["status"] not in common.OPEN_STATUSES:
            # M6：幂等重试不仅返回记录——该操作尚未完成的派生状态必须补齐
            # （early 插入成功但 task 基准字段更新失败的场景）；不产生第二条
            # early，下一轮 due 语义不变（按既有 handled 时刻，不用重试时刻）。
            _repair_after_completion_baseline(client, task, prior[0], now)
            return presentation.serialize_occurrence(prior[0], task, now)

    # 只对本任务做生命周期校正（生成 / 到期清理 / 顺延），不触碰其他任务：
    # 本请求后续写入失败时，无关任务不得已被改变（B9）。
    configured, transition, absorbed = cycles._load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    daily_enabled = cycles.get_cycle_settings(now)["daily_refresh_enabled"]
    try:
        _, _, events = generation._reconcile_task_rounds(
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
            recompute.recompute_today(now)
        except Exception:
            log.exception("planning 提前完成恢复重算失败: task=%s", task_id)
        raise

    open_rows = runtime._rows(
        client, "planning_occurrence",
        lambda q: q.eq("task_id", task_id).in_("status", list(common.OPEN_STATUSES)),
    )
    if open_rows:
        occ = prior[0] if idempotency_key and prior else sorted(open_rows, key=lambda r: r["id"])[0]
        if idempotency_key and occ.get("generation_request_key") not in (None, idempotency_key):
            raise common.PlanningError("round_busy", "当前轮次正在由其他请求处理", 409)
        if idempotency_key and occ.get("generation_request_key") is None:
            client.table("planning_occurrence").update({
                "generation_request_key": idempotency_key,
            }).eq("id", occ["id"]).execute()
        result = set_occurrence_status(
            occ["id"], {"status": "completed", "actual_end": common._iso(now)}, now,
        )
    else:
        if task.get("time_mode") == "explicit":
            # 窗口批次收口（2026-09-27 Review MEDIUM）：额外完成记录继承
            # 生成时快照，旧显式定义不得再产生带 explicit 快照的新行；存量
            # 开放轮次仍可正常完成（走上方分支，不产生新行）。
            raise common.PlanningError(
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
                default=common._parse_dt(task["created_at"], "created_at"),
            )
            early_period_date = period_start.date()
            prior_extra = [
                row for row in runtime._rows(
                    client, "planning_occurrence", lambda q: q.eq("task_id", task_id))
                if row.get("source") == "early"
                and row.get("early_period_date") == early_period_date.isoformat()
            ]
            if prior_extra:
                return presentation.serialize_occurrence(prior_extra[-1], task, now)
        today = cycles._current_cycle(now).key
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
            "actual_start": common._iso(now),
            "actual_end": common._iso(now),
            "actual_minutes": 0,
            "status": "completed",
            "sort_order": task["id"] * 10,
            "is_fixed": False,
            "estimated_time_source": "unassigned",
            "fixed_source": None,
            "schedule_managed": True,
            # 窗口批次：deadline 事实生成期停止写入，与 _generation_snapshots 一致
            "is_limited": False,
            "closed_at": common._iso(now),
            "handled_at": common._iso(now),
            "source": "early",
            "early_period_date": early_period_date.isoformat() if early_period_date else None,
            **generation._generation_snapshots(task, identity.schedule_date, None),
            "created_at": common._iso(now),
            "updated_at": common._iso(now),
        }
        try:
            response = client.table("planning_occurrence").insert(row).execute()
        except Exception as exc:
            if "planning_occurrence_early_period_uq" in str(exc):
                # 并发同周期：数据库唯一约束兜底，收敛到已存在的那条。
                prior = next(
                    (row for row in runtime._rows(
                        client, "planning_occurrence", lambda q: q.eq("task_id", task_id))
                     if row.get("source") == "early"
                     and row.get("early_period_date")
                     and row["early_period_date"]
                     == (early_period_date.isoformat() if early_period_date else None)),
                    None,
                )
                if not prior:
                    raise
                return presentation.serialize_occurrence(prior, task, now)
            if "after_completion early completions within the same 30-minute window" in str(exc):
                # BF3/G：真实 PostgreSQL 触发器拒绝同一 30 分钟窗口内的第二条
                # after_completion 提前完成（不同 key 并发）→ 收敛到已成立的
                # 成功事实，并补齐其派生基准字段；基准不再二次推进。附带重复
                # 窗口元数据供前端反馈。
                prior = max(
                    (row for row in runtime._rows(
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
                result = presentation.serialize_occurrence(prior, task, now)
                fact_time = common._parse_dt(prior["handled_at"], "handled_at")
                elapsed = max(0.0, (now - fact_time).total_seconds())
                result.update({
                    "duplicate_within_window": True,
                    "previous_handled_at": common._iso(fact_time),
                    "elapsed_seconds": int(elapsed),
                    "retry_after_seconds": max(
                        0, int(round(common.EARLY_DEDUPE_WINDOW.total_seconds() - elapsed))),
                })
                return result
            if not any(name in str(exc) for name in (
                "planning_occurrence_generation_request_uq", "planning_occurrence_round_phase_uq",
            )):
                raise
            prior = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).eq("generation_request_key", request_key).limit(1))
            if not prior:
                raise
            return presentation.serialize_occurrence(prior[0], task, now)
        created = (response.data or [{}])[0]
        result = presentation.serialize_occurrence(created, task, now)
    if task["refresh_mode"] == "after_completion":
        client.table("planning_task").update({
            "last_handled_at": common._iso(now),
            "refresh_next_due_at": common._iso(now + timedelta(days=interval)),
            "updated_at": common._iso(now),
        }).eq("id", task_id).execute()
    return result
