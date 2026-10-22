"""Authenticated OneBot v11 reverse WS; socket writes are never delivery receipts."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from collections import deque
from uuid import uuid4

from starlette.websockets import WebSocketDisconnect

log = logging.getLogger("gateway.qqbot.transport")


class NotSubmitted(RuntimeError):
    """No send action was written; retrying later is safe."""

class SendCancelled(NotSubmitted):
    """No action was written and its delivery permission has been revoked."""


class OneBotConnection:
    def __init__(self, config, on_event, now=time.time, *, before_submit=None):
        self.config, self.on_event, self.now = config, on_event, now
        self.before_submit = before_submit
        self.socket = None
        self.verified = False
        self.online = False
        self.last_seen = 0
        self.pending = {}
        self.write_lock = asyncio.Lock()
        self.close_task = None

    @property
    def ready(self):
        return bool(self.socket and self.verified and self.online
                    and self.now()-self.last_seen <= self.config.heartbeat_timeout)

    async def action(self, action, params, *, before_submit=None):
        socket = self.socket
        if socket is None:
            raise NotSubmitted("qq_disconnected")
        echo = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        writing = False
        try:
            async with asyncio.timeout(self.config.action_timeout):
                async with self.write_lock:
                    if self.socket is not socket:
                        raise NotSubmitted("qq_connection_changed")
                    if before_submit is not None:
                        before_submit()
                    writing = True
                    await socket.send_json({"action":action,"params":params,"echo":echo})
                return await future
        except (TimeoutError,asyncio.CancelledError) as exc:
            if writing:
                self.verified = self.online = False
                if self.close_task is None or self.close_task.done():
                    self.close_task = asyncio.create_task(self._bounded_close(socket,1011))
            elif isinstance(exc,TimeoutError):
                raise NotSubmitted('qq_write_lock_timeout') from exc
            raise
        finally:
            self.pending.pop(echo,None)
            if not future.done():
                future.cancel()

    async def send(self, segments):
        if not self.ready:
            raise NotSubmitted("qq_not_ready")
        result = await self.action("send_private_msg", {"user_id":int(self.config.owner_id),"message":segments},
                                   before_submit=self.before_submit)
        if result.get("status")=="async" or result.get("retcode")==1200:
            return "unknown", None, "async_receipt"
        if result.get("status")=="ok" and result.get("retcode")==0:
            message_id = (result.get("data") or {}).get("message_id")
            if isinstance(message_id,(int,str)) and not isinstance(message_id,bool) and str(message_id):
                return "confirmed", str(message_id), None
            return "unknown", None, "missing_message_id"
        return "failed", None, "onebot_rejected"

    async def _bounded_close(self, socket, code):
        try:
            async with asyncio.timeout(1):
                await socket.close(code=code)
        except (RuntimeError,WebSocketDisconnect,TimeoutError) as exc:
            log.debug('QQ连接关闭未完成 code=%s type=%s',code,type(exc).__name__,exc_info=exc)

    async def _identify(self, socket, buffered, delivery_lock):
        try:
            info = await self.action("get_login_info", {})
            if self.socket is not socket:
                return
            data = info.get("data") or {}
            if info.get("status")!="ok" or info.get("retcode")!=0 or str(data.get("user_id"))!=self.config.account_id:
                await self._bounded_close(socket,1008)
                return
            status = await self.action("get_status", {})
            if self.socket is socket:
                if status.get('status')!='ok' or status.get('retcode')!=0:
                    await self._bounded_close(socket,1011)
                    return
                async with delivery_lock:
                    self.verified = True
                    self.online = (status.get('data') or {}).get('online') is True
                    while buffered:
                        data,_ = buffered.popleft()
                        await self.on_event(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("QQ账号核验失败 type=%s",type(exc).__name__,exc_info=exc)
            if self.socket is socket:
                await self._bounded_close(socket,1011)

    async def serve(self, ws):
        auth = ws.headers.get("authorization","")
        token = auth.removeprefix("Bearer ")
        if (not auth.startswith("Bearer ") or not hmac.compare_digest(token.encode(),self.config.token.encode())
                or ws.headers.get("x-self-id","")!=self.config.account_id):
            await ws.close(code=1008)
            return
        if self.socket is not None:
            await ws.close(code=1013)
            return
        await ws.accept()
        self.socket = ws
        self.verified = self.online = False
        self.last_seen = self.now()
        buffered, delivery_lock = deque(),asyncio.Lock()
        identify = asyncio.create_task(self._identify(ws,buffered,delivery_lock))
        try:
            while True:
                raw = await asyncio.wait_for(ws.receive_text(),self.config.heartbeat_timeout)
                if len(raw)>1024*1024:
                    await ws.close(code=1009)
                    break
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("QQ忽略无效JSON事件")
                    continue
                if not isinstance(data,dict):
                    continue
                self.last_seen = self.now()
                echo = data.get("echo")
                if isinstance(echo,str) and echo in self.pending:
                    future = self.pending[echo]
                    if not future.done():
                        future.set_result(data)
                    continue
                if str(data.get("self_id")) != self.config.account_id:
                    continue
                if data.get("post_type")=="meta_event":
                    if data.get("meta_event_type")=="heartbeat":
                        self.online = (data.get("status") or {}).get("online") is True
                    continue
                if (data.get('post_type')!='message' or data.get('message_type')!='private'
                        or str(data.get('user_id'))!=self.config.owner_id or data.get('group_id') is not None):
                    continue
                async with delivery_lock:
                    if self.verified:
                        await self.on_event(data)
                    else:
                        size = len(raw.encode('utf-8'))
                        if len(buffered)>=64 or sum(item[1] for item in buffered)+size>262144:
                            log.warning('QQ核验暂存达到上限 count=%s bytes=%s',len(buffered),size)
                            await self._bounded_close(ws,1013)
                            break
                        buffered.append((data,size))
        except WebSocketDisconnect:
            log.info("QQ协议连接断开")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("QQ协议连接结束 type=%s",type(exc).__name__,exc_info=exc)
        finally:
            identify.cancel()
            await asyncio.gather(identify,return_exceptions=True)
            buffered.clear()
            if self.close_task is not None:
                await asyncio.gather(self.close_task,return_exceptions=True)
                self.close_task = None
            if self.socket is ws:
                self.socket = None
                self.verified = self.online = False
                for future in list(self.pending.values()):
                    if not future.done():
                        future.set_exception(ConnectionError("qq_disconnected"))
                await self._bounded_close(ws,1001)

    async def close(self):
        if self.socket:
            self.verified = self.online = False
            await self._bounded_close(self.socket,1001)
