"""应用级开关：app_settings 读取层，带 60 秒 TTL 模块级缓存。

每条聊天请求都会经过 build_context，若每次都查库会放大 Supabase 压力；
这里用模块级缓存把读取频率压到每分钟一次。fail-open 语义固定：查询异常、
行缺失、值非法时一律按开启处理——关闭注入是显式动作，任何读取层面的
不确定性都不应让聊天静默丢失注入块。
"""
import logging
import time
from typing import Any

from . import db

log = logging.getLogger("gateway.app_settings")

EVENTIDE_INJECT_KEY = "eventide.inject_enabled"
RECENT_CHAT_INJECT_KEY = "recent_chat.inject_enabled"
RECENT_CHAT_LIMIT_KEY = "recent_chat.inject_limit"
TIMESTAMP_INJECT_KEY = "timestamp.inject_enabled"

# 注入条数边界：前端输入框、管理 API 校验与读取层夹取共用同一条边界，
# 避免三处各自漂移。
RECENT_CHAT_LIMIT_MIN = 1
RECENT_CHAT_LIMIT_MAX = 100
_RECENT_CHAT_LIMIT_DEFAULT = 10

# 缓存 TTL（秒）。测试用 reset_settings_cache() 清空。
_SETTINGS_TTL_SECONDS = 60.0

# 按 key 缓存，值只可能是 bool / int（解析失败不会进缓存，直接落默认值）。
# 单值全局变量撑不住多 key：TTL 到期时间各不相同，按 key 独立过期。
_cache: dict[str, Any] = {}
_cache_expires_at: dict[str, float] = {}

_MISSING = object()


def reset_settings_cache() -> None:
    """清空 TTL 缓存。写库后与测试中都必须调用。"""
    _cache.clear()
    _cache_expires_at.clear()


def _cache_get(key: str) -> Any:
    now = time.monotonic()
    if key in _cache and now < _cache_expires_at.get(key, 0.0):
        return _cache[key]
    return _MISSING


def _cache_set(key: str, value: Any) -> None:
    now = time.monotonic()
    _cache[key] = value
    _cache_expires_at[key] = now + _SETTINGS_TTL_SECONDS


def _parse_enabled(value: Any) -> bool | None:
    """把 app_settings.value 解析成布尔；解析不出返回 None。

    正常只存 jsonb 布尔；对历史/手写数据做一层防御（字符串、
    {"inject_enabled": bool} 对象也认）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "1"):
            return True
        if lowered in ("false", "0"):
            return False
        return None
    if isinstance(value, dict):
        inner = value.get("inject_enabled")
        if isinstance(inner, bool):
            return inner
    return None


def _load_enabled(key: str) -> bool:
    """开关类设置的公共读取路径：fail-open，异常/缺失/非法按开启。"""
    cached = _cache_get(key)
    if cached is not _MISSING:
        return cached

    raw = db.load_app_setting(key)
    if raw is db.APP_SETTING_QUERY_FAILED:
        # 查询异常或 Supabase 不可用：fail-open，保持注入现状。
        log.warning("app_settings 开关读取失败，按开启处理（fail-open）")
        enabled = True
    else:
        enabled = _parse_enabled(raw)
        if enabled is None:
            # 行缺失或值非法：同样按开启，不静默改行为。
            log.info("app_settings 开关缺失或值非法，按开启处理")
            enabled = True

    _cache_set(key, enabled)
    return enabled


def _parse_limit(value: Any) -> int | None:
    """把 app_settings.value 解析成注入条数；解析不出返回 None。

    jsonb 数字反序列化成 int/float；对手写数据再放行纯数字字符串。
    bool 是 int 子类，显式挡掉，避免 true 被当成 1。
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def is_eventide_injection_enabled() -> bool:
    """Eventide 身体状态注入总开关（60 秒 TTL 缓存）。"""
    return _load_enabled(EVENTIDE_INJECT_KEY)


def is_recent_chat_injection_enabled() -> bool:
    """近期对话（流式上下文）注入开关，fail-open 同款语义。"""
    return _load_enabled(RECENT_CHAT_INJECT_KEY)


def get_recent_chat_injection_limit() -> int:
    """近期对话注入条数（1–100），非法/缺失/查询失败回退 10。

    回退到 10 而不是夹取到边界：读取失败说明根本没拿到用户配置，
    此时贴着默认值走比猜一个边界值更接近用户预期。
    """
    cached = _cache_get(RECENT_CHAT_LIMIT_KEY)
    if cached is not _MISSING:
        return cached

    raw = db.load_app_setting(RECENT_CHAT_LIMIT_KEY)
    if raw is db.APP_SETTING_QUERY_FAILED:
        log.warning("app_settings 注入条数读取失败，按默认 10 处理（fail-open）")
        limit = _RECENT_CHAT_LIMIT_DEFAULT
    else:
        parsed = _parse_limit(raw)
        if parsed is None:
            log.info("app_settings 注入条数缺失或值非法，按默认 10 处理")
            limit = _RECENT_CHAT_LIMIT_DEFAULT
        else:
            limit = max(RECENT_CHAT_LIMIT_MIN, min(RECENT_CHAT_LIMIT_MAX, parsed))

    _cache_set(RECENT_CHAT_LIMIT_KEY, limit)
    return limit


def is_timestamp_injection_enabled() -> bool:
    """内置时间戳注入开关，fail-open 同款语义。"""
    return _load_enabled(TIMESTAMP_INJECT_KEY)
