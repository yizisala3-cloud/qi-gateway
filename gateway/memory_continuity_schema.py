"""Shared validation for versioned six-class continuity memories."""
from __future__ import annotations

import re
from typing import Any

CONTINUITY_TYPES = frozenset({"moment", "thread", "episode", "inside_joke", "profile", "interaction_rule"})
AUTOMATIC_TYPES = frozenset({"moment", "thread", "episode", "inside_joke"})
THREAD_STATES = frozenset({"open", "paused", "resolved", "dissolved", "abandoned", "unknown"})
SCHEMA_VERSION = 1
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?$")


class ContinuityDataError(ValueError):
    pass


def _only(data: dict[str, Any], names: set[str]) -> None:
    extra = set(data) - names
    if extra:
        raise ContinuityDataError(f"unsupported continuity_data fields: {', '.join(sorted(extra))}")


def _text(data: dict[str, Any], key: str, required: bool = False, limit: int = 600) -> str | None:
    value = data.get(key)
    if value is None:
        if required:
            raise ContinuityDataError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise ContinuityDataError(f"{key} must be a string or null")
    value = re.sub(r"\s+", " ", value).strip()
    if required and not value:
        raise ContinuityDataError(f"{key} is required")
    if len(value) > limit:
        raise ContinuityDataError(f"{key} is too long")
    return value or None


def _array(data: dict[str, Any], key: str, required: bool = False) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or len(value) > 8:
        raise ContinuityDataError(f"{key} must be an array with at most 8 items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ContinuityDataError(f"{key} entries must be strings")
        item = re.sub(r"\s+", " ", item).strip()
        if not item or len(item) > 120:
            raise ContinuityDataError(f"{key} entries must be 1-120 characters")
        if item not in result:
            result.append(item)
    if required and not result:
        raise ContinuityDataError(f"{key} must not be empty")
    return result


def _time(data: dict[str, Any], key: str) -> str | None:
    value = _text(data, key, limit=40)
    if value and not _TIME.fullmatch(value):
        raise ContinuityDataError(f"{key} must be an ISO date/time")
    return value


def validate_continuity_data(kind: Any, state: Any, value: Any, *, automatic: bool = False) -> dict[str, Any]:
    kind = str(kind or "").strip().casefold()
    if kind not in (AUTOMATIC_TYPES if automatic else CONTINUITY_TYPES):
        raise ContinuityDataError("invalid continuity_type for this source")
    if not isinstance(value, dict):
        raise ContinuityDataError("continuity_data must be an object")
    data = dict(value)
    state = str(state or "").strip().casefold() or None
    if kind == "thread" and state not in THREAD_STATES:
        raise ContinuityDataError("invalid thread_state")
    if kind != "thread" and state is not None:
        raise ContinuityDataError("thread_state is only valid for thread")

    if kind == "moment":
        _only(data, {"scene", "event", "response", "outcome", "moment_state", "salience_reason"})
        result = {"scene": _text(data, "scene", True), "event": _text(data, "event", True),
                  "response": _text(data, "response"), "outcome": _text(data, "outcome"),
                  "moment_state": str(data.get("moment_state") or "").casefold(),
                  "salience_reason": _text(data, "salience_reason")}
        if result["moment_state"] not in {"standalone", "linked", "absorbed"}:
            raise ContinuityDataError("invalid moment_state")
    elif kind == "thread":
        _only(data, {"open_question", "current_state", "next_expected", "closure_criteria", "closure_summary",
                     "closure_reason", "opened_at", "closed_at", "abstract_retrieval_hints", "concrete_retrieval_hints"})
        result = {"open_question": _text(data, "open_question", True), "current_state": _text(data, "current_state", True),
                  "next_expected": _text(data, "next_expected"), "closure_criteria": _array(data, "closure_criteria"),
                  "closure_summary": _text(data, "closure_summary"), "closure_reason": _text(data, "closure_reason"),
                  "opened_at": _time(data, "opened_at"), "closed_at": _time(data, "closed_at"),
                  "abstract_retrieval_hints": _array(data, "abstract_retrieval_hints"),
                  "concrete_retrieval_hints": _array(data, "concrete_retrieval_hints")}
        closed = state in {"resolved", "dissolved", "abandoned"}
        if closed and not all(result[k] for k in ("closure_summary", "closure_reason", "closed_at")):
            raise ContinuityDataError("closed thread requires closure_summary, closure_reason, and closed_at")
        if state in {"open", "paused"} and any(result[k] for k in ("closure_summary", "closure_reason", "closed_at")):
            raise ContinuityDataError("open thread cannot contain closure fields")
    elif kind == "episode":
        _only(data, {"beginning", "development", "turning_point", "outcome", "aftereffect", "episode_start_time", "episode_end_time", "closure_quality"})
        result = {"beginning": _text(data, "beginning", True), "development": _text(data, "development", True),
                  "turning_point": _text(data, "turning_point"), "outcome": _text(data, "outcome", True),
                  "aftereffect": _text(data, "aftereffect"), "episode_start_time": _time(data, "episode_start_time"),
                  "episode_end_time": _time(data, "episode_end_time"), "closure_quality": str(data.get("closure_quality") or "").casefold()}
        if result["closure_quality"] not in {"complete", "partial", "uncertain"}:
            raise ContinuityDataError("invalid closure_quality")
    elif kind == "inside_joke":
        _only(data, {"origin", "trigger_phrases", "shared_meaning", "usage_context", "avoid_context", "response_style", "first_seen_at", "last_reinforced_at", "reinforcement_count"})
        count = data.get("reinforcement_count", 0)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ContinuityDataError("reinforcement_count must be a non-negative integer")
        result = {"origin": _text(data, "origin", True), "trigger_phrases": _array(data, "trigger_phrases", True),
                  "shared_meaning": _text(data, "shared_meaning", True), "usage_context": _array(data, "usage_context"),
                  "avoid_context": _array(data, "avoid_context"), "response_style": _text(data, "response_style"),
                  "first_seen_at": _time(data, "first_seen_at"), "last_reinforced_at": _time(data, "last_reinforced_at"),
                  "reinforcement_count": count}
    elif kind == "profile":
        _only(data, {"facet", "statement", "scope", "effective_from", "effective_until", "stability", "exceptions", "basis"})
        result = {"facet": _text(data, "facet", True), "statement": _text(data, "statement", True), "scope": _text(data, "scope", True),
                  "effective_from": _time(data, "effective_from"), "effective_until": _time(data, "effective_until"),
                  "stability": str(data.get("stability") or "").casefold(), "exceptions": _array(data, "exceptions"),
                  "basis": str(data.get("basis") or "").casefold()}
        if result["stability"] not in {"stable", "contextual", "provisional"} or result["basis"] not in {"explicit_self_report", "explicit_preference", "repeated_observation", "reviewed_summary"}:
            raise ContinuityDataError("invalid profile enum")
    else:
        _only(data, {"trigger", "expected_behavior", "forbidden_behavior", "scope", "priority", "rule_state", "effective_from", "effective_until", "exceptions", "explicit_instruction"})
        priority = data.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 10:
            raise ContinuityDataError("priority must be an integer between 1 and 10")
        result = {"trigger": _text(data, "trigger", True), "expected_behavior": _text(data, "expected_behavior", True),
                  "forbidden_behavior": _array(data, "forbidden_behavior"), "scope": _text(data, "scope", True), "priority": priority,
                  "rule_state": str(data.get("rule_state") or "").casefold(), "effective_from": _time(data, "effective_from"),
                  "effective_until": _time(data, "effective_until"), "exceptions": _array(data, "exceptions"),
                  "explicit_instruction": _text(data, "explicit_instruction", True)}
        if result["rule_state"] not in {"active", "revoked", "superseded"}:
            raise ContinuityDataError("invalid rule_state")
    return {key: item for key, item in result.items() if item is not None}
