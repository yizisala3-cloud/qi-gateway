"""记忆搜索模块：关键词 + 向量双通道混合检索。"""
import logging
import re
from typing import Optional

import httpx

from .config import cfg
from .db import get_client, safe_query

log = logging.getLogger("gateway.memory_search")

# ── Embedding ─────────────────────────────────────────────────────

EMBEDDING_MODEL = "Pro/Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024


async def _get_embedding(text: str) -> Optional[list[float]]:
    """调硅基流动 Embedding API 获取向量。"""
    if not cfg.ANALYSIS_API_KEY:
        return None
    url = f"{cfg.ANALYSIS_BASE_URL}/embeddings"
    headers = {"Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}"}
    payload = {
        "model": EMBEDDING_MODEL,
        "input": text[:2000],  # 截断防超限
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data["data"][0]["embedding"]
    except Exception as e:
        log.warning("Embedding 请求失败: %s", e)
        return None


# ── 关键词分词 ────────────────────────────────────────────────────

def _extract_keywords(text: str) -> list[str]:
    """简易中文分词提取关键词（无 jieba 依赖时的降级方案）。"""
    try:
        import jieba.analyse
        keywords = jieba.analyse.extract_tags(text, topK=8)
        return keywords
    except ImportError:
        # 降级：按标点和空格切分，过滤短词
        tokens = re.split(r'[\s,，。！？、；：""''（）\(\)\[\]【】]+', text)
        return [t for t in tokens if len(t) >= 2][:8]


# ── 关键词搜索 ────────────────────────────────────────────────────

@safe_query
def _keyword_search(keywords: list[str], limit: int = 20) -> list[dict]:
    """通过 tags 数组重叠 + content ILIKE 搜索。"""
    client = get_client()
    if not client or not keywords:
        return []

    # 用 OR 组合多个关键词的 ILIKE 条件
    conditions = " OR ".join(
        [f"content.ilike.%{kw}%" for kw in keywords[:5]]
    )
    # tags 重叠查询
    resp = (
        client.table("memories")
        .select("id, content, title, tags, heat, importance, created_at, last_recalled_at")
        .eq("is_active", True)
        .neq("verified", "rejected")
        .or_(conditions)
        .order("heat", desc=True)
        .limit(limit)
        .execute()
    )
    return resp.data if resp.data else []


# ── 向量搜索 ──────────────────────────────────────────────────────

@safe_query
def _vector_search_sync(embedding: list[float], limit: int = 20) -> list[dict]:
    """通过 pgvector 余弦相似度搜索（RPC 调用）。"""
    client = get_client()
    if not client:
        return []

    resp = client.rpc("match_memories", {
        "query_embedding": embedding,
        "match_threshold": 0.5,
        "match_count": limit,
    }).execute()
    return resp.data if resp.data else []


# ── 热度升温 ──────────────────────────────────────────────────────

@safe_query
def _boost_heat(memory_ids: list[int]):
    """被召回的记忆热度 +15。"""
    client = get_client()
    if not client or not memory_ids:
        return
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    for mid in memory_ids:
        # 用 RPC 原子更新（避免并发问题）
        client.rpc("boost_memory_heat", {
            "memory_id": mid,
            "boost_amount": 15,
            "recalled_at": now,
        }).execute()


# ── 综合搜索（主入口）────────────────────────────────────────────

async def search_memories(query: str, top_k: int = 8) -> list[dict]:
    """双通道混合搜索，返回按综合分排序的记忆列表。

    每条记忆附带 'inject_mode': 'full' | 'title_only'
    """
    if not query.strip():
        return []

    # 1. 关键词通道
    keywords = _extract_keywords(query)
    keyword_results = _keyword_search(keywords) or []

    # 2. 向量通道
    embedding = await _get_embedding(query)
    vector_results = []
    if embedding:
        vector_results = _vector_search_sync(embedding) or []

    # 3. 合并去重
    seen_ids = set()
    merged = []

    for item in keyword_results:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            item["_kw_rank"] = keyword_results.index(item)
            item["_vec_rank"] = None
            merged.append(item)

    for item in vector_results:
        if item["id"] not in seen_ids:
            seen_ids.add(item["id"])
            item["_kw_rank"] = None
            item["_vec_rank"] = vector_results.index(item)
            merged.append(item)
        else:
            # 已存在，补充向量排名
            for m in merged:
                if m["id"] == item["id"]:
                    m["_vec_rank"] = vector_results.index(item)
                    break

    # 4. 综合评分（RRF 简化版）
    def score(item):
        kw_score = 1.0 / (60 + item["_kw_rank"]) if item.get("_kw_rank") is not None else 0
        vec_score = 1.0 / (60 + item["_vec_rank"]) if item.get("_vec_rank") is not None else 0
        heat_score = item.get("heat", 0) / 100.0
        # 时间近度
        from datetime import datetime, timezone
        created = item.get("created_at", "")
        try:
            if isinstance(created, str):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = created
            days = (datetime.now(timezone.utc) - created_dt).days
            time_score = 1.0 / (1.0 + days * 0.1)
        except Exception:
            time_score = 0.1

        return (
            kw_score * 0.35
            + vec_score * 0.35
            + heat_score * 0.15
            + time_score * 0.15
        )

    merged.sort(key=score, reverse=True)
    top_results = merged[:top_k]

    # 5. 标记注入模式 + 升温
    recalled_ids = []
    for item in top_results:
        heat = item.get("heat", 0)
        if heat >= 60:
            item["inject_mode"] = "full"
        else:
            item["inject_mode"] = "title_only"
        recalled_ids.append(item["id"])

    # 异步升温（不阻塞返回）
    if recalled_ids:
        _boost_heat(recalled_ids)

    # 6. 清理内部字段
    for item in top_results:
        item.pop("_kw_rank", None)
        item.pop("_vec_rank", None)

    log.info("记忆搜索完成: query=%s, 关键词=%s, 结果=%d条", query[:30], keywords, len(top_results))
    return top_results


def format_memories_for_injection(memories: list[dict]) -> str:
    """将搜索结果格式化为注入 prompt 的文本。"""
    if not memories:
        return ""

    lines = ["[相关记忆]"]
    for i, mem in enumerate(memories, 1):
        if mem.get("inject_mode") == "full":
            lines.append(f"{i}. {mem.get('content', '')}")
        else:
            title = mem.get("title") or mem.get("content", "")[:50]
            lines.append(f"{i}. (模糊) {title}")

    return "\n".join(lines)
