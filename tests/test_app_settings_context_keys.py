"""Unit tests for the context-injection settings in gateway.app_settings.

近期对话注入与时间戳注入共用 Eventide 开关的 fail-open 语义（查询失败、
行缺失、值非法一律按开启/默认值处理），这里把三个新读取函数的契约固化。
"""
import time
import unittest
from unittest import mock

from gateway import app_settings, db


def reset_cache():
    app_settings.reset_settings_cache()


class EnabledFlagContractTests(unittest.TestCase):
    """开关类设置共享的 fail-open 语义：三个 reader 逐一断言。"""

    READERS = (
        app_settings.is_eventide_injection_enabled,
        app_settings.is_recent_chat_injection_enabled,
        app_settings.is_timestamp_injection_enabled,
    )

    def setUp(self):
        reset_cache()
        self.addCleanup(reset_cache)

    def _read_with(self, reader, raw):
        reset_cache()
        with mock.patch.object(app_settings.db, "load_app_setting", return_value=raw):
            return reader()

    def test_fail_open_on_query_failure(self):
        for reader in self.READERS:
            with self.subTest(reader=reader.__name__):
                self.assertTrue(
                    self._read_with(reader, db.APP_SETTING_QUERY_FAILED)
                )

    def test_fail_open_on_missing_row(self):
        for reader in self.READERS:
            with self.subTest(reader=reader.__name__):
                self.assertTrue(self._read_with(reader, None))

    def test_fail_open_on_invalid_value(self):
        invalid = ("garbage", 123, {"inject_enabled": "not-bool"}, [])
        for reader in self.READERS:
            for raw in invalid:
                with self.subTest(reader=reader.__name__, raw=raw):
                    self.assertTrue(self._read_with(reader, raw))

    def test_honors_false_in_bool_and_defensive_string_forms(self):
        for reader in self.READERS:
            for raw in (False, "false", "0"):
                with self.subTest(reader=reader.__name__, raw=raw):
                    self.assertFalse(self._read_with(reader, raw))

    def test_honors_true_in_bool_and_defensive_string_forms(self):
        for reader in self.READERS:
            for raw in (True, "true", "1"):
                with self.subTest(reader=reader.__name__, raw=raw):
                    self.assertTrue(self._read_with(reader, raw))


class RecentChatLimitTests(unittest.TestCase):
    """注入条数：解析防御 + 1–100 夹取 + 默认回退 10。"""

    def setUp(self):
        reset_cache()
        self.addCleanup(reset_cache)

    def _read_with(self, raw):
        reset_cache()
        with mock.patch.object(app_settings.db, "load_app_setting", return_value=raw):
            return app_settings.get_recent_chat_injection_limit()

    def test_query_failure_falls_back_to_default(self):
        self.assertEqual(self._read_with(db.APP_SETTING_QUERY_FAILED), 10)

    def test_missing_or_invalid_falls_back_to_default(self):
        for raw in (None, "garbage", "12.5", "1e2", True, {"limit": 30}, [], 12.5):
            with self.subTest(raw=raw):
                self.assertEqual(self._read_with(raw), 10)

    def test_accepts_int_integral_float_and_numeric_string(self):
        for raw, expected in ((30, 30), (30.0, 30), ("25", 25), (" 25 ", 25)):
            with self.subTest(raw=raw):
                self.assertEqual(self._read_with(raw), expected)

    def test_clamps_out_of_range_values(self):
        for raw, expected in ((0, 1), (-5, 1), (1, 1), (100, 100), (101, 100), (1000, 100)):
            with self.subTest(raw=raw):
                self.assertEqual(self._read_with(raw), expected)


class SettingsCacheTests(unittest.TestCase):
    """按 key 的 TTL 缓存：互不串味，reset 一次清全部。"""

    def setUp(self):
        reset_cache()
        self.addCleanup(reset_cache)

    def test_each_key_hits_db_once_per_ttl_window(self):
        with mock.patch.object(
            app_settings.db, "load_app_setting", return_value=False
        ) as load:
            self.assertFalse(app_settings.is_recent_chat_injection_enabled())
            self.assertFalse(app_settings.is_recent_chat_injection_enabled())
            self.assertEqual(app_settings.get_recent_chat_injection_limit(), 10)

        keys = [call.args[0] for call in load.call_args_list]
        self.assertEqual(keys.count(app_settings.RECENT_CHAT_INJECT_KEY), 1)
        self.assertIn(app_settings.RECENT_CHAT_LIMIT_KEY, keys)

    def test_expired_ttl_rereads_db(self):
        with mock.patch.object(
            app_settings.db, "load_app_setting", return_value="false"
        ) as load:
            self.assertFalse(app_settings.is_timestamp_injection_enabled())
            self.assertFalse(app_settings.is_timestamp_injection_enabled())
            load.assert_called_once()
            with mock.patch.object(
                app_settings.time, "monotonic", return_value=time.monotonic() + 61
            ):
                self.assertFalse(app_settings.is_timestamp_injection_enabled())

        self.assertEqual(load.call_count, 2)

    def test_reset_clears_all_keys_at_once(self):
        values = {
            app_settings.RECENT_CHAT_LIMIT_KEY: "40",
            app_settings.TIMESTAMP_INJECT_KEY: "false",
        }
        with mock.patch.object(
            app_settings.db, "load_app_setting", side_effect=lambda key: values[key]
        ):
            self.assertEqual(app_settings.get_recent_chat_injection_limit(), 40)
            self.assertFalse(app_settings.is_timestamp_injection_enabled())

            # 未 reset 时改库不生效（缓存窗口内）。
            values[app_settings.TIMESTAMP_INJECT_KEY] = "true"
            self.assertFalse(app_settings.is_timestamp_injection_enabled())

            reset_cache()
            self.assertTrue(app_settings.is_timestamp_injection_enabled())
            self.assertEqual(app_settings.get_recent_chat_injection_limit(), 40)


if __name__ == "__main__":
    unittest.main()
