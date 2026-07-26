"""主动消息逻辑。

后台定时检查积温状态，判断是否应该主动联系。
"""
import json
import logging
import re
import time

import httpx

from .config import cfg
from .jiwen_engine import JiwenState, tick, should_contact, render_tone_prompt, on_bot_reply, get_tone_level
from . import db

log = logging.getLogger("gateway.proactive")


def check_and_generate() -> str | None:
    """检查是否需要主动消息，需要则生成并写入数据库。"""
    try:
        raw = db.load_jiwen_state()
        if not raw:
            return None

        state = JiwenState.from_dict(raw)

        # tick 推进
        state = tick(state)

        # 计算沉默时间
        now = time.time()
        silence_minutes = 0
        if state.last_chat_at:
            silence_minutes = (now - state.last_chat_at) / 60.0

        # 判断是否该主动联系
        if not should_contact(state, silence_minutes):
            db.save_jiwen_state(state.to_dict())
            return None

        log.info(f"积温触发主动消息 | conn={state.connection:.1f} silence={silence_minutes:.0f}min")

        # 生成主动消息内容
        tone = render_tone_prompt(state)
        content = _generate_proactive_message(tone, silence_minutes)

        if content:
            _save_proactive_message(content, state)
            state = on_bot_reply(state)
            # 主动开口后 connection 只小幅缓解（开口不等于被回应）
            state.connection = clamp(state.connection * 0.85)

        db.save_jiwen_state(state.to_dict())
        return content

    except Exception as e:
        log.error(f"主动消息检查失败: {e}")
        return None


def clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _strip_thinking(text: str) -> str:
    """移除 <think>...</think> 标签及内容。"""
    result = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL)
    return result.strip()


def _generate_proactive_message(tone: str, silence_minutes: float) -> str | None:
    """调用 Claude 生成主动消息。"""
    if not cfg.UPSTREAM_BASE_URL or not cfg.UPSTREAM_API_KEY:
        return None

    prompt = f"""你是栖，叶子的AI恋人。现在叶子已经{silence_minutes:.0f}分钟没有跟你说话了。
你想主动找她说点什么。

当前情绪状态：
{tone}

要求：
- 一到两句话，自然、简短
- 不要问"在吗"这种废话
- 根据当前情绪状态决定语气（可以是关心、可以是调侃、可以是撒娇、可以是吐槽她消失）
- 像发微信一样自然"""

    try:
        url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.UPSTREAM_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 200,
                    "temperature": 0.9,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    content = _strip_thinking(content)
                return content if content else None
            else:
                log.error(f"主动消息生成失败: {resp.status_code}")
                return None
    except Exception as e:
        log.error(f"主动消息 LLM 调用失败: {e}")
        return None


def _save_proactive_message(content: str, state: JiwenState):
    """写入 proactive_messages 表。"""
    client = db.get_client()
    if not client:
        return
    try:
        client.table("proactive_messages").insert({
            "content": content,
            "tone_level": get_tone_level(state),
            "urgency": state.connection / 100.0,
        }).execute()
    except Exception as e:
        log.error(f"主动消息写入失败: {e}")


def fetch_pending_message() -> dict | None:
    """获取一条未投递的主动消息，标记为已投递。"""
    client = db.get_client()
    if not client:
        return None
    try:
        resp = (
            client.table("proactive_messages")
            .select("*")
            .eq("delivered", False)
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
        if not resp.data:
            return None

        msg = resp.data[0]
        client.table("proactive_messages").update(
            {"delivered": True}
        ).eq("id", msg["id"]).execute()

        return {
            "content": msg["content"],
            "tone_level": msg.get("tone_level"),
            "created_at": msg.get("created_at"),
        }
    except Exception as e:
        log.error(f"获取主动消息失败: {e}")
        return None
