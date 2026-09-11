"""Independent rumination continuity path.

反刍路径与连续感快速路径相互独立：独立游标（memory_rumination_cursors）、
独立 pipeline 标记（memory_digest_runs.pipeline='rumination'）、独立运行记录
与 claim/heartbeat 租约、独立提示词，以及原子批次提交 RPC
（commit_rumination_batch）。``public.chat_messages`` 在本模块中只读。

批次规则：首批只取当时最近的 120 条；日常批次最多 120 条、不足 60 条不处理；
成功提交后游标才推进到该批最后一条；失败不推进、重试幂等。

模型可见范围（严格限定）：
- 本批聊天原文；
- 正式 memories 中未完成（open/paused）thread 的正文与必要结构；
- 反刍自己产生的 pending/rejected/duplicate/conflict 申请。
resolved thread 正文与其他五类正式记忆正文绝不进入模型输入。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import cfg
from .db import get_client
from .memory_continuity_schema import (
    SCHEMA_VERSION,
    ContinuityDataError,
    validate_continuity_data,
)
from .memory_continuity_shadow import (
    ShadowPreviewError,
    _clean_content,
    _contains_secret,
    _normalize_memory_time,
    _parse_time,
    _response_diagnostic,
    _resolve_message_time,
)
from .memory_extract import (
    DigestPipelineError,
    _clean_message_content,
    _get_embedding_sync,
    _update_heartbeat,
    resolve_assistant_id,
)

log = logging.getLogger("gateway.memory_rumination")

CST = timezone(timedelta(hours=8))

FIRST_RUN_MAX_MESSAGES = 120
RUMINATION_BATCH_MAX = 120
RUMINATION_BATCH_MIN = 60
MAX_EVIDENCE_IDS = 8
STALE_RUN_MINUTES = 30
REQUEST_INPUT_LIMIT = 50

OPERATION_TYPES = frozenset({
    "ignore", "create_memory", "create_tracked_thread", "adopt_thread",
    "evidence_only", "update_thread", "pause_thread", "resume_thread",
    "resolve_thread", "create_request",
})
# Direct rumination writes never include review-gated classes; thread has its
# own lifecycle ops and is never a terminal class.
DIRECT_WRITE_TYPES = frozenset({"moment", "inside_joke"})
REQUEST_TYPES = frozenset({"episode", "profile", "interaction_rule"})
THREAD_OP_TYPES = frozenset({
    "adopt_thread", "evidence_only", "update_thread",
    "pause_thread", "resume_thread", "resolve_thread",
})
_THREAD_STATE_KINDS = {
    "resolved": "resolve_thread",
    "paused": "pause_thread",
    "open": "resume_thread",
}
RUMINATION_TRIGGERS = frozenset({
    "rumination_scheduled", "rumination_manual", "rumination_retry",
})
MEMORY_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:/-]{2,119}")
SOURCE_TYPES = frozenset({
    "natural_chat", "persona_prompt", "code", "document", "quote",
    "roleplay", "tool_result", "system_meta", "unknown",
})

# Per-op allowed keys; anything else is a model contract violation and the
# whole batch must fail rather than the field being silently ignored.
_OP_FIELDS: dict[str, frozenset[str]] = {
    "ignore": frozenset({"op", "reason", "evidence_message_ids"}),
    "create_memory": frozenset({
        "op", "reason", "evidence_message_ids", "continuity_type", "content",
        "title", "continuity_data", "importance", "confidence", "source_type",
        "recall_scene", "recall_tags", "memory_time", "time_precision",
        "absorbed_fast_path_memory_ids",
    }),
    "create_tracked_thread": frozenset({
        "op", "reason", "evidence_message_ids", "content", "title",
        "thread_state", "continuity_data", "memory_key", "importance",
        "confidence", "source_type", "recall_scene", "recall_tags",
        "memory_time", "time_precision",
    }),
    "adopt_thread": frozenset({
        "op", "reason", "evidence_message_ids", "target_memory_id",
        "target_memory_key", "target_continuity_id", "target_content_hash",
        "target_thread_state",
        "memory_key", "content", "title", "thread_state", "continuity_data",
        "importance", "confidence", "source_type", "recall_scene",
        "recall_tags", "memory_time", "time_precision",
    }),
    "evidence_only": frozenset({
        "op", "reason", "evidence_message_ids", "target_memory_id",
        "target_memory_key", "target_continuity_id", "target_content_hash",
        "target_thread_state",
    }),
    "update_thread": frozenset({
        "op", "reason", "evidence_message_ids", "target_memory_id",
        "target_memory_key", "target_continuity_id", "target_content_hash",
        "target_thread_state",
        "content", "title", "thread_state", "continuity_data", "memory_key",
        "memory_time", "time_precision",
    }),
    "pause_thread": frozenset({
        "op", "reason", "evidence_message_ids", "target_memory_id",
        "target_memory_key", "target_continuity_id", "target_content_hash",
        "target_thread_state",
        "content", "title", "continuity_data", "memory_key", "memory_time",
        "time_precision",
    }),
    "resume_thread": frozenset({
        "op", "reason", "evidence_message_ids", "target_memory_id",
        "target_memory_key", "target_continuity_id", "target_content_hash",
        "target_thread_state",
        "content", "title", "continuity_data", "memory_key", "memory_time",
        "time_precision",
    }),
    "resolve_thread": frozenset({
        "op", "reason", "evidence_message_ids", "target_memory_id",
        "target_memory_key", "target_continuity_id", "target_content_hash",
        "target_thread_state",
        "content", "title", "continuity_data", "memory_key", "memory_time",
        "time_precision",
    }),
    "create_request": frozenset({
        "op", "reason", "evidence_message_ids", "continuity_type", "content",
        "title", "continuity_data", "memory_key", "importance", "confidence",
        "source_type", "recall_scene", "recall_tags", "memory_time",
        "time_precision", "absorbed_fast_path_memory_ids",
    }),
}

RETIRED_FIELDS = frozenset({"memory_type"})


def _normalize_legacy_operation_fields(
    raw: dict[str, Any], op_type: str,
) -> dict[str, Any]:
    """Strip known retired fields that the model sometimes emits.

    Only handles `memory_type` (retired in favour of `continuity_type`).
    Other unknown fields are left in place for the strict whitelist check.
    """
    normalized = dict(raw)
    if "memory_type" not in normalized:
        return normalized
    if "memory_type" not in RETIRED_FIELDS:
        return normalized

    continuity_type = normalized.get("continuity_type")
    memory_type_value = normalized["memory_type"]

    if not continuity_type:
        raise RuminationPipelineError(
            "model_schema_error",
            f"op {op_type}: memory_type is a retired field and no "
            "continuity_type was provided; use continuity_type instead",
        )

    normalized_type = str(continuity_type).strip().casefold()
    legacy_type = str(memory_type_value).strip().casefold()
    if legacy_type and legacy_type != normalized_type:
        raise RuminationPipelineError(
            "model_schema_error",
            f"op {op_type}: memory_type='{legacy_type}' conflicts with "
            f"continuity_type='{normalized_type}'; memory_type is retired, "
            "use continuity_type only",
        )

    # Compatible: strip the legacy field, keep continuity_type.
    normalized.pop("memory_type")
    return normalized


_DEFAULT_OPERATION_REASONS = {
    "ignore": "本批证据未形成可执行记忆操作",
    "create_memory": "反刍根据本批原文提取的独立记忆",
    "create_tracked_thread": "反刍根据本批原文发现的长期进程",
    "adopt_thread": "反刍根据本批原文接管快速路径线索",
    "evidence_only": "反刍根据本批原文补充线索证据",
    "update_thread": "反刍根据本批原文更新未完线索",
    "pause_thread": "反刍根据本批原文暂停未完线索",
    "resume_thread": "反刍根据本批原文恢复未完线索",
    "resolve_thread": "反刍根据本批原文结束未完线索",
    "create_request": "反刍根据本批原文生成审核申请",
}

RUMINATION_SYSTEM_PROMPT = """你是“反刍连续感”提取器，负责在独立每日管线中回看一段聊天原文，维护长期进程并产出结构化操作。你与“连续感总结”快速路径相互独立，不要模仿它的输出格式。

## 输入
- <chat_log>：本批带 id、北京时间和 role 的聊天原文，是你唯一的事实依据。
- <unfinished_threads>：正式记忆中所有未完成（open/paused）thread 的当前状态，包括快速路径产物与反刍长期 thread；maintained_by 标记维护方。
- <rumination_requests>：仅反刍自己产生、状态为 pending/rejected/duplicate/conflict 的审核申请。

你看不到 resolved/已关闭 thread 的正文，也看不到 moment、episode、inside_joke、profile、interaction_rule 的正式正文，也看不到其他来源的申请。输入中出现的 id 才是真实存在的。

## 操作语义（互斥，逐项选择）
- ignore：本批证据不值得形成或推进任何记忆；仍须列出依据消息。
- create_memory：把本批原文整合成一条有独立含义的普通记忆；只允许 moment 或 inside_joke。
- create_tracked_thread：从原文发现明确、值得跨会话追踪的新长期进程，直接建立正式 thread（不需要审核）。memory_key 必须是稳定主题键，同一进程以后永远复用同一 key，不得按日期随机换 key。
- adopt_thread：接管快速路径的未完成 thread（maintained_by=fast_path），为其分配稳定 memory_key；可附新的完整当前状态正文（提供 content 与完整 continuity_data 时会创建新版本，否则只做接管）。
- evidence_only：thread 当前状态没有变化，本批只是重复表达；把本批新证据并入当前版本，不改正文、不建版本。
- update_thread：进程有实质进展，输出完整当前状态的新版本（不只写增量），复用原 memory_key；若正文与当前版本实质相同，会被自动降级为只补证据。
- pause_thread：原文有明确暂停、搁置、推迟的证据才能用；长期没提到不是暂停。
- resume_thread：暂停的进程在原文中重新开始推进。
- resolve_thread：原文明确说明完成/解决/结束，或新事实明确满足该 thread 已有 closure_criteria。沉默、时间流逝或你觉得不重要都不能作为完成依据。必须给出 closure_summary、closure_reason、closed_at。
- create_request：生成 episode、profile 或 interaction_rule 的审核申请（不直接写正式记忆）。

## 硬性规则
1. 以原文为事实依据；即使正式 thread 已存在，原文中的修正信息也要尊重。
2. 不把短期小事强行升级为长期进程；允许把零散短期片段整合为 moment 或其他合适分类。
3. 不提取泛化的长期用户偏好；不把一次行为推断为稳定人格、习惯或互动规则；只有真实依据充分的 profile/interaction_rule 才用 create_request。
4. 一个进程完成后，可以生成一条或多条有独立含义的终态记忆（episode/moment/inside_joke/profile/interaction_rule），多条必须表达不同信息，不得同义改写；终态绝不允许 thread 类型。
5. 正文忠于事实：AI 参与的经历可以写成共同经历；AI 未参与时不得写成“我们共同完成”。
6. 每项操作的 evidence_message_ids 必须是本批 <chat_log> 中真实存在、且直接支持该操作的消息 id（1-8 条）；引用输入列表之外的 id 会被整批拒绝。
7. 目标 thread 操作的 target_memory_id 必须来自 <unfinished_threads>，并且必须逐字回显该 thread 的快照字段：target_memory_key（无 key 的 fast_path thread 回显 null）、target_continuity_id、target_content_hash、target_thread_state。快照缺失、写错或与输入不一致时整批被拒绝；回显快照用于确保你提交时的判断仍基于读取时的状态。
8. content 是完整、独立可理解的正文（5-600 字符），不写“今天/昨天”等相对时间；绝对时间放 memory_time，无法可靠确定时填 null 且 time_precision=unknown。
9. recall_scene 是以后触发召回的场景描述，不是正文复制；无法确定填 null。recall_tags 来自原文真实依据，没有就留空数组。
10. 不输出 API Key、Token、密码、service_role 等秘密。
11. 已有实质相同的 pending 反刍申请时不要再提交；rejected/duplicate/conflict 的申请只有在出现拒绝之后的新原文证据时才能重新提交。
12. 没有新证据的长期进程不要重写；不确定时选择 ignore 或 evidence_only。
13. 同一条 thread 在本批出现多个连续进展时（例如上午完成、下午部署、晚上验收），必须把它们合并为一个操作：content 写最终完整当前状态，evidence_message_ids 取各进展消息的并集，按证据时间得到的最终状态决定操作类型；不要为同一 thread 输出多个版本操作。
14. create_request 与 create_memory（moment/inside_joke）可以带可选的 absorbed_fast_path_memory_ids：仅当该操作明确吸收或覆盖某条快速路径正式记忆时才列出其 memory_id（最多 8 条）。只能引用 <absorbable_fast_path_memories> 中列出的候选 ID——该列表为空时禁止输出任何吸收 ID；候选之外的任何 ID（包括碰巧真实存在的记忆）都会整批拒绝。候选只提供最小元数据（id、类型、标题、证据 ID），不含完整正文；吸收判断以你本批原文证据为准。不得因为 evidence_message_ids 相同就吸收所有候选——相同原文可以合法支撑不同分类和不同语义。不能确定目标时省略该字段。thread 的生命周期请使用专门的 thread 操作，不要通过吸收来处置 thread。

## 字段规则
- memory_type 是已经退役的旧字段，绝对禁止输出。
- 只能使用 continuity_type。
- 不要输出旧版记忆格式中的 memory_type。
- 不要从聊天原文、代码、旧提示词或示例中复制 memory_type。
- create_memory 的 continuity_type 只能是 moment 或 inside_joke。
- create_request 的 continuity_type 只能是 episode、profile 或 interaction_rule。
- interaction_rule 必须使用符合格式的稳定 memory_key。
- episode/profile 不得输出 memory_key。
- thread 的 memory_key 必须是 3-120 位小写 ASCII 稳定主题键，只能包含 a-z、0-9、点、下划线、冒号、斜杠和连字符。
- 中文标题不能直接当作 memory_key。
- 如果 unfinished_threads 中的 fast_path thread 的 memory_key 为 null，
  而你无法根据聊天内容确定稳定的 ASCII memory_key，
  不要输出 adopt_thread。请选择 ignore。

## 输出 JSON
只返回严格 JSON，不要 Markdown、解释或代码围栏：
{"operations":[{"op":"update_thread","target_memory_id":12,"target_memory_key":"topic.example","target_continuity_id":"21111111-1111-1111-1111-1111111111a1","target_content_hash":"<输入中的 content_hash>","target_thread_state":"open","reason":"进程有实质进展","content":"完整当前状态……","thread_state":"open","continuity_data":{"open_question":"...","current_state":"...","next_expected":"...","closure_criteria":["..."],"closure_summary":null,"closure_reason":null,"opened_at":null,"closed_at":null,"abstract_retrieval_hints":[],"concrete_retrieval_hints":[]},"evidence_message_ids":[101,102]}]}
没有可执行操作时必须返回 {"operations":[]}。"""


class RuminationPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _client():
    client = get_client()
    if not client:
        raise RuminationPipelineError(
            "database_unavailable", "Supabase server client is unavailable", 503,
        )
    return client


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _model_config() -> tuple[str, str, str]:
    """Rumination provider with explicit fallback to the continuity provider."""
    base_url = cfg.RUMINATION_BASE_URL.strip() or cfg.CONTINUITY_BASE_URL.strip()
    api_key = cfg.RUMINATION_API_KEY.strip() or cfg.CONTINUITY_API_KEY.strip()
    model = cfg.RUMINATION_MODEL.strip() or cfg.CONTINUITY_MODEL.strip()
    return base_url, api_key, model


def _rumination_analysis_configured() -> bool:
    base_url, api_key, model = _model_config()
    return bool(base_url and api_key and model)


def resolve_rumination_assistant_id() -> str:
    """复用现有 assistant_id 解析方式，绝不默认。"""
    try:
        return resolve_assistant_id()
    except DigestPipelineError as exc:
        raise RuminationPipelineError(exc.code, str(exc), 404) from exc


def _rpc_object(name: str, params: dict[str, Any]) -> dict[str, Any]:
    response = _client().rpc(name, params).execute()
    data = response.data
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise RuminationPipelineError(
            "database_response_error", f"{name} returned an invalid response", 500,
        )
    return data


def get_rumination_cursor(assistant_id: str) -> dict[str, Any]:
    return _rpc_object(
        "get_or_create_rumination_cursor", {"p_assistant_id": assistant_id},
    )


# ---------------------------------------------------------------------------
# Batch planning (pure; directly unit-tested)
# ---------------------------------------------------------------------------

def plan_rumination_batches(
    message_ids: list[int],
    *,
    initialized: bool,
) -> list[tuple[int, int, int]]:
    """Return (first_id, last_id, count) batches for the given ascending ids.

    首批（initialized=False）只消费传入的最新消息（调用方负责截取最近 120 条）。
    日常批次：每次最多 120 条真实消息行，剩余不足 60 条停止并留到次日。
    生产管线按 120 条一页增量实现同一规则（见 run_rumination_digest），
    该纯函数保留作为批次规则的规范实现与测试锚点。
    """
    if not initialized:
        if not message_ids:
            return []
        return [(int(message_ids[0]), int(message_ids[-1]), len(message_ids))]
    batches: list[tuple[int, int, int]] = []
    remaining = [int(value) for value in message_ids]
    while len(remaining) >= RUMINATION_BATCH_MIN:
        batch = remaining[:RUMINATION_BATCH_MAX]
        batches.append((batch[0], batch[-1], len(batch)))
        remaining = remaining[len(batch):]
    return batches


def first_run_message_ids(all_ids_desc: list[int]) -> list[int]:
    """首次正式运行只取最近 120 条（按 id 升序返回），更早历史一律不进入反刍。"""
    return sorted(int(value) for value in all_ids_desc[:FIRST_RUN_MAX_MESSAGES])


# ---------------------------------------------------------------------------
# Model input assembly (visibility rules enforced here)
# ---------------------------------------------------------------------------

def _fetch_message_ids(assistant_id: str, *, after: int, limit: int) -> list[int]:
    response = (
        _client().table("chat_messages")
        .select("id")
        .eq("assistant_id", assistant_id)
        .gt("id", int(after))
        .order("id")
        .limit(max(1, int(limit)))
        .execute()
    )
    return [int(row["id"]) for row in (response.data or [])]


def _fetch_latest_message_ids(assistant_id: str, limit: int) -> list[int]:
    response = (
        _client().table("chat_messages")
        .select("id")
        .eq("assistant_id", assistant_id)
        .order("id", desc=True)
        .limit(max(1, int(limit)))
        .execute()
    )
    return [int(row["id"]) for row in (response.data or [])]


def _fetch_batch_rows(assistant_id: str, first_id: int, last_id: int) -> list[dict[str, Any]]:
    response = (
        _client().table("chat_messages")
        .select("id,assistant_id,conversation_id,role,content,created_at")
        .eq("assistant_id", assistant_id)
        .gte("id", int(first_id))
        .lte("id", int(last_id))
        .order("id")
        .execute()
    )
    return response.data or []


def _normalize_batch_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Clean, trim and fold assistant retries inside the fixed id window."""
    normalized: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: int(item.get("id") or 0)):
        role = str(row.get("role") or "").strip().casefold()
        if role not in {"user", "assistant"}:
            continue
        content = _clean_message_content(role, row.get("content"))
        if not content:
            continue
        item = {
            "id": int(row["id"]),
            "conversation_id": str(row.get("conversation_id") or ""),
            "role": role,
            "content": content,
            "source_time": _resolve_message_time(row.get("created_at"), row.get("content")),
        }
        if (
            role == "assistant"
            and normalized
            and normalized[-1]["role"] == "assistant"
            and normalized[-1]["conversation_id"] == item["conversation_id"]
        ):
            normalized[-1] = item
        else:
            normalized.append(item)
    return normalized


def _load_unfinished_threads(assistant_id: str) -> list[dict[str, Any]]:
    """正式未完成 thread（open/paused）：快速路径与反刍产物都在内。

    加载当前 assistant 下全部 verified、active、open/paused 的 thread，不按
    创建时间或 ID 截断——较旧但仍未完成的 thread 不得永久不可见。resolved/
    dissolved/abandoned thread 与其他五类正文绝不进入模型输入。
    """
    response = (
        _client().table("memories")
        .select(
            "id,memory_key,continuity_id,thread_state,maintained_by,producer_path,"
            "content,content_hash,continuity_data,evidence_message_ids,"
            "evidence_start_time,evidence_end_time,created_at"
        )
        .eq("assistant_id", assistant_id)
        .eq("continuity_type", "thread")
        .eq("verified", "verified")
        .eq("is_active", True)
        .in_("thread_state", ["open", "paused"])
        .order("id")
        .execute()
    )
    return response.data or []


def _load_absorbable_candidates(
    assistant_id: str,
    batch_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fast-path formal memories that overlap THIS batch's evidence.

    候选只暴露吸收判断所需的最小元数据（memory_id、continuity_type、title、
    evidence_message_ids、memory_key、thread_state）：五类正式记忆的完整正文
    仍不进入模型输入，标题（≤100 字符的短标签）仅用于把候选与本批原文对齐。
    候选围绕本批证据交集生成，绝不把全部正式记忆列给模型；closed thread 不
    是合法吸收目标，直接排除。
    """
    evidence_ids = sorted({int(row["id"]) for row in batch_rows})
    if not evidence_ids:
        return []
    # PostgREST client's ov() uses ",".join(values) internally, which raises
    # TypeError when values are ints. Pass strings; the DB column stays
    # bigint[] and the overlaps semantics are unchanged.
    response = (
        _client().table("memories")
        .select(
            "id,continuity_type,producer_path,is_active,verified,"
            "evidence_message_ids,title,memory_key,thread_state"
        )
        .eq("assistant_id", assistant_id)
        .eq("producer_path", "fast_path")
        .eq("verified", "verified")
        .eq("is_active", True)
        .overlaps("evidence_message_ids", [str(v) for v in evidence_ids])
        .order("id", desc=True)
        .execute()
    )
    candidates = []
    for row in response.data or []:
        if (
            row.get("continuity_type") == "thread"
            and row.get("thread_state") in ("resolved", "dissolved", "abandoned")
        ):
            continue
        candidates.append({
            "memory_id": int(row["id"]),
            "continuity_type": row.get("continuity_type"),
            "title": str(row.get("title") or "")[:100] or None,
            "evidence_message_ids": row.get("evidence_message_ids") or [],
            "memory_key": row.get("memory_key"),
            "thread_state": row.get("thread_state"),
        })
    return candidates


def _load_own_requests(assistant_id: str) -> list[dict[str, Any]]:
    """只读取反刍自己产生且状态为 pending/rejected/duplicate/conflict 的申请。"""
    response = (
        _client().table("memory_requests")
        .select(
            "id,status,continuity_type,content,reason,evidence_message_ids,"
            "review_note,created_at"
        )
        .eq("assistant_id", assistant_id)
        .eq("source", "rumination")
        .in_("status", ["pending", "rejected", "duplicate", "conflict"])
        .order("created_at", desc=True)
        .limit(REQUEST_INPUT_LIMIT)
        .execute()
    )
    return response.data or []


def _compact_thread(row: dict[str, Any]) -> dict[str, Any]:
    data = row.get("continuity_data") if isinstance(row.get("continuity_data"), dict) else {}
    return {
        "memory_id": int(row["id"]),
        "memory_key": row.get("memory_key"),
        "continuity_id": str(row.get("continuity_id") or ""),
        "thread_state": row.get("thread_state"),
        "maintained_by": row.get("maintained_by"),
        "content": str(row.get("content") or ""),
        "content_hash": row.get("content_hash"),
        "open_question": data.get("open_question"),
        "current_state": data.get("current_state"),
        "next_expected": data.get("next_expected"),
        "closure_criteria": data.get("closure_criteria") or [],
        "evidence_message_ids": row.get("evidence_message_ids") or [],
        "evidence_start_time": (
            row.get("evidence_start_time").isoformat()
            if isinstance(row.get("evidence_start_time"), datetime)
            else row.get("evidence_start_time")
        ),
        "evidence_end_time": (
            row.get("evidence_end_time").isoformat()
            if isinstance(row.get("evidence_end_time"), datetime)
            else row.get("evidence_end_time")
        ),
    }


def _compact_request(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "request_id": int(row["id"]),
        "status": row.get("status"),
        "continuity_type": row.get("continuity_type"),
        "content": str(row.get("content") or ""),
        "reason": str(row.get("reason") or ""),
        "evidence_message_ids": row.get("evidence_message_ids") or [],
        "review_note": row.get("review_note"),
        "created_at": (
            row.get("created_at").isoformat()
            if isinstance(row.get("created_at"), datetime)
            else row.get("created_at")
        ),
    }


def _fmt_str(value: Any, default: str = "") -> str:
    """Explicit str() boundary: never let a non-string reach join()."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _format_rumination_conversation(
    messages: list[dict[str, Any]],
) -> str:
    """反刍专用的聊天原文格式化函数。

    与 Shadow Preview 的 _format_conversation 分离：每个字段在加入 lines
    列表前都经过显式 str() 转换，确保 "\\n".join() 的每个元素都是 str。
    生产 Supabase 返回的 conversation_id、id 等字段类型可能与测试 fixture
    不同（int vs str），此函数作为类型边界屏障。
    """
    lines: list[str] = []
    previous_conversation: str | None = None
    for message in messages:
        conversation_id = _fmt_str(message.get("conversation_id")) or "unknown"
        if conversation_id != previous_conversation:
            lines.append(
                f"<conversation id={json.dumps(conversation_id, ensure_ascii=False)}>"
            )
            previous_conversation = conversation_id
        msg_id = _fmt_str(message.get("id"))
        source_time = _fmt_str(message.get("source_time")) or "unknown"
        role = _fmt_str(message.get("role"))
        content = _fmt_str(message.get("content"))
        lines.append(f"[id={msg_id} t={source_time} role={role}] {content}")
    return "\n".join(lines)


def build_model_input(
    messages: list[dict[str, Any]],
    threads: list[dict[str, Any]],
    own_requests: list[dict[str, Any]],
    absorbable_candidates: list[dict[str, Any]] | None = None,
) -> str:
    chat_log = _format_rumination_conversation(messages)
    threads_json = json.dumps(
        [_compact_thread(row) for row in threads], ensure_ascii=False, indent=1,
    )
    requests_json = json.dumps(
        [_compact_request(row) for row in own_requests], ensure_ascii=False, indent=1,
    )
    candidates_json = json.dumps(
        absorbable_candidates or [], ensure_ascii=False, indent=1,
    )
    return (
        f"<chat_log>\n{chat_log}\n</chat_log>\n\n"
        f"<unfinished_threads>\n{threads_json}\n</unfinished_threads>\n\n"
        f"<rumination_requests>\n{requests_json}\n</rumination_requests>\n\n"
        f"<absorbable_fast_path_memories>\n{candidates_json}\n"
        f"</absorbable_fast_path_memories>"
    )


# ---------------------------------------------------------------------------
# Output parsing and validation (hallucinated ids are hard failures)
# ---------------------------------------------------------------------------

def _clean_text_field(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _key_safe_summary(value: Any) -> str:
    """Safe summary of a memory_key for structured logging — never the full key."""
    key = str(value or "")
    import re as _re
    pattern_ok = bool(_re.fullmatch(
        r"[a-z0-9][a-z0-9._:/-]{2,119}", key,
    ))
    prefix = key[:2] if len(key) >= 2 else key
    suffix = key[-2:] if len(key) >= 4 else ""
    return (
        f"value_length={len(key)} pattern_valid={pattern_ok} "
        f"prefix='{prefix}' suffix='{suffix}'"
    )


def _log_key_validation_error(op_type: str, field: str, value: Any) -> None:
    log.warning(
        "Rumination operation validation failed: stage=parse_output "
        "op=%s field=%s %s error=invalid_memory_key",
        op_type, field, _key_safe_summary(value),
    )


def _normalize_memory_key(value: Any, *, op_type: str = "", field: str = "memory_key") -> str | None:
    key = str(value or "").strip().casefold()
    if not key:
        return None
    if not MEMORY_KEY_PATTERN.fullmatch(key):
        _log_key_validation_error(op_type, field, key)
        raise RuminationPipelineError(
            "model_schema_error",
            f"invalid memory_key: {key[:40]} (op={op_type} field={field})",
        )
    return key


def parse_rumination_output(
    text: str,
    *,
    evidence_times: dict[int, str | None],
    threads_by_id: dict[int, dict[str, Any]],
    absorbable_ids: set[int] = frozenset(),
) -> list[dict[str, Any]]:
    """Validate the model's operation list. Any hallucinated id, unknown op,
    illegal type or secret raises instead of being silently accepted."""
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise RuminationPipelineError(
            "model_parse_error", f"Rumination model returned invalid JSON: {exc}",
        ) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("operations"), list):
        raise RuminationPipelineError(
            "model_schema_error", "Rumination model JSON has no operations array",
        )
    # 操作数量没有产品上限：合法操作数由模型输出预算与单批 120 条消息自然
    # 约束；任何非法操作仍整批拒绝。
    raw_ops = payload["operations"]

    validated: list[dict[str, Any]] = []
    seen_content_hashes: set[str] = set()
    for raw in raw_ops:
        if not isinstance(raw, dict):
            raise RuminationPipelineError("model_schema_error", "operation must be an object")
        op_type = str(raw.get("op") or "").strip().casefold()
        if op_type not in OPERATION_TYPES:
            raise RuminationPipelineError(
                "model_schema_error", f"unknown rumination op: {op_type or '(missing)'}",
            )
        raw = _normalize_legacy_operation_fields(raw, op_type)
        unknown = set(raw) - _OP_FIELDS[op_type]
        if unknown:
            raise RuminationPipelineError(
                "model_schema_error",
                f"op {op_type} has unsupported fields: {', '.join(sorted(unknown))}",
            )

        reason = _clean_text_field(raw.get("reason"), 500)
        if not reason:
            # The model occasionally omits the reason field. For a private
            # gateway, a default reason is better than killing the batch —
            # content, continuity_type and evidence_ids are the critical
            # fields; reason is human-readable metadata only.
            reason = _DEFAULT_OPERATION_REASONS.get(op_type, "反刍路径自动提取")
            log.info(
                "Rumination operation reason missing; using default: op=%s",
                op_type,
            )

        evidence_ids: list[int] = []
        raw_evidence = raw.get("evidence_message_ids")
        if not isinstance(raw_evidence, list) or not raw_evidence:
            raise RuminationPipelineError(
                "model_schema_error", f"op {op_type} requires evidence_message_ids",
            )
        for candidate in raw_evidence:
            if isinstance(candidate, bool):
                raise RuminationPipelineError(
                    "model_schema_error", "evidence ids must be integers",
                )
            try:
                message_id = int(candidate)
            except (TypeError, ValueError) as exc:
                raise RuminationPipelineError(
                    "model_schema_error", "evidence ids must be integers",
                ) from exc
            if message_id not in evidence_times:
                # 幻觉或批次之外的消息 ID：整批拒绝，绝不静默接受。
                raise RuminationPipelineError(
                    "model_schema_error",
                    f"evidence id {message_id} is outside this batch",
                )
            if message_id not in evidence_ids:
                evidence_ids.append(message_id)
        if len(evidence_ids) > MAX_EVIDENCE_IDS:
            raise RuminationPipelineError(
                "model_schema_error",
                f"op {op_type} cites more than {MAX_EVIDENCE_IDS} evidence ids",
            )

        op: dict[str, Any] = {
            "op": op_type,
            "reason": reason,
            "evidence_message_ids": evidence_ids,
        }

        if op_type in THREAD_OP_TYPES:
            target_id = raw.get("target_memory_id")
            if isinstance(target_id, bool):
                raise RuminationPipelineError(
                    "model_schema_error", "target_memory_id must be an integer",
                )
            try:
                target_id = int(target_id)
            except (TypeError, ValueError) as exc:
                raise RuminationPipelineError(
                    "model_schema_error", "target_memory_id must be an integer",
                ) from exc
            target = threads_by_id.get(target_id)
            if target is None:
                raise RuminationPipelineError(
                    "model_schema_error",
                    f"target_memory_id {target_id} is not an unfinished thread in this input",
                )
            op["target_memory_id"] = target_id
            # Optimistic target snapshot: the model must echo exactly what it
            # read. Missing, malformed, or stale snapshots fail the batch.
            op.update(_validated_target_snapshot(raw, target))
            # A stable key may only accompany a thread op to fill in the key
            # of a fast-path takeover; it never re-keys a rumination thread.
            if raw.get("memory_key"):
                op["memory_key"] = _normalize_memory_key(raw.get("memory_key"), op_type=op_type)
            if op_type == "pause_thread" and target.get("thread_state") != "open":
                raise RuminationPipelineError(
                    "model_schema_error", "pause_thread requires an open thread",
                )
            if op_type == "resume_thread" and target.get("thread_state") != "paused":
                raise RuminationPipelineError(
                    "model_schema_error", "resume_thread requires a paused thread",
                )

        if op_type == "ignore":
            validated.append(op)
            continue

        content = re.sub(r"\s+", " ", str(raw.get("content") or "")).strip()
        requires_content = op_type in {
            "create_memory", "create_tracked_thread", "update_thread",
            "pause_thread", "resume_thread", "resolve_thread", "create_request",
        }
        if requires_content or (op_type == "adopt_thread" and content):
            if len(content) < 5 or len(content) > 600:
                raise RuminationPipelineError(
                    "model_schema_error",
                    f"op {op_type} content must be 5-600 characters",
                )
        title = _clean_text_field(raw.get("title"), 100) or None
        op_content_hash = (
            hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
            if content else None
        )
        if op_content_hash and op_content_hash in seen_content_hashes:
            continue
        if _contains_secret(content, title, reason):
            raise RuminationPipelineError(
                "model_schema_error", "rumination output contains a suspected secret",
            )

        importance = raw.get("importance", 5)
        try:
            importance = max(1, min(10, int(importance)))
        except (TypeError, ValueError):
            importance = 5
        confidence = raw.get("confidence", 0.6)
        try:
            confidence = round(max(0.0, min(1.0, float(confidence))), 3)
        except (TypeError, ValueError):
            confidence = 0.6

        source_type: str | None = None
        raw_source_type = raw.get("source_type")
        if raw_source_type is not None:
            source_type = str(raw_source_type).strip().casefold() or None
            if source_type is not None and source_type not in SOURCE_TYPES:
                raise RuminationPipelineError(
                    "model_schema_error", f"invalid source_type: {source_type}",
                )

        memory_time, time_precision = _normalize_memory_time(
            raw.get("memory_time"),
            str(raw.get("time_precision") or "unknown").strip().casefold(),
        )
        recall_scene = _clean_text_field(raw.get("recall_scene"), 600) or None
        recall_tags: list[str] = []
        raw_recall_tags = raw.get("recall_tags")
        if raw_recall_tags is not None:
            if not isinstance(raw_recall_tags, list):
                raise RuminationPipelineError(
                    "model_schema_error", "recall_tags must be an array",
                )
            for candidate in raw_recall_tags:
                tag = _clean_text_field(candidate, 120)
                if tag and tag not in recall_tags:
                    recall_tags.append(tag)

        op.update({
            "content": content,
            "title": title,
            "importance": importance,
            "confidence": confidence,
            "source_type": source_type,
            "recall_scene": recall_scene,
            "recall_tags": recall_tags,
            "memory_time": memory_time,
            "time_precision": time_precision,
        })
        if op_content_hash:
            op["content_hash"] = op_content_hash
            seen_content_hashes.add(op_content_hash)

        if op_type == "create_memory":
            continuity_type = str(raw.get("continuity_type") or "").strip().casefold()
            if continuity_type not in DIRECT_WRITE_TYPES:
                raise RuminationPipelineError(
                    "model_schema_error",
                    "create_memory only supports moment or inside_joke",
                )
            op["continuity_type"] = continuity_type
            op["continuity_data"] = _validated_continuity_data(
                continuity_type, None, raw.get("continuity_data"),
            )
            op["absorbed_fast_path_memory_ids"] = _validated_absorb_targets(
                raw.get("absorbed_fast_path_memory_ids"), absorbable_ids,
            )
        elif op_type == "create_tracked_thread":
            op["memory_key"] = _require_memory_key(raw.get("memory_key"), op_type=op_type)
            thread_state = str(raw.get("thread_state") or "").strip().casefold() or "open"
            if thread_state != "open":
                raise RuminationPipelineError(
                    "model_schema_error", "create_tracked_thread must start as open",
                )
            op["continuity_data"] = _validated_continuity_data(
                "thread", "open", raw.get("continuity_data"),
            )
        elif op_type == "adopt_thread":
            key = _normalize_memory_key(raw.get("memory_key"), op_type=op_type)
            if key:
                op["memory_key"] = key
            # Keyless fast_path target with no key from the model:
            # gracefully skip this single operation instead of killing
            # the batch. Other operations continue to be processed.
            if (
                target.get("maintained_by") == "fast_path"
                and target.get("memory_key") is None
                and not key
            ):
                skip_reason = (
                    "无法接管 keyless fast_path thread：模型未提供合法 "
                    "memory_key，本批跳过该接管操作"
                )
                validated.append({
                    "op": "ignore",
                    "reason": skip_reason,
                    "evidence_message_ids": list(op["evidence_message_ids"]),
                })
                continue
            state = str(raw.get("thread_state") or "").strip().casefold() or None
            if state and state != target.get("thread_state"):
                raise RuminationPipelineError(
                    "model_schema_error",
                    "adopt_thread cannot change thread_state; use pause/resume/resolve ops",
                )
            if content:
                op["continuity_data"] = _validated_continuity_data(
                    "thread", target.get("thread_state"), raw.get("continuity_data"),
                )
        elif op_type in {"update_thread", "pause_thread", "resume_thread", "resolve_thread"}:
            expected_state = {
                "update_thread": target.get("thread_state"),
                "pause_thread": "paused",
                "resume_thread": "open",
                "resolve_thread": "resolved",
            }[op_type]
            if op_type == "update_thread":
                state = str(raw.get("thread_state") or "").strip().casefold() or None
                if state and state != target.get("thread_state"):
                    raise RuminationPipelineError(
                        "model_schema_error",
                        "update_thread cannot change thread_state; use pause/resume/resolve ops",
                    )
            op["thread_state"] = expected_state
            op["continuity_data"] = _validated_continuity_data(
                "thread", expected_state, raw.get("continuity_data"),
            )
        elif op_type == "create_request":
            continuity_type = str(raw.get("continuity_type") or "").strip().casefold()
            if continuity_type not in REQUEST_TYPES:
                raise RuminationPipelineError(
                    "model_schema_error",
                    "create_request only supports episode, profile or interaction_rule",
                )
            op["continuity_type"] = continuity_type
            op["continuity_data"] = _validated_continuity_data(
                continuity_type, None, raw.get("continuity_data"),
            )
            key = _normalize_memory_key(raw.get("memory_key"), op_type=op_type)
            if continuity_type == "interaction_rule":
                if not key:
                    raise RuminationPipelineError(
                        "model_schema_error",
                        "interaction_rule requests require a stable memory_key",
                    )
                op["memory_key"] = key
            elif key:
                raise RuminationPipelineError(
                    "model_schema_error",
                    "episode/profile requests must not carry a memory_key",
                )
            op["absorbed_fast_path_memory_ids"] = _validated_absorb_targets(
                raw.get("absorbed_fast_path_memory_ids"), absorbable_ids,
            )

        validated.append(op)
    return validated


def _validated_continuity_data(
    continuity_type: str,
    thread_state: str | None,
    value: Any,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuminationPipelineError(
            "model_schema_error", f"{continuity_type} continuity_data must be an object",
        )
    try:
        return validate_continuity_data(
            continuity_type, thread_state, value, automatic=False,
        )
    except ContinuityDataError as exc:
        raise RuminationPipelineError(
            "model_schema_error", f"invalid {continuity_type} continuity_data: {exc}",
        ) from exc


def _require_memory_key(value: Any, *, op_type: str = "") -> str:
    key = _normalize_memory_key(value, op_type=op_type)
    if not key:
        raise RuminationPipelineError(
            "model_schema_error", "a stable memory_key is required",
        )
    return key


# ---------------------------------------------------------------------------
# Target snapshot and absorption-target validation
# ---------------------------------------------------------------------------

_UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
)
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
MAX_ABSORB_TARGETS = 8


def _validated_target_snapshot(
    raw: dict[str, Any],
    target: dict[str, Any],
) -> dict[str, Any]:
    """Echoed target snapshot must match the loaded unfinished thread."""
    target_key = target.get("memory_key")
    snapshot_key = raw.get("target_memory_key")
    if (str(snapshot_key).strip().casefold() if snapshot_key else None) != (
        str(target_key).strip().casefold() if target_key else None
    ):
        raise RuminationPipelineError(
            "model_schema_error",
            f"target_memory_key snapshot mismatch for memory {target.get('id')}",
        )

    snapshot_id = str(raw.get("target_continuity_id") or "").strip().casefold()
    loaded_id = str(target.get("continuity_id") or "").strip().casefold()
    if not _UUID_PATTERN.fullmatch(snapshot_id) or snapshot_id != loaded_id:
        raise RuminationPipelineError(
            "model_schema_error",
            f"target_continuity_id snapshot mismatch for memory {target.get('id')}",
        )

    snapshot_hash = str(raw.get("target_content_hash") or "").strip().casefold()
    loaded_hash = str(target.get("content_hash") or "").strip().casefold()
    if not _HASH_PATTERN.fullmatch(snapshot_hash) or snapshot_hash != loaded_hash:
        raise RuminationPipelineError(
            "model_schema_error",
            f"target_content_hash snapshot mismatch for memory {target.get('id')}",
        )

    snapshot_state = str(raw.get("target_thread_state") or "").strip().casefold()
    if snapshot_state != target.get("thread_state"):
        raise RuminationPipelineError(
            "model_schema_error",
            f"target_thread_state snapshot mismatch for memory {target.get('id')}",
        )
    return {
        "target_memory_key": snapshot_key if snapshot_key else None,
        "target_continuity_id": snapshot_id,
        "target_content_hash": snapshot_hash,
        "target_thread_state": snapshot_state,
    }


def _validated_absorb_targets(
    value: Any,
    absorbable_ids: set[int],
) -> list[int]:
    """Explicit fast-path absorption list; never auto-derived.

    模型只能引用本次输入 <absorbable_fast_path_memories> 中明确提供的候选
    ID：候选集合为空时禁止输出任何吸收 ID；候选之外的 ID（包括碰巧存在的
    真实 fast-path 记忆 ID）一律整批拒绝。数据库在提交事务内继续复核结构
    条件（同 assistant、fast_path 生产、verified、active、非闭合 thread）。
    """
    if value is None:
        return []
    if not isinstance(value, list) or not value:
        raise RuminationPipelineError(
            "model_schema_error",
            "absorbed_fast_path_memory_ids must be a non-empty array when provided",
        )
    if not absorbable_ids:
        raise RuminationPipelineError(
            "model_schema_error",
            "no absorbable fast-path memories were provided in this input",
        )
    ids: list[int] = []
    for candidate in value:
        if isinstance(candidate, bool):
            raise RuminationPipelineError(
                "model_schema_error", "absorbed ids must be integers",
            )
        try:
            memory_id = int(candidate)
        except (TypeError, ValueError) as exc:
            raise RuminationPipelineError(
                "model_schema_error", "absorbed ids must be integers",
            ) from exc
        if memory_id <= 0:
            raise RuminationPipelineError(
                "model_schema_error", "absorbed ids must be positive",
            )
        if memory_id not in absorbable_ids:
            raise RuminationPipelineError(
                "model_schema_error",
                f"absorbed id {memory_id} is not an absorbable candidate in this input",
            )
        if memory_id not in ids:
            ids.append(memory_id)
    if len(ids) > MAX_ABSORB_TARGETS:
        raise RuminationPipelineError(
            "model_schema_error",
            f"at most {MAX_ABSORB_TARGETS} absorption targets per request",
        )
    return ids


# ---------------------------------------------------------------------------
# Same-batch consecutive progress merging
# ---------------------------------------------------------------------------

def _op_evidence_time(op: dict[str, Any], evidence_times: dict[int, str | None]) -> str:
    """Latest real evidence time of an op, or a hard failure when the ops on
    one thread cannot be ordered by genuine evidence."""
    times = [
        evidence_times.get(message_id)
        for message_id in op["evidence_message_ids"]
    ]
    valid = [time for time in times if time]
    if not valid:
        raise RuminationPipelineError(
            "model_schema_error",
            "multiple operations on one thread cannot be ordered by real "
            "evidence time: no valid evidence times",
        )
    return max(valid)


def _merged_thread_op(
    target_id: int,
    entries: list[tuple[int, dict[str, Any]]],
    target: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    """Merge several same-thread ops into one final operation.

    entries are already sorted by real evidence time. The final current state
    is derived by walking the state machine; the merged op keeps the union of
    evidence, the last content-bearing body and the latest state's continuity
    data, so one batch produces exactly one new active version per thread.
    """
    chain = target.get("thread_state")
    resolved_seen = False
    for _, op in entries:
        kind = op["op"]
        if resolved_seen:
            raise RuminationPipelineError(
                "model_schema_error",
                "contradictory operations: progress after resolve_thread on "
                f"memory {target_id}",
            )
        if kind == "resolve_thread":
            if chain not in ("open", "paused"):
                raise RuminationPipelineError(
                    "model_schema_error",
                    f"contradictory operations: resolve_thread on a {chain} "
                    f"thread (memory {target_id})",
                )
            chain = "resolved"
            resolved_seen = True
        elif kind == "pause_thread":
            if chain != "open":
                raise RuminationPipelineError(
                    "model_schema_error",
                    f"contradictory operations: pause_thread on a {chain} "
                    f"thread (memory {target_id})",
                )
            chain = "paused"
        elif kind == "resume_thread":
            if chain != "paused":
                raise RuminationPipelineError(
                    "model_schema_error",
                    f"contradictory operations: resume_thread on a {chain} "
                    f"thread (memory {target_id})",
                )
            chain = "open"
        # update_thread / adopt_thread / evidence_only keep the state.

    content_ops = [op for _, op in entries if op.get("content")]
    if chain == "resolved":
        merged_kind = "resolve_thread"
    elif chain != target.get("thread_state"):
        merged_kind = _THREAD_STATE_KINDS[chain]
    elif content_ops:
        merged_kind = "update_thread"
    else:
        merged_kind = "evidence_only"
    evidence_union = sorted({
        message_id
        for _, op in entries
        for message_id in op["evidence_message_ids"]
    })
    merged: dict[str, Any] = {
        "op": merged_kind,
        "target_memory_id": target_id,
        "reason": "；".join(
            dict.fromkeys(op["reason"] for _, op in entries)
        )[:500],
        "evidence_message_ids": evidence_union,
        "thread_state": chain,
    }
    # Every op in the group echoed the same validated snapshot; keep it.
    for key in (
        "target_memory_key", "target_continuity_id",
        "target_content_hash", "target_thread_state",
    ):
        if entries[0][1].get(key) is not None:
            merged[key] = entries[0][1][key]
    # Body and state data come from the last op that carries them (the latest
    # progress represents the final current state).
    for key in ("content", "continuity_data", "title", "memory_time", "time_precision"):
        for _, op in reversed(entries):
            if op.get(key) is not None:
                merged[key] = op[key]
                break
    # Scalar presentation fields prefer the latest explicit value.
    for key in ("importance", "confidence", "source_type", "recall_scene", "recall_tags"):
        for _, op in reversed(entries):
            if op.get(key) is not None:
                merged[key] = op[key]
                break
    # A stable key proposed for a fast-path takeover must survive the merge.
    for _, op in entries:
        if op.get("memory_key"):
            merged["memory_key"] = op["memory_key"]
            break
    if merged.get("content"):
        merged["content_hash"] = hashlib.sha256(
            merged["content"].casefold().encode("utf-8"),
        ).hexdigest()
    # 状态未变且最终正文与当前版本相同：不重写正文，只合并证据。
    if (
        merged_kind == "update_thread"
        and merged.get("content")
        and str(target.get("content") or "")
        and merged["content"].casefold() == str(target["content"]).casefold()
    ):
        merged_kind = "evidence_only"
        merged["op"] = merged_kind
        for key in ("content", "content_hash", "continuity_data"):
            merged.pop(key, None)
    first_index = entries[0][0]
    return first_index, merged


def merge_thread_operations(
    ops: list[dict[str, Any]],
    *,
    evidence_times: dict[int, str | None],
    threads_by_id: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    """同批内同一条 thread 的多个连续进展合并为一个最终操作。

    按真实证据时间（消息时间）排序后沿状态机推进；无法排序或操作互相矛盾
    时整批失败。最终每条 thread 在本批至多产生一个新 active 版本，中间进展
    的消息全部并入合并后操作的 evidence_message_ids。
    """
    grouped: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for index, op in enumerate(ops):
        if op.get("op") in THREAD_OP_TYPES:
            target_id = op["target_memory_id"]
            grouped.setdefault(target_id, []).append((index, op))

    replacements: dict[int, dict[str, Any] | None] = {}
    for target_id, entries in grouped.items():
        if len(entries) == 1:
            continue
        target = threads_by_id.get(target_id)
        if target is None:  # pragma: no cover - parse already validated
            raise RuminationPipelineError(
                "model_schema_error",
                f"target_memory_id {target_id} is not an unfinished thread",
            )
        ordered = sorted(
            entries,
            key=lambda entry: (_op_evidence_time(entry[1], evidence_times), entry[0]),
        )
        first_index, merged = _merged_thread_op(target_id, ordered, target)
        for index, _ in entries:
            replacements[index] = None
        replacements[first_index] = merged

    if not replacements:
        return ops
    merged_ops: list[dict[str, Any]] = []
    for index, op in enumerate(ops):
        if index in replacements:
            merged = replacements[index]
            if merged is not None:
                merged_ops.append(merged)
        else:
            merged_ops.append(op)
    return merged_ops


# ---------------------------------------------------------------------------
# Model call (independent provider, independent prompt)
# ---------------------------------------------------------------------------

def _call_rumination_model(model_input: str) -> str:
    base_url, api_key, model = _model_config()
    if not (base_url and api_key and model):
        raise RuminationPipelineError(
            "analysis_not_configured",
            "The rumination model provider is not fully configured",
            503,
        )
    url = f"{base_url.rstrip('/')}/chat/completions"
    request_body = {
        "model": model,
        "messages": [
            {"role": "system", "content": RUMINATION_SYSTEM_PROMPT},
            {"role": "user", "content": model_input},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": cfg.RUMINATION_MAX_TOKENS,
        "temperature": 0.1,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(url, headers=headers, json=request_body)
            if response.status_code == 400:
                excerpt = response.text[:500].casefold()
                if "response_format" in excerpt or "json_object" in excerpt:
                    request_body.pop("response_format", None)
                    response = client.post(url, headers=headers, json=request_body)
    except Exception as exc:
        raise RuminationPipelineError(
            "model_request_failed",
            f"Rumination model request failed: {type(exc).__name__}",
        ) from exc

    if response.status_code != 200:
        raise RuminationPipelineError(
            "model_http_error",
            f"Rumination model returned HTTP {response.status_code}",
        )
    try:
        payload = response.json()
        choice = payload["choices"][0]
        message = choice.get("message") if isinstance(choice, dict) else None
        output = message.get("content") if isinstance(message, dict) else None
    except Exception as exc:
        raise RuminationPipelineError(
            "model_response_error", "Rumination model response shape is invalid",
        ) from exc
    if not isinstance(output, str) or not output.strip():
        detail = _response_diagnostic(response.status_code, choice, message, payload)
        if isinstance(choice, dict) and choice.get("finish_reason") == "length":
            log.error("Rumination output budget exhausted before final content: %s", detail)
            raise RuminationPipelineError(
                "model_response_error",
                "Rumination output budget exhausted before final content; raise RUMINATION_MAX_TOKENS",
            )
        log.error("Rumination model returned no final content: %s", detail)
        raise RuminationPipelineError(
            "model_response_error",
            f"Rumination model response shape is invalid ({detail})",
        )
    return output


# ---------------------------------------------------------------------------
# Embedding enrichment
# ---------------------------------------------------------------------------

def enrich_rumination_ops(ops: list[dict[str, Any]], run_id: int) -> list[dict[str, Any]]:
    """正文向量失败或非空场景的召回向量失败都让整批失败。

    召回向量只服务于 recall_scene；正式写入绝不允许出现"场景已写入但向量
    永久缺失"的半完成状态。整批在同一事务内提交，任何失败都不会留下记忆行、
    continuity 对象、交接记录，也不会推进游标。没有可靠场景的操作显式写入
    空场景（recall_scene=None 且 recall_embedding=None）。
    """
    enriched: list[dict[str, Any]] = []
    for op in ops:
        _update_heartbeat(run_id)
        item = dict(op)
        content = str(item.get("content") or "")
        if content:
            item["embedding"] = _get_embedding_sync(content)
            recall_scene = str(item.get("recall_scene") or "").strip()
            if recall_scene:
                try:
                    item["recall_embedding"] = _get_embedding_sync(recall_scene)
                except DigestPipelineError as exc:
                    # 场景非空必须有向量：失败让整批失败并回滚，等待重试。
                    raise RuminationPipelineError(
                        "recall_embedding_failed",
                        f"Recall scene embedding failed ({exc.code}); "
                        "the batch was not committed",
                    ) from exc
                item["recall_embedding"] = item.get("recall_embedding")
        enriched.append(item)
    return enriched


# ---------------------------------------------------------------------------
# Batch execution: claim → model → atomic commit
# ---------------------------------------------------------------------------

def _mark_stale_rumination_runs() -> None:
    heartbeat_cutoff = (
        _now() - timedelta(minutes=STALE_RUN_MINUTES)
    ).isoformat()
    (
        _client().table("memory_digest_runs")
        .update({
            "status": "failed",
            "error_code": "stale_run_recovered",
            "error_message": "Run heartbeat expired; recovered by another instance",
            "completed_at": _now().isoformat(),
        })
        .eq("pipeline", "rumination")
        .in_("status", ["claimed", "running"])
        .or_(
            f"and(heartbeat_at.is.null,claimed_at.lt.{heartbeat_cutoff}),"
            f"heartbeat_at.lt.{heartbeat_cutoff}"
        )
        .execute()
    )


def _set_run_model_name(run_id: int) -> None:
    _, _, model = _model_config()
    try:
        _client().table("memory_digest_runs").update(
            {"model_name": model}
        ).eq("id", run_id).execute()
    except Exception:
        pass


def _mark_failed(run_id: int, code: str, message: str) -> None:
    try:
        (
            _client().table("memory_digest_runs")
            .update({
                "status": "failed",
                "error_code": code,
                "error_message": message[:2000],
                "completed_at": _now().isoformat(),
                "heartbeat_at": None,
            })
            .eq("id", run_id)
            .execute()
        )
    except Exception:
        log.exception("Failed to persist rumination run failure: run_id=%s code=%s", run_id, code)


def run_rumination_batch(
    assistant_id: str,
    trigger: str,
    batch: tuple[int, int, int],
    *,
    first_batch: bool,
    scheduled_execution_id: int | None = None,
) -> dict[str, Any]:
    """Claim, process and atomically commit a single rumination batch."""
    first_id, last_id, count = batch
    if trigger not in RUMINATION_TRIGGERS:
        raise ValueError("unsupported rumination trigger")

    claim = _rpc_object("claim_rumination_batch", {
        "p_assistant_id": assistant_id,
        "p_trigger": trigger,
        "p_first_message_id": first_id,
        "p_last_message_id": last_id,
        "p_message_count": count,
        "p_first_batch": bool(first_batch),
        "p_scheduled_execution_id": scheduled_execution_id,
    })
    if claim.get("status") == "already_running":
        raise RuminationPipelineError(
            "already_running", "Another rumination batch is already running", 409,
        )
    if claim.get("status") in {
        "already_initialized", "not_initialized", "batch_stale",
        "already_scheduled_today",
    }:
        return {"status": "skipped", "reason": claim.get("status"), "cursor": claim.get("cursor")}
    if claim.get("status") != "claimed":
        raise RuminationPipelineError(
            "commit_failed", "Failed to claim a rumination batch", 500,
        )
    run_id = int(claim["run_id"])
    scheduled_execution_id = claim.get("scheduled_execution_id")
    _set_run_model_name(run_id)

    stage = "fetch_batch_rows"
    try:
        rows = _fetch_batch_rows(assistant_id, first_id, last_id)
        stage = "normalize_batch_messages"
        messages = _normalize_batch_messages(rows)
        if not messages:
            raise RuminationPipelineError(
                "no_usable_messages", "The claimed rumination batch has no usable messages",
            )
        stage = "build_evidence_times"
        evidence_times = {message["id"]: message["source_time"] for message in messages}
        stage = "load_unfinished_threads"
        threads = _load_unfinished_threads(assistant_id)
        stage = "index_threads_by_id"
        threads_by_id = {int(row["id"]): row for row in threads}
        stage = "load_own_requests"
        own_requests = _load_own_requests(assistant_id)
        stage = "load_absorbable_candidates"
        absorbable = _load_absorbable_candidates(assistant_id, rows)
        stage = "index_absorbable_ids"
        absorbable_ids = {int(row["memory_id"]) for row in absorbable}
        _update_heartbeat(run_id)

        stage = "build_model_input"
        model_input = build_model_input(messages, threads, own_requests, absorbable)
        stage = "model_request"
        model_output = _call_rumination_model(model_input)
        _update_heartbeat(run_id)
        stage = "parse_output"
        ops = parse_rumination_output(
            model_output,
            evidence_times=evidence_times,
            threads_by_id=threads_by_id,
            absorbable_ids=absorbable_ids,
        )
        stage = "merge_thread_operations"
        # 同批内同一条 thread 的多个连续进展按真实证据时间合并为一个最终
        # 版本操作；互相矛盾或无法排序时整批失败。
        ops = merge_thread_operations(
            ops,
            evidence_times=evidence_times,
            threads_by_id=threads_by_id,
        )
        stage = "enrich_embeddings"
        ops = enrich_rumination_ops(ops, run_id)

        stage = "commit_batch"
        commit = _rpc_object("commit_rumination_batch", {
            "p_run_id": run_id,
            "p_ops": {"operations": ops},
        })
        return {
            "status": "succeeded",
            "run_id": run_id,
            "scheduled_execution_id": scheduled_execution_id,
            "batch": {"first_message_id": first_id, "last_message_id": last_id, "message_count": count},
            "op_counts": commit.get("op_counts") or {},
            "cursor_after": int((commit.get("cursor") or {}).get("last_processed_message_id") or last_id),
            "preview": commit.get("preview") or [],
        }
    except RuminationPipelineError as exc:
        _mark_failed(run_id, exc.code, str(exc))
        exc.scheduled_execution_id = scheduled_execution_id
        raise
    except Exception as exc:
        log.exception(
            "Rumination batch failed: run_id=%s assistant_id=%s "
            "trigger=%s first_message_id=%s last_message_id=%s "
            "message_count=%s stage=%s error=%s",
            run_id, assistant_id, trigger, first_id, last_id, count,
            stage, type(exc).__name__,
        )
        _mark_failed(run_id, "pipeline_error", f"{type(exc).__name__}: {str(exc)[:1200]}")
        wrapped = RuminationPipelineError(
            "pipeline_error", "Rumination pipeline failed", 500,
        )
        wrapped.scheduled_execution_id = scheduled_execution_id
        raise wrapped from exc


def _backlog_count(assistant_id: str, cursor: int) -> int:
    response = (
        _client().table("chat_messages")
        .select("id", count="exact")
        .eq("assistant_id", assistant_id)
        .gt("id", int(cursor))
        .limit(1)
        .execute()
    )
    return int(response.count or 0)


def run_rumination_digest(trigger: str = "rumination_manual") -> dict[str, Any]:
    """Process every qualifying batch; earlier committed batches survive a
    later batch's failure. 回到调用方前不吞掉失败（手动执行需要真实错误）。"""
    if trigger not in RUMINATION_TRIGGERS:
        raise ValueError("unsupported rumination trigger")
    if not _rumination_analysis_configured():
        raise RuminationPipelineError(
            "analysis_not_configured",
            "The rumination model provider is not fully configured",
            503,
        )

    assistant_id = resolve_rumination_assistant_id()
    _mark_stale_rumination_runs()
    cursor = get_rumination_cursor(assistant_id)
    initialized = bool(cursor.get("initialized"))
    cursor_id = int(cursor.get("last_processed_message_id") or 0)

    results: list[dict[str, Any]] = []
    tail_count = 0
    execution_id: int | None = None

    def _failed_result(exc: RuminationPipelineError, batch: tuple[int, int, int]):
        # Earlier committed batches survive; this batch keeps the cursor
        # untouched, so the next run retries it idempotently.
        return {
            "status": "failed",
            "error_code": exc.code,
            "error_message": str(exc),
            "batch": {
                "first_message_id": batch[0],
                "last_message_id": batch[1],
                "message_count": batch[2],
            },
        }

    try:
        if initialized:
            # 逐页交错处理积压：取一页（≤120 条真实消息行）→ claim → 模型 →
            # 原子提交 → 推进读取位置；不足 60 条的尾部留到次日。绝不一次性
            # 读取或持有全部积压，也不设总批次上限；批次大小按真实行数计，
            # 与消息 ID 是否连续无关。任一批失败即停止，其后消息不推进游标。
            # scheduled 触发时：第一个批次在数据库内领取当日 execution
            # identity，后续批次携带它绕过当日防重连批；整个执行（无论完成、
            # 尾部不足、失败）由 finally 统一收尾。
            position = cursor_id
            while True:
                page = _fetch_message_ids(
                    assistant_id, after=position, limit=RUMINATION_BATCH_MAX,
                )
                if len(page) < RUMINATION_BATCH_MIN:
                    tail_count = len(page)
                    break
                batch = (page[0], page[-1], len(page))
                try:
                    result = run_rumination_batch(
                        assistant_id, trigger, batch, first_batch=False,
                        scheduled_execution_id=execution_id,
                    )
                except RuminationPipelineError as exc:
                    results.append(_failed_result(exc, batch))
                    execution_id = (
                        getattr(exc, "scheduled_execution_id", None) or execution_id
                    )
                    break
                results.append(result)
                execution_id = result.get("scheduled_execution_id") or execution_id
                if result.get("status") != "succeeded":
                    break
                position = page[-1]
            if not results:
                skipped = _rpc_object("record_rumination_skipped", {
                    "p_assistant_id": assistant_id,
                    "p_trigger": trigger,
                    "p_backlog_count": tail_count,
                    "p_reason": (
                        f"backlog {tail_count} below the {RUMINATION_BATCH_MIN}-message batch threshold"
                    ),
                })
                return {
                    "status": "skipped",
                    "trigger": trigger,
                    "run_id": skipped.get("run_id"),
                    "backlog_count": tail_count,
                    "cursor_before": cursor_id,
                    "cursor_after": cursor_id,
                    "batch_count": 0,
                    "batches": [],
                }
        else:
            latest_ids = _fetch_latest_message_ids(assistant_id, FIRST_RUN_MAX_MESSAGES)
            first_run_ids = first_run_message_ids(latest_ids)
            batches = plan_rumination_batches(first_run_ids, initialized=False)
            if not batches:
                return {
                    "status": "skipped",
                    "trigger": trigger,
                    "run_id": None,
                    "backlog_count": 0,
                    "cursor_before": cursor_id,
                    "cursor_after": cursor_id,
                    "batch_count": 0,
                    "batches": [],
                }
            batch = batches[0]
            try:
                result = run_rumination_batch(
                    assistant_id, trigger, batch, first_batch=True,
                )
            except RuminationPipelineError as exc:
                results.append(_failed_result(exc, batch))
                execution_id = (
                    getattr(exc, "scheduled_execution_id", None) or execution_id
                )
            else:
                results.append(result)
                if results[-1].get("scheduled_execution_id"):
                    execution_id = results[-1]["scheduled_execution_id"]
    finally:
        # Unified lifecycle close: the day's scheduled execution always reaches
        # a terminal state regardless of success, model failure, embedding
        # failure, JSON parse failure, DB commit failure, or legitimate skip.
        # No-op for manual triggers (no execution was created) and for
        # already_scheduled_today / already_running claims (no execution was
        # created for this instance).
        if trigger == "rumination_scheduled" and execution_id is not None:
            try:
                finish_result = _rpc_object(
                    "finish_rumination_scheduled_execution",
                    {"p_execution_id": execution_id},
                )
                if finish_result.get("status") != "finished":
                    log.error(
                        "finish_rumination_scheduled_execution returned "
                        "unexpected status: execution_id=%s status=%s trigger=%s",
                        execution_id, finish_result.get("status"), trigger,
                    )
                elif finish_result.get("execution_id") != execution_id:
                    log.error(
                        "finish_rumination_scheduled_execution returned "
                        "mismatched execution_id: expected=%s got=%s trigger=%s",
                        execution_id, finish_result.get("execution_id"), trigger,
                    )
                elif not finish_result.get("changed"):
                    log.info(
                        "finish_rumination_scheduled_execution idempotent: "
                        "execution_id=%s was already finished trigger=%s",
                        execution_id, trigger,
                    )
                else:
                    log.info(
                        "Scheduled execution finished: execution_id=%s trigger=%s",
                        execution_id, trigger,
                    )
            except Exception:
                log.exception(
                    "Failed to finish scheduled execution: execution_id=%s trigger=%s",
                    execution_id, trigger,
                )

    failed = [item for item in results if item.get("status") == "failed"]
    skipped_only = results and all(
        item.get("status") == "skipped" for item in results
    )
    cursor_after = next(
        (
            item["cursor_after"]
            for item in reversed(results)
            if item.get("cursor_after") is not None
        ),
        cursor_id,
    )
    return {
        "status": (
            "failed" if failed else ("skipped" if skipped_only else "succeeded")
        ),
        "reason": "already_scheduled_today" if skipped_only else None,
        "trigger": trigger,
        "assistant_id": assistant_id,
        "batch_count": len(results),
        "batches": results,
        "op_counts": _merge_op_counts(results),
        "cursor_after": cursor_after,
    }


def _merge_op_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for result in results:
        for key, value in (result.get("op_counts") or {}).items():
            try:
                merged[key] = merged.get(key, 0) + int(value)
            except (TypeError, ValueError):
                continue
    return merged


def _scheduled_already_done(cursor: dict[str, Any], now_cst: datetime) -> bool:
    done_date = cursor.get("last_scheduled_date")
    if not done_date:
        return False
    if isinstance(done_date, datetime):
        return done_date.date() >= now_cst.date()
    try:
        return datetime.strptime(str(done_date)[:10], "%Y-%m-%d").date() >= now_cst.date()
    except ValueError:
        return False


def run_rumination_digest_if_due() -> dict[str, Any] | None:
    """每日 Asia/Shanghai 06:00（可配）后由调度器调用，一天最多正式运行一次。"""
    if not _rumination_analysis_configured():
        return None
    try:
        assistant_id = resolve_rumination_assistant_id()
    except Exception:
        log.warning("反刍调度跳过: 无法确定 assistant_id")
        return None
    try:
        cursor = get_rumination_cursor(assistant_id)
        now_cst = datetime.now(CST)
        if _scheduled_already_done(cursor, now_cst):
            return None
        if now_cst.hour < max(0, min(23, int(cfg.RUMINATION_DAILY_HOUR or 6))):
            return None
        result = run_rumination_digest("rumination_scheduled")
        if result and result.get("status") == "skipped":
            # 当天自动尝试已被数据库判定消耗（或同日已在运行）：
            # 正常静默返回，不算失败。
            return None
        return result
    except RuminationPipelineError as exc:
        if exc.code in {"already_running", "no_usable_messages"}:
            return None
        log.warning("Rumination scheduled check failed: code=%s", exc.code)
        return None
    except Exception as exc:
        log.exception("Rumination scheduled check failed: error=%s", type(exc).__name__)
        return None


# ---------------------------------------------------------------------------
# Admin observability
# ---------------------------------------------------------------------------

def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "assistant_id", "pipeline", "trigger", "mode", "status",
        "source_first_message_id", "source_last_message_id", "message_count",
        "extracted_count", "inserted_count", "model_name", "preview_memories",
        "op_counts", "error_code", "error_message", "started_at", "completed_at",
        "created_at",
    }
    return {key: run.get(key) for key in allowed}


def list_rumination_runs(limit: int = 20, assistant_id: str | None = None) -> list[dict[str, Any]]:
    query = (
        _client().table("memory_digest_runs")
        .select("*")
        .eq("pipeline", "rumination")
        .order("started_at", desc=True)
        .limit(max(1, min(100, int(limit))))
    )
    if assistant_id:
        query = query.eq("assistant_id", assistant_id)
    return [_public_run(row) for row in (query.execute().data or [])]


def get_rumination_status() -> dict[str, Any]:
    assistant_id = resolve_rumination_assistant_id()
    cursor = get_rumination_cursor(assistant_id)
    cursor_id = int(cursor.get("last_processed_message_id") or 0)
    initialized = bool(cursor.get("initialized"))
    backlog = _backlog_count(assistant_id, cursor_id)
    _, _, model = _model_config()
    threshold_met = bool(initialized) or backlog >= RUMINATION_BATCH_MIN
    return {
        "assistant_id": assistant_id,
        "configured": _rumination_analysis_configured(),
        "model": model,
        "initialized": initialized,
        "cursor": cursor_id,
        "last_success_at": cursor.get("last_success_at"),
        "last_scheduled_date": cursor.get("last_scheduled_date"),
        "daily_hour": max(0, min(23, int(cfg.RUMINATION_DAILY_HOUR or 6))),
        "latest_batch": {
            "first_message_id": cursor.get("last_batch_first_message_id"),
            "last_message_id": cursor.get("last_batch_last_message_id"),
            "message_count": cursor.get("last_batch_message_count"),
        },
        "backlog_count": backlog,
        "batch_min": RUMINATION_BATCH_MIN,
        "batch_max": RUMINATION_BATCH_MAX,
        "first_run_max": FIRST_RUN_MAX_MESSAGES,
        "threshold_met": threshold_met,
        "recent_runs": list_rumination_runs(20, assistant_id),
    }
