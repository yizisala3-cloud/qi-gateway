"""Long-term goals: validated commands and authoritative SQL aggregates.

The database serializes commands with completion facts; never count an admin
list or make progress depend on a client-side refresh/maintenance pass.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from . import planning_runtime as runtime
from .planning_common import PlanningError

MAX_COUNT = 9_007_199_254_740_991


def _rpc(name: str, params: dict[str, Any]) -> Any:
    try:
        return runtime._require_client().rpc(name, params).execute().data
    except PlanningError:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "sqlstate", None)
        if code in {"PG400", "PG404", "PG409"}:
            message = getattr(exc, "message", None) or str(exc).split("CONTEXT:")[0].strip()
            raise PlanningError("goal_" + code[2:], message, int(code[2:])) from exc
        raise


def list_goals() -> list[dict[str, Any]]:
    return _rpc("planning_goal_read", {"p_goal_id": None})


def get_goal(goal_id: int) -> dict[str, Any]:
    result = _rpc("planning_goal_read", {"p_goal_id": goal_id})
    if result is None:
        raise PlanningError("goal_404", "目标已删除或不存在", 404)
    return result


def candidates() -> list[dict[str, Any]]:
    return _rpc("planning_goal_candidates", {})


def _integer(payload: dict[str, Any], key: str, label: str, minimum: int) -> None:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_COUNT:
        raise PlanningError("invalid_payload", f"{label}须为{'正整数' if minimum else '非负整数'}", 400)


def command(action: str, goal_id: int | None, payload: Any, *, idempotency_key: str | None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise PlanningError("invalid_payload", "请求内容必须是对象", 400)
    try:
        request_key = str(UUID(idempotency_key or ""))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PlanningError("invalid_payload", "缺少有效的 Idempotency-Key 操作身份", 400) from exc
    if action != "create":
        _integer(payload, "expected_version", "目标版本", 1)
    if action in {"create", "edit"}:
        for key, label in (("content", "目标内容"), ("unit", "单位")):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise PlanningError("invalid_payload", f"请填写{label}", 400)
        value = payload.get("target_value")
        try:
            number = Decimal(str(value))
            if isinstance(value, bool) or not number.is_finite() or number <= 0:
                raise InvalidOperation
        except (InvalidOperation, ValueError) as exc:
            raise PlanningError("invalid_payload", "目标数值必须为有限正数", 400) from exc
        _integer(payload, "planned_count", "计划执行次数", 1)
    elif action == "adjust":
        _integer(payload, "executed_count", "已执行次数", 0)
    elif action == "link":
        if "task_id" not in payload:
            raise PlanningError("invalid_payload", "请选择待办或明确解除关联", 400)
        if payload["task_id"] is not None:
            _integer(payload, "task_id", "待办身份", 1)
    return _rpc("planning_goal_command", {
        "p_action": action, "p_goal_id": goal_id, "p_payload": payload,
        "p_request_key": request_key,
    })
