"""每日记忆总结提取模块。

触发时机：每天凌晨3点 或 连续沉默6小时以上。
从当天 chat_messages 中提取值得长期记住的信息，存入 memories 表。
同时做矛盾检测：新记忆与旧记忆冲突时，旧记忆标记失效。
"""
import json
import logging
import re
import time
from datetime import datetime, timezone, timedelta

import httpx

from .config import cfg
from .db import get_client, safe_query

log = logging.getLogger("gateway.memory_extract")


# ── 提取 prompt ──────────────────────────────────

EXTRACT_PROMPT = """从以下对话中提取值得长期记住的信息。

规则：
- 只提取对话中**明确出现**的内容，禁止推测或脑补
- 只提取有长期价值的信息：偏好变化、重要事件、关系变化、承诺、新习惯
- 日常寒暄、情绪表达、重复已知信息不提取
- 如果没有值得记住的内容，返回空数组 []
- 每条记忆用一两句话概括
- importance 范围 1-10（1=琐事，5=普通偏好，8=重要事件，10=核心关系变化）
- emotion_weight 范围 0-1（0=纯事实，0.5=有情感色彩，1=强烈情绪事件）
- tags 给 3-5 个关键词

返回 JSON 数组，每个元素格式：
{"content": "记忆内容", "title": "一句话摘要", "importance": 5, "emotion_weight": 0.5, "tags": ["标签1", "标签2"]}

对话内容：
{conversation}"""


# ── 矛盾检测 prompt ──────────────────────────────

CONFLICT_PROMPT = """判断新记忆是否与旧记忆矛盾（描述同一件事但信息更新了）。

旧记忆：{old_content}
新记忆：{new_content}

如果新记忆是旧记忆的更新版本（比如偏好改变、状态更新、事件进展），回答 YES。
如果两者描述不同的事，或者是补充关系不是替代关系，回答 NO。

只回答 YES 或 NO，不要解释。"""


# ── 核心：每日总结 ────────────────────────────────

def run_daily_digest():
    """执行每日总结：拉当天对话 → 提取记忆 → 矛盾检测 → 存入。"""
    log.info("开始每日记忆总结")

    # 1. 拉当天的 chat_messages
    messages = _fetch_today_messages()
    if not messages or len(messages) < 4:
        log.info("当天对话不足，跳过总结")
        return

    # 2. 拼接对话文本
    conversation = _format_conversation(messages)

    # 3. 调模型提取记忆
    new_memories = _extract_memories(conversation)
    if not new_memories:
        log.info("未提取到新记忆")
        return

    log.info(f"提取到 {len(new_memories)} 条新记忆")

    # 4. 逐条处理：矛盾检测 + 生成 embedding + 存入
    for mem in new_memories:
        try:
            _process_single_memory(mem)
        except Exception as e:
            log.error(f"处理记忆失败: {e} | {mem.get('title', '')}")

    log.info("每日总结完成")


def _fetch_today_messages() -> list[dict]:
    """拉取最近 24 小时的 chat_messages。"""
    client = get_client()
    if not client:
        return []
    try:
        # 东八区今天零点
        cst = timezone(timedelta(hours=8))
        today_start = datetime.now(cst).replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_utc = today_start.astimezone(timezone.utc).isoformat()

        resp = (
            client.table("chat_messages")
            .select("role, content, created_at")
            .gte("created_at", today_start_utc)
            .order("created_at")
            .limit(200)
            .execute()
        )
        return resp.data if resp.data else []
    except Exception as e:
        log.error(f"拉取当天消息失败: {e}")
        return []


def _format_conversation(messages: list[dict]) -> str:
    """格式化对话文本供提取模型使用。"""
    lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if content:
            prefix = "叶子" if role == "user" else "栖"
            lines.append(f"{prefix}: {content[:500]}")
    return "\n".join(lines)


def _extract_memories(conversation: str) -> list[dict]:
    """调硅基流动 Qwen 从对话中提取记忆。"""
    if not cfg.ANALYSIS_API_KEY:
        return []

    prompt = EXTRACT_PROMPT.format(conversation=conversation[:8000])

    try:
        url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=60.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.ANALYSIS_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2000,
                    "temperature": 0.2,
                },
            )
            if resp.status_code != 200:
                log.error(f"记忆提取模型返回 {resp.status_code}")
                return []

            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return _parse_extraction_result(content)

    except Exception as e:
        log.error(f"记忆提取调用失败: {e}")
        return []


def _parse_extraction_result(text: str) -> list[dict]:
    """解析提取模型返回的 JSON 数组。"""
    if not text:
        return []

    text = text.strip()

    # 直接解析
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # 提取 ```json ... ```
    code_block = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', text, re.DOTALL)
    if code_block:
        try:
            result = json.loads(code_block.group(1))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    # 找 [...] 结构
    array_match = re.search(r'\[.*\]', text, re.DOTALL)
    if array_match:
        try:
            result = json.loads(array_match.group(0))
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return []


def _process_single_memory(mem: dict):
    """处理单条提取出的记忆：矛盾检测 + embedding + 存入。"""
    content = mem.get("content", "").strip()
    if not content or len(content) < 5:
        return

    title = mem.get("title", content[:50])
    importance = max(1, min(10, int(mem.get("importance", 5))))
    emotion_weight = max(0.0, min(1.0, float(mem.get("emotion_weight", 0.5))))
    tags = mem.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",")]

    # 生成 embedding
    embedding = _get_embedding_sync(content)

    # 矛盾检测（如果有 embedding）
    if embedding:
        _check_and_resolve_conflicts(content, tags, embedding)

    # 存入 memories 表
    client = get_client()
    if not client:
        return

    insert_data = {
        "content": content,
        "title": title,
        "tags": tags,
        "heat": importance * 10.0,
        "importance": importance,
        "layer": "碎片",
        "source": "daily_digest",
        "verified": "pending",
        "is_active": True,
        "emotion_weight": emotion_weight,
        "recall_count": 0,
    }
    if embedding:
        insert_data["embedding"] = embedding

    client.table("memories").insert(insert_data).execute()
    log.debug(f"记忆已存入: {title}")


def _get_embedding_sync(text: str) -> list[float] | None:
    """同步获取 embedding（供后台任务使用）。"""
    if not cfg.ANALYSIS_API_KEY:
        return None

    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/embeddings"
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                url,
                headers={"Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}"},
                json={
                    "model": "Pro/Qwen/Qwen3-Embedding-0.6B",
                    "input": text[:2000],
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                return data["data"][0]["embedding"]
    except Exception as e:
        log.warning(f"Embedding 获取失败: {e}")
    return None


def _check_and_resolve_conflicts(new_content: str, new_tags: list[str], new_embedding: list[float]):
    """矛盾检测：新记忆是否取代了旧记忆。"""
    client = get_client()
    if not client:
        return

    try:
        # 用向量搜索找相似旧记忆
        resp = client.rpc("match_memories", {
            "query_embedding": new_embedding,
            "match_threshold": 0.85,
            "match_count": 3,
        }).execute()

        if not resp.data:
            return

        for old_mem in resp.data:
            old_tags = old_mem.get("tags", [])
            # tags 交集 >= 2
            if isinstance(old_tags, list) and isinstance(new_tags, list):
                overlap = set(old_tags) & set(new_tags)
                if len(overlap) < 2:
                    continue

            # 调模型判断是否矛盾
            is_conflict = _judge_conflict(old_mem["content"], new_content)
            if is_conflict:
                # 旧记忆失效
                client.table("memories").update({
                    "is_active": False
                }).eq("id", old_mem["id"]).execute()
                log.info(f"矛盾覆盖: 旧记忆 id={old_mem['id']} 被新记忆取代")

    except Exception as e:
        log.warning(f"矛盾检测失败: {e}")


def _judge_conflict(old_content: str, new_content: str) -> bool:
    """调模型判断新旧记忆是否矛盾。"""
    if not cfg.ANALYSIS_API_KEY:
        return False

    prompt = CONFLICT_PROMPT.format(old_content=old_content[:300], new_content=new_content[:300])

    try:
        url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/chat/completions"
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                url,
                headers={
                    "Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": cfg.ANALYSIS_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 10,
                    "temperature": 0.1,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                answer = data.get("choices", [{}])[0].get("message", {}).get("content", "").strip().upper()
                return "YES" in answer
    except Exception as e:
        log.warning(f"矛盾判断调用失败: {e}")
    return False
