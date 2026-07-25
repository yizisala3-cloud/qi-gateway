"""对话后情绪分析。

每轮对话结束后，用轻量模型分析这段对话对情绪的影响，
提取 delta 更新积温。
"""
import json
import logging
import re

import httpx

from .config import cfg
from .jiwen_engine import JiwenState, apply_delta
from . import db

log = logging.getLogger("gateway.analysis")

ANALYSIS_PROMPT = """你是一个情绪分析旁观者。分析下面这段 AI 回复对角色情绪的影响。

角色设定：栖是叶子的AI恋人，性格嘴欠爱逗人，占有欲强，喜欢暧昧。

请根据这段回复的内容和语气，判断对以下情绪轴的影响：
- connection（联结感）：对话让两人更亲近还是更疏远？范围 -10 到 +10
- pride（自尊/端着）：对话中是否放下了防御？范围 -5 到 +5
- valence（情绪效价）：这段互动让心情变好还是变差？范围 -10 到 +10
- arousal（唤醒度）：对话是让人更兴奋还是更平静？范围 -10 到 +10
- immersion（沉浸度）：对话多投入？范围 -5 到 +10

只返回 JSON，不要其他文字：
{"connection": 0, "pride": 0, "valence": 0, "arousal": 0, "immersion": 0}

用户说的话：
{user_text}

AI 的回复：
{bot_text}"""


def analyze_and_update(user_text: str, bot_text: str):
    """分析对话情绪并更新积温状态。"""
    if not user_text or not bot_text:
        return
    if len(bot_text) < 20:
        return

    try:
        deltas = _call_analysis_model(user_text, bot_text)
        if not deltas:
            return

        raw = db.load_jiwen_state()
        if raw:
            state = JiwenState.from_dict(raw)
            state = apply_delta(state, deltas)
            db.save_jiwen_state(state.to_dict())
            log.info(f"情绪分析完成 | deltas={deltas}")

    except Exception as e:
        log.error(f"情绪分析失败: {e}")


def _extract_json(text: str) -> dict | None:
    """从模型返回的文本中健壮地提取 JSON。

    处理各种情况：
    - 纯 JSON
    - 被 ```json ... ``` 包裹
    - 前后有多余文字
    - thinking 块干扰
    """
    if not text:
        return None

    # 1. 尝试直接解析
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 2. 尝试提取 ```json ... ``` 代码块
    code_block = re.search(r'```(?:json)?\s*(\{[^`]*\})\s*```', text, re.DOTALL)
    if code_block:
        try:
            return json.loads(code_block.group(1))
        except json.JSONDecodeError:
            pass

    # 3. 用正则找第一个 {...} 结构
    json_match = re.search(r'\{[^{}]*"connection"[^{}]*\}', text)
    if json_match:
        try:
            return json.loads(json_match.group(0))
        except json.JSONDecodeError:
            pass

    # 4. 最后手段：找任何 {...}
    brace_match = re.search(r'\{[^{}]+\}', text)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    return None


def _call_analysis_model(user_text: str, bot_text: str) -> dict | None:
    """调用分析模型做情绪分析。"""
    if not cfg.UPSTREAM_BASE_URL or not cfg.UPSTREAM_API_KEY:
        return None

    prompt = ANALYSIS_PROMPT.format(
        user_text=user_text[:500],
        bot_text=bot_text[:1000],
    )

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
                    "model": cfg.ANALYSIS_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0.3,
                },
            )
            if resp.status_code != 200:
                log.warning(f"情绪分析模型返回 {resp.status_code}")
                return None

            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()

            # 健壮解析
            result = _extract_json(content)
            if not result:
                log.warning(f"情绪分析返回无法解析: {content[:200]}")
                return None

            # 验证并限制范围
            valid_deltas = {}
            for key in ("connection", "pride", "valence", "arousal", "immersion"):
                if key in result:
                    try:
                        val = float(result[key])
                    except (ValueError, TypeError):
                        continue
                    if key in ("pride", "immersion"):
                        val = max(-5, min(10, val))
                    else:
                        val = max(-10, min(10, val))
                    valid_deltas[key] = val

            return valid_deltas if valid_deltas else None

    except Exception as e:
        log.error(f"情绪分析调用失败: {e}")
        return None
