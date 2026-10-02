"""2026-10-01 创建校验与单次可选日期定向验收（#17 / #18；§6.7 / §10 / §12.1 /
§18.1 / §30.6 / §32.40 / §32.41 / §32.45 / §33）。

场景来自需求最低验收 A–H 与待修复清单 #17 / #18：

* #17：重复待办新建不受当前剩余时间限制——本轮最晚完成已到或越过时保存
  任务但不生成当前轮、不制造当前轮超时记录，从次日起按原重复规则生效；
  尚未截止但耗时装不下 → 允许创建并呈现排程冲突；单次仅最晚完成已到才
  因时间过期拒绝；只填最早开始不构成过期；跳过首轮与后台维护、暂停恢复、
  重复 generate_due 一致（不补回、不伪造 handled_at / completed、不停用、
  处理后刷新型不因跳过卡死）。
* #18：单次目标日期可选（省略或显式 NULL 等价、不自动补今天）；无日期
  不设窗口（空日期 + 任一窗口端拒绝且零写入）；创建即常驻同一开放实例，
  跨周期 / 刷新 / 重启 / 多次维护唯一，关闭后不再生成；填写日期仍不得早
  于今天；无日期已生成同值保存 NULL 成功，改日期仍受身份锁定。
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from gateway import planning


from tests.support.planning_context import CST, CreationContext as Context, at


def test_scenario_a_daily_deadline_passed_skips_current_round():
    # 10:00 创建每日 08:00–09:00：任务保存成功、当前轮零实例；次日正常
    # 生成；反复维护不补回被跳过的首轮。
    with Context() as c:
        task = c.create("daily", at(24, 10),
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        assert len(c.tasks) == 1  # 任务本身已保存
        # 同日反复维护：不补回被跳过的首轮。
        assert planning.generate_due(at(24, 12))["created"] == 0
        assert planning.generate_due(at(24, 18))["created"] == 0
        assert c.rows == []
        # 次日周期正常生成有效轮次（窗口冻结在次日）。
        assert planning.generate_due(at(25, 6))["created"] == 1
        occ = c.rows[0]
        assert (occ["round_key"], occ["schedule_date"]) == ("cycle:2026-09-25", "2026-09-25")
        assert occ["window_start_at"] == planning._iso(at(25, 8))
        assert occ["window_end_at"] == planning._iso(at(25, 9))
        # 被跳过的 9/24 轮从未出现：无实例、无超时记录、无伪造事实。
        assert all(row["round_key"] != "cycle:2026-09-24" for row in c.rows)
        assert planning.generate_due(at(25, 9))["created"] == 0
        assert len(c.rows) == 1


def test_scenario_a_deadline_exactly_now_counts_as_passed():
    # 截止恰等于当前时刻 = 已到（§12.1：当前时刻 ≥ 绝对截止）→ 跳过。
    with Context() as c:
        task = c.create("daily", at(24, 9),
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []


def test_scenario_a_weekly_skips_current_round_and_keeps_rule_days():
    # 每周四 08:00–09:00，9/24（周四）10:00 创建：跳过周四当前轮；下一个
    # 合法轮次是下周四 10/1，不为「次日起」强造周五轮。
    with Context() as c:
        task = c.create("weekly", at(24, 10), weekdays=[3],
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        assert planning.generate_due(at(25, 6))["created"] == 0  # 周五：不造轮
        assert planning.generate_due(at(29, 6))["created"] == 0  # 周二：不造轮
        assert c.rows == []
        # 10/1（周四）06:00 刷新生成下一轮。
        assert planning.generate_due(at(1, 6, month=10))["created"] == 1
        occ = c.rows[0]
        assert occ["round_key"] == "cycle:2026-10-01"
        assert occ["window_end_at"] == planning._iso(at(1, 9, month=10))
        # 被跳过的 9/24 轮不补回：重复维护计数稳定。
        assert planning.generate_due(at(1, 7, month=10))["created"] == 0
        assert len(c.rows) == 1


def test_scenario_a_monthly_skips_current_round_and_keeps_month_days():
    # 每月 24 日 08:00–09:00，9/24 10:00 创建：跳过本月当前轮；10/24 生成。
    with Context() as c:
        task = c.create("monthly", at(24, 10), month_days=[24],
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        assert planning.generate_due(at(30, 6))["created"] == 0
        assert planning.generate_due(at(24, 6, month=10))["created"] == 1
        assert c.rows[0]["round_key"] == "cycle:2026-10-24"


def test_scenario_a_fixed_interval_skips_current_round_and_keeps_axis():
    # 固定间隔 3 天 08:00–09:00，9/24 10:00 创建（锚点 = 创建时刻）：跳过
    # 首轮、游标推进；下一轴点 9/27 10:00 到期后按候选解析生成（轴不动）。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="fixed_interval",
                        interval_days=3,
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        assert planning.generate_due(at(25, 6))["created"] == 0
        assert c.tasks[0]["refresh_generated_through"] == "2026-09-24"
        # 9/27 轴点 10:00 到期：非首轮，按原规则生成（候选窗口 = 9/28）。
        assert planning.generate_due(at(27, 10))["created"] == 1
        occ = c.rows[0]
        assert occ["round_key"].startswith("fixed:2026-09-27:")
        assert occ["window_start_at"] == planning._iso(at(28, 8))
        assert occ["window_end_at"] == planning._iso(at(28, 9))
        # 重复维护不补回被跳过的首轮，也不重复生成。
        assert planning.generate_due(at(27, 11))["created"] == 0
        assert len(c.rows) == 1


def test_scenario_a_hollow_skips_whole_round_not_half():
    # 中空首轮已截止：整轮跳过（两阶段都不生成），不是只生成一个阶段。
    with Context() as c:
        task = c.create("daily", at(24, 10), is_hollow=True,
                        hollow_start_content="开始", hollow_start_minutes=30,
                        hollow_wait_minutes=60, hollow_end_content="结束",
                        hollow_end_minutes=30,
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        assert planning.generate_due(at(25, 6))["created"] == 2  # 次日两阶段


def test_scenario_a_after_completion_never_skips_first_round():
    # 处理后刷新型没有日历轴：跳过首轮会让它永远等不到首个有效实例
    # （§6.7 明文禁止）→ 不跳过，实例照常生成，处理后按原规则续链。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="after_completion",
                        interval_days=3,
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is False
        assert len(c.rows) == 1
        # 处理后从处理时间推进下一轮（3 天后），链路不因首轮窗口已过卡死。
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 11))
        assert planning.generate_due(at(27, 12))["created"] == 1
        assert len(c.rows) == 2


def test_scenario_a_pause_resume_does_not_backfill_skipped_first_round():
    # 暂停 / 恢复一致性：首轮被跳过后，同日恢复不把首轮补回来；次日起按
    # 原规则正常生成。既有任务的迟到补生成沿用候选解析（§7.1.1 对照）。
    with Context() as c:
        task = c.create("daily", at(24, 10),
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 11))
        planning.update_task(task["id"], {"refresh_enabled": True}, at(24, 15))
        # 恢复触发同步补生成：首轮（9/24）窗口已过，不补回。
        assert planning.generate_due(at(24, 15))["created"] == 0
        assert c.rows == []
        # 次日周期正常生成。
        assert planning.generate_due(at(25, 6))["created"] == 1
        assert c.rows[0]["round_key"] == "cycle:2026-09-25"


def test_existing_task_missed_round_keeps_candidate_semantics():
    # 对照（§7.1.1）：既有任务的当前周期轮次迟到补生成沿用候选解析，不按
    # 首轮裁决跳过——每日 08:00–09:00 于 9/24 06:00 正常创建，9/25 全天
    # 离线、9/26 06:00 恢复：9/26 当期轮照常生成。
    with Context() as c:
        task = c.create("daily", at(24, 6),
                        window_start_tod="08:00", window_end_tod="09:00")
        assert len(c.rows) == 1
        assert planning.generate_due(at(25, 6))["created"] == 1
        # 9/26 06:00：9/26 当期轮生成（窗口未到，正常）。
        assert planning.generate_due(at(26, 6))["created"] == 1
        assert c.rows[-1]["round_key"] == "cycle:2026-09-26"
        assert c.rows[-1]["window_end_at"] == planning._iso(at(26, 9))


# ── #17 场景 B/C：创建成功但冲突 / 单次时间过期拒绝 ────────────────

def test_scenario_b_recurring_and_once_created_with_conflict():
    # 10:00 创建每日或今天的单次，窗口 09:00–11:00、耗时 90 分钟：均允许
    # 创建；当前实例报告排程冲突；不拒绝创建、不截短耗时。
    with Context() as c:
        daily = c.create("daily", at(24, 10), estimated_minutes=90,
                         window_start_tod="09:00", window_end_tod="11:00")
        assert daily["schedule_conflict"] is True
        assert daily["first_round_skipped"] is False
        assert len(c.rows) == 1
        assert c.rows[0]["planned_minutes"] == 90  # 耗时不截短
        # 冲突是派生展示：读取时由同一排程纯函数派生。
        board = planning.today_board(at(24, 10))
        assert any(item["task_id"] == daily["id"] for item in board["progress"])
        assert board["conflicts"], "窗口装不下必须在看板派生排程冲突"
    with Context() as c:
        once = c.create("once", at(24, 10), target_date="2026-09-24",
                        estimated_minutes=90,
                        window_start_tod="09:00", window_end_tod="11:00")
        assert once["schedule_conflict"] is True
        assert len(c.rows) == 1
        board = planning.today_board(at(24, 10))
        assert board["conflicts"]


def test_scenario_c_once_deadline_reached_rejected_zero_writes():
    # 10:00 创建今天、最晚完成 09:00 或 10:00 的单次：明确拒绝（时间过期），
    # 任务与实例零写入。
    with Context() as c:
        for end_tod in ("09:00", "10:00"):
            with pytest.raises(planning.PlanningError) as error:
                c.create("once", at(24, 10), target_date="2026-09-24",
                         window_end_tod=end_tod)
            assert error.value.status_code == 400
            assert "已到或已过" in str(error.value)
        assert c.tasks == [] and c.rows == []
        # 跨午夜窗口按绝对结束时刻判断：今天 23:00–明天 02:00 在 10:00 未过期。
        c.create("once", at(24, 10), target_date="2026-09-24",
                 window_start_tod="23:00", window_end_tod="02:00")
        assert len(c.rows) == 1


def test_scenario_c_once_without_window_or_only_earliest_never_time_expired():
    # 无窗口 / 只有最早开始（即使开始已过）的单次不构成过期，照常创建。
    with Context() as c:
        c.create("once", at(24, 10), target_date="2026-09-24")
        c.create("once", at(24, 10), content="单次2", task_type="once",
                 target_date="2026-09-24", window_start_tod="03:00")
        assert len(c.rows) == 2
        assert all(row["window_end_at"] is None for row in c.rows)


# ── #18 场景 E/F/G：日期可选、无日期常驻、身份锁定 ─────────────────

def test_scenario_e_once_without_date_is_resident_single_instance():
    # 省略日期：创建成功、target_date 保持 NULL、立即进入当前待办；跨日、
    # 刷新、多次维护仍只有同一个业务实例；日期不被回填。
    with Context() as c:
        task = c.create("once", at(24, 10))
        assert task["target_date"] is None
        assert c.tasks[0]["target_date"] is None
        assert len(c.rows) == 1
        occ = c.rows[0]
        assert occ["round_key"] == "once"
        assert occ["display_cycle_date"] == "2026-09-24"
        assert occ["window_start_at"] is None and occ["window_end_at"] is None
        # 跨日：同一个开放实例顺延展示，不重建、不超时、日期仍为空。
        assert planning.generate_due(at(25, 6))["created"] == 0
        assert planning.generate_due(at(26, 6))["created"] == 0
        assert planning.generate_due(at(27, 6))["created"] == 0
        assert len(c.rows) == 1
        carried = c.rows[0]
        assert carried["id"] == occ["id"]
        assert carried["round_key"] == "once"
        assert carried["display_cycle_date"] == "2026-09-27"
        assert carried["display_reason"] == "carryover"
        assert c.tasks[0]["target_date"] is None
        # 当前待办可见（进度中）。
        board = planning.today_board(at(27, 7))
        assert any(item["id"] == occ["id"] for item in board["progress"])


def test_scenario_e_explicit_null_equals_omitted_and_closes_permanently():
    # 显式 NULL 与省略等价；完成后不再生成（不每日重建）。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date=None)
        assert task["target_date"] is None
        assert len(c.rows) == 1
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 11))
        for day in (25, 26, 27):
            planning.generate_due(at(day, 6))
        assert len(c.rows) == 1  # 关闭后不再生成
        assert c.rows[0]["status"] == "completed"
        assert c.rows[0]["handled_at"] is not None


def test_scenario_e_no_date_once_keeps_estimated_minutes_and_schedules():
    # 无日期单次保留预计耗时并参与正常自动排程（无窗口约束）。
    with Context() as c:
        task = c.create("once", at(24, 10), estimated_minutes=45)
        assert task["estimated_minutes"] == 45
        occ = c.rows[0]
        assert occ["planned_minutes"] == 45
        board = planning.today_board(at(24, 10))
        item = next(item for item in board["progress"] if item["id"] == occ["id"])
        assert item["estimated_minutes"] == 45
        # 重算把无窗口实例正常排上（不因无日期被排除）。
        result = planning.recompute_today(at(24, 10, 30))
        assert result["conflicts"] == []
        refreshed = next(row for row in c.rows if row["id"] == occ["id"])
        assert refreshed["est_start"] is not None


def test_scenario_f_no_date_with_any_window_end_rejected_zero_writes():
    # 空日期 + 任一窗口端：拒绝且零写入。
    with Context() as c:
        for payload in (
            {"window_start_tod": "09:00"},
            {"window_end_tod": "18:00"},
            {"window_start_tod": "09:00", "window_end_tod": "18:00"},
        ):
            with pytest.raises(planning.PlanningError) as error:
                c.create("once", at(24, 10), target_date=None, **payload)
            assert error.value.status_code == 400
            assert "不能设置可安排时段" in str(error.value)
        assert c.tasks == [] and c.rows == []
        # 填写昨天仍拒绝（日期下界保留）；今天与未来合法。
        with pytest.raises(planning.PlanningError) as error:
            c.create("once", at(24, 10), target_date="2026-09-23")
        assert "目标日期不能早于当前业务日期" in str(error.value)
        assert c.tasks == []
        c.create("once", at(24, 10), content="今天", task_type="once",
                 target_date="2026-09-24")
        c.create("once", at(24, 10), content="未来", task_type="once",
                 target_date="2026-09-30")
        assert len(c.tasks) == 2


def test_scenario_g_generated_no_date_once_null_save_ok_and_date_locked():
    # 已生成无日期单次：NULL 同值保存成功（不报日期必填）；实际改成有
    # 日期仍受身份锁定拒绝；窗口模板同样锁定。
    with Context() as c:
        task = c.create("once", at(24, 10))
        assert len(c.rows) == 1
        updated = planning.update_task(task["id"], {
            "target_date": None, "window_start_tod": None, "window_end_tod": None,
        }, at(24, 11))
        assert updated["target_date"] is None
        assert c.tasks[0]["target_date"] is None
        # 实际变化：NULL → 日期（已生成）→ 身份锁定拒绝。
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"target_date": "2026-09-30"}, at(24, 12))
        assert error.value.status_code == 400
        assert "已生成当前实例" in str(error.value)
        assert c.tasks[0]["target_date"] is None  # 零写入
        with pytest.raises(planning.PlanningError):
            planning.update_task(task["id"], {"window_end_tod": "18:00"}, at(24, 12))
        assert c.tasks[0]["window_end_tod"] is None


def test_no_date_once_ungenerated_edit_paths():
    # 未生成（此处创建即生成，故用另一无实例任务行验证编辑组合规则）：
    # 清除日期必须同时清空窗口；补日期按日期下界校验。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-30",
                        window_start_tod="09:00", window_end_tod="11:00")
        assert c.rows == []  # 内部周期未到，未生成
        # 只清日期、残留窗口 → 拒绝（零写入）。
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"target_date": None}, at(24, 11))
        assert "不能设置可安排时段" in str(error.value)
        assert c.tasks[0]["target_date"] == "2026-09-30"
        # 日期与窗口一起清空 → 允许，转成无日期常驻（尚未生成才能改）。
        updated = planning.update_task(task["id"], {
            "target_date": None, "window_start_tod": None, "window_end_tod": None,
        }, at(24, 11))
        assert updated["target_date"] is None
        assert updated["window_start_tod"] is None
        # 无日期 once 编辑保存后立即生成常驻实例（创建时确定内部周期），
        # 后续维护不再重建，target_date 保持空。
        assert len(c.rows) == 1
        assert c.rows[0]["round_key"] == "once"
        assert c.rows[0]["display_cycle_date"] == "2026-09-24"
        assert planning.generate_due(at(30, 6))["created"] == 0
        assert len(c.rows) == 1
        assert c.tasks[0]["target_date"] is None


def test_no_date_once_instance_window_edit_rejected():
    # 无日期常驻实例不能借当前实例编辑引入时间窗口 / 截止（§28.3）。
    with Context() as c:
        task = c.create("once", at(24, 10))
        occ = c.rows[0]
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(occ["id"], {
                "window_end_at": planning._iso(at(24, 18)),
            }, at(24, 11))
        assert error.value.status_code == 400
        assert "不能为它的当前实例新增时间窗口" in str(error.value)
        refreshed = next(row for row in c.rows if row["id"] == occ["id"])
        assert refreshed["window_end_at"] is None  # 零写入


# ── 审查修复 R1 / R2 / R3 / R6 回归（2026-10-01 20:04 审查轮） ──────

def test_r1_fixed_interval_before_boundary_cross_midnight_skips_first_round():
    # R1：boundary 06:00，9/24 04:00 创建固定间隔 3 天、窗口 23:00–02:00。
    # 当前规划周期是 9/23，本轮窗口 = 9/23 23:00–9/24 02:00，截止已过 →
    # 跳过首轮（此前错误地生成 9/23 轮并挂 9/24 晚窗口：截止判定误用
    # 事件自然日而非本轮规划周期）。
    with Context() as c:
        task = c.create("interval", at(24, 4), refresh_mode="fixed_interval",
                        interval_days=3,
                        window_start_tod="23:00", window_end_tod="02:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        # 结算游标 = 首轮事件日（创建自然日），生成侧按游标跳过。
        assert c.tasks[0]["refresh_generated_through"] == "2026-09-24"
        assert planning.generate_due(at(24, 12))["created"] == 0
        # 下一轴点 9/27 04:00 到期后按候选解析生成（固定轴与轮次键不变）。
        assert planning.generate_due(at(27, 4))["created"] == 1
        occ = c.rows[0]
        assert occ["round_key"].startswith("fixed:2026-09-27:")
        assert (occ["window_start_at"], occ["window_end_at"]) == (
            planning._iso(at(27, 23)), planning._iso(at(28, 2)))


def test_r1_fixed_interval_due_case_uses_current_cycle_window():
    # R1 对照（DUE）：9/24 04:00 创建固定间隔、窗口 03:00–05:00——当前
    # 周期 9/23 的窗口出现是 9/24 03:00–05:00（清晨侧下一次出现），截止
    # 未到 → 首轮 DUE，生成并冻结当前上午窗口（不挂下一晚）。
    with Context() as c:
        task = c.create("interval", at(24, 4), refresh_mode="fixed_interval",
                        interval_days=3,
                        window_start_tod="03:00", window_end_tod="05:00")
        assert task["first_round_skipped"] is False
        assert len(c.rows) == 1
        occ = c.rows[0]
        assert (occ["window_start_at"], occ["window_end_at"]) == (
            planning._iso(at(24, 3)), planning._iso(at(24, 5)))


def test_r1_daily_before_boundary_cross_midnight_still_skips():
    # R1 对照：同配置 daily 在 boundary 前创建本来就按当前周期（9/23）
    # 判定并跳过；统一归属后行为保持。
    with Context() as c:
        task = c.create("daily", at(24, 4),
                        window_start_tod="23:00", window_end_tod="02:00")
        assert task["first_round_skipped"] is True
        assert c.rows == []
        assert c.tasks[0]["refresh_generated_through"] == "2026-09-23"


@pytest.mark.parametrize("kind,extra", [
    ("daily", {}),
    ("weekly", {"weekdays": [3]}),
    ("monthly", {"month_days": [24]}),
    ("interval", {"refresh_mode": "fixed_interval", "interval_days": 3}),
])
def test_r3a_generation_failure_keeps_first_round_recoverable(kind, extra):
    # R3-A：08:30 创建 08:00–09:00（截止未到，首轮 DUE），即时轮次 INSERT
    # 暂时失败；10:00 恢复维护必须补生成首轮（候选解析），不得被当作主动
    # 跳过；固定类型游标只在成功后推进，首轮不得丢失。
    with Context() as c:
        original_rpc = c.db.rpc

        def failing_rpc(name, params=None):
            if name == "planning_insert_round_occurrence":
                raise RuntimeError("simulated transient insert failure")
            return original_rpc(name, params)

        with mock.patch.object(c.db, "rpc", side_effect=failing_rpc):
            task = c.create(kind, at(24, 8, 30), window_start_tod="08:00",
                            window_end_tod="09:00", **extra)
        assert task["first_round_skipped"] is False
        assert c.rows == []
        result = planning.generate_due(at(24, 10))
        assert result["created"] == 1, result
        assert len(c.rows) == 1
        # 恢复后再维护不重复生成。
        assert planning.generate_due(at(24, 11))["created"] == 0
        assert len(c.rows) == 1


def test_r3b_settled_skip_survives_template_edit_resume_and_maintenance():
    # R3-B：已结算跳过的首轮不因模板编辑、暂停恢复或重复维护复活
    # （此前 daily 无游标，模板终点改到未来会把被跳过的当天轮补回来）。
    for kind, extra in (
        ("daily", {}),
        ("weekly", {"weekdays": [3]}),
        ("monthly", {"month_days": [24]}),
        ("interval", {"refresh_mode": "fixed_interval", "interval_days": 3}),
    ):
        with Context() as c:
            task = c.create(kind, at(24, 10), window_start_tod="08:00",
                            window_end_tod="09:00", **extra)
            assert task["first_round_skipped"] is True
            assert c.rows == []
            # 模板最晚完成改到未来：被跳过的首轮不复活。
            planning.update_task(task["id"], {"window_end_tod": "18:00"}, at(24, 11))
            assert c.rows == []
            # 暂停 → 恢复：同样不复活。
            planning.update_task(task["id"], {"refresh_enabled": False}, at(24, 12))
            planning.update_task(task["id"], {"refresh_enabled": True}, at(24, 13))
            assert planning.generate_due(at(24, 14))["created"] == 0
            assert c.rows == []
            # 次日起按原重复规则正常生成（skip 只结算首轮）。
            if kind == "daily":
                assert planning.generate_due(at(25, 6))["created"] == 1


def test_r6_non_rule_day_creation_reports_no_skip():
    # R6：周四 10:00 创建只在周五出现的 weekly——当天本无合法轮次，
    # 不得返回「首轮已跳过」；下一个合法日正常生成。
    with Context() as c:
        task = c.create("weekly", at(24, 10), weekdays=[4],
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is False
        assert c.rows == []
        assert planning.generate_due(at(25, 6))["created"] == 1  # 9/25 周五
        assert c.rows[0]["round_key"] == "cycle:2026-09-25"
    # monthly 同理：24 日创建、只在 25 日出现 → 无跳过反馈。
    with Context() as c:
        task = c.create("monthly", at(24, 10), month_days=[25],
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is False
        assert c.rows == []
        assert planning.generate_due(at(25, 6))["created"] == 1


def test_r6_rule_day_before_boundary_without_current_event_reports_no_skip():
    # R6 补充：周五 04:00（当前周期是周四 9/24）创建只在周五出现的
    # weekly——当前周期键不是规则日，无「当前轮」，不报跳过；06:00 后
    # 当期轮正常生成。
    with Context() as c:
        task = c.create("weekly", at(25, 4), weekdays=[4],
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is False
        assert c.rows == []
        assert planning.generate_due(at(25, 6))["created"] == 1
        assert c.rows[0]["round_key"] == "cycle:2026-09-25"


def test_r2_zero_freedom_remaining_conflict_derived_on_board():
    # R2：零自由度窗口（09:00–11:00 = 120 分钟耗时）10:00 创建——剩余
    # 60 < 120：实例保留 rule 固定位置（est 不移动、不截短），看板必须
    # 派生剩余不足冲突（此前 conflicts 为空）。
    for kind, extra in (("daily", {}), ("once", {"target_date": "2026-09-24"})):
        with Context() as c:
            task = c.create(kind, at(24, 10), estimated_minutes=120,
                            window_start_tod="09:00", window_end_tod="11:00", **extra)
            assert task["schedule_conflict"] is True
            occ = c.rows[0]
            assert occ["is_fixed"] is True
            assert (occ["est_start"], occ["est_end"]) == (
                planning._iso(at(24, 9)), planning._iso(at(24, 11)))
            board = planning.today_board(at(24, 10))
            assert any(item["constraint"] == "window_end"
                       and item["occurrence_id"] == occ["id"]
                       and "剩余空间不足" in item["reason"]
                       for item in board["conflicts"])


def test_r2_hollow_zero_freedom_envelope_conflict_on_board():
    # R2（中空）：包络 30+60+30 恰等窗口 09:00–11:00，10:00 创建 → 开始
    # 阶段按完整包络派生一次冲突；结束阶段不重复上报；两阶段 est 不动。
    with Context() as c:
        task = c.create("daily", at(24, 10), is_hollow=True,
                        hollow_start_content="开始", hollow_start_minutes=30,
                        hollow_wait_minutes=60, hollow_end_content="结束",
                        hollow_end_minutes=30,
                        window_start_tod="09:00", window_end_tod="11:00")
        assert task["schedule_conflict"] is True
        assert len(c.rows) == 2
        start_row = next(r for r in c.rows if r["phase"] == "start")
        end_row = next(r for r in c.rows if r["phase"] == "end")
        assert (start_row["est_start"], start_row["est_end"]) == (
            planning._iso(at(24, 9)), planning._iso(at(24, 9, 30)))
        board = planning.today_board(at(24, 10))
        assert any(item["occurrence_id"] == start_row["id"]
                   and "包络" in item["reason"] for item in board["conflicts"])
        assert all(item["occurrence_id"] != end_row["id"] for item in board["conflicts"])


def test_r2_started_instances_keep_remaining_shortage_exemption():
    # §18.1 豁免：开始执行后不因剩余窗口缩小被重判冲突（超时另由 sweep）。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=120,
                        window_start_tod="09:00", window_end_tod="11:00")
        occ = c.rows[0]
        assert planning.today_board(at(24, 10))["conflicts"]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 5))
        assert planning.today_board(at(24, 10, 10))["conflicts"] == []


def test_r2_feasible_zero_freedom_conflicts_only_after_time_passes():
    # R2 边界：创建时刻剩余充足 → 无冲突；时间流逝使剩余不足 → 冲突派生。
    with Context() as c:
        c.create("daily", at(24, 8), estimated_minutes=120,
                 window_start_tod="09:00", window_end_tod="11:00")
        assert planning.today_board(at(24, 8, 30))["conflicts"] == []
        assert planning.today_board(at(24, 10))["conflicts"]


# ── 复审遗留 R7 / R9 / R10（#21 / #23 / #24；2026-10-02 修复轮） ────

def test_r7_future_explicit_anchor_same_day_not_settled_early():
    # R7（#21）：9/24 10:00 创建固定间隔 3 天、显式首次基准当天 16:00、
    # 窗口 08:00–09:00。首个轴点尚未到期——不得按创建日窗口提前结算游标
    # （此前响应 first_round_skipped=true、游标已写 9/24，16:00 首个事件
    # 被游标吞掉，第一次任务丢失）；到期后按候选解析正常生成首个实例。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="fixed_interval",
                        interval_days=3,
                        refresh_anchor_at="2026-09-24T16:00:00+08:00",
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is False
        assert task["schedule_conflict"] is False
        assert c.rows == []
        assert c.tasks[0].get("refresh_generated_through") is None
        # 锚点未到期间的维护：不生成、不结算、游标保持为空。
        assert planning.generate_due(at(24, 12))["created"] == 0
        assert c.tasks[0].get("refresh_generated_through") is None
        # 16:00 首个事件到期：首轮照常生成（当日窗口已过 → 候选解析为次日）。
        assert planning.generate_due(at(24, 16))["created"] == 1
        occ = c.rows[0]
        assert occ["round_key"].startswith("fixed:2026-09-24:")
        assert (occ["window_start_at"], occ["window_end_at"]) == (
            planning._iso(at(25, 8)), planning._iso(at(25, 9)))
        # 重复维护不重复生成。
        assert planning.generate_due(at(24, 17))["created"] == 0
        assert len(c.rows) == 1


def test_r7_future_explicit_anchor_next_days_generates_when_due():
    # R7 补充：显式首次基准在 9/27 16:00——创建时与随后两天都不结算、
    # 不生成；9/27 16:00 到期生成首个实例。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="fixed_interval",
                        interval_days=3,
                        refresh_anchor_at="2026-09-27T16:00:00+08:00",
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is False
        assert c.rows == []
        assert c.tasks[0].get("refresh_generated_through") is None
        assert planning.generate_due(at(25, 6))["created"] == 0
        assert planning.generate_due(at(26, 6))["created"] == 0
        assert c.tasks[0].get("refresh_generated_through") is None
        assert planning.generate_due(at(27, 16))["created"] == 1
        assert c.rows[0]["round_key"].startswith("fixed:2026-09-27:")
        assert (c.rows[0]["window_start_at"], c.rows[0]["window_end_at"]) == (
            planning._iso(at(28, 8)), planning._iso(at(28, 9)))


def test_r7_past_anchor_settles_only_first_event_day():
    # R7（结算日 = 首个轴点事件日，不把整段历史轴统一结算为创建日）：
    # 9/24 10:00 创建固定间隔 1 天、显式锚点 9/23 08:00、窗口 08:00–09:00
    # ——锚点事件（9/23）已到期且本轮窗口已过 → 首轮跳过（无实例）；9/24
    # 轴点属非首轮迟到补生成，创建后的即时生成照常产出（旧实现把游标统一
    # 结算为创建日 9/24，会把这一轮也吞掉）。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="fixed_interval",
                        interval_days=1,
                        refresh_anchor_at="2026-09-23T08:00:00+08:00",
                        window_start_tod="08:00", window_end_tod="09:00")
        assert task["first_round_skipped"] is True
        assert len(c.rows) == 1
        assert c.rows[0]["round_key"].startswith("fixed:2026-09-24:")
        assert not any(row["round_key"].startswith("fixed:2026-09-23:")
                       for row in c.rows)
        assert (c.rows[0]["window_start_at"], c.rows[0]["window_end_at"]) == (
            planning._iso(at(25, 8)), planning._iso(at(25, 9)))
        # 重复维护不重复生成。
        assert planning.generate_due(at(24, 11))["created"] == 0
        assert len(c.rows) == 1


def test_r9_after_completion_creation_reports_remaining_conflict():
    # R9（#23）：10:00 创建处理后刷新（间隔 3 天）、窗口 09:00–11:00、
    # 耗时 90 分钟——首轮照常生成（不跳过），创建响应必须与看板同源报告
    # 剩余不足冲突（此前响应 schedule_conflict=false，前端只显示普通成功）。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="after_completion",
                        interval_days=3, estimated_minutes=90,
                        window_start_tod="09:00", window_end_tod="11:00")
        assert task["first_round_skipped"] is False
        assert task["schedule_conflict"] is True
        assert len(c.rows) == 1  # 首轮照常生成，不因冲突反馈缺失
        assert c.rows[0]["planned_minutes"] == 90  # 耗时不截短
        board = planning.today_board(at(24, 10))
        assert any(item["occurrence_id"] == c.rows[0]["id"]
                   and "剩余空间不足" in item["reason"] for item in board["conflicts"])
    # 对照：剩余空间足够（60 分钟恰好放进剩余窗口）→ 无冲突反馈。
    with Context() as c:
        task = c.create("interval", at(24, 10), refresh_mode="after_completion",
                        interval_days=3, estimated_minutes=60,
                        window_start_tod="09:00", window_end_tod="11:00")
        assert task["schedule_conflict"] is False
        assert task["first_round_skipped"] is False
        assert len(c.rows) == 1


@pytest.mark.parametrize("kind,extra", [
    ("weekly", {"weekdays": [4]}),  # 只在周五（9/25）出现
    ("monthly", {"month_days": [25]}),
])
def test_r10_non_rule_day_creation_reports_no_conflict(kind, extra):
    # R10（#24）：周四 9/24 10:00 创建只在周五/25 日出现的任务，窗口
    # 09:00–11:00、耗时 90 分钟——当天没有合法轮次，不得按周四剩余时间
    # 编造「已创建但存在排程冲突」（此前响应冲突为 true、看板零冲突）。
    with Context() as c:
        task = c.create(kind, at(24, 10), estimated_minutes=90,
                        window_start_tod="09:00", window_end_tod="11:00", **extra)
        assert task["first_round_skipped"] is False
        assert task["schedule_conflict"] is False
        assert c.rows == []
        # 下个合法轮次照常生成，窗口与排程正常、无冲突。
        assert planning.generate_due(at(25, 6))["created"] == 1
        assert planning.today_board(at(25, 7))["conflicts"] == []
    # 对照：规则日当天剩余不足仍正确报告冲突（真正不可行不掩盖）。
    with Context() as c:
        task = c.create(kind, at(25, 10), estimated_minutes=90,
                        window_start_tod="09:00", window_end_tod="11:00", **extra)
        assert task["schedule_conflict"] is True
        assert task["first_round_skipped"] is False


def test_dated_once_regressions_preserved():
    # 已有有日期任务回归：日期下界、窗口自然日冻结、内部周期分离保持正确。
    with Context() as c:
        task = c.create("once", at(27, 10), target_date="2026-09-28",
                        window_start_tod="03:00", window_end_tod="05:00")
        assert c.tasks[0]["target_date"] == "2026-09-28"
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["display_cycle_date"]) == ("2026-09-27", "2026-09-27")
        assert occ["window_start_at"] == planning._iso(at(28, 3))
        # 有日期单次不填窗口：schedule_date = target_date（现行规则）。
        dated = c.create("once", at(27, 10), content="普通单次", task_type="once",
                         target_date="2026-09-30")
        assert dated["target_date"] == "2026-09-30"
        assert planning.generate_due(at(28, 7))["created"] == 0  # 9/30 未到
