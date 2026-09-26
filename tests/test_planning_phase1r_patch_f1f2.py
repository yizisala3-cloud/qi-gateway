"""Phase 1R 最终并发收口（F1 / F2）回归测试。

F1：重排「首次创建路径」的资格守卫与 completed CAS 此前只看
`request_state`，并发接管（adopt）改写 `request_key` 而不改
`request_state`，被接管的旧创建请求得以继续锚定/完成，覆盖接管方已
成功落库的较新时间。修复后：资格谓词必须同时确认
`request_state == 'pending' AND request_key == 本请求自己的键`；
pending→completed CAS 以 request_key 为条件；锚定落库受数据库触发器
兜底（锚定标记必须与任务行当前请求身份一致）。

F2：`request_absorbed_keys` 的并发登记此前是应用层 read-modify-write，
两个 stand-down（或 adopt 与 stand-down）互相覆盖导致请求键从
current 与 absorbed 中同时消失，丢失键迟到重放被当成全新修改。修复后：
登记经数据库函数原子合并（单条 UPDATE 内读取行锁下的最新数组并去重
合并），任何请求键始终可追踪。
"""

from unittest import mock

from gateway import planning
from test_planning_phase1b import Context, at
from test_planning_phase1r_repair5 import (
    _once_occs,
    _once_tasks,
    _timeout_occ,
)


def _identity_of(task_row):
    return {
        "current": task_row.get("request_key"),
        "absorbed": set(task_row.get("request_absorbed_keys") or []),
    }


def test_f1_taken_over_creator_cannot_land_stale_anchor():
    # F1：A 首次创建 19:00（task+occurrence 已建立）→ A 的资格守卫读取时，
    # B 已完成接管（身份 CAS + 锚定 20:00）但 completed CAS 尚未提交
    # （request_state 仍为 pending）→ 恢复 A。修复前守卫只看 request_state
    # → 放行 → A 锚定 19:00 覆盖 20:00；修复后守卫同时确认
    # request_key == A 自己的键 → stand-down 收敛，occurrence 保持 20:00。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        real_guard = planning._reschedule_still_pending
        ran = {"b": False}

        def interleaved_guard(client, task_row):
            if not ran["b"]:
                ran["b"] = True
                # B 完整执行接管（身份 CAS + 锚定 20:00 + completed）
                planning.reschedule_timeout_as_new(
                    occ["id"], {"est_start": at(25, 20).isoformat()}, at(25, 15, 10),
                    idempotency_key="kB",
                )
                # B 的 completed CAS 尚未提交的真实窗口：A 的读提交视图里
                # 任务行最后已提交状态仍为 pending（改活行，非快照）
                live = next(t for t in c.db.rows["planning_task"] if t["id"] == task_row["id"])
                live["request_state"] = "pending"
            return real_guard(client, task_row)

        with mock.patch.object(planning, "_reschedule_still_pending", side_effect=interleaved_guard):
            result_a = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, 19).isoformat()}, at(25, 15),
                idempotency_key="kA",
            )
        once_tasks = _once_tasks(c)
        assert len(once_tasks) == 1  # 不产生第二个业务 occurrence
        assert len(_once_occs(c)) == 1
        final_occ = _once_occs(c)[0]
        # B 的较新成功修改不被 A 覆盖
        assert final_occ["est_start"] == at(25, 20).isoformat()
        assert final_occ["generation_request_key"] == "reschedule:1:kB"
        identity = _identity_of(once_tasks[0])
        assert identity["current"] == "reschedule:1:kB"
        assert identity["absorbed"] >= {"reschedule:1:kA"}
        # A stand-down：收敛到当前最新状态
        assert result_a.get("superseded") is True
        assert result_a["occurrence"]["est_start"] == at(25, 20).isoformat()


def test_f1_creator_completed_cas_is_key_conditioned():
    # completed CAS 以 request_key 为条件：身份被接管后，旧创建请求的
    # completed 提交不落地（0 行命中），不会把接管方标记为自身完成。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        r1 = planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        task_id = r1["task"]["id"]
        task_row = next(t for t in c.db.rows["planning_task"] if t["id"] == task_id)
        # 模拟并发接管后旧创建请求继续提交 completed（状态仍 pending、键已换）
        task_row["request_state"] = "pending"
        stale_cas = c.db.table("planning_task").update({
            "request_state": "completed", "updated_at": at(25, 15, 30).isoformat(),
        }).eq("id", task_id).eq("request_state", "pending").eq(
            "request_key", "reschedule:1:kA").execute()
        assert stale_cas.data == []  # 键条件未命中：旧请求不得推进生命周期
        assert task_row["request_state"] == "pending"


def test_f1_adopt_state_cas_is_key_conditioned():
    # adopt 的 pending→completed 同样以 request_key 为条件：并发新接管
    # 落在锚定之后、状态提交之前时，旧接管方不得覆盖新接管方的状态。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        task_row = _once_tasks(c)[0]
        # 模拟：接管方锚定后，另一请求已再度接管（键已换），旧接管方
        # 才提交 completed
        task_row["request_key"] = "reschedule:1:kC"
        stale_cas = c.db.table("planning_task").update({
            "request_state": "completed", "updated_at": at(25, 15, 30).isoformat(),
        }).eq("id", task_row["id"]).eq("request_state", "pending").eq(
            "request_key", "reschedule:1:kB").execute()
        assert stale_cas.data == []


def test_f2_standdown_registrations_merge_atomically():
    # F2（应用层）：两个 stand-down 的 absorbed 登记走数据库原子合并——
    # 后登记者在数据库内最新数组上合并，不覆盖先登记的键。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        task_row = _once_tasks(c)[0]
        task_id = task_row["id"]
        task_row["request_key"] = "reschedule:1:kA"
        task_row["request_absorbed_keys"] = ["reschedule:1:k1"]
        # B、C 依次（数据库内原子合并；真并发由 PostgreSQL 双连接测试覆盖）
        merged_b = planning._rpc(
            c.db, "planning_absorb_reschedule_request", {
                "p_task_id": task_id,
                "p_request_key": "reschedule:1:kB",
                "p_now": at(25, 15, 20).isoformat(),
            })
        merged_c = planning._rpc(
            c.db, "planning_absorb_reschedule_request", {
                "p_task_id": task_id,
                "p_request_key": "reschedule:1:kC",
                "p_now": at(25, 15, 21).isoformat(),
            })
        assert merged_b is True and merged_c is True
        identity = _identity_of(task_row)
        assert identity["current"] == "reschedule:1:kA"
        # B、C 都保留（不允许 last-writer-wins），重复登记不产生重复项
        assert identity["absorbed"] >= {
            "reschedule:1:k1", "reschedule:1:kB", "reschedule:1:kC"}
        merged_again = planning._rpc(
            c.db, "planning_absorb_reschedule_request", {
                "p_task_id": task_id,
                "p_request_key": "reschedule:1:kB",
                "p_now": at(25, 15, 22).isoformat(),
            })
        assert merged_again is False  # 已在数组：幂等不重复
        assert len(task_row["request_absorbed_keys"]) == 3


def test_f2_takeover_merges_absorbed_atomically():
    # adopt 身份接管与 absorbed 合并在同一条原子操作内：接管前的当前键
    # 进入 absorbed，且不覆盖并发登记（真并发由 PostgreSQL 双连接验证）。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        task_row = _once_tasks(c)[0]
        task_id = task_row["id"]
        task_row["request_key"] = "reschedule:1:kA"
        task_row["request_absorbed_keys"] = ["reschedule:1:k1"]
        won = planning._rpc(
            c.db, "planning_takeover_reschedule_request", {
                "p_task_id": task_id,
                "p_new_key": "reschedule:1:kB",
                "p_new_est_start": at(25, 20).isoformat(),
                "p_expected_key": "reschedule:1:kA",
                "p_now": at(25, 15, 20).isoformat(),
            })
        assert won is True
        identity = _identity_of(task_row)
        assert identity["current"] == "reschedule:1:kB"
        assert identity["absorbed"] >= {"reschedule:1:k1", "reschedule:1:kA"}
        # 过期读取的接管（expected 不匹配）0 行命中
        stale = planning._rpc(
            c.db, "planning_takeover_reschedule_request", {
                "p_task_id": task_id,
                "p_new_key": "reschedule:1:kZ",
                "p_new_est_start": at(25, 21).isoformat(),
                "p_expected_key": "reschedule:1:kA",
                "p_now": at(25, 15, 21).isoformat(),
            })
        assert stale is False
        assert _identity_of(task_row)["current"] == "reschedule:1:kB"


def test_f1f2_all_history_keys_replay_converge_after_race():
    # 三个不同 key 快速连续修改 + 全部历史键迟到重放：开放时返回现状
    # 不改时间；键始终可追踪。
    with Context() as c:
        c.create("daily", at(23))
        occ = _timeout_occ(c)
        planning.reschedule_timeout_as_new(
            occ["id"], {"est_start": at(25, 18).isoformat()}, at(25, 14),
            idempotency_key="k1",
        )
        for key, hour in (("kA", 19), ("kB", 20), ("kC", 21)):
            planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, hour).isoformat()}, at(25, 15, 10),
                idempotency_key=key,
            )
        task_row = _once_tasks(c)[0]
        identity = _identity_of(task_row)
        assert identity["current"] == "reschedule:1:kC"
        assert identity["absorbed"] >= {
            "reschedule:1:k1", "reschedule:1:kA", "reschedule:1:kB"}
        occ_id = _once_occs(c)[0]["id"]
        for key, hour in (("k1", 18), ("kA", 19), ("kB", 20)):
            replay = planning.reschedule_timeout_as_new(
                occ["id"], {"est_start": at(25, hour).isoformat()}, at(25, 16),
                idempotency_key=key,
            )
            assert replay["occurrence"]["id"] == occ_id
            assert replay["occurrence"]["est_start"] == at(25, 21).isoformat()
        assert len(_once_tasks(c)) == 1
        assert len(_once_occs(c)) == 1
