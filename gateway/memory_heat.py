"""Atomic, idempotent heat decay for verified long-term memories."""

import logging
from typing import Any

from .db import get_client

log = logging.getLogger("gateway.memory_heat")

_SUCCESS_STATUSES = {"succeeded", "already_ran"}


def _result_payload(data: Any) -> dict[str, Any] | None:
    """Normalize the JSON value returned by a Supabase scalar RPC."""
    if isinstance(data, dict):
        return dict(data)
    if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
        return dict(data[0])
    return None


def run_heat_decay() -> dict[str, Any]:
    """Run the database-owned daily decay job.

    The RPC owns calendar-day idempotency and updates all eligible memories in
    one transaction.  This wrapper deliberately performs no direct table
    updates, so restarts and multiple gateway instances cannot double-decay a
    memory.
    """
    client = get_client()
    if not client:
        return {"status": "skipped", "reason": "supabase_unavailable"}

    try:
        response = client.rpc("run_memory_heat_decay", {}).execute()
        result = _result_payload(getattr(response, "data", None))
        if not result or result.get("status") not in _SUCCESS_STATUSES:
            log.error("Memory heat decay RPC returned an invalid response")
            return {"status": "failed", "reason": "invalid_rpc_response"}

        log.info(
            "Memory heat decay: status=%s run_date=%s elapsed_days=%s updated=%s archived=%s",
            result.get("status"),
            result.get("run_date"),
            result.get("elapsed_days", 0),
            result.get("updated_count", 0),
            result.get("archived_count", 0),
        )
        return result
    except Exception as exc:
        log.error("Memory heat decay RPC failed: %s", type(exc).__name__)
        return {"status": "failed", "reason": "rpc_failed"}

