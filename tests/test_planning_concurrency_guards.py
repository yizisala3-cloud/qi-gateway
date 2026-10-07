"""批次 6 最终修复定向测试：并发安全与入口一致性（2026-09-28）。

覆盖最终 Review 的七个问题：
* 问题 1：RPC 锁内生命周期二次校验（Python 检查与 RPC 执行之间的并发
  状态变化在锁内被发现，整体拒绝、零写入）；普通单行窗口 / 预估编辑以
  条件 UPDATE 内联同一门控，不绕过。
* 问题 2：中空同轮两阶段的状态流转 + 时间联动统一经原子 RPC；注入失败
  两行整体回滚；不存在顺序双行写。
* 问题 3：once 身份编辑与生成共享 _maintenance_lock，交错不产生
  「任务日期 ≠ 唯一实例」。
* 问题 4：预估时间编辑复用统一生命周期门控；实际时间 / 说明的事实修正
  不受此限。
* 问题 5：可排程谓词 = 状态允许 + 无任何生命周期事实（pending + 事实
  字段的脏状态不进入重算）。
* 问题 6：actual_minutes 读派生输入带乐观等值条件，并发修改拒绝。
真库（锁内校验 / 回滚 / 窗口一致性）的权威证明见 pgserver 套件。
"""

import threading
from datetime import date

import pytest
from unittest import mock

from gateway import planning, planning_common, planning_occurrences, planning_recompute, planning_tasks
from tests.support.planning_context import Context, at
from tests.support.planning_fixtures import HOLLOW, _fields, iso

# Context 会 mock planning.request_recompute；模块导入时（任何 patch 生效前）
# 捕获真实现，供 BUG A / BUG B 测试还原真实登记行为。
_REAL_REQUEST_RECOMPUTE = planning_recompute.request_recompute


def _hollow_round(c):
    return (next(row for row in c.rows if row["phase"] == "start"),
            next(row for row in c.rows if row["phase"] == "end"))


# ── 问题 1：检查与写入之间的并发生命周期变化 ────────────────────────

def _complete_row(row):
    """模拟并发完成：状态 + 关闭 + 处理事实一次落齐。"""
    row["status"] = "completed"
    row["closed_at"] = iso(24, 11, 30)
    row["handled_at"] = iso(24, 11, 30)


def test_hollow_window_edit_rejected_after_concurrent_completion():
    # A: Python 门控通过（pending）→ B: 并发完成 → C: RPC 执行 →
    # 锁内二次校验拒绝，两行零写入。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        before_e = _fields(end)
        original_edit = planning_occurrences._occurrence_window_edit

        def edit_then_concurrent_complete(client, occ, task, payload, now):
            result = original_edit(client, occ, task, payload, now)
            _complete_row(start)  # B：并发请求在 RPC 执行前完成该行
            return result

        planning_occurrences._occurrence_window_edit = edit_then_concurrent_complete
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.patch_occurrence(
                    start["id"],
                    {"window_start_at": iso(24, 12), "window_end_at": iso(24, 16)},
                    at(24, 11))
        finally:
            planning_occurrences._occurrence_window_edit = original_edit
        assert error.value.status_code == 409
        assert "并发" in str(error.value)
        # B 的并发完成保留；A 的窗口编辑零写入（两行窗口均为空）
        assert start["status"] == "completed"
        assert (start["window_start_at"], start["window_end_at"]) == (None, None)
        assert (end["window_start_at"], end["window_end_at"]) == (None, None)
        assert _fields(end) == before_e


def test_single_row_window_update_guarded_against_concurrent_completion():
    # 普通单行窗口 UPDATE 同样内联生命周期条件：并发完成后条件未命中 →
    # 409、零写入。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        original_edit = planning_occurrences._occurrence_window_edit

        def edit_then_concurrent_complete(client, occ_ref, task, payload, now):
            result = original_edit(client, occ_ref, task, payload, now)
            _complete_row(occ)  # B：并发完成
            return result

        planning_occurrences._occurrence_window_edit = edit_then_concurrent_complete
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.patch_occurrence(
                    occ["id"], {"window_start_at": iso(24, 18)}, at(24, 11))
        finally:
            planning_occurrences._occurrence_window_edit = original_edit
        assert error.value.status_code == 409
        # B 的并发完成保留；A 的窗口编辑零写入
        assert occ["status"] == "completed"
        assert occ["window_start_at"] is None


# ── 问题 2：中空状态流转统一原子入口 ────────────────────────────────

def test_hollow_defer_uses_round_rpc_atomically():
    # 延后 = 目标行状态流转 + 兄弟行时间联动 + 展示一致：单次 RPC，
    # 无任何 planning_occurrence 顺序 UPDATE。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        planning_recompute.recompute_today(at(24, 7, 30))
        assert start["est_start"] == iso(24, 7, 30)
        calls = []
        import test_planning_phase1a as p1a
        original_rpc = p1a._RpcCall.execute
        original_query = p1a._Query.execute

        def spy_rpc(self):
            if self.fn == "planning_patch_occurrence_round":
                calls.append(("rpc", dict(self.params)))
            return original_rpc(self)

        def spy_query(self):
            if self.name == "planning_occurrence" and self.action == "update":
                calls.append(("update", list(self.filters)))
            return original_query(self)

        p1a._RpcCall.execute = spy_rpc
        p1a._Query.execute = spy_query
        try:
            planning.set_occurrence_status(
                start["id"], {"status": "deferred", "est_start": iso(24, 18)}, at(24, 8))
        finally:
            p1a._RpcCall.execute = original_rpc
            p1a._Query.execute = original_query
        rpcs = [params for kind, params in calls if kind == "rpc"]
        updates = [filters for kind, filters in calls if kind == "update"]
        assert len(rpcs) == 1 and not updates, calls
        params = rpcs[0]
        assert params["p_target_id"] == start["id"]
        assert params["p_target_patch"]["status"] == "deferred"
        assert params["p_target_patch"]["est_start"] == iso(24, 18)
        # 兄弟行只带时间联动补丁（写集合不扩大）
        assert params["p_sibling_patch"]["est_start"] == iso(24, 19, 30)
        assert not ({"status", "partial_note", "partial_at", "handled_at",
                     "closed_at"} & set(params["p_sibling_patch"]))
        assert start["status"] == "deferred" and start["est_start"] == iso(24, 18)
        assert end["est_start"] == iso(24, 19, 30)


def test_hollow_defer_failure_rolls_back_both_phases():
    # 注入第二阶段（RPC）失败：目标行状态与兄弟行时间全部回滚。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        planning_recompute.recompute_today(at(24, 7, 30))
        before_s, before_e = _fields(start), _fields(end)
        import test_planning_phase1a as p1a
        original_rpc = p1a._Database.rpc

        def failing_rpc(self, fn, params=None):
            if fn == "planning_patch_occurrence_round":
                raise RuntimeError("simulated second-phase failure")
            return original_rpc(self, fn, params)

        p1a._Database.rpc = failing_rpc
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.set_occurrence_status(
                    start["id"], {"status": "deferred", "est_start": iso(24, 18)},
                    at(24, 8))
        finally:
            p1a._Database.rpc = original_rpc
        assert error.value.status_code == 503
        assert _fields(start) == before_s and _fields(end) == before_e


def test_hollow_defer_from_in_progress_still_allowed():
    # 既有语义保留：延后可自执行中发起（RPC 宽松门：开放且无关闭事实）。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        planning_recompute.recompute_today(at(24, 7, 30))
        planning.set_occurrence_status(
            start["id"], {"status": "in_progress"}, at(24, 7, 45))
        planning.set_occurrence_status(
            start["id"], {"status": "deferred", "est_start": iso(24, 18)}, at(24, 8))
        assert start["status"] == "deferred" and start["est_start"] == iso(24, 18)
        assert end["est_start"] == iso(24, 19, 30)


def test_status_transition_is_conditionally_guarded():
    # 单行状态流转带状态等值条件：并发状态变化使条件未命中 → 409、零写入。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        original_reschedule = planning_occurrences._reschedule_occurrence

        def reschedule_then_concurrent_start(occ_ref, task, new_start, now):
            result = original_reschedule(occ_ref, task, new_start, now)
            occ["status"] = "in_progress"  # B：并发开始执行
            occ["actual_start"] = iso(24, 10, 45)
            return result

        planning_occurrences._reschedule_occurrence = reschedule_then_concurrent_start
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.set_occurrence_status(
                    occ["id"], {"status": "deferred", "est_start": iso(24, 18)},
                    at(24, 11))
        finally:
            planning_occurrences._reschedule_occurrence = original_reschedule
        assert error.value.status_code == 409
        # B 的并发开始保留；A 的延后（状态 + 时间修改）零写入（原 est 保持）
        assert occ["status"] == "in_progress"
        assert occ["est_start"] == iso(24, 10)


# ── 问题 3：once 编辑与生成互斥 ─────────────────────────────────────

def test_once_edit_and_generation_are_mutually_exclusive():
    # 交错场景：once 编辑持锁期间，并发生成必须等待——先生成旧实例再保存
    # 新日期的半状态不可能出现；编辑完成后生成从新日期创建，任务与唯一
    # 实例一致。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-25",
                        window_start_tod="18:00", window_end_tod="22:00")
        assert c.rows == []  # 内部周期未到，尚未生成
        generation_done = threading.Event()
        original_validate = planning_tasks._validate_template_window_constraints

        def validate_during_edit(row, now):
            original_validate(row, now)
            # 编辑已通过 once 检查、尚未保存：此刻并发生成必须被锁挡住
            thread = threading.Thread(
                target=lambda: (
                    planning._generate_due_quietly(c.db, at(26, 7)),
                    generation_done.set(),
                ))
            thread.start()
            thread.join(timeout=2)
            assert thread.is_alive(), (
                "generation must be blocked while once edit holds the task lock")

        planning_tasks._validate_template_window_constraints = validate_during_edit
        try:
            planning.update_task(
                task["id"],
                {"target_date": "2026-09-26",
                 "window_start_tod": "14:00", "window_end_tod": "18:00"},
                at(24, 11))
        finally:
            planning_tasks._validate_template_window_constraints = original_validate
        assert generation_done.wait(timeout=2)
        # 一致性：唯一实例从编辑后的新日期生成，任务与实例不背离
        assert len(c.rows) == 1
        assert c.rows[0]["schedule_date"] == "2026-09-26"
        assert c.rows[0]["window_start_at"].endswith("T14:00:00+08:00")
        assert c.db.rows["planning_task"][0]["target_date"] == "2026-09-26"


def test_once_edit_completes_before_generation_uses_new_date():
    # 对照顺序：编辑先完成（无实例 → 允许），生成随后从新日期创建。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-25",
                        window_start_tod="18:00", window_end_tod="22:00")
        planning.update_task(task["id"], {"target_date": "2026-09-26"}, at(24, 11))
        planning.generate_due(at(26, 7))
        assert len(c.rows) == 1
        assert c.rows[0]["schedule_date"] == "2026-09-26"
        assert c.db.rows["planning_task"][0]["target_date"] == "2026-09-26"


# ── 问题 4：预估时间编辑的统一生命周期门控 ──────────────────────────

def test_estimate_edit_gate_blocks_fact_bearing_rows():
    # completed / timeout / in_progress / partial 一律拒绝预估时间编辑，
    # 且零写入。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="09:00", window_end_tod="12:00")
        occ = c.rows[0]
        planning.sweep_timeouts(at(24, 12, 30))  # timeout
    scenarios = []
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 10, 30))
        scenarios.append(dict(occ))
    for seeded in scenarios:
        pass  # 场景在下方分别构造（fake 行不能跨 Context 复用）

    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
        before = _fields(occ)
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(occ["id"], {"est_start": iso(24, 18)}, at(24, 11))
        assert error.value.status_code == 422
        assert "修改预估时间" in str(error.value)
        assert _fields(occ) == before
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "半"}, at(24, 10, 30))
        before = _fields(occ)
        with pytest.raises(planning.PlanningError):
            planning.patch_occurrence(occ["id"], {"est_start": iso(24, 18)}, at(24, 11))
        assert _fields(occ) == before
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 10, 30))
        before = _fields(occ)
        with pytest.raises(planning.PlanningError):
            planning.patch_occurrence(occ["id"], {"est_start": iso(24, 18)}, at(24, 11))
        assert _fields(occ) == before
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="09:00", window_end_tod="12:00")
        occ = c.rows[0]
        planning.sweep_timeouts(at(24, 12, 30))
        assert occ["status"] == "timeout"
        before = _fields(occ)
        with pytest.raises(planning.PlanningError):
            planning.patch_occurrence(occ["id"], {"est_start": iso(24, 18)}, at(24, 13))
        assert _fields(occ) == before


def test_fact_correction_still_allowed_and_deferred_est_edit_allowed():
    # §23 历史修正不受预估门控影响：已完成实例补填实际时间 / 说明照常；
    # 尚未开始的 deferred 实例仍可修改预估时间。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "completed"}, at(24, 10, 30))
        planning.patch_occurrence(
            occ["id"], {"actual_end": iso(24, 10, 25),
                        "partial_note": "补记说明"}, at(24, 11))
        assert occ["actual_end"] == iso(24, 10, 25)
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "deferred", "est_start": iso(26, 9)}, at(24, 10, 30))
        assert not occ.get("actual_start")
        planning.patch_occurrence(occ["id"], {"est_start": iso(26, 10)}, at(24, 11))
        assert occ["est_start"] == iso(26, 10)


# ── 问题 5：可排程谓词含完整事实集合 ────────────────────────────────

def test_recompute_skips_fact_bearing_pending_rows():
    # pending + actual_end / pending + partial_at / partial 回 pending：
    # 脏状态行不进入重算；干净对照行照常排程。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        dirty_end = c.rows[0]
        c.create("daily", at(24, 10), estimated_minutes=30)
        clean = c.rows[1]
        dirty_end["actual_end"] = iso(24, 12)
        est_before = dirty_end["est_start"]
        planning_recompute.recompute_today(at(24, 13))
        assert clean["est_start"] == iso(24, 13)
        assert dirty_end["est_start"] == est_before  # 脏行不参与重算（est 保持）
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        dirty_partial = c.rows[0]
        c.create("daily", at(24, 10), estimated_minutes=30)
        clean = c.rows[1]
        dirty_partial["partial_at"] = iso(24, 12)
        est_before = dirty_partial["est_start"]
        planning_recompute.recompute_today(at(24, 13))
        assert clean["est_start"] == iso(24, 13)
        assert dirty_partial["est_start"] == est_before
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "半"}, at(24, 10, 30))
        assert occ["partial_at"] is not None
        planning.set_occurrence_status(occ["id"], {"status": "pending"}, at(24, 10, 45))
        est_before = occ["est_start"]
        planning_recompute.recompute_today(at(24, 13))
        assert occ["est_start"] == est_before  # partial_at 事实保留 → 不重排


# ── 问题 6：actual_minutes 乐观一致性 ───────────────────────────────

def test_actual_minutes_stale_read_rejected():
    # A 读取 start → B 并发修改 start → A 提交：读派生等值条件未命中 →
    # 409、零写入（三字段不一致不可能落库）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
        assert occ["actual_start"] == iso(24, 10, 30)
        original_compute = planning_common._compute_actual_minutes

        def compute_after_concurrent_start(merged):
            occ["actual_start"] = iso(24, 11)  # B：并发修改开始时刻
            return original_compute(merged)

        planning_common._compute_actual_minutes = compute_after_concurrent_start
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.patch_occurrence(
                    occ["id"], {"actual_end": iso(24, 11, 30)}, at(24, 12))
        finally:
            planning_common._compute_actual_minutes = original_compute
        assert error.value.status_code == 409
        # 零写入：B 的值保持，A 的 end / minutes 未落库
        assert occ["actual_start"] == iso(24, 11)
        assert occ.get("actual_end") is None
        assert occ.get("actual_minutes") is None


def test_actual_minutes_fresh_flow_computes_consistently():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
        planning.patch_occurrence(occ["id"], {"actual_end": iso(24, 11)}, at(24, 11, 5))
        assert occ["actual_end"] == iso(24, 11)
        assert occ.get("actual_minutes") == 30  # 10:30 → 11:00


# ── 最终验收修复：recompute 并发 / hollow 半提交 / once 跨进程 / minutes NULL ──

def test_recompute_skips_row_completed_after_read():
    # 问题 1（#6 收紧后）：A 读取 pending 并完成排程计算 → B 并发完成
    # （状态 + 事实）→ 该行是本次计算的参与行，快照漂移 = 整次计算输入
    # 失效 → 整批放弃（updated=0、stale_skipped=全部待写行，§19.1 不留
    # 「干净行新排程 + 脏行旧状态」的混合状态）；已完成实例事实不被覆盖。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        dirty = c.rows[0]
        c.create("daily", at(24, 10), estimated_minutes=30)
        clean = c.rows[1]
        original_estimate = planning_common._estimate_patch

        def patch_then_concurrent_complete(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            dirty["status"] = "completed"
            dirty["actual_start"] = iso(24, 10, 30)
            dirty["actual_end"] = iso(24, 11)
            dirty["handled_at"] = iso(24, 11, 5)
            dirty["closed_at"] = iso(24, 11, 5)
            return result

        planning_common._estimate_patch = patch_then_concurrent_complete
        try:
            result = planning_recompute.recompute_today(at(24, 13))
        finally:
            planning_common._estimate_patch = original_estimate
        assert result["updated"] == 0
        assert result.get("stale_skipped") == 2  # 整批放弃
        # 已完成实例保持完成状态与全部事实，est 未被覆盖
        assert dirty["status"] == "completed"
        assert dirty["est_start"] == iso(24, 10)
        assert dirty["actual_start"] == iso(24, 10, 30)
        assert dirty["actual_end"] == iso(24, 11)
        # 干净行同样保持旧排程（创建时已被排到 10:30——不再单独写入，
        # 「干净行新排程 + 脏行旧状态」的混合状态即缺陷）
        assert clean["est_start"] == iso(24, 10, 30)


def test_recompute_batch_failure_leaves_no_partial_schedule():
    # #6 缺口 A：跨任务中途写失败 → 整批回滚——两行都保持旧排程，不存在
    # 「第一行新排程、第二行旧排程」的混合状态（此前逐行提交，第 2 行失败
    # 时第 1 行已落库）；基础设施失败如实向上传播（不伪装成并发跳过）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        row_a = c.rows[0]
        c.create("daily", at(24, 10), estimated_minutes=30)
        row_b = c.rows[1]
        est_before = (row_a["est_start"], row_b["est_start"])
        original_rpc = c.db.rpc

        def failing_rpc(name, params=None):
            if name == "planning_apply_recompute_batch":
                raise RuntimeError("simulated mid-batch write failure")
            return original_rpc(name, params)

        with mock.patch.object(c.db, "rpc", side_effect=failing_rpc):
            with pytest.raises(RuntimeError):
                planning_recompute.recompute_today(at(24, 13))
        assert (row_a["est_start"], row_b["est_start"]) == est_before
        # 故障恢复后重算完整成功（两行一次写入）。
        result = planning_recompute.recompute_today(at(24, 13))
        assert result["updated"] == 2
        assert (row_a["est_start"], row_b["est_start"]) == (
            iso(24, 13), iso(24, 13, 30))


def test_recompute_batch_abandons_on_non_written_row_drift():
    # #6 缺口 B：expected 快照覆盖全部参与计算行——未写行（固定槽）读取后
    # 漂移同样使整次计算输入失效 → 整批放弃、待写行不落库（此前只守卫
    # 待写行自身，B 照常落库）。
    with Context() as c:
        # B（先建）：可移动行，08:30 重算会改写其 est（08:00 → 08:30）。
        c.create("daily", at(24, 8), estimated_minutes=30)
        movable = c.rows[0]
        # A（后建）：120 分钟零自由度窗口 10:00–12:00（固定槽，不参与重排、
        # 不会被写）。
        c.create("daily", at(24, 8), estimated_minutes=120,
                 window_start_tod="10:00", window_end_tod="12:00")
        fixed_row = c.rows[1]
        assert fixed_row["is_fixed"] is True
        est_before = movable["est_start"]
        original_estimate = planning_common._estimate_patch

        def patch_then_drift_fixed_slot(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            # 用户并发收窄固定槽窗口（未写行漂移）
            fixed_row["window_end_at"] = iso(24, 10)
            return result

        planning_common._estimate_patch = patch_then_drift_fixed_slot
        try:
            result = planning_recompute.recompute_today(at(24, 8, 30))
        finally:
            planning_common._estimate_patch = original_estimate
        assert result["updated"] == 0
        assert result.get("stale_skipped") == 1
        assert movable["est_start"] == est_before  # 待写行不落库
        # 用户的窗口修改照常成立（冲突由下一次重算派生呈现）。
        assert fixed_row["window_end_at"] == iso(24, 10)


def test_recompute_abandons_when_written_row_gains_actual_start():
    # #25（2026-10-02，R1 回退）：重算读取 pending 行并计算后、提交前，另一
    # 请求经合法 patch_occurrence 补录 actual_start（status 仍 pending）——
    # 旧计算不得把已开始实例重新排程：expected 快照与库行生命周期事实漂移
    # → 整批放弃（updated=0、stale_skipped=待写行数），已有事实保留
    #（基线 4c838a5 的旧条件 UPDATE 对同一交错返回 false，est 不动）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        est_before = occ["est_start"]
        original_rpc = c.db.rpc

        def record_actual_start_then_rpc(name, params=None):
            if name == "planning_apply_recompute_batch":
                # 计算与提交之间：另一请求补录实际开始（已落库）
                occ["actual_start"] = iso(24, 10, 45)
            return original_rpc(name, params)

        with mock.patch.object(c.db, "rpc",
                               side_effect=record_actual_start_then_rpc):
            result = planning_recompute.recompute_today(at(24, 13))
        assert result["updated"] == 0
        assert result.get("stale_skipped") == 1
        assert occ["est_start"] == est_before  # 旧排程结果不落库
        assert occ["actual_start"] == iso(24, 10, 45)  # 并发事实保留
        # 故障恢复视角：事实已在，下一次重算不再重排该行（不可排程）。
        assert not planning._freely_schedulable(occ, {})


def _batch_payload(rows, *, extra_single_fields=None):
    """按生产同形构造 truthful expected 与 singles（直接调批量 RPC 仿真）。"""
    expected = [planning._recompute_expected_snapshot(row) for row in rows]
    singles = []
    for row in rows:
        patch = planning_common._estimate_patch(at(24, 13), at(24, 13, 30),
                                         source="automatic")
        patch["updated_at"] = iso(24, 13)
        if extra_single_fields:
            patch.update(extra_single_fields)
        singles.append({"id": row["id"], **patch})
    return expected, singles


def test_recompute_batch_fake_rolls_back_on_late_single_guard():
    # 复审 26.10.2.15.01 R1（P3）：expected 如实覆盖两行，第二行 pending 但
    # 已携带 actual_start（不可排程）；singles 依次请求修改两行——第一行
    # 先被修改、第二行被写侧可排程守卫拒绝时，fake 必须整体撤回（真库
    # PC001 整体回滚：第一行保持 08:00），调用前已提交的 actual_start 保留，
    # 本次新增字段（nominal_start）一并移除；恢复就地作用于测试持有的
    # 同一 row 字典引用，完整行状态与调用前一致。
    with Context() as c:
        c.create("daily", at(24, 8), estimated_minutes=30)
        c.create("daily", at(24, 8), estimated_minutes=30)
        row_a, row_b = c.rows[0], c.rows[1]
        planning.patch_occurrence(
            row_b["id"], {"actual_start": iso(24, 8, 45)}, at(24, 8, 45))
        # 生产在行缺 nominal_start 时由补丁新增（重算入口同形）——删去以构造
        # 「本次新增字段」，回滚必须把它一并移除。
        row_a.pop("nominal_start", None)
        expected, singles = _batch_payload(c.rows, extra_single_fields={
            "nominal_start": iso(24, 13)})
        assert "nominal_start" not in row_a
        before_a, before_b = dict(row_a), dict(row_b)
        with pytest.raises(RuntimeError) as error:
            c.db.rpc("planning_apply_recompute_batch", {
                "p_expected": expected, "p_singles": singles,
                "p_rounds": [],
            }).execute()
        assert "schedule inputs drifted" in str(error.value)
        # 完整行状态逐字段恢复（第二行的并发事实不在本次写集合内，保留）；
        # 通过原先持有的引用断言——替身若换成新对象即在此暴露。
        assert c.rows[0] is row_a and c.rows[1] is row_b
        assert row_a == before_a
        assert "nominal_start" not in row_a  # 本次新增字段被移除
        assert row_b == before_b
        assert row_b["actual_start"] == iso(24, 8, 45)


def test_recompute_batch_fake_rolls_back_singles_when_round_rejected():
    # 同上 R1 的 rounds 段：singles 已修改第一行，后面的 round 因中空阶段
    # 携带生命周期事实被严格门拒绝——已执行的 singles 同样必须整体撤回。
    with Context() as c:
        c.create("daily", at(24, 8), estimated_minutes=30)
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        row_a = c.rows[0]
        start, end = _hollow_round(c)
        planning.patch_occurrence(
            start["id"], {"actual_start": iso(24, 8, 45)}, at(24, 8, 45))
        # 同上：行缺 nominal_start、由本次 singles 补丁新增，回滚须移除。
        row_a.pop("nominal_start", None)
        expected = [planning._recompute_expected_snapshot(row) for row in c.rows]
        patch = planning_common._estimate_patch(at(24, 13), at(24, 13, 30),
                                         source="automatic")
        patch["updated_at"] = iso(24, 13)
        patch["nominal_start"] = iso(24, 13)
        singles = [{"id": row_a["id"], **patch}]
        rounds = [{
            "target_id": start["id"], "sibling_id": end["id"],
            "target_patch": {"est_start": iso(24, 14), "est_end": iso(24, 14, 30),
                             "updated_at": iso(24, 13)},
            "sibling_patch": {"est_start": iso(24, 15), "est_end": iso(24, 15, 30),
                              "updated_at": iso(24, 13)},
        }]
        before_a, before_start, before_end = dict(row_a), dict(start), dict(end)
        with pytest.raises(RuntimeError) as error:
            c.db.rpc("planning_apply_recompute_batch", {
                "p_expected": expected, "p_singles": singles,
                "p_rounds": rounds,
            }).execute()
        assert "no longer editable" in str(error.value)
        assert c.rows[0] is row_a
        assert row_a == before_a
        assert "nominal_start" not in row_a
        assert start == before_start and end == before_end


def test_recompute_batch_fake_success_keeps_all_writes():
    # 成功路径不受回滚包装影响：singles 与 rounds 全部写入并正常返回写集，
    # 不发生任何恢复。
    with Context() as c:
        c.create("daily", at(24, 8), estimated_minutes=30)
        c.create("daily", at(24, 8), estimated_minutes=30)
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        row_a, row_b = c.rows[0], c.rows[1]
        start, end = _hollow_round(c)
        expected = [planning._recompute_expected_snapshot(row) for row in c.rows]
        patch = planning_common._estimate_patch(at(24, 13), at(24, 13, 30),
                                         source="automatic")
        patch["updated_at"] = iso(24, 13)
        singles = [{"id": row_a["id"], **patch},
                   {"id": row_b["id"], **patch}]
        rounds = [{
            "target_id": start["id"], "sibling_id": end["id"],
            "target_patch": {"est_start": iso(24, 14), "est_end": iso(24, 14, 30),
                             "estimated_time_source": "automatic",
                             "fixed_source": None, "schedule_managed": True,
                             "is_fixed": False, "updated_at": iso(24, 13)},
            "sibling_patch": {"est_start": iso(24, 15), "est_end": iso(24, 15, 30),
                              "estimated_time_source": "automatic",
                              "fixed_source": None, "schedule_managed": True,
                              "is_fixed": False, "updated_at": iso(24, 13)},
        }]
        result = c.db.rpc("planning_apply_recompute_batch", {
            "p_expected": expected, "p_singles": singles,
            "p_rounds": rounds,
        }).execute()
        assert sorted(result.data) == sorted(
            [row_a["id"], row_b["id"], start["id"], end["id"]])
        assert row_a["est_start"] == iso(24, 13)
        assert row_b["est_start"] == iso(24, 13)
        assert start["est_start"] == iso(24, 14)
        assert end["est_start"] == iso(24, 15)


def test_recompute_hollow_round_is_atomic():
    # 问题 2：中空同轮两阶段同时被重排 → 单次 RPC；注入失败 → 两阶段保持
    # 旧时间（无 A 新 B 旧半提交）。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        planning_recompute.recompute_today(at(24, 7, 30))
        old_start_est, old_end_est = start["est_start"], end["est_start"]
        calls = []
        import test_planning_phase1a as p1a
        original_rpc = p1a._RpcCall.execute
        original_query = p1a._Query.execute

        def spy_rpc(self):
            # #6：重算写集合经单次批量 RPC（planning_apply_recompute_batch）
            # 提交——不再逐行 UPDATE、不再逐轮 round patch。
            if self.fn == "planning_apply_recompute_batch":
                calls.append(dict(self.params))
            return original_rpc(self)

        def spy_query(self):
            if self.name == "planning_occurrence" and self.action == "update":
                calls.append({"__update__": True})
            return original_query(self)

        p1a._RpcCall.execute = spy_rpc
        p1a._Query.execute = spy_query
        try:
            planning_recompute.recompute_today(at(24, 9))
        finally:
            p1a._RpcCall.execute = original_rpc
            p1a._Query.execute = original_query
        assert len(calls) == 1 and "__update__" not in calls[0], calls
        assert calls[0]["p_rounds"], "中空两阶段经批量 RPC 的 rounds 段提交"
        assert start["est_start"] == iso(24, 9) and end["est_start"] == iso(24, 10, 30)
        # 注入失败：两阶段整体回滚（保持本次重算前的值，无 A 新 B 旧）
        pre_failure = (start["est_start"], end["est_start"])
        calls.clear()

        def failing_rpc(self):
            if self.fn == "planning_apply_recompute_batch":
                raise RuntimeError("simulated phase failure")
            return original_rpc(self)

        p1a._RpcCall.execute = failing_rpc
        try:
            # 基础设施失败必须向上传播（不伪装成并发跳过——最终修复问题 6）；
            # 两阶段保持本次重算前的值（无 A 新 B 旧）。
            with pytest.raises(RuntimeError):
                planning_recompute.recompute_today(at(24, 11))
        finally:
            p1a._RpcCall.execute = original_rpc
        assert (start["est_start"], end["est_start"]) == pre_failure
        assert start["est_start"] != iso(24, 11) and end["est_start"] != iso(24, 11)


def test_once_edit_cross_process_race_closed_by_database_lock():
    # 问题 3（跨进程）：进程内 RLock 失效的多 worker 场景——直接以数据库
    # 守护 RPC 的语义验证：生成侧锁内插入旧日期实例后，编辑侧锁内复核发现
    # 实例已存在 → 拒绝（任务日期不变）；编辑先提交 → 生成侧锁内复核发现
    # 定义漂移 → 本轮作废（0 行），下一次维护从新日期生成。
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-25")
        # 「进程 B」：生成抢先（读旧定义）并锁内插入
        task_rows = [dict(c.db.rows["planning_task"][0])]
        planning._generate_due_quietly(c.db, at(25, 7))
        assert len(c.rows) == 1
        # 「进程 A」：编辑侧锁内复核发现实例已存在 → 拒绝、任务日期不变
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"target_date": "2026-09-26"}, at(24, 11))
        assert "单次待办已生成当前实例" in str(error.value)
        assert c.db.rows["planning_task"][0]["target_date"] == "2026-09-25"
    with Context() as c:
        task = c.create("once", at(24, 10), target_date="2026-09-25")
        # 「进程 A」：编辑先提交（新日期 9/26）
        planning.update_task(task["id"], {"target_date": "2026-09-26"}, at(24, 11))
        # 「进程 B」：生成按旧读（9/25）计算后锁内提交 → 定义漂移 → 作废
        stale_task = dict(c.db.rows["planning_task"][0])
        stale_task["target_date"] = "2026-09-25"
        created = planning._create_occurrences(c.db, stale_task, date(2026, 9, 25), at(25, 7))
        assert created == 0 and c.rows == []
        # 下一次维护按新定义生成 → 任务与唯一实例一致
        planning.generate_due(at(26, 7))
        assert len(c.rows) == 1
        assert c.rows[0]["schedule_date"] == "2026-09-26"


def test_actual_minutes_null_old_value_participates_in_guard():
    # 问题 4：读取 actual_start=NULL → B 并发写入 actual_start → A 提交
    # 时 IS NULL 条件未命中 → 409、零写入（不会留下错误 minutes）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
        assert occ.get("actual_start")  # in_progress 已有开始事实
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        # 构造 pending + actual_end 事实（actual_start=NULL 的读取场景）
        occ["actual_end"] = iso(24, 12)
        original_compute = planning_common._compute_actual_minutes

        def compute_after_concurrent_start(merged):
            occ["actual_start"] = iso(24, 10, 30)  # B：并发写入开始时刻
            return original_compute(merged)

        planning_common._compute_actual_minutes = compute_after_concurrent_start
        try:
            with pytest.raises(planning.PlanningError) as error:
                # A 修正 actual_end：minutes 读派生输入 actual_start=NULL
                planning.patch_occurrence(
                    occ["id"], {"actual_end": iso(24, 12, 30)}, at(24, 13))
        finally:
            planning_common._compute_actual_minutes = original_compute
        assert error.value.status_code == 409
        # B 的写入保持；A 未产生错误 minutes（start=09:30, end, minutes 不一致不可能落库）
        assert occ["actual_start"] == iso(24, 10, 30)
        assert occ["actual_end"] == iso(24, 12)


def test_rpc_window_edit_cannot_use_status_to_lower_gate():
    # 问题 5：status='pending' + 窗口修改 + actual_start 事实存在 → 严格门
    # （status 不能降低保护）→ 拒绝。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        planning_recompute.recompute_today(at(24, 7, 30))
        planning.set_occurrence_status(start["id"], {"status": "in_progress"}, at(24, 7, 45))
        assert start["actual_start"]
        before_s, before_e = _fields(start), _fields(end)
        # 直接调用 RPC：status='pending' + 窗口修改 + actual_start 存在 →
        # 严格门（status 不能降低保护）→ 拒绝、零写入。
        with pytest.raises(planning.PlanningError) as error:
            planning._atomic_round_write(
                c.db, start,
                {"status": "pending",
                 "window_start_at": iso(24, 12), "window_end_at": iso(24, 16),
                 "updated_at": iso(24, 8)},
                end,
                {"window_start_at": iso(24, 12), "window_end_at": iso(24, 16),
                 "updated_at": iso(24, 8)})
        assert error.value.status_code == 409
        assert _fields(start) == before_s and _fields(end) == before_e


def test_recompute_skips_row_with_ownership_drift():
    # M3 守卫：重算读取后、写入前，实例被用户手动钉住（所有权元组漂移
    # automatic→manual）→ 条件 UPDATE 未命中 → 旧自动排程结果不覆盖。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        assert occ["estimated_time_source"] == "automatic"
        original_estimate = planning_common._estimate_patch

        def patch_then_manual_takeover(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            occ["est_start"] = iso(24, 19)
            occ["est_end"] = iso(24, 19, 30)
            occ["estimated_time_source"] = "manual"
            occ["fixed_source"] = "manual"
            occ["is_fixed"] = True
            occ["schedule_managed"] = False
            return result

        planning_common._estimate_patch = patch_then_manual_takeover
        try:
            result = planning_recompute.recompute_today(at(24, 13))
        finally:
            planning_common._estimate_patch = original_estimate
        # 手动 19:00 不被旧自动结果覆盖
        assert (occ["est_start"], occ["est_end"]) == (iso(24, 19), iso(24, 19, 30))
        assert occ["estimated_time_source"] == "manual"
        assert result["updated"] == 0


def test_recompute_skips_row_with_ownership_drift_single_row():
    # D-M3 守卫（单行路径）：重算读取后、写入前，用户把同一时刻重新保存为
    # 手动锚点（est 不变、所有权 automatic→manual 漂移）→ 单行条件 UPDATE
    # 的所有权条件未命中 → 自动重算结果不得把 manual 改回 automatic。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        assert occ["estimated_time_source"] == "automatic"
        original_estimate = planning_common._estimate_patch

        def patch_then_pin_in_place(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            # 用户并发操作：同一时刻改存为 manual 锚点（est 不变）
            occ["estimated_time_source"] = "manual"
            occ["fixed_source"] = "manual"
            occ["is_fixed"] = True
            occ["schedule_managed"] = False
            return result

        planning_common._estimate_patch = patch_then_pin_in_place
        try:
            result = planning_recompute.recompute_today(at(24, 13))
        finally:
            planning_common._estimate_patch = original_estimate
        # manual 锚点保持（不被自动重算改回 automatic）
        assert occ["estimated_time_source"] == "manual"
        assert occ["fixed_source"] == "manual"
        assert result["updated"] == 0


def test_recompute_skips_row_with_window_drift():
    # M4 守卫：重算读取旧窗口（18:00–22:00）计算出新 est（18:30）后、写入
    # 前，用户把窗口收窄到 15:00–17:00 → 快照漂移 → 旧结果（窗外）不得落库。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="18:00", window_end_tod="22:00")
        occ = c.rows[0]
        assert occ["est_start"] == iso(24, 18)  # 创建重算：placed 在窗口起点
        original_estimate = planning_common._estimate_patch

        def patch_then_window_narrow(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            # 用户并发收窄窗口（漂移）
            occ["window_start_at"] = iso(24, 15)
            occ["window_end_at"] = iso(24, 17)
            return result

        planning_common._estimate_patch = patch_then_window_narrow
        try:
            planning_recompute.recompute_today(at(24, 18, 30))
        finally:
            planning_common._estimate_patch = original_estimate
        # est 保持 18:00（18:30 的旧结果被放弃）；窗口保持用户收窄值
        assert occ["est_start"] == iso(24, 18)
        assert occ["window_start_at"] == iso(24, 15)
        assert occ["window_end_at"] == iso(24, 17)


def test_sweep_hollow_round_single_statement_atomic():
    # M5 守卫：sweep 对中空同轮两阶段只发一条 round 级 UPDATE（语句级
    # 原子）——不存在「第一阶段成功、第二阶段失败」的顺序写。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        frozen = planning._iso(at(24, 12))
        start["window_end_at"] = frozen
        end["window_end_at"] = frozen
        updates = []
        import test_planning_phase1a as p1a
        original_execute = p1a._Query.execute

        def spy_execute(self):
            if self.name == "planning_occurrence" and self.action == "update":
                updates.append(dict(self.filters))
            return original_execute(self)

        p1a._Query.execute = spy_execute
        try:
            planning.sweep_timeouts(at(24, 12, 30))
        finally:
            p1a._Query.execute = original_execute
        # 恰一条 round 级 UPDATE（task_id + round_key 定位），无逐行写
        assert len(updates) == 1
        assert updates[0].get("task_id") == start["task_id"]
        assert updates[0].get("round_key") == start["round_key"]
        assert start["status"] == "timeout" and end["status"] == "timeout"
        assert start["closed_at"] == end["closed_at"] == frozen


def test_discard_rpc_failure_leaves_everything_unchanged():
    # M6 守卫：discard RPC 失败（整命令回滚）→ 任务仍启用、开放实例仍开放、
    # 无半关闭状态。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        import test_planning_phase1a as p1a
        original_rpc = p1a._Database.rpc

        def failing_rpc(self, fn, params=None):
            if fn == "planning_discard_task":
                raise RuntimeError("simulated discard failure")
            return original_rpc(self, fn, params)

        p1a._Database.rpc = failing_rpc
        try:
            with pytest.raises(RuntimeError):
                planning.set_occurrence_status(
                    start["id"], {"status": "discarded"}, at(24, 8))
        finally:
            p1a._Database.rpc = original_rpc
        assert c.db.rows["planning_task"][0]["is_active"] is True
        assert start["status"] == "pending" and end["status"] == "pending"
        assert start.get("closed_at") is None and end.get("closed_at") is None


def test_recompute_sort_order_drift_skips_and_preserves_mark():
    # 问题 3：A 按 A→B 算出 09:00/09:30；持久化阶段用户 save_order 改为
    # B→A → sort_order 漂移 → 条件写未命中 → 旧结果全部放弃（updated=0、
    # stale_skipped=2）；重算等待标记不被清除；下一次 recompute 按 B→A
    # 生成新时间。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        task_a = c.db.rows["planning_task"][0]
        c.create("daily", at(24, 10), estimated_minutes=30)
        task_b = c.db.rows["planning_task"][1]
        row_a = next(r for r in c.rows if r["task_id"] == task_a["id"])
        row_b = next(r for r in c.rows if r["task_id"] == task_b["id"])
        assert row_a["sort_order"] < row_b["sort_order"]
        state_row = c.db.rows["planning_recompute_state"][0]

        order_state = {"saved": False}
        original_estimate = planning_common._estimate_patch

        def patch_then_reorder(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            if not order_state["saved"]:
                order_state["saved"] = True
                # 用户并发：save_order 改为 B→A（产生新排序）；其
                # request_recompute 在 Context 中被 mock，此处直接写状态行
                # 模拟并发产生的新重算请求标记
                planning.save_order([task_b["id"], task_a["id"]], at(24, 12))
                state_row["requested_at"] = planning._iso(at(24, 12))
                state_row["reason"] = "reorder"
            return result

        est_before = {"a": row_a["est_start"], "b": row_b["est_start"]}
        planning_common._estimate_patch = patch_then_reorder
        try:
            result = planning_recompute.recompute_today(at(24, 13))
        finally:
            planning_common._estimate_patch = original_estimate
        assert result["updated"] == 0
        assert result.get("stale_skipped") == 2
        # 旧结果不得落库（est 保持读取时的值）
        assert row_a["est_start"] == est_before["a"]
        assert row_b["est_start"] == est_before["b"]
        # 新重算请求仍然保留（未被 stale recompute 清掉）
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        # 下一次 recompute 按 B→A 正常生成：B 先于 A
        planning_recompute.recompute_today(at(24, 14))
        assert row_b["est_start"] == iso(24, 14)
        assert row_a["est_start"] == iso(24, 14, 30)


def test_recompute_hollow_round_sort_order_drift_skips_whole_round():
    # #13 复现 A：两个中空待办按 A→B 算出排程；持久化阶段用户 save_order
    # 把完整顺序改为 B→A → 中空 expected 快照此前不含 sort_order，旧结果
    # （A 在前）仍全部写入且无 stale_skipped。修复后快照携带两阶段排序：
    # 两轮旧结果整体放弃（updated=0、stale_skipped=4），原 est 不变；
    # 下一次重算按 B→A 生成新时间。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        task_a = c.db.rows["planning_task"][0]
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        task_b = c.db.rows["planning_task"][1]
        rows_by = {(r["task_id"], r["phase"]): r for r in c.rows}
        a_start, a_end = rows_by[(task_a["id"], "start")], rows_by[(task_a["id"], "end")]
        b_start, b_end = rows_by[(task_b["id"], "start")], rows_by[(task_b["id"], "end")]
        assert a_start["sort_order"] < b_start["sort_order"]  # 初始 A 在前
        est_before = {r["id"]: (r["est_start"], r["est_end"])
                      for r in (a_start, a_end, b_start, b_end)}

        order_state = {"saved": False}
        original_estimate = planning_common._estimate_patch

        def patch_then_reorder(start, end, *, source, fixed_source=None):
            result = original_estimate(start, end, source=source, fixed_source=fixed_source)
            if not order_state["saved"]:
                order_state["saved"] = True
                # 用户并发：save_order 改为 B.start、B.end、A.start、A.end
                planning.save_order(
                    [b_start["id"], b_end["id"], a_start["id"], a_end["id"]],
                    at(24, 12))
            return result

        planning_common._estimate_patch = patch_then_reorder
        try:
            result = planning_recompute.recompute_today(at(24, 14))
        finally:
            planning_common._estimate_patch = original_estimate
        # 两轮旧结果全部放弃：不写半新半旧、不虚报 updated。
        assert result["updated"] == 0
        assert result.get("stale_skipped") == 4
        for row in (a_start, a_end, b_start, b_end):
            assert (row["est_start"], row["est_end"]) == est_before[row["id"]]
        # 下一次重算按 B→A 正常生成：B 先于 A（B.end 等待收口后游标 17:00，
        # A.start 从 17:00 起）。
        planning_recompute.recompute_today(at(24, 15))
        assert b_start["est_start"] == iso(24, 15)
        assert a_start["est_start"] == iso(24, 17)


def test_stale_recompute_does_not_clear_wait_mark():
    # M5 守卫：stale_skipped 的 recompute 不得清掉重算等待标记（含并发
    # save_order 产生的新请求）；干净结果才清。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        _register_recompute(c, "reorder", at(24, 12))
        state_row = c.db.rows["planning_recompute_state"][0]

        def stale_recompute(now=None):
            return {"updated": 0, "at": planning._iso(at(24, 13)),
                    "conflicts": [], "stale_skipped": 1}

        with mock.patch.object(planning_recompute, "recompute_today", stale_recompute):
            # now = requested_at + 30 分钟等待（默认 RECOMPUTE_WAIT），进入
            # 自动重算分支
            planning.run_maintenance(at(24, 13))
        # stale 结果不视为已完成：标记必须保留（M5 守卫核心断言）
        assert state_row["requested_at"] == planning._iso(at(24, 12))

        def clean_recompute(now=None):
            return {"updated": 0, "at": planning._iso(at(24, 14)), "conflicts": []}

        with mock.patch.object(planning_recompute, "recompute_today", clean_recompute):
            planning.run_maintenance(at(24, 14))
        assert state_row["requested_at"] is None


def test_update_task_deactivate_is_single_atomic_rpc():
    # M1 守卫（问题 1A）：update_task 停用 = 单事务 RPC——不出现任何
    # 事务外 planning_occurrence 写；task 停用失败（注入）时整体命令
    # 不产生半状态。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        task_id = c.db.rows["planning_task"][0]["id"]
        # §25（2026-10-07）：播种完成事实 → 删除走历史保留分支（本测试
        # 关注 RPC 原子性而非分支选择）。
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": task_id})
        events = []
        import test_planning_phase1a as p1a
        original_rpc = p1a._Database.rpc
        original_query = p1a._Query.execute

        def spy_rpc(self, fn, params=None):
            if fn == "planning_discard_task":
                events.append(("rpc", dict(params or {})))
            return original_rpc(self, fn, params)

        def spy_query(self):
            if self.name == "planning_occurrence" and self.action == "update":
                events.append(("occ_update", dict(self.payload)))
            if self.name == "planning_task" and self.action == "update":
                events.append(("task_update_fail", {}))
                raise RuntimeError("simulated task update failure")
            return original_query(self)

        p1a._Database.rpc = spy_rpc
        p1a._Query.execute = spy_query
        try:
            planning.update_task(task_id, {"is_active": False}, at(24, 8))
        finally:
            p1a._Database.rpc = original_rpc
            p1a._Query.execute = original_query
        rpcs = [p for kind, p in events if kind == "rpc"]
        assert len(rpcs) == 1, events
        assert not [e for e in events if e[0] == "occ_update"], events
        assert not [e for e in events if e[0] == "task_update_fail"], events
        assert c.db.rows["planning_task"][0]["is_active"] is False
        assert start["status"] == "discarded" and end["status"] == "discarded"


def test_update_task_deactivate_failure_leaves_no_half_state():
    # 注入主任务更新失败：正确实现下废弃已由 RPC 原子完成（无事务外
    # occurrence 写），两阶段保持 discarded、任务保持停用——一致性不被
    # 后续失败破坏。M1 变异（恢复事务外两段写）下本测试红。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        task_id = c.db.rows["planning_task"][0]["id"]
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": task_id})
        import test_planning_phase1a as p1a
        original_query = p1a._Query.execute

        def failing_task_update(self):
            if (self.name == "planning_task" and self.action == "update"
                    and self.payload.get("is_active") is False):
                raise RuntimeError("simulated main update failure")
            return original_query(self)

        p1a._Query.execute = failing_task_update
        try:
            planning.update_task(task_id, {"is_active": False}, at(24, 8))
        finally:
            p1a._Query.execute = original_query
        assert c.db.rows["planning_task"][0]["is_active"] is False
        assert start["status"] == "discarded" and end["status"] == "discarded"


def test_sweep_skips_row_with_window_drift():
    # 明日可用 BLOCKER 1：扫描读取 window_end_at=12:00 后、写入前并发改为
    # 18:00 → 条件 UPDATE 未命中 → 实例保持开放、closed_at 不得写成 12:00。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="09:00", window_end_tod="12:00")
        occ = c.rows[0]
        original = planning_common._parse_dt

        def parse_then_extend(value, field):
            result = original(value, field)
            if field == "window_end_at" and value == occ["window_end_at"]:
                # 并发：用户把最晚完成延到 18:00
                occ["window_end_at"] = iso(24, 18)
            return result

        planning_common._parse_dt = parse_then_extend
        try:
            result = planning.sweep_timeouts(at(24, 12, 30))
        finally:
            planning_common._parse_dt = original
        assert result["timed_out"] == 0
        # 实例仍开放、窗口保持 18:00、closed_at 未写入
        assert occ["status"] == "pending"
        assert occ["window_end_at"] == iso(24, 18)
        assert occ.get("closed_at") is None


def test_sweep_skips_row_with_new_lifecycle_fact():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_end_tod="12:00")
        occ = c.rows[0]
        original = planning_common._parse_dt

        def parse_then_start(value, field):
            result = original(value, field)
            if field == "window_end_at" and value == occ["window_end_at"]:
                occ["actual_start"] = iso(24, 11)
            return result

        planning_common._parse_dt = parse_then_start
        try:
            result = planning.sweep_timeouts(at(24, 12, 30))
        finally:
            planning_common._parse_dt = original
        assert result["timed_out"] == 0
        assert occ["status"] == "pending"
        assert occ["actual_start"] == iso(24, 11)
        assert occ.get("closed_at") is None


def test_sweep_still_times_out_unchanged_in_progress_row():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_end_tod="12:00")
        occ = c.rows[0]
        occ["status"] = "in_progress"
        occ["actual_start"] = iso(24, 11)
        result = planning.sweep_timeouts(at(24, 12, 30))
        assert result["timed_out"] == 1
        assert occ["status"] == "timeout"
        assert occ["closed_at"] == iso(24, 12)


def test_discard_preserves_concurrently_completed_target():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        original_rpc = c.db.rpc

        def complete_before_discard(fn, params=None):
            if fn == "planning_discard_task":
                _complete_row(occ)
                occ["actual_start"] = iso(24, 10)
                occ["actual_end"] = iso(24, 11, 30)
                occ["actual_minutes"] = 90
            return original_rpc(fn, params)

        c.db.rpc = complete_before_discard
        try:
            planning.set_occurrence_status(
                occ["id"], {"status": "discarded"}, at(24, 12))
        finally:
            c.db.rpc = original_rpc
        assert c.db.rows["planning_task"][0]["is_active"] is False
        assert occ["status"] == "completed"
        assert occ["actual_start"] == iso(24, 10)
        assert occ["actual_end"] == iso(24, 11, 30)
        assert occ["actual_minutes"] == 90
        assert occ["handled_at"] == iso(24, 11, 30)


def test_generation_drift_does_not_advance_fixed_cursor():
    with Context() as c:
        c.create("interval", at(24, 7), refresh_mode="fixed_interval",
                 interval_days=1)
        task = c.db.rows["planning_task"][0]
        before = task["refresh_generated_through"]
        original_rpc = c.db.rpc
        drifted = False

        def change_content_before_insert(fn, params=None):
            nonlocal drifted
            if fn == "planning_insert_round_occurrence" and not drifted:
                drifted = True
                task["content"] = "新内容"
                task["updated_at"] = iso(25, 7)
            return original_rpc(fn, params)

        c.db.rpc = change_content_before_insert
        try:
            result = planning.generate_due(at(26, 7))
        finally:
            c.db.rpc = original_rpc
        assert result.get("errors")
        assert task["refresh_generated_through"] == before
        retry = planning.generate_due(at(26, 7))
        assert retry["created"] == 2
        assert task["refresh_generated_through"] == "2026-09-26"
        assert all(row["content_snapshot"] == "新内容"
                   for row in c.rows if row["schedule_date"] > before)


def test_generation_cursor_cas_does_not_overwrite_new_edit():
    with Context() as c:
        c.create("interval", at(24, 7), refresh_mode="fixed_interval",
                 interval_days=1)
        task = c.db.rows["planning_task"][0]
        before = task["refresh_generated_through"]
        import test_planning_phase1a as p1a
        original_execute = p1a._Query.execute
        drifted = False

        def edit_before_cursor_write(query):
            nonlocal drifted
            if (query.name == "planning_task" and query.action == "update"
                    and "refresh_generated_through" in query.payload and not drifted):
                drifted = True
                task["content"] = "编辑后"
                task["updated_at"] = iso(25, 8)
            return original_execute(query)

        p1a._Query.execute = edit_before_cursor_write
        try:
            result = planning.generate_due(at(26, 7))
        finally:
            p1a._Query.execute = original_execute
        assert result.get("errors")
        assert task["refresh_generated_through"] == before
        # 已插入的旧轮合法且唯一；重试从未推进的游标补齐之后的轮次。
        retry = planning.generate_due(at(26, 7))
        assert retry["created"] >= 1
        assert task["refresh_generated_through"] == "2026-09-26"


def test_deactivate_with_other_changes_rejected_zero_write():
    # 明日可用 HIGH（方案 A）：is_active=false + content 修改 → 前置拒绝
    # （要求停用单独提交），task / occurrence 零写入。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        task_id = c.db.rows["planning_task"][0]["id"]
        before_task = dict(c.db.rows["planning_task"][0])
        before_start, before_end = _fields(start), _fields(end)
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(
                task_id, {"is_active": False, "content": "新文案"}, at(24, 8))
        assert error.value.status_code == 400
        assert "单独执行停用" in str(error.value)
        assert c.db.rows["planning_task"][0] == before_task
        assert _fields(start) == before_start and _fields(end) == before_end


def test_update_task_deactivate_rpc_failure_atomic():
    # 主更新不再写字段；RPC 失败 → 整体命令失败，无半状态。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30, **HOLLOW)
        start, end = _hollow_round(c)
        task_id = c.db.rows["planning_task"][0]["id"]
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": task_id})
        import test_planning_phase1a as p1a
        original_rpc = p1a._Database.rpc

        def failing_rpc(self, fn, params=None):
            if fn == "planning_discard_task":
                raise RuntimeError("simulated discard failure")
            return original_rpc(self, fn, params)

        p1a._Database.rpc = failing_rpc
        try:
            with pytest.raises(planning.PlanningError):
                planning.update_task(task_id, {"is_active": False}, at(24, 8))
        finally:
            p1a._Database.rpc = original_rpc
        assert c.db.rows["planning_task"][0]["is_active"] is True
        assert start["status"] == "pending" and end["status"] == "pending"


def _register_recompute(c, reason, requested_at):
    """经由 fake RPC 登记重算请求（数据库原子生成唯一消费身份 token）。"""
    c.db.rpc("planning_request_recompute", {
        "p_reason": reason,
        "p_requested_at": planning._iso(requested_at),
    }).execute()
    return c.db.rows["planning_recompute_state"][0]["request_token"]


# ── 批次 6 收尾（BUG A）：一次 recompute 只消费开始时看到的那一版请求 ──

def test_maintenance_recompute_keeps_new_request_written_during_execution():
    # 时间线：12:00 请求 A → maintenance 开始执行 A → 12:59 并发写入新
    # 请求 B → A 正常完成 → B 必须保留给下一轮，不得被 A 的清除吞掉。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        state_row = c.db.rows["planning_recompute_state"][0]
        _register_recompute(c, "reorder", at(24, 12))

        original = planning_recompute.recompute_today

        def recompute_then_new_request(now=None):
            result = original(now)
            # 执行中途：用户拖动排序产生新请求 B
            _register_recompute(c, "reorder_mid", at(24, 12, 59))
            return result

        with mock.patch.object(planning_recompute, "recompute_today", recompute_then_new_request):
            results = planning.run_maintenance(at(24, 13))
        assert results["auto_recompute"]["conflicts"] == []
        # A 正常完成，但 B（12:59）不被 A 清除
        assert state_row["requested_at"] == planning._iso(at(24, 12, 59))
        assert state_row["reason"] == "reorder_mid"
        # 下一轮 maintenance 消费 B：等待期满（31 分钟 ≥ 30）正常执行并清除
        with mock.patch.object(planning_recompute, "recompute_today", original):
            planning.run_maintenance(at(24, 13, 30))
        assert state_row["requested_at"] is None


def test_manual_recompute_keeps_new_request_written_during_execution():
    # 手动重算同语义：开始时捕获的身份之外的并发新请求保留。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        state_row = c.db.rows["planning_recompute_state"][0]
        _register_recompute(c, "status_change", at(24, 12))

        original = planning_recompute.recompute_today

        def recompute_then_new_request(now=None):
            result = original(now)
            _register_recompute(c, "reorder", at(24, 12, 59))
            return result

        with mock.patch.object(planning_recompute, "recompute_today", recompute_then_new_request):
            result = planning.trigger_recompute(at(24, 13))
        assert result["conflicts"] == []
        assert state_row["requested_at"] == planning._iso(at(24, 12, 59))
        with mock.patch.object(planning_recompute, "recompute_today", original):
            planning.run_maintenance(at(24, 13, 30))
        assert state_row["requested_at"] is None


def test_recompute_identity_unique_per_registration_even_with_same_business_now():
    # A1 核心（HIGH）：requested_at 来自业务 now，两次业务操作可能捕获同一
    # 时间戳 T——消费身份必须与时间戳无关地唯一。时间线：
    # 1) A、B 两次操作使用同一业务时间 T；2) A 登记请求；3) old recompute
    # 捕获 A 的身份；4) B 以同一 T 登记请求；5) B 的消费身份 ≠ A；
    # 6) old recompute 完成只清 A（数据库当前为 B → 0 行）→ B 保留；
    # 7) 下一轮消费 B；8) 无新请求时正常清除。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        state_row = c.db.rows["planning_recompute_state"][0]
        # A、B 捕获同一业务时间戳 T
        token_a = _register_recompute(c, "reorder", at(24, 12))
        # old recompute 开始时捕获
        captured = planning.get_recompute_state(at(24, 12))["request_token"]
        assert captured == token_a
        # B 以同一 T 登记（requested_at 等值身份无法区分的形态）
        token_b = _register_recompute(c, "status_change", at(24, 12))
        assert token_b != token_a, "同一业务时间戳必须产生不同的消费身份"
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        # old recompute 完成：按 A 的身份清除——B 保留
        planning.clear_recompute_mark(at(24, 12, 5), expected_request_token=captured)
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        assert state_row["reason"] == "status_change"
        assert state_row["request_token"] == token_b
        # 下一轮消费 B：捕获 B 的身份 → 清除成功
        captured_b = planning.get_recompute_state(at(24, 12, 6))["request_token"]
        assert captured_b == token_b
        planning.clear_recompute_mark(at(24, 12, 6), expected_request_token=captured_b)
        assert state_row["requested_at"] is None
        assert state_row["request_token"] is None


def test_recompute_identity_same_timestamp_interleave_manual_and_maintenance():
    # A1 端到端（手动 + maintenance 两路）：同时间戳交错的 B 不被旧消费
    # 吞掉；B 由下一轮真正消费后清除。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        state_row = c.db.rows["planning_recompute_state"][0]
        token_a = _register_recompute(c, "reorder", at(24, 12))
        original = planning_recompute.recompute_today

        def recompute_then_same_time_request(now=None):
            result = original(now)
            # B 与 A 捕获同一业务时间 12:00（同秒并发操作的复现形态）
            _register_recompute(c, "status_change", at(24, 12))
            return result

        with mock.patch.object(planning_recompute, "recompute_today", recompute_then_same_time_request):
            planning.trigger_recompute(at(24, 13))
        # B 保留（同 T 也不被 A 的清除吞掉）
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        assert state_row["request_token"] != token_a
        # 下一轮 maintenance 消费 B（等待期满）→ 清除
        with mock.patch.object(planning_recompute, "recompute_today", original):
            planning.run_maintenance(at(24, 13, 1))
        assert state_row["requested_at"] is None
        assert state_row["request_token"] is None


def test_recompute_without_concurrent_request_still_clears_mark():
    # 无并发新请求时，旧 request 仍可正常清除（手动 + maintenance 两路）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        state_row = c.db.rows["planning_recompute_state"][0]
        _register_recompute(c, "reorder", at(24, 12))
        planning.trigger_recompute(at(24, 13))
        assert state_row["requested_at"] is None
        _register_recompute(c, "status_change", at(24, 14))
        planning.run_maintenance(at(24, 14, 31))
        assert state_row["requested_at"] is None


# ── 批次 6 收尾（BUG B）：停用 / 废弃成功后必须重新请求排程 ──────────

def _record_recompute(c):
    """在 Context 的 request_recompute mock 之上套一层真实现记录器。"""
    calls = []

    def recording_request(reason, now=None):
        calls.append((reason, planning._iso(now) if now else None))
        _REAL_REQUEST_RECOMPUTE(reason, now)

    planning_recompute.request_recompute = recording_request
    return calls


def test_pure_deactivation_registers_recompute_request():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        task_id = c.db.rows["planning_task"][0]["id"]
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": task_id})
        calls = _record_recompute(c)
        planning.update_task(task_id, {"is_active": False}, at(24, 12))
        assert calls == [("task_discarded", planning._iso(at(24, 12)))]
        state_row = c.db.rows["planning_recompute_state"][0]
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        assert state_row["reason"] == "task_discarded"
        assert c.db.rows["planning_task"][0]["is_active"] is False
        assert occ["status"] == "discarded"


def test_whole_task_discard_registers_recompute_request():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": occ["task_id"]})
        calls = _record_recompute(c)
        planning.set_occurrence_status(occ["id"], {"status": "discarded"}, at(24, 12))
        assert calls == [("task_discarded", planning._iso(at(24, 12)))]
        state_row = c.db.rows["planning_recompute_state"][0]
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        assert state_row["reason"] == "task_discarded"
        assert c.db.rows["planning_task"][0]["is_active"] is False
        assert occ["status"] == "discarded"


def test_discard_merges_into_existing_recompute_request():
    # 已有待处理请求时沿用现有合并语义（同一单例行 upsert），
    # 不制造重复行、不报错。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        state_row = c.db.rows["planning_recompute_state"][0]
        state_row["requested_at"] = planning._iso(at(24, 9))
        state_row["reason"] = "reorder"
        _record_recompute(c)
        planning.set_occurrence_status(occ["id"], {"status": "discarded"}, at(24, 12))
        assert len(c.db.rows["planning_recompute_state"]) == 1
        assert state_row["requested_at"] == planning._iso(at(24, 12))
        assert state_row["reason"] == "task_discarded"


def test_recompute_after_discard_uses_released_slot():
    # A 08:30–09:30、B 09:30–10:30、C 10:30–11:30；废弃 B 后下一轮重算
    # 以当前时间 09:00 为游标重排：B 的槽位消失，C 由 10:30 提前到 10:00
    #（用户无需手动重算即看到释放的时间槽被利用）。
    with Context() as c:
        for _ in range(3):
            c.create("daily", at(24, 8), estimated_minutes=60)
        rows = list(c.rows)
        planning_recompute.recompute_today(at(24, 8, 30))
        assert [r["est_start"] for r in rows] == [
            iso(24, 8, 30), iso(24, 9, 30), iso(24, 10, 30)]
        _record_recompute(c)
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": rows[1]["task_id"]})
        planning.set_occurrence_status(rows[1]["id"], {"status": "discarded"}, at(24, 9))
        assert rows[1]["status"] == "discarded"
        planning.trigger_recompute(at(24, 9))
        assert rows[0]["est_start"] == iso(24, 9)          # A 按新游标重排
        assert rows[2]["est_start"] == iso(24, 10)         # C 提前 30 分钟
        assert rows[2]["est_end"] == iso(24, 11)
        assert c.db.rows["planning_recompute_state"][0]["requested_at"] is None


def test_discard_succeeds_when_recompute_enqueue_fails(caplog):
    # 不得谎报：停用 / 废弃已在 RPC 事务内成功，登记失败（注入）不得让
    # API 报「停用失败」；但也不得静默——warning 日志保留可观测性。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        task_id = c.db.rows["planning_task"][0]["id"]
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": task_id})

        def failing_request(reason, now=None):
            raise RuntimeError("simulated enqueue failure")

        planning_recompute.request_recompute = failing_request
        with caplog.at_level("WARNING", logger="gateway.planning"):
            result = planning.update_task(task_id, {"is_active": False}, at(24, 12))
        # §25（2026-10-07）：删除响应表达结果（历史已保留），不依赖任务行。
        assert result["deleted"] is True
        assert result["history_preserved"] is True
        assert c.db.rows["planning_task"][0]["is_active"] is False
        assert occ["status"] == "discarded"
        assert any("重算请求登记失败" in record.message for record in caplog.records)
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ2 = c.rows[-1]
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": occ2["task_id"]})
        planning.set_occurrence_status(occ2["id"], {"status": "discarded"}, at(24, 13))
        assert occ2["status"] == "discarded"
        assert c.db.rows["planning_task"][1]["is_active"] is False
