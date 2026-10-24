"""Explicit review reproductions; excluded from normal test discovery by filename."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from test_sky_companion import b, config, private_files, peer_line, FakeDevice, screen


@pytest.mark.parametrize("name", ["叶·子", "阿祁·叶子"])
def test_player_name_remains_complete(config, name):
    actual = b.chat_messages([peer_line("你好 - " + name)], config["chat_region"],
                             ["叶子"], mode="group")
    assert actual == [{"sender": name, "text": "你好"}]


def test_initial_batch_respects_request_budget(config):
    config["chat_mode"] = "group"
    inbox = b.Inbox()
    messages = [{"sender": "玩家甲", "text": f"消息{i}:" + "a" * 25} for i in range(400)]
    messages.append({"sender": "叶子", "text": "你还在吗"})
    inbox.add(messages, 1)
    received = inbox.take(timeout=0)
    assert len(b.conversation_query(received["messages"], config)) <= 20000


def test_delivery_retry_preserves_original_assistant(monkeypatch):
    from gateway import sky_delivery as sd
    authors = iter(["original", "another-client"])
    monkeypatch.setattr(sd, "resolve_assistant_id", lambda: next(authors))
    monkeypatch.setattr(sd.db, "get_client_status", lambda: {"elevated_active": True})
    committed = {}
    class Client:
        def rpc(self, name, payload):
            def execute():
                key = payload["p_delivery_id"]
                if key in committed and committed[key] != payload:
                    error = ValueError("payload mismatch")
                    error.code = "22023"
                    raise error
                duplicate = key in committed
                committed[key] = payload
                return SimpleNamespace(data={"delivery_id": key, "user_message_id": 1,
                                             "assistant_message_id": 2, "duplicate": duplicate})
            return SimpleNamespace(execute=execute)
    monkeypatch.setattr(sd.db, "get_client", lambda: Client())
    payload = sd.validate_delivery({"delivery_id": str(uuid4()), "vm_index": 10,
        "query": "叶子：在吗", "text": "在呢", "confirmed": True,
        "confirmed_at": "2026-10-24T12:00:00+00:00"})
    sd.publish_delivery(payload)
    assert sd.publish_delivery(payload)["duplicate"]


@pytest.mark.parametrize("when", ["before_take", "during_merge"])
def test_partial_ocr_does_not_make_old_message_latest(config, when):
    config["chat_mode"] = "group"
    old = {"sender": "叶子", "text": "去雨林吗"}
    latest = {"sender": "朋友甲", "text": "改去霞谷吧"}
    inbox = b.Inbox()
    inbox.observe_order([old, latest])
    inbox.add([old, latest], 1)
    if when == "during_merge":
        inbox.take(timeout=0)
        incoming = {"sender": "朋友乙", "text": "都可以"}
        inbox.add([incoming], 2)
        inbox.observe_order([old, incoming])  # Latest existing row temporarily missed.
        batch = inbox.merge_pending(config)
        assert batch["messages"] == [old, latest, incoming]
    else:
        inbox.observe_order([old])  # Last row drops out for one OCR frame.
        batch = inbox.take(timeout=0)
        assert json.loads(b.conversation_query(batch["messages"], config))["latest_message"] == latest


def test_confirmed_send_survives_optional_snapshot_failure(config, monkeypatch):
    requests = []
    def respond(request):
        requests.append(request.url.path)
        if request.url.path == "/status":
            return httpx.Response(200, json={"sky_delivery_protocol": 1})
        if request.url.path.endswith("chat/completions"):
            return httpx.Response(200, json={"choices": [{"message": {"content": "在呢。"}}]})
        payload = json.loads(request.content)
        return httpx.Response(200, json={"delivery_id": payload["delivery_id"],
                                        "user_message_id": 1, "assistant_message_id": 2})
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    device = FakeDevice()
    companion = b.Companion(config, device, gateway,
        reader=lambda _: screen(history=["在呢。"] if device.keys else []))
    monkeypatch.setattr(companion, "collect", lambda: None)
    companion.inbox.add([{"sender": "叶子", "text": "在吗"}], 1)
    def snapshot_error():
        raise PermissionError("diagnostic screen.png is held open")
    monkeypatch.setattr(companion, "save_snapshot", snapshot_error)
    original_ack = companion.inbox.acknowledge
    def finish():
        original_ack()
        companion.stop.set()
    monkeypatch.setattr(companion.inbox, "acknowledge", finish)
    error = None
    try:
        companion.run()
    except PermissionError as exc:
        error = str(exc)
    finally:
        gateway.close()
    assert len(device.keys) == 1
    confirmed = json.loads((b.PRIVATE / "last_send.json").read_text(encoding="utf-8"))
    assert confirmed["confirmed"] is True
    decisions = [json.loads(p.read_text(encoding="utf-8"))
                 for p in (b.PRIVATE / "decisions").glob("*.json")]
    assert "/v1/sky/deliveries" in requests, {
        "error": error, "confirmed": confirmed["confirmed"], "states": decisions,
        "delivery_files": len(list((b.PRIVATE / "deliveries").glob("*.json"))),
    }
