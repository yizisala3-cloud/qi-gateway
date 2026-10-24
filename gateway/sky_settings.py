"""Persisted Sky access gate. Confirmed deliveries remain recoverable when off."""
import asyncio
import logging
import threading
import time
from uuid import UUID, uuid4

from starlette.responses import JSONResponse
from starlette.routing import Route

from . import db

KEY = 'sky.enabled'
INITIAL_EPOCH = '00000000-0000-0000-0000-000000000000'
log = logging.getLogger('gateway.sky_settings')
_lock = threading.RLock()
_cached = None
_expires = 0


def reset_cache():
    global _cached, _expires
    with _lock:
        _cached, _expires = None, 0


def parse_value(raw):
    if raw is None:
        return {'enabled': False, 'pause_epoch': INITIAL_EPOCH}
    if (not isinstance(raw, dict) or set(raw) != {'enabled', 'pause_epoch'}
            or type(raw['enabled']) is not bool or not isinstance(raw['pause_epoch'], str)):
        return None
    try:
        if str(UUID(raw['pause_epoch'])) != raw['pause_epoch']:
            return None
    except ValueError:
        return None
    return dict(raw)


def snapshot(*, refresh=False):
    global _cached, _expires
    with _lock:
        if not refresh and _cached is not None and time.monotonic() < _expires:
            return dict(_cached)
        raw = db.load_app_setting(KEY)
        value = parse_value(raw)
        available = value is not None
        _cached = {'enabled': value['enabled'] if available else False,
                   'pause_epoch': value['pause_epoch'] if available else None,
                   'available': available,
                   'writable': available and bool(db.get_client_status().get('elevated_active')),
                   'source': 'default' if raw is None else 'database' if available else 'unavailable'}
        _expires = time.monotonic() + 1
        if not available:
            log.warning('sky_setting_unavailable action=pause')
        return dict(_cached)


def save(enabled):
    global _cached, _expires
    with _lock:
        if type(enabled) is not bool or not snapshot(refresh=True)['writable']:
            raise RuntimeError('光遇接入开关保存失败，请检查数据库连接。')
        try:
            client = db.get_client()
            if client is None:
                raise RuntimeError('storage unavailable')
            # The DB transaction serializes different workers. Enabling keeps
            # the latest pause epoch; disabling commits a fresh one atomically.
            result = client.rpc('sky_set_access', {'p_enabled': enabled,
                               'p_pause_epoch': str(uuid4())}).execute().data
            value = parse_value(result)
            if value is None or result is None or value['enabled'] is not enabled:
                raise RuntimeError('invalid setting receipt')
        except Exception as exc:
            reset_cache()
            log.exception('sky_setting_transaction_failed')
            raise RuntimeError('光遇接入设置未能确认，请检查数据库迁移及连接。') from exc
        _cached = {**value, 'available': True, 'writable': True, 'source': 'database'}
        _expires = time.monotonic() + 1
        return dict(_cached)


async def settings_endpoint(request):
    from .main import verify_token
    headers = {'Cache-Control': 'no-store'}
    if not verify_token(request):
        return JSONResponse({'error': 'unauthorized'}, status_code=401, headers=headers)
    if request.method == 'PUT':
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return JSONResponse({'error': '请求正文必须为合法JSON。'}, status_code=400, headers=headers)
        if not isinstance(body, dict) or set(body) != {'enabled'} or type(body['enabled']) is not bool:
            return JSONResponse({'error': '请只提交布尔值enabled。'}, status_code=400, headers=headers)
        try:
            result = await asyncio.to_thread(save, body['enabled'])
        except RuntimeError as exc:
            log.warning('sky_setting_save_failed', exc_info=True)
            return JSONResponse({'error': str(exc)}, status_code=503, headers=headers)
    else:
        result = await asyncio.to_thread(snapshot, refresh=True)
    return JSONResponse(result, headers=headers)


sky_settings_routes = [
    Route('/admin/api/sky/settings', settings_endpoint, methods=['GET', 'PUT']),
    Route('/v1/sky/settings', settings_endpoint, methods=['GET']),
]
