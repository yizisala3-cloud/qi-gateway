"""User review service for pending AI memory applications."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .config import cfg
from .db import get_client
from .memory_requests import MemoryRequestError
from .memory_requests import _clean_memory_key


def _server_writes_allowed() -> bool:
    return cfg.supabase_elevated_key_configured


def _text(value: Any, field: str, maximum: int, *, required: bool = False) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
    else:
        raise MemoryRequestError("invalid_review", f"{field} must be a string")
    if required and len(text) < 5:
        raise MemoryRequestError("invalid_review", f"{field} must contain at least 5 characters")
    if len(text) > maximum:
        raise MemoryRequestError("invalid_review", f"{field} must not exceed {maximum} characters")
    return text


def _tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    candidates = re.split(r"[,，]", value) if isinstance(value, str) else value
    if not isinstance(candidates, list):
        raise MemoryRequestError("invalid_review", "tags must be a string or array")
    result: list[str] = []
    for item in candidates:
        tag = _text(item, "tag", 24)
        if tag and tag not in result:
            result.append(tag)
        if len(result) >= 5:
            break
    return result


def _importance(value: Any) -> int:
    if isinstance(value, bool):
        raise MemoryRequestError("invalid_review", "importance must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryRequestError("invalid_review", "importance must be an integer") from exc
    if result < 1 or result > 10:
        raise MemoryRequestError("invalid_review", "importance must be between 1 and 10")
    return result


def validate_review(request_id: Any, payload: Any) -> dict[str, Any]:
    try:
        normalized_id = int(request_id)
    except (TypeError, ValueError) as exc:
        raise MemoryRequestError("invalid_review", "request_id must be an integer") from exc
    if normalized_id <= 0:
        raise MemoryRequestError("invalid_review", "request_id must be positive")
    if not isinstance(payload, dict):
        raise MemoryRequestError("invalid_review", "JSON body must be an object")

    allowed = {
        "action", "content", "title", "tags", "importance", "review_note",
        "memory_key", "update_mode",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise MemoryRequestError(
            "invalid_review",
            f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"approve", "reject"}:
        raise MemoryRequestError("invalid_review", "action must be approve or reject")

    review_note = _text(payload.get("review_note"), "review_note", 500)
    if action == "reject":
        if any(key in payload for key in (
            "content", "title", "tags", "importance", "memory_key", "update_mode",
        )):
            raise MemoryRequestError(
                "invalid_review",
                "rejected applications cannot include memory edits",
            )
        return {
            "request_id": normalized_id,
            "action": action,
            "content": None,
            "title": None,
            "tags": None,
            "importance": None,
            "content_hash": None,
            "review_note": review_note or None,
            "memory_key": None,
            "update_mode": None,
        }

    content = _text(payload.get("content"), "content", 600, required=True)
    title = _text(payload.get("title"), "title", 100)
    importance = _importance(payload.get("importance", 5))
    memory_key = None
    if "memory_key" in payload:
        try:
            memory_key = _clean_memory_key(payload.get("memory_key"))
        except MemoryRequestError as exc:
            raise MemoryRequestError("invalid_review", str(exc)) from exc
    update_mode = None
    if "update_mode" in payload:
        update_mode = str(payload.get("update_mode") or "").strip().casefold()
        if update_mode not in {"append", "replace"}:
            raise MemoryRequestError(
                "invalid_review",
                "update_mode must be append or replace",
            )
        if update_mode == "replace" and not memory_key:
            raise MemoryRequestError(
                "invalid_review",
                "memory_key is required when update_mode is replace",
            )
        if update_mode == "append" and memory_key:
            raise MemoryRequestError(
                "invalid_review",
                "memory_key is only allowed when update_mode is replace",
            )
    return {
        "request_id": normalized_id,
        "action": action,
        "content": content,
        "title": title or None,
        "tags": _tags(payload.get("tags")),
        "importance": importance,
        "content_hash": hashlib.sha256(content.casefold().encode("utf-8")).hexdigest(),
        "review_note": review_note or None,
        "memory_key": memory_key,
        "update_mode": update_mode,
    }


def _result(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MemoryRequestError("review_failed", "review RPC returned invalid JSON", 500) from exc
    if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
        raise MemoryRequestError("review_failed", "review RPC returned an invalid response", 500)
    return data


def review_memory_request(request_id: Any, payload: Any) -> dict[str, Any]:
    review = validate_review(request_id, payload)
    if not _server_writes_allowed():
        raise MemoryRequestError(
            "database_permissions_unavailable",
            "an elevated Supabase server key is required",
            503,
        )
    client = get_client()
    if not client:
        raise MemoryRequestError("database_unavailable", "Supabase is unavailable", 503)

    rpc_payload = {
        "p_request_id": review["request_id"],
        "p_action": review["action"],
        "p_content": review["content"],
        "p_title": review["title"],
        "p_tags": review["tags"],
        "p_importance": review["importance"],
        "p_content_hash": review["content_hash"],
        "p_reviewed_by": "gateway_admin",
        "p_review_note": review["review_note"],
        "p_memory_key": review["memory_key"],
        "p_update_mode": review["update_mode"],
    }
    try:
        response = client.rpc("review_memory_request_v2", rpc_payload).execute()
    except Exception as exc:
        message = str(exc).casefold()
        if "memory_request_not_found" in message:
            raise MemoryRequestError("request_not_found", "memory application was not found", 404) from exc
        if "memory_request_not_pending" in message:
            raise MemoryRequestError(
                "request_not_pending",
                "memory application has already been reviewed",
                409,
            ) from exc
        if "memory_request_stale_update" in message:
            raise MemoryRequestError(
                "stale_update",
                "a newer version of this memory key is already active",
                409,
            ) from exc
        if "memory_request_invalid_" in message:
            raise MemoryRequestError("invalid_review", "database rejected review values", 400) from exc
        raise MemoryRequestError(
            "review_failed",
            f"failed to review memory application: {type(exc).__name__}",
            500,
        ) from exc

    result = _result(response.data)
    row = result["request"]
    return {
        "request_id": row.get("id"),
        "status": row.get("status"),
        "memory_id": row.get("memory_id"),
        "reviewed_at": row.get("reviewed_at"),
        "changed": bool(result.get("changed")),
        "superseded_memory_id": result.get("superseded_memory_id"),
    }

