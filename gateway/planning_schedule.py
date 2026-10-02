"""Pure planning scheduler over frozen occurrence inputs.

No database, settings or implicit clock access. Both board reads and recompute
use this function to derive placements and conflicts from the same snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .planning_window import ResolvedWindow, hollow_envelope_duration, window_feasible
from . import planning_common as common


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
        and not common._has_lifecycle_fact(occ)
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
    start = common._parse_dt(est_start, "est_start")
    end = common._parse_dt(est_end, "est_end")
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
        start_at=common._parse_dt(start, "window_start_at") if start else None,
        end_at=common._parse_dt(end, "window_end_at") if end else None,
    )


def _window_conflict(
    occ: dict[str, Any], window: ResolvedWindow, quantity: str, *, after_avoidance: bool,
) -> dict[str, Any]:
    """排程冲突的派生结果（§19 三要素：哪个待办 / 哪项约束 / 为什么）。

    派生事实：不落库、不新增生命周期状态；由调用方随响应返回或由
    today 看板读取时派生展示。``quantity`` 描述容纳对象（预计耗时 /
    中空完整包络）。
    """
    end_at = common._iso(window.end_at) if window.end_at else None
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
    if not common._is_untouched_open(occ):
        return None  # 执行中 / partial / 已开始 / 已延期：豁免剩余不足重判
    if not occ.get("is_fixed") and occ.get("fixed_source") is None:
        return None  # 非固定锚点实例由主循环 / 中空包络预判覆盖
    if occ.get("phase") == "end":
        start_row = _hollow_sibling(occ, ordered, "start")
        if start_row is not None and common._is_untouched_open(start_row):
            return None  # 开始阶段仍开放且未触动：整轮剩余不足由它按包络上报一次
        # 开始阶段不在开放集合（已完成 / 已关闭）或已豁免重判：结束阶段
        # 自行检查（R8），否则无人上报剩余不足。
    window = _occurrence_window(occ)
    if window is None or window.end_at is None:
        return None  # 只有最早开始 / 无窗口：没有最晚完成，无剩余空间约束
    if not _has_schedulable_duration_source(occ, task):
        return None
    if occ.get("phase") == "start":
        start_duration = common._duration_of(occ, task, "start")
        end_row = _hollow_sibling(occ, ordered, "end")
        envelope = (
            _hollow_movable_envelope_duration(start_duration, task, end_row)
            if end_row is not None else None)
        span = envelope if envelope is not None else start_duration
        quantity = (
            f"中空完整包络 {common._format_duration(span)}" if envelope is not None
            else f"预计耗时 {common._format_duration(span)}")
    else:
        span = common._duration_of(occ, task)
        quantity = f"预计耗时 {common._format_duration(span)}"
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
            start_duration, wait, common._duration_of(end_row, task, "end"))
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
        occ, window, f"中空完整包络 {common._format_duration(envelope)}",
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
            f"{common._iso(start_end)} 结束，加等待 {wait_minutes} 分钟晚于结束阶段"
            f"已固定的开始时刻 {common._iso(frozen_end_start)}"
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
        duration = common._duration_of(occ, task)
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
                occ, window, f"预计耗时 {common._format_duration(duration)}",
                after_avoidance=window_feasible(window, start_floor, duration),
            ))
            if occ.get("phase") == "start":
                failed_round_starts.add(occ["id"])
            continue
        placed[occ["id"]] = (start, end)
        cursor = end
    return ScheduleResult(placed=placed, conflicts=conflicts)
