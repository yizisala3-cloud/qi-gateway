"""Additional independent storage probes; synthetic data, no external services."""
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
from unittest import mock

import pytest
from gateway import model_config as registry


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "models.sqlite3"))


def provider():
    return {"name": "Independent", "base_url": "https://independent.invalid/v1",
            "models": ["independent-model"],
            "keys": [{"name": "synthetic", "value": "opaque_independent_value_123456"}]}


def test_failed_first_publication_never_exposes_empty_store():
    with mock.patch.object(registry.os, "link", side_effect=OSError("synthetic publication failure")):
        with pytest.raises(registry.ConfigError):
            registry.mutate(0, "provider", provider())
    assert not registry._path().exists()
    assert list(registry._path().parent.iterdir()) == []
    assert registry.public()["revision"] == 0


def test_failed_existing_transaction_keeps_exact_database_contents():
    registry.mutate(0, "provider", provider())
    digest = hashlib.sha256(registry._path().read_bytes()).hexdigest()
    with pytest.raises(registry.ConfigError):
        registry.mutate(0, "provider", provider())
    assert hashlib.sha256(registry._path().read_bytes()).hexdigest() == digest
    with pytest.raises(registry.ConfigError):
        registry.mutate(1, "chat", {"provider_id": "missing", "key_id": "missing", "model": "missing"})
    assert hashlib.sha256(registry._path().read_bytes()).hexdigest() == digest


def test_first_creation_across_real_processes_publishes_one_complete_state():
    code = r'''
import json
from gateway import model_config as registry
try:
    value = registry.mutate(0, "provider", {"name":"Process fixture", "base_url":"https://process.invalid/v1",
        "models":["model-a"], "keys":[{"name":"Synthetic", "value":"opaque_process_fixture_123456"}]})
    result = value["revision"]
except registry.ConfigError as exc:
    result = exc.status
print(json.dumps(result))
'''
    def save(_):
        result = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(),
                                capture_output=True, text=True, check=True, timeout=30)
        return json.loads(result.stdout)
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        assert sorted(executor.map(save, range(4))) == [1, 409, 409, 409]
    state = registry.load()
    assert state["revision"] == 1 and len(state["providers"]) == 1
    assert list(registry._path().parent.glob(registry._path().name + ".*.tmp")) == []


def test_actual_status_blocks_console_boot_while_management_auth_remains_valid(monkeypatch):
    from gateway import main
    from gateway.model_config_api import model_config_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient
    for suffix, value in (("BASE_URL", "https://environment.invalid/v1"),
                          ("API_KEY", "synthetic-status-key"), ("MODEL", "old-model")):
        monkeypatch.setattr(registry.cfg, "UPSTREAM_" + suffix, value)
    registry.mutate(0, "chat", {"provider_id": "env-upstream", "key_id": "environment", "model": "old-model"})
    monkeypatch.setattr(registry.cfg, "UPSTREAM_MODEL", "new-model")
    monkeypatch.setattr(registry.cfg, "GATEWAY_TOKEN", "synthetic-status-auth")
    monkeypatch.setattr(main.db, "get_client_status", lambda: {"elevated_active": False})
    app = Starlette(routes=[main.Route("/status", main.status), *model_config_routes])
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer synthetic-status-auth"}
        metadata = client.get("/admin/api/model-config", headers=headers)
        status = client.get("/status", headers=headers)
    assert metadata.status_code == 200
    assert next(p for p in metadata.json()["purposes"] if p["id"] == "chat")["current"]["available"] is False
    assert status.status_code == 409
