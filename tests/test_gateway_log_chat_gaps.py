"""聊天转发 / SSE 缺口补齐测试（§3.1 L04 / L05）。

此前「非流式超时返回 504、上游非 2xx 透传、SSE 上游非 200 / 超时」
均无日志。本批用 emit_error 补齐；此处通过真实 main.app + mock 上游
客户端验证：每种故障恰好产生一条可检索的结构化事件，业务响应语义
（状态码 / SSE 流内错误协议）保持不变。
"""

import json
import logging
import unittest
from unittest import mock

import httpx
from starlette.testclient import TestClient

from gateway import logging_events
from gateway.config import cfg

GATEWAY_TOKEN = "gateway-token-chat-test"


class FakeResponse:
    def __init__(self, status_code, content=b"{}"):
        self.status_code = status_code
        self.content = content


class FakeStreamResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    async def aread(self):
        return b'{"error": "upstream rejected"}'

    async def aiter_lines(self):
        return
        yield  # pragma: no cover - make it an async generator


class FakeStreamContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *args):
        return False


class FakeHttpClient:
    """可编程假上游客户端：post / stream 按用例注入行为。"""

    def __init__(self):
        self.post_behavior = None
        self.stream_behavior = None

    async def post(self, *args, **kwargs):
        behavior = self.post_behavior
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    def stream(self, *args, **kwargs):
        behavior = self.stream_behavior
        if isinstance(behavior, Exception):

            class Raising:
                async def __aenter__(self):
                    raise behavior

                async def __aexit__(self, *a):
                    return False

            return Raising()
        return FakeStreamContext(behavior)


class ChatGapLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gateway import main

        cls.main = main

    def setUp(self):
        self.queue = logging_events._queue
        self.queue.drain(10000)
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True),
            mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"),
            mock.patch.object(cfg, "GEMINI_BROWSER_TOOL_COMPAT_ENABLED", False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(self.queue.drain, 10000)
        self.fake_client = FakeHttpClient()
        patch = mock.patch.object(self.main, "http_client", self.fake_client)
        patch.start()
        self.addCleanup(patch.stop)
        # lifespan 用例会关闭模块级 bg_executor；每个用例用独立执行器隔离
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gw-test")
        executor_patch = mock.patch.object(self.main, "bg_executor", executor)
        executor_patch.start()
        self.addCleanup(executor_patch.stop)
        self.addCleanup(executor.shutdown, wait=False)
        self.http = TestClient(self.main.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def _chat_events(self):
        return [e for e in self.queue.drain(10000) if e["module"] == "chat"]

    def _post(self, stream=False):
        return self.http.post(
            "/v1/chat/completions",
            json={"model": "test-model", "stream": stream,
                  "messages": [{"role": "user", "content": "你好"}]},
            headers=self.auth,
        )

    def test_non_stream_timeout_returns_504_and_logs_event(self):
        self.fake_client.post_behavior = httpx.TimeoutException("read timeout")
        response = self._post()
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["error"], "upstream timeout")
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "upstream_timeout")
        self.assertEqual(event["message_zh"], "聊天转发请求超时")
        self.assertEqual(event["outcome"], "failed")
        self.assertEqual(event["safe_context"].get("timeout_seconds"), 180)
        self.assertTrue(event["request_id"])
        # BUG-18：真实异常对象保留——类型与堆栈可查
        self.assertEqual(event["exception_type"], "TimeoutException")
        self.assertIn("TimeoutException", event["stack_trace"])

    def test_non_stream_read_timeout_keeps_specific_type(self):
        # ReadTimeout / ConnectTimeout 等具体超时类型可区分（BUG-18）
        self.fake_client.post_behavior = httpx.ReadTimeout("read timeout")
        response = self._post()
        self.assertEqual(response.status_code, 504)
        events = self._chat_events()
        self.assertEqual(events[0]["exception_type"], "ReadTimeout")

    def test_non_stream_connect_error_returns_502_and_logs_event(self):
        self.fake_client.post_behavior = httpx.ConnectError("connection refused")
        response = self._post()
        self.assertEqual(response.status_code, 502)
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "upstream_connect_error")
        self.assertEqual(event["exception_type"], "ConnectError")
        self.assertIn("connection refused", event["stack_trace"])

    def test_non_stream_upstream_5xx_passthrough_logs_degraded_event(self):
        self.fake_client.post_behavior = FakeResponse(
            503, json.dumps({"error": "model overloaded"}).encode())
        response = self._post()
        # 业务语义保持：状态码与正文原样透传
        self.assertEqual(response.status_code, 503)
        self.assertIn("model overloaded", response.text)
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "upstream_status_error")
        self.assertEqual(event["level"], "ERROR")
        self.assertEqual(event["outcome"], "degraded")
        self.assertEqual(event["upstream_status"], 503)
        # 事件先于响应产生：状态由中间件回填
        self.assertEqual(event["http_status"], 503)

    def test_non_stream_upstream_4xx_logs_warning(self):
        self.fake_client.post_behavior = FakeResponse(
            429, json.dumps({"error": "rate limited"}).encode())
        response = self._post()
        self.assertEqual(response.status_code, 429)
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["level"], "WARNING")
        self.assertEqual(events[0]["upstream_status"], 429)

    def test_stream_upstream_non_200_keeps_sse_protocol(self):
        self.fake_client.stream_behavior = FakeStreamResponse(500)
        response = self._post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        body = response.text
        self.assertIn("upstream rejected", body)
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "upstream_status_error")
        self.assertEqual(event["operation"], "chat_stream")
        self.assertEqual(event["upstream_status"], 500)
        self.assertEqual(event["http_status"], 200)  # SSE 头已发出，不改写状态

    def test_stream_timeout_yields_in_stream_error_and_logs(self):
        self.fake_client.stream_behavior = httpx.ReadTimeout("read timeout")
        response = self._post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("upstream read timeout", response.text)
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "stream_timeout")
        # BUG-18：SSE 超时同样保留异常类型与堆栈
        self.assertEqual(events[0]["exception_type"], "ReadTimeout")
        self.assertIn("ReadTimeout", events[0]["stack_trace"])

    def test_stream_interrupt_logs_exception(self):
        self.fake_client.stream_behavior = RuntimeError("connection reset")
        response = self._post(stream=True)
        self.assertEqual(response.status_code, 200)
        events = self._chat_events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "stream_interrupted")
        self.assertEqual(event["exception_type"], "RuntimeError")

    def test_normal_upstream_200_logs_nothing(self):
        self.fake_client.post_behavior = FakeResponse(
            200, json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "好的"}}]
            }).encode())

        async def _noop_persist(user_text, assistant_text):
            return None

        # 旁路保存单独覆盖（无 DB 时它自身的警告是真实事件，不属本用例）
        with mock.patch.object(self.main, "persist_chat_records", _noop_persist):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        # 正常成功请求不进入报错日志（§3.2）
        self.assertEqual(self._chat_events(), [])

    def test_nested_executor_failures_keep_request_id_and_degraded(self):
        """BUG-09/10：两层线程（run_in_executor → executor.submit）内的
        数据源故障仍关联本次请求 ID；降级不标 failed，聊天继续成功。"""
        from gateway import context as context_module
        from gateway import persona as persona_module

        self.fake_client.post_behavior = FakeResponse(
            200, json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "好的"}}]
            }).encode())

        async def _noop_persist(user_text, assistant_text):
            return None

        def _raise(*args, **kwargs):
            raise RuntimeError("eventide source down")

        with mock.patch.object(self.main, "persist_chat_records", _noop_persist), \
                mock.patch.object(persona_module, "_cache", None), \
                mock.patch.object(context_module, "build_eventide_context", side_effect=_raise):
            response = self._post()
        # fail-open：数据源故障不阻断聊天
        self.assertEqual(response.status_code, 200)
        request_id = response.headers["x-request-id"]
        events = self.queue.drain(10000)

        # persona 的「读取失败且无缓存」在内层线程发出：必须携带请求关联
        persona_events = [e for e in events if e["module"] == "persona"]
        self.assertEqual(len(persona_events), 1)
        persona_event = persona_events[0]
        self.assertEqual(persona_event["request_id"], request_id)
        self.assertEqual(persona_event["route"], "/v1/chat/completions")
        self.assertEqual(persona_event["outcome"], "degraded")  # BUG-10
        self.assertEqual(persona_event["error_code"], "context_source_failed")

        # 公共 db 初始化的警告同样发生在内层线程（BUG-09 此前丢关联）
        db_events = [e for e in events if e["module"] == "database"]
        self.assertGreaterEqual(len(db_events), 1)
        for event in db_events:
            self.assertEqual(event["request_id"], request_id)

        # 外层数据源捕获（eventide 注入失败）按降级记录且关联请求
        context_events = [
            e for e in events
            if e["module"] == "context" and e.get("safe_context", {}).get("data_source") == "eventide"
        ]
        self.assertEqual(len(context_events), 1)
        self.assertEqual(context_events[0]["request_id"], request_id)
        self.assertEqual(context_events[0]["outcome"], "degraded")

    def test_lifespan_startup_shutdown_cycle(self):
        """真实 lifespan 周期：启动装 handler + 起 writer，关闭先停接收再 flush。

        memory_mcp 的 session_manager 是模块级单例，真实 run() 一进程只能
        启动一次；此处打桩隔离，避免与 test_memory_mcp_protocol 互相污染。
        """
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        @asynccontextmanager
        async def _stub_run():
            yield

        stub = SimpleNamespace(session_manager=SimpleNamespace(run=_stub_run))
        with mock.patch.object(self.main, "memory_mcp", stub), \
                mock.patch.object(self.main.db, "load_app_setting", return_value=True), \
                mock.patch.object(cfg, "GATEWAY_LOG_FLUSH_TIMEOUT", 0.5):
            with TestClient(self.main.app) as client:
                self.assertTrue(logging_events.handler_installed())
                writer = logging_events.get_writer()
                self.assertIsNotNone(writer)
                self.assertTrue(writer._thread.is_alive())
                response = client.get("/health")
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers.get("x-request-id"))
            # 退出后：handler 卸载、writer 停止（有界关闭，不阻塞）
            self.assertFalse(logging_events.handler_installed())
            self.assertIsNone(logging_events.get_writer())

    def test_lifespan_default_off_has_no_collector_or_writer(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace

        @asynccontextmanager
        async def _stub_run():
            yield

        stub = SimpleNamespace(session_manager=SimpleNamespace(run=_stub_run))
        with mock.patch.object(self.main, "memory_mcp", stub), \
                mock.patch.object(self.main.db, "load_app_setting", return_value=None), \
                mock.patch.object(self.main.log_settings, "_environment_default", False):
            with TestClient(self.main.app) as client:
                self.assertFalse(logging_events.handler_installed())
                self.assertIsNone(logging_events.get_writer())
                self.assertEqual(client.get("/health").status_code, 200)
            self.assertIsNone(logging_events.get_writer())


class ContextCaughtExceptionTests(unittest.TestCase):
    """BUG-18（残留）：context 各真实捕获点把原异常交给主记录者。

    只写 type(e).__name__ 进消息、不传 exc_info 的捕获会让事件丢掉真实
    异常类型与调用帧；这里逐个数据源验证 degraded 事件带类型与堆栈。
    """

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

    def test_recent_chat_failure_keeps_exception_and_stack(self):
        from gateway import context as context_module

        client = mock.MagicMock()
        (client.table.return_value.select.return_value.order.return_value
         .limit.return_value.execute.side_effect) = RuntimeError(
            "recent source failure")
        with mock.patch.object(context_module.db, "get_client", return_value=client):
            returned = context_module.build_recent_chat_context()
        self.assertEqual(returned, "")  # 降级返回空，主请求继续
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "context")
        self.assertEqual(event["outcome"], "degraded")
        self.assertEqual(event["exception_type"], "RuntimeError")  # BUG-18
        self.assertIn("recent source failure", event["stack_trace"])
        self.assertIn("build_recent_chat_context", event["stack_trace"])

    def test_eventide_failure_keeps_exception_and_stack(self):
        from gateway import context as context_module

        with mock.patch.object(
                context_module.app_settings, "is_eventide_injection_enabled",
                return_value=True), \
                mock.patch.object(
                    context_module.db, "load_eventide_state",
                    side_effect=RuntimeError("eventide source failure")):
            returned = context_module.build_eventide_context()
        self.assertEqual(returned, "")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["outcome"], "degraded")
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertIn("eventide source failure", event["stack_trace"])

    def test_persona_future_failure_keeps_exception_and_stack(self):
        from gateway import context as context_module

        def _raise():
            raise RuntimeError("persona source failure")

        with mock.patch.object(
                context_module.app_settings, "is_timestamp_injection_enabled",
                return_value=False), \
                mock.patch.object(
                    context_module.app_settings, "is_recent_chat_injection_enabled",
                    return_value=False), \
                mock.patch.object(
                    context_module.app_settings, "is_eventide_injection_enabled",
                    return_value=False), \
                mock.patch.object(context_module, "load_persona", side_effect=_raise):
            result = context_module.build_context("")
        self.assertEqual(result, "")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "context")
        self.assertEqual(event["outcome"], "degraded")
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertIn("persona source failure", event["stack_trace"])

    def test_memory_search_failure_keeps_exception_and_stack(self):
        from gateway import context as context_module

        async def _raise(*args, **kwargs):
            raise RuntimeError("memory search failure")

        with mock.patch.object(
                context_module.app_settings, "is_timestamp_injection_enabled",
                return_value=False), \
                mock.patch.object(
                    context_module.app_settings, "is_recent_chat_injection_enabled",
                    return_value=False), \
                mock.patch.object(
                    context_module.app_settings, "is_eventide_injection_enabled",
                    return_value=False), \
                mock.patch.object(context_module, "load_persona", return_value=""), \
                mock.patch.object(context_module, "search_memories", side_effect=_raise):
            result = context_module.build_context("找一下相关记忆")
        self.assertEqual(result, "")
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "context")
        self.assertEqual(event["outcome"], "degraded")
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertIn("memory search failure", event["stack_trace"])


SECRET = "sk-chat-echo-secret-1234567890abcd"


class ChatErrorBodyScrubTests(unittest.TestCase):
    """BUG-26：聊天 / SSE 错误出口不回传上游敏感原文（L32）。

    上游错误正文或 SDK / 网络异常文本包含已配置密钥时，普通请求 / SSE
    的错误响应出口先清洗后返回；成功响应（2xx）原样透传不受影响。
    """

    @classmethod
    def setUpClass(cls):
        from gateway import main

        cls.main = main

    def setUp(self):
        self.queue = logging_events._queue
        self.queue.drain(10000)
        self.secret = "sk-chat-echo-secret-1234567890abcd"
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True),
            mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"),
            mock.patch.object(cfg, "GEMINI_BROWSER_TOOL_COMPAT_ENABLED", False),
            mock.patch.object(cfg, "UPSTREAM_API_KEY", self.secret),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(self.queue.drain, 10000)
        self.fake_client = FakeHttpClient()
        patch = mock.patch.object(self.main, "http_client", self.fake_client)
        patch.start()
        self.addCleanup(patch.stop)
        from concurrent.futures import ThreadPoolExecutor
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="gw-test26")
        executor_patch = mock.patch.object(self.main, "bg_executor", executor)
        executor_patch.start()
        self.addCleanup(executor_patch.stop)
        self.addCleanup(executor.shutdown, wait=False)
        self.http = TestClient(self.main.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def _post(self, stream=False):
        return self.http.post(
            "/v1/chat/completions",
            json={"model": "test-model", "stream": stream,
                  "messages": [{"role": "user", "content": "你好"}]},
            headers=self.auth,
        )

    def test_non_stream_upstream_error_body_is_scrubbed(self):
        # 上游 401 错误体回显已配置密钥（真实供应商常见形态）
        self.fake_client.post_behavior = FakeResponse(
            401, json.dumps({
                "error": f"Incorrect API key provided: {self.secret}"
            }).encode())
        response = self._post()
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(self.secret, response.text)
        self.assertIn("***", response.text)
        # JSON 结构保留（客户端协议不破坏）
        self.assertIn("error", response.json())

    def test_non_stream_success_body_is_untouched(self):
        # 正常成功响应不做任何密钥替换（不破坏正常协议）
        body = json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "好的"}}
                        ]}).encode()
        self.fake_client.post_behavior = FakeResponse(200, body)

        async def _noop_persist(user_text, assistant_text):
            return None

        with mock.patch.object(self.main, "persist_chat_records", _noop_persist):
            response = self._post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, body)

    def test_sse_upstream_error_body_is_scrubbed(self):
        class SecretEchoStream(FakeStreamResponse):
            async def aread(self):
                return (f'{{"error": "key {SECRET} rejected"}}').encode()

        self.fake_client.stream_behavior = SecretEchoStream(401)
        response = self._post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.secret, response.text)
        self.assertIn("***", response.text)

    def test_sse_interrupt_exception_text_is_scrubbed(self):
        # 中断异常文本包含密钥：出口清洗后进入流内错误
        self.fake_client.stream_behavior = httpx.ReadTimeout(
            f"read failed for {self.secret}")
        response = self._post(stream=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.secret, response.text)
        # 事件侧同样无密钥（持久化链清洗）
        events = [e for e in self.queue.drain(10000) if e["module"] == "chat"]
        self.assertEqual(len(events), 1)
        self.assertNotIn(self.secret, events[0]["message_raw"])
        self.assertNotIn(self.secret, events[0]["stack_trace"])


if __name__ == "__main__":
    unittest.main()
