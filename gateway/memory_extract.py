"""Reliable, observable long-term memory digest pipeline.

`chat_messages` is treated as an immutable, read-only source. Progress is tracked
separately per assistant and advances only after an atomic successful commit.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import cfg
from .db import get_client

log = logging.getLogger("gateway.memory_extract")

CST = timezone(timedelta(hours=8))
EMBEDDING_MODEL = "Pro/Qwen/Qwen3-Embedding-0.6B"
DEFAULT_BATCH_SIZE = 60
MAX_BATCH_SIZE = 100
DEFAULT_MAX_CHARS = 12000
STALE_RUN_MINUTES = 30
FAILED_RETRY_MINUTES = 60

_pipeline_lock = threading.Lock()

EXTRACT_SYSTEM_PROMPT = """你是长期记忆提取器。请从提供的聊天原文中提取值得长期记住、且被原文明确信息支持的事实。

严格规则：
1. 禁止推测、脑补或把模型生成内容当成用户现实事实。
2. 优先提取用户明确表达的偏好、边界、长期习惯、重要事件、承诺和关系变化。
3. 角色扮演或成人内容只可抽象成明确的长期偏好、边界或约定；必须使用中性、不露骨的语言，不能复述过程。
4. 模型拒绝、系统报错、时间戳、控制标签和同一轮的重复回复都不是记忆。
5. 日常寒暄、短暂情绪、一次性指令和没有长期价值的信息不要提取。
6. 没有合格记忆时返回空数组。
7. 每条记忆必须能独立理解，content 一到两句话，title 是一句短摘要。
8. importance 为 1-10；emotion_weight 和 confidence 为 0-1；tags 为 1-5 个简短关键词。

只返回严格 JSON，不要 Markdown，不要解释：
{"memories":[{"content":"...","title":"...","importance":5,"emotion_weight":0.5,"confidence":0.8,"tags":["...","..."]}]}"""

_REFUSAL_MARKERS = (
    "i cannot fulfill this request",
    "i can’t fulfill this request",
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


def _client():
    client = get_client()
    if not client:
        raise DigestPipelineError("database_unavailable", "Supabase server client is unavailable")
    return client


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
    text = re.sub(r"(?m)^\s*\d{2}\.\d{2}\.\d{2}\s+\d{2}:\d{2}\s*$", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    lowered = text.lower()
    if role == "assistant" and any(marker in lowered for marker in _REFUSAL_MARKERS):
        return ""

    per_message_limit = 1000 if role == "user" else 700
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

    selected: list[dict[str, Any]] = []
    used_chars = 0
    max_chars = max(2000, int(cfg.MEMORY_DIGEST_MAX_CHARS or DEFAULT_MAX_CHARS))
    for row in response.data or []:
        cleaned = _clean_message_content(str(row.get("role") or ""), row.get("content"))
        estimated = len(cleaned) + 80
        if selected and used_chars + estimated > max_chars:
            break
        copied = dict(row)
        copied["_cleaned_content"] = cleaned
        selected.append(copied)
        used_chars += estimated
    return cursor, selected


def _format_conversation(messages: list[dict[str, Any]]) -> str:
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
            "created_at": row.get("created_at") or "",
        }
        # Multiple consecutive assistant rows are usually retries/alternatives.
        # Keep only the final one without modifying the source table.
        if role == "assistant" and normalized and normalized[-1]["role"] == "assistant":
            normalized[-1] = item
        else:
            normalized.append(item)

    lines = []
    for item in normalized:
        speaker = "叶子" if item["role"] == "user" else "栖"
        lines.append(
            f"[message_id={item['id']} time={item['created_at']}] {speaker}: {item['content']}"
        )
    return "\n".join(lines)


def _parse_model_output(text: str) -> list[dict[str, Any]]:
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
    for raw in items:
        if not isinstance(raw, dict):
            continue
        content = re.sub(r"\s+", " ", str(raw.get("content") or "")).strip()[:600]
        if len(content) < 5:
            continue
        title = re.sub(r"\s+", " ", str(raw.get("title") or content[:40])).strip()[:100]

        raw_tags = raw.get("tags")
        if isinstance(raw_tags, str):
            raw_tags = re.split(r"[,，]", raw_tags)
        tags: list[str] = []
        for tag in raw_tags if isinstance(raw_tags, list) else []:
            clean_tag = re.sub(r"\s+", " ", str(tag)).strip()[:24]
            if clean_tag and clean_tag not in tags:
                tags.append(clean_tag)
            if len(tags) >= 5:
                break
        if not tags:
            tags = ["长期记忆"]

        content_hash = hashlib.sha256(content.casefold().encode("utf-8")).hexdigest()
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)

        validated.append({
            "content": content,
            "title": title,
            "importance": int(round(_clamp(raw.get("importance"), 1, 10, 5))),
            "emotion_weight": round(_clamp(raw.get("emotion_weight"), 0, 1, 0.5), 3),
            "confidence": round(_clamp(raw.get("confidence"), 0, 1, 0.6), 3),
            "tags": tags,
            "content_hash": content_hash,
        })
    return validated


def _extract_memories(conversation: str) -> tuple[list[dict[str, Any]], str]:
    if not conversation.strip():
        return [], '{"memories":[]}'
    if not cfg.ANALYSIS_API_KEY:
        raise DigestPipelineError("analysis_not_configured", "ANALYSIS_API_KEY is not configured")

    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/chat/completions"
    try:
        with httpx.Client(timeout=60.0) as client:
            response = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.ANALYSIS_MODEL,
                    "messages": [
                        {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                        {"role": "user", "content": f"<chat_log>\n{conversation}\n</chat_log>"},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_tokens": 2200,
                    "temperature": 0.1,
                },
            )
    except Exception as exc:
        raise DigestPipelineError("model_request_failed", f"Memory extraction request failed: {type(exc).__name__}") from exc

    if response.status_code != 200:
        excerpt = response.text[:1500]
        raise DigestPipelineError("model_http_error", f"Memory extraction model returned HTTP {response.status_code}", excerpt)

    try:
        output = response.json().get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as exc:
        raise DigestPipelineError("model_response_error", "Memory extraction response shape is invalid", response.text[:1500]) from exc
    return _parse_model_output(output), output


def _get_embedding_sync(text: str) -> list[float] | None:
    if not cfg.ANALYSIS_API_KEY:
        return None
    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/embeddings"
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                url,
                headers={"Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}"},
                json={"model": EMBEDDING_MODEL, "input": text[:2000]},
            )
        if response.status_code == 200:
            return response.json()["data"][0]["embedding"]
        log.warning("Embedding returned HTTP %s", response.status_code)
    except Exception as exc:
        log.warning("Embedding request failed: %s", type(exc).__name__)
    return None


def _mark_stale_runs() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_RUN_MINUTES)).isoformat()
    (
        _client().table("memory_digest_runs")
        .update({
            "status": "failed",
            "error_code": "stale_run_recovered",
            "error_message": "Run was still marked running after a restart or timeout",
            "completed_at": _iso_now(),
        })
        .eq("status", "running")
        .lt("started_at", cutoff)
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
        "id", "assistant_id", "trigger", "mode", "status", "source_first_message_id",
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

    batch_size = max_messages or cfg.MEMORY_DIGEST_MAX_MESSAGES or DEFAULT_BATCH_SIZE
    batch_size = max(1, min(MAX_BATCH_SIZE, int(batch_size)))

    with _pipeline_lock:
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

        run = _create_run(assistant_id, trigger, mode, messages)
        run_id = int(run["id"])
        conversation = _format_conversation(messages)

        try:
            memories, raw_output = _extract_memories(conversation)
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
                item = dict(memory)
                embedding = _get_embedding_sync(item["content"])
                if embedding:
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
        "analysis_configured": bool(cfg.ANALYSIS_API_KEY),
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
    try:
        _mark_stale_runs()
        status = get_digest_status()
        if status["backlog_count"] <= 0:
            return None

        assistant_id = status["assistant_id"]
        if _recent_run_blocks_retry(assistant_id):
            return None

        now_cst = datetime.now(CST)
        if 3 <= now_cst.hour < 4 and not _daily_run_exists(assistant_id, now_cst):
            return run_memory_digest("scheduled_daily", "execute")

        latest_at = _parse_time(status.get("latest_message_at"), CST)
        if latest_at and now_cst - latest_at.astimezone(CST) >= timedelta(hours=cfg.MEMORY_DIGEST_IDLE_HOURS):
            return run_memory_digest("idle_six_hours", "execute")
    except Exception:
        log.exception("Scheduled memory digest check failed")
    return None


# Backward-compatible entry point used by older callers.
def run_daily_digest() -> dict[str, Any]:
    return run_memory_digest("scheduled_daily", "execute")
