"""Independent review probes. Synthetic secrets and temporary SQLite only."""
import asyncio
import json
import logging
import sqlite3

import httpx
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import model_config as registry
from gateway import logging_events
from gateway.model_config_api import model_config_routes


KEY = "review_runtime_opaque_abcdef_987654"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "models.sqlite3"))
    for prefix in ("UPSTREAM", "ANALYSIS", "CONTINUITY", "RUMINATION"):
        for suffix, value in (("BASE_URL", "https://environment.invalid/v1"),
                              ("API_KEY", "review-environment-placeholder"),
                              ("MODEL", "environment-model")):
            monkeypatch.setattr(registry.cfg, prefix + "_" + suffix, value)
    monkeypatch.setattr(registry.cfg, "GATEWAY_TOKEN", "review-auth-placeholder")
    monkeypatch.setattr(registry.cfg, "GATEWAY_KEY_REF_SECRET", "review-reference-placeholder-00000000000000")


def add_and_select():
    state = registry.mutate(0, "provider", {
        "name": "Review provider", "base_url": "https://provider.invalid/v1",
        "models": ["runtime-model"], "keys": [{"name": "Review key", "value": KEY}]})
    provider = next(p for p in state["providers"] if not p["readonly"])
    return registry.mutate(1, "chat", {"provider_id": provider["id"],
        "key_id": provider["keys"][0]["id"], "model": "runtime-model"})


@pytest.mark.parametrize("stream", [False, True])
def test_runtime_key_is_redacted_from_actual_chat_error(monkeypatch, stream):
    from gateway import main
    add_and_select()
    calls = []

    def upstream(request):
        calls.append(request.headers["authorization"])
        return httpx.Response(401, json={"error": {"message": f"Rejected credential {KEY}"}})

    transport = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(main, "http_client", transport)
    monkeypatch.setattr(main, "build_context", lambda *_: "")
    monkeypatch.setattr(main, "build_todo_feedback_guidance", lambda *_: "")
    try:
        with TestClient(Starlette(routes=[main.Route("/v1/chat/completions", main.chat_completions, methods=["POST"])])) as client:
            response = client.post("/v1/chat/completions",
                headers={"Authorization": "Bearer review-auth-placeholder"},
                json={"messages": [], "stream": stream})
        assert calls == ["Bearer " + KEY]
        assert KEY not in response.text, "Saved runtime key escaped through upstream error response"
    finally:
        asyncio.run(transport.aclose())


@pytest.mark.parametrize("sink", ["scrubber", "event", "platform"])
def test_runtime_key_is_redacted_in_log_scrubber(sink):
    add_and_select()
    with logging_events.credential_scope("chat", "provider.invalid", KEY):
        record = logging.LogRecord("gateway.main", logging.ERROR, __file__, 1,
                                   "Rejected credential %s", (KEY,), None)
        if sink == "event":
            event = logging_events._build_event_from_record(record)
            assert event["key_ref"] == logging_events.compute_key_ref(KEY)
            cleaned = json.dumps(event)
        elif sink == "platform":
            logging_events._PlatformScrubFilter().filter(record)
            cleaned = record.getMessage()
        else:
            cleaned = logging_events.scrub_error_text("Rejected credential " + KEY)
    assert KEY not in cleaned, "Runtime provider keys are absent from configured-secret scrubbing"


def test_environment_provider_model_update_keeps_management_readable(monkeypatch):
    public = registry.public()
    env_provider = next(p for p in public["providers"] if p["id"] == "env-upstream")
    for purpose in ("qq", "sky"):
        public = registry.mutate(public["revision"], purpose, {
            "provider_id": env_provider["id"], "key_id": "environment", "model": "environment-model"})
    monkeypatch.setattr(registry.cfg, "UPSTREAM_MODEL", "replacement-environment-model")
    # An unrelated purpose remains usable, but the management page must still
    # open so the user can repair the two old selections.
    assert registry.resolve("chat").model == "replacement-environment-model"
    headers = {"Authorization": "Bearer review-auth-placeholder"}
    with TestClient(Starlette(routes=model_config_routes)) as client:
        read = client.get("/admin/api/model-config", headers=headers)
        reset = client.put("/admin/api/model-config/routes/qq", headers=headers,
                           json={"revision": public["revision"], "value": None})
    assert (read.status_code, reset.status_code) == (200, 200), (
        f"Environment update locked management: read={read.status_code}, reset={reset.status_code}")


def test_failed_save_does_not_reinitialize_missing_state_row():
    add_and_select()
    with sqlite3.connect(registry._path()) as conn:
        conn.execute("DELETE FROM model_config WHERE id=1")
    with pytest.raises(registry.ConfigError):
        registry.load()
    with pytest.raises(registry.ConfigError) as raised:
        registry.mutate(2, "chat", None)
    assert raised.value.status in (409, 503)
    with pytest.raises(registry.ConfigError):
        registry.load()
