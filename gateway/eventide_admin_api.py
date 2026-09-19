"""Token-protected Eventide admin API: injection switch + read-only body state.

鉴权与 admin_api.py 同一套：Bearer GATEWAY_TOKEN + hmac.compare_digest。
/body 只读——绝不 load_state 之外的推进、落库或创建初始状态。
"""
from __future__ import annotations

import asyncio
import hmac
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import app_settings, db, eventide_bridge
from .config import cfg

log = logging.getLogger("gateway.eventide_admin_api")

# 状态卡展示时区：与聊天客户端的挂钟约定一致（Asia/Shanghai）。
_CST = timezone(timedelta(hours=8))


def _authorized(request: Request) -> bool:
    if not cfg.GATEWAY_TOKEN:
        return False
    auth = request.headers.get("authorization", "")
    token = auth.removeprefix("Bearer ").strip()
    return bool(token) and hmac.compare_digest(token, cfg.GATEWAY_TOKEN)


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _remaining_text(expires_at: Any) -> str | None:
    """由状态内的时间戳计算剩余时间，结束点按 Asia/Shanghai 展示。

    时长取整口径与 Eventide 状态卡一致（分钟/小时/天）；已到点时提示
    等待下次推进，不倒报负数。
    """
    expires = _parse_iso(expires_at)
    if not expires:
        return None
    seconds = (expires - datetime.now(timezone.utc)).total_seconds()
    if seconds <= 0:
        return "已到预计时间，待下次聊天推进"
    minutes = max(1, round(seconds / 60))
    if minutes < 90:
        duration = f"{minutes} 分钟"
    else:
        hours = round(minutes / 60)
        duration = f"{hours} 小时" if hours < 48 else f"{round(hours / 24)} 天"
    end_local = expires.astimezone(_CST)
    return f"预计还剩 {duration} · 至 {end_local.strftime('%m-%d %H:%M')}"


def _field_list(fields: Any) -> list[dict[str, Any]]:
    """payload 的 {key: {...}} 转数组；缺字段降级为 null，不伪造。"""
    result: list[dict[str, Any]] = []
    if not isinstance(fields, dict):
        return result
    for key, item in fields.items():
        if not isinstance(item, dict):
            continue
        value = item.get("value")
        result.append({
            "key": key,
            "label": item.get("label") or key,
            "value": value if isinstance(value, (int, float)) else None,
            "level": item.get("level") or None,
            "description": item.get("description") or None,
        })
    return result


async def eventide_settings(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)

    if request.method == "PUT":
        try:
            payload = await request.json()
        except Exception:
            return _error("invalid json body", 400)
        if not isinstance(payload, dict) or not isinstance(payload.get("inject_enabled"), bool):
            return _error("inject_enabled must be a boolean", 400)
        enabled = payload["inject_enabled"]
        saved = await asyncio.to_thread(
            db.save_app_setting, app_settings.EVENTIDE_INJECT_KEY, enabled
        )
        if not saved:
            log.error("Eventide 注入开关保存失败")
            return _error("failed to save setting", 500)
        # 立即生效：不让最多 60 秒的 TTL 缓存拖延下一次聊天的行为。
        app_settings.reset_settings_cache()
        return JSONResponse({"inject_enabled": enabled})

    enabled = await asyncio.to_thread(app_settings.is_eventide_injection_enabled)
    return JSONResponse({"inject_enabled": enabled})


async def eventide_body(request: Request):
    if not _authorized(request):
        return _error("unauthorized", 401)

    inject_enabled = await asyncio.to_thread(app_settings.is_eventide_injection_enabled)
    state_data = await asyncio.to_thread(db.load_eventide_state)
    overview = None
    if state_data:
        overview = await asyncio.to_thread(eventide_bridge.get_body_overview, state_data)

    response: dict[str, Any] = {
        # 状态行缺失、Eventide 未安装或状态解析失败都视为未初始化，
        # 前端据此展示引导文案而不是空表格。
        "initialized": overview is not None,
        "inject_enabled": inject_enabled,
        "cycle": None,
        "event": None,
        "fields": [],
        "updated_at": None,
    }
    if not overview:
        return JSONResponse(response)

    cycle_label = overview.get("cycle_label")
    if cycle_label:
        response["cycle"] = {
            "label": cycle_label,
            "remaining_text": _remaining_text(overview.get("cycle_expires_at")),
        }
    event_label = overview.get("event_label")
    if event_label:
        response["event"] = {
            "label": event_label,
            "description": overview.get("event_description"),
            "remaining_text": _remaining_text(overview.get("event_expires_at")),
        }
    response["fields"] = _field_list(overview.get("fields"))
    updated_at = overview.get("last_tick_at")
    response["updated_at"] = str(updated_at) if updated_at else None
    return JSONResponse(response)


eventide_admin_routes = [
    Route("/admin/api/eventide/settings", eventide_settings, methods=["GET", "PUT"]),
    Route("/admin/api/eventide/body", eventide_body, methods=["GET"]),
]
