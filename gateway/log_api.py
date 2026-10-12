"""网关报错日志只读查询 API（§8 契约）。

* Bearer ``GATEWAY_TOKEN`` + hmac 恒时比较；未配置管理 token 时拒绝访问；
* 参数白名单校验（级别 / 模块 / 时间区间 / cursor 格式 / 页大小）；
* 列表不含完整堆栈，详情单独请求；
* 存储未就绪返回中文错误 + 稳定 ``error_code``，绝不 ``200 + []`` 伪装成功；
* 查询自身失败限频记录一条安全事件（不无限进入日志写入链）。
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import log_store
from .config import cfg
from .logging_events import current_request_id, suppress_boundary_for_current_request

log = logging.getLogger("gateway.log_api")

_LEVELS = ("WARNING", "ERROR", "CRITICAL")
_MODULE_RE = re.compile(r"^[a-z][a-z_]{0,31}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
# TASK-02：完整密钥标识（kh1- + 32 位十六进制）。
_KEY_REF_RE = re.compile(r"^kh1-[0-9a-f]{32}$")
# BUG-05：pg_snapshot 文本（xmin:xmax:xip 列表）。
_VISIBLE_SNAPSHOT_RE = re.compile(r"^\d+:\d+:(\d+(,\d+)*)?$")
_EVENT_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SEARCH_RE = re.compile(r"[\x00-\x1f\x7f]")
# BUG-05：已看集合分页的会话上限（cursor 长度有界；超出按 400 引导刷新）。
_MAX_SEEN_FINGERPRINTS = 1000
# 无时区的时间按 Asia/Shanghai 解释（页面本地时区），带时区原样解析。
_CST = timezone(timedelta(hours=8))

# 查询自身失败的限频记录：60 秒最多一条安全事件。
_self_log_lock = threading.Lock()
_self_log_last = 0.0


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(
    message: str, status: int = 400, code: str = "invalid_request",
) -> JSONResponse:
    body: dict[str, Any] = {"error": message, "error_code": code}
    request_id = current_request_id()
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(body, status_code=status)


def _self_log_failure(stage: str, exc: Exception) -> None:
    """查询链自身失败：限频记录一条（不无限进入日志写入链）。"""
    global _self_log_last
    with _self_log_lock:
        now = time.monotonic()
        if now - _self_log_last < 60.0:
            return
        _self_log_last = now
    log.exception("日志查询自身失败: stage=%s type=%s", stage, type(exc).__name__)


def _store_error_response(exc: Exception) -> JSONResponse:
    # BUG-12：查询链自身失败（含限频被抑制的请求）一律置位——本请求 5xx
    # 已由内部事件解释，边界兜底不得随查询次数放大；所有失败类型都过
    # 限频内部事件，恢复后真实采集与查询不受影响。
    suppress_boundary_for_current_request()
    _self_log_failure("store", exc)
    if isinstance(exc, log_store.LogStoreUnavailable):
        return _error(
            "日志查询暂不可用，请检查服务或数据库状态",
            503, "log_store_unavailable",
        )
    if isinstance(exc, log_store.LogSchemaMissing):
        return _error(
            "日志数据表尚未创建，请先执行网关日志迁移",
            503, "log_schema_missing",
        )
    return _error(
        "日志查询失败：服务暂时出现异常，请稍后重试",
        500, "internal_error",
    )


def _parse_time(value: str | None, name: str) -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise _Invalid(f"{name} 不是合法的 ISO 时间（示例 2026-10-10T08:00:00+08:00）")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CST)
    return parsed.astimezone(timezone.utc)


class _Invalid(Exception):
    pass


def _parse_filters(request: Request) -> dict[str, Any]:
    params = request.query_params
    level = (params.get("level") or "").strip().upper()
    if level and level not in _LEVELS:
        raise _Invalid("level 只支持 WARNING / ERROR / CRITICAL")
    module = (params.get("module") or "").strip()
    if module and not _MODULE_RE.match(module):
        raise _Invalid("module 参数无效")
    key_ref = (params.get("key_ref") or "").strip()
    if key_ref and not _KEY_REF_RE.match(key_ref):
        raise _Invalid("key_ref 参数无效")
    start = _parse_time(params.get("start"), "start")
    end = _parse_time(params.get("end"), "end")
    if start and end and start > end:
        raise _Invalid("start 不能晚于 end")
    search = (params.get("search") or "").strip()
    if search:
        search = _SEARCH_RE.sub("", search)[:100]
    return {
        "level": level or None,
        "module": module or None,
        "key_ref": key_ref or None,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "search": search or None,
    }


def _parse_cursor(request: Request) -> tuple[list[str], str | None, str | None]:
    """cursor = snapshot|visible|fp,fp,…（BUG-05：快照 + 可见性 + 已看集合）。

    无 cursor 的第一页由服务端建立新快照（含事务可见性快照，由 RPC 返回
    ``visible_snapshot``）；翻页复用同一快照并排除已看组——已看组的聚合
    在会话内冻结，晚提交的新组按排序位置落入后续页，已看组不重现。已看
    集合有数量上限（查询有界，§8.2）。
    """
    raw = (request.query_params.get("cursor") or "").strip()
    if not raw:
        return [], None, None
    parts = raw.split("|", 2)
    if len(parts) != 3 or not parts[2]:
        raise _Invalid("cursor 格式无效")
    snapshot_raw, visible_raw, seen_raw = parts
    try:
        snapshot = datetime.fromisoformat(snapshot_raw)
    except ValueError:
        raise _Invalid("cursor 时间格式无效") from None
    if snapshot.tzinfo is None:
        snapshot = snapshot.replace(tzinfo=_CST)
    if not _VISIBLE_SNAPSHOT_RE.match(visible_raw):
        raise _Invalid("cursor 可见性快照格式无效")
    seen = [item for item in seen_raw.split(",") if item]
    for fingerprint in seen:
        if not _FINGERPRINT_RE.match(fingerprint):
            raise _Invalid("cursor fingerprint 格式无效")
    if len(seen) > _MAX_SEEN_FINGERPRINTS:
        raise _Invalid("翻页会话过长，请刷新或缩小筛选范围后重新查询")
    return (
        seen,
        snapshot.astimezone(timezone.utc).isoformat(),
        visible_raw,
    )


def _parse_limit(request: Request, default: int = 50, maximum: int = 100) -> int:
    raw = (request.query_params.get("limit") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise _Invalid("limit 必须是整数") from None
    if value < 1 or value > maximum:
        raise _Invalid(f"limit 只支持 1 ~ {maximum}")
    return value


def _parse_snapshot_param(request: Request) -> str | None:
    """详情请求携带列表会话的入库快照（BUG-05/06）；非法值按 400 拒绝。"""
    raw = (request.query_params.get("snapshot") or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        raise _Invalid("snapshot 不是合法的 ISO 时间") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CST)
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_visible_param(request: Request) -> str | None:
    """详情请求携带列表会话的事务可见性快照（BUG-05）；非法值按 400 拒绝。"""
    raw = (request.query_params.get("visible") or "").strip()
    if not raw:
        return None
    if not _VISIBLE_SNAPSHOT_RE.match(raw):
        raise _Invalid("visible 不是合法的可见性快照")
    return raw


async def logs_list(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    try:
        filters = _parse_filters(request)
        seen, snapshot, visible = _parse_cursor(request)
        limit = _parse_limit(request)
    except _Invalid as exc:
        return _error(str(exc), 400, "invalid_request")
    if snapshot is None:
        # 第一页建立入库快照（BUG-05）：本次翻页会话内聚合范围冻结；
        # 事务可见性快照由 RPC 在同一事务内取回（visible_snapshot）。
        snapshot = datetime.now(timezone.utc).isoformat()
    try:
        result = await asyncio.to_thread(
            log_store.query_groups,
            level=filters["level"],
            module=filters["module"],
            start=filters["start"],
            end=filters["end"],
            search=filters["search"],
            limit=limit,
            seen=seen,
            snapshot=snapshot,
            visible=visible,
            key_ref=filters["key_ref"],
        )
    except Exception as exc:
        return _store_error_response(exc)
    groups = result.get("groups") or []
    has_more = bool(result.get("has_more"))
    # BUG-05：RPC 返回本事务的可见性快照；翻页 / 详情随会话回传。
    visible_snapshot = result.get("visible_snapshot") or visible or ""
    next_cursor = None
    if has_more and groups:
        # BUG-05：cursor = 快照 + 可见性快照 + 已看集合（本页组并入已看，
        # 后续页排除）。pg_snapshot 文本含 ':' 与 ','，置于第二段。
        page_seen = seen + [g.get("fingerprint") for g in groups if g.get("fingerprint")]
        next_cursor = f"{snapshot}|{visible_snapshot}|{','.join(page_seen)}"
    return JSONResponse(
        {
            "groups": groups,
            "has_more": has_more,
            "next_cursor": next_cursor,
            "snapshot": snapshot,
            "visible_snapshot": visible_snapshot,
            "filters": filters,
            "request_id": current_request_id(),
        }
    )


async def logs_meta(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    try:
        filters = _parse_filters(request)
    except _Invalid as exc:
        return _error(str(exc), 400, "invalid_request")
    try:
        facets = await asyncio.to_thread(
            log_store.facets, start=filters["start"], end=filters["end"]
        )
    except Exception as exc:
        return _store_error_response(exc)
    from . import logging_events

    return JSONResponse(
        {
            "modules": facets.get("modules") or [],
            "levels": facets.get("levels") or [],
            # TASK-02：密钥筛选选项（完整 key_ref + 用途 / 上游 / 计数）。
            "keys": facets.get("keys") or [],
            "total_events": facets.get("total_events") or 0,
            "oldest_at": facets.get("oldest_at"),
            "newest_at": facets.get("newest_at"),
            "collection": logging_events.collection_status(),
            "request_id": current_request_id(),
        }
    )


async def logs_event_detail(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    event_id = request.path_params.get("event_id", "")
    if not _EVENT_ID_RE.match(str(event_id)):
        return _error("event_id 格式无效", 400, "invalid_request")
    try:
        event = await asyncio.to_thread(log_store.get_event, str(event_id))
    except Exception as exc:
        return _store_error_response(exc)
    if not event:
        return _error("日志记录不存在", 404, "not_found")
    return JSONResponse({"event": event, "request_id": current_request_id()})


async def logs_group_detail(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    fingerprint = (request.query_params.get("fingerprint") or "").strip()
    if not _FINGERPRINT_RE.match(fingerprint):
        return _error("fingerprint 格式无效", 400, "invalid_request")
    try:
        filters = _parse_filters(request)
        sample_limit = _parse_limit(request, default=20, maximum=50)
        snapshot = _parse_snapshot_param(request)
        visible = _parse_visible_param(request)
    except _Invalid as exc:
        return _error(str(exc), 400, "invalid_request")
    try:
        result = await asyncio.to_thread(
            log_store.get_group,
            fingerprint,
            start=filters["start"],
            end=filters["end"],
            level=filters["level"],
            module=filters["module"],
            search=filters["search"],
            snapshot=snapshot,
            visible=visible,
            key_ref=filters["key_ref"],
            sample_limit=sample_limit,
        )
    except Exception as exc:
        return _store_error_response(exc)
    group = (result or {}).get("group") or {}
    if not group or not group.get("event_count"):
        return _error("日志记录不存在", 404, "not_found")
    return JSONResponse(
        {
            "group": group,
            "samples": (result or {}).get("samples") or [],
            "sample_limit": sample_limit,
            "filters": filters,
            "snapshot": snapshot,
            "request_id": current_request_id(),
        }
    )


# 静态路由先注册，避免被参数路由吞掉（§8.1）。
log_api_routes = [
    Route("/admin/api/logs/meta", logs_meta, methods=["GET"]),
    Route("/admin/api/logs/event/{event_id}", logs_event_detail, methods=["GET"]),
    Route("/admin/api/logs/group", logs_group_detail, methods=["GET"]),
    Route("/admin/api/logs", logs_list, methods=["GET"]),
]
