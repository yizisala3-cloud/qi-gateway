"""Persisted opt-in collector: restart recovery, fail-closed, real enable/disable."""
import importlib.util
import logging
import os
import unittest
from pathlib import Path
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import db, log_settings, logging_events
from gateway.config import cfg


class LogSettingsTests(unittest.TestCase):
    def setUp(self):
        self.values = {}
        self.saved = True
        self.enterContext(mock.patch.object(cfg, "GATEWAY_TOKEN", "synthetic-settings-token"))
        self.enterContext(mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", False))
        self.enterContext(mock.patch.object(log_settings, "_environment_default", False))
        self.enterContext(mock.patch.object(log_settings, "_closing", False))
        self.enterContext(mock.patch.object(log_settings, "_writable", False))
        self.enterContext(mock.patch.object(db, "load_app_setting", side_effect=lambda key: self.values.get(key)))
        self.enterContext(mock.patch.object(db, "save_app_setting", side_effect=self._save))
        self.start = self.enterContext(mock.patch.object(logging_events, "startup"))
        self.stop = self.enterContext(mock.patch.object(logging_events, "shutdown"))
        self.enterContext(mock.patch.object(logging_events, "handler_installed", return_value=False))
        self.enterContext(mock.patch.object(logging_events, "get_writer", return_value=None))
        self.client = TestClient(Starlette(routes=log_settings.log_settings_routes))
        self.headers = {"Authorization": "Bearer synthetic-settings-token"}

    def _save(self, key, value):
        if self.saved:
            self.values[key] = value
        return self.saved

    def test_auth_and_body_validation_never_write(self):
        for method in ("get", "put"):
            self.assertEqual(getattr(self.client, method)("/admin/api/logs/settings").status_code, 401)
        for payload in ({}, {"enabled": "false"}, {"enabled": 1}, [], {"enabled": True, "extra": True}):
            self.assertEqual(self.client.put("/admin/api/logs/settings", headers=self.headers, json=payload).status_code, 400)
        self.assertEqual(self.client.put("/admin/api/logs/settings", headers=self.headers, content="{bad").status_code, 400)
        self.assertEqual(self.values, {})
        self.start.assert_not_called()

    def test_default_off_and_saved_selection_restored_after_restart(self):
        response = self.client.get("/admin/api/logs/settings", headers=self.headers)
        self.assertFalse(response.json()["enabled"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.start.assert_not_called()
        self.assertTrue(self.client.put("/admin/api/logs/settings", headers=self.headers, json={"enabled": True}).json()["enabled"])
        self.assertTrue(self.values[log_settings.SETTING_KEY])
        log_settings.shutdown()
        self.assertFalse(cfg.GATEWAY_LOG_ENABLED)
        log_settings.startup()
        self.assertTrue(cfg.GATEWAY_LOG_ENABLED)
        self.client.put("/admin/api/logs/settings", headers=self.headers, json={"enabled": False})
        log_settings.shutdown()
        log_settings.startup()
        self.assertFalse(cfg.GATEWAY_LOG_ENABLED)

    def test_missing_invalid_unavailable_never_enable_without_explicit_choice(self):
        for value in (None, "true", 1, {}, db.APP_SETTING_QUERY_FAILED):
            self.values[log_settings.SETTING_KEY] = value
            cfg.GATEWAY_LOG_ENABLED = True
            result = log_settings.refresh()
            self.assertFalse(result["enabled"])
            self.assertEqual(result["writable"], value is not db.APP_SETTING_QUERY_FAILED)
        self.start.assert_not_called()

    def test_failed_write_preserves_active_state(self):
        self.values[log_settings.SETTING_KEY] = True
        log_settings.refresh()
        self.saved = False
        response = self.client.put("/admin/api/logs/settings", headers=self.headers, json={"enabled": False})
        self.assertEqual(response.status_code, 503)
        self.assertTrue(cfg.GATEWAY_LOG_ENABLED)
        self.assertTrue(self.values[log_settings.SETTING_KEY])

    def test_remote_change_sync_and_shutdown_prevent_late_restart(self):
        self.values[log_settings.SETTING_KEY] = True
        self.assertTrue(log_settings.refresh()["enabled"])
        self.values[log_settings.SETTING_KEY] = False
        self.assertFalse(log_settings.refresh()["enabled"])
        log_settings.shutdown()
        self.start.reset_mock()
        self.values[log_settings.SETTING_KEY] = True
        self.assertFalse(log_settings.refresh()["enabled"])
        self.start.assert_not_called()
        self.assertEqual(self.client.put("/admin/api/logs/settings", headers=self.headers, json={"enabled": True}).status_code, 503)

    def test_environment_default_is_false_without_loading_env_files(self):
        spec = importlib.util.spec_from_file_location("gateway._config_default_probe", Path(__file__).parents[1] / "gateway/config.py")
        module = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("dotenv.load_dotenv"):
            spec.loader.exec_module(module)
        self.assertFalse(module.cfg.GATEWAY_LOG_ENABLED)


class RealCollectorToggleTests(unittest.TestCase):
    def test_switch_controls_real_handler_writer_and_keeps_history(self):
        values, history = {}, []
        class Store:
            def insert_events(self, events):
                history.extend(events)
                return len(events)

        original_startup = logging_events.startup
        def start():
            original_startup(Store())

        def save(key, value):
            values[key] = value
            return True

        logging_events.shutdown()
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", False), \
             mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", ""), \
             mock.patch.object(cfg, "GATEWAY_LOG_FLUSH_TIMEOUT", 0.5), \
             mock.patch.object(log_settings, "_closing", False), \
             mock.patch.object(log_settings, "_environment_default", False), \
             mock.patch.object(db, "load_app_setting", side_effect=lambda key: values.get(key)), \
             mock.patch.object(db, "save_app_setting", side_effect=save), \
             mock.patch.object(logging_events, "startup", side_effect=start):
            try:
                log_settings.startup()
                self.assertIsNone(logging_events.get_writer())
                self.assertFalse(logging_events.handler_installed())
                log_settings.save(True)
                self.assertTrue(logging_events.handler_installed())
                writer = logging_events.get_writer()
                logging.getLogger("gateway.planning").error("synthetic toggle verification")
                log_settings.save(False)
                self.assertFalse(logging_events.handler_installed())
                self.assertIsNone(logging_events.get_writer())
                self.assertFalse(writer._thread.is_alive())
                self.assertEqual(len(history), 1)
                logging.getLogger("gateway.planning").error("synthetic disabled verification")
                self.assertEqual(len(history), 1)
            finally:
                logging_events.shutdown()
