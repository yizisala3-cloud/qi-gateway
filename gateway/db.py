"""Supabase 读写封装。"""
import logging
from typing import Any

from .config import cfg

log = logging.getLogger("gateway.db")

_client = None


def get_client():
    """延迟初始化服务端 Supabase 客户端。"""
    global _client
    if _client is None:
        server_key = cfg.supabase_server_key
        if not cfg.SUPABASE_URL or not server_key:
            log.warning("Supabase 未配置，跳过数据库功能")
            return None
        try:
            from supabase import create_client
            _client = create_client(cfg.SUPABASE_URL, server_key)
            key_mode = "elevated" if cfg.supabase_elevated_key_configured else "fallback"
            log.info("Supabase 服务端客户端初始化成功（key_mode=%s）", key_mode)
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


# ── 积温状态 ──────────────────────────────────────────────────────

@safe_query
def load_jiwen_state() -> dict[str, Any] | None:
    """从 Supabase 读取积温状态。"""
    client = get_client()
    if not client:
        return None
    resp = client.table("jiwen_state").select("*").eq("id", 1).execute()
    if resp.data:
        return resp.data[0]
    return None


@safe_query
def save_jiwen_state(state_dict: dict[str, Any]) -> bool:
    """保存积温状态到 Supabase。"""
    client = get_client()
    if not client:
        return False
    data = {
        "connection": state_dict.get("connection", 0),
        "pride": state_dict.get("pride", 0),
        "valence": state_dict.get("valence", 0),
        "arousal": state_dict.get("arousal", 0),
        "immersion": state_dict.get("immersion", 0),
        "last_tick_at": state_dict.get("last_tick_at"),
        "last_chat_at": state_dict.get("last_chat_at"),
        "last_bot_at": state_dict.get("last_bot_at"),
        "user_status": state_dict.get("user_status", "active"),
    }
    client.table("jiwen_state").update(data).eq("id", 1).execute()
    return True


# ── Eventide 状态 ─────────────────────────────────────────────────

@safe_query
def load_eventide_state() -> dict[str, Any] | None:
    """从 Supabase 读取 Eventide 身体状态。"""
    client = get_client()
    if not client:
        return None
    resp = client.table("eventide_state").select("*").eq("id", 1).execute()
    if resp.data:
        return resp.data[0].get("state_data")
    return None


@safe_query
def save_eventide_state(state_data: dict[str, Any]) -> bool:
    """保存 Eventide 状态到 Supabase。"""
    client = get_client()
    if not client:
        return False
    client.table("eventide_state").upsert({
        "id": 1,
        "state_data": state_data,
    }).execute()
    return True
