"""Pause/resume generations and OCR source boundaries, without game or network."""
import json
from uuid import uuid4

import httpx
import pytest
from starlette.responses import JSONResponse

from test_sky_companion import b, config, private_files, FakeDevice, screen, receipt_clock, peer_line
from test_sky_companion_vision import frame, vision_config
from test_sky_settings import store, api
from gateway import main as gm, sky_settings as settings
from tools.sky_companion.layout import merge_rows


def test_pause_epoch_survives_fast_aba_cache_reset_and_repeated_enable(api, store):
    first = api.put('/admin/api/sky/settings', json={'enabled': True}).json()
    paused = api.put('/admin/api/sky/settings', json={'enabled': False}).json()
    resumed = api.put('/admin/api/sky/settings', json={'enabled': True}).json()
    assert first['pause_epoch'] != paused['pause_epoch'] == resumed['pause_epoch']
    settings.reset_cache()
    assert api.get('/v1/sky/settings').json()['pause_epoch'] == paused['pause_epoch']
    assert api.put('/admin/api/sky/settings', json={'enabled': True}).json()['pause_epoch'] == paused['pause_epoch']
    assert api.put('/admin/api/sky/settings', json={'enabled': False}).json()['pause_epoch'] != paused['pause_epoch']


@pytest.mark.parametrize('result', [True, None, {}, {'enabled': True, 'pause_epoch': 'bad'}])
def test_corrupt_or_legacy_storage_cannot_authorize_generation(api, store, result):
    store['raw'] = result if result is not None else {'enabled': True, 'pause_epoch': None}
    assert api.get('/v1/sky/settings').json()['available'] is False
    assert api.post('/v1/sky/chat/completions', json={}).status_code == 503


@pytest.mark.parametrize('finish', ['reply', 'pass', 'look'])
def test_server_rejects_preview_if_disabled_and_reenabled_during_upstream(api, store, monkeypatch, finish):
    settings.save(True)
    async def model(request, **kwargs):
        assert kwargs['persist'] is False
        settings.save(False); settings.save(True)
        return JSONResponse({'choices': [{'message': {'content': finish}}]})
    monkeypatch.setattr(gm, 'chat_completions', model)
    response = api.post('/v1/sky/chat/completions', json={})
    assert response.status_code == 403 and response.json() == {'error': 'sky_generation_cancelled'}


def test_unversioned_client_is_rejected_before_upstream(api, monkeypatch):
    def unexpected(*args, **kwargs): pytest.fail('old protocol reached generation')
    monkeypatch.setattr(gm, 'chat_completions', unexpected)
    assert api.post('/v1/sky/chat/completions', headers={'x-sky-access-protocol': '1'}, json={}).status_code == 426


@pytest.mark.parametrize('state', [{'enabled': True, 'available': True},
    {'enabled': True, 'available': True, 'pause_epoch': 'bad'}])
def test_bridge_refuses_legacy_or_invalid_permission_receipt(config, state):
    def transport(request):
        assert request.headers['x-sky-access-protocol'] == '2'
        return httpx.Response(200, json={'sky_access_control': 2} if request.url.path == '/status' else state)
    gateway = b.Gateway(config, transport=httpx.MockTransport(transport))
    try:
        gateway.status()
        with pytest.raises(b.BridgeError, match='暂停代次'): gateway.access_state()
    finally: gateway.close()


class Gate:
    def __init__(self):
        self.state = {'enabled': True, 'pause_epoch': str(uuid4())}
        self.calls, self.confirmed = [], []
    def status(self): return {}
    def access_state(self, **kwargs): return dict(self.state)
    def flush_deliveries(self): pass
    def aba(self): self.state['pause_epoch'] = str(uuid4())
    def confirm_delivery(self, *args, **kwargs): self.confirmed.append((args, kwargs))


def companion_for(config, gate):
    device = FakeDevice()
    companion = b.Companion(config, device, gate, reader=lambda _: screen(history=['新回复'] if device.keys else []))
    companion.collect = lambda: None
    return companion, device


@pytest.mark.parametrize('old_reply', ['旧草稿', None])
def test_receiver_cancels_old_turn_but_fresh_post_resume_message_still_replies(config, receipt_clock, old_reply):
    gate = Gate(); companion, device = companion_for(config, gate)
    def generate(query, *args, **kwargs):
        gate.calls.append(query)
        if len(gate.calls) == 1:
            gate.state['enabled'] = False; gate.aba()
            assert not companion.access_enabled(refresh=True)
            gate.state['enabled'] = True
            assert companion.access_enabled(refresh=True)
            companion.resume_access()
            # A fresh baseline ignores the disabled period, including visible old lines.
            old = [{'sender': '叶子', 'text': '关闭期间的消息'}]
            fresh = {'sender': '叶子', 'text': '重新开启后问你'}
            for messages in (old, old + [fresh], old + [fresh]):
                companion.queue_observation({'messages': messages})
            return old_reply
        return '新回复'
    gate.generate = generate
    companion.inbox.add([{'sender': '叶子', 'text': '旧问题'}], 1, access_generation=0)
    original_ack = companion.inbox.acknowledge
    def done(): original_ack(); companion.stop.set()
    companion.inbox.acknowledge = done
    companion.run()
    assert len(gate.calls) == 2 and len(device.keys) == len(gate.confirmed) == 1
    assert device.typed == ['新回复']
    assert '重新开启后问你' in gate.calls[1] and '关闭期间的消息' not in gate.calls[1]
    assert len(companion.history) == 2 and all('旧问题' not in entry['content'] for entry in companion.history)


def test_receiver_observes_pause_while_model_is_blocked(config):
    import threading
    config['poll_seconds'] = .01
    gate = Gate(); companion, device = companion_for(config, gate)
    companion.collect = b.Companion.collect.__get__(companion)
    observed = threading.Event()
    original_pause = companion.pause_access
    def pause(): original_pause(); observed.set(); companion.stop.set()
    companion.pause_access = pause
    def generate(*args, **kwargs):
        gate.state['enabled'] = False; gate.aba()
        assert observed.wait(2), 'receiver stopped checking the gate during model generation'
        gate.state['enabled'] = True
        return '旧回复'
    gate.generate = generate
    companion.inbox.add([{'sender': '叶子', 'text': '旧问题'}], 1, access_generation=0)
    companion.run()
    assert not device.keys and not companion.inbox.inflight and not companion.inbox.pending


def test_pause_after_submit_keeps_receipt_and_fresh_pending_separate(config, receipt_clock):
    gate = Gate(); companion, device = companion_for(config, gate)
    gate.generate = lambda *args, **kwargs: '新回复'
    companion.inbox.add([{'sender': '叶子', 'text': '已决定的问题'}], 1, access_generation=0)
    original_submit = device.adb
    def submit(*args):
        original_submit(*args)
        gate.state['enabled'] = False; gate.aba()
        assert not companion.access_enabled(refresh=True)
        assert companion.inbox.inflight and companion.active_decision['status'] == 'submitting'
        gate.state['enabled'] = True
        assert companion.access_enabled(refresh=True)
        companion.resume_access()
        companion.inbox.add([{'sender': '叶子', 'text': '新一轮的问题'}], 2, access_generation=companion.access_generation)
    device.adb = submit
    original_ack = companion.inbox.acknowledge
    def done(): original_ack(); companion.stop.set()
    companion.inbox.acknowledge = done
    companion.run()
    assert len(device.keys) == len(gate.confirmed) == 1
    assert [item['message']['text'] for item in companion.inbox.pending] == ['新一轮的问题']
    decision = json.loads(next((b.PRIVATE/'decisions').glob('*.json')).read_text(encoding='utf8'))
    assert decision['status'] == 'completed' and decision['sent'] and decision['handled']
    assert len(list((b.PRIVATE/'deliveries').glob('*.json'))) == 1


@pytest.mark.parametrize('phase', ['preparing', 'typed', 'pass_commit', 'image', 'look'])
def test_pause_generation_checked_at_all_unsent_boundaries(config, receipt_clock, monkeypatch, phase):
    gate = Gate()
    if phase in ('image', 'look'): config = vision_config(config)
    companion, device = companion_for(config, gate)
    captures = []
    companion.capture_visual = lambda: captures.append(True) or frame()
    if phase == 'look': companion.vision.accept(frame(), b.VisionReply('旧回复', '旧场景'))
    def generate(query, *args, **kwargs):
        gate.calls.append(query)
        if phase == 'image':
            gate.aba()
            return b.VisionReply('旧回复', '关闭前的场景')
        if phase == 'look':
            gate.aba(); raise b.VisionRequested()
        return None if phase == 'pass_commit' else '旧回复'
    gate.generate = generate
    if phase == 'preparing':
        original = companion.reader
        def read(device): gate.aba(); return original(device)
        companion.reader = read
    elif phase == 'typed':
        original = device.input_text
        def type_text(text): original(text); gate.aba()
        device.input_text = type_text
    elif phase == 'pass_commit':
        original = companion.inbox.merge_pending
        def merge(config): gate.aba(); return original(config)
        companion.inbox.merge_pending = merge
    original_pause = companion.pause_access
    def pause(): original_pause(); companion.stop.set()
    companion.pause_access = pause
    companion.inbox.add([{'sender': '叶子', 'text': '旧问题'}], 1, access_generation=0)
    companion.run()
    assert not device.keys and not gate.confirmed and not companion.history
    assert device.draft == ''
    assert len(gate.calls) == 1
    assert len(captures) == (1 if phase == 'image' else 0)
    assert companion.vision.context(100)['summary'] == ''
    decision = json.loads(next((b.PRIVATE/'decisions').glob('*.json')).read_text(encoding='utf8'))
    assert decision['status'] == 'cancelled_by_switch' and not decision['sent']


@pytest.mark.parametrize('mode', ['group', 'direct'])
@pytest.mark.parametrize('split', [('你好', '-阿祁', '-叶子'), ('你好-', '阿祁', '-叶子'),
                                  ('你好', '-阿祁-', '叶子')])
def test_repeated_merge_and_json_transport_do_not_turn_box_spacing_into_name_boundary(config, mode, split):
    parts = [peer_line(text, x=20+i*100, width=95) for i, text in enumerate(split)]
    merged = merge_rows(merge_rows(parts))
    rows = json.loads(json.dumps(merged))  # OCR worker protocol roundtrip.
    result = b.chat_messages(rows, config['chat_region'], ['叶子'], mode=mode)
    assert not any(message['sender'] == '叶子' for message in result)


def test_unambiguous_real_spacing_and_wrapped_boundary_preserve_full_hyphenated_names(config):
    cases = [[peer_line('你好 - 阿祁-叶子')],
             [peer_line('hello world', width=150), peer_line('-阿祁-叶子', y=130, width=150)],
             [peer_line('算式1-2 - 叶子')]]
    expected = [{'sender': '阿祁-叶子', 'text': '你好'},
                {'sender': '阿祁-叶子', 'text': 'hello world'},
                {'sender': '叶子', 'text': '算式1-2'}]
    for rows, result in zip(cases, expected):
        assert b.chat_messages(rows, config['chat_region'], ['叶子'], mode='group') == [result]
    assert b.chat_messages(cases[0], config['chat_region'], ['叶子'], mode='direct') == []
