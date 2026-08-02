"""记忆搜索模块：关键词 + 向量双通道混合检索。"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

import httpx

from .config import cfg
from .db import get_client, safe_query

log = logging.getLogger("gateway.memory_search")

EMBEDDING_MODEL = "Pro/Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024
MAX_CANDIDATES = 50


async def _get_embedding(text: str) -> Optional[list[float]]:
    if not cfg.ANALYSIS_API_KEY:
        return None
    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}"}
    payload = {"model": EMBEDDING_MODEL, "input": text[:2000]}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception as exc:
        log.warning("Embedding 请求失败: %s", exc)
        return None


def _extract_keywords(text: str) -> list[str]:
    try:
        import jieba.analyse
        return jieba.analyse.extract_tags(text, topK=8)
    except ImportError:
        tokens = re.split(r'[\s,，。！？、；："（）\(\)\[\]【】]+', text)
        return [token for token in tokens if len(token) >= 2][:8]


@safe_query
def _keyword_search(keywords: list[str], limit: int = 20) -> list[dict]:
    client = get_client()
    if not client or not keywords:
        return []
    safe_keywords = [re.sub(r"[,%()]", " ", word).strip() for word in keywords[:5]]
    conditions = ",".join(f"content.ilike.%{word}%" for word in safe_keywords if word)
    if not conditions:
        return []
    resp = (
        client.table("memories")
        .select("id,content,title,tags,heat,importance,layer,created_at,last_recalled_at")
        .eq("is_active", True)
        .eq("verified", "verified")
        .or_(conditions)
        .order("heat", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data or []


@safe_query
def _vector_search_sync(embedding: list[float], limit: int = 20) -> list[dict]:
    client = get_client()
    if not client:
        return []
    resp = client.rpc("match_memories", {
        "query_embedding": embedding,
        "match_threshold": 0.5,
        "match_count": limit,
    }).execute()
    return resp.data or []


@safe_query
def _boost_heat(memory_ids: list[int]):
    client = get_client()
    if not client or not memory_ids:
        return
    now = datetime.now(timezone.utc).isoformat()
    for memory_id in memory_ids:
        client.rpc("boost_memory_heat", {
            "memory_id": memory_id,
            "boost_amount": 15,
            "recalled_at": now,
        }).execute()


def _clamp(value: object, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        return min(max(float(value), minimum), maximum)
    except (TypeError, ValueError):
        return minimum


def _keyword_relevance(memory: dict, keywords: list[str]) -> float:
    """Score actual keyword coverage instead of treating heat order as relevance."""
    normalized = [word.casefold().strip() for word in keywords[:5] if word.strip()]
    if not normalized:
        return 0.0

    content = str(memory.get("content") or "").casefold()
    title = str(memory.get("title") or "").casefold()
    tags = " ".join(str(tag) for tag in (memory.get("tags") or [])).casefold()
    matched = [word for word in normalized if word in content or word in title or word in tags]
    if not matched:
        return 0.0

    coverage = len(matched) / len(normalized)
    title_or_tag_bonus = 0.2 if any(word in title or word in tags for word in matched) else 0.0
    return _clamp(coverage * 0.8 + title_or_tag_bonus)


def _freshness_score(created_at: object, now: datetime) -> float:
    try:
        created = (
            datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            if isinstance(created_at, str)
            else created_at
        )
        if not isinstance(created, datetime):
            return 0.1
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days = max((now - created.astimezone(timezone.utc)).total_seconds() / 86400.0, 0.0)
        return 1.0 / (1.0 + days / 30.0)
    except (TypeError, ValueError, OverflowError):
        return 0.1


def _hybrid_rank(
    keyword_results: list[dict],
    vector_results: list[dict],
    keywords: list[str],
    top_k: int,
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Merge both channels with relevance dominant over heat and freshness."""
    merged_by_id: dict[int, dict] = {}
    for item in keyword_results:
        copied = dict(item)
        copied["_kw_score"] = _keyword_relevance(copied, keywords)
        copied["_vec_score"] = 0.0
        copied["_from_keyword"] = True
        copied["_from_vector"] = False
        merged_by_id[copied["id"]] = copied

    for rank, item in enumerate(vector_results):
        memory_id = item["id"]
        vector_score = _clamp(item.get("similarity", 1.0 / (rank + 1)))
        if memory_id in merged_by_id:
            merged_by_id[memory_id].update({
                key: value for key, value in item.items()
                if value is not None and key not in {"_kw_score", "_vec_score"}
            })
            merged_by_id[memory_id]["_vec_score"] = vector_score
            merged_by_id[memory_id]["_from_vector"] = True
        else:
            copied = dict(item)
            copied["_kw_score"] = 0.0
            copied["_vec_score"] = vector_score
            copied["_from_keyword"] = False
            copied["_from_vector"] = True
            merged_by_id[memory_id] = copied

    current_time = now or datetime.now(timezone.utc)
    for item in merged_by_id.values():
        keyword_score = _clamp(item.get("_kw_score"))
        vector_score = _clamp(item.get("_vec_score"))
        primary_relevance = max(keyword_score, vector_score)
        secondary_relevance = min(keyword_score, vector_score)
        both_channels = bool(item.get("_from_keyword") and item.get("_from_vector"))
        item["_retrieval_score"] = (
            primary_relevance * 0.60
            + secondary_relevance * 0.12
            + (0.08 if both_channels else 0.0)
            + _clamp(item.get("importance"), 0.0, 10.0) / 10.0 * 0.08
            + _clamp(item.get("heat"), 0.0, 100.0) / 100.0 * 0.06
            + _freshness_score(item.get("created_at"), current_time) * 0.06
        )

    ranked = sorted(
        merged_by_id.values(),
        key=lambda item: (item["_retrieval_score"], item.get("importance") or 0, item.get("heat") or 0),
        reverse=True,
    )
    return ranked[:top_k]


async def search_memories(query: str, top_k: int = 8) -> list[dict]:
    if not query.strip():
        return []

    bounded_top_k = max(1, min(int(top_k), 20))
    keywords = _extract_keywords(query)[:5]
    candidate_limit = min(MAX_CANDIDATES, max(20, bounded_top_k * 5))
    keyword_results = _keyword_search(keywords, candidate_limit) or []
    embedding = await _get_embedding(query)
    vector_results = (_vector_search_sync(embedding, candidate_limit) or []) if embedding else []
    top_results = _hybrid_rank(
        keyword_results,
        vector_results,
        keywords,
        bounded_top_k,
    )
    recalled_ids = []
    for item in top_results:
        score = item.get("_retrieval_score", 0.0)
        item["inject_mode"] = (
            "full"
            if item.get("layer") == "核心" or score >= 0.62
            else "title_only"
        )
        recalled_ids.append(item["id"])
        for internal_key in (
            "_kw_score", "_vec_score", "_from_keyword", "_from_vector", "_retrieval_score",
        ):
            item.pop(internal_key, None)

    if recalled_ids:
        _boost_heat(recalled_ids)
    log.info(
        "记忆搜索完成: query=%s 关键词=%s keyword=%d vector=%d selected=%d",
        query[:30], keywords, len(keyword_results), len(vector_results), len(top_results),
    )
    return top_results


def format_memories_for_injection(memories: list[dict]) -> str:
    if not memories:
        return ""
    lines = ["[相关记忆]"]
    for index, memory in enumerate(memories, 1):
        if memory.get("inject_mode") == "full":
            lines.append(f"{index}. {memory.get('content', '')}")
        else:
            title = memory.get("title") or memory.get("content", "")[:50]
            lines.append(f"{index}. (模糊) {title}")
    return "\n".join(lines)

