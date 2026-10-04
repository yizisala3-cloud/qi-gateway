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

from gateway import planning, planning_runtime, planning_tasks
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

    # ── 创建请求幂等（清单 #9，2026-10-04） ──────────────────────────

    def test_create_same_key_retries_converge_to_one_task(self):
        """结果未知的重试（同键同内容）收敛到同一任务，不重复建任务。"""
        headers = {**self.auth, "Idempotency-Key": "create-key-1"}
        payload = {"content": "背单词", "task_type": "daily", "estimated_minutes": 30}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(first.json()["id"], retry.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        tasks = self.client.rows["planning_task"]
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0]["creation_request_key"], "create-key-1")

    def test_create_replay_restores_feedback_flags(self):
        """重放返回创建事件的真实反馈（首轮跳过口径不按重放时刻重算）。"""
        headers = {**self.auth, "Idempotency-Key": "create-key-skip"}
        payload = {"content": "晨读", "task_type": "daily", "estimated_minutes": 30,
                   "window_start_tod": "08:00", "window_end_tod": "09:00"}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertTrue(first.json()["first_round_skipped"])
        self.assertEqual(retry.status_code, 201)
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertTrue(retry.json()["first_round_skipped"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)
        self.assertEqual(len(self.client.rows["planning_occurrence"]), 0)

    def test_create_same_key_different_payload_rejected_409(self):
        """同键不同内容明确拒绝，不静默合并也不误伤首个任务。"""
        headers = {**self.auth, "Idempotency-Key": "create-key-2"}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=headers)
            second = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 45},
                headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error_code"], "request_conflict")
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    def test_create_different_keys_same_payload_creates_two_tasks(self):
        """不同键同内容 = 两个独立创建意图（不能按内容相同合并）。"""
        payload = {"content": "背单词", "task_type": "daily", "estimated_minutes": 30}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post(
                "/admin/api/planning/tasks",
                json=payload, headers={**self.auth, "Idempotency-Key": "key-a"})
            second = self.http.post(
                "/admin/api/planning/tasks",
                json=payload, headers={**self.auth, "Idempotency-Key": "key-b"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.client.rows["planning_task"]), 2)

    def test_create_replay_recovers_task_when_generation_failed(self):
        """任务已提交、同步生成暂时失败时，同键重试恢复同一任务并补生成。"""
        headers = {**self.auth, "Idempotency-Key": "create-key-rec"}
        payload = {"content": "背单词", "task_type": "daily", "estimated_minutes": 30}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            with mock.patch.object(planning_tasks, "_generate_created_task_quietly"):
                first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            self.assertEqual(first.status_code, 201)
            self.assertEqual(len(self.client.rows["planning_task"]), 1)
            self.assertEqual(self.client.rows["planning_occurrence"], [])
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)
        rounds = [row for row in self.client.rows["planning_occurrence"]
                  if row.get("task_id") == first.json()["id"]]
        self.assertEqual(len(rounds), 1)

    # ── R3（残留 A）：同键同内容的迟到重试先于时效准入重放 ──────────

    def test_create_late_retry_after_window_deadline_replays(self):
        """9/22 07:00 创建窗口 08:00–09:00 的 once 成功；09:01 同键重试
        必须重放同一任务，不得用当前时间重新否认已成立的创建。"""
        clock = {"now": _cst(2026, 9, 22, 7, 0)}
        headers = {**self.auth, "Idempotency-Key": "late-window-1"}
        payload = {"content": "晨读", "task_type": "once", "estimated_minutes": 30,
                   "target_date": "2026-09-22",
                   "window_start_tod": "08:00", "window_end_tod": "09:00"}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            clock["now"] = _cst(2026, 9, 22, 9, 1)
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    def test_create_late_retry_next_day_replays(self):
        """23:59 创建当天无窗口 once 成功；次日 00:01 同键重试重放同一任务
        （目标日期早于当前业务日期的下界校验不适用于已成立创建）。"""
        clock = {"now": _cst(2026, 9, 22, 23, 59)}
        headers = {**self.auth, "Idempotency-Key": "late-day-1"}
        payload = {"content": "常驻", "task_type": "once", "estimated_minutes": 30,
                   "target_date": "2026-09-22"}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            clock["now"] = _cst(2026, 9, 23, 0, 1)
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    def test_create_late_retry_after_boundary_change_replays(self):
        """创建与重试之间边界配置变化：同键同内容重放不读当前配置、
        不受首次创建时效准入影响。"""
        clock = {"now": _cst(2026, 9, 21, 12, 0)}
        headers = {**self.auth, "Idempotency-Key": "late-boundary-1"}
        payload = {"content": "晨读", "task_type": "once", "estimated_minutes": 30,
                   "target_date": "2026-09-22",
                   "window_start_tod": "08:00", "window_end_tod": "09:00"}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            self.http.patch(
                "/admin/api/planning/cycle",
                json={"refresh_boundary_time": "04:00"}, headers=self.auth)
            clock["now"] = _cst(2026, 9, 22, 10, 0)
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])

    def test_create_new_key_after_deadline_still_rejected(self):
        """首次创建（新 key）的时效准入保留：截止已过的同形请求仍 400。"""
        clock = {"now": _cst(2026, 9, 22, 7, 0)}
        payload = {"content": "晨读", "task_type": "once", "estimated_minutes": 30,
                   "target_date": "2026-09-22",
                   "window_start_tod": "08:00", "window_end_tod": "09:00"}
        with mock.patch.object(planning_runtime, "_now", lambda: clock["now"]):
            first = self.http.post(
                "/admin/api/planning/tasks", json=payload,
                headers={**self.auth, "Idempotency-Key": "fresh-1"})
            clock["now"] = _cst(2026, 9, 22, 9, 1)
            second = self.http.post(
                "/admin/api/planning/tasks", json=payload,
                headers={**self.auth, "Idempotency-Key": "fresh-2"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.json()["error_code"], "invalid_payload")
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    # ── R4（残留 B）：refresh_enabled / refresh_mode 纳入语义内容快照 ──

    def test_create_same_key_refresh_toggle_rejected_409(self):
        """同键其它字段相同但 refresh_enabled 翻转 = 不同创建意图：
        409 request_conflict，原任务与实例零副作用。"""
        headers = {**self.auth, "Idempotency-Key": "refresh-toggle-1"}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily",
                      "estimated_minutes": 30, "refresh_enabled": False},
                headers=headers)
            second = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily",
                      "estimated_minutes": 30, "refresh_enabled": True},
                headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["error_code"], "request_conflict")
        tasks = self.client.rows["planning_task"]
        self.assertEqual(len(tasks), 1)
        self.assertIs(tasks[0]["refresh_enabled"], False)
        self.assertEqual(self.client.rows["planning_occurrence"], [])

    def test_create_same_key_omitted_refresh_enabled_matches_explicit_true(self):
        """省略 refresh_enabled 与显式 true 语义等价：同键重放，不 409。"""
        headers = {**self.auth, "Idempotency-Key": "refresh-default-1"}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=headers)
            retry = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily",
                      "estimated_minutes": 30, "refresh_enabled": True},
                headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    def test_create_same_key_refresh_mode_semantic_default_matches(self):
        """省略 daily 的 refresh_mode 与显式传 daily 语义等价（审查补充
        观察 semantic_default）：同键重放，不 409。"""
        headers = {**self.auth, "Idempotency-Key": "mode-default-1"}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily", "estimated_minutes": 30},
                headers=headers)
            retry = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily",
                      "estimated_minutes": 30, "refresh_mode": "daily"},
                headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    def test_create_same_key_refresh_false_same_value_replays(self):
        """refresh_enabled=false 同键同值重放照常收敛。"""
        headers = {**self.auth, "Idempotency-Key": "refresh-false-1"}
        payload = {"content": "背单词", "task_type": "daily",
                   "estimated_minutes": 30, "refresh_enabled": False}
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
            retry = self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(retry.status_code, 201)
        self.assertEqual(retry.json()["id"], first.json()["id"])
        self.assertTrue(retry.json()["idempotent_replay"])
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

    def test_create_different_key_refresh_toggle_creates_independent_task(self):
        """不同键的 refresh_enabled 差异 = 两个独立创建意图（不按内容去重）。"""
        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            first = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily",
                      "estimated_minutes": 30, "refresh_enabled": False},
                headers={**self.auth, "Idempotency-Key": "toggle-a"})
            second = self.http.post(
                "/admin/api/planning/tasks",
                json={"content": "背单词", "task_type": "daily",
                      "estimated_minutes": 30, "refresh_enabled": True},
                headers={**self.auth, "Idempotency-Key": "toggle-b"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertNotEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(self.client.rows["planning_task"]), 2)

    def test_create_same_key_concurrent_requests_converge(self):
        """同键并发创建：唯一索引收敛到先提交者，失败方重读重放，
        全部请求返回同一任务，不重复建任务。"""
        headers = {**self.auth, "Idempotency-Key": "concurrent-1"}
        payload = {"content": "背单词", "task_type": "daily", "estimated_minutes": 30}
        from concurrent.futures import ThreadPoolExecutor

        def post(_):
            return self.http.post("/admin/api/planning/tasks", json=payload, headers=headers)

        with mock.patch.object(planning_runtime, "_now", lambda: NOW):
            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(post, range(8)))
        self.assertTrue(all(r.status_code == 201 for r in responses),
                        [r.status_code for r in responses])
        ids = {r.json()["id"] for r in responses}
        self.assertEqual(len(ids), 1, ids)
        self.assertTrue(any(r.json().get("idempotent_replay") for r in responses))
        self.assertEqual(len(self.client.rows["planning_task"]), 1)

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
