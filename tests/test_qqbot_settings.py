"""Real dashboard controller/routes with isolated files and fake QQ transports."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway.config import cfg
from gateway.qqbot import QQRuntime
from gateway.qqbot_settings import QQSettings, SettingsError, qqbot_settings_routes
from tests.test_qqbot import Clock, Model, Transport, event


class QQSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.clock=Clock();self.instances=[]
        self.env={'QQBOT_ENABLED':'false','QQBOT_TOKEN':'x'*32,'QQBOT_ACCOUNT_ID':'12345',
            'QQBOT_OWNER_ID':'67890','QQBOT_ASSISTANT_ID':'role','QQBOT_TIMEZONE':'Asia/Shanghai',
            'QQBOT_ACTIVE_START':'09:00','QQBOT_ACTIVE_END':'22:00','QQBOT_DEBOUNCE_SECONDS':'0',
            'QQBOT_STORE_PATH':str(self.root/'ledger.sqlite3')}
        self.app=Starlette();self.app.state.qqbot=None
        self.manager=self.make_manager();self.addAsyncCleanup(self.manager.shutdown)

    def factory(self,config,client):
        runtime=QQRuntime(config,model=Model(),transport=Transport(),exporter=lambda *a:None,now=self.clock)
        self.instances.append(runtime);return runtime

    def make_manager(self,env=None):
        return QQSettings(self.app,None,env=self.env if env is None else env,path=self.root/'switches.json',runtime_factory=self.factory)

    async def test_disabled_default_creates_no_files_runtime_or_message_tasks(self):
        await self.manager.startup();s=self.manager.snapshot()
        self.assertFalse(s['enabled']);self.assertFalse(s['proactive_enabled'])
        self.assertTrue(s['configured']);self.assertEqual(self.instances,[])
        self.assertEqual(list(self.root.iterdir()),[])

    async def test_saved_choices_restore_and_override_environment_after_restart(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        self.assertEqual(json.loads((self.root/'switches.json').read_text()),{'enabled':True,'proactive_enabled':True,'daily_limit':3})
        await self.manager.shutdown()
        second=self.make_manager();self.addAsyncCleanup(second.shutdown);await second.startup()
        self.assertTrue(second.snapshot()['enabled']);self.assertTrue(second.runtime.config.proactive_enabled)
        await second.save({'enabled':False});await second.shutdown()
        third=self.make_manager({**self.env,'QQBOT_ENABLED':'true','QQBOT_PROACTIVE_ENABLED':'true'})
        self.addAsyncCleanup(third.shutdown);await third.startup()
        self.assertFalse(third.snapshot()['enabled']);self.assertIsNone(third.runtime)

    async def test_master_off_stops_runtime_and_clears_both_flags(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        runtime=self.manager.runtime
        result=await self.manager.save({'enabled':False})
        self.assertFalse(result['enabled']);self.assertFalse(result['proactive_enabled'])
        self.assertTrue(runtime.closed);self.assertTrue(runtime.stopped);self.assertIsNone(self.app.state.qqbot)
        self.assertFalse(await runtime.ingest(event(1,'while disabled')))
        await runtime.stop()  # Closing a runtime twice is safe.

    async def test_proactive_requires_master_and_explicit_schedule(self):
        with self.assertRaises(SettingsError) as ctx: await self.manager.save({'proactive_enabled':True})
        self.assertEqual(ctx.exception.status,409)
        await self.manager.save({'enabled':True})
        self.manager.env.pop('QQBOT_TIMEZONE')
        with self.assertRaises(SettingsError) as ctx: await self.manager.save({'proactive_enabled':True})
        self.assertEqual(ctx.exception.status,422)
        self.assertFalse(self.manager.runtime.config.proactive_enabled)

    async def test_missing_identity_is_visible_and_cannot_enable(self):
        self.manager.env.pop('QQBOT_TOKEN');self.manager.env.pop('QQBOT_OWNER_ID')
        state=self.manager.snapshot()
        self.assertFalse(state['configured']);self.assertIn('连接令牌',state['configuration_note'])
        self.assertIn('主人QQ号',state['configuration_note'])
        with self.assertRaises(SettingsError): await self.manager.save({'enabled':True})
        self.assertEqual(self.instances,[]);self.assertFalse((self.root/'switches.json').exists())

    async def test_file_save_failure_never_changes_runtime_or_old_file(self):
        await self.manager.save({'enabled':True});runtime=self.manager.runtime
        before=(self.root/'switches.json').read_bytes()
        with patch('gateway.qqbot_settings.os.replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(SettingsError): await self.manager.save({'enabled':False})
        self.assertTrue(self.manager.snapshot()['enabled']);self.assertIs(self.manager.runtime,runtime)
        self.assertFalse(runtime.stopped);self.assertEqual((self.root/'switches.json').read_bytes(),before)
        self.assertEqual(list(self.root.glob('switches.json.*')),[])

    async def test_corrupt_saved_file_fails_closed_and_never_uses_enabled_env(self):
        (self.root/'switches.json').write_text('{broken',encoding='utf-8')
        manager=self.make_manager({**self.env,'QQBOT_ENABLED':'true'})
        self.addAsyncCleanup(manager.shutdown);await manager.startup()
        state=manager.snapshot();self.assertFalse(state['enabled']);self.assertFalse(state['writable'])
        self.assertTrue(state['error']);self.assertEqual(self.instances,[])

    async def test_start_failure_is_saved_but_visible_and_can_be_turned_off(self):
        self.manager.runtime_factory=lambda *a: (_ for _ in ()).throw(OSError('store unavailable'))
        with self.assertRaises(SettingsError): await self.manager.save({'enabled':True})
        state=self.manager.snapshot()
        self.assertTrue(state['enabled']);self.assertFalse(state['running']);self.assertTrue(state['error'])
        self.assertFalse((await self.manager.save({'enabled':False}))['enabled'])

    async def test_switches_do_not_clear_owner_pause_and_keep_private_chat(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        runtime=self.manager.runtime;runtime.store.control('pause',self.clock())
        await self.manager.save({'proactive_enabled':False});await self.manager.save({'proactive_enabled':True})
        self.assertTrue(runtime.store.state()['paused'])
        self.assertTrue(await runtime.ingest(event(1,'private question')))
        await runtime.reply_once();await runtime.send_once()
        self.assertEqual(len(runtime.transport.sent),1)

    async def test_off_on_invalidates_queued_and_delayed_unsubmitted_proactive(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        runtime=self.manager.runtime;state=runtime.store.state();now=self.clock()
        runtime.store.queue('stale','proactive',[],'old greeting',now,now+300,revision=state['revision'])
        task=runtime.store.claim_send(now,owner=runtime.owner)
        runtime.store.conn.execute("CREATE TRIGGER fail_queue BEFORE UPDATE ON outbox WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT,'temporary'); END")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError): runtime.store.finish_send(task['id'],'queued',now)
        await self.manager.save({'proactive_enabled':False});await self.manager.save({'proactive_enabled':True})
        runtime.store.conn.execute('DROP TRIGGER fail_queue');await runtime.send_once()
        self.assertEqual(runtime.store.conn.execute("SELECT status FROM outbox WHERE task_key='stale'").fetchone()[0],'failed')
        self.assertEqual(runtime.transport.sent,[])

    async def test_master_off_on_never_restores_old_queued_greeting(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        runtime=self.manager.runtime;now=self.clock();revision=runtime.store.state()['revision']
        runtime.store.queue('old','proactive',[],'old greeting',now,now+300,revision=revision)
        await self.manager.save({'enabled':False});await self.manager.save({'enabled':True})
        await self.manager.save({'proactive_enabled':True});await self.manager.runtime.send_once()
        self.assertEqual(self.manager.runtime.transport.sent,[])
        self.assertEqual(self.manager.runtime.store.conn.execute("SELECT status FROM outbox WHERE task_key='old'").fetchone()[0],'failed')

    async def test_shutdown_does_not_allow_late_enable(self):
        await self.manager.save({'enabled':True});await self.manager.shutdown()
        with self.assertRaises(SettingsError): await self.manager.save({'enabled':True})
        self.assertIsNone(self.app.state.qqbot)

    async def test_proactive_off_cancels_generation_and_private_chat_remains_available(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        runtime=self.manager.runtime
        await runtime.ingest(event(1,'hello'));await runtime.reply_once();await runtime.send_once()
        self.clock.advance(1900);runtime.activate();runtime.model.wait=asyncio.Event()
        task=asyncio.create_task(runtime.proactive_once())
        await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertIsNotNone(runtime.proactive_generation)
        await self.manager.save({'proactive_enabled':False})
        await asyncio.wait_for(task,0.5)
        self.assertFalse(runtime.store.open_proactive());self.assertFalse(runtime.generate_lock.locked())
        runtime.model.wait=None
        await runtime.ingest(event(2,'still here'));await runtime.reply_once();await runtime.send_once()
        self.assertEqual(len(runtime.transport.sent),2)

    async def test_noop_save_preserves_current_proactive_generation(self):
        await self.manager.save({'enabled':True});await self.manager.save({'proactive_enabled':True})
        runtime=self.manager.runtime
        pending=asyncio.create_task(asyncio.Event().wait());runtime.proactive_generation=pending
        revision=runtime.store.state()['revision']
        try:
            await self.manager.save({'enabled':True})
            self.assertFalse(pending.cancelling());self.assertEqual(runtime.store.state()['revision'],revision)
        finally:
            pending.cancel();await asyncio.gather(pending,return_exceptions=True);runtime.proactive_generation=None

    async def test_late_lease_acquisition_discards_old_greeting(self):
        from gateway.qqbot_store import QQStore
        store=QQStore(self.env['QQBOT_STORE_PATH'],json.dumps(['12345','67890','role']))
        now=self.clock();store.acquire('prior-process',now)
        store.queue('stale','proactive',[],'old greeting',now,now+300,revision=store.state()['revision'])
        store.close()
        await self.manager.save({'enabled':True});runtime=self.manager.runtime
        self.assertFalse(runtime.leader)
        self.clock.advance(60)
        self.assertTrue(runtime.activate())
        await self.manager.save({'proactive_enabled':True});await runtime.send_once()
        self.assertEqual(runtime.transport.sent,[])
        self.assertEqual(runtime.store.conn.execute("SELECT status FROM outbox WHERE task_key='stale'").fetchone()[0],'failed')

    async def test_concurrent_changes_keep_file_runtime_and_flags_consistent(self):
        await self.manager.save({'enabled':True})
        await asyncio.gather(self.manager.save({'proactive_enabled':True}),self.manager.save({'enabled':False}))
        self.assertEqual(self.manager.flags,{'enabled':False,'proactive_enabled':False,'daily_limit':3})
        self.assertEqual(json.loads(self.manager.path.read_text()),self.manager.flags)
        self.assertIsNone(self.app.state.qqbot);self.assertIsNone(self.manager.runtime)

    async def test_shutdown_during_save_never_restarts_runtime(self):
        import threading
        entered,release=threading.Event(),threading.Event();write=self.manager._write
        def delayed_write(flags):
            entered.set()
            if not release.wait(2): raise TimeoutError('test save stalled')
            write(flags)
        with patch.object(self.manager,'_write',side_effect=delayed_write):
            save=asyncio.create_task(self.manager.save({'enabled':True}))
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            closing=asyncio.create_task(self.manager.shutdown());await asyncio.sleep(0)
            release.set();await save;await closing
        self.assertIsNone(self.manager.runtime);self.assertEqual(self.instances,[])

    async def test_status_read_failure_does_not_hide_switches(self):
        await self.manager.save({'enabled':True})
        with patch.object(self.manager.runtime,'status',side_effect=OSError('read failure')):
            state=self.manager.snapshot()
        self.assertTrue(state['enabled']);self.assertTrue(state['writable']);self.assertTrue(state['error'])

    async def test_legacy_switch_file_accepts_env_limit_and_saved_limit_overrides_env(self):
        self.manager.path.write_text(json.dumps({'enabled':False,'proactive_enabled':False}))
        manager=self.make_manager({**self.env,'QQBOT_DAILY_LIMIT':'7'})
        self.addAsyncCleanup(manager.shutdown)
        self.assertEqual(manager.snapshot()['daily_limit'],7)
        await manager.save({'daily_limit':30})
        restored=self.make_manager({**self.env,'QQBOT_DAILY_LIMIT':'5'})
        self.addAsyncCleanup(restored.shutdown)
        self.assertEqual(restored.snapshot()['daily_limit'],30)
        self.assertFalse(restored.snapshot()['enabled'])
        self.assertFalse((self.root/'ledger.sqlite3').exists())

    async def test_daily_limit_hot_apply_keeps_connection_and_used_count(self):
        await self.manager.save({'enabled':True,'proactive_enabled':True})
        runtime=self.manager.runtime;transport=runtime.transport;now=self.clock()
        from datetime import datetime
        from zoneinfo import ZoneInfo
        state=runtime.store.state()
        state.update(day=datetime.fromtimestamp(now,ZoneInfo(runtime.config.timezone)).date().isoformat(),count=3)
        runtime.store._save_state(state)
        self.assertEqual(runtime._proactive_timing_error(state,now),'daily_limit')
        await self.manager.save({'daily_limit':30})
        self.assertIs(self.manager.runtime,runtime);self.assertIs(runtime.transport,transport)
        self.assertFalse(runtime.stopped);self.assertTrue(runtime.config.proactive_enabled)
        self.assertEqual(runtime.store.state()['count'],3)
        self.assertIsNone(runtime._proactive_timing_error(runtime.store.state(),now))
        state=runtime.store.state();state['count']=30;runtime.store._save_state(state)
        self.assertEqual(runtime._proactive_timing_error(state,now),'daily_limit')
        await self.manager.save({'daily_limit':2})
        self.assertEqual(runtime.store.state()['count'],30)
        self.assertEqual(runtime._proactive_timing_error(runtime.store.state(),now),'daily_limit')

    async def test_limit_change_invalidates_old_unsent_generation(self):
        await self.manager.save({'enabled':True,'proactive_enabled':True})
        runtime=self.manager.runtime;now=self.clock();revision=runtime.store.state()['revision']
        async with runtime.send_lock:
            runtime.store.queue('prior-limit','proactive',[],'old greeting',now,now+300,revision=revision)
            await self.manager.save({'daily_limit':30})
        await runtime.send_once()
        self.assertFalse(runtime.store.open_proactive());self.assertEqual(runtime.transport.sent,[])
        self.assertGreater(runtime.store.state()['revision'],revision)

    async def test_invalid_limit_or_failed_write_preserves_file_and_runtime(self):
        await self.manager.save({'enabled':True});runtime=self.manager.runtime
        before=self.manager.path.read_bytes()
        for value in [0,101,True,30.5,'30',None]:
            with self.subTest(value=value),self.assertRaises(SettingsError):
                await self.manager.save({'daily_limit':value})
        with patch('gateway.qqbot_settings.os.replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(SettingsError):await self.manager.save({'daily_limit':30})
        self.assertEqual(self.manager.path.read_bytes(),before)
        self.assertEqual(self.manager.snapshot()['daily_limit'],3)
        self.assertEqual(runtime.config.daily_limit,3)

    async def test_saved_invalid_daily_limit_fails_closed(self):
        for value in [True,0,101,'30',30.5]:
            with self.subTest(value=value):
                self.manager.path.write_text(json.dumps({'enabled':False,'proactive_enabled':False,'daily_limit':value}))
                manager=self.make_manager();self.addAsyncCleanup(manager.shutdown)
                self.assertFalse(manager.snapshot()['writable'])
                await manager.startup();self.assertIsNone(manager.runtime)


class QQSettingsAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.app=Starlette(routes=qqbot_settings_routes);self.app.state.qqbot=None
        self.manager=QQSettings(self.app,None,env={},path=Path(self.tmp.name)/'settings.json')
        self.app.state.qqbot_settings=self.manager
        self.enterContext(patch.object(cfg,'GATEWAY_TOKEN','test-settings-token'))
        self.client=self.enterContext(TestClient(self.app));self.headers={'Authorization':'Bearer test-settings-token'}

    def test_auth_strict_payload_and_no_private_data(self):
        url='/admin/api/qqbot/settings'
        self.assertEqual(self.client.get(url).status_code,401)
        self.assertEqual(self.client.put(url,json={'enabled':False}).status_code,401)
        for data in [{},[],{'enabled':'false'},{'enabled':1},{'token':'leak'},{'proactive_enabled':None}]:
            with self.subTest(data=data):
                self.assertEqual(self.client.put(url,headers=self.headers,json=data).status_code,400)
        self.assertEqual(self.client.put(url,headers=self.headers,content='{bad').status_code,400)
        response=self.client.get(url,headers=self.headers)
        self.assertEqual(response.status_code,200);self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertFalse(response.json()['configured']);self.assertNotIn('test-settings-token',response.text)
        self.assertNotIn(self.tmp.name,response.text)

    def test_disabled_can_be_saved_without_account_or_qq_storage(self):
        response=self.client.put('/admin/api/qqbot/settings',headers=self.headers,json={'enabled':False})
        self.assertEqual(response.status_code,200);self.assertFalse(response.json()['enabled'])
        self.assertEqual(len(list(Path(self.tmp.name).iterdir())),1)

    def test_daily_limit_api_validates_auth_type_range_and_preserves_disabled(self):
        url='/admin/api/qqbot/settings'
        self.assertEqual(self.client.put(url,json={'daily_limit':30}).status_code,401)
        for value in [True,'30',30.5,None]:
            with self.subTest(value=value):
                self.assertEqual(self.client.put(url,headers=self.headers,json={'daily_limit':value}).status_code,400)
        for value in [0,101,-1]:
            with self.subTest(value=value):
                self.assertEqual(self.client.put(url,headers=self.headers,json={'daily_limit':value}).status_code,422)
        for value in [1,30,100]:
            response=self.client.put(url,headers=self.headers,json={'daily_limit':value})
            self.assertEqual(response.status_code,200);self.assertEqual(response.json()['daily_limit'],value)
            self.assertFalse(response.json()['enabled']);self.assertFalse(response.json()['proactive_enabled'])
        self.assertEqual(self.client.get(url,headers=self.headers).json()['daily_limit'],100)

    def test_http_switch_cycle_and_restart_use_real_runtime_controller(self):
        env={'QQBOT_TOKEN':'x'*32,'QQBOT_ACCOUNT_ID':'12345','QQBOT_OWNER_ID':'67890',
            'QQBOT_ASSISTANT_ID':'role','QQBOT_TIMEZONE':'Asia/Shanghai',
            'QQBOT_ACTIVE_START':'09:00','QQBOT_ACTIVE_END':'22:00',
            'QQBOT_STORE_PATH':str(Path(self.tmp.name)/'ledger.sqlite3')}
        def factory(config,client):
            return QQRuntime(config,model=Model(),transport=Transport(),exporter=lambda *a:None)
        @asynccontextmanager
        async def lifespan(app):
            manager=QQSettings(app,None,env=env,path=Path(self.tmp.name)/'switches.json',runtime_factory=factory)
            app.state.qqbot_settings=manager;await manager.startup()
            try: yield
            finally: await manager.shutdown()
        app=Starlette(routes=qqbot_settings_routes,lifespan=lifespan)
        url='/admin/api/qqbot/settings'
        with TestClient(app) as client:
            for payload in [{'enabled':True},{'proactive_enabled':True}]:
                response=client.put(url,headers=self.headers,json=payload)
                self.assertEqual(response.status_code,200);self.assertTrue(response.json()['running'])
            runtime=app.state.qqbot
            self.assertTrue(runtime.config.proactive_enabled)
        self.assertTrue(runtime.closed)
        with TestClient(app) as client:
            self.assertTrue(client.get(url,headers=self.headers).json()['proactive_enabled'])
            response=client.put(url,headers=self.headers,json={'enabled':False})
            self.assertEqual(response.status_code,200);self.assertFalse(response.json()['running'])
            self.assertFalse(response.json()['proactive_enabled']);self.assertIsNone(app.state.qqbot)
