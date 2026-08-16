"""Reviewed continuity digest pipeline with an independent durable cursor.

``public.chat_messages`` is an immutable input here. Every database interaction
with that table is a SELECT; state changes happen only through continuity state,
run, and pending-memory RPCs defined by the continuity migration.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from .config import cfg
from .db import get_client
from .memory_continuity_shadow import (
    DEFAULT_MAX_CHARS,
    ShadowPreviewError,
    _analysis_configured,
    _clean_content,
    _format_conversation,
    _parse_time,
    _resolve_message_time,
    extract_continuity_candidates,
    resolve_shadow_assistant_id,
)
from .memory_extract import (
    DigestPipelineError,
    _claim_slot,
    _get_embedding_sync,
    _update_heartbeat,
)

log = logging.getLogger("gateway.memory_continuity")

INITIAL_CURSOR = 177
AUTO_THRESHOLD = 80
MAX_BATCH_MESSAGES = 80
MAX_BATCH_CHARS = DEFAULT_MAX_CHARS
CONTINUITY_TRIGGERS = frozenset({
    "continuity_threshold", "continuity_manual", "continuity_retry",
})


class ContinuityPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _client():
    client = get_client()
    if not client:
        raise ContinuityPipelineError(
            "database_unavailable",
            "Supabase server client is unavailable",
            503,
        )
    return client


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rpc_object(name: str, params: dict[str, Any]) -> dict[str, Any]:
    response = _client().rpc(name, params).execute()
    data = response.data
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        raise ContinuityPipelineError("database_response_error", f"{name} returned an invalid response", 500)
    return data


def resolve_continuity_assistant_id() -> str:
    assistant_id = resolve_shadow_assistant_id()
    if not assistant_id:
        raise ContinuityPipelineError("assistant_not_found", "No usable assistant_id exists in chat_messages", 404)
    return assistant_id


def _get_cursor(assistant_id: str) -> dict[str, Any]:
    return _rpc_object(
        "get_or_create_memory_continuity_cursor",
        {"p_assistant_id": assistant_id},
    )


def _future(value: Any, now: datetime | None = None) -> bool:
    parsed = _parse_time(value, timezone.utc)
    return bool(parsed and parsed > (now or _now()))


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


def _backlog_count(assistant_id: str, cursor: int) -> int:
    response = (
        _client().table("chat_messages")
        .select("id", count="exact")
        .eq("assistant_id", assistant_id)
        .gt("id", cursor)
        .limit(1)
        .execute()
    )
    return int(response.count or 0)


def list_continuity_runs(limit: int = 20, assistant_id: str | None = None) -> list[dict[str, Any]]:
    query = (
        _client().table("memory_digest_runs")
        .select("*")
        .eq("pipeline", "continuity")
        .order("started_at", desc=True)
        .limit(max(1, min(100, int(limit))))
    )
    if assistant_id:
        query = query.eq("assistant_id", assistant_id)
    return [_public_run(row) for row in (query.execute().data or [])]


def get_continuity_status() -> dict[str, Any]:
    assistant_id = resolve_continuity_assistant_id()
    cursor = _get_cursor(assistant_id)
    cursor_id = int(cursor.get("last_processed_message_id") or INITIAL_CURSOR)
    latest = _latest_message(assistant_id)
    return {
        "assistant_id": assistant_id,
        "status": cursor.get("status") or "ready",
        "cursor": cursor_id,
        "latest_message_id": int(latest.get("id") or 0) if latest else None,
        "latest_message_at": latest.get("created_at") if latest else None,
        "backlog_count": _backlog_count(assistant_id, cursor_id),
        "auto_threshold": AUTO_THRESHOLD,
        "manual_cooldown_until": cursor.get("manual_cooldown_until"),
        "auto_cooldown_until": cursor.get("auto_cooldown_until"),
        "blocked_first_message_id": cursor.get("blocked_first_message_id"),
        "blocked_last_message_id": cursor.get("blocked_last_message_id"),
        "blocked_message_count": cursor.get("blocked_message_count"),
        "blocked_at": cursor.get("blocked_at"),
        "pause_reason": cursor.get("pause_reason"),
        "analysis_model": cfg.ANALYSIS_MODEL,
        "analysis_configured": _analysis_configured(),
        "recent_runs": list_continuity_runs(20, assistant_id),
    }


def _fetch_rows_after(assistant_id: str, cursor: int) -> list[dict[str, Any]]:
    response = (
        _client().table("chat_messages")
        .select("id,assistant_id,conversation_id,role,content,created_at")
        .eq("assistant_id", assistant_id)
        .gt("id", cursor)
        .order("id")
        .limit(MAX_BATCH_MESSAGES)
        .execute()
    )
    return response.data or []


def _fetch_blocked_rows(assistant_id: str, cursor: dict[str, Any]) -> list[dict[str, Any]]:
    first_id = cursor.get("blocked_first_message_id")
    last_id = cursor.get("blocked_last_message_id")
    expected = int(cursor.get("blocked_message_count") or 0)
    if first_id is None or last_id is None or expected < 1:
        raise ContinuityPipelineError("blocked_batch_missing", "The paused batch metadata is incomplete", 409)
    response = (
        _client().table("chat_messages")
        .select("id,assistant_id,conversation_id,role,content,created_at")
        .eq("assistant_id", assistant_id)
        .gte("id", int(first_id))
        .lte("id", int(last_id))
        .order("id")
        .limit(MAX_BATCH_MESSAGES)
        .execute()
    )
    rows = response.data or []
    if len(rows) != expected or not rows or int(rows[0]["id"]) != int(first_id) or int(rows[-1]["id"]) != int(last_id):
        raise ContinuityPipelineError("blocked_batch_missing", "The paused source batch is no longer complete", 409)
    return rows


def _turns(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    ordered = sorted(rows, key=lambda row: int(row.get("id") or 0))
    turns: list[list[dict[str, Any]]] = []
    index = 0
    while index < len(ordered):
        start = index
        role = str(ordered[index].get("role") or "").strip().casefold()
        conversation_id = str(ordered[index].get("conversation_id") or "")
        index += 1
        if role in {"user", "assistant"}:
            while index < len(ordered):
                next_role = str(ordered[index].get("role") or "").strip().casefold()
                next_conversation = str(ordered[index].get("conversation_id") or "")
                if next_role == "assistant" and next_conversation == conversation_id:
                    index += 1
                else:
                    break
        turns.append(ordered[start:index])
    return turns


def _normalize_selected(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for row in rows:
        role = str(row.get("role") or "").strip().casefold()
        if role not in {"user", "assistant"}:
            continue
        content = _clean_content(row.get("content"))
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


def _trim_oversized_turn_messages(
    messages: list[dict[str, Any]],
    max_chars: int,
) -> list[dict[str, Any]]:
    """Trim model text while preserving the oversized turn's source boundary.

    Normal batches never use this path. For the first turn only, retain as many
    normalized message envelopes as the budget can represent, favoring user
    text and the final assistant reply, then distribute the remaining content
    budget with the same priority. Raw rows are deliberately left untouched.
    """
    usable = [dict(message) for message in messages if str(message.get("content") or "")]
    if not usable or max_chars <= 100:
        return []

    final_assistant = next(
        (
            index
            for index in range(len(usable) - 1, -1, -1)
            if usable[index].get("role") == "assistant"
        ),
        None,
    )

    def weight(index: int) -> int:
        if usable[index].get("role") == "user":
            return 3
        if index == final_assistant:
            return 2
        return 1

    # Each retained message costs 100 characters of formatting allowance plus
    # at least one character of useful body text.
    max_message_count = min(len(usable), max_chars // 101)
    if max_message_count < 1:
        return []
    selected_indices = sorted(
        sorted(range(len(usable)), key=lambda index: (-weight(index), index))[:max_message_count]
    )
    selected = [usable[index] for index in selected_indices]
    weights = [weight(index) for index in selected_indices]
    capacity = max_chars - (100 * len(selected))
    allocations = [1] * len(selected)
    remaining = capacity - len(selected)

    while remaining > 0:
        active = [
            index
            for index, message in enumerate(selected)
            if allocations[index] < len(message["content"])
        ]
        if not active:
            break
        total_weight = sum(weights[index] for index in active)
        grants = {
            index: min(
                len(selected[index]["content"]) - allocations[index],
                max(1, remaining * weights[index] // total_weight),
            )
            for index in active
        }
        progressed = 0
        for index in active:
            grant = min(grants[index], remaining)
            allocations[index] += grant
            remaining -= grant
            progressed += grant
            if remaining <= 0:
                break
        if progressed == 0:
            break

    trimmed: list[dict[str, Any]] = []
    for message, allocation in zip(selected, allocations):
        copied = dict(message)
        copied["content"] = message["content"][:allocation].rstrip()
        if copied["content"]:
            trimmed.append(copied)
    return trimmed


def prepare_continuity_batch(
    rows: list[dict[str, Any]],
    max_chars: int = MAX_BATCH_CHARS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return raw rows covered by the cursor and normalized model messages."""
    selected_raw: list[dict[str, Any]] = []
    for turn in _turns(rows):
        candidate_raw = selected_raw + turn
        candidate_messages = _normalize_selected(candidate_raw)
        candidate_chars = sum(len(item["content"]) + 100 for item in candidate_messages)
        if candidate_chars > max_chars:
            if not selected_raw:
                # The cursor must be able to move past a single pasted document
                # or code block. Cover the complete raw turn, but trim only the
                # model-facing copies to the configured character budget.
                return list(turn), _trim_oversized_turn_messages(candidate_messages, max_chars)
            break
        selected_raw = candidate_raw
    return selected_raw, _normalize_selected(selected_raw)


def _set_running_run(run_id: int, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    update = {
        "status": "running",
        "pipeline": "continuity",
        "source_first_message_id": int(raw_rows[0]["id"]),
        "source_last_message_id": int(raw_rows[-1]["id"]),
        "message_count": len(raw_rows),
        "model_name": cfg.ANALYSIS_MODEL,
    }
    _client().table("memory_digest_runs").update(update).eq("id", run_id).execute()
    _update_heartbeat(run_id)
    return {"id": run_id, **update}


def _load_run(run_id: int, fallback: dict[str, Any] | None = None) -> dict[str, Any]:
    response = (
        _client().table("memory_digest_runs")
        .select("*")
        .eq("id", run_id)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else (fallback or {"id": run_id})


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "assistant_id", "pipeline", "trigger", "mode", "status",
        "source_first_message_id", "source_last_message_id", "message_count",
        "extracted_count", "inserted_count", "model_name", "preview_memories",
        "error_code", "error_message", "started_at", "completed_at", "created_at",
    }
    return {key: run.get(key) for key in allowed}


def _mark_failed(run_id: int, code: str, message: str) -> dict[str, Any]:
    payload = {
        "status": "failed",
        "error_code": code,
        "error_message": message[:2000],
        "completed_at": _now().isoformat(),
        "heartbeat_at": None,
    }
    response = _client().table("memory_digest_runs").update(payload).eq("id", run_id).execute()
    return response.data[0] if response.data else {"id": run_id, **payload}


def _record_failure(run_id: int, code: str, message: str) -> None:
    try:
        _mark_failed(run_id, code, message)
    except Exception:
        log.exception("Failed to persist continuity run failure: run_id=%s code=%s", run_id, code)


def _enrich_candidates(candidates: list[dict[str, Any]], run_id: int) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for candidate in candidates:
        _update_heartbeat(run_id)
        item = dict(candidate)
        item["content"] = str(item["content"])[:600].strip()
        item["content_hash"] = hashlib.sha256(item["content"].casefold().encode("utf-8")).hexdigest()
        try:
            item["embedding"] = _get_embedding_sync(item["content"])
        except DigestPipelineError as exc:
            raise ContinuityPipelineError("embedding_error", str(exc), 422) from exc
        enriched.append(item)
    return enriched


def run_continuity_digest(*, automatic: bool = False) -> dict[str, Any]:
    if not _analysis_configured():
        raise ContinuityPipelineError(
            "analysis_not_configured",
            "The analysis model provider is not fully configured",
            503,
        )

    assistant_id = resolve_continuity_assistant_id()
    cursor = _get_cursor(assistant_id)
    paused = cursor.get("status") == "paused_empty"
    now = _now()

    if automatic:
        if paused:
            raise ContinuityPipelineError("paused_empty", "Continuity automation is paused", 409)
        if _future(cursor.get("auto_cooldown_until"), now):
            raise ContinuityPipelineError("auto_cooldown", "Continuity automation is cooling down", 409)
        backlog = _backlog_count(
            assistant_id,
            int(cursor.get("last_processed_message_id") or INITIAL_CURSOR),
        )
        if backlog < AUTO_THRESHOLD:
            raise ContinuityPipelineError("below_threshold", "Continuity backlog is below the automatic threshold", 409)
        trigger = "continuity_threshold"
        rows = _fetch_rows_after(
            assistant_id,
            int(cursor.get("last_processed_message_id") or INITIAL_CURSOR),
        )
    else:
        if _future(cursor.get("manual_cooldown_until"), now):
            raise ContinuityPipelineError("manual_cooldown", "Manual continuity execution is cooling down", 429)
        trigger = "continuity_retry" if paused else "continuity_manual"
        rows = (
            _fetch_blocked_rows(assistant_id, cursor)
            if paused
            else _fetch_rows_after(
                assistant_id,
                int(cursor.get("last_processed_message_id") or INITIAL_CURSOR),
            )
        )

    if not rows:
        raise ContinuityPipelineError("no_new_messages", "No new chat messages are available", 409)

    raw_rows, messages = prepare_continuity_batch(rows)
    if not raw_rows or not messages:
        raise ContinuityPipelineError("no_new_messages", "No usable chat messages are available", 409)

    claim = _claim_slot(assistant_id, trigger, "execute")
    if claim.get("status") == "already_running":
        raise ContinuityPipelineError("already_running", "Another digest is already running", 409)
    if claim.get("status") == "not_paused":
        raise ContinuityPipelineError("not_paused", "The paused batch has already been resumed", 409)
    if claim.get("status") == "paused_empty":
        raise ContinuityPipelineError("paused_empty", "Continuity automation is paused", 409)
    if claim.get("status") != "claimed":
        raise ContinuityPipelineError("commit_failed", "Failed to claim a digest execution slot", 500)
    run_id = int(claim["run_id"])
    run = _set_running_run(run_id, raw_rows)

    try:
        evidence_times = {message["id"]: message["source_time"] for message in messages}
        candidates = extract_continuity_candidates(
            _format_conversation(messages),
            evidence_times,
        )
        candidates = [
            candidate
            for candidate in candidates
            if len(str(candidate.get("content") or "").strip()) >= 5
        ]
        _update_heartbeat(run_id)

        if not candidates:
            _client().rpc(
                "pause_memory_continuity_empty",
                {"p_run_id": run_id},
            ).execute()
            try:
                saved = _load_run(run_id, run)
            except Exception:
                saved = {
                    **run,
                    "status": "succeeded",
                    "extracted_count": 0,
                    "inserted_count": 0,
                    "preview_memories": [],
                }
            result = _public_run(saved)
            result.update({
                "cursor_before": int(cursor.get("last_processed_message_id") or INITIAL_CURSOR),
                "cursor_after": int(cursor.get("last_processed_message_id") or INITIAL_CURSOR),
                "paused_empty": True,
            })
            return result

        enriched = _enrich_candidates(candidates, run_id)
        try:
            response = _client().rpc(
                "commit_memory_continuity_run",
                {"p_run_id": run_id, "p_candidates": enriched},
            ).execute()
            inserted_count = int(response.data or 0)
        except Exception as exc:
            raise ContinuityPipelineError("commit_failed", "Continuity commit failed", 500) from exc

        try:
            saved = _load_run(run_id, run)
        except Exception:
            saved = {
                **run,
                "status": "succeeded",
                "extracted_count": len(candidates),
                "inserted_count": inserted_count,
                "preview_memories": candidates,
            }
        result = _public_run(saved)
        result.update({
            "inserted_count": inserted_count,
            "cursor_before": int(cursor.get("last_processed_message_id") or INITIAL_CURSOR),
            "cursor_after": int(raw_rows[-1]["id"]),
            "paused_empty": False,
        })
        return result
    except ContinuityPipelineError as exc:
        _record_failure(run_id, exc.code, str(exc))
        raise
    except ShadowPreviewError as exc:
        _record_failure(run_id, exc.code, str(exc))
        raise ContinuityPipelineError(exc.code, str(exc), 422) from exc
    except Exception as exc:
        log.exception("Continuity digest failed: run_id=%s error=%s", run_id, type(exc).__name__)
        _record_failure(run_id, "pipeline_error", f"{type(exc).__name__}: {str(exc)[:1200]}")
        raise ContinuityPipelineError("pipeline_error", "Continuity pipeline failed", 500) from exc


def run_continuity_digest_if_due() -> dict[str, Any] | None:
    if not _analysis_configured():
        return None
    try:
        return run_continuity_digest(automatic=True)
    except ContinuityPipelineError as exc:
        if exc.code in {"paused_empty", "auto_cooldown", "below_threshold", "no_new_messages", "already_running"}:
            return None
        log.warning("Continuity automatic check failed: code=%s", exc.code)
        return None
    except Exception as exc:
        log.exception("Continuity automatic check failed: error=%s", type(exc).__name__)
        return None


def skip_blocked_continuity_batch() -> dict[str, Any]:
    assistant_id = resolve_continuity_assistant_id()
    cursor = _get_cursor(assistant_id)
    if cursor.get("status") != "paused_empty":
        raise ContinuityPipelineError("not_paused", "Continuity pipeline is not paused", 409)
    if _future(cursor.get("manual_cooldown_until")):
        raise ContinuityPipelineError("manual_cooldown", "Manual continuity execution is cooling down", 429)
    try:
        result = _rpc_object(
            "skip_memory_continuity_blocked",
            {"p_assistant_id": assistant_id},
        )
    except ContinuityPipelineError:
        raise
    except Exception as exc:
        message = str(exc).casefold()
        if "not_paused" in message:
            raise ContinuityPipelineError("not_paused", "Continuity pipeline is not paused", 409) from exc
        if "blocked_batch_missing" in message:
            raise ContinuityPipelineError("blocked_batch_missing", "The paused batch metadata is incomplete", 409) from exc
        if "already_running" in message:
            raise ContinuityPipelineError("already_running", "Another digest is already running", 409) from exc
        raise ContinuityPipelineError("commit_failed", "Failed to skip the paused batch", 500) from exc
    return {
        "status": "succeeded",
        "trigger": "continuity_skip",
        "run_id": result.get("run_id"),
        "cursor": result.get("cursor"),
        "inserted_count": 0,
    }
