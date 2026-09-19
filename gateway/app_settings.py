"""应用级开关：app_settings 读取层，带 60 秒 TTL 模块级缓存。

每条聊天请求都会经过 build_context，若每次都查库会放大 Supabase 压力；
这里用模块级缓存把读取频率压到每分钟一次。fail-open 语义固定：查询异常、
行缺失、值非法时一律按开启处理——关闭注入是显式动作，任何读取层面的
不确定性都不应让聊天静默丢失身体状态卡。
"""
import logging
import time
from typing import Any

from . import db

log = logging.getLogger("gateway.app_settings")

EVENTIDE_INJECT_KEY = "eventide.inject_enabled"

# 缓存 TTL（秒）。测试用 reset_settings_cache() 清空。
_SETTINGS_TTL_SECONDS = 60.0

_cache_enabled: bool | None = None
_cache_expires_at: float = 0.0


def reset_settings_cache() -> None:
    """清空 TTL 缓存。写库后与测试中都必须调用。"""
    global _cache_enabled, _cache_expires_at
    _cache_enabled = None
    _cache_expires_at = 0.0


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


def is_eventide_injection_enabled() -> bool:
    """Eventide 身体状态注入总开关（60 秒 TTL 缓存）。"""
    global _cache_enabled, _cache_expires_at
    now = time.monotonic()
    if _cache_enabled is not None and now < _cache_expires_at:
        return _cache_enabled

    raw = db.load_app_setting(EVENTIDE_INJECT_KEY)
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

    _cache_enabled = enabled
    _cache_expires_at = now + _SETTINGS_TTL_SECONDS
    return enabled
