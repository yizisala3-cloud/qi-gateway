"""Supabase 读写封装。

Phase 1 只是骨架，后续 Phase 加入积温/Eventide 状态读写。
"""
import logging
from functools import lru_cache

from .config import cfg

log = logging.getLogger("gateway.db")

_client = None


def get_client():
    """延迟初始化 Supabase 客户端。"""
    global _client
    if _client is None:
        if not cfg.SUPABASE_URL or not cfg.SUPABASE_KEY:
            log.warning("Supabase 未配置，跳过数据库功能")
            return None
        try:
            from supabase import create_client
            _client = create_client(cfg.SUPABASE_URL, cfg.SUPABASE_KEY)
            log.info("Supabase 客户端初始化成功")
        except Exception as e:
            log.error(f"Supabase 初始化失败: {e}")
    return _client


def safe_query(fn):
    """装饰器：Supabase 查询容错 + 一次重试。"""
    def wrapper(*args, **kwargs):
        for attempt in range(2):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if attempt == 0:
                    log.warning(f"Supabase 查询重试: {fn.__name__} | {e}")
                else:
                    log.error(f"Supabase 查询失败: {fn.__name__} | {e}")
                    return None
    return wrapper
