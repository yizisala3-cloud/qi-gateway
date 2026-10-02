"""Phase 1R 日常路径收尾（拆分 B1/B2 + 高可达小修）回归测试。

拆分正式语义（2026-09-26 user 确认）：
- 拆分是可选辅助功能，不是 partial 的必经步骤（partial → 已全部完成才是
  主流程）；
- 拆分 = 结束当前这一轮 + 创建 1～10 个新的单次待办（once）；
- 拆分属于「本轮已经处理结束」，对周期推进与「此次不执行」同级：当前轮
  以 discarded_this + handled_at（拆分处理时间）合法关闭，after_completion
  从该处理时间推进下一轮；fixed refresh 固定时间轴不变；
- 已有 partial_note / partial_at / actual_* 等用户事实原样保留，不得为
  记录"已拆分为 N 个待办"而覆盖；
- 只允许开放实例（pending/in_progress/partial/deferred）拆分；已关闭实例
  （含已被并发拆分收口的）拒绝再次拆分，重复请求不产生第二组任务。
"""

from datetime import timedelta
from unittest import mock

import pytest

from gateway import planning
from test_planning_phase1b import Context, at


def _once_tasks(c):
    return [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]


# ── B1：after_completion 拆分后不再永久停滞 ──────────────────────────


def test_b1_after_completion_split_advances_next_round():
    # 复现基线：after_completion 当前轮拆分 → 旧实现写 discarded 且无
    # handled_at → _after_completion_due 找不到合法基准 → 永不刷新。
    # 正式语义：拆分 = 本轮处理结束（discarded_this + handled_at），
    # after_completion 从拆分处理时间推进下一轮。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        round_row = c.rows[0]
        planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        closed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert closed["status"] == "discarded_this"
        assert closed["handled_at"] == at(24, 10).isoformat()
        assert closed["closed_at"] == at(24, 10).isoformat()
        # 处理基准成立：task 行立即推进，且下一轮按拆分处理时间计算
        task_row = c.db.rows["planning_task"][0]
        assert task_row["last_handled_at"] == at(24, 10).isoformat()
        assert task_row["refresh_next_due_at"] == at(27, 10).isoformat()
        # interval 到期后生成下一轮（不再永久停滞）
        assert planning.generate_due(at(27, 10))["created"] == 1
        next_round = c.rows[-1]
        assert next_round["id"] != round_row["id"]
        assert next_round["status"] == "pending"
        # 重复维护不重复生成
        assert planning.generate_due(at(27, 10))["created"] == 0


def test_b1_after_completion_split_baseline_self_heals_from_rows():
    # 即使 task 行基准字段写入失败（模拟故障），_after_completion_due 也能
    # 从轮次行的 handled_at 恢复推进——拆分收口语义本身即充分。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        round_row = c.rows[0]
        planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        task_row = c.db.rows["planning_task"][0]
        task_row["last_handled_at"] = None
        task_row["refresh_next_due_at"] = None
        assert planning.generate_due(at(27, 10, 30))["created"] == 1


def test_b1_split_on_open_round_preserves_partial_facts():
    # partial → split：partial_note / partial_at / actual 事实不被覆盖；
    # 不写入"已拆分为 N 个待办"之类内容。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        round_row = c.rows[0]
        planning.set_occurrence_status(
            round_row["id"],
            {"status": "partial", "partial_note": "衣柜已经整理完成"},
            at(24, 9),
        )
        planning.start_occurrence(round_row["id"], at(24, 9, 30))
        planning.split_occurrence(
            round_row["id"],
            {"parts": [{"content": "整理书桌"}, {"content": "拖地"}]},
            at(24, 10),
        )
        closed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert closed["partial_note"] == "衣柜已经整理完成"
        assert closed["partial_at"] == at(24, 9).isoformat()
        assert closed["actual_start"] == at(24, 9, 30).isoformat()
        assert "已拆分" not in (closed["partial_note"] or "")
        task_row = c.db.rows["planning_task"][0]
        assert task_row["refresh_next_due_at"] == at(27, 10).isoformat()


# ── fixed refresh：拆分不改变固定时间轴 ──────────────────────────────


def test_split_fixed_refresh_keeps_axis():
    with Context() as c:
        c.create("interval", at(24, 7), refresh_mode="fixed_interval", interval_days=3)
        round_row = c.rows[0]
        planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        closed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert closed["status"] == "discarded_this"
        assert closed["handled_at"] == at(24, 10).isoformat()
        # 固定时间轴不动：下一轮仍按原到期事件生成
        task_row = c.db.rows["planning_task"][0]
        assert task_row.get("refresh_next_due_at") is None  # 固定型无处理后基准
        assert planning.generate_due(at(27, 7))["created"] == 1
        assert c.rows[-1]["round_key"].startswith("fixed:2026-09-27:")
        assert c.rows[-1]["schedule_date"] == "2026-09-27"


def test_split_daily_round_next_day_still_generates():
    # 每日任务拆分当前轮：当日轮关闭，次日轮正常生成（不复制、不漏）。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        round_row = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-24")
        planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        assert planning.generate_due(at(25, 6))["created"] == 1
        assert c.rows[-1]["round_key"] == "cycle:2026-09-25"


# ── 拆分数量与状态守卫 ───────────────────────────────────────────────


def test_split_single_part_is_legal():
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        result = planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理剩余杂物"}]}, at(24, 10))
        assert len(result["created_task_ids"]) == 1
        once = _once_tasks(c)
        assert len(once) == 1
        assert once[0]["content"] == "整理剩余杂物"
        assert once[0]["task_type"] == "once"
        assert once[0]["refresh_mode"] == "none"
        closed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert closed["status"] == "discarded_this"


def test_split_ten_parts_is_legal():
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        parts = [{"content": f"部分{i}"} for i in range(1, 11)]
        result = planning.split_occurrence(round_row["id"], {"parts": parts}, at(24, 10))
        assert len(result["created_task_ids"]) == 10
        assert len(_once_tasks(c)) == 10


def test_split_zero_and_eleven_parts_rejected():
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        for bad in ([], [{"content": f"p{i}"} for i in range(11)]):
            try:
                planning.split_occurrence(round_row["id"], {"parts": bad}, at(24, 10))
            except planning.PlanningError as error:
                assert error.status_code == 400
            else:
                raise AssertionError("invalid parts count must be rejected")
        # 非法输入不产生任何任务、不关闭原轮
        assert len(_once_tasks(c)) == 0
        assert round_row["status"] == "pending"


def test_split_closed_instance_rejected_no_second_group():
    # 已关闭实例（completed / discarded_this / discarded / timeout）不能
    # 再次拆分；重复请求不产生第二组任务（B2 业务兜底）。
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        first = planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        count_after_first = len(_once_tasks(c))
        for now in (at(24, 10, 30), at(24, 11)):
            try:
                planning.split_occurrence(
                    round_row["id"], {"parts": [{"content": "再拆"}]}, now)
            except planning.PlanningError as error:
                assert error.status_code == 422
            else:
                raise AssertionError("closed occurrence must not be split again")
        assert len(_once_tasks(c)) == count_after_first == 1
        assert first["created_task_ids"]


def test_split_timeout_instance_rejected():
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        round_row["status"] = "timeout"
        round_row["closed_at"] = at(24, 8).isoformat()
        try:
            planning.split_occurrence(
                round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        except planning.PlanningError as error:
            assert error.status_code == 422
        else:
            raise AssertionError("timeout occurrence must not be split")
        assert len(_once_tasks(c)) == 0


def test_split_concurrent_second_request_wins_nothing():
    # 双击/并发：第二个请求在任何阶段进入，条件收口（状态在 UPDATE 条件内）
    # 未命中 → 409，不产生第二组拆分任务。
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        real_fetch = planning._fetch_occurrence
        state = {"first_done": False}

        def interleaved_fetch(client, occurrence_id):
            row = real_fetch(client, occurrence_id)
            if not state["first_done"]:
                state["first_done"] = True
                # 第一个请求在此期间完整执行（收口 + 建任务）
                planning.split_occurrence(
                    occurrence_id, {"parts": [{"content": "整理书桌"}]}, at(24, 10))
            return row  # 第二个请求基于过期读取继续

        with mock.patch.object(planning, "_fetch_occurrence", side_effect=interleaved_fetch):
            try:
                planning.split_occurrence(
                    round_row["id"], {"parts": [{"content": "再拆"}]}, at(24, 10, 30))
            except planning.PlanningError as error:
                assert error.status_code in (409, 422)
            else:
                raise AssertionError("concurrent second split must not succeed")
        assert len(_once_tasks(c)) == 1
        closed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert closed["status"] == "discarded_this"


def test_split_hollow_round_closes_both_phases():
    # 中空轮拆分：同一轮的两个阶段必须一起原子收口（不得留下半关闭轮次），
    # handled_at 一致，任务不被卡死。
    with Context() as c:
        c.create("daily", at(23), is_hollow=True, hollow_start_content="泡豆",
                 hollow_start_minutes=10, hollow_wait_minutes=30,
                 hollow_end_minutes=5, hollow_end_content="煮饭")
        planning.generate_due(at(24, 6))
        start_row = next(row for row in c.rows if row["phase"] == "start"
                         and row["round_key"] == "cycle:2026-09-24")
        planning.split_occurrence(
            start_row["id"], {"parts": [{"content": "整理厨房"}]}, at(24, 10))
        phases = [row for row in c.rows
                  if row["phase"] in ("start", "end")
                  and row["round_key"] == "cycle:2026-09-24"]
        assert len(phases) == 2
        assert all(row["status"] == "discarded_this" for row in phases)
        assert all(row["handled_at"] == at(24, 10).isoformat() for row in phases)


def test_split_creates_tasks_on_current_cycle_and_recompute_mark():
    with Context() as c:
        c.create("daily", at(23))
        round_row = c.rows[0]
        result = planning.split_occurrence(
            round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        once = _once_tasks(c)[0]
        assert once["target_date"] == "2026-09-24"
        assert len(result["created_task_ids"]) == 1
        # 拆出的单次待办立即生成实例（不等后台循环）
        assert any(row["task_id"] == once["id"] for row in c.rows)

# ── #1（2026-10-02）：拆分单事务原子化 ────────────────────────────────


def test_split_rpc_failure_leaves_no_partial_state():
    # #1：关闭原轮、创建任务与基准推进在同一事务内（迁移 20261002030000
    # RPC）——任一失败整体回滚：原轮保持开放、零任务创建，失败如实上报
    # 503，重试可完整重放（此前关闭先落库、中途 INSERT 失败留下「原轮已
    # 关闭 + 部分任务」的不可重试半状态）。
    with Context() as c:
        c.create("daily", at(24, 7), estimated_minutes=30)
        round_row = c.rows[0]
        original_rpc = c.db.rpc

        def failing_rpc(name, params=None):
            if name == "planning_split_occurrence":
                raise RuntimeError("simulated mid-transaction failure")
            return original_rpc(name, params)

        with mock.patch.object(c.db, "rpc", side_effect=failing_rpc):
            with pytest.raises(planning.PlanningError) as error:
                planning.split_occurrence(
                    round_row["id"],
                    {"parts": [{"content": "整理书桌"}, {"content": "拖地"}]},
                    at(24, 10))
        assert error.value.status_code == 503
        # 零净写入：原轮仍开放、无任何 once 任务、无 handled_at 事实。
        refreshed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert refreshed["status"] == "pending"
        assert refreshed.get("handled_at") is None
        assert refreshed.get("closed_at") is None
        assert _once_tasks(c) == []
        # 重试（故障恢复后）完整成功。
        result = planning.split_occurrence(
            round_row["id"],
            {"parts": [{"content": "整理书桌"}, {"content": "拖地"}]},
            at(24, 11))
        assert len(result["created_task_ids"]) == 2
        refreshed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert refreshed["status"] == "discarded_this"
        assert refreshed["handled_at"] == at(24, 11).isoformat()
        assert len(_once_tasks(c)) == 2


def test_split_after_completion_rpc_failure_keeps_baseline_untouched():
    # 基准推进也在同一事务内：RPC 失败时 task 行基准字段不得被提前推进。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        round_row = c.rows[0]
        original_rpc = c.db.rpc

        def failing_rpc(name, params=None):
            if name == "planning_split_occurrence":
                raise RuntimeError("simulated mid-transaction failure")
            return original_rpc(name, params)

        with mock.patch.object(c.db, "rpc", side_effect=failing_rpc):
            with pytest.raises(planning.PlanningError):
                planning.split_occurrence(
                    round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        task_row = c.db.rows["planning_task"][0]
        assert task_row.get("last_handled_at") is None
        assert task_row.get("refresh_next_due_at") is None


# ── #27（2026-10-02）：主事务提交成功后的登记失败不误报 ────────────────


def test_split_recompute_registration_failure_keeps_success_result():
    # 复审 R3：拆分 RPC 已完整提交（原轮收口 + once 任务 + 基准推进）后，
    # request_recompute 登记失败属于 post-commit side effect——不得向上传播
    # 把完整成功误报为 500，也不得短路即时生成；created_task_ids 如实返回。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        round_row = c.rows[0]
        with mock.patch.object(planning, "request_recompute",
                               side_effect=RuntimeError("registration unavailable")):
            result = planning.split_occurrence(
                round_row["id"], {"parts": [{"content": "整理书桌"}]}, at(24, 10))
        assert result["created_task_ids"]
        closed = next(row for row in c.rows if row["id"] == round_row["id"])
        assert closed["status"] == "discarded_this"
        assert closed["handled_at"] == at(24, 10).isoformat()
        once = _once_tasks(c)
        assert len(once) == 1
        # 登记失败不阻断即时生成：拆出的单次待办实例已生成（不等后台循环）。
        assert any(row["task_id"] == once[0]["id"] for row in c.rows)
        task_row = c.db.rows["planning_task"][0]
        assert task_row["last_handled_at"] == at(24, 10).isoformat()
        assert task_row["refresh_next_due_at"] == at(27, 10).isoformat()
