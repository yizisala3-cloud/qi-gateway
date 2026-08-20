import asyncio
import json
import unittest
from unittest.mock import patch

from gateway.config import cfg
from gateway.memory_mcp import MCPBearerAuth, memory_mcp, request_memory


class MCPToolContractTests(unittest.TestCase):
    def test_sdk_exposes_exactly_two_tools_with_object_continuity_data(self):
        tools = {tool.name: tool for tool in asyncio.run(memory_mcp.list_tools())}
        self.assertEqual(set(tools), {"request_memory", "review_memory_requests"})
        schema = tools["request_memory"].input_schema
        self.assertEqual(schema["properties"]["continuity_data"]["type"], "object")
        self.assertNotIn("assistant_id", schema["properties"])
        self.assertNotIn("proposed_relations", schema["properties"])

    def test_mcp_request_injects_server_assistant_and_source(self):
        captured = {}

        def fake_create(payload, idempotency_key, **kwargs):
            captured.update({"payload": payload, "idempotency_key": idempotency_key, **kwargs})
            return {"status": "pending", "request_id": 7}

        with (
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch("gateway.memory_mcp.create_memory_request", side_effect=fake_create),
        ):
            result = asyncio.run(request_memory(
                content="叶子希望记住这次确认。",
                reason="以后继续这个话题时有用。",
                continuity_type="moment",
                continuity_data={"scene": "聊天", "event": "确认", "moment_state": "standalone"},
            ))
        self.assertEqual(result["request_id"], 7)
        self.assertNotIn("assistant_id", captured["payload"])
        self.assertEqual(captured["assistant_id"], "server-assistant")
        self.assertEqual(captured["source"], "mcp_memory")


class MCPBearerAuthTests(unittest.TestCase):
    @staticmethod
    def _invoke(token: str | None, configured: str):
        called = []
        messages = []

        async def inner(_scope, _receive, send):
            called.append(True)
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        headers = [] if token is None else [(b"authorization", f"Bearer {token}".encode())]
        scope = {"type": "http", "headers": headers}

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        with patch.object(cfg, "MCP_MEMORY_TOKEN", configured):
            asyncio.run(MCPBearerAuth(inner)(scope, receive, send))
        return called, messages

    def test_missing_configuration_returns_503_without_calling_sdk(self):
        called, messages = self._invoke("anything", "")
        self.assertFalse(called)
        self.assertEqual(messages[0]["status"], 503)
        self.assertEqual(json.loads(messages[1]["body"]), {"error": "mcp_not_configured"})

    def test_wrong_token_returns_401_and_correct_token_passes(self):
        called, messages = self._invoke("wrong", "right")
        self.assertFalse(called)
        self.assertEqual(messages[0]["status"], 401)
        called, messages = self._invoke("right", "right")
        self.assertTrue(called)
        self.assertEqual(messages[0]["status"], 204)


if __name__ == "__main__":
    unittest.main()
