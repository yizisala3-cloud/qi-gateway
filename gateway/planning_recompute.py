"""Planning schedule persistence, time ownership edits and recompute requests.

Batch/round RPC boundaries, optimistic guards and request-token consumption
are preserved. Manual estimate edits resolve cycles through the cycle service."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from . import db
from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_cycles as cycles
from . import planning_schedule as scheduler

log = logging.getLogger("gateway.planning")


def _conditional_lifecycle_update(client, occ: dict[str, Any], patch: dict[str, Any],
                                  current_status: str) -> bool:
    """编辑写入的条件 UPDATE（最终修复问题 1）：状态等值 + 生命周期事实
    集合内联 WHERE——检查与写入之间的并发变化使条件未命中 → 0 行 → 调用方
    以 409 拒绝。返回是否实际写入。"""
    query = client.table("planning_occurrence").update(patch).eq("id", occ["id"])
    query = query.eq("status", current_status)
    for field in common.LIFECYCLE_FACT_FIELDS:
        query = query.is_(field, None)
    result = query.execute()
    return bool(result.data)


def _recompute_expected_snapshot(occ: dict[str, Any]) -> dict[str, Any]:
    """重算某行的 expected snapshot（最终验收修复问题 3；#6 扩为全部参与
    计算行；#25 补回生命周期事实）：compute_schedule 实际读取并决定「可排 /
    可覆盖 / 窗口」的输入字段——状态（含 in_progress / partial 等冻结槽的
    真实开放状态，#6 前硬编码 pending 会误判参与行漂移）、生命周期事实
    （#25：actual_start 等事实读取后并发补录使可排程谓词失效——旧
    _conditional_schedulable_update 的内联门在批量路径的承接）、所有权
    元组、冻结窗口、既有 est 预态与排序（#13：sort_order 是遍历顺序输入，
    读取后 save_order 改序即旧结果作废，与单行条件 UPDATE 的内联等值守卫
    同源）。NULL 显式参与复核。"""
    snapshot: dict[str, Any] = {
        "id": occ["id"],
        "status": occ.get("status"),
        "window_start_at": occ.get("window_start_at"),
        "window_end_at": occ.get("window_end_at"),
        "est_start": occ.get("est_start"),
        "est_end": occ.get("est_end"),
        "estimated_time_source": occ.get("estimated_time_source"),
        "fixed_source": occ.get("fixed_source"),
        "is_fixed": bool(occ.get("is_fixed")),
        "schedule_managed": bool(occ.get("schedule_managed")),
        "sort_order": occ["sort_order"],
    }
    # #25：生命周期事实字段集合单一来源（LIFECYCLE_FACT_FIELDS），与
    # _has_lifecycle_fact / 实例编辑门控同一集合，不复制第二份字段清单。
    for field in common.LIFECYCLE_FACT_FIELDS:
        snapshot[field] = occ.get(field)
    return snapshot


def _atomic_round_write(client, target: dict[str, Any], main_patch: dict[str, Any],
                        sibling: dict[str, Any] | None,
                        sibling_patch: dict[str, Any] | None,
                        expected: list[dict[str, Any]] | None = None) -> None:
    """同轮编辑的原子提交（批次 6 二轮 user 批准 RPC；最终修复问题 1/6）。

    * 单行实例 = 条件 UPDATE（状态 + 生命周期事实集合内联 WHERE）——检查
      与写入之间的并发变化使条件未命中 → 409、零写入（编辑路径不携带
      expected：严格门后 minutes 必为请求自洽）；
    * 中空同轮两阶段 = 一次 ``planning_patch_occurrence_round`` RPC——函数
      内完成行锁、round 身份确认、硬白名单、锁内生命周期二次校验（问题 5：
      窗口字段永远严格门）、expected snapshot 复核（最终验收修复问题 3）、
      窗口一致性、两行各自补丁与整体 rollback；
    * 错误分类（最终验收修复问题 6）：仅固定 ERRCODE 'PC001' / 已知拒绝
      消息映射为并发跳过（``ConcurrencyRejected``）；RPC 缺失 / 连接故障 /
      非预期约束违反 / 未知错误一律向上传播。``expected`` 非 None（重算
      路径）时基础设施失败也直接传播——由 maintenance / 手动重算如实失败；
      编辑路径（None）包装为用户可见 503。
    * 调用方必须在进入本函数前完成全部 payload 校验（validation-before-write）。
    """
    if sibling is None or sibling_patch is None:
        if not _conditional_lifecycle_update(client, target, main_patch,
                                             current_status=target["status"]):
            raise common.ConcurrencyRejected(
                "concurrent_modified",
                "该待办已被并发操作改变，本次编辑未执行，请刷新后重试", 409,
            )
        return
    try:
        client.rpc("planning_patch_occurrence_round", {
            "p_target_id": target["id"],
            "p_sibling_id": sibling["id"],
            "p_target_patch": main_patch,
            "p_sibling_patch": sibling_patch,
            "p_expected": expected,
        }).execute()
    except common.PlanningError:
        raise
    except Exception as exc:
        if common._is_rpc_concurrency_rejection(exc):
            raise common.ConcurrencyRejected(
                "concurrent_modified",
                "同轮原子编辑因并发状态变化被数据库拒绝，本次编辑未执行", 409,
            ) from exc
        if expected is not None:
            # 重算路径：基础设施失败向上传播——maintenance / 手动重算如实
            # 失败（保留等待标记），绝不伪装成并发跳过（最终修复问题 6）。
            raise
        raise common.PlanningError(
            "database_unavailable",
            "同轮原子编辑暂时无法完成，请稍后重试", 503,
        ) from exc


def recompute_today(now: datetime | None = None) -> dict[str, Any]:
    """手动 / 自动重算：只更新当天可自动排程实例的预估起止。

    窗口批次（§19.1 原子性）：任一排程冲突 → 本轮整体不持久化，保留最近
    一次成功排程的既有 est 不清空；冲突清单（派生结果，不落库）随响应
    返回，并由 today 看板按同一纯函数读取时派生展示。
    最终修复（问题 1）：写入阶段以条件 UPDATE 复核生命周期事实集合——
    读取后实例被并发完成 / 开始 / 关闭时放弃该行排程结果（静默跳过）。
    最终修复（问题 2）：中空同轮两阶段同时被重排时经原子 RPC 一次提交
    （锁内复核 + 任意失败整体回滚），不存在「A 新时间、B 旧时间」半提交。
    #6（2026-10-02，迁移 20261002040000）：普通行与中空轮的全部写集合在
    **同一事务**内一次提交，expected 快照覆盖全部参与计算行（不只待写行）
    ——任一行漂移整批放弃（updated=0、stale_skipped=待写行数、等待标记
    保留），不留「部分行新排程、部分行旧排程」的混合状态。
    """
    now = now or runtime._now()
    today = cycles._current_cycle(now).key
    client = runtime._require_client()
    open_rows = runtime._rows(
        client, "planning_occurrence",
        lambda q: q.eq("display_cycle_date", today.isoformat()).in_("status", list(common.OPEN_STATUSES)),
    )
    if not open_rows:
        return {"updated": 0, "at": common._iso(now), "conflicts": []}
    tasks = runtime._task_map(client, {row["task_id"] for row in open_rows})
    result = scheduler.compute_schedule(open_rows, tasks, now)
    if result.conflicts:
        log.info("planning 重算冲突: count=%s date=%s",
                 len(result.conflicts), today.isoformat())
        return {"updated": 0, "at": common._iso(now), "conflicts": result.conflicts}
    placed = result.placed
    updated = 0
    pending_rows: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    for occ_id, (start, end) in placed.items():
        occ = next(row for row in open_rows if row["id"] == occ_id)
        old_start = common._parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
        old_end = common._parse_dt(occ["est_end"], "est_end") if occ.get("est_end") else None
        if old_start == start and old_end == end and occ.get("estimated_time_source") == "automatic":
            continue
        patch = common._estimate_patch(start, end, source="automatic")
        patch["updated_at"] = common._iso(now)
        if not occ.get("nominal_start"):
            patch["nominal_start"] = common._iso(start)
        pending_rows[occ_id] = (occ, patch)
    # 按轮分组：中空同轮两阶段一起经原子 RPC；其余单行条件 UPDATE。
    rounds: dict[tuple[int, str], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    singles: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for occ, patch in pending_rows.values():
        if occ.get("phase") and occ.get("round_key"):
            rounds.setdefault((occ["task_id"], occ["round_key"]), []).append((occ, patch))
        else:
            singles.append((occ, patch))
    stale_skipped = 0
    updated = 0
    if pending_rows:
        # 整批事务 + 完整计算输入复核（#6，迁移 20261002040000 RPC）：
        # * expected 快照覆盖**全部参与计算行**（不只待写行）——固定槽、其它
        #   行状态 / 排序、成员集合任一在读取后漂移 = 整次计算输入失效；
        # * 普通行与中空轮补丁在同一事务内应用（中空轮复用
        #   planning_patch_occurrence_round 的白名单 / 严格门 / 窗口一致性）；
        # * 任一漂移或失败 → PC001 整批放弃：updated=0、stale_skipped=待写
        #   行数、重算等待标记保留，下一次重算按最新输入重新执行（§19.1：
        #   不留「部分行新排程、部分行旧排程」的混合状态）；
        # * 基础设施失败如实向上传播，不伪装成功（最终修复问题 6）。
        expected = [_recompute_expected_snapshot(row) for row in open_rows]
        singles_payload: list[dict[str, Any]] = []
        rounds_payload: list[dict[str, Any]] = []
        for occ, patch in singles:
            singles_payload.append({"id": occ["id"], **patch})
        for entries in rounds.values():
            if len(entries) >= 2:
                (target, target_patch), (sibling, sibling_patch) = entries[0], entries[1]
                rounds_payload.append({
                    "target_id": target["id"], "sibling_id": sibling["id"],
                    "target_patch": target_patch, "sibling_patch": sibling_patch,
                })
            else:
                occ, patch = entries[0]
                singles_payload.append({"id": occ["id"], **patch})
        try:
            response = client.rpc("planning_apply_recompute_batch", {
                "p_expected": expected,
                "p_singles": singles_payload,
                "p_rounds": rounds_payload,
            }).execute()
        except common.PlanningError:
            raise
        except Exception as exc:
            if common._is_rpc_concurrency_rejection(exc):
                log.info("planning 重算因并发状态变化整批放弃: %s", exc)
                stale_skipped = len(pending_rows)
            else:
                raise
        else:
            updated = len(response.data or [])
    log.info("planning 重算完成: updated=%s stale_skipped=%s date=%s",
             updated, stale_skipped, today.isoformat())
    result = {"updated": updated, "at": common._iso(now), "conflicts": []}
    if stale_skipped:
        # 最终 Debug（问题 3）：本次结果不完整，调用方不得据此清掉重算等待
        # 标记；下一次重算按最新输入重新执行。
        result["stale_skipped"] = stale_skipped
    return result


# ── 重算等待标记 ──────────────────────────────────────────────────

def _auto_recompute_config(now: datetime) -> tuple[bool, timedelta]:
    """自动重算开关与等待时长；读取失败时保持既有默认（开启 / 30 分钟）。"""
    enabled_raw = db.load_app_setting(common.PLANNING_AUTO_RECOMPUTE_ENABLED_KEY)
    enabled = True if not isinstance(enabled_raw, bool) else enabled_raw
    wait_raw = db.load_app_setting(common.PLANNING_AUTO_RECOMPUTE_WAIT_KEY)
    minutes = (wait_raw if isinstance(wait_raw, int) and 1 <= wait_raw <= 1440
               else int(common.RECOMPUTE_WAIT.total_seconds() // 60))
    return enabled, timedelta(minutes=minutes)


def request_recompute(reason: str, now: datetime | None = None) -> None:
    now = now or runtime._now()
    enabled, _ = _auto_recompute_config(now)
    if not enabled:
        # 关闭自动重算（需求 16.3）：顺序仍保存，但不进入「等待自动重算」状态。
        return
    client = runtime._require_client()
    # 批次 6 收尾（A1）：消费身份由数据库原子生成（request_token uuid）。
    # requested_at 来自业务 now，两次业务操作可能捕获同一时间戳 T——它不能
    # 充当请求身份；token 在 RPC 函数体内生成，同一时间戳的两次登记必然
    # 得到不同的消费身份。
    client.rpc("planning_request_recompute", {
        "p_reason": reason[:100],
        "p_requested_at": common._iso(now),
    }).execute()


def _request_recompute_quietly(reason: str, now: datetime) -> None:
    """主业务提交成功后的排程请求登记（post-commit side effect）。

    覆盖两类入口：任务停用 / 废弃（批次 6 收尾 BUG B）与拆分待办
    （复审 R3，清单 #27）。两者的主业务结果（任务已停用、开放实例已
    关闭、新单次待办已创建）在 RPC 事务内提交成功后，
    ``request_recompute`` 登记失败不得伪装成主业务失败（用户已看到
    成功），但也不得静默假装后续工作完成——记警告日志保留可观测性，
    失败时由用户手动重算 / 后续维护循环兜底（与 ``_generate_due_quietly``
    同一 quiet 语义）。
    """
    try:
        request_recompute(reason, now)
    except Exception as exc:
        log.warning(
            "planning 主业务提交后重算请求登记失败（等待手动重算或后续触发兜底）: reason=%s error=%s",
            reason, type(exc).__name__,
        )


def clear_recompute_mark(now: datetime | None = None,
                         expected_request_token: str | None = None) -> None:
    """清除重算等待标记；提供 ``expected_request_token`` 时仅条件清除。

    批次 6 收尾（BUG A → A1 升级）：一次 recompute 只能消费它**开始时**
    捕获的那一版请求。消费身份是数据库原子生成的 ``request_token``——
    requested_at 来自业务 now，两次业务操作可能捕获同一时间戳 T，等值
    条件会把执行期间并发登记的同 T 新请求一并清掉；token 等值条件命中
    0 行，新请求保留给下一轮消费。
    ``expected_request_token`` 为 None（捕获时本无待处理请求）时不清除：
    执行期间到达的新请求同样必须保留。
    """
    now = now or runtime._now()
    if expected_request_token is None:
        return
    client = runtime._require_client()
    client.rpc("planning_clear_recompute_mark", {
        "p_request_token": expected_request_token,
    }).execute()


def get_recompute_state(now: datetime | None = None) -> dict[str, Any]:
    now = now or runtime._now()
    enabled, wait = _auto_recompute_config(now)
    client = runtime._require_client()
    rows = runtime._rows(client, "planning_recompute_state", lambda q: q.eq("id", 1).limit(1))
    requested_at = rows[0].get("requested_at") if rows else None
    request_token = rows[0].get("request_token") if rows else None
    pending = bool(requested_at) and enabled
    wait_minutes = None
    if pending:
        requested = common._parse_dt(requested_at, "requested_at")
        wait_minutes = max(0, round((wait - (now - requested)).total_seconds() / 60))
    return {
        "pending": pending,
        # 自动重算开关随状态返回：关闭时前端不得提示「等待自动重算」（需求 16.3）
        "enabled": enabled,
        "requested_at": requested_at,
        # 消费身份（A1）：内部条件清除使用，不进入前端展示契约
        "request_token": request_token,
        "reason": rows[0].get("reason") if rows else None,
        "wait_minutes": wait_minutes,
    }


def trigger_recompute(now: datetime | None = None) -> dict[str, Any]:
    """手动重算：立即执行；仅零冲突（成功）时清空等待标记（§16.2 / §19.1）。

    成功判定只看 conflicts 是否为空：updated=0（无可修改但合法完成）同样
    属于成功；存在冲突则本轮整体未生效，等待标记保留，不新增状态或重试
    机制——后续触发 / 维护循环按既有语义再次执行。
    批次 6 收尾（BUG A → A1 升级）：清除以开始时捕获的 request_token 为
    条件——手动重算执行期间并发产生的新请求（如拖动排序，即使其业务时间
    与捕获值相同）不由本次消费，保留给下一轮。
    """
    now = now or runtime._now()
    request_token = get_recompute_state(now).get("request_token")
    result = recompute_today(now)
    # 最终 Debug（问题 3）：stale_skipped = 本次计算基于旧快照、结果被部分
    # 放弃（如并发 save_order 改序）——并发产生的新重算请求必须保留。
    if not result.get("conflicts") and not result.get("stale_skipped"):
        clear_recompute_mark(now, expected_request_token=request_token)
    return result


# ── 排列保存 ──────────────────────────────────────────────────────

def save_order(ordered_ids: list[int], now: datetime | None = None) -> dict[str, Any]:
    now = now or runtime._now()
    today = cycles._current_cycle(now).key
    if not isinstance(ordered_ids, list) or not all(isinstance(v, int) for v in ordered_ids):
        raise common.PlanningError("invalid_payload", "order must be an array of occurrence ids")
    if len(set(ordered_ids)) != len(ordered_ids):
        raise common.PlanningError("invalid_payload", "order contains duplicate ids")
    client = runtime._require_client()
    open_rows = runtime._rows(
        client, "planning_occurrence",
        lambda q: q.eq("display_cycle_date", today.isoformat()).in_("status", list(common.OPEN_STATUSES)),
    )
    by_id = {row["id"]: row for row in open_rows}
    unknown = [occ_id for occ_id in ordered_ids if occ_id not in by_id]
    if unknown:
        raise common.PlanningError("invalid_payload", "order includes occurrences outside today's open list")
    missing = [occ_id for occ_id in by_id if occ_id not in set(ordered_ids)]
    if missing:
        raise common.PlanningError("invalid_payload", "order must include every open occurrence of today")

    # 中空待办的结束阶段必须排在其开始阶段之后。
    position = {occ_id: index for index, occ_id in enumerate(ordered_ids)}
    for occ_id, occ in by_id.items():
        if occ.get("phase") == "end":
            start_occ = next(
                (row for row in open_rows
                 if row["task_id"] == occ["task_id"]
                 and row.get("round_key") == occ.get("round_key")
                 and row.get("phase_group") == occ.get("phase_group")
                 and row.get("phase") == "start"),
                None,
            )
            if start_occ and position[occ_id] < position[start_occ["id"]]:
                raise common.PlanningError("invalid_payload", "hollow end phase must stay after its start phase")

    for occ_id, index in position.items():
        client.table("planning_occurrence").update({
            "sort_order": index, "updated_at": common._iso(now),
        }).eq("id", occ_id).execute()
    request_recompute("reorder", now)
    return {"saved": len(ordered_ids)}


def save_order_from_payload(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict) or "order" not in payload:
        raise common.PlanningError("invalid_payload", "order is required")
    return save_order(payload["order"], now)


def _manual_estimate_patch(
    occ: dict[str, Any], task: dict[str, Any], payload: dict[str, Any], now: datetime,
) -> dict[str, Any]:
    """Validate and persist a user time edit or explicit release as one row update."""
    start = common._parse_dt(payload["est_start"], "est_start") if payload.get("est_start") else None
    if "est_start" not in payload:
        start = common._parse_dt(occ["est_start"], "est_start") if occ.get("est_start") else None
    end = common._parse_dt(payload["est_end"], "est_end") if payload.get("est_end") else None
    if "est_end" not in payload:
        if "est_start" in payload:
            end = start + common._duration_of(occ, task) if start else None
        else:
            end = common._parse_dt(occ["est_end"], "est_end") if occ.get("est_end") else None
    if start is None and end is not None:
        raise common.PlanningError("invalid_payload", "预估结束时间不能脱离开始时间", 400)
    if start is not None and end is None and "est_end" in payload:
        # 显式置空结束的半区间（#3）：与上一条互为镜像的中文业务拒绝——
        # 省略 est_end 的请求不受影响（仍按预计耗时自动补终点）；不放开
        # 半区间，也不把领域 ValueError 泄漏成英文技术错误。
        raise common.PlanningError(
            "invalid_payload",
            "预估开始时间不能脱离结束时间：只修改开始时间时请省略结束时间，"
            "由系统按预计耗时自动补齐", 400,
        )
    if start is not None and end is not None and end <= start:
        raise common.PlanningError("invalid_payload", "预估结束时间必须晚于开始时间", 400)
    explicit_release = payload.get("is_fixed") is False
    if explicit_release and any(key in payload for key in ("est_start", "est_end")):
        raise common.PlanningError("invalid_payload", "修改预估时间时不能同时取消固定", 400)
    if explicit_release and occ.get("fixed_source") == "rule":
        raise common.PlanningError("rule_fixed", "规则固定时间须通过修改任务定义调整", 409)
    if payload.get("is_fixed") is True and start is None:
        raise common.PlanningError("invalid_payload", "人工固定必须提供有效预估时间", 400)
    if explicit_release and occ.get("fixed_source") == "manual":
        patch = common._estimate_patch(None, None, source="unassigned")
        patch["nominal_start"] = None
    elif start is None:
        patch = common._estimate_patch(None, None, source="unassigned")
    elif explicit_release:
        # An already automatic estimate stays automatic; release never invents
        # scheduler provenance for a rule or manual time.
        patch = common._estimate_patch(start, end, source=occ.get("estimated_time_source") or "automatic")
    else:
        patch = common._estimate_patch(start, end, source="manual", fixed_source="manual")
        patch["nominal_start"] = common._iso(start)
    if "est_start" in payload and start is not None:
        original = date.fromisoformat(occ["schedule_date"])
        cycle = cycles._current_cycle(start).key
        display = max(date.fromisoformat(occ["display_cycle_date"]), cycles._current_cycle(now).key, cycle)
        if display.isoformat() != occ["display_cycle_date"]:
            patch["display_cycle_date"] = display.isoformat()
            patch["display_reason"] = "manual_defer" if display > original else "initial"
    patch["updated_at"] = common._iso(now)
    return patch
