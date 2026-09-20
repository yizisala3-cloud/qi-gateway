"""上下文拼装：并发获取所有注入源，按固定顺序拼装。

注入结构（静态 → 动态）：
[0] 当前时间戳（可关）
[1] 人设 persona（静态）
[2] Eventide 身体状态卡（可关）
[3] 长期记忆搜索结果
[4] 短期上下文 chat_messages 最近 N 条（可关，N 可配）
"""
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .persona import load_persona
from .memory_search import search_memories, format_memories_for_injection
from . import app_settings
from . import db
from . import eventide_bridge

log = logging.getLogger("gateway.context")

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ctx")

# 时间戳展示时区：与 db.py / eventide_admin_api.py 的挂钟约定一致（Asia/Shanghai）。
_CST = timezone(timedelta(hours=8))
# Python weekday() 周一=0；标签按周一到周日排。
_WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")

# ── 数据源构建函数 ────────────────────────────────


def build_eventide_context() -> str:
    """[2] Eventide 身体状态卡。"""
    # 注入开关关闭 = 彻底暂停：不 load、不 save、不 tick、不创建初始状态，
    # 数值冻结在关闭那一刻。early return 同时是防御层与直测入口。
    if not app_settings.is_eventide_injection_enabled():
        return ""
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


def build_timestamp_context() -> str:
    """[0] 当前时间块。每轮请求实时生成，让模型对齐"现在"，
    取代此前用户在客户端提示词里手动维护的时间。"""
    if not app_settings.is_timestamp_injection_enabled():
        return ""
    now = datetime.now(_CST)
    weekday = _WEEKDAY_LABELS[now.weekday()]
    return f"[当前时间] {now.strftime('%Y-%m-%d %H:%M')} 星期{weekday}"


def build_recent_chat_context(limit: int = 10) -> str:
    """[4] 从 chat_messages 拉最近 N 条对话作为短期上下文。

    N 条只看数据库里最近的 N 行（user/assistant 各算一行），与客户端
    本次请求携带多少条历史完全无关，也不做去重：这样无论客户端怎么
    裁剪 history，模型看到的近期背景都是稳定的。
    """
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

def build_context(user_message: str = "", history_turns=None) -> str:
    """并发拼装完整上下文注入内容。

    Args:
        user_message: 用户最新一条消息（用于记忆搜索 query）。
                      为空时跳过记忆搜索。
        history_turns: 最近几轮普通 user/assistant 对话 [(role, text)]，
                       仅扩展向量召回的 embedding 输入；为空时向量通道
                       只用当前消息。
    """
    futures = {
        "persona": _executor.submit(load_persona),
    }
    results: dict[str, str] = {}

    # 时间戳是纯内存计算，不值得占用 executor 线程。
    results["timestamp"] = build_timestamp_context()

    # 开关与条数都在运行 build_context 的线程里读一次：落到内层 executor
    # 任务里再读会绕开 fail-open 缓存的预期时序，也让"读几次库"变得
    # 不可推理。近期对话开关关闭时干脆不提交任务：连 db 读取都不会发生。
    if app_settings.is_recent_chat_injection_enabled():
        futures["recent_chat"] = _executor.submit(
            build_recent_chat_context,
            app_settings.get_recent_chat_injection_limit(),
        )
    else:
        results["recent_chat"] = ""

    # 开关关闭时干脆不提交 eventide 任务：连 db 读取都不会发生。
    if app_settings.is_eventide_injection_enabled():
        futures["eventide"] = _executor.submit(build_eventide_context)
    else:
        results["eventide"] = ""

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
            memories = loop.run_until_complete(
                search_memories(user_message, top_k=8, history_turns=history_turns)
            )
            loop.close()
            memories_text = format_memories_for_injection(memories)
        except Exception as e:
            log.warning(f"记忆搜索失败: {e}")

    # 按固定顺序拼装：时间 → 人设 → 身体 → 记忆 → 近期对话。
    parts = []

    if results["timestamp"]:
        parts.append(results["timestamp"])

    if results["persona"]:
        parts.append(results["persona"])

    if results["eventide"]:
        parts.append(results["eventide"])

    if memories_text:
        parts.append(memories_text)

    if results["recent_chat"]:
        parts.append(results["recent_chat"])

    return "\n\n".join(parts)
