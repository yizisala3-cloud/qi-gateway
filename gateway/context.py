"""上下文拼装：并发获取积温 + Eventide 状态，注入到 system prompt。"""
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from .jiwen_engine import JiwenState, tick, render_tone_prompt, on_user_message, on_bot_reply
from . import db
from . import eventide_bridge

log = logging.getLogger("gateway.context")

_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="ctx")


def _load_jiwen() -> JiwenState:
    """从数据库加载积温状态。"""
    raw = db.load_jiwen_state()
    if not raw:
        return JiwenState()
    return JiwenState.from_dict(raw)


def build_jiwen_context() -> str:
    """读取积温状态 -> tick -> 生成语气提示词。"""
    try:
        state = _load_jiwen()
        state = tick(state)
        tone = render_tone_prompt(state)
        db.save_jiwen_state(state.to_dict())
        return tone
    except Exception as e:
        log.error(f"积温 context 构建失败: {e}")
        return ""


def build_eventide_context() -> str:
    """读取 Eventide 状态 -> advance -> 渲染状态卡。"""
    try:
        state_data = db.load_eventide_state()

        if not state_data:
            state_data = eventide_bridge.create_initial_state()
            if not state_data:
                return ""
            db.save_eventide_state(state_data)

        jiwen_raw = db.load_jiwen_state()
        last_msg_at = None
        if jiwen_raw and jiwen_raw.get("last_chat_at"):
            try:
                from .jiwen_engine import _iso_to_ts
                ts = _iso_to_ts(jiwen_raw["last_chat_at"])
                if ts:
                    last_msg_at = datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, TypeError):
                pass

        new_data, card = eventide_bridge.advance_and_render(
            state_data,
            last_counterpart_message_at=last_msg_at,
        )

        if new_data:
            db.save_eventide_state(new_data)

        return card or ""

    except Exception as e:
        log.error(f"Eventide context 构建失败: {e}")
        return ""


def build_context() -> str:
    """并发拼装完整上下文注入内容。"""
    futures = {
        "jiwen": _executor.submit(build_jiwen_context),
        "eventide": _executor.submit(build_eventide_context),
    }

    parts = []
    for name, future in futures.items():
        try:
            result = future.result(timeout=8.0)
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
        state = _load_jiwen()
        state = on_user_message(state)
        db.save_jiwen_state(state.to_dict())
    except Exception as e:
        log.error(f"积温用户消息更新失败: {e}")


def update_jiwen_on_bot_reply():
    """bot 回复后更新积温状态。"""
    try:
        state = _load_jiwen()
        state = on_bot_reply(state)
        db.save_jiwen_state(state.to_dict())
    except Exception as e:
        log.error(f"积温bot回复更新失败: {e}")
