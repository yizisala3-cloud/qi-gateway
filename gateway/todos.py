"""Validated access to the existing ``todos`` table.

Plugin clients never receive Supabase credentials. Every write/update
operation is scoped by both ``user_name`` and ``ai_name`` because the gateway
uses an elevated server key that bypasses RLS. Cancellation is a soft hide;
this module never deletes todo rows and never reads or writes
``chat_messages``.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from .config import cfg
from .db import get_client


log = logging.getLogger("gateway.todos")

MAX_NAME_LENGTH = 100
MAX_CONTENT_LENGTH = 1_000
MAX_NOTE_LENGTH = 1_000
MAX_ESTIMATED_TIME_LENGTH = 100
MAX_QUERY_ROWS = 200
TODO_FIELDS = (
    "id,user_name,ai_name,content,todo_type,status,estimated_time,"
    "scheduled_start,scheduled_end,sort_order,is_completed,completed_at,"
    "is_private,is_hidden,parent_id,is_start_marker,is_end_marker,note,"
    "created_at,updated_at"
)


class TodoError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _server_access_allowed() -> bool:
    return cfg.supabase_elevated_key_configured


def _clean_text(
    value: Any,
    field: str,
    *,
    required: bool,
    maximum: int,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
    else:
        raise TodoError("invalid_payload", f"{field} must be a string")
    if required and not text:
        raise TodoError("invalid_payload", f"{field} is required")
    if len(text) > maximum:
        raise TodoError(
            "invalid_payload",
            f"{field} must not exceed {maximum} characters",
        )
    return text


def _clean_bool(value: Any, field: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise TodoError("invalid_payload", f"{field} must be a boolean")
    return value


def _clean_choice(value: Any, field: str, allowed: set[str], default: str) -> str:
    text = str(value or default).strip().casefold()
    if text not in allowed:
        choices = ", ".join(sorted(allowed))
        raise TodoError("invalid_payload", f"{field} must be one of: {choices}")
    return text


def _parse_datetime(value: Any, field: str, *, required: bool = False) -> datetime | None:
    if value in (None, ""):
        if required:
            raise TodoError("invalid_payload", f"{field} is required")
        return None
    if not isinstance(value, str):
        raise TodoError("invalid_payload", f"{field} must be an ISO 8601 string")
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise TodoError("invalid_payload", f"{field} must be a valid ISO 8601 time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TodoError(
            "invalid_payload",
            f"{field} must include a timezone offset, for example +08:00",
        )
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def _scope(payload: dict[str, Any]) -> tuple[str, str]:
    user_name = _clean_text(
        payload.get("user_name"),
        "user_name",
        required=True,
        maximum=MAX_NAME_LENGTH,
    )
    ai_name = _clean_text(
        payload.get("ai_name"),
        "ai_name",
        required=True,
        maximum=MAX_NAME_LENGTH,
    )
    return user_name, ai_name


def _require_object(payload: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TodoError("invalid_payload", "JSON body must be an object")
    unknown = set(payload) - allowed
    if unknown:
        raise TodoError(
            "invalid_payload",
            f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    return payload


def _client():
    if not _server_access_allowed():
        raise TodoError(
            "database_permissions_unavailable",
            "an elevated Supabase server key is required",
            503,
        )
    client = get_client()
    if not client:
        raise TodoError("database_unavailable", "Supabase server client is unavailable", 503)
    return client


def _todo_id(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TodoError("invalid_todo_id", "todo_id must be a UUID") from exc


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    return {field: row.get(field) for field in TODO_FIELDS.split(",")}


def _same_instant(left: Any, right: datetime | None) -> bool:
    try:
        return _parse_datetime(left, "stored_time") == right
    except TodoError:
        return False


def validate_create_todo(payload: Any) -> dict[str, Any]:
    payload = _require_object(payload, {
        "user_name", "ai_name", "content", "todo_type", "status",
        "estimated_time", "scheduled_start", "scheduled_end", "is_private", "note",
    })
    user_name, ai_name = _scope(payload)
    scheduled_start = _parse_datetime(payload.get("scheduled_start"), "scheduled_start")
    scheduled_end = _parse_datetime(payload.get("scheduled_end"), "scheduled_end")
    if scheduled_start and scheduled_end and scheduled_end < scheduled_start:
        raise TodoError("invalid_payload", "scheduled_end must not be earlier than scheduled_start")
    return {
        "user_name": user_name,
        "ai_name": ai_name,
        "content": _clean_text(
            payload.get("content"),
            "content",
            required=True,
            maximum=MAX_CONTENT_LENGTH,
        ),
        "todo_type": _clean_choice(payload.get("todo_type"), "todo_type", {"user", "ai"}, "ai"),
        "status": _clean_choice(
            payload.get("status"),
            "status",
            {"regular", "long_term"},
            "regular",
        ),
        "estimated_time": _clean_text(
            payload.get("estimated_time"),
            "estimated_time",
            required=False,
            maximum=MAX_ESTIMATED_TIME_LENGTH,
        ) or None,
        "scheduled_start": _iso(scheduled_start),
        "scheduled_end": _iso(scheduled_end),
        "is_private": _clean_bool(payload.get("is_private"), "is_private"),
        "note": _clean_text(
            payload.get("note"),
            "note",
            required=False,
            maximum=MAX_NOTE_LENGTH,
        ) or None,
    }


def create_todo(payload: Any) -> dict[str, Any]:
    data = validate_create_todo(payload)
    client = _client()

    # Best-effort retry deduplication using the existing schema. A future
    # migration can make this atomic with a persisted idempotency key.
    existing_response = (
        client.table("todos")
        .select(TODO_FIELDS)
        .eq("user_name", data["user_name"])
        .eq("ai_name", data["ai_name"])
        .eq("content", data["content"])
        .eq("todo_type", data["todo_type"])
        .eq("status", data["status"])
        .eq("is_completed", False)
        .eq("is_hidden", False)
        .limit(20)
        .execute()
    )
    scheduled_start = _parse_datetime(data["scheduled_start"], "scheduled_start")
    for row in existing_response.data or []:
        if _same_instant(row.get("scheduled_start"), scheduled_start):
            return {"todo": _serialize(row), "created": False, "deduplicated": True}

    row = {
        **data,
        "is_completed": False,
        "is_hidden": False,
        "is_start_marker": False,
        "is_end_marker": False,
    }
    try:
        response = client.table("todos").insert(row).execute()
    except Exception as exc:
        raise TodoError("todo_store_failed", f"failed to create todo: {type(exc).__name__}", 500) from exc
    if not response.data:
        raise TodoError("todo_store_failed", "database returned no created todo", 500)
    return {"todo": _serialize(response.data[0]), "created": True, "deduplicated": False}


def validate_list_todos(payload: Any) -> dict[str, Any]:
    payload = _require_object(payload, {
        "user_name", "ai_name", "scope", "timezone_offset_minutes", "limit",
    })
    user_name, ai_name = _scope(payload)
    scope = _clean_choice(payload.get("scope"), "scope", {"today", "open", "overdue"}, "today")
    offset = payload.get("timezone_offset_minutes", 480)
    limit = payload.get("limit", 50)
    if isinstance(offset, bool) or not isinstance(offset, int) or not -720 <= offset <= 840:
        raise TodoError("invalid_payload", "timezone_offset_minutes must be an integer from -720 to 840")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise TodoError("invalid_payload", "limit must be an integer from 1 to 100")
    return {
        "user_name": user_name,
        "ai_name": ai_name,
        "scope": scope,
        "timezone_offset_minutes": offset,
        "limit": limit,
    }


def _stored_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_datetime(value, "stored_time")
    except TodoError:
        return None


def list_todos(payload: Any, *, now: datetime | None = None) -> dict[str, Any]:
    request_data = validate_list_todos(payload)
    client = _client()
    response = (
        client.table("todos")
        .select(TODO_FIELDS)
        .eq("user_name", request_data["user_name"])
        .eq("ai_name", request_data["ai_name"])
        .eq("is_completed", False)
        .eq("is_hidden", False)
        .eq("is_start_marker", False)
        .eq("is_end_marker", False)
        .limit(MAX_QUERY_ROWS)
        .execute()
    )

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    local_tz = timezone(timedelta(minutes=request_data["timezone_offset_minutes"]))
    local_now = now_utc.astimezone(local_tz)
    tomorrow_local = (local_now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_utc = tomorrow_local.astimezone(timezone.utc)

    selected: list[dict[str, Any]] = []
    for row in response.data or []:
        if row.get("status") == "hollow":
            continue
        scheduled = _stored_datetime(row.get("scheduled_start"))
        if request_data["scope"] == "today" and scheduled and scheduled >= tomorrow_utc:
            continue
        if request_data["scope"] == "overdue" and (scheduled is None or scheduled > now_utc):
            continue
        selected.append(row)

    def sort_key(row: dict[str, Any]):
        scheduled = _stored_datetime(row.get("scheduled_start"))
        return (
            scheduled is None,
            scheduled or datetime.max.replace(tzinfo=timezone.utc),
            int(row.get("sort_order") or 0),
            str(row.get("created_at") or ""),
        )

    selected.sort(key=sort_key)
    selected = selected[:request_data["limit"]]
    return {
        "todos": [_serialize(row) for row in selected],
        "count": len(selected),
        "scope": request_data["scope"],
        "current_time": _iso(now_utc),
        "timezone_offset_minutes": request_data["timezone_offset_minutes"],
    }


def _mutation_scope(payload: Any, extra_allowed: set[str]) -> tuple[dict[str, Any], str, str]:
    payload = _require_object(payload, {"user_name", "ai_name", *extra_allowed})
    user_name, ai_name = _scope(payload)
    return payload, user_name, ai_name


def _update_scoped(todo_id: Any, user_name: str, ai_name: str, changes: dict[str, Any]) -> dict[str, Any]:
    client = _client()
    changes["updated_at"] = _iso(datetime.now(timezone.utc))
    try:
        response = (
            client.table("todos")
            .update(changes)
            .eq("id", _todo_id(todo_id))
            .eq("user_name", user_name)
            .eq("ai_name", ai_name)
            .execute()
        )
    except TodoError:
        raise
    except Exception as exc:
        raise TodoError("todo_update_failed", f"failed to update todo: {type(exc).__name__}", 500) from exc
    if not response.data:
        raise TodoError("todo_not_found", "todo does not exist in this user/AI scope", 404)
    return _serialize(response.data[0])


def complete_todo(todo_id: Any, payload: Any) -> dict[str, Any]:
    _payload, user_name, ai_name = _mutation_scope(payload, set())
    now = _iso(datetime.now(timezone.utc))
    return _update_scoped(todo_id, user_name, ai_name, {
        "is_completed": True,
        "completed_at": now,
    })


def snooze_todo(todo_id: Any, payload: Any) -> dict[str, Any]:
    payload, user_name, ai_name = _mutation_scope(
        payload,
        {"scheduled_start", "scheduled_end"},
    )
    start = _parse_datetime(payload.get("scheduled_start"), "scheduled_start", required=True)
    end = _parse_datetime(payload.get("scheduled_end"), "scheduled_end")
    if end and end < start:
        raise TodoError("invalid_payload", "scheduled_end must not be earlier than scheduled_start")
    return _update_scoped(todo_id, user_name, ai_name, {
        "scheduled_start": _iso(start),
        "scheduled_end": _iso(end),
        "is_completed": False,
        "completed_at": None,
        "is_hidden": False,
    })


def cancel_todo(todo_id: Any, payload: Any) -> dict[str, Any]:
    _payload, user_name, ai_name = _mutation_scope(payload, set())
    return _update_scoped(todo_id, user_name, ai_name, {"is_hidden": True})

