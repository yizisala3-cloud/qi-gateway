"""HTTP-level tests for the context admin endpoint (/admin/api/context/settings).

鉴权与 eventide_admin_api.py 同款；PUT 只更新出现的键，三个键各自可
独立校验失败。响应始终返回完整的当前设置，便于前端一次性刷新。
"""

import unittest
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import app_settings, db
from gateway.config import cfg
from gateway.context_admin_api import context_admin_routes

GATEWAY_TOKEN = "gateway-token-test"


class ContextAdminApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(context_admin_routes))

    def setUp(self):
        app_settings.reset_settings_cache()
        self.addCleanup(app_settings.reset_settings_cache)
        self.client = TestClient(self.app)
        patcher = mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _auth(self):
        return {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def _patch_readers(self, recent=True, limit=10, timestamp=True):
        return (
            mock.patch.object(
                app_settings, "is_recent_chat_injection_enabled", return_value=recent
            ),
            mock.patch.object(
                app_settings, "get_recent_chat_injection_limit", return_value=limit
            ),
            mock.patch.object(
                app_settings, "is_timestamp_injection_enabled", return_value=timestamp
            ),
        )

    # ---- 鉴权 ----

    def test_settings_requires_token(self):
        resp = self.client.get("/admin/api/context/settings")
        self.assertEqual(resp.status_code, 401)
        resp = self.client.put(
            "/admin/api/context/settings", json={"recent_chat_enabled": True}
        )
        self.assertEqual(resp.status_code, 401)

    def test_settings_rejects_wrong_token(self):
        resp = self.client.get(
            "/admin/api/context/settings", headers={"Authorization": "Bearer nope"}
        )
        self.assertEqual(resp.status_code, 401)

    # ---- GET ----

    def test_get_returns_full_settings_shape(self):
        patches = self._patch_readers(recent=False, limit=25, timestamp=False)
        with patches[0], patches[1], patches[2]:
            resp = self.client.get("/admin/api/context/settings", headers=self._auth())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {
            "recent_chat_enabled": False,
            "recent_chat_limit": 25,
            "timestamp_enabled": False,
        })

    # ---- PUT 校验 ----

    def test_put_rejects_non_object_and_invalid_json(self):
        for payload in ("a string", 123, [], [True]):
            resp = self.client.put(
                "/admin/api/context/settings", headers=self._auth(), json=payload
            )
            self.assertEqual(resp.status_code, 400, repr(payload))
        resp = self.client.put(
            "/admin/api/context/settings", headers=self._auth(), content=b"{not json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_put_rejects_non_boolean_switches(self):
        for payload in (
            {"recent_chat_enabled": "yes"},
            {"recent_chat_enabled": 1},
            {"recent_chat_enabled": None},
            {"timestamp_enabled": "true"},
            {"timestamp_enabled": 0},
        ):
            resp = self.client.put(
                "/admin/api/context/settings", headers=self._auth(), json=payload
            )
            self.assertEqual(resp.status_code, 400, repr(payload))

    def test_put_rejects_out_of_range_or_non_integer_limit(self):
        for payload in (
            {"recent_chat_limit": 0},
            {"recent_chat_limit": -1},
            {"recent_chat_limit": 101},
            {"recent_chat_limit": "20"},
            {"recent_chat_limit": 12.5},
            {"recent_chat_limit": 20.0},
            {"recent_chat_limit": True},
            {"recent_chat_limit": None},
        ):
            resp = self.client.put(
                "/admin/api/context/settings", headers=self._auth(), json=payload
            )
            self.assertEqual(resp.status_code, 400, repr(payload))

    def test_put_validation_failure_never_touches_db_or_cache(self):
        with (
            mock.patch.object(db, "save_app_setting") as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
        ):
            resp = self.client.put(
                "/admin/api/context/settings",
                headers=self._auth(),
                json={"recent_chat_limit": 0},
            )
        self.assertEqual(resp.status_code, 400)
        save.assert_not_called()
        reset.assert_not_called()

    # ---- PUT 成功路径 ----

    def test_put_single_switch_saves_and_resets_cache(self):
        patches = self._patch_readers(recent=False)
        with (
            mock.patch.object(db, "save_app_setting", return_value=True) as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
            patches[0], patches[1], patches[2],
        ):
            resp = self.client.put(
                "/admin/api/context/settings",
                headers=self._auth(),
                json={"recent_chat_enabled": False},
            )

        self.assertEqual(resp.status_code, 200)
        save.assert_called_once_with(app_settings.RECENT_CHAT_INJECT_KEY, False)
        reset.assert_called_once()
        # 响应始终是完整的当前设置，前端一次拿到三件套。
        self.assertEqual(resp.json(), {
            "recent_chat_enabled": False,
            "recent_chat_limit": 10,
            "timestamp_enabled": True,
        })

    def test_put_all_keys_updates_each(self):
        with (
            mock.patch.object(db, "save_app_setting", return_value=True) as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
        ):
            resp = self.client.put(
                "/admin/api/context/settings",
                headers=self._auth(),
                json={
                    "recent_chat_enabled": False,
                    "recent_chat_limit": 30,
                    "timestamp_enabled": False,
                },
            )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            sorted(call.args for call in save.call_args_list),
            sorted([
                (app_settings.RECENT_CHAT_INJECT_KEY, False),
                (app_settings.RECENT_CHAT_LIMIT_KEY, 30),
                (app_settings.TIMESTAMP_INJECT_KEY, False),
            ]),
        )
        reset.assert_called_once()

    def test_put_accepts_limit_boundaries(self):
        with mock.patch.object(db, "save_app_setting", return_value=True) as save:
            for limit in (1, 100):
                resp = self.client.put(
                    "/admin/api/context/settings",
                    headers=self._auth(),
                    json={"recent_chat_limit": limit},
                )
                self.assertEqual(resp.status_code, 200, repr(limit))

        # 边界值原样落库（响应回显的是设置层读到的当前值，不必等于刚存的值）。
        self.assertEqual(
            [call.args[1] for call in save.call_args_list], [1, 100]
        )

    def test_put_empty_object_updates_nothing(self):
        reader_patches = self._patch_readers()
        with (
            mock.patch.object(db, "save_app_setting") as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
            reader_patches[0],
            reader_patches[1],
            reader_patches[2],
        ):
            resp = self.client.put(
                "/admin/api/context/settings", headers=self._auth(), json={}
            )

        self.assertEqual(resp.status_code, 200)
        save.assert_not_called()
        reset.assert_not_called()

    def test_put_save_failure_returns_500(self):
        with mock.patch.object(db, "save_app_setting", return_value=False):
            resp = self.client.put(
                "/admin/api/context/settings",
                headers=self._auth(),
                json={"recent_chat_limit": 30},
            )
        self.assertEqual(resp.status_code, 500)

    def test_put_partial_success_resets_cache_before_500(self):
        # 双键逐键落库、第二键失败：第一个键已写入旧缓存窗口，
        # 返回 500 前也必须失效缓存，否则已落库的键滞留最长 60 秒。
        with (
            mock.patch.object(db, "save_app_setting", side_effect=[True, False]) as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
        ):
            resp = self.client.put(
                "/admin/api/context/settings",
                headers=self._auth(),
                json={"recent_chat_enabled": True, "timestamp_enabled": False},
            )
        self.assertEqual(resp.status_code, 500)
        self.assertEqual(save.call_count, 2)
        reset.assert_called_once()

    def test_put_single_key_failure_does_not_reset_cache(self):
        # 一个键都没落库时保持现状：不清缓存（缓存里还是合法旧值）。
        with (
            mock.patch.object(db, "save_app_setting", return_value=False) as save,
            mock.patch.object(app_settings, "reset_settings_cache") as reset,
        ):
            resp = self.client.put(
                "/admin/api/context/settings",
                headers=self._auth(),
                json={"recent_chat_enabled": True},
            )
        self.assertEqual(resp.status_code, 500)
        save.assert_called_once()
        reset.assert_not_called()

    def test_routes_are_registered_on_gateway_main(self):
        from gateway import main as gateway_main

        paths = {route.path for route in gateway_main._routes if hasattr(route, "path")}
        self.assertIn("/admin/api/context/settings", paths)


if __name__ == "__main__":
    unittest.main()
