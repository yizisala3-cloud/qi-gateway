"""HTTP API for the OrangeChat todo plugin."""
from __future__ import annotations

import asyncio
import hmac
import logging
import threading
import time
from collections import deque
from typing import Callable

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from .todos import TodoError, cancel_todo, complete_todo, create_todo, list_todos, snooze_todo


log = logging.getLogger("gateway.todo_api")
MAX_REQUEST_BYTES = 32_768
_request_times: deque[float] = deque()
_rate_lock = threading.Lock()


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error_code": code, "error": message},
        status_code=status_code,
    )


def _authorized(request: Request) -> bool:
    configured = cfg.TODO_PLUGIN_TOKEN.strip()
    if not configured:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, configured)


def _within_rate_limit() -> bool:
    now = time.monotonic()
    limit = max(1, min(600, int(cfg.TODO_REQUEST_RATE_LIMIT)))
    with _rate_lock:
        while _request_times and now - _request_times[0] >= 60:
            _request_times.popleft()
        if len(_request_times) >= limit:
            return False
        _request_times.append(now)
        return True


async def _payload(request: Request):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                raise TodoError("payload_too_large", "request body is too large", 413)
        except ValueError as exc:
            raise TodoError("invalid_content_length", "invalid Content-Length", 400) from exc
    try:
        return await request.json()
    except Exception as exc:
        raise TodoError("invalid_json", "request body must be valid JSON", 400) from exc


async def _handle(
    request: Request,
    operation: Callable[..., dict],
    *,
    include_todo_id: bool = False,
    created_status: bool = False,
) -> JSONResponse:
    if not cfg.TODO_PLUGIN_TOKEN.strip():
        return _error("plugin_not_configured", "todo plugin access is not configured", 503)
    if not _authorized(request):
        return _error("unauthorized", "invalid plugin token", 401)
    if not _within_rate_limit():
        return _error("rate_limited", "too many todo requests; retry later", 429)
    try:
        payload = await _payload(request)
        if include_todo_id:
            result = await asyncio.to_thread(operation, request.path_params.get("todo_id"), payload)
        else:
            result = await asyncio.to_thread(operation, payload)
        status_code = 201 if created_status and result.get("created") else 200
        return JSONResponse({"success": True, **result}, status_code=status_code)
    except TodoError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception("Unexpected todo API failure: %s", type(exc).__name__)
        return _error("internal_error", "unexpected todo API failure", 500)


async def create_todo_endpoint(request: Request) -> JSONResponse:
    return await _handle(request, create_todo, created_status=True)


async def list_todos_endpoint(request: Request) -> JSONResponse:
    return await _handle(request, list_todos)


async def complete_todo_endpoint(request: Request) -> JSONResponse:
    return await _handle(request, complete_todo, include_todo_id=True)


async def snooze_todo_endpoint(request: Request) -> JSONResponse:
    return await _handle(request, snooze_todo, include_todo_id=True)


async def cancel_todo_endpoint(request: Request) -> JSONResponse:
    return await _handle(request, cancel_todo, include_todo_id=True)


todo_routes = [
    Route("/v1/todos", create_todo_endpoint, methods=["POST"]),
    Route("/v1/todos/query", list_todos_endpoint, methods=["POST"]),
    Route("/v1/todos/{todo_id}/complete", complete_todo_endpoint, methods=["POST"]),
    Route("/v1/todos/{todo_id}/snooze", snooze_todo_endpoint, methods=["POST"]),
    Route("/v1/todos/{todo_id}/cancel", cancel_todo_endpoint, methods=["POST"]),
]

