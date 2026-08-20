"""Reliable, observable long-term memory digest pipeline.

`chat_messages` is treated as an immutable, read-only source. Progress is tracked
separately per assistant and advances only after an atomic successful commit.
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
from .memory_continuity_schema import AUTOMATIC_TYPES, SCHEMA_VERSION, THREAD_STATES, ContinuityDataError, validate_continuity_data

log = logging.getLogger("gateway.memory_extract")

CST = timezone(timedelta(hours=8))
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024
DEFAULT_BATCH_SIZE = 60
MAX_BATCH_SIZE = 100
DEFAULT_MAX_CHARS = 12000
STALE_RUN_MINUTES = 30
FAILED_RETRY_MINUTES = 60
MAX_EXTRACTED_MEMORIES = 8

EXTRACT_SYSTEM_PROMPT = """从带 id、北京时间 t 和 role 的聊天原文中，提取最多 8 条有长期价值且有原文证据的独立记忆。

规则：
1. 只提取证据明确的近期片段、未完线索、完整共同经历和内部梗；禁止推测。
2. 排除寒暄、短暂情绪、待办、一次性指令、报错、拒绝、控制标签和重复回复。角色扮演内容只能中性抽象为明确的长期偏好、边界或约定。
3. continuity_type 只允许 moment、thread、episode、inside_joke；不得生成 profile、interaction_rule 或 relationship。
4. 独立对象用 append 且 memory_key=null；只有原文明示同一对象的当前状态变化时才用 replace，并给稳定主题键。
5. 不要把关系模式改名为 interaction_rule；自动总结绝对不能推断互动规则。
6. evidence_message_ids 必须列出直接支持该记忆的原文 id。importance 为 1-10，confidence 为 0-1。
7. memory_time 是事情实际发生或状态生效的北京时间 ISO 8601；无法从原文明示时间或相对时间可靠确定时填 null。time_precision 只允许 minute、day、approximate、unknown。
8. content 用一到两句话独立说明记忆。没有合格内容时返回空数组。

每条必须提供符合分类结构的 continuity_data；thread_state 允许 open、paused、resolved、dissolved、abandoned、unknown。
只返回严格 JSON：
{"memories":[{"content":"...","continuity_type":"moment","continuity_data":{"scene":"...","event":"...","moment_state":"standalone"},"thread_state":null,"update_mode":"append","memory_key":null,"importance":6,"confidence":0.9,"evidence_message_ids":[12,13],"memory_time":"2026-08-03","time_precision":"day"}]}"""
EXTRACT_SYSTEM_PROMPT += """
9. 如果原文明确显示该内容已通过记忆工具提交，或已通过待办工具创建，不要再提取。不能确定时仍可输出，由数据库保守去重和用户审核。
"""

# Sent as an extra user turn when the first model response parsed as JSON but
# had no memories array. Must be strict: an empty object is not an empty result.
EXTRACT_REPAIR_PROMPT = (
    "上一次输出缺少 memories 数组。只返回严格 JSON：\n"
    '{"memories":[...]}\n'
    "没有合格记忆时必须返回：\n"
    '{"memories":[]}\n'
    "不得返回空对象、说明文字或 Markdown。"
)

CONTINUITY_TYPES = AUTOMATIC_TYPES
UPDATE_MODES = frozenset({"append", "replace"})
MEMORY_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9._:/-]{2,119}")
TIME_PRECISIONS = frozenset({"minute", "day", "approximate", "unknown"})
CONTINUITY_TYPE_TAGS = {"moment": "近期片段", "thread": "未完线索", "episode": "共同经历", "inside_joke": "内部梗"}
EMBEDDED_TIMESTAMP_PATTERN = re.compile(
    r"(?m)^\s*(?P<year>\d{2}|\d{4})[.\-/](?P<month>\d{1,2})[.\-/](?P<day>\d{1,2})"
    r"\s+(?P<hour>\d{1,2}):(?P<minute>\d{2})(?::(?P<second>\d{2}))?\s*$"
)

_REFUSAL_MARKERS = (
    "i cannot fulfill this request",
    "i can't fulfill this request",
    "i cannot engage in",
    "我无法满足这个请求",
    "我不能满足这个请求",
)


class DigestPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, model_output: str = ""):
        super().__init__(message)
        self.code = code
        self.model_output = model_output


def _analysis_configured() -> bool:
    """Return whether the extraction and embedding provider is usable."""
    return bool(
        cfg.ANALYSIS_BASE_URL.strip()
        and cfg.ANALYSIS_API_KEY.strip()
        and cfg.ANALYSIS_MODEL.strip()
    )


def _client():
    client = get_client()
    if not client:
        raise DigestPipelineError("database_unavailable", "Supabase server client is unavailable")
    return client


def _claim_slot(assistant_id: str, trigger: str, mode: str) -> dict[str, Any]:
    """Atomically claim a cross-instance processing slot for this assistant.

    Uses the database advisory lock + heartbeat lease so only one gateway
    instance processes a given assistant at a time.
    """
    try:
        resp = _client().rpc(
            "claim_digest_slot",
            {
                "p_assistant_id": assistant_id,
                "p_trigger": trigger,
                "p_mode": mode,
            },
        ).execute()
        return resp.data or {}
    except Exception as exc:
        log.warning("Failed to claim digest slot: %s", exc)
        return {"status": "error"}


def _update_heartbeat(run_id: int) -> None:
    """Best-effort heartbeat while doing long model calls."""
    try:
        _client().rpc(
            "update_digest_heartbeat",
            {"p_run_id": run_id},
        ).execute()
    except Exception:
        pass


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(lo, min(hi, number))


def _parse_time(value: Any, assume_tz=timezone.utc) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume_tz)
    return parsed


def _parse_embedded_timestamp(content: Any) -> datetime | None:
    """Parse OrangeChat's display timestamp without asking the model to do it."""
    match = EMBEDDED_TIMESTAMP_PATTERN.search(str(content or ""))
    if not match:
        return None
    try:
        year = int(match.group("year"))
        if year < 100:
            year += 2000
        return datetime(
            year,
            int(match.group("month")),
            int(match.group("day")),
            int(match.group("hour")),
            int(match.group("minute")),
            int(match.group("second") or 0),
            tzinfo=CST,
        )
    except ValueError:
        return None


def _resolve_message_time(created_at: Any, content: Any) -> str | None:
    """Return one canonical Beijing source time, preferring a sane display time."""
    embedded = _parse_embedded_timestamp(content)
    # OrangeChat's chat_messages.created_at is a timestamp without time zone
    # whose stored wall clock is Asia/Shanghai. Explicitly zoned values retain
    # their own offset because _parse_time only applies CST to naive values.
    database_time = _parse_time(created_at, CST)
    if database_time:
        database_time = database_time.astimezone(CST)

    chosen = database_time
    if embedded and database_time:
        if abs((embedded - database_time).total_seconds()) <= 6 * 60 * 60:
            chosen = embedded
        else:
            log.warning(
                "消息内时间戳与数据库时间冲突，使用数据库时间（embedded=%s database=%s）",
                embedded.isoformat(),
                database_time.isoformat(),
            )
    elif embedded:
        chosen = embedded

    return chosen.isoformat(timespec="minutes") if chosen else None


def resolve_assistant_id() -> str:
    configured = cfg.MEMORY_ASSISTANT_ID.strip()
    if configured:
        return configured

    response = (
        _client().table("chat_messages")
        .select("assistant_id")
        .order("id", desc=True)
        .limit(50)
        .execute()
    )
    for row in response.data or []:
        assistant_id = str(row.get("assistant_id") or "").strip()
        if assistant_id and assistant_id.lower() != "test":
            return assistant_id
    raise DigestPipelineError("assistant_not_found", "No usable assistant_id exists in chat_messages")


def _get_cursor(assistant_id: str) -> int:
    response = (
        _client().table("memory_digest_cursors")
        .select("last_processed_message_id")
        .eq("assistant_id", assistant_id)
        .limit(1)
        .execute()
    )
    if not response.data:
        return 0
    return int(response.data[0].get("last_processed_message_id") or 0)


def _latest_message(assistant_id: str) -> dict[str, Any] | None:
    response = (
        _client().table("chat_messages")
        .select("id,created_at")
        .eq("assistant_id", assistant_id)
        .order("id", desc=True)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _clean_message_content(role: str, content: Any) -> str:
    text = str(content or "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<<(?:delay|schedule|busy):.*?>>", "", text, flags=re.IGNORECASE)
    text = EMBEDDED_TIMESTAMP_PATTERN.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    lowered = text.lower()
    if role == "assistant" and any(marker in lowered for marker in _REFUSAL_MARKERS):
        return ""

    per_message_limit = 800 if role == "user" else 400
    return text[:per_message_limit].strip()


def _fetch_batch(assistant_id: str, max_messages: int) -> tuple[int, list[dict[str, Any]]]:
    cursor = _get_cursor(assistant_id)
    fetch_limit = max(1, min(MAX_BATCH_SIZE, int(max_messages)))
    response = (
        _client().table("chat_messages")
        .select("id,assistant_id,conversation_id,role,content,created_at")
        .eq("assistant_id", assistant_id)
        .gt("id", cursor)
        .order("id")
        .limit(fetch_limit)
        .execute()
    )

    raw = response.data or []
    if not raw:
        return cursor, []

    # Pre-clean and compute per-message size budget.
    prepared: list[dict[str, Any]] = []
    for row in raw:
        cleaned = _clean_message_content(str(row.get("role") or ""), row.get("content"))
        prepared.append({
            **row,
            "_cleaned_content": cleaned,
            "_source_time": _resolve_message_time(
                row.get("created_at"),
                row.get("content"),
            ),
            "_chars": len(cleaned) + 80,
        })

    max_chars = max(2000, int(cfg.MEMORY_DIGEST_MAX_CHARS or DEFAULT_MAX_CHARS))

    # Build complete turns. A turn is either:
    #   - a user message + all following assistant messages in the same
    #     conversation_id, or
    #   - a standalone orphan assistant block in one conversation_id.
    # We never break in the middle of a turn so the cursor never skips
    # partially-processed messages.
    turn_ends: list[int] = []
    i = 0
    n = len(prepared)
    while i < n:
        role = str(prepared[i].get("role") or "").strip().lower()
        conv_id = str(prepared[i].get("conversation_id") or "")
        j = i + 1
        if role == "user":
            # Consume following assistants in the SAME conversation.
            while j < n:
                next_role = str(prepared[j].get("role") or "").strip().lower()
                next_conv = str(prepared[j].get("conversation_id") or "")
                if next_role == "assistant" and next_conv == conv_id:
                    j += 1
                else:
                    break
        elif role == "assistant":
            # Consume consecutive assistants in the SAME conversation.
            while j < n:
                next_role = str(prepared[j].get("role") or "").strip().lower()
                next_conv = str(prepared[j].get("conversation_id") or "")
                if next_role == "assistant" and next_conv == conv_id:
                    j += 1
                else:
                    break
        # Unknown roles are silently skipped (they do not form a turn).
        turn_ends.append(j)
        i = j

    # Accumulate complete turns until the character budget is exhausted.
    selected: list[dict[str, Any]] = []
    used_chars = 0
    prev = 0
    for end in turn_ends:
        turn = prepared[prev:end]
        turn_chars = sum(item["_chars"] for item in turn)
        if selected and used_chars + turn_chars > max_chars:
            break
        selected.extend(turn)
        used_chars += turn_chars
        prev = end

    return cursor, selected


def _conversation_context(messages: list[dict[str, Any]]) -> tuple[str, dict[int, str | None]]:
    normalized: list[dict[str, Any]] = []
    for row in messages:
        role = str(row.get("role") or "").strip().lower()
        if role not in {"user", "assistant"}:
            continue
        content = str(row.get("_cleaned_content") or "").strip()
        if not content:
            continue

        item = {
            "id": int(row["id"]),
            "role": role,
            "content": content,
            "conversation_id": str(row.get("conversation_id") or ""),
            "source_time": row.get("_source_time") or _resolve_message_time(
                row.get("created_at"),
                row.get("content"),
            ),
        }
        # Multiple consecutive assistant rows are usually retries/alternatives.
        # Keep only the final one, but ONLY within the same conversation.
        if (
            role == "assistant"
            and normalized
            and normalized[-1]["role"] == "assistant"
            and normalized[-1]["conversation_id"] == item["conversation_id"]
        ):
            normalized[-1] = item
        else:
            normalized.append(item)

    lines: list[str] = []
    source_times: dict[int, str | None] = {}
    for item in normalized:
        source_time = item["source_time"] or "unknown"
        source_times[item["id"]] = item["source_time"]
        lines.append(f"[id={item['id']} t={source_time} role={item['role']}] {item['content']}")
    return "\n".join(lines), source_times


def _format_conversation(messages: list[dict[str, Any]]) -> str:
    """Backward-compatible formatter used by tests and older callers."""
    return _conversation_context(messages)[0]


def _normalize_memory_time(value: Any, precision: str) -> tuple[str | None, str]:
    raw = str(value or "").strip()
    clean_precision = precision if precision in TIME_PRECISIONS else "unknown"
    if not raw:
        return None, "unknown"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            return None, "unknown"
        return raw, "day" if clean_precision == "unknown" else clean_precision
    parsed = _parse_time(raw, CST)
    if not parsed:
        return None, "unknown"
    normalized = parsed.astimezone(CST).isoformat(timespec="minutes")
    return normalized, "minute" if clean_precision == "unknown" else clean_precision


def _parse_model_output(
    text: str,
    source_times: dict[int, str | None] | None = None,
) -> list[dict[str, Any]]:
    cleaned = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE).strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise DigestPipelineError("model_parse_error", f"Model returned invalid JSON: {exc}", cleaned[:1500]) from exc

    items = payload.get("memories") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        raise DigestPipelineError("model_schema_error", "Model JSON does not contain a memories array", cleaned[:1500])

    validated: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for raw in items[:MAX_EXTRACTED_MEMORIES]:
        if not isinstance(raw, dict):
            continue
        content = re.sub(r"\s+", " ", str(raw.get("content") or "")).strip()[:600]
        if len(content) < 5:
            continue
        title = content[:40]
        continuity_type = str(raw.get("continuity_type") or "").strip().casefold()
        if continuity_type not in CONTINUITY_TYPES:
            continue
        thread_state = str(raw.get("thread_state") or "").strip().casefold() or None
        if continuity_type == "thread" and thread_state not in THREAD_STATES:
            thread_state = "unknown"
        elif continuity_type != "thread":
            thread_state = None
        try:
            continuity_data = validate_continuity_data(continuity_type, thread_state, raw.get("continuity_data"), automatic=True)
        except ContinuityDataError:
            continue

        update_mode = str(raw.get("update_mode") or "append").strip().casefold()
        if update_mode not in UPDATE_MODES:
            update_mode = "append"

        raw_memory_key = raw.get("memory_key")
        memory_key = str(raw_memory_key or "").strip().casefold() or None
        if update_mode == "replace":
            # A malformed or missing key must never turn a model suggestion into
            # an unsafe replacement. Keep the candidate, but downgrade it to an
            # independent append-only memory for human review.
            if not memory_key or not MEMORY_KEY_PATTERN.fullmatch(memory_key):
                update_mode = "append"
                memory_key = None
        else:
            memory_key = None

        evidence_ids: list[int] = []
        for candidate in raw.get("evidence_message_ids") or []:
            if isinstance(candidate, bool):
                continue
            try:
                message_id = int(candidate)
            except (TypeError, ValueError):
                continue
            if source_times is not None and message_id not in source_times:
                continue
            if message_id not in evidence_ids:
                evidence_ids.append(message_id)
            if len(evidence_ids) >= 8:
                break
        if source_times is not None and not evidence_ids:
            continue

        source_time = None
        if source_times is not None and evidence_ids:
            evidence_times = [
                source_times[message_id]
                for message_id in evidence_ids
                if source_times[message_id]
            ]
            source_time = max(evidence_times) if evidence_times else None
        raw_precision = str(raw.get("time_precision") or "unknown").strip().casefold()
        memory_time, time_precision = _normalize_memory_time(
            raw.get("memory_time"),
            raw_precision,
        )

        content_hash = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        validated.append({
            "content": content,
            "title": title,
            "continuity_type": continuity_type,
            "thread_state": thread_state,
            "continuity_schema_version": SCHEMA_VERSION,
            "continuity_data": continuity_data,
            "subject": "shared",
            "source_type": "natural_chat",
            "continuity_value": int(round(_clamp(raw.get("continuity_value"), 1, 10, raw.get("importance") or 5))),
            "retention_class": "normal",
            "participants": ["yezi", "qi"],
            "update_mode": update_mode,
            "memory_key": memory_key,
            "importance": int(round(_clamp(raw.get("importance"), 1, 10, 5))),
            # Kept internally for compatibility with the current pending-memory
            # commit RPC; the model no longer spends output tokens on this field.
            "emotion_weight": 0.5,
            "confidence": round(_clamp(raw.get("confidence"), 0, 1, 0.6), 3),
            "tags": [CONTINUITY_TYPE_TAGS[continuity_type]],
            "evidence_message_ids": evidence_ids,
            "source_time": source_time,
            "memory_time": memory_time,
            "time_precision": time_precision,
            "content_hash": content_hash,
        })
    return validated


def _extract_memories(
    conversation: str,
    source_times: dict[int, str | None] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    if not conversation.strip():
        return [], '{"memories":[]}'
    if not _analysis_configured():
        raise DigestPipelineError(
            "analysis_not_configured",
            "The analysis model provider is not fully configured",
        )

    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/chat/completions"
    base_messages = [
        {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
        {"role": "user", "content": f"<chat_log>\n{conversation}\n</chat_log>"},
    ]

    def _post(
        messages: list[dict[str, Any]],
        with_response_format: bool,
    ) -> httpx.Response:
        body = {
            "model": cfg.ANALYSIS_MODEL,
            "messages": messages,
            "max_tokens": 1200,
            "temperature": 0.1,
        }
        if with_response_format:
            body["response_format"] = {"type": "json_object"}
        try:
            with httpx.Client(timeout=60.0) as client:
                return client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        except Exception as exc:
            raise DigestPipelineError(
                "model_request_failed",
                f"Memory extraction request failed: {type(exc).__name__}",
            ) from exc

    def _content(response: httpx.Response) -> str:
        if response.status_code != 200:
            raise DigestPipelineError(
                "model_http_error",
                f"Memory extraction model returned HTTP {response.status_code}",
                response.text[:1500],
            )
        try:
            payload = response.json()
            output = payload["choices"][0]["message"]["content"]
            if not isinstance(output, str) or not output.strip():
                raise ValueError("model content is empty")
        except Exception as exc:
            raise DigestPipelineError(
                "model_response_error",
                "Memory extraction response is not valid OpenAI-compatible JSON",
                response.text[:1500],
            ) from exc
        return output

    response = _post(base_messages, with_response_format=True)

    # Fallback: some providers (e.g. certain SiliconFlow endpoints) return 400
    # because they do not support response_format: {"type": "json_object"}.
    # This fallback consumes the single retry attempt.
    retry_used = False
    if response.status_code == 400:
        body_excerpt = response.text[:500].lower()
        if "response_format" in body_excerpt or "json_object" in body_excerpt:
            log.warning("Provider rejected response_format; retrying without it")
            retry_used = True
            response = _post(base_messages, with_response_format=False)

    output = _content(response)

    try:
        return _parse_model_output(output, source_times), output
    except DigestPipelineError as exc:
        # Only a JSON body that parses but lacks the memories array is worth a
        # repair retry. Invalid JSON stays a hard error, and the repair retry
        # must never run when the 400 fallback already used the retry budget.
        if exc.code != "model_schema_error" or retry_used:
            raise
        log.warning("Model output parsed as JSON but has no memories array; running one repair retry")

    # Repair retry: same conversation, explicit format instruction, and
    # response_format removed so JSON-mode providers do not degrade to {}.
    repair_response = _post(
        base_messages + [{"role": "user", "content": EXTRACT_REPAIR_PROMPT}],
        with_response_format=False,
    )
    try:
        repair_output = _content(repair_response)
        return _parse_model_output(repair_output, source_times), repair_output
    except DigestPipelineError as exc:
        if exc.code == "model_http_error":
            raise
        # Any content failure on the repair attempt is a schema failure:
        # {} is never silently converted into a legal empty result.
        raise DigestPipelineError(
            "model_schema_error",
            f"Model did not return a memories array on the repair attempt ({exc.code})",
            repair_response.text[:1500],
        ) from exc


def _get_embedding_sync(text: str) -> list[float]:
    if not _analysis_configured():
        raise DigestPipelineError(
            "analysis_not_configured",
            "The analysis model provider is not fully configured",
        )
    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/embeddings"
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}"},
                json={
                    "model": EMBEDDING_MODEL,
                    "input": text[:2000],
                    "dimensions": EMBEDDING_DIM,
                },
            )
    except Exception as exc:
        raise DigestPipelineError(
            "embedding_request_failed",
            f"Embedding request failed: {type(exc).__name__}",
        ) from exc

    if response.status_code != 200:
        raise DigestPipelineError(
            "embedding_http_error",
            f"Embedding model returned HTTP {response.status_code}",
            response.text[:1500],
        )

    try:
        embedding = response.json()["data"][0]["embedding"]
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("embedding is empty")
        return [float(value) for value in embedding]
    except Exception as exc:
        raise DigestPipelineError(
            "embedding_response_error",
            "Embedding response shape is invalid",
            response.text[:1500],
        ) from exc


def _mark_stale_runs() -> None:
    heartbeat_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=STALE_RUN_MINUTES)
    ).isoformat()
    (
        _client().table("memory_digest_runs")
        .update({
            "status": "failed",
            "error_code": "stale_run_recovered",
            "error_message": "Run heartbeat expired; recovered by another instance",
            "completed_at": _iso_now(),
        })
        .in_("status", ["claimed", "running"])
        .or_(
            f"and(heartbeat_at.is.null,claimed_at.lt.{heartbeat_cutoff}),"
            f"heartbeat_at.lt.{heartbeat_cutoff}"
        )
        .execute()
    )


def _create_run(
    assistant_id: str,
    trigger: str,
    mode: str,
    messages: list[dict[str, Any]],
    status: str = "running",
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    payload = {
        "assistant_id": assistant_id,
        "pipeline": "legacy",
        "trigger": trigger,
        "mode": mode,
        "status": status,
        "source_first_message_id": int(messages[0]["id"]) if messages else None,
        "source_last_message_id": int(messages[-1]["id"]) if messages else None,
        "message_count": len(messages),
        "model_name": cfg.ANALYSIS_MODEL,
        "error_code": error_code,
        "error_message": error_message,
        "completed_at": _iso_now() if status != "running" else None,
    }
    response = _client().table("memory_digest_runs").insert(payload).execute()
    if not response.data:
        raise DigestPipelineError("run_create_failed", "Failed to create memory digest run")
    return response.data[0]


def _public_memories(memories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in item.items() if key not in {"embedding", "content_hash"}} for item in memories]


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "assistant_id", "pipeline", "trigger", "mode", "status", "source_first_message_id",
        "source_last_message_id", "message_count", "extracted_count", "inserted_count",
        "model_name", "preview_memories", "error_code", "error_message",
        "model_output_excerpt", "started_at", "completed_at", "created_at",
    }
    return {key: run.get(key) for key in allowed}


def run_memory_digest(trigger: str, mode: str, max_messages: int | None = None) -> dict[str, Any]:
    if trigger not in {"manual_preview", "manual_execute", "scheduled_daily", "idle_six_hours"}:
        raise ValueError("unsupported digest trigger")
    if mode not in {"preview", "execute"}:
        raise ValueError("unsupported digest mode")
    if not _analysis_configured():
        # This check deliberately happens before any database access. Missing
        # provider configuration is an operational state, not a failed run.
        raise DigestPipelineError(
            "analysis_not_configured",
            "The analysis model provider is not fully configured",
        )

    batch_size = max_messages or cfg.MEMORY_DIGEST_MAX_MESSAGES or DEFAULT_BATCH_SIZE
    batch_size = max(1, min(MAX_BATCH_SIZE, int(batch_size)))

    _mark_stale_runs()
    assistant_id = resolve_assistant_id()
    cursor_before, messages = _fetch_batch(assistant_id, batch_size)

    if not messages:
        run = _create_run(
            assistant_id,
            trigger,
            mode,
            [],
            status="skipped",
            error_code="no_new_messages",
            error_message="No unprocessed chat messages are available",
        )
        result = _public_run(run)
        result.update({"cursor_before": cursor_before, "cursor_after": cursor_before})
        return result

    # Cross-instance claim: advisory lock + heartbeat lease.
    claim = _claim_slot(assistant_id, trigger, mode)
    if claim.get("status") != "claimed":
        run = _create_run(
            assistant_id,
            trigger,
            mode,
            [],
            status="skipped",
            error_code="concurrent_run",
            error_message="Another instance is actively processing this assistant",
        )
        result = _public_run(run)
        result.update({"cursor_before": cursor_before, "cursor_after": cursor_before})
        return result

    run_id = int(claim["run_id"])

    # Transition the claimed slot to running and populate source metadata.
    running_update = {
        "status": "running",
        "source_first_message_id": int(messages[0]["id"]),
        "source_last_message_id": int(messages[-1]["id"]),
        "message_count": len(messages),
        "model_name": cfg.ANALYSIS_MODEL,
    }
    _client().table("memory_digest_runs").update(running_update).eq("id", run_id).execute()
    _update_heartbeat(run_id)

    # Re-fetch so the in-memory run dict reflects the database state.
    run_resp = (
        _client().table("memory_digest_runs")
        .select("*")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )
    run = run_resp.data[0] if run_resp.data else {**claim, **running_update}

    conversation, source_times = _conversation_context(messages)

    try:
        memories, raw_output = _extract_memories(conversation, source_times)
        _update_heartbeat(run_id)
        public_preview = _public_memories(memories)

        if mode == "preview":
            update_payload = {
                "status": "succeeded",
                "extracted_count": len(memories),
                "inserted_count": 0,
                "preview_memories": public_preview,
                "model_output_excerpt": (raw_output or "")[:1500],
                "completed_at": _iso_now(),
            }
            response = (
                _client().table("memory_digest_runs")
                .update(update_payload)
                .eq("id", run_id)
                .execute()
            )
            saved = response.data[0] if response.data else {**run, **update_payload}
            result = _public_run(saved)
            result.update({"cursor_before": cursor_before, "cursor_after": cursor_before})
            return result

        enriched: list[dict[str, Any]] = []
        for memory in memories:
            _update_heartbeat(run_id)
            item = dict(memory)
            embedding = _get_embedding_sync(item["content"])
            item["embedding"] = embedding
            enriched.append(item)

        commit_response = _client().rpc(
            "commit_memory_digest_run",
            {"p_run_id": run_id, "p_memories": enriched},
        ).execute()
        inserted_count = int(commit_response.data or 0)
        saved_response = (
            _client().table("memory_digest_runs")
            .select("*")
            .eq("id", run_id)
            .limit(1)
            .execute()
        )
        saved = saved_response.data[0] if saved_response.data else run
        result = _public_run(saved)
        result.update({
            "inserted_count": inserted_count,
            "cursor_before": cursor_before,
            "cursor_after": int(messages[-1]["id"]),
        })
        return result

    except DigestPipelineError as exc:
        error_code = exc.code
        error_message = str(exc)
        model_excerpt = exc.model_output[:1500]
    except Exception as exc:
        log.exception("Memory digest run failed: run_id=%s", run_id)
        error_code = "pipeline_error"
        error_message = f"{type(exc).__name__}: {str(exc)[:1200]}"
        model_excerpt = ""

    failed_payload = {
        "status": "failed",
        "error_code": error_code,
        "error_message": error_message[:2000],
        "model_output_excerpt": model_excerpt,
        "completed_at": _iso_now(),
    }
    response = (
        _client().table("memory_digest_runs")
        .update(failed_payload)
        .eq("id", run_id)
        .execute()
    )
    saved = response.data[0] if response.data else {**run, **failed_payload}
    result = _public_run(saved)
    result.update({"cursor_before": cursor_before, "cursor_after": cursor_before})
    return result


def list_digest_runs(limit: int = 30) -> list[dict[str, Any]]:
    safe_limit = max(1, min(100, int(limit)))
    response = (
        _client().table("memory_digest_runs")
        .select("*")
        .eq("pipeline", "legacy")
        .order("started_at", desc=True)
        .limit(safe_limit)
        .execute()
    )
    return [_public_run(run) for run in response.data or []]


def get_digest_status() -> dict[str, Any]:
    assistant_id = resolve_assistant_id()
    cursor = _get_cursor(assistant_id)
    latest = _latest_message(assistant_id)
    latest_id = int(latest.get("id") or 0) if latest else 0

    count_response = (
        _client().table("chat_messages")
        .select("id", count="exact")
        .eq("assistant_id", assistant_id)
        .gt("id", cursor)
        .limit(1)
        .execute()
    )
    cursor_response = (
        _client().table("memory_digest_cursors")
        .select("*")
        .eq("assistant_id", assistant_id)
        .limit(1)
        .execute()
    )
    return {
        "assistant_id": assistant_id,
        "cursor": cursor_response.data[0] if cursor_response.data else {
            "assistant_id": assistant_id,
            "last_processed_message_id": 0,
            "last_success_at": None,
        },
        "latest_message_id": latest_id,
        "latest_message_at": latest.get("created_at") if latest else None,
        "backlog_count": int(count_response.count or 0),
        "analysis_model": cfg.ANALYSIS_MODEL,
        "analysis_configured": _analysis_configured(),
        "recent_runs": list_digest_runs(20),
    }


def _recent_run_blocks_retry(assistant_id: str) -> bool:
    response = (
        _client().table("memory_digest_runs")
        .select("status,started_at")
        .eq("assistant_id", assistant_id)
        .eq("mode", "execute")
        .order("started_at", desc=True)
        .limit(1)
        .execute()
    )
    if not response.data:
        return False
    latest = response.data[0]
    if latest.get("status") == "running":
        return True
    if latest.get("status") != "failed":
        return False
    started = _parse_time(latest.get("started_at"), timezone.utc)
    return bool(started and datetime.now(timezone.utc) - started < timedelta(minutes=FAILED_RETRY_MINUTES))


def _daily_run_exists(assistant_id: str, now_cst: datetime) -> bool:
    local_midnight = now_cst.replace(hour=0, minute=0, second=0, microsecond=0)
    response = (
        _client().table("memory_digest_runs")
        .select("id")
        .eq("assistant_id", assistant_id)
        .eq("trigger", "scheduled_daily")
        .in_("status", ["running", "succeeded", "skipped"])
        .gte("started_at", local_midnight.astimezone(timezone.utc).isoformat())
        .limit(1)
        .execute()
    )
    return bool(response.data)


def run_scheduled_digest_if_due() -> dict[str, Any] | None:
    # Do not query the database or create audit rows every scheduler tick when
    # the provider is intentionally not configured.
    if not _analysis_configured():
        return None
    assistant_id: str | None = None
    try:
        assistant_id = resolve_assistant_id()
    except Exception:
        log.exception("Scheduled digest failed: cannot resolve assistant_id")
        return None

    try:
        _mark_stale_runs()
        status = get_digest_status()
        if status["backlog_count"] <= 0:
            return None

        if _recent_run_blocks_retry(assistant_id):
            return None

        now_cst = datetime.now(CST)
        daily_hour = max(0, min(23, int(cfg.MEMORY_DIGEST_DAILY_HOUR or 3)))
        if now_cst.hour >= daily_hour and not _daily_run_exists(assistant_id, now_cst):
            return run_memory_digest("scheduled_daily", "execute")

        latest_at = _parse_time(status.get("latest_message_at"), CST)
        if latest_at and now_cst - latest_at.astimezone(CST) >= timedelta(hours=cfg.MEMORY_DIGEST_IDLE_HOURS):
            return run_memory_digest("idle_six_hours", "execute")
    except Exception as exc:
        log.exception("Scheduled memory digest check failed")
        try:
            failed_run = _create_run(
                assistant_id,
                "scheduled_daily",
                "execute",
                [],
                status="failed",
                error_code="scheduled_check_error",
                error_message=f"{type(exc).__name__}: {str(exc)[:1200]}",
            )
            return _public_run(failed_run)
        except Exception:
            log.exception("Failed to persist scheduled digest error")
    return None


# Backward-compatible entry point used by older callers.
def run_daily_digest() -> dict[str, Any]:
    return run_memory_digest("scheduled_daily", "execute")
