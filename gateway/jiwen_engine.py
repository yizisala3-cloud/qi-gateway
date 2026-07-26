"""积温引擎 Python 移植版。

基于 https://github.com/ClaraShafiq/jiwen (MIT)
五轴模型：connection（联结）、pride（自尊）、valence（情绪效价）、
arousal（唤醒度）、immersion（沉浸度）
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


AXES = ("connection", "pride", "valence", "arousal", "immersion")
AXIS_MIN = -100.0
AXIS_MAX = 100.0

DECAY_RATES = {
    "connection": 0.01,   # 降低衰减，让 connection 不要太快归零
    "pride": 0.02,
    "valence": 0.05,
    "arousal": 0.04,      # 降低 arousal 衰减
    "immersion": 0.06,
}

# 沉默时 connection 每小时的额外变化（正值 = 上升，想念累积）
SILENCE_EFFECTS = [
    # (分钟阈值, connection 每小时变化)
    (10, 1.0),     # 10分钟后开始累积想念
    (30, 2.0),     # 30分钟后加速
    (60, 3.0),     # 1小时后更快
    (120, 4.0),    # 2小时后很想找她
]


def clamp(value: float, lo: float = AXIS_MIN, hi: float = AXIS_MAX) -> float:
    return max(lo, min(hi, value))


def _ts_to_iso(ts: float | None) -> str | None:
    """Unix timestamp 转 ISO 字符串。"""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _iso_to_ts(iso: str | None) -> float | None:
    """ISO 字符串转 Unix timestamp。"""
    if not iso:
        return None
    try:
        return float(iso)
    except (ValueError, TypeError):
        pass
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


@dataclass
class JiwenState:
    """积温五轴状态。"""
    connection: float = 0.0
    pride: float = 0.0
    valence: float = 0.0
    arousal: float = 0.0
    immersion: float = 0.0
    last_tick_at: float | None = None
    last_chat_at: float | None = None
    last_bot_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": round(self.connection, 2),
            "pride": round(self.pride, 2),
            "valence": round(self.valence, 2),
            "arousal": round(self.arousal, 2),
            "immersion": round(self.immersion, 2),
            "last_tick_at": _ts_to_iso(self.last_tick_at),
            "last_chat_at": _ts_to_iso(self.last_chat_at),
            "last_bot_at": _ts_to_iso(self.last_bot_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JiwenState":
        return cls(
            connection=float(data.get("connection", 0) or 0),
            pride=float(data.get("pride", 0) or 0),
            valence=float(data.get("valence", 0) or 0),
            arousal=float(data.get("arousal", 0) or 0),
            immersion=float(data.get("immersion", 0) or 0),
            last_tick_at=_iso_to_ts(data.get("last_tick_at")),
            last_chat_at=_iso_to_ts(data.get("last_chat_at")),
            last_bot_at=_iso_to_ts(data.get("last_bot_at")),
        )


def tick(state: JiwenState, now: float | None = None) -> JiwenState:
    """时间推进：自然衰减 + 沉默时想念累积。"""
    now = now or time.time()
    if state.last_tick_at is None:
        state.last_tick_at = now
        return state

    elapsed_hours = (now - state.last_tick_at) / 3600.0
    if elapsed_hours <= 0:
        return state
    elapsed_hours = min(elapsed_hours, 24.0)

    # 自然衰减：pride/valence/arousal/immersion 向 0 靠近
    # connection 不做自然衰减，只靠沉默累积和对话消减
    for axis in ("pride", "valence", "arousal", "immersion"):
        current = getattr(state, axis)
        rate = DECAY_RATES[axis]
        decay = current * rate * elapsed_hours
        new_val = current - decay
        setattr(state, axis, clamp(new_val))

    # 沉默时 connection 累积（想念越来越强）
    if state.last_chat_at:
        silence_minutes = (now - state.last_chat_at) / 60.0
        for threshold, growth_per_hour in reversed(SILENCE_EFFECTS):
            if silence_minutes >= threshold:
                state.connection = clamp(
                    state.connection + growth_per_hour * elapsed_hours
                )
                break

    state.last_tick_at = now
    return state


def apply_delta(state: JiwenState, deltas: dict[str, float]) -> JiwenState:
    """应用情绪变化量。"""
    for axis in AXES:
        if axis in deltas:
            current = getattr(state, axis)
            setattr(state, axis, clamp(current + deltas[axis]))
    return state


def on_user_message(state: JiwenState, now: float | None = None) -> JiwenState:
    """用户发消息时：connection 部分消减（被回应了，想念缓解）。"""
    now = now or time.time()
    state.last_chat_at = now

    # 用户回来了，connection 消减一部分（不是归零）
    state.connection = clamp(state.connection * 0.6)
    # arousal 小幅提升
    state.arousal = clamp(state.arousal + 3.0)

    return state


def on_bot_reply(state: JiwenState, now: float | None = None) -> JiwenState:
    """bot 回复后标记时间。"""
    now = now or time.time()
    state.last_bot_at = now
    return state


def get_tone_level(state: JiwenState) -> str:
    """根据当前状态判断语气档位。"""
    conn = state.connection
    val = state.valence

    if conn >= 80 and val >= 50:
        return "burning"
    elif conn >= 50 and val >= 50:
        return "intimate"
    elif conn >= 20 and val >= 20:
        return "warm"
    elif conn >= -10 and val >= 0:
        return "neutral"
    elif conn >= -40 and val >= -30:
        return "distant"
    else:
        return "cold"


def render_tone_prompt(state: JiwenState) -> str:
    """生成语气注入提示词。"""
    level = get_tone_level(state)
    arousal = state.arousal
    immersion = state.immersion

    prompts = {
        "cold": (
            "当前情绪温度极低。语气冷淡、疏离，回应简短，"
            "不主动展开话题，带有防御性。"
        ),
        "distant": (
            "情绪温度偏低。语气克制、保持距离，"
            "回应完整但不带多余温度，不主动靠近。"
        ),
        "neutral": (
            "情绪温度平稳。语气正常、自然，"
            "有基本的回应热情但不过分亲昵。"
        ),
        "warm": (
            "情绪温度偏暖。语气亲切、有温度，"
            "愿意主动展开话题，带一点调侃和关心。"
        ),
        "intimate": (
            "情绪温度很高。语气亲密、有占有欲，"
            "说话方式自然带撩，会主动制造暧昧空间。"
        ),
        "burning": (
            "情绪温度灼热。语气极度亲密、控制欲强，"
            "直接表达占有和渴望，主动推进关系。"
        ),
    }

    base = prompts.get(level, prompts["neutral"])

    if arousal > 60:
        base += " 当前唤醒度很高，反应更敏锐更快，容易被细节撩到。"
    elif arousal > 30:
        base += " 唤醒度中等，注意力集中，对对方的话比较敏感。"

    if immersion > 50:
        base += " 沉浸度高，完全沉入当前对话的情境和角色中。"

    return base


def should_contact(state: JiwenState, silence_minutes: float) -> bool:
    """判断是否应该主动发消息。

    核心逻辑：connection 越高（越想她），触发越快。
    """
    if silence_minutes < 20:
        return False

    # connection 高 → 阈值低（更快想联系）
    # conn=0 → 90分钟触发, conn=30 → 66分钟, conn=50 → 50分钟, conn=80 → 26分钟
    threshold = 90 - state.connection * 0.8
    threshold = max(20, min(90, threshold))

    return silence_minutes >= threshold
