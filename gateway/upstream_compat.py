"""Narrow compatibility fixes for OpenAI-compatible upstream proxies."""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("gateway.upstream_compat")


def _is_explicit_gemini_model(selected_model: Any) -> bool:
    return (
        isinstance(selected_model, str)
        and selected_model.strip().casefold().startswith("gemini-")
    )


def _safe_tool_call_id(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "<missing-or-invalid>"
    return value.replace("\r", "\\r").replace("\n", "\\n")[:200]


def normalize_gemini_browser_tool_history(
    messages: Any,
    enabled: bool,
    selected_model: str,
) -> Any:
    """Restore tool names required by some Gemini browser proxies.

    The transform is deliberately inactive unless the opt-in switch is true and
    the final upstream model explicitly starts with ``gemini-``. It scans only
    preceding assistant tool calls, never invents a fallback name, and copies a
    tool message only when its ``name`` must be added or corrected.
    """
    if not enabled or not _is_explicit_gemini_model(selected_model):
        return messages
    if not isinstance(messages, list):
        return messages

    call_names: dict[str, str] = {}
    normalized = messages

    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role == "assistant":
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    continue
                tool_call_id = tool_call.get("id")
                function = tool_call.get("function")
                function_name = function.get("name") if isinstance(function, dict) else None
                if (
                    isinstance(tool_call_id, str)
                    and tool_call_id
                    and isinstance(function_name, str)
                    and function_name
                ):
                    call_names[tool_call_id] = function_name
            continue

        if role != "tool":
            continue

        tool_call_id = message.get("tool_call_id")
        expected_name = call_names.get(tool_call_id) if isinstance(tool_call_id, str) else None
        if expected_name is None:
            log.warning(
                "Gemini browser tool call name unresolved: "
                "location=normalize_gemini_browser_tool_history model=%s tool_call_id=%s",
                selected_model,
                _safe_tool_call_id(tool_call_id),
            )
            continue

        current_name = message.get("name")
        if current_name == expected_name:
            continue
        if current_name not in (None, ""):
            log.warning(
                "Gemini browser tool call name conflict: "
                "location=normalize_gemini_browser_tool_history model=%s "
                "tool_call_id=%s function_name=%s",
                selected_model,
                _safe_tool_call_id(tool_call_id),
                expected_name,
            )

        if normalized is messages:
            normalized = list(messages)
        normalized_message = dict(message)
        normalized_message["name"] = expected_name
        normalized[index] = normalized_message

    return normalized
