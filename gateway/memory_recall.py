"""Persist actual context injections off the chat path, with bounded backlog.

One RPC atomically records snapshots, updates heat/count/time and retains 150
events. A stable batch ID makes the single retry safe, even after events expire.
"""
from __future__ import annotations

import contextvars
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import BoundedSemaphore
from uuid import uuid4

from .db import get_client

log = logging.getLogger("gateway.memory_recall")
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-recall")
_slots = BoundedSemaphore(32)


def _event(code: str, count: int) -> dict:
    return {"evt": {
        "module": "memory", "operation": "record_memory_recall",
        "error_code": code, "outcome": "committed_side_effect_failed",
        "message_zh": "记忆已注入，但召回记录后台保存未完成",
        "safe_context": {"count": count},
    }}


def _persist(payload: dict) -> None:
    for attempt in range(2):
        try:
            client = get_client()
            if client is None:
                raise RuntimeError("memory recall database unavailable")
            response = client.rpc("record_memory_recall_batch_v1", payload).execute()
            if not isinstance(response.data, dict) or response.data.get("status") not in {"recorded", "already_recorded"}:
                raise RuntimeError("memory recall result unconfirmed")
            return
        except Exception:
            log.warning(
                "召回批次保存失败 batch_id=%s count=%d attempt=%d",
                payload["p_batch_id"], len(payload["p_memories"]), attempt + 1,
                exc_info=True,
                extra=_event("memory_recall_write_failed", len(payload["p_memories"])),
            )


def record_memories_in_background(memories: list[dict]) -> None:
    """Called only after a nonempty memory block joins the request context.

    No database access, JSON serialization or waiting for a free slot here.
    Only the actual injected strings are copied; vectors/full bodies are absent.
    """
    acquired = False
    try:
        snapshot = []
        seen = set()
        for memory in memories[:20]:
            if not isinstance(memory, dict):
                continue
            memory_id = memory.get("id")
            text = memory.get("injection_text")
            if (not isinstance(memory_id, int) or isinstance(memory_id, bool)
                    or memory_id <= 0 or memory_id in seen
                    or not isinstance(text, str) or not text.strip()):
                continue
            seen.add(memory_id)
            snapshot.append({
                "memory_id": memory_id, "injection_text": text,
                "title": str(memory.get("title") or ""),
                "continuity_type": memory.get("continuity_type"),
                "position": len(snapshot) + 1,
            })
        if not snapshot:
            return
        if not _slots.acquire(blocking=False):
            log.warning("召回记录后台队列已满 count=%d", len(snapshot),
                        extra=_event("memory_recall_queue_full", len(snapshot)))
            return
        acquired = True
        payload = {
            "p_batch_id": str(uuid4()),
            "p_recalled_at": datetime.now(timezone.utc).isoformat(),
            "p_memories": snapshot,
        }
        ctx = contextvars.copy_context()

        def job():
            try:
                _persist(payload)
            finally:
                _slots.release()

        _executor.submit(ctx.run, job)
        acquired = False  # Worker owns the slot from this point.
    except Exception:
        if acquired:
            _slots.release()
        log.exception("召回记录任务提交失败",
                      extra=_event("memory_recall_submit_failed", 0))
