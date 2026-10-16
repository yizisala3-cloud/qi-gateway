"""Token-protected memo API used by the admin dashboard (备忘录一期).

路径约定 ``/admin/api/memo/*``；鉴权与错误结构沿用 planning_api 惯例
（Bearer GATEWAY_TOKEN + hmac.compare_digest，错误返回
``{"error"[, "error_code"]}``）。所有 DB 调用经 ``asyncio.to_thread``，
多行写入全部经 memo_* RPC 单事务完成。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import memo
from .config import cfg

log = logging.getLogger("gateway.memo_api")


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int = 400, code: str | None = None,
           details: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    if code:
        body["error_code"] = code
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


async def _dispatch(request: Request, fn, *args, created: bool = False) -> JSONResponse:
    if not _authorized(request):
        return _error("unauthorized", 401, "unauthorized")
    try:
        result = await asyncio.to_thread(fn, *args)
        return JSONResponse(result, status_code=201 if created else 200)
    except memo.MemoError as exc:
        return _error(exc.message, exc.status_code, exc.code, exc.details)
    except ValueError as exc:
        return _error(f"invalid query or payload value: {exc}", 400, "invalid_payload")
    except Exception as exc:
        log.exception("Memo API failure: %s %s", request.method, request.url.path)
        return _error(f"unexpected memo failure: {type(exc).__name__}", 500, "internal_error")


async def _dispatch_json(request: Request, fn, *args, created: bool = False) -> JSONResponse:
    if not _authorized(request):
        return _error("unauthorized", 401, "unauthorized")
    try:
        payload = await request.json()
    except Exception:
        return _error("request body must be valid JSON", 400, "invalid_json")
    if not isinstance(payload, dict):
        return _error("request body must be a JSON object", 400, "invalid_json")
    return await _dispatch(request, fn, *args, payload, created=created)


async def board(request: Request) -> JSONResponse:
    return await _dispatch(request, memo.get_board)


async def entries_collection(request: Request) -> JSONResponse:
    # GET 路由会自动接收 HEAD（F20）：读取语义必须同时覆盖两者，
    # 写分支只由声明的 POST/PATCH 进入。
    if request.method in ("GET", "HEAD"):
        status = request.query_params.get("status", "active")
        query = request.query_params.get("q")
        return await _dispatch(request, memo.list_entries, status, query)
    return await _dispatch_json(request, memo.create_entry, created=True)


async def entry_item(request: Request) -> JSONResponse:
    entry_id = request.path_params["entry_id"]
    if request.method in ("GET", "HEAD"):
        return await _dispatch(request, memo.get_entry, entry_id)
    return await _dispatch_json(request, memo.update_entry, entry_id)


async def entry_archive(request: Request) -> JSONResponse:
    return await _dispatch_json(
        request, _lifecycle_with_payload, request.path_params["entry_id"], "archive",
    )


async def entry_delete(request: Request) -> JSONResponse:
    return await _dispatch_json(
        request, _lifecycle_with_payload, request.path_params["entry_id"], "delete",
    )


async def entry_restore(request: Request) -> JSONResponse:
    return await _dispatch_json(
        request, _lifecycle_with_payload, request.path_params["entry_id"], "restore",
    )


def _lifecycle_with_payload(entry_id: int, action: str, payload: dict) -> dict:
    return memo.set_entry_lifecycle(
        entry_id, action, payload.get("expected_version"),
    )


async def tags_collection(request: Request) -> JSONResponse:
    if request.method in ("GET", "HEAD"):
        return await _dispatch(request, memo.list_tags)
    return await _dispatch_json(request, memo.create_tag, created=True)


async def tag_item_delete(request: Request) -> JSONResponse:
    return await _dispatch(request, memo.delete_tag, request.path_params["tag_id"])


async def reorder(request: Request) -> JSONResponse:
    return await _dispatch_json(request, memo.reorder)


async def note_mode(request: Request) -> JSONResponse:
    return await _dispatch_json(request, memo.set_note_mode)


memo_api_routes = [
    Route("/admin/api/memo/board", board, methods=["GET"]),
    Route("/admin/api/memo/entries", entries_collection, methods=["GET", "POST"]),
    Route("/admin/api/memo/entries/{entry_id:int}", entry_item, methods=["GET", "PATCH"]),
    Route("/admin/api/memo/entries/{entry_id:int}/archive", entry_archive, methods=["POST"]),
    Route("/admin/api/memo/entries/{entry_id:int}/delete", entry_delete, methods=["POST"]),
    Route("/admin/api/memo/entries/{entry_id:int}/restore", entry_restore, methods=["POST"]),
    Route("/admin/api/memo/tags", tags_collection, methods=["GET", "POST"]),
    Route("/admin/api/memo/tags/{tag_id:int}/delete", tag_item_delete, methods=["POST"]),
    Route("/admin/api/memo/reorder", reorder, methods=["POST"]),
    Route("/admin/api/memo/note-mode", note_mode, methods=["POST"]),
]
