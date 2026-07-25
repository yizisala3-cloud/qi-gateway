"""积温引擎 Python 移植版。

基于 https://github.com/ClaraShafiq/jiwen (MIT)
五轴模型：connection（联结）、pride（自尊）、valence（情绪效价）、
arousal（唤醒度）、immersion（沉浸度）

每轴范围 -100 ~ +100，tick 时自然衰减，聊天时根据情绪 delta 更新。
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any


# ── 常量 ─────────────────────────────────────────────────
AXES = ("connection", "pride", "valence", "arousal", "immersion")
AXIS_MIN = -100.0
AXIS_MAX = 100.0

# 每小时自然衰减率（乘以当前值，向 0 靠近）
DECAY_RATES = {
    "connection": 0.03,
    "pride": 0.02,
    "valence": 0.05,
    "arousal": 0.08,
    "immersion": 0.06,
}

# 沉默时间对 connection 的额外影响
SILENCE_THRESHOLDS = [
    # (分钟阈值, connection 每小时变化)
    (30, -0.5),
    (60, -1.2),
    (120, -2.0),
    (240, -3.5),
]

# 语气档位阈值
TONE_LEVELS = {
    "cold": {"connection": (-100, -40), "valence": (-100, -30)},
    "distant": {"connection": (-40, -10), "valence": (-30, 0)},
    "neutral": {"connection": (-10, 20), "valence": (0, 20)},
    "warm": {"connection": (20, 50), "valence": (20, 50)},
    "intimate": {"connection": (50, 80), "valence": (50, 80)},
    "burning": {"connection": (80, 100), "valence": (50, 100)},
}


def clamp(value: float, lo: float = AXIS_MIN, hi: float = AXIS_MAX) -> float:
    return max(lo, min(hi, value))


@dataclass
class JiwenState:
    """积温五轴状态。"""
    connection: float = 0.0
    pride: float = 0.0
    valence: float = 0.0
    arousal: float = 0.0
    immersion: float = 0.0
    last_tick_at: float | None = None  # unix timestamp
    last_chat_at: float | None = None  # 最后一次用户消息时间
    last_bot_at: float | None = None   # 最后一次 bot 回复时间

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": round(self.connection, 2),
            "pride": round(self.pride, 2),
            "valence": round(self.valence, 2),
            "arousal": round(self.arousal, 2),
            "immersion": round(self.immersion, 2),
            "last_tick_at": self.last_tick_at,
            "last_chat_at": self.last_chat_at,
            "last_bot_at": self.last_bot_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JiwenState":
        return cls(
            connection=float(data.get("connection", 0)),
            pride=float(data.get("pride", 0)),
            valence=float(data.get("valence", 0)),
            arousal=float(data.get("arousal", 0)),
            immersion=float(data.get("immersion", 0)),
            last_tick_at=data.get("last_tick_at"),
            last_chat_at=data.get("last_chat_at"),
            last_bot_at=data.get("last_bot_at"),
        )


def tick(state: JiwenState, now: float | None = None) -> JiwenState:
    """时间推进：自然衰减 + 沉默惩罚。"""
    now = now or time.time()
    if state.last_tick_at is None:
        state.last_tick_at = now
        return state

    elapsed_hours = (now - state.last_tick_at) / 3600.0
    if elapsed_hours <= 0:
        return state
    # 限制单次最大推进 24 小时
    elapsed_hours = min(elapsed_hours, 24.0)

    # 自然衰减：每轴向 0 靠近
    for axis in AXES:
        current = getattr(state, axis)
        rate = DECAY_RATES[axis]
        decay = current * rate * elapsed_hours
        new_val = current - decay
        setattr(state, axis, clamp(new_val))

    # 沉默惩罚：对方久未回复时 connection 下降
    if state.last_chat_at:
        silence_minutes = (now - state.last_chat_at) / 60.0
        for threshold, delta_per_hour in reversed(SILENCE_THRESHOLDS):
            if silence_minutes >= threshold:
                state.connection = clamp(
                    state.connection + delta_per_hour * elapsed_hours
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
    """用户发消息时的基础 connection 回升。"""
    now = now or time.time()
    state.last_chat_at = now

    # 用户主动说话 → connection 小幅回升
    silence = 0
    if state.last_bot_at:
        silence = (now - state.last_bot_at) / 60.0

    # 沉默越久，回来时 connection 回升越多（但有上限）
    boost = min(3.0 + silence * 0.02, 8.0)
    state.connection = clamp(state.connection + boost)
    # arousal 小幅提升
    state.arousal = clamp(state.arousal + 2.0)

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
    conn = state.connection
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

    # arousal 修饰
    if arousal > 60:
        base += " 当前唤醒度很高，反应更敏锐更快，容易被细节撩到。"
    elif arousal > 30:
        base += " 唤醒度中等，注意力集中，对对方的话比较敏感。"

    # immersion 修饰
    if immersion > 50:
        base += " 沉浸度高，完全沉入当前对话的情境和角色中。"

    return base


def should_contact(state: JiwenState, silence_minutes: float) -> bool:
    """判断是否应该主动发消息。

    积温引擎的主动意识：当 connection 足够高且沉默够久时，
    产生主动联系的冲动。
    """
    if silence_minutes < 30:
        return False

    # connection 越高，越容易想主动联系
    threshold = 120 - state.connection * 0.8  # conn=50 → 80分钟, conn=80 → 56分钟
    threshold = max(30, threshold)

    if silence_minutes >= threshold:
        # arousal 高时更急切
        if state.arousal > 40:
            return True
        # 否则需要更长时间
        return silence_minutes >= threshold * 1.3

    return False
