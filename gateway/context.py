"""上下文拼装：并发获取所有注入源，按固定顺序拼装。

正常对话注入结构（静态 → 动态）：
[1] 人设 persona（静态）
[2] 积温语气指引
[3] Eventide 身体状态卡
[4] 标签定时器状态
[5] 长期记忆搜索结果
[6] 短期上下文 chat_messages 最近10条
[7] 标签使用说明

主动消息注入结构（瘦身版）：
[1] 人设 persona
[2] 积温语气指引（简短）
[3] 最近对话摘要（带时间戳，含AI自己发的）
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from .jiwen_engine import JiwenState, tick, render_tone_prompt, on_user_message, on_bot_reply
from .persona import load_persona
from .memory_search import search_memories, format_memories_for_injection
from . import db
from . import eventide_bridge

log = logging.getLogger("gateway.context")

_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="ctx")

# ── 主动消息检测关键词 ────────────────────────────
_PROACTIVE_KEYWORDS = [
    "主动消息上下文",
    "距离上次聊天",
    "请根据以上上下文决定是否发消息",
    "请根据以上用户动向决定是否发消息",
    "[PASS]",
]

# ── 标签使用说明（静态）──────────────────────────
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


def _is_proactive_message(user_message: str) -> bool:
    """检测是否为橘瓣内置主动消息触发的请求。"""
    if not user_message:
        return False
    return any(kw in user_message for kw in _PROACTIVE_KEYWORDS)


# ── 数据源构建函数 ────────────────────────────────

def _load_jiwen() -> JiwenState:
    raw = db.load_jiwen_state()
    if not raw:
        return JiwenState()
    return JiwenState.from_dict(raw)


def build_jiwen_context() -> str:
    """[2] 积温语气指引。"""
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
    """[3] Eventide 身体状态卡。"""
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


def build_recent_chat_context(limit: int = 10) -> str:
    """[6] 从 chat_messages 拉最近 N 条对话作为短期上下文（正常对话用）。"""
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


def build_proactive_chat_summary() -> str:
    """主动消息专用：带时间戳的最近对话摘要（含 AI 自己发的主动消息）。"""
    try:
        client = db.get_client()
        if not client:
            return ""

        # 拉最近 8 条消息（包含主动消息和被动消息）
        resp = (
            client.table("chat_messages")
            .select("role, content, created_at")
            .order("created_at", desc=True)
            .limit(8)
            .execute()
        )
        if not resp.data:
            return ""

        messages = list(reversed(resp.data))
        cst = timezone(timedelta(hours=8))
        now = datetime.now(cst)

        lines = ["【最近对话记录（含你之前主动发的消息）】"]
        last_user_time = None

        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            created_at = msg.get("created_at", "")

            if not content:
                continue

            # 解析时间
            try:
                if isinstance(created_at, str):
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(cst)
                else:
                    dt = created_at.astimezone(cst)
                time_str = dt.strftime("%H:%M")
            except Exception:
                time_str = "??:??"

            prefix = "叶子" if role == "user" else "栖"
            lines.append(f"- {time_str} {prefix}: {content[:150]}")

            if role == "user":
                last_user_time = dt

        # 计算叶子最后一次说话距今多久
        if last_user_time:
            silence = now - last_user_time
            silence_minutes = int(silence.total_seconds() / 60)
            if silence_minutes > 0:
                lines.append(f"\n（叶子最后一次说话距现在已过去 {silence_minutes} 分钟）")

        lines.append("\n注意：不要重复你上面已经说过的内容。如果已经发了多条叶子都没回，考虑是否还要继续打扰，或者换个完全不同的角度。")

        return "\n".join(lines)
    except Exception as e:
        log.error(f"主动消息对话摘要构建失败: {e}")
        return ""


# ── 主入口：完整上下文构建 ────────────────────────

def build_context(user_message: str = "") -> str:
    """并发拼装完整上下文注入内容。

    检测到是主动消息请求时，切换为瘦身注入模式。
    """
    is_proactive = _is_proactive_message(user_message)

    if is_proactive:
        return _build_proactive_context()
    else:
        return _build_normal_context(user_message)


def _build_proactive_context() -> str:
    """主动消息瘦身注入：人设 + 积温语气 + 带时间戳对话摘要。"""
    futures = {
        "persona": _executor.submit(load_persona),
        "jiwen": _executor.submit(build_jiwen_context),
        "summary": _executor.submit(build_proactive_chat_summary),
    }

    results = {}
    for name, future in futures.items():
        try:
            results[name] = future.result(timeout=8.0) or ""
        except Exception as e:
            log.warning(f"proactive context {name} 失败: {e}")
            results[name] = ""

    parts = []

    # 人设
    if results["persona"]:
        parts.append(results["persona"])

    # 积温语气（简短）
    if results["jiwen"]:
        parts.append(f"【当前情绪状态】\n{results['jiwen']}")

    # 带时间戳的对话摘要
    if results["summary"]:
        parts.append(results["summary"])

    log.info("主动消息瘦身注入 | 组件数=%d", len(parts))
    return "\n\n".join(parts)


def _build_normal_context(user_message: str) -> str:
    """正常对话完整注入。"""
    futures = {
        "persona": _executor.submit(load_persona),
        "jiwen": _executor.submit(build_jiwen_context),
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

    parts = []

    if results["persona"]:
        parts.append(results["persona"])

    if results["jiwen"]:
        parts.append(f"【当前情绪状态】\n{results['jiwen']}")

    if results["eventide"]:
        parts.append(results["eventide"])

    if memories_text:
        parts.append(memories_text)

    if results["recent_chat"]:
        parts.append(results["recent_chat"])

    parts.append(TIMER_INSTRUCTIONS)

    return "\n\n".join(parts)


# ── 积温更新（保留原接口）────────────────────────

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
