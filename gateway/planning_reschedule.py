"""Idempotent timeout rescheduling, replay recovery and request takeover.

Request identity, absorbed keys and manual-anchor provenance converge through
the existing guarded database operations; closed source history is preserved."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_cycles as cycles
from . import planning_recompute as recompute
from . import planning_generation as generation
from . import planning_tasks as task_service
from . import planning_serialization as presentation

log = logging.getLogger("gateway.planning")


def reschedule_timeout_as_new(
    occurrence_id: int, payload: Any, now: datetime | None = None,
    *, idempotency_key: str | None = None,
) -> dict[str, Any]:
    """超时实例的「重新安排」：不复活旧实例。

    旧超时记录原样保留（状态、closed_at、handled_at 均不改写）。第一次
    重排创建一个全新的单次待办承载后续执行；对该超时记录的再次「修改
    时间」（无论新键旧键、实例是否 partial、是否经过后台恢复）统一收敛到
    **同一个当前业务待办**：同一实例保持业务身份，只移动排程时间
    （BF1/BF2 第七轮语义）。请求身份（模型 A）持久化在**任务行**上，早于
    实例成立：同键重放、实例缺失恢复、并发碰撞都收敛到同一份业务结果。

    用户选择的是**绝对日历日期时间**：实例立即可见于包含该时刻的规划周期
    （9/25 04:00 选 9/25 05:00 → 当前 9/24 周期内立即可见），预估时刻以
    人工锚点恒等于所选时刻，不随周期归属漂移。
    """
    now = now or runtime._now()
    if not isinstance(payload, dict):
        raise common.PlanningError("invalid_payload", "request body must be a JSON object")
    if idempotency_key is not None and (not isinstance(idempotency_key, str)
                                        or not 1 <= len(idempotency_key) <= 200):
        raise common.PlanningError("invalid_payload", "Idempotency-Key 必须为 1 至 200 字符", 400)
    new_start_raw = payload.get("est_start")
    if not new_start_raw:
        raise common.PlanningError("invalid_payload", "est_start is required", 422)
    new_start = common._parse_dt(new_start_raw, "est_start")
    client = runtime._require_client()
    occ = runtime._fetch_occurrence(client, occurrence_id)
    if not occ:
        raise common.PlanningError("not_found", "planning occurrence not found", 404)
    if occ.get("status") != "timeout":
        raise common.PlanningError(
            "invalid_transition", "只有已超时的实例可以重新安排为新的单次待办", 422,
        )
    if not occ.get("round_key"):
        raise common.PlanningError("legacy_instance", "旧实例须在受控升级后处理", 409)
    task = runtime._fetch_task(client, occ["task_id"])
    if not task:
        raise common.PlanningError("not_found", "planning task not found", 404)

    request_key = (
        f"reschedule:{occurrence_id}:{idempotency_key}" if idempotency_key else None
    )
    if request_key:
        # 收敛路径（BF1/BF2）：同键 = 同一次请求的重放 / 恢复；键已被吸收 =
        # 迟到重放（返回现状，不改写时间）；全新键 = 对当前业务待办的再一次
        # 「修改时间」（接管同一实例）。都不落入首次创建。
        converged = _converge_reschedule_request(
            client, request_key, new_start, occ, task, now,
        )
        if converged is not None:
            return converged
    # 仅新请求执行「目标时间不能早于当前时间」校验（H1）。
    if new_start < now - timedelta(minutes=5):
        raise common.PlanningError("invalid_payload", "新的执行时间不能早于当前时间", 422)

    today = cycles._current_cycle(now).key
    # 中空阶段超时（口径 2026-09-25）：按 user 操作的那个条目确定内容与
    # 有效耗时（M4：显式区间优先，与排程同源）；内容取生成时冻结的展示
    # 快照（BF5），不回读任务当前名称。
    duration = common._effective_minutes(occ, task) or 30
    row = task_service.validate_task_payload({
        "content": occ.get("display_content") or presentation._display_content(task, occ),
        "task_type": "once",
        "target_date": today.isoformat(),
        "time_mode": "duration",
        "estimated_minutes": duration,
    }, partial=False)
    task_service._prepare_refresh_definition(row, now)
    row["created_at"] = common._iso(now)
    row["updated_at"] = common._iso(now)
    if request_key:
        # 请求身份 + 请求内容 + 初始 pending 状态随任务行落库（早于实例）：
        # 部分唯一索引收敛并发；绝对执行时刻在实例缺失 / 被后台重排时仍可
        # 恢复。同一超时实例的当前业务待办唯一性由数据库触发器兜底。
        row["request_key"] = request_key
        row["request_est_start"] = common._iso(new_start)
        row["request_state"] = "pending"
        row["request_source_occurrence_id"] = occurrence_id
    try:
        response = client.table("planning_task").insert(row).execute()
    except Exception as exc:
        if request_key and _is_reschedule_convergence_conflict(exc):
            # 并发碰撞（同键唯一索引 / 同源业务待办触发器）→ 收敛到已成立的
            # 结果：接管当前业务待办或按请求身份恢复。
            converged = _converge_reschedule_request(
                client, request_key, new_start, occ, task, now,
            )
            if converged is not None:
                return converged
        raise
    created_task = (response.data or [{}])[0]

    # 实例立即生成于当前周期（target = 当前周期 ≤ today 恒成立），随后把
    # 用户所选绝对时刻以人工锚点写入；生成失败报 503 且任务保留——同键
    # 重试经请求身份 + 内容恢复（B4/M1/N1）。并发下另一请求可能已代为
    # 建立实例（返回 0）：不视为失败，重新读取并核对请求结果（N3）。
    try:
        created = generation._create_occurrences(
            client, created_task, today, now, display_cycle_date=today,
        )
    except common.PlanningError:
        raise
    except Exception as exc:
        log.warning("planning 超时重排实例生成失败: error=%s", type(exc).__name__)
        raise common.PlanningError(
            "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
        ) from exc
    if not created:
        converged = _converge_reschedule_request(
            client, request_key, new_start, occ, task, now,
        ) if request_key else None
        if converged is not None:
            return converged
        raise common.PlanningError(
            "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
        )
    # H4/F1：副作用提交前重读资格——并发新请求可能已接管本请求的业务待办
    #（接管只改 request_key 不改 request_state，资格必须同时确认两者）。
    if request_key and not _reschedule_still_pending(client, created_task):
        return _converge_reschedule_request(
            client, request_key, new_start, occ, task, now,
        )
    try:
        result = _finalize_reschedule_occurrence(
            client, created_task, new_start, now, old_occurrence_id=occurrence_id,
            request_key=request_key,
        )
    except common.PlanningError:
        raise
    except Exception as exc:
        if request_key and _is_anchor_rejection(exc):
            # 锚定标记守卫拒绝：检查与写入之间身份已被并发请求接管——本
            # 请求立即 stand-down，收敛到当前最新状态（F1 数据库兜底）。
            return _converge_reschedule_request(
                client, request_key, new_start, occ, task, now,
            )
        raise
    if request_key:
        # 请求生命周期：completed 提交用条件更新（H4/F1）——条件同时包含
        # request_state 与 request_key：并发接管后本方不再落地 completed，
        # 也不会把接管方的请求错误标记为自身完成。
        completed_cas = client.table("planning_task").update({
            "request_state": "completed", "updated_at": common._iso(now),
        }).eq("id", created_task["id"]).eq("request_state", "pending").eq(
            "request_key", request_key,
        ).execute()
        if not completed_cas.data:
            return _converge_reschedule_request(
                client, request_key, new_start, occ, task, now,
            )
    log.info(
        "planning 超时重排为新建单次待办: old=%s new_task=%s",
        occurrence_id, created_task["id"],
    )
    return result


def _is_reschedule_convergence_conflict(exc: Exception) -> bool:
    """首次创建命中的两类数据库收敛点：请求键唯一索引 / 同源业务待办守卫。"""
    text = str(exc)
    return (
        "planning_task_request_key_uq" in text
        or "another reschedule todo for this timeout is still current" in text
    )


def _is_anchor_rejection(exc: Exception) -> bool:
    """锚定标记守卫拒绝：写入标记时任务行身份已被并发请求接管（F1）。"""
    return "reschedule anchor marker must match the current request identity" in str(exc)


def _rpc(client, fn: str, params: dict[str, Any]) -> Any:
    """调用数据库函数（PostgREST RPC），返回 .data（布尔函数为 True/False）。"""
    return client.rpc(fn, params).execute().data


def _reschedule_still_pending(client, task_row):
    """H4/F1：重读任务行，核对请求仍处于 pending **且身份未被并发接管**。

    资格必须同时确认 ``request_state == 'pending'`` 与
    ``request_key == 本请求自己的键``——adopt 接管只改 request_key 不改
    request_state，仅看状态的守卫对接管事件是盲的。
    """
    current = runtime._fetch_task(client, task_row["id"])
    return bool(current) and current.get("request_state") == "pending" and (
        current.get("request_key") == task_row.get("request_key")
    )


def _reschedule_request_family(
    client, old_occurrence_id: int,
) -> list[dict[str, Any]]:
    """同一旧超时实例名下的全部重排请求任务（含被吸收身份的任务行）。"""
    prefix = f"reschedule:{old_occurrence_id}:"
    return [
        row for row in runtime._rows(client, "planning_task")
        if str(row.get("request_key") or "").startswith(prefix)
        or any(str(key or "").startswith(prefix)
               for key in (row.get("request_absorbed_keys") or []))
    ]


def _converge_reschedule_request(
    client, request_key: str, new_start: datetime,
    old_occ: dict[str, Any], old_task: dict[str, Any], now: datetime,
) -> dict[str, Any] | None:
    """按「请求身份 + 请求内容」收敛重排结果（重放 / 恢复 / 接管共用）。

    请求身份（含被吸收的旧键）持久化在任务行上：
    - 同 key：同一次请求的重放或恢复。恢复时若实例不存在则补生成；若实例
      的预估时刻不是请求时刻（后台维护在恢复前把它排成了自动时间），强制
      重新应用用户人工锚点——请求未按其内容完成前，后台不得永久覆盖。
    - key 已被吸收：迟到重放。该请求已被更新的「修改时间」操作吸收，返回
      当前状态，不再改写时间、不复活（N1/H5 的用户修改不受影响）。
    - 全新 key：对该超时记录当前业务待办的再一次「修改时间」——接管同一
      实例并把时间移动到新值；当前业务待办尚未收尾时绝不新建第二条
      （BF1/BF2）。仅当之前的重排业务待办都已正常关闭时才走首次创建。
    - 同 key + 不同参数：明确拒绝（409），不静默返回旧结果。
    返回 None 表示尚无该超时记录名下的请求（调用方继续首次创建）。
    """
    family = _reschedule_request_family(client, old_occ["id"])
    if not family:
        return None
    prior = next(
        (row for row in family if row.get("request_key") == request_key), None)
    if prior is not None:
        recorded = prior.get("request_est_start")
        if recorded is not None and common._parse_dt(recorded, "request_est_start") != new_start:
            raise common.PlanningError(
                "request_conflict",
                "同一请求键已绑定不同的执行时间；请使用新的请求提交新的时间", 409,
            )
        return _resume_reschedule_request(client, prior, new_start, old_occ, now)
    absorbed_host = next(
        (row for row in family
         if request_key in (row.get("request_absorbed_keys") or [])), None)
    if absorbed_host is not None:
        # 迟到重放：请求已被后续修改时间操作吸收，返回现状即可。
        return _replay_reschedule_result(
            client, absorbed_host, old_occ, now, superseded=True)
    # 全新的一次「修改时间」：接管当前业务待办（同一实例只移动时间）。
    adoptable = _adoptable_reschedule_tasks(client, family)
    if not adoptable:
        return None  # 之前的重排待办均已正常关闭：本次属于新的业务安排
    target = max(adoptable, key=lambda row: row["id"])
    if new_start < now - timedelta(minutes=5):
        raise common.PlanningError("invalid_payload", "新的执行时间不能早于当前时间", 422)
    return _adopt_reschedule_task(
        client, target, request_key, new_start, old_occ, now)


def _adoptable_reschedule_tasks(
    client, family: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """仍构成「当前业务待办」的请求任务：pending 请求或名下仍有开放实例。

    与数据库守卫（planning_reschedule_todo_guard）同一判定；数据库保证
    同一来源至多一个，此处冗余防御只取最新。
    """
    adoptable = [
        row for row in family
        if row.get("request_state") != "superseded"
        and row.get("request_state") == "pending"
    ]
    with_open = [
        row for row in family
        if row.get("request_state") != "superseded"
        and any(
            occ.get("status") in common.OPEN_STATUSES
            for occ in runtime._rows(client, "planning_occurrence",
                             lambda q: q.eq("task_id", row["id"]))
        )
    ]
    seen: dict[int, dict[str, Any]] = {row["id"]: row for row in adoptable}
    for row in with_open:
        seen.setdefault(row["id"], row)
    return list(seen.values())


def _replay_reschedule_result(
    client, task_row: dict[str, Any], old_occ: dict[str, Any], now: datetime,
    *, superseded: bool,
) -> dict[str, Any]:
    occ_rows = runtime._rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", task_row["id"]).limit(1),
    )
    result: dict[str, Any] = {
        "task": presentation.serialize_task(task_row, now),
        "occurrence": presentation.serialize_occurrence(occ_rows[0], task_row, now) if occ_rows else None,
        "rescheduled_from": old_occ["id"],
        "replayed": True,
    }
    if superseded:
        result["superseded"] = True
    return result


def _resume_reschedule_request(
    client, prior: dict[str, Any], new_start: datetime,
    old_occ: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """同 key 重放 / 恢复：补齐未完成的副作用，不覆盖已确立的用户事实。"""
    state = prior.get("request_state") or "pending"
    occ_rows = runtime._rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", prior["id"]).limit(1),
    )
    today = cycles._current_cycle(now).key
    target = common._parse_date(prior["target_date"], "target_date")
    if state == "superseded":
        # 已被新操作取代的旧请求——明确返回，不恢复、不创建。
        return _replay_reschedule_result(client, prior, old_occ, now, superseded=True)
    if not occ_rows:
        if target > today:
            return _replay_reschedule_result(client, prior, old_occ, now, superseded=False)
        try:
            generation._create_occurrences(client, prior, target, now, display_cycle_date=today)
        except Exception as exc:
            log.warning("planning 超时重排恢复生成失败: error=%s", type(exc).__name__)
            raise common.PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            ) from exc
        occ_rows = runtime._rows(
            client, "planning_occurrence", lambda q: q.eq("task_id", prior["id"]).limit(1),
        )
        if not occ_rows:
            raise common.PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            )
    if state == "completed":
        # 请求已成功完成。锚定标记（generation_request_key）区分两种情形：
        # 标记=本请求键 → 本请求锚定已达成，重放只返回结果身份，不重写
        # 锚点（实例之后的合法用户修改不被撤销，H5）；标记≠本请求键 →
        # 身份 CAS 已归属本请求但锚定落库前中断（C-1 崩溃窗口）→ 补应用
        # 本请求所选时刻（仅开放实例；已关闭历史不改写）。
        current = occ_rows[0]
        anchored_by_self = current.get("generation_request_key") == prior.get("request_key")
        if not anchored_by_self and current.get("status") in common.OPEN_STATUSES:
            _finalize_reschedule_occurrence(
                client, prior, new_start, now, old_occurrence_id=old_occ["id"],
                request_key=prior.get("request_key"),
            )
        return _replay_reschedule_result(client, prior, old_occ, now, superseded=False)
    current = occ_rows[0]
    anchored = (
        current.get("est_start") is not None
        and current.get("fixed_source") == "manual"
    )
    if not anchored:
        # pending：实例缺失，或实例被后台维护排成了自动时间（锚定未达成）
        # ——强制恢复用户所选时刻与人工所有权（N1）。
        try:
            _finalize_reschedule_occurrence(
                client, prior, new_start, now, old_occurrence_id=old_occ["id"],
                request_key=prior.get("request_key"),
            )
        except common.PlanningError:
            raise
        except Exception as exc:
            if _is_anchor_rejection(exc):
                # 锚定标记守卫拒绝：身份已被并发请求接管——本请求立即
                # stand-down，返回当前最新状态（F1 数据库兜底）。
                return _replay_reschedule_result(
                    client, prior, old_occ, now, superseded=True)
            raise
    else:
        # H5：锚定副作用已达成（manual 所有权即用户已确立的时间事实）——
        # 无论时刻是否等于请求时刻（用户随后可能合法改过时间），只补请求
        # 状态，不重写实例。
        pass
    completed_cas = client.table("planning_task").update({
        "request_state": "completed", "updated_at": common._iso(now),
    }).eq("id", prior["id"]).eq("request_state", "pending").eq(
        "request_key", prior.get("request_key"),
    ).execute()
    if not completed_cas.data:
        # 身份已被并发请求接管（F1）：返回当前最新状态，不推进他人生命周期。
        refreshed = runtime._fetch_task(client, prior["id"]) or prior
        return _replay_reschedule_result(
            client, refreshed, old_occ, now, superseded=True)
    return _replay_reschedule_result(client, prior, old_occ, now, superseded=False)


def _adopt_reschedule_task(
    client, target: dict[str, Any], request_key: str, new_start: datetime,
    old_occ: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """BF1/BF2：再次「修改时间」接管当前业务待办——同一实例保持身份。

    C-1（并发 CAS）：接管首先以条件更新（CAS）夺取任务行请求身份——
    ``UPDATE ... WHERE request_key = 本次读取到的当前键``。CAS 失败说明
    读取之后已有并发请求改写身份，本请求随后**不得产生任何实例副作用**
    （不锚定、不建实例），只重读最新状态并把本键登记进
    `request_absorbed_keys`（登记本身同样带 CAS 条件），迟到重放即可收敛
    ——绝不基于旧快照覆盖数据库，也绝不覆盖更晚的用户修改。赢得 CAS 后
    才允许移动同一实例的预估时刻（partial 等用户事实原行保留）；锚定落库
    时写入本请求键标记（`generation_request_key`），崩溃窗口由同键重试经
    恢复路径补应用。不关闭、不删除任何实例。
    """
    for _ in range(5):
        fresh = runtime._fetch_task(client, target["id"])
        if not fresh:
            raise common.PlanningError("not_found", "planning task not found", 404)
        current_key = fresh.get("request_key")
        if current_key == request_key:
            # 并发窗口内本请求身份已被自己此前的尝试确立：按重放收敛。
            return _resume_reschedule_request(
                client, fresh, new_start, old_occ, now)
        if request_key in (fresh.get("request_absorbed_keys") or []):
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=True)
        # CAS（F2：数据库原子函数）：仅当身份仍等于本次读取值时接管；
        # absorbed 合并在同一条 UPDATE 内于行锁下读取最新数组完成——
        # 并发登记不可能被本写入覆盖，本请求的键也不可能丢失。
        won = _rpc(client, "planning_takeover_reschedule_request", {
            "p_task_id": fresh["id"],
            "p_new_key": request_key,
            "p_new_est_start": common._iso(new_start),
            "p_expected_key": current_key,
            "p_now": common._iso(now),
        })
        if won:
            return _apply_adopted_intent(
                client, fresh, request_key, new_start, old_occ, now)
        # CAS 失败：身份已被并发请求改写。本请求尚未建立任何事实，重读
        # 最新状态并把本键原子登记为 absorbed（stand-down），保证键可追踪。
        fresh = runtime._fetch_task(client, target["id"])
        if not fresh:
            continue
        current_key = fresh.get("request_key")
        if current_key == request_key:
            return _resume_reschedule_request(
                client, fresh, new_start, old_occ, now)
        if request_key in (fresh.get("request_absorbed_keys") or []):
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=True)
        registered = _rpc(client, "planning_absorb_reschedule_request", {
            "p_task_id": fresh["id"],
            "p_request_key": request_key,
            "p_now": common._iso(now),
        })
        if registered:
            # 登记成功：本请求被更新的修改吸收，返回当前状态（不改时间、
            # 不建第二实例、不覆盖更晚的用户修改）。
            refreshed = runtime._fetch_task(client, target["id"]) or fresh
            return _replay_reschedule_result(
                client, refreshed, old_occ, now, superseded=True)
        if request_key in (fresh.get("request_absorbed_keys") or []):
            # 并发窗口内另一路径已完成登记：同样按吸收重放收敛。
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=True)
        # 两次 CAS 都输给并发写 → 重读重试（有限次，耗尽报 503）。
    raise common.PlanningError(
        "database_unavailable", "当前修改请求并发冲突，请稍后重试", 503,
    )


def _apply_adopted_intent(
    client, fresh: dict[str, Any], request_key: str, new_start: datetime,
    old_occ: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """CAS 赢得身份后的副作用阶段：本请求现在是该业务待办的最新意图。

    顺序：先建缺失实例 → 移动同一实例时刻（仅开放实例；已关闭实例不改写
    est，避免向关闭历史补写旧预估）→ 条件写 completed 终态。任何一步中断
    都由同键重试经恢复路径补齐（pending 未锚定 / completed 标记缺失）。
    """
    occ_rows = runtime._rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", fresh["id"]).limit(1),
    )
    today = cycles._current_cycle(now).key
    if not occ_rows:
        task_target = common._parse_date(fresh["target_date"], "target_date")
        if task_target > today:
            # 理论不可达（请求建立时目标周期已到期）；身份已接管，返回现状。
            return _replay_reschedule_result(
                client, fresh, old_occ, now, superseded=False)
        try:
            generation._create_occurrences(client, fresh, task_target, now, display_cycle_date=today)
        except Exception as exc:
            log.warning("planning 超时重排接管生成失败: error=%s", type(exc).__name__)
            raise common.PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            ) from exc
        occ_rows = runtime._rows(
            client, "planning_occurrence", lambda q: q.eq("task_id", fresh["id"]).limit(1),
        )
        if not occ_rows:
            raise common.PlanningError(
                "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
            )
    if occ_rows[0].get("status") in common.OPEN_STATUSES:
        # 同一实例移动到用户所选时刻（partial 等用户事实保留在原行）。
        try:
            _finalize_reschedule_occurrence(
                client, fresh, new_start, now, old_occurrence_id=old_occ["id"],
                request_key=request_key,
            )
        except common.PlanningError:
            raise
        except Exception as exc:
            if _is_anchor_rejection(exc):
                # 并发再度接管发生在锚定与身份提交的间隙之外（防御）：本方
                # 立即停止，返回当前最新状态，不覆盖更晚的修改。
                refreshed = runtime._fetch_task(client, fresh["id"]) or fresh
                return _replay_reschedule_result(
                    client, refreshed, old_occ, now, superseded=True)
            raise
    else:
        log.info(
            "planning 接管目标实例已关闭，跳过锚定: task=%s", fresh["id"],
        )
    # pending → completed（条件更新，F1：同时以 request_key 为条件；已
    # completed 的接管保持终态不变；并发再度接管后本方不再推进状态）。
    completed_cas = client.table("planning_task").update({
        "request_state": "completed", "updated_at": common._iso(now),
    }).eq("id", fresh["id"]).eq("request_state", "pending").eq(
        "request_key", request_key,
    ).execute()
    refreshed = runtime._fetch_task(client, fresh["id"]) or fresh
    occ_rows = runtime._rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", fresh["id"]).limit(1),
    )
    if not completed_cas.data:
        # 状态提交未命中：身份已被并发请求再度接管或已达终态——返回当前
        # 最新状态（最新意图由其持有者负责完成）。
        return _replay_reschedule_result(
            client, refreshed, old_occ, now, superseded=False)
    log.info(
        "planning 超时重排接管当前业务待办: old=%s task=%s",
        old_occ["id"], fresh["id"],
    )
    return {
        "task": presentation.serialize_task(refreshed, now),
        "occurrence": presentation.serialize_occurrence(occ_rows[0], refreshed, now) if occ_rows else None,
        "rescheduled_from": old_occ["id"],
        "adopted": True,
    }


def _finalize_reschedule_occurrence(
    client, new_task: dict[str, Any], new_start: datetime, now: datetime,
    old_occurrence_id: int | None = None, request_key: str | None = None,
) -> dict[str, Any]:
    """把用户所选绝对时刻写入新实例（人工锚点）并返回结果。

    展示周期遵循既有「人工改时间」语义：实例出现在包含该执行时刻的规划
    周期（更晚则 manual_defer 顺延），绝不早于其原始周期。

    锚定落库时把请求键标记到实例 `generation_request_key`：同键重放据此
    区分「本请求锚定已达成」（H5：不重写，用户后续修改不被撤销）与「身份
    已接管但锚定尚未落库」（CAS 后锚定前的崩溃窗口：补应用本请求锚点）。
    """
    occ_rows = runtime._rows(
        client, "planning_occurrence", lambda q: q.eq("task_id", new_task["id"]).limit(1),
    )
    if not occ_rows:
        raise common.PlanningError(
            "database_unavailable", "新待办实例生成失败，请稍后重试", 503,
        )
    occ = occ_rows[0]
    anchored = (
        occ.get("est_start") is not None
        and common._parse_dt(occ["est_start"], "est_start") == new_start
        and occ.get("fixed_source") == "manual"
        and (request_key is None or occ.get("generation_request_key") == request_key)
    )
    if not anchored:
        # 首次锚定，或恢复被后台维护改写过的时刻，或身份已接管但锚定尚未
        # 落库：强制回到用户所选绝对时刻与 manual 所有权（请求内容未达成
        # 前，后台不得永久覆盖）。
        patch = recompute._manual_estimate_patch(occ, new_task, {"est_start": common._iso(new_start)}, now)
        patch["updated_at"] = common._iso(now)
        if request_key:
            patch["generation_request_key"] = request_key
        client.table("planning_occurrence").update(patch).eq("id", occ["id"]).execute()
        occ = {**occ, **patch}
    return {
        "task": presentation.serialize_task(new_task, now),
        "occurrence": presentation.serialize_occurrence(occ, new_task, now),
        "rescheduled_from": old_occurrence_id,
    }
