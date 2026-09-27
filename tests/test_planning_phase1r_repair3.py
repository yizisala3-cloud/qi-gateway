"""Phase 1R 第五轮收口修复（HIGH 1–3 / MEDIUM 4、6）回归测试。

- H1：迟到重试（所选时刻已过去）走恢复路径，不被新请求校验拦截。
- H2：已成功请求的重放只返回结果，不覆盖后续人工修改。
- H3：失败旧请求被新操作取代后，后台不再产生幽灵待办；已完成请求不受影响。
- M4：超时重排复制有效耗时（显式区间优先）。
- M6：after_completion 幂等重试补齐 task 行基准字段。
"""

from datetime import datetime, timedelta, timezone
from unittest import mock

from gateway import planning
from test_planning_phase1b import Context, at


CST = timezone(timedelta(hours=8))


def _timeout_occ(c):
    occ = c.rows[0]
    occ["status"] = "timeout"
    return occ


def test_h1_late_retry_recovers_persisted_request():
    # H1：18:00 请求失败（task+请求内容已持久化），18:38 才重试——
    # 恢复路径不受「新请求时间不能早于当前时间」拦截。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError as error:
                assert error.status_code == 503
        # 迟到重试（所选时刻已过去）
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 18, 38),
            idempotency_key="k1",
        )
        once = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once) == 1
        assert result["occurrence"]["est_start"] == at(25, 18).isoformat()
        assert result["occurrence"]["estimated_time_source"] == "manual"


def test_h1_late_retry_recovers_after_background_heal():
    # H1 变体：失败后后台已自愈生成实例（自动时间），迟到重试仍恢复为
    # 用户所选时刻 manual。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError:
                pass
        planning.generate_due(at(25, 18, 30))
        planning.recompute_today(at(25, 18, 31))
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 18, 38),
            idempotency_key="k1",
        )
        assert result["occurrence"]["est_start"] == at(25, 18).isoformat()
        assert result["occurrence"]["estimated_time_source"] == "manual"
        assert result["occurrence"]["fixed_source"] == "manual"


def test_h2_replay_returns_current_state_without_reanchor():
    # H2：成功请求重放只返回结果身份——立即重放 18:00 不变；用户改 19:00
    # 后重放返回 19:00（不写回 18:00）；完成后其他合法操作亦不被撤销。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        first = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        occ_id = first["occurrence"]["id"]
        # 立即重放：结果不变
        replay1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15, 30),
            idempotency_key="k1",
        )
        assert replay1["occurrence"]["est_start"] == at(25, 18).isoformat()
        # 用户人工改到 19:00
        planning.patch_occurrence(occ_id, {"est_start": at(25, 19).isoformat()}, at(25, 15, 45))
        replay2 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16),
            idempotency_key="k1",
        )
        assert replay2["occurrence"]["est_start"] == at(25, 19).isoformat()
        # 用户完成后重放：返回已完成状态，不复活
        planning.set_occurrence_status(occ_id, {"status": "completed"}, at(25, 19, 30))
        replay3 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 20),
            idempotency_key="k1",
        )
        assert replay3["occurrence"]["status"] == "completed"
        assert replay3["occurrence"]["est_start"] == at(25, 19).isoformat()


def test_h3_takeover_prevents_ghost_after_background_maintenance():
    # H3 场景 A（第七轮语义）：18:00 失败 → 改 19:00 接管成功 → 后台维护
    # → 只有一个业务待办（19:00 manual），无幽灵实例。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError:
                pass
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        # 后台维护：被吸收的旧请求不得再生成任何实例
        planning.generate_due(at(25, 16))
        planning.recompute_today(at(25, 16, 1))
        board = planning.today_board(at(25, 16, 2))["progress"]
        once_visible = [item for item in board if item["task_type"] == "once"]
        assert [(item["est_start"], item["estimated_time_source"]) for item in once_visible] == [
            (at(25, 19).isoformat(), "manual"),
        ]
        # 接管后的任务行：请求键为最新操作，旧键留档，终态 completed
        task_row = next(
            row for row in c.db.rows["planning_task"] if row["id"] == result["task"]["id"]
        )
        assert task_row["request_state"] == "completed"
        assert task_row["is_active"] is True
        assert task_row["request_key"] == "reschedule:1:k2"
        assert task_row["request_absorbed_keys"] == ["reschedule:1:k1"]
        once_occs = [row for row in c.rows if row["task_id"] == task_row["id"]]
        assert len(once_occs) == 1
        assert once_occs[0]["status"] == "pending"


def test_h3_completed_request_taken_over_by_new_operation():
    # H3 场景 B（第七轮语义）：已完成的 18:00 请求不被删除或丢弃；随后的
    # 19:00 新操作接管同一业务待办（同一实例移动到 19:00，仍开放）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        first = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        second = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        old_task = next(
            row for row in c.db.rows["planning_task"] if row["id"] == first["task"]["id"]
        )
        # 同一任务行被接管：请求身份更新为最新操作，终态 completed
        assert old_task["request_state"] == "completed"
        assert old_task["is_active"] is True
        assert old_task["request_key"] == "reschedule:1:k2"
        assert old_task["request_absorbed_keys"] == ["reschedule:1:k1"]
        # 原 18:00 实例就是当前业务待办（同一实例移动到 19:00，仍开放）
        assert second["occurrence"]["id"] == first["occurrence"]["id"]
        assert any(row["task_id"] == old_task["id"] and row["status"] == "pending"
                   for row in c.rows)
        once_occs = [row for row in c.rows if row["task_id"] == old_task["id"]]
        assert len(once_occs) == 1
        assert once_occs[0]["est_start"] == at(25, 19).isoformat()


def test_h3_same_key_retry_recovers_rather_than_supersede():
    # H3 场景 C：失败后不改时间、原 key 重试 → 恢复原请求（非取代）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError:
                pass
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15, 30),
            idempotency_key="k1",
        )
        once = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        assert len(once) == 1
        assert once[0]["request_state"] == "completed"
        assert not result.get("superseded")
        assert result["occurrence"]["est_start"] == at(25, 18).isoformat()


def test_h3_old_key_replay_reports_superseded_and_never_revives():
    # H3 场景 D（第七轮语义）：18:00 失败 → 19:00 接管成功 → 旧 key 重放：
    # 明确已被吸收，不创建、不恢复、不改写时间；重复重放稳定。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        with mock.patch.object(
            planning, "_create_occurrences", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                    idempotency_key="k1",
                )
            except planning.PlanningError:
                pass
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        for _ in range(2):
            replay = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16, 30),
                idempotency_key="k1",
            )
            assert replay.get("superseded") is True
            assert replay["occurrence"]["est_start"] == at(25, 19).isoformat()
        planning.generate_due(at(25, 17))
        planning.recompute_today(at(25, 17, 1))
        once_tasks = [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]
        # 只有一个请求任务行：completed 终态 + 旧键留档，不复活
        assert len(once_tasks) == 1
        assert once_tasks[0]["request_state"] == "completed"
        assert once_tasks[0]["request_key"] == "reschedule:1:k2"
        assert once_tasks[0]["request_absorbed_keys"] == ["reschedule:1:k1"]
        # 唯一的 once 实例就是 19:00 manual（无幽灵）
        once_occs = [row for row in c.rows
                     if row["task_id"] in {t["id"] for t in once_tasks}]
        assert len(once_occs) == 1
        assert (once_occs[0]["est_start"], once_occs[0]["fixed_source"]) == (
            at(25, 19).isoformat(), "manual")


def test_m4_reschedule_uses_interval_fact_over_snapshot():
    # M4 优先级分叉保护（Review MEDIUM）：超时重排/adopt 的有效耗时 = est
    # 区间事实（60 分钟），而不是 planned_minutes 快照（30 分钟）——直接
    # 播种存量实例制造分叉（不恢复旧创建入口）。
    with Context() as c:
        c.create("daily", at(23), estimated_minutes=30)
        occ = c.rows[0]
        occ.update({
            "time_mode_snapshot": "explicit",
            "planned_minutes": 30,
            "est_start": at(23, 8).isoformat(),
            "est_end": at(23, 9).isoformat(),
            "status": "timeout",
        })
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="m4seed",
        )
        o = result["occurrence"]
        assert (datetime.fromisoformat(o["est_end"])
                - datetime.fromisoformat(o["est_start"])) == timedelta(minutes=60)


def test_m4_reschedule_duration_task_and_hollow_phase():
    # M4：duration 普通任务 → 30 分钟；中空阶段条目 → 阶段耗时（不取主任务）。
    with Context() as c:
        c.create("daily", at(23), estimated_minutes=30)
        occ = _timeout_occ(c)
        r = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="d1",
        )
        o = r["occurrence"]
        assert (datetime.fromisoformat(o["est_end"])
                - datetime.fromisoformat(o["est_start"])) == timedelta(minutes=30)

    with Context() as c:
        c.create("once", at(24), target_date="2026-09-24", is_hollow=True,
                 hollow_start_minutes=10, hollow_wait_minutes=30, hollow_end_minutes=5)
        start_occ = next(row for row in c.rows if row["phase"] == "start")
        start_occ["status"] = "timeout"
        r = planning.reschedule_timeout_as_new(
            start_occ["id"], {"est_start": at(25, 16).isoformat()}, at(25, 15),
            idempotency_key="h1",
        )
        o = r["occurrence"]
        assert (datetime.fromisoformat(o["est_end"])
                - datetime.fromisoformat(o["est_start"])) == timedelta(minutes=10)
        assert "开始" in o["content"]


def test_m6_idempotent_retry_repairs_baseline_fields():
    # M6：early 记录插入成功、task 基准字段更新失败（以失败后的持久状态
    # 呈现）→ 同 key 重试补齐 last_handled_at / refresh_next_due_at；
    # 连续重试稳定、不产生第二条 early、下一轮 due 语义不变。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        # 先正常完成初始轮，使后续提前完成走 early-insert 路径
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        task_row = c.db.rows["planning_task"][0]
        assert task_row["last_handled_at"] == at(24, 7).isoformat()
        first = planning.complete_task_early(1, at(24, 8), idempotency_key="m6-1")
        assert first["round_key"].startswith("early:2026-09-24:")
        assert task_row["last_handled_at"] == at(24, 8).isoformat()
        # 模拟「task 行更新失败后的持久状态」：字段回退为旧值
        task_row["last_handled_at"] = None
        task_row["refresh_next_due_at"] = None
        # 同 key 重试：补齐而非仅返回
        planning.complete_task_early(1, at(24, 10), idempotency_key="m6-1")
        assert task_row["last_handled_at"] == at(24, 8).isoformat()  # 原请求时刻
        assert task_row["refresh_next_due_at"] == at(27, 8).isoformat()
        # 连续重试稳定、不产生第二条 early
        for _ in range(2):
            planning.complete_task_early(1, at(24, 11), idempotency_key="m6-1")
        early_rows = [row for row in c.rows if row.get("source") == "early"]
        assert len(early_rows) == 1
        assert task_row["refresh_next_due_at"] == at(27, 8).isoformat()


class _FailOnceTable:
    """planning_task 行更新首次失败的真实注入（M6 故障断点）。"""

    def __init__(self, inner, state):
        self._inner = inner
        self._state = state

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def update(self, value):
        if self._state["fail_updates"] and value.get("last_handled_at"):
            self._state["fail_updates"] = False
            raise RuntimeError("task update down")
        return self._inner.update(value)


class _FailOnceClient:
    def __init__(self, inner, state):
        self._inner = inner
        self._state = state

    def table(self, name):
        q = self._inner.table(name)
        if name == "planning_task":
            return _FailOnceTable(q, self._state)
        return q

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_m6_true_failure_injection_then_idempotent_repair():
    # M6 故障注入：early 插入成功 → task 行更新真实失败 → 请求失败 →
    # 同 key 重试补齐；fixed 刷新不受该修复逻辑影响。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        state = {"fail_updates": True}
        failed = False
        with mock.patch.object(planning, "get_client", return_value=_FailOnceClient(c.db, state)):
            # 基准更新失败以原始异常上抛（API 层映射 500）——请求失败
            try:
                planning.complete_task_early(1, at(24, 8), idempotency_key="m6-fix")
            except RuntimeError:
                failed = True
        assert failed is True
        # 注入点在 set_occurrence_status 的基准更新（open-rows 路径）：
        # 实例行已completed+handled，task 行字段失败后保持旧值
        task_row = c.db.rows["planning_task"][0]
        rounds = [row for row in c.rows if row["task_id"] == 1]
        assert len(rounds) == 1
        assert rounds[0]["status"] == "completed"
        assert rounds[0]["handled_at"] == at(24, 8).isoformat()
        # 同键重试补齐
        planning.complete_task_early(1, at(24, 9), idempotency_key="m6-fix")
        assert task_row["last_handled_at"] == at(24, 8).isoformat()
        assert task_row["refresh_next_due_at"] == at(27, 8).isoformat()
        assert len([row for row in c.rows if row["task_id"] == 1]) == 1

    # fixed 刷新不被误伤：同键重试不写 last_handled_at
    with Context() as c:
        c.create("interval", at(24, 6), refresh_mode="fixed_interval", interval_days=3)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        planning.complete_task_early(1, at(24, 8), idempotency_key="f1")
        assert c.db.rows["planning_task"][0].get("last_handled_at") is None
        planning.complete_task_early(1, at(24, 9), idempotency_key="f1")
        assert c.db.rows["planning_task"][0].get("last_handled_at") is None
