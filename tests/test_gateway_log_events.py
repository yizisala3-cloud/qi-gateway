"""全网关报错日志采集层单元测试（§36 批次）。

覆盖：脱敏（密钥 / 控制字符 / 截断）、logger→模块映射、fingerprint
归一化、有界队列优先保留高等级、handler 幂等安装与排除名单、emit_error
结构化入口、请求上下文计数（防 HTTP 边界双重计数）。
"""

import logging
import unittest
from contextlib import contextmanager
from unittest import mock

from gateway import logging_events
from gateway.config import cfg


class ScrubTextTests(unittest.TestCase):
    def test_bearer_token_is_redacted(self):
        cleaned, _ = logging_events.scrub_text(
            "Authorization: Bearer abc123def456ghi789", 100)
        self.assertNotIn("abc123def456ghi789", cleaned)
        self.assertIn("Bearer ***", cleaned)

    def test_supabase_secret_key_is_redacted(self):
        cleaned, _ = logging_events.scrub_text(
            "key sb_secret_0123456789abcdefGHIJ failed", 100)
        self.assertNotIn("0123456789abcdefGHIJ", cleaned)
        self.assertIn("sb_secret_***", cleaned)

    def test_jwt_is_redacted(self):
        token = (
            "eyJhbGciOiJIUzI1NiIs.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
        )
        cleaned, _ = logging_events.scrub_text(f"token {token} rejected", 300)
        self.assertNotIn(token, cleaned)
        self.assertIn("***JWT***", cleaned)

    def test_api_key_assignment_is_redacted(self):
        cleaned, _ = logging_events.scrub_text("api_key = supersecretvalue123", 100)
        self.assertNotIn("supersecretvalue123", cleaned)

    def test_url_query_token_is_redacted(self):
        cleaned, _ = logging_events.scrub_text(
            "GET https://x.example/v1?token=abcdef123456&other=1", 200)
        self.assertNotIn("abcdef123456", cleaned)

    def test_control_characters_and_ansi_are_stripped(self):
        cleaned, _ = logging_events.scrub_text("bad\x00\x1b[31mtext\x07", 100)
        self.assertEqual(cleaned, "badtext")

    def test_carriage_returns_are_normalized(self):
        cleaned, _ = logging_events.scrub_text("a\r\nb\rc", 100)
        self.assertEqual(cleaned, "a\nb\nc")

    def test_truncation_is_reported(self):
        cleaned, truncated = logging_events.scrub_text("x" * 50, 10)
        self.assertEqual(cleaned, "x" * 10)
        self.assertTrue(truncated)

    def test_none_returns_empty(self):
        self.assertEqual(logging_events.scrub_text(None, 10), ("", False))


class ConfiguredSecretScrubTests(unittest.TestCase):
    """BUG-01：配置密钥按实际值掩码，不依赖格式正则命中。"""

    def test_gateway_token_value_is_masked(self):
        with mock.patch.object(cfg, "GATEWAY_TOKEN", "REVIEW_GATEWAY_SECRET_123456"):
            cleaned, _ = logging_events.scrub_text(
                "rejected credential REVIEW_GATEWAY_SECRET_123456", 4000)
        self.assertNotIn("REVIEW_GATEWAY_SECRET_123456", cleaned)
        self.assertIn("***", cleaned)

    def test_all_configured_secret_values_are_masked(self):
        secrets = {
            "UPSTREAM_API_KEY": "upstream-key-value-987654",
            "SUPABASE_SECRET_KEY": "sb_secret_0123456789abcdefGHIJ",
            "SUPABASE_SERVICE_ROLE_KEY": "service-role-key-abcdef123456",
            "SUPABASE_KEY": "fallback-publishable-key-abcdef",
            "ANALYSIS_API_KEY": "analysis-key-value-1234567890",
            "CONTINUITY_API_KEY": "continuity-key-value-12345678",
            "RUMINATION_API_KEY": "rumination-key-value-123456789",
            "MEMORY_PLUGIN_TOKEN": "memory-plugin-token-12345678",
            "MCP_MEMORY_TOKEN": "mcp-memory-token-1234567890",
            "TODO_PLUGIN_TOKEN": "todo-plugin-token-1234567890",
        }
        patches = [mock.patch.object(cfg, name, value)
                   for name, value in secrets.items()]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        for value in secrets.values():
            cleaned, _ = logging_events.scrub_text(f"error with {value} inside", 4000)
            self.assertNotIn(value, cleaned, f"密钥 {value[:12]}… 未被掩码")

    def test_short_configured_values_are_masked(self):
        # BUG-01：被系统接受的短凭证（1～5 字符）同样掩码——鉴权接受却不
        # 清洗会留下密钥红线缺口；长度豁免已移除。
        for token in ("qT9!x", "abc12", "k", "ab"):
            with self.subTest(token=token):
                with mock.patch.object(cfg, "GATEWAY_TOKEN", token):
                    cleaned, _ = logging_events.scrub_text(
                        f"rejected credential {token} here", 4000)
                self.assertNotIn(token, cleaned)

    def test_url_userinfo_credentials_are_masked(self):
        cleaned, _ = logging_events.scrub_text(
            "postgres://user:passw0rd123@db.example:5432/postgres failed", 200)
        self.assertNotIn("passw0rd123", cleaned)
        self.assertIn("postgres://user:***@db.example", cleaned)

    def test_masking_happens_before_truncation(self):
        # 密钥跨截断边界时也先整体掩码（截断不能拆出密钥残段）
        secret = "S" * 40
        with mock.patch.object(cfg, "GATEWAY_TOKEN", secret):
            cleaned, truncated = logging_events.scrub_text(
                "x" * 30 + secret + "y" * 30, 50)
        self.assertNotIn("S" * 10, cleaned)
        self.assertTrue(truncated)


class DbBodyScrubTests(unittest.TestCase):
    """BUG-02：数据库异常中的行值 / 键值回显不进入任何持久化文本。"""

    def test_failing_row_values_are_removed(self):
        cleaned, _ = logging_events.scrub_text(
            "Error 23502:\nMessage: null value violates not-null constraint\n"
            "Details: Failing row contains (42, REVIEW_PRIVATE_MEMO_BODY_123456)",
            4000)
        self.assertNotIn("REVIEW_PRIVATE_MEMO_BODY_123456", cleaned)
        self.assertIn("failing row contains (***)", cleaned.casefold())
        self.assertIn("null value violates not-null constraint", cleaned)

    def test_unique_violation_key_values_are_removed(self):
        cleaned, _ = logging_events.scrub_text(
            'duplicate key value violates unique constraint "chat_messages_pkey" '
            'Key (content)=(SECRET_CHAT_BODY_987654) already exists.', 4000)
        self.assertNotIn("SECRET_CHAT_BODY_987654", cleaned)
        self.assertIn("Key (content)=(***)", cleaned)

    def test_pg_message_echo_is_removed(self):
        # BUG-02：22P02 等 message 主字段直接回显非法输入（含 APIError
        # message 形态），模板保留、输入值移除。
        marker = "REREVIEW_PRIVATE_CHAT_BODY_123456"
        cleaned, _ = logging_events.scrub_text(
            f'invalid input syntax for type uuid: "{marker}"', 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn('invalid input syntax for type uuid', cleaned)
        self.assertIn('"***"', cleaned)
        # 单引号变体与数组字面量回显同规则
        cleaned2, _ = logging_events.scrub_text(
            f"malformed array literal: '{marker}'", 4000)
        self.assertNotIn(marker, cleaned2)

    def test_multiline_row_echo_is_removed(self):
        # BUG-02：Failing row / Key 值列表可跨行（存储内容含换行时 PG 多行
        # 回显）；跨行文本与单行同规则清洗。
        marker = "REREVIEW_PRIVATE_CHAT_BODY_123456"
        cleaned, _ = logging_events.scrub_text(
            "memo RPC rejected: Failing row contains (42, first line\n"
            f"{marker}\n, more)", 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn("failing row contains (***)", cleaned.casefold())
        cleaned2, _ = logging_events.scrub_text(
            f"Key (content)=(\n'{marker}'\n) already exists.", 4000)
        self.assertNotIn(marker, cleaned2)
        self.assertIn("Key (content)=(***)", cleaned2)

    def test_structured_api_error_exception_is_rebuilt_without_details(self):
        from postgrest.exceptions import APIError

        exc = APIError({
            "code": "23502",
            "message": "null value violates not-null constraint",
            "details": "Failing row contains (42, REVIEW_PRIVATE_MEMO_BODY_123456)",
            "hint": "some hint",
        })
        scrubbed = logging_events._scrubbed_exception(exc)
        text = str(scrubbed)
        self.assertNotIn("REVIEW_PRIVATE_MEMO_BODY_123456", text)
        self.assertNotIn("some hint", text)
        self.assertIn("23502", text)
        # 原异常对象不被修改
        self.assertIn("REVIEW_PRIVATE_MEMO_BODY_123456", str(exc))

    def test_generic_exception_text_is_scrubbed_in_clone(self):
        exc = RuntimeError("failed with Bearer abc123def456ghi789 inside")
        scrubbed = logging_events._scrubbed_exception(exc)
        self.assertNotIn("abc123def456ghi789", str(scrubbed))
        self.assertIn("Bearer ***", str(scrubbed))
        # 无敏感内容时返回原对象（不制造副本）
        plain = RuntimeError("plain failure")
        self.assertIs(logging_events._scrubbed_exception(plain), plain)

    def test_full_pipeline_keeps_body_out_of_events_and_spool(self):
        """真实 APIError + safe_query 形态：事件与 spool 落盘值均无正文。"""
        import tempfile
        from pathlib import Path

        from postgrest.exceptions import APIError

        from gateway import db as gateway_db

        marker = "REVIEW_PRIVATE_MEMO_BODY_123456"
        queue = logging_events._queue
        queue.drain(10000)
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True), \
                mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"):
            logging_events.install_handler()
            self.addCleanup(logging_events.uninstall_handler)
            self.addCleanup(queue.drain, 10000)

            @gateway_db.safe_query
            def failing_query():
                raise APIError({
                    "code": "23502",
                    "message": "null value violates not-null constraint",
                    "details": "Failing row contains (42, " + marker + ")",
                    "hint": None,
                })

            self.assertIsNone(failing_query())
            events = queue.drain(100)
        self.assertEqual(len(events), 2)  # 重试 warning + 最终 error
        for event in events:
            self.assertNotIn(marker, event["message_raw"])
            self.assertNotIn(marker, event["stack_trace"])
            self.assertEqual(event["module"], "database")
            self.assertEqual(event["outcome"], "degraded")
        # 最终失败事件带真实异常类型与堆栈（BUG-18）
        final = events[-1]
        self.assertEqual(final["exception_type"], "APIError")
        self.assertIn("failing_query", final["stack_trace"])
        with tempfile.TemporaryDirectory(prefix="qigate-scrub-spool-") as directory:
            spool = logging_events._SpoolStore(directory, "inst-scrub", 10, 1_000_000)
            spool.write(events)
            persisted = "".join(
                Path(directory, name).read_text(encoding="utf-8")
                for name in spool._files()
            )
        self.assertNotIn(marker, persisted)


class PlatformScrubFilterTests(unittest.TestCase):
    """BUG-01：平台输出与持久化使用同一安全格式。"""

    def _platform_output(self, record_factory) -> str:
        import io

        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        root = logging.getLogger()
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True), \
                mock.patch.object(cfg, "GATEWAY_TOKEN", "REVIEW_GATEWAY_SECRET_123456"):
            logging_events.install_handler()
            self.addCleanup(logging_events.uninstall_handler)
            record_factory()
        return stream.getvalue()

    def test_platform_output_masks_configured_secret(self):
        def emit():
            logging.getLogger("gateway.planning").error(
                "auth failed with REVIEW_GATEWAY_SECRET_123456")

        output = self._platform_output(emit)
        self.assertNotIn("REVIEW_GATEWAY_SECRET_123456", output)
        self.assertIn("***", output)

    def test_platform_output_masks_exception_body(self):
        def emit():
            try:
                raise RuntimeError("boom REVIEW_GATEWAY_SECRET_123456")
            except RuntimeError:
                logging.getLogger("gateway.planning").exception("failed")

        output = self._platform_output(emit)
        self.assertNotIn("REVIEW_GATEWAY_SECRET_123456", output)

    def test_filter_is_removed_on_uninstall(self):
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True):
            logging_events.install_handler()
            scrub_filter = logging_events._scrub_filter
            self.assertIsNotNone(scrub_filter)
            root = logging.getLogger()
            platform_handlers = [
                h for h in root.handlers
                if not isinstance(h, logging_events.GatewayLogHandler)
            ]
            self.assertTrue(platform_handlers)
            for handler in platform_handlers:
                self.assertIn(scrub_filter, handler.filters)
            logging_events.uninstall_handler()
            for handler in platform_handlers:
                self.assertNotIn(scrub_filter, handler.filters)


class ExceptionIdentityAndChainTests(unittest.TestCase):
    """BUG-20/21：平台脱敏不破坏异常身份；异常链在副本上保留。"""

    @contextmanager
    def _capture_with_platform_handler(self, secret: str):
        """平台 handler 在采集 handler 之前执行（顺序与生产一致）。"""
        import io

        queue = logging_events._queue
        queue.drain(10000)
        self.addCleanup(queue.drain, 10000)
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        stream = io.StringIO()
        root.handlers = [logging.StreamHandler(stream)]
        root.setLevel(logging.INFO)
        try:
            with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True), \
                    mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"), \
                    mock.patch.object(cfg, "GATEWAY_TOKEN", secret):
                logging_events.install_handler()
                try:
                    yield stream
                finally:
                    logging_events.uninstall_handler()
        finally:
            root.handlers = handlers
            root.setLevel(level)

    def test_reraise_after_scrubbed_log_counts_once(self):
        # BUG-20：异常文本含已配置密钥时，平台 filter 不得替换共享 record
        # 的异常身份——端点 log.exception 后 re-raise 的同一异常只计一次。
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Route
        from starlette.testclient import TestClient

        secret = "REREVIEW_SECRET_123456"
        captured = {}
        queue = logging_events._queue

        async def endpoint(request):
            try:
                raise RuntimeError("synthetic dependency failed " + secret)
            except RuntimeError as exc:
                captured["original_id"] = id(exc)
                logging.getLogger("gateway.planning").exception("operation failed")
                raise

        app = Starlette(
            routes=[Route("/probe", endpoint)],
            middleware=[Middleware(logging_events.GatewayLogMiddleware)],
        )
        with self._capture_with_platform_handler(secret):
            client = TestClient(app, raise_server_exceptions=False)
            response = client.get("/probe")
        self.assertEqual(response.status_code, 500)
        events = queue.drain(100)
        self.assertEqual(len(events), 1, "同一异常只计一次（BUG-20）")
        # 唯一事件是端点自己的 log.exception（unlogged_error）；中间件没有
        # 因身份被平台 filter 替换而再补一条 unhandled_exception。
        self.assertEqual(events[0]["error_code"], "unlogged_error")
        # 平台输出与事件都不泄露密钥
        self.assertNotIn(secret, events[0]["message_raw"])
        self.assertNotIn(secret, events[0]["stack_trace"])

    def test_reraise_plain_control_counts_once(self):
        # 对照：无敏感内容的 log.exception → re-raise 同样只计一次。
        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Route
        from starlette.testclient import TestClient

        queue = logging_events._queue

        async def endpoint(request):
            try:
                raise RuntimeError("plain failure")
            except RuntimeError:
                logging.getLogger("gateway.planning").exception("operation failed")
                raise

        app = Starlette(
            routes=[Route("/probe", endpoint)],
            middleware=[Middleware(logging_events.GatewayLogMiddleware)],
        )
        with self._capture_with_platform_handler("unrelated-secret-value"):
            client = TestClient(app, raise_server_exceptions=False)
            client.get("/probe")
        self.assertEqual(len(queue.drain(100)), 1)

    def test_scrubbed_clone_preserves_exception_chain(self):
        # BUG-21：脱敏副本保留 __cause__ / __context__ / __suppress_context__。
        # 深层链上的敏感文本（Bearer 形态）逐层递归清洗，中间层（文本
        # 干净但带链）同样克隆，避免原对象深层链漏出。
        from postgrest.exceptions import APIError

        secret = "Bearer abc123def456ghi789"
        try:
            try:
                raise ValueError("synthetic original cause " + secret)
            except ValueError as cause:
                try:
                    raise RuntimeError("intermediate context") from cause
                except RuntimeError:
                    raise APIError({
                        "code": "23502", "message": "constraint failure",
                        "details": "row metadata", "hint": None,
                    })
        except APIError as exc:
            original_cause_text = str(exc.__context__.__cause__)
            scrubbed = logging_events._scrubbed_exception(exc)
            formatted = "".join(logging_events.traceback.format_exception(
                type(scrubbed), scrubbed, scrubbed.__traceback__))
        # 链上的底层根因与帧保留（类型可见），各层敏感文本移除
        self.assertIn("ValueError", formatted)
        self.assertIn("RuntimeError", formatted)
        self.assertIn("APIError", formatted)
        self.assertNotIn("abc123def456ghi789", formatted)
        # 原异常对象不被修改：链与文本保持原样
        self.assertIn(secret, original_cause_text)
        # from None：抑制上下文语义保留
        try:
            try:
                raise ValueError("context-a")
            except ValueError:
                raise RuntimeError("from-none-target") from None
        except RuntimeError as exc2:
            clone = logging_events._scrubbed_exception(exc2)
            self.assertTrue(clone.__suppress_context__)
            self.assertIsNone(clone.__cause__)

    def test_chain_cycle_is_bounded(self):
        # 循环链按深度上限截断，不无限递归。
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__context__ = second
        second.__context__ = first
        scrubbed = logging_events._scrubbed_exception(first)
        self.assertIsInstance(scrubbed, RuntimeError)
        self.assertIn("first", str(scrubbed))

    def test_apierror_message_echo_full_pipeline(self):
        # BUG-02 全链路：真实 APIError 的 message 回显输入值，事件堆栈 /
        # 平台输出 / spool 落盘值均不含正文，错误码与帧保留。
        import tempfile
        from pathlib import Path

        from postgrest.exceptions import APIError

        marker = "REREVIEW_PRIVATE_CHAT_BODY_123456"
        queue = logging_events._queue
        queue.drain(10000)
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        import io

        stream = io.StringIO()
        root.handlers = [logging.StreamHandler(stream)]
        root.setLevel(logging.INFO)
        try:
            with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True), \
                    mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"):
                logging_events.install_handler()
                try:
                    try:
                        raise APIError({
                            "code": "22P02",
                            "message": 'invalid input syntax for type uuid: "'
                                       + marker + '"',
                            "details": None, "hint": None,
                        })
                    except APIError:
                        logging.getLogger("gateway.memo").exception(
                            "memo RPC infrastructure failure")
                finally:
                    logging_events.uninstall_handler()
        finally:
            root.handlers = handlers
            root.setLevel(level)
        events = queue.drain(100)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["exception_type"], "APIError")
        self.assertIn("22P02", event["stack_trace"])
        self.assertIn("test_gateway_log_events", event["stack_trace"])  # 调用帧保留
        self.assertNotIn(marker, event["stack_trace"])
        self.assertNotIn(marker, event["message_raw"])
        self.assertNotIn(marker, stream.getvalue())
        with tempfile.TemporaryDirectory(prefix="qigate-msg-spool-") as directory:
            spool = logging_events._SpoolStore(directory, "inst-msg", 10, 1_000_000)
            spool.write(events)
            persisted = "".join(
                Path(directory, name).read_text(encoding="utf-8")
                for name in spool._files()
            )
        self.assertNotIn(marker, persisted)


class SafeContextTests(unittest.TestCase):
    def test_whitelisted_scalars_are_kept(self):
        result = logging_events._sanitize_safe_context(
            {"attempts": 2, "stage": "write", "exists": True, "duration_ms": 12.5}
        )
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(result["stage"], "write")
        self.assertTrue(result["exists"])
        self.assertEqual(result["duration_ms"], 12.5)

    def test_unknown_keys_are_rejected(self):
        result = logging_events._sanitize_safe_context(
            {"attempts": 1, "user_content": "正文不该进来", "nested": {"a": 1}}
        )
        self.assertEqual(result, {"attempts": 1})

    def test_string_values_are_scrubbed(self):
        result = logging_events._sanitize_safe_context(
            {"stage": "Bearer abc123def456ghi789"})
        self.assertEqual(result["stage"], "Bearer ***")


class ModuleMappingTests(unittest.TestCase):
    def test_known_loggers_map_to_modules(self):
        cases = {
            "gateway.planning_api": "planning",
            "gateway.planning": "planning",
            "gateway.memo_api": "memo",
            "gateway.memory_heat": "memory",
            "gateway.admin_memory_api": "memory",
            "gateway.context": "context",
            "gateway.chat": "chat",
            "gateway.db": "database",
            "gateway.admin_api": "database",
            "gateway.todo_api": "todo",
            "gateway.eventide_bridge": "eventide",
            "gateway.persona": "persona",
            "gateway.app_settings": "system",
            "gateway": "system",
        }
        for name, module in cases.items():
            with self.subTest(logger=name):
                self.assertEqual(logging_events._module_for_logger(name), module)

    def test_unmapped_loggers_show_other(self):
        self.assertEqual(logging_events._module_for_logger("some.third.party"), "other")
        self.assertEqual(logging_events._module_for_logger("supabase.client"), "database")

    def test_excluded_loggers(self):
        for name in (
            "gateway.log_store", "gateway.log_internal", "uvicorn.error",
            "asyncio", "uvicorn",
        ):
            with self.subTest(logger=name):
                self.assertTrue(logging_events._is_excluded_logger(name))
        self.assertFalse(logging_events._is_excluded_logger("gateway.planning"))

    def test_route_module_rules(self):
        cases = {
            "/admin/api/planning/tasks": "planning",
            "/admin/api/memo/entries": "memo",
            "/admin/api/data/memories": "database",
            "/v1/chat/completions": "chat",
            "/admin/api/unknown/thing": "system",
        }
        for route, module in cases.items():
            with self.subTest(route=route):
                self.assertEqual(logging_events._module_for_route(route), module)


class FingerprintTests(unittest.TestCase):
    def test_line_numbers_do_not_fragment_groups(self):
        first = logging_events._fingerprint(
            "planning", "create", "internal_error", "APIError",
            "gateway.planning_api", "a.py:fn:12")
        second = logging_events._fingerprint(
            "planning", "create", "internal_error", "APIError",
            "gateway.planning_api", "a.py:fn:99")
        self.assertEqual(first, second)

    def test_different_modules_do_not_merge(self):
        first = logging_events._fingerprint(
            "planning", "create", "internal_error", "", "l", "s")
        second = logging_events._fingerprint(
            "memo", "create", "internal_error", "", "l", "s")
        self.assertNotEqual(first, second)

    def test_different_error_codes_do_not_merge(self):
        first = logging_events._fingerprint(
            "chat", "op", "upstream_timeout", "", "l", "s")
        second = logging_events._fingerprint(
            "chat", "op", "upstream_connect_error", "", "l", "s")
        self.assertNotEqual(first, second)


class FallbackMessageTests(unittest.TestCase):
    def test_known_module_gets_chinese_summary(self):
        text = logging_events._fallback_message_zh("planning", "ERROR", "APIError", "gateway.planning_api")
        self.assertIn("规划管理", text)
        self.assertIn("错误", text)
        self.assertIn("APIError", text)

    def test_other_module_keeps_logger_name(self):
        text = logging_events._fallback_message_zh("other", "WARNING", "", "some.lib")
        self.assertIn("其他组件", text)
        self.assertIn("some.lib", text)


class EventQueueTests(unittest.TestCase):
    def _event(self, level="WARNING"):
        return {"level": level, "event_id": level}

    def test_put_and_drain(self):
        queue = logging_events._EventQueue(10)
        queue.put(self._event("WARNING"))
        queue.put(self._event("ERROR"))
        batch = queue.drain(10)
        self.assertEqual([e["level"] for e in batch], ["WARNING", "ERROR"])
        self.assertEqual(queue.depth(), 0)

    def test_full_queue_drops_low_level(self):
        queue = logging_events._EventQueue(2)
        queue.put(self._event("WARNING"))
        queue.put(self._event("WARNING"))
        # 队列满：新 ERROR 事件替换最旧的 WARNING，而不是被丢掉
        self.assertTrue(queue.put(self._event("ERROR")))
        self.assertEqual(queue.depth(), 2)
        self.assertEqual(queue.dropped_total, 1)
        self.assertEqual(queue.dropped_by_level["WARNING"], 1)
        self.assertEqual(queue.last_drop_reason, "queue_full_replaced")

    def test_full_queue_drops_incoming_when_not_higher(self):
        queue = logging_events._EventQueue(1)
        queue.put(self._event("ERROR"))
        self.assertFalse(queue.put(self._event("WARNING")))
        self.assertEqual(queue.dropped_total, 1)
        self.assertEqual(queue.last_drop_reason, "queue_full")
        batch = queue.drain(1)
        self.assertEqual(batch[0]["level"], "ERROR")

    def test_snapshot_reports_state(self):
        queue = logging_events._EventQueue(5)
        queue.put(self._event())
        snap = queue.snapshot()
        self.assertEqual(snap["queue_depth"], 1)
        self.assertEqual(snap["queue_max"], 5)
        self.assertEqual(snap["dropped_total"], 0)


class HandlerCaptureTests(unittest.TestCase):
    def setUp(self):
        self.queue = logging_events._queue
        self.queue.drain(10000)
        patches = [
            mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True),
            mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(self.queue.drain, 10000)

    def _events(self):
        return self.queue.drain(10000)

    def test_planning_exception_is_captured_with_stack(self):
        try:
            raise ValueError("boom-detail")
        except ValueError:
            logging.getLogger("gateway.planning_api").exception("planning 失败: %s", "ctx")
        events = self._events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "planning")
        self.assertEqual(event["level"], "ERROR")
        self.assertEqual(event["exception_type"], "ValueError")
        self.assertIn("boom-detail", event["stack_trace"])
        self.assertIn("ValueError", event["stack_trace"])
        self.assertTrue(event["error_code"])
        self.assertTrue(event["message_zh"])
        self.assertEqual(len(event["fingerprint"]), 16)

    def test_info_records_are_not_captured(self):
        logging.getLogger("gateway.planning").info("正常信息不采集")
        self.assertEqual(self._events(), [])

    def test_excluded_loggers_are_not_captured(self):
        logging.getLogger("gateway.log_store").error("writer 自身诊断不进采集链")
        logging.getLogger("uvicorn.error").error("ASGI 异常由中间件记录，防双计")
        self.assertEqual(self._events(), [])

    def test_third_party_warning_maps_to_other(self):
        logging.getLogger("some.third.party").warning("component degraded")
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["module"], "other")
        self.assertEqual(events[0]["level"], "WARNING")
        self.assertIn("some.third.party", events[0]["message_zh"])

    def test_install_is_idempotent(self):
        # setUp 已安装一次；再次安装不产生第二个 handler
        first = logging_events._handler
        self.assertFalse(logging_events.install_handler())
        self.assertIs(logging_events._handler, first)
        root_handlers = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging_events.GatewayLogHandler)
        ]
        self.assertEqual(len(root_handlers), 1)

    def test_uninstall_stops_intake(self):
        logging_events.uninstall_handler()
        logging.getLogger("gateway.memo").error("卸载后不采集")
        self.assertEqual(self._events(), [])
        # 重新装回供后续用例使用
        logging_events.install_handler()

    def test_evt_extra_enriches_event(self):
        logging.getLogger("gateway.planning").warning(
            "planning 同步生成失败（等待后台循环重试）: error=%s", "X",
            extra={"evt": {
                "module": "planning", "operation": "generate_due",
                "error_code": "quiet_generate_failed",
                "message_zh": "待办已保存，但同步补生成失败，将由维护流程重试",
                "outcome": "committed_side_effect_failed",
                "safe_context": {"task_id": 7, "user_content": "不该存"},
            }},
        )
        events = self._events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["operation"], "generate_due")
        self.assertEqual(event["error_code"], "quiet_generate_failed")
        self.assertEqual(event["outcome"], "committed_side_effect_failed")
        self.assertIn("待办已保存", event["message_zh"])
        self.assertEqual(event["safe_context"], {"task_id": 7})


class EmitErrorTests(unittest.TestCase):
    def setUp(self):
        self.queue = logging_events._queue
        self.queue.drain(10000)
        patches = [
            mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True),
            mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(self.queue.drain, 10000)

    def test_emit_error_produces_structured_event(self):
        logging_events.emit_error(
            "chat", "chat_completions", "upstream_timeout", "聊天转发请求超时",
            outcome="failed", upstream_status=None,
            safe_context={"timeout_seconds": 180},
        )
        events = self.queue.drain(100)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "chat")
        self.assertEqual(event["error_code"], "upstream_timeout")
        self.assertEqual(event["message_zh"], "聊天转发请求超时")
        self.assertEqual(event["message_raw"], "聊天转发请求超时")
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["safe_context"], {"timeout_seconds": 180})

    def test_emit_error_with_exception_captures_stack(self):
        try:
            raise RuntimeError("connect refused")
        except RuntimeError as exc:
            logging_events.emit_error(
                "chat", "chat_completions", "upstream_connect_error",
                "聊天上游连接失败", exc=exc,
            )
        events = self.queue.drain(100)
        self.assertEqual(events[0]["exception_type"], "RuntimeError")
        self.assertIn("connect refused", events[0]["stack_trace"])

    def test_emit_error_level_warning(self):
        logging_events.emit_error(
            "chat", "chat_stream", "upstream_status_error",
            "聊天上游返回错误状态", level="WARNING", outcome="degraded",
            upstream_status=429,
        )
        events = self.queue.drain(100)
        self.assertEqual(events[0]["level"], "WARNING")
        self.assertEqual(events[0]["upstream_status"], 429)
        self.assertEqual(events[0]["outcome"], "degraded")


class RequestContextTests(unittest.TestCase):
    def test_request_context_counter_is_shared_mutable(self):
        ctx = logging_events.RequestLogContext("req-abc", "POST", "/v1/chat/completions")
        token = logging_events._request_ctx.set(ctx)
        try:
            self.assertEqual(logging_events.current_request_id(), "req-abc")
            ctx.events_recorded += 1
            self.assertEqual(
                logging_events.current_request_context().events_recorded, 1)
        finally:
            logging_events._request_ctx.reset(token)
        self.assertIsNone(logging_events.current_request_context())


class CollectionStatusTests(unittest.TestCase):
    def test_status_reports_disabled_state_without_writer(self):
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", False):
            status = logging_events.collection_status()
        self.assertFalse(status["enabled"])
        self.assertIn("queue_depth", status)
        self.assertIn("spool", status)


if __name__ == "__main__":
    unittest.main()
