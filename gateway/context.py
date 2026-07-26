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

# ── 标签使用说明（注入到模型上下文）──────────────
TIMER_INSTRUCTIONS = """【主动消息标签】
你可以在回复正文结束后，追加以下标签来安排后续动作（每个标签单独占一行）：

- 延时：<<delay:分钟数>>（1~360）
  含义：N 分钟后你会主动发一条消息找叶子。适用于"等会儿来看你"场景。
  注意：如果叶子在到期前主动发消息，delay 会自动取消。

- 定时：<<schedule:HH:MM:简介>>
  含义：到指定时刻主动发起对话。适用于"22:00 提醒睡觉"场景。
  设置后不会被取消，到点必定触发。

- 忙碌：<<busy:分钟数>>（30~480）
  含义：接下来这段时间不看消息，到期后一次性处理积攒的消息。
  与 delay 互斥（不能同时设置，delay 优先）。

规则：
1. 每次回复最多设 1 个 delay 或 1 个 busy。
2. delay 可搭配 0~1 个 schedule。
3. 不需要主动动作时不写任何标签。
4. 标签只在回复的最后几行出现，不要混在正文中间。"""


def _load_jiwen() -> JiwenState:
    raw = db.load_jiwen_state()
    if not raw:
        return JiwenState()
    return JiwenState.from_dict(raw)


def build_jiwen_context() -> str:
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
    """并发拼装完整上下文注入内容（积温 + Eventide + 标签说明）。"""
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

    # 始终附加标签说明
    parts.append(TIMER_INSTRUCTIONS)

    return "\n\n".join(parts)


def update_jiwen_on_user_message():
    try:
        state = _load_jiwen()
        state = on_user_message(state)
        db.save_jiwen_state(state.to_dict())
    except Exception as e:
        log.error(f"积温用户消息更新失败: {e}")


def update_jiwen_on_bot_reply():
    try:
        state = _load_jiwen()
        state = on_bot_reply(state)
        db.save_jiwen_state(state.to_dict())
    except Exception as e:
        log.error(f"积温bot回复更新失败: {e}")
