"""Token-protected planning API used by the admin dashboard (一期).

路径约定 ``/admin/api/planning/*``；鉴权与错误结构沿用 admin_api 惯例
（Bearer GATEWAY_TOKEN + hmac.compare_digest，错误返回
``{"error"[, "error_code"]}``）。所有 DB 调用经 ``asyncio.to_thread``。
"""
from __future__ import annotations

import asyncio
import hmac
import json
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


def _error(message: str, status: int = 400, code: str | None = None,
           details: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"error": message}
    if code:
        body["error_code"] = code
    if details:
        body["details"] = details
    return JSONResponse(body, status_code=status)


def _run(fn, *args, **kwargs):
    return asyncio.to_thread(fn, *args, **kwargs)


def _is_schema_mismatch(exc: Exception) -> bool:
    """可可靠分类的「数据库结构未升级」故障（§36.1）：缺列 / 缺表 / PostgREST
    schema 缓存不识别新字段。只在证据明确时使用，不能所有 500 都说缺迁移。"""
    text = str(exc)
    lowered = text.casefold()
    if getattr(exc, "sqlstate", None) == "42703" or getattr(exc, "code", None) == "42703":
        return True
    markers = (
        "does not exist",                      # PostgreSQL 缺列 / 缺关系
        "could not find the",                  # PostgREST 不识别的列（PGRST204）
        "schema cache",                        # PostgREST schema 缓存过期
        "relation \"planning_",                # 缺表
    )
    return any(marker in lowered for marker in markers)


async def _dispatch(
    request: Request, fn, *args, created: bool = False, **kwargs,
) -> JSONResponse:
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    try:
        result = await _run(fn, *args, **kwargs)
        return JSONResponse(result, status_code=201 if created else 200)
    except planning.PlanningError as exc:
        return _error(str(exc), exc.status_code, exc.code, exc.details)
    except ValueError as exc:
        return _error(f"请求参数无效：{exc}", 400, "invalid_payload")
    except Exception as exc:
        log.exception("Planning API failure: %s %s", request.method, request.url.path)
        # §36.1（2026-10-07）：页面中文为主，不把 ``APIError`` 等内部异常
        # 类名当唯一解释；原异常与堆栈只在后台日志，敏感内容不出网。
        if _is_schema_mismatch(exc):
            return _error(
                "操作失败：数据库尚未完成升级，请联系管理员", 500, "schema_not_migrated")
        return _error(
            "操作失败：服务暂时出现异常，请稍后重试", 500, "internal_error")


async def _dispatch_json(
    request: Request, fn, *args, created: bool = False, **kwargs,
) -> JSONResponse:
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    try:
        payload = await request.json()
    except Exception:
        return _error("请求内容必须是合法的 JSON", 400, "invalid_json")
    return await _dispatch(request, fn, *args, payload, created=created, **kwargs)


async def _dispatch_json_optional(
    request: Request, fn, *args, created: bool = False,
) -> JSONResponse:
    """带可选 JSON body 的端点：无 body / 空 body 视为 ``{}``（既有契约——
    ``/finish`` 原本无 body 仍可用）；body 存在但非法 JSON 才拒绝。"""
    if not _authorized(request):
        return _error("未登录或令牌无效", 401, "unauthorized")
    raw = await request.body()
    if not raw or not raw.strip():
        return await _dispatch(request, fn, *args, {}, created=created)
    try:
        payload = json.loads(raw)
    except Exception:
        # R13（2026-10-07 复审 #13，低优先级修复）：可选 body 端点的非法
        # JSON 与 _dispatch_json 同口径中文提示（旧实现此处为英文）。
        return _error("请求内容必须是合法的 JSON", 400, "invalid_json")
    return await _dispatch(request, fn, *args, payload, created=created)


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


async def today(request: Request) -> JSONResponse:
    return await _dispatch(request, planning.today_board)


async def cycle_settings(request: Request) -> JSONResponse:
    if request.method == "GET":
        return await _dispatch(request, planning.get_cycle_settings)
    return await _dispatch_json(request, planning.set_cycle_settings)


async def tasks_collection(request: Request) -> JSONResponse:
    if request.method == "GET":
        include_inactive = _parse_bool(request.query_params.get("include_inactive"), True)
        return await _dispatch(request, planning.list_tasks, include_inactive)
    # 创建请求幂等（清单 #9）：键可选（兼容无键调用方）；前端为每次创建
    # 意图生成稳定键，结果未知的重试同键收敛到同一任务，同键不同内容 409。
    key = request.headers.get("Idempotency-Key")
    return await _dispatch_json(
        request, planning.create_task, created=True, idempotency_key=key)


async def task_item(request: Request) -> JSONResponse:
    task_id = request.path_params["task_id"]
    if request.method == "GET":
        return await _dispatch(request, planning.get_task, task_id)
    return await _dispatch_json(request, planning.update_task, task_id)


async def task_complete_early(request: Request) -> JSONResponse:
    if not _authorized(request):
        # R13（低优先级修复）：独立鉴权分支与 _dispatch 同口径中文提示。
        return _error("未登录或令牌无效", 401, "unauthorized")
    key = request.headers.get("Idempotency-Key")
    if not key:
        return _error("缺少 Idempotency-Key 请求头", 400, "invalid_payload")
    return await _dispatch(request, planning.complete_task_early, request.path_params["task_id"],
                           idempotency_key=key)


async def occurrences_collection(request: Request) -> JSONResponse:
    params = request.query_params
    return await _dispatch(
        request,
        planning.list_occurrences,
        task_type=params.get("task_type"),
        status=params.get("status"),
        for_date=params.get("for_date"),
        schedule_date=params.get("schedule_date"),
        display_cycle_date=params.get("display_cycle_date"),
        date_from=params.get("date_from"),
        date_to=params.get("date_to"),
        limit=params.get("limit"),
    )


async def occurrence_item(request: Request) -> JSONResponse:
    occurrence_id = request.path_params["occurrence_id"]
    if request.method == "GET":
        return await _dispatch(request, planning.get_occurrence, occurrence_id)
    return await _dispatch_json(request, planning.patch_occurrence, occurrence_id)


async def occurrence_status(request: Request) -> JSONResponse:
    return await _dispatch_json(
        request, planning.set_occurrence_status, request.path_params["occurrence_id"],
    )


async def occurrence_reschedule_timeout(request: Request) -> JSONResponse:
    if not _authorized(request):
        # R13（低优先级修复）：独立鉴权分支与 _dispatch 同口径中文提示。
        return _error("未登录或令牌无效", 401, "unauthorized")
    key = request.headers.get("Idempotency-Key")
    if not key:
        return _error("缺少 Idempotency-Key 请求头", 400, "invalid_payload")
    return await _dispatch_json(
        request, planning.reschedule_timeout_as_new, request.path_params["occurrence_id"],
        idempotency_key=key, created=True,
    )


async def occurrence_start(request: Request) -> JSONResponse:
    return await _dispatch(request, planning.start_occurrence, request.path_params["occurrence_id"])


async def occurrence_finish(request: Request) -> JSONResponse:
    # 完成耗时手填（2026-10-01 确认 §12.3）：body 可选，支持
    # actual_logged_duration（原始 h/m/s 文本，后端权威解析为秒）。
    return await _dispatch_json_optional(
        request, planning.finish_occurrence, request.path_params["occurrence_id"],
    )


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
    Route("/admin/api/planning/cycle", cycle_settings, methods=["GET", "PATCH"]),
    Route("/admin/api/planning/today", today, methods=["GET"]),
    Route("/admin/api/planning/tasks", tasks_collection, methods=["GET", "POST"]),
    Route("/admin/api/planning/tasks/{task_id:int}", task_item, methods=["GET", "PATCH"]),
    Route("/admin/api/planning/tasks/{task_id:int}/complete-early", task_complete_early, methods=["POST"]),
    Route("/admin/api/planning/occurrences", occurrences_collection, methods=["GET"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}", occurrence_item, methods=["GET", "PATCH"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/status", occurrence_status, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/reschedule-timeout", occurrence_reschedule_timeout, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/start", occurrence_start, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/finish", occurrence_finish, methods=["POST"]),
    Route("/admin/api/planning/occurrences/{occurrence_id:int}/split", occurrence_split, methods=["POST"]),
    Route("/admin/api/planning/reorder", reorder, methods=["POST"]),
    Route("/admin/api/planning/recompute", recompute_collection, methods=["GET", "POST"]),
]
