"""Persisted collection switch; no settings I/O occurs on the log emit path."""
from __future__ import annotations

import asyncio
import hmac
import logging
import threading
from contextlib import suppress

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import db, logging_events
from .config import cfg

SETTING_KEY = "gateway_log.collect_enabled"
SYNC_INTERVAL_SECONDS = 30
_environment_default = cfg.GATEWAY_LOG_ENABLED
_lock = threading.RLock()
_closing = False
_writable = False
_source = "environment"
log = logging.getLogger("gateway.log_internal")


def _snapshot() -> dict:
    return {
        "enabled": cfg.GATEWAY_LOG_ENABLED,
        "writable": _writable and not _closing,
        "source": _source,
        "default_enabled": _environment_default,
        "sync_interval_seconds": SYNC_INTERVAL_SECONDS,
    }


def _apply(enabled: bool) -> None:
    cfg.GATEWAY_LOG_ENABLED = enabled
    if enabled:
        logging_events.startup()
    elif logging_events.handler_installed() or logging_events.get_writer() is not None:
        # Stop accepting first, then bound the flush of previously collected events.
        logging_events.shutdown()


def _refresh_locked() -> dict:
    global _writable, _source
    if _closing:
        return _snapshot()
    with logging_events.suppress_capture():
        raw = db.load_app_setting(SETTING_KEY)
    _writable = raw is not db.APP_SETTING_QUERY_FAILED
    if isinstance(raw, bool):
        enabled, _source = raw, "database"
    elif raw is db.APP_SETTING_QUERY_FAILED or raw is not None:
        # A missing connection or invalid value must never silently enable collection.
        enabled, _source = False, "unavailable" if not _writable else "invalid"
    else:
        enabled, _source = _environment_default, "environment"
    _apply(enabled)
    return _snapshot()


def refresh() -> dict:
    with _lock:
        return _refresh_locked()


def startup() -> None:
    global _closing
    with _lock:
        _closing = False
        _refresh_locked()


def shutdown() -> None:
    global _closing
    with _lock:
        _closing = True
        cfg.GATEWAY_LOG_ENABLED = False
        logging_events.shutdown()


async def watch_settings() -> None:
    """Other processes adopt saved settings without querying on business requests."""
    while True:
        await asyncio.sleep(SYNC_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(refresh)
        except Exception:
            log.exception("日志收集设置同步失败，将在下个周期重试")


async def stop_watcher(task: asyncio.Task) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    await asyncio.to_thread(shutdown)


class SettingSaveError(Exception):
    pass


def save(enabled: bool) -> dict:
    global _writable, _source
    with _lock:
        if _closing:
            raise SettingSaveError("网关正在关闭，请稍后重试")
        with logging_events.suppress_capture():
            saved = db.save_app_setting(SETTING_KEY, enabled)
        if not saved:
            # Failed writes leave the running collector unchanged.
            raise SettingSaveError("日志收集开关保存失败，请检查数据库连接后重试")
        _writable, _source = True, "database"
        try:
            _apply(enabled)
        except Exception as exc:
            cfg.GATEWAY_LOG_ENABLED = False
            logging_events.shutdown()
            log.exception("日志收集开关已保存，但采集启动失败")
            raise SettingSaveError("设置已保存，但采集未能启动，请重试") from exc
        return _snapshot()


async def collection_settings(request: Request) -> JSONResponse:
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not cfg.GATEWAY_TOKEN or not token or not hmac.compare_digest(token, cfg.GATEWAY_TOKEN):
        return JSONResponse({"error": "未登录或令牌无效"}, status_code=401)
    if request.method == "PUT":
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({"error": "请求正文必须为合法 JSON"}, status_code=400)
        if not isinstance(payload, dict) or set(payload) != {"enabled"} or not isinstance(payload["enabled"], bool):
            return JSONResponse({"error": "请只提交布尔值 enabled"}, status_code=400)
        try:
            result = await asyncio.to_thread(save, payload["enabled"])
        except SettingSaveError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
    else:
        result = await asyncio.to_thread(refresh)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


log_settings_routes = [
    Route("/admin/api/logs/settings", collection_settings, methods=["GET", "PUT"]),
]
