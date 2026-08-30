"""Token-protected admin endpoint for reviewing AI memory applications."""
from __future__ import annotations

import asyncio
import hmac
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import cfg
from .memory_requests import MemoryRequestError
from .memory_review import edit_memory_recall, review_memory_request


log = logging.getLogger("gateway.memory_review_api")


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


async def review_request(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("unauthorized", "invalid gateway token", 401)
    try:
        payload = await request.json()
    except Exception:
        return _error("invalid_json", "request body must be valid JSON", 400)

    try:
        result = await asyncio.to_thread(
            review_memory_request,
            request.path_params["request_id"],
            payload,
        )
        return JSONResponse({"success": True, **result})
    except MemoryRequestError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception("Unexpected memory review failure: %s", type(exc).__name__)
        return _error("internal_error", "unexpected memory review failure", 500)


async def edit_memory_recall_request(request: Request) -> JSONResponse:
    """Purpose-built atomic recall editor for formal memories.

    Recall edits re-derive the embedding server-side in the same write; the
    generic table PATCH stays unable to touch recall fields so a scene can
    never be saved with a stale vector.
    """
    if not _authorized(request):
        return _error("unauthorized", "invalid gateway token", 401)
    try:
        payload = await request.json()
    except Exception:
        return _error("invalid_json", "request body must be valid JSON", 400)

    try:
        result = await asyncio.to_thread(
            edit_memory_recall,
            request.path_params["memory_id"],
            payload,
        )
        return JSONResponse({"success": True, **result})
    except MemoryRequestError as exc:
        return _error(exc.code, str(exc), exc.status_code)
    except Exception as exc:
        log.exception("Unexpected memory recall edit failure: %s", type(exc).__name__)
        return _error("internal_error", "unexpected memory recall edit failure", 500)


memory_review_routes = [
    Route(
        "/admin/api/memory-requests/{request_id:int}/review",
        review_request,
        methods=["POST"],
    ),
    Route(
        "/admin/api/memories/{memory_id:int}/recall",
        edit_memory_recall_request,
        methods=["POST"],
    ),
]

