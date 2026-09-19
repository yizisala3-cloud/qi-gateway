"""规划管理 API 契约测试（HTTP 层，参照 test_admin_memory_api.py 惯例）。

鉴权只认 Bearer GATEWAY_TOKEN；错误返回 ``{"error"[, "error_code"]}``
配 400/401/404/422/500。
"""

import importlib.util
import sys
import types
import unittest
from datetime import timedelta, timezone
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests.test_planning import CST, _Client, _cst

GATEWAY_TOKEN = "gateway-token-test"

MODULE = "gateway.planning"
NOW = _cst(2026, 9, 20, 14, 7)


class PlanningApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(planning_api_routes))

    def setUp(self):
        self.client = _Client()
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(planning, "get_client", lambda: self.client),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = TestClient(self.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def test_gateway_token_is_required(self):
        for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                response = self.http.get("/admin/api/planning/today", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error_code"], "unauthorized")

    def test_create_task_returns_201(self):
        response = self.http.post(
            "/admin/api/planning/tasks",
            json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual(body["task_type"], "daily")
        self.assertTrue(body["is_active"])

    def test_invalid_task_payload_is_400_with_code(self):
        response = self.http.post(
            "/admin/api/planning/tasks",
            json={"content": "", "task_type": "daily"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_payload")

    def test_invalid_json_body_is_400(self):
        response = self.http.post(
            "/admin/api/planning/tasks",
            content=b"not-json",
            headers={**self.auth, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_json")

    def test_missing_task_returns_404(self):
        response = self.http.patch(
            "/admin/api/planning/tasks/999",
            json={"content": "新内容"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error_code"], "not_found")

    def test_today_board_shape(self):
        with mock.patch.object(planning, "_now", lambda: NOW):
            self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=self.auth,
            )
            planning.generate_due(NOW)
            response = self.http.get("/admin/api/planning/today", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["date"], "2026-09-20")
        self.assertEqual(len(body["progress"]), 1)
        self.assertEqual(body["progress"][0]["status"], "pending")
        self.assertEqual(body["attention"], [])
        self.assertEqual(body["done"], [])
        self.assertIn("recompute", body)

    def test_status_transition_rules_surface_as_422(self):
        with mock.patch.object(planning, "_now", lambda: NOW):
            created = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=self.auth,
            ).json()
            planning.generate_due(NOW)
            occ = self.client.rows["planning_occurrence"][0]
            self.client.rows["planning_occurrence"][0]["status"] = "timeout"
            response = self.http.post(
                f"/admin/api/planning/occurrences/{occ['id']}/status",
                json={"status": "completed"},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "invalid_transition")

    def test_manual_recompute_and_wait_state(self):
        with mock.patch.object(planning, "_now", lambda: NOW):
            state = self.http.get("/admin/api/planning/recompute", headers=self.auth).json()
            self.assertFalse(state["pending"])
            response = self.http.post("/admin/api/planning/recompute", headers=self.auth)
            self.assertEqual(response.status_code, 200)
            self.assertIn("updated", response.json())

    def test_reorder_requires_order_payload(self):
        response = self.http.post(
            "/admin/api/planning/reorder", json={}, headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_payload")

    def test_occurrence_filters_reject_unknown_status(self):
        response = self.http.get(
            "/admin/api/planning/occurrences?status=bogus", headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_complete_early_only_for_interval_tasks(self):
        with mock.patch.object(planning, "_now", lambda: NOW):
            created = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=self.auth,
            ).json()
            response = self.http.post(
                f"/admin/api/planning/tasks/{created['id']}/complete-early",
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "invalid_transition")


if __name__ == "__main__":
    unittest.main()
