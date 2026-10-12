"""BUG-04 / BUG-10 回归：MCP 记忆工具失败采集与 quiet 内层语义。

* BUG-04：MCP tools/call 的服务端依赖故障被 SDK 包成 HTTP 200 + isError，
  HTTP 边界兜底永远看不到——必须在工具边界采集；预期 4xx 业务拒绝不升级。
* BUG-10：待办创建成功后 generate_task 在内层捕获异常并返回 errors，
  事件必须标 committed_side_effect_failed（主操作已成功），不误报保存失败。
"""

import logging
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, time, timezone
from unittest import mock

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Mount
from starlette.testclient import TestClient

from gateway import logging_events
from gateway.config import cfg
import gateway.memory_mcp as memory_mcp_module
from gateway.memory_requests import MemoryRequestError

GATEWAY_TOKEN = "gateway-token-mcp-quiet"


class McpToolFailureCaptureTests(unittest.TestCase):
    """BUG-04：经真实 Streamable HTTP tools/call 验证采集。

    SDK 的 session manager 一进程只能 run() 一次；这里为每个用例构建
    全新的 streamable app（新 manager），并在结束后恢复底层
    ``_session_manager``，避免影响 test_memory_mcp_protocol 的单例。
    """

    def _app(self):
        lowlevel = memory_mcp_module.memory_mcp._lowlevel_server
        original_manager = lowlevel._session_manager
        self.addCleanup(setattr, lowlevel, "_session_manager", original_manager)
        fresh_app = memory_mcp_module.memory_mcp.streamable_http_app(
            streamable_http_path="/mcp",
            stateless_http=True,
            json_response=True,
            max_request_body_size=65_536,
            transport_security=memory_mcp_module.TransportSecuritySettings(
                enable_dns_rebinding_protection=False,
            ),
        )

        @asynccontextmanager
        async def lifespan(_app):
            async with memory_mcp_module.memory_mcp.session_manager.run():
                yield

        return Starlette(
            routes=[Mount("/", app=memory_mcp_module.MCPBearerAuth(fresh_app))],
            lifespan=lifespan,
            middleware=[Middleware(logging_events.GatewayLogMiddleware)],
        )

    def setUp(self):
        self.queue = logging_events._queue
        self.queue.drain(10000)
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(cfg, "MCP_MEMORY_TOKEN", "mcp-log-secret"),
            mock.patch.object(cfg, "MEMORY_ASSISTANT_ID", "assistant-log"),
            mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True),
            mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(self.queue.drain, 10000)
        # 进入 TestClient 上下文以运行 lifespan（session manager run()）
        self.http = self.enterContext(
            TestClient(self._app(), raise_server_exceptions=False))
        self.auth = {
            "Authorization": "Bearer mcp-log-secret",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

    def _rpc(self, method, params):
        response = self.http.post(
            "/mcp", headers=self.auth,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            follow_redirects=False,
        )
        self.assertEqual(response.status_code, 200)
        return response.json()

    def _tools_call(self, name, arguments):
        return self._rpc("tools/call", {"name": name, "arguments": arguments})

    def test_infrastructure_failure_is_captured(self):
        def fake_create(payload, _key, **trusted):
            raise MemoryRequestError(
                "request_store_failed", "database unavailable", 503)

        with mock.patch.object(
                memory_mcp_module, "create_memory_request",
                side_effect=fake_create):
            result = self._tools_call("remember_moment", {
                "content": "验证基础设施故障采集的内容",
                "reason": "验证 MCP 采集",
                "scene": "场景", "event": "事件",
                "moment_state": "standalone",
            })
        # MCP 协议不变：HTTP 200 + isError 结果
        self.assertTrue(result["result"]["isError"])
        self.assertIn("request_store_failed", result["result"]["content"][0]["text"])
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "memory")
        self.assertEqual(event["operation"], "mcp_remember_moment")
        self.assertEqual(event["error_code"], "request_store_failed")
        self.assertEqual(event["level"], "ERROR")
        self.assertEqual(event["outcome"], "failed")
        # 保留原始异常链（MemoryRequestError 的 cause 不可用时为自身）
        self.assertEqual(event["exception_type"], "MemoryRequestError")
        self.assertTrue(event["stack_trace"])
        self.assertTrue(event["request_id"])  # 经 MCPBearerAuth 的请求也有关联

    def test_unexpected_exception_is_captured_with_protocol_intact(self):
        def fake_create(payload, _key, **trusted):
            raise RuntimeError("unexpected tool crash")

        with mock.patch.object(
                memory_mcp_module, "create_memory_request",
                side_effect=fake_create):
            result = self._tools_call("remember_moment", {
                "content": "验证意外异常采集的内容",
                "reason": "验证 MCP 采集",
                "scene": "场景", "event": "事件",
                "moment_state": "standalone",
            })
        self.assertTrue(result["result"]["isError"])
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["error_code"], "mcp_tool_failed")
        self.assertEqual(events[0]["exception_type"], "RuntimeError")

    def test_expected_business_rejection_is_not_escalated(self):
        def fake_create(payload, _key, **trusted):
            raise MemoryRequestError(
                "invalid_payload", "content must be a string", 400)

        with mock.patch.object(
                memory_mcp_module, "create_memory_request",
                side_effect=fake_create):
            result = self._tools_call("remember_moment", {
                "content": "验证预期拒绝不采集的内容",
                "reason": "验证 MCP 采集",
                "scene": "场景", "event": "事件",
                "moment_state": "standalone",
            })
        self.assertTrue(result["result"]["isError"])
        # 预期 4xx 业务拒绝：不进报错日志
        self.assertEqual(self.queue.drain(10000), [])

    def test_review_tool_infrastructure_failure_is_captured(self):
        def fake_review(_assistant_id, request_id, _payload):
            raise MemoryRequestError(
                "request_store_failed", "review database unavailable", 503)

        with mock.patch.object(
                memory_mcp_module, "review_ai_memory_request",
                side_effect=fake_review):
            result = self._tools_call("review_memory_requests", {
                "action": "approve", "request_id": 10,
            })
        self.assertTrue(result["result"]["isError"])
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["operation"], "mcp_review_memory_requests")
        self.assertEqual(events[0]["error_code"], "request_store_failed")


class PlanningQuietOutcomeTests(unittest.TestCase):
    """BUG-10：quiet 内层捕获（返回 errors 路径）标「已提交后失败」。"""

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

    def test_generate_task_inner_failure_marks_committed_side_effect(self):
        from gateway import planning_common
        from gateway import planning_cycles as cycles
        from gateway import planning_generation
        from gateway.planning_generation import generate_task
        from gateway import planning_runtime as runtime

        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        context = cycles.PlanningRequestContext(now)
        # 预置边界状态（不读库）：boundary / cycle / daily_refresh 都可用
        context._boundary = (time(3, 0), None, frozenset())
        context._values = {
            planning_common.PLANNING_BOUNDARY_STATE_KEY: None,
            planning_common.PLANNING_DAILY_REFRESH_KEY: True,
        }
        with mock.patch.object(runtime, "_require_client", return_value=object()), \
                mock.patch.object(
                    runtime, "_fetch_task",
                    return_value={"id": 7, "is_active": True}), \
                mock.patch.object(
                    planning_generation, "_reconcile_task_rounds",
                    side_effect=RuntimeError("reconcile boom")):
            with planning_generation.quiet_generation_scope():
                result = generate_task(7, now, context=context)
        # 返回 errors（不抛出）：主调用方继续重算
        self.assertEqual(result["errors"], [{"task_id": 7, "error": "RuntimeError"}])
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "planning")
        self.assertEqual(event["operation"], "generate_task")
        self.assertEqual(event["error_code"], "quiet_generate_failed")
        self.assertEqual(event["outcome"], "committed_side_effect_failed")
        self.assertEqual(event["exception_type"], "RuntimeError")
        self.assertIn("reconcile boom", event["stack_trace"])
        self.assertEqual(event["safe_context"], {"task_id": 7})
        self.assertIn("待办已保存", event["message_zh"])

    def test_quiet_double_failure_marks_both_committed_side_effect(self):
        # BUG-10（残留）：quiet 固定轮生成失败 + finally 到期清理失败——
        # 两个独立异常分别记录，且都标「已提交后失败」，不误报主操作失败。
        from gateway import planning_common
        from gateway import planning_cycles as cycles
        from gateway import planning_generation
        from gateway.planning_generation import generate_task
        from gateway import planning_runtime as runtime

        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        context = cycles.PlanningRequestContext(now)
        context._boundary = (time(3, 0), None, frozenset())
        context._values = {
            planning_common.PLANNING_BOUNDARY_STATE_KEY: None,
            planning_common.PLANNING_DAILY_REFRESH_KEY: True,
        }
        task = {"id": 7, "is_active": True, "task_type": "interval",
                "refresh_mode": "fixed_interval", "time_mode": "rule"}
        with mock.patch.object(runtime, "_require_client", return_value=object()), \
                mock.patch.object(runtime, "_fetch_task", return_value=task), \
                mock.patch.object(
                    planning_generation, "_fixed_rounds",
                    return_value=[(now.date(), now)]), \
                mock.patch.object(
                    planning_generation, "_following_fixed_event",
                    return_value=now), \
                mock.patch.object(
                    planning_generation, "_expire_fixed_rounds",
                    side_effect=[0, RuntimeError("cleanup boom")]), \
                mock.patch.object(
                    planning_generation, "_create_occurrences",
                    side_effect=ValueError("generation boom")):
            with planning_generation.quiet_generation_scope():
                result = generate_task(7, now, context=context)
        self.assertEqual(
            result["errors"], [{"task_id": 7, "error": "ValueError"}])
        events = self.queue.drain(10000)
        # 清理事件先于生成事件（finally 先于外层 catch 记录）
        self.assertEqual(len(events), 2)
        cleanup, generation = events
        self.assertEqual(cleanup["operation"], "expire_fixed_rounds")
        self.assertEqual(cleanup["error_code"], "quiet_cleanup_failed")
        self.assertEqual(cleanup["outcome"], "committed_side_effect_failed")
        self.assertEqual(cleanup["exception_type"], "RuntimeError")
        self.assertIn("cleanup boom", cleanup["stack_trace"])
        self.assertEqual(cleanup["safe_context"], {"task_id": 7})
        self.assertEqual(generation["operation"], "generate_task")
        self.assertEqual(generation["error_code"], "quiet_generate_failed")
        self.assertEqual(generation["outcome"], "committed_side_effect_failed")
        self.assertEqual(generation["exception_type"], "ValueError")

    def test_maintenance_cleanup_failure_marks_failed(self):
        # 非 quiet 维护路径的清理失败保持 failed 语义（BUG-10 对照）。
        from gateway import planning_common
        from gateway import planning_cycles as cycles
        from gateway import planning_generation
        from gateway.planning_generation import generate_task
        from gateway import planning_runtime as runtime

        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        context = cycles.PlanningRequestContext(now)
        context._boundary = (time(3, 0), None, frozenset())
        context._values = {
            planning_common.PLANNING_BOUNDARY_STATE_KEY: None,
            planning_common.PLANNING_DAILY_REFRESH_KEY: True,
        }
        task = {"id": 8, "is_active": True, "task_type": "interval",
                "refresh_mode": "fixed_interval", "time_mode": "rule"}
        with mock.patch.object(runtime, "_require_client", return_value=object()), \
                mock.patch.object(runtime, "_fetch_task", return_value=task), \
                mock.patch.object(
                    planning_generation, "_fixed_rounds",
                    return_value=[(now.date(), now)]), \
                mock.patch.object(
                    planning_generation, "_following_fixed_event",
                    return_value=now), \
                mock.patch.object(
                    planning_generation, "_expire_fixed_rounds",
                    side_effect=[0, RuntimeError("cleanup boom")]), \
                mock.patch.object(
                    planning_generation, "_create_occurrences",
                    side_effect=ValueError("generation boom")):
            result = generate_task(8, now, context=context)
        self.assertEqual(
            result["errors"], [{"task_id": 8, "error": "ValueError"}])
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 2)
        cleanup = events[0]
        self.assertEqual(cleanup["operation"], "expire_fixed_rounds")
        self.assertEqual(cleanup["error_code"], "cleanup_failed")
        self.assertEqual(cleanup["outcome"], "failed")
        self.assertEqual(events[1]["outcome"], "failed")

    def test_quiet_generate_due_warning_keeps_exception(self):
        # BUG-18（quiet warning 捕获点）：_generate_due_quietly 外层 warning
        # 捕获传原异常——类型与堆栈可查，outcome 保持已提交后失败。
        from gateway import planning_generation as generation
        from gateway import planning_tasks

        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(
                generation, "generate_due",
                side_effect=RuntimeError("quiet due failure")):
            planning_tasks._generate_due_quietly(object(), now)
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["module"], "planning")
        self.assertEqual(event["error_code"], "quiet_generate_failed")
        self.assertEqual(event["outcome"], "committed_side_effect_failed")
        self.assertEqual(event["exception_type"], "RuntimeError")  # BUG-18
        self.assertIn("quiet due failure", event["stack_trace"])

    def test_maintenance_path_generate_due_marks_failed(self):
        # 非 quiet 作用域（维护循环）：同一内层捕获保持 failed 语义
        from gateway import planning_cycles as cycles
        from gateway import planning_generation
        from gateway.planning_generation import generate_due
        from gateway import planning_runtime as runtime

        now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        with mock.patch.object(runtime, "_require_client", return_value=object()), \
                mock.patch.object(
                    runtime, "_rows",
                    return_value=[{"id": 9, "is_active": True}]), \
                mock.patch.object(
                    cycles, "_load_boundary_state",
                    return_value=(time(3, 0), None, frozenset())), \
                mock.patch.object(
                    cycles, "get_cycle_settings",
                    return_value={"daily_refresh_enabled": True}), \
                mock.patch.object(
                    planning_generation, "_reconcile_task_rounds",
                    side_effect=RuntimeError("maintenance boom")):
            result = generate_due(now)
        self.assertEqual(result["errors"], [{"task_id": 9, "error": "RuntimeError"}])
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "failed")
        self.assertEqual(events[0]["error_code"], "generate_task_failed")


if __name__ == "__main__":
    unittest.main()
