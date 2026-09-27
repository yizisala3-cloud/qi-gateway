"""批次 1 定向测试：可安排时段窗口的领域数学（一期规范 §6.7 / §12.1 / §17.4 / §18）。

只测领域数学本身：候选取舍、boundary 跨越（touch ≠ cross）、剩余空间与
可行性、单侧约束、中空包络；不接线生成 / 排程 / 数据库（批次 2～4 的事项），
也不依赖任何 fixture。
"""

from datetime import date, datetime, time, timedelta, timezone

import pytest

from gateway.planning_domain import BUSINESS_TIMEZONE
from gateway.planning_window import (
    ResolvedWindow,
    WindowTemplate,
    hollow_envelope_minutes,
    remaining_window_space,
    resolve_window,
    validate_template_window,
    window_crosses_boundary,
    window_feasible,
)

ANCHOR = date(2026, 9, 28)  # 周期任务锚点 = 生成日；once = 目标日期
BOUNDARY = time(6, 0)       # 默认每日刷新时间

MORNING = WindowTemplate(start_tod=time(3, 0), end_tod=time(5, 0))
NIGHT = WindowTemplate(start_tod=time(23, 0), end_tod=time(2, 0))
NEXT = ANCHOR + timedelta(days=1)


def at(day, hour, minute=0):
    return datetime.combine(day, time(hour, minute), tzinfo=BUSINESS_TIMEZONE)


# ── A. 窗口长度与形状 ─────────────────────────────────────────────

def test_window_length_same_day():
    resolved = resolve_window(
        WindowTemplate(start_tod=time(18, 0), end_tod=time(22, 0)), ANCHOR, at(ANCHOR, 10))
    assert resolved.end_at - resolved.start_at == timedelta(hours=4)


def test_window_length_crosses_natural_midnight():
    resolved = resolve_window(NIGHT, ANCHOR, at(ANCHOR, 10))
    assert resolved.start_at == at(ANCHOR, 23)
    assert resolved.end_at == at(NEXT, 2)
    assert resolved.end_at - resolved.start_at == timedelta(hours=3)


def test_window_length_morning_window():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 2))
    assert resolved.end_at - resolved.start_at == timedelta(hours=2)


def test_equal_start_and_end_are_invalid_not_a_24h_window():
    with pytest.raises(ValueError):
        WindowTemplate(start_tod=time(3, 0), end_tod=time(3, 0))


def test_minute_precision_enforced():
    with pytest.raises(ValueError):
        WindowTemplate(start_tod=time(3, 0, second=30), end_tod=time(5, 0))
    with pytest.raises(ValueError):
        WindowTemplate(start_tod=time(3, 0), end_tod=time(5, 0, microsecond=1))


def test_template_endpoints_reject_timezone_aware_times():
    # 裁决（2026-09-27 Review）：模板端点必须是无 tzinfo 的业务本地时刻，
    # 与 parse_refresh_boundary 契约一致；绝不静默丢弃时区。
    with pytest.raises(ValueError):
        WindowTemplate(start_tod=time(3, 0, tzinfo=timezone.utc), end_tod=time(5, 0))
    with pytest.raises(ValueError):
        WindowTemplate(start_tod=time(3, 0),
                       end_tod=time(5, 0, tzinfo=timezone(timedelta(hours=8))))
    with pytest.raises(ValueError):
        WindowTemplate(start_tod=time(23, 0, tzinfo=timezone.utc))


def test_resolved_window_rejects_inverted_or_naive_instants():
    with pytest.raises(ValueError):
        ResolvedWindow(start_at=at(ANCHOR, 5), end_at=at(ANCHOR, 3))
    with pytest.raises(ValueError):
        ResolvedWindow(start_at=datetime(2026, 9, 28, 3, 0))


# ── B. boundary 跨越判断（touch ≠ cross） ────────────────────────

@pytest.mark.parametrize("earliest,latest,crosses", [
    (time(18, 0), time(22, 0), False),  # 合法
    (time(23, 0), time(2, 0), False),   # 合法：跨自然午夜但不跨 boundary
    (time(3, 0), time(5, 0), False),    # 合法：整体在 boundary 之前
    (time(5, 0), time(7, 0), True),     # 非法：跨越 boundary
    (time(23, 0), time(8, 0), True),    # 非法：跨越 boundary
    (time(6, 0), time(8, 0), False),    # 合法：start touch boundary
    (time(0, 0), time(6, 0), False),    # 合法：end touch boundary
])
def test_boundary_0600_touch_is_legal_cross_is_illegal(earliest, latest, crosses):
    template = WindowTemplate(start_tod=earliest, end_tod=latest)
    assert window_crosses_boundary(template, BOUNDARY) is crosses
    if crosses:
        with pytest.raises(ValueError):
            validate_template_window(template, BOUNDARY)
    else:
        validate_template_window(template, BOUNDARY)


@pytest.mark.parametrize("boundary,earliest,latest,crosses", [
    (time(0, 0), time(1, 0), time(5, 0), False),     # boundary 00:00：凌晨窗口合法
    (time(0, 0), time(23, 0), time(2, 0), True),     # 跨越 00:00 boundary
    (time(0, 0), time(5, 0), time(1, 0), True),      # 跨越 00:00 boundary
    (time(0, 0), time(0, 0), time(6, 0), False),     # start touch 00:00
    (time(23, 30), time(0, 0), time(23, 0), False),  # boundary 23:30：日内窗口合法
    (time(23, 30), time(23, 0), time(2, 0), True),   # 23:30 落在 23:00→02:00 内
    (time(12, 0), time(11, 0), time(13, 0), True),   # boundary 12:00 被区间包含
    (time(12, 0), time(12, 0), time(18, 0), False),  # start touch 12:00
    (time(12, 0), time(6, 0), time(12, 0), False),   # end touch 12:00
])
def test_boundary_other_than_default_is_not_hardcoded(boundary, earliest, latest, crosses):
    template = WindowTemplate(start_tod=earliest, end_tod=latest)
    assert window_crosses_boundary(template, boundary) is crosses
    if crosses:
        with pytest.raises(ValueError):
            validate_template_window(template, boundary)
    else:
        validate_template_window(template, boundary)


def test_boundary_accepts_hhmm_string():
    assert window_crosses_boundary(
        WindowTemplate(start_tod=time(5, 0), end_tod=time(7, 0)), "06:00") is True
    assert window_crosses_boundary(
        WindowTemplate(start_tod=time(18, 0), end_tod=time(22, 0)), "06:00") is False


def test_single_sided_template_never_crosses_any_boundary():
    # 单侧约束不是区间：没有可跨越的对象，也不做跨越校验（§6.7 校验对象
    # 是开始/结束时刻的顺时针开区间）。
    template = WindowTemplate(start_tod=time(3, 0))
    validate_template_window(template, time(4, 0))
    assert window_crosses_boundary(template, time(4, 0)) is False


# ── C. 候选窗口：只看是否整体结束 ─────────────────────────────────

def test_candidate_before_start_uses_upcoming_window():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 2))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 3), at(ANCHOR, 5))


def test_candidate_exactly_at_start_uses_current_window():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 3))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 3), at(ANCHOR, 5))


def test_candidate_after_start_keeps_current_window_and_does_not_roll_to_next_day():
    # ★ start 已过去但 end 未过去：继续使用当前窗口。
    # §6.7：不存在「起点已过就整体推到明天」的规则。
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 4))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 3), at(ANCHOR, 5))


def test_candidate_exactly_at_end_takes_next_window():
    # end_at <= reference 即已结束：终点时刻本身取下一候选。
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 5))
    assert (resolved.start_at, resolved.end_at) == (at(NEXT, 3), at(NEXT, 5))


def test_candidate_after_end_takes_next_window():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 5, 30))
    assert (resolved.start_at, resolved.end_at) == (at(NEXT, 3), at(NEXT, 5))


# ── D. 跨自然午夜的候选解析（绝对日期正确性） ─────────────────────

def test_cross_midnight_candidate_before_start_uses_upcoming():
    resolved = resolve_window(NIGHT, ANCHOR, at(ANCHOR, 22))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 23), at(NEXT, 2))


def test_cross_midnight_candidate_after_start_keeps_current():
    resolved = resolve_window(NIGHT, ANCHOR, at(ANCHOR, 23, 30))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 23), at(NEXT, 2))


def test_cross_midnight_candidate_next_day_before_end_keeps_current():
    resolved = resolve_window(NIGHT, ANCHOR, at(NEXT, 1))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 23), at(NEXT, 2))


def test_cross_midnight_candidate_exactly_at_end_takes_next():
    resolved = resolve_window(NIGHT, ANCHOR, at(NEXT, 2))
    assert (resolved.start_at, resolved.end_at) == (
        at(NEXT, 23), at(ANCHOR + timedelta(days=2), 2))


def test_cross_midnight_candidate_after_end_takes_next():
    resolved = resolve_window(NIGHT, ANCHOR, at(NEXT, 3))
    assert (resolved.start_at, resolved.end_at) == (
        at(NEXT, 23), at(ANCHOR + timedelta(days=2), 2))


def test_double_sided_long_date_gap_uses_date_math_without_limits():
    # 裁决（2026-09-27 Review）：删除 400 天枚举上限——锚点比 reference 早
    # 数百天时按日期差直接定位候选（原实现会在同场景抛 RuntimeError）。
    far = ANCHOR + timedelta(days=400)
    resolved = resolve_window(MORNING, ANCHOR, at(far, 4))  # 当日 03→05 尚未结束
    assert (resolved.start_at, resolved.end_at) == (at(far, 3), at(far, 5))

    resolved = resolve_window(MORNING, ANCHOR, at(far, 6))  # 当日 05:00 已结束
    assert (resolved.start_at, resolved.end_at) == (
        at(far + timedelta(days=1), 3), at(far + timedelta(days=1), 5))

    resolved = resolve_window(NIGHT, ANCHOR, at(ANCHOR + timedelta(days=450), 1, 30))
    assert (resolved.start_at, resolved.end_at) == (
        at(ANCHOR + timedelta(days=449), 23), at(ANCHOR + timedelta(days=450), 2))


# ── E. 剩余空间与可行性（窗口事实与剩余容量分离） ─────────────────

def test_feasibility_with_reference_inside_window():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 4))
    assert remaining_window_space(resolved, at(ANCHOR, 4)) == timedelta(minutes=60)
    assert window_feasible(resolved, at(ANCHOR, 4), 30) is True
    # 恰好容纳 = 零自由度，可行（§6.7 固定由时间约束涌现）
    assert window_feasible(resolved, at(ANCHOR, 4), 60) is True
    assert window_feasible(resolved, at(ANCHOR, 4), 90) is False


def test_feasibility_never_rewrites_window_facts():
    # 剩余空间不足不得把窗口事实改写成 04:00→05:00：
    # 解析结果仍是完整 03:00→05:00，剩余容量只是派生值。
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 4))
    window_feasible(resolved, at(ANCHOR, 4), 30)
    window_feasible(resolved, at(ANCHOR, 4), 90)
    remaining_window_space(resolved, at(ANCHOR, 4))
    assert (resolved.start_at, resolved.end_at) == (at(ANCHOR, 3), at(ANCHOR, 5))


def test_feasibility_rejects_nonpositive_occupancy():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 4))
    with pytest.raises(ValueError):
        window_feasible(resolved, at(ANCHOR, 4), 0)


def test_reference_must_be_timezone_aware():
    naive = datetime(2026, 9, 28, 4, 0)
    with pytest.raises(ValueError):
        resolve_window(MORNING, ANCHOR, naive)
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 2))
    with pytest.raises(ValueError):
        remaining_window_space(resolved, naive)


# ── F. 单侧约束（不伪造缺失的一端） ───────────────────────────────

def test_only_earliest_resolves_without_fabricated_end():
    template = WindowTemplate(start_tod=time(3, 0))
    resolved = resolve_window(template, ANCHOR, at(ANCHOR, 2))
    assert resolved.start_at == at(ANCHOR, 3)
    assert resolved.end_at is None
    # 无上界：不构成空间约束，也不伪造隐式窗口终点
    assert remaining_window_space(resolved, at(ANCHOR, 4)) is None
    assert window_feasible(resolved, at(ANCHOR, 4), 10_000) is True


def test_only_earliest_already_past_takes_next_occurrence():
    # 裁决（2026-09-27 Review）：只有最早开始以 reference 为基准选择第一个
    # 尚未到达的该时刻；已越过即取下一次该时刻（冻结后仍只有下界）。
    template = WindowTemplate(start_tod=time(3, 0))
    resolved = resolve_window(template, ANCHOR, at(ANCHOR, 6))
    assert resolved.start_at == at(NEXT, 3)
    assert resolved.end_at is None


def test_only_earliest_long_date_gap_uses_date_math_without_limits():
    # 锚点比 reference 早 500 天：日期数学直接定位，无枚举上限。
    template = WindowTemplate(start_tod=time(3, 0))
    far = ANCHOR + timedelta(days=500)
    resolved = resolve_window(template, ANCHOR, at(far, 4))
    assert resolved.start_at == at(far + timedelta(days=1), 3)
    assert resolved.end_at is None


def test_only_earliest_before_boundary_has_no_implicit_deadline():
    # 只有最早开始、没有最晚完成：不存在隐式 boundary 截止，不自动补
    # user 未填写的最晚完成；最终排程允许跨规划周期（§15，裁决 2/3）。
    template = WindowTemplate(start_tod=time(3, 0))  # 03:00 在 boundary 06:00 之前
    validate_template_window(template, BOUNDARY)  # 不做跨越校验、不拒绝
    resolved = resolve_window(template, ANCHOR, at(ANCHOR, 2))
    assert resolved.start_at == at(ANCHOR, 3)
    assert resolved.end_at is None


def test_only_latest_resolves_without_fabricated_start():
    template = WindowTemplate(end_tod=time(22, 0))
    resolved = resolve_window(template, ANCHOR, at(ANCHOR, 10))
    assert resolved.start_at is None
    assert resolved.end_at == at(ANCHOR, 22)
    assert remaining_window_space(resolved, at(ANCHOR, 10)) == timedelta(hours=12)
    assert window_feasible(resolved, at(ANCHOR, 10), 720) is True
    assert window_feasible(resolved, at(ANCHOR, 10), 721) is False


def test_only_latest_past_reference_takes_next_occurrence():
    # 裁决（2026-09-27 Review）：只有最晚完成以 reference 为基准选择第一个
    # 尚未到达/结束的该时刻——已到达或越过即取下一次该时刻，不得生成
    # 出生即过期/截止的单侧约束。
    template = WindowTemplate(end_tod=time(5, 0))
    resolved = resolve_window(template, ANCHOR, at(ANCHOR, 6))
    assert resolved.end_at == at(NEXT, 5)
    assert resolved.end_at > at(ANCHOR, 6)  # 出生即过期不可能成立
    assert resolved.start_at is None
    assert remaining_window_space(resolved, at(ANCHOR, 6)) == timedelta(hours=23)
    assert window_feasible(resolved, at(ANCHOR, 6), 30) is True


def test_only_latest_long_date_gap_uses_date_math_without_limits():
    # 锚点比 reference 早 500 天：日期数学直接定位，无枚举上限。
    template = WindowTemplate(end_tod=time(22, 0))
    far = ANCHOR + timedelta(days=500)
    resolved = resolve_window(template, ANCHOR, at(far, 3))
    assert resolved.end_at == at(far, 22)
    assert resolved.start_at is None


def test_no_window_resolves_empty_and_always_feasible():
    resolved = resolve_window(WindowTemplate(), ANCHOR, at(ANCHOR, 10))
    assert resolved.start_at is None and resolved.end_at is None
    assert remaining_window_space(resolved, at(ANCHOR, 10)) is None
    assert window_feasible(resolved, at(ANCHOR, 10), 1440) is True


# ── F2. 单侧候选选择的精确矩阵（裁决 2026-09-27：两侧等号语义不同） ──

@pytest.mark.parametrize("ref_hour,expected_day_offset", [
    (2, 0),   # 尚未到达 → 当日该时刻
    (3, 0),   # 恰好等于该时刻 → 当前时刻即可生效（不早于 = 含等号）
    (4, 1),   # 已越过 → 下一次该时刻
])
def test_only_earliest_occurrence_selection_matrix(ref_hour, expected_day_offset):
    resolved = resolve_window(
        WindowTemplate(start_tod=time(3, 0)), ANCHOR, at(ANCHOR, ref_hour))
    assert resolved.start_at == at(ANCHOR + timedelta(days=expected_day_offset), 3)
    assert resolved.end_at is None


@pytest.mark.parametrize("ref_hour,expected_day_offset", [
    (21, 0),  # 尚未到达 → 当日该时刻
    (22, 1),  # 恰好到达该时刻 → 已到达/结束，取下一次（严格晚于 = 不含等号）
    (23, 1),  # 已越过 → 下一次该时刻
])
def test_only_latest_occurrence_selection_matrix(ref_hour, expected_day_offset):
    resolved = resolve_window(
        WindowTemplate(end_tod=time(22, 0)), ANCHOR, at(ANCHOR, ref_hour))
    assert resolved.end_at == at(ANCHOR + timedelta(days=expected_day_offset), 22)
    assert resolved.end_at > at(ANCHOR, ref_hour)  # 出生即过期不可能成立


def test_only_latest_cross_day_reference_matrix():
    template = WindowTemplate(end_tod=time(5, 0))
    assert resolve_window(template, ANCHOR, at(NEXT, 4)).end_at == at(NEXT, 5)
    assert resolve_window(template, ANCHOR, at(NEXT, 5)).end_at == at(NEXT + timedelta(days=1), 5)
    assert resolve_window(template, ANCHOR, at(NEXT, 5, 30)).end_at == at(NEXT + timedelta(days=1), 5)


# ── G. 中空包络跨度（§17.4） ──────────────────────────────────────

def test_hollow_envelope_spans_start_wait_end():
    # 包络 = 开始 + 等待 + 结束（180），不是两段耗时之和（60）；
    # 等待不是普通排程槽，但计入包络以判断窗口能否容纳整个中空轮次。
    assert hollow_envelope_minutes(30, 120, 30) == 180


def test_hollow_envelope_rejects_negative_components():
    # 裁决（2026-09-27 Review）：负值一律拒绝，不得用负值互相抵消包络
    # （总和仍为正的负等待同样拒绝）。
    with pytest.raises(ValueError):
        hollow_envelope_minutes(-30, 60, 30)
    with pytest.raises(ValueError):
        hollow_envelope_minutes(30, -60, 60)
    with pytest.raises(ValueError):
        hollow_envelope_minutes(30, 60, -30)


def test_hollow_envelope_zero_rejected_per_existing_input_contract():
    # 零值沿用现有 hollow_*_minutes 正式输入规则（1..1440）：不允许零，
    # 也不新增别的产品限制。
    with pytest.raises(ValueError):
        hollow_envelope_minutes(30, 0, 30)


def test_hollow_envelope_component_range_follows_existing_contract():
    with pytest.raises(ValueError):
        hollow_envelope_minutes(1441, 10, 10)  # 单段沿用现有 1440 上界
    assert hollow_envelope_minutes(1440, 1440, 1440) == 4320  # 总包络不设额外上限


def test_hollow_envelope_feasibility_uses_full_envelope():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 2))  # 03→05 = 120 分钟
    envelope = hollow_envelope_minutes(30, 60, 30)             # 包络 120
    assert window_feasible(resolved, at(ANCHOR, 2), envelope) is True      # 零自由度
    assert window_feasible(resolved, at(ANCHOR, 2), envelope + 1) is False


def test_hollow_envelope_feasibility_uses_remaining_space():
    resolved = resolve_window(MORNING, ANCHOR, at(ANCHOR, 4))  # 剩余 60 分钟
    assert window_feasible(
        resolved, at(ANCHOR, 4), hollow_envelope_minutes(20, 30, 20)) is False  # 70 > 60
    assert window_feasible(
        resolved, at(ANCHOR, 4), hollow_envelope_minutes(10, 40, 10)) is True   # 60 恰等


# ── H. 第二轮 Review 补强：时区一致性 / 历史偏移 / 日历边界 / 等号跨日 ──
#
# 根因（已修复）：CPython 对同一 tzinfo 对象的两个 aware datetime 做减法/
# 比较走 fast path，按相同偏移做 naive 运算；Asia/Shanghai 含历史偏移与
# DST（1991 年夏 +09:00），跨偏移区间的差会差出整小时。修复后候选定位为
# 业务本地日历数学（reference 先统一到业务时区），绝对比较/减法一律过
# 固定 UTC 域。

def test_same_instant_different_tz_representations_resolve_identically():
    # Codex 反例 A：only-latest=05:00，anchor=1991-06-01（历史 DST 期 +09:00）。
    # 同一瞬间用 Asia/Shanghai / UTC / +09:00 表达必须解析到同一业务候选。
    template = WindowTemplate(end_tod=time(5, 0))
    ref_cst = datetime(2026, 9, 28, 4, 30, tzinfo=BUSINESS_TIMEZONE)
    expected = at(date(2026, 9, 28), 5)
    for ref in (ref_cst, ref_cst.astimezone(timezone.utc),
                ref_cst.astimezone(timezone(timedelta(hours=9)))):
        assert resolve_window(template, date(1991, 6, 1), ref).end_at == expected
    double = WindowTemplate(start_tod=time(3, 0), end_tod=time(5, 0))
    for ref in (ref_cst, ref_cst.astimezone(timezone.utc)):
        resolved = resolve_window(double, date(1991, 6, 1), ref)
        assert (resolved.start_at, resolved.end_at) == (
            at(date(2026, 9, 28), 3), at(date(2026, 9, 28), 5))


def test_historical_shanghai_offset_change_never_raises_or_rolls_wrong_day():
    # Codex 反例 B：anchor=1991-01-01，reference 为 1991-07-02 05:30+09:00
    # （夏令时期间）。旧实现按绝对差除以 24 小时定位，会触发合法输入
    # RuntimeError；现在按业务本地日历定位：7/2 的 05:00 已结束 → 下一次
    # 1991-07-03 05:00。UTC / +09:00 / 业务时区三种表达结果一致。
    template = WindowTemplate(end_tod=time(5, 0))
    ref = datetime(1991, 7, 2, 5, 30, tzinfo=timezone(timedelta(hours=9)))
    expected = datetime(1991, 7, 3, 5, 0, tzinfo=BUSINESS_TIMEZONE)
    for variant in (ref, ref.astimezone(timezone.utc), ref.astimezone(BUSINESS_TIMEZONE)):
        assert resolve_window(template, date(1991, 1, 1), variant).end_at == expected


def test_remaining_space_depends_on_instant_not_tz_representation():
    resolved = resolve_window(
        MORNING, date(1991, 6, 1), datetime(2026, 9, 28, 4, 30, tzinfo=BUSINESS_TIMEZONE))
    ref = datetime(2026, 9, 28, 4, 30, tzinfo=BUSINESS_TIMEZONE)
    assert remaining_window_space(resolved, ref) == timedelta(minutes=30)
    assert remaining_window_space(resolved, ref.astimezone(timezone.utc)) == timedelta(minutes=30)


def test_double_sided_date_gaps_399_400_401_and_multi_year():
    # 修复时区算法后确认没有重新引入 horizon。
    for gap in (399, 400, 401, 500):
        far = ANCHOR + timedelta(days=gap)
        resolved = resolve_window(MORNING, ANCHOR, at(far, 4))  # 当日 03→05 未结束
        assert (resolved.start_at, resolved.end_at) == (at(far, 3), at(far, 5))
    far = ANCHOR.replace(year=ANCHOR.year + 7)  # 数年跨度
    resolved = resolve_window(MORNING, ANCHOR, at(far, 2))
    assert (resolved.start_at, resolved.end_at) == (at(far, 3), at(far, 5))


def test_candidate_math_handles_leap_day_month_end_and_year_end():
    # 闰日候选：候选落在 2028-02-29 本身。
    resolved = resolve_window(
        WindowTemplate(end_tod=time(5, 0)), date(2027, 2, 28), at(date(2028, 2, 29), 4))
    assert resolved.end_at == at(date(2028, 2, 29), 5)
    # 月末滚到次月 1 日。
    resolved = resolve_window(
        WindowTemplate(end_tod=time(5, 0)), date(2026, 12, 31), at(date(2027, 1, 31), 6))
    assert resolved.end_at == at(date(2027, 2, 1), 5)
    # 年末跨年双端：12/31 23:00 → 1/1 02:00。
    resolved = resolve_window(NIGHT, date(2026, 12, 31), at(date(2026, 12, 31), 22))
    assert (resolved.start_at, resolved.end_at) == (
        at(date(2026, 12, 31), 23), at(date(2027, 1, 1), 2))


def test_only_earliest_exact_equality_takes_effect_immediately_across_days():
    # LOW 3：earliest 等号立即生效不得只在锚点当天成立；必须断言绝对日期
    # （第 163 行 `<` 变异为 `<=` 时这些用例必须失败）。
    template = WindowTemplate(start_tod=time(3, 0))
    assert resolve_window(template, ANCHOR, at(NEXT, 3)).start_at == at(NEXT, 3)
    far = ANCHOR + timedelta(days=400)
    assert resolve_window(template, ANCHOR, at(far, 3)).start_at == at(far, 3)
    cross_year = date(2027, 3, 1)
    assert resolve_window(template, ANCHOR, at(cross_year, 3)).start_at == at(cross_year, 3)


def test_hollow_envelope_rejects_non_integer_minutes():
    # 与现有 _clean_int / 数据库 integer 契约严格同源：float（含 1.0）与
    # bool 一律拒绝；合法整数正常工作；总包络不设额外上限。
    for bad in (1.5, 1.0, True):
        with pytest.raises(ValueError):
            hollow_envelope_minutes(bad, 60, 30)
        with pytest.raises(ValueError):
            hollow_envelope_minutes(30, bad, 30)
        with pytest.raises(ValueError):
            hollow_envelope_minutes(30, 60, bad)
    assert hollow_envelope_minutes(30, 60, 30) == 120


# ── I. 第三轮 Review 补强：DST 前跳/回拨的候选与容量 ──────────────
#
# Asia/Shanghai 1991：04-14 02:00(+08) 前跳为 03:00(+09)；09-15 02:00(+09)
# 回拨为 01:00(+08)。回拨日 01:00–02:00 区间钟面歧义（fold0=+09、
# fold1=+08，绝对相差 1 小时）；naive fast path 会把跨偏移的差与比较
# 全部算错，必须经 _absolute() 的绝对瞬间域。

import gateway.planning_window as planning_window
from gateway.planning_window import _absolute

FALL_DATE = date(1991, 9, 15)


def test_dst_fall_back_duplicate_hour_earliest_takes_second_occurrence():
    # only-earliest=01:45：第一次出现（+09:00, fold=0, 16:45Z）已过
    # （reference = 01:15+08:00 fold=1 = 17:15Z）→ 下一次该时刻是同日
    # fold=1 的 01:45（+08:00, 17:45Z），而不是次日；postcondition 不抛错。
    template = WindowTemplate(start_tod=time(1, 45))
    reference = datetime(1991, 9, 15, 1, 15, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    resolved = resolve_window(template, FALL_DATE, reference)
    assert resolved.start_at == datetime(
        1991, 9, 15, 1, 45, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    assert resolved.end_at is None
    # 同一瞬间 UTC / 固定 +09:00 表达结果一致
    for variant in (reference.astimezone(timezone.utc),
                    reference.astimezone(timezone(timedelta(hours=9)))):
        assert resolve_window(template, FALL_DATE, variant).start_at == resolved.start_at


def test_dst_fall_back_duplicate_hour_latest_takes_second_occurrence():
    # only-latest=01:45：reference = 01:15+08:00 fold=1 = 17:15Z；
    # 第一次 01:45（16:45Z）已到达/越过 → 同日 fold=1 的 01:45（17:45Z）。
    template = WindowTemplate(end_tod=time(1, 45))
    reference = datetime(1991, 9, 15, 1, 15, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    resolved = resolve_window(template, FALL_DATE, reference)
    assert resolved.end_at == datetime(
        1991, 9, 15, 1, 45, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    assert resolved.start_at is None
    for variant in (reference.astimezone(timezone.utc),
                    reference.astimezone(timezone(timedelta(hours=9)))):
        assert resolve_window(template, FALL_DATE, variant).end_at == resolved.end_at


def test_dst_fall_back_double_sided_window_and_equal_latest_occurrence():
    # 双端 01:45→03:30：end 候选 03:30（+08:00, 19:30Z，回拨后唯一出现）
    # 尚未结束；start 锚定第一次 01:45（+09:00, fold=0）——已开始未结束，
    # start 在 reference 之前是合法的窗口事实。
    template = WindowTemplate(start_tod=time(1, 45), end_tod=time(3, 30))
    reference = datetime(1991, 9, 15, 1, 15, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    resolved = resolve_window(template, FALL_DATE, reference)
    assert resolved.start_at == datetime(1991, 9, 15, 1, 45, tzinfo=BUSINESS_TIMEZONE)
    assert resolved.end_at == datetime(1991, 9, 15, 3, 30, tzinfo=BUSINESS_TIMEZONE)
    # only-latest=02:00（回拨后唯一出现，18:00Z）：reference 恰等于该时刻
    # → 已到达 → 取下一次 9/16 02:00（无同日 fold=1 可用）。
    latest = WindowTemplate(end_tod=time(2, 0))
    equal_ref = datetime(1991, 9, 15, 2, 0, tzinfo=BUSINESS_TIMEZONE)
    resolved = resolve_window(latest, FALL_DATE, equal_ref)
    assert resolved.end_at == at(FALL_DATE + timedelta(days=1), 2)


def test_resolved_window_order_uses_absolute_instant():
    # 回拨日两端分属 +09/+08（fold 0/1）：01:45(+09, fold=0)=16:45Z →
    # 01:15(+08, fold=1)=17:15Z 实际 +30 分钟，绝对有序，合法；
    # 交换两端实际 -30 分钟，拒绝。naive 比较会给出相反结论。
    first = datetime(1991, 9, 15, 1, 45, tzinfo=BUSINESS_TIMEZONE)               # fold=0
    second = datetime(1991, 9, 15, 1, 15, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    ResolvedWindow(start_at=first, end_at=second)
    with pytest.raises(ValueError):
        ResolvedWindow(start_at=second, end_at=first)


def test_dst_spring_forward_window_capacity():
    # 1991-04-14 01:30→03:30：02:00 前跳（+08→+09），真实容量 60 分钟
    # （钟面 120 分钟是 naive fast path 的错误答案）。
    window = ResolvedWindow(
        start_at=datetime(1991, 4, 14, 1, 30, tzinfo=BUSINESS_TIMEZONE),
        end_at=datetime(1991, 4, 14, 3, 30, tzinfo=BUSINESS_TIMEZONE))
    reference = datetime(1991, 4, 14, 1, 30, tzinfo=BUSINESS_TIMEZONE)
    assert remaining_window_space(window, reference) == timedelta(minutes=60)
    assert remaining_window_space(window, reference.astimezone(timezone.utc)) == timedelta(minutes=60)


def test_dst_fall_back_window_capacity_and_duplicate_hour_cursor():
    # 1991-09-15 00:30→02:30：01:30(+09) 回拨为 00:30(+08)，真实容量
    # 180 分钟；回拨重复小时中的 cursor（01:45+08:00 fold=1 = 17:45Z）
    # 剩余 45 分钟；同一瞬间 UTC 表达结果相同。
    window = ResolvedWindow(
        start_at=datetime(1991, 9, 15, 0, 30, tzinfo=BUSINESS_TIMEZONE),
        end_at=datetime(1991, 9, 15, 2, 30, tzinfo=BUSINESS_TIMEZONE))
    reference = datetime(1991, 9, 15, 0, 30, tzinfo=BUSINESS_TIMEZONE)
    assert remaining_window_space(window, reference) == timedelta(minutes=180)
    cursor = datetime(1991, 9, 15, 1, 45, tzinfo=BUSINESS_TIMEZONE).replace(fold=1)
    assert remaining_window_space(window, cursor) == timedelta(minutes=45)
    assert remaining_window_space(window, cursor.astimezone(timezone.utc)) == timedelta(minutes=45)


def test_absolute_domain_is_required_for_cross_offset_instants():
    # 回归保险：钉住 CPython「同 tzinfo 对象 → naive fast path」行为事实
    # 与 _absolute() 的必要性——DST 切换日跨偏移的两个 aware datetime，
    # 直接相减/比较必然错，经 _absolute() 才是绝对瞬间差。
    spring_start = datetime(1991, 4, 14, 1, 30, tzinfo=BUSINESS_TIMEZONE)
    spring_end = datetime(1991, 4, 14, 3, 30, tzinfo=BUSINESS_TIMEZONE)
    assert spring_end - spring_start == timedelta(minutes=120)  # fast path 错值
    assert _absolute(spring_end) - _absolute(spring_start) == timedelta(minutes=60)
    assert not _absolute(spring_end) <= _absolute(spring_start)


def test_remaining_space_guarded_by_absolute_domain(monkeypatch):
    # 变异守卫：临时令 _absolute() 直接返回输入（模拟绕过绝对域）时，
    # 回拨日容量计算必须偏离正确值——证明 remaining 依赖 _absolute。
    window = ResolvedWindow(
        start_at=datetime(1991, 9, 15, 0, 30, tzinfo=BUSINESS_TIMEZONE),
        end_at=datetime(1991, 9, 15, 2, 30, tzinfo=BUSINESS_TIMEZONE))
    cursor = datetime(1991, 9, 15, 0, 45, tzinfo=BUSINESS_TIMEZONE)  # +09 期
    assert remaining_window_space(window, cursor) == timedelta(minutes=165)
    monkeypatch.setattr(planning_window, "_absolute", lambda value: value)
    try:
        assert remaining_window_space(window, cursor) != timedelta(minutes=165)
    finally:
        monkeypatch.undo()
    assert remaining_window_space(window, cursor) == timedelta(minutes=165)
