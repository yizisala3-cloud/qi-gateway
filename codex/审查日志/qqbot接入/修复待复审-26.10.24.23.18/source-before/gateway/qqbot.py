"""Owner-only private chat and proactive runtime. Disabled unless explicitly configured."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute

from .qqbot_config import QQConfig
from .qqbot_memory import export_message
from .qqbot_model import QQModel
from .qqbot_stickers import StickerCatalog, split_sticker_summaries
from .qqbot_store import QQStore
from .qqbot_transport import OneBotConnection, NotSubmitted, SendCancelled
from .qqbot_proactive import initial_state, public_status, classify_failure, ERROR_MESSAGES
from .logging_events import emit_error

log = logging.getLogger("gateway.qqbot")


def control_action(text):
    value = re.sub(r"[。！!\s]+$","",text.strip())
    return {"暂停主动消息":"pause","暂停主动聊天":"pause","/主动消息 暂停":"pause",
            "恢复主动消息":"resume","恢复主动聊天":"resume","/主动消息 恢复":"resume",
            "今天先别找我":"today","今天别主动找我":"today",
            "主动消息状态":"status","查看主动消息状态":"status","/主动消息 状态":"status"}.get(value)


def multiple_stickers(text):
    return bool(re.search(r"(?:多发|连发|发(?:给我)?几[张个]|[两二三四五六七八九十2-9]\s*[张个]).{0,12}(?:表情|猫猫|狗狗)|(?:表情包?).{0,10}(?:多[张个]|[两二三四五六七八九十2-9][张个])",text))


class QQRuntime:
    def __init__(self, config, client=None, *, store=None, model=None, catalog=None,
                 transport=None, exporter=export_message, now=time.time):
        self.config, self.now, self.exporter = config, now, exporter
        self.catalog = catalog if catalog is not None else StickerCatalog(config.sticker_manifest)
        self.model = model if model is not None else QQModel(client,config,self.catalog)
        self.store = store if store is not None else QQStore(config.store_path,
            json.dumps([config.account_id,config.owner_id,config.assistant_id]))
        self.transport = transport if transport is not None else OneBotConnection(config,self.ingest,now,before_submit=self._before_submit)
        self.owner = uuid4().hex
        self.leader = False
        self.stopped = False
        self.closed = False
        self.discard_proactive_on_activation = False
        self.tasks = []
        self.generate_lock = asyncio.Lock()
        self.send_lock = asyncio.Lock()
        self.export_lock = asyncio.Lock()
        self.export_pending = None
        self.proactive_generation = None
        self.sending_task = None
        self.proactive_state_error = False
        self.last_evaluation_state = initial_state()

    def activate(self):
        self.leader = self.store.acquire(self.owner,self.now())
        if self.leader:
            self._recover_activation()
        return self.leader

    def _recover_activation(self):
        self._recover()
        try:
            recovered = self.store.recover_evaluation(self.now(),self.config.check_seconds,self.owner)
            if recovered:
                self.last_evaluation_state = recovered
                self._log_evaluation_failure(None,'interrupted',recovered)
        except Exception as exc:
            self._evaluation_storage_failed(exc)
        if self.discard_proactive_on_activation:
            self.store.invalidate_proactive('settings_activation')

    def _recover(self):
        self.store.recover(self.now(),datetime.fromtimestamp(self.now(),ZoneInfo(self.config.timezone)).date().isoformat())

    def start(self):
        self.activate()
        self.tasks = [asyncio.create_task(self._loop()),asyncio.create_task(self._send_loop()),
                      asyncio.create_task(self._lease_loop())]

    async def stop(self):
        if self.closed:
            return
        self.stopped = True
        await self.transport.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks,return_exceptions=True)
        if self.leader:
            self._recover()
            self.store.release(self.owner)
        self.store.close()
        self.closed = True

    def status(self):
        state = self.store.state()
        return {"enabled":True,"leader":self.leader,"connected":self.transport.ready,
                "proactive_enabled":self.config.proactive_enabled,"paused":state["paused"],
                "silence_until":state["silence_until"],"timezone":self.config.timezone,
                "outbox":self.store.counts(),"stickers":len(self.catalog.entries),
                "proactive_evaluation":self.evaluation_status()}

    def evaluation_status(self):
        if not self.proactive_state_error:
            try:
                self.last_evaluation_state = self.store.evaluation_state()
            except Exception as exc:
                self._evaluation_storage_failed(exc)
        result = public_status(self.last_evaluation_state)
        if self.proactive_state_error:
            result.update(fault_stopped=True,last_error_code='state_unavailable',
                last_error_message=ERROR_MESSAGES['state_unavailable'],next_check_at=0)
        return result

    def resume_evaluation(self, version):
        if self.proactive_generation is not None:
            raise ValueError('主动评估仍在结束，请稍后重试。')
        self.last_evaluation_state = self.store.resume_evaluation(self.now(),self.config.check_seconds,
            self.owner,version,force=self.proactive_state_error)
        self.proactive_state_error = False

    def _evaluation_storage_failed(self, exc):
        first = not self.proactive_state_error
        self.proactive_state_error = True
        if first:
            emit_error('qqbot','proactive_state','proactive_state_unavailable',
                'QQ主动评估状态不可用，已停止调用，请检查存储后到配置页恢复',
                exc=exc,outcome='failed',safe_context={'reason_code':'state_unavailable'})

    def _log_evaluation_failure(self, exc, code, evaluation, status=None):
        stopped = evaluation['fault_stopped']
        message = ERROR_MESSAGES[code] + ('；已停止调用，请到配置页恢复' if stopped else '；稍后按退避规则重试')
        emit_error('qqbot','proactive_evaluation','proactive_calls_stopped' if stopped else 'proactive_'+code,
            message,level='ERROR' if stopped else 'WARNING',exc=exc,
            outcome='failed' if stopped else 'degraded',upstream_status=status,
            safe_context={'attempts':evaluation['failure_streak'],'reason_code':code,
                'retry':not stopped,'timeout_seconds':max(0,evaluation['next_check_at']-self.now())})

    def _finish_evaluation(self, attempt, outcome, **failure):
        try:
            result = self.store.finish_evaluation(attempt,outcome,self.now(),self.config.check_seconds,
                self.owner,**failure)
            if result:
                self.last_evaluation_state = result
            return result
        except Exception as exc:
            self._evaluation_storage_failed(exc)
            return None

    async def ingest(self, event):
        c = self.config
        if (self.stopped or not self.leader or not self.store.owns(self.owner,self.now())
                or event.get("post_type")!="message" or event.get("message_type")!="private"
                or event.get("group_id") is not None or str(event.get("self_id"))!=c.account_id
                or str(event.get("user_id"))!=c.owner_id or event.get("sub_type") not in {None,"friend"}):
            return False
        mid = event.get("message_id")
        if not isinstance(mid,(int,str)) or isinstance(mid,bool) or not str(mid) or len(str(mid))>100:
            return False
        segments = event.get("message")
        # Array format preserves text/CQ boundaries; raw_message is deliberately not trusted.
        if isinstance(segments,list):
            text = "".join(s["data"]["text"] for s in segments if isinstance(s,dict)
                and s.get("type")=="text" and isinstance(s.get("data"),dict) and isinstance(s["data"].get("text"),str))
        elif isinstance(segments,str):
            text = re.sub(r"\[CQ:[^\]]*\]","",segments)
        else:
            return False
        text = text.strip()
        if not text or len(text)>8000:
            return False
        now = self.now()
        action = control_action(text)
        reply, tomorrow = None, 0
        if action:
            local = datetime.fromtimestamp(now,ZoneInfo(c.timezone))
            tomorrow = (local+timedelta(days=1)).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
            state = self.store.state()
            if action in {'resume','status'} and self.evaluation_status()['fault_stopped']:
                reply = '主动评估因故障已停止，请到配置页点击恢复调用；你仍可正常私聊。'
            elif not c.proactive_enabled:
                reply = "服务器尚未启用主动消息；你的控制选择已保存。"
            elif action=="pause":
                reply = "好，主动消息已暂停。你仍然可以正常和我聊天。"
            elif action=="resume":
                reply = "主动消息已恢复，会按允许的时段和最近交流情况安排。"
            elif action=="today":
                reply = "好，今天先不主动找你，明天再按设置安排。"
            else:
                reply = "主动消息当前"+("已暂停。" if state["paused"] else
                    "今天已静默。" if state["silence_until"]>now else "已开启。")
        if not self.store.accept(mid,text,now,action=action,silence_until=tomorrow,reply=reply):
            return False
        if self.proactive_generation is not None:
            self.proactive_generation.cancel()
        return True

    def _segments(self, result, query):
        text = result.get("text","")
        keys = result.get("sticker_ids",[])
        if not isinstance(text,str) or not isinstance(keys,list):
            raise ValueError("qq_invalid_generation")
        text, references = split_sticker_summaries(text)
        keys = [key for key in keys if isinstance(key,str) and key in self.catalog.entries]
        represented = Counter(keys)
        for reference in references:
            key = reference["id"]
            if key not in self.catalog.entries:
                continue
            if represented[key]:
                represented[key] -= 1
            else:
                keys.append(key)
        segments = [{"type":"text","data":{"text":text}}] if text else []
        summaries = [text] if text else []
        limit = 10 if multiple_stickers(query) else 1
        for key in keys[:limit]:
            if isinstance(key,str) and key in self.catalog.entries:
                try:
                    segments.append(self.catalog.segment(key))
                    summaries.append(self.catalog.summary(key))
                except (OSError,ValueError) as exc:
                    log.warning("QQ表情素材不可用 type=%s",type(exc).__name__,exc_info=exc)
        if not segments:
            raise ValueError("qq_empty_generation")
        return segments,"\n".join(summaries)

    async def reply_once(self):
        if self.generate_lock.locked():
            return
        async with self.generate_lock:
            batch = self.store.batch(self.now(),self.config.debounce_seconds,self.config.max_batch_wait,owner=self.owner)
            if not batch:
                return
            try:
                history = self.store.batch_history(batch,self.config.history_limit)
                selected = {r["history_id"] for r in batch}
                query = "\n".join(r["content"] for r in history if r["id"] in selected)
                try:
                    result = await self.model.generate(history,query)
                    if result.get("send",True) is False:
                        raise ValueError("qq_direct_request_silent")
                    segments, summary = self._segments(result,query)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("QQ私聊生成失败 type=%s",type(exc).__name__,exc_info=exc)
                    summary = "刚才这条消息暂时没能处理完成，你可以稍后再问我。"
                    segments = [{"type":"text","data":{"text":summary}}]
                self.store.queue(f"reply:{batch[0]['id']}:{batch[-1]['id']}","reply",segments,summary,
                                 self.now(),self.now()+86400,batch,owner=self.owner)
            finally:
                try:
                    self.store.release_batch(batch,self.owner,self.now())
                except Exception as exc:
                    log.error('QQ批次释放失败 first=%s last=%s type=%s',batch[0]['id'],batch[-1]['id'],type(exc).__name__,exc_info=exc)

    def _active(self, now):
        local = datetime.fromtimestamp(now,ZoneInfo(self.config.timezone))
        value = local.strftime("%H:%M")
        a,b = self.config.active_start,self.config.active_end
        return (a==b or (a<=value<b if a<b else value>=a or value<b)),local

    def _proactive_expiry(self, now):
        active, local = self._active(now)
        if not active:
            return now
        if self.config.active_start==self.config.active_end:
            return now+self.config.proactive_ttl
        hour, minute = map(int,self.config.active_end.split(":"))
        end = local.replace(hour=hour,minute=minute,second=0,microsecond=0)
        if end<=local:
            end += timedelta(days=1)
        return min(now+self.config.proactive_ttl,end.timestamp())

    def _proactive_timing_error(self, state, now):
        c = self.config
        if self.proactive_state_error or self.store.evaluation_state()['fault_stopped']:
            return 'evaluation_fault_stopped'
        active,local = self._active(now)
        if not c.proactive_enabled or not active:
            return 'outside_active_window'
        if now-state['last_incoming']<c.quiet_seconds:
            return 'owner_quiet_period'
        if state['day']==local.date().isoformat() and state['count']>=c.daily_limit:
            return 'daily_limit'
        if now-state['last_sent']<min(604800,c.cooldown_seconds*2**max(0,state['unanswered']-1)):
            return 'proactive_cooldown'
        return None

    async def proactive_once(self):
        c, now = self.config,self.now()
        if not c.proactive_enabled or self.proactive_state_error:
            return
        evaluation = self.evaluation_status()
        if evaluation['fault_stopped'] or evaluation['in_flight'] or evaluation['next_check_at'] > now:
            return
        state = self.store.state()
        if (not c.proactive_enabled or not self.transport.ready or self.generate_lock.locked()
                or self.store.pending() or state["paused"] or state["silence_until"]>now
                or self.store.open_proactive()
                or state["next_check"]>now or self._proactive_timing_error(state,now)):
            return
        async with self.generate_lock:
            try:
                attempt = self.store.begin_evaluation(now,c.check_seconds,self.owner)
                if attempt is None:
                    return
                self.last_evaluation_state = attempt
                history = self.store.conversation_history(c.history_limit)
            except Exception as exc:
                self._evaluation_storage_failed(exc)
                return
            if not history:
                self._finish_evaluation(attempt,'cancelled')
                return
            query = "\n".join(r["content"] for r in history if r["role"]=="user")[-1600:]
            try:
                self.proactive_generation = asyncio.create_task(self.model.generate(history,query,proactive=True))
                result = await self.proactive_generation
                if not isinstance(result,dict) or type(result.get('send')) is not bool:
                    raise ValueError('qq_invalid_proactive_result')
                if not result.get("send",False):
                    outcome = 'silent'
                else:
                    segments, summary = self._segments(result,"")
                    outcome = 'ready'
            except asyncio.CancelledError:
                self._finish_evaluation(attempt,'cancelled')
                if asyncio.current_task().cancelling():
                    raise
                return
            except Exception as exc:
                code,fatal,retry_after,status = classify_failure(exc)
                finished = self._finish_evaluation(attempt,'error',code=code,fatal=fatal,retry_after=retry_after)
                if finished and finished['outcome']=='error':
                    self._log_evaluation_failure(exc,code,finished,status)
                elif self.proactive_state_error:
                    failed = {**attempt,'fault_stopped':True,'failure_streak':min(5,attempt['failure_streak']+1),'next_check_at':0}
                    self._log_evaluation_failure(exc,code,failed,status)
                return
            finally:
                self.proactive_generation = None
            finished = self._finish_evaluation(attempt,outcome)
            if not finished or finished['outcome'] != 'ready':
                return
            current = self.now()
            active_now, _ = self._active(current)
            if active_now and self.transport.ready and not self.stopped:
                try:
                    self.store.queue("proactive:"+uuid4().hex,"proactive",segments,summary,current,
                        self._proactive_expiry(current),revision=attempt['attempt_revision'],owner=self.owner)
                except Exception as exc:
                    self._evaluation_storage_failed(exc)

    def _before_submit(self):
        try:
            now = self.now()
            if self.stopped or not self.leader or not self.store.owns(self.owner,now) or not self.transport.ready:
                raise NotSubmitted('sender_not_ready')
            reason = self.store.submission_error(self.sending_task['id'],now)
            if not reason and self.sending_task['kind']=='proactive':
                reason = self._proactive_timing_error(self.store.state(),now)
        except NotSubmitted:
            raise
        except Exception as exc:
            log.warning('QQ发送前资格读取失败 task=%s type=%s',self.sending_task['id'] if self.sending_task else None,type(exc).__name__,exc_info=exc)
            raise NotSubmitted('send_eligibility_unavailable') from exc
        if reason:
            raise SendCancelled(reason)

    async def send_once(self):
        if self.send_lock.locked():
            return
        async with self.send_lock:
            self.store.retry_receipts(self.now())
            if not self.transport.ready:
                return
            task = self.store.claim_send(self.now(),owner=self.owner)
            if not task:
                return
            self.sending_task = task
            status, mid, error = "unknown",None,"send_interrupted"
            try:
                self._before_submit()
                status,mid,error = await self.transport.send(json.loads(task["payload"]))
            except SendCancelled as exc:
                status,error = 'failed',str(exc)
            except NotSubmitted:
                status,error = "queued","not_submitted"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("QQ投递结果不明 type=%s",type(exc).__name__,exc_info=exc)
            finally:
                self.sending_task = None
                received = self.now()
                self.store.finish_send(task["id"],status,received,mid,error,
                    day=datetime.fromtimestamp(received,ZoneInfo(self.config.timezone)).date().isoformat(),
                    cooldown=self.config.cooldown_seconds)

    async def export_once(self):
        if self.export_lock.locked():
            return
        async with self.export_lock:
            rows = [self.export_pending[0]] if self.export_pending else self.store.exports(self.now())
            for row in rows:
                if self.export_pending is None:
                    job = asyncio.create_task(asyncio.to_thread(self.exporter,self.config,
                        self.store.export_key(row['message_key']),row))
                    self.export_pending = (row,job,False)
                _,job,timed_out = self.export_pending
                try:
                    # A timed-out sync RPC may still be running. Retain one job;
                    # never overlap it with retries or later conversation rows.
                    await asyncio.wait_for(asyncio.shield(job),self.config.export_timeout)
                except Exception as exc:
                    if not timed_out or job.done():
                        self.store.export_result(row['id'],False,self.now())
                        log.warning('QQ记忆同步等待重试 row=%s type=%s',row['id'],type(exc).__name__,exc_info=exc)
                    self.export_pending = None if job.done() else (row,job,True)
                    break
                else:
                    self.store.export_result(row['id'],True,self.now())
                    self.export_pending = None

    async def _export_loop(self):
        try:
            while not self.stopped:
                try:
                    if self.leader and self.store.owns(self.owner,self.now()):
                        await self.export_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error('QQ记忆后台循环失败 type=%s',type(exc).__name__,exc_info=exc)
                await asyncio.sleep(5)
        finally:
            if self.export_pending is not None:
                job = self.export_pending[1]
                job.cancel()
                await asyncio.gather(job,return_exceptions=True)

    async def _loop(self):
        export_task = asyncio.create_task(self._export_loop())
        try:
            while not self.stopped:
                try:
                    if self.leader:
                        await self.reply_once()
                        await self.proactive_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("QQ后台循环失败 type=%s",type(exc).__name__,exc_info=exc)
                await asyncio.sleep(0.2)
        finally:
            export_task.cancel()
            await asyncio.gather(export_task,return_exceptions=True)

    async def _lease_loop(self):
        while not self.stopped:
            try:
                previous = self.leader
                self.leader = self.store.acquire(self.owner,self.now())
                if self.leader and not previous:
                    self._recover_activation()
                elif previous and not self.leader:
                    await self.transport.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.leader = False
                log.error("QQ运行租约续期失败 type=%s",type(exc).__name__,exc_info=exc)
                await self.transport.close()
            await asyncio.sleep(5)

    async def _send_loop(self):
        while not self.stopped:
            try:
                if self.leader:
                    await self.send_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("QQ发送循环失败 type=%s",type(exc).__name__,exc_info=exc)
            await asyncio.sleep(0.2)


async def websocket_endpoint(ws):
    runtime = getattr(ws.app.state,"qqbot",None)
    if runtime is None or not runtime.leader or runtime.stopped or not runtime.store.owns(runtime.owner,runtime.now()):
        await ws.close(code=1008 if runtime is None else 1013)
        return
    await runtime.transport.serve(ws)


async def status_endpoint(request):
    from .main import verify_token
    if not verify_token(request):
        return JSONResponse({"error":"unauthorized"},status_code=401)
    runtime = getattr(request.app.state,"qqbot",None)
    return JSONResponse(runtime.status() if runtime else {"enabled":False})


qqbot_routes = [WebSocketRoute("/qqbot/onebot",websocket_endpoint),
                Route("/admin/api/qqbot/status",status_endpoint,methods=["GET"])]
