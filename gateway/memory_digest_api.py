"""Token-protected control plane for memory digest preview and execution."""
from __future__ import annotations

import asyncio
import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from .memory_continuity_shadow import ShadowPreviewError, run_shadow_preview
from .memory_extract import (
    DigestPipelineError,
    get_digest_status,
    list_digest_runs,
    run_memory_digest,
)

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
    except DigestPipelineError as exc:
        status = 503 if exc.code == "analysis_not_configured" else 422
        return JSONResponse(
            {"error": str(exc), "error_code": exc.code},
            status_code=status,
        )
    except Exception as exc:
        log.exception("Memory digest request failed")
        return _error(f"{type(exc).__name__}: {str(exc)[:500]}", 500)


async def digest_preview(request: Request):
    return await _run_digest(request, "manual_preview", "preview")


async def digest_execute(request: Request):
    return await _run_digest(request, "manual_execute", "execute")


async def continuity_shadow_preview(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            return _error("request body must be a JSON object", 400)

        max_messages = body.get("max_messages", 80)
        max_chars = body.get("max_chars", 16000)
        if isinstance(max_messages, bool) or not isinstance(max_messages, int):
            return _error("max_messages must be an integer between 1 and 100", 400)
        if isinstance(max_chars, bool) or not isinstance(max_chars, int):
            return _error("max_chars must be an integer between 2000 and 24000", 400)
        if not 1 <= max_messages <= 100:
            return _error("max_messages must be between 1 and 100", 400)
        if not 2000 <= max_chars <= 24000:
            return _error("max_chars must be between 2000 and 24000", 400)

        result = await asyncio.to_thread(run_shadow_preview, max_messages, max_chars)
        return JSONResponse(result)
    except ShadowPreviewError as exc:
        status = 503 if exc.code in {
            "analysis_not_configured", "database_unavailable", "source_read_failed",
        } else 422
        return JSONResponse(
            {"error": str(exc), "error_code": exc.code},
            status_code=status,
        )
    except Exception:
        log.exception("Continuity shadow preview failed")
        return JSONResponse(
            {"error": "Continuity shadow preview failed", "error_code": "shadow_preview_error"},
            status_code=500,
        )


memory_digest_routes = [
    Route("/admin/api/memory-digest/status", digest_status, methods=["GET"]),
    Route("/admin/api/memory-digest/runs", digest_runs, methods=["GET"]),
    Route("/admin/api/memory-digest/preview", digest_preview, methods=["POST"]),
    Route("/admin/api/memory-digest/execute", digest_execute, methods=["POST"]),
    Route(
        "/admin/api/memory-continuity/shadow-preview",
        continuity_shadow_preview,
        methods=["POST"],
    ),
]
