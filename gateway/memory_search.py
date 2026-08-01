"""记忆搜索模块：关键词 + 向量双通道混合检索。"""
import logging
import re
from typing import Optional

import httpx

from .config import cfg
from .db import get_client, safe_query

log = logging.getLogger("gateway.memory_search")

EMBEDDING_MODEL = "Pro/Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024


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
        .select("id,content,title,tags,heat,importance,created_at,last_recalled_at")
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
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for memory_id in memory_ids:
        client.rpc("boost_memory_heat", {
            "memory_id": memory_id,
            "boost_amount": 15,
            "recalled_at": now,
        }).execute()


async def search_memories(query: str, top_k: int = 8) -> list[dict]:
    if not query.strip():
        return []

    keywords = _extract_keywords(query)
    keyword_results = _keyword_search(keywords) or []
    embedding = await _get_embedding(query)
    vector_results = _vector_search_sync(embedding) or [] if embedding else []

    merged_by_id: dict[int, dict] = {}
    for rank, item in enumerate(keyword_results):
        copied = dict(item)
        copied["_kw_rank"] = rank
        copied["_vec_rank"] = None
        merged_by_id[copied["id"]] = copied
    for rank, item in enumerate(vector_results):
        memory_id = item["id"]
        if memory_id in merged_by_id:
            merged_by_id[memory_id]["_vec_rank"] = rank
        else:
            copied = dict(item)
            copied["_kw_rank"] = None
            copied["_vec_rank"] = rank
            merged_by_id[memory_id] = copied

    merged = list(merged_by_id.values())

    def score(item):
        kw_score = 1.0 / (60 + item["_kw_rank"]) if item.get("_kw_rank") is not None else 0
        vec_score = 1.0 / (60 + item["_vec_rank"]) if item.get("_vec_rank") is not None else 0
        heat_score = item.get("heat", 0) / 100.0
        from datetime import datetime, timezone
        try:
            created = item.get("created_at", "")
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00")) if isinstance(created, str) else created
            days = (datetime.now(timezone.utc) - created_dt).days
            time_score = 1.0 / (1.0 + days * 0.1)
        except Exception:
            time_score = 0.1
        return kw_score * 0.35 + vec_score * 0.35 + heat_score * 0.15 + time_score * 0.15

    merged.sort(key=score, reverse=True)
    top_results = merged[:top_k]
    recalled_ids = []
    for item in top_results:
        item["inject_mode"] = "full" if item.get("heat", 0) >= 60 else "title_only"
        recalled_ids.append(item["id"])
        item.pop("_kw_rank", None)
        item.pop("_vec_rank", None)

    if recalled_ids:
        _boost_heat(recalled_ids)
    log.info("记忆搜索完成: query=%s, 关键词=%s, 结果=%d条", query[:30], keywords, len(top_results))
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

