"""Purpose-built admin endpoints for the memory authoring lifecycle.

These routes are the only write path the admin dashboard uses for creating,
editing, re-classifying, undoing, and restoring formal memories. They are
protected by GATEWAY_TOKEN only: the MCP memory token deliberately has no
access here. Business conflicts return stable ``error_code`` values with
Chinese messages so the frontend never has to parse database errors.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from .admin_memory import (
    AdminMemoryError,
    change_memory_type,
    create_admin_memory,
    edit_admin_memory,
    restore_archived_memory,
    undo_memory_type_change,
)

log = logging.getLogger("gateway.admin_memory_api")
MAX_REQUEST_BYTES = 16_384


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error_code": code, "error": message},
        status_code=status_code,
    )


async def _read_json(request: Request) -> dict[str, Any] | None:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return None
        except ValueError:
            return None
    try:
        payload = await request.json()
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


async def create_memory_entry(request: Request) -> JSONResponse:
    """Create a user-authored formal memory straight into formal storage."""
    if not _authorized(request):
        return _error("unauthorized", "无效的网关 Token", 401)
    payload = await _read_json(request)
    if payload is None:
        return _error("invalid_json", "请求体必须是合法的 JSON 对象", 400)
    try:
        result = await asyncio.to_thread(create_admin_memory, payload)
        return JSONResponse({"success": True, **result}, status_code=201)
    except AdminMemoryError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception("admin memory create failed: error=%s", type(exc).__name__)
        return _error("internal_error", "新增记忆失败，请稍后重试", 500)


async def edit_memory_entry(request: Request) -> JSONResponse:
    """Full ordinary-field edit of a current formal memory."""
    if not _authorized(request):
        return _error("unauthorized", "无效的网关 Token", 401)
    payload = await _read_json(request)
    if payload is None:
        return _error("invalid_json", "请求体必须是合法的 JSON 对象", 400)
    memory_id = request.path_params["memory_id"]
    try:
        result = await asyncio.to_thread(edit_admin_memory, memory_id, payload)
        return JSONResponse({"success": True, **result})
    except AdminMemoryError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception(
            "admin memory edit failed: memory_id=%s error=%s", memory_id, type(exc).__name__
        )
        return _error("internal_error", "保存修改失败，请稍后重试", 500)


async def change_memory_type_entry(request: Request) -> JSONResponse:
    """Versioned continuity-class change: new version plus cleanup, atomically."""
    if not _authorized(request):
        return _error("unauthorized", "无效的网关 Token", 401)
    payload = await _read_json(request)
    if payload is None:
        return _error("invalid_json", "请求体必须是合法的 JSON 对象", 400)
    memory_id = request.path_params["memory_id"]
    try:
        result = await asyncio.to_thread(change_memory_type, memory_id, payload)
        return JSONResponse({"success": True, **result}, status_code=201)
    except AdminMemoryError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception(
            "admin memory type change failed: memory_id=%s error=%s",
            memory_id, type(exc).__name__,
        )
        return _error("internal_error", "类型修改失败，请稍后重试", 500)


async def undo_type_change_entry(request: Request) -> JSONResponse:
    """Undo the most recent type change of the current version."""
    if not _authorized(request):
        return _error("unauthorized", "无效的网关 Token", 401)
    memory_id = request.path_params["memory_id"]
    try:
        result = await asyncio.to_thread(undo_memory_type_change, memory_id)
        return JSONResponse({"success": True, **result})
    except AdminMemoryError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception(
            "admin memory undo failed: memory_id=%s error=%s", memory_id, type(exc).__name__
        )
        return _error("internal_error", "撤销失败，请稍后重试", 500)


async def restore_memory_entry(request: Request) -> JSONResponse:
    """Restore a naturally archived memory after conflict checks."""
    if not _authorized(request):
        return _error("unauthorized", "无效的网关 Token", 401)
    memory_id = request.path_params["memory_id"]
    try:
        result = await asyncio.to_thread(restore_archived_memory, memory_id)
        return JSONResponse({"success": True, **result})
    except AdminMemoryError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception(
            "admin memory restore failed: memory_id=%s error=%s", memory_id, type(exc).__name__
        )
        return _error("internal_error", "恢复失败，请稍后重试", 500)


admin_memory_routes = [
    Route("/admin/api/memories/manual", create_memory_entry, methods=["POST"]),
    Route("/admin/api/memories/{memory_id:int}/edit", edit_memory_entry, methods=["POST"]),
    Route("/admin/api/memories/{memory_id:int}/change-type", change_memory_type_entry, methods=["POST"]),
    Route("/admin/api/memories/{memory_id:int}/undo-type-change", undo_type_change_entry, methods=["POST"]),
    Route("/admin/api/memories/{memory_id:int}/restore", restore_memory_entry, methods=["POST"]),
]
