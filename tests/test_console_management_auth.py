"""Console identity is independent of model readiness and configuration storage."""
from unittest import mock

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from gateway import credential_api, model_config
from gateway.model_config_api import model_config_routes

TOKEN = "synthetic-console-auth-987654"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "models.sqlite3"))
    monkeypatch.setattr(credential_api.cfg, "GATEWAY_TOKEN", TOKEN)
    for prefix in ("UPSTREAM", "ANALYSIS", "CONTINUITY", "RUMINATION"):
        for suffix, value in (("BASE_URL", "https://fixture.invalid/v1"),
                              ("API_KEY", "synthetic-fixture-secret-987654"), ("MODEL", "old-model")):
            monkeypatch.setattr(model_config.cfg, prefix + "_" + suffix, value)


def test_authentication_has_no_model_or_database_dependency():
    with mock.patch.object(model_config, "load", side_effect=AssertionError("authentication must not read model storage")) as load:
        with TestClient(Starlette(routes=credential_api.credential_api_routes)) as client:
            response = client.get("/admin/api/auth", headers={"Authorization": "Bearer " + TOKEN})
    assert response.status_code == 200
    assert response.json() == {"authenticated": True}
    assert response.headers["cache-control"] == "no-store"
    assert TOKEN not in response.text
    load.assert_not_called()


@pytest.mark.parametrize("value", [None, "Bearer wrong-token", "Bearer "])
def test_authentication_rejects_missing_and_invalid_tokens(value):
    headers = {} if value is None else {"Authorization": value}
    with TestClient(Starlette(routes=credential_api.credential_api_routes)) as client:
        response = client.get("/admin/api/auth", headers=headers)
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_unconfigured_gateway_token_does_not_open_console(monkeypatch):
    monkeypatch.setattr(credential_api.cfg, "GATEWAY_TOKEN", "")
    with TestClient(Starlette(routes=credential_api.credential_api_routes)) as client:
        assert client.get("/admin/api/auth", headers={"Authorization": "Bearer " + TOKEN}).status_code == 401


@pytest.mark.parametrize("purposes", [("chat",), ("chat", "qq", "sky")])
@pytest.mark.parametrize("change", ["model", "key"])
def test_actual_status_failure_does_not_block_management_identity_or_repair(monkeypatch, purposes, change):
    from gateway import main
    state = model_config.public()
    for purpose in purposes:
        state = model_config.mutate(state["revision"], purpose, {
            "provider_id": "env-upstream", "key_id": "environment", "model": "old-model"})
    monkeypatch.setattr(model_config.cfg, "UPSTREAM_MODEL" if change == "model" else "UPSTREAM_API_KEY",
                        "new-model" if change == "model" else "")
    monkeypatch.setattr(main.db, "get_client_status", lambda: {"elevated_active": False})
    routes = [*credential_api.credential_api_routes, *model_config_routes, Route("/status", main.status)]
    with TestClient(Starlette(routes=routes)) as client:
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.get("/status", headers=headers).status_code == 409
        assert client.get("/admin/api/auth", headers=headers).status_code == 200
        metadata = client.get("/admin/api/model-config", headers=headers).json()
        assert all(not p["current"]["available"] for p in metadata["purposes"] if p["id"] in purposes)
        for purpose in purposes:
            response = client.put("/admin/api/model-config/routes/" + purpose, headers=headers,
                                  json={"revision": metadata["revision"], "value": None})
            assert response.status_code == 200
            metadata = response.json()
        assert client.get("/status", headers=headers).status_code == 200
    assert all(p["selection"] is None for p in metadata["purposes"])
