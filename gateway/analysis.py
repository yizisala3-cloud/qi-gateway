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


def _extract_text_from_response(data: dict) -> str:
    """从各种格式的 API 响应中提取文本内容。

    处理：
    - 标准 content 字符串
    - content 为 None + thinking 字段
    - content 为 list（多模态内容块）
    - Claude 格式的 content blocks
    """
    choices = data.get("choices", [])
    if not choices:
        return ""

    message = choices[0].get("message", {})

    # 1. 标准：content 是字符串
    content = message.get("content")
    if isinstance(content, str) and content.strip():
        return content.strip()

    # 2. content 是 list（Claude/多模态格式）
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif block.get("type") == "thinking":
                    # 跳过 thinking 块
                    continue
        if texts:
            return "\n".join(texts).strip()

    # 3. 某些中转站把 thinking 和 content 分开
    # content 可能是 None，实际文本在别处
    # 尝试从 raw response 的其他位置找
    if content is None:
        # 某些格式把文本放在 message.content 外面
        # 或者整个回复只有 thinking 没有实际输出
        pass

    return ""


def _extract_json(text: str) -> dict | None:
    """从模型返回的文本中健壮地提取 JSON。"""
    if not text:
        return None

    text = text.strip()

    # 1. 直接解析
    try:
        result = json.loads(text)
        if isinstance(result, dict):
            return result
    except json.JSONDecodeError:
        pass

    # 2. 提取 ```json ... ``` 代码块
    code_block = re.search(r'```(?:json)?\s*(\{[^`]*\})\s*```', text, re.DOTALL)
    if code_block:
        try:
            result = json.loads(code_block.group(1))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # 3. 找包含 connection 的 {...}
    json_match = re.search(r'\{[^{}]*"connection"[^{}]*\}', text)
    if json_match:
        try:
            result = json.loads(json_match.group(0))
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError:
            pass

    # 4. 找任何 {...}
    brace_match = re.search(r'\{[^{}]+\}', text)
    if brace_match:
        try:
            result = json.loads(brace_match.group(0))
            if isinstance(result, dict):
                return result
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

            # 健壮提取文本
            content = _extract_text_from_response(data)
            if not content:
                log.warning(f"情绪分析模型返回空内容: {json.dumps(data.get('choices', [{}])[0].get('message', {}), ensure_ascii=False)[:300]}")
                return None

            # 健壮解析 JSON
            result = _extract_json(content)
            if not result:
                log.warning(f"情绪分析返回无法解析: {content[:200]}")
                return None

            # 验证字段存在且类型正确
            if not isinstance(result, dict):
                log.warning(f"情绪分析解析结果非 dict: {type(result)}")
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

            if valid_deltas:
                return valid_deltas

            log.warning(f"情绪分析无有效 delta: {result}")
            return None

    except Exception as e:
        log.error(f"情绪分析调用失败: {e}")
        return None
