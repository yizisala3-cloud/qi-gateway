"""User review service for pending AI memory applications."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .config import cfg
from .db import get_client
from .memory_requests import MemoryRequestError, _clean_memory_key, _recall_embedding


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


def _related_memory_id(value: Any) -> int:
    if isinstance(value, bool):
        raise MemoryRequestError("invalid_review", "related_memory_id must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise MemoryRequestError("invalid_review", "related_memory_id must be an integer") from exc
    if result <= 0:
        raise MemoryRequestError("invalid_review", "related_memory_id must be positive")
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
        "memory_key", "update_mode", "related_memory_id",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise MemoryRequestError(
            "invalid_review",
            f"unsupported fields: {', '.join(sorted(unknown))}",
        )
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"approve", "reject", "merge", "duplicate", "conflict"}:
        raise MemoryRequestError(
            "invalid_review",
            "action must be approve, reject, merge, duplicate, or conflict",
        )

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
            "related_memory_id": None,
        }

    if action in {"duplicate", "conflict"}:
        disallowed = {
            "content", "title", "tags", "importance", "memory_key", "update_mode",
        }
        if disallowed.intersection(payload):
            raise MemoryRequestError(
                "invalid_review",
                f"{action} reviews cannot include memory edits",
            )
        if "related_memory_id" not in payload:
            raise MemoryRequestError(
                "invalid_review",
                "related_memory_id is required for relational reviews",
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
            "related_memory_id": _related_memory_id(payload.get("related_memory_id")),
        }

    related_memory_id = None
    if action == "merge":
        if "related_memory_id" not in payload:
            raise MemoryRequestError(
                "invalid_review",
                "related_memory_id is required for merge reviews",
            )
        if "memory_key" in payload or "update_mode" in payload:
            raise MemoryRequestError(
                "invalid_review",
                "merge reviews cannot include memory_key or update_mode",
            )
        related_memory_id = _related_memory_id(payload.get("related_memory_id"))

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
        "related_memory_id": related_memory_id,
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


AI_REVIEWABLE_TYPES = ("moment", "thread", "inside_joke")


def _request_recall_embedding(client: Any, request_id: int) -> list[float] | None:
    """Embed the request's own recall_scene for the vector recall channel.

    The review flow never edits recall_scene, so the approved memory keeps the
    scene the writing AI supplied. A scene-carrying request must embed
    successfully before the review RPC runs: read or embedding failures raise
    here so the request keeps its original status instead of producing a formal
    memory that could never be vector-recalled.
    """
    try:
        response = (
            client.table("memory_requests")
            .select("recall_scene")
            .eq("id", request_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise MemoryRequestError(
            "recall_embedding_failed",
            f"failed to read recall_scene for embedding: {type(exc).__name__}",
            500,
        ) from exc
    rows = response.data if isinstance(response.data, list) else []
    scene = str((rows[0] if rows else {}).get("recall_scene") or "").strip()
    return _recall_embedding(scene)


def list_reviewable_memory_requests(assistant_id: str, limit: int = 50) -> list[dict[str, Any]]:
    assistant = _text(assistant_id, "assistant_id", 160)
    if not assistant:
        raise MemoryRequestError("invalid_review", "assistant_id is required")
    if not _server_writes_allowed():
        raise MemoryRequestError(
            "database_permissions_unavailable",
            "an elevated Supabase server key is required",
            503,
        )
    client = get_client()
    if not client:
        raise MemoryRequestError("database_unavailable", "Supabase is unavailable", 503)
    try:
        response = (
            client.table("memory_requests")
            .select(
                "id,content,title,tags,importance,reason,status,source,created_at,"
                "memory_key,update_mode,continuity_type,thread_state,continuity_data,"
                "subject,source_type,continuity_value,retention_class,participants,"
                "recall_scene,recall_tags"
            )
            .eq("assistant_id", assistant)
            .eq("status", "pending")
            .in_("continuity_type", list(AI_REVIEWABLE_TYPES))
            .order("created_at", desc=True)
            .limit(max(1, min(int(limit), 50)))
            .execute()
        )
    except Exception as exc:
        raise MemoryRequestError(
            "review_list_failed",
            f"failed to list reviewable memory applications: {type(exc).__name__}",
            500,
        ) from exc
    return response.data if isinstance(response.data, list) else []


def _assert_review_scope(client: Any, request_id: int, assistant_id: str, allowed_types: tuple[str, ...]) -> None:
    try:
        response = (
            client.table("memory_requests")
            .select("id,assistant_id,status,continuity_type")
            .eq("id", request_id)
            .eq("assistant_id", assistant_id)
            .limit(1)
            .execute()
        )
    except Exception as exc:
        raise MemoryRequestError("review_failed", "failed to verify review scope", 500) from exc
    row = response.data[0] if isinstance(response.data, list) and response.data else None
    if not row or row.get("status") != "pending" or row.get("continuity_type") not in allowed_types:
        raise MemoryRequestError(
            "request_not_reviewable",
            "memory application is unavailable or requires user review",
            404,
        )


def review_memory_request(
    request_id: Any,
    payload: Any,
    *,
    assistant_id: str | None = None,
    reviewed_by: str = "gateway_admin",
    allowed_types: tuple[str, ...] | None = None,
) -> dict[str, Any]:
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

    if assistant_id is not None:
        _assert_review_scope(
            client,
            review["request_id"],
            _text(assistant_id, "assistant_id", 160),
            allowed_types or AI_REVIEWABLE_TYPES,
        )

    rpc_payload = {
        "p_request_id": review["request_id"],
        "p_action": review["action"],
        "p_content": review["content"],
        "p_title": review["title"],
        "p_tags": review["tags"],
        "p_importance": review["importance"],
        "p_content_hash": review["content_hash"],
        "p_reviewed_by": reviewed_by,
        "p_review_note": review["review_note"],
        "p_memory_key": review["memory_key"],
        "p_update_mode": review["update_mode"],
        "p_related_memory_id": review["related_memory_id"],
        "p_recall_embedding": (
            _request_recall_embedding(client, review["request_id"])
            if review["action"] in {"approve", "merge"}
            else None
        ),
    }
    try:
        response = client.rpc("review_memory_request_v5", rpc_payload).execute()
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
        if "memory_request_unclassified_legacy" in message:
            raise MemoryRequestError("unclassified_legacy_request", "legacy request has no safe continuity classification", 409) from exc
        if "memory_request_related_memory_not_found" in message:
            raise MemoryRequestError(
                "related_memory_not_found",
                "the selected memory was not found",
                404,
            ) from exc
        if "memory_request_related_memory_inactive" in message:
            raise MemoryRequestError(
                "related_memory_inactive",
                "the selected memory is no longer active and verified",
                409,
            ) from exc
        if (
            "memory_request_related_memory_required" in message
            or "memory_request_relation_disallows_edits" in message
            or "memory_request_merge_disallows_update_mode" in message
        ):
            raise MemoryRequestError(
                "invalid_review",
                "database rejected relational review values",
                400,
            ) from exc
        if "memory_request_merge_unchanged" in message:
            raise MemoryRequestError(
                "merge_unchanged",
                "the merged content is unchanged; mark it as duplicate instead",
                409,
            ) from exc
        if "memory_request_merge_content_exists" in message:
            raise MemoryRequestError(
                "merge_content_exists",
                "the merged content already exists as another memory",
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
        "related_memory_id": result.get("related_memory_id"),
    }


def review_ai_memory_request(assistant_id: str, request_id: Any, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise MemoryRequestError("invalid_review", "JSON body must be an object")
    rows = list_reviewable_memory_requests(assistant_id, 50)
    try:
        normalized_id = int(request_id)
    except (TypeError, ValueError):
        normalized_id = 0
    selected = next((row for row in rows if int(row.get("id") or 0) == normalized_id), None)
    if selected is None:
        raise MemoryRequestError(
            "request_not_reviewable",
            "memory application is unavailable or requires user review",
            404,
        )
    normalized = dict(payload)
    if str(normalized.get("action") or "").strip().casefold() in {"approve", "merge"}:
        normalized.setdefault("content", selected.get("content"))
        normalized.setdefault("title", selected.get("title"))
        normalized.setdefault("tags", selected.get("tags") or [])
        normalized.setdefault("importance", selected.get("importance") or 5)
    return review_memory_request(
        normalized_id,
        normalized,
        assistant_id=assistant_id,
        reviewed_by="orangechat_ai",
        allowed_types=AI_REVIEWABLE_TYPES,
    )

