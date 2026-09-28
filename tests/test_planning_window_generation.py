"""批次 3 定向测试：实例生成与冻结 + 创建校验（一期规范 §6.7 / §10 / §12.1 / §13.2 / §17.4 / §30.6 / §32.40）。

覆盖：五类周期 + once 的模板窗口解析冻结（生成即冻结）、零自由度窗口的
rule 固定预锚定（机制继承 explicit 分支）、创建入口拒绝（跨 boundary /
start==end / 剩余空间不足 / 过去 target_date）、新行停止写入 deadline 事实、
创建校验与生成冻结同源一致。窗口数学全部经批次 1 领域函数，不重复实现。
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from gateway import planning
from gateway.planning_domain import BUSINESS_TIMEZONE
from gateway.planning_window import resolve_window
from test_planning_phase1b import Context, at


def cst(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=BUSINESS_TIMEZONE)


def iso(day, hour, minute=0):
    return planning._iso(cst(day, hour, minute))


# ── A. 创建校验（§12.1 / §30.6 / §32.40） ─────────────────────────

def test_create_accepts_window_fields_and_serializes():
    with Context() as c:
        task = c.create("daily", at(24, 10), window_start_tod="18:00", window_end_tod="22:00")
        assert task["window_start_tod"] == "18:00"
        assert task["window_end_tod"] == "22:00"


def test_create_rejects_equal_start_and_end():
    # start == end 无效，不解释为 24h 窗口（§6.7）。
    with Context() as c:
        with pytest.raises(planning.PlanningError) as error:
            c.create("daily", at(24, 10), window_start_tod="10:00", window_end_tod="10:00")
        assert error.value.status_code == 400
        assert "不能相同" in str(error.value)


def test_create_rejects_window_crossing_boundary_and_touch_is_legal():
    with Context() as c:
        with pytest.raises(planning.PlanningError) as error:
            c.create("daily", at(24, 10), window_start_tod="05:00", window_end_tod="07:00")
        assert error.value.status_code == 400
        assert "不能跨越每日刷新时间 06:00" in str(error.value)
        # 端点接触合法：start == boundary、end == boundary 均可创建。
        c.create("daily", at(24, 10), window_start_tod="06:00", window_end_tod="08:00")
        c.create("daily", at(24, 10), window_start_tod="00:00", window_end_tod="06:00")


def test_create_rejects_insufficient_space_before_and_after_window_start():
    with Context() as c:
        # 完整窗口 120 分钟 < 预计耗时 180 分钟。
        with pytest.raises(planning.PlanningError) as error:
            c.create("daily", at(24, 10), estimated_minutes=180,
                     window_start_tod="18:00", window_end_tod="20:00")
        assert "剩余空间不足" in str(error.value)
        assert "180" in str(error.value)
        # 已开始未结束：剩余 60 分钟 < 90 分钟 → 拒绝（不存在「整体推到明天」）。
        with pytest.raises(planning.PlanningError) as error:
            c.create("daily", at(24, 10), estimated_minutes=90,
                     window_start_tod="09:00", window_end_tod="11:00")
        assert "剩余空间不足" in str(error.value)
        # 恰好容纳剩余空间 → 可创建。
        c.create("daily", at(24, 10), estimated_minutes=60,
                 window_start_tod="09:00", window_end_tod="11:00")


def test_create_accepts_single_sided_windows_independently():
    # §18 四种组合：单侧约束合法且两端独立可空。
    with Context() as c:
        c.create("daily", at(24, 10), window_start_tod="08:00", window_end_tod=None)
        c.create("daily", at(24, 10), window_start_tod=None, window_end_tod="22:00")


def test_create_hollow_uses_full_envelope_for_feasibility():
    # §17.4：窗口容纳 开始 + 等待 + 结束 的整个包络（120），不是两段之和。
    hollow = dict(is_hollow=True, hollow_start_content="准备", hollow_start_minutes=30,
                  hollow_wait_minutes=60, hollow_end_content="收尾", hollow_end_minutes=30)
    with Context() as c:
        with pytest.raises(planning.PlanningError) as error:
            c.create("daily", at(24, 10), window_start_tod="08:00", window_end_tod="09:30",
                     **hollow)
        assert "剩余空间不足" in str(error.value)
        c.create("daily", at(24, 10), window_start_tod="08:00", window_end_tod="10:00",
                 **hollow)


def test_create_once_allows_today_and_future_target_only():
    with Context() as c:
        c.create("once", at(24, 10), target_date="2026-09-24")
        c.create("once", at(24, 10), target_date="2026-09-30")
        with pytest.raises(planning.PlanningError) as error:
            c.create("once", at(24, 10), target_date="2026-09-23")
        assert "目标日期不能早于当前业务日期（2026-09-24）" in str(error.value)


def test_explicit_time_mode_rejected_with_chinese_reason():
    with Context() as c:
        with pytest.raises(planning.PlanningError) as error:
            c.create("daily", at(24, 10), time_mode="explicit", estimated_minutes=60)
        assert "显式起止已停用" in str(error.value)


# ── B. 五类周期 + once 的窗口继承（生成即冻结，§6.7 / §28.1） ─────

def test_daily_round_freezes_window_and_stops_deadline_facts():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["round_key"]) == ("2026-09-24", "cycle:2026-09-24")
        assert occ["window_start_at"] == iso(24, 18)
        assert occ["window_end_at"] == iso(24, 22)
        # 生成行恒为无 deadline 形状（1A CHECK：is_limited = deadline_at 非空）
        assert occ["is_limited"] is False
        assert occ["deadline_at"] is None


def test_weekly_and_monthly_rounds_freeze_window_on_schedule_date():
    with Context() as c:
        c.create("weekly", at(24, 10), weekdays=[3],
                 window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows[0]["window_start_at"] == iso(24, 18)
        assert c.rows[0]["window_end_at"] == iso(24, 22)
    with Context() as c:
        c.create("monthly", at(24, 10), month_days=[24],
                 window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows[0]["window_start_at"] == iso(24, 18)
        assert c.rows[0]["window_end_at"] == iso(24, 22)


def test_fixed_interval_and_after_completion_rounds_freeze_window():
    with Context() as c:
        c.create("interval", at(24, 10), refresh_mode="fixed_interval", interval_days=3,
                 window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows[0]["round_key"].startswith("fixed:2026-09-24:")
        assert c.rows[0]["window_start_at"] == iso(24, 18)
        assert c.rows[0]["window_end_at"] == iso(24, 22)
    with Context() as c:
        c.create("interval", at(24, 10), refresh_mode="after_completion", interval_days=3,
                 window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows[0]["round_key"].startswith("handled:2026-09-24:")
        assert c.rows[0]["window_start_at"] == iso(24, 18)
        assert c.rows[0]["window_end_at"] == iso(24, 22)


def test_once_future_target_freezes_window_on_target_date():
    # once 锚点 = 用户自然日 target_date（§6.7）；未来目标创建时不生成，
    # 到达内部周期才生成并把窗口冻结在目标日上（晚间窗口 → 内部周期 =
    # target_date 本身）。
    with Context() as c:
        c.create("once", at(24, 10), target_date="2026-09-25",
                 window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows == []
        planning.generate_due(at(25, 7))
        assert len(c.rows) == 1
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["display_cycle_date"]) == ("2026-09-25", "2026-09-25")
        assert occ["window_start_at"] == iso(25, 18)
        assert occ["window_end_at"] == iso(25, 22)


def test_once_window_already_ended_rejected_at_creation():
    # 指定日期路径不顺延（§32.41）：创建时自然日窗口已结束且无法容纳耗时
    # → 400 拒绝，绝不滚到下一候选。
    with Context() as c:
        with pytest.raises(planning.PlanningError) as error:
            c.create("once", at(24, 10), target_date="2026-09-24",
                     window_start_tod="03:00", window_end_tod="05:00")
        assert error.value.status_code == 400
        assert "剩余空间不足" in str(error.value)
        assert c.rows == []


def test_once_morning_window_attributed_to_previous_cycle():
    # 分离裁决核心场景：boundary 06:00，9/27 创建「9/28 03:00–05:00」→
    # 绝对窗口恒为 9/28 03:00–05:00，内部 schedule_date = 9/27，9/27 周期
    # 内立即生成展示；task 行 target_date 保持自然日 9/28。
    with Context() as c:
        c.create("once", at(27, 10), target_date="2026-09-28",
                 window_start_tod="03:00", window_end_tod="05:00")
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["display_cycle_date"]) == ("2026-09-27", "2026-09-27")
        assert occ["window_start_at"] == iso(28, 3)
        assert occ["window_end_at"] == iso(28, 5)
        assert c.db.rows["planning_task"][0]["target_date"] == "2026-09-28"


def test_once_morning_window_waits_for_internal_cycle_when_created_early():
    # 9/26 创建（内部周期 9/27 尚未到来）→ 不生成；9/27 刷新提前生成。
    with Context() as c:
        c.create("once", at(26, 22), target_date="2026-09-28",
                 window_start_tod="03:00", window_end_tod="05:00")
        assert c.rows == []
        planning.generate_due(at(27, 7))
        assert len(c.rows) == 1
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["display_cycle_date"]) == ("2026-09-27", "2026-09-27")
        assert occ["window_start_at"] == iso(28, 3)
        assert occ["window_end_at"] == iso(28, 5)


def test_once_cross_midnight_window_attributed_to_target_cycle():
    # 9/28 + 23:00–02:00：绝对窗口 9/28 23:00 → 9/29 02:00，起点在 boundary
    # 之后 → schedule_date = 9/28；9/27 创建时不生成（内部周期未到）。
    with Context() as c:
        c.create("once", at(27, 22), target_date="2026-09-28",
                 window_start_tod="23:00", window_end_tod="02:00")
        assert c.rows == []
        planning.generate_due(at(28, 7))
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["display_cycle_date"]) == ("2026-09-28", "2026-09-28")
        assert occ["window_start_at"] == iso(28, 23)
        assert occ["window_end_at"] == iso(29, 2)


def test_once_only_latest_pre_boundary_instant_attributed_to_previous_cycle():
    # only-latest 9/28 02:00（boundary 06:00）→ schedule_date = 9/27；
    # 冻结终点 = 9/28 02:00 本身。
    with Context() as c:
        c.create("once", at(27, 10), target_date="2026-09-28", window_end_tod="02:00")
        occ = c.rows[0]
        assert occ["schedule_date"] == "2026-09-27"
        assert occ["window_start_at"] is None
        assert occ["window_end_at"] == iso(28, 2)


def test_once_only_latest_midnight_stays_on_target_day():
    # ★ 指定日期 only-latest 00:00 = 当日零点本身：绝不因「reference 等号
    # 取下一次」滚到 9/29（严格解析路径存在的理由）；内部归属按 9/28 00:00
    # 所属周期 = 9/27。
    with Context() as c:
        c.create("once", at(27, 10), target_date="2026-09-28", window_end_tod="00:00")
        occ = c.rows[0]
        assert occ["schedule_date"] == "2026-09-27"
        assert occ["window_start_at"] is None
        assert occ["window_end_at"] == iso(28, 0)


def test_once_only_earliest_keeps_lower_bound_only_semantics():
    # 只有最早开始：冻结下界（9/28 03:00）、无上界；即使创建时刻（9/28
    # 10:00）已越过该下界也照常接受，不凭空补截止（§15/§32.41）。
    with Context() as c:
        c.create("once", at(28, 10), target_date="2026-09-28", window_start_tod="03:00")
        occ = c.rows[0]
        assert occ["schedule_date"] == "2026-09-27"
        assert occ["window_start_at"] == iso(28, 3)
        assert occ["window_end_at"] is None


def test_once_no_window_keeps_schedule_date_equal_target_date():
    # 无窗口 once 沿用现行规则：schedule_date = target_date。
    with Context() as c:
        c.create("once", at(24, 10), target_date="2026-09-26")
        assert c.rows == []
        planning.generate_due(at(26, 7))
        occ = c.rows[0]
        assert occ["schedule_date"] == "2026-09-26"
        assert occ["window_start_at"] is None and occ["window_end_at"] is None


def test_daily_window_already_ended_still_takes_next_candidate():
    # 未指定日期路径不变（§6.7）：周期任务以生成时刻为参考——9/24 10:00
    # 创建 daily（窗口 03:00–05:00 已结束）→ 顺延取 9/25 候选并冻结。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="03:00", window_end_tod="05:00")
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["round_key"]) == ("2026-09-24", "cycle:2026-09-24")
        assert occ["window_start_at"] == iso(25, 3)
        assert occ["window_end_at"] == iso(25, 5)


def test_once_zero_freedom_window_preanchors_on_natural_date():
    # 零自由度 + 指定日期：目标日窗口恰等耗时 → 预锚定在自然日窗口起点
    # （rule 固定），即使生成发生在内部周期当日。
    with Context() as c:
        c.create("once", at(27, 10), target_date="2026-09-28", estimated_minutes=120,
                 window_start_tod="18:00", window_end_tod="20:00")
        assert c.rows == []  # 内部周期 9/28 未到（18:00 ≥ boundary）
        planning.generate_due(at(28, 7))
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["display_cycle_date"]) == ("2026-09-28", "2026-09-28")
        assert (occ["est_start"], occ["est_end"]) == (iso(28, 18), iso(28, 20))
        assert (occ["estimated_time_source"], occ["fixed_source"], occ["is_fixed"]) == (
            "rule", "rule", True)


# ── E. 旧显式任务定义门禁（Review MEDIUM：停止繁殖旧模型实例） ─────

def _seed_legacy_explicit_task(c, task_id=9, **overrides):
    """直接播种旧 time_mode='explicit' 任务定义（创建入口已拒绝该形状）。"""
    row = {
        "id": task_id, "content": "旧显式", "task_type": "daily",
        "refresh_mode": "daily", "refresh_enabled": True,
        "time_mode": "explicit", "estimated_minutes": None,
        "window_start_tod": None, "window_end_tod": None,
        "est_start_tod": "08:00", "est_end_tod": "09:00",
        "is_fixed": True, "deadline_tod": None, "deadline_end_tod": None,
        "weekdays": None, "interval_days": None, "target_date": None,
        "is_hollow": False, "is_active": True,
        "created_at": planning._iso(at(23)), "updated_at": planning._iso(at(23)),
    }
    row.update(overrides)
    c.db.rows["planning_task"].append(row)
    return row


def test_legacy_explicit_definition_stops_breeding_new_rounds():
    # 旧显式定义不得继续产生带 explicit 快照的新实例，也不得静默生成畸形
    # unassigned 实例——生成直接停止（存量实例生命周期另测）。
    with Context() as c:
        _seed_legacy_explicit_task(c)
        planning.generate_due(at(24, 7))
        planning.generate_due(at(25, 7))
        assert c.rows == []


def test_legacy_explicit_existing_rounds_keep_lifecycle_and_snapshot():
    # 存量 explicit 实例继续按兼容读取走完生命周期：顺延照常、快照与
    # rule 固定所有权不被改写、无新轮次。
    with Context() as c:
        _seed_legacy_explicit_task(
            c, task_id=9, task_type="weekly", refresh_mode="fixed_weekday", weekdays=[3])
        c.db.rows["planning_occurrence"].append({
            "id": 1, "task_id": 9, "round_key": "cycle:2026-09-24",
            "schedule_date": "2026-09-24", "display_cycle_date": "2026-09-24",
            "display_reason": "initial", "phase": None, "phase_group": None,
            "for_date": "2026-09-24", "est_start": iso(24, 8), "est_end": iso(24, 9),
            "nominal_start": iso(24, 8), "status": "pending", "planned_minutes": None,
            "planned_wait_minutes": None, "sort_order": 90, "is_fixed": True,
            "estimated_time_source": "rule", "fixed_source": "rule",
            "schedule_managed": True, "is_limited": False,
            "window_start_at": None, "window_end_at": None,
            "time_mode_snapshot": "explicit", "content_snapshot": "旧显式",
            "display_content": "旧显式", "deadline_at": None,
            "source": "schedule", "created_at": planning._iso(at(24)),
            "updated_at": planning._iso(at(24)),
        })
        planning.generate_due(at(25, 7))
        assert len(c.rows) == 1  # 无新轮次繁殖
        occ = c.rows[0]
        assert (occ["display_cycle_date"], occ["display_reason"]) == ("2026-09-25", "carryover")
        assert occ["time_mode_snapshot"] == "explicit"
        assert (occ["estimated_time_source"], occ["fixed_source"]) == ("rule", "rule")


def test_early_completion_rejects_legacy_explicit_definition():
    # 提前完成的额外记录继承生成时快照：旧显式定义不得再产生带 explicit
    # 快照的新行 → 明确 409（存量开放轮次的正常完成不受影响）。
    with Context() as c:
        _seed_legacy_explicit_task(
            c, task_id=9, task_type="interval", refresh_mode="after_completion",
            interval_days=3)
        with pytest.raises(planning.PlanningError) as error:
            planning.complete_task_early(9, at(24, 8), idempotency_key="k1")
        assert error.value.status_code == 409
        assert "旧显式" in str(error.value)
        assert c.rows == []


def test_legacy_deadline_only_definition_still_generates_plain_rounds():
    # 负对照：仅带旧 deadline 定义（time_mode=duration）不受门禁——新行为
    # 普通耗时轮次（无 deadline 事实），deadline 定义本身不繁殖。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        c.db.rows["planning_task"][0]["deadline_tod"] = "12:00"
        planning.generate_due(at(25, 7))
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        assert fresh["is_limited"] is False and fresh["deadline_at"] is None
        assert fresh["time_mode_snapshot"] == "duration"


def _seed_fixed_round_occurrence(c, task_id=9, due_day=17, due_hour=6,
                                 status="pending", occ_id=1, expires_at=None):
    """播种一条带 fixed_due_at 的存量 schedule 轮次（到期清理的判定对象）。

    ``expires_at`` 模拟生成入口随行冻结的 fixed_expires_at（20260928010000）；
    传 None 即存量无冻结事实的形状。"""
    c.db.rows["planning_occurrence"].append({
        "id": occ_id, "task_id": task_id,
        "round_key": f"cycle:2026-09-{due_day:02d}",
        "schedule_date": f"2026-09-{due_day:02d}",
        "display_cycle_date": f"2026-09-{due_day:02d}",
        "display_reason": "initial", "phase": None, "phase_group": None,
        "for_date": f"2026-09-{due_day:02d}",
        "est_start": None, "est_end": None, "nominal_start": None,
        "status": status, "planned_minutes": None, "planned_wait_minutes": None,
        "sort_order": 90, "is_fixed": False,
        "estimated_time_source": "unassigned", "fixed_source": None,
        "schedule_managed": True, "is_limited": False,
        "window_start_at": None, "window_end_at": None,
        "fixed_expires_at": planning._iso(at(*expires_at)) if expires_at else None,
        "time_mode_snapshot": "explicit", "content_snapshot": "旧显式",
        "display_content": "旧显式", "deadline_at": None,
        "closed_at": None, "handled_at": None, "partial_at": None,
        "source": "schedule", "fixed_due_at": planning._iso(at(due_day, due_hour)),
        "created_at": planning._iso(at(due_day, due_hour)),
        "updated_at": planning._iso(at(due_day, due_hour)),
    })


# 三种固定周期的暂停矩阵配置：(任务覆盖字段, 存量轮 fixed_due_at, 越过点运行日,
# 生成时冻结的到期死亡边界 fixed_expires_at)。
# 每种配置下「下一规则点」已越过：若到期清理未被暂停冻结，存量轮必被标 timeout。
LEGACY_PAUSE_MATRIX = [
    ({"task_type": "interval", "refresh_mode": "fixed_interval", "interval_days": 3,
      "refresh_anchor_at": planning._iso(at(14, 7)), "created_at": planning._iso(at(14, 7))},
     17, 7, 20, (20, 7)),
    ({"task_type": "weekly", "refresh_mode": "fixed_weekday",
      "weekdays": [3, 4], "created_at": planning._iso(at(16, 12))},
     17, 6, 18, (18, 6)),
    ({"task_type": "monthly", "refresh_mode": "fixed_monthday",
      "month_days": [10, 20], "created_at": planning._iso(at(5, 12))},
     10, 6, 20, (20, 6)),
]


@pytest.mark.parametrize("task_overrides,due_day,due_hour,run_day,expires_at", LEGACY_PAUSE_MATRIX)
@pytest.mark.parametrize("status", ["pending", "in_progress", "partial"])
def test_paused_legacy_explicit_fixed_rounds_never_expire(
        task_overrides, due_day, due_hour, run_day, expires_at, status):
    # 二轮 Review HIGH：9 组暂停矩阵——legacy explicit 固定型 + refresh_enabled=
    # false 时，越过下一规则点后不生成、不到期清理；pending / in_progress /
    # partial 全部保持原开放状态，不写 closed_at；展示顺延照常（维护存活）。
    # 冻结的 fixed_expires_at 保留原值（暂停只暂停执行，不删不改冻结事实）。
    with Context() as c:
        _seed_legacy_explicit_task(c, task_id=9, refresh_enabled=False, **task_overrides)
        _seed_fixed_round_occurrence(c, task_id=9, due_day=due_day,
                                     due_hour=due_hour, status=status,
                                     expires_at=expires_at)
        planning.generate_due(at(run_day, 7))
        assert len(c.rows) == 1  # 无新轮次繁殖
        occ = c.rows[0]
        assert occ["status"] == status
        assert occ["closed_at"] is None
        assert occ["fixed_expires_at"] == planning._iso(at(*expires_at))
        # 暂停不冻结展示顺延：存量实例照常进入当前周期
        assert occ["display_cycle_date"] == f"2026-09-{run_day:02d}"


def test_paused_normal_duration_fixed_rounds_never_expire():
    # 对照组（需求 24 既有语义）：暂停的正常 duration 固定型同样不生成、
    # 不到期清理——legacy 门禁修复不制造正常路径与 legacy 路径的分叉。
    with Context() as c:
        _seed_legacy_explicit_task(
            c, task_id=9, time_mode="duration", estimated_minutes=30,
            est_start_tod=None, est_end_tod=None, is_fixed=False,
            refresh_enabled=False,
            task_type="interval", refresh_mode="fixed_interval", interval_days=3,
            refresh_anchor_at=planning._iso(at(14, 7)),
            created_at=planning._iso(at(14, 7)))
        _seed_fixed_round_occurrence(c, task_id=9, due_day=17, due_hour=7, status="pending",
                                     expires_at=(20, 7))
        planning.generate_due(at(20, 7))
        assert len(c.rows) == 1
        occ = c.rows[0]
        assert occ["status"] == "pending"
        assert occ["closed_at"] is None


def test_enabled_legacy_explicit_still_expires_existing_fixed_rounds():
    # refresh_enabled=true 的 legacy explicit：generation 仍被永久禁止
    # （无新 explicit 实例），但存量已到期的固定型开放实例照常执行到期清理
    # ——closed_at 取生成时冻结的 fixed_expires_at（播种时无窗口）。
    with Context() as c:
        _seed_legacy_explicit_task(
            c, task_id=9, task_type="weekly", refresh_mode="fixed_weekday",
            weekdays=[3, 4], created_at=planning._iso(at(16, 12)))
        _seed_fixed_round_occurrence(c, task_id=9, due_day=17, due_hour=6, status="pending",
                                     expires_at=(18, 6))
        planning.generate_due(at(18, 7))
        assert len(c.rows) == 1  # 不繁殖新轮次
        occ = c.rows[0]
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == planning._iso(at(18, 6))


def test_resumed_legacy_explicit_still_does_not_breed():
    # 恢复刷新（false → true）：legacy explicit 仍不得开始繁殖新 explicit
    # 实例——门禁只关于定义形状，与暂停状态无关。
    with Context() as c:
        task = _seed_legacy_explicit_task(c, task_id=9, refresh_enabled=False)
        planning.generate_due(at(24, 7))
        assert c.rows == []
        task["refresh_enabled"] = True
        planning.generate_due(at(25, 7))
        assert c.rows == []


def test_window_already_started_freezes_full_candidate():
    # 已开始未结束 → 冻结完整候选窗口事实（03:00→05:00），剩余容量由
    # 排程层按 reference 派生，绝不把窗口改写成 04:00→05:00。
    with Context() as c:
        c.create("daily", at(24, 4), estimated_minutes=30,
                 window_start_tod="03:00", window_end_tod="05:00")
        occ = c.rows[0]
        assert occ["window_start_at"] == iso(24, 3)
        assert occ["window_end_at"] == iso(24, 5)


def test_freeze_survives_carryover_and_template_edit():
    # 冻结窗口不改写：顺延（展示周期变化）与模板后续修改均不追溯；
    # 模板修改只作用于尚未生成的未来轮次（§28.1、不变量 36）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        first = c.rows[0]
        planning.generate_due(at(25, 7))
        # 顺延到 9/25 展示后窗口仍冻结；同批生成的 9/25 轮按旧模板冻结
        planning.generate_due(at(26, 7))
        assert first["display_cycle_date"] == "2026-09-26"
        assert first["window_start_at"] == iso(24, 18)
        assert first["window_end_at"] == iso(24, 22)
        second = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        assert (second["window_start_at"], second["window_end_at"]) == (iso(25, 18), iso(25, 22))
        # 模板窗口修改（编辑入口接线属批次 6，此处播种任务行模拟）
        task_row = c.db.rows["planning_task"][0]
        task_row["window_start_tod"] = "20:00"
        task_row["window_end_tod"] = "23:00"
        planning.generate_due(at(27, 7))
        third = next(row for row in c.rows if row["schedule_date"] == "2026-09-27")
        assert third["window_start_at"] == iso(27, 20)
        assert third["window_end_at"] == iso(27, 23)
        # 已生成轮次不因模板修改被重新解释
        assert first["window_start_at"] == iso(24, 18)
        assert second["window_start_at"] == iso(25, 18)


# ── C. 零自由度窗口：rule 固定预锚定（§6.7 / §13.2 / §14.3） ──────

def test_zero_freedom_window_preanchors_rule_fixed_est():
    # 窗口长恰等于占用跨度 → 唯一可行位置即固定位置；生成期 est 预锚定在
    # 窗口起点，所有权为 rule 固定（机制继承原 explicit 分支）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=120,
                 window_start_tod="18:00", window_end_tod="20:00")
        occ = c.rows[0]
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 18), iso(24, 20))
        assert (occ["estimated_time_source"], occ["fixed_source"]) == ("rule", "rule")
        assert occ["is_fixed"] is True and occ["schedule_managed"] is True
        assert occ["nominal_start"] == iso(24, 18)
        # 预锚定实例不参与普通重算（_freely_schedulable 排除）。
        planning.recompute_today(at(24, 11))
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 18), iso(24, 20))


def test_zero_freedom_cross_midnight_window_preanchors():
    # 23:00→02:00 跨自然午夜 + 180 分钟耗时：窗口长 == 占用跨度 → 预锚定。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=180,
                 window_start_tod="23:00", window_end_tod="02:00")
        occ = c.rows[0]
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 23), iso(25, 2))
        assert (occ["estimated_time_source"], occ["fixed_source"]) == ("rule", "rule")


def test_zero_freedom_hollow_envelope_preanchors_both_phases():
    # 中空包络（30+60+30=120）恰等于窗口长：开始阶段锚在窗口起点，结束
    # 阶段经等待链在窗口终点收口（§17.4），两阶段均为 rule 固定。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30,
                 is_hollow=True, hollow_start_content="准备", hollow_start_minutes=30,
                 hollow_wait_minutes=60, hollow_end_content="收尾", hollow_end_minutes=30,
                 window_start_tod="08:00", window_end_tod="10:00")
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        assert (start["est_start"], start["est_end"]) == (iso(24, 8), iso(24, 8, 30))
        assert (end["est_start"], end["est_end"]) == (iso(24, 9, 30), iso(24, 10))
        assert (start["estimated_time_source"], start["fixed_source"]) == ("rule", "rule")
        assert (end["estimated_time_source"], end["fixed_source"]) == ("rule", "rule")
        for row in (start, end):
            assert row["window_start_at"] == iso(24, 8)
            assert row["window_end_at"] == iso(24, 10)


def test_flexible_window_stays_unassigned_at_generation():
    # 非零自由度窗口不预锚定：生成层不写 est（排程层派生；窗口约束进排程
    # 属批次 4），但窗口事实已随行冻结。直接调用生成入口以排除创建后的
    # 自动重算副作用。
    with Context() as c:
        task = {"id": 7, "content": "灵活", "task_type": "daily", "refresh_mode": "daily",
                "time_mode": "duration", "estimated_minutes": 60,
                "window_start_tod": "08:00", "window_end_tod": "10:00"}
        c.db.rows["planning_task"].append(task)
        planning._create_occurrences(c.db, task, date(2026, 9, 24), at(24, 7))
        occ = c.rows[0]
        assert occ["est_start"] is None and occ["est_end"] is None
        assert occ["estimated_time_source"] == "unassigned"
        assert occ["is_fixed"] is False and occ["fixed_source"] is None
        assert occ["window_start_at"] == iso(24, 8)
        assert occ["window_end_at"] == iso(24, 10)


# ── D. 一致性 / 序列化 / 存量兼容 ─────────────────────────────────

def test_create_check_and_generation_resolve_identically():
    # 创建校验与生成冻结共用批次 1 领域函数：同输入必同结果。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        template = planning._task_window_template(c.db.rows["planning_task"][0])
        resolved = resolve_window(template, date(2026, 9, 24), at(24, 10))
        assert occ["window_start_at"] == planning._iso(resolved.start_at)
        assert occ["window_end_at"] == planning._iso(resolved.end_at)


def test_serialize_exposes_window_fields():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        task_row = c.db.rows["planning_task"][0]
        serialized_occ = planning.serialize_occurrence(occ, task_row, at(24, 11))
        assert serialized_occ["window_start_at"] == iso(24, 18)
        assert serialized_occ["window_end_at"] == iso(24, 22)
        assert task["window_start_tod"] == "18:00"


def test_legacy_deadline_task_generates_rows_without_deadline_facts():
    # 存量限时任务（deadline_tod 停止新写入）：新生成行恒 is_limited=False /
    # deadline_at=None；序列化不回填 deadline（存量行按历史语义走完生命周期）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        task_row = c.db.rows["planning_task"][0]
        task_row["deadline_tod"] = "12:00"
        planning.generate_due(at(25, 7))
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        assert fresh["is_limited"] is False
        assert fresh["deadline_at"] is None
        serialized = planning.serialize_occurrence(fresh, task_row, at(25, 8))
        assert serialized["is_limited"] is False
        assert serialized["deadline_at"] is None


def test_single_sided_freeze_writes_only_one_endpoint():
    # 单侧冻结只有一端（另一端 NULL = 无该端约束，不伪造）。
    with Context() as c:
        # 只有最早开始：07:00 创建，08:00 尚未到达 → 当日该时刻生效。
        c.create("daily", at(24, 7), estimated_minutes=30, window_start_tod="08:00")
        occ = c.rows[0]
        assert occ["window_start_at"] == iso(24, 8)
        assert occ["window_end_at"] is None
    with Context() as c:
        # 只有最晚完成：22:00 尚未到达 → 当日该时刻；不伪造开始端。
        c.create("daily", at(24, 10), estimated_minutes=30, window_end_tod="22:00")
        occ = c.rows[0]
        assert occ["window_start_at"] is None
        assert occ["window_end_at"] == iso(24, 22)


def test_window_end_past_marks_timeout_with_frozen_closed_at():
    # 批次 5 换源：实例冻结窗口终点被真实时间越过 → sweep 打标 timeout，
    # closed_at = 窗口终点（非扫描时刻）；不写处理 / 完成事实。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        result = planning.sweep_timeouts(at(25, 7))
        assert result["timed_out"] == 1
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == iso(24, 22)
        assert occ.get("handled_at") is None
        assert occ.get("actual_end") is None
