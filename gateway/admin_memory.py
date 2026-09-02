"""Admin memory lifecycle service.

User-authored formal memories are written straight into ``public.memories``
through purpose-built transactional RPCs -- never through the review queue
and never through the generic admin data PATCH. The gateway owns every
internal field: assistant_id comes from the existing resolve mechanism,
source is fixed to ``manual``, verified/is_active/heat are fixed server
values, and the recall embedding is generated here BEFORE the RPC runs so a
failed embedding can never leave a half-written formal memory behind.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from .config import cfg
from .db import get_client
from .memory_continuity_schema import (
    CONTINUITY_TYPES,
    THREAD_STATES,
    ContinuityDataError,
    validate_continuity_data,
)
from .memory_extract import resolve_assistant_id
from .memory_requests import MemoryRequestError, _recall_embedding

log = logging.getLogger("gateway.admin_memory")

MIN_CONTENT_LENGTH = 5
MAX_CONTENT_LENGTH = 600
MAX_TITLE_LENGTH = 100
MAX_TAG_LENGTH = 200

SOURCE_TYPES = frozenset({
    "natural_chat", "persona_prompt", "code", "document", "quote",
    "roleplay", "tool_result", "system_meta", "unknown",
})
TIME_PRECISIONS = frozenset({"minute", "hour", "day", "approximate", "unknown"})
_MEMORY_TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}([T ][0-9:.+-]+Z?)?$")

# Fields the admin console may supply. Everything else -- assistant_id,
# source, verified, is_active, heat, content_hash, continuity_id,
# embeddings -- is server-owned, so an unknown key is refused outright.
_COMMON_FIELDS = frozenset({
    "content", "title", "tags", "importance", "source_type",
    "memory_time", "time_precision", "recall_scene", "recall_tags",
    "evidence_message_ids", "continuity_type", "thread_state", "continuity_data",
})


class AdminMemoryError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _clean_content(value: Any) -> str:
    if not isinstance(value, str):
        raise AdminMemoryError("admin_memory_invalid_content", "正文必须是字符串")
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) not in range(MIN_CONTENT_LENGTH, MAX_CONTENT_LENGTH + 1):
        raise AdminMemoryError(
            "admin_memory_invalid_content",
            f"正文长度必须在 {MIN_CONTENT_LENGTH} 到 {MAX_CONTENT_LENGTH} 个字符之间",
        )
    return text


def _clean_title(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AdminMemoryError("admin_memory_invalid_title", "标题必须是字符串")
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > MAX_TITLE_LENGTH:
        raise AdminMemoryError(
            "admin_memory_invalid_title",
            f"标题不能超过 {MAX_TITLE_LENGTH} 个字符",
        )
    return text or None


def _clean_admin_tags(value: Any, field: str) -> list[str]:
    """普通标签与召回标签共用规则：去首尾空白、不存空标签、组内去重、
    每条最多 200 字符、不限数量。"""
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise AdminMemoryError("admin_memory_invalid_tags", f"{field}必须是数组")
    tags: list[str] = []
    for candidate in value:
        if not isinstance(candidate, str):
            raise AdminMemoryError("admin_memory_invalid_tags", f"{field}的每一项都必须是字符串")
        tag = candidate.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise AdminMemoryError(
                "admin_memory_invalid_tags",
                f"{field}每条标签不能超过 {MAX_TAG_LENGTH} 个字符",
            )
        if tag not in tags:
            tags.append(tag)
    return tags


def _clean_importance(value: Any) -> int:
    if isinstance(value, bool):
        raise AdminMemoryError("admin_memory_invalid_importance", "重要性必须是 1 到 10 的整数")
    try:
        importance = int(value)
    except (TypeError, ValueError) as exc:
        raise AdminMemoryError("admin_memory_invalid_importance", "重要性必须是 1 到 10 的整数") from exc
    if importance < 1 or importance > 10:
        raise AdminMemoryError("admin_memory_invalid_importance", "重要性必须是 1 到 10 的整数")
    return importance


def _clean_source_type(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        # 空值保存为数据库 NULL，绝不落成空字符串。
        return None
    if not isinstance(value, str):
        raise AdminMemoryError("admin_memory_invalid_source_type", "来源类型必须是字符串")
    source_type = value.strip().casefold()
    if source_type not in SOURCE_TYPES:
        raise AdminMemoryError("admin_memory_invalid_source_type", "来源类型不是有效选项")
    return source_type


def _clean_time_precision(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise AdminMemoryError("admin_memory_invalid_time_precision", "时间精度必须是字符串")
    precision = value.strip().casefold()
    if precision not in TIME_PRECISIONS:
        raise AdminMemoryError("admin_memory_invalid_time_precision", "时间精度不是有效选项")
    return precision


def _clean_memory_time(value: Any) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        # 可选时间没有填写时保存 NULL，不伪造默认时间。
        return None
    if not isinstance(value, str):
        raise AdminMemoryError("admin_memory_invalid_memory_time", "记忆时间必须是字符串")
    text = value.strip()
    if len(text) > 40 or not _MEMORY_TIME_PATTERN.fullmatch(text):
        raise AdminMemoryError("admin_memory_invalid_memory_time", "记忆时间必须是 ISO 日期或日期时间")
    return text


def _clean_evidence_ids(value: Any) -> list[int]:
    if value is None or value == "":
        return []
    if not isinstance(value, list):
        raise AdminMemoryError("admin_memory_invalid_evidence", "证据消息 ID 必须是数组")
    ids: list[int] = []
    for candidate in value:
        if isinstance(candidate, bool):
            raise AdminMemoryError("admin_memory_invalid_evidence", "证据消息 ID 必须是正整数")
        try:
            entry_id = int(candidate)
        except (TypeError, ValueError) as exc:
            raise AdminMemoryError("admin_memory_invalid_evidence", "证据消息 ID 必须是正整数") from exc
        if entry_id <= 0:
            raise AdminMemoryError("admin_memory_invalid_evidence", "证据消息 ID 必须是正整数")
        if entry_id not in ids:
            ids.append(entry_id)
    return ids


def _clean_continuity(
    payload_type: Any,
    payload_state: Any,
    payload_data: Any,
) -> tuple[str, str | None, dict[str, Any]]:
    """六类统一校验：复用 memory_continuity_schema 的既有规则，绝不另立一套。"""
    continuity_type = str(payload_type or "").strip().casefold()
    if not continuity_type:
        raise AdminMemoryError("admin_memory_invalid_type", "必须选择六类连续感类型之一")
    thread_state = str(payload_state or "").strip().casefold() or None
    try:
        continuity_data = validate_continuity_data(
            continuity_type, thread_state, payload_data, automatic=False
        )
    except ContinuityDataError as exc:
        raise AdminMemoryError("admin_memory_invalid_continuity_data", f"连续感结构校验失败：{exc}") from exc
    return continuity_type, thread_state, continuity_data


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()


def _recall_embedding_for(scene: str | None) -> list[float] | None:
    """场景为空时直接返回 NULL 且不调用向量服务；有场景必须生成成功。"""
    if not scene:
        return None
    try:
        return _recall_embedding(scene)
    except MemoryRequestError as exc:
        raise AdminMemoryError(
            exc.code,
            "召回向量生成失败，记忆未写入；请稍后重试",
            exc.status_code if exc.status_code >= 500 else 503,
        ) from exc


def _server_writes_allowed() -> bool:
    return cfg.supabase_elevated_key_configured


def _require_client() -> Any:
    if not _server_writes_allowed():
        raise AdminMemoryError(
            "database_permissions_unavailable",
            "需要配置更高权限的 Supabase 服务端密钥",
            503,
        )
    client = get_client()
    if not client:
        raise AdminMemoryError("database_unavailable", "数据库当前不可用", 503)
    return client


def _resolve_assistant() -> str:
    try:
        return resolve_assistant_id()
    except Exception as exc:
        raise AdminMemoryError(
            "admin_memory_assistant_required",
            "无法确定助手身份，请检查 MEMORY_ASSISTANT_ID 配置或聊天记录",
            503,
        ) from exc


# ---------------------------------------------------------------------------
# Per-field validators shared by create (all fields) and edit (provided keys)
# ---------------------------------------------------------------------------

def _validate_field(key: str, value: Any) -> Any:
    if key == "content":
        return _clean_content(value)
    if key == "title":
        return _clean_title(value)
    if key == "tags":
        return _clean_admin_tags(value, "标签")
    if key == "recall_tags":
        return _clean_admin_tags(value, "召回标签")
    if key == "importance":
        return _clean_importance(value)
    if key == "source_type":
        return _clean_source_type(value)
    if key == "memory_time":
        return _clean_memory_time(value)
    if key == "time_precision":
        return _clean_time_precision(value)
    if key == "recall_scene":
        if value is None:
            return None
        if not isinstance(value, str):
            raise AdminMemoryError("admin_memory_invalid_recall_scene", "召回场景必须是字符串")
        return re.sub(r"\s+", " ", value).strip() or None
    if key == "evidence_message_ids":
        return _clean_evidence_ids(value)
    if key == "continuity_type":
        continuity_type = str(value or "").strip().casefold()
        if not continuity_type:
            raise AdminMemoryError("admin_memory_invalid_type", "必须选择六类连续感类型之一")
        return continuity_type
    if key == "thread_state":
        thread_state = str(value or "").strip().casefold() or None
        if thread_state is not None and thread_state not in THREAD_STATES:
            raise AdminMemoryError("admin_memory_invalid_thread_state", "线索状态不是有效选项")
        return thread_state
    if key == "continuity_data":
        if not isinstance(value, dict):
            raise AdminMemoryError("admin_memory_invalid_continuity_data", "连续感结构必须是对象")
        return value
    raise AdminMemoryError("admin_memory_unsupported_field", f"不支持的字段：{key}")


def _reject_unknown_fields(payload: dict[str, Any]) -> None:
    unknown = set(payload) - _COMMON_FIELDS
    if unknown:
        raise AdminMemoryError(
            "admin_memory_unsupported_field",
            "内部字段由服务端维护，不能提交：" + "、".join(sorted(unknown)),
        )


def _validated_optional_continuity(payload: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    return _clean_continuity(
        payload.get("continuity_type"),
        payload.get("thread_state"),
        payload.get("continuity_data"),
    )


def _rpc_result(data: Any, fallback_code: str) -> dict[str, Any]:
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise AdminMemoryError(fallback_code, "数据库返回了意外结果", 500)
    return data


_RPC_ERROR_MESSAGES: dict[str, tuple[str, int]] = {
    "admin_memory_not_found": ("记忆不存在或已被删除", 404),
    "admin_memory_not_editable": ("这条记忆当前不可编辑（已归档、被替代或未确认）", 409),
    "admin_memory_not_undoable": ("只有当前有效版本才能撤销", 409),
    "admin_memory_no_previous_version": ("这条记忆没有可撤销的类型修改", 409),
    "admin_memory_previous_conflict": ("恢复会造成两个版本同时生效，当前无法撤销", 409),
    "admin_memory_superseded": ("这条记忆已被新版本替代，禁止恢复", 409),
    "admin_memory_not_archived": ("这条记忆不在归档状态", 409),
    "admin_memory_already_archived": ("这条记忆已经处于归档状态", 409),
    "admin_memory_not_archivable": ("只有已确认的当前有效版本才能归档", 409),
    "admin_memory_continuity_conflict": ("恢复后同一连续感身份会有两个有效版本，当前无法恢复", 409),
    "admin_memory_key_conflict": ("恢复后同一主题键会有两个有效版本，当前无法恢复", 409),
    "admin_memory_chain_conflict": ("恢复会与现有版本链冲突，当前无法恢复", 409),
    "admin_memory_type_unchanged": ("请选择与当前不同的连续感类型", 400),
    "admin_memory_type_change_forbidden": ("修改类型请使用专门的类型修改流程", 400),
    "admin_memory_class_required": ("请先为这条未分类记忆补充连续感类型", 400),
    "admin_memory_source_unclassified": ("未分类记忆请先补充类型，再使用类型修改", 400),
    "admin_memory_content_exists": ("相同内容的记忆已存在", 409),
    "admin_memory_recall_vector_missing": ("召回向量缺失，属于内部状态异常", 500),
    "admin_memory_assistant_required": ("无法确定助手身份", 503),
    "admin_memory_invalid_content": ("正文长度必须在 5 到 600 个字符之间", 400),
    "admin_memory_invalid_title": ("标题格式不正确", 400),
    "admin_memory_invalid_tags": ("标签格式不正确（每条不超过 200 字符，组内不重复）", 400),
    "admin_memory_invalid_importance": ("重要性必须是 1 到 10 的整数", 400),
    "admin_memory_invalid_source_type": ("来源类型不是有效选项", 400),
    "admin_memory_invalid_time_precision": ("时间精度不是有效选项", 400),
    "admin_memory_invalid_memory_time": ("记忆时间格式不正确", 400),
    "admin_memory_invalid_evidence": ("证据消息 ID 格式不正确", 400),
    "admin_memory_invalid_type": ("必须选择六类连续感类型之一", 400),
    "admin_memory_invalid_continuity_data": ("连续感结构未通过校验", 400),
    "admin_memory_invalid_content_hash": ("内容哈希校验失败", 400),
    "admin_memory_invalid_patch": ("编辑载荷格式不正确", 400),
}


def _call_rpc(client: Any, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.rpc(name, payload).execute()
    except Exception as exc:
        message = str(exc).casefold()
        for code, (text, status) in _RPC_ERROR_MESSAGES.items():
            if code in message:
                raise AdminMemoryError(code, text, status) from exc
        # 结构化日志：RPC 名称、异常类型与完整堆栈；绝不含 Token、密钥、
        # 记忆正文、召回场景或向量内容。
        log.exception(
            "admin memory RPC failed: rpc=%s stage=call_database error_type=%s",
            name, type(exc).__name__,
        )
        raise AdminMemoryError("admin_memory_rpc_failed", "数据库操作未完成，请稍后重试", 500) from exc
    return _rpc_result(response.data, "admin_memory_rpc_failed")


def _memory_brief(row: dict[str, Any]) -> dict[str, Any]:
    memory = row.get("memory") if isinstance(row.get("memory"), dict) else row
    return {
        "memory_id": memory.get("id"),
        "continuity_id": memory.get("continuity_id"),
        "continuity_type": memory.get("continuity_type"),
        "is_active": memory.get("is_active"),
        "superseded_by_memory_id": memory.get("superseded_by_memory_id"),
    }


# ---------------------------------------------------------------------------
# 1. Create a user-authored formal memory
# ---------------------------------------------------------------------------

def create_admin_memory(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AdminMemoryError("admin_memory_unsupported_field", "请求体必须是 JSON 对象")
    _reject_unknown_fields(payload)
    missing = [key for key in ("content", "continuity_type", "continuity_data") if key not in payload]
    if missing:
        raise AdminMemoryError(
            "admin_memory_invalid_payload",
            "缺少必填字段：" + "、".join(missing),
        )

    content = _clean_content(payload.get("content"))
    continuity_type, thread_state, continuity_data = _validated_optional_continuity(payload)
    recall_scene = _validate_field("recall_scene", payload.get("recall_scene"))
    # 向量在进入数据库事务前生成；场景为空则保持 NULL 且不调用向量服务。
    recall_embedding = _recall_embedding_for(recall_scene)

    assistant_id = _resolve_assistant()
    client = _require_client()
    row = _call_rpc(client, "create_admin_memory_v1", {
        "p_assistant_id": assistant_id,
        "p_content": content,
        "p_content_hash": _content_hash(content),
        "p_title": _validate_field("title", payload.get("title")),
        "p_tags": _validate_field("tags", payload.get("tags")),
        "p_importance": _validate_field("importance", payload.get("importance", 5)),
        "p_source_type": _validate_field("source_type", payload.get("source_type")),
        "p_memory_time": _validate_field("memory_time", payload.get("memory_time")),
        "p_time_precision": _validate_field("time_precision", payload.get("time_precision")),
        "p_recall_scene": recall_scene,
        "p_recall_tags": _validate_field("recall_tags", payload.get("recall_tags")),
        "p_recall_embedding": recall_embedding,
        "p_continuity_type": continuity_type,
        "p_thread_state": thread_state,
        "p_continuity_data": continuity_data,
        "p_evidence_message_ids": _validate_field(
            "evidence_message_ids", payload.get("evidence_message_ids")
        ),
    })
    log.info(
        "admin_memory_op op=create memory_id=%s continuity_type=%s",
        row.get("memory_id"), continuity_type,
    )
    return {
        "memory_id": row.get("memory_id"),
        "continuity_id": row.get("continuity_id"),
        "source": row.get("source"),
        "verified": row.get("verified"),
        "is_active": row.get("is_active"),
        "heat": row.get("heat"),
    }


# ---------------------------------------------------------------------------
# 2. Edit a formal memory (ordinary fields plus same-type continuity_data)
# ---------------------------------------------------------------------------

def edit_admin_memory(memory_id: Any, payload: Any) -> dict[str, Any]:
    try:
        normalized_id = int(memory_id)
    except (TypeError, ValueError) as exc:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是整数") from exc
    if normalized_id <= 0:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是正整数")
    if not isinstance(payload, dict):
        raise AdminMemoryError("admin_memory_unsupported_field", "请求体必须是 JSON 对象")
    _reject_unknown_fields(payload)
    if not payload:
        raise AdminMemoryError("admin_memory_empty_patch", "没有提供任何要修改的字段")

    patch: dict[str, Any] = {}
    for key, value in payload.items():
        patch[key] = _validate_field(key, value)

    # Continuity fields are validated with full context by the RPC (it knows
    # the row's current class); here each supplied key is checked on its own,
    # and a supplied structure is validated against the supplied class.
    if "continuity_data" in patch and "continuity_type" not in patch:
        raise AdminMemoryError(
            "admin_memory_invalid_payload",
            "修改连续感结构时必须同时提供 continuity_type",
        )
    if "continuity_type" in patch:
        continuity_type = str(patch["continuity_type"]).strip().casefold()
        if continuity_type not in CONTINUITY_TYPES:
            raise AdminMemoryError("admin_memory_invalid_type", "必须选择六类连续感类型之一")
        patch["continuity_type"] = continuity_type
    if "thread_state" in patch:
        thread_state = str(patch["thread_state"] or "").strip().casefold() or None
        if thread_state is not None and thread_state not in THREAD_STATES:
            raise AdminMemoryError("admin_memory_invalid_thread_state", "线索状态不是有效选项")
        patch["thread_state"] = thread_state
    if "continuity_data" in patch:
        try:
            patch["continuity_data"] = validate_continuity_data(
                patch["continuity_type"], patch.get("thread_state"),
                patch["continuity_data"], automatic=False,
            )
        except ContinuityDataError as exc:
            raise AdminMemoryError(
                "admin_memory_invalid_continuity_data", f"连续感结构校验失败：{exc}"
            ) from exc

    content_hash = None
    if "content" in patch:
        content_hash = _content_hash(patch["content"])
    recall_embedding = None
    if "recall_scene" in patch:
        recall_embedding = _recall_embedding_for(patch["recall_scene"])

    assistant_id = _resolve_assistant()
    client = _require_client()
    row = _call_rpc(client, "edit_admin_memory_v1", {
        "p_memory_id": normalized_id,
        "p_patch": patch,
        "p_content_hash": content_hash,
        "p_recall_embedding": recall_embedding,
        "p_assistant_id": assistant_id,
    })
    memory = row.get("memory") or {}
    log.info(
        "admin_memory_op op=edit memory_id=%s fields=%s",
        normalized_id, ",".join(sorted(patch)),
    )
    return {
        "memory": memory,
        "memory_id": memory.get("id", normalized_id),
        "edited_fields": sorted(patch),
    }


# ---------------------------------------------------------------------------
# 3. Change the continuity class, creating a new version atomically
# ---------------------------------------------------------------------------

def change_memory_type(memory_id: Any, payload: Any) -> dict[str, Any]:
    try:
        normalized_id = int(memory_id)
    except (TypeError, ValueError) as exc:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是整数") from exc
    if normalized_id <= 0:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是正整数")
    if not isinstance(payload, dict):
        raise AdminMemoryError("admin_memory_unsupported_field", "请求体必须是 JSON 对象")
    _reject_unknown_fields(payload)
    missing = [key for key in ("content", "continuity_type", "continuity_data") if key not in payload]
    if missing:
        raise AdminMemoryError(
            "admin_memory_invalid_payload",
            "缺少必填字段：" + "、".join(missing),
        )

    content = _clean_content(payload.get("content"))
    continuity_type, thread_state, continuity_data = _validated_optional_continuity(payload)
    recall_scene = _validate_field("recall_scene", payload.get("recall_scene"))
    recall_embedding = _recall_embedding_for(recall_scene)

    client = _require_client()
    row = _call_rpc(client, "change_memory_type_v1", {
        "p_memory_id": normalized_id,
        "p_content": content,
        "p_content_hash": _content_hash(content),
        "p_title": _validate_field("title", payload.get("title")),
        "p_tags": _validate_field("tags", payload.get("tags")),
        "p_importance": _validate_field("importance", payload.get("importance", 5)),
        "p_source_type": _validate_field("source_type", payload.get("source_type")),
        "p_memory_time": _validate_field("memory_time", payload.get("memory_time")),
        "p_time_precision": _validate_field("time_precision", payload.get("time_precision")),
        "p_recall_scene": recall_scene,
        "p_recall_tags": _validate_field("recall_tags", payload.get("recall_tags")),
        "p_recall_embedding": recall_embedding,
        "p_continuity_type": continuity_type,
        "p_thread_state": thread_state,
        "p_continuity_data": continuity_data,
        "p_evidence_message_ids": _validate_field(
            "evidence_message_ids", payload.get("evidence_message_ids")
        ),
    })
    memory = row.get("memory") or {}
    log.info(
        "admin_memory_op op=change_type memory_id=%s new_memory_id=%s new_type=%s",
        normalized_id, memory.get("id"), continuity_type,
    )
    return {
        "memory": memory,
        "memory_id": memory.get("id"),
        "previous_version_id": row.get("previous_version_id"),
        "removed_version_id": row.get("removed_version_id"),
    }


# ---------------------------------------------------------------------------
# 4. Undo the most recent type change of the current version
# ---------------------------------------------------------------------------

def undo_memory_type_change(memory_id: Any) -> dict[str, Any]:
    try:
        normalized_id = int(memory_id)
    except (TypeError, ValueError) as exc:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是整数") from exc
    if normalized_id <= 0:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是正整数")

    client = _require_client()
    row = _call_rpc(client, "undo_memory_type_change_v1", {"p_memory_id": normalized_id})
    log.info(
        "admin_memory_op op=undo_type_change memory_id=%s restored=%s deleted=%s",
        normalized_id, row.get("restored_memory_id"), row.get("undo_deleted"),
    )
    return {
        "restored_memory_id": row.get("restored_memory_id"),
        "undo_memory_id": row.get("undo_memory_id"),
        "undo_deleted": bool(row.get("undo_deleted")),
    }


# ---------------------------------------------------------------------------
# 5. Restore a naturally archived memory
# ---------------------------------------------------------------------------

def restore_archived_memory(memory_id: Any) -> dict[str, Any]:
    try:
        normalized_id = int(memory_id)
    except (TypeError, ValueError) as exc:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是整数") from exc
    if normalized_id <= 0:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是正整数")

    client = _require_client()
    row = _call_rpc(client, "restore_archived_memory_v1", {"p_memory_id": normalized_id})
    memory = row.get("memory") or {}
    log.info(
        "admin_memory_op op=restore memory_id=%s heat=%s",
        memory.get("id", normalized_id), memory.get("heat"),
    )
    return {
        "memory": memory,
        "memory_id": memory.get("id", normalized_id),
        "heat": memory.get("heat"),
    }


# ---------------------------------------------------------------------------
# 6. Archive a current formal memory (the only is_active=false write path)
# ---------------------------------------------------------------------------

def archive_admin_memory(memory_id: Any) -> dict[str, Any]:
    try:
        normalized_id = int(memory_id)
    except (TypeError, ValueError) as exc:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是整数") from exc
    if normalized_id <= 0:
        raise AdminMemoryError("admin_memory_invalid_memory_id", "记忆 ID 必须是正整数")

    client = _require_client()
    row = _call_rpc(client, "archive_admin_memory_v1", {"p_memory_id": normalized_id})
    memory = row.get("memory") or {}
    log.info(
        "admin_memory_op op=archive memory_id=%s is_active=%s",
        memory.get("id", normalized_id), memory.get("is_active"),
    )
    return {
        "memory": memory,
        "memory_id": memory.get("id", normalized_id),
        "is_active": memory.get("is_active"),
    }
