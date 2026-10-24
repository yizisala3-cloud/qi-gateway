"""Explicit independent rereview probes; excluded from default collection."""
import json

import httpx
import pytest

from test_sky_companion import b, config, private_files, peer_line, FakeDevice, screen, receipt_clock
from test_sky_settings import store
from gateway import sky_settings as settings


@pytest.mark.parametrize("mode", ["group", "direct"])
def test_ocr_name_fragments_cannot_impersonate_primary(config, mode):
    # OCR splits the actual suffix 阿祁-叶子 into two same-row boxes.
    # merge_rows inserts a layout space before the second dash.
    rows = [peer_line("你好", x=20, width=50),
            peer_line("-阿祁", x=80, width=60),
            peer_line("-叶子", x=150, width=60)]
    actual = b.chat_messages(rows, config["chat_region"], ["叶子"], mode=mode)
    assert not any(message["sender"] == "叶子" for message in actual), actual


@pytest.mark.parametrize("message_while_disabled", [False, True])
def test_disable_then_reenable_during_model_cancels_old_batch(
        config, store, receipt_clock, monkeypatch, message_while_disabled):
    config["chat_mode"] = "group"
    receipt_clock.now = 1
    settings.save(True)
    requests, previews = [], []

    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/status":
            return httpx.Response(200, json={"sky_access_control": 1, "sky_delivery_protocol": 1})
        if request.url.path == "/v1/sky/settings":
            return httpx.Response(200, json=settings.snapshot(refresh=True))
        if request.url.path.endswith("chat/completions"):
            previews.append(json.loads(request.content))
            if len(previews) == 1:
                # Both dashboard saves complete while this model request is in flight.
                settings.save(False)
                if message_while_disabled:
                    companion.inbox.add([{"sender": "朋友甲", "text": "开关关闭期间的消息"}], 2)
                receipt_clock.now += 30
                settings.save(True)
            return httpx.Response(200, json={"choices": [{"message": {"content": "在呢。"}}]})
        payload = json.loads(request.content)
        return httpx.Response(200, json={"delivery_id": payload["delivery_id"],
                                        "user_message_id": 1, "assistant_message_id": 2})

    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    device = FakeDevice()
    companion = b.Companion(config, device, gateway,
        reader=lambda _: screen(history=["在呢。"] if device.keys else []))
    monkeypatch.setattr(companion, "collect", lambda: None)
    companion.inbox.add([{"sender": "叶子", "text": "关闭前的问题"}], 1)
    acknowledge = companion.inbox.acknowledge
    def finish():
        acknowledge()
        companion.stop.set()
    monkeypatch.setattr(companion.inbox, "acknowledge", finish)
    # Stop a correct implementation once it has cancelled the stale generation.
    original_pause = companion.pause_access
    def pause():
        original_pause()
        companion.stop.set()
    monkeypatch.setattr(companion, "pause_access", pause)
    try:
        companion.run()
    finally:
        gateway.close()
    assert not device.keys, {
        "saves": store["writes"], "send_keys": len(device.keys),
        "generated_user_queries": [request["messages"][-1]["content"] for request in previews],
        "deliveries": requests.count("/v1/sky/deliveries"),
    }
