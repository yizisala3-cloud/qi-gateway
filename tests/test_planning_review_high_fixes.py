"""批次 9 Review 修复定向测试（4 HIGH + 2 UI 的 Python / fake-DB 部分）。

* HIGH #1：真实 PostgREST time 列形状 ``HH:MM:SS`` 经前端编辑表单原样
  回传——后端必须按同值接受（分钟精度契约，秒恒 0）；非法形状仍拒绝。
* HIGH #4（Python 预检）：inactive 任务重新启用时按「重新启用时的当前
  正式 boundary」重新验证模板窗口，非法拒绝、零写入；修正窗口后启用
  成功且 generation 正常（冻结窗口不跨 boundary）。
* UI #1（后端支撑）：任务列表携带 ``has_generated_occurrence``。
* 真库（RPC 事务 / 并发守卫 / 触发器）权威证明见
  tests/test_planning_boundary_guard_pgserver.py。
"""

import pytest
from datetime import datetime

from gateway import planning
from tests.support.planning_context import Context, at


# ── HIGH #1：HH:MM:SS 往返（真实 PostgREST time 列形状） ────────────

def _with_real_db_time_shape(c, task_id):
    """模拟真实 PostgREST round-trip：time 列序列化为 HH:MM:SS。

    返回该行当前值——前端编辑表单的 payload 来自任务列表接口（数据库
    原值），未触碰端会原样回传带秒形状。
    """
    row = next(r for r in c.db.rows["planning_task"] if r["id"] == task_id)
    if row.get("window_start_tod"):
        row["window_start_tod"] += ":00"
    if row.get("window_end_tod"):
        row["window_end_tod"] += ":00"
    return row


def _frontend_edit_body(task, **overrides):
    """Batch 8 openTaskForm 编辑模式的真实 payload 形状（editing 分支）。

    ``task`` 传 :func:`_with_real_db_time_shape` 的返回值（数据库当前行），
    模拟前端拿列表接口的原值回填表单再原样提交。
    """
    body = {
        "content": task["content"],
        "task_type": task.get("task_type", "daily"),
        "estimated_minutes": str(task.get("estimated_minutes", 30)),
        # editing 模式显式发送双端（未触碰端为数据库原值，可能带秒）
        "window_start_tod": task.get("window_start_tod"),
        "window_end_tod": task.get("window_end_tod"),
        "is_active": True,
    }
    body.update(overrides)
    return body


def test_high1_daily_edit_with_untouched_seconds_ends_saves():
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
        shape = _with_real_db_time_shape(c, t["id"])
        # 只改非窗口字段：未触碰端原样回传 HH:MM:SS——必须保存成功
        result = planning.update_task(
            t["id"], _frontend_edit_body(shape, content="改名"), at(24, 15))
        assert result["content"] == "改名"
        assert (result["window_start_tod"], result["window_end_tod"]) == ("09:00", "12:00")


def test_high1_move_one_end_while_other_carries_seconds():
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
        shape = _with_real_db_time_shape(c, t["id"])
        # 30min 任务：09:00–12:00 → 10:00–14:00（end 未触碰，HH:MM:SS）
        result = planning.update_task(
            t["id"],
            _frontend_edit_body(shape, window_start_tod="10:00", window_end_tod="14:00"),
            at(24, 15))
        assert (result["window_start_tod"], result["window_end_tod"]) == ("10:00", "14:00")


@pytest.mark.parametrize("start,end,expect", [
    ("10:00", "14:00", ("10:00", "14:00")),   # none → both
    ("09:00", None, ("09:00", None)),          # both → start-only（end 带秒清除）
    (None, "12:00", (None, "12:00")),          # both → end-only（start 带秒清除）
    (None, None, (None, None)),                # both → none（模板清除允许）
])
def test_high1_window_combinations_with_seconds_roundtrip(start, end, expect):
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
        shape = _with_real_db_time_shape(c, t["id"])
        result = planning.update_task(
            t["id"], _frontend_edit_body(shape, window_start_tod=start, window_end_tod=end),
            at(24, 15))
        assert (result["window_start_tod"], result["window_end_tod"]) == expect


@pytest.mark.parametrize("kind,kwargs", [
    ("interval", {"interval_days": 3, "refresh_mode": "after_completion"}),
    ("interval", {"interval_days": 3, "refresh_mode": "fixed_interval"}),
    ("weekly", {"weekdays": [0, 3]}),
    ("monthly", {"month_days": [1, 15]}),
])
def test_high1_recurrence_template_edits_accept_seconds_shape(kind, kwargs):
    with Context() as c:
        t = c.create(kind, at(24, 10), estimated_minutes=30, **kwargs)
        shape = _with_real_db_time_shape(c, t["id"])
        body = _frontend_edit_body(shape, window_start_tod="10:00", window_end_tod="14:00")
        if kind == "interval":
            body["interval_days"] = t["interval_days"]
            body["refresh_mode"] = t["refresh_mode"]
        elif kind == "weekly":
            body["weekdays"] = t["weekdays"]
        elif kind == "monthly":
            body["month_days"] = t["month_days"]
        result = planning.update_task(t["id"], body, at(24, 15))
        assert (result["window_start_tod"], result["window_end_tod"]) == ("10:00", "14:00")


def test_high1_non_window_fields_edit_together_with_seconds_window():
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="22:00", window_end_tod="05:00")
        shape = _with_real_db_time_shape(c, t["id"])
        result = planning.update_task(
            t["id"],
            _frontend_edit_body(shape, content="跨午夜任务", estimated_minutes="45"),
            at(24, 15))
        assert result["content"] == "跨午夜任务"
        assert result["estimated_minutes"] == 45
        assert (result["window_start_tod"], result["window_end_tod"]) == ("22:00", "05:00")


def test_high1_genuinely_invalid_time_still_rejected():
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
        shape = _with_real_db_time_shape(c, t["id"])
        for value in ("09:00:30", "25:00", "abc"):
            with pytest.raises(planning.PlanningError) as error:
                planning.update_task(
                    t["id"],
                    _frontend_edit_body(shape, window_start_tod=value),
                    at(24, 15))
            assert "时间格式无效" in str(error.value)


def test_high1_occurrence_freeze_unaffected_by_template_edit():
    # §28.1：模板编辑只影响未来；已生成实例冻结窗口零改写
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
        planning.generate_due(at(24, 11))
        occ = c.rows[0]
        frozen = (occ["window_start_at"], occ["window_end_at"])
        assert frozen[0] is not None
        shape = _with_real_db_time_shape(c, t["id"])
        planning.update_task(
            t["id"],
            _frontend_edit_body(shape, window_start_tod="10:00", window_end_tod="14:00"),
            at(24, 15))
        assert (occ["window_start_at"], occ["window_end_at"]) == frozen


# ── HIGH #4：重新启用按「重新启用时的 boundary」重校验 ───────────────

def _legacy_inactive(c, task_id):
    """模拟迁移前的旧停用行：is_active=false 且无 deleted_at（本批删除
    一律写删除标记且不可恢复；重新启用入口只对旧停用行保留）。"""
    row = next(r for r in c.db.rows["planning_task"] if r["id"] == task_id)
    row["deleted_at"] = None
    row["is_active"] = False


def test_high4_reenable_rejected_after_boundary_moved():
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="10:00", window_end_tod="14:00")
        # §25（2026-10-07）：播种完成事实 → 删除走历史保留分支（任务行
        # 保留，重新启用语义可测；无事实者物理删除）。
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": t["id"]})
        planning.update_task(t["id"], {"is_active": False}, at(24, 11))
        # §25（2026-10-07）：删除一律写 deleted_at 且不得恢复；「重新启用」
        # 入口只对迁移前的旧停用行（is_active=false 且无删除标记）保留——
        # 清除删除标记模拟旧停用行，保留 boundary 复验路径的覆盖。
        _legacy_inactive(c, t["id"])
        # boundary 06:00 → 12:00：inactive 任务不参与扫描，保存成功
        planning.set_cycle_settings({"refresh_boundary_time": "12:00"}, at(24, 12))
        # 重新启用：窗口 10:00–14:00 跨新 boundary → 拒绝
        with pytest.raises(planning.PlanningError) as error:
            planning.update_task(t["id"], {"is_active": True}, at(24, 13))
        assert "跨越" in str(error.value)
        task = next(r for r in c.db.rows["planning_task"] if r["id"] == t["id"])
        assert task["is_active"] is False
        # maintenance：inactive 任务不能生成新 occurrence（创建期旧轮已被
        # 停用废弃关闭，此后任何周期零新增、无开放行）
        before = len(c.rows)
        assert planning.generate_due(at(25, 6))["created"] == 0
        assert len(c.rows) == before
        assert all(r["status"] in ("discarded", "completed", "timeout") for r in c.rows)


def test_high4_window_fixed_then_reenable_succeeds_and_generation_resolves():
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="10:00", window_end_tod="14:00")
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": t["id"]})
        planning.update_task(t["id"], {"is_active": False}, at(24, 11))
        # §25（2026-10-07）：删除一律写 deleted_at 且不得恢复；「重新启用」
        # 入口只对迁移前的旧停用行（is_active=false 且无删除标记）保留——
        # 清除删除标记模拟旧停用行，保留 boundary 复验路径的覆盖。
        _legacy_inactive(c, t["id"])
        planning.set_cycle_settings({"refresh_boundary_time": "12:00"}, at(24, 12))
        # 修正窗口为 14:00–16:00 → 重新启用成功
        planning.update_task(
            t["id"], {"window_start_tod": "14:00", "window_end_tod": "16:00"},
            at(24, 13))
        result = planning.update_task(t["id"], {"is_active": True}, at(24, 14))
        assert result["is_active"] is True
        # generation 正常：进入新 boundary（12:00）后的下一个周期生成 9/25 轮
        # （旧 9/24 轮已被停用废弃关闭，轮次唯一键不重建历史轮）
        planning.generate_due(at(25, 13))
        fresh = [r for r in c.rows if r["round_key"] == "cycle:2026-09-25"]
        assert len(fresh) == 1
        occ = fresh[0]
        assert occ["status"] == "pending"
        # 冻结窗口 = 9/25 14:00–16:00：按新 boundary 解析、完整落在单一
        # 规划周期内（不跨 12:00），生成即冻结
        assert occ["window_start_at"] and occ["window_end_at"]
        start = datetime.fromisoformat(occ["window_start_at"])
        end = datetime.fromisoformat(occ["window_end_at"])
        assert start.day == 25 and end.day == 25
        assert (start.hour, start.minute) == (14, 0)
        assert (end.hour, end.minute) == (16, 0)


def test_high4_single_sided_reenable_needs_no_crossing_check():
    # 单侧约束不构成区间（§6.7）：re-enable 不做跨越校验，合法放行
    with Context() as c:
        t = c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="10:00", window_end_tod=None)
        c.db.rows.setdefault("planning_task_completion_fact", []).append(
            {"task_id": t["id"]})
        planning.update_task(t["id"], {"is_active": False}, at(24, 11))
        # §25（2026-10-07）：删除一律写 deleted_at 且不得恢复；「重新启用」
        # 入口只对迁移前的旧停用行（is_active=false 且无删除标记）保留——
        # 清除删除标记模拟旧停用行，保留 boundary 复验路径的覆盖。
        _legacy_inactive(c, t["id"])
        planning.set_cycle_settings({"refresh_boundary_time": "12:00"}, at(24, 12))
        result = planning.update_task(t["id"], {"is_active": True}, at(24, 13))
        assert result["is_active"] is True


# ── UI #1（后端支撑）：任务列表携带 has_generated_occurrence ─────────

def test_ui1_list_tasks_marks_generated_once():
    with Context() as c:
        fresh = c.create("once", at(24, 10), estimated_minutes=30, target_date="2026-09-25")
        done = c.create("once", at(24, 10), estimated_minutes=30, target_date="2026-09-24")
        planning.generate_due(at(24, 11))  # 9/24 once 立即生成
        tasks = {t["id"]: t for t in planning.list_tasks(now=at(24, 12))}
        assert tasks[fresh["id"]]["has_generated_occurrence"] is False
        assert tasks[done["id"]]["has_generated_occurrence"] is True
        daily = c.create("daily", at(24, 12), estimated_minutes=30)
        planning.generate_due(at(24, 13))
        tasks = {t["id"]: t for t in planning.list_tasks(now=at(24, 14))}
        # 非 once 任务同样携带字段（once 表单判定只消费 once 行，其它值无副作用）
        assert isinstance(tasks[daily["id"]]["has_generated_occurrence"], bool)


# ── 部署兼容守卫：postgrest 过滤链尾不得再接 .select(...) ────────────
# requirements 锁定的 supabase 2.15.1 的 postgrest builder 不支持
# in_→select 链序（AttributeError；生产 2026-10-01 smoke 复现，本地开发
# 环境 2.31.0 可链因此单测无法暴露）。列裁剪交给 _rows 的 select("*")。

def test_query_filter_chain_never_appends_select():
    import re
    from pathlib import Path
    gateway = Path(__file__).resolve().parents[1] / "gateway"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(gateway.glob("planning*.py"))
    )
    leaked = re.findall(r"\.in_\([^)]*\)\s*\.\s*select\(", source)
    assert leaked == [], f"postgrest in_→select 链序回归（supabase 2.15.1 不兼容）: {leaked}"
