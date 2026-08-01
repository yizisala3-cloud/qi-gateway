"""Supabase 读写封装。"""
import logging
from typing import Any

from .config import cfg

log = logging.getLogger("gateway.db")

_client = None
_client_mode = "uninitialized"
_client_error = ""


def _key_candidates() -> list[tuple[str, str]]:
    """Return unique server key candidates in priority order."""
    candidates = [
        ("secret", cfg.SUPABASE_SECRET_KEY),
        ("service_role", cfg.SUPABASE_SERVICE_ROLE_KEY),
        ("fallback", cfg.SUPABASE_KEY),
    ]
    result = []
    seen = set()
    for mode, key in candidates:
        if key and key not in seen:
            result.append((mode, key))
            seen.add(key)
    return result


def _probe_client(client) -> None:
    """Run a harmless query so malformed/revoked keys fail before activation."""
    client.table("jiwen_state").select("id").eq("id", 1).limit(1).execute()


def get_client():
    """延迟初始化服务端 Supabase 客户端。

    Elevated keys are probed first. Before RLS is enabled, an invalid elevated key
    safely falls back to the existing SUPABASE_KEY so automatic storage keeps working.
    """
    global _client, _client_mode, _client_error
    if _client is not None:
        return _client

    if not cfg.SUPABASE_URL:
        _client_mode = "unavailable"
        _client_error = "SUPABASE_URL is missing"
        log.warning("Supabase 未配置，跳过数据库功能")
        return None

    candidates = _key_candidates()
    if not candidates:
        _client_mode = "unavailable"
        _client_error = "no Supabase server key is configured"
        log.warning("Supabase 未配置，跳过数据库功能")
        return None

    try:
        from supabase import create_client
    except Exception as exc:
        _client_mode = "unavailable"
        _client_error = type(exc).__name__
        log.error("Supabase SDK 导入失败: %s", exc)
        return None

    failures = []
    for mode, key in candidates:
        try:
            candidate = create_client(cfg.SUPABASE_URL, key)
            _probe_client(candidate)
            _client = candidate
            _client_mode = mode
            _client_error = ""
            log.info("Supabase 服务端客户端初始化成功（key_mode=%s）", mode)
            return _client
        except Exception as exc:
            failures.append(f"{mode}:{type(exc).__name__}")
            log.warning("Supabase key 探测失败（key_mode=%s, error=%s）", mode, type(exc).__name__)

    _client_mode = "unavailable"
    _client_error = ", ".join(failures)
    log.error("所有 Supabase key 均不可用（%s）", _client_error)
    return None


def get_client_status() -> dict[str, Any]:
    """Return non-sensitive connection status for readiness checks."""
    client = get_client()
    return {
        "access_ok": client is not None,
        "key_mode": _client_mode,
        "elevated_configured": cfg.supabase_elevated_key_configured,
        "elevated_active": _client_mode in {"secret", "service_role"},
        "error": _client_error,
    }


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

