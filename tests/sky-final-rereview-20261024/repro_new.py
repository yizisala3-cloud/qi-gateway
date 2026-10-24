"""Independent cancellation-order probes; run explicitly, with no live services."""
import threading

import httpx
import pytest

from test_sky_companion import b, config, private_files, FakeDevice, screen, receipt_clock
from test_sky_settings import store
from test_sky_pause_epoch import Gate
from test_sky_companion_vision import frame, vision_config
from gateway import sky_settings as settings


@pytest.mark.parametrize('error', ['sky_generation_cancelled', 'sky_disabled'])
def test_late_old_http_rejection_preserves_messages_already_received_after_resume(
        config, store, receipt_clock, monkeypatch, error):
    settings.save(True)
    fresh = {'sender': '叶子', 'text': '重新开启以后刚问的新问题'}
    old = {'sender': '叶子', 'text': '暂停前的问题'}
    queued_before_response = []

    def transport(request):
        if request.url.path == '/status':
            return httpx.Response(200, json={'sky_access_control': 2})
        if request.url.path == '/v1/sky/settings':
            return httpx.Response(200, json=settings.snapshot(refresh=True))
        assert request.url.path == '/v1/sky/chat/completions'
        # The receiver observes the real persisted off/on while HTTP is pending.
        settings.save(False)
        assert not companion.access_enabled(refresh=True)
        settings.save(True)
        assert companion.access_enabled(refresh=True)
        companion.resume_access()
        for visible in ([old], [old, fresh], [old, fresh]):
            companion.queue_observation({'messages': visible})
        queued_before_response.extend(item['message'] for item in companion.inbox.pending)
        assert queued_before_response == [fresh]
        return httpx.Response(403, json={'error': error})

    gateway = b.Gateway(config, transport=httpx.MockTransport(transport))
    device = FakeDevice()
    companion = b.Companion(config, device, gateway, reader=lambda _: screen())
    companion.collect = lambda: None
    companion.inbox.add([old], 1, access_generation=0)
    generate = gateway.generate

    def finish_old_request(*args, **kwargs):
        try:
            return generate(*args, **kwargs)
        finally:
            # Stop before a fresh turn is taken, regardless of whether the old
            # response is correctly scoped or incorrectly resets the receiver.
            companion.stop.set()

    monkeypatch.setattr(gateway, 'generate', finish_old_request)
    try:
        companion.run()
    finally:
        gateway.close()
    assert not device.keys
    assert [item['message'] for item in companion.inbox.pending] == [fresh], {
        'queued_before_old_response': queued_before_response,
        'pending_after_old_response': companion.inbox.pending,
        'events': list(companion.inbox.events.values()),
    }


def test_pause_between_visual_permission_check_and_commit_cannot_restore_old_scene(
        config, monkeypatch):
    gate = Gate()
    gate.generate = lambda *args, **kwargs: b.VisionReply('旧回复', '暂停以前的旧画面')
    companion = b.Companion(vision_config(config), FakeDevice(), gate, reader=lambda _: screen())
    assert companion.access_enabled(refresh=True)
    companion.inbox.add([{'sender': '叶子', 'text': '看下画面'}], 1, access_generation=0)
    companion.begin_decision(companion.inbox.take(), '看下画面')
    companion.capture_visual = frame
    checked, paused = threading.Event(), threading.Event()
    ensure = companion.ensure_access
    calls = []

    def ensure_then_yield(*args, **kwargs):
        ensure(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:  # Post-response permission check succeeded.
            checked.set()
            # With an atomic check/commit, the receiver may wait for the lock;
            # it can then clear the committed scene after this method returns.
            paused.wait(.2)

    def receiver():
        if not checked.wait(2):
            return
        gate.aba()
        companion.access_enabled(refresh=True)
        companion.access_enabled(refresh=True)
        companion.resume_access()
        paused.set()

    monkeypatch.setattr(companion, 'ensure_access', ensure_then_yield)
    worker = threading.Thread(target=receiver)
    worker.start()
    try:
        try:
            companion.generate_reply('看下画面')
        except b.SkyPaused:
            assert companion.vision.summary == ''  # Early cancellation must leave a clean scene.
    finally:
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert paused.is_set()
    with pytest.raises(b.SkyPaused):
        ensure()  # The old reply is cancelled, as in run(), after vision commit.
    assert companion.vision.summary == '', companion.vision.context(b.time.time())
