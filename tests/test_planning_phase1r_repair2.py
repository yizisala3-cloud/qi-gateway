"""Phase 1R 第四轮收口修复（N1–N6）回归测试。

- N1/N2/N3：重排「请求身份 + 请求内容 + 请求结果」完整模型
- N4：额外完成判重统一依据 early_period_date
- N5：显式区间优先的有效耗时单一语义
- N6：固定刷新 early 周期身份非空（数据库侧见 pgserver 套件）
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from gateway import planning
from test_planning_phase1b import Context, at


CST = timezone(timedelta(hours=8))


def _timeout_occ(c, task_id=1):
    occ = c.rows[0]
    occ["status"] = "timeout"
    return occ


def test_n1_recovery_restores_user_time_after_background_recompute():
    # N1：18:00 重排 → task 成功、occurrence 首次失败 → 后台 generate_due
    # 补出实例 + recompute 排成自动时间 → 原 key 重试 → 最终仍 18:00 manual。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        real_create = planning._create_occurrences
        calls = {"n": 0}

        def fail_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("gen down")
            return real_create(*args, **kwargs)

        with mock.patch.object(planning, "_create_occurrences", side_effect=fail_first):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError as error:
                assert error.status_code == 503
        # 后台维护：补出实例并自动排程（覆盖为非用户时刻）
        planning.generate_due(at(25, 15, 30))
        planning.recompute_today(at(25, 15, 31))
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        once_task_id = once_tasks[0]["id"] if once_tasks else None
        orphan = next(row for row in c.rows if row["task_id"] == once_task_id)
        assert orphan["estimated_time_source"] == "automatic"
        # 原 key 重试：按持久化请求内容恢复 18:00 manual
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15, 32),
            idempotency_key="k1",
        )
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert once_tasks[0]["request_est_start"] == at(25, 18).isoformat()
        assert result["occurrence"]["est_start"] == at(25, 18).isoformat()
        assert result["occurrence"]["estimated_time_source"] == "manual"
        assert result["occurrence"]["fixed_source"] == "manual"


def test_n2_same_key_with_different_time_is_rejected():
    # N2：16:00 成功（响应可能丢失）→ 用户改 17:00 用原 key 再提交 →
    # 明确拒绝参数冲突，不静默返回 16:00。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        first = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        assert first["occurrence"]["est_start"] == at(25, 16).isoformat()
        try:
            planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 17).isoformat()}, at(25, 15, 30),
                idempotency_key="k1",
            )
        except planning.PlanningError as error:
            assert error.status_code == 409
            assert error.code == "request_conflict"
        else:
            raise AssertionError("same key with different time must be rejected")
        # 原请求结果不受影响
        assert first["occurrence"]["est_start"] == at(25, 16).isoformat()


def test_n2_conflict_rejected_even_before_occurrence_exists():
    # N2/N1：同 key 不同参数的冲突语义与失败阶段无关——task 已建、实例
    # 尚未生成时同样拒绝。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError as error:
                assert error.status_code == 503
        # task 已落库（带请求内容），实例未生成
        once = next(row for row in c.db.rows["planning_task"] if row["task_type"] == "once")
        assert once["request_est_start"] == at(25, 16).isoformat()
        assert not any(row["task_id"] == once["id"] for row in c.rows)
        try:
            planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 17).isoformat()}, at(25, 15, 30),
                idempotency_key="k1",
            )
        except planning.PlanningError as error:
            assert error.status_code == 409
            assert error.code == "request_conflict"
        else:
            raise AssertionError("conflict must be rejected regardless of stage")


def test_n3_zero_creation_converges_to_existing_success():
    # N3：同 key 并发，另一方已建实例（本方 _create_occurrences 返回 0）→
    # 不报 503，重新读取并核对人工锚点，两边收敛到同一成功结果。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)

        real_create = planning._create_occurrences
        calls = {"n": 0}

        def zero_then_real(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0  # 模拟并发对方已建实例，本方插入命中轮次唯一
            return real_create(*args, **kwargs)

        with mock.patch.object(planning, "_create_occurrences", side_effect=zero_then_real):
            result = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
                idempotency_key="k1",
            )
        # 对方尚未真正建实例（本测试中 0 为空表象）→ converge 自行补建，
        # 最终同一任务同一实例、用户时刻 manual
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert result["occurrence"] is not None
        assert result["occurrence"]["est_start"] == at(25, 16).isoformat()
        assert result["occurrence"]["fixed_source"] == "manual"


def test_n3_concurrent_both_sides_converge_to_one_result():
    # N3：请求 A 停在 task 插入后；请求 B（同 key 同参数）恢复并建实例；
    # A 继续时 create 返回 0 → A 也收敛到同一成功结果，全程无 503。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        state = {"task_inserted": False, "b_done": False}

        real_insert = planning._create_occurrences

        def b_recovers_then_a_zero(*args, **kwargs):
            # 第一次调用 = 请求 A 建实例前，先让请求 B 完整跑一遍（恢复路径）
            if not state["task_inserted"]:
                state["task_inserted"] = True
                with mock.patch.object(planning, "_create_occurrences", side_effect=real_insert):
                    planning.reschedule_timeout_as_new(
                        occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15, 10),
                        idempotency_key="k1",
                    )
                state["b_done"] = True
                return 0  # A 的插入命中轮次唯一 → 0
            return real_insert(*args, **kwargs)

        with mock.patch.object(planning, "_create_occurrences", side_effect=b_recovers_then_a_zero):
            result_a = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
                idempotency_key="k1",
            )
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert result_a["occurrence"] is not None
        assert result_a["occurrence"]["est_start"] == at(25, 16).isoformat()
        assert result_a["occurrence"]["fixed_source"] == "manual"


def test_n4_rule_edit_new_period_allows_new_extra():
    # N4：weekly extra 完成于周五 08:00（closed_at 落在新周期窗口内）；
    # 周五 09:00 改规则为周五并生成新轮 → 新周期的 extra 不被旧记录挡住；
    # 旧记录的 early_period_date / closed_at 均不被重解释。
    with Context() as c:
        c.create("weekly", at(24, 7), weekdays=[3], refresh_mode="fixed_weekday")
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 8))
        e1 = planning.complete_task_early(1, at(25, 8), idempotency_key="e1")
        assert e1["schedule_date"] == "2026-09-25"  # 额外完成于周五 08:00
        # 周五 09:00 改规则为周五并生成 9/25 正常轮；先正常完成该轮，再做
        # 新周期的额外完成
        planning.update_task(1, {"weekdays": [4]}, at(25, 9))
        planning.generate_due(at(25, 9, 30))
        for row in c.rows:
            if row["schedule_date"] == "2026-09-25" and row["status"] in planning.OPEN_STATUSES:
                planning.set_occurrence_status(row["id"], {"status": "completed"}, at(25, 9, 45))
        e2 = planning.complete_task_early(1, at(25, 10), idempotency_key="e2")
        # 新周期的 extra：判重依据 early_period_date，而非 closed_at
        assert e2["id"] != e1["id"]
        rows = {row["id"]: row for row in c.rows if row.get("source") == "early"}
        assert rows[e1["id"]]["early_period_date"] == "2026-09-24"
        assert rows[e2["id"]]["early_period_date"] == "2026-09-25"
        assert rows[e1["id"]]["closed_at"] == at(25, 8).isoformat()  # 旧事实不变


def test_n4_monthly_and_interval_share_period_identity_semantics():
    # N4：monthly 与 fixed interval 的同期判重同样依据 early_period_date；
    # 修改规则只影响未来周期。
    with Context() as c:
        c.create("monthly", at(24, 7), month_days=[25], refresh_mode="fixed_monthday")
        planning.generate_due(at(25, 7))
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(25, 8))
        e1 = planning.complete_task_early(1, at(25, 9), idempotency_key="m1")
        rows = {row["id"]: row for row in c.rows if row.get("source") == "early"}
        old_row = rows[e1["id"]]
        assert old_row["early_period_date"] == "2026-09-25"
        # 改为每月 26 日：9/26 生成新轮；先正常完成该轮，再做新周期额外完成
        planning.update_task(1, {"month_days": [26]}, at(26, 7))
        planning.generate_due(at(26, 8))
        for row in c.rows:
            if row["schedule_date"] == "2026-09-26" and row["status"] in planning.OPEN_STATUSES:
                planning.set_occurrence_status(row["id"], {"status": "completed"}, at(26, 9))
        e2 = planning.complete_task_early(1, at(27, 9), idempotency_key="m2")
        rows = {row["id"]: row for row in c.rows if row.get("source") == "early"}
        assert rows[e2["id"]]["early_period_date"] == "2026-09-26"
        assert rows[e1["id"]]["early_period_date"] == "2026-09-25"  # 旧记录不变

    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        e1 = planning.complete_task_early(1, at(24, 8), idempotency_key="i1")
        rows = {row["id"]: row for row in c.rows if row.get("source") == "early"}
        assert rows[e1["id"]]["early_period_date"] == "2026-09-24"
        # 间隔改 1 天：9/25、9/26 依次生成新轮；各轮正常完成后，再做新周期
        # 额外完成（9/26 轮在 9/26 到点生成）
        planning.update_task(1, {"interval_days": 1}, at(25, 7))
        planning.generate_due(at(25, 7, 30))
        for row in c.rows:
            if row["schedule_date"] == "2026-09-25" and row["status"] in planning.OPEN_STATUSES:
                planning.set_occurrence_status(row["id"], {"status": "completed"}, at(25, 8))
        planning.generate_due(at(26, 8, 30))
        for row in c.rows:
            if row["schedule_date"] == "2026-09-26" and row["status"] in planning.OPEN_STATUSES:
                planning.set_occurrence_status(row["id"], {"status": "completed"}, at(26, 8, 45))
        e2 = planning.complete_task_early(1, at(26, 9), idempotency_key="i2")
        rows = {row["id"]: row for row in c.rows if row.get("source") == "early"}
        # 9/26 09:00 已处于 9/26 槽位周期（9/26 轮 08:45 完成），额外完成
        # 属于该周期
        assert rows[e2["id"]]["early_period_date"] == "2026-09-26"
        assert rows[e1["id"]]["early_period_date"] == "2026-09-24"
        assert len(rows) == 2


def test_n5_explicit_interval_wins_in_api_and_after_manual_move():
    # N5：显式 08:00–09:00 + 30 分钟 → 有效耗时 60（API/展示与排程同源）；
    # 人工移动开始后仍按有效区间语义推导（10:00 → 11:00）。
    with Context() as c:
        c.create("daily", at(23), time_mode="explicit", est_start_tod="08:00",
                 est_end_tod="09:00", estimated_minutes=30)
        occ = c.rows[0]
        task_row = c.db.rows["planning_task"][0]
        serialized = planning.serialize_occurrence(occ, task_row, at(23, 8))
        assert serialized["estimated_minutes"] == 60
        # 人工移动开始时间：保持有效区间语义
        planning.patch_occurrence(occ["id"], {"est_start": at(23, 10).isoformat()}, at(23, 9, 30))
        assert occ["est_end"] == at(23, 11).isoformat()
        serialized = planning.serialize_occurrence(occ, task_row, at(23, 10))
        assert serialized["estimated_minutes"] == 60
        # 排程同源：排程耗时来源按 60 分钟（显式区间优先于快照）
        assert planning._duration_of(occ, task_row) == timedelta(minutes=60)
