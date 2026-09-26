"""Phase 1R 第六轮收口（H1–H4 / M6）回归测试（第七轮语义下更新）。

- H1：after_completion 提前完成的 30 分钟防重复窗口（UI 失败重试新 key
  在窗口内不重复入账；窗口外为新的真实操作——见 repair5 场景 D/E）。
- H2：superseded 请求不得通过普通重新启用入口复活（后端 + 后台防御）。
  （第七轮接管模型下，superseded 只能由旧数据 / 直接改库形成；守卫保留
  为数据库与应用层双兜底。）
- H3：supersede 用户事实分流已被第七轮接管模型取代：同一业务实例被接管
  而非关闭/删除，partial / in_progress 用户事实天然保留（repair5 场景 B/C）。
- H4：并发接管后，旧在途请求不得落地 completed（资格重核对 + 收敛）。
- H5：completed 状态写失败后，同键重试补状态且不覆盖后续人工修改。
- M6：接管路径失败不破坏当前业务待办（修复后语义，见 repair5）。
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


def test_h1_after_completion_ui_retry_with_new_key_does_not_double_count():
    # H1：第一次提前完成成功（基准后移）；UI 失败重试场景下用户在 30 分钟
    # 窗口内再次点击（新 key）→ 不得再次入账，返回已有记录。
    with Context() as c:
        c.create("interval", at(24), refresh_mode="after_completion", interval_days=3)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        e1 = planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        e2 = planning.complete_task_early(1, at(24, 8, 5), idempotency_key="k2")  # 新 key
        early = [row for row in c.rows if row.get("source") == "early"]
        assert len(early) == 1
        assert e2["id"] == e1["id"]
        task_row = c.db.rows["planning_task"][0]
        assert task_row["refresh_next_due_at"] == at(27, 8).isoformat()  # 基准只移一次


def test_h2_superseded_task_cannot_be_reenabled():
    # H2：被取代请求（superseded 终态）不得通过普通启用入口复活；后台亦
    # 不再生成。第七轮下该状态由直接改库 / 旧数据形成，守卫保持双兜底。
    with Context() as c:
        c.create("daily", at(23))
        c.create("once", at(23), target_date="2026-09-25")  # 目标未到期：无实例
        stale = next(t for t in c.db.rows["planning_task"] if t["task_type"] == "once")
        # 模拟被取代的旧请求任务行（与应用层第六轮取代路径落库形状一致）
        stale["request_key"] = "reschedule:99:k-old"
        stale["request_state"] = "superseded"
        stale["is_active"] = False
        # 普通启用入口被拒绝
        try:
            planning.update_task(stale["id"], {"is_active": True}, at(25, 16))
        except planning.PlanningError as error:
            assert error.status_code == 409
        else:
            raise AssertionError("superseded task must not be re-enabled")
        # 后台防御：即便被特权方式启用，也不为被取代任务生成实例
        # （daily 主任务 9/25 轮正常生成，计 1 条）
        stale["is_active"] = True
        assert planning.generate_due(at(25, 16, 30))["created"] == 1
        assert not any(row["task_id"] == stale["id"] for row in c.rows)


def test_h3a_partial_user_fact_survives_takeover():
    # H3a（第七轮语义）：后台自愈实例承载 partial 用户事实 → 再次修改时间
    # 接管同一实例：partial 保留、实例保持开放、不产生第二条业务待办。
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
        planning.generate_due(at(25, 15, 30))
        healed = next(r for r in c.rows if r["task_id"] == 2)
        planning.set_occurrence_status(
            healed["id"], {"status": "partial", "partial_note": "做了一半"}, at(25, 15, 45))
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 16),
            idempotency_key="k2",
        )
        assert result["occurrence"]["id"] == healed["id"]  # 同一实例被接管
        assert healed["status"] == "partial"  # 保持开放生命周期
        assert healed["partial_note"] == "做了一半"  # 用户说明保留
        assert healed["partial_at"] == at(25, 15, 45).isoformat()
        assert healed["est_start"] == at(25, 19).isoformat()
        once_tasks = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once_tasks) == 1  # 无双开
        assert not any(r["status"] == "discarded_this" for r in c.rows)


def test_h3b_in_progress_user_fact_survives_takeover():
    # H3b（第七轮语义）：用户点开始（actual_start）的实例 → 接管保留
    # actual_start，不双开、不关闭。
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
        planning.generate_due(at(25, 15, 30))
        healed = next(r for r in c.rows if r["task_id"] == 2)
        planning.start_occurrence(healed["id"], at(25, 15, 45))
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 16),
            idempotency_key="k2",
        )
        assert result["occurrence"]["id"] == healed["id"]
        assert healed["actual_start"] is not None  # 用户事实保留
        assert healed["status"] == "in_progress"
        once_tasks = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once_tasks) == 1  # 无双开
        assert not any(r["status"] == "discarded_this" for r in c.rows)


def test_h3c_background_artifact_is_adopted_not_duplicated():
    # H3 对照（第七轮语义）：用户从未触碰的后台自愈产物 → 接管时被重新
    # 锚定为当前业务待办（零删除），不产生第二条。
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
        planning.generate_due(at(25, 15, 30))
        healed = next(r for r in c.rows if r["task_id"] == 2)
        assert healed["estimated_time_source"] in ("unassigned", "automatic")
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 16),
            idempotency_key="k2",
        )
        assert result["occurrence"]["id"] == healed["id"]  # 产物被接管（未删除）
        assert healed["est_start"] == at(25, 19).isoformat()
        assert healed["fixed_source"] == "manual"
        once_tasks = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once_tasks) == 1


def test_h4_superseded_inflight_call_cannot_land_completed():
    # H4：18:00 请求在途，19:00 请求接管后，18:00 旧调用继续执行也不得
    # 覆盖接管结果；最终只有一个业务待办（19:00，manual）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        real_create = planning._create_occurrences
        state = {"phase": "A"}

        def interleaved(*args, **kwargs):
            if state["phase"] == "A":
                state["phase"] = "B-done"
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
                    idempotency_key="k2",
                )
            return real_create(*args, **kwargs)

        with mock.patch.object(planning, "_create_occurrences", side_effect=interleaved):
            r1 = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                idempotency_key="k1",
            )
        once_occs = [r for r in c.rows if r["task_id"] in (2, 3)]
        assert len(once_occs) == 1  # 只剩一个业务待办
        assert once_occs[0]["est_start"] == at(25, 19).isoformat()
        assert once_occs[0]["fixed_source"] == "manual"
        # 接管请求落地 completed；旧请求键被吸收（迟到重放不再改写）
        task_row = next(t for t in c.db.rows["planning_task"] if t["task_type"] == "once")
        assert task_row["request_state"] == "completed"
        assert task_row["request_key"] == "reschedule:1:k2"
        assert "reschedule:1:k1" in (task_row.get("request_absorbed_keys") or [])
        assert r1["occurrence"]["est_start"] == at(25, 19).isoformat()


def test_h5_pending_with_anchor_missing_state_is_repaired():
    # H5：锚定成功但 completed 写失败（状态仍 pending）→ 同键重试补状态，
    # 不覆盖实例（18:00 保持 manual）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        task_row = next(t for t in c.db.rows["planning_task"] if t["id"] == r1["task"]["id"])
        task_row["request_state"] = "pending"  # 模拟状态写入失败
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16),
            idempotency_key="k1",
        )
        assert task_row["request_state"] == "completed"
        occ_row = next(row for row in c.rows if row["task_id"] == task_row["id"])
        assert occ_row["est_start"] == at(25, 18).isoformat()
        assert occ_row["fixed_source"] == "manual"


def test_h5_pending_user_edit_after_anchor_is_preserved():
    # H5/I5：completed 写失败 + 用户随后合法改时间 → 重试补状态且不覆盖
    # 用户 20:00（manual 所有权即用户已确立的时间事实）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        occ_id = r1["occurrence"]["id"]
        task_row = next(t for t in c.db.rows["planning_task"] if t["id"] == r1["task"]["id"])
        task_row["request_state"] = "pending"  # 模拟状态写入失败
        planning.patch_occurrence(occ_id, {"est_start": at(25, 20).isoformat()}, at(25, 15, 45))
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16),
            idempotency_key="k1",
        )
        assert task_row["request_state"] == "completed"
        occ_row = next(row for row in c.rows if row["id"] == occ_id)
        assert occ_row["est_start"] == at(25, 20).isoformat()  # 用户修改保留
        assert occ_row["fixed_source"] == "manual"


def test_m6_failed_first_request_stays_recoverable_and_is_taken_over_cleanly():
    # M6（第七轮语义）：首次请求生成失败 → 请求保持 pending 可恢复；后续
    # 新请求接管该请求的业务待办（吸收旧键）；旧键迟到重放返回现状。
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
        old_task = next(t for t in c.db.rows["planning_task"] if t["id"] == 2)
        # 首次请求失败：请求保持 pending 可恢复
        assert old_task["request_state"] == "pending"
        assert old_task["is_active"] is True
        # 后续新请求：接管该 pending 请求的业务待办（无第二条任务）
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        once_tasks = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert result["occurrence"]["est_start"] == at(25, 19).isoformat()
        assert once_tasks[0]["request_key"] == "reschedule:1:k2"
        assert once_tasks[0]["request_absorbed_keys"] == ["reschedule:1:k1"]
        # 旧键迟到重放：返回现状，不复活、不改写
        replay = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16),
            idempotency_key="k1",
        )
        assert replay.get("superseded") is True
        assert replay["occurrence"]["est_start"] == at(25, 19).isoformat()


def test_matrix_c_background_heal_then_new_action_takes_over():
    # 矩阵 C（第七轮语义）：后台自愈与再次修改时间交错——自愈产物被接管
    # 为当前业务待办，只有一个 once 任务。
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
        planning.generate_due(at(25, 15, 30))  # 后台自愈
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 40),
            idempotency_key="k2",
        )
        once_tasks = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once_tasks) == 1
        assert result["occurrence"]["est_start"] == at(25, 19).isoformat()
        assert once_tasks[0]["request_state"] == "completed"
        assert once_tasks[0]["request_key"] == "reschedule:1:k2"


def test_matrix_d_same_key_interleaved_calls_converge():
    # 矩阵 D：同 key 两个调用交错（第二个在第一个 finalize 前开始）——
    # 第二个先完成锚定与 completed，第一个继续时按 completed 重放，
    # 不重复建任务、不覆盖。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        real_finalize = planning._finalize_reschedule_occurrence
        calls = {"n": 0}

        def interleaved_finalize(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一个调用 finalize 前，第二个完整请求先跑完
                result_b = planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15, 30),
                    idempotency_key="k1",
                )
                assert result_b["occurrence"] is not None
            return real_finalize(*args, **kwargs)

        with mock.patch.object(
            planning, "_finalize_reschedule_occurrence", side_effect=interleaved_finalize,
        ):
            result_a = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                idempotency_key="k1",
            )
        once = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once) == 1
        assert once[0]["request_state"] == "completed"
        occs = [r for r in c.rows if r["task_id"] == once[0]["id"]]
        assert len(occs) == 1
        assert result_a["occurrence"]["id"] == occs[0]["id"]


def test_matrix_e_different_times_new_keys_move_same_todo():
    # 矩阵 E（第七轮语义）：不同 key / 不同时间 = 对同一当前业务待办的
    # 两次时间修改——同一实例保持身份，最终一个待办、时间取最后一次。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="ka",
        )
        r2 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 15, 30),
            idempotency_key="kb",
        )
        once = [t for t in c.db.rows["planning_task"] if t["task_type"] == "once"]
        assert len(once) == 1
        assert r2["occurrence"]["id"] == r1["occurrence"]["id"]
        assert r2["occurrence"]["est_start"] == at(25, 20).isoformat()
        assert r2["occurrence"]["fixed_source"] == "manual"


def test_matrix_f_completed_write_failure_then_user_edit_then_retry():
    # 矩阵 F：副作用成功 → 状态写失败 → 用户改时间 → 重试补状态不覆盖。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        task_row = next(t for t in c.db.rows["planning_task"] if t["id"] == r1["task"]["id"])
        task_row["request_state"] = "pending"
        occ_id = r1["occurrence"]["id"]
        planning.patch_occurrence(occ_id, {"est_start": at(25, 20).isoformat()}, at(25, 15, 45))
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16),
            idempotency_key="k1",
        )
        assert task_row["request_state"] == "completed"
        row = next(r for r in c.rows if r["id"] == occ_id)
        assert row["est_start"] == at(25, 20).isoformat()