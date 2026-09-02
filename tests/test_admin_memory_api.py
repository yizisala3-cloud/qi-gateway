"""HTTP-level tests for the five admin memory lifecycle endpoints.

Auth must accept GATEWAY_TOKEN only -- the MCP memory token, the plugin
token, and anonymous requests all stay outside. Errors are stable
error_code values with Chinese messages; internal fields are refused at
the door.
"""

import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import admin_memory
from gateway.admin_memory_api import admin_memory_routes
from gateway.config import cfg

GATEWAY_TOKEN = "gateway-token-test"
# MCP 令牌与网关令牌完全不同：它绝不能获得管理后台的用户写权限。
MCP_TOKEN = "mcp-memory-token-test"

CREATE_OK = {
    "memory_id": 501,
    "continuity_id": "cu-1",
    "source": "manual",
    "verified": "verified",
    "is_active": True,
    "heat": 50.0,
}
MOMENT_DATA = {"scene": "聊天窗口", "event": "约定赶海", "moment_state": "standalone"}
VALID_CREATE = {
    "content": "这是一条经过管理后台写入的记忆。",
    "recall_tags": ["测试"],
    "continuity_type": "moment",
    "continuity_data": MOMENT_DATA,
}


class FakeBuilder:
    def __init__(self, outcome):
        self.outcome = outcome

    def execute(self):
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return SimpleNamespace(data=self.outcome)


class FakeClient:
    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls = []

    def rpc(self, name, payload):
        self.calls.append((name, payload))
        outcome = self.outcomes.get(name)
        if isinstance(outcome, Exception):
            raise outcome
        return FakeBuilder(outcome)


class AdminMemoryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(admin_memory_routes))

    def setUp(self):
        self.client = FakeClient({"create_admin_memory_v1": CREATE_OK})
        self.embedding_calls = []
        patches = [
            mock.patch.object(admin_memory, "get_client", lambda: self.client),
            mock.patch.object(admin_memory, "resolve_assistant_id", lambda: "assistant-test"),
            mock.patch.object(admin_memory, "_server_writes_allowed", lambda: True),
            mock.patch.object(
                admin_memory, "_recall_embedding",
                lambda scene: self.embedding_calls.append(scene) or [0.1, 0.2],
            ),
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = TestClient(self.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def test_gateway_token_is_required(self):
        for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer wrong"},
                        {"Authorization": f"Bearer {MCP_TOKEN}"}):
            with self.subTest(headers=headers):
                response = self.http.post("/admin/api/memories/manual", json=VALID_CREATE, headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error_code"], "unauthorized")
                self.assertFalse(self.client.calls)

    def test_create_returns_memory_id_with_gateway_token(self):
        response = self.http.post("/admin/api/memories/manual", json=VALID_CREATE, headers=self.auth)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["memory_id"], 501)
        self.assertEqual(self.client.calls[0][0], "create_admin_memory_v1")

    def test_internal_fields_are_refused_at_the_api_boundary(self):
        for field, value in (("assistant_id", "x"), ("source", "mcp_memory"), ("is_active", False)):
            with self.subTest(field=field):
                response = self.http.post(
                    "/admin/api/memories/manual",
                    json={**VALID_CREATE, field: value},
                    headers=self.auth,
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error_code"], "admin_memory_unsupported_field")

    def test_invalid_json_returns_stable_error(self):
        response = self.http.post(
            "/admin/api/memories/manual",
            content=b"not-json",
            headers={**self.auth, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_json")

    def test_business_conflicts_carry_stable_codes(self):
        cases = [
            ("edit", "/admin/api/memories/7/edit", {"title": "x"},
             "admin_memory_not_editable", 409),
            ("undo", "/admin/api/memories/9/undo-type-change", {},
             "admin_memory_previous_conflict", 409),
            ("restore", "/admin/api/memories/8/restore", {},
             "admin_memory_superseded", 409),
            ("edit-type-switch", "/admin/api/memories/7/edit",
             {"continuity_type": "profile",
              "continuity_data": {"facet": "作息", "statement": "习惯晚睡",
                                   "scope": "全局", "stability": "stable",
                                   "basis": "explicit_self_report"}},
             "admin_memory_type_change_forbidden", 400),
        ]
        for name, url, payload, code, status in cases:
            with self.subTest(endpoint=name):
                self.client.outcomes = {
                    "edit_admin_memory_v1": RuntimeError("admin_memory_not_editable"),
                    "undo_memory_type_change_v1": RuntimeError("admin_memory_previous_conflict"),
                    "restore_archived_memory_v1": RuntimeError("admin_memory_superseded"),
                }
                if name == "edit-type-switch":
                    self.client.outcomes["edit_admin_memory_v1"] = RuntimeError(
                        "admin_memory_type_change_forbidden")
                response = self.http.post(url, json=payload, headers=self.auth)
                self.assertEqual(response.status_code, status)
                body = response.json()
                self.assertFalse(body["success"])
                self.assertEqual(body["error_code"], code)

    def test_type_change_endpoint_wires_through(self):
        self.client.outcomes = {"change_memory_type_v1": {
            "memory": {"id": 9, "continuity_type": "episode"},
            "previous_version_id": 7,
            "removed_version_id": 6,
        }}
        response = self.http.post(
            "/admin/api/memories/7/change-type",
            json={
                "content": "类型修改后的全新版本正文。",
                "continuity_type": "episode",
                "continuity_data": {
                    "beginning": "开端", "development": "经过",
                    "outcome": "结局", "closure_quality": "complete",
                },
            },
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["memory_id"], 9)
        self.assertEqual(body["previous_version_id"], 7)

    def test_unexpected_failures_map_to_stable_error_without_leaking_details(self):
        # 未知 RPC 异常由服务层统一映射，原始报错文字不进入响应。
        self.client.outcomes = {"create_admin_memory_v1": RuntimeError("connection reset")}
        response = self.http.post("/admin/api/memories/manual", json=VALID_CREATE, headers=self.auth)
        self.assertEqual(response.status_code, 500)
        body = response.json()
        self.assertEqual(body["error_code"], "admin_memory_rpc_failed")
        self.assertNotIn("connection reset", body["error"])

    def test_generic_data_patch_no_longer_accepts_content_fields(self):
        from gateway.admin_api import _TABLES
        for field in ("content", "title", "tags", "importance", "source",
                      "is_active", "heat"):
            with self.subTest(field=field):
                self.assertNotIn(field, _TABLES["memories"]["write"])
        # 既有审核动作（确认/驳回）仍走 verified 字段。
        self.assertIn("verified", _TABLES["memories"]["write"])

    def test_archive_endpoint_archives_current_version(self):
        self.client.outcomes = {"archive_admin_memory_v1": {
            "memory": {"id": 8, "is_active": False, "heat": 60.0},
        }}
        response = self.http.post("/admin/api/memories/8/archive", json={}, headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertFalse(body["memory"]["is_active"])
        payload = self.client.calls[0][1]
        self.assertEqual(payload, {"p_memory_id": 8})

    def test_archive_endpoint_refuses_with_stable_codes(self):
        cases = [
            ("admin_memory_superseded", 409),
            ("admin_memory_already_archived", 409),
            ("admin_memory_not_archivable", 409),
            ("admin_memory_not_found", 404),
        ]
        for code, status in cases:
            with self.subTest(code=code):
                self.client.outcomes = {"archive_admin_memory_v1": RuntimeError(code)}
                response = self.http.post("/admin/api/memories/8/archive", json={}, headers=self.auth)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error_code"], code)

    def test_mcp_token_cannot_archive(self):
        response = self.http.post(
            "/admin/api/memories/8/archive", json={},
            headers={"Authorization": f"Bearer {MCP_TOKEN}"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertFalse(self.client.calls)


if __name__ == "__main__":
    unittest.main()
