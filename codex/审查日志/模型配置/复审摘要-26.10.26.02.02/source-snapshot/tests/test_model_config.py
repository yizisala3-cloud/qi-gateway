"""Isolated model registry, API authorization, concurrency and read-only embedding."""
import concurrent.futures
import asyncio
import json
import os
from types import SimpleNamespace
from unittest import mock

import pytest
import httpx
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import model_config as registry
from gateway.model_config_api import model_config_routes


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "models.sqlite3"))
    for prefix in ("UPSTREAM", "ANALYSIS", "CONTINUITY", "RUMINATION"):
        for name, value in (("BASE_URL", "https://environment.invalid/v1"),
                            ("API_KEY", "private-environment-key"), ("MODEL", prefix.lower())):
            monkeypatch.setattr(registry.cfg, prefix + "_" + name, value)
    monkeypatch.setattr(registry.cfg, "GATEWAY_TOKEN", "isolated-model-test")


def provider():
    return {"name": "共享供应商", "base_url": "https://provider.invalid/v1",
            "models": ["model-a", "model-b"],
            "keys": [{"name": "个人", "value": "private-provider-key-a"},
                     {"name": "备用", "value": "private-provider-key-b"}]}


def add():
    state = registry.mutate(0, "provider", provider())
    item = next(p for p in state["providers"] if not p["readonly"])
    return state, item


def binding(item, index=0, model="model-a"):
    return {"provider_id": item["id"], "key_id": item["keys"][index]["id"], "model": model}


def test_reads_preserve_defaults_without_creating_storage():
    assert registry.resolve("qq").model == "upstream"
    assert not registry._path().exists()
    assert registry.public()["vector"]["readonly"] is True


def test_default_store_reuses_existing_persistent_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL_CONFIG_PATH")
    monkeypatch.setenv("QQBOT_SETTINGS_PATH", str(tmp_path / "persist" / "qqbot-settings.json"))
    assert registry._path() == tmp_path / "persist" / "model-config.sqlite3"
    assert not registry._path().exists()


def test_shared_library_independent_routes_and_restart():
    state, item = add()
    vector = state["vector"]
    state = registry.mutate(state["revision"], "chat", binding(item))
    state = registry.mutate(state["revision"], "qq", binding(item, 1, "model-b"))
    state = registry.mutate(state["revision"], "analysis", binding(item))
    assert registry.resolve("chat").api_key == "private-provider-key-a"
    assert registry.resolve("qq").api_key == "private-provider-key-b"
    assert registry.resolve("sky").model == "upstream"
    assert registry.resolve("continuity").model == "continuity"
    assert registry.public()["vector"] == vector
    assert registry.load()["revision"] == 4
    assert "private-provider-key-a" not in json.dumps(state)
    assert "private-environment-key" not in json.dumps(state)
    registry.mutate(4, "chat", None)
    assert registry.resolve("chat").model == "upstream"


def test_request_snapshot_is_fixed_during_changes():
    state, item = add()
    registry.mutate(1, "chat", binding(item))
    with registry.snapshot():
        original = registry.resolve("chat")
        registry.mutate(2, "chat", binding(item, 1, "model-b"))
        assert registry.resolve("chat") == original
    assert registry.resolve("chat").model == "model-b"
    assert "private-provider-key-b" not in repr(registry.resolve("chat"))


def test_edit_preserves_hidden_keys_and_rejects_removing_in_use_model():
    state, item = add()
    registry.mutate(1, "rumination", binding(item))
    edit = {k: item[k] for k in ("id", "name", "base_url", "models")}
    edit["keys"] = [{"id": k["id"], "name": k["name"], "value": ""} for k in item["keys"]]
    registry.mutate(2, "provider", edit)
    assert registry.resolve("rumination").api_key == "private-provider-key-a"
    edit["models"] = ["model-b"]
    with pytest.raises(registry.ConfigError):
        registry.mutate(3, "provider", edit)
    assert registry.load()["revision"] == 3


def test_concurrent_stale_writers_cannot_lose_changes():
    def write(_):
        try:
            return registry.mutate(0, "provider", provider())["revision"]
        except registry.ConfigError as exc:
            return exc.status
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(write, range(2))) == [1, 409]
    assert len(registry.load()["providers"]) == 1


@pytest.mark.parametrize("change", [
    {"base_url": "https://user:password@provider.invalid/v1"},
    {"base_url": "file:///tmp/models"}, {"models": []}, {"keys": []},
    {"keys": [{"name": "bad", "value": "secret\nheader"}]},
    {"id": "env-analysis"},
])
def test_invalid_provider_never_changes_active_config(change):
    with pytest.raises(registry.ConfigError):
        registry.mutate(0, "provider", provider() | change)
    assert registry.load()["revision"] == 0
    assert registry.resolve("analysis").model == "analysis"


def test_readonly_vector_and_corrupt_storage_fail_closed():
    with pytest.raises(registry.ConfigError):
        registry.mutate(0, "vector", {})
    registry._path().write_bytes(b"corrupt synthetic configuration")
    with pytest.raises(registry.ConfigError):
        registry.resolve("chat")


def test_wrong_schema_never_silently_reverts_to_environment():
    import sqlite3
    add()
    with sqlite3.connect(registry._path()) as conn:
        conn.execute("UPDATE model_config SET value=? WHERE id=1", ('{"providers":[{}],"routes":{}}',))
    with pytest.raises(registry.ConfigError) as raised:
        registry.resolve("chat")
    assert raised.value.status == 503


def test_model_storage_failure_does_not_break_plugin_credentials(monkeypatch):
    from gateway.credential_api import credential_api_routes
    monkeypatch.setattr(registry.cfg, "MEMORY_PLUGIN_TOKEN", "isolated-plugin-token")
    registry._path().write_bytes(b"corrupt model configuration")
    headers = {"Authorization": "Bearer isolated-model-test"}
    with TestClient(Starlette(routes=credential_api_routes)) as client:
        metadata = client.get("/admin/api/config/credentials", headers=headers)
        assert metadata.status_code == 200
        assert metadata.json()["model_config_error"]
        response = client.post("/admin/api/config/credentials/memory_plugin/reveal", headers=headers)
        assert response.json()["value"] == "isolated-plugin-token"


def test_all_purposes_can_choose_a_shared_provider():
    state, item = add()
    for purpose in registry.PURPOSES:
        state = registry.mutate(state["revision"], purpose, binding(item))
        assert registry.resolve(purpose).provider_id == item["id"]
    assert state["vector"]["model"] == "Qwen/Qwen3-Embedding-0.6B"


def test_api_authentication_metadata_and_errors():
    app = Starlette(routes=model_config_routes)
    with TestClient(app) as client:
        assert client.get("/admin/api/model-config").status_code == 401
        headers = {"Authorization": "Bearer isolated-model-test"}
        response = client.put("/admin/api/model-config/providers", headers=headers,
                              json={"revision": 0, "value": provider()})
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "private-provider-key-a" not in response.text
        assert "private-environment-key" not in response.text
        assert client.put("/admin/api/model-config/routes/vector", headers=headers,
                          json={"revision": 1, "value": None}).status_code == 400
        assert client.put("/admin/api/model-config/providers", headers=headers,
                          json={"revision": 0, "value": provider()}).status_code == 409
        assert client.put("/admin/api/model-config/providers", headers=headers, content="{").status_code == 400
        with mock.patch.object(registry.cfg, "GATEWAY_TOKEN", ""):
            assert client.get("/admin/api/model-config", headers=headers).status_code == 401


@pytest.mark.parametrize("stream", [False, True])
def test_chat_and_sky_send_independently_selected_configuration(monkeypatch, stream):
    from gateway import main
    state, item = add()
    registry.mutate(1, "chat", binding(item))
    registry.mutate(2, "sky", binding(item, 1, "model-b"))
    calls = []

    def upstream(request):
        calls.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=b'data: [DONE]\n\n')
        return httpx.Response(200, json={"choices": []})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(main, "http_client", transport)
    monkeypatch.setattr(main, "build_context", lambda *_: "")
    monkeypatch.setattr(main, "build_todo_feedback_guidance", lambda *_: "")
    monkeypatch.setattr(main.sky_settings, "snapshot", lambda **_: {"available": True, "enabled": True, "pause_epoch": "test"})
    # No context manager: do not enter the real application's lifespan.
    client = TestClient(main.app, raise_server_exceptions=True)
    try:
        headers = {"Authorization": "Bearer isolated-model-test"}
        response = client.post("/v1/chat/completions", headers=headers,
                               json={"messages": [], "model": "client-model", "stream": stream})
        assert response.status_code == 200
        response = client.post("/v1/sky/chat/completions",
                               headers=headers | {"x-sky-access-protocol": "2"}, json={"messages": []})
        assert response.status_code == 200
    finally:
        client.close()
        asyncio.run(transport.aclose())
    assert len(calls) == 2
    assert calls[0][1] == "Bearer private-provider-key-a"
    assert calls[0][2]["model"] == "model-a"
    assert calls[1][1] == "Bearer private-provider-key-b"
    assert calls[1][2]["model"] == "model-b"


def test_qq_request_uses_its_route_and_never_retries():
    from gateway.qqbot_model import QQModel
    from gateway.qqbot_config import QQConfig
    from gateway.qqbot_proactive import QQModelHTTPError
    from gateway.qqbot_stickers import StickerCatalog
    state, item = add()
    registry.mutate(1, "qq", binding(item, 1, "model-b"))
    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        return httpx.Response(429, json={"error": "synthetic rate limit"})

    model = QQModel(SimpleNamespace(post=post), QQConfig(), StickerCatalog(), context_builder=lambda *_a, **_k: "")
    with pytest.raises(QQModelHTTPError):
        asyncio.run(model.generate([], "test"))
    assert len(calls) == 1
    assert calls[0][1]["headers"]["Authorization"] == "Bearer private-provider-key-b"
    assert calls[0][1]["json"]["model"] == "model-b"


def test_summary_callers_switch_text_models_but_embedding_keeps_environment(monkeypatch):
    from gateway import memory_extract, memory_continuity_shadow, memory_rumination
    state, item = add()
    for purpose in ("analysis", "continuity", "rumination"):
        state = registry.mutate(state["revision"], purpose, binding(item, 1, "model-b"))
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/embeddings"):
            return httpx.Response(200, json={"data": [{"embedding": [0.5] * 1024}]})
        content = '{"candidates":[]}' if len(calls) == 2 else '{"memories":[]}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with mock.patch.object(httpx, "Client") as client:
        client.return_value.__enter__.return_value.post.side_effect = post
        assert memory_extract._extract_memories("synthetic conversation")[0] == []
        assert memory_continuity_shadow.extract_continuity_candidates("synthetic conversation", {}) == []
        assert memory_rumination._call_rumination_model("synthetic conversation")
        assert len(memory_extract._get_embedding_sync("synthetic memory")) == 1024
    for url, kwargs in calls[:3]:
        assert url == "https://provider.invalid/v1/chat/completions"
        assert kwargs["json"]["model"] == "model-b"
        assert kwargs["headers"]["Authorization"] == "Bearer private-provider-key-b"
    url, kwargs = calls[3]
    assert url == "https://environment.invalid/v1/embeddings"
    assert kwargs["json"]["model"] == "Qwen/Qwen3-Embedding-0.6B"
    assert kwargs["headers"]["Authorization"] == "Bearer private-environment-key"

