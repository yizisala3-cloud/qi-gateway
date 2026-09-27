"""可安排时段窗口的领域数学（一期规范 §6.7 / §12.1 / §17.4 / §18 / §22.5）。

单一真相：模板窗口合法性、boundary 跨越判断、候选窗口取舍、剩余空间与
可行性判断只在本模块实现一次。创建校验、实例窗口冻结与排程冲突判断
（后续施工批次）必须共用这里的一套函数，不得各自重写窗口时间数学。

本模块是纯领域层：无 IO、无数据库依赖、不接线任何现行业务路径——
窗口字段的落地属于批次 2～4，本模块加入后现有 planning 行为完全不变。

正式产品边界（2026-09-27）：创建/编辑入口不得指定早于当前业务日期
（Asia/Shanghai 当日）的目标日期（一期规范 §10 / §30.6 / §32.40）——这是
唯一的日期下限规则；系统内部补生成（离线恢复、漏跑补生成、
after_completion）允许产生早于当前业务日期的 schedule_date / 轮次锚点，
不设部署日下限，继续服从既有生命周期与 §6.7。当前 tzdata 下 Asia/Shanghai
自 1992 年起为固定 +08:00（历史 DST 最后一次结束于 1991-09-15），这是
tzdata 事实而非产品规则；针对历史 DST/fold（如 1991 年 +09:00 期）的处理
与测试属于领域函数的防御性回归保险，不代表产品允许用户创建历史日期待办。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from .planning_domain import BUSINESS_TIMEZONE, parse_refresh_boundary

__all__ = [
    "ResolvedWindow",
    "WindowTemplate",
    "hollow_envelope_duration",
    "hollow_envelope_minutes",
    "remaining_window_space",
    "resolve_window",
    "resolve_window_on_date",
    "validate_template_window",
    "window_crosses_boundary",
    "window_feasible",
]


def _local_minute_time(value: time, field: str) -> time:
    """模板端点必须是无 tzinfo 的业务本地分钟精度时刻（§5.1）。

    与 ``parse_refresh_boundary`` 的既有契约一致：本地时刻模板用 naive
    ``time`` 表达，绝对时间用 aware ``datetime`` 表达，两个契约不得混用。
    """
    if value.tzinfo is not None:
        raise ValueError(f"{field} must be a naive business-local HH:MM time")
    if value.second or value.microsecond:
        raise ValueError(f"{field} must be a minute-precision HH:MM time")
    return value


@dataclass(frozen=True)
class WindowTemplate:
    """任务模板上的可安排时段（§6.7）：一对可选的当日时刻。

    * ``start_tod`` = 最早开始；``end_tod`` = 最晚完成；两者都可独立为空；
    * 双侧窗口允许 ``end_tod < start_tod``（结束在次日的跨自然午夜写法）；
    * ``start_tod == end_tod`` 无效，不解释为 24 小时窗口（§6.7）；
    * 单侧约束是合法形状（§18 四种组合）：只有最早开始或只有最晚完成，
      不伪造缺失的另一端。
    """

    start_tod: time | None = None
    end_tod: time | None = None

    def __post_init__(self) -> None:
        if self.start_tod is not None:
            _local_minute_time(self.start_tod, "window start")
        if self.end_tod is not None:
            _local_minute_time(self.end_tod, "window end")
        if (self.start_tod is None) != (self.end_tod is None):
            return  # 单侧约束：没有「开始==结束」可比较
        if self.start_tod is not None and self.start_tod == self.end_tod:
            raise ValueError(
                "window start and end must differ; equal times are not a 24h window"
            )

    @property
    def is_empty(self) -> bool:
        """无窗口：正常自动排程，允许跨日（§15）。"""
        return self.start_tod is None and self.end_tod is None

    @property
    def is_bounded(self) -> bool:
        """双侧完整窗口：构成一个可判断跨越与候选顺延的时间区间。"""
        return self.start_tod is not None and self.end_tod is not None


@dataclass(frozen=True)
class ResolvedWindow:
    """实例窗口的冻结事实（§6.7）：解析后的绝对时间约束。

    双侧窗口是一对 ``start_at < end_at`` 的完整区间；单侧约束只有一端，
    另一端为 ``None``（不伪造）。这是「窗口事实」：顺延、展示周期变化、
    模板后续修改都不得改写本对象；参考时刻带来的剩余容量差异由
    :func:`remaining_window_space` 派生，绝不回写窗口本身。
    """

    start_at: datetime | None = None
    end_at: datetime | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.start_at, "resolved window start"),
            (self.end_at, "resolved window end"),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{field} must have a timezone")
        if (
            self.start_at is not None and self.end_at is not None
            and _absolute(self.end_at) <= _absolute(self.start_at)
        ):
            # 绝对瞬间先后：回拨日两端可分属 +09/+08 偏移（fold 0/1），
            # 同 tzinfo 的 naive fast path 比较会给出相反结论。
            raise ValueError("resolved window end must follow its start")


def _in_clockwise_open_interval(point: time, start: time, end: time) -> bool:
    """``point`` 是否落在 ``start → end`` 的顺时针开区间内（端点不算）。"""
    if start < end:
        return start < point < end
    if start > end:
        return point > start or point < end
    return False  # start == end 的开区间为空；该形状在模板构造时已被拒绝


def window_crosses_boundary(template: WindowTemplate, boundary: time | str) -> bool:
    """双侧窗口是否跨越每日刷新 boundary（§6.7）。

    boundary 时刻位于开始/结束时刻的顺时针开区间内即跨越；端点接触
    （相等）合法。单侧约束不是区间，没有可跨越的对象，恒为 ``False``
    ——这与 §5.2.2「新 boundary 落在其开始/结束时刻的顺时针开区间内」
    的校验对象一致，不是新产品语义。
    """
    boundary = parse_refresh_boundary(boundary)
    if not template.is_bounded:
        return False
    return _in_clockwise_open_interval(boundary, template.start_tod, template.end_tod)


def validate_template_window(template: WindowTemplate, boundary: time | str) -> None:
    """创建校验、规则编辑与 boundary 修改共用的模板合法性校验（§6.7、§30.6）。

    形状非法（start == end）在 :class:`WindowTemplate` 构造时拒绝；此处
    只负责跨越校验：非法即 ``raise ValueError``，合法原样返回。
    """
    if template.is_bounded and window_crosses_boundary(template, boundary):
        raise ValueError("template window must not cross the daily refresh boundary")


def _absolute(instant: datetime) -> datetime:
    """aware 时刻统一转固定 UTC 表达，供减法与比较使用。

    同一个 ZoneInfo 对象跨越历史偏移 / 夏令时区间的两个 aware datetime，
    直接相减或比较会命中 CPython 的「同 tzinfo 对象」fast path——按相同
    偏移做 naive 运算。Asia/Shanghai 含历史偏移与 DST（如 1991 年夏 +09:00），
    该 fast path 会差出整小时。本模块所有 aware 时刻的减法与比较一律先过
    本函数（``astimezone`` 按日期正确求偏移，绝对转换可靠）。
    """
    return instant.astimezone(timezone.utc)


def _first_candidate_offset(
    base_date: date, tod: time, ref_local: datetime, *, after_end: bool,
) -> int:
    """初步日历定位：候选相对锚点日的日偏移（业务本地日历数学）。

    以 reference 的**业务本地日期与钟面**做日历差计算；绝不把绝对时间差
    除以 24 小时推导日历天数（Asia/Shanghai 的历史偏移使绝对差不等
    于日历差），也绝不逐日枚举扫描（无上限、无 horizon）。

    结果只是**近似起点**：回拨日的重复钟面（fold 0/1 绝对不同）无法仅凭
    本地钟面大小可靠判断先后，调用方必须在候选 datetime 构造后用
    :func:`_resolve_candidate` 做统一的绝对瞬间验证与确定性微调——起点
    至多回退一天即可覆盖钟面等号 / fold 场景，之后逐日前进至满足。

    ``after_end=True``：候选必须严格晚于 reference（该时刻 <= reference
    即已到达/结束，取下一次）；``after_end=False``：候选不早于 reference
    （reference 恰等于该时刻时，当前时刻即可生效）。
    """
    day_delta = (ref_local.date() - base_date).days
    if day_delta < 0:
        return 0  # reference 在锚点日之前：首个候选（锚点日）天然满足
    tod_passes = tod > ref_local.time() if after_end else tod >= ref_local.time()
    return day_delta if tod_passes else day_delta + 1


def _resolve_candidate(
    base_date: date, tod: time, reference_abs: datetime, *, after_end: bool,
) -> datetime:
    """从初步日历定位的钟面出发，按**绝对瞬间**确定最终候选。

    每个日历日的候选钟面先按业务时区构造（fold=0），再叠加 fold=1：
    回拨日的重复钟面（如 Asia/Shanghai 1991-09-15 的 01:15/01:45，
    fold0=+09:00、fold1=+08:00，绝对相差 1 小时）是两个真实的「该时刻」
    出现，按绝对时间排序先后验证；非歧义钟面两个 fold 绝对相同，去重。
    候选绝对时刻逐日严格递增（前跳日 +23h、回拨日 +25h、平日 +24h），
    从 reference 邻近的初步定位出发确定性前进，必然有限终止——初步
    候选不满足条件时调整到下一合法候选，而不是抛 RuntimeError。

    ``after_end=True``（latest / 双端窗口结束判定）：候选绝对瞬间必须
    严格晚于 reference；``after_end=False``（earliest）：候选绝对瞬间
    不早于 reference（reference 恰等于该时刻时，当前时刻即可生效）。
    """
    day = base_date
    while True:
        naive_candidate = datetime.combine(day, tod, tzinfo=BUSINESS_TIMEZONE)
        appearances: list[tuple[datetime, datetime]] = []
        for fold in (0, 1):
            candidate = naive_candidate.replace(fold=fold)
            candidate_abs = _absolute(candidate)
            if appearances and appearances[-1][0] == candidate_abs:
                continue  # 非回拨重复钟面：fold=1 与 fold=0 是同一绝对时刻
            appearances.append((candidate_abs, candidate))
        appearances.sort(key=lambda item: item[0])
        for candidate_abs, candidate in appearances:
            satisfied = (
                candidate_abs > reference_abs if after_end
                else candidate_abs >= reference_abs
            )
            if satisfied:
                return candidate
        day += timedelta(days=1)


def resolve_window(
    template: WindowTemplate, anchor_date: date, reference: datetime,
) -> ResolvedWindow:
    """把模板窗口解析为锚点日上的冻结实例窗口（§6.7、§18）。

    * 锚点日由调用方决定：周期任务 = 生成日；once = 目标日期。
    * reference 先统一转换到业务时区：同一瞬间无论用 Asia/Shanghai、
      UTC 还是其他偏移表达，业务本地日期与钟面唯一，候选必然唯一
      （窗口候选属于业务本地日历数学，不属于绝对 timedelta 天数数学）。
    * 双侧窗口：候选取舍只看是否整体结束——候选结束瞬间 **<= reference
      按绝对瞬间比较** 才顺延至下一候选；已开始未结束继续使用当前候选，
      不存在「起点已过就整体推到明天」的规则。解析结果始终是完整候选
      区间：reference 只影响候选选择，不改写窗口事实；当前剩余容量用
      :func:`remaining_window_space` 派生。
    * 只有最晚完成：以 reference 为基准选择第一个**尚未到达/结束**的
      该时刻（绝对瞬间 > reference）——该时刻已到达或越过即取下一次
      该时刻，不得生成出生即过期的单侧截止。
    * 只有最早开始：以 reference 为基准选择第一个**尚未到达**的该时刻
      （绝对瞬间 >= reference；reference 恰等于该时刻时当前时刻即可
      生效）。冻结后只有下界、没有上界：不存在隐式 boundary 截止，
      最终排程允许跨规划周期（§15），不自动补 user 未填写的最晚完成。
    * 单侧约束不构成时间区间：不做 boundary 跨越校验（§6.7 的跨越
      校验对象是开始/结束时刻的顺时针开区间）。
    * 候选定位按业务本地日历差初定、按绝对瞬间终验（DST 前跳 / 回拨
      的重复与跳过钟面由 :func:`_resolve_candidate` 确定性处理），
      无枚举上限、无日期下限。
    """
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("window reference instant must have a timezone")
    ref_local = reference.astimezone(BUSINESS_TIMEZONE)
    reference_abs = _absolute(reference)
    if template.is_empty:
        return ResolvedWindow()
    if template.is_bounded:
        end_base_date = (
            anchor_date + timedelta(days=1)
            if template.end_tod < template.start_tod else anchor_date
        )
        initial = _first_candidate_offset(
            end_base_date, template.end_tod, ref_local, after_end=True)
        end_at = _resolve_candidate(
            end_base_date + timedelta(days=max(0, initial - 1)),
            template.end_tod, reference_abs, after_end=True)
        offset = (end_at.replace(tzinfo=None).date() - end_base_date).days
        start_at = datetime.combine(
            anchor_date + timedelta(days=offset), template.start_tod,
            tzinfo=BUSINESS_TIMEZONE)
        return ResolvedWindow(start_at=start_at, end_at=end_at)
    if template.start_tod is not None:  # only-earliest：第一个尚未到达的该时刻
        initial = _first_candidate_offset(
            anchor_date, template.start_tod, ref_local, after_end=False)
        start_at = _resolve_candidate(
            anchor_date + timedelta(days=max(0, initial - 1)),
            template.start_tod, reference_abs, after_end=False)
        return ResolvedWindow(start_at=start_at)
    # only-latest：第一个尚未到达/结束的该时刻（不得出生即过期）
    initial = _first_candidate_offset(
        anchor_date, template.end_tod, ref_local, after_end=True)
    end_at = _resolve_candidate(
        anchor_date + timedelta(days=max(0, initial - 1)),
        template.end_tod, reference_abs, after_end=True)
    return ResolvedWindow(end_at=end_at)


def resolve_window_on_date(template: WindowTemplate, on_date: date) -> ResolvedWindow:
    """严格按指定自然日期解析（2026-09-27 分离裁决，规范 §6.7/§10/§32.41）。

    把 user 的自然日期与模板时刻组合成**固定绝对约束**：没有 reference、
    没有「已结束顺延」——它不是「找候选」，而是「user 指定日期上的窗口
    是哪个绝对区间」。user 填写的 00:00 是当天 00:00 本身，绝不是次日的
    第一次出现。与 :func:`resolve_window`（候选解析）是两条不得混用的
    路径：候选解析回答「从 reference 往后找第一个合法且尚未结束的窗口」
    （未指定日期的周期任务）；本函数回答指定日期的事实（once）。

    * 双侧窗口：起点 = 当日 start_tod；终点 = 当日 end_tod，end_tod <
      start_tod（跨自然午夜写法）时终点在次日；
    * 只有最早开始：冻结当日 start_tod（含 00:00），只有下界；
    * 只有最晚完成：冻结当日 end_tod（含 00:00 = 当日零点，绝不滚到
      次日——通用候选语义在该形状下会把「reference 等号取下一次」用上，
      故本路径必须独立实现，不能用候选解析伪装），只有上界；
    * 无窗口：空解析。

    窗口已过去导致不可行的判断不属于本函数：由调用方以
    :func:`window_feasible` 按创建时刻判定（§12.1 创建拒绝，不顺延）。
    """
    if template.is_empty:
        return ResolvedWindow()
    if template.is_bounded:
        end_date = (
            on_date + timedelta(days=1)
            if template.end_tod < template.start_tod else on_date
        )
        return ResolvedWindow(
            start_at=datetime.combine(on_date, template.start_tod, tzinfo=BUSINESS_TIMEZONE),
            end_at=datetime.combine(end_date, template.end_tod, tzinfo=BUSINESS_TIMEZONE),
        )
    if template.start_tod is not None:
        return ResolvedWindow(
            start_at=datetime.combine(on_date, template.start_tod, tzinfo=BUSINESS_TIMEZONE))
    return ResolvedWindow(
        end_at=datetime.combine(on_date, template.end_tod, tzinfo=BUSINESS_TIMEZONE))


def remaining_window_space(
    resolved: ResolvedWindow, reference: datetime,
) -> timedelta | None:
    """从参考时刻（当前时间 / 排程游标）起，窗口还能容纳的剩余空间。

    * 双侧窗口：``end_at - max(reference, start_at)``——完整窗口事实保持
      不变，剩余容量在此派生（§6.7「已开始未结束使用剩余部分」的
      表达形态）；
    * 只有最晚完成：``end_at - reference``；
    * 只有最早开始或无窗口：``None``——不存在上界，不构成空间约束，
      不得伪造一个隐式窗口终点。

    返回值可能为负：参考时刻已越过窗口终点时空间耗尽。aware 时刻的
    比较与相减一律在固定 UTC 域进行（见 :func:`_absolute`），跨历史
    偏移 / 夏令时的同 tzinfo 时刻不会命中 naive fast path。
    """
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ValueError("window reference instant must have a timezone")
    if resolved.end_at is None:
        return None
    reference_abs = _absolute(reference)
    floor_abs = reference_abs
    if resolved.start_at is not None:
        start_abs = _absolute(resolved.start_at)
        if start_abs > reference_abs:
            floor_abs = start_abs
    return _absolute(resolved.end_at) - floor_abs


def window_feasible(
    resolved: ResolvedWindow, reference: datetime, occupancy_minutes: int | timedelta,
) -> bool:
    """占用跨度能否完整放进窗口剩余空间（§12.1 / §18.1）。

    占用跨度：普通待办 = 预计耗时；中空待办 = 包络跨度（见
    :func:`hollow_envelope_minutes`）。创建校验（剩余不足直接拒绝）与
    排程冲突判断（装不下报告冲突）必须共用本判定，禁止两套算法。

    占用跨度接受整数分钟或**精确 timedelta**（2026-09-28 四轮修复：有效
    est 区间可能含秒——PostgreSQL timestamptz 与既有区间都允许秒级事实，
    排程必须尊重真实时长，不得 floor / ceil / round 截断后再校验）。

    无上界约束（无窗口 / 只有最早开始）恒可行：单侧下界「起点不早于
    窗口起点」由排程放置表达（§14.2），不属于空间可行性。
    """
    if isinstance(occupancy_minutes, timedelta):
        if occupancy_minutes <= timedelta(0):
            raise ValueError("occupancy must be a positive duration")
        occupancy = occupancy_minutes
    else:
        # 五轮修复（Review LOW）：分钟分支严格类型门禁——bool 是 int 子类、
        # float 含 1.0/1.5 都不得被静默当成分钟数（与 hollow_envelope_minutes
        # 的 _clean_int 同源契约一致）；接口只允许整数分钟或精确 timedelta。
        if isinstance(occupancy_minutes, bool) or not isinstance(occupancy_minutes, int):
            raise ValueError(
                "occupancy must be an integer number of minutes or a timedelta")
        if occupancy_minutes < 1:
            raise ValueError("occupancy must be at least one minute")
        occupancy = timedelta(minutes=occupancy_minutes)
    space = remaining_window_space(resolved, reference)
    if space is None:
        return True
    return space >= occupancy


def hollow_envelope_minutes(
    start_minutes: int, wait_minutes: int, end_minutes: int,
) -> int:
    """中空待办的占用跨度 = 开始阶段 + 中间等待 + 结束阶段的整个包络
    （§17.4）。等待不是普通排程槽：窗口可行性按包络跨度判断；结束
    阶段的最早开始锚定（开始阶段预计结束 + 等待）仍由排程层按现有
    中空规则处理（§17.2/§17.3），本函数只承担窗口可行性数学。

    各分量与现有 ``_clean_int`` / 数据库 ``integer`` 契约严格同源：
    必须是真正的整数输入（bool 是 int 子类，显式拒绝；float 含 1.0
    一律拒绝），范围 1..1440——负值、零、越界均拒绝，不能用负值互相
    抵消包络；总包络不设额外上限。"""
    for value, field in (
        (start_minutes, "hollow start minutes"),
        (wait_minutes, "hollow wait minutes"),
        (end_minutes, "hollow end minutes"),
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{field} must be an integer number of minutes")
        if not 1 <= value <= 1440:
            raise ValueError(f"{field} must be between 1 and 1440")
    return start_minutes + wait_minutes + end_minutes


def hollow_envelope_duration(
    start_duration: timedelta, wait_minutes: int, end_duration: timedelta,
) -> timedelta:
    """中空包络跨度的**精确时长入口**（2026-09-28 四轮修复）：与
    :func:`hollow_envelope_minutes` 同一包络数学，但开始 / 结束接受精确
    ``timedelta``——有效 est 区间可能含秒，排程预判必须与实际落位使用
    完全相同的真实时长，不得截断成整数分钟。

    等待分量仍是整数分钟（``planned_wait_minutes`` 与 payload / 数据库
    整数契约同源，沿用 1..1440 规则）；开始 / 结束必须为正 timedelta。
    总包络不设额外上限；负值 / 零时长与非法等待一律拒绝。"""
    for value, field in (
        (start_duration, "hollow start duration"),
        (end_duration, "hollow end duration"),
    ):
        if not isinstance(value, timedelta) or value <= timedelta(0):
            raise ValueError(f"{field} must be a positive timedelta")
    if isinstance(wait_minutes, bool) or not isinstance(wait_minutes, int):
        raise ValueError("hollow wait minutes must be an integer number of minutes")
    if not 1 <= wait_minutes <= 1440:
        raise ValueError("hollow wait minutes must be between 1 and 1440")
    return start_duration + timedelta(minutes=wait_minutes) + end_duration
