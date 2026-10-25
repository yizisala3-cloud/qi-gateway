"""Single-transaction commands for the console's common occurrence actions."""
from datetime import datetime
from uuid import UUID

from . import planning_common as common
from . import planning_runtime as runtime
from . import planning_serialization as presentation


def occurrence_command(occurrence_id: int, payload, now: datetime | None = None):
    if not isinstance(payload, dict) or set(payload) - {
        "status", "request_key", "partial_note", "actual_logged_duration",
    }:
        raise common.PlanningError("invalid_payload", "待办操作参数无效")
    target = payload.get("status")
    if target not in {"in_progress", "partial", "completed"}:
        raise common.PlanningError("invalid_payload", "不支持的待办操作")
    try:
        key = str(UUID(str(payload.get("request_key"))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise common.PlanningError("invalid_payload", "缺少有效的操作身份") from exc
    note = (common._clean_text(payload.get("partial_note"), "partial_note", required=True,
                               maximum=common.MAX_NOTE_LENGTH) if target == "partial" else None)
    seconds = (common.parse_logged_duration_seconds(payload.get("actual_logged_duration"),
               "actual_logged_duration") if target == "completed" else None)
    now = now or runtime._now()
    try:
        result = runtime._require_client().rpc("planning_occurrence_command", {
            "p_occurrence_id": occurrence_id, "p_status": target, "p_request_key": key,
            "p_partial_note": note, "p_logged_seconds": seconds, "p_now": common._iso(now),
        }).execute().data
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "sqlstate", None)
        if code in {"PO400", "PO404", "PO409", "PO422"}:
            message = getattr(exc, "message", None) or str(exc).split("CONTEXT:")[0].strip()
            raise common.PlanningError("occurrence_" + code[2:], message, int(code[2:])) from exc
        raise
    row = presentation.serialize_occurrence(result["occurrence"], result["task"], now)
    # Used only to reject a late receipt; it does not change display semantics.
    row["updated_at"] = result["occurrence"]["updated_at"]
    row["replayed"] = bool(result.get("replayed"))
    return row
