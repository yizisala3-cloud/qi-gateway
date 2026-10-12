"""GatewayLogMiddleware 测试（§4.1 / §6 主记录者仲裁）。

覆盖：X-Request-ID 响应头（服务端生成，不信任客户端）、5xx 无日志时的
边界兜底事件、已记录时不双重计数、未处理异常事件、404/400 不记故障、
SSE 200 流内错误不触发边界事件、请求上下文在线程中可见。
"""

import asyncio
import logging
import unittest
from unittest import mock

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from gateway import logging_events
from gateway.config import cfg


async def ok(request):
    return JSONResponse({"ok": True})


async def bad_request(request):
    return JSONResponse({"error": "bad"}, status_code=400)


async def five_without_log(request):
    # 模拟「返回 5xx 却没有任何日志」的缺口路径
    return JSONResponse({"error": "boom"}, status_code=500)


async def five_with_log(request):
    logging_events.emit_error(
        "planning", "create", "internal_error", "规划接口发生内部错误",
        outcome="failed",
    )
    return JSONResponse({"error": "boom"}, status_code=500)


async def unhandled(request):
    raise RuntimeError("unhandled boom")


async def sse(request):
    async def gen():
        yield "data: hello\n\n"
        logging_events.emit_error(
            "chat", "chat_stream", "stream_interrupted",
            "聊天流式输出中断", outcome="failed",
        )
    return StreamingResponse(gen(), media_type="text/event-stream")


async def warning_then_crash(request):
    # BUG-03：可恢复 warning 之后抛出独立未处理异常
    logging.getLogger("gateway.context").warning("Synthetic recoverable context warning")
    raise RuntimeError("REVIEW_UNHANDLED_ROOT_CAUSE")


async def warning_then_silent_500(request):
    # BUG-03：可恢复 warning 之后返回无日志的 500
    logging.getLogger("gateway.context").warning("Synthetic recoverable context warning")
    return JSONResponse({"error": "unlogged failure"}, status_code=500)


async def logged_then_reraise(request):
    # BUG-03：端点已 log.exception 后重新 raise 同一异常——不得双计
    try:
        raise ValueError("logged and re-raised")
    except ValueError as exc:
        logging.getLogger("gateway.planning_api").exception("planning 失败")
        raise


def _app():
    return Starlette(
        routes=[
            Route("/ok", ok),
            Route("/bad", bad_request),
            Route("/five-silent", five_without_log),
            Route("/five-logged", five_with_log),
            Route("/unhandled", unhandled),
            Route("/sse", sse),
            Route("/warn-crash", warning_then_crash),
            Route("/warn-five", warning_then_silent_500),
            Route("/logged-reraise", logged_then_reraise),
        ],
        middleware=[Middleware(logging_events.GatewayLogMiddleware)],
    )


class GatewayLogMiddlewareTests(unittest.TestCase):
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
        self.http = TestClient(_app(), raise_server_exceptions=False)

    def _events(self):
        return self.queue.drain(10000)

    def test_response_carries_server_generated_request_id(self):
        response = self.http.get("/ok")
        self.assertEqual(response.status_code, 200)
        request_id = response.headers.get("x-request-id")
        self.assertTrue(request_id)
        self.assertEqual(len(request_id), 16)
        # 客户端提供的 X-Request-ID 不被信任：响应头仍是服务端生成的
        response = self.http.get(
            "/ok", headers={"X-Request-ID": "client-forged-id"})
        self.assertNotEqual(
            response.headers.get("x-request-id"), "client-forged-id")

    def test_5xx_without_log_records_boundary_event(self):
        response = self.http.get("/five-silent")
        self.assertEqual(response.status_code, 500)
        events = self._events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "http_5xx_boundary")
        self.assertEqual(event["module"], "system")  # 未匹配路由规则
        self.assertEqual(event["http_status"], 500)
        self.assertTrue(event["request_id"])
        self.assertEqual(event["route"], "/five-silent")

    def test_5xx_with_log_does_not_double_count(self):
        response = self.http.get("/five-logged")
        self.assertEqual(response.status_code, 500)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "internal_error")
        self.assertEqual(events[0]["module"], "planning")
        self.assertEqual(events[0]["http_status"], 500)
        self.assertEqual(events[0]["route"], "/five-logged")

    def test_unhandled_exception_is_recorded_once(self):
        response = self.http.get("/unhandled")
        self.assertEqual(response.status_code, 500)
        events = self._events()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["error_code"], "unhandled_exception")
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertIn("unhandled boom", event["stack_trace"])
        self.assertEqual(event["route"], "/unhandled")

    def test_4xx_and_404_are_not_recorded(self):
        self.http.get("/bad")
        self.http.get("/no-such-route")
        self.assertEqual(self._events(), [])

    # ── BUG-03：warning 不抑制后续异常 / 5xx 兜底 ───────────────

    def test_warning_does_not_suppress_unhandled_exception(self):
        response = self.http.get("/warn-crash")
        self.assertEqual(response.status_code, 500)
        events = self._events()
        # warning 与未处理异常并存：根因必须可查
        self.assertEqual(len(events), 2)
        levels = {event["level"] for event in events}
        self.assertEqual(levels, {"WARNING", "ERROR"})
        root_cause = next(
            event for event in events if event["level"] == "ERROR")
        self.assertEqual(root_cause["error_code"], "unhandled_exception")
        self.assertEqual(root_cause["exception_type"], "RuntimeError")
        self.assertIn("REVIEW_UNHANDLED_ROOT_CAUSE", root_cause["stack_trace"])
        self.assertTrue(root_cause["request_id"])

    def test_warning_does_not_suppress_5xx_boundary(self):
        response = self.http.get("/warn-five")
        self.assertEqual(response.status_code, 500)
        events = self._events()
        self.assertEqual(len(events), 2)
        codes = {event["error_code"] for event in events}
        self.assertEqual(codes, {"unlogged_error", "http_5xx_boundary"})
        boundary = next(
            event for event in events
            if event["error_code"] == "http_5xx_boundary")
        self.assertEqual(boundary["http_status"], 500)

    def test_logged_and_reraised_exception_counts_once(self):
        response = self.http.get("/logged-reraise")
        self.assertEqual(response.status_code, 500)
        events = self._events()
        # 端点 log.exception 后重新 raise：只计一次（异常身份去重）
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["exception_type"], "ValueError")
        self.assertEqual(events[0]["error_code"], "unlogged_error")

    def test_sse_stream_error_records_event_without_boundary_duplication(self):
        with self.http.stream("GET", "/sse") as response:
            self.assertEqual(response.status_code, 200)
            body = b"".join(response.iter_bytes())
        self.assertIn(b"hello", body)
        events = self._events()
        # 只有流内 emit_error 的一条；HTTP 200 不触发边界兜底
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "stream_interrupted")
        self.assertEqual(events[0]["module"], "chat")
        self.assertEqual(events[0]["http_status"], 200)

    def test_route_template_hides_path_params(self):
        # Starlette 0.49 不写 scope["route"]：模板由路由表正则匹配得出，
        # 路径参数（业务 ID）不进入事件（§4 route 语义）。
        from starlette.responses import JSONResponse
        from starlette.routing import Route

        async def item(request):
            logging.getLogger("gateway.db").error("db error")
            return JSONResponse({"id": request.path_params["row_id"]})

        app = Starlette(
            routes=[Route("/items/{row_id:int}", item)],
            middleware=[Middleware(logging_events.GatewayLogMiddleware)],
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/items/424242")
        self.assertEqual(response.status_code, 200)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["route"], "/items/{row_id:int}")
        self.assertNotIn("424242", events[0]["route"])

    def test_request_context_visible_in_to_thread(self):
        # asyncio.to_thread 复制上下文：线程内日志能关联 request_id
        captured = {}

        async def thread_log(request):
            def job():
                captured["request_id"] = logging_events.current_request_id()
                logging.getLogger("gateway.planning").error("线程内错误")
            await asyncio.to_thread(job)
            return JSONResponse({"ok": True})

        app = Starlette(
            routes=[Route("/thread", thread_log)],
            middleware=[Middleware(logging_events.GatewayLogMiddleware)],
        )
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/thread")
        self.assertEqual(response.status_code, 200)
        request_id = response.headers["x-request-id"]
        self.assertEqual(captured["request_id"], request_id)
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["request_id"], request_id)
        self.assertEqual(events[0]["module"], "planning")


if __name__ == "__main__":
    unittest.main()
