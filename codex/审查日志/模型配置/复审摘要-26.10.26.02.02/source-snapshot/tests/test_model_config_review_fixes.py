"""Permanent boundary regressions for BUG-01/03/04; no real credentials or services."""
import json
import logging
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

import pytest
from gateway import logging_events as events
from gateway import model_config as registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "models.sqlite3"))
    monkeypatch.setattr(events, "_runtime_secret_values", set())
    for prefix in ("UPSTREAM", "ANALYSIS", "CONTINUITY", "RUMINATION"):
        for suffix, value in (("BASE_URL", "https://environment.invalid/v1"),
                              ("API_KEY", "opaque_environment_long_987654"), ("MODEL", "env-model")):
            monkeypatch.setattr(registry.cfg, prefix + "_" + suffix, value)


KEYS = ["opaque_first_vendor_secret_111111", "opaque_second_vendor_secret_222222"]


def seed():
    state = registry.mutate(0, "provider", {"name": "Fixture", "base_url": "https://fixture.invalid/v1",
        "models": ["model-a"], "keys": [{"name": str(i), "value": value} for i, value in enumerate(KEYS)]})
    return state, next(p for p in state["providers"] if not p["readonly"])


def bind(provider):
    return {"provider_id": provider["id"], "key_id": provider["keys"][0]["id"], "model": "model-a"}


def test_all_keys_and_retired_inflight_keys_are_scrubbed_without_storage_io(monkeypatch):
    state, provider = seed()
    registry.mutate(1, "chat", bind(provider))
    new_key = "opaque_replacement_vendor_secret_333333"
    with registry.snapshot():
        original = registry.resolve("chat")
        payload = {k: provider[k] for k in ("id", "name", "base_url", "models")}
        payload["keys"] = [{"id": k["id"], "name": k["name"], "value": new_key if i == 0 else ""}
                           for i, k in enumerate(provider["keys"])]
        registry.mutate(2, "provider", payload)
        assert registry.resolve("chat").api_key == original.api_key == KEYS[0]
    assert registry.resolve("chat").api_key == new_key
    monkeypatch.setattr(registry, "load", mock.Mock(side_effect=AssertionError("log path must not load storage")))
    try:
        try:
            raise ValueError(KEYS[1])
        except ValueError as exc:
            raise RuntimeError(KEYS[0] + " " + new_key) from exc
    except RuntimeError:
        info = sys.exc_info()
    raw = " | ".join(KEYS + [new_key])
    for sink in ("error", "event", "platform"):
        record = logging.LogRecord("gateway.main", logging.ERROR, __file__, 1, raw, (), info)
        if sink == "error":
            output = events.scrub_error_text(raw)
        elif sink == "event":
            output = json.dumps(events._build_event_from_record(record))
        else:
            events._PlatformScrubFilter().filter(record)
            output = logging.Formatter().format(record)
        assert all(key not in output for key in KEYS + [new_key])
    registry.load.assert_not_called()


def test_runtime_scrubbing_survives_another_worker_style_reload(monkeypatch):
    seed()
    events._runtime_secret_values.clear()
    registry.load()
    assert all(key not in events.scrub_error_text(" ".join(KEYS)) for key in KEYS)


@pytest.mark.parametrize("change", ["model", "key"])
def test_invalid_environment_bindings_are_visible_and_repaired_individually(monkeypatch, change):
    state = registry.public()
    choice = {"provider_id": "env-upstream", "key_id": "environment", "model": "env-model"}
    for purpose in ("qq", "sky", "rumination"):
        state = registry.mutate(state["revision"], purpose, choice)
    monkeypatch.setattr(registry.cfg, "UPSTREAM_MODEL" if change == "model" else "UPSTREAM_API_KEY",
                        "new-env-model" if change == "model" else "")
    state = registry.public()
    for purpose in ("qq", "sky", "rumination"):
        item = next(p for p in state["purposes"] if p["id"] == purpose)
        assert item["selection"] == choice
        assert item["current"]["available"] is False and item["current"]["error"]
        with pytest.raises(registry.ConfigError):
            registry.resolve(purpose)
    # Unrelated provider additions must remain available while old bindings are invalid.
    state = registry.mutate(state["revision"], "provider", {"name": "Repair", "base_url": "https://fixture.invalid/v1",
        "models": ["model-a"], "keys": [{"name": "repair", "value": KEYS[0]}]})
    provider = next(p for p in state["providers"] if not p["readonly"])
    state = registry.mutate(state["revision"], "qq", bind(provider))
    assert registry.resolve("qq").model == "model-a"
    for purpose in ("sky", "rumination"):
        state = registry.mutate(state["revision"], purpose, None)
    assert all(p["current"]["available"] for p in state["purposes"])


@pytest.mark.parametrize("damage", ["missing-row", "missing-table", "empty-file", "truncated", "json"])
def test_failed_save_preserves_damaged_existing_store(damage):
    seed()
    if damage in ("missing-row", "missing-table", "json"):
        with sqlite3.connect(registry._path()) as conn:
            if damage == "missing-row": conn.execute("DELETE FROM model_config")
            elif damage == "missing-table": conn.execute("DROP TABLE model_config")
            else: conn.execute("UPDATE model_config SET value='{' WHERE id=1")
    else:
        registry._path().write_bytes(b"" if damage == "empty-file" else b"SQLite format 3\0broken")
    before = registry._path().read_bytes()
    with pytest.raises(registry.ConfigError): registry.load()
    with pytest.raises(registry.ConfigError): registry.mutate(2, "chat", None)
    assert registry._path().read_bytes() == before
    with pytest.raises(registry.ConfigError): registry.load()


def test_first_failed_creation_does_not_publish_empty_state():
    with pytest.raises(registry.ConfigError): registry.mutate(2, "chat", None)
    assert not registry._path().exists()
    with pytest.raises(registry.ConfigError): registry.mutate(0, "provider", {})
    assert not registry._path().exists()
    assert list(registry._path().parent.glob(registry._path().name + ".*.tmp")) == []


def test_concurrent_first_creation_only_publishes_one_valid_state():
    def save(_):
        try: return seed()[0]["revision"]
        except registry.ConfigError as exc: return exc.status
    with ThreadPoolExecutor(max_workers=4) as executor:
        assert sorted(executor.map(save, range(4))) == [1, 409, 409, 409]
    assert registry.load()["revision"] == 1
    assert len(registry.load()["providers"]) == 1
