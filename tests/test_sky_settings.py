"""Isolated access settings, pause/cancel boundaries and delivery independence."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient
from starlette.routing import Route

from test_sky_companion import b, config, private_files, FakeDevice, screen, receipt_clock
from test_sky_delivery import server, delivery, commit
from gateway import main as gm, sky_settings as settings


@pytest.fixture
def store(monkeypatch):
    value={'raw':None,'save':True,'elevated':True,'writes':[]}
    monkeypatch.setattr(gm,'verify_token',lambda _:True)
    monkeypatch.setattr(settings.db,'load_app_setting',lambda key:value['raw'])
    monkeypatch.setattr(settings.db,'get_client_status',lambda:{'elevated_active':value['elevated']})
    class Client:
        def rpc(self,name,payload):
            assert name=='sky_set_access'
            def execute():
                enabled=payload['p_enabled']
                value['writes'].append((settings.KEY,enabled))
                if not value['save']: raise RuntimeError('injected save failure')
                old=settings.parse_value(value['raw'])
                assert old is not None
                value['raw']={'enabled':enabled,'pause_epoch':old['pause_epoch'] if enabled else payload['p_pause_epoch']}
                return SimpleNamespace(data=dict(value['raw']))
            return SimpleNamespace(execute=execute)
    monkeypatch.setattr(settings.db,'get_client',lambda:Client())
    settings.reset_cache()
    yield value
    settings.reset_cache()


@pytest.fixture
def api(store):
    app=Starlette(routes=[*settings.sky_settings_routes,
                          Route('/v1/sky/chat/completions',gm.sky_chat_preview,methods=['POST'])])
    with TestClient(app,headers={'x-sky-access-protocol':'2'}) as client: yield client


def test_default_off_persistent_toggle_and_readback(api,store):
    first=api.get('/admin/api/sky/settings')
    assert first.json()=={'enabled':False,'pause_epoch':settings.INITIAL_EPOCH,
                          'available':True,'writable':True,'source':'default'}
    assert first.headers['cache-control']=='no-store'
    assert api.put('/admin/api/sky/settings',json={'enabled':True}).json()['enabled'] is True
    settings.reset_cache()  # Process restart, not just the running cache.
    assert api.get('/v1/sky/settings').json()['enabled'] is True
    assert api.put('/admin/api/sky/settings',json={'enabled':False}).json()['enabled'] is False
    assert store['writes']==[(settings.KEY,True),(settings.KEY,False)]


@pytest.mark.parametrize('body',[{},[],{'enabled':1},{'enabled':'true'},{'enabled':True,'other':False}])
def test_setting_schema_cannot_silently_coerce_values(api,store,body):
    assert api.put('/admin/api/sky/settings',json=body).status_code==400
    assert not store['writes']


def test_read_write_auth_and_readonly_access(api,store,monkeypatch):
    monkeypatch.setattr(gm,'verify_token',lambda _:False)
    assert api.get('/admin/api/sky/settings').status_code==401
    assert api.put('/admin/api/sky/settings',json={'enabled':True}).status_code==401
    assert not store['writes']
    monkeypatch.setattr(gm,'verify_token',lambda _:True)
    store['elevated']=False
    assert api.get('/admin/api/sky/settings').json()['writable'] is False
    assert api.put('/admin/api/sky/settings',json={'enabled':True}).status_code==503


def test_save_failure_does_not_report_success_or_change_enabled(api,store):
    store['save']=False
    assert api.put('/admin/api/sky/settings',json={'enabled':True}).status_code==503
    assert api.get('/admin/api/sky/settings').json()['enabled'] is False


@pytest.mark.parametrize('bad',[{'enabled':True},'true',123])
def test_invalid_storage_is_unknown_and_cannot_enable_requests(api,store,bad):
    store['raw']=bad
    response=api.get('/admin/api/sky/settings').json()
    assert response['available'] is False and response['writable'] is False and response['enabled'] is False
    assert api.post('/v1/sky/chat/completions',json={}).status_code==503


def test_disabled_rejects_before_context_or_upstream(api,monkeypatch):
    def unexpected(*a,**k): raise AssertionError('disabled request reached model context')
    monkeypatch.setattr(gm,'build_context',unexpected)
    assert api.post('/v1/sky/chat/completions',json={}).json()=={'error':'sky_disabled'}


def test_confirmed_delivery_still_commits_when_switch_is_off(server,monkeypatch):
    main,_,committed=server
    monkeypatch.setattr(main.sky_settings,'snapshot',lambda **_:{'available':True,'enabled':False})
    assert commit(main,delivery()).status_code==200 and len(committed)==1


def test_gateway_reads_gate_and_refreshes_without_extra_model_calls(config,receipt_clock):
    enabled=[False]; paths=[]
    def transport(request):
        paths.append(request.url.path)
        if request.url.path=='/status': return httpx.Response(200,json={'sky_access_control':1})
        if request.url.path=='/v1/sky/settings':
            return httpx.Response(200,json={'enabled':enabled[0],'available':True,'pause_epoch':settings.INITIAL_EPOCH})
        return httpx.Response(403,json={'error':'sky_disabled'})
    gateway=b.Gateway(config,transport=httpx.MockTransport(transport))
    try:
        gateway.status(); assert not gateway.is_enabled()
        enabled[0]=True; receipt_clock.now+=2
        assert gateway.is_enabled()
        enabled[0]=False
        assert gateway.send_enabled() is False  # Submit bypasses the fresh positive cache.
        enabled[0]=True;receipt_clock.now+=2
        assert gateway.is_enabled()
        with pytest.raises(b.SkyPaused): gateway.generate('在吗')
        assert gateway.is_enabled() is True  # A delayed request cannot overwrite an authoritative receipt.
        enabled[0]=False
        assert gateway.is_enabled(refresh=True) is False  # Current permission still wins when reconciled.
    finally: gateway.close()
    assert paths.count('/v1/sky/settings')==5


class SwitchGateway:
    enabled=True
    def status(self): return {}
    def flush_deliveries(self): pass
    def is_enabled(self):
        if not self.enabled and self.companion.access_paused:
            self.companion.stop.set()
        return self.enabled
    def confirm_delivery(self,*args,**kwargs):
        self.companion.stop.set()


def setup_companion(config,gateway,device=None):
    device=device or FakeDevice()
    companion=b.Companion(config,device,gateway,
                          reader=lambda _:screen(history=['在呢。'] if device.keys else []))
    gateway.companion=companion
    companion.collect=lambda:None
    companion.inbox.add([{'sender':'叶子','text':'在吗'}],1)
    return companion,device


@pytest.mark.parametrize('rejected',[False,True])
def test_disable_during_generation_cancels_without_send_or_record(config,rejected):
    class Gateway(SwitchGateway):
        def generate(self,*args,**kwargs):
            self.enabled=False
            if rejected: raise b.SkyPaused('disabled')
            return '在呢。'
    gateway=Gateway(); companion,device=setup_companion(config,gateway)
    companion.run()
    assert not device.keys and not list((b.PRIVATE/'deliveries').glob('*.json'))
    state=json.loads(next((b.PRIVATE/'decisions').glob('*.json')).read_text(encoding='utf8'))
    assert state['status']=='cancelled_by_switch' and not state['sent']
    assert not companion.inbox.inflight and not companion.inbox.pending


def test_disable_after_typing_clears_only_owned_draft(config):
    class Gateway(SwitchGateway):
        def generate(self,*a,**k): return '在呢。'
    gateway=Gateway(); companion,device=setup_companion(config,gateway)
    original=device.input_text; cleared=[]
    def input_text(text): original(text); gateway.enabled=False
    def clear(expected):
        assert device.draft==expected
        device.draft='';cleared.append(expected)
    device.input_text=input_text;device.clear_owned_draft=clear
    companion.run()
    assert cleared==['在呢。'] and not device.keys and device.draft==''


def test_disable_after_submit_does_not_interrupt_confirmed_delivery(config):
    class Gateway(SwitchGateway):
        confirmed=[]
        def generate(self,*a,**k): return '在呢。'
        def confirm_delivery(self,*a,**k):
            self.confirmed.append((a,k));self.companion.stop.set()
    gateway=Gateway();companion,device=setup_companion(config,gateway)
    original=device.adb
    def submit(*args,**kwargs):
        original(*args,**kwargs);gateway.enabled=False
    device.adb=submit
    companion.run()
    assert len(device.keys)==1 and len(gateway.confirmed)==1
    assert json.loads(next((b.PRIVATE/'decisions').glob('*.json')).read_text(encoding='utf8'))['status']=='completed'
