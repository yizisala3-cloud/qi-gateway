"""Token-protected control plane for memory digest preview and execution."""
from __future__ import annotations

import asyncio
import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from .memory_extract import get_digest_status, list_digest_runs, run_memory_digest

log = logging.getLogger("gateway.memory_digest_api")


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def digest_status(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)
    try:
        result = await asyncio.to_thread(get_digest_status)
        return JSONResponse(result)
    except Exception as exc:
        log.exception("Failed to read digest status")
        return _error(f"{type(exc).__name__}: {str(exc)[:500]}", 500)


async def digest_runs(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "30"))))
        result = await asyncio.to_thread(list_digest_runs, limit)
        return JSONResponse({"data": result})
    except Exception as exc:
        log.exception("Failed to read digest runs")
        return _error(f"{type(exc).__name__}: {str(exc)[:500]}", 500)


async def _run_digest(request: Request, trigger: str, mode: str):
    if not _authorized(request):
        return _error("unauthorized", 401)
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        max_messages = body.get("max_messages") if isinstance(body, dict) else None
        if max_messages is not None:
            max_messages = max(1, min(100, int(max_messages)))
        result = await asyncio.to_thread(run_memory_digest, trigger, mode, max_messages)
        return JSONResponse(result)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        log.exception("Memory digest request failed")
        return _error(f"{type(exc).__name__}: {str(exc)[:500]}", 500)


async def digest_preview(request: Request):
    return await _run_digest(request, "manual_preview", "preview")


async def digest_execute(request: Request):
    return await _run_digest(request, "manual_execute", "execute")


memory_digest_routes = [
    Route("/admin/api/memory-digest/status", digest_status, methods=["GET"]),
    Route("/admin/api/memory-digest/runs", digest_runs, methods=["GET"]),
    Route("/admin/api/memory-digest/preview", digest_preview, methods=["POST"]),
    Route("/admin/api/memory-digest/execute", digest_execute, methods=["POST"]),
]
