"""Planning task definitions, rule-switch settlement and post-write generation.

Task changes affect future rounds; old-rule closeout precedes the task write.
The synchronous generation path shares the runtime lock with task edits."""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .planning_domain import (
    BoundaryTransition,
    PlanningCycle,
    cycle_start_boundary,
    planning_cycle_at,
    validate_task_refresh_mode,
)
from .planning_window import (
    resolve_window,
    resolve_window_on_date,
    validate_template_window,
    window_feasible,
)
from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_cycles as cycles
from . import planning_recompute as recompute
from . import planning_generation as generation
from . import planning_serialization as presentation

log = logging.getLogger("gateway.planning")


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
        raise common.PlanningError("invalid_payload", "请求内容必须是 JSON 对象")

    allowed = {
        "content", "task_type", "interval_days", "weekdays", "month_days",
        "target_date", "time_mode", "estimated_minutes",
        "is_hollow", "hollow_start_content", "hollow_start_minutes",
        "hollow_wait_minutes", "hollow_wait_note", "hollow_end_content",
        "hollow_end_minutes",
        "alarm_start", "alarm_end", "timer_minutes", "is_active",
        "refresh_mode", "refresh_anchor_at", "refresh_enabled",
        "window_start_tod", "window_end_tod",
        # 处理后刷新间隔（§9.5，2026-10-07）：API 接收原始时长文本
        # （after_completion_interval），后端权威解析为分钟；也接受已归一
        # 的整数分钟字段。两键不得同请求携带冲突值。
        "after_completion_interval", "after_completion_minutes",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise common.PlanningError("invalid_payload", f"不支持的字段：{', '.join(sorted(unknown))}")
    if not payload and not partial:
        raise common.PlanningError("invalid_payload", "请求内容不能为空")

    result: dict[str, Any] = {}
    if "content" in payload or not partial:
        content = common._clean_text(payload.get("content"), "content", required=True, maximum=common.MAX_CONTENT_LENGTH)
        result["content"] = content

    task_type = payload.get("task_type", result.get("task_type"))
    if task_type is not None or not partial:
        text = str(task_type or "").strip().casefold()
        if text not in common.TASK_TYPES:
            raise common.PlanningError("invalid_payload", f"待办类型必须是：{'、'.join(common.TASK_TYPES)}")
        result["task_type"] = text
    effective_type: str | None = result.get("task_type")

    if "refresh_mode" in payload:
        mode = payload["refresh_mode"]
        if not isinstance(mode, str):
            raise common.PlanningError("invalid_payload", "刷新方式必须是文本")
        result["refresh_mode"] = mode
    if "refresh_anchor_at" in payload:
        raw = payload["refresh_anchor_at"]
        result["refresh_anchor_at"] = common._iso(common._parse_dt(raw, "refresh_anchor_at")) if raw else None
    if "refresh_enabled" in payload:
        # 暂停/恢复刷新必须是明确布尔值：null 静默变成 false（暂停）是语义陷阱。
        if payload["refresh_enabled"] is None:
            raise common.PlanningError("invalid_payload", "refresh_enabled 必须是布尔值", 400)
        result["refresh_enabled"] = common._clean_bool(payload["refresh_enabled"], "refresh_enabled")

    if "interval_days" in payload:
        result["interval_days"] = common._clean_int(payload.get("interval_days"), "interval_days", lo=1, hi=3650)
    if "after_completion_interval" in payload:
        raw = payload.get("after_completion_interval")
        result["after_completion_minutes"] = (
            common.parse_interval_shorthand(raw, "after_completion_interval")
            if raw is not None else None)
    if "after_completion_minutes" in payload:
        minutes = payload.get("after_completion_minutes")
        if "after_completion_interval" in payload and minutes is not None:
            raise common.PlanningError(
                "invalid_payload",
                "after_completion_interval 与 after_completion_minutes 不能同时提交", 400)
        if minutes is not None:
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not (
                common.MIN_AFTER_COMPLETION_MINUTES <= minutes
                <= common.MAX_AFTER_COMPLETION_MINUTES
            ):
                raise common.PlanningError(
                    "invalid_payload",
                    "after_completion_minutes 必须为 1 至 525600 的整数分钟", 400)
            result["after_completion_minutes"] = minutes
    if "weekdays" in payload:
        result["weekdays"] = common._clean_int_list(payload.get("weekdays"), "weekdays", lo=0, hi=6)
    if "month_days" in payload:
        result["month_days"] = common._clean_int_list(payload.get("month_days"), "month_days", lo=1, hi=31)
    if "target_date" in payload:
        raw = payload.get("target_date")
        result["target_date"] = common._parse_date(raw, "target_date").isoformat() if raw is not None else None
    if effective_type and not partial:
        # 2026-10-01（§32.45）：单次目标日期为可选项——once 不再要求
        # target_date；空日期表达「未指定日期、常驻显示」，不得自动补今天。
        if effective_type == "weekly" and result.get("weekdays") is None:
            raise common.PlanningError("invalid_payload", "每周待办必须选择星期")
        if effective_type == "monthly" and result.get("month_days") is None:
            raise common.PlanningError("invalid_payload", "每月待办必须填写日期")
        if effective_type == "interval":
            # 间隔权威按刷新模式分流（§9.5）：fixed_interval 用 interval_days
            # 天数轴；after_completion 用 after_completion_minutes 时长
            # （1m–365d，d/h/m 文本已解析为分钟）。旧调用以 interval_days
            # 天数表达 after_completion 间隔的，按 days×1440 等价换算
            # （§9.5「旧整数天数据等价于对应的 d 值」，存储仍单一权威）。
            mode = result.get("refresh_mode")
            if mode == "after_completion":
                if (result.get("after_completion_minutes") is None
                        and result.get("interval_days") is None):
                    raise common.PlanningError(
                        "invalid_payload",
                        "处理后刷新间隔不能为空：请填写 1d、2h、30m 或 1d1h1m（纯数字按天）", 400)
                if result.get("after_completion_minutes") is None:
                    result["after_completion_minutes"] = result["interval_days"] * 1440
                    result["interval_days"] = None
            elif mode == "fixed_interval" and result.get("interval_days") is None:
                raise common.PlanningError("invalid_payload", "固定间隔待办必须填写间隔天数")
        if effective_type == "once":
            # 省略与显式 NULL 等价：落库显式 NULL（不依赖列默认值、不补今天）。
            result.setdefault("target_date", None)

    if "time_mode" in payload:
        mode = str(payload.get("time_mode") or "").strip().casefold()
        if mode not in common.TIME_MODES:
            raise common.PlanningError("invalid_payload", "时间模式必须是 duration 或 explicit")
        result["time_mode"] = mode

    if "estimated_minutes" in payload:
        raw = payload.get("estimated_minutes")
        result["estimated_minutes"] = (
            common.parse_duration_shorthand(raw, "estimated_minutes") if raw is not None else None
        )
        if partial and not result["estimated_minutes"]:
            # 四轮修复（Review MEDIUM + user 产品事实核验）：预计耗时是
            # 可自动排程待办的必填信息——创建入口已强制（含 hollow / idle），
            # 前端编辑从不提交空值；编辑入口把任务清成无耗时形状属校验
            # 缺失，会使未来轮次生成无 planned_minutes 快照的实例。清空
            # 一律拒绝；排程层对异常缺耗时行另有防御性 skip 兜底。
            raise common.PlanningError(
                "invalid_payload", "预计耗时不能清空：请填写 1–1440 分钟的有效预计耗时", 400,
            )
    if "window_start_tod" in payload or "window_end_tod" in payload:
        # 批次 6 二轮 MEDIUM：模板编辑入口的时间格式错误统一为项目中文
        # PlanningError（"abc" / 非法时间类型等），不向外暴露英文解析文案。
        try:
            if "window_start_tod" in payload:
                result["window_start_tod"] = common._tod_str(
                    payload.get("window_start_tod"), "window_start_tod")
            if "window_end_tod" in payload:
                result["window_end_tod"] = common._tod_str(
                    payload.get("window_end_tod"), "window_end_tod")
        except common.PlanningError as exc:
            raise common.PlanningError(
                "invalid_payload",
                "可安排时段的时间格式无效：请使用 HH:MM（例如 09:00）", 400,
            ) from exc
    if result.get("window_start_tod") or result.get("window_end_tod"):
        # 形状校验与批次 1 领域构造同源：start == end 无效（不解释为 24h
        # 窗口）；单侧约束合法。boundary 跨越与可行性在创建入口校验。
        try:
            common._task_window_template(result)
        except ValueError as exc:
            raise common.PlanningError(
                "invalid_payload",
                "可安排时段的开始与结束不能相同（相同时刻不代表 24 小时窗口）", 400,
            ) from exc

    if not partial:
        result.setdefault("time_mode", "duration")
        for flag in ("is_fixed", "is_hollow", "alarm_start", "alarm_end"):
            result.setdefault(flag, False)
        result.setdefault("is_active", True)

    if "alarm_start" in payload:
        result["alarm_start"] = common._clean_bool(payload.get("alarm_start"), "alarm_start")
    if "alarm_end" in payload:
        result["alarm_end"] = common._clean_bool(payload.get("alarm_end"), "alarm_end")
    if "timer_minutes" in payload:
        raw = payload.get("timer_minutes")
        result["timer_minutes"] = (
            common.parse_duration_shorthand(raw, "timer_minutes") if raw is not None else None
        )
    if "is_active" in payload:
        result["is_active"] = common._clean_bool(payload.get("is_active"), "is_active")

    if "is_hollow" in payload:
        result["is_hollow"] = common._clean_bool(payload.get("is_hollow"), "is_hollow")
    for field, maximum in (
        ("hollow_start_content", 200),
        ("hollow_wait_note", 200),
        ("hollow_end_content", 200),
    ):
        if field in payload:
            result[field] = common._clean_text(payload.get(field), field, required=False, maximum=maximum)
    for field in ("hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes"):
        if field in payload:
            result[field] = common._clean_int(payload.get(field), field, lo=1, hi=1440)

    if not partial:
        mode = result.get("time_mode", "duration")
        if mode == "explicit":
            # 显式起止随窗口批次停止新写入：新任务一律为耗时（+ 可选时段）。
            raise common.PlanningError(
                "invalid_payload", "显式起止已停用：请改用预计耗时与可安排时段", 400,
            )
        if not result.get("estimated_minutes"):
            raise common.PlanningError("invalid_payload", "仅耗时待办必须填写预计耗时")
        if result.get("is_hollow"):
            for field in (
                "hollow_start_minutes", "hollow_wait_minutes", "hollow_end_minutes",
            ):
                if not result.get(field):
                    raise common.PlanningError("invalid_payload", f"中空待办必须填写 {field}")
            if not (result.get("hollow_start_content") or result.get("content")):
                raise common.PlanningError("invalid_payload", "hollow_start_content is required for hollow tasks")
            if not (result.get("hollow_end_content") or result.get("content")):
                raise common.PlanningError("invalid_payload", "hollow_end_content is required for hollow tasks")

    return result


def _validate_template_window_constraints(
    row: dict[str, Any], now: datetime, *,
    context: cycles.PlanningRequestContext | None = None,
) -> None:
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
        template = common._task_window_template(row)
    except ValueError as exc:
        raise common.PlanningError(
            "invalid_payload",
            "可安排时段的开始与结束不能相同（相同时刻不代表 24 小时窗口）", 400,
        ) from exc
    if template is None:
        return
    occupancy = common._window_occupancy_minutes(row)
    if not isinstance(occupancy, int) or occupancy < 1:
        raise common.PlanningError("invalid_payload", "填写了可安排时段的待办必须提供有效预计耗时", 400)
    if not template.is_bounded:
        return
    boundary, _, _ = context.boundary_state() if context is not None else cycles._load_boundary_state(now)
    try:
        validate_template_window(template, boundary)
    except ValueError as exc:
        raise common.PlanningError(
            "invalid_payload",
            f"可安排时段不能跨越每日刷新时间 {boundary.strftime('%H:%M')}，请调整时段", 400,
        ) from exc


def _validate_window_creation(
    row: dict[str, Any], now: datetime, *,
    context: cycles.PlanningRequestContext | None = None,
) -> None:
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
            today = common._cst_date(now)
            target = common._parse_date(row["target_date"], "target_date")
            if target < today:
                raise common.PlanningError(
                    "invalid_payload",
                    f"目标日期不能早于当前业务日期（{today.isoformat()}）", 400,
                )
        if row.get("window_start_tod") or row.get("window_end_tod"):
            _validate_once_date_window_pair(row)
    _validate_template_window_constraints(row, now, context=context)
    template = common._task_window_template(row)
    if template is None:
        return
    if row["task_type"] == "once":
        # 指定日期 once：user 自然日期 + 时刻组合成固定绝对约束，不做
        # 候选取舍（§32.41）；仅最晚完成已到或越过时按时间过期拒绝，
        # 绝不顺延（§12.1：2026-10-01 剩余不足不再代替「已过期」）。
        resolved = resolve_window_on_date(
            template, common._parse_date(row["target_date"], "target_date"))
        if resolved.end_at is not None and now >= resolved.end_at:
            raise common.PlanningError(
                "invalid_payload",
                f"单次待办的最晚完成（{resolved.end_at.astimezone(common._CST).strftime('%m-%d %H:%M')}）"
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
        raise common.PlanningError(
            "invalid_payload",
            "未指定日期的单次待办不能设置可安排时段：无日期单次常驻显示，"
            "不设最早开始或最晚完成", 400,
        )


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
    if task.get("refresh_mode") not in common._ROUND_SKIP_REFRESH_MODES:
        return False
    template = common._task_window_template(task)
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
    return common._parse_dt(anchor, "refresh_anchor_at") <= now


def _first_round_settlement(
    row: dict[str, Any], now: datetime,
    *, context: cycles.PlanningRequestContext | None = None,
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
    if row.get("refresh_mode") not in common._ROUND_SKIP_REFRESH_MODES:
        return False, None
    template = common._task_window_template(row)
    if template is None or template.end_tod is None:
        return False, None  # 无窗口 / 只有最早开始：没有最晚完成，不存在截止
    mode = row["refresh_mode"]
    if mode == "fixed_interval" and not _fixed_interval_anchor_due(row, now):
        return False, None  # 显式未来首次基准未到期：首轮尚未成为当前轮（R7）
    configured, transition, _ = context.boundary_state() if context is not None else cycles._load_boundary_state(now)
    cycle_key = context.cycle.key if context is not None else cycles._current_cycle(now).key
    if mode in ("fixed_weekday", "fixed_monthday") and not generation._should_occur(row, cycle_key):
        return False, None  # 当前周期无合法轮次：无「当前轮」可跳过（R6）
    if not _round_deadline_passed(row, cycle_key, now, configured, transition):
        return False, None  # 首轮 DUE：保留生成资格（R3-A 恢复语义）
    settle_day = (
        common._parse_dt(row["refresh_anchor_at"], "refresh_anchor_at").date()
        if mode == "fixed_interval" else cycle_key)
    return True, settle_day


def _creation_window_outcome(
    row: dict[str, Any], now: datetime,
    *, context: cycles.PlanningRequestContext | None = None,
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
    template = common._task_window_template(row)
    if template is None or template.end_tod is None:
        return False, False, None
    occupancy = common._window_occupancy_minutes(row)
    if row.get("task_type") == "once":
        resolved = resolve_window_on_date(
            template, common._parse_date(row["target_date"], "target_date"))
        return False, not window_feasible(resolved, now, occupancy), None
    mode = row.get("refresh_mode")
    # 跳过资格（固定刷新型）与冲突反馈资格（固定刷新型 + 处理后刷新型）
    # 分开（R9 审查修复）：after_completion 不跳过首轮，不代表它不需要
    # 检查创建时冲突。
    skip_eligible = mode in common._ROUND_SKIP_REFRESH_MODES
    if not skip_eligible and mode != "after_completion":
        return False, False, None
    if not isinstance(occupancy, int) or occupancy < 1:
        return False, False, None
    if mode == "fixed_interval" and not _fixed_interval_anchor_due(row, now):
        return False, False, None  # 首个轴点未到期：尚无当前轮（R7）
    configured, transition, _ = context.boundary_state() if context is not None else cycles._load_boundary_state(now)
    cycle_key = context.cycle.key if context is not None else cycles._current_cycle(now).key
    if (mode in ("fixed_weekday", "fixed_monthday")
            and not generation._should_occur(row, cycle_key)):
        return False, False, None  # 非规则日没有当前轮（R6 / R10）
    if skip_eligible:
        skipped, settle_day = _first_round_settlement(row, now, context=context)
        if skipped:
            return True, False, settle_day
    # 未跳过时本轮冻结窗口 = 以当前时刻为参考的候选解析（与
    # _resolve_generation_window 同一归属与参考）；剩余装不下即排程冲突。
    resolved = resolve_window(template, cycle_key, now)
    return False, not window_feasible(resolved, now, occupancy), None


# 创建幂等内容快照字段（清单 #9）：取校验归一化后的用户语义输入。
# ``weekdays`` / ``month_days`` 经 ``_clean_int_list`` 排序去重、时刻经
# ``_tod_str`` 归一化、耗时简写解析为整数分钟、处理后刷新间隔解析为分钟
# （``1`` 与 ``1d`` 同义），同义写法视为同内容；
# 服务端注入的缺省（固定间隔 anchor=当前时刻、created_at 等）不参与
# 内容比对——两次相同意图在不同时刻创建的缺省不同不构成「不同内容」。
_CREATION_CONTENT_FIELDS = (
    "content", "task_type", "time_mode", "estimated_minutes",
    "window_start_tod", "window_end_tod",
    "interval_days", "weekdays", "month_days", "target_date",
    "refresh_mode", "refresh_enabled", "refresh_anchor_at",
    "after_completion_minutes",
    "is_hollow", "hollow_start_content", "hollow_start_minutes",
    "hollow_wait_minutes", "hollow_wait_note", "hollow_end_content",
    "hollow_end_minutes",
    "alarm_start", "alarm_end", "timer_minutes", "is_active",
)

# 省略与显式同值的确定性缺省（R4 + 审查补充观察 semantic_default）：
# 快照按语义等价归一——refresh_mode 按任务类型补齐派生缺省（interval
# 必须显式选择，不会落到缺省映射），refresh_enabled 省略 = 显式开启
# （显式 null 已被字段校验拒绝，None 只可能是「未提供」）。
_SEMANTIC_REFRESH_MODE_DEFAULTS = {
    "daily": "daily", "weekly": "fixed_weekday", "monthly": "fixed_monthday",
    "once": "none", "idle": "none",
}


def _creation_request_content(row: dict[str, Any]) -> dict[str, Any]:
    content = {field: row.get(field) for field in _CREATION_CONTENT_FIELDS}
    if content.get("refresh_mode") is None:
        content["refresh_mode"] = _SEMANTIC_REFRESH_MODE_DEFAULTS.get(
            content.get("task_type"))
    if content.get("refresh_enabled") is None:
        content["refresh_enabled"] = True
    return content


def _normalize_stored_creation_content(stored: Any) -> dict[str, Any] | None:
    """旧版本快照兼容（§6.2 / 清单 #9 残留；R05，2026-10-07 复审 #5）：
    按旧快照自身格式做语义等价归一，不能用任务当前定义还原历史请求
    （任务可能已编辑）。

    * **投影到当前字段集合**（R05 修复）：旧版快照缺键（如
      ``after_completion_minutes``）补 None、多出的历史键剔除——旧实现只
      补三个缺省键，daily / once / fixed 快照与新格式形状仍不同，升级后
      原键原内容重试被误报 409；
    * 旧版缺省 ``refresh_enabled`` 未存 → 显式 true（语义一致；显式 false
      与 true 仍为真实差异）；
    * 旧版缺省 ``refresh_mode`` 为 null → 按快照内的任务类型补齐派生缺省
      （类型派生缺省与同值显式模式一致）；
    * after_completion 旧快照以 ``interval_days`` 天数承载间隔 → 等价换算
      为分钟（``3`` ≙ ``3d`` ≙ 4320m），并清空天数键与当前快照形状对齐。
    """
    if stored is None or not isinstance(stored, dict):
        return stored
    content = {field: stored.get(field) for field in _CREATION_CONTENT_FIELDS}
    if content.get("refresh_enabled") is None:
        content["refresh_enabled"] = True
    if content.get("refresh_mode") is None:
        content["refresh_mode"] = _SEMANTIC_REFRESH_MODE_DEFAULTS.get(
            content.get("task_type"))
    if (content.get("refresh_mode") == "after_completion"
            and content.get("after_completion_minutes") is None):
        days = content.get("interval_days")
        if isinstance(days, int) and not isinstance(days, bool):
            content["after_completion_minutes"] = days * 1440
            content["interval_days"] = None
    return content


def _creation_content_digest(stored: Any) -> str | None:
    """创建请求内容的规范化语义摘要（R09，2026-10-07 复审 #9）：sha256
    of canonical JSON。

    物理删除把任务行携带的创建快照正文真正删除，登记表只保留本摘要——
    同键重放按摘要比对（同内容 → 已删除结果；不同内容 → 409）。摘要必须
    与 :func:`_normalize_stored_creation_content` 同一归一口径（旧快照先
    投影 / 补缺省 / 天数换算再摘要），两侧同形才可比。SQL 的 jsonb::text
    键序（长度优先）与 Python canonical JSON（字典序）不一致，摘要只能在
    应用层计算后传入删除 RPC，不能在库内对 jsonb 计算。
    """
    normalized = _normalize_stored_creation_content(stored)
    if not isinstance(normalized, dict):
        return None
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _find_task_by_creation_key(client, key: str) -> dict[str, Any] | None:
    rows = runtime._rows(
        client, "planning_task", lambda q: q.eq("creation_request_key", key))
    return rows[0] if rows else None


def _find_creation_request_tombstone(client, key: str) -> dict[str, Any] | None:
    """已删除创建操作登记（§30.7）：物理删除的带键任务在这里保留请求身份。"""
    rows = runtime._rows(
        client, "planning_creation_request", lambda q: q.eq("request_key", key))
    return rows[0] if rows else None


def _creation_deleted_result() -> dict[str, Any]:
    """同键重放命中已删除操作的稳定结果（§30.7 / C06）：不复建任务、
    不补建实例，明确表达「旧操作已删除，请发起新的创建」。"""
    return {
        "creation_request_deleted": True,
        "idempotent_replay": True,
        "message": "该次创建对应的待办已删除；如需再次创建，请发起新的创建操作",
    }


def _replay_creation_target(
    client, existing: dict[str, Any] | None, tombstone: dict[str, Any] | None,
    request_content: dict[str, Any] | None, idempotency_key: str, now,
) -> dict[str, Any] | None:
    """同键请求的身份收敛：返回重放结果；无既有身份时返回 None 放行首次创建。

    收敛顺序（R3）：任务行在存续期间承载请求身份 → 优先按键查任务；
    物理删除后任务行消失 → 查已删除登记（tombstone）。两条路径都区分
    「同内容重放 / 已删除结果」与「不同内容 409」，绝不悄悄覆盖或新建。

    R14（2026-10-07 复审 #14）：历史保留分支的已删除任务（deleted_at 非空
    但任务行留存）同样先做原创建内容语义比较——真实不同内容 409、同内容
    才返回已删除结果，与物理删除登记分支同一收敛顺序（旧实现该分支先返回
    已删除结果、跳过内容核对，两个删除分支不一致）。

    R09（2026-10-07 复审 #9）：物理删除登记只存语义摘要（sha256）——同键
    重放按摘要比对；摘要缺失 / 不可核对（空串）按内容冲突 409 收敛，不放行
    复建。
    """
    if existing is not None:
        if existing.get("deleted_at"):
            stored = _normalize_stored_creation_content(
                existing.get("creation_request_content"))
            if stored is None or request_content is None or dict(stored) != request_content:
                raise common.PlanningError(
                    "request_conflict",
                    "同一请求键已绑定不同的创建内容；请使用新的请求提交新的待办", 409,
                )
            return _creation_deleted_result()
        return _replay_task_creation(
            client, existing, request_content, idempotency_key, now)
    if tombstone is not None:
        request_digest = _creation_content_digest(request_content)
        if (request_digest is not None
                and tombstone.get("content_digest") == request_digest):
            return _creation_deleted_result()
        raise common.PlanningError(
            "request_conflict",
            "同一请求键已绑定不同的创建内容；请使用新的请求提交新的待办", 409,
        )
    return None


def _is_creation_key_conflict(exc: Exception) -> bool:
    """首次创建命中请求键唯一索引（并发同键的另一请求已提交）。"""
    return "planning_task_creation_key_uq" in str(exc)


def create_task(
    payload: Any, now: datetime | None = None, *,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    now = now or runtime._now()
    row = validate_task_payload(payload, partial=False)
    # 幂等内容快照（清单 #9）在服务端缺省注入前按归一化输入构造。
    request_content = (
        _creation_request_content(row) if idempotency_key else None)
    client = runtime._require_client()
    if idempotency_key:
        # R3：结果未知的重试先于首次创建的时效 / 当前配置准入收敛——
        # 稳定输入归一与按键核对只依赖请求内容，命中同键同内容即按
        # 重放 / 恢复契约返回，不重新用当前时间否认已成立的创建；
        # 首次创建（未命中）才执行依赖 now 与当前配置的准入。
        # §30.7（2026-10-07）：任务行不存在时核对已删除登记——物理删除的
        # 带键任务按「已删除结果」收敛，同键不同内容 409，绝不复建。
        existing = _find_task_by_creation_key(client, idempotency_key)
        tombstone = (
            _find_creation_request_tombstone(client, idempotency_key)
            if existing is None else None)
        replay = _replay_creation_target(
            client, existing, tombstone, request_content, idempotency_key, now)
        if replay is not None:
            return replay
    _prepare_refresh_definition(row, now)
    context = cycles.PlanningRequestContext(now)
    _validate_window_creation(row, now, context=context)
    row["created_at"] = common._iso(now)
    row["updated_at"] = common._iso(now)
    row["is_fixed"] = bool(row.get("is_fixed"))
    # 创建反馈 + 首轮结算（§30.6 / §18.1 / §32.45）：判定、提示与结算在
    # 写入前一次完成；跳过经生成游标随任务行原子落库（R3 稳定裁决），
    # 生成侧不再按当下时间重判。
    first_round_skipped, schedule_conflict, settle_day = _creation_window_outcome(row, now, context=context)
    if settle_day is not None:
        row["refresh_generated_through"] = settle_day.isoformat()
    if idempotency_key:
        # 首次创建把键、内容与反馈随任务行原子落库；并发同键由部分唯一
        # 索引收敛到先提交者（失败方重读后走同一重放分支，R3：不再过
        # 时效准入；先提交者若已被删除，按登记收敛为已删除结果）。
        row["creation_request_key"] = idempotency_key
        row["creation_request_content"] = request_content
        row["creation_feedback"] = {
            "first_round_skipped": first_round_skipped,
            "schedule_conflict": schedule_conflict,
        }
        try:
            response = client.table("planning_task").insert(row).execute()
        except Exception as exc:
            if not _is_creation_key_conflict(exc):
                raise
            existing = _find_task_by_creation_key(client, idempotency_key)
            tombstone = (
                _find_creation_request_tombstone(client, idempotency_key)
                if existing is None else None)
            if existing is None and tombstone is None:
                raise
            replay = _replay_creation_target(
                client, existing, tombstone, request_content, idempotency_key, now)
            if replay is None:
                raise
            return replay
        created = (response.data or [{}])[0]
        # 即时生成：新建的待办（含 interval 立即到期）不等后台循环，
        # 立刻出现在列表。
        _generate_created_task_quietly(created["id"], now, context)
        serialized = presentation.serialize_task(created, now)
        serialized["first_round_skipped"] = first_round_skipped
        serialized["schedule_conflict"] = schedule_conflict
        return serialized
    response = client.table("planning_task").insert(row).execute()
    created = (response.data or [{}])[0]
    # 即时生成：新建的待办（含 interval 立即到期）不等后台循环，立刻出现在列表。
    _generate_created_task_quietly(created["id"], now, context)
    serialized = presentation.serialize_task(created, now)
    # 创建反馈（§30.6 / §18.1）：区分「本轮已截止、次日起生效」与
    # 「已创建但存在排程冲突」，两者都不改变任务已保存的事实。
    serialized["first_round_skipped"] = first_round_skipped
    serialized["schedule_conflict"] = schedule_conflict
    return serialized


def _replay_task_creation(
    client, existing: dict[str, Any], request_content: dict[str, Any],
    idempotency_key: str, now: datetime,
) -> dict[str, Any]:
    """同键重放：核对内容一致后返回既有任务（重排「同键重试恢复」同型）。

    R3：本入口不执行任何依赖当前时间 / 当前配置的首次创建准入——已
    成立的创建不因重试时刻被重新否认，内容核对先于一切时效判定；
    同键不同内容明确拒绝（409），不静默合并。
    """
    stored = _normalize_stored_creation_content(existing.get("creation_request_content"))
    if stored is None or dict(stored) != request_content:
        raise common.PlanningError(
            "request_conflict",
            "同一请求键已绑定不同的创建内容；请使用新的请求提交新的待办", 409,
        )
    # 创建时生成暂时失败（首轮缺失）的重试补一次幂等生成：生成门禁与
    # 轮次唯一键保证不重复；失败安静记录，等待后台循环。
    _generate_created_task_quietly(
        existing["id"], now, cycles.PlanningRequestContext(now))
    serialized = presentation.serialize_task(existing, now)
    feedback = existing.get("creation_feedback") or {}
    serialized["first_round_skipped"] = bool(feedback.get("first_round_skipped"))
    serialized["schedule_conflict"] = bool(feedback.get("schedule_conflict"))
    serialized["idempotent_replay"] = True
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
            raise common.PlanningError("invalid_payload", "间歇待办必须明确选择刷新模式", 400)
        mode = default
    try:
        validate_task_refresh_mode(task_type, mode)
    except ValueError as exc:
        raise common.PlanningError("invalid_payload", "刷新模式与待办类型不匹配", 400) from exc
    if mode == "after_completion":
        # 间隔唯一权威 = after_completion_minutes（§9.5，2026-10-07）：
        # interval_days 在 after_completion 行上必须为空，双列不能各自变化
        # 造成两个真实间隔；fixed_interval 继续使用 interval_days 天数轴。
        # R06（2026-10-07 复审 #6）：间隔解析以**本次请求的显式输入**优先
        # ——先归一本次 PATCH / 创建输入，再回退旧任务行；旧实现先合并
        # 旧任务再取合并后的分钟值，导致「旧分钟吞掉本次天数修改」（
        # PATCH interval_days:3 在 after_completion_minutes=1440 的任务上
        # 返回成功却仍是 1440、interval_days 被置空）。旧调用 / 旧任务行以
        # interval_days 天数表达的，按 days×1440 等价换算（「1」≙「1d」≙
        # 1440m，语义不变）；两键同请求且不等价 → 明确拒绝，不静默取舍。
        if current is None:
            # 创建路径：validate_task_payload 已把 interval_days 换算为分钟。
            minutes = row.get("after_completion_minutes")
        else:
            patch_minutes = row.get("after_completion_minutes")
            patch_days = row.get("interval_days")
            if (patch_minutes is not None and patch_days is not None
                    and patch_days * 1440 != patch_minutes):
                raise common.PlanningError(
                    "invalid_payload",
                    "after_completion_minutes 与 interval_days 不能同时提交"
                    "不同的间隔值；请只填写一个间隔", 400,
                )
            if patch_minutes is not None:
                minutes = patch_minutes
            elif patch_days is not None:
                minutes = patch_days * 1440
            else:
                minutes = current.get("after_completion_minutes")
                if minutes is None and isinstance(current.get("interval_days"), int) \
                        and not isinstance(current.get("interval_days"), bool):
                    minutes = current["interval_days"] * 1440
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not (
            common.MIN_AFTER_COMPLETION_MINUTES <= minutes
            <= common.MAX_AFTER_COMPLETION_MINUTES
        ):
            raise common.PlanningError(
                "invalid_payload",
                "处理后刷新间隔必须为 1 分钟至 365 天的有效时长"
                "（如 30m、2h、1d1h1m；纯数字按天）", 400,
            )
        row["after_completion_minutes"] = minutes
        row["interval_days"] = None
    elif combined.get("task_type") == "interval" and not 1 <= (combined.get("interval_days") or 0) <= 3650:
        raise common.PlanningError("invalid_payload", "固定间隔必须为 1 至 3650 天", 400)
    row["refresh_mode"] = mode
    mode_changed = current is not None and current.get("refresh_mode") != mode
    if mode_changed:
        row["refresh_generated_through"] = None
    if mode == "fixed_interval":
        row["refresh_anchor_at"] = (
            row.get("refresh_anchor_at")
            or (current.get("refresh_anchor_at") if current and not mode_changed else None)
            or common._iso(now)
        )
        if mode_changed:
            row["last_handled_at"] = None
    else:
        if row.get("refresh_anchor_at"):
            raise common.PlanningError("invalid_payload", "只有固定间隔待办可以设置刷新起点", 400)
        if mode_changed:
            row["refresh_anchor_at"] = None
    if mode_changed and mode != "after_completion":
        row["last_handled_at"] = None
        row["refresh_next_due_at"] = None
    elif mode_changed:
        row["refresh_next_due_at"] = None
    elif mode == "after_completion" and "after_completion_minutes" in row:
        # 编辑间隔后基于既有合法处理基准重算下一到期；基准缺失保持空
        # （生成侧从轮次行 handled_at 自愈），不改写历史处理时间。
        handled = combined.get("last_handled_at")
        row["refresh_next_due_at"] = (
            common._iso(common._parse_dt(handled, "last_handled_at")
                        + timedelta(minutes=row["after_completion_minutes"]))
            if handled else None
        )
    if combined.get("is_fixed") and not combined.get("est_start_tod"):
        raise common.PlanningError("invalid_payload", "固定时间必须有有效的预估开始时间", 400)


def _generate_created_task_quietly(
    task_id: int, now: datetime, context: cycles.PlanningRequestContext,
) -> None:
    """Create the new task's due round immediately, then schedule the whole cycle."""
    with runtime._maintenance_lock:
        try:
            result = generation.generate_task(task_id, now, context=context)
        except Exception:
            log.exception("planning 新任务同步生成失败（等待后台循环重试）: task=%s", task_id)
            return
        if not common._should_recompute_after_generation(result):
            return
        try:
            recompute._recompute_for_cycle(now, context.cycle)
        except Exception:
            log.exception("planning 新任务同步重算失败: task=%s", task_id)


def _generate_due_quietly(client, now: datetime) -> None:
    """写操作后的同步补生成：幂等，失败只记日志，不吞掉已成功的写操作。

    当天有新生成实例、或 generation 报告了 task-level failure（partial
    create 的 created 计数会丢失，见 `_should_recompute_after_generation`）
    时，顺带重算一次，让用户立刻看到带起止时间的列表；重算以排列顺序与
    固定槽为准，不会动用户已固定的内容。
    最终修复（问题 3）：与 once 身份编辑共享 _maintenance_lock——生成
    （含本入口）不得与 once 编辑的「检查 + 保存」交错产生半状态。
    """
    with runtime._maintenance_lock:
        try:
            result = generation.generate_due(now)
        except Exception as exc:
            log.warning(
                "planning 同步生成失败（等待后台循环重试）: error=%s", type(exc).__name__,
            )
            return
        if not common._should_recompute_after_generation(result):
            return
        try:
            recompute.recompute_today(now)
        except Exception as exc:
            log.warning("planning 同步重算失败: error=%s", type(exc).__name__)


def _close_out_recurrence_before_switch(client, old_task: dict[str, Any], now: datetime) -> None:
    """规则切换 Phase A（2026-09-28 一轮 Review BLOCKER 1 裁决）：旧规则收尾。

    rule_switch_at = 本次规则编辑生效时刻（= now）。旧规则负责全部
    ``due_at <= rule_switch_at`` 的轮次——用**修改前的任务快照**按既有
    reconcile 语义补齐旧轴已到期但尚未生成的漏轮、执行既有的固定到期
    清理与开放轮次顺延。收尾失败（生成异常等）时异常向上传播：新规则
    一律不保存（Phase B 不执行），已补齐的旧轴轮次是旧规则欠下的合法
    事实，重试整个编辑时 Phase A 幂等（轮次唯一键）。
    """
    configured, transition, absorbed = cycles._load_boundary_state(now)
    cycle = planning_cycle_at(now, configured, transition)
    daily_enabled = cycles.get_cycle_settings(now)["daily_refresh_enabled"]
    generation._reconcile_task_rounds(
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
        anchor = common._parse_dt(task.get("refresh_anchor_at"), "refresh_anchor_at")
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
    created = common._parse_dt(task.get("created_at"), "created_at")
    start = planning_cycle_at(created, configured, transition).key
    day = start
    first_due: datetime | None = None
    for _ in range(400):
        if day not in absorbed and generation._should_occur(task, day):
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
        nxt = generation._following_fixed_event(task, event_day, due, configured, transition, absorbed)
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
    configured, transition, absorbed = cycles._load_boundary_state(switch_at)
    first = _first_rule_event_after(new_task, switch_at, configured, transition, absorbed)
    if first is None:
        raise common.PlanningError(
            "invalid_task", "无法确定新规则在编辑时刻之后的首个轮次，规则编辑未保存", 409)
    day, _ = first
    return (day - timedelta(days=1)).isoformat()


def update_task(task_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    """任务编辑入口（最终修复问题 3）：与全部生成入口共享
    ``_maintenance_lock``——once 身份编辑的「检查无实例 → 保存新日期」与
    生成创建 once 实例互斥，杜绝「任务日期 ≠ 唯一实例」的交错半状态。
    不改 once 轮次唯一键语义、不新增状态字段。
    """
    with runtime._maintenance_lock:
        return _update_task(task_id, payload, now)


def _update_task(task_id: int, payload: Any, now: datetime | None = None) -> dict[str, Any]:
    now = now or runtime._now()
    client = runtime._require_client()
    task = runtime._fetch_task(client, task_id)
    if not task:
        raise common.PlanningError("not_found", "待办任务不存在", 404)
    row = validate_task_payload(payload, partial=True)
    if not row:
        raise common.PlanningError("invalid_payload", "没有可修改的字段")
    if any(field in row and row[field] != task.get(field) for field in
           ("task_type", "refresh_mode", "refresh_anchor_at")):
        existing = runtime._rows(client, "planning_occurrence", lambda q: q.eq("task_id", task_id).limit(1))
        if existing:
            raise common.PlanningError("round_identity_locked", "已有业务轮次时不能改变刷新模式、类型或首次基准", 409)
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
        raise common.PlanningError("invalid_payload", "仅耗时待办必须提供有效预估耗时", 400)
    # once 已生成后的任务级排程身份锁定（2026-09-28 一轮 Review HIGH 裁决；
    # 二轮 HIGH：「实际变化」判定先做语义规范化——DB ``09:00:00`` ≡ PATCH
    # ``09:00``，不用原始字符串比较）。once 没有「未来轮次」可消费新模板，
    # 已生成实例存在时禁止实际变化地修改 target_date / 未来窗口模板——
    # 否则形成「任务显示新日期、唯一实例仍属旧日期、新日期永不生成」的半
    # 重定向状态。仅语义无变化的幂等 PATCH 按现有语义放行；调整已生成的
    # 这一次走当前实例窗口编辑。身份锁定先于值校验。
    once_identity_edit = merged.get("task_type") == "once" and any(
            field in row
            and common._canonical_template_value(field, row.get(field))
            != common._canonical_template_value(field, task.get(field))
            for field in common._ONCE_LOCKED_TEMPLATE_FIELDS)
    if once_identity_edit:
        # 预检（友好错误；权威复核在写入阶段的锁内 RPC——最终修复问题 3）：
        # once 没有「未来轮次」可消费新模板，已生成实例存在时禁止实际变化
        # 地修改任务日期 / 未来窗口模板。
        existing_once = runtime._rows(
            client, "planning_occurrence", lambda q: q.eq("task_id", task_id).limit(1))
        if existing_once:
            raise common.PlanningError(
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
        today = common._cst_date(now)
        if common._parse_date(row["target_date"], "target_date") < today:
            raise common.PlanningError(
                "invalid_payload",
                f"目标日期不能早于当前业务日期（{today.isoformat()}）", 400,
            )

    # 废弃整个任务：终止后续刷新，并关闭所有仍开放的出现实例。
    reactivated = bool(row.get("is_active")) and not task.get("is_active")
    if reactivated and task.get("deleted_at"):
        # §25（2026-10-07，D09）：已删除的待办不得经旧 is_active=true 恢复
        # ——删除只终止刷新与展示，历史保留分支也不提供恢复入口。
        raise common.PlanningError(
            "invalid_transition", "已删除的待办不能恢复；如需相同待办请重新创建", 409,
        )
    if reactivated and task.get("request_state") == "superseded":
        # H2/I6：被取代的重排请求是终态，不得通过普通启用入口复活。
        raise common.PlanningError(
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
                    and common._canonical_template_value(key, row[key])
                    != common._canonical_template_value(key, task.get(key))
                    for key in row)):
        raise common.PlanningError(
            "invalid_payload",
            "停用待办不能与其它修改同时提交：请单独执行停用操作", 400,
        )
    if row.get("is_active") is False and (task.get("is_active") or task.get("deleted_at")):
        # §25（2026-10-07）：删除整个任务 = 单事务命令——复用
        # planning_discard_task（锁任务行 → 锁全部实例 → 锁内按执行事实
        # 判定：有完成/部分完成事实者收口开放实例并保留全部历史；无事实者
        # 物理删除任务与实例并登记创建请求身份；任一失败整体回滚）。
        # occurrence 关闭 / 物理删除不再发生于事务之外。
        # R09：创建快照正文随任务行物理删除；登记表只保留规范化语义摘要
        # （creation_request_content 创建后不可变，锁前读取即权威）。
        discard_result = runtime._discard_task_atomically(
            client, task_id, now,
            creation_digest=_creation_content_digest(
                task.get("creation_request_content")))
        # 停用命令已在 RPC 内完成全部写入。这里不得再发普通 UPDATE：即使
        # 只写 updated_at，失败也会造成 API 报错而任务实际已删除。
        # 批次 6 收尾（BUG B）：删除释放的时间槽——成功后必须登记重算
        # 请求；登记是 post-commit side effect，失败不伪装成删除失败（quiet）。
        recompute._request_recompute_quietly("task_discarded", now)
        # 删除响应必须表达「业务记录已清除」或「已删除，历史已保留」，
        # 不依赖已不存在的任务行序列化成功结果（§5.2 / D01）。
        return {
            "id": task_id,
            "deleted": True,
            "history_preserved": bool(discard_result.get("history_preserved", True)),
            "closed_occurrences": int(discard_result.get("closed") or 0),
        }

    row["updated_at"] = common._iso(now)
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
        for field in common._RECURRENCE_SWITCH_FIELDS)
    window_changed = any(
        field in row
        and common._canonical_template_value(field, row.get(field))
        != common._canonical_template_value(field, task.get(field))
        for field in ("window_start_tod", "window_end_tod"))
    switching = (
        (recurrence_changed or window_changed)
        and task.get("refresh_mode") in common._FIXED_EXPIRING_MODES
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
        and common._canonical_template_value(field, row[field])
        != common._canonical_template_value(field, task.get(field))
        for field in common.SCHEDULE_FIELDS
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
        except common.PlanningError:
            raise
        except Exception as exc:
            if "once identity locked" in str(exc):
                raise common.PlanningError(
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
        recompute.request_recompute("task_discarded", now)
    elif (schedule_changed or resume_refresh) and not deactivated:
        # 停用命令（is_active=False）不得触发补生成——废弃后新轮次必须
        # 不存在（最终 Debug 问题 1A：single-transaction discard）。
        # 规则变更后只尝试当前应有轮次；唯一键保护既有轮次。
        _generate_due_quietly(client, now)
    return presentation.serialize_task(updated, now)


def _ensure_type_requirements(task: dict[str, Any]) -> None:
    """编辑合并后的完整任务定义必须仍满足其类型的必填字段。

    2026-10-01（§32.45）：once 的 target_date 为可选项，不再属于必填；
    无日期 once 的窗口组合约束由 :func:`_validate_once_date_window_pair`
    在合并视图上单独执行。2026-10-07（§9.5）：interval 类型的间隔权威按
    刷新模式分流——after_completion 用分钟（1m–365d），fixed_interval 沿用
    interval_days 天数。"""
    task_type = task.get("task_type")
    if task_type == "interval":
        if task.get("refresh_mode") == "after_completion":
            minutes = task.get("after_completion_minutes")
            if isinstance(minutes, bool) or not isinstance(minutes, int) or not (
                common.MIN_AFTER_COMPLETION_MINUTES <= minutes
                <= common.MAX_AFTER_COMPLETION_MINUTES
            ):
                raise common.PlanningError(
                    "invalid_payload",
                    "处理后刷新间隔无效：请填写 1d、2h、30m 或 1d1h1m（纯数字按天）", 400,
                )
        else:
            value = task.get("interval_days")
            if value is None or (isinstance(value, bool) or not isinstance(value, int)
                                 or not 1 <= value <= 3650):
                raise common.PlanningError("invalid_payload", "固定间隔待办必须填写间隔天数")
    elif task_type == "weekly":
        weekdays = task.get("weekdays")
        if weekdays is None or (isinstance(weekdays, list) and not weekdays):
            raise common.PlanningError("invalid_payload", "每周待办必须选择星期")
    elif task_type == "monthly":
        month_days = task.get("month_days")
        if month_days is None or (isinstance(month_days, list) and not month_days):
            raise common.PlanningError("invalid_payload", "每月待办必须填写日期")
    if task.get("time_mode") == "explicit" and not task.get("est_start_tod"):
        raise common.PlanningError("invalid_payload", "旧显式起止待办必须保留预估开始时间")
    if task.get("time_mode") == "explicit" and not (
        task.get("est_end_tod") or task.get("estimated_minutes")
    ):
        raise common.PlanningError("invalid_payload", "显式预估时间必须有结束时间或有效耗时")


def list_tasks(include_inactive: bool = True, now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or runtime._now()
    client = runtime._require_client()
    query_fn = None if include_inactive else lambda q: q.eq("is_active", True)
    rows = runtime._rows(client, "planning_task", query_fn)
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
        occ_rows = runtime._rows(
            client, "planning_occurrence", lambda q: q.in_("task_id", once_ids))
        generated = {occ["task_id"] for occ in occ_rows}
    return [
        presentation.serialize_task({**row, "has_generated_occurrence": row["id"] in generated}, now)
        for row in rows
    ]
