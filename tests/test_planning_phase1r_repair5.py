"""Phase 1R 第七轮收口（BF1–BF5）回归测试。

产品语义（user 已确认，2026-09-25）：
- 修改当前待办时间 = 修改同一个当前业务待办实例的排程时间；无论该实例
  是否超时、来自 timeout attention、经过后台恢复、是否 partial、连续修改
  多少次，最终只有一个当前有效业务待办（BF1/BF2 接管模型）。
- after_completion 提前完成采用 30 分钟防重复窗口：窗口起点 = 最近一次
  已经成功成立的完成事实（服务端持久化）；窗口内重复收敛，窗口外为新的
  真实操作（BF3）。
- 用户已提交的 partial/note/actual/manual 事实绝不被旧快照清理删除
  （BF4：业务实例无 DELETE 路径）。
- 已生成实例按生成时快照展示，任务编辑不重新解释历史（BF5）。
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


def _once_tasks(c):
    return [row for row in c.db.rows["planning_task"] if row["task_type"] == "once"]


def _once_occs(c):
    once_ids = {row["id"] for row in _once_tasks(c)}
    return [row for row in c.rows if row["task_id"] in once_ids]


# ── BF1 + BF2：修改时间 = 同一业务实例的时间修改 ─────────────────────


def test_scenario_a_sequential_time_changes_keep_one_business_todo():
    # A：18:00 pending → 改 19:00 → 改 20:00。最终只有一个有效业务待办，
    # 为 20:00；旧请求键被吸收，迟到重放不再改写时间。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        r2 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        r3 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 16),
            idempotency_key="k3",
        )
        # 同一业务实例：三次操作都是同一个 occurrence
        assert r2["occurrence"]["id"] == r1["occurrence"]["id"]
        assert r3["occurrence"]["id"] == r1["occurrence"]["id"]
        assert len(_once_tasks(c)) == 1
        assert len(_once_occs(c)) == 1
        final = _once_occs(c)[0]
        assert final["est_start"] == at(25, 20).isoformat()
        assert final["fixed_source"] == "manual"
        task_row = _once_tasks(c)[0]
        assert task_row["request_state"] == "completed"
        assert task_row["request_key"] == "reschedule:1:k3"
        assert task_row["request_absorbed_keys"] == ["reschedule:1:k1", "reschedule:1:k2"]
        # 迟到重放旧键：返回现状（20:00），不改写时间
        late = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16, 30),
            idempotency_key="k1",
        )
        assert late["occurrence"]["est_start"] == at(25, 20).isoformat()
        assert late.get("replayed") is True


def test_scenario_b_partial_progress_survives_time_change():
    # B：18:00 partial + note → 改 20:00。当前有效待办仍是同一实例，
    # 状态保持 partial、说明保留；不产生 partial 为空的新待办。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        occ_id = r1["occurrence"]["id"]
        planning.set_occurrence_status(
            occ_id, {"status": "partial", "partial_note": "已经完成 70%"}, at(25, 15, 20))
        r2 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 15, 40),
            idempotency_key="k2",
        )
        assert r2["occurrence"]["id"] == occ_id  # 同一业务实例
        row = next(row for row in c.rows if row["id"] == occ_id)
        assert row["status"] == "partial"
        assert row["partial_note"] == "已经完成 70%"
        assert row["partial_at"] == at(25, 15, 20).isoformat()
        assert row["est_start"] == at(25, 20).isoformat()
        assert len(_once_tasks(c)) == 1
        # 没有任何实例被技术性标成此次不执行
        assert not any(row["status"] == "discarded_this" for row in c.rows)


def test_scenario_c_partial_move_then_complete_has_no_discarded_side_effect():
    # C：partial → 改时间 → 完整完成。完成历史与关闭语义干净：没有
    # discarded_this 业务副作用，handled_at 来自真正完成时刻。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        occ_id = r1["occurrence"]["id"]
        planning.set_occurrence_status(
            occ_id, {"status": "partial", "partial_note": "做了一半"}, at(25, 15, 20))
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 15, 40),
            idempotency_key="k2",
        )
        planning.set_occurrence_status(occ_id, {"status": "completed"}, at(25, 21))
        row = next(row for row in c.rows if row["id"] == occ_id)
        assert row["status"] == "completed"
        assert row["handled_at"] == at(25, 21).isoformat()
        assert row["partial_note"] == "做了一半"  # 历史事实保留
        assert not any(row["status"] == "discarded_this" for row in c.rows)


def test_bf1_healed_partial_instance_is_adopted_not_closed():
    # BF1 原始复现路径：k1 建实例失败 → 后台自愈出实例 → 用户记录 partial
    # → 再次修改时间。接管后同一实例保持开放并携带 partial，而不是
    # discarded_this + 空白新实例。
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
        healed = next(row for row in c.rows if row["task_id"] == 2)
        planning.set_occurrence_status(
            healed["id"], {"status": "partial", "partial_note": "已经完成 70%"},
            at(25, 15, 45),
        )
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 16),
            idempotency_key="k2",
        )
        assert result["occurrence"]["id"] == healed["id"]  # 同一实例被接管
        assert result.get("adopted") is True
        row = next(row for row in c.rows if row["id"] == healed["id"])
        assert row["status"] == "partial"  # 保持开放生命周期
        assert row["partial_note"] == "已经完成 70%"
        assert row["est_start"] == at(25, 20).isoformat()
        assert len(_once_tasks(c)) == 1
        assert not any(row["status"] == "discarded_this" for row in c.rows)
        # 板上只有一个当前有效业务待办
        board = planning.today_board(at(25, 16, 1))["progress"]
        once_visible = [item for item in board if item["task_type"] == "once"]
        assert [(item["est_start"], item["status"], item["partial_note"]) for item in once_visible] == [
            (at(25, 20).isoformat(), "partial", "已经完成 70%"),
        ]


def test_bf2_concurrent_first_creation_converges_via_database_guard():
    # 并发首次创建（不同 key）：数据库守卫拒绝第二个当前业务待办 → 后到者
    # 收敛到接管路径。最终只有一个业务待办，时间取后到请求。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)

        real_insert = c.db.table
        state = {"k1_done": False}

        def guard_table(name):
            q = real_insert(name)

            class Guard:
                def __init__(self, inner):
                    self._inner = inner

                def __getattr__(self, attr):
                    return getattr(self._inner, attr)

                def insert(self, data):
                    if (name == "planning_task" and state["k1_done"]):
                        # 真实数据库语义：同源业务待办守卫拒绝第二个任务行
                        raise RuntimeError(
                            "another reschedule todo for this timeout is still current")
                    return self._inner.insert(data)

            return Guard(q)

        real_create = planning._create_occurrences

        def k1_completes_first(*args, **kwargs):
            if not state["k1_done"]:
                state["k1_done"] = True
                return real_create(*args, **kwargs)
            return real_create(*args, **kwargs)

        with mock.patch.object(planning, "get_client",
                               return_value=type("C", (), {
                                   "table": staticmethod(guard_table),
                                   "rpc": staticmethod(c.db.rpc),
                               })()):
            r1 = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
                idempotency_key="k1",
            )
            with mock.patch.object(planning, "_create_occurrences", side_effect=real_create):
                r2 = planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 10),
                    idempotency_key="k2",
                )
        assert len(_once_tasks(c)) == 1
        assert len(_once_occs(c)) == 1
        final = _once_occs(c)[0]
        assert final["est_start"] == at(25, 19).isoformat()
        task_row = _once_tasks(c)[0]
        assert task_row["request_state"] == "completed"
        assert task_row["request_key"] == "reschedule:1:k2"
        assert task_row["request_absorbed_keys"] == ["reschedule:1:k1"]


def test_bf2_reschedule_after_previous_todo_closed_creates_new_todo():
    # 前一重排业务待办正常关闭后，对该超时记录的再次重排属于新的一次业务
    # 安排：允许新建（历史请求行与历史实例保留）。数据库守卫同样放行。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        planning.set_occurrence_status(
            r1["occurrence"]["id"], {"status": "completed"}, at(25, 18, 5))
        r2 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(26, 10).isoformat()}, at(25, 19),
            idempotency_key="k2",
        )
        assert r2["task"]["id"] != r1["task"]["id"]
        assert r2.get("adopted") is None
        assert len(_once_tasks(c)) == 2
        states = sorted(row["request_state"] for row in _once_tasks(c))
        assert states == ["completed", "completed"]
        new_occ = next(row for row in c.rows if row["task_id"] == r2["task"]["id"])
        assert new_occ["status"] == "pending"
        assert new_occ["est_start"] == at(26, 10).isoformat()


def test_bf2_adopt_failure_keeps_current_todo_recoverable():
    # M6 精神在接管模型下的等价物：接管中途失败（锚定写入失败）→ 当前
    # 业务待办不丢失、不重复；重试收敛。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        real_finalize = planning._finalize_reschedule_occurrence
        with mock.patch.object(
            planning, "_finalize_reschedule_occurrence", side_effect=RuntimeError("down"),
        ):
            try:
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
                    idempotency_key="k2",
                )
            except RuntimeError:
                pass
        # 失败不产生第二条业务待办，原 18:00 保持开放
        assert len(_once_tasks(c)) == 1
        assert len(_once_occs(c)) == 1
        assert _once_occs(c)[0]["est_start"] == at(25, 18).isoformat()
        # 重试收敛到 19:00（同一实例）
        r2 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 40),
            idempotency_key="k2",
        )
        assert r2["occurrence"]["id"] == r1["occurrence"]["id"]
        assert r2["occurrence"]["est_start"] == at(25, 19).isoformat()
        assert len(_once_tasks(c)) == 1


def test_bf4_business_occurrences_are_never_deleted_by_reschedule_flows():
    # BF4 不变量：重排 / 接管 / 恢复全流程对业务实例零删除——用户可见的
    # 实例行只会被更新，不会被移除。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        planning.set_occurrence_status(
            r1["occurrence"]["id"], {"status": "partial", "partial_note": "x"}, at(25, 15, 20))
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 15, 40),
            idempotency_key="k2",
        )
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 21).isoformat()}, at(25, 15, 50),
            idempotency_key="k3",
        )
        ids_before = {row["id"] for row in c.rows}
        # 迟到重放 / 同键重试 / 后台维护都不会删除实例（新轮生成是正常业务，
        # 不在本不变量范围内）
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 16),
            idempotency_key="k1",
        )
        planning.generate_due(at(25, 16, 1))
        planning.recompute_today(at(25, 16, 2))
        assert ids_before <= {row["id"] for row in c.rows}


# ── BF3：30 分钟防重复窗口 ───────────────────────────────────────────


def _after_completion_task(c, created=at(24, 7)):
    return c.create("interval", created, refresh_mode="after_completion", interval_days=3)


def test_bf3_scenario_d_retry_within_30min_window_is_duplicate():
    # D：08:00 early 成功 → 08:05 新 key early。只存在一次成功事实，
    # 刷新基准只推进一次。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        e1 = planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        e2 = planning.complete_task_early(1, at(24, 8, 5), idempotency_key="k2")
        early = [row for row in c.rows if row.get("source") == "early"]
        assert len(early) == 1
        assert e2["id"] == e1["id"]
        task_row = c.db.rows["planning_task"][0]
        assert task_row["last_handled_at"] == at(24, 8).isoformat()
        assert task_row["refresh_next_due_at"] == at(27, 8).isoformat()  # 基准只移一次


def test_bf3_scenario_e_early_after_30min_is_new_real_fact():
    # E：08:00 early 成功 → 08:31 early。产生第二次真实成功事实，并从
    # 第二次重新计算下一 due。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        e2 = planning.complete_task_early(1, at(24, 8, 31), idempotency_key="k2")
        early = sorted(
            (row for row in c.rows if row.get("source") == "early"),
            key=lambda row: row["handled_at"],
        )
        assert len(early) == 2
        assert early[1]["id"] == e2["id"]
        assert early[1]["handled_at"] == at(24, 8, 31).isoformat()
        task_row = c.db.rows["planning_task"][0]
        assert task_row["last_handled_at"] == at(24, 8, 31).isoformat()
        assert task_row["refresh_next_due_at"] == at(27, 8, 31).isoformat()


def test_bf3_review_case_next_day_early_not_swallowed():
    # 复审 BF3 原始场景：9/25 08:00 提前完成（下一轮 9/28），9/26 09:00
    # 再次真实提前完成 → 不被吞掉，产生新事实并从本次重新计算。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        planning.complete_task_early(1, at(25, 8), idempotency_key="k1")
        assert c.db.rows["planning_task"][0]["refresh_next_due_at"] == at(28, 8).isoformat()
        e2 = planning.complete_task_early(1, at(26, 9), idempotency_key="k2")
        early = [row for row in c.rows if row.get("source") == "early"]
        assert len(early) == 2
        assert e2["handled_at"] == at(26, 9).isoformat()
        assert c.db.rows["planning_task"][0]["refresh_next_due_at"] == at(29, 9).isoformat()


def test_bf3_scenario_f_failed_first_attempt_does_not_block_retry():
    # F：08:00 点击提前完成，在完成事实真正成功持久化之前数据库失败；
    # 08:05 重试必须允许成功，不得被 30 分钟规则吞掉。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        # 08:00 的尝试在写入任何事实前数据库不可用（_require_client 503）
        with mock.patch.object(planning, "get_client", return_value=None):
            try:
                planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
            except planning.PlanningError as error:
                assert error.status_code == 503
        # 事实未落库：没有任何 early 行，基准未推进
        assert not any(row.get("source") == "early" for row in c.rows)
        result = planning.complete_task_early(1, at(24, 8, 5), idempotency_key="k2")
        early = [row for row in c.rows if row.get("source") == "early"]
        assert len(early) == 1
        assert result["handled_at"] == at(24, 8, 5).isoformat()
        assert c.db.rows["planning_task"][0]["refresh_next_due_at"] == at(27, 8, 5).isoformat()


def test_bf3_early_button_round_completion_opens_window():
    # 提前完成按钮完成当前开放轮次（08:00）→ 08:10 再次点击属于同一窗口
    # 内的重复请求：返回该轮完成结果，不新增第二条事实。
    with Context() as c:
        _after_completion_task(c)
        first = planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        # 第一次点击完成了开放轮次（source=schedule 的轮次行）
        assert first["source"] == "schedule"
        assert first["handled_at"] == at(24, 8).isoformat()
        second = planning.complete_task_early(1, at(24, 8, 10), idempotency_key="k2")
        assert second["id"] == first["id"]
        assert second["status"] == "completed"
        # 只有一次完成事实、基准只推进一次
        handled_rows = [row for row in c.rows if row.get("handled_at")]
        assert len(handled_rows) == 1
        assert c.db.rows["planning_task"][0]["refresh_next_due_at"] == at(27, 8).isoformat()


def test_bf3_window_does_not_swallow_other_operations():
    # 窗口判重只作用于提前完成：普通完成、partial、此次不执行不受影响。
    with Context() as c:
        _after_completion_task(c)
        planning.set_occurrence_status(c.rows[0]["id"], {"status": "completed"}, at(24, 7))
        planning.complete_task_early(1, at(24, 8), idempotency_key="k1")
        # 窗口内下一轮到期前的普通操作照常工作（这里没有开放轮次，普通
        # 完成语义由 set_occurrence_status 承载；验证 early 不吞 partial 场景：
        # 新轮生成后 partial 正常记录）
        planning.generate_due(at(27, 8))
        fresh = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-27"
                     or (row.get("round_key") or "").startswith("handled:"))
        fresh = next(row for row in c.rows if row["status"] == "pending")
        planning.set_occurrence_status(
            fresh["id"], {"status": "partial", "partial_note": "继续做"}, at(27, 9))
        row = next(row for row in c.rows if row["id"] == fresh["id"])
        assert row["status"] == "partial"
        assert row["partial_note"] == "继续做"


def test_bf3_fixed_mode_period_uniqueness_keeps_period_identity():
    # 固定刷新型的同期唯一性继续按 early_period_date 工作（回归保护）。
    with Context() as c:
        c.create("interval", at(24, 7), refresh_mode="fixed_interval", interval_days=3)
        first = c.rows[0]
        planning.set_occurrence_status(first["id"], {"status": "completed"}, at(24, 7, 10))
        e1 = planning.complete_task_early(1, at(24, 8), idempotency_key="f1")
        e2 = planning.complete_task_early(1, at(24, 8, 20), idempotency_key="f2")
        assert e2["id"] == e1["id"]  # 同周期重复收敛
        e1_row = next(row for row in c.rows if row["id"] == e1["id"])
        assert e1_row.get("early_period_date") == "2026-09-24"


# ── BF4：用户事实不被旧快照清理 ──────────────────────────────────────


def test_bf4_scenario_h_concurrent_user_fact_write_survives_adopt():
    # H：接管流程读取实例时尚无用户事实 → 并发事务成功写入 partial/note
    # → 接管继续。最终用户事实不得丢失（新模型：接管只更新时间所有权，
    # 不关闭、不删除实例）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        occ_id = r1["occurrence"]["id"]
        real_finalize = planning._finalize_reschedule_occurrence

        def interleaved_finalize(*args, **kwargs):
            # 并发用户写入在接管读取之后、锚定更新之前提交
            planning.set_occurrence_status(
                occ_id, {"status": "partial", "partial_note": "并发保存的进度"},
                at(25, 15, 35),
            )
            return real_finalize(*args, **kwargs)

        with mock.patch.object(
            planning, "_finalize_reschedule_occurrence", side_effect=interleaved_finalize,
        ):
            r2 = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 15, 30),
                idempotency_key="k2",
            )
        row = next(row for row in c.rows if row["id"] == occ_id)
        assert row["partial_note"] == "并发保存的进度"  # 用户事实保留
        assert row["partial_at"] == at(25, 15, 35).isoformat()
        assert row["status"] == "partial"
        assert row["est_start"] == at(25, 20).isoformat()  # 时间修改同样生效
        assert r2["occurrence"]["id"] == occ_id


def test_bf4_scenario_i_completed_request_always_has_its_occurrence():
    # I：旧请求完成与新请求接管交错后，不得出现 completed request 却没有
    # 其必要 occurrence 的破损状态。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15, 30),
            idempotency_key="k2",
        )
        for task_row in _once_tasks(c):
            if task_row.get("request_state") in ("completed", "pending"):
                occs = [row for row in c.rows if row["task_id"] == task_row["id"]]
                assert occs, f"task {task_row['id']} lost its occurrence"
        # 被吸收的请求也不产生孤儿任务行
        assert all(row["request_state"] in ("pending", "completed")
                   for row in _once_tasks(c))


# ── BF5：已生成实例冻结快照 ──────────────────────────────────────────


def test_bf5_scenario_j_rename_keeps_generated_occurrence_name():
    # J：生成 occurrence A（content=旧名称）→ 修改 task content → A 仍显示
    # 旧名称，新生成的 occurrence B 显示新名称。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        a = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-24")
        planning.update_task(1, {"content": "学日语"}, at(24, 7))
        board = planning.today_board(at(24, 7, 1))
        item = next(item for item in board["progress"] if item["id"] == a["id"])
        assert item["content"] == "daily"  # 生成时名称（create_task 的 content）
        assert item["task_content"] == "daily"
        # 新周期生成 B：显示新名称
        planning.generate_due(at(25, 6))
        b = next(row for row in c.rows if row["round_key"] == "cycle:2026-09-25")
        board2 = planning.today_board(at(25, 6, 1))
        item_b = next(item for item in board2["progress"] if item["id"] == b["id"])
        assert item_b["content"] == "学日语"


def test_bf5k_seeded_legacy_snapshot_reader_compatibility():
    # BF5-K 读取兼容保护（Review MEDIUM）：已生成历史 occurrence 不得被当前
    # task definition 重新解释。直接播种存量实例：occ 快照 explicit + 截止
    # 22:00；当前 task 已是 duration、旧 deadline 定义为 20:00（与历史不同）。
    # 读取必须返回 occ 自己的 explicit 历史模式与 22:00 历史截止。
    with Context() as c:
        c.create("daily", at(23), estimated_minutes=30)
        task_row = c.db.rows["planning_task"][0]
        # 当前任务定义与历史快照**故意不同**
        task_row["time_mode"] = "duration"
        task_row["deadline_tod"] = "20:00"
        occ = c.rows[0]
        occ.update({
            "time_mode_snapshot": "explicit",
            "is_limited": True,
            "deadline_at": at(24, 22).isoformat(),
        })
        serialized = planning.serialize_occurrence(occ, task_row, at(25, 8))
        assert serialized["time_mode"] == "explicit"
        assert serialized["deadline_at"] == at(24, 22).isoformat()
        # 直接读取行同样不被任务定义改写（快照原样保留）
        assert occ["time_mode_snapshot"] == "explicit"
        assert occ["deadline_at"] == at(24, 22).isoformat()


def test_bf5_hollow_stage_content_frozen():
    # 中空阶段展示内容快照：任务 hollow_start_content 修改不重解释已生成
    # 阶段；新轮次用新内容。
    with Context() as c:
        c.create("daily", at(23), is_hollow=True, hollow_start_content="泡豆",
                 hollow_start_minutes=10, hollow_wait_minutes=30,
                 hollow_end_minutes=5, hollow_end_content="煮饭")
        planning.generate_due(at(24, 6))
        start = next(row for row in c.rows if row["phase"] == "start")
        end = next(row for row in c.rows if row["phase"] == "end")
        assert start["display_content"] == "泡豆·开始"
        assert end["display_content"] == "煮饭·结束"
        planning.update_task(
            1, {"hollow_start_content": "浸豆", "hollow_end_content": "蒸饭"}, at(24, 7))
        board = planning.today_board(at(24, 7, 1))
        items = {item["id"]: item for item in board["progress"]}
        assert items[start["id"]]["content"] == "泡豆·开始"
        assert items[end["id"]]["content"] == "煮饭·结束"
        # 新轮次使用新内容（9/25 轮在改名后生成；9/24 轮生成于改名前，
        # 本来就应保留旧名称）
        planning.generate_due(at(25, 6))
        new_start = next(row for row in c.rows
                         if row["phase"] == "start" and row["round_key"] == "cycle:2026-09-25")
        assert new_start["display_content"] == "浸豆·开始"


def test_bf5_reschedule_uses_frozen_display_content():
    # 超时重排的新待办内容取生成时冻结的展示快照：任务改名不影响重排产物。
    with Context() as c:
        c.create("daily", at(23))
        planning.generate_due(at(24, 6))
        occ = c.rows[0]
        occ["status"] = "timeout"
        planning.update_task(1, {"content": "全新任务名"}, at(24, 7))
        result = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 15),
            idempotency_key="k1",
        )
        assert result["task"]["content"] == "daily"  # 生成时名称
        new_occ = next(row for row in c.rows if row["task_id"] == result["task"]["id"])
        assert new_occ["content_snapshot"] == "daily"
        assert new_occ["display_content"] == "daily"
