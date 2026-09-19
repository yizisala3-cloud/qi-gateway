"""Token-protected planning API used by the admin dashboard (一期).

路径约定 ``/admin/api/planning/*``；鉴权与错误结构沿用 admin_api 惯例
（Bearer GATEWAY_TOKEN + hmac.compare_digest，错误返回
``{"error"[, "error_code"]}``）。所有 DB 调用经 ``asyncio.to_thread``。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import planning
from .config import cfg

log = logging.getLogger("gateway.planning_api")


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int = 400, code: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    if code:
        body["error_code"] = code
    return JSONResponse(body, status_code=status)


def _run(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


async def _dispatch(
    request: Request, fn, *args, created: bool = False, **kwargs,
) -> JSONResponse:
    if not _authorized(request):
        return _error("unauthorized", 401, "unauthorized")
    try:
        result = await _run(fn, *args, **kwargs)
        return JSONResponse(result, status_code=201 if created else 200)
    except planning.PlanningError as exc:
        return _error(str(exc), exc.status_code, exc.code)
    except ValueError as exc:
        return _error(f"invalid query or payload value: {exc}", 400, "invalid_payload")
    except Exception as exc:
        log.exception("Planning API failure: %s %s", request.method, request.url.path)
        return _error(f"unexpected planning failure: {type(exc).__name__}", 500, "internal_error")


async def _dispatch_json(
    request: Request, fn, *args, created: bool = False,
) -> JSONResponse:
    if not _authorized(request):
        return _error("unauthorized", 401, "unauthorized")
    try:
        payload = await request.json()
    except Exception:
        return _error("request body must be valid JSON", 400, "invalid_json")
    return await _dispatch(request, fn, *args, payload, created=created)


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


async def today(request: Request) -> JSONResponse:
    return await _dispatch(request, planning.today_board)


async def tasks_collection(request: Request) -> JSONResponse:
    if request.method == "GET":
        include_inactive = _parse_bool(request.query_params.get("include_inactive"), True)
        return await _dispatch(request, planning.list_tasks, include_inactive)
    return await _dispatch_json(request, planning.create_task, created=True)


async def task_item(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]

    def _get() -> dict[str, Any]:
        client = planning._require_client()
        task = planning._fetch_task(client, task_id)
        if not task:
            raise planning.PlanningError("not_found", "planning task not found", 404)
        return planning.serialize_task(task, planning._now())

    if request.method == "GET":
        return await _dispatch(request, _get)
    return await _dispatch_json(request, planning.update_task, task_id)


async def task_complete_early(request: Request) -> JSONResponse:
    return await _dispatch(request, planning.complete_task_early, request.path_params["task_id"])


async def occurrences_collection(request: Request) -> JSONResponse:
    params = request.query_params
    return await _dispatch(
        request,
        planning.list_occurrences,
        task_type=params.get("task_type"),
        status=params.get("status"),
        for_date=params.get("for_date"),
        date_from=params.get("date_from"),
        date_to=params.get("date_to"),
        limit=params.get("limit"),
    )


async def occurrence_item(request: Request) -> JSONResponse:
    occurrence_id = request.path_params["occurrence_id"]

    def _get() -> dict[str, Any]:
        client = planning._require_client()
        occ = planning._fetch_occurrence(client, occurrence_id)
        if not occ:
            raise planning.PlanningError("not_found", "planning occurrence not found", 404)
        task = planning._fetch_task(client, occ["task_id"])
        if not task:
            raise planning.PlanningError("not_found", "planning task not found", 404)
        return planning.serialize_occurrence(occ, task, planning._now())

    if request.method == "GET":
        return await _dispatch(request, _get)
    return await _dispatch_json(request, planning.patch_occurrence, occurrence_id)


async def occurrence_status(request: Request) -> JSONResponse:
    return await _dispatch_json(
        request, planning.set_occurrence_status, request.path_params["occurrence_id"],
    )


async def occurrence_start(request: Request) -> JSONResponse:
    return await _dispatch(request, planning.start_occurrence, request.path_params["occurrence_id"])


async def occurrence_finish(request: Request) -> JSONResponse:
    return await _dispatch(request, planning.finish_occurrence, request.path_params["occurrence_id"])


async def occurrence_split(request: Request) -> JSONResponse:
    return await _dispatch_json(
        request, planning.split_occurrence, request.path_params["occurrence_id"], created=True,
    )


async def reorder(request: Request) -> JSONResponse:
    return await _dispatch_json(request, planning.save_order_from_payload)


async def recompute_collection(request: Request) -> JSONResponse:
    if request.method == "GET":
        return await _dispatch(request, planning.get_recompute_state)
    return await _dispatch(request, planning.trigger_recompute)


planning_api_routes = [
    Route("/admin/api/planning/today", today, methods=["GET"]),
    Route("/admin/api/planning/tasks", tasks_collection, methods=["GET", "POST"]),
    Route("/admin/api/planning/tasks/{task_id:int}", task_item, methods=["GET", "PATCH"]),
    Route("/admin/api/planning/tasks/{task_id:int}/complete-early", task_complete_early, methods=["POST"]),
    Route("/admin/api/planning/occurrences", occurrences_collection, methods=["GET"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}", occurrence_item, methods=["GET", "PATCH"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/status", occurrence_status, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/start", occurrence_start, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/finish", occurrence_finish, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/split", occurrence_split, methods=["POST"]),
    Route("/admin/api/planning/reorder", reorder, methods=["POST"]),
    Route("/admin/api/planning/recompute", recompute_collection, methods=["GET", "POST"]),
]
