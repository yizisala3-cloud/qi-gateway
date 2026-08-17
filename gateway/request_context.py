"""Classify chat requests and add gateway context without mutating client prompts."""
from __future__ import annotations

import re
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
PROACTIVE_CONTROL_HEADING = "【客户端控制信号说明】"
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


def annotate_proactive_control_signal(messages: Any) -> list[dict]:
    """Identify a synthetic trigger without changing the client's send policy.

    OrangeChat's original system message is left byte-for-byte unchanged. This
    separate instruction distinguishes the synthetic final user message from
    real conversation history. Whether and how to send remains governed by the
    client's original system prompt.
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
            f"{PROACTIVE_CONTROL_HEADING}\n\n"
            "最后一条 role=user 消息由客户端自动生成，只是启动本次主动判断的控制信号。\n"
            "这条控制信号不是用户本人发言，也不代表用户说过、做过或表达过任何事情。\n\n"
            "真实对话历史截止于这条控制信号之前。\n"
            "不得根据控制信号虚构用户的行为、状态或意图，也不要重复回答最后一条真实用户消息。\n"
            "是否发送消息、如何拒绝发送以及输出格式，完全遵循客户端原始 system prompt；"
            "本说明不增加或替换任何发送协议。"
        ),
    })
    return result

