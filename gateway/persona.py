"""人设管理模块：从 Supabase persona 表读取当前活跃人设。"""
import logging
import time
from typing import Optional

from .db import get_client, safe_query

log = logging.getLogger("gateway.persona")

# 内存缓存（人设不常变，缓存 5 分钟）
_cache: Optional[str] = None
_cache_ts: float = 0
_CACHE_TTL = 300  # 秒


@safe_query
def _fetch_persona() -> Optional[str]:
    """从 Supabase 拉取当前活跃人设。"""
    client = get_client()
    if not client:
        return None
    resp = (
        client.table("persona")
        .select("content")
        .eq("is_active", True)
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    if resp.data:
        return resp.data[0]["content"]
    return None


def load_persona() -> str:
    """读取人设 prompt，带内存缓存。

    Returns:
        人设文本；如果读取失败返回空字符串（不阻断流程）。
    """
    global _cache, _cache_ts

    now = time.time()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    content = _fetch_persona()
    if content:
        _cache = content
        _cache_ts = now
        log.debug("人设已加载（%d 字符）", len(content))
        return content

    # 读取失败时用缓存兜底
    if _cache is not None:
        log.warning("人设读取失败，使用缓存")
        return _cache

    log.warning("人设读取失败且无缓存")
    return ""
