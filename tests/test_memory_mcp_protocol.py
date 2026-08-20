"""End-to-end MCP JSON-RPC checks through the real ASGI transport."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from itertools import count
import unittest
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from gateway.config import cfg
import gateway.memory_mcp as memory_mcp_module
from gateway.memory_requests import MemoryRequestError


AUTO_TYPES = {"moment", "thread", "inside_joke"}
PENDING_TYPES = {"episode", "profile", "interaction_rule"}

EXPECTED_TOOLS = [
    "remember_moment",
    "remember_thread",
    "remember_inside_joke",
    "propose_episode",
    "propose_profile",
    "propose_interaction_rule",
    "review_memory_requests",
]


def _tool_call(kind: str, content: str, reason: str) -> tuple[str, dict]:
    """Map a continuity type to its typed tool name and flat arguments."""
    values = {
        "moment": ("remember_moment", {
            "scene": "聊天窗口", "event": "确认计划", "moment_state": "standalone",
        }),
        "thread": ("remember_thread", {
            "thread_state": "open",
            "open_question": "下一步是什么",
            "current_state": "等待继续",
            "closure_criteria": [],
        }),
        "inside_joke": ("remember_inside_joke", {
            "origin": "一次口误",
            "trigger_phrases": ["小橘子"],
            "shared_meaning": "共同玩笑",
        }),
        "episode": ("propose_episode", {
            "beginning": "开始讨论", "development": "比较方案",
            "outcome": "确认方案", "closure_quality": "complete",
        }),
        "profile": ("propose_profile", {
            "facet": "偏好", "statement": "喜欢清晨", "scope": "日常",
            "stability": "stable", "basis": "explicit_preference",
        }),
        "interaction_rule": ("propose_interaction_rule", {
            "memory_key": "rule.reply-order",
            "trigger": "需要建议",
            "expected_behavior": "先给结论",
            "scope": "对话",
            "priority": 8,
            "rule_state": "active",
            "explicit_instruction": "叶子明确要求先给结论",
        }),
    }
    tool, typed = values[kind]
    arguments = {"content": content, "reason": reason, **typed}
    # The SDK currently ignores unknown arguments; this proves the trusted
    # server value still wins.
    arguments["assistant_id"] = "client-cannot-override"
    arguments["continuity_type"] = "client-cannot-override"
    arguments["continuity_data"] = {"injected": "client-cannot-override"}
    return tool, arguments


class MCPProtocolTests(unittest.TestCase):
    def test_real_streamable_http_tools_call_contract(self):
        calls: list[dict] = []

        def fake_create(payload, _idempotency_key, **trusted):
            if payload["content"] == "触发业务失败":
                raise MemoryRequestError("request_store_failed", "database unavailable", 503)
            kind = payload["continuity_type"]
            calls.append({"payload": payload, **trusted})
            automatic = kind in AUTO_TYPES
            assigned_id = (
                1000 + int(payload["content"].rsplit(" ", 1)[1])
                if payload["content"].startswith("并发状态隔离内容 ")
                else len(calls)
            )
            return {
                "request_id": assigned_id,
                "memory_id": 100 + assigned_id if automatic else None,
                "status": "approved" if automatic else "pending",
                "created": True,
                "updated": False,
                "deduplicated": False,
                "requires_user_review": not automatic,
            }

        def fake_review(_assistant_id, request_id, _payload):
            if request_id in {20, 21, 22}:
                raise MemoryRequestError(
                    "request_not_reviewable",
                    "memory application requires user review",
                    404,
                )
            return {"request_id": request_id, "status": "approved", "changed": True}

        @asynccontextmanager
        async def lifespan(_app):
            async with memory_mcp_module.memory_mcp.session_manager.run():
                yield

        app = Starlette(
            routes=[Mount("/", app=memory_mcp_module.memory_mcp_http_app)],
            lifespan=lifespan,
        )
        good_headers = {
            "Authorization": "Bearer protocol-secret",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }

        request_ids = count(1)

        def rpc(client, method, params, headers=good_headers):
            request_id = next(request_ids)
            response = client.post(
                "/mcp",
                headers=headers,
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                follow_redirects=False,
            )
            self.assertEqual(response.history, [])
            return response

        with (
            patch.object(cfg, "MCP_MEMORY_TOKEN", "protocol-secret"),
            patch.object(cfg, "MEMORY_PLUGIN_TOKEN", "legacy-secret"),
            patch.object(cfg, "GATEWAY_TOKEN", "gateway-secret"),
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch.object(memory_mcp_module, "create_memory_request", side_effect=fake_create),
            patch.object(
                memory_mcp_module,
                "list_reviewable_memory_requests",
                return_value=[{"id": 10, "continuity_type": "moment", "status": "pending"}],
            ),
            patch.object(memory_mcp_module, "review_ai_memory_request", side_effect=fake_review),
        ):
            with TestClient(app, base_url="https://gateway.example") as client:
                missing = client.post("/mcp", json={})
                self.assertEqual(missing.status_code, 401)
                wrong = client.post(
                    "/mcp",
                    headers={"Authorization": "Bearer legacy-secret"},
                    json={},
                )
                self.assertEqual(wrong.status_code, 401)
                self.assertNotIn("protocol-secret", missing.text + wrong.text)

                initialized = rpc(client, "initialize", {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "protocol-test", "version": "1"},
                })
                self.assertEqual(initialized.status_code, 200)
                self.assertEqual(initialized.url.path, "/mcp")
                listed = rpc(client, "tools/list", {})
                self.assertEqual(
                    [tool["name"] for tool in listed.json()["result"]["tools"]],
                    EXPECTED_TOOLS,
                )

                for kind in (*sorted(AUTO_TYPES), *sorted(PENDING_TYPES)):
                    tool, arguments = _tool_call(
                        kind, f"用于验证 {kind} 的记忆内容", "验证真实 MCP tools/call 分流"
                    )
                    response = rpc(client, "tools/call", {
                        "name": tool,
                        "arguments": arguments,
                    })
                    result = response.json()["result"]
                    self.assertFalse(result["isError"], kind)
                    structured = result["structuredContent"]
                    self.assertEqual(structured["status"], "approved" if kind in AUTO_TYPES else "pending")
                    self.assertIsInstance(calls[-1]["payload"]["continuity_data"], dict)
                    self.assertEqual(calls[-1]["payload"]["continuity_type"], kind)
                    self.assertNotIn("assistant_id", calls[-1]["payload"])
                    self.assertEqual(calls[-1]["assistant_id"], "server-assistant")
                    self.assertEqual(calls[-1]["source"], "mcp_memory")
                    # Client-injected generic payload fields must be ignored.
                    self.assertNotIn("injected", calls[-1]["payload"]["continuity_data"])
                    if kind == "interaction_rule":
                        self.assertEqual(calls[-1]["payload"]["update_mode"], "replace")
                        self.assertEqual(calls[-1]["payload"]["memory_key"], "rule.reply-order")

                review_list = rpc(client, "tools/call", {
                    "name": "review_memory_requests", "arguments": {"action": "list"},
                }).json()["result"]
                self.assertFalse(review_list["isError"])
                self.assertEqual(review_list["structuredContent"]["requests"][0]["id"], 10)

                allowed_review = rpc(client, "tools/call", {
                    "name": "review_memory_requests",
                    "arguments": {"action": "approve", "request_id": 10},
                }).json()["result"]
                self.assertFalse(allowed_review["isError"])

                for high_id in (20, 21, 22):
                    denied = rpc(client, "tools/call", {
                        "name": "review_memory_requests",
                        "arguments": {"action": "approve", "request_id": high_id},
                    }).json()["result"]
                    self.assertTrue(denied["isError"])
                    self.assertIn("request_not_reviewable", denied["content"][0]["text"])

                invalid = rpc(client, "tools/call", {
                    "name": "remember_moment",
                    "arguments": {"content": "缺少必填字段的片段。", "reason": "invalid"},
                }).json()["result"]
                self.assertTrue(invalid["isError"])

                legacy = rpc(client, "tools/call", {
                    "name": "request_memory",
                    "arguments": {
                        "content": "泛型工具不应再存在。",
                        "reason": "验证泛型工具已移除",
                        "continuity_type": "moment",
                        "continuity_data": {"scene": "s", "event": "e", "moment_state": "standalone"},
                    },
                }).json()["result"]
                self.assertTrue(legacy["isError"])

                unknown = rpc(client, "tools/call", {
                    "name": "unknown_memory_tool", "arguments": {},
                }).json()["result"]
                self.assertTrue(unknown["isError"])

                failed_tool, failed_arguments = _tool_call(
                    "moment", "触发业务失败", "验证失败不伪装成功"
                )
                failed = rpc(client, "tools/call", {
                    "name": failed_tool,
                    "arguments": failed_arguments,
                }).json()["result"]
                self.assertTrue(failed["isError"])
                self.assertNotIn("protocol-secret", failed["content"][0]["text"])

                def concurrent_call(index):
                    tool, arguments = _tool_call(
                        "moment", f"并发状态隔离内容 {index}", "验证 stateless 请求状态隔离"
                    )
                    response = rpc(client, "tools/call", {
                        "name": tool,
                        "arguments": arguments,
                    })
                    return response.json()["result"]["structuredContent"]["request_id"]

                with ThreadPoolExecutor(max_workers=4) as executor:
                    concurrent_ids = list(executor.map(concurrent_call, range(4)))
                self.assertEqual(len(set(concurrent_ids)), 4)

                with patch.object(cfg, "MCP_MEMORY_TOKEN", ""):
                    unavailable = client.post("/mcp", json={})
                self.assertEqual(unavailable.status_code, 503)
                self.assertNotIn("protocol-secret", unavailable.text)


if __name__ == "__main__":
    unittest.main()
