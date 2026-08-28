"""Supabase 读写封装。"""
import logging
from datetime import datetime, timedelta, timezone
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
    client.table("memory_digest_runs").select("id").limit(1).execute()


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


# ── 聊天原文 ──────────────────────────────────────────────────────

CHAT_MESSAGE_ROLES = ("user", "assistant")
# OrangeChat 客户端以设备本地（Asia/Shanghai）挂钟写入 timestamp without time
# zone 的 created_at。网关写入必须保持同一约定，否则现有按 created_at 排序的
# 读取方（如短期上下文）会把网关行排错位置。带微秒保证同请求内 user 行先于
# assistant 行。
_CST = timezone(timedelta(hours=8))


def save_chat_message(
    role: str,
    content: str,
    assistant_id: str,
    conversation_id: str | None = None,
) -> bool:
    """保存一条聊天原文到 public.chat_messages。

    只做一次写入尝试：表上没有请求唯一 ID 可作幂等键，重试可能在首次实际
    成功但响应丢失时造成重复行，因此失败不重试、只记日志、绝不抛出。
    日志只含操作位置、role、身份字段是否存在与异常类型，不含任何密钥。
    """
    if role not in CHAT_MESSAGE_ROLES:
        log.error("聊天原文保存拒绝: save_chat_message | 非法 role=%s", role)
        return False
    if not isinstance(content, str) or not content.strip():
        log.error("聊天原文保存拒绝: save_chat_message | role=%s | 空内容", role)
        return False
    if not isinstance(assistant_id, str) or not assistant_id.strip():
        log.error(
            "聊天原文保存拒绝: save_chat_message | role=%s | assistant_id 缺失", role
        )
        return False

    payload = {
        "assistant_id": assistant_id,
        "conversation_id": conversation_id if conversation_id else None,
        "role": role,
        "content": content,
        "created_at": datetime.now(_CST).strftime("%Y-%m-%d %H:%M:%S.%f"),
    }
    try:
        client = get_client()
        if not client:
            log.error(
                "聊天原文保存失败: save_chat_message | role=%s | assistant_id=%s | "
                "conversation_id=%s | Supabase 客户端不可用 | attempts=1",
                role, bool(assistant_id), bool(conversation_id),
            )
            return False
        client.table("chat_messages").insert(payload).execute()
        return True
    except Exception as exc:
        log.error(
            "聊天原文保存失败: save_chat_message | role=%s | assistant_id=%s | "
            "conversation_id=%s | error=%s | attempts=1（无幂等键，不重试）",
            role, bool(assistant_id), bool(conversation_id), type(exc).__name__,
        )
        return False


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
