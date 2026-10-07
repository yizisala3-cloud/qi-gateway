"""Shared planning data semantics, validation primitives and error identities.

This module never reads settings, obtains a database client or samples the clock."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from .planning_domain import EstimatedTimeOwnership
from .planning_window import WindowTemplate, hollow_envelope_minutes


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


# 处理后刷新（after_completion）间隔的分钟权威范围（§9.5，2026-10-07）：
# 至少 1 分钟，总量沿用原上限 365 天。fixed_interval 固定轴不使用本范围。
MIN_AFTER_COMPLETION_MINUTES = 1
MAX_AFTER_COMPLETION_MINUTES = 365 * 1440


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


# 处理后刷新间隔简写（§9.5，2026-10-07）：d→h→m 固定顺序、单位不重复；
# 乱序（1m1d）、重复（1d1d）、小数（1.5h）与未知单位由整体不匹配拒绝。
_INTERVAL_SHORTHAND_RE = re.compile(r"^(?:(\d+)\s*d)?(?:(\d+)\s*h)?(?:(\d+)\s*m)?$")


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
            raise PlanningError("invalid_payload", f"{field} 须为合法的时间（ISO 格式）") from exc
    else:
        raise PlanningError("invalid_payload", f"{field} 不能为空")
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
            raise PlanningError("invalid_payload", f"{field} 不是真实存在的日期") from exc
    raise PlanningError("invalid_payload", f"{field} 须为 YYYY-MM-DD 格式的日期")


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
                raise PlanningError("invalid_payload", f"{field} 须为 HH:MM 格式的时刻")
            return time(int(hour), int(minute))
    raise PlanningError("invalid_payload", f"{field} 须为 HH:MM 格式的时刻")


def _tod_str(value: Any, field: str) -> str | None:
    return _parse_tod(value, field).strftime("%H:%M") if value else None


def parse_duration_shorthand(value: Any, field: str) -> int:
    """计时器/耗时简写：整数分钟，或 ``1h`` / ``30m`` / ``1h30m`` / ``1m30s``。

    秒数进位到分钟（最少 1 分钟），与前端展示一致。
    """
    if isinstance(value, bool):
        raise PlanningError("invalid_payload", f"{field} 须为分钟数或时长简写")
    if isinstance(value, int):
        minutes = value
    elif isinstance(value, str):
        text = value.strip().lower().replace(" ", "")
        if text.isdigit():
            minutes = int(text)
        else:
            match = _SHORTHAND_RE.match(text)
            if not match or not any(match.groups()):
                raise PlanningError("invalid_payload", f"{field} 格式：分钟数，或 1h / 30m / 1h30m 简写")
            hours, mins, secs = (int(g) if g else 0 for g in match.groups())
            minutes = -(-((hours * 60 + mins) * 60 + secs) // 60)
    else:
        raise PlanningError("invalid_payload", f"{field} 须为分钟数或时长简写")
    if not 1 <= minutes <= 1440:
        raise PlanningError("invalid_payload", f"{field} 须在 1 至 1440 分钟之间")
    return minutes


def parse_logged_duration_seconds(value: Any, field: str = "actual_logged_duration") -> int | None:
    """完成时手填的实际耗时（2026-10-01 确认，§12.3 / 清单 #19）。

    输入为 user 原始文本：``h`` / ``m`` / ``s`` 后缀（**无后缀默认分钟**），
    可组合（``1h1m1s``、``1h30m``、``45``）；**可留空**——None / 空白返回
    None（未手填，不是错误）。返回秒粒度整数，不进位（预估耗时的
    ``parse_duration_shorthand`` 秒进位分钟语义不适用本字段）。

    手填值仅存独立字段（``actual_logged_seconds``），不覆盖自动计算的
    ``actual_*`` 事实；非法输入（负数、0、超上限、乱后缀、非文本）以
    中文原因拒绝。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise PlanningError(
            "invalid_payload",
            f"{field} 须为时长文本：纯数字按分钟，或 1h / 30m / 1h1m1s 组合",
        )
    text = value.strip().casefold().replace(" ", "")
    if text.isdigit():
        total = int(text) * 60
    else:
        match = _SHORTHAND_RE.match(text)
        if not match or not any(match.groups()):
            raise PlanningError(
                "invalid_payload",
                f"{field} 格式：纯数字按分钟，或 1h / 30m / 1h1m1s 组合",
            )
        hours, mins, secs = (int(g) if g else 0 for g in match.groups())
        total = hours * 3600 + mins * 60 + secs
    if total <= 0:
        raise PlanningError("invalid_payload", f"{field} 不能为 0 或负数")
    if total > 86400:
        raise PlanningError("invalid_payload", f"{field} 不能超过 24 小时")
    return total


def parse_interval_shorthand(value: Any, field: str = "after_completion_interval") -> int:
    """处理后刷新间隔（§9.5，2026-10-07 确认）：``1``/``1d``/``2h``/``30m``
    /``1d1h1m``/``1h30m``，**纯数字默认天**——与预计耗时简写的「无后缀默认
    分钟」语义严格区分，不得混用解析器。

    接受首尾空格与大小写归一；单位按 ``d→h→m`` 顺序且不重复（乱序
    ``1m1d``、重复 ``1d1d``、小数 ``1.5h``、负数、零、空、未知单位一律
    拒绝）。返回整分钟：1 分钟 ≤ 总时长 ≤ 365 天（既有上限的精度扩展）。
    """
    if isinstance(value, bool):
        raise PlanningError(
            "invalid_payload",
            f"{field} 格式不正确：请填写 1d、2h、30m 或 1d1h1m；纯数字按天",
        )
    if isinstance(value, int):
        days = value
    elif isinstance(value, str):
        text = value.strip().casefold().replace(" ", "")
        if text.isdigit():
            days = int(text)
        else:
            match = _INTERVAL_SHORTHAND_RE.match(text)
            if not match or not any(match.groups()):
                raise PlanningError(
                    "invalid_payload",
                    f"{field} 格式不正确：请填写 1d、2h、30m 或 1d1h1m；纯数字按天",
                )
            d, h, m = (int(g) if g else 0 for g in match.groups())
            minutes = d * 1440 + h * 60 + m
            if not MIN_AFTER_COMPLETION_MINUTES <= minutes <= MAX_AFTER_COMPLETION_MINUTES:
                raise PlanningError(
                    "invalid_payload",
                    f"{field} 最短 1 分钟，总时长不超过 365 天",
                )
            return minutes
    else:
        raise PlanningError(
            "invalid_payload",
            f"{field} 格式不正确：请填写 1d、2h、30m 或 1d1h1m；纯数字按天",
        )
    minutes = days * 1440
    if not MIN_AFTER_COMPLETION_MINUTES <= minutes <= MAX_AFTER_COMPLETION_MINUTES:
        raise PlanningError(
            "invalid_payload",
            f"{field} 最短 1 分钟，总时长不超过 365 天",
        )
    return minutes


def format_interval_shorthand(minutes: int | None) -> str:
    """after_completion 分钟数的规范简写回显（如 1440→``1d``、1501→
    ``1d1h1m``）；与 :func:`parse_interval_shorthand` 互为逆运算。"""
    if not isinstance(minutes, int) or minutes <= 0:
        return ""
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if mins or not parts:
        parts.append(f"{mins}m")
    return "".join(parts)


def _after_completion_interval_minutes(task: dict[str, Any]) -> int | None:
    """after_completion 间隔的统一读取口径（§9.5）：``after_completion_minutes``
    分钟权威优先；旧任务行 / 迁移回填前以 ``interval_days`` 天数表达的按
    days×1440 等价换算（「3」≙「3d」≙ 4320m）。双列同时有值时以分钟为准。
    返回 None 表示间隔缺失（调用方按各自契约拒绝）。"""
    minutes = task.get("after_completion_minutes")
    if isinstance(minutes, int) and not isinstance(minutes, bool):
        return minutes
    days = task.get("interval_days")
    if isinstance(days, int) and not isinstance(days, bool):
        return days * 1440
    return None


def _combine(for_date: date, tod: time) -> datetime:
    return datetime.combine(for_date, tod, tzinfo=_CST)


def _minutes_between(start: datetime, end: datetime) -> int:
    return max(0, round((end - start).total_seconds() / 60))


# ── 校验 ──────────────────────────────────────────────────────────

def _clean_text(value: Any, field: str, *, required: bool, maximum: int) -> str | None:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise PlanningError("invalid_payload", f"{field} 须为文本")
    if required and not text:
        raise PlanningError("invalid_payload", f"{field} 不能为空")
    if len(text) > maximum:
        raise PlanningError("invalid_payload", f"{field} 不能超过 {maximum} 个字符")
    return text or None


def _clean_int(value: Any, field: str, *, lo: int, hi: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlanningError("invalid_payload", f"{field} 须为整数")
    if not lo <= value <= hi:
        raise PlanningError("invalid_payload", f"{field} 须在 {lo} 至 {hi} 之间")
    return value


def _clean_bool(value: Any, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlanningError("invalid_payload", f"{field} 须为布尔值")
    return value


def _clean_int_list(value: Any, field: str, *, lo: int, hi: int) -> list[int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or any(
        isinstance(v, bool) or not isinstance(v, int) for v in value
    ):
        raise PlanningError("invalid_payload", f"{field} 须为整数数组")
    if any(not lo <= v <= hi for v in value):
        raise PlanningError("invalid_payload", f"{field} 的取值须在 {lo} 至 {hi} 之间")
    unique = sorted(set(value))
    if not unique:
        raise PlanningError("invalid_payload", f"{field} 不能为空数组")
    return unique


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


# 2026-10-01 新建首轮裁决（§6.7 / §12.1 / §32.45）适用的固定刷新模式：
# 任务创建时刻当前轮的最晚完成已到或越过时跳过该轮（不生成实例、不制造
# 超时记录），从次日起按原重复规则生效。after_completion（处理后刷新型）
# 没有日历轴、其轮次链依赖首个实例启动，跳过会使它永远等不到首个有效
# 实例——因此不适用本裁决；once 属指定/常驻单次，不在此列。首轮裁决在
# 创建时刻**一次性结算**（R3 审查修复）：跳过经生成游标随任务行落库，
# 生成侧不再按当下时间重判——模板编辑、暂停恢复、重复维护不复活被跳过
# 的首轮，即时生成的暂时失败也不被误判成跳过（首轮 DUE 保留恢复资格）。
_ROUND_SKIP_REFRESH_MODES = ("daily", "fixed_interval", "fixed_weekday", "fixed_monthday")


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


SCHEDULE_FIELDS = {
    "task_type", "interval_days", "weekdays", "month_days", "target_date",
    "refresh_mode", "refresh_anchor_at", "after_completion_minutes",
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
        raise PlanningError("invalid_payload", "实际结束时间不能早于实际开始时间")
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
