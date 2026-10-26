"""Independent check of the auth route in the complete application router."""
from unittest import mock

from starlette.testclient import TestClient
from gateway import main, model_config


def test_full_app_auth_works_without_model_store_or_database(monkeypatch, tmp_path):
    token = "synthetic-full-router-auth-987654"
    monkeypatch.setattr(model_config.cfg, "GATEWAY_TOKEN", token)
    monkeypatch.setenv("MODEL_CONFIG_PATH", str(tmp_path / "damaged.sqlite3"))
    (tmp_path / "damaged.sqlite3").write_bytes(b"synthetic broken model database")
    with mock.patch.object(model_config, "load", side_effect=AssertionError("auth touched model store")) as load, \
         mock.patch.object(main.db, "get_client_status", side_effect=AssertionError("auth touched database")) as db_status:
        # No context manager: do not start production/background lifespan tasks.
        client = TestClient(main.app)
        try:
            response = client.get("/admin/api/auth", headers={"Authorization": "Bearer " + token})
            assert response.status_code == 200
            assert response.json() == {"authenticated": True}
            assert response.headers["cache-control"] == "no-store"
            assert token not in response.text
            denied = client.get("/admin/api/auth", headers={"Authorization": "Bearer wrong-synthetic-token"})
            assert denied.status_code == 401 and denied.headers["cache-control"] == "no-store"
            assert client.get("/admin/api/auth").status_code == 401
            assert client.post("/admin/api/auth").status_code == 405
        finally:
            client.close()
        load.assert_not_called()
        db_status.assert_not_called()
