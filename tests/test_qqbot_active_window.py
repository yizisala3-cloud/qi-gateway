"""Persisted sending windows, backward compatibility and exact midnight boundaries."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway.config import cfg
from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_settings import QQSettings, SettingsError, qqbot_settings_routes
from tests.test_qqbot import Clock, Model, Transport


class ActiveWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.clock=Clock();self.app=Starlette()
        self.env={'QQBOT_TOKEN':'x'*32,'QQBOT_ACCOUNT_ID':'12345','QQBOT_OWNER_ID':'67890',
            'QQBOT_ASSISTANT_ID':'role','QQBOT_STORE_PATH':str(self.root/'ledger.sqlite3')}
        self.manager=self.make_manager()

    def make_manager(self,env=None):
        manager=QQSettings(self.app,None,env=self.env if env is None else env,path=self.root/'settings.json',
            runtime_factory=lambda config,client:QQRuntime(config,model=Model(),transport=Transport(),
                exporter=lambda *args:None,now=self.clock))
        self.addAsyncCleanup(manager.shutdown)
        return manager

    async def test_defaults_unlock_proactive_without_creating_files(self):
        state=self.manager.snapshot()
        self.assertEqual((state['timezone'],state['active_start'],state['active_end']),('Asia/Shanghai','08:00','24:00'))
        self.assertTrue(state['proactive_configured']);self.assertEqual(list(self.root.iterdir()),[])
        await self.manager.save({'enabled':True,'proactive_enabled':True})
        self.assertEqual(self.manager.runtime.config.active_end,'24:00')

    async def test_old_file_retains_flags_limit_and_environment_initial_window(self):
        for old in ({'enabled':False,'proactive_enabled':False},
                    {'enabled':False,'proactive_enabled':False,'daily_limit':30}):
            with self.subTest(old=old):
                self.manager.path.write_text(json.dumps(old))
                manager=self.make_manager({**self.env,'QQBOT_ACTIVE_START':'09:30','QQBOT_ACTIVE_END':'23:00'})
                state=manager.snapshot()
                self.assertTrue(state['writable']);self.assertFalse(state['enabled'])
                self.assertEqual(state['daily_limit'],old.get('daily_limit',3))
                self.assertEqual((state['active_start'],state['active_end']),('09:30','23:00'))

    async def test_save_while_disabled_and_restart_override_environment(self):
        await self.manager.save({'daily_limit':30,'active_start':'22:30','active_end':'07:15'})
        self.assertFalse((self.root/'ledger.sqlite3').exists());self.assertIsNone(self.manager.runtime)
        restored=self.make_manager({**self.env,'QQBOT_ACTIVE_START':'01:00','QQBOT_ACTIVE_END':'02:00'})
        state=restored.snapshot()
        self.assertEqual((state['active_start'],state['active_end'],state['daily_limit']),('22:30','07:15',30))
        self.assertFalse(state['enabled']);self.assertFalse(state['proactive_enabled'])

    async def test_live_change_preserves_count_connection_and_cancels_unsent_greeting(self):
        await self.manager.save({'enabled':True,'proactive_enabled':True})
        runtime=self.manager.runtime;transport=runtime.transport;now=self.clock()
        state=runtime.store.state();state.update(day='2026-10-21',count=2)
        runtime.store._save_state(state);revision=state['revision']
        async with runtime.send_lock:
            runtime.store.queue('prior-window','proactive',[],'old greeting',now,now+300,revision=revision)
            await self.manager.save({'active_start':'22:00','active_end':'08:00'})
        await runtime.send_once()
        self.assertIs(self.manager.runtime,runtime);self.assertIs(runtime.transport,transport)
        self.assertTrue(runtime.config.proactive_enabled);self.assertEqual(runtime.store.state()['count'],2)
        self.assertFalse(runtime.store.open_proactive());self.assertEqual(transport.sent,[])
        self.assertFalse(runtime._active(now)[0])

    async def test_invalid_times_and_disk_failure_preserve_saved_configuration(self):
        await self.manager.save({'enabled':True});runtime=self.manager.runtime;before=self.manager.path.read_bytes()
        for payload in ({'active_start':'24:00'},{'active_end':'24:01'},{'active_end':'25:00'},
                        {'active_start':'8:00'},{'active_start':'08.00'},{'active_start':'０８:００'},
                        {'active_end':None},{'active_end':True}):
            with self.subTest(payload=payload),self.assertRaises(SettingsError):await self.manager.save(payload)
        with patch('gateway.qqbot_settings.os.replace',side_effect=OSError('disk unavailable')):
            with self.assertRaises(SettingsError):await self.manager.save({'active_end':'23:00'})
        self.assertEqual(self.manager.path.read_bytes(),before);self.assertEqual(runtime.config.active_end,'24:00')

    async def test_invalid_saved_time_fails_closed(self):
        self.manager.path.write_text(json.dumps({'enabled':True,'proactive_enabled':True,'active_end':'24:01'}))
        restored=self.make_manager();self.assertFalse(restored.snapshot()['writable'])
        await restored.startup();self.assertIsNone(restored.runtime)

    async def test_default_window_boundaries_and_midnight_expiry(self):
        await self.manager.save({'enabled':True})
        runtime=self.manager.runtime
        def stamp(hour,minute=0,second=0):
            return datetime(2026,10,21,hour,minute,second,tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()
        for hour,minute,active in ((0,0,False),(7,59,False),(8,0,True),(23,59,True)):
            self.assertEqual(runtime._active(stamp(hour,minute))[0],active)
        self.assertEqual(runtime._proactive_expiry(stamp(23,59,30)),stamp(0)+86400)
        self.assertEqual(runtime._proactive_expiry(stamp(10)),stamp(10)+runtime.config.proactive_ttl)


class WindowAPITests(unittest.TestCase):
    def test_schedule_types_validation_auth_and_persistence(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(cfg,'GATEWAY_TOKEN','window-test-token'):
            app=Starlette(routes=qqbot_settings_routes)
            manager=QQSettings(app,None,env={},path=Path(folder)/'settings.json');app.state.qqbot_settings=manager
            url='/admin/api/qqbot/settings';headers={'Authorization':'Bearer window-test-token'}
            with TestClient(app) as client:
                self.assertEqual(client.put(url,json={'active_end':'24:00'}).status_code,401)
                for value in (None,True,123,['24:00']):
                    self.assertEqual(client.put(url,headers=headers,json={'active_end':value}).status_code,400)
                for value in ('24:01','25:00','','8:00'):
                    self.assertEqual(client.put(url,headers=headers,json={'active_end':value}).status_code,422)
                response=client.put(url,headers=headers,json={'active_start':'08:00','active_end':'24:00'})
                self.assertEqual(response.status_code,200);self.assertEqual(response.headers['Cache-Control'],'no-store')
                state=client.get(url,headers=headers).json();self.assertEqual(state['active_end'],'24:00')
                self.assertEqual(state['timezone'],'Asia/Shanghai');self.assertFalse(state['enabled'])
                self.assertEqual(json.loads(manager.path.read_text())['active_start'],'08:00')
