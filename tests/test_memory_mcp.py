import asyncio
import json
import unittest
from unittest.mock import patch

from gateway.config import cfg
from gateway.memory_mcp import (
    MCPBearerAuth,
    memory_mcp,
    propose_interaction_rule,
    remember_moment,
)


TYPED_TOOLS = {
    "remember_moment", "remember_thread", "remember_inside_joke",
    "propose_episode", "propose_profile", "propose_interaction_rule",
}
ALL_TOOLS = TYPED_TOOLS | {"review_memory_requests"}


class MCPToolContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = {tool.name: tool for tool in asyncio.run(memory_mcp.list_tools())}

    def test_sdk_exposes_exactly_seven_typed_tools(self):
        self.assertEqual(set(self.tools), ALL_TOOLS)
        self.assertNotIn("request_memory", self.tools)

    def test_typed_tools_expose_flat_fields_without_generic_payload(self):
        for name in TYPED_TOOLS:
            schema = self.tools[name].input_schema
            properties = schema["properties"]
            with self.subTest(tool=name):
                for forbidden in ("continuity_data", "continuity_type", "assistant_id", "proposed_relations"):
                    self.assertNotIn(forbidden, properties)
        self.assertIn("scene", self.tools["remember_moment"].input_schema["properties"])
        self.assertIn("event", self.tools["remember_moment"].input_schema["properties"])
        self.assertEqual(
            self.tools["remember_moment"].input_schema["properties"]["moment_state"]["enum"],
            ["standalone", "linked", "absorbed"],
        )
        self.assertIn("open_question", self.tools["remember_thread"].input_schema["properties"])
        self.assertIn("current_state", self.tools["remember_thread"].input_schema["properties"])
        self.assertIn("closure_summary", self.tools["remember_thread"].input_schema["properties"])
        self.assertEqual(
            self.tools["remember_thread"].input_schema["properties"]["thread_state"]["enum"],
            ["open", "paused", "resolved", "dissolved", "abandoned", "unknown"],
        )
        self.assertIn("origin", self.tools["remember_inside_joke"].input_schema["properties"])
        self.assertIn("trigger_phrases", self.tools["remember_inside_joke"].input_schema["properties"])
        self.assertIn("shared_meaning", self.tools["remember_inside_joke"].input_schema["properties"])
        self.assertIn("reinforcement_count", self.tools["remember_inside_joke"].input_schema["properties"])
        self.assertIn("beginning", self.tools["propose_episode"].input_schema["properties"])
        self.assertIn("development", self.tools["propose_episode"].input_schema["properties"])
        self.assertIn("outcome", self.tools["propose_episode"].input_schema["properties"])
        self.assertEqual(
            self.tools["propose_episode"].input_schema["properties"]["closure_quality"]["enum"],
            ["complete", "partial", "uncertain"],
        )
        profile_properties = self.tools["propose_profile"].input_schema["properties"]
        for field in ("facet", "statement", "scope", "stability", "basis"):
            self.assertIn(field, profile_properties)
        self.assertEqual(
            profile_properties["stability"]["enum"], ["stable", "contextual", "provisional"]
        )
        self.assertEqual(
            profile_properties["basis"]["enum"],
            ["explicit_self_report", "explicit_preference", "repeated_observation", "reviewed_summary"],
        )
        rule_properties = self.tools["propose_interaction_rule"].input_schema["properties"]
        for field in ("memory_key", "trigger", "expected_behavior", "scope", "priority", "rule_state", "explicit_instruction"):
            self.assertIn(field, rule_properties)
        self.assertEqual(
            rule_properties["rule_state"]["enum"], ["active", "revoked", "superseded"]
        )
        self.assertIn("memory_key", self.tools["propose_interaction_rule"].input_schema["required"])
        self.assertNotIn("update_mode", rule_properties)

    def test_tool_descriptions_explain_usage_and_review_policy(self):
        for name in TYPED_TOOLS:
            description = self.tools[name].description
            with self.subTest(tool=name):
                self.assertIn("何时调用", description)
                self.assertIn("content", description)
                self.assertIn("reason", description)
                self.assertIn("update_mode", description)
        for name in ("remember_moment", "remember_thread", "remember_inside_joke"):
            self.assertIn("直接写入正式记忆", self.tools[name].description)
        for name in ("propose_episode", "propose_profile", "propose_interaction_rule"):
            self.assertIn("pending", self.tools[name].description)
            self.assertIn("叶子审核", self.tools[name].description)
        rule_description = self.tools["propose_interaction_rule"].description
        self.assertIn("replace", rule_description)
        self.assertIn("explicit_instruction", rule_description)
        review_description = self.tools["review_memory_requests"].description
        for action in ("list", "approve", "reject", "merge", "duplicate", "conflict"):
            self.assertIn(f"【{action}】", review_description)
        for kind in ("moment", "thread", "inside_joke"):
            self.assertIn(kind, review_description)
        for kind in ("episode", "profile", "interaction_rule"):
            self.assertIn(kind, review_description)
        self.assertIn("不能审核", review_description)
        self.assertIn("memory_relations", review_description)

    def test_all_write_tools_expose_recall_scene_and_tags(self):
        for name in TYPED_TOOLS:
            properties = self.tools[name].input_schema["properties"]
            with self.subTest(tool=name):
                self.assertIn("recall_scene", properties)
                self.assertIn("recall_tags", properties)
                # 召回场景不设业务长度/数量限制。
                self.assertNotIn("maxLength", properties["recall_scene"])
                self.assertNotIn("maxItems", properties["recall_tags"])

    def test_tool_descriptions_explain_recall_scene_semantics(self):
        description = self.tools["remember_moment"].description
        self.assertIn("【recall_scene】", description)
        self.assertIn("不是记忆正文", description)
        self.assertIn("【recall_tags】", description)
        self.assertIn("不要编造", description)

    def test_mcp_request_injects_server_assistant_and_source(self):
        captured = {}

        def fake_create(payload, idempotency_key, **kwargs):
            captured.update({"payload": payload, "idempotency_key": idempotency_key, **kwargs})
            return {"status": "approved", "memory_id": 7, "request_id": 7}

        with (
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch("gateway.memory_mcp.create_memory_request", side_effect=fake_create),
        ):
            result = asyncio.run(remember_moment(
                content="叶子希望记住这次确认。",
                reason="以后继续这个话题时有用。",
                scene="聊天",
                event="确认",
                moment_state="standalone",
            ))
        self.assertEqual(result["request_id"], 7)
        self.assertNotIn("assistant_id", captured["payload"])
        self.assertEqual(captured["payload"]["continuity_type"], "moment")
        self.assertEqual(
            captured["payload"]["continuity_data"],
            {"scene": "聊天", "event": "确认", "moment_state": "standalone"},
        )
        self.assertEqual(captured["assistant_id"], "server-assistant")
        self.assertEqual(captured["source"], "mcp_memory")

    def test_optional_none_fields_are_dropped_from_continuity_data(self):
        captured = {}

        def fake_create(payload, idempotency_key, **kwargs):
            captured.update({"payload": payload, **kwargs})
            return {"status": "pending", "request_id": 9}

        with (
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch("gateway.memory_mcp.create_memory_request", side_effect=fake_create),
        ):
            asyncio.run(remember_moment(
                content="一条没有可选字段的片段。",
                reason="验证可选字段省略。",
                scene="场景",
                event="事件",
                moment_state="linked",
            ))
        self.assertEqual(
            captured["payload"]["continuity_data"],
            {"scene": "场景", "event": "事件", "moment_state": "linked"},
        )
        self.assertEqual(captured["payload"]["participants"], ["yezi", "qi"])
        self.assertEqual(captured["payload"]["continuity_value"], 5)

    def test_recall_fields_pass_through_to_the_request_payload(self):
        captured = {}

        def fake_create(payload, idempotency_key, **kwargs):
            captured.update({"payload": payload, **kwargs})
            return {"status": "approved", "memory_id": 12, "request_id": 12}

        with (
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch("gateway.memory_mcp.create_memory_request", side_effect=fake_create),
        ):
            asyncio.run(remember_moment(
                content="叶子希望记住这次确认。",
                reason="以后继续这个话题时有用。",
                scene="聊天",
                event="确认",
                moment_state="standalone",
                recall_scene="当叶子再提起这次约定时",
                recall_tags=["约定", "网关"],
            ))
        self.assertEqual(captured["payload"]["recall_scene"], "当叶子再提起这次约定时")
        self.assertEqual(captured["payload"]["recall_tags"], ["约定", "网关"])

        captured.clear()
        with (
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch("gateway.memory_mcp.create_memory_request", side_effect=fake_create),
        ):
            asyncio.run(remember_moment(
                content="一条没有召回场景的片段。",
                reason="验证召回字段可省略。",
                scene="场景",
                event="事件",
                moment_state="standalone",
            ))
        self.assertIsNone(captured["payload"]["recall_scene"])
        self.assertEqual(captured["payload"]["recall_tags"], [])

    def test_interaction_rule_fixes_replace_and_requires_memory_key(self):
        captured = {}

        def fake_create(payload, idempotency_key, **kwargs):
            captured.update({"payload": payload, **kwargs})
            return {"status": "pending", "request_id": 11}

        with (
            patch.object(cfg, "MEMORY_ASSISTANT_ID", "server-assistant"),
            patch("gateway.memory_mcp.create_memory_request", side_effect=fake_create),
        ):
            asyncio.run(propose_interaction_rule(
                content="叶子要求深夜聊天时先确认状态。",
                reason="长期互动规则，影响回应方式。",
                memory_key="rule.night-checkin",
                trigger="深夜对话开始",
                expected_behavior="先询问叶子今天状态",
                scope="深夜聊天",
                priority=6,
                rule_state="active",
                explicit_instruction="叶子原话：晚上先问我今天怎么样。",
            ))
        self.assertEqual(captured["payload"]["continuity_type"], "interaction_rule")
        self.assertEqual(captured["payload"]["update_mode"], "replace")
        self.assertEqual(captured["payload"]["memory_key"], "rule.night-checkin")
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
