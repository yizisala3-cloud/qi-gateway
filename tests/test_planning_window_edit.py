"""批次 6 定向测试：current/future 编辑语义（一期规范 §18.3 / §28 / §28.1 / §10 / §12.1 / §13.2 / §30.6）。

核心不变量：
* 已生成 occurrence 是历史冻结事实——task template（任务模板）的后续编辑
  只影响未来尚未生成的 occurrence，绝不追溯改写（§28.1、不变量 36）；
* 当前实例窗口编辑走 occurrence 级专用入口（§18.3 / §12.1 / §13.2），
  与任务模板编辑无任何同步；
* 任务模板窗口保存校验与创建校验共用批次 1 领域函数（§30.6）；
* recurrence rule 编辑不改写已生成轮次的 fixed 生命周期冻结事实
  （fixed_due_at / fixed_expires_at / 轮次身份）；
* 超时判定只认实例自身冻结的 window_end_at，不回看任务当前模板。
"""

import pytest

from gateway import planning, planning_recompute
from gateway.planning_domain import BUSINESS_TIMEZONE
from tests.support.planning_context import Context, at


from tests.support.planning_fixtures import HOLLOW, datetime_tz, iso, iso_dt, _fields


# ── A. 任务模板窗口编辑：只影响未来轮次（§18.3 / §28.1） ──────────

def test_patch_template_window_future_rounds_adopt_new_template():
    # 指令示例：已生成 A（09:00～12:00）→ 模板改 14:00～18:00 →
    # A 保持旧窗口，后续 B 使用新窗口。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        first = c.rows[0]
        assert (first["window_start_at"], first["window_end_at"]) == (iso(24, 9), iso(24, 12))
        before = _fields(first)
        occ_count = len(c.rows)

        planning.update_task(task["id"], {"window_start_tod": "14:00", "window_end_tod": "18:00"},
                             at(24, 11))
        # 模板更新
        stored = c.db.rows["planning_task"][0]
        assert (stored["window_start_tod"], stored["window_end_tod"]) == ("14:00", "18:00")
        # 已生成 A 的冻结事实逐字节不变（无追溯、无重建、无重新解析）
        assert len(c.rows) == occ_count
        assert _fields(first) == before
        # 未来轮次 B 按新模板冻结
        planning.generate_due(at(25, 7))
        second = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        assert (second["window_start_at"], second["window_end_at"]) == (iso(25, 14), iso(25, 18))
        # A 随每日周期收场（清单 #32 口裁决，属生命周期规则而非模板改写），
        # 冻结窗口事实仍逐字节原样（status 是唯一合法变化）
        assert first["status"] == "timeout"
        # R2 双死亡边界：冻结窗口早于周期终点，收场取窗口终点
        assert first["closed_at"] == iso(24, 12)
        assert (first["window_start_at"], first["window_end_at"]) == (iso(24, 9), iso(24, 12))
        assert ({k: v for k, v in _fields(first).items() if k != "status"}
                == {k: v for k, v in before.items() if k != "status"})


def test_patch_template_window_to_single_sided_and_clear():
    # 双端 → 只有最早开始 → 只有最晚完成 → 双空：历史轮不变，未来轮按新形状。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        first = c.rows[0]
        assert (first["window_start_at"], first["window_end_at"]) == (iso(24, 9), iso(24, 12))
        planning.generate_due(at(25, 7))
        old_round = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        assert (old_round["window_start_at"], old_round["window_end_at"]) == (iso(25, 9), iso(25, 12))

        # 双端 → 只有最早开始
        planning.update_task(task["id"], {"window_end_tod": None}, at(25, 8))
        stored = c.db.rows["planning_task"][0]
        assert (stored["window_start_tod"], stored["window_end_tod"]) == ("09:00", None)
        assert (first["window_start_at"], first["window_end_at"]) == (iso(24, 9), iso(24, 12))
        planning.generate_due(at(26, 7))
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-26")
        assert (fresh["window_start_at"], fresh["window_end_at"]) == (iso(26, 9), None)

        # 只有最早开始 → 只有最晚完成
        planning.update_task(
            task["id"], {"window_start_tod": None, "window_end_tod": "22:00"}, at(26, 8))
        stored = c.db.rows["planning_task"][0]
        assert (stored["window_start_tod"], stored["window_end_tod"]) == (None, "22:00")
        planning.generate_due(at(27, 7))
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-27")
        assert (fresh["window_start_at"], fresh["window_end_at"]) == (None, iso(27, 22))

        # → 双空（清除模板窗口）
        planning.update_task(
            task["id"], {"window_start_tod": None, "window_end_tod": None}, at(27, 8))
        stored = c.db.rows["planning_task"][0]
        assert (stored["window_start_tod"], stored["window_end_tod"]) == (None, None)
        planning.generate_due(at(28, 7))
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-28")
        assert fresh["window_start_at"] is None and fresh["window_end_at"] is None
        # 全部历史轮的冻结事实保持最初生成值
        assert (first["window_start_at"], first["window_end_at"]) == (iso(24, 9), iso(24, 12))
        assert (old_round["window_start_at"], old_round["window_end_at"]) == (iso(25, 9), iso(25, 12))


def test_patch_template_window_accepts_legal_shapes():
    # 批次 1 校验矩阵在编辑路径同样成立：跨午夜、端点接触、单侧。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        planning.update_task(
            task["id"], {"window_start_tod": "23:00", "window_end_tod": "02:00"}, at(24, 11))
        planning.update_task(task["id"], {"window_start_tod": "06:00"}, at(24, 11, 30))
        planning.update_task(task["id"], {"window_start_tod": None, "window_end_tod": "06:00"},
                             at(24, 12))
        stored = c.db.rows["planning_task"][0]
        assert (stored["window_start_tod"], stored["window_end_tod"]) == (None, "06:00")


def test_patch_rejects_invalid_window_without_partial_save():
    # 非法窗口拒绝（start==end / 跨 boundary / 非法格式），且同请求的其它
    # 字段不部分保存（校验先于任何写入）。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        before = dict(c.db.rows["planning_task"][0])
        for payload in (
            {"content": "改名", "window_start_tod": "10:00", "window_end_tod": "10:00"},
            {"content": "改名", "window_start_tod": "05:00", "window_end_tod": "07:00"},
            {"content": "改名", "window_start_tod": "25:00"},
            {"content": "改名", "window_end_tod": "abc"},
        ):
            with pytest.raises(planning.PlanningError) as error:
                planning.update_task(task["id"], payload, at(24, 11))
            assert error.value.status_code == 400
            stored = c.db.rows["planning_task"][0]
            assert stored["content"] == before["content"], payload
            assert (stored["window_start_tod"], stored["window_end_tod"]) == ("09:00", "12:00")
        # start == end 与跨 boundary 的中文原因
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(
                task["id"], {"window_start_tod": "10:00", "window_end_tod": "10:00"}, at(24, 11))
        assert "不能相同" in str(error.value)
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(
                task["id"], {"window_start_tod": "05:00", "window_end_tod": "07:00"}, at(24, 11))
        assert "不能跨越每日刷新时间 06:00" in str(error.value)


def test_patch_window_requires_valid_occupancy():
    # 无有效占用跨度的任务定义不得配置窗口模板（与创建入口同一约束）。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        c.db.rows["planning_task"][0]["estimated_minutes"] = None
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(
                task["id"], {"window_start_tod": "18:00", "window_end_tod": "20:00"}, at(24, 11))
        assert "必须提供有效预计耗时" in str(error.value)


# ── D/E. recurrence rule 编辑：fixed 冻结事实不追溯（§28.1 / 不变量 31） ──

def test_interval_edit_keeps_frozen_fixed_facts_and_future_uses_new_rule():
    # 固定间隔 3 天 → 5 天：旧轮的 fixed_due_at / fixed_expires_at / 窗口
    # 冻结事实不变（不得按新轴缩短或延长旧轮寿命），未来轮按新规则生成。
    with Context() as c:
        task = c.create("interval", at(18, 10), refresh_mode="fixed_interval", interval_days=3,
                        estimated_minutes=30, window_start_tod="18:00", window_end_tod="22:00")
        planning.generate_due(at(24, 11))
        current = next(row for row in c.rows if row["round_key"].startswith("fixed:2026-09-24"))
        assert current["status"] == "pending"
        assert current["fixed_expires_at"] == iso(27, 10)  # 旧规则冻结的下一规则点
        old_window = (current["window_start_at"], current["window_end_at"])
        before = _fields(current)

        planning.update_task(task["id"], {"interval_days": 5}, at(24, 11, 30))
        # 旧轮冻结事实逐字节不变
        assert _fields(current) == before
        assert (current["window_start_at"], current["window_end_at"]) == old_window

        # 未来轮按新规则生成：下一个 9/28 轮，冻结自己的边界（9/28 + 5 天）。
        planning.generate_due(at(28, 11))
        fresh = next(row for row in c.rows if row["round_key"].startswith("fixed:2026-09-28"))
        assert fresh["fixed_expires_at"] == iso_dt(2026, 10, 3, 10)
        assert (fresh["window_start_at"], fresh["window_end_at"]) == (iso(28, 18), iso(28, 22))
        # 旧 9/24 轮按**自己冻结的旧事实**活完生命周期：9/28 维护时其冻结边界
        # （旧规则 9/27 10:00）与冻结窗口终点（9/24 22:00）均已越过——双死亡
        # 边界取更早的窗口终点关闭；若被新轴重新解释，边界会漂移到 9/28 10:00。
        assert current["status"] == "timeout"
        assert current["closed_at"] == iso(24, 22)
        for key in ("window_start_at", "window_end_at", "fixed_due_at", "fixed_expires_at",
                    "round_key", "schedule_date"):
            assert current[key] == before[key], key


def test_weekday_edit_keeps_frozen_facts_and_future_uses_new_rule():
    with Context() as c:
        task = c.create("weekly", at(24, 10), weekdays=[3], estimated_minutes=30,
                        window_start_tod="18:00", window_end_tod="22:00")
        first = c.rows[0]
        assert first["round_key"] == "cycle:2026-09-24"
        assert first["fixed_expires_at"] == iso_dt(2026, 10, 1, 6)  # 下一个周四（旧规则冻结）
        before = _fields(first)

        planning.update_task(task["id"], {"weekdays": [0]}, at(24, 11))
        assert _fields(first) == before
        planning.generate_due(at(28, 7))  # 9/28 周一
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-28")
        assert (fresh["window_start_at"], fresh["window_end_at"]) == (iso(28, 18), iso(28, 22))
        assert fresh["fixed_expires_at"] == iso_dt(2026, 10, 5, 6)
        assert _fields(first) == before


def test_monthday_edit_keeps_frozen_facts_and_future_uses_new_rule():
    with Context() as c:
        task = c.create("monthly", at(24, 10), month_days=[24], estimated_minutes=30,
                        window_start_tod="18:00", window_end_tod="22:00")
        first = c.rows[0]
        assert first["fixed_expires_at"] == iso_dt(2026, 10, 24, 6)  # 下一个 24 日（旧规则冻结）
        before = _fields(first)

        planning.update_task(task["id"], {"month_days": [30]}, at(24, 11))
        assert _fields(first) == before
        planning.generate_due(at(30, 7))
        fresh = next(row for row in c.rows if row["schedule_date"] == "2026-09-30")
        assert (fresh["window_start_at"], fresh["window_end_at"]) == (iso(30, 18), iso(30, 22))
        assert _fields(first) == before


# ── F. 超时只认旧冻结 window_end_at，不回看任务当前模板 ───────────

def test_timeout_after_template_edit_uses_frozen_window_both_directions():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        occ = c.rows[0]
        assert occ["window_end_at"] == iso(24, 12)

        # 方向一：模板改早（09:00–10:00）——旧轮冻结终点 12:00 未越过，
        # 不因「当前模板已说 10:00」而超时。
        planning.update_task(
            task["id"], {"window_start_tod": "09:00", "window_end_tod": "10:00"}, at(24, 10, 30))
        result = planning.sweep_timeouts(at(24, 10, 31))
        assert result["timed_out"] == 0
        assert occ["status"] == "pending"

        # 方向二：模板改晚（14:00–18:00）——真实时间 12:30 越过旧冻结终点
        # 12:00 即超时，closed_at = 12:00（不由当前模板 18:00 决定）。
        planning.update_task(
            task["id"], {"window_start_tod": "14:00", "window_end_tod": "18:00"}, at(24, 10, 32))
        result = planning.sweep_timeouts(at(24, 12, 30))
        assert result["timed_out"] == 1
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == iso(24, 12)


# ── G. 排程：新轮用新窗口约束，旧轮不被模板编辑触碰 ───────────────

def test_schedule_uses_new_template_window_for_new_rounds():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        first = c.rows[0]
        planning.update_task(
            task["id"], {"window_start_tod": "14:00", "window_end_tod": "18:00"}, at(24, 10, 35))
        # 旧轮先按其冻结窗口走完生命周期（越过 12:00 超时），避免顺延干扰
        planning.sweep_timeouts(at(24, 12, 30))
        planning.generate_due(at(25, 7))
        second = next(row for row in c.rows if row["schedule_date"] == "2026-09-25")
        planning.recompute_today(at(25, 7, 30))
        # 新轮 est 落在新窗口内（起点 = 窗口起点 14:00）
        assert second["est_start"] == iso(25, 14)
        assert second["est_end"] == iso(25, 14, 30)
        # 旧轮窗口 / est / 身份保持自身冻结事实
        assert (first["window_start_at"], first["window_end_at"]) == (iso(24, 9), iso(24, 12))
        assert first["est_start"] == iso(24, 10)
        assert first["round_key"] == "cycle:2026-09-24"


# ── B. 当前实例窗口编辑（patch_occurrence，§18.3 / §12.1 / §13.2） ──

def test_occurrence_window_pan_updates_window_and_reschedules():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        assert occ["est_start"] == iso(24, 10)  # 创建后同步重算的自动 est
        planning.patch_occurrence(
            occ["id"], {"window_start_at": iso(24, 18), "window_end_at": iso(24, 22)}, at(24, 11))
        assert occ["window_start_at"] == iso(24, 18)
        assert occ["window_end_at"] == iso(24, 22)
        # 实例窗口编辑绝不回写任务模板（两条路径无同步）
        stored = c.db.rows["planning_task"][0]
        assert stored.get("window_start_tod") is None and stored.get("window_end_tod") is None
        # est 未被本编辑改写；由下一次重算在新窗口内重新派生
        assert occ["est_start"] == iso(24, 10)
        assert planning_recompute.request_recompute.called  # 约束变化 → 请求自动重算
        planning.recompute_today(at(24, 11, 5))
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 18), iso(24, 18, 30))


def test_occurrence_window_pin_zero_freedom_manual_anchor():
    # 收窄至恰等占用跨度（§13.2）：manual 锚点钉住，重算不移动。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=60)
        occ = c.rows[0]
        planning.patch_occurrence(
            occ["id"], {"window_start_at": iso(24, 18), "window_end_at": iso(24, 19)}, at(24, 11))
        assert (occ["estimated_time_source"], occ["fixed_source"], occ["is_fixed"]) == (
            "manual", "manual", True)
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 18), iso(24, 19))
        assert occ["nominal_start"] == iso(24, 18)
        planning.recompute_today(at(24, 12))
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 18), iso(24, 19))


def test_occurrence_window_narrow_below_occupancy_rejected():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=60)
        occ = c.rows[0]
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 18), "window_end_at": iso(24, 18, 30)},
                at(24, 11))
        assert error.value.status_code == 400
        assert "剩余空间不足" in str(error.value)
        assert occ["window_start_at"] is None  # 零写入


def test_occurrence_window_rejects_end_before_start_and_boundary_crossing():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 20), "window_end_at": iso(24, 19)},
                at(24, 11))
        assert "结束必须晚于开始" in str(error.value)
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 5), "window_end_at": iso(24, 7)},
                at(24, 11))
        assert "不能跨越每日刷新时间 06:00" in str(error.value)
        # 端点接触合法（start == boundary / end == boundary），且窗口在未来
        planning.patch_occurrence(
            occ["id"], {"window_start_at": iso(25, 6), "window_end_at": iso(25, 8)}, at(24, 11))
        assert occ["window_start_at"] == iso(25, 6)


def test_occurrence_window_single_sided_edits():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        # 只有最晚完成（未来）：可行
        planning.patch_occurrence(occ["id"], {"window_end_at": iso(25, 22)}, at(24, 11))
        assert (occ["window_start_at"], occ["window_end_at"]) == (None, iso(25, 22))
        # 只有最晚完成（已越过）：拒绝，不顺延
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(occ["id"], {"window_end_at": iso(24, 9)}, at(24, 11))
        assert "剩余空间不足" in str(error.value)
        assert occ["window_end_at"] == iso(25, 22)
        # 只有最早开始：无上界，不补隐式截止；sweep 永不因窗口超时
        planning.patch_occurrence(
            occ["id"], {"window_start_at": iso(24, 18), "window_end_at": None}, at(24, 11, 30))
        assert (occ["window_start_at"], occ["window_end_at"]) == (iso(24, 18), None)
        assert planning.sweep_timeouts(at(25, 7))["timed_out"] == 0


def test_occurrence_window_no_clear_existing_window():
    # 一轮 Review 裁决 6：已带窗口约束的当前轮不允许 PATCH 成双 NULL——
    # 不得通过当前编辑取消这一轮既有的窗口 / 超时约束；单边保留合法。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        occ = c.rows[0]
        before = (occ["window_start_at"], occ["window_end_at"])
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": None, "window_end_at": None}, at(24, 11))
        assert error.value.status_code == 400
        assert "不能清空" in str(error.value)
        assert (occ["window_start_at"], occ["window_end_at"]) == before  # 零写入
        # 改为单边合法（保留最晚完成约束）
        planning.patch_occurrence(occ["id"], {"window_start_at": None}, at(24, 11, 30))
        assert (occ["window_start_at"], occ["window_end_at"]) == (None, before[1])
        # 剩单边时双 NULL 同样被拒（仍有窗口约束）
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": None, "window_end_at": None}, at(24, 11, 45))
        assert "不能清空" in str(error.value)
    # 本来就无窗口的实例：显式双 NULL 是 no-op（不创造新行为）
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.patch_occurrence(
            occ["id"], {"window_start_at": None, "window_end_at": None}, at(24, 11))
        assert occ["window_start_at"] is None and occ["window_end_at"] is None
        # 无窗口不因窗口模型超时（§22.5）
        assert planning.sweep_timeouts(at(26, 7))["timed_out"] == 0
        assert occ["status"] == "pending"


def test_occurrence_window_rejected_for_closed_or_timeout():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 11))
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 18)}, at(24, 11, 30))
        assert error.value.status_code == 422
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="09:00", window_end_tod="12:00")
        occ = c.rows[0]
        planning.sweep_timeouts(at(24, 12, 30))
        assert occ["status"] == "timeout"
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 18)}, at(24, 13))
        assert error.value.status_code == 422


def test_occurrence_window_anchor_stranding_rejected_for_fixed_est():
    # 固定锚点（manual 钉住）不在新窗口内 → 拒绝：锚点不会被重算移动，
    # 留在约束框之外会制造排程自相矛盾。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.patch_occurrence(occ["id"], {"est_start": iso(24, 19)}, at(24, 11))
        assert occ["is_fixed"] is True and occ["fixed_source"] == "manual"
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 14), "window_end_at": iso(24, 17)},
                at(24, 11, 30))
        assert "预估时间在新的可安排时段之外" in str(error.value)
        assert occ["window_start_at"] is None  # 零写入
    # 对照：可重排（automatic / pending）实例不受此限——est 由重算重新派生。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.patch_occurrence(
            occ["id"], {"window_start_at": iso(24, 14), "window_end_at": iso(24, 17)},
            at(24, 11))
        assert occ["window_start_at"] == iso(24, 14)
        planning.recompute_today(at(24, 11, 5))
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 14), iso(24, 14, 30))


def test_occurrence_window_and_est_cannot_mix():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"],
                {"window_start_at": iso(24, 18), "est_start": iso(24, 19)}, at(24, 11))
        assert "不能在同一次请求中同时修改" in str(error.value)
        assert occ["window_start_at"] is None and occ["est_start"] == iso(24, 10)


def test_occurrence_window_hollow_round_shared_and_envelope():
    # 中空：窗口是轮次级约束——编辑开始阶段行对同轮两阶段一致生效；
    # 可行性按完整包络判断；零自由度包络两阶段一起钉住。
    hollow = dict(is_hollow=True, hollow_start_content="准备", hollow_start_minutes=30,
                  hollow_wait_minutes=60, hollow_end_content="收尾", hollow_end_minutes=30)
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **hollow)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        planning.recompute_today(at(24, 7, 30))
        assert start["est_start"] == iso(24, 7, 30) and end["est_start"] == iso(24, 9)
        # 包络 120 分钟 > 窗口 90 分钟 → 拒绝
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                start["id"], {"window_start_at": iso(24, 18), "window_end_at": iso(24, 19, 30)},
                at(24, 8))
        assert "剩余空间不足" in str(error.value)
        assert start["window_start_at"] is None and end["window_start_at"] is None
        # 零自由度包络（窗口 120 == 开始 30 + 等待 60 + 结束 30）→ 两阶段
        # 钉住经 planning_patch_occurrence_round RPC 原子完成（user 批准）。
        planning.patch_occurrence(
            start["id"], {"window_start_at": iso(24, 18), "window_end_at": iso(24, 20)},
            at(24, 8))
        for row in (start, end):
            assert (row["window_start_at"], row["window_end_at"]) == (iso(24, 18), iso(24, 20))
            assert (row["estimated_time_source"], row["fixed_source"], row["is_fixed"]) == (
                "manual", "manual", True)
        assert (start["est_start"], start["est_end"]) == (iso(24, 18), iso(24, 18, 30))
        assert (end["est_start"], end["est_end"]) == (iso(24, 19, 30), iso(24, 20))
        planning.recompute_today(at(24, 9))
        assert (start["est_start"], end["est_start"]) == (iso(24, 18), iso(24, 19, 30))


def test_occurrence_window_edit_on_end_phase_updates_both_phases():
    hollow = dict(is_hollow=True, hollow_start_content="准备", hollow_start_minutes=30,
                  hollow_wait_minutes=60, hollow_end_content="收尾", hollow_end_minutes=30)
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **hollow)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        # 从结束阶段行编辑：两阶段窗口一致生效
        planning.patch_occurrence(
            end["id"], {"window_start_at": iso(24, 12), "window_end_at": iso(24, 16)}, at(24, 8))
        for row in (start, end):
            assert (row["window_start_at"], row["window_end_at"]) == (iso(24, 12), iso(24, 16))


# ── once：目标日期重定向边界 + 已生成实例冻结（§10 / §28.1） ──────

def test_once_redirect_target_date_not_before_today():
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-30")
        # 实际变化且合法
        planning.update_task(task["id"], {"target_date": "2026-09-27"}, at(24, 11))
        # 实际变化且早于当前业务日期 → 拒绝
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"target_date": "2026-09-23"}, at(24, 11, 30))
        assert error.value.status_code == 400
        assert "目标日期不能早于当前业务日期（2026-09-24）" in str(error.value)
        # 未实际变化（原值重发）不触发校验
        planning.update_task(task["id"], {"content": "只改名字"}, at(24, 12))
        assert c.db.rows["planning_task"][0]["target_date"] == "2026-09-27"


def test_once_generated_locks_schedule_identity():
    # 一轮 Review HIGH 裁决：once 已生成后锁定任务级排程身份——实际变化的
    # target_date / 未来窗口模板一律 400（once 没有「未来轮次」可消费新
    # 模板；禁止「任务显示 9/27、唯一实例仍属 9/24、9/27 永不生成」的半
    # 重定向状态）。调整已生成的这一次走当前实例窗口编辑。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-25",
                        window_start_tod="03:00", window_end_tod="05:00")
        assert len(c.rows) == 1  # 内部周期 9/24 提前生成
        occ = c.rows[0]
        assert (occ["schedule_date"], occ["window_start_at"], occ["window_end_at"]) == (
            "2026-09-24", iso(25, 3), iso(25, 5))
        task_before = dict(c.db.rows["planning_task"][0])
        before = _fields(occ)

        for payload in (
            {"target_date": "2026-09-26"},
            {"window_start_tod": "10:00"},
            {"window_end_tod": "12:00"},
            {"window_start_tod": "10:00", "window_end_tod": "12:00"},
            {"target_date": "2026-09-26", "window_start_tod": "10:00",
             "window_end_tod": "12:00"},
        ):
            with pytest.raises(planning.PlanningError) as error:
                planning.update_task(task["id"], payload, at(24, 11))
            assert error.value.status_code == 400, payload
            assert "单次待办已生成当前实例" in str(error.value)
            # 拒绝时 task / occurrence 零写入
            assert c.db.rows["planning_task"][0] == task_before
            assert _fields(occ) == before
        # 无变化 PATCH（幂等）仍可用
        planning.update_task(task["id"], {"target_date": "2026-09-25"}, at(24, 11, 30))
        assert c.db.rows["planning_task"][0]["target_date"] == "2026-09-25"
        # 已生成 once 的当前实例窗口调整仍按 current edit 规则可用
        planning.patch_occurrence(
            occ["id"], {"window_end_at": iso(25, 4, 30)}, at(24, 12))
        assert occ["window_end_at"] == iso(25, 4, 30)
        assert task_before["target_date"] == "2026-09-25"


def test_once_ungenerated_still_editable():
    # 对照：once 尚未生成（目标周期未到）→ target_date / 未来窗口模板照常
    # 可编辑（strict natural-date 语义重新校验）；到达内部周期后按编辑后的
    # 值生成。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-30",
                        window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows == []  # 内部周期未到，未生成
        planning.update_task(task["id"], {"target_date": "2026-09-28"}, at(24, 11))
        planning.update_task(
            task["id"], {"window_start_tod": "14:00", "window_end_tod": "18:00"}, at(24, 11, 30))
        planning.generate_due(at(28, 7))
        assert len(c.rows) == 1
        occ = c.rows[0]
        assert occ["schedule_date"] == "2026-09-28"
        assert (occ["window_start_at"], occ["window_end_at"]) == (iso(28, 14), iso(28, 18))


def test_once_lock_uses_canonical_tod_comparison():
    # 十五：once 已生成的锁定字段按语义值比较——DB "09:00:00" ≡ PATCH
    # "09:00"，幂等放行；同值锁定字段 + 其它合法字段修改 → 允许；真不同
    # 值 → 400。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-25",
                        window_start_tod="03:00", window_end_tod="05:00")
        assert len(c.rows) == 1
        stored = c.db.rows["planning_task"][0]
        stored["window_start_tod"] = "09:00:00"  # 模拟 DB 秒级表示
        # 幂等（语义同值）放行
        planning.update_task(task["id"], {"window_start_tod": "09:00"}, at(24, 11))
        # 同值锁定字段 + 其它合法字段修改 → 允许
        planning.update_task(task["id"], {"content": "只改名字"}, at(24, 11, 30))
        assert c.db.rows["planning_task"][0]["content"] == "只改名字"
        # 真不同值 → 400
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"window_start_tod": "09:30"}, at(24, 12))
        assert error.value.status_code == 400
        assert "单次待办已生成当前实例" in str(error.value)


def test_template_tod_format_error_chinese():
    # 十六：模板编辑入口的时间格式错误（"abc" / 非法类型）统一中文 400，
    # task 零写入。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30)
        before = dict(c.db.rows["planning_task"][0])
        for payload in ({"window_start_tod": "abc"}, {"window_end_tod": 25}):
            with pytest.raises(planning.PlanningError) as error:
                planning.update_task(task["id"], payload, at(24, 11))
            assert error.value.status_code == 400
            assert "时间格式无效" in str(error.value)
            assert c.db.rows["planning_task"][0] == before
