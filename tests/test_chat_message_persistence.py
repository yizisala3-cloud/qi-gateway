"""网关自动保存聊天原文到 public.chat_messages 的行为测试。

设计取舍（有意为之并有测试固化）：
- user 与 assistant 成对保存：只有上游成功产出有效 assistant 文本时才写；
  上游失败时不保留用户输入，避免出现没有回复的孤儿 user 行。
- 主动请求的合成控制信号不保存为 user；主动产生的 assistant 回复保存。
- 历史消息绝不重复落库，每次请求最多一条 user + 一条 assistant。
- assistant_id 来自网关现有协议（MEMORY_ASSISTANT_ID 或自动发现）；
  取不到时明确跳过并记日志，不伪造身份。
- conversation_id 固定写 NULL：请求 JSON 中不存在该字段，且表结构与现有
  读取方均允许为空。
- 保存失败只记日志，绝不改变聊天响应的状态码和正文。
"""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import httpx

from gateway import db as gateway_db
from gateway import main as gateway_main

CST = timezone(timedelta(hours=8))


class SaveRecorder:
    """替换 db.save_chat_message，记录调用或在需要时抛错。"""

    def __init__(self):
        self.rows = []
        self.raise_error = None

    def __call__(self, role, content, assistant_id, conversation_id=None):
        if self.raise_error is not None:
            raise self.raise_error
        self.rows.append({
            "role": role,
            "content": content,
            "assistant_id": assistant_id,
            "conversation_id": conversation_id,
        })


class FakeRequest:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    async def json(self):
        return self._body


def upstream_completion(content):
    return json.dumps({
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
    }).encode("utf-8")


class FakePostClient:
    def __init__(self, status_code=200, payload=b"{}", exc=None):
        self.status_code = status_code
        self.payload = payload
        self.exc = exc

    async def post(self, url, headers=None, json=None, timeout=None):
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(status_code=self.status_code, content=self.payload)


class FakeStreamResponse:
    def __init__(self, status_code=200, lines=(), fail_on_line=None):
        self.status_code = status_code
        self.lines = list(lines)
        self.fail_on_line = fail_on_line

    async def aiter_lines(self):
        for line in self.lines:
            if self.fail_on_line is not None and line == self.fail_on_line:
                raise httpx.TimeoutException("simulated upstream timeout")
            yield line

    async def aread(self):
        return json.dumps({"error": "upstream failure"}).encode()


class FakeStreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeStreamClient:
    def __init__(self, response):
        self._response = response

    def stream(self, method, url, **kwargs):
        return FakeStreamContext(self._response)


def sse_delta(text):
    return "data: " + json.dumps({
        "id": "chatcmpl-1",
        "choices": [{"index": 0, "delta": {"content": text}}],
    })


SSE_LINES = [
    sse_delta("你好"),
    sse_delta("，"),
    sse_delta("我在"),
    "data: [DONE]",
]


def ordinary_messages():
    return [
        {"role": "system", "content": "人设 system prompt"},
        {"role": "user", "content": "历史用户消息"},
        {"role": "assistant", "content": "历史助手回复"},
        {"role": "tool", "content": "工具返回，不是聊天"},
        {"role": "user", "content": "当前消息"},
    ]


async def drain_background_tasks():
    while gateway_main._background_tasks:
        await asyncio.gather(*list(gateway_main._background_tasks))


def call_chat(body_dict, run_coro):
    return asyncio.run(run_coro(FakeRequest(body_dict)))


async def consume_stream(response):
    parts = []
    async for chunk in response.body_iterator:
        parts.append(chunk.decode() if isinstance(chunk, (bytes, bytearray)) else chunk)
    return "".join(parts)


class GatewayTestCase(unittest.TestCase):
    """公共 patch 环境：无鉴权、无真实上下文构建、可控制的 Supabase。"""

    def setUp(self):
        self.recorder = SaveRecorder()
        patches = [
            mock.patch.object(gateway_main.cfg, "GATEWAY_TOKEN", ""),
            mock.patch("gateway.main.build_context", lambda *a, **k: ""),
            mock.patch("gateway.main.get_proactive_todo_context", lambda: ""),
            mock.patch("gateway.main.resolve_assistant_id", lambda: "assistant-1"),
            mock.patch("gateway.db.save_chat_message", self.recorder),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(setattr, gateway_main, "http_client", None)

    def run_non_stream(self, messages, client):
        gateway_main.http_client = client

        async def scenario():
            response = await gateway_main.chat_completions(FakeRequest({
                "model": "some-model", "messages": messages, "stream": False,
            }))
            await drain_background_tasks()
            return response

        return asyncio.run(scenario())

    def run_stream(self, messages, client):
        gateway_main.http_client = client

        async def scenario():
            response = await gateway_main.chat_completions(FakeRequest({
                "model": "some-model", "messages": messages, "stream": True,
            }))
            output = await consume_stream(response)
            await drain_background_tasks()
            return output

        return asyncio.run(scenario())


class NonStreamPersistenceTests(GatewayTestCase):
    def test_saves_one_user_and_one_assistant(self):
        upstream = upstream_completion("新回复")
        response = self.run_non_stream(ordinary_messages(), FakePostClient(200, upstream))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, upstream)
        self.assertEqual(
            [(row["role"], row["content"]) for row in self.recorder.rows],
            [("user", "当前消息"), ("assistant", "新回复")],
        )
        self.assertTrue(all(row["assistant_id"] == "assistant-1" for row in self.recorder.rows))

    def test_history_system_and_tool_messages_are_not_saved(self):
        self.run_non_stream(ordinary_messages(), FakePostClient(200, upstream_completion("回复")))

        saved_roles = {row["role"] for row in self.recorder.rows}
        self.assertEqual(saved_roles, {"user", "assistant"})
        saved_contents = [row["content"] for row in self.recorder.rows]
        self.assertNotIn("历史用户消息", saved_contents)
        self.assertNotIn("历史助手回复", saved_contents)
        self.assertNotIn("工具返回，不是聊天", saved_contents)
        self.assertNotIn("人设 system prompt", saved_contents)

    def test_gateway_injected_system_context_is_not_saved(self):
        with mock.patch("gateway.main.build_context", lambda *a, **k: "注入的辅助上下文"):
            self.run_non_stream(
                ordinary_messages(), FakePostClient(200, upstream_completion("回复")),
            )

        for row in self.recorder.rows:
            self.assertNotIn("注入的辅助上下文", row["content"])

    def test_multimodal_user_saves_text_parts_only(self):
        messages = [
            {"role": "system", "content": "人设"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "看看这张图"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
                ],
            },
        ]
        self.run_non_stream(messages, FakePostClient(200, upstream_completion("好的")))

        self.assertEqual(
            [row["content"] for row in self.recorder.rows if row["role"] == "user"],
            ["看看这张图"],
        )

    def test_upstream_non_2xx_saves_nothing(self):
        response = self.run_non_stream(
            ordinary_messages(), FakePostClient(500, b'{"error":"boom"}'),
        )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.recorder.rows, [])

    def test_upstream_timeout_saves_nothing(self):
        response = self.run_non_stream(
            ordinary_messages(), FakePostClient(exc=httpx.TimeoutException("t")),
        )

        self.assertEqual(response.status_code, 504)
        self.assertEqual(self.recorder.rows, [])

    def test_malformed_or_empty_assistant_content_saves_nothing(self):
        for payload in (
            b"not json",
            json.dumps({"choices": []}).encode(),
            json.dumps({"choices": [{"message": {"content": "   "}}]}).encode(),
            json.dumps({"choices": [{"message": {"content": ""}}]}).encode(),
        ):
            with self.subTest(payload=payload[:40]):
                self.run_non_stream(ordinary_messages(), FakePostClient(200, payload))
                self.assertEqual(self.recorder.rows, [])

    def test_save_failure_does_not_change_response(self):
        self.recorder.raise_error = RuntimeError("supabase down")
        upstream = upstream_completion("新回复")
        response = self.run_non_stream(ordinary_messages(), FakePostClient(200, upstream))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, upstream)

    def test_missing_assistant_id_skips_save_and_logs(self):
        with (
            mock.patch("gateway.main.resolve_assistant_id", side_effect=RuntimeError("no id")),
            self.assertLogs("gateway", level="WARNING") as captured,
        ):
            self.run_non_stream(ordinary_messages(), FakePostClient(200, upstream_completion("回复")))

        self.assertEqual(self.recorder.rows, [])
        self.assertTrue(any("assistant_id" in line for line in captured.output))

    def test_blank_user_text_saves_assistant_only(self):
        messages = [
            {"role": "system", "content": "人设"},
            {"role": "user", "content": "   "},
        ]
        self.run_non_stream(messages, FakePostClient(200, upstream_completion("回复")))

        self.assertEqual(
            [(row["role"], row["content"]) for row in self.recorder.rows],
            [("assistant", "回复")],
        )


class StreamPersistenceTests(GatewayTestCase):
    def test_saves_one_user_and_complete_assistant(self):
        output = self.run_stream(ordinary_messages(), FakeStreamClient(FakeStreamResponse(200, SSE_LINES)))

        self.assertIn("data: [DONE]", output)
        self.assertEqual(
            [(row["role"], row["content"]) for row in self.recorder.rows],
            [("user", "当前消息"), ("assistant", "你好，我在")],
        )

    def test_forwarded_sse_protocol_is_unchanged(self):
        output = self.run_stream(ordinary_messages(), FakeStreamClient(FakeStreamResponse(200, SSE_LINES)))

        for line in SSE_LINES:
            self.assertIn(line, output)

    def test_truncated_stream_saves_nothing(self):
        truncated = SSE_LINES[:2]
        output = self.run_stream(
            ordinary_messages(),
            FakeStreamClient(FakeStreamResponse(200, truncated, fail_on_line=truncated[-1])),
        )

        self.assertIn("upstream read timeout", output)
        self.assertEqual(self.recorder.rows, [])

    def test_upstream_non_200_saves_nothing(self):
        output = self.run_stream(
            ordinary_messages(),
            FakeStreamClient(FakeStreamResponse(502, ['data: {"error":"bad gateway"}'])),
        )

        self.assertIn("upstream failure", output)
        self.assertEqual(self.recorder.rows, [])

    def test_stream_save_failure_does_not_change_events(self):
        self.recorder.raise_error = RuntimeError("supabase down")
        output = self.run_stream(ordinary_messages(), FakeStreamClient(FakeStreamResponse(200, SSE_LINES)))

        for line in SSE_LINES:
            self.assertIn(line, output)
        self.assertNotIn("supabase down", output)

    def test_exception_mid_stream_saves_nothing(self):
        lines = [sse_delta("半截")]
        output = self.run_stream(
            ordinary_messages(),
            FakeStreamClient(FakeStreamResponse(200, lines, fail_on_line=lines[0])),
        )

        self.assertEqual(self.recorder.rows, [])
        self.assertIn("error", output)

    def test_client_disconnect_saves_nothing(self):
        async def scenario():
            gateway_main.http_client = FakeStreamClient(FakeStreamResponse(200, SSE_LINES))
            response = await gateway_main.chat_completions(FakeRequest({
                "model": "m", "messages": ordinary_messages(), "stream": True,
            }))
            agen = response.body_iterator
            await agen.__anext__()  # 只取第一个事件即"断开"
            await agen.aclose()
            return self.recorder.rows

        rows = asyncio.run(scenario())
        self.assertEqual(rows, [])


class ProactivePersistenceTests(GatewayTestCase):
    def proactive_messages(self):
        return [
            {
                "role": "system",
                "content": "原始人设\n\n## 主动消息触发（定时触发）\n规则",
            },
            {"role": "user", "content": "最后一条真人消息"},
            {"role": "assistant", "content": "已回复过的内容"},
            {
                "role": "user",
                "content": "请根据以上上下文决定是否发消息。没什么好说的就回复 [PASS] 即可。",
            },
        ]

    def test_proactive_control_signal_not_saved_but_reply_saved(self):
        self.run_non_stream(
            self.proactive_messages(), FakePostClient(200, upstream_completion("[PASS]")),
        )

        self.assertEqual(
            [(row["role"], row["content"]) for row in self.recorder.rows],
            [("assistant", "[PASS]")],
        )

    def test_proactive_stream_reply_saved_without_control_signal(self):
        self.run_stream(
            self.proactive_messages(), FakeStreamClient(FakeStreamResponse(200, SSE_LINES)),
        )

        self.assertEqual(
            [(row["role"], row["content"]) for row in self.recorder.rows],
            [("assistant", "你好，我在")],
        )

    def test_proactive_history_not_repeated(self):
        self.run_non_stream(
            self.proactive_messages(), FakePostClient(200, upstream_completion("[PASS]")),
        )

        saved_contents = [row["content"] for row in self.recorder.rows]
        self.assertNotIn("最后一条真人消息", saved_contents)
        self.assertNotIn("已回复过的内容", saved_contents)
        self.assertEqual(len(self.recorder.rows), 1)


class SaveChatMessageTests(unittest.TestCase):
    """直接测试 db.save_chat_message 的写入契约。"""

    def setUp(self):
        self.payloads = []
        self.execute_calls = 0

        class FakeInsert:
            def insert(self, payload):
                self.payload = payload
                return self

            def execute(self):
                self.owner.execute_calls += 1
                if self.fail:
                    raise self.fail
                self.owner.payloads.append(self.payload)

        class FakeClient:
            def __init__(self, owner, fail=None):
                self.owner = owner
                self.builder = FakeInsert()
                self.builder.owner = owner
                self.builder.fail = fail

            def table(self, name):
                self.owner.table_name = name
                return self.builder

        self.fake_client_cls = FakeClient
        self.table_name = ""

    def test_inserts_null_conversation_id_and_cst_created_at(self):
        client = self.fake_client_cls(self)
        before = datetime.now(CST).replace(tzinfo=None)
        with mock.patch("gateway.db.get_client", lambda: client):
            ok = gateway_db.save_chat_message("user", "你好", "assistant-1")

        self.assertTrue(ok)
        self.assertEqual(self.table_name, "chat_messages")
        payload = self.payloads[0]
        self.assertEqual(payload["conversation_id"], None)
        self.assertEqual(payload["role"], "user")
        self.assertEqual(payload["content"], "你好")
        created_at = datetime.strptime(payload["created_at"], "%Y-%m-%d %H:%M:%S.%f")
        self.assertLessEqual((created_at - before).total_seconds(), 5)
        self.assertGreaterEqual((created_at - before).total_seconds(), -1)

    def test_blank_content_and_invalid_role_are_rejected(self):
        with mock.patch("gateway.db.get_client") as get_client:
            self.assertFalse(gateway_db.save_chat_message("system", "x", "a"))
            self.assertFalse(gateway_db.save_chat_message("tool", "x", "a"))
            self.assertFalse(gateway_db.save_chat_message("user", "   ", "a"))
            self.assertFalse(gateway_db.save_chat_message("assistant", "", "a"))
            self.assertFalse(gateway_db.save_chat_message("user", "x", "  "))
        get_client.assert_not_called()
        self.assertEqual(self.payloads, [])

    def test_failure_logs_required_fields_without_secrets_and_never_retries(self):
        secret = "SUPER_SECRET_KEY_VALUE"
        client = self.fake_client_cls(self, fail=RuntimeError(f"connection refused key={secret}"))
        with (
            mock.patch("gateway.db.get_client", lambda: client),
            self.assertLogs("gateway.db", level="ERROR") as captured,
        ):
            ok = gateway_db.save_chat_message("assistant", "回复", "assistant-1")

        self.assertFalse(ok)
        self.assertEqual(len(self.payloads), 0)
        self.assertEqual(self.execute_calls, 1)  # 失败后不重试，插入只调用一次
        output = "\n".join(captured.output)
        self.assertNotIn(secret, output)
        self.assertIn("save_chat_message", output)
        self.assertIn("role=assistant", output)
        self.assertIn("assistant_id=True", output)
        self.assertIn("conversation_id=False", output)
        self.assertIn("RuntimeError", output)
        self.assertIn("attempts=1", output)

    def test_missing_supabase_client_is_logged_not_raised(self):
        with (
            mock.patch("gateway.db.get_client", lambda: None),
            self.assertLogs("gateway.db", level="ERROR") as captured,
        ):
            ok = gateway_db.save_chat_message("user", "你好", "assistant-1")

        self.assertFalse(ok)
        self.assertIn("Supabase 客户端不可用", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
