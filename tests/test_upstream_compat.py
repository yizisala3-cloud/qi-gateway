"""Gemini browser-proxy tool history compatibility tests."""
import asyncio
import copy
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from gateway import main as gateway_main
from gateway.request_context import GATEWAY_CONTEXT_HEADING
from gateway.upstream_compat import normalize_gemini_browser_tool_history


def tool_call(call_id, name, arguments='{"city":"Shanghai"}'):
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def one_tool_history(content="sunny", name_marker=False):
    tool_message = {
        "role": "tool",
        "tool_call_id": "call_weather",
        "content": content,
    }
    if name_marker is not False:
        tool_message["name"] = name_marker
    return [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [tool_call("call_weather", "get_weather")],
        },
        tool_message,
    ]


class NormalizeGeminiBrowserToolHistoryTests(unittest.TestCase):
    def test_disabled_gemini_request_is_the_same_object_and_unchanged(self):
        messages = one_tool_history()
        before = copy.deepcopy(messages)

        result = normalize_gemini_browser_tool_history(messages, False, "gemini-2.5-pro")

        self.assertIs(result, messages)
        self.assertEqual(result, before)

    def test_enabled_non_gemini_request_is_the_same_object_and_unchanged(self):
        messages = one_tool_history()
        before = copy.deepcopy(messages)

        result = normalize_gemini_browser_tool_history(messages, True, "claude-sonnet-4")

        self.assertIs(result, messages)
        self.assertEqual(result, before)

    def test_single_tool_result_gets_name_from_matching_call_id(self):
        result = normalize_gemini_browser_tool_history(
            one_tool_history(), True, "gemini-2.5-pro"
        )

        self.assertEqual(result[2]["name"], "get_weather")
        self.assertEqual(result[2]["tool_call_id"], "call_weather")
        self.assertEqual(result[2]["content"], "sunny")

    def test_parallel_tool_results_keep_order_and_match_independently(self):
        messages = [
            {"role": "user", "content": "weather and time?"},
            {
                "role": "assistant",
                "tool_calls": [
                    tool_call("call_weather", "get_weather"),
                    tool_call("call_time", "get_time", '{"zone":"Asia/Shanghai"}'),
                ],
            },
            {"role": "tool", "tool_call_id": "call_time", "content": "12:00"},
            {"role": "tool", "tool_call_id": "call_weather", "content": "sunny"},
        ]

        result = normalize_gemini_browser_tool_history(messages, True, "gemini-2.5-flash")

        self.assertEqual([message["role"] for message in result], [
            "user", "assistant", "tool", "tool",
        ])
        self.assertEqual(result[2]["name"], "get_time")
        self.assertEqual(result[3]["name"], "get_weather")

    def test_consecutive_tool_rounds_use_preceding_calls(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "tool_calls": [tool_call("call_1", "first_tool")]},
            {"role": "tool", "tool_call_id": "call_1", "content": "one"},
            {"role": "assistant", "tool_calls": [tool_call("call_2", "second_tool")]},
            {"role": "tool", "tool_call_id": "call_2", "content": "two"},
            {"role": "assistant", "content": "done"},
        ]

        result = normalize_gemini_browser_tool_history(messages, True, "GEMINI-2.5-PRO")

        self.assertEqual(result[2]["name"], "first_tool")
        self.assertEqual(result[4]["name"], "second_tool")
        self.assertEqual([message["role"] for message in result], [
            "user", "assistant", "tool", "assistant", "tool", "assistant",
        ])

    def test_existing_correct_name_is_preserved_without_copying(self):
        messages = one_tool_history(name_marker="get_weather")

        result = normalize_gemini_browser_tool_history(messages, True, "gemini-2.5-pro")

        self.assertIs(result, messages)
        self.assertEqual(result[2]["name"], "get_weather")

    def test_conflicting_name_is_corrected_and_warning_contains_no_payload(self):
        messages = one_tool_history(content="SECRET_RESPONSE", name_marker="wrong_tool")
        messages[1]["tool_calls"][0]["function"]["arguments"] = "SECRET_ARGUMENTS"

        with self.assertLogs("gateway.upstream_compat", level="WARNING") as captured:
            result = normalize_gemini_browser_tool_history(
                messages, True, "gemini-2.5-pro"
            )

        output = "\n".join(captured.output)
        self.assertEqual(result[2]["name"], "get_weather")
        self.assertIn("tool_call_id=call_weather", output)
        self.assertIn("function_name=get_weather", output)
        self.assertNotIn("wrong_tool", output)
        self.assertNotIn("SECRET_RESPONSE", output)
        self.assertNotIn("SECRET_ARGUMENTS", output)

    def test_unknown_tool_call_id_is_unchanged_and_never_fabricates_name(self):
        messages = one_tool_history()
        messages[2]["tool_call_id"] = "call_unknown"
        before = copy.deepcopy(messages)

        with self.assertLogs("gateway.upstream_compat", level="WARNING") as captured:
            result = normalize_gemini_browser_tool_history(
                messages, True, "gemini-2.5-pro"
            )

        self.assertIs(result, messages)
        self.assertEqual(result, before)
        self.assertNotIn("name", result[2])
        self.assertNotIn("unknown_function", "\n".join(captured.output))

    def test_string_json_string_and_array_content_are_preserved_exactly(self):
        contents = (
            "plain text",
            '{"temperature":24}',
            [{"type": "text", "text": "array content"}],
        )
        for content in contents:
            with self.subTest(content=content):
                result = normalize_gemini_browser_tool_history(
                    one_tool_history(copy.deepcopy(content)), True, "gemini-2.0-flash"
                )
                self.assertEqual(result[2]["content"], content)

    def test_original_messages_and_nested_values_are_not_mutated(self):
        messages = one_tool_history([{"type": "text", "text": "result"}])
        before = copy.deepcopy(messages)

        result = normalize_gemini_browser_tool_history(messages, True, "gemini-2.5-pro")

        self.assertEqual(messages, before)
        self.assertIsNot(result, messages)
        self.assertIsNot(result[2], messages[2])
        self.assertIs(result[1], messages[1])
        self.assertIs(result[2]["content"], messages[2]["content"])


class FakeRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


class RecordingPostClient:
    def __init__(self, status_code=200, content=None):
        self.sent_body = None
        self.status_code = status_code
        self.content = content if content is not None else json.dumps({
            "choices": [{"message": {"role": "assistant", "content": ""}}],
        }).encode()

    async def post(self, _url, *, headers, json, timeout):
        self.sent_body = copy.deepcopy(json)
        return SimpleNamespace(status_code=self.status_code, content=self.content)


class RecordingStreamResponse:
    status_code = 200

    async def aiter_lines(self):
        yield "data: [DONE]"

    async def aread(self):
        return b""


class RecordingStreamContext:
    async def __aenter__(self):
        return RecordingStreamResponse()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class RecordingStreamClient:
    def __init__(self):
        self.sent_body = None

    def stream(self, _method, _url, *, headers, json, timeout):
        self.sent_body = copy.deepcopy(json)
        return RecordingStreamContext()


async def consume_stream(response):
    return "".join([
        chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk
        async for chunk in response.body_iterator
    ])


class ChatCompletionCompatibilityTests(unittest.TestCase):
    def _patch_chat(self, client, *, enabled=True, configured_model="gemini-2.5-pro", context=""):
        return (
            mock.patch.object(gateway_main, "http_client", client),
            mock.patch.object(gateway_main, "verify_token", return_value=True),
            mock.patch.object(gateway_main, "build_context", return_value=context),
            mock.patch.object(
                gateway_main.cfg, "GEMINI_BROWSER_TOOL_COMPAT_ENABLED", enabled
            ),
            mock.patch.object(gateway_main.cfg, "UPSTREAM_MODEL", configured_model),
        )

    def test_context_position_is_unchanged_and_non_stream_sends_normalized_body(self):
        client = RecordingPostClient()
        history = [
            {"role": "system", "content": "original system"},
            *one_tool_history(),
            {"role": "user", "content": "continue"},
        ]
        body = {
            "model": "client-model",
            "messages": history,
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
            "stream": False,
        }
        before = copy.deepcopy(body)

        patches = self._patch_chat(client, context="memory context")
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = asyncio.run(gateway_main.chat_completions(FakeRequest(body)))

        self.assertEqual(response.status_code, 200)
        sent = client.sent_body
        self.assertEqual(sent["model"], "gemini-2.5-pro")
        self.assertEqual([message["role"] for message in sent["messages"]], [
            "system", "system", "user", "assistant", "tool", "user",
        ])
        self.assertIn(GATEWAY_CONTEXT_HEADING, sent["messages"][1]["content"])
        self.assertEqual(sent["messages"][4]["name"], "get_weather")
        self.assertEqual(sent["messages"][4]["tool_call_id"], "call_weather")
        self.assertEqual(sent["tools"], before["tools"])
        self.assertEqual(history, before["messages"])

    def test_stream_sends_the_same_normalized_request_shape(self):
        client = RecordingStreamClient()
        body = {
            "model": "gemini-2.5-pro",
            "messages": one_tool_history(),
            "stream": True,
        }
        patches = self._patch_chat(client, configured_model="")

        async def scenario():
            with patches[0], patches[1], patches[2], patches[3], patches[4]:
                response = await gateway_main.chat_completions(FakeRequest(body))
                return await consume_stream(response)

        output = asyncio.run(scenario())

        self.assertIn("data: [DONE]", output)
        self.assertEqual(client.sent_body["messages"][2]["name"], "get_weather")

    def test_non_gemini_request_body_keeps_existing_forwarding_behavior(self):
        client = RecordingPostClient()
        body = {
            "model": "qwen-max",
            "messages": one_tool_history(),
            "tools": [{"type": "function", "function": {"name": "get_weather"}}],
            "temperature": 0.25,
            "stream": False,
        }
        expected = copy.deepcopy(body)
        patches = self._patch_chat(client, configured_model="")

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            asyncio.run(gateway_main.chat_completions(FakeRequest(body)))

        self.assertEqual(client.sent_body, expected)
        self.assertNotIn("name", client.sent_body["messages"][2])

    def test_configured_non_gemini_model_controls_compatibility_decision(self):
        client = RecordingPostClient()
        body = {
            "model": "gemini-2.5-pro",
            "messages": one_tool_history(),
            "stream": False,
        }
        patches = self._patch_chat(client, configured_model="deepseek-chat")

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            asyncio.run(gateway_main.chat_completions(FakeRequest(body)))

        self.assertEqual(client.sent_body["model"], "deepseek-chat")
        self.assertNotIn("name", client.sent_body["messages"][2])

    def test_configured_gemini_model_controls_compatibility_decision(self):
        client = RecordingPostClient()
        body = {
            "model": "qwen-max",
            "messages": one_tool_history(),
            "stream": False,
        }
        patches = self._patch_chat(client, configured_model="gemini-2.5-flash")

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            asyncio.run(gateway_main.chat_completions(FakeRequest(body)))

        self.assertEqual(client.sent_body["model"], "gemini-2.5-flash")
        self.assertEqual(client.sent_body["messages"][2]["name"], "get_weather")

    def test_upstream_error_status_and_body_are_still_forwarded_verbatim(self):
        error_body = b'{"error":{"code":400,"message":"INVALID_ARGUMENT"}}'
        client = RecordingPostClient(status_code=400, content=error_body)
        body = {
            "model": "gemini-2.5-pro",
            "messages": one_tool_history(),
            "stream": False,
        }
        patches = self._patch_chat(client)

        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            response = asyncio.run(gateway_main.chat_completions(FakeRequest(body)))

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.body, error_body)
        self.assertEqual(client.sent_body["messages"][2]["name"], "get_weather")


if __name__ == "__main__":
    unittest.main()
