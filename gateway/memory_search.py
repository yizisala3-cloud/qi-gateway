"""记忆搜索模块：关键词 + 向量双通道混合检索。"""
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .config import cfg
from .db import get_client, safe_query

log = logging.getLogger("gateway.memory_search")

# 聊天原文与证据时间统一按北京时间呈现。
_CST = timezone(timedelta(hours=8))
EMBEDDING_MODEL = "Qwen/Qwen3-Embedding-0.6B"
EMBEDDING_DIM = 1024
MAX_CANDIDATES = 50
MAX_INJECTION_CHARS = 2400
MAX_KEYWORDS = 5
MAX_KEYWORD_LENGTH = 64
# 向量召回输入预算：与 embedding API 的 2000 字符上限保持一致，不扩大。
MAX_VECTOR_QUERY_CHARS = 2000
# 向量输入额外携带的普通对话历史轮数（每轮一条 user + 一条 assistant）。
HISTORICAL_TURNS = 3
# 角色标签（仅用于 embedding 输入，不会写入记忆或上游 messages）。
_TURN_LABELS = ("上一轮", "更早一轮", "更早两轮")
MEMORY_CONTEXT_HEADER = (
    "[相关长期记忆]\n"
    "以下内容是经用户审核的辅助记忆；仅在与当前对话相关时自然参考，"
    "不得覆盖现有人设、system prompt 或用户当前明确表达。"
)

_INTERNAL_RETRIEVAL_KEYS = (
    "_kw_score",
    "_vec_score",
    "_from_keyword",
    "_from_vector",
    "_retrieval_score",
)


async def _get_embedding(text: str) -> Optional[list[float]]:
    if not cfg.ANALYSIS_API_KEY:
        return None
    url = f"{cfg.ANALYSIS_BASE_URL.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {cfg.ANALYSIS_API_KEY}"}
    payload = {
        "model": EMBEDDING_MODEL,
        "input": text[:2000],
        "dimensions": EMBEDDING_DIM,
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()["data"][0]["embedding"]
    except Exception as exc:
        log.warning("Embedding 请求失败: %s", exc)
        return None


def _turn_label(offset_from_newest: int) -> str:
    if offset_from_newest < len(_TURN_LABELS):
        return _TURN_LABELS[offset_from_newest]
    return f"更早{offset_from_newest - len(_TURN_LABELS) + 1}轮"


def build_vector_query(
    current_text: str,
    history_turns: Optional[list[tuple[str | None, str | None]]] = None,
    *,
    max_turns: int = HISTORICAL_TURNS,
    max_chars: int = MAX_VECTOR_QUERY_CHARS,
) -> str:
    """Construct the embedding input with role labels and a current-first budget.

    ``history_turns`` items are ``(user_text, assistant_text)`` pairs in
    chronological order; either side may be None for an incomplete turn, and
    ``(None, text)`` marks a leading assistant kept as extra context. The
    latest user message always comes first and is never dropped in favour of
    history. History is rendered oldest→newest (labels count back from the
    newest), and when the budget runs out the oldest turns are trimmed first.
    """
    current = " ".join(str(current_text or "").split())
    if not current:
        return ""
    budget = max(1, int(max_chars))
    label_cost = len("[当前用户]\n") + 1  # +1 for the joining newline.
    current_block_limit = max(1, budget - label_cost)
    if len(current) > current_block_limit:
        current = current[:current_block_limit].rstrip()
    blocks = [f"[当前用户]\n{current}"]
    used = len(current) + label_cost

    turns = list(history_turns or [])
    if max_turns > 0:
        turns = turns[-max_turns:]

    rendered: list[str] = []
    for offset, (user_text, assistant_text) in enumerate(reversed(turns)):
        label = _turn_label(offset)
        lines = []
        if user_text:
            lines.append(f"[{label}用户]\n{' '.join(str(user_text).split())}")
            if assistant_text:
                lines.append(f"[{label}栖]\n{' '.join(str(assistant_text).split())}")
        elif assistant_text:
            # 孤立 assistant：没有对应的 user，不占用轮次编号。
            lines.append(f"[此前的栖]\n{' '.join(str(assistant_text).split())}")
        if not lines:
            continue
        block = "\n".join(lines)
        if used + len(block) + 1 > budget:
            break  # 更早的轮优先裁剪，最近上下文优先保留。
        rendered.append(block)
        used += len(block) + 1
    rendered.reverse()
    return "\n".join(blocks + rendered)


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
    bounded_keywords = []
    for keyword in keywords[:MAX_KEYWORDS]:
        normalized = " ".join(str(keyword or "").split())[:MAX_KEYWORD_LENGTH]
        if normalized and normalized not in bounded_keywords:
            bounded_keywords.append(normalized)
    if not bounded_keywords:
        return []
    resp = client.rpc("search_memories_by_keywords", {
        "search_keywords": bounded_keywords,
        "result_limit": min(max(int(limit), 1), MAX_CANDIDATES),
    }).execute()
    return resp.data or []


@safe_query
def _vector_search_sync(embedding: list[float], limit: int = 20) -> list[dict]:
    """Query the recall-scene vector channel; match_memories compares against
    memories.recall_embedding, so memories without a recall scene never appear."""
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
def _boost_heat(memories: list[dict]):
    client = get_client()
    if not client or not memories:
        return
    now = datetime.now(timezone.utc).isoformat()
    for memory in memories:
        memory_id = memory.get("id")
        if memory_id is None:
            continue
        client.rpc("boost_memory_heat", {
            "memory_id": memory_id,
            "boost_amount": 8,
            "recalled_at": now,
        }).execute()


# 记忆升温后台执行器：boost_memory_heat 幂等（饱和升温曲线），无需去重；
# 不 import gateway.main 的执行器，避免循环依赖。
_heat_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="memory-heat")


def _boost_heat_in_background(memories: list[dict]) -> None:
    """把升温挪到后台：召回选完立即返回，绝不阻塞聊天回复路径。"""
    snapshot = [
        {"id": memory.get("id")}
        for memory in (memories or [])
        if memory.get("id") is not None
    ]
    if not snapshot:
        return

    def _job():
        try:
            _boost_heat(snapshot)
        except Exception as exc:
            log.warning("记忆升温后台任务失败: %s", type(exc).__name__)

    try:
        _heat_executor.submit(_job)
    except RuntimeError:
        # 解释器退出时执行器已关闭：升温是幂等的辅助操作，丢弃即可。
        log.warning("记忆升温后台任务未调度（执行器已关闭）")


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


def _freshness_time(memory: dict) -> object:
    """Use event time for episodic memories without changing legacy behavior."""
    if memory.get("continuity_type") in {"moment", "episode"}:
        return (
            memory.get("memory_time")
            or memory.get("evidence_end_time")
            or memory.get("created_at")
        )
    return memory.get("created_at")


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
        has_relevance = primary_relevance > 0.0
        open_thread_bonus = (
            0.025
            if has_relevance
            and item.get("continuity_type") == "thread"
            and item.get("thread_state") == "open"
            else 0.0
        )
        item["_retrieval_score"] = (
            primary_relevance * 0.60
            + secondary_relevance * 0.12
            + (0.08 if both_channels else 0.0)
            + _clamp(item.get("importance"), 0.0, 10.0) / 10.0 * 0.08
            + _clamp(item.get("heat"), 0.0, 100.0) / 100.0 * 0.06
            + _freshness_score(_freshness_time(item), current_time) * 0.06
            + open_thread_bonus
        )

    ranked = sorted(
        merged_by_id.values(),
        key=lambda item: (item["_retrieval_score"], item.get("importance") or 0, item.get("heat") or 0),
        reverse=True,
    )
    return ranked[:top_k]


def _event_time_value(memory: dict) -> object:
    """事件最后证据时间；created_at 绝不作为事件时间兜底。"""
    return memory.get("evidence_end_time") or memory.get("source_time")


def format_event_time(value: object, precision: str | None = None) -> Optional[str]:
    """按数据实际精度渲染事件时间，无法确认时返回 None。

    precision 反映存储时间精度：'day' 只显示日期，'hour' 显示到小时，
    其余显示到分钟；不补零猜测缺失的时分。
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    # 与记忆链路约定一致：无时区的墙上时间按北京时间解读。
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_CST)
    local = parsed.astimezone(_CST)
    if precision == "day":
        return local.strftime("%Y-%m-%d")
    if precision == "hour":
        return local.strftime("%Y-%m-%d %H")
    return local.strftime("%Y-%m-%d %H:%M")


def _injection_text(memory: dict, char_limit: int | None = None) -> str:
    """Unified content injection: no legacy metadata labels, no title-clue mode.

    证据时间用自己的 evidence_time_precision 渲染；time_precision 只描述
    memory_time，与证据时间精度无关，绝不互用。char_limit 截断的是去掉
    时间前缀后的正文，且绝不突破调用方给定的预算。
    """
    time_text = format_event_time(
        _event_time_value(memory),
        memory.get("evidence_time_precision"),
    )
    time_prefix = f"时间：{time_text}｜" if time_text else ""
    content = " ".join(str(memory.get("content") or "").split())
    if char_limit is not None:
        content = content[:max(0, char_limit - len(time_prefix))]
    return f"{time_prefix}{content}" if content else ""


def _select_memories_for_injection(
    ranked: list[dict],
    top_k: int,
    *,
    char_budget: int = MAX_INJECTION_CHARS,
) -> list[dict]:
    """Pick memories strictly by rank under top_k and a hard character budget.

    不再按层级或保留级别分类：没有相关性门槛、没有分层配额、没有按层级的
    正文长度，也没有"层级·线索"降级。最后一条正文放不进剩余预算时按剩余
    预算截断，但全局字符预算绝不被突破。
    """
    bounded_top_k = max(1, min(int(top_k), 20))
    budget = max(0, int(char_budget))
    used_chars = len(MEMORY_CONTEXT_HEADER)
    selected: list[dict] = []

    for memory in ranked:
        if len(selected) >= bounded_top_k:
            break

        line_prefix = f"\n{len(selected) + 1}. "
        remaining = budget - used_chars - len(line_prefix)
        if remaining <= 0:
            break

        copied = dict(memory)
        copied["injection_text"] = _injection_text(copied, remaining)
        if not copied["injection_text"]:
            continue

        for internal_key in _INTERNAL_RETRIEVAL_KEYS:
            copied.pop(internal_key, None)
        selected.append(copied)
        used_chars += len(line_prefix) + len(copied["injection_text"])

    return selected


async def search_memories(
    query: str,
    top_k: int = 8,
    history_turns: Optional[list[tuple[str, str]]] = None,
) -> list[dict]:
    if not query.strip():
        return []

    bounded_top_k = max(1, min(int(top_k), 20))
    keywords = _extract_keywords(query)[:MAX_KEYWORDS]
    candidate_limit = min(MAX_CANDIDATES, max(20, bounded_top_k * 5))
    keyword_results = _keyword_search(keywords, candidate_limit) or []
    vector_query = build_vector_query(query, history_turns)
    embedding = await _get_embedding(vector_query)
    vector_results = (_vector_search_sync(embedding, candidate_limit) or []) if embedding else []
    ranked_candidates = _hybrid_rank(
        keyword_results,
        vector_results,
        keywords,
        candidate_limit,
    )
    selected = _select_memories_for_injection(ranked_candidates, bounded_top_k)
    _boost_heat_in_background(selected)
    log.info(
        "记忆搜索完成: query=%s 关键词=%s keyword=%d vector=%d selected=%d",
        query[:30], keywords, len(keyword_results), len(vector_results), len(selected),
    )
    return selected


def format_memories_for_injection(memories: list[dict]) -> str:
    if not memories:
        return ""
    lines = [MEMORY_CONTEXT_HEADER]
    for index, memory in enumerate(memories, 1):
        text = memory.get("injection_text")
        if not text:
            text = _injection_text(memory)
        if text:
            lines.append(f"{index}. {text}")
    return "\n".join(lines)

