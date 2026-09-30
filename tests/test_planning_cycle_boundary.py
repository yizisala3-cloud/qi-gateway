"""批次 7 定向测试：boundary 修改与关联待办窗口调整原子生效（§5.2.2）。

覆盖施工计划 B7 矩阵：干跑只读 / 冲突清单 / 端点接触合法 / 开区间跨越
非法 / 停用任务不参与 / 暂停刷新不豁免 / once 与已生成实例零改写 /
最终保存再校验 / 调整 + boundary 原子成功 / 失败全回滚 / 多 worker CAS。
真库（RPC 事务回滚 / 重放）权威证明见 pgserver 套件。
"""

import contextlib
from unittest import mock

import pytest

from gateway import planning
from test_planning_phase1b import Context, at

BOUNDARY_KEY = planning.PLANNING_BOUNDARY_STATE_KEY


def _task_by_id(c, task_id):
    return next(row for row in c.db.rows["planning_task"] if row["id"] == task_id)


def _state(c):
    return c.settings.get(BOUNDARY_KEY)


def _spy_rpc(c):
    """记录 fake RPC 调用序列（用于零写入证明）。"""
    calls = []
    original_rpc = c.db.rpc

    def spy(fn, params=None):
        calls.append(fn)
        return original_rpc(fn, params)

    c.db.rpc = spy
    return calls, original_rpc


# ── dry-run（§5.2.2：绝对零写入）────────────────────────────────────

def test_dry_run_lists_conflicts_and_writes_nothing():
    with Context() as c:
        cross = c.create("daily", at(24, 10), estimated_minutes=30,
                         window_start_tod="22:00", window_end_tod="05:00")
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="09:00", window_end_tod="12:00")
        calls, original_rpc = _spy_rpc(c)
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True}, at(24, 15))
        c.db.rpc = original_rpc
        assert result["dry_run"] is True
        assert result["boundary_time"] == "04:00"
        assert [item["task_id"] for item in result["conflicts"]] == [cross["id"]]
        conflict = result["conflicts"][0]
        assert conflict["content"] == "daily"
        assert conflict["window_start_tod"] == "22:00"
        assert conflict["window_end_tod"] == "05:00"
        assert "04:00" in conflict["reason"] and "跨越" in conflict["reason"]
        # 零写入：没有任何 RPC、状态行与任务行原样
        assert calls == []
        assert _state(c) is None
        assert _task_by_id(c, cross["id"])["window_start_tod"] == "22:00"


def test_dry_run_without_conflicts_returns_empty_list():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="09:00", window_end_tod="12:00")
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True}, at(24, 15))
        assert result["conflicts"] == []
        assert _state(c) is None  # dry-run 绝不落任何状态


# ── 保存：当前周期保持旧 boundary，下一周期使用新 boundary（§5.2.1）──

def test_boundary_save_current_cycle_keeps_old_next_cycle_uses_new():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00"}, at(24, 15))
        # 当前周期（9/24）保持原边界继续；过渡记录等待 9/25 04:00 生效
        assert result["cycle_key"] == "2026-09-24"
        assert result["pending_boundary"]["previous_time"] == "06:00"
        assert result["pending_boundary"]["effective_at"].endswith("T04:00:00+08:00")
        assert _state(c)["boundary"] == "04:00"
        # 生效前：仍处于旧 boundary 的跨越周期
        assert planning.get_cycle_settings(at(25, 3))["cycle_key"] == "2026-09-24"
        # 生效后：新周期按新 boundary 划分（9/25 04:00 起）
        assert planning.get_cycle_settings(at(25, 5))["cycle_key"] == "2026-09-25"


# ── 端点接触合法 / 开区间跨越非法（§6.7 顺时针开区间）───────────────

def test_boundary_touching_window_endpoints_is_legal():
    with Context() as c:
        # 起点恰等新 boundary（04:00–05:00 在旧 boundary 06:00 下合法）
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="04:00", window_end_tod="05:00")
        # 终点恰等新 boundary（跨自然午夜写法，终点在次日 04:00）
        c.create("daily", at(24, 11), estimated_minutes=30,
                 window_start_tod="22:00", window_end_tod="04:00")
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True}, at(24, 15))
        assert result["conflicts"] == []


def test_boundary_inside_window_open_interval_is_illegal():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="10:00", window_end_tod="14:00")
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "12:00", "dry_run": True}, at(24, 15))
        assert len(result["conflicts"]) == 1
        # 跨自然午夜：04:00 ∈ (22:00 → 06:00) 顺时针开区间
        c.create("daily", at(24, 11), estimated_minutes=30,
                 window_start_tod="22:00", window_end_tod="06:00")
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True}, at(24, 15))
        # 10:00–14:00 不跨越 04:00；仅跨午夜任务冲突
        assert [item["window_start_tod"] for item in result["conflicts"]] == ["22:00"]


def test_final_save_conflict_returns_409_with_structured_list():
    with Context() as c:
        cross = c.create("daily", at(24, 10), estimated_minutes=30,
                         window_start_tod="10:00", window_end_tod="14:00")
        with pytest.raises(planning.PlanningError) as error:
            planning.set_cycle_settings({"refresh_boundary_time": "12:00"}, at(24, 15))
        assert error.value.status_code == 409
        assert error.value.code == "boundary_window_conflicts"
        conflicts = error.value.details["conflicts"]
        assert [item["task_id"] for item in conflicts] == [cross["id"]]
        assert conflicts[0]["window_start_tod"] == "10:00"
        assert _state(c) is None  # 冲突整体拒绝：零写入


# ── 参与范围：停用不参与；暂停刷新不豁免（B7-6/7）───────────────────

def test_disabled_task_is_excluded_from_boundary_validation():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="22:00", window_end_tod="05:00")
        c.db.rows["planning_task"][0]["is_active"] = False
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True}, at(24, 15))
        assert result["conflicts"] == []
        # 停用任务的窗口模板不被 boundary 修改触碰
        assert _task_by_id(c, task["id"])["window_start_tod"] == "22:00"


def test_paused_refresh_task_is_not_exempt_from_boundary_validation():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="22:00", window_end_tod="05:00",
                 refresh_enabled=False)
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True}, at(24, 15))
        assert len(result["conflicts"]) == 1


# ── 已生成实例与 once 身份零改写（B7-8/9 / B5 冻结）─────────────────

def test_boundary_change_never_touches_once_identity_or_occurrences():
    with Context() as c:
        c.create("once", at(24, 10), target_date="2026-09-25",
                 window_start_tod="18:00", window_end_tod="22:00")
        daily = c.create("daily", at(24, 10), estimated_minutes=30)
        planning.recompute_today(at(24, 10, 30))
        task_before = [dict(row) for row in c.db.rows["planning_task"]]
        occ_before = [dict(row) for row in c.db.rows["planning_occurrence"]]
        planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(24, 15))
        # 任务定义（含 once 的 target_date / 窗口模板）逐字段不变
        assert c.db.rows["planning_task"] == task_before
        # 已生成 occurrence（冻结窗口 / est / 身份）逐字段不变
        assert c.db.rows["planning_occurrence"] == occ_before
        assert _task_by_id(c, daily["id"]).get("target_date") is None


# ── 调整 + boundary 原子成功 / 失败全回滚（§5.2.2 / 不变量 39）──────

def test_boundary_save_with_task_adjustment_succeeds_atomically():
    with Context() as c:
        cross = c.create("daily", at(24, 10), estimated_minutes=30,
                         window_start_tod="22:00", window_end_tod="05:00")
        once = c.create("once", at(24, 10), target_date="2026-09-25",
                        window_start_tod="18:00", window_end_tod="22:00")
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00",
             "task_adjustments": [
                 {"task_id": cross["id"],
                  "window_start_tod": "04:00", "window_end_tod": "09:00"},
             ]},
            at(24, 15))
        # 同一事务：boundary 状态与关联任务窗口一起生效
        assert _state(c)["boundary"] == "04:00"
        assert result["adjusted_tasks"] == 1
        row = _task_by_id(c, cross["id"])
        assert row["window_start_tod"] == "04:00"
        assert row["window_end_tod"] == "09:00"
        # 未调整任务（含 once 的 target_date）不被本次保存移日或改写
        assert _task_by_id(c, once["id"])["target_date"] == "2026-09-25"
        assert _task_by_id(c, once["id"])["window_start_tod"] == "18:00"
        # 新 boundary 下调整后的模板合法（起点接触合法）
        assert planning.get_cycle_settings(at(25, 5))["cycle_key"] == "2026-09-25"


def test_adjustment_to_single_side_is_allowed():
    with Context() as c:
        cross = c.create("daily", at(24, 10), estimated_minutes=30,
                         window_start_tod="22:00", window_end_tod="05:00")
        # 调整为只有最早开始（单侧约束不构成区间，不参与跨越校验）
        planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00",
             "task_adjustments": [
                 {"task_id": cross["id"],
                  "window_start_tod": "05:00", "window_end_tod": None},
             ]},
            at(24, 15))
        row = _task_by_id(c, cross["id"])
        assert row["window_start_tod"] == "05:00"
        assert row["window_end_tod"] is None


def test_adjustment_cannot_leave_other_conflicts_unresolved():
    with Context() as c:
        cross_a = c.create("daily", at(24, 10), estimated_minutes=30,
                           window_start_tod="22:00", window_end_tod="05:00")
        cross_b = c.create("daily", at(24, 11), estimated_minutes=30,
                           window_start_tod="01:00", window_end_tod="05:00")
        with pytest.raises(planning.PlanningError) as error:
            planning.set_cycle_settings(
                {"refresh_boundary_time": "04:00",
                 "task_adjustments": [
                     {"task_id": cross_a["id"],
                      "window_start_tod": "04:00", "window_end_tod": "09:00"},
                 ]},
                at(24, 15))
        assert error.value.status_code == 409
        # 未调整的冲突者被最终校验列出；全部零写入
        assert [item["task_id"] for item in error.value.details["conflicts"]] == [
            cross_b["id"]]
        assert _state(c) is None
        assert _task_by_id(c, cross_a["id"])["window_start_tod"] == "22:00"
        assert _task_by_id(c, cross_b["id"])["window_start_tod"] == "01:00"


def test_invalid_adjustment_shape_is_rejected_with_zero_writes():
    with Context() as c:
        task = c.create("daily", at(24, 10), estimated_minutes=30,
                        window_start_tod="22:00", window_end_tod="05:00")
        cases = [
            # start == end 无效
            {"refresh_boundary_time": "04:00", "task_adjustments": [
                {"task_id": task["id"], "window_start_tod": "05:00",
                 "window_end_tod": "05:00"}]},
            # 未知字段
            {"refresh_boundary_time": "04:00", "task_adjustments": [
                {"task_id": task["id"], "window_start_tod": "05:00",
                 "window_end_tod": "06:00", "content": "hack"}]},
            # 重复待办
            {"refresh_boundary_time": "04:00", "task_adjustments": [
                {"task_id": task["id"], "window_start_tod": "05:00",
                 "window_end_tod": "06:00"},
                {"task_id": task["id"], "window_start_tod": "05:00",
                 "window_end_tod": "06:00"}]},
            # 非法时间格式
            {"refresh_boundary_time": "04:00", "task_adjustments": [
                {"task_id": task["id"], "window_start_tod": "abc",
                 "window_end_tod": None}]},
            # 缺端
            {"refresh_boundary_time": "04:00", "task_adjustments": [
                {"task_id": task["id"], "window_start_tod": "05:00"}]},
            # dry_run 非布尔
            {"refresh_boundary_time": "04:00", "dry_run": "yes"},
            # 无 boundary 时的试算 / 调整
            {"dry_run": True},
            {"task_adjustments": []},
        ]
        for bad in cases:
            with pytest.raises(planning.PlanningError) as error:
                planning.set_cycle_settings(bad, at(24, 15))
            assert error.value.status_code == 400
        assert _state(c) is None
        assert _task_by_id(c, task["id"])["window_start_tod"] == "22:00"


# ── 最终保存再校验：modal 期间他人新增的跨越任务被拒绝（B4）─────────

def test_final_save_revalidates_tasks_modified_after_dry_run():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        assert planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00", "dry_run": True},
            at(24, 15))["conflicts"] == []
        # modal 打开期间：另一 worker 创建了跨越新 boundary 的任务
        #（01:00–05:00 在旧 boundary 06:00 下合法）
        c.create("daily", at(24, 16), estimated_minutes=30,
                 window_start_tod="01:00", window_end_tod="05:00")
        with pytest.raises(planning.PlanningError) as error:
            planning.set_cycle_settings({"refresh_boundary_time": "04:00"}, at(24, 17))
        assert error.value.status_code == 409
        assert error.value.details["conflicts"][0]["window_start_tod"] == "01:00"
        assert _state(c) is None


# ── 多 worker：状态 CAS（B4 / 施工计划并发场景 3）───────────────────

def test_concurrent_boundary_modifications_rejected_by_state_cas():
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30)
        state_first = {"boundary": "06:00", "transition": None, "absorbed": []}
        state_after_other_worker = {
            "boundary": "05:00",
            "transition": {"spanning_key": "2026-09-24",
                           "spanning_boundary": "06:00",
                           "change_at": at(24, 14).isoformat()},
            "absorbed": [],
        }
        # _save_cycle_boundary 第一次读取 state_first 预计算；RPC 执行时
        # 状态已是另一 worker 落库后的 state_after_other_worker → CAS 未命中。
        c.settings[BOUNDARY_KEY] = state_first
        loads = {"count": 0}
        original_load = planning.db.load_app_setting

        def shifting_load(key):
            if key != BOUNDARY_KEY:
                return None
            loads["count"] += 1
            if loads["count"] > 1:
                # 另一 worker 的提交此刻落库
                c.settings[BOUNDARY_KEY] = state_after_other_worker
            return c.settings.get(key)

        planning.db.load_app_setting = shifting_load
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.set_cycle_settings(
                    {"refresh_boundary_time": "04:00"}, at(24, 15))
        finally:
            planning.db.load_app_setting = original_load
        assert error.value.status_code == 409
        assert error.value.code == "boundary_state_conflict"
        # 另一 worker 的落库状态原样保留（零部分提交）
        assert c.settings[BOUNDARY_KEY]["boundary"] == "05:00"


# ── RPC / 状态保存失败 → 全部回滚（B4 / 不变量 39）──────────────────

def test_boundary_save_failure_rolls_back_task_adjustments():
    with Context() as c:
        cross = c.create("daily", at(24, 10), estimated_minutes=30,
                         window_start_tod="22:00", window_end_tod="05:00")
        original_save = planning.db.save_app_setting

        def failing_save(key, value):
            return False  # 状态行写入失败（半途 DB failure 注入）

        planning.db.save_app_setting = failing_save
        try:
            with pytest.raises(planning.PlanningError) as error:
                planning.set_cycle_settings(
                    {"refresh_boundary_time": "04:00",
                     "task_adjustments": [
                         {"task_id": cross["id"],
                          "window_start_tod": "04:00", "window_end_tod": "09:00"},
                     ]},
                    at(24, 15))
        finally:
            planning.db.save_app_setting = original_save
        assert error.value.status_code == 503
        # 全回滚：任务窗口模板保持原值，状态未写入
        assert _task_by_id(c, cross["id"])["window_start_tod"] == "22:00"
        assert _task_by_id(c, cross["id"])["window_end_tod"] == "05:00"
        assert _state(c) is None


# ── 其余设置键保持原路径（B6）───────────────────────────────────────

def test_other_cycle_settings_keep_single_key_path():
    with Context() as c:
        planning.set_cycle_settings({"daily_refresh_enabled": False}, at(24, 15))
        planning.set_cycle_settings({"auto_recompute_enabled": False}, at(24, 15))
        planning.set_cycle_settings({"auto_recompute_wait_minutes": 45}, at(24, 15))
        settings_view = planning.get_cycle_settings(at(24, 15))
        assert settings_view["daily_refresh_enabled"] is False
        assert settings_view["auto_recompute_enabled"] is False
        assert settings_view["auto_recompute_wait_minutes"] == 45
        assert _state(c) is None  # 走各自设置键，不触碰 boundary 状态行
        with pytest.raises(planning.PlanningError):
            planning.set_cycle_settings(
                {"daily_refresh_enabled": False, "auto_recompute_enabled": True},
                at(24, 15))


# ── RPC 内重新全量校验是最终拒绝的权威（绕过 Python 预检直接驱动）────

def test_boundary_rpc_revalidation_finds_conflicts_without_python_precheck():
    # Python 预检与 RPC 校验是同一规则的两道独立防线；本测试绕过预检
    # 直接调用保存 RPC（模拟预检后他人写入冲突模板的窗口）——RPC 内的
    # 全量校验必须独立发现冲突并零写入。
    with Context() as c:
        cross = c.create("daily", at(24, 10), estimated_minutes=30,
                         window_start_tod="22:00", window_end_tod="05:00")
        data = c.db.rpc("planning_update_cycle_boundary", {
            "p_new_boundary": "04:00",
            "p_expected_state": {"boundary": "06:00", "transition": None},
            "p_transition": None,
            "p_absorbed": [],
            "p_adjustments": [],
        }).execute().data
        assert data["status"] == "conflicts"
        assert data["conflicts"][0]["task_id"] == cross["id"]
        assert _state(c) is None


def test_disabled_crossing_task_does_not_block_final_save():
    # 停用任务不参与校验：其窗口跨越新 boundary 也不阻塞最终保存
    #（Python 预检排除；RPC 校验同样排除——若 RPC 误纳入将 409）。
    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="22:00", window_end_tod="05:00",
                 is_active=False)
        result = planning.set_cycle_settings(
            {"refresh_boundary_time": "04:00"}, at(24, 15))
        assert _state(c)["boundary"] == "04:00"
        assert result["adjusted_tasks"] == 0


# ── API 形状：409 + details.conflicts / dry-run 200（B6）────────────

def test_cycle_api_returns_409_with_conflict_details():
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    from gateway.config import cfg
    from gateway.planning_api import planning_api_routes

    with Context() as c:
        c.create("daily", at(24, 10), estimated_minutes=30,
                 window_start_tod="10:00", window_end_tod="14:00")
        tokens = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", "b7-test"),
            mock.patch.object(planning, "get_client", return_value=c.db),
        ]

        @contextlib.contextmanager
        def patched():
            for token in tokens:
                token.start()
            try:
                yield
            finally:
                for token in reversed(tokens):
                    token.stop()

        with patched():
            client = TestClient(Starlette(routes=list(planning_api_routes)))
            headers = {"Authorization": "Bearer b7-test"}
            response = client.patch(
                "/admin/api/planning/cycle",
                json={"refresh_boundary_time": "12:00"},
                headers=headers,
            )
            assert response.status_code == 409
            body = response.json()
            assert body["error_code"] == "boundary_window_conflicts"
            assert body["details"]["conflicts"][0]["content"] == "daily"
            # dry-run 走同一端点：200 + dry_run 形状
            response = client.patch(
                "/admin/api/planning/cycle",
                json={"refresh_boundary_time": "12:00", "dry_run": True},
                headers=headers,
            )
            assert response.status_code == 200
            assert response.json()["dry_run"] is True
            assert len(response.json()["conflicts"]) == 1
