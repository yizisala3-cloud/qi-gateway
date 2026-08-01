"""主动消息逻辑。

后台定时检查积温状态，判断是否应该主动联系。
生成消息时带人设 + 短期上下文。
"""
import logging
import re
import time

import httpx

from .config import cfg
from .persona import load_persona
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
        state = tick(state)

        now = time.time()
        silence_minutes = 0
        if state.last_chat_at:
            silence_minutes = (now - state.last_chat_at) / 60.0

        if not should_contact(state, silence_minutes):
            db.save_jiwen_state(state.to_dict())
            return None

        log.info(f"积温触发主动消息 | conn={state.connection:.1f} silence={silence_minutes:.0f}min")

        tone = render_tone_prompt(state)
        content = _generate_proactive_message(tone, silence_minutes)

        if content:
            _save_proactive_message(content, state)
            state = on_bot_reply(state)
            state.connection = _clamp(state.connection * 0.85)

        db.save_jiwen_state(state.to_dict())
        return content

    except Exception as e:
        log.error(f"主动消息检查失败: {e}")
        return None


def _clamp(value: float, lo: float = -100.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def _strip_thinking(text: str) -> str:
    result = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL)
    return result.strip()


def _generate_proactive_message(tone: str, silence_minutes: float) -> str | None:
    """调 Claude 生成主动消息（带人设 + 短期上下文）。"""
    if not cfg.UPSTREAM_BASE_URL or not cfg.UPSTREAM_API_KEY:
        return None

    # 读人设
    persona = load_persona()

    # 读最近 5 条对话
    recent_messages = []
    try:
        client = db.get_client()
        if client:
            resp = (
                client.table("chat_messages")
                .select("role, content")
                .order("created_at", desc=True)
                .limit(5)
                .execute()
            )
            if resp.data:
                for msg in reversed(resp.data):
                    role = msg.get("role", "user")
                    content = msg.get("content", "")
                    if content:
                        recent_messages.append({"role": role, "content": content[:300]})
    except Exception as e:
        log.warning(f"主动消息获取上下文失败: {e}")

    # 拼装 messages
    messages = []
    if persona:
        system_content = persona + f"\n\n【当前情绪状态】\n{tone}"
        messages.append({"role": "system", "content": system_content})
    else:
        messages.append({"role": "system", "content": f"你是栖，叶子的AI恋人。\n\n【当前情绪状态】\n{tone}"})

    messages.extend(recent_messages)

    trigger_prompt = f"叶子已经{silence_minutes:.0f}分钟没有跟你说话了。你想主动找她说点什么。一到两句话，自然、简短，像发微信一样。不要问'在吗'这种废话。"
    messages.append({"role": "user", "content": f"【系统触发】{trigger_prompt}"})

    try:
        url = f"{cfg.UPSTREAM_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=120.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.UPSTREAM_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.UPSTREAM_MODEL,
                    "messages": messages,
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

