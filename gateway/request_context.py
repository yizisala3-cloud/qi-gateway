"""Classify chat requests and add gateway context without mutating client prompts."""
from __future__ import annotations

import re
from typing import Any


GATEWAY_CONTEXT_HEADING = "【qi-gateway 辅助上下文】"
TODO_FEEDBACK_HEADING = "【待办状态反馈说明】"

TODO_FEEDBACK_PATTERNS = (
    r"(?:做完了?|完成了|搞定了?|弄好了?|处理完了?|交完了?|已经交了)",
    r"(?:延后|延期|推迟|顺延|改到|挪到)",
    r"(?:晚点|稍后|过会儿|一会儿|明天|改天|下周|再过\s*[一二两三四五六七八九十\d]+\s*个?\s*(?:分钟|小时|天)|[一二两三四五六七八九十\d]+\s*个?\s*(?:分钟|小时|天)后)[^。！？]{0,12}(?:再|提醒|做|处理)",
    r"(?:不做了|不用做了|别再提醒|不用提醒|取消(?:这个|那个|该|刚才的)(?:待办|提醒|任务)?|取消(?:待办|提醒|任务)|(?:这个|那个|刚才的?).{0,8}(?:待办|提醒|任务)取消(?:掉|了)?)",
)


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


def extract_recent_turns(
    messages: Any, max_turns: int = 3
) -> list[tuple[str | None, str | None]]:
    """Split ordinary history before the latest user message into strict turns.

    Returns chronological ``(user_text, assistant_text)`` pairs; either side is
    None when that half of the turn does not exist (never fabricated). One
    turn is one historical user message plus the ordinary assistant replies
    before the next user message; when those replies are consecutive, only the
    latest one is kept (explicit policy, tested, not silently merged). A
    leading assistant with no preceding user becomes ``(None, text)`` and does
    not count against max_turns, but is dropped when turns were truncated.
    System, tool, and other non-chat roles are excluded.
    """
    if not isinstance(messages, list):
        return []
    latest_user_index = None
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, dict) and message.get("role") == "user":
            latest_user_index = index
            break
    if latest_user_index is None:
        return []

    turns: list[list[str | None]] = []
    leading_assistant: str | None = None
    for index in range(latest_user_index):
        message = messages[index]
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        text = message_text(message).strip()
        if not text:
            continue
        if role == "user":
            turns.append([text, None])
        elif turns:
            turns[-1][1] = text
        else:
            leading_assistant = text

    limited = turns[-max_turns:] if max_turns > 0 else []
    result: list[tuple[str | None, str | None]] = [
        (user_text, assistant_text) for user_text, assistant_text in limited
    ]
    if leading_assistant and len(turns) < max_turns:
        result.insert(0, (None, leading_assistant))
    return result


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


def build_todo_feedback_guidance(user_text: Any) -> str:
    """Return guidance only for clear feedback about an existing todo.

    The model still decides whether the user's words refer to a todo. This
    helper merely prevents an existing item from being recreated when a user
    clearly reports completion, postponement, or cancellation.
    """
    if not isinstance(user_text, str):
        return ""
    normalized = re.sub(r"\s+", " ", user_text).strip()
    if not normalized or not any(
        re.search(pattern, normalized, flags=re.IGNORECASE)
        for pattern in TODO_FEEDBACK_PATTERNS
    ):
        return ""
    return (
        f"{TODO_FEEDBACK_HEADING}\n"
        "用户当前表达可能是在反馈一条已经存在的待办，而不是要求创建新待办。\n"
        "如果待办工具可用，请先调用 list_today_todos 查找现有记录；"
        "目标唯一且含义明确时，再按用户原意调用 complete_todo、snooze_todo 或 cancel_todo。\n"
        "状态变化不得调用 create_todo，不得复制出内容相同的新待办。\n"
        "如果匹配到多条、指代不清，或无法可靠确定新的时间，请先向用户确认，不要猜测修改。"
    )

