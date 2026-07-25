"""上下文拼装：并发获取积温状态，注入到 system prompt。

Phase 2: 积温语气注入
Phase 3+: Eventide 身体状态卡、记忆注入
"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from .jiwen_engine import JiwenState, tick, render_tone_prompt, on_user_message, on_bot_reply
from . import db

log = logging.getLogger("gateway.context")

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="ctx")


def build_jiwen_context() -> str:
    """读取积温状态 → tick → 生成语气提示词。

    任何一步失败返回空字符串，不阻断主流程。
    """
    try:
        raw = db.load_jiwen_state()
        if not raw:
            return ""

        state = JiwenState(
            connection=float(raw.get("connection", 0)),
            pride=float(raw.get("pride", 0)),
            valence=float(raw.get("valence", 0)),
            arousal=float(raw.get("arousal", 0)),
            immersion=float(raw.get("immersion", 0)),
            last_tick_at=raw.get("last_tick_at"),
            last_chat_at=raw.get("last_chat_at"),
            last_bot_at=raw.get("last_bot_at"),
        )

        # tick 推进
        state = tick(state)

        # 生成语气提示词
        tone = render_tone_prompt(state)

        # 保存更新后的状态
        db.save_jiwen_state(state.to_dict())

        return tone

    except Exception as e:
        log.error(f"积温 context 构建失败: {e}")
        return ""


def build_context() -> str:
    """并发拼装完整上下文注入内容。

    目前只有积温，后续加入 Eventide 和记忆。
    任何数据源失败返回空字符串，不影响整体。
    """
    futures = {
        "jiwen": _executor.submit(build_jiwen_context),
        # Phase 3: "eventide": _executor.submit(build_eventide_context),
        # Phase 4: "memory": _executor.submit(build_memory_context),
    }

    parts = []
    for name, future in futures.items():
        try:
            result = future.result(timeout=5.0)
            if result:
                parts.append(result)
        except Exception as e:
            log.warning(f"context 数据源 {name} 超时或失败: {e}")

    if not parts:
        return ""

    return "\n\n".join(parts)


def update_jiwen_on_user_message():
    """用户发消息时更新积温状态。"""
    try:
        raw = db.load_jiwen_state()
        if not raw:
            return
        state = JiwenState(
            connection=float(raw.get("connection", 0)),
            pride=float(raw.get("pride", 0)),
            valence=float(raw.get("valence", 0)),
            arousal=float(raw.get("arousal", 0)),
            immersion=float(raw.get("immersion", 0)),
            last_tick_at=raw.get("last_tick_at"),
            last_chat_at=raw.get("last_chat_at"),
            last_bot_at=raw.get("last_bot_at"),
        )
        state = on_user_message(state)
        db.save_jiwen_state(state.to_dict())
    except Exception as e:
        log.error(f"积温用户消息更新失败: {e}")


def update_jiwen_on_bot_reply():
    """bot 回复后更新积温状态。"""
    try:
        raw = db.load_jiwen_state()
        if not raw:
            return
        state = JiwenState(
            connection=float(raw.get("connection", 0)),
            pride=float(raw.get("pride", 0)),
            valence=float(raw.get("valence", 0)),
            arousal=float(raw.get("arousal", 0)),
            immersion=float(raw.get("immersion", 0)),
            last_tick_at=raw.get("last_tick_at"),
            last_chat_at=raw.get("last_chat_at"),
            last_bot_at=raw.get("last_bot_at"),
        )
        state = on_bot_reply(state)
        db.save_jiwen_state(state.to_dict())
    except Exception as e:
        log.error(f"积温bot回复更新失败: {e}")
