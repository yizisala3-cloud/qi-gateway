"""批次 6 一轮 Review 修复矩阵测试（2026-09-28 裁决 4/5/6 + BLOCKER 2/3 + MEDIUM）。

覆盖：生命周期门控矩阵（in_progress / partial / terminal 拒绝，deferred
未开始合法）、中空整轮判断、写前完整校验零写入、中空两阶段单语句原子写、
单字段任务 PATCH 合并模板中文错误。
"""

import pytest

from gateway import planning
from tests.support.planning_context import Context, at
from tests.support.planning_fixtures import HOLLOW, _fields, iso


def test_gate_matrix_rejects_started_partial_terminal_states():
    # 矩阵 16：只有尚未开始且开放的实例可编辑——in_progress / partial /
    # completed / timeout 一律拒绝；deferred（真正未开始）合法（矩阵 15）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
        before = (occ["window_start_at"], occ["window_end_at"], occ["est_start"])
        for payload in (
            # 零自由度形状（门控必须先于钉住——一轮 Review HIGH，矩阵 8）
            {"window_start_at": iso(24, 18), "window_end_at": iso(24, 19)},
            {"window_start_at": iso(24, 18), "window_end_at": iso(24, 22)},
        ):
            with pytest.raises(planning.PlanningError) as error:
                planning.patch_occurrence(occ["id"], payload, at(24, 11))
            assert error.value.status_code == 422, payload
            assert "尚未开始" in str(error.value)
        # 执行中的 est 不被 zero-slack 瞬移
        assert (occ["window_start_at"], occ["window_end_at"], occ["est_start"]) == before
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "先做了一半"}, at(24, 10, 30))
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(occ["id"], {"window_start_at": iso(24, 18)}, at(24, 11))
        assert "尚未开始" in str(error.value)


def test_gate_allows_truly_not_started_deferred():
    # 矩阵 15：deferred 且真正未开始（无 actual_start）→ 合法编辑。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "deferred", "est_start": iso(26, 9)}, at(24, 10, 30))
        assert not occ.get("actual_start")
        planning.patch_occurrence(
            occ["id"], {"window_start_at": iso(26, 8), "window_end_at": iso(26, 12)},
            at(24, 11))
        assert (occ["window_start_at"], occ["window_end_at"]) == (iso(26, 8), iso(26, 12))


def test_gate_blocks_started_deferred():
    # 已开始的 deferred（有 actual_start）不可编辑。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(occ["id"], {"status": "in_progress"}, at(24, 10, 30))
        planning.set_occurrence_status(
            occ["id"], {"status": "deferred", "est_start": iso(25, 9)}, at(24, 11))
        assert occ.get("actual_start")
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(25, 18)}, at(24, 11, 30))
        assert "事实" in str(error.value)


def test_hollow_whole_round_gate_blocks_mixed_state_edit():
    # BLOCKER 2 / 矩阵 5：中空按整轮判断——start 已 completed、end 仍
    # pending 时，从 end 发起窗口编辑 → 整轮拒绝，completed start 的历史
    # est / 窗口零变化。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        planning.recompute_today(at(24, 7, 30))
        planning.set_occurrence_status(start["id"], {"status": "completed"}, at(24, 8))
        start_before = _fields(start)
        end_before = _fields(end)
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                end["id"],
                {"window_start_at": iso(24, 18), "window_end_at": iso(24, 20)}, at(24, 9))
        assert error.value.status_code == 422
        assert "尚未开始" in str(error.value)
        assert _fields(start) == start_before
        assert _fields(end) == end_before


def test_hollow_whole_round_gate_blocks_in_progress_start():
    # 同轮任一阶段 in_progress → 整轮拒绝（end 仍 pending 也不能改）。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        planning.recompute_today(at(24, 7, 30))
        planning.set_occurrence_status(start["id"], {"status": "in_progress"}, at(24, 7, 45))
        before_s, before_e = _fields(start), _fields(end)
        with pytest.raises(planning.PlanningError):
            planning.patch_occurrence(end["id"], {"window_end_at": iso(24, 21)}, at(24, 8))
        assert _fields(start) == before_s and _fields(end) == before_e
        assert start["status"] == "in_progress" and end["status"] == "pending"


def test_hollow_late_payload_validation_zero_write():
    # BLOCKER 3 / 矩阵 6：完整 payload 的后段校验失败（超长 partial_note /
    # 实际时间倒挂）→ 整体拒绝且同轮两行零写入（validation-before-write）。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        before_start, before_end = _fields(start), _fields(end)
        with pytest.raises(planning.PlanningError):
            planning.patch_occurrence(
                end["id"],
                {"window_start_at": iso(24, 18), "window_end_at": iso(24, 20),
                 "partial_note": "x" * 2000}, at(24, 9))
        assert _fields(start) == before_start and _fields(end) == before_end
        with pytest.raises(planning.PlanningError):
            planning.patch_occurrence(
                end["id"],
                {"window_start_at": iso(24, 18), "window_end_at": iso(24, 20),
                 "actual_start": iso(24, 9), "actual_end": iso(24, 8)}, at(24, 9))
        assert _fields(start) == before_start and _fields(end) == before_end


def test_hollow_multi_row_db_failure_atomic_rollback():
    # BLOCKER 3 / 矩阵 7：中空两阶段写入 = 单次 RPC（函数级事务）。RPC
    # 失败（故障注入）时两行都保持修改前状态——fake 层验证行为，真库
    # 回滚证据由 pgserver 套件提供（fake 不承担 atomicity 权威）。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        before_start, before_end = _fields(start), _fields(end)
        import test_planning_phase1a as p1a
        original_rpc = p1a._Database.rpc

        def failing_rpc(self, fn, params=None):
            if fn == "planning_patch_occurrence_round":
                raise RuntimeError("simulated rpc failure")
            return original_rpc(self, fn, params)

        p1a._Database.rpc = failing_rpc
        try:
            with pytest.raises(RuntimeError):
                planning.patch_occurrence(
                    start["id"],
                    {"window_start_at": iso(24, 18), "window_end_at": iso(24, 20)},
                    at(24, 9))
        finally:
            p1a._Database.rpc = original_rpc
        assert _fields(start) == before_start
        assert _fields(end) == before_end


def test_hollow_pin_restored_via_round_rpc():
    # user 批准 RPC 后恢复正式行为：零自由度钉住（两行不同 est 值）经
    # planning_patch_occurrence_round 原子完成——两行各自消费自己的补丁，
    # 不再有暂停期 409，也不存在顺序两行写。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        planning.recompute_today(at(24, 7, 30))
        seen = []
        import test_planning_phase1a as p1a
        original_execute = p1a._RpcCall.execute

        def recording_execute(self):
            if self.fn == "planning_patch_occurrence_round":
                seen.append(dict(self.params))
            return original_execute(self)

        p1a._RpcCall.execute = recording_execute
        try:
            planning.patch_occurrence(
                start["id"],
                {"window_start_at": iso(24, 18), "window_end_at": iso(24, 20)}, at(24, 9))
        finally:
            p1a._RpcCall.execute = original_execute
        # PostgREST / Supabase rpc() 调用形态：函数名 + p_* 命名参数
        assert len(seen) == 1
        params = seen[0]
        assert set(params) == {
            "p_target_id", "p_sibling_id", "p_target_patch", "p_sibling_patch",
            "p_expected"}
        assert params["p_target_id"] == start["id"]
        assert params["p_sibling_id"] == end["id"]
        # 两行各自补丁：钉住的 est 不同（开始锚窗口起点、结束收口窗口终点）
        assert params["p_target_patch"]["est_start"] == iso(24, 18)
        assert params["p_sibling_patch"]["est_start"] == iso(24, 19, 30)
        assert params["p_sibling_patch"]["est_end"] == iso(24, 20)
        # 兄弟行补丁不含说明 / 实际时间 / 生命周期字段（写集合不扩大）
        assert not ({"partial_note", "actual_start", "actual_end", "status",
                     "handled_at", "closed_at"} & set(params["p_sibling_patch"]))
        for row in (start, end):
            assert (row["window_start_at"], row["window_end_at"]) == (iso(24, 18), iso(24, 20))
            assert (row["estimated_time_source"], row["fixed_source"], row["is_fixed"]) == (
                "manual", "manual", True)
        assert (start["est_start"], start["est_end"]) == (iso(24, 18), iso(24, 18, 30))
        assert (end["est_start"], end["est_end"]) == (iso(24, 19, 30), iso(24, 20))
        planning.recompute_today(at(24, 9, 30))
        assert (start["est_start"], end["est_start"]) == (iso(24, 18), iso(24, 19, 30))


def test_hollow_plain_window_write_via_round_rpc():
    # 同值窗口联动同样统一走 RPC（十一：不存在同轮两行顺序写路径）；
    # spy 断言恰好一次 rpc、p_* 形态、两行生效、绝不调用 upsert。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        calls = []
        import test_planning_phase1a as p1a
        original_rpc = p1a._Database.rpc
        original_execute = p1a._Query.execute
        original_rpc_execute = p1a._RpcCall.execute

        def recording_rpc(self, fn, params=None):
            calls.append((fn, dict(params or {})))
            return original_rpc(self, fn, params)

        def recording_query_execute(self):
            if self.name == "planning_occurrence" and self.action == "update":
                calls.append(("update", dict(self.filters)))
            return original_execute(self)

        def recording_rpc_execute(self):
            if self.fn == "planning_patch_occurrence_round":
                calls.append((self.fn, dict(self.params)))
            return original_rpc_execute(self)

        p1a._Database.rpc = recording_rpc
        p1a._Query.execute = recording_query_execute
        p1a._RpcCall.execute = recording_rpc_execute
        try:
            planning.patch_occurrence(
                start["id"],
                {"window_start_at": iso(24, 12), "window_end_at": iso(24, 16)}, at(24, 9))
        finally:
            p1a._Database.rpc = original_rpc
            p1a._Query.execute = original_execute
            p1a._RpcCall.execute = original_rpc_execute
        assert all(fn != "upsert" for fn, _ in calls), calls
        assert any(fn == "planning_patch_occurrence_round" for fn, _ in calls), calls
        assert (start["window_start_at"], start["window_end_at"]) == (iso(24, 12), iso(24, 16))
        assert (end["window_start_at"], end["window_end_at"]) == (iso(24, 12), iso(24, 16))


def test_task_single_field_patch_merged_template_chinese_error():
    # MEDIUM / 矩阵 17：原 09:00–12:00 仅 PATCH window_start_tod=12:00 →
    # 合并后 12:00–12:00 必须返回项目中文 400，不泄漏原始 ValueError，
    # task 零写入。
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="09:00", window_end_tod="12:00")
        before = dict(c.db.rows["planning_task"][0])
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(task["id"], {"window_start_tod": "12:00"}, at(24, 11))
        assert error.value.status_code == 400
        assert "不能相同" in str(error.value)
        assert c.db.rows["planning_task"][0] == before
        # 对照：合并后合法的单字段修改照常
        planning.update_task(task["id"], {"window_start_tod": "10:00"}, at(24, 11, 30))
        assert c.db.rows["planning_task"][0]["window_start_tod"] == "10:00"


def test_gate_dirty_fact_matrix_zero_write():
    # 十四：完整事实集合——pending + actual_end / partial_at / actual_start、
    # partial 回 pending 保留 partial_at，一律拒绝且零写入。
    scenarios = (
        ("actual_end", {"actual_end": iso(24, 9)}),
        ("partial_at", {"partial_at": iso(24, 9), "partial_note": "做了一半"}),
        ("actual_start", {"actual_start": iso(24, 9)}),
    )
    for name, facts in scenarios:
        with Context() as c:
            c.create("daily", at(24, 10), estimated_minutes=30)
            occ = c.rows[0]
            for key, value in facts.items():
                occ[key] = value
            before = (occ["window_start_at"], occ["window_end_at"])
            with pytest.raises(planning.PlanningError) as error:
                planning.patch_occurrence(
                    occ["id"], {"window_start_at": iso(24, 18)}, at(24, 11))
            assert "事实" in str(error.value), name
            assert (occ["window_start_at"], occ["window_end_at"]) == before, name
    with Context() as c:
        # partial 后状态回 pending，partial_at 事实保留 → 拒绝
        c.create("daily", at(24, 10), estimated_minutes=30)
        occ = c.rows[0]
        planning.set_occurrence_status(
            occ["id"], {"status": "partial", "partial_note": "半"}, at(24, 10, 30))
        assert occ["partial_at"] is not None
        planning.set_occurrence_status(occ["id"], {"status": "pending"}, at(24, 10, 45))
        assert occ["status"] == "pending" and occ["partial_at"] is not None
        with pytest.raises(planning.PlanningError) as error:
            planning.patch_occurrence(
                occ["id"], {"window_start_at": iso(24, 18)}, at(24, 11))
        assert "事实" in str(error.value)
        assert occ["window_start_at"] is None


def test_hollow_mixed_request_sibling_write_set_not_expanded():
    # 二轮 HIGH / 指令六七：混合请求（窗口 + 说明）中说明仅授权目标行——
    # RPC 的 sibling 补丁不得为对齐形状而并入 partial_note / actual_*；
    # sibling 行这些字段保持完全不变。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30, **HOLLOW)
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        planning.recompute_today(at(24, 7, 30))
        end["partial_note"] = "既有说明"
        seen = []
        import test_planning_phase1a as p1a
        original_execute = p1a._RpcCall.execute

        def recording_execute(self):
            if self.fn == "planning_patch_occurrence_round":
                seen.append(dict(self.params))
            return original_execute(self)

        p1a._RpcCall.execute = recording_execute
        try:
            planning.patch_occurrence(
                start["id"],
                {"window_start_at": iso(24, 12), "window_end_at": iso(24, 16),
                 "partial_note": "仅目标行"}, at(24, 8))
        finally:
            p1a._RpcCall.execute = original_execute
        assert len(seen) == 1
        sibling_patch = seen[0]["p_sibling_patch"]
        assert "partial_note" not in sibling_patch
        assert "actual_start" not in sibling_patch and "actual_end" not in sibling_patch
        assert end["partial_note"] == "既有说明"
        assert (end["window_start_at"], end["window_end_at"]) == (iso(24, 12), iso(24, 16))
        assert start["partial_note"] == "仅目标行"
