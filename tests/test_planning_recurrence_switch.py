"""批次 6 一轮 Review 修复定向测试：recurrence 规则切换时刻语义（2026-09-28 user 裁决）。

正式裁决：rule_switch_at = 本次规则编辑实际生效时刻；旧规则负责全部
``due_at <= rule_switch_at`` 的轮次，新规则只负责 ``due_at > rule_switch_at``
的轮次。编辑顺序：Phase A 用修改前快照补齐旧轴截至切换时刻的漏轮 →
Phase B 保存新规则 → Phase C 生成游标 = 新规则首个合法事件（due 严格晚于
切换时刻）日期的前一天（现有按日游标 + 该计算即可精确表达切换点，无需
新增持久化字段）。模板窗口编辑不是 recurrence 编辑，不得触碰游标。
"""

from datetime import datetime

import pytest

from gateway import planning, planning_generation
from gateway.planning_domain import BUSINESS_TIMEZONE
from tests.support.planning_context import Context, at


def octo(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=BUSINESS_TIMEZONE)


def sep_iso(day, hour, minute=0):
    return planning._iso(at(day, hour, minute))


def oct_iso(day, hour, minute=0):
    return planning._iso(octo(day, hour, minute))


def rounds_by_due(c):
    return {row.get("fixed_due_at"): row for row in c.rows if row.get("fixed_due_at")}


def test_fixed_interval_shortened_old_missed_round_preserved_and_no_retro():
    # 必测：fixed_interval anchor 9/21 10:00，旧 = 每 3 天，9/28 11:00 改
    # 每 1 天。旧轴 9/24、9/27；9/27 若漏生成 → 编辑时必须按旧规则补出；
    # 新轴不得补出 9/28 10:00（早于编辑 11:00），新规则第一条是 9/29 10:00。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3)
        planning.generate_due(at(24, 11))  # 只生成到 9/24；9/27 漏
        assert set(rounds_by_due(c)) == {sep_iso(21, 10), sep_iso(24, 10)}

        planning.update_task(task["id"], {"interval_days": 1}, at(28, 11))
        assert c.db.rows["planning_task"][0]["interval_days"] == 1
        # Phase A：旧轴漏轮 9/27 已按旧规则补出（冻结边界 = 旧轴下一事件
        # 9/30 10:00，不随新轴 1 天缩短）
        by_due = rounds_by_due(c)
        assert by_due[sep_iso(27, 10)]["fixed_expires_at"] == sep_iso(30, 10)
        # 新轴没有 9/28 10:00 的追溯轮
        assert sep_iso(28, 10) not in by_due
        assert len(c.rows) == 3

        # 9/28 12:00 维护：9/28 10:00（早于切换 11:00）不生成
        planning.generate_due(at(28, 12))
        assert sep_iso(28, 10) not in rounds_by_due(c)
        # 新规则第一条 = 9/29 10:00 到点生成；冻结边界 = 9/30 10:00
        planning.generate_due(at(29, 10, 1))
        by_due = rounds_by_due(c)
        assert by_due[sep_iso(29, 10)]["fixed_expires_at"] == sep_iso(30, 10)
        # 旧轴各轮冻结事实不变
        assert by_due[sep_iso(27, 10)]["fixed_expires_at"] == sep_iso(30, 10)
        assert by_due[sep_iso(21, 10)]["fixed_expires_at"] == sep_iso(24, 10)


def test_fixed_interval_lengthened_does_not_swallow_old_missed_rounds():
    # 必测：1 天 → 5 天。新轴变稀不得吞掉旧轴已到期的漏轮：
    # 只生成到 9/26（9/27、9/28 10:00 漏），9/28 11:00 改 5 天 → 两轮都必须
    # 按「旧规则 = 每 1 天」补出；新轴切换后第一条是 10/1 10:00。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=1)
        planning.generate_due(at(26, 11))
        assert len(c.rows) == 6

        planning.update_task(task["id"], {"interval_days": 5}, at(28, 11))
        by_due = rounds_by_due(c)
        # due <= 切换时刻（9/28 11:00）的旧轴漏轮全部补出
        assert sep_iso(27, 10) in by_due and sep_iso(28, 10) in by_due
        assert len(c.rows) == 8
        # 新轴（9/21 + 5k）不追溯：9/29、9/30 无新轴事件
        planning.generate_due(at(29, 12))
        assert sep_iso(29, 10) not in rounds_by_due(c)
        planning.generate_due(octo(1, 10, 1))
        by_due = rounds_by_due(c)
        assert by_due[oct_iso(1, 10)]["fixed_expires_at"] == oct_iso(6, 10)


def test_fixed_weekday_edit_after_same_day_event_no_morning_retro():
    # 必测：fixed_weekday 在新 weekday 当天 event（周期起点 06:00）已过后
    # 编辑 → 不得追溯生成当天早晨的新规则轮。旧 = 周三（9/23 轮已生成）；
    # 9/28 11:00（周一）改每周一 → 9/28 06:00 事件早于切换不生成，
    # 新轴第一条是 10/5（周一）06:00。
    with Context() as c:
        task = c.create("weekly", at(23, 10), weekdays=[2], refresh_mode="fixed_weekday")
        planning.generate_due(at(24, 11))
        assert len(c.rows) == 1

        planning.update_task(task["id"], {"weekdays": [0]}, at(28, 11))
        assert c.db.rows["planning_task"][0]["weekdays"] == [0]
        # 无 9/28 早晨的追溯轮
        assert len(c.rows) == 1
        planning.generate_due(at(28, 12))
        assert len(c.rows) == 1
        planning.generate_due(octo(5, 6, 1))
        assert c.rows[-1]["schedule_date"] == "2026-10-05"
        # 旧轴轮冻结事实不变
        assert c.rows[0]["schedule_date"] == "2026-09-23"


def test_fixed_weekday_missed_old_round_reconciled_before_switch():
    # 旧 weekday 漏轮在切换前按旧规则保全：旧 = 周四（weekdays=[3]，
    # Python weekday 周一=0）。9/24（周四）创建、首个周四轮 = 9/24 本身；
    # 游标回拨 + 删轮构造「旧轴事件已到期但漏生成」，9/28 11:00（周一）
    # 改每周一 → Phase A 按旧规则补出 9/24 轮；新轴 9/28 06:00 事件早于
    # 切换不生成，第一条是 10/5（周一）。
    with Context() as c:
        task = c.create("weekly", at(24, 10), weekdays=[3], refresh_mode="fixed_weekday")
        c.db.rows["planning_task"][0]["created_at"] = planning._iso(at(22, 10))
        c.db.rows["planning_task"][0]["refresh_generated_through"] = None
        for row in list(c.rows):
            c.db.rows["planning_occurrence"].remove(row)
        planning.update_task(task["id"], {"weekdays": [0]}, at(28, 11))
        # Phase A：旧规则（周四）轴上 due <= 9/28 11:00 的事件 = 9/24 06:00
        assert any(row["schedule_date"] == "2026-09-24" for row in c.rows)
        # 新轴（周一）不生成 9/28 早晨的追溯轮
        planning.generate_due(at(28, 12))
        assert not any(row["schedule_date"] == "2026-09-28" for row in c.rows)
        planning.generate_due(octo(5, 6, 1))
        assert c.rows[-1]["schedule_date"] == "2026-10-05"


def test_fixed_monthday_switch_same_semantics():
    # 必测：fixed_monthday 同类切换。旧 = 每月 24 日（9/24 轮漏）；
    # 9/28 11:00 改每月 30 日 → 9/24 漏轮补出；新轴第一条 9/30 06:00
    # （晚于切换），9/29 维护不产生追溯轮。
    with Context() as c:
        task = c.create("monthly", at(20, 10), month_days=[24], refresh_mode="fixed_monthday")
        # 模拟 9/24 轮漏生成：游标回拨 + 删除已有轮
        c.db.rows["planning_task"][0]["refresh_generated_through"] = "2026-09-20"
        for row in list(c.rows):
            c.db.rows["planning_occurrence"].remove(row)
        planning.update_task(task["id"], {"month_days": [30]}, at(28, 11))
        assert len(c.rows) == 1
        assert c.rows[0]["schedule_date"] == "2026-09-24"
        planning.generate_due(at(29, 12))
        assert len(c.rows) == 1
        planning.generate_due(octo(30, 6, 1))
        assert any(row["schedule_date"] == "2026-09-30" for row in c.rows)


def test_window_only_edit_does_not_touch_recurrence_cursor():
    # 裁决 2：窗口模板不是 recurrence 字段——仅 PATCH window_start_tod /
    # window_end_tod 时生成游标逐字节不变、不凭新窗口补生历史 occurrence、
    # 已生成 occurrence 不变，下一真正未来轮使用新窗口 snapshot。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3, estimated_minutes=30,
                        window_start_tod="18:00", window_end_tod="22:00")
        planning.generate_due(at(24, 11))  # 3 天轴：9/21、9/24（9/27 漏）
        assert len(c.rows) == 2
        cursor_before = c.db.rows["planning_task"][0]["refresh_generated_through"]
        frozen = [(row["window_start_at"], row["window_end_at"]) for row in c.rows]

        planning.update_task(
            task["id"], {"window_start_tod": "20:00", "window_end_tod": "23:00"}, at(28, 11))
        stored = c.db.rows["planning_task"][0]
        assert (stored["window_start_tod"], stored["window_end_tod"]) == ("20:00", "23:00")
        # 游标不因窗口编辑倒退（无重置语义）；编辑后的幂等补生成把旧规则
        # 漏轮 9/27 照常补出——若游标被重置到「昨天」（9/27），该漏轮会被
        # 永久跳掉，此处断言即失败
        assert stored["refresh_generated_through"] >= cursor_before
        assert sep_iso(27, 10) in rounds_by_due(c)
        planning.generate_due(at(29, 10, 1))
        assert sep_iso(27, 10) in rounds_by_due(c)
        # 已生成 occurrence 冻结不变（新增行 = 补生成的漏轮，合法追加）
        assert [(row["window_start_at"], row["window_end_at"]) for row in c.rows[:len(frozen)]] == frozen
        # 下一个真正未来轮（9/30 10:00 到点）使用新窗口 snapshot（fixed
        # interval 轮锚定其出生周期，窗口 tod 取当前模板 20:00/23:00）
        planning.generate_due(octo(30, 10, 1))
        fresh = rounds_by_due(c)[sep_iso(30, 10)]
        assert fresh["window_start_at"].endswith("T20:00:00+08:00")
        assert fresh["window_end_at"].endswith("T23:00:00+08:00")


def test_rule_edit_while_paused_freezes_phase_a_and_sets_switch_cursor():
    # 暂停语义（需求 24）优先：暂停中编辑规则 → Phase A 不补生成（生成被
    # 暂停冻结），Phase C 仍写入切换下界（恢复后从新规则合法事件开始）。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3)
        planning.generate_due(at(24, 11))  # 9/21 轮到期死亡；9/24 轮开放
        assert len(c.rows) == 2
        planning.update_task(task["id"], {"refresh_enabled": False}, at(27, 9))
        planning.update_task(task["id"], {"interval_days": 1}, at(28, 11))
        assert c.db.rows["planning_task"][0]["interval_days"] == 1
        # 暂停冻结：旧轴 9/27 漏轮不补、无新轴追溯
        assert len(c.rows) == 2
        # 恢复刷新：从切换下界开始——9/27（旧轴）与 9/28 10:00（早于切换
        # 11:00 的新轴事件）都不出现；9/29 10:00 正常生成
        planning.update_task(task["id"], {"refresh_enabled": True}, at(29, 9))
        planning.generate_due(at(29, 9, 30))
        due_keys = set(rounds_by_due(c))
        assert sep_iso(27, 10) not in due_keys
        assert sep_iso(28, 10) not in due_keys
        assert sep_iso(29, 10) not in due_keys
        planning.generate_due(at(29, 10, 1))
        assert sep_iso(29, 10) in rounds_by_due(c)


def test_switch_cursor_precision_same_day_boundaries():
    # 精确切换点：切换日内、事件时刻早于/晚于切换时刻的两侧都必须精确。
    # 侧 A：编辑在新轴同日事件之后 → 该同日事件不重复、不追溯。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=7)
        planning.generate_due(at(28, 11))  # 9/21 轮 + 9/28 10:00 轮（10:00 <= 11:00）
        assert len(c.rows) == 2
        planning.update_task(task["id"], {"interval_days": 1}, at(28, 11, 30))
        planning.generate_due(at(28, 12))
        # 9/28 10:00 轮 = 旧轴已生成的合法事实（保留），无重复追溯
        assert len(rounds_by_due(c)) == 2
        planning.generate_due(at(29, 10, 1))
        assert sep_iso(29, 10) in rounds_by_due(c)
    # 侧 B：编辑发生在当日事件之后不久，新轴下一事件在次日——切换时刻前
    # 的同日事件不被新轴重复生成。
    with Context() as c:
        task = c.create("interval", at(28, 5), refresh_mode="fixed_interval",
                        interval_days=7)
        planning.generate_due(at(28, 5, 15))  # 9/28 05:00 轮（创建即到期）
        assert len(c.rows) == 1
        planning.update_task(task["id"], {"interval_days": 1}, at(28, 5, 30))
        planning.generate_due(at(29, 5, 1))
        assert sep_iso(29, 5) in rounds_by_due(c)
        assert len(rounds_by_due(c)) == 2  # 无 9/28 重复


def test_first_event_uncomputable_rejects_edit_without_save():
    # 规则数据异常导致无法完成切换流程 → 拒绝编辑，规则不被保存。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3)
        planning.generate_due(at(24, 11))
        c.db.rows["planning_task"][0]["refresh_anchor_at"] = None
        with pytest.raises(planning.PlanningError):
            planning.update_task(task["id"], {"interval_days": 1}, at(28, 11))
        # 规则未被保存（校验层拒绝；定位失败的 409 同样拒绝保存）
        assert c.db.rows["planning_task"][0]["interval_days"] == 3


# ── 二轮 BLOCKER 2：window-only 编辑先结清旧模板漏轮（九节 A–D） ────

def test_window_only_switch_missed_round_freezes_old_window():
    # A：旧 3 天轴 + 旧窗口 09:00–12:00；9/27 轮漏生成；9/28 11:00 把窗口
    # 模板改为 14:00–18:00 → Phase A 用完整旧快照补出的 9/27 轮必须冻结
    # 旧窗口（09:00–12:00），不得使用新模板。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3, estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        planning.generate_due(at(24, 11))  # 9/21、9/24；9/27 漏
        assert len(c.rows) == 2
        old_window = [(row["window_start_at"], row["window_end_at"]) for row in c.rows]
        assert old_window[0] == (sep_iso(21, 9), sep_iso(21, 12))

        planning.update_task(
            task["id"],
            {"window_start_tod": "14:00", "window_end_tod": "18:00"}, at(28, 11))
        # Phase A：9/27 漏轮已补出，冻结旧窗口模板的 tod（09:00/12:00；
        # fixed_interval 轮按批次 3 既有语义锚定其出生周期 9/28）
        by_due = rounds_by_due(c)
        assert sep_iso(27, 10) in by_due
        missed = by_due[sep_iso(27, 10)]
        assert missed["window_start_at"].endswith("T09:00:00+08:00")
        assert missed["window_end_at"].endswith("T12:00:00+08:00")
        # B：switch 后真正未来轮（9/30 10:00 到点）冻结新窗口
        planning.generate_due(octo(30, 10, 1))
        fresh = rounds_by_due(c)[sep_iso(30, 10)]
        assert fresh["window_start_at"].endswith("T14:00:00+08:00")
        assert fresh["window_end_at"].endswith("T18:00:00+08:00")
        # 已生成轮冻结不变（旧 tod 保持，不被新模板重写）
        assert missed["window_start_at"].endswith("T09:00:00+08:00")
        assert missed["window_end_at"].endswith("T12:00:00+08:00")


def test_window_only_switch_does_not_touch_cursor():
    # D：window-only 编辑不人工 reset recurrence cursor（游标只能被真实
    # 生成推进），漏轮补出后不丢任何后续事件。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3, estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        planning.generate_due(at(24, 11))
        cursor_before = c.db.rows["planning_task"][0]["refresh_generated_through"]
        planning.update_task(
            task["id"], {"window_start_tod": "14:00", "window_end_tod": "18:00"}, at(28, 11))
        stored = c.db.rows["planning_task"][0]
        # 游标不倒退、不被人工重置（Phase A 补生成推进是合法前进）
        assert stored["refresh_generated_through"] >= cursor_before
        assert sep_iso(27, 10) in rounds_by_due(c)
        planning.generate_due(at(29, 12))
        # 新轴事件不被窗口编辑吞掉：9/30 10:00 之后正常生成
        planning.generate_due(octo(30, 10, 1))
        assert sep_iso(30, 10) in rounds_by_due(c)


def test_recurrence_and_window_combined_switch_single_closeout():
    # C / 十三：同请求 recurrence + window → 只做一次旧快照 closeout；
    # 旧漏轮冻结旧 recurrence + 旧窗口；未来轮 = 新 recurrence + 新窗口；
    # 新规则与新游标同一次 task UPDATE 原子保存。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3, estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        planning.generate_due(at(24, 11))  # 9/27 漏
        assert len(c.rows) == 2
        closeouts = []
        import test_planning_phase1a as p1a
        original_reconcile = planning_generation._reconcile_task_rounds

        def counting_reconcile(client, task_arg, cycle, now, *args, **kwargs):
            closeouts.append(dict(task_arg))
            return original_reconcile(client, task_arg, cycle, now, *args, **kwargs)

        planning_generation._reconcile_task_rounds = counting_reconcile
        try:
            planning.update_task(
                task["id"],
                {"interval_days": 5,
                 "window_start_tod": "14:00", "window_end_tod": "18:00"}, at(28, 11))
        finally:
            planning_generation._reconcile_task_rounds = original_reconcile
        stored = c.db.rows["planning_task"][0]
        assert stored["interval_days"] == 5
        assert (stored["window_start_tod"], stored["window_end_tod"]) == ("14:00", "18:00")
        # 新规则下界随同一次保存生效（首个新事件 10/1 10:00 → 游标 9/30）
        assert stored["refresh_generated_through"] == "2026-09-30"
        # 恰一次旧快照 closeout（旧 interval + 旧窗口）；保存后的 reconcile
        #（新值，_generate_due_quietly 的幂等补生成）不算 closeout
        old_closeouts = [c for c in closeouts
                         if c["interval_days"] == 3 and c.get("window_start_tod") == "09:00"]
        assert len(old_closeouts) == 1, closeouts
        # 旧漏轮：旧 recurrence（冻结边界 9/30 10:00 = 旧 3 天轴）+ 旧窗口 tod
        missed = rounds_by_due(c)[sep_iso(27, 10)]
        assert missed["fixed_expires_at"] == sep_iso(30, 10)
        assert missed["window_start_at"].endswith("T09:00:00+08:00")
        assert missed["window_end_at"].endswith("T12:00:00+08:00")
        # 未来轮 = 新 recurrence（5 天）+ 新窗口；新游标随同一次保存生效
        planning.generate_due(octo(1, 10, 1))
        fresh = rounds_by_due(c)[oct_iso(1, 10)]
        assert fresh["fixed_expires_at"] == oct_iso(6, 10)
        assert fresh["window_start_at"].endswith("T14:00:00+08:00")


def test_window_only_switch_paused_freezes_closeout():
    # 已暂停任务的 window-only 编辑：Phase A 生成被暂停冻结（漏轮不补），
    # 新模板照常保存；恢复后漏轮按当前（新）模板生成——暂停语义优先。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3, estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        planning.generate_due(at(24, 11))
        planning.update_task(task["id"], {"refresh_enabled": False}, at(27, 9))
        count = len(c.rows)
        planning.update_task(
            task["id"], {"window_start_tod": "14:00", "window_end_tod": "18:00"}, at(28, 11))
        assert c.db.rows["planning_task"][0]["window_start_tod"] == "14:00"
        assert len(c.rows) == count  # 暂停冻结：漏轮不补
        planning.update_task(task["id"], {"refresh_enabled": True}, at(29, 9))
        planning.generate_due(at(29, 9, 30))
        # 恢复后漏轮按当前模板（新窗口 tod）补出
        missed = rounds_by_due(c)[sep_iso(27, 10)]
        assert missed["window_start_at"].endswith("T14:00:00+08:00")


# ── 二轮裁决 E：recurrence 修改 + 暂停 / 停用 同请求 ────────────────

def test_rule_change_and_pause_same_request_closeout_first():
    # 裁决 E：同 PATCH 修改 recurrence 并设置 refresh_enabled=false →
    # 先以旧规则结清 due <= switch_at 的旧轴漏轮，再保存新规则 + 暂停；
    # pause 自 switch_at 后生效，不追溯抹掉编辑前已到期的旧规则事件。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3)
        planning.generate_due(at(24, 11))  # 9/27 漏
        assert len(c.rows) == 2
        planning.update_task(
            task["id"], {"interval_days": 1, "refresh_enabled": False}, at(28, 11))
        stored = c.db.rows["planning_task"][0]
        assert stored["interval_days"] == 1 and stored["refresh_enabled"] is False
        # Phase A：编辑前已到期的 9/27 旧漏轮已补出（pause 未追溯生效）
        assert sep_iso(27, 10) in rounds_by_due(c)
        assert len(c.rows) == 3
        # 暂停期间不生成 switch 后的新规则轮（9/28 10:00 < 11:00 亦不追溯）
        planning.generate_due(at(29, 9))
        assert sep_iso(28, 10) not in rounds_by_due(c)
        assert len(c.rows) == 3
        # resume 后沿新规则继续：9/29 10:00 正常生成
        planning.update_task(task["id"], {"refresh_enabled": True}, at(29, 9, 30))
        planning.generate_due(at(29, 10, 1))
        assert sep_iso(29, 10) in rounds_by_due(c)


def test_rule_change_and_deactivate_keeps_deactivation_semantics():
    # 最终 Debug（问题 1A 方案 A）：rule 变更 + is_active=false 同请求 →
    # 明确拒绝（停用必须单独提交，避免 RPC 提交后其它字段保存失败的半成功）。
    # 分两次提交：先改规则，再单独停用——停用语义照旧（关闭开放实例、
    # 终止刷新、不补旧轴漏轮）。
    with Context() as c:
        task = c.create("interval", at(21, 10), refresh_mode="fixed_interval",
                        interval_days=3)
        planning.generate_due(at(24, 11))
        assert len(c.rows) == 2
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(
                task["id"], {"interval_days": 1, "is_active": False}, at(28, 11))
        assert error.value.status_code == 400
        assert "单独执行停用" in str(error.value)
        # 拒绝后零写入
        stored = c.db.rows["planning_task"][0]
        assert stored["interval_days"] == 3 and stored["is_active"] is True
        # 第二步：单独停用
        planning.update_task(task["id"], {"is_active": False}, at(28, 11, 30))
        stored = c.db.rows["planning_task"][0]
        assert stored["interval_days"] == 3 and stored["is_active"] is False
        # 开放实例被停用语义关闭；不补旧轴漏轮（9/27 不出现）
        assert all(row["status"] not in planning.OPEN_STATUSES for row in c.rows)
        assert sep_iso(27, 10) not in rounds_by_due(c)
        planning.generate_due(at(29, 10, 1))
        assert len(c.rows) == 2  # 停用后不再生成
