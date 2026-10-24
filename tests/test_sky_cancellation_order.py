"""Request-scoped rejection and atomic visual commits, with isolated devices."""
import json
import threading

import httpx
import pytest

from test_sky_companion import b, config, private_files, FakeDevice, screen, receipt_clock
from test_sky_settings import store
from test_sky_pause_epoch import Gate, companion_for
from test_sky_companion_vision import frame, vision_config
from gateway import sky_settings as settings


@pytest.mark.parametrize('error', ['sky_generation_cancelled', 'sky_disabled'])
@pytest.mark.parametrize('mode', ['text', 'image', 'look'])
def test_late_rejection_allows_fresh_turn_to_send_and_record_once(config, store, receipt_clock, error, mode):
    if mode != 'text': config = vision_config(config)
    settings.save(True)
    previews, deliveries = [], []
    old, fresh = ({'sender': '叶子', 'text': text} for text in ('暂停前的问题', '重开后新问题'))
    rejected = False
    def response(request):
        nonlocal rejected
        path = request.url.path
        if path == '/status': return httpx.Response(200, json={'sky_access_control': 2})
        if path == '/v1/sky/settings': return httpx.Response(200, json=settings.snapshot(refresh=True))
        body = json.loads(request.content)
        if path == '/v1/sky/deliveries':
            deliveries.append(body)
            companion.stop.set()
            return httpx.Response(200, json={'delivery_id': body['delivery_id'],
                                            'user_message_id': 1, 'assistant_message_id': 2})
        assert path == '/v1/sky/chat/completions'
        previews.append(body)
        if mode == 'look' and len(previews) == 1:
            return httpx.Response(200, json={'choices': [{'message': {'content': 'LOOK'}}]})
        if not rejected:
            settings.save(False)
            assert not companion.access_enabled(refresh=True)
            settings.save(True)
            assert companion.access_enabled(refresh=True)
            companion.resume_access()
            for visible in ([old], [old, fresh], [old, fresh]):
                companion.queue_observation({'messages': visible})
            rejected = True
            return httpx.Response(403, json={'error': error})
        content = '新回复' if mode == 'text' else json.dumps({'reply': '新回复', 'scene': '重新开启后的新画面'})
        return httpx.Response(200, json={'choices': [{'message': {'content': content}}]})
    gateway = b.Gateway(config, transport=httpx.MockTransport(response))
    device = FakeDevice()
    companion = b.Companion(config, device, gateway, reader=lambda _: screen(history=['新回复'] if device.keys else []))
    companion.collect = lambda: None
    companion.capture_visual = frame
    if mode == 'look': companion.vision.accept(frame(), b.VisionReply('旧回答', '旧装扮'))
    companion.inbox.add([old], 1, access_generation=0)
    try: companion.run()
    finally: gateway.close()
    assert len(previews) == (3 if mode == 'look' else 2)
    assert device.typed == ['新回复'] and len(device.keys) == len(deliveries) == 1
    assert '重开后新问题' in deliveries[0]['query'] and '暂停前的问题' not in deliveries[0]['query']
    assert len(companion.history) == 2 and not companion.inbox.inflight and not companion.inbox.pending
    decisions = [json.loads(path.read_text(encoding='utf8')) for path in (b.PRIVATE/'decisions').glob('*.json')]
    assert sorted(item['status'] for item in decisions) == ['cancelled_by_switch', 'completed']
    if mode != 'text':
        assert companion.vision.summary == '重新开启后的新画面'
        assert json.loads((b.PRIVATE/'last_vision.json').read_text(encoding='utf8'))['summary'] == companion.vision.summary


@pytest.mark.parametrize('error', ['sky_generation_cancelled', 'sky_disabled'])
def test_rejection_with_unchanged_authority_cancels_only_originating_events(config, receipt_clock, error):
    gate = Gate(); companion, device = companion_for(config, gate)
    def generate(query, *args, **kwargs):
        gate.calls.append(query)
        if len(gate.calls) == 1:
            companion.inbox.add([{'sender': '叶子', 'text': '同代次后来的新问题'}], 2, access_generation=0)
            raise b.SkyPaused(error)
        return '新回复'
    gate.generate = generate
    companion.inbox.add([{'sender': '叶子', 'text': '被拒绝的旧问题'}], 1, access_generation=0)
    acknowledge = companion.inbox.acknowledge
    def done(): acknowledge(); companion.stop.set()
    companion.inbox.acknowledge = done
    companion.run()
    assert device.typed == ['新回复'] and len(gate.confirmed) == len(device.keys) == 1
    assert len(gate.calls) == 2 and '被拒绝的旧问题' not in gate.calls[1]
    assert companion.access_generation == 0 and not companion.access_paused
    assert sorted(item['state'] for item in companion.inbox.events.values()) == ['cancelled_by_gateway', 'handled']


def test_current_state_read_failure_stops_without_cancelling_post_resume_pending(config):
    gate = Gate(); companion, device = companion_for(config, gate)
    old = {'sender': '叶子', 'text': '旧问题'}
    fresh = {'sender': '叶子', 'text': '重新开启后新问题'}
    def generate(*args, **kwargs):
        gate.aba()
        assert not companion.access_enabled(refresh=True)
        assert companion.access_enabled(refresh=True)
        companion.resume_access()
        companion.inbox.add([fresh], 2, access_generation=companion.access_generation)
        def unavailable(**kwargs): raise b.BridgeError('settings unavailable')
        gate.access_state = unavailable
        raise b.SkyPaused('old HTTP rejection')
    gate.generate = generate
    companion.inbox.add([old], 1, access_generation=0)
    with pytest.raises(b.BridgeError, match='settings unavailable'): companion.run()
    assert not device.typed and not device.keys
    assert [item['message'] for item in companion.inbox.pending] == [fresh]


def test_pause_cannot_leave_old_failure_backoff_in_new_visual_state(config, monkeypatch):
    gate = Gate()
    companion = b.Companion(vision_config(config), FakeDevice(), gate, reader=lambda _: screen())
    companion.capture_visual = frame
    assert companion.access_enabled(refresh=True)
    companion.inbox.add([{'sender': '叶子', 'text': '看看画面'}], 1, access_generation=0)
    companion.begin_decision(companion.inbox.take(), '看看画面')
    def generate(*args, **kwargs):
        if kwargs.get('image'): raise b.BridgeError('injected image failure')
        return '文字回复'
    gate.generate = generate
    checked, resumed = threading.Event(), threading.Event()
    ensure = companion.ensure_access
    calls = []
    def ensure_then_yield(*args, **kwargs):
        ensure(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:  # Fallback state check, before failed() can mutate it.
            checked.set()
            resumed.wait(.2)
    def receiver():
        if not checked.wait(2): return
        gate.aba()
        companion.access_enabled(refresh=True)
        companion.access_enabled(refresh=True)
        companion.resume_access()
        resumed.set()
    monkeypatch.setattr(companion, 'ensure_access', ensure_then_yield)
    worker = threading.Thread(target=receiver)
    worker.start()
    try:
        try: companion.generate_reply('看看画面')
        except b.SkyPaused:
            assert companion.vision.retry_after == 0  # Cancellation leaves the resumed state clean.
    finally: worker.join(timeout=3)
    assert not worker.is_alive() and resumed.is_set()
    with pytest.raises(b.SkyPaused): ensure()
    assert companion.vision.summary == '' and companion.vision.retry_after == 0
    assert companion.vision.due(b.time.time())
