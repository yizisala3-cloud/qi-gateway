"""Token-protected data API used by the bundled admin dashboard."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import db
from .config import cfg

log = logging.getLogger("gateway.admin_api")

_TABLES: dict[str, dict[str, Any]] = {
    "memories": {
        "read": {
            "id", "content", "title", "tags", "heat", "importance", "layer",
            "source", "verified", "is_active", "last_recalled_at", "recall_count",
            "emotion_weight", "created_at", "memory_key", "supersedes_memory_id",
            "superseded_by_memory_id", "superseded_at",
        },
        "write": {
            "content", "title", "tags", "heat", "importance", "layer", "source",
            "verified", "is_active", "last_recalled_at", "emotion_weight",
        },
        "insert": True,
        "update": True,
        "delete": False,
        "default_order": "created_at",
    },
    "persona": {
        "read": {"id", "name", "content", "is_active", "updated_at"},
        "write": {"name", "content", "is_active", "updated_at"},
        "insert": True,
        "update": True,
        "delete": False,
        "default_order": "id",
    },
    "jiwen_state": {
        "read": {
            "id", "connection", "pride", "valence", "arousal", "immersion",
            "last_activity", "last_tick_at", "last_chat_message_id",
            "last_bot_message_id", "user_status", "updated_at", "last_chat_at",
            "last_bot_at",
        },
        "write": {
            "connection", "pride", "valence", "arousal", "immersion", "user_status",
        },
        "insert": False,
        "update": True,
        "delete": False,
        "default_order": "id",
    },
    "timers": {
        "read": {
            "id", "type", "minutes", "target_time", "summary", "target_date",
            "set_at", "expire_at", "executed", "executed_at", "cancelled",
            "fail_count", "trigger_context", "created_at",
        },
        "write": {"cancelled"},
        "insert": False,
        "update": True,
        "delete": False,
        "default_order": "created_at",
    },
    "chat_messages": {
        "read": {"id", "assistant_id", "conversation_id", "role", "content", "created_at"},
        "write": set(),
        "insert": False,
        "update": False,
        "delete": False,
        "default_order": "created_at",
    },
    "memory_requests": {
        "read": {
            "id", "assistant_id", "conversation_id", "source_message_id",
            "content", "title", "tags", "importance", "reason", "status",
            "source", "memory_id", "created_at", "reviewed_at", "reviewed_by",
            "review_note", "memory_key", "update_mode",
        },
        "write": set(),
        "insert": False,
        "update": False,
        "delete": False,
        "default_order": "created_at",
    },
}


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _table_config(request: Request) -> tuple[str, dict[str, Any]] | None:
    table = request.path_params.get("table", "")
    config = _TABLES.get(table)
    return (table, config) if config else None


def _parse_int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        parsed = default
    return max(lo, min(hi, parsed))


def _parse_eq(raw: str | None, allowed: set[str]) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid eq filter") from exc
    if not isinstance(values, dict):
        raise ValueError("eq filter must be an object")
    if any(key not in allowed for key in values):
        raise ValueError("unsupported eq filter field")
    return values


def _select_fields(raw: str | None, allowed: set[str]) -> str:
    if not raw or raw == "*":
        return ",".join(sorted(allowed))
    fields = [field.strip() for field in raw.split(",") if field.strip()]
    if not fields or any(field not in allowed for field in fields):
        raise ValueError("unsupported select field")
    return ",".join(fields)


def _sanitize_row(payload: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    row = {key: value for key, value in payload.items() if key in allowed}
    if not row:
        raise ValueError("no writable fields supplied")
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("unsupported write field")
    return row


def _execute_list(
    table: str,
    config: dict[str, Any],
    *,
    select: str,
    eq: dict[str, Any],
    order: str,
    ascending: bool,
    offset: int,
    limit: int,
    search: str,
    count_only: bool,
) -> dict[str, Any]:
    client = db.get_client()
    if not client:
        raise RuntimeError("Supabase is not configured")

    if count_only:
        query = client.table(table).select("id", count="exact")
    else:
        query = client.table(table).select(select)

    for key, value in eq.items():
        query = query.eq(key, value)

    if search:
        if table != "memories":
            raise ValueError("search is only supported for memories")
        safe_search = re.sub(r"[,%()]", " ", search).strip()[:100]
        if safe_search:
            query = query.or_(f"title.ilike.%{safe_search}%,content.ilike.%{safe_search}%")

    if count_only:
        response = query.limit(1).execute()
        return {"data": [], "count": response.count or 0}

    response = (
        query.order(order, desc=not ascending)
        .range(offset, offset + limit - 1)
        .execute()
    )
    return {"data": response.data or [], "count": response.count}


def _execute_insert(table: str, row: dict[str, Any]) -> list[dict[str, Any]]:
    client = db.get_client()
    if not client:
        raise RuntimeError("Supabase is not configured")
    response = client.table(table).insert(row).execute()
    return response.data or []


def _execute_update(table: str, row_id: int, row: dict[str, Any]) -> list[dict[str, Any]]:
    client = db.get_client()
    if not client:
        raise RuntimeError("Supabase is not configured")
    response = client.table(table).update(row).eq("id", row_id).execute()
    return response.data or []


async def admin_collection(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)

    table_entry = _table_config(request)
    if not table_entry:
        return _error("unsupported table", 404)
    table, config = table_entry

    try:
        if request.method == "GET":
            params = request.query_params
            select = _select_fields(params.get("select"), config["read"])
            eq = _parse_eq(params.get("eq"), config["read"])
            order = params.get("order") or config["default_order"]
            if order not in config["read"]:
                raise ValueError("unsupported order field")
            ascending = params.get("asc", "false").lower() == "true"
            offset = _parse_int(params.get("offset"), 0, 0, 1_000_000)
            limit = _parse_int(params.get("limit"), 50, 1, 100)
            search = (params.get("search") or "").strip()
            count_only = params.get("count", "false").lower() == "true"
            result = await asyncio.to_thread(
                _execute_list,
                table,
                config,
                select=select,
                eq=eq,
                order=order,
                ascending=ascending,
                offset=offset,
                limit=limit,
                search=search,
                count_only=count_only,
            )
            return JSONResponse(result)

        if not config["insert"]:
            return _error("insert not allowed", 405)
        payload = await request.json()
        row = _sanitize_row(payload, config["write"])
        data = await asyncio.to_thread(_execute_insert, table, row)
        return JSONResponse({"data": data}, status_code=201)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        log.exception("Admin collection operation failed: table=%s", table)
        return _error(str(exc), 500)


async def admin_item(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)

    table_entry = _table_config(request)
    if not table_entry:
        return _error("unsupported table", 404)
    table, config = table_entry
    row_id = request.path_params["row_id"]

    try:
        if request.method == "GET":
            result = await asyncio.to_thread(
                _execute_list,
                table,
                config,
                select=",".join(sorted(config["read"])),
                eq={"id": row_id},
                order="id",
                ascending=True,
                offset=0,
                limit=1,
                search="",
                count_only=False,
            )
            if not result["data"]:
                return _error("not found", 404)
            return JSONResponse({"data": result["data"]})

        if request.method == "PATCH":
            if not config["update"]:
                return _error("update not allowed", 405)
            payload = await request.json()
            row = _sanitize_row(payload, config["write"])
            data = await asyncio.to_thread(_execute_update, table, row_id, row)
            return JSONResponse({"data": data})

        return _error("delete not allowed", 405)
    except ValueError as exc:
        return _error(str(exc), 400)
    except Exception as exc:
        log.exception("Admin item operation failed: table=%s id=%s", table, row_id)
        return _error(str(exc), 500)


admin_api_routes = [
    Route("/admin/api/data/{table:str}", admin_collection, methods=["GET", "POST"]),
    Route("/admin/api/data/{table:str}/{row_id:int}", admin_item, methods=["GET", "PATCH", "DELETE"]),
]

