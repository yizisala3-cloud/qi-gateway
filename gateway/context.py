"""上下文拼装：并发获取所有注入源，按固定顺序拼装。

注入结构（静态 → 动态）：
[1] 人设 persona（静态）
[2] Eventide 身体状态卡
[3] 长期记忆搜索结果
[4] 短期上下文 chat_messages 最近10条
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from .persona import load_persona
from .memory_search import search_memories, format_memories_for_injection
from . import db
from . import eventide_bridge

log = logging.getLogger("gateway.context")

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ctx")

# ── 数据源构建函数 ────────────────────────────────


def build_eventide_context() -> str:
    """[2] Eventide 身体状态卡。"""
    try:
        state_data = db.load_eventide_state()

        if not state_data:
            state_data = eventide_bridge.create_initial_state()
            if not state_data:
                return ""
            db.save_eventide_state(state_data)

        # chat_messages may contain client-generated proactive control signals.
        # Until those rows have a durable source marker, omit the optional
        # counterpart timestamp rather than misclassifying a control message.
        new_data, card = eventide_bridge.advance_and_render(state_data)

        if new_data:
            db.save_eventide_state(new_data)

        return card or ""

    except Exception as e:
        log.error(f"Eventide context 构建失败: {e}")
        return ""


def build_recent_chat_context(limit: int = 10) -> str:
    """[4] 从 chat_messages 拉最近 N 条对话作为短期上下文。"""
    try:
        client = db.get_client()
        if not client:
            return ""
        resp = (
            client.table("chat_messages")
            .select("role, content, created_at")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        if not resp.data:
            return ""

        messages = list(reversed(resp.data))
        lines = ["[最近对话]"]
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                prefix = "叶子" if role == "user" else "栖"
                lines.append(f"{prefix}: {content[:200]}")

        return "\n".join(lines)
    except Exception as e:
        log.error(f"短期上下文构建失败: {e}")
        return ""


# ── 主入口：完整上下文构建 ────────────────────────

def build_context(user_message: str = "") -> str:
    """并发拼装完整上下文注入内容。

    Args:
        user_message: 用户最新一条消息（用于记忆搜索 query）。
                      为空时跳过记忆搜索。
    """
    futures = {
        "persona": _executor.submit(load_persona),
        "eventide": _executor.submit(build_eventide_context),
        "recent_chat": _executor.submit(build_recent_chat_context, 10),
    }

    results = {}
    for name, future in futures.items():
        try:
            results[name] = future.result(timeout=8.0) or ""
        except Exception as e:
            log.warning(f"context 数据源 {name} 超时或失败: {e}")
            results[name] = ""

    # 记忆搜索
    memories_text = ""
    if user_message.strip():
        try:
            loop = asyncio.new_event_loop()
            memories = loop.run_until_complete(search_memories(user_message, top_k=8))
            loop.close()
            memories_text = format_memories_for_injection(memories)
        except Exception as e:
            log.warning(f"记忆搜索失败: {e}")

    # 按固定顺序拼装
    parts = []

    if results["persona"]:
        parts.append(results["persona"])

    if results["eventide"]:
        parts.append(results["eventide"])

    if memories_text:
        parts.append(memories_text)

    if results["recent_chat"]:
        parts.append(results["recent_chat"])

    return "\n\n".join(parts)
