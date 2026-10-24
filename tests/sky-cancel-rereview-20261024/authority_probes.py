"""Independent HTTP rejection probes for unobserved authority changes."""
import json

import httpx
import pytest

from test_sky_companion import b, config, private_files, FakeDevice, screen, receipt_clock
from test_sky_settings import store
from gateway import sky_settings as settings


@pytest.mark.parametrize('resume', [False, True])
@pytest.mark.parametrize('error', ['sky_disabled', 'sky_generation_cancelled'])
def test_late_rejection_still_cancels_stale_arrivals_when_receiver_missed_pause(
        config, store, receipt_clock, monkeypatch, resume, error):
    settings.save(True)
    deliveries = []

    def respond(request):
        if request.url.path == '/status':
            return httpx.Response(200, json={'sky_access_control': 2})
        if request.url.path == '/v1/sky/settings':
            return httpx.Response(200, json=settings.snapshot(refresh=True))
        if request.url.path == '/v1/sky/deliveries':
            deliveries.append(json.loads(request.content))
            pytest.fail('cancelled request must not create a delivery')
        assert request.url.path == '/v1/sky/chat/completions'
        settings.save(False)
        # A receiver using its still-positive one-second cache can have queued
        # this message without having observed the off interval yet.
        companion.inbox.add([{'sender': '叶子', 'text': '暂停期间进入旧代次的消息'}],
                            2, access_generation=companion.access_generation)
        if resume:
            settings.save(True)
        # No call to companion.access_enabled here: reconciliation must discover
        # the persisted state/epoch itself, bypassing the old positive cache.
        return httpx.Response(403, json={'error': error})

    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    device = FakeDevice()
    companion = b.Companion(config, device, gateway, reader=lambda _: screen())
    companion.collect = lambda: None
    companion.inbox.add([{'sender': '叶子', 'text': '暂停前的问题'}], 1, access_generation=0)
    original_generate = gateway.generate

    def finish_request(*args, **kwargs):
        try:
            return original_generate(*args, **kwargs)
        finally:
            companion.stop.set()  # Inspect cancellation before another turn.

    monkeypatch.setattr(gateway, 'generate', finish_request)
    try:
        companion.run()
    finally:
        gateway.close()
    assert not device.typed and not device.keys and not deliveries and not companion.history
    assert not companion.inbox.pending and not companion.inbox.inflight
    assert companion.access_generation == 1 and companion.access_paused
    assert {event['state'] for event in companion.inbox.events.values()} == {'cancelled_by_switch'}
    decision = json.loads(next((b.PRIVATE/'decisions').glob('*.json')).read_text(encoding='utf8'))
    assert decision['status'] == 'cancelled_by_switch' and decision['sent'] is False
