"""显式结束时间单独清除（编辑表单清除字段收尾）回归测试。

场景（独立复审 Non-blocking #1）：编辑显式起止任务时保留开始、清空结束，
前端编辑模式显式发送 ``est_end_tod: null``（后端既有契约），旧结束时间
必须真正写库清除——不得因「不发送字段」而保存成功却残留旧值。
"""

from gateway import planning
from test_planning_phase1b import Context, at


def test_edit_keeps_start_clears_end_really_clears():
    # 设置结束时间（首次保存生效）→ 再编辑清空结束时间（保留开始，
    # 编辑模式显式发送 est_end_tod: null）→ 保存 → 重新读取：旧值清除。
    with Context() as c:
        planning.create_task({
            "content": "书面工作", "task_type": "daily",
            "time_mode": "explicit", "est_start_tod": "08:00",
            "est_end_tod": "09:00", "estimated_minutes": 45,
        }, at(24))
        task = c.db.rows["planning_task"][0]
        assert task["est_start_tod"] == "08:00"
        assert task["est_end_tod"] == "09:00"  # 首次保存生效
        # 再编辑：保留开始 08:00、清空结束（编辑模式显式发送 null）
        planning.update_task(task["id"], {
            "est_start_tod": "08:00", "est_end_tod": None,
        }, at(24, 7))
        refreshed = planning._fetch_task(c.db, task["id"])
        assert refreshed["est_start_tod"] == "08:00"  # 开始保留
        assert refreshed["est_end_tod"] is None       # 旧结束时间真正清除
        assert refreshed["time_mode"] == "explicit"   # 模式不变（有耗时兜底）


def test_cleared_end_does_not_linger_in_future_rounds():
    # 行为级证明：清除后未来新轮按 开始 + 有效耗时 生成结束时刻，
    # 旧结束时刻不再出现在新实例里；已生成实例不被任务编辑重解释。
    with Context() as c:
        planning.create_task({
            "content": "书面工作", "task_type": "daily",
            "time_mode": "explicit", "est_start_tod": "08:00",
            "est_end_tod": "09:00", "estimated_minutes": 45,
        }, at(23))
        planning.generate_due(at(24, 6))
        old = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-24")
        assert old["est_end"] == at(24, 9).isoformat()  # 首轮按旧结束 09:00 生成
        task = c.db.rows["planning_task"][0]
        planning.update_task(task["id"], {
            "est_start_tod": "08:00", "est_end_tod": None,
        }, at(24, 7))
        assert task["est_end_tod"] is None
        planning.generate_due(at(25, 6))
        fresh = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-25")
        # 新轮按开始 + 有效耗时（45 分钟）生成：旧 09:00 不残留
        assert fresh["est_start"] == at(25, 8).isoformat()
        assert fresh["est_end"] == at(25, 8, 45).isoformat()
        # 已生成实例不被任务编辑重解释（过去不重写）
        assert old["est_end"] == at(24, 9).isoformat()


def test_clear_end_without_minutes_fallback_gets_clear_400():
    # 无有效耗时兜底时：清空显式结束被后端以明确 400 拒绝（不写半区间，
    # 不静默成功）——前端此时给出字段级错误，用户需补耗时或改模式。
    with Context() as c:
        planning.create_task({
            "content": "无耗时兜底", "task_type": "daily",
            "time_mode": "explicit", "est_start_tod": "08:00",
            "est_end_tod": "09:00",
        }, at(24))
        task = c.db.rows["planning_task"][0]
        try:
            planning.update_task(task["id"], {
                "est_start_tod": "08:00", "est_end_tod": None,
            }, at(24, 7))
        except planning.PlanningError as error:
            assert error.status_code == 400
            assert "结束时间" in str(error) or "有效耗时" in str(error)
        else:
            raise AssertionError("explicit task without end or minutes must be rejected")
        refreshed = planning._fetch_task(c.db, task["id"])
        assert refreshed["est_end_tod"] == "09:00"  # 拒绝后旧值原样保留
