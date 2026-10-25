"""Durable proactive pacing, fault stops, manual recovery and captured errors."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from email.utils import format_datetime
import json
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import logging_events
from gateway.config import cfg
from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_proactive import QQModelHTTPError, parse_retry_after
from gateway.qqbot_settings import QQSettings, qqbot_settings_routes
from tests.test_qqbot import Clock, Model, Transport, event


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=Clock();self.model=Model();self.transport=Transport()
        self.failure_calls=0
        self.config=QQConfig(enabled=True,proactive_enabled=True,account_id='12345',owner_id='67890',
            assistant_id='role',token='x'*32,store_path=str(Path(self.tmp.name)/'state.sqlite3'),
            active_start='00:00',active_end='00:00',quiet_seconds=1,cooldown_seconds=60,check_seconds=300)
        self.runtime=self.make_runtime();self.addAsyncCleanup(self.close_runtime)
        self.clock.on_advance=lambda:self.runtime.store.acquire(self.runtime.owner,self.clock())

    def make_runtime(self):
        runtime=QQRuntime(self.config,model=self.model,transport=self.transport,now=self.clock,exporter=lambda *a:None)
        runtime.activate();return runtime

    async def close_runtime(self):
        await self.runtime.stop()

    async def prepare(self):
        await self.runtime.ingest(event(1,'hello'));self.clock.advance(4)
        await self.runtime.reply_once();await self.runtime.send_once();self.clock.advance(2)

    async def fail_evaluation(self, error=None):
        async def generate(*args,**kwargs):
            self.failure_calls+=1
            raise error or httpx.ReadTimeout('test timeout')
        self.runtime.model.generate=generate
        await self.runtime.proactive_once()

    def advance_to_due(self):
        due=self.runtime.store.evaluation_state()['next_check_at']
        self.clock.advance(max(0,due-self.clock()))

    async def stop_by_five_errors(self):
        await self.prepare()
        for i,delay in enumerate([300,900,3600,7200,None],1):
            await self.fail_evaluation()
            state=self.runtime.store.evaluation_state()
            self.assertEqual(state['failure_streak'],i)
            if delay is not None:
                self.assertEqual(state['next_check_at']-self.clock(),delay)
                self.advance_to_due()
        self.assertTrue(self.runtime.evaluation_status()['fault_stopped'])

    async def test_silent_backoff_ends_at_one_hour_and_new_input_resets_only_silence(self):
        await self.prepare();self.model.result={'send':False,'text':'','sticker_ids':[]}
        for delay in [600,1200,2400,3600,3600]:
            await self.runtime.proactive_once();state=self.runtime.store.evaluation_state()
            self.assertEqual(state['next_check_at']-self.clock(),delay)
            calls=len(self.model.calls);self.clock.advance(delay-1);await self.runtime.proactive_once()
            self.assertEqual(len(self.model.calls),calls);self.clock.advance(1)
        await self.runtime.ingest(event(2,'new topic'))
        self.assertEqual(self.runtime.store.evaluation_state()['silent_streak'],0)
        self.assertEqual(self.runtime.store.evaluation_state()['next_check_at'],0)
        self.assertFalse(self.runtime.store.evaluation_state()['fault_stopped'])

    async def test_five_errors_stop_and_messages_or_qq_resume_do_not_reenable(self):
        await self.stop_by_five_errors()
        original=self.runtime.store.evaluation_state()
        self.clock.advance(86400);await self.runtime.proactive_once()
        await self.runtime.ingest(event(2,'恢复主动消息'))
        await self.runtime.send_once()
        current=self.runtime.store.evaluation_state()
        self.assertTrue(current['fault_stopped']);self.assertEqual(current['failure_streak'],5)
        self.assertEqual(current['generation'],original['generation'])
        self.assertEqual(self.failure_calls,5)
        self.assertIn('配置页',self.runtime.store.history()[-1]['content'])

    async def test_fault_survives_restart_and_manual_recovery_keeps_switches_and_count(self):
        await self.stop_by_five_errors();before=self.runtime.evaluation_status()
        schedule=self.runtime.store.state();schedule['count']=7;self.runtime.store._save_state(schedule)
        await self.runtime.stop();self.transport=Transport();self.runtime=self.make_runtime()
        self.assertTrue(self.runtime.evaluation_status()['fault_stopped'])
        self.runtime.config=replace(self.config,proactive_enabled=False)
        self.runtime.resume_evaluation(before['fault_version'])
        state=self.runtime.evaluation_status()
        self.assertFalse(state['fault_stopped']);self.assertEqual(state['failure_streak'],0)
        self.assertFalse(self.runtime.config.proactive_enabled)
        self.assertEqual(self.runtime.store.state()['count'],7)
        self.assertEqual(state['next_check_at']-self.clock(),300)
        with self.assertRaises(ValueError):self.runtime.resume_evaluation(before['fault_version'])

    async def test_normal_silence_clears_error_streak_and_new_message_keeps_retry_deadline(self):
        await self.prepare();await self.fail_evaluation()
        state=self.runtime.store.evaluation_state();due=state['next_check_at']
        await self.runtime.ingest(event(2,'new topic'))
        self.assertEqual(self.runtime.store.evaluation_state()['next_check_at'],due)
        self.assertEqual(self.runtime.store.evaluation_state()['failure_streak'],1)
        self.runtime.store.conn.execute("update inbox set status='done'")
        self.runtime.model=Model();self.runtime.model.result={'send':False,'text':'','sticker_ids':[]}
        self.advance_to_due();await self.runtime.proactive_once()
        self.assertEqual(self.runtime.evaluation_status()['failure_streak'],0)
        self.assertEqual(self.runtime.evaluation_status()['silent_streak'],1)

    async def test_auth_failure_stops_immediately_and_rate_limit_obeys_retry_after(self):
        await self.prepare();await self.fail_evaluation(QQModelHTTPError(429,7200))
        state=self.runtime.evaluation_status();self.assertEqual(state['next_check_at']-self.clock(),7200)
        self.assertFalse(state['fault_stopped']);self.advance_to_due()
        await self.fail_evaluation(QQModelHTTPError(401))
        self.assertTrue(self.runtime.evaluation_status()['fault_stopped'])
        self.assertEqual(self.runtime.evaluation_status()['last_error_code'],'auth_failed')

    async def test_slow_failure_delay_starts_after_completion(self):
        await self.prepare()
        async def slow(*args,**kwargs):
            self.clock.advance(700);raise httpx.ReadTimeout('late timeout')
        self.runtime.model.generate=slow;await self.runtime.proactive_once()
        self.assertEqual(self.runtime.evaluation_status()['next_check_at']-self.clock(),300)

    async def test_custom_base_interval_is_measured_after_slow_request_finishes(self):
        await self.prepare();self.runtime.config=replace(self.config,check_seconds=1200)
        async def slow(*args,**kwargs):
            self.clock.advance(1300);raise httpx.ReadTimeout('late timeout')
        self.runtime.model.generate=slow;await self.runtime.proactive_once()
        self.assertEqual(self.runtime.evaluation_status()['next_check_at']-self.clock(),1200)

    async def test_cancel_for_new_input_does_not_count_or_restore_old_silent_streak(self):
        await self.prepare();self.model.wait=asyncio.Event()
        pending=asyncio.create_task(self.runtime.proactive_once());await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertIsNotNone(self.runtime.proactive_generation)
        await self.runtime.ingest(event(2,'new message'));await pending
        state=self.runtime.evaluation_status()
        self.assertEqual(state['failure_streak'],0);self.assertEqual(state['silent_streak'],0)
        self.assertFalse(state['in_flight']);self.assertFalse(state['fault_stopped'])

    async def test_restart_inflight_counts_interruption_without_retrying_immediately(self):
        await self.prepare()
        self.runtime.store.begin_evaluation(self.clock(),300,self.runtime.owner)
        await self.runtime.stop();self.transport=Transport();self.runtime=self.make_runtime()
        state=self.runtime.evaluation_status()
        self.assertFalse(state['in_flight']);self.assertEqual(state['failure_streak'],1)
        self.assertEqual(state['last_error_code'],'interrupted')
        self.assertEqual(state['next_check_at']-self.clock(),300)

    async def test_storage_failure_stops_extra_calls_and_private_ingest_survives_corrupt_evaluation(self):
        await self.prepare()
        self.runtime.store.conn.execute("insert or replace into meta values('proactive_evaluation','{broken')")
        before=len(self.model.calls)
        await self.runtime.proactive_once();await self.runtime.proactive_once()
        self.assertTrue(self.runtime.evaluation_status()['fault_stopped'])
        self.assertEqual(len(self.model.calls),before)
        self.assertTrue(await self.runtime.ingest(event(2,'ordinary chat')))

    async def test_sendable_result_resets_silence_without_counting_until_real_send(self):
        await self.prepare();self.model.result={'send':False,'text':'','sticker_ids':[]}
        await self.runtime.proactive_once();self.advance_to_due()
        self.model.result={'send':True,'text':'hello again','sticker_ids':[]}
        await self.runtime.proactive_once()
        self.assertEqual(self.runtime.evaluation_status()['silent_streak'],0)
        self.assertEqual(self.runtime.store.state()['count'],0)
        await self.runtime.send_once();self.assertEqual(self.runtime.store.state()['count'],1)

    async def test_logged_failure_is_qqbot_event_with_trace_and_safe_retry_context(self):
        await self.prepare();records=[]
        class Capture(logging.Handler):
            def emit(self,record):records.append(record)
        handler=Capture();logger=logging.getLogger('gateway.qqbot');logger.addHandler(handler)
        try:await self.fail_evaluation(httpx.ReadTimeout('Bearer private-token-12345678901234567890'))
        finally:logger.removeHandler(handler)
        normalized=[logging_events._build_event_from_record(record) for record in records]
        row=next(row for row in normalized if row and row['operation']=='proactive_evaluation')
        self.assertEqual(row['module'],'qqbot');self.assertEqual(row['safe_context']['attempts'],1)
        self.assertTrue(row['stack_trace']);self.assertEqual(row['safe_context']['timeout_seconds'],300)
        self.assertNotIn('private-token-12345678901234567890',json.dumps(row))

    async def test_collection_switch_controls_qqbot_capture_without_affecting_failure_state(self):
        await self.prepare();queue=logging_events._EventQueue(10)
        handler=logging_events.GatewayLogHandler();logger=logging.getLogger('gateway.qqbot')
        logger.addHandler(handler)
        try:
            with patch.object(logging_events,'_queue',queue),patch.object(logging_events,'_capture_active',False):
                await self.fail_evaluation()
            self.assertEqual(queue.depth(),0)
            self.advance_to_due()
            with patch.object(logging_events,'_queue',queue),patch.object(logging_events,'_capture_active',True):
                await self.fail_evaluation()
            captured=queue.drain(10)
            self.assertEqual(len(captured),1);self.assertEqual(captured[0]['module'],'qqbot')
            self.assertEqual(captured[0]['safe_context']['attempts'],2)
        finally:logger.removeHandler(handler)

    async def test_crash_during_fifth_attempt_stops_instead_of_resetting_failures(self):
        await self.prepare()
        for _ in range(4):await self.fail_evaluation();self.advance_to_due()
        attempt=self.runtime.store.begin_evaluation(self.clock(),300,self.runtime.owner)
        self.assertIsNotNone(attempt)
        await self.runtime.stop();self.transport=Transport();self.runtime=self.make_runtime()
        state=self.runtime.evaluation_status()
        self.assertTrue(state['fault_stopped']);self.assertEqual(state['failure_streak'],5)

    async def test_late_result_after_new_message_cannot_change_fault_or_silence_state(self):
        await self.prepare()
        for mid,late in [(2,{'send':False,'text':'','sticker_ids':[]}),(3,QQModelHTTPError(401))]:
            self.runtime.store.conn.execute("update inbox set status='done'")
            self.clock.advance(400)
            async def ignore_cancel(*args,**kwargs):
                try:await asyncio.Event().wait()
                except asyncio.CancelledError:
                    if isinstance(late,Exception):raise late
                    return late
            self.runtime.model.generate=ignore_cancel
            pending=asyncio.create_task(self.runtime.proactive_once());await asyncio.sleep(0);await asyncio.sleep(0)
            self.assertIsNotNone(self.runtime.proactive_generation)
            await self.runtime.ingest(event(mid,'changed topic'));await pending
            state=self.runtime.evaluation_status()
            self.assertEqual(state['failure_streak'],0);self.assertEqual(state['silent_streak'],0)
            self.assertFalse(state['fault_stopped']);self.assertFalse(self.runtime.store.open_proactive())

    async def test_state_write_failure_blocks_further_model_calls_until_manual_recovery(self):
        await self.prepare();calls=0
        async def silent(*args,**kwargs):
            nonlocal calls
            calls+=1
            return {'send':False,'text':'','sticker_ids':[]}
        self.runtime.model.generate=silent
        with patch.object(self.runtime.store,'finish_evaluation',side_effect=OSError('disk full')):
            await self.runtime.proactive_once()
        self.assertTrue(self.runtime.evaluation_status()['fault_stopped'])
        self.clock.advance(10000);await self.runtime.proactive_once();self.assertEqual(calls,1)
        # A finished provider request with an uncommitted result remains durable
        # in-flight; recovery must not guess whether it completed successfully.
        self.assertTrue(self.runtime.store.evaluation_state()['in_flight'])
        self.runtime.resume_evaluation(self.runtime.evaluation_status()['fault_version'])
        self.assertFalse(self.runtime.evaluation_status()['fault_stopped'])
        self.assertFalse(self.runtime.evaluation_status()['in_flight'])
        self.assertEqual(self.runtime.evaluation_status()['next_check_at']-self.clock(),300)


class RetryAfterTests(unittest.TestCase):
    def test_seconds_date_and_invalid_headers(self):
        self.assertEqual(parse_retry_after('900'),900)
        self.assertEqual(parse_retry_after('-5'),0)
        now=1770000000
        self.assertEqual(parse_retry_after(format_datetime(datetime.fromtimestamp(now+600,timezone.utc)),now),600)
        for value in ['nan','inf','not-a-date','',None]:self.assertIsNone(parse_retry_after(value,now))


class ResumeApiTests(unittest.TestCase):
    def test_auth_strict_version_and_offline_master(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(cfg,'GATEWAY_TOKEN','resume-test-token'):
            app=Starlette(routes=qqbot_settings_routes);app.state.qqbot=None
            app.state.qqbot_settings=QQSettings(app,None,env={},path=Path(tmp)/'settings.json')
            headers={'Authorization':'Bearer resume-test-token'};url='/admin/api/qqbot/proactive/resume'
            with TestClient(app) as client:
                self.assertEqual(client.post(url,json={'fault_version':0}).status_code,401)
                for payload in [{},{'fault_version':True},{'fault_version':-1},{'fault_version':0,'enabled':True}]:
                    self.assertEqual(client.post(url,headers=headers,json=payload).status_code,400)
                response=client.post(url,headers=headers,json={'fault_version':0})
                self.assertEqual(response.status_code,409)
                self.assertEqual(response.headers['cache-control'],'no-store')

    def test_manual_resume_preserves_flags_and_restart_and_rejects_stale_fault(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(cfg,'GATEWAY_TOKEN','resume-test-token'):
            root=Path(tmp);clock=Clock()
            env={'QQBOT_ENABLED':'true','QQBOT_TOKEN':'x'*32,'QQBOT_ACCOUNT_ID':'12345','QQBOT_OWNER_ID':'67890',
                'QQBOT_ASSISTANT_ID':'role','QQBOT_DAILY_LIMIT':'30','QQBOT_STORE_PATH':str(root/'ledger.sqlite3')}
            @asynccontextmanager
            async def lifespan(app):
                manager=QQSettings(app,None,env=env,path=root/'settings.json',runtime_factory=lambda config,client:
                    QQRuntime(config,model=Model(),transport=Transport(),now=clock,exporter=lambda *a:None))
                app.state.qqbot_settings=manager;await manager.startup()
                try:yield
                finally:await manager.shutdown()
            app=Starlette(routes=qqbot_settings_routes,lifespan=lifespan)
            headers={'Authorization':'Bearer resume-test-token'};url='/admin/api/qqbot/proactive/resume'
            with TestClient(app) as client:
                runtime=app.state.qqbot
                with runtime.store.transaction():
                    state=runtime.store.evaluation_state();state.update(fault_stopped=True,failure_streak=5,generation=8)
                    runtime.store._save_evaluation(state)
                    used=runtime.store.state();used['count']=7;runtime.store._save_state(used)
                self.assertEqual(client.post(url,headers=headers,json={'fault_version':7}).status_code,409)
                result=client.post(url,headers=headers,json={'fault_version':8})
                self.assertEqual(result.status_code,200);data=result.json()
                self.assertFalse(data['proactive_evaluation']['fault_stopped'])
                self.assertFalse(data['proactive_enabled']);self.assertTrue(data['enabled']);self.assertEqual(data['daily_limit'],30)
                self.assertEqual(runtime.store.state()['count'],7)
                self.assertEqual(client.post(url,headers=headers,json={'fault_version':8}).status_code,409)
                # Toggling and reloading must keep a later fault stopped.
                with runtime.store.transaction():
                    state=runtime.store.evaluation_state();state.update(fault_stopped=True,failure_streak=5,generation=10)
                    runtime.store._save_evaluation(state)
                self.assertEqual(client.put('/admin/api/qqbot/settings',headers=headers,json={'enabled':False}).status_code,200)
                self.assertEqual(client.put('/admin/api/qqbot/settings',headers=headers,json={'enabled':True}).status_code,200)
                self.assertTrue(client.get('/admin/api/qqbot/settings',headers=headers).json()['proactive_evaluation']['fault_stopped'])
            with TestClient(app) as client:
                self.assertTrue(client.get('/admin/api/qqbot/settings',headers=headers).json()['proactive_evaluation']['fault_stopped'])
