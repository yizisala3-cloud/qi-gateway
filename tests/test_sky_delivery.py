"""Sky preview/commit contract. All HTTP and database calls are local fakes."""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from test_sky_companion import b, config, private_files, receipt_clock, screen, FakeDevice


def delivery():
    return {"delivery_id": str(uuid4()), "vm_index": 10, "query": "叶子：在吗",
            "text": "在呢。", "confirmed": True, "confirmed_at": "2026-10-23T10:00:00+00:00"}


class Request:
    headers = {'x-sky-access-protocol':'2'}
    def __init__(self, body):
        self.body = body
    async def json(self):
        return self.body


@pytest.fixture
def server(monkeypatch):
    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    from gateway import main as gm
    from gateway import sky_delivery as sd
    monkeypatch.setattr(gm.sky_settings, "snapshot", lambda **_: {"enabled": True, "available": True,
                        "pause_epoch":gm.sky_settings.INITIAL_EPOCH})
    monkeypatch.setattr(gm, "verify_token", lambda _: True)
    monkeypatch.setattr(sd, "resolve_assistant_id", lambda: "assistant-test")
    monkeypatch.setattr(sd.db, "get_client_status", lambda: {"elevated_active": True})
    committed = {}
    class Client:
        def rpc(self, name, payload):
            assert name == "sky_publish_chat_delivery"
            assert payload["p_assistant_id"] == "assistant-test"
            key = payload["p_delivery_id"]
            def execute():
                if key in committed and committed[key] != payload:
                    error = ValueError("conflicting payload")
                    error.code = "22023"
                    raise error
                duplicate = key in committed
                committed[key] = payload
                return SimpleNamespace(data={"delivery_id": key, "user_message_id": 1,
                                             "assistant_message_id": 2, "duplicate": duplicate})
            return SimpleNamespace(execute=execute)
    monkeypatch.setattr(sd.db, "get_client", lambda: Client())
    return gm, sd, committed


def commit(gm, payload):
    return asyncio.run(gm.sky_confirm_delivery(Request(payload)))


def test_confirmed_delivery_and_identical_retry_share_one_receipt(server):
    gm, _, committed = server
    payload = delivery()
    first, second = commit(gm, payload), commit(gm, payload)
    assert first.status_code == second.status_code == 200
    assert not json.loads(first.body)["duplicate"] and json.loads(second.body)["duplicate"]
    assert len(committed) == 1
    changed = commit(gm, {**payload, "text": "这是另一条"})
    assert changed.status_code == 409 and len(committed) == 1


@pytest.mark.parametrize("change", [{"confirmed": False}, {"confirmed": "true"},
    {"delivery_id": "bad"}, {"confirmed_at": "2026-10-23T10:00:00"},
    {"text": "PASS"}, {"text": "字"*101}, {"text": "两行\n文字"},
    {"query": ""}, {"vm_index": True}, {"vm_index": -1}])
def test_unconfirmed_or_invalid_delivery_never_reaches_storage(server, change):
    gm, _, committed = server
    response = commit(gm, {**delivery(), **change})
    assert response.status_code == 400 and not committed


def test_delivery_requires_gateway_auth(server, monkeypatch):
    gm, _, committed = server
    monkeypatch.setattr(gm, "verify_token", lambda _: False)
    assert commit(gm, delivery()).status_code == 401 and not committed


def test_database_failure_returns_no_success_and_does_not_leak_details(server, monkeypatch):
    gm, sd, _ = server
    def failing_client():
        raise RuntimeError("private database response body")
    monkeypatch.setattr(sd.db, "get_client", failing_client)
    response = commit(gm, delivery())
    assert response.status_code == 503 and b"private database" not in response.body


def test_preview_pass_is_not_a_delivered_chat(server, config, monkeypatch):
    gm, sd, committed = server
    saved = []
    monkeypatch.setattr(gm, "build_context", lambda *a, **kw: "")
    monkeypatch.setattr(sd.db, "save_chat_message", lambda *args: saved.append(args))
    class Upstream:
        async def post(self, *a, **kw):
            return httpx.Response(200, json={"choices": [{"message": {"content": "PASS"}}]})
    monkeypatch.setattr(gm, "http_client", Upstream())
    def respond(request):
        assert request.url.path == "/v1/sky/chat/completions"
        response = asyncio.run(gm.sky_chat_preview(Request(json.loads(request.content))))
        return httpx.Response(response.status_code, content=response.body)
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    try:
        assert gateway.generate("叶子：不用回复") is None
    finally:
        gateway.close()
    assert saved == [] and committed == {} and not gm._background_tasks


def test_legacy_gateway_cannot_silently_save_a_preview(config):
    requests = []
    def respond(request):
        requests.append(request.url.path)
        return httpx.Response(404, json={"error": "not found"})
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(b.BridgeError, match="404"):
            gateway.generate("叶子：在吗")
    finally:
        gateway.close()
    assert requests == ["/v1/sky/chat/completions"]


def test_lost_commit_response_is_recovered_without_new_delivery_or_game_send(config):
    committed, requests = {}, []
    def respond(request):
        assert request.url.path == "/v1/sky/deliveries"
        payload = json.loads(request.content)
        requests.append(payload)
        key = payload["delivery_id"]
        committed[key] = payload
        if len(requests) == 1:
            raise httpx.ReadTimeout("lost receipt", request=request)
        return httpx.Response(200, json={"delivery_id": key, "user_message_id": 10, "assistant_message_id": 11})
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    try:
        with pytest.raises(b.BridgeError, match="已经确认"):
            gateway.confirm_delivery("叶子：在吗", "在呢。")
        paths = list((b.PRIVATE / "deliveries").glob("*.json"))
        assert len(paths) == 1 and not json.loads(paths[0].read_text(encoding="utf-8"))["persisted"]
        gateway.flush_deliveries()
        gateway.flush_deliveries()
        assert json.loads(paths[0].read_text(encoding="utf-8"))["persisted"]
    finally:
        gateway.close()
    assert requests[0] == requests[1] and len(requests) == 2 and len(committed) == 1


def test_reply_decision_id_is_reused_for_metadata_recovery_and_cannot_change_content(config):
    requests = []
    identity = str(uuid4())
    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            raise httpx.ReadTimeout('response lost', request=request)
        return httpx.Response(200, json={'delivery_id': identity, 'user_message_id': 1, 'assistant_message_id': 2})
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    path = b.PRIVATE / 'decisions' / (identity + '.json')
    b.save_json(path, {'decision_id': identity, 'status': 'record_pending', 'sent': True})
    try:
        with pytest.raises(b.BridgeError, match='已经确认'):
            gateway.confirm_delivery('叶子：在吗', '在呢。', delivery_id=identity)
        gateway.flush_deliveries()
        receipt = gateway.confirm_delivery('叶子：在吗', '在呢。', delivery_id=identity)
        with pytest.raises(b.BridgeError, match='内容冲突'):
            gateway.confirm_delivery('叶子：在吗', '变了的回复', delivery_id=identity)
        gateway.flush_deliveries()
    finally:
        gateway.close()
    assert requests[0] == requests[1] and len(requests) == 2
    assert receipt['delivery_id'] == identity
    assert json.loads(path.read_text(encoding='utf-8'))['status'] == 'completed'
    stored = json.loads((b.PRIVATE / 'deliveries' / (identity + '.json')).read_text(encoding='utf-8'))
    assert stored['persisted'] and stored['payload']['text'] == '在呢。'


@pytest.mark.parametrize('outcome', ['confirmed', 'stop', 'draft', 'input', 'receipt'])
def test_game_delivery_pipeline_only_commits_after_confirmed_echo(
        server, config, monkeypatch, receipt_clock, outcome):
    gm, sd, committed = server
    saved, requested = [], []
    monkeypatch.setattr(gm, 'verify_token', lambda _: True)
    monkeypatch.setattr(gm, 'build_context', lambda *a, **kw: '')
    monkeypatch.setattr(sd.db, 'save_chat_message', lambda *args: saved.append(args))
    class Upstream:
        async def post(self, *a, **kw):
            return httpx.Response(200, json={'choices': [{'message': {'content': '在呢。'}}]})
    monkeypatch.setattr(gm, 'http_client', Upstream())
    def respond(request):
        requested.append(request.url.path)
        if request.method == 'GET':
            return httpx.Response(200, json={'sky_delivery_protocol': 1})
        if request.url.path == '/v1/sky/chat/completions':
            assert not committed
            response = asyncio.run(gm.sky_chat_preview(Request(json.loads(request.content))))
            if outcome == 'stop':
                companion.stop.set()
        else:
            assert request.url.path == '/v1/sky/deliveries'
            assert json.loads((b.PRIVATE / 'last_send.json').read_text(encoding='utf-8'))['confirmed']
            response = commit(gm, json.loads(request.content))
        return httpx.Response(response.status_code, content=response.body)
    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    device, reads = FakeDevice(), 0
    if outcome == 'draft':
        device.draft = '手动输入的草稿'
    if outcome == 'input':
        device.corrupt_text = '输入错字'
    def reader(_):
        nonlocal reads
        reads += 1
        return screen(history=['在呢。'] if reads > 1 and outcome == 'confirmed' else [])
    companion = b.Companion(config, device, gateway, reader=reader)
    companion.collect = lambda: companion.inbox.add(
        [{'sender': '叶子', 'text': '在吗'}], b.time.monotonic())
    acknowledge = companion.inbox.acknowledge
    def finish():
        acknowledge()
        companion.stop.set()
    monkeypatch.setattr(companion.inbox, 'acknowledge', finish)
    try:
        if outcome in ('draft', 'input', 'receipt'):
            with pytest.raises(b.BridgeError):
                companion.run()
        else:
            companion.run()
    finally:
        gateway.close()
    assert saved == []
    assert len(committed) == (1 if outcome == 'confirmed' else 0)
    assert requested.count('/v1/sky/deliveries') == (1 if outcome == 'confirmed' else 0)
    assert len(device.keys) == (1 if outcome in ('confirmed', 'receipt') else 0)
    paths = list((b.PRIVATE / 'decisions').glob('*.json'))
    assert len(paths) == 1
    decision = json.loads(paths[0].read_text(encoding='utf-8'))
    assert decision['status'] == {'confirmed': 'completed', 'stop': 'cancelled',
                                  'draft': 'failed_before_send', 'input': 'failed_before_send',
                                  'receipt': 'unconfirmed'}[outcome]
    if outcome == 'confirmed':
        assert decision['decision_id'] == next(iter(committed))
