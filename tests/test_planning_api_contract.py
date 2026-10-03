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

from gateway import planning, planning_runtime
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests.support.planning_context import CST, cst as _cst
from tests.support.planning_db import CoreClient as _Client

GATEWAY_TOKEN = "gateway-token-test"

MODULE = "gateway.planning"
NOW = _cst(2026, 9, 20, 14, 7)


class PlanningApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(planning_api_routes))

    def setUp(self):
        self.client = _Client()

        def fake_load(key):
            for row in self.client.rows["app_settings"]:
                if row.get("key") == key:
                    return row.get("value")
            return None

        def fake_save(key, value):
            for row in self.client.rows["app_settings"]:
                if row.get("key") == key:
                    row["value"] = value
                    return True
            self.client.rows["app_settings"].append({"key": key, "value": value})
            return True

        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(planning_runtime, "get_client", lambda: self.client),
            mock.patch.object(planning.db, "load_app_setting", fake_load),
            mock.patch.object(planning.db, "load_app_settings", lambda keys: {
                key: planning.db.load_app_setting(key) for key in keys
            }),
            mock.patch.object(planning.db, "save_app_setting", fake_save),
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
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
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
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
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

    def test_half_estimate_interval_patch_returns_chinese_reason(self):
        # #3：显式 est_end=null 的半区间预估修改返回 400 中文业务原因
        # （此前泄漏英文领域 ValueError「estimated time must be a complete
        # interval」），且零写入。
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=self.auth,
            )
            planning.generate_due(NOW)
            occ = self.client.rows["planning_occurrence"][0]
            self.assertIsNotNone(occ["est_start"])
            original = (occ["est_start"], occ["est_end"])
            response = self.http.patch(
                f"/admin/api/planning/occurrences/{occ['id']}",
                json={"est_start": "2026-09-20T15:00:00+08:00", "est_end": None},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(body["error_code"], "invalid_payload")
        self.assertNotIn("complete interval", body["error"])  # 不再泄漏英文领域文本
        self.assertIn("预估", body["error"])
        self.assertEqual((occ["est_start"], occ["est_end"]), original)  # 零写入
        # 对照（既有语义不回归）：省略 est_end 仍按预计耗时自动补终点。
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            ok = self.http.patch(
                f"/admin/api/planning/occurrences/{occ['id']}",
                json={"est_start": "2026-09-20T15:00:00+08:00"},
                headers=self.auth,
            )
        self.assertEqual(ok.status_code, 200)
        row = self.client.rows["planning_occurrence"][0]
        self.assertEqual(row["est_end"], planning._iso(_cst(2026, 9, 20, 15, 30)))

    def test_manual_recompute_and_wait_state(self):
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
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

    def test_complete_early_only_for_refreshable_tasks(self):
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            created = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=self.auth,
            ).json()
            response = self.http.post(
                f"/admin/api/planning/tasks/{created['id']}/complete-early",
                headers={**self.auth, "Idempotency-Key": "contract-early-1"},
            )
        # 每日待办没有「提前完成」：当天轮次始终存在，直接完成即可
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error_code"], "invalid_transition")


    def _seed_daily_round(self, clock):
        """建每日任务并生成当前轮、开始执行；返回 occurrence id。"""
        self.http.post(
            "/admin/api/planning/tasks",
            json={"content": "整理房间", "task_type": "daily", "estimated_minutes": 30},
            headers=self.auth,
        )
        planning.generate_due(clock["now"])
        occ = self.client.rows["planning_occurrence"][0]
        planning.start_occurrence(occ["id"], clock["now"])
        return occ["id"]

    def test_finish_with_manual_logged_duration_stores_seconds_and_keeps_auto_facts(self):
        # #19（2026-10-01 §12.3）：完成时手填 1h1m1s → 独立秒粒度字段落库；
        # 真实起止与自动耗时（actual_minutes）照常记录、不被手填覆盖。
        clock = {"now": NOW}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            occ_id = self._seed_daily_round(clock)
            clock["now"] = NOW + timedelta(minutes=35)
            response = self.http.post(
                f"/admin/api/planning/occurrences/{occ_id}/finish",
                json={"actual_logged_duration": "1h1m1s"},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["actual_logged_seconds"], 3661)
        self.assertEqual(body["status"], "completed")
        row = self.client.rows["planning_occurrence"][0]
        self.assertEqual(row["actual_logged_seconds"], 3661)
        self.assertEqual(row["actual_minutes"], 35)
        self.assertIsNotNone(row["actual_start"])
        self.assertIsNotNone(row["actual_end"])

    def test_finish_accepts_bare_minutes_and_blank_input(self):
        # 无后缀「90」按 90 分钟（5400 秒）解析；留空 = 未手填（NULL）。
        clock = {"now": NOW}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            occ_id = self._seed_daily_round(clock)
            response = self.http.post(
                f"/admin/api/planning/occurrences/{occ_id}/finish",
                json={"actual_logged_duration": "90"},
                headers=self.auth,
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["actual_logged_seconds"], 5400)
            row = self.client.rows["planning_occurrence"][0]
            self.assertEqual(row["status"], "completed")
            self.assertEqual(row["actual_logged_seconds"], 5400)

        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "擦桌子", "task_type": "daily", "estimated_minutes": 10},
                headers=self.auth,
            )
            planning.generate_due(clock["now"])
            occ_id = self.client.rows["planning_occurrence"][-1]["id"]
            response = self.http.post(
                f"/admin/api/planning/occurrences/{occ_id}/finish",
                json={"actual_logged_duration": "   "},
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200, response.text)
        row = self.client.rows["planning_occurrence"][-1]
        self.assertIsNone(row.get("actual_logged_seconds"))
        # 序列化始终携带该字段（前端按 NULL 走「预估耗时」标注口径）
        self.assertIn("actual_logged_seconds", response.json())

    def test_finish_without_body_keeps_legacy_contract(self):
        # /finish 原本无 body 仍可用（body 可选端点契约）。
        clock = {"now": NOW}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            occ_id = self._seed_daily_round(clock)
            response = self.http.post(
                f"/admin/api/planning/occurrences/{occ_id}/finish",
                headers=self.auth,
            )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.rows["planning_occurrence"][0]["status"], "completed")

    def test_finish_rejects_invalid_logged_duration_with_chinese_reason(self):
        clock = {"now": NOW}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            occ_id = self._seed_daily_round(clock)
            for bad, fragment in (
                ("45x", "格式"),
                ("-5m", "格式"),
                ("0", "不能为 0 或负数"),
                ("0m", "不能为 0 或负数"),
                ("1441m", "不能超过 24 小时"),
                ("1h1m1s1", "格式"),
            ):
                with self.subTest(bad=bad):
                    response = self.http.post(
                        f"/admin/api/planning/occurrences/{occ_id}/finish",
                        json={"actual_logged_duration": bad},
                        headers=self.auth,
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertEqual(response.json()["error_code"], "invalid_payload")
                    self.assertIn(fragment, response.json()["error"])
            # 非文本（JSON 数字 / 布尔）同样以中文拒绝——文本语义（无后缀
            # 默认分钟）只对字符串成立，数字单位有歧义。
            for bad in (90, True):
                with self.subTest(bad=bad):
                    response = self.http.post(
                        f"/admin/api/planning/occurrences/{occ_id}/finish",
                        json={"actual_logged_duration": bad},
                        headers=self.auth,
                    )
                    self.assertEqual(response.status_code, 400, response.text)
                    self.assertIn("须为时长文本", response.json()["error"])
            # 非法输入零写入：实例仍开放、无手填值。
            row = self.client.rows["planning_occurrence"][0]
            self.assertEqual(row["status"], "in_progress")
            self.assertIsNone(row.get("actual_logged_seconds"))


if __name__ == "__main__":
    unittest.main()
