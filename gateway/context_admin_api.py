"""Token-protected context admin API: recent-chat + timestamp injection settings.

鉴权与 eventide_admin_api.py 同一套：Bearer GATEWAY_TOKEN + hmac.compare_digest。
PUT 三个键都可选，只更新出现的键；保存成功后清空 TTL 缓存，
下一次聊天请求立即生效，无需重启。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import app_settings, db
from .config import cfg

log = logging.getLogger("gateway.context_admin_api")


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def _current_settings() -> dict:
    recent_enabled = await asyncio.to_thread(app_settings.is_recent_chat_injection_enabled)
    recent_limit = await asyncio.to_thread(app_settings.get_recent_chat_injection_limit)
    timestamp_enabled = await asyncio.to_thread(app_settings.is_timestamp_injection_enabled)
    return {
        "recent_chat_enabled": recent_enabled,
        "recent_chat_limit": recent_limit,
        "timestamp_enabled": timestamp_enabled,
    }


def _validated_updates(payload: dict) -> dict[str, Any] | JSONResponse:
    """校验 payload 里出现的键，返回 {setting_key: value}；非法返回 400。

    只校验出现的键，未出现的键保持现状——前端三个控件各自独立保存，
    不应要求调用方每次都带上完整设置。
    """
    updates: dict[str, Any] = {}
    if "recent_chat_enabled" in payload:
        value = payload["recent_chat_enabled"]
        if not isinstance(value, bool):
            return _error("recent_chat_enabled must be a boolean", 400)
        updates[app_settings.RECENT_CHAT_INJECT_KEY] = value
    if "timestamp_enabled" in payload:
        value = payload["timestamp_enabled"]
        if not isinstance(value, bool):
            return _error("timestamp_enabled must be a boolean", 400)
        updates[app_settings.TIMESTAMP_INJECT_KEY] = value
    if "recent_chat_limit" in payload:
        value = payload["recent_chat_limit"]
        # bool 是 int 的子类，显式挡掉，避免 true 被当成 1 存库。
        if isinstance(value, bool) or not isinstance(value, int):
            return _error(
                "recent_chat_limit must be an integer between "
                f"{app_settings.RECENT_CHAT_LIMIT_MIN} and {app_settings.RECENT_CHAT_LIMIT_MAX}",
                400,
            )
        if not (
            app_settings.RECENT_CHAT_LIMIT_MIN
            <= value
            <= app_settings.RECENT_CHAT_LIMIT_MAX
        ):
            return _error(
                "recent_chat_limit must be between "
                f"{app_settings.RECENT_CHAT_LIMIT_MIN} and {app_settings.RECENT_CHAT_LIMIT_MAX}",
                400,
            )
        updates[app_settings.RECENT_CHAT_LIMIT_KEY] = value
    return updates


async def context_settings(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)

    if request.method == "PUT":
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid json body", 400)
        if not isinstance(payload, dict):
            return _error("request body must be a json object", 400)

        updates = _validated_updates(payload)
        if isinstance(updates, JSONResponse):
            return updates

        saved_any = False
        for key, value in updates.items():
            saved = await asyncio.to_thread(db.save_app_setting, key, value)
            if not saved:
                log.error(f"上下文注入设置保存失败: {key}")
                # db 层就是逐键 upsert，没有跨键事务；部分落库时旧缓存若不
                # 失效，已写入的键会再滞留最长 60 秒读不到新值。
                if saved_any:
                    app_settings.reset_settings_cache()
                return _error("failed to save setting", 500)
            saved_any = True
        if updates:
            # 立即生效：不让最多 60 秒的 TTL 缓存拖延下一次聊天的行为。
            app_settings.reset_settings_cache()

        return JSONResponse(await _current_settings())

    return JSONResponse(await _current_settings())


context_admin_routes = [
    Route("/admin/api/context/settings", context_settings, methods=["GET", "PUT"]),
]
