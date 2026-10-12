"""网关日志批次残留缺陷与新增需求的采集层测试（2026-10-11 批次）。

覆盖：
* BUG-02 残留：正文内嵌套括号 / 引号不再截断脱敏范围（真实 PG 回显形态）；
* BUG-21 残留：深度边界与 ExceptionGroup 不返回未脱敏对象（fail-closed）；
* BUG-25：默认 Uvicorn 命名 logger 的独立 handler 挂平台脱敏过滤器；
* TASK-02：稳定密钥标识（key_ref）、调用边界归属、按密钥分组与兼容口径。
"""

import logging
import traceback
import unittest
from unittest import mock

from gateway import logging_events


class NestedDelimiterScrubTests(unittest.TestCase):
    """BUG-02 残留：值内嵌套括号 / 引号不再截断脱敏范围（L17）。"""

    def test_nested_parenthesis_row_value_fully_removed(self):
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        cleaned, _ = logging_events.scrub_text(
            f"Failing row contains (null, ordinary (aside) {marker}).", 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn("failing row contains (***)", cleaned.casefold())

    def test_embedded_quote_uuid_echo_fully_removed(self):
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        cleaned, _ = logging_events.scrub_text(
            f'invalid input syntax for type uuid: "prefix" {marker}"', 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn('"***"', cleaned)

    def test_single_quote_embedded_echo_fully_removed(self):
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        cleaned, _ = logging_events.scrub_text(
            f"invalid input syntax for type uuid: 'prefix' {marker}'", 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn("'***'", cleaned)

    def test_stray_close_paren_value_fully_removed(self):
        # 值内多余右括号（深度提前归零）：继续向后配对，正文不保留
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        cleaned, _ = logging_events.scrub_text(
            f"Failing row contains (null, a)b {marker}).", 4000)
        self.assertNotIn(marker, cleaned)

    def test_unclosed_row_echo_consumes_to_frame_boundary(self):
        # 未闭合：fail-closed 取到帧边界，正文不保留、调用帧保留
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        text = (
            f"DETAIL: Failing row contains (null, {marker}\n"
            "Traceback (most recent call last):\n"
            '  File "a.py", line 1, in f\n'
            "    x = 1\n"
            "ValueError: boom")
        cleaned, _ = logging_events.scrub_text(text, 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn('File "a.py", line 1, in f', cleaned)

    def test_quote_echo_does_not_swallow_frames(self):
        # 引号贪婪不越过帧边界：值清洗、后续帧完整保留
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        text = (
            f'ValueError: invalid input syntax for type uuid: "a {marker}"\n'
            "Traceback (most recent call last):\n"
            '  File "b.py", line 2, in g\n'
            "    y = 2")
        cleaned, _ = logging_events.scrub_text(text, 4000)
        self.assertNotIn(marker, cleaned)
        self.assertIn('File "b.py", line 2, in g', cleaned)

    def test_combined_newline_quote_and_parens(self):
        marker = "REVIEW2_PRIVATE_BODY_END_123456"
        cleaned, _ = logging_events.scrub_text(
            f'Failing row contains (null, "quo (te) {marker}\nmore") tail', 4000)
        self.assertNotIn(marker, cleaned)


class DeepChainAndExceptionGroupTests(unittest.TestCase):
    """BUG-21 残留：深度边界与 ExceptionGroup 不返回未脱敏对象。"""

    class _APIError(Exception):
        def __init__(self, code, message, details=""):
            self.code = code
            self.message = message
            self.details = details
            super().__init__(f"{code}: {message} {details}")

    def _deep_chain(self, marker):
        exc = RuntimeError("level0")
        for index in range(1, 11):
            if index == 7:
                inner = self._APIError(
                    "23502", "constraint failed",
                    f"Failing row contains (x) {marker}")
                inner.__cause__ = exc
                exc = inner
            else:
                wrapper = RuntimeError(f"wrap{index} {marker}")
                wrapper.__cause__ = exc
                exc = wrapper
        return exc

    def test_deep_chain_layers_do_not_leak(self):
        # 标记为已配置密钥实际值：浅层会被掩码，深层此前原样透传（BUG-21）
        marker = "sk-deep-chain-secret-1234567890"
        with mock.patch.object(logging_events.cfg, "UPSTREAM_API_KEY", marker):
            exc = self._deep_chain(marker)
            formatted = "".join(traceback.format_exception(
                type(exc), logging_events._scrubbed_exception(exc), None))
        self.assertNotIn(marker, formatted)
        # 安全类 / 码保留：类型名与层级文本仍在
        self.assertIn("APIError", formatted)
        self.assertIn("wrap10", formatted)

    def test_exception_group_children_are_scrubbed(self):
        # 标记为已配置密钥实际值（配置值掩码是清洗红线）
        marker = "sk-group-child-secret-1234567890"
        with mock.patch.object(logging_events.cfg, "UPSTREAM_API_KEY", marker):
            child = self._APIError(
                "22P02", "bad uuid", f'invalid input syntax for type uuid: "{marker}"')
            group = ExceptionGroup("group msg", [child, RuntimeError(f"plain {marker}")])
            formatted = "".join(traceback.format_exception(
                type(group), logging_events._scrubbed_exception(group), None))
        self.assertNotIn(marker, formatted)
        # 组结构与子异常身份保留
        self.assertIn("group msg", formatted)

    def test_nested_exception_group_is_scrubbed(self):
        marker = "sk-nested-group-secret-1234567890"
        with mock.patch.object(logging_events.cfg, "UPSTREAM_API_KEY", marker):
            inner = ExceptionGroup(
                "inner", [self._APIError("E1", "m", f"Failing row contains (q) {marker}")])
            outer = ExceptionGroup("outer", [inner])
            formatted = "".join(traceback.format_exception(
                type(outer), logging_events._scrubbed_exception(outer), None))
        self.assertNotIn(marker, formatted)

    def test_group_clone_failure_fails_closed(self):
        # 构造失败（自定义组子类要求特殊参数）→ 安全占位，不返回原对象
        class PickyGroup(ExceptionGroup):
            def __new__(cls, message, exceptions):
                if message != "expected":
                    raise TypeError("bad message")
                return super().__new__(cls, message, exceptions)

        marker = "sk-picky-group-secret-1234567890"
        with mock.patch.object(logging_events.cfg, "UPSTREAM_API_KEY", marker):
            group = PickyGroup("expected", [RuntimeError(f"x {marker}")])
            scrubbed = logging_events._scrubbed_exception(group)
            formatted = "".join(traceback.format_exception(
                type(scrubbed), scrubbed, None))
        self.assertNotIn(marker, formatted)

    def test_depth_limit_placeholder_keeps_type_name(self):
        # 直接构造超深场景：占位保留类型名；底层文本为已配置密钥形态
        marker = "sk-depth-limit-secret-1234567890"
        with mock.patch.object(logging_events.cfg, "UPSTREAM_API_KEY", marker):
            bottom = RuntimeError(f"bottom {marker}")
            current = bottom
            for _ in range(12):
                wrapper = RuntimeError("w")
                wrapper.__cause__ = current
                current = wrapper
            scrubbed = logging_events._scrubbed_exception(current)
            formatted = "".join(traceback.format_exception(
                type(scrubbed), scrubbed, None))
        self.assertNotIn(marker, formatted)
        self.assertIn("chain depth limit reached", formatted)


class UvicornPlatformFilterTests(unittest.TestCase):
    """BUG-25：默认 Uvicorn 命名 logger 的独立 handler 也挂脱敏过滤器。"""

    def setUp(self):
        # 开启态覆盖检查显式启用，不依赖已变更的部署默认值。
        enabled = mock.patch.object(logging_events.cfg, "GATEWAY_LOG_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

    def test_install_covers_uvicorn_handlers(self):
        uvicorn_logger = logging.getLogger("uvicorn")
        uvicorn_error = logging.getLogger("uvicorn.error")
        uvicorn_access = logging.getLogger("uvicorn.access")
        handler = logging.StreamHandler()
        uvicorn_logger.addHandler(handler)
        access_handler = logging.StreamHandler()
        uvicorn_access.addHandler(access_handler)
        self.addCleanup(uvicorn_logger.removeHandler, handler)
        self.addCleanup(uvicorn_access.removeHandler, access_handler)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.assertIn(logging_events._scrub_filter, handler.filters)
        self.assertIn(logging_events._scrub_filter, access_handler.filters)
        # uvicorn.error 无独立 handler 时经 uvicorn 传播覆盖；有则同样覆盖
        error_handler = logging.StreamHandler()
        uvicorn_error.addHandler(error_handler)
        self.addCleanup(uvicorn_error.removeHandler, error_handler)
        logging_events.install_handler()  # 幂等重装
        self.assertIn(logging_events._scrub_filter, error_handler.filters)

    def test_uninstall_removes_filter_from_uvicorn_handlers(self):
        uvicorn_logger = logging.getLogger("uvicorn")
        handler = logging.StreamHandler()
        uvicorn_logger.addHandler(handler)
        self.addCleanup(uvicorn_logger.removeHandler, handler)
        logging_events.install_handler()
        self.assertIn(logging_events._scrub_filter, handler.filters)
        logging_events.uninstall_handler()
        self.assertNotIn(logging_events._scrub_filter, handler.filters)

    def test_uvicorn_platform_output_is_scrubbed(self):
        marker = "sk-uvicorn-platform-secret-1234567890"
        with mock.patch.object(
            logging_events.cfg, "UPSTREAM_API_KEY", marker
        ):
            uvicorn_logger = logging.getLogger("uvicorn.error")
            captured = []

            class CaptureHandler(logging.Handler):
                def emit(self, record):
                    captured.append(self.format(record))

            capture = CaptureHandler()
            uvicorn_logger.addHandler(capture)
            self.addCleanup(uvicorn_logger.removeHandler, capture)
            logging_events.install_handler()
            self.addCleanup(logging_events.uninstall_handler)
            uvicorn_logger.error("upstream failed with %s", marker)
        self.assertEqual(len(captured), 1)
        self.assertNotIn(marker, captured[0])
        self.assertIn("***", captured[0])


class KeyRefIdentityTests(unittest.TestCase):
    """TASK-02：稳定密钥标识、事件归属与按密钥分组（L29/L30）。"""

    SECRET = "k" * 40

    def setUp(self):
        self.queue = logging_events._queue
        self.queue.drain(10000)
        patches = [
            mock.patch.object(logging_events.cfg, "GATEWAY_LOG_ENABLED", True),
            mock.patch.object(
                logging_events.cfg, "GATEWAY_KEY_REF_SECRET", self.SECRET),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(self.queue.drain, 10000)

    def test_same_value_same_ref_and_short_label(self):
        ref1 = logging_events.compute_key_ref("key-A")
        ref2 = logging_events.compute_key_ref("key-A")
        self.assertEqual(ref1, ref2)
        self.assertTrue(ref1.startswith("kh1-"))
        self.assertEqual(len(ref1), 36)
        self.assertEqual(logging_events.key_ref_label(ref1), f"K-{ref1[4:12]}")
        self.assertNotEqual(logging_events.compute_key_ref("key-B"), ref1)

    def test_missing_or_short_secret_yields_no_ref(self):
        with mock.patch.object(
                logging_events.cfg, "GATEWAY_KEY_REF_SECRET", ""):
            self.assertEqual(logging_events.compute_key_ref("key-A"), "")
            self.assertFalse(logging_events.key_ref_ready())
        with mock.patch.object(
                logging_events.cfg, "GATEWAY_KEY_REF_SECRET", "short"):
            self.assertEqual(logging_events.compute_key_ref("key-A"), "")
            self.assertFalse(logging_events.key_ref_ready())

    def test_key_ref_secret_value_is_masked_in_scrub(self):
        # 标识秘密本身绝不进入日志 / 平台输出
        cleaned, _ = logging_events.scrub_text(
            f"leaked secret {self.SECRET}", 4000)
        self.assertNotIn(self.SECRET, cleaned)

    def test_credential_scope_stamps_events(self):
        with logging_events.credential_scope(
                "chat", "api.example.com", "key-A"):
            logging.getLogger("gateway.chat").error("chat failed")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["key_ref"], logging_events.compute_key_ref("key-A"))
        self.assertEqual(event["credential_role"], "chat")
        self.assertEqual(event["provider"], "api.example.com")

    def test_two_keys_same_fault_split_into_groups(self):
        # 同功能 / 来源 / 错误、两把密钥：fingerprint 与分组身份不同
        for key in ("key-A", "key-B"):
            with logging_events.credential_scope("chat", "api.example.com", key):
                logging.getLogger("gateway.chat").error("chat failed")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 2)
        refs = {e["key_ref"] for e in events}
        self.assertEqual(len(refs), 2)
        self.assertEqual(len({e["fingerprint"] for e in events}), 2)

    def test_unscoped_events_keep_legacy_fingerprint_and_merge(self):
        # 无凭证事件与历史未知记录同口径：保持旧公式指纹，不拆组
        logging.getLogger("gateway.chat").error("chat failed")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["key_ref"], "")
        legacy = logging_events._fingerprint(
            event["module"], event["operation"], event["error_code"],
            event["exception_type"], event["logger_name"], event["source"])
        self.assertEqual(event["fingerprint"], legacy)

    def test_exception_annotation_propagates_credential(self):
        # 穿过调用边界的异常被标注：边界外捕获点的事件仍归属实际密钥
        def inner():
            with logging_events.credential_scope(
                    "analysis", "api.siliconflow.cn", "key-C"):
                raise RuntimeError("model failed")

        try:
            inner()
        except RuntimeError:
            # 边界外（模拟 to_thread 另一侧的捕获点）记录
            logging.getLogger("gateway.memory").exception("digest failed")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["key_ref"], logging_events.compute_key_ref("key-C"))
        self.assertEqual(events[0]["credential_role"], "analysis")

    def test_rumination_fallback_attributes_effective_key(self):
        # 反刍回退：实际生效 Key（CONTINUITY 回退）归属正确
        from gateway import memory_rumination

        with mock.patch.object(
                logging_events.cfg, "RUMINATION_API_KEY", ""), \
                mock.patch.object(
                    logging_events.cfg, "CONTINUITY_API_KEY", "key-fallback"):
            base_url, api_key, _ = memory_rumination._model_config()
        self.assertEqual(api_key, "key-fallback")
        with logging_events.credential_scope("rumination", "host", api_key):
            logging.getLogger("gateway.memory").error("rumination failed")
        events = self.queue.drain(10000)
        self.assertEqual(
            events[0]["key_ref"], logging_events.compute_key_ref("key-fallback"))

    def test_collection_status_reports_key_ref_ready(self):
        status = logging_events.collection_status()
        self.assertTrue(status["key_ref_ready"])


if __name__ == "__main__":
    unittest.main()
