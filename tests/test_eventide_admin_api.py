"""HTTP-level tests for the Eventide admin endpoints.

Covers the injection switch (GET/PUT) and the read-only body endpoint's
three branches: no stored state, Eventide unavailable, and a normal mock
overview payload. Auth follows admin_api.py: Bearer GATEWAY_TOKEN only.
"""

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import app_settings, db, eventide_bridge
from gateway.config import cfg
from gateway.eventide_admin_api import eventide_admin_routes

GATEWAY_TOKEN = "gateway-token-test"

TICK_ISO = datetime.now(timezone.utc).isoformat()
FUTURE_ISO = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat()
PAST_ISO = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

STATE = {"version": 1}

OVERVIEW = {
    "fields": {
        "heat": {"value": 42, "level": "中低", "description": "微热", "label": "热度"},
        "pressure": {"value": 30, "level": "中低", "description": "尚可", "label": "压抑感"},
        "control": {"value": 70, "level": "中高", "description": "压着", "label": "控制力"},
        "sensitivity": {"value": 35, "level": "中低", "description": "轻微", "label": "敏感度"},
        "reserve": {"value": 20, "level": "中低", "description": "有一点", "label": "蓄积感"},
        "possessiveness": {"value": 40, "level": "中", "description": "在意", "label": "占有欲"},
        "fatigue": {"value": 15, "level": "低", "description": "还紧绷", "label": "疲惫感"},
        # 防御式映射：结构异常的条目被丢弃，而不是让接口 500。
        "bogus": "not-a-dict",
    },
    "cycle_label": "平稳期",
    "cycle_expires_at": FUTURE_ISO,
    "event_label": "低烧黏连",
    "event_description": None,
    "event_expires_at": PAST_ISO,
    "last_tick_at": TICK_ISO,
}


class EventideAdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(eventide_admin_routes))

    def setUp(self):
        app_settings.reset_settings_cache()
        self.addCleanup(app_settings.reset_settings_cache)
        self.client = TestClient(self.app)
        patcher = mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth(self):
        return {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    # ---- 注入开关 ----

    def test_settings_requires_token(self):
        resp = self.client.get("/admin/api/eventide/settings")
        self.assertEqual(resp.status_code, 401)
        resp = self.client.put(
            "/admin/api/eventide/settings", json={"inject_enabled": True}
        )
        self.assertEqual(resp.status_code, 401)

    def test_settings_rejects_wrong_token(self):
        resp = self.client.get(
            "/admin/api/eventide/settings", headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(resp.status_code, 401)

    def test_get_settings_reads_db_value(self):
        with mock.patch.object(db, "load_app_setting", return_value=False):
            resp = self.client.get("/admin/api/eventide/settings", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"inject_enabled": False})

    def test_put_settings_saves_and_resets_cache(self):
        with (
            mock.patch.object(db, "save_app_setting", return_value=True) as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
        ):
            resp = self.client.put(
                "/admin/api/eventide/settings",
                headers=self._auth(),
                json={"inject_enabled": False},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"inject_enabled": False})
        save.assert_called_once_with(app_settings.EVENTIDE_INJECT_KEY, False)
        reset.assert_called_once()

    def test_put_settings_invalid_bodies_return_400(self):
        for payload in ("a string", 123, {}, {"inject_enabled": "yes"}, {"inject_enabled": None}):
            resp = self.client.put(
                "/admin/api/eventide/settings", headers=self._auth(), json=payload
            )
            self.assertEqual(resp.status_code, 400, repr(payload))
        resp = self.client.put(
            "/admin/api/eventide/settings", headers=self._auth(), content=b"{not json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_settings_save_failure_returns_500(self):
        with mock.patch.object(db, "save_app_setting", return_value=False):
            resp = self.client.put(
                "/admin/api/eventide/settings",
                headers=self._auth(),
                json={"inject_enabled": True},
            )
        self.assertEqual(resp.status_code, 500)

    # ---- 身体状态（只读） ----

    def test_body_requires_token(self):
        resp = self.client.get("/admin/api/eventide/body")
        self.assertEqual(resp.status_code, 401)

    def test_body_without_state_reports_uninitialized(self):
        with (
            mock.patch.object(app_settings, "is_eventide_injection_enabled", return_value=True),
            mock.patch.object(db, "load_eventide_state", return_value=None),
        ):
            resp = self.client.get("/admin/api/eventide/body", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["initialized"], False)
        self.assertEqual(payload["inject_enabled"], True)
        self.assertIsNone(payload["cycle"])
        self.assertIsNone(payload["event"])
        self.assertEqual(payload["fields"], [])
        self.assertIsNone(payload["updated_at"])

    def test_body_without_eventide_runtime_reports_uninitialized(self):
        with (
            mock.patch.object(app_settings, "is_eventide_injection_enabled", return_value=True),
            mock.patch.object(db, "load_eventide_state", return_value=STATE),
            mock.patch.object(eventide_bridge, "get_body_overview", return_value=None),
        ):
            resp = self.client.get("/admin/api/eventide/body", headers=self._auth())
        payload = resp.json()
        self.assertEqual(payload["initialized"], False)
        self.assertEqual(payload["fields"], [])

    def test_body_renders_mock_overview(self):
        with (
            mock.patch.object(app_settings, "is_eventide_injection_enabled", return_value=False),
            mock.patch.object(db, "load_eventide_state", return_value=STATE),
            mock.patch.object(eventide_bridge, "get_body_overview", return_value=OVERVIEW),
        ):
            resp = self.client.get("/admin/api/eventide/body", headers=self._auth())
        self.assertEqual(resp.status_code, 200)
        payload = resp.json()
        self.assertEqual(payload["initialized"], True)
        self.assertEqual(payload["inject_enabled"], False)

        self.assertEqual(payload["cycle"]["label"], "平稳期")
        self.assertIn("预计还剩", payload["cycle"]["remaining_text"])
        self.assertEqual(payload["event"]["label"], "低烧黏连")
        # 已过期的结束时间不倒报负数。
        self.assertIn("已到预计时间", payload["event"]["remaining_text"])
        self.assertIsNone(payload["event"]["description"])

        # 七项数值按 payload 顺序输出，异常条目被丢弃。
        self.assertEqual(len(payload["fields"]), 7)
        self.assertEqual(payload["fields"][0], {
            "key": "heat", "label": "热度", "value": 42,
            "level": "中低", "description": "微热",
        })
        self.assertEqual(payload["updated_at"], TICK_ISO)

    def test_body_defends_against_bad_timestamps(self):
        overview = dict(
            OVERVIEW, cycle_expires_at="garbage", event_expires_at=None, last_tick_at=None
        )
        with (
            mock.patch.object(app_settings, "is_eventide_injection_enabled", return_value=True),
            mock.patch.object(db, "load_eventide_state", return_value=STATE),
            mock.patch.object(eventide_bridge, "get_body_overview", return_value=overview),
        ):
            resp = self.client.get("/admin/api/eventide/body", headers=self._auth())
        payload = resp.json()
        self.assertIsNone(payload["cycle"]["remaining_text"])
        self.assertIsNone(payload["event"]["remaining_text"])
        self.assertIsNone(payload["updated_at"])


if __name__ == "__main__":
    unittest.main()
