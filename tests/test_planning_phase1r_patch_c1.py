"""Phase 1R 第七轮 Final Patch（C-1 CAS + 30 分钟窗口元数据）回归测试。

C-1（复审唯一 C 类 finding）：adopt 并发修改时间时 `request_absorbed_keys`
可能丢更新，丢失键迟到重放会覆盖较新的用户时间（实例开放时）或产生幽灵
新待办（实例关闭后）。修复：接管身份用条件更新（CAS）先行夺取，CAS 失败
方 stand-down 吸收（登记自身键后返回现状），赢得方才允许触碰实例；锚定
落库写入请求键标记（generation_request_key），同键重试据此补应用崩溃窗口。

30 分钟窗口正式语义：一次成功「完成处理」（含普通完成当前开放轮次、提前
完成产生的事实）后 30 分钟内再次提前完成 = 重复操作；重复响应携带后端
权威元数据（duplicate_within_window / previous_handled_at / elapsed_seconds
/ retry_after_seconds），前端只负责展示、不依赖客户端时钟。
"""

from datetime import timedelta
from unittest import mock

from gateway import planning
from test_planning_phase1b import Context, at
from test_planning_phase1r_repair5 import (
    _after_completion_task,
    _once_occs,
    _once_tasks,
    _timeout_occ,
)


def _identity_of(task_row):
    """request_key + absorbed keys 的集合视图：任何键都不得同时从两处消失。"""
    return {
        "current": task_row.get("request_key"),
        "absorbed": set(task_row.get("request_absorbed_keys") or []),
    }


def _interleave_second_request_before_first_cas(occ, second_payload, second_key,
                                                extra_hook=None):
    """制造真实交错：请求 A 读取任务后、提交身份 CAS 前，请求 B 完整执行。

    通过包装 _fetch_task 实现：A 的 adopt 循环读取被拦截——先让 B 完整
    跑完（读到的是同一份现状），再把 B 执行前的快照返回给 A，使 A 的
    CAS 建立在过期身份上（真实数据库中该 CAS 必然 0 行命中）。
    """
    real_fetch = planning._fetch_task
    state = {"stage": "top", "b_ran": False}

    def hooked_fetch(client, task_id):
        row = real_fetch(client, task_id)
        if state["stage"] == "top":
            state["stage"] = "adopt"  # A 的下一次取用 = adopt 循环读取
            return row
        if state["stage"] == "adopt" and not state["b_ran"]:
            state["b_ran"] = True
            snapshot = dict(row)
            planning.reschedule_timeout_as_new(
                occ["id"], second_payload, at(25, 15, 10),
                idempotency_key=second_key,
            )
            if extra_hook:
                extra_hook()
            return snapshot  # A 基于过期身份发起 CAS
        return row

    return hooked_fetch


def test_c1a_concurrent_double_modify_keeps_both_keys_tracked():
    # C1-A：A→19:00 与 B→20:00 并发（B 在 A 的读取与 CAS 之间完整执行）。
    # B 的 CAS 胜出；A 的 CAS 失败后 stand-down 吸收——A/B/初始三个键全部
    # 可追踪，只有一个业务 occurrence，时间取成功提交的 B。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        hooked = _interleave_second_request_before_first_cas(
            occ, {"est_start": at(25, 20).isoformat()}, "kB",
        )
        with mock.patch.object(planning, "_fetch_task", side_effect=hooked):
            result_a = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                idempotency_key="kA",
            )
        once_tasks = _once_tasks(c)
        assert len(once_tasks) == 1
        assert len(_once_occs(c)) == 1
        assert _once_occs(c)[0]["est_start"] == at(25, 20).isoformat()
        identity = _identity_of(once_tasks[0])
        assert identity["current"] == "reschedule:1:kB"
        # 任何键都不得从 request_key + absorbed keys 中同时消失
        assert identity["absorbed"] >= {"reschedule:1:k1", "reschedule:1:kA"}
        # A 收敛为重放（stand-down），不产生第二条业务待办
        assert result_a.get("replayed") is True
        assert result_a.get("superseded") is True
        assert result_a["occurrence"]["est_start"] == at(25, 20).isoformat()


def test_c1b_lost_key_late_replays_converge_to_current():
    # C1-B：并发完成后分别迟到重放 A、B、初始键——均只能返回/收敛当前
    # 状态，不改写时间、不建第二 occurrence、不覆盖 partial。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        hooked = _interleave_second_request_before_first_cas(
            occ, {"est_start": at(25, 20).isoformat()}, "kB",
        )
        with mock.patch.object(planning, "_fetch_task", side_effect=hooked):
            planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                idempotency_key="kA",
            )
        occ_id = _once_occs(c)[0]["id"]
        planning.set_occurrence_status(
            occ_id, {"status": "partial", "partial_note": "进度"}, at(25, 15, 30))
        for late_key, late_payload in (
            ("kA", {"est_start": at(25, 19).isoformat()}),
            ("kB", {"est_start": at(25, 20).isoformat()}),
            ("k1", {"est_start": at(25, 18).isoformat()}),
        ):
            replay = planning.reschedule_timeout_as_new(
                occ["id"], late_payload, at(25, 16), idempotency_key=late_key,
            )
            assert replay["occurrence"]["id"] == occ_id
            assert replay["occurrence"]["est_start"] == at(25, 20).isoformat()
        row = next(r for r in c.rows if r["id"] == occ_id)
        assert row["est_start"] == at(25, 20).isoformat()
        assert row["partial_note"] == "进度"
        assert len(_once_tasks(c)) == 1
        assert len(_once_occs(c)) == 1


def test_c1c_replay_after_occurrence_closed_creates_no_ghost():
    # C1-C：并发修改后当前业务待办正常关闭 → 重放任一旧键（含丢失键）。
    # 旧键仍被识别为历史请求，不产生幽灵新待办。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        hooked = _interleave_second_request_before_first_cas(
            occ, {"est_start": at(25, 20).isoformat()}, "kB",
        )
        with mock.patch.object(planning, "_fetch_task", side_effect=hooked):
            planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                idempotency_key="kA",
            )
        occ_id = _once_occs(c)[0]["id"]
        planning.set_occurrence_status(occ_id, {"status": "completed"}, at(25, 20, 30))
        # 同键重放必须携带各自原参数（同键不同参数会被 409 拒绝——N2 语义）
        for late_key, late_payload in (
            ("kA", {"est_start": at(25, 19).isoformat()}),
            ("kB", {"est_start": at(25, 20).isoformat()}),
            ("k1", {"est_start": at(25, 18).isoformat()}),
        ):
            replay = planning.reschedule_timeout_as_new(
                occ["id"], late_payload, at(25, 21),
                idempotency_key=late_key,
            )
            assert replay["occurrence"]["id"] == occ_id
            assert replay["occurrence"]["status"] == "completed"
        assert len(_once_tasks(c)) == 1  # 无幽灵
        assert len(_once_occs(c)) == 1


def test_c1d_cas_failure_preserves_newer_user_facts():
    # C1-D：A 读取 → B 成功修改时间 → 用户保存 partial/note → A CAS 失败
    # 并重新收敛。B 的较新时间不被 A 覆盖；partial/note 不丢失；同一
    # occurrence 身份不变。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        occ_id = _once_occs(c)[0]["id"]

        def user_saves_partial():
            planning.set_occurrence_status(
                occ_id, {"status": "partial", "partial_note": "并发进度"},
                at(25, 15, 20),
            )

        hooked = _interleave_second_request_before_first_cas(
            occ, {"est_start": at(25, 20).isoformat()}, "kB",
            extra_hook=user_saves_partial,
        )
        with mock.patch.object(planning, "_fetch_task", side_effect=hooked):
            result_a = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                idempotency_key="kA",
            )
        row = next(r for r in c.rows if r["id"] == occ_id)
        assert row["id"] == occ_id  # occurrence 身份不变
        assert row["est_start"] == at(25, 20).isoformat()  # B 的较新修改保留
        assert row["partial_note"] == "并发进度"  # 用户事实不丢失
        assert row["partial_at"] == at(25, 15, 20).isoformat()
        identity = _identity_of(_once_tasks(c)[0])
        assert identity["current"] == "reschedule:1:kB"
        assert identity["absorbed"] >= {"reschedule:1:k1", "reschedule:1:kA"}
        assert result_a.get("superseded") is True


def test_c1e_winner_crash_window_completed_by_same_key_retry():
    # 崩溃窗口：接管 CAS 赢得身份后、锚定落库前中断 → 同键重试经锚定
    # 标记识别「身份已归属但锚定未达成」，补应用本请求所选时刻。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        occ_id = r1["occurrence"]["id"]

        def crash_after_cas(*args, **kwargs):
            raise RuntimeError("crash between CAS and anchor")

        with mock.patch.object(
            planning, "_finalize_reschedule_occurrence", side_effect=crash_after_cas,
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                    idempotency_key="k2",
                )
            except RuntimeError:
                pass
        # 身份已归属 k2，但实例仍在 k1 的 18:00（标记=k1）
        task_row = _once_tasks(c)[0]
        assert task_row["request_key"] == "reschedule:1:k2"
        assert _once_occs(c)[0]["est_start"] == at(25, 18).isoformat()
        # 同键重试：补应用 19:00（标记=k2），不产生第二条
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        assert result["occurrence"]["id"] == occ_id
        assert result["occurrence"]["est_start"] == at(25, 19).isoformat()
        assert len(_once_tasks(c)) == 1
        row = next(r for r in c.rows if r["id"] == occ_id)
        assert row["generation_request_key"] == "reschedule:1:k2"


def test_c1f_adopt_never_touches_closed_occurrence_est():
    # 接管目标在身份 CAS 前后被关闭：不向已关闭历史补写 est / manual 锚点。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        occ_id = _once_occs(c)[0]["id"]
        real_fetch = planning._fetch_task
        state = {"stage": "top", "closed": False}

        def hooked_fetch(client, task_id):
            row = real_fetch(client, task_id)
            if state["stage"] == "top":
                state["stage"] = "adopt"
                return row
            if state["stage"] == "adopt" and not state["closed"]:
                state["closed"] = True
                snapshot = dict(row)
                # 并发：用户在接管身份落库前关闭了当前业务待办
                planning.set_occurrence_status(occ_id, {"status": "completed"}, at(25, 15, 5))
                return snapshot
            return row

        with mock.patch.object(planning, "_fetch_task", side_effect=hooked_fetch):
            result = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                idempotency_key="k2",
            )
        row = next(r for r in c.rows if r["id"] == occ_id)
        assert row["status"] == "completed"
        assert row["est_start"] == at(25, 18).isoformat()  # 关闭历史 est 不被改写
        assert result["occurrence"]["status"] == "completed"


# ── 30 分钟窗口：正式语义（普通完成也开窗）、边界与元数据 ─────────────


def test_window_29_and_30_minutes_are_duplicates():
    # ≤30 分钟 = 重复：29 分钟与恰好 30:00 均收敛，不新增事实。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        for offset_minutes in (29, 30):
            dup = planning.complete_task_early(
                1, at(24, 8, offset_minutes), idempotency_key=f"k{offset_minutes}")
            early = [row for row in c.rows if row.get("source") == "early"]
            assert len(early) == 1
            assert dup["duplicate_within_window"] is True


def test_window_duplicate_meta_fields_present():
    # 重复响应携带窗口元数据（后端权威，前端只展示）：上次完成时刻、
    # 已过时间、剩余可再次提前完成时间。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        dup = planning.complete_task_early(1, at(24, 8, 4), idempotency_key="k2")
        assert dup["duplicate_within_window"] is True
        assert dup["previous_handled_at"] == at(24, 8).isoformat()
        assert dup["elapsed_seconds"] == 240
        assert dup["retry_after_seconds"] == 1800 - 240


def test_normal_completion_opens_window():
    # 正式语义：一次成功「完成处理」（含普通完成当前开放轮次）后的
    # 30 分钟内，再次提前完成 = 重复。
    with Context() as c:
        _after_completion_task(c)
        round_row = c.rows[0]
        planning.set_occurrence_status(round_row["id"], {"status": "completed"}, at(24, 8))
        dup = planning.complete_task_early(1, at(24, 8, 10), idempotency_key="k1")
        # 收敛到 08:00 的普通完成记录；无 early 行；基准只推进一次
        assert dup["id"] == round_row["id"]
        assert dup["duplicate_within_window"] is True
        assert dup["previous_handled_at"] == at(24, 8).isoformat()
        assert not any(row.get("source") == "early" for row in c.rows)
        assert c.db.rows["planning_task"][0]["refresh_next_due_at"] == at(27, 8).isoformat()


def test_first_fact_failure_still_does_not_open_window():
    # F 回归：完成事实未成功持久化前失败 → 窗口不存在 → 重试成功且无
    # 重复元数据。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        with mock.patch.object(planning, "get_client", return_value=None):
            try:
                planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
            except planning.PlanningError as error:
                assert error.status_code == 503
        assert not any(row.get("source") == "early" for row in c.rows)
        result = planning.complete_task_early(1, at(24, 8, 5), idempotency_key="k2")
        assert "duplicate_within_window" not in result
        assert result["handled_at"] == at(24, 8, 5).isoformat()
