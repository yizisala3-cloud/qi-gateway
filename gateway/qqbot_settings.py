"""Local persisted dashboard switches; identities/secrets stay in the environment."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import replace
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from .qqbot import QQRuntime
from .qqbot_config import QQConfig, _flag

log = logging.getLogger('gateway.qqbot.settings')


class SettingsError(Exception):
    def __init__(self,message,status=503):
        super().__init__(message)
        self.status = status


class QQSettings:
    def __init__(self, app, client, *, env=None, path=None, runtime_factory=QQRuntime):
        self.app,self.client = app,client
        self.env = dict(os.environ if env is None else env)
        self.path = Path(path or self.env.get('QQBOT_SETTINGS_PATH') or 'data/qqbot-settings.json')
        self.runtime_factory = runtime_factory
        self.runtime = None
        self.lock = asyncio.Lock()
        self.closing = False
        self.read_error = ''
        self.apply_error = ''
        self.source = 'environment'
        self.flags = {'enabled':False,'proactive_enabled':False}
        try:
            if self.path.exists():
                if self.path.stat().st_size>4096:
                    raise ValueError('settings_file_too_large')
                flags = json.loads(self.path.read_text(encoding='utf-8'))
                if (not isinstance(flags,dict) or set(flags)!=set(self.flags)
                        or any(not isinstance(v,bool) for v in flags.values())
                        or (flags['proactive_enabled'] and not flags['enabled'])):
                    raise ValueError('invalid_settings_file')
                self.flags = flags
                self.source = 'file'
            else:
                enabled = _flag(self.env,'QQBOT_ENABLED')
                self.flags = dict(enabled=enabled,proactive_enabled=enabled and _flag(self.env,'QQBOT_PROACTIVE_ENABLED'))
        except (OSError,ValueError) as exc:
            self.read_error = 'QQBot开关读取失败，请检查本地配置文件后重启网关。'
            log.error('QQ开关读取失败 type=%s',type(exc).__name__,exc_info=exc)

    def config(self, flags):
        return QQConfig.from_env({**self.env,'QQBOT_ENABLED':'true',
            'QQBOT_PROACTIVE_ENABLED':'true' if flags['proactive_enabled'] else 'false'})

    def readiness(self, proactive=False):
        required = [('QQBOT_TOKEN','连接令牌'),('QQBOT_ACCOUNT_ID','机器人QQ号'),
                    ('QQBOT_OWNER_ID','主人QQ号'),('QQBOT_ASSISTANT_ID','角色')]
        if proactive:
            required += [('QQBOT_TIMEZONE','时区'),('QQBOT_ACTIVE_START','开始时间'),('QQBOT_ACTIVE_END','结束时间')]
        missing = [label for key,label in required if not self.env.get(key,'').strip()]
        if missing:
            return False,'请先配置'+ '、'.join(missing)+'。'
        try:
            self.config(dict(proactive_enabled=proactive))
        except ValueError as exc:
            return False,str(exc)
        return True,''

    def snapshot(self):
        configured,note = self.readiness()
        proactive_configured,proactive_note = self.readiness(True)
        running = bool(self.runtime and not self.runtime.stopped)
        runtime_state,status_error = {},''
        if running:
            try:
                runtime_state = self.runtime.status()
            except Exception as exc:
                status_error = 'QQBot运行状态读取失败，可关闭总开关后重试。'
                runtime_state = {'connected':self.runtime.transport.ready}
                log.warning('QQ运行状态读取失败 type=%s',type(exc).__name__,exc_info=exc)
        return {**self.flags,'writable':not self.read_error and not self.closing,'source':self.source,
            'configured':configured,'configuration_note':note,
            'proactive_configured':proactive_configured,'proactive_note':proactive_note,
            'running':running,'connected':bool(runtime_state.get('connected')),
            'owner_paused':bool(runtime_state.get('paused')),'silence_until':runtime_state.get('silence_until',0),
            'error':self.read_error or self.apply_error or status_error}

    def _write(self, flags):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=self.path.parent,
                    prefix=self.path.name+'.',delete=False) as file:
                temporary = Path(file.name)
                json.dump(flags,file,ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary,self.path)
        finally:
            if temporary is not None and temporary.exists():
                try:
                    temporary.unlink()
                except OSError as exc:
                    log.warning('QQ开关临时文件清理失败 type=%s',type(exc).__name__,exc_info=exc)

    async def startup(self):
        async with self.lock:
            if not self.read_error and self.flags['enabled']:
                try:
                    await self._apply()
                except Exception as exc:
                    self.apply_error = 'QQBot未能启动，请检查账号配置和本地存储。'
                    log.error('QQ开关启动应用失败 type=%s',type(exc).__name__,exc_info=exc)

    async def _stop(self):
        if self.runtime is None:
            return
        self.app.state.qqbot = None
        await self.runtime.stop()
        self.runtime = None

    async def _apply(self):
        if not self.flags['enabled'] or self.closing:
            await self._stop()
            return
        config = self.config(self.flags)
        if self.runtime is not None and self.runtime.stopped:
            await self._stop()
        if self.runtime is None:
            runtime = self.runtime_factory(config,self.client)
            self.runtime = runtime
            runtime.discard_proactive_on_activation = True
            try:
                runtime.start()
            except Exception:
                runtime.stopped = True
                await self._stop()
                raise
            self.app.state.qqbot = runtime
        else:
            runtime = self.runtime
            if runtime.config.proactive_enabled == config.proactive_enabled and not self.apply_error:
                return
            # Gate sending before any database operation; turning it on always
            # invalidates the old generation first, including delayed receipts.
            runtime.config = replace(runtime.config,proactive_enabled=False)
            if runtime.proactive_generation is not None:
                runtime.proactive_generation.cancel()
            if runtime.leader:
                runtime.store.invalidate_proactive('settings_changed')
            runtime.config = config

    async def save(self, payload):
        async with self.lock:
            if self.closing or self.read_error:
                raise SettingsError(self.read_error or '网关正在关闭，请稍后重试。')
            flags = {**self.flags,**payload}
            if not flags['enabled']:
                if payload.get('proactive_enabled') is True:
                    raise SettingsError('请先开启QQBot总开关。',409)
                flags['proactive_enabled'] = False
            if flags['enabled']:
                try:
                    self.config(flags)
                except ValueError as exc:
                    raise SettingsError(str(exc),422) from exc
            try:
                await asyncio.to_thread(self._write,flags)
            except OSError as exc:
                log.error('QQ开关保存失败 type=%s',type(exc).__name__,exc_info=exc)
                raise SettingsError('开关未能保存，请检查本地存储后重试。') from exc
            self.flags,self.source = flags,'file'
            try:
                await self._apply()
            except Exception as exc:
                self.apply_error = '设置已保存，但QQBot未能完成切换，请检查本地存储后重试。'
                log.error('QQ开关运行切换失败 enabled=%s proactive=%s type=%s',flags['enabled'],flags['proactive_enabled'],type(exc).__name__,exc_info=exc)
                raise SettingsError(self.apply_error) from exc
            self.apply_error = ''
            return self.snapshot()

    async def shutdown(self):
        self.closing = True
        async with self.lock:
            await self._stop()


async def settings_endpoint(request):
    from .main import verify_token
    if not verify_token(request):
        return JSONResponse({'error':'unauthorized'},status_code=401,headers={'Cache-Control':'no-store'})
    manager = getattr(request.app.state,'qqbot_settings',None)
    if manager is None:
        return JSONResponse({'error':'QQBot配置服务未就绪'},status_code=503,headers={'Cache-Control':'no-store'})
    try:
        if request.method=='PUT':
            try:
                payload = await request.json()
            except (ValueError,UnicodeDecodeError):
                raise SettingsError('请求正文必须为合法JSON。',400) from None
            if (not isinstance(payload,dict) or not payload or not set(payload)<={'enabled','proactive_enabled'}
                    or any(not isinstance(v,bool) for v in payload.values())):
                raise SettingsError('请只提交QQBot总开关或主动消息的布尔值。',400)
            data = await manager.save(payload)
        else:
            async with manager.lock:
                data = manager.snapshot()
    except SettingsError as exc:
        return JSONResponse({'error':str(exc)},status_code=exc.status,headers={'Cache-Control':'no-store'})
    return JSONResponse(data,headers={'Cache-Control':'no-store'})


qqbot_settings_routes = [Route('/admin/api/qqbot/settings',settings_endpoint,methods=['GET','PUT'])]
