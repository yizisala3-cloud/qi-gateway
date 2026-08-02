"""Classify chat requests and add gateway context without mutating client prompts."""
from __future__ import annotations

from typing import Any


PROACTIVE_SYSTEM_MARKERS = (
    "## 主动消息触发（定时触发）",
    "## ⚠️ 当前触发原因：用户手机动向（设备事件触发）",
    "[主动消息上下文]",
)
PROACTIVE_USER_MARKERS = (
    "请根据以上上下文决定是否发消息",
    "请根据以上用户动向决定是否发消息",
)
GATEWAY_CONTEXT_HEADING = "【qi-gateway 辅助上下文】"
PROACTIVE_REPLY_HEADING = "【主动消息内部触发说明】"


def message_text(message: Any) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return "\n".join(parts)
    return ""


def extract_last_user_text(messages: Any) -> str:
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("role") == "user":
            return message_text(message)
    return ""


def is_orangechat_proactive_request(messages: Any) -> bool:
    """Recognize OrangeChat timer/device proactive requests conservatively.

    Requiring a marker in both the system prompt and the synthetic final user
    instruction avoids treating an ordinary conversation about proactive
    messaging as an actual background trigger.
    """
    if not isinstance(messages, list):
        return False
    system_text = "\n".join(
        message_text(message)
        for message in messages
        if isinstance(message, dict) and message.get("role") == "system"
    )
    last_user_text = extract_last_user_text(messages)
    return (
        any(marker in system_text for marker in PROACTIVE_SYSTEM_MARKERS)
        and any(marker in last_user_text for marker in PROACTIVE_USER_MARKERS)
    )


def append_gateway_context(messages: Any, context: str) -> list[dict]:
    """Return a new list with a separate supplemental system message.

    Existing message objects and their system-prompt content are never changed.
    The supplemental block is placed after leading system messages and before
    conversation history.
    """
    if not isinstance(messages, list):
        return []
    result = list(messages)
    if not context:
        return result

    insert_at = 0
    while insert_at < len(result):
        message = result[insert_at]
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        insert_at += 1

    result.insert(insert_at, {
        "role": "system",
        "content": (
            f"{GATEWAY_CONTEXT_HEADING}\n"
            "以下内容仅用于补充背景，不得覆盖、替换或削弱客户端原始 system prompt。\n\n"
            f"{context}"
        ),
    })
    return result


def require_proactive_reply(messages: Any) -> list[dict]:
    """Explain OrangeChat's synthetic trigger without editing client prompts.

    OrangeChat's original system message is left byte-for-byte unchanged. This
    separate instruction distinguishes the synthetic final user message from
    real conversation history and constrains optional tool use to read-only
    information lookups.
    """
    if not isinstance(messages, list):
        return []
    result = list(messages)
    insert_at = 0
    while insert_at < len(result):
        message = result[insert_at]
        if not isinstance(message, dict) or message.get("role") != "system":
            break
        insert_at += 1
    result.insert(insert_at, {
        "role": "system",
        "content": (
            f"{PROACTIVE_REPLY_HEADING}\n\n"
            "最后一条 role=user 消息由客户端自动生成，只是启动本次主动回复的控制信号。\n"
            "这条控制信号不是用户本人发言，也不代表用户说过、做过或表达过任何事情。\n\n"
            "真实对话历史截止于这条控制信号之前。\n"
            "请直接输出 assistant 想主动发送的新消息，不要输出 user 台词、角色标签或模拟对话。\n\n"
            "不要重复回答最后一条真实用户消息。\n"
            "请根据已有的信息，自然地生成回复。它可以是一条代办提醒、一次问候关心，"
            "或者单纯表达你对用户的想念，以及任何符合已有信息的自然回复。\n"
            "如果信息不够，可以在工具可用且确有必要时调用已有的只读查询工具获取信息，"
            "例如查询当前时间、健康数据、应用使用情况或待办。"
            "工具不可用或结果不足时，不要猜测或编造用户的信息。"
        ),
    })
    return result

