"""Validation and persistence for AI-initiated memory applications.

The OrangeChat plugin never receives a Supabase server key. It submits a
request to the gateway, which stores a pending application through an atomic
database function. `chat_messages` is only read when an optional source ID is
provided and is never modified here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from .config import cfg
from .db import get_client
from .memory_continuity_schema import SCHEMA_VERSION, ContinuityDataError, validate_continuity_data
from .memory_extract import _get_embedding_sync

log = logging.getLogger("gateway.memory_requests")


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


_SOURCE_TYPES = frozenset({
    "natural_chat", "persona_prompt", "code", "document", "quote",
    "roleplay", "tool_result", "system_meta", "unknown",
})


def _clean_source_type(value: Any) -> str | None:
    """source_type is optional: missing/null/blank normalize to NULL.

    非空时只接受固定枚举；绝不自动补 natural_chat 或 unknown。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryRequestError("invalid_payload", "source_type must be a string")
    source_type = value.strip().casefold()
    if not source_type:
        return None
    if source_type not in _SOURCE_TYPES:
        raise MemoryRequestError("invalid_payload", "invalid source_type")
    return source_type


def _clean_recall_scene(value: Any) -> str | None:
    """Normalize the AI-supplied recall scene without business length limits."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise MemoryRequestError("invalid_payload", "recall_scene must be a string")
    return re.sub(r"\s+", " ", value).strip() or None


def _clean_recall_tags(value: Any) -> list[str] | None:
    """Free-form recall tags: arrays only, no count, length, or value limits."""
    if value is None or value == "":
        return None
    if not isinstance(value, list):
        raise MemoryRequestError("invalid_payload", "recall_tags must be an array")
    tags: list[str] = []
    for candidate in value:
        if not isinstance(candidate, str):
            raise MemoryRequestError("invalid_payload", "recall_tags entries must be strings")
        tag = re.sub(r"\s+", " ", candidate).strip()
        if tag and tag not in tags:
            tags.append(tag)
    return tags or None


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
        "recall_scene",
        "recall_tags",
        "importance",
        "memory_key",
        "update_mode",
        "continuity_type", "thread_state", "continuity_data",
        "source_type",
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
    source_type = _clean_source_type(payload.get("source_type"))

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
        "recall_scene": _clean_recall_scene(payload.get("recall_scene")),
        "recall_tags": _clean_recall_tags(payload.get("recall_tags")),
        "importance": _clean_importance(payload.get("importance")),
        "memory_key": memory_key,
        "update_mode": update_mode,
        "content_hash": content_hash,
        "idempotency_key": normalized_key,
        "continuity_type": continuity_type, "thread_state": thread_state,
        "continuity_schema_version": SCHEMA_VERSION, "continuity_data": continuity_data,
        "source_type": source_type,
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


def _embedding_host() -> str:
    """Return only the configured embedding endpoint host for diagnostics."""
    try:
        return urlsplit(cfg.ANALYSIS_BASE_URL.strip()).netloc or "unconfigured"
    except ValueError:
        return "invalid-url"


def _recall_embedding(scene: str | None) -> list[float] | None:
    """Embed recall_scene for the vector recall channel.

    Scene-less requests skip the provider entirely and keep a NULL recall
    embedding. A scene-carrying request must embed successfully: provider or
    response failures raise here so the caller never reaches the formal-write
    or review RPC with a scene that could never be vector-recalled.
    """
    if not scene:
        return None
    # 定位日志：只含耗时、主机名和异常类型/错误码，不含密钥与内容。
    host = _embedding_host()
    started = time.monotonic()
    log.info("recall_embedding_start host=%s chars=%d", host, len(scene))
    try:
        embedding = _get_embedding_sync(scene)
    except Exception as exc:
        detail = getattr(exc, "code", None) or type(exc).__name__
        log.warning(
            "recall_embedding_failed duration_ms=%d host=%s detail=%s",
            int((time.monotonic() - started) * 1000),
            host,
            detail,
        )
        raise MemoryRequestError(
            "recall_embedding_failed",
            f"recall scene embedding failed: {type(exc).__name__}",
            503,
        ) from exc
    log.info(
        "recall_embedding_success duration_ms=%d host=%s",
        int((time.monotonic() - started) * 1000),
        host,
    )
    if not isinstance(embedding, list) or not embedding:
        raise MemoryRequestError(
            "recall_embedding_failed",
            "recall scene embedding response is empty or malformed",
            502,
        )
    return embedding


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
        "p_source_type": request_data["source_type"],
        "p_source": source,
        "p_recall_scene": request_data["recall_scene"],
        "p_recall_tags": request_data["recall_tags"],
    }
    automatic = request_data["continuity_type"] in {"moment", "thread", "inside_joke"}
    # 直接写入要求"场景非空且召回向量生成成功"；场景缺失或向量失败时改为
    # pending 审核，等待叶子补充召回场景，绝不写入没有向量的场景记忆。
    direct_writable = False
    if automatic and request_data["recall_scene"]:
        try:
            recall_embedding = _recall_embedding(request_data["recall_scene"])
            direct_writable = True
        except MemoryRequestError as exc:
            log.warning(
                "recall_scene embedding 生成失败，改为 pending 审核: %s", exc.code,
            )
    if automatic and direct_writable:
        rpc_name = "write_memory_direct_v1"
        rpc_payload["p_reviewed_by"] = "orangechat_ai"
        rpc_payload["p_recall_embedding"] = recall_embedding
    else:
        rpc_name = "create_memory_request_v4"
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
        "requires_user_review": rpc_name == "create_memory_request_v4",
    }

