"""Validation and persistence for AI-initiated memory applications.

The OrangeChat plugin never receives a Supabase server key. It submits a
request to the gateway, which stores a pending application through an atomic
database function. `chat_messages` is only read when an optional source ID is
provided and is never modified here.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .config import cfg
from .db import get_client
from .memory_continuity_schema import SCHEMA_VERSION, ContinuityDataError, validate_continuity_data


MAX_CONTENT_LENGTH = 600
MAX_REASON_LENGTH = 500
MAX_TITLE_LENGTH = 100
MAX_IDENTIFIER_LENGTH = 160
MAX_MEMORY_KEY_LENGTH = 120
MAX_TAGS = 5
MAX_TAG_LENGTH = 24


class MemoryRequestError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _server_writes_allowed() -> bool:
    return cfg.supabase_elevated_key_configured


def _clean_text(
    value: Any,
    field: str,
    *,
    required: bool,
    minimum: int = 0,
    maximum: int,
) -> str:
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = re.sub(r"\s+", " ", value).strip()
    else:
        raise MemoryRequestError("invalid_payload", f"{field} must be a string")

    if required and len(text) < minimum:
        raise MemoryRequestError(
            "invalid_payload",
            f"{field} must contain at least {minimum} characters",
        )
    if len(text) > maximum:
        raise MemoryRequestError(
            "invalid_payload",
            f"{field} must not exceed {maximum} characters",
        )
    return text


def _clean_tags(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        candidates = re.split(r"[,，]", value)
    elif isinstance(value, list):
        candidates = value
    else:
        raise MemoryRequestError("invalid_payload", "tags must be a string or array")

    tags: list[str] = []
    for candidate in candidates:
        tag = _clean_text(
            candidate,
            "tag",
            required=False,
            maximum=MAX_TAG_LENGTH,
        )
        if tag and tag not in tags:
            tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags


def _clean_importance(value: Any) -> int:
    if value in (None, ""):
        return 5
    if isinstance(value, bool):
        raise MemoryRequestError("invalid_payload", "importance must be an integer")
    try:
        importance = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryRequestError("invalid_payload", "importance must be an integer") from exc
    if importance < 1 or importance > 10:
        raise MemoryRequestError("invalid_payload", "importance must be between 1 and 10")
    return importance


def _clean_source_message_id(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise MemoryRequestError("invalid_payload", "source_message_id must be an integer")
    try:
        source_message_id = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryRequestError("invalid_payload", "source_message_id must be an integer") from exc
    if source_message_id <= 0:
        raise MemoryRequestError("invalid_payload", "source_message_id must be positive")
    return source_message_id


def _clean_memory_key(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise MemoryRequestError("invalid_payload", "memory_key must be a string")
    memory_key = re.sub(r"\s+", "-", value.strip().casefold())
    if not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]{2,119}", memory_key):
        raise MemoryRequestError(
            "invalid_payload",
            "memory_key must be 3-120 lowercase ASCII letters, numbers, or ._:/-",
        )
    return memory_key


def _clean_update_mode(value: Any, memory_key: str | None) -> str:
    update_mode = str(value or "append").strip().casefold()
    if update_mode not in {"append", "replace"}:
        raise MemoryRequestError("invalid_payload", "update_mode must be append or replace")
    if update_mode == "replace" and not memory_key:
        raise MemoryRequestError(
            "invalid_payload",
            "memory_key is required when update_mode is replace",
        )
    if update_mode == "append" and memory_key:
        raise MemoryRequestError(
            "invalid_payload",
            "memory_key is only allowed when update_mode is replace",
        )
    return update_mode


def validate_memory_request(payload: Any, idempotency_key: str = "") -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MemoryRequestError("invalid_payload", "JSON body must be an object")

    allowed = {
        "assistant_id",
        "conversation_id",
        "source_message_id",
        "content",
        "reason",
        "title",
        "tags",
        "importance",
        "memory_key",
        "update_mode",
        "continuity_type", "thread_state", "continuity_data",
        "subject", "source_type", "continuity_value", "retention_class", "participants",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise MemoryRequestError(
            "invalid_payload",
            f"unsupported fields: {', '.join(sorted(unknown))}",
        )

    assistant_id = _clean_text(
        payload.get("assistant_id"),
        "assistant_id",
        required=True,
        minimum=1,
        maximum=MAX_IDENTIFIER_LENGTH,
    )
    conversation_id = _clean_text(
        payload.get("conversation_id"),
        "conversation_id",
        required=False,
        maximum=MAX_IDENTIFIER_LENGTH,
    )
    content = _clean_text(
        payload.get("content"),
        "content",
        required=True,
        minimum=5,
        maximum=MAX_CONTENT_LENGTH,
    )
    reason = _clean_text(
        payload.get("reason"),
        "reason",
        required=True,
        minimum=3,
        maximum=MAX_REASON_LENGTH,
    )
    title = _clean_text(
        payload.get("title"),
        "title",
        required=False,
        maximum=MAX_TITLE_LENGTH,
    )
    content_hash = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
    memory_key = _clean_memory_key(payload.get("memory_key"))
    update_mode = _clean_update_mode(payload.get("update_mode"), memory_key)
    continuity_type = str(payload.get("continuity_type") or "").strip().casefold()
    thread_state = str(payload.get("thread_state") or "").strip().casefold() or None
    try:
        continuity_data = validate_continuity_data(continuity_type, thread_state, payload.get("continuity_data"))
    except ContinuityDataError as exc:
        raise MemoryRequestError("invalid_payload", str(exc)) from exc
    subject = str(payload.get("subject") or "shared").strip().casefold()
    source_type = str(payload.get("source_type") or "natural_chat").strip().casefold()
    if subject not in {"yezi", "qi", "shared", "project", "other"}:
        raise MemoryRequestError("invalid_payload", "invalid subject")
    if source_type not in {"natural_chat", "persona_prompt", "code", "document", "quote", "roleplay", "tool_result", "system_meta", "unknown"}:
        raise MemoryRequestError("invalid_payload", "invalid source_type")
    continuity_value = _clean_importance(payload.get("continuity_value"))
    retention_class = str(payload.get("retention_class") or "normal").strip().casefold()
    if retention_class not in {"normal", "core"}:
        raise MemoryRequestError("invalid_payload", "invalid retention_class")
    participants = payload.get("participants", [])
    if not isinstance(participants, list):
        raise MemoryRequestError("invalid_payload", "participants must be an array")
    participants = list(dict.fromkeys(str(v).strip().casefold() for v in participants if str(v).strip().casefold() in {"yezi", "qi", "other"}))[:3]

    supplied_key = str(idempotency_key or "").strip()
    if supplied_key:
        if not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", supplied_key):
            raise MemoryRequestError(
                "invalid_idempotency_key",
                "Idempotency-Key must be 8-128 safe ASCII characters",
            )
        normalized_key = supplied_key
    else:
        normalized_key = hashlib.sha256(
            f"{assistant_id}\0{continuity_type}\0{content_hash}".encode("utf-8")
        ).hexdigest()

    return {
        "assistant_id": assistant_id,
        "conversation_id": conversation_id or None,
        "source_message_id": _clean_source_message_id(payload.get("source_message_id")),
        "content": content,
        "reason": reason,
        "title": title or None,
        "tags": _clean_tags(payload.get("tags")),
        "importance": _clean_importance(payload.get("importance")),
        "memory_key": memory_key,
        "update_mode": update_mode,
        "content_hash": content_hash,
        "idempotency_key": normalized_key,
        "continuity_type": continuity_type, "thread_state": thread_state,
        "continuity_schema_version": SCHEMA_VERSION, "continuity_data": continuity_data,
        "subject": subject, "source_type": source_type,
        "continuity_value": continuity_value, "retention_class": retention_class, "participants": participants,
    }


def _validate_source(client: Any, request_data: dict[str, Any]) -> None:
    source_message_id = request_data["source_message_id"]
    if source_message_id is None:
        return

    response = (
        client.table("chat_messages")
        .select("id,assistant_id,conversation_id")
        .eq("id", source_message_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        raise MemoryRequestError("source_not_found", "source message does not exist")

    source = response.data[0]
    if str(source.get("assistant_id") or "") != request_data["assistant_id"]:
        raise MemoryRequestError("source_mismatch", "source message belongs to another assistant")
    if (
        request_data["conversation_id"]
        and str(source.get("conversation_id") or "") != request_data["conversation_id"]
    ):
        raise MemoryRequestError("source_mismatch", "source message belongs to another conversation")


def _rpc_result(data: Any) -> dict[str, Any]:
    if isinstance(data, list):
        data = data[0] if data else None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as exc:
            raise MemoryRequestError(
                "request_store_failed",
                "memory request RPC returned invalid JSON",
                500,
            ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("request"), dict):
        raise MemoryRequestError(
            "request_store_failed",
            "memory request RPC returned an invalid response",
            500,
        )
    return data


def create_memory_request(
    payload: Any,
    idempotency_key: str = "",
    *,
    source: str = "orangechat_plugin",
    assistant_id: str | None = None,
) -> dict[str, Any]:
    if source not in {"orangechat_plugin", "mcp_memory"}:
        raise MemoryRequestError("invalid_source", "unsupported memory request source")
    if assistant_id is not None:
        if not isinstance(payload, dict):
            raise MemoryRequestError("invalid_payload", "JSON body must be an object")
        if "assistant_id" in payload:
            raise MemoryRequestError("invalid_payload", "assistant_id is server controlled")
        payload = {**payload, "assistant_id": assistant_id}
    request_data = validate_memory_request(payload, idempotency_key)
    if not _server_writes_allowed():
        raise MemoryRequestError(
            "database_permissions_unavailable",
            "an elevated Supabase server key is required",
            503,
        )

    client = get_client()
    if not client:
        raise MemoryRequestError(
            "database_unavailable",
            "Supabase server client is unavailable",
            503,
        )

    _validate_source(client, request_data)
    rpc_payload = {
        "p_assistant_id": request_data["assistant_id"],
        "p_conversation_id": request_data["conversation_id"],
        "p_source_message_id": request_data["source_message_id"],
        "p_content": request_data["content"],
        "p_title": request_data["title"],
        "p_tags": request_data["tags"],
        "p_importance": request_data["importance"],
        "p_reason": request_data["reason"],
        "p_content_hash": request_data["content_hash"],
        "p_idempotency_key": request_data["idempotency_key"],
        "p_rate_limit": max(1, min(60, int(cfg.MEMORY_REQUEST_RATE_LIMIT))),
        "p_memory_key": request_data["memory_key"],
        "p_update_mode": request_data["update_mode"],
        "p_continuity_type": request_data["continuity_type"], "p_thread_state": request_data["thread_state"],
        "p_continuity_schema_version": request_data["continuity_schema_version"], "p_continuity_data": request_data["continuity_data"],
        "p_subject": request_data["subject"],
        "p_source_type": request_data["source_type"], "p_continuity_value": request_data["continuity_value"],
        "p_retention_class": request_data["retention_class"], "p_participants": request_data["participants"],
        "p_source": source,
    }
    automatic = request_data["continuity_type"] in {"moment", "thread", "inside_joke"}
    rpc_name = "write_memory_direct_v1" if automatic else "create_memory_request_v4"
    if automatic:
        rpc_payload["p_reviewed_by"] = "orangechat_ai"
    try:
        response = client.rpc(rpc_name, rpc_payload).execute()
    except Exception as exc:
        if "memory_request_rate_limited" in str(exc).casefold():
            raise MemoryRequestError(
                "rate_limited",
                "too many memory applications; retry later",
                429,
            ) from exc
        raise MemoryRequestError(
            "request_store_failed",
            f"failed to store memory application: {type(exc).__name__}",
            500,
        ) from exc

    result = _rpc_result(response.data)
    request_row = result["request"]
    return {
        "request_id": request_row.get("id"),
        "status": request_row.get("status") or "pending",
        "created_at": request_row.get("created_at"),
        "created": bool(result.get("created")),
        "deduplicated": not bool(result.get("created")) and not bool(result.get("updated")),
        "memory_key": request_row.get("memory_key"),
        "update_mode": request_row.get("update_mode") or "append",
        "continuity_id": request_row.get("continuity_id"),
        "continuity_type": request_row.get("continuity_type"),
        "memory_id": request_row.get("memory_id"),
        "updated": bool(result.get("updated")),
        "requires_user_review": not automatic,
    }

