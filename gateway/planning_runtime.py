"""Planning runtime seams and basic database adapters.

All services share this clock, client provider and reentrant maintenance lock.
Tests replace attributes here, rather than aliases exposed by the public facade."""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from .db import get_client
from . import planning_common as common


# 可重入锁（最终修复问题 3）：既做维护循环互斥，也做 once 身份编辑与
# 全部生成入口的任务级互斥——update_task 持锁期间调用
# _generate_due_quietly 靠可重入避免自锁。
_maintenance_lock = threading.RLock()


# ── 时间工具 ──────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(common._CST)


# ── 数据库访问 ────────────────────────────────────────────────────

def _require_client():
    client = get_client()
    if not client:
        raise common.PlanningError("database_unavailable", "Supabase is not configured", 503)
    return client


def _rows(client, table: str, query_fn=None) -> list[dict[str, Any]]:
    query = client.table(table).select("*")
    if query_fn:
        query = query_fn(query)
    response = query.execute()
    return response.data or []


def _fetch_task(client, task_id: int) -> dict[str, Any] | None:
    rows = _rows(client, "planning_task", lambda q: q.eq("id", task_id).limit(1))
    return rows[0] if rows else None


def _fetch_occurrence(client, occurrence_id: int) -> dict[str, Any] | None:
    rows = _rows(client, "planning_occurrence", lambda q: q.eq("id", occurrence_id).limit(1))
    return rows[0] if rows else None


def _task_map(client, task_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not task_ids:
        return {}
    rows = _rows(client, "planning_task", lambda q: q.in_("id", sorted(task_ids)))
    return {row["id"]: row for row in rows}


def _discard_task_atomically(client, task_id: int, now: datetime,
                             target_id: int | None = None,
                             target_patch: dict[str, Any] | None = None,
                             creation_digest: str | None = None) -> dict[str, Any]:
    """删除整个任务 = 跨 task + occurrence 的原子命令（§25，2026-10-07）：
    `planning_discard_task` 在单个数据库事务内锁任务行 → 按 id 升序锁全部
    实例行 → 锁内按执行事实判定（有完成 / 部分完成事实者收口开放实例并
    保留全部历史；无事实者物理删除任务与实例并登记创建请求身份）；任一
    失败整体回滚。返回 ``{deleted, history_preserved, already_deleted,
    closed}``。仅已知的并发删除拒绝映射为 409；数据库故障向上传播。

    ``creation_digest``（R09，2026-10-07 复审 #9）：任务行创建快照的规范化
    语义摘要（sha256，调用方按不可变的 ``creation_request_content`` 计算）。
    物理删除分支只把它随请求键登记进 tombstone——业务正文随任务行真正
    删除，登记表不保存完整内容。"""
    try:
        response = client.rpc("planning_discard_task", {
            "p_task_id": task_id, "p_now": common._iso(now),
            "p_target_id": target_id,
            "p_target_patch": target_patch,
            "p_creation_digest": creation_digest,
        }).execute()
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else {}
        return data if isinstance(data, dict) else {}
    except common.PlanningError:
        raise
    except Exception as exc:
        if getattr(exc, "code", None) == common.CONCURRENCY_ERRCODE                 or "task already inactive" in str(exc):
            raise common.PlanningError(
                "concurrent_modified",
                "该待办已被并发操作删除，本次操作未执行", 409,
            ) from exc
        # 基础设施失败（连接 / 非预期约束 / RPC 缺失）必须用户可见，不伪装成功。
        raise common.PlanningError(
            "database_unavailable",
            "删除待办暂时无法完成，请稍后重试", 503,
        ) from exc
