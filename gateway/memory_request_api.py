"""HTTP endpoint used by the OrangeChat request_memory plugin."""
from __future__ import annotations

import asyncio
import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from .memory_requests import MemoryRequestError, create_memory_request


log = logging.getLogger("gateway.memory_request_api")
MAX_REQUEST_BYTES = 16_384


def _error(code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        {"success": False, "error_code": code, "error": message},
        status_code=status_code,
    )


def _authorized(request: Request) -> bool:
    configured = cfg.MEMORY_PLUGIN_TOKEN.strip()
    if not configured:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, configured)


async def submit_memory_request(request: Request) -> JSONResponse:
    if not cfg.MEMORY_PLUGIN_TOKEN.strip():
        return _error(
            "plugin_not_configured",
            "memory plugin access is not configured",
            503,
        )
    if not _authorized(request):
        return _error("unauthorized", "invalid plugin token", 401)

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_REQUEST_BYTES:
                return _error("payload_too_large", "request body is too large", 413)
        except ValueError:
            return _error("invalid_content_length", "invalid Content-Length", 400)

    try:
        payload = await request.json()
    except Exception:
        return _error("invalid_json", "request body must be valid JSON", 400)

    try:
        result = await asyncio.to_thread(
            create_memory_request,
            payload,
            request.headers.get("idempotency-key", ""),
        )
        return JSONResponse(
            {
                "success": True,
                **result,
                "message": "memory application is pending user review",
            },
            status_code=201 if result["created"] else 200,
        )
    except MemoryRequestError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception("Unexpected memory request failure: %s", type(exc).__name__)
        return _error("internal_error", "unexpected memory request failure", 500)


memory_request_routes = [
    Route("/v1/memory-requests", submit_memory_request, methods=["POST"]),
]
