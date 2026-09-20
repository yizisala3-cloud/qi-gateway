"""Context-level tests for the streaming-context and timestamp injection.

覆盖 build_context 的提交门控（开关关闭不提交任务）、条数配置在调用
线程读取一次并透传、时间戳块格式与开关、以及五段注入的固定拼装顺序。
"""
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from gateway import app_settings, context

CST = timezone(timedelta(hours=8))


def reset_cache():
    app_settings.reset_settings_cache()


class TimestampContextTests(unittest.TestCase):
    def setUp(self):
        reset_cache()
        self.addCleanup(reset_cache)

    def test_disabled_returns_empty(self):
        with patch.object(
            app_settings, "is_timestamp_injection_enabled", return_value=False
        ):
            self.assertEqual(context.build_timestamp_context(), "")

    def test_format_matches_spec_with_frozen_clock(self):
        # 周一 08:05：验证日期、时间与星期映射逐字符对齐。
        fake_now = datetime(2026, 9, 21, 8, 5, tzinfo=CST)
        with (
            patch.object(
                app_settings, "is_timestamp_injection_enabled", return_value=True
            ),
            patch.object(context, "datetime") as mock_dt,
        ):
            mock_dt.now.return_value = fake_now
            self.assertEqual(
                context.build_timestamp_context(),
                "[当前时间] 2026-09-21 08:05 星期一",
            )

    def test_weekday_mapping_covers_week_boundaries(self):
        cases = {
            datetime(2026, 9, 20, 15, 34, tzinfo=CST): "星期日",
            datetime(2026, 9, 26, 0, 1, tzinfo=CST): "星期六",
        }
        for fake_now, label in cases.items():
            with self.subTest(label=label):
                with (
                    patch.object(
                        app_settings,
                        "is_timestamp_injection_enabled",
                        return_value=True,
                    ),
                    patch.object(context, "datetime") as mock_dt,
                ):
                    mock_dt.now.return_value = fake_now
                    self.assertTrue(
                        context.build_timestamp_context().endswith(label)
                    )

    def test_real_clock_matches_expected_shape(self):
        with patch.object(
            app_settings, "is_timestamp_injection_enabled", return_value=True
        ):
            text = context.build_timestamp_context()
        self.assertRegex(
            text, r"^\[当前时间\] \d{4}-\d{2}-\d{2} \d{2}:\d{2} 星期[一二三四五六日]$"
        )


class RecentChatContextTests(unittest.TestCase):
    def test_query_uses_configured_limit(self):
        calls = []

        class Query:
            def select(self, value):
                calls.append(("select", value))
                return self

            def order(self, column, desc=False):
                calls.append(("order", column, desc))
                return self

            def limit(self, value):
                calls.append(("limit", value))
                return self

            def execute(self):
                calls.append(("execute", None))
                return types.SimpleNamespace(data=[
                    {"role": "assistant", "content": "新回复", "created_at": "2026-09-20T10:01:00+08:00"},
                    {"role": "user", "content": "问题", "created_at": "2026-09-20T10:00:00+08:00"},
                ])

        class Client:
            def table(self, name):
                calls.append(("table", name))
                return Query()

        with patch.object(context.db, "get_client", return_value=Client()):
            text = context.build_recent_chat_context(30)

        self.assertEqual(calls, [
            ("table", "chat_messages"),
            ("select", "role, content, created_at"),
            ("order", "created_at", True),
            ("limit", 30),
            ("execute", None),
        ])
        # 倒序取回后翻成正序拼接，user/assistant 各算一行。
        self.assertEqual(text, "[最近对话]\n叶子: 问题\n栖: 新回复")

    def test_missing_client_returns_empty(self):
        with patch.object(context.db, "get_client", return_value=None):
            self.assertEqual(context.build_recent_chat_context(10), "")


class BuildContextInjectionTests(unittest.TestCase):
    def setUp(self):
        reset_cache()
        self.addCleanup(reset_cache)

    def _patch_all_sources(self, **overrides):
        """固定全部注入源输出与开关；overrides 覆盖个别 patch 目标。"""
        patches = {
            "is_timestamp_injection_enabled": patch.object(
                app_settings, "is_timestamp_injection_enabled", return_value=False
            ),
            "is_recent_chat_injection_enabled": patch.object(
                app_settings, "is_recent_chat_injection_enabled", return_value=True
            ),
            "get_recent_chat_injection_limit": patch.object(
                app_settings, "get_recent_chat_injection_limit", return_value=30
            ),
            "is_eventide_injection_enabled": patch.object(
                app_settings, "is_eventide_injection_enabled", return_value=True
            ),
            "build_timestamp_context": patch(
                "gateway.context.build_timestamp_context", return_value=""
            ),
            "build_recent_chat_context": patch(
                "gateway.context.build_recent_chat_context", return_value="RECENT"
            ),
            "build_eventide_context": patch(
                "gateway.context.build_eventide_context", return_value="EVENTIDE"
            ),
            "load_persona": patch(
                "gateway.context.load_persona", return_value="PERSONA"
            ),
            "search_memories": patch(
                "gateway.context.search_memories", new=AsyncMock(return_value=[])
            ),
        }
        patches.update(overrides)
        for p in patches.values():
            p.start()
            self.addCleanup(p.stop)

    def test_disabled_recent_chat_skips_submission(self):
        self._patch_all_sources(
            is_recent_chat_injection_enabled=patch.object(
                app_settings, "is_recent_chat_injection_enabled", return_value=False
            ),
        )
        rendered = context.build_context("hello")

        # 近期对话开关关闭与 eventide 同款语义：连 db 读取都不发生。
        context.build_recent_chat_context.assert_not_called()
        self.assertIn("PERSONA", rendered)
        self.assertIn("EVENTIDE", rendered)
        self.assertNotIn("RECENT", rendered)

    def test_limit_is_read_once_on_caller_thread_and_forwarded(self):
        # 生产里 build_context 跑在 bg_executor 工作线程；真实保证是
        # "在调用 build_context 的同一线程读一次"，不落到内层 executor 任务。
        reader_threads = []
        caller_thread = threading.current_thread()

        def record_thread():
            reader_threads.append(threading.current_thread())
            return 30

        self._patch_all_sources(
            get_recent_chat_injection_limit=patch.object(
                app_settings,
                "get_recent_chat_injection_limit",
                side_effect=record_thread,
            ),
        )
        context.build_context("hello")

        self.assertEqual(len(reader_threads), 1)
        self.assertIs(reader_threads[0], caller_thread)
        context.build_recent_chat_context.assert_called_once_with(30)

    def test_assembly_order_timestamp_persona_eventide_memories_recent(self):
        self._patch_all_sources(
            build_timestamp_context=patch(
                "gateway.context.build_timestamp_context", return_value="TIMESTAMP"
            ),
            search_memories=patch(
                "gateway.context.search_memories", new=AsyncMock(return_value=["m"])
            ),
        )
        with patch(
            "gateway.context.format_memories_for_injection", return_value="MEMORIES"
        ):
            rendered = context.build_context("hello")

        positions = [rendered.index(s) for s in
                     ("TIMESTAMP", "PERSONA", "EVENTIDE", "MEMORIES", "RECENT")]
        self.assertEqual(positions, sorted(positions))

    def test_empty_blocks_are_dropped(self):
        self._patch_all_sources(
            build_timestamp_context=patch(
                "gateway.context.build_timestamp_context", return_value=""
            ),
            build_eventide_context=patch(
                "gateway.context.build_eventide_context", return_value=""
            ),
            is_recent_chat_injection_enabled=patch.object(
                app_settings, "is_recent_chat_injection_enabled", return_value=False
            ),
        )
        rendered = context.build_context("")

        self.assertEqual(rendered, "PERSONA")


if __name__ == "__main__":
    unittest.main()
