"""网关错误事件持久化（Supabase ``gateway_error_events`` 表读写）。

只被 :mod:`gateway.logging_events` 的后台 writer（写入 / 清理 / spool
补写）与 :mod:`gateway.log_api`（只读查询）调用；业务代码不直接使用。
所有异常向上抛出，由调用方按隔离规则处理，绝不影响业务请求。
"""

from __future__ import annotations

import logging
from typing import Any

from . import db

log = logging.getLogger("gateway.log_store")  # 采集排除名单内：防递归

TABLE = "gateway_error_events"

# 写入行白名单：与迁移列一一对应，未知字段一律丢弃。commit_xid 由数据库
# 默认值（pg_current_xact_id()）生成，不由客户端提供。key_ref /
# credential_role / provider 对旧 spool 事件缺失时按空串兼容（TASK-02）。
_ROW_FIELDS = (
    "event_id", "occurred_at", "level", "module", "operation", "error_code",
    "message_zh", "exception_type", "message_raw", "stack_trace",
    "logger_name", "source", "request_id", "operation_id", "route", "method",
    "http_status", "upstream_status", "outcome", "instance_id", "release_id",
    "safe_context", "fingerprint", "truncated_fields",
    "key_ref", "credential_role", "provider",
)
_ROW_TEXT_DEFAULTS = frozenset({"key_ref", "credential_role", "provider"})


class LogStoreUnavailable(RuntimeError):
    """Supabase 客户端不可用（未配置 / 探测失败）。"""


class LogSchemaMissing(RuntimeError):
    """日志表或 RPC 尚未创建（迁移未执行）。"""


# 明确的「对象不存在」SQLSTATE：42P01 未定义表、42703 未定义列、
# 42883 未定义函数；PGRST202 / PGRST204 是 PostgREST schema 缓存未命中。
_SCHEMA_MISSING_CODES = frozenset({"42P01", "42703", "42883", "PGRST202", "PGRST204"})
# 明确的非缺迁移证据（权限 / 连接 / 超时等）：优先排除，防止把权限错误
# 误诊成缺迁移（BUG-17）。
_NON_SCHEMA_MARKERS = (
    "permission denied",
    "timed out",
    "timeout",
    "connection refused",
    "connection reset",
    "went away",
    "ssl",
)


def _error_code(exc: Exception) -> str:
    for attr in ("sqlstate", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_schema_missing(exc: Exception) -> bool:
    """可可靠识别的「迁移未执行」证据；不能把所有失败都说成缺迁移。

    只认明确的缺表 / 缺列 / 缺函数错误码与缺失语义文本（BUG-17）；
    对象名本身（gateway_error_events / gateway_log_*）出现在权限拒绝等
    错误里不构成缺失证据。
    """
    code = _error_code(exc)
    if code:
        return code in _SCHEMA_MISSING_CODES
    text = str(exc).casefold()
    if any(marker in text for marker in _NON_SCHEMA_MARKERS):
        return False
    markers = (
        "could not find the",       # PostgREST 不识别表 / 函数
        "schema cache",             # PostgREST schema 缓存
        "does not exist",
    )
    return any(marker in text for marker in markers)


def _client():
    client = db.get_client()
    if client is None:
        raise LogStoreUnavailable("supabase client unavailable")
    return client


def _wrap(exc: Exception) -> Exception:
    if isinstance(exc, (LogStoreUnavailable, LogSchemaMissing)):
        return exc
    if _is_schema_missing(exc):
        return LogSchemaMissing(type(exc).__name__)
    return exc


def insert_events(events: list[dict[str, Any]]) -> int:
    """批量幂等写入：同 ``event_id`` 重试不重复计数（ON CONFLICT DO NOTHING）。"""
    client = _client()
    rows = []
    for event in events:
        row = {key: event.get(key) for key in _ROW_FIELDS}
        for key in _ROW_TEXT_DEFAULTS:
            if row.get(key) is None:
                row[key] = ""  # 旧 spool 事件缺新字段：空串（未关联密钥）
        rows.append(row)
    if not rows:
        return 0
    try:
        client.table(TABLE).upsert(
            rows, on_conflict="event_id", ignore_duplicates=True
        ).execute()
    except Exception as exc:
        raise _wrap(exc) from exc
    return len(rows)


def query_groups(
    *,
    level: str | None = None,
    module: str | None = None,
    start: str | None = None,
    end: str | None = None,
    search: str | None = None,
    limit: int = 50,
    seen: list[str] | None = None,
    snapshot: str | None = None,
    visible: str | None = None,
    key_ref: str | None = None,
) -> dict[str, Any]:
    """按 fingerprint 汇总查询（查询时聚合，原始事件表为唯一事实来源）。

    ``snapshot`` 是入库时间上限（BUG-05）：一次翻页会话内冻结，快照之后
    新增 / spool 延迟补写的事件不进入本次会话。``visible`` 是会话的事务
    可见性快照（pg_snapshot 文本，第一页由 RPC 返回 ``visible_snapshot``
    建立）：已看组的聚合在会话内冻结，晚提交的新组仍会落入后续页。
    ``seen`` 是本会话已看 fingerprint 集合：已看组在后续页不重现。
    ``key_ref`` 按完整密钥标识筛选（TASK-02）。
    """
    client = _client()
    params = {
        "p_level": level or None,
        "p_module": module or None,
        "p_start": start or None,
        "p_end": end or None,
        "p_search": search or None,
        "p_limit": limit,
        "p_seen": ",".join(seen) if seen else None,
        "p_snapshot": snapshot or None,
        "p_visible": visible or None,
        "p_key_ref": key_ref or None,
    }
    try:
        response = client.rpc("gateway_log_query", params).execute()
    except Exception as exc:
        raise _wrap(exc) from exc
    data = response.data
    if isinstance(data, dict):
        return data
    return {"groups": [], "has_more": False}


def get_event(event_id: str) -> dict[str, Any] | None:
    client = _client()
    try:
        response = client.rpc("gateway_log_event", {"p_event_id": event_id}).execute()
    except Exception as exc:
        raise _wrap(exc) from exc
    data = response.data
    if isinstance(data, dict):
        return data.get("event")
    return None


def get_group(
    fingerprint: str,
    *,
    start: str | None = None,
    end: str | None = None,
    level: str | None = None,
    module: str | None = None,
    search: str | None = None,
    snapshot: str | None = None,
    visible: str | None = None,
    key_ref: str | None = None,
    sample_limit: int = 20,
) -> dict[str, Any] | None:
    """汇总组详情：复用列表的完整筛选与快照（BUG-06）。

    列表按 level / search / key_ref 过滤后，详情对同一 fingerprint 应用
    相同条件与同一会话可见性快照（BUG-05），次数与样本和列表一致，
    不再静默扩大到整组。
    """
    client = _client()
    params = {
        "p_fingerprint": fingerprint,
        "p_start": start or None,
        "p_end": end or None,
        "p_level": level or None,
        "p_module": module or None,
        "p_search": search or None,
        "p_snapshot": snapshot or None,
        "p_sample_limit": sample_limit,
        "p_visible": visible or None,
        "p_key_ref": key_ref or None,
    }
    try:
        response = client.rpc("gateway_log_group", params).execute()
    except Exception as exc:
        raise _wrap(exc) from exc
    data = response.data
    if not isinstance(data, dict):
        return None
    return data


def facets(start: str | None = None, end: str | None = None) -> dict[str, Any]:
    client = _client()
    params = {"p_start": start or None, "p_end": end or None}
    try:
        response = client.rpc("gateway_log_facets", params).execute()
    except Exception as exc:
        raise _wrap(exc) from exc
    data = response.data
    if isinstance(data, dict):
        return data
    return {"modules": [], "levels": [], "total_events": 0}


def cleanup(retention_days: int) -> dict[str, Any]:
    """按真实 ``occurred_at`` 分批清理；RPC 内 advisory xact 锁保证多实例互斥。"""
    client = _client()
    try:
        response = client.rpc(
            "gateway_log_cleanup",
            {"p_retention_days": retention_days, "p_batch_size": 5000},
        ).execute()
    except Exception as exc:
        raise _wrap(exc) from exc
    data = response.data
    if isinstance(data, dict):
        return data
    return {"status": "unknown", "deleted": 0}
