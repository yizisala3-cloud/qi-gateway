import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Keep the retirement tests runnable in the same bare-Python environment as the
# existing unit suite. Production installs these dependencies from requirements.
if importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

if importlib.util.find_spec("httpx") is None:
    httpx = types.ModuleType("httpx")
    httpx.AsyncClient = object
    httpx.Client = object
    httpx.Response = object
    httpx.Timeout = lambda *args, **kwargs: None
    httpx.Limits = lambda *args, **kwargs: None
    httpx.TimeoutException = TimeoutError
    sys.modules["httpx"] = httpx

if importlib.util.find_spec("starlette") is None:
    starlette = types.ModuleType("starlette")
    applications = types.ModuleType("starlette.applications")
    requests = types.ModuleType("starlette.requests")
    responses = types.ModuleType("starlette.responses")
    routing = types.ModuleType("starlette.routing")
    staticfiles = types.ModuleType("starlette.staticfiles")

    class Response:
        def __init__(self, content=b"", status_code=200, media_type=None, headers=None):
            self.body = content.encode() if isinstance(content, str) else content
            self.status_code = status_code
            self.media_type = media_type
            self.headers = headers or {}

    class JSONResponse(Response):
        def __init__(self, content, status_code=200, headers=None):
            super().__init__(json.dumps(content).encode(), status_code, "application/json", headers)

    class StreamingResponse(Response):
        def __init__(self, content, media_type=None, headers=None, status_code=200):
            super().__init__(b"", status_code, media_type, headers)
            self.body_iterator = content

    class Route:
        def __init__(self, path, endpoint, methods=None, **kwargs):
            self.path = path
            self.endpoint = endpoint
            self.methods = methods or []

    class Mount(Route):
        def __init__(self, path, app=None, name=None, **kwargs):
            super().__init__(path, app, **kwargs)
            self.app = app
            self.name = name

    class StaticFiles:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class Starlette:
        def __init__(self, routes=None, lifespan=None, **kwargs):
            self.routes = routes or []
            self.lifespan = lifespan

    applications.Starlette = Starlette
    requests.Request = object
    responses.JSONResponse = JSONResponse
    responses.Response = Response
    responses.StreamingResponse = StreamingResponse
    routing.Route = Route
    routing.Mount = Mount
    staticfiles.StaticFiles = StaticFiles
    sys.modules.update({
        "starlette": starlette,
        "starlette.applications": applications,
        "starlette.requests": requests,
        "starlette.responses": responses,
        "starlette.routing": routing,
        "starlette.staticfiles": staticfiles,
    })

from gateway import context, db, main


ROOT = Path(__file__).resolve().parents[1]


class _Request:
    def __init__(self, payload):
        self.headers = {}
        self._payload = payload

    async def json(self):
        return self._payload


class _NonStreamingClient:
    def __init__(self, content):
        self.content = content
        self.last_body = None

    async def post(self, _url, *, headers, json, timeout):
        self.last_body = json
        payload = {"choices": [{"message": {"role": "assistant", "content": self.content}}]}
        return types.SimpleNamespace(
            status_code=200,
            content=__import__("json").dumps(payload).encode(),
        )


class _StreamResult:
    status_code = 200

    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"literal <<delay:5>>"}}]}'
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _StreamContext:
    async def __aenter__(self):
        return _StreamResult()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _StreamingClient:
    def stream(self, _method, _url, *, headers, json, timeout):
        return _StreamContext()


class RuntimeRetirementTests(unittest.TestCase):
    def test_context_has_no_jiwen_or_timer_instructions(self):
        with (
            patch("gateway.context.load_persona", return_value="PERSONA"),
            patch("gateway.context.build_eventide_context", return_value="EVENTIDE"),
            patch("gateway.context.build_recent_chat_context", return_value="RECENT"),
            patch("gateway.context.search_memories", new=AsyncMock(return_value=[])),
        ):
            rendered = context.build_context("hello")

        self.assertIn("PERSONA", rendered)
        self.assertIn("EVENTIDE", rendered)
        self.assertIn("RECENT", rendered)
        self.assertNotIn("当前情绪状态", rendered)
        self.assertNotIn("主动消息标签", rendered)
        self.assertNotIn("<<delay", rendered)
        self.assertNotIn("<<schedule", rendered)
        self.assertNotIn("<<busy", rendered)

    def test_eventide_omits_untrusted_counterpart_time(self):
        state = {"version": 1}
        with (
            patch.object(context.db, "load_eventide_state", return_value=state),
            patch.object(context.db, "save_eventide_state") as save_state,
            patch.object(
                context.eventide_bridge,
                "advance_and_render",
                return_value=({"version": 2}, "CARD"),
            ) as advance,
        ):
            self.assertEqual(context.build_eventide_context(), "CARD")

        advance.assert_called_once_with(state)
        save_state.assert_called_once_with({"version": 2})

    def test_non_streaming_chat_preserves_literal_legacy_tag(self):
        client = _NonStreamingClient("reply <<busy:30>>")
        request = _Request({
            "messages": [{"role": "user", "content": "hello"}],
            "stream": False,
        })
        with (
            patch.object(main, "verify_token", return_value=True),
            patch.object(main, "build_context", return_value=""),
            patch.object(main, "http_client", client),
        ):
            response = asyncio.run(main.chat_completions(request))

        payload = json.loads(response.body)
        self.assertEqual(payload["choices"][0]["message"]["content"], "reply <<busy:30>>")
        self.assertEqual(response.status_code, 200)

    def test_streaming_chat_forwards_literal_legacy_tag(self):
        request = _Request({
            "messages": [{"role": "user", "content": "hello"}],
            "stream": True,
        })

        async def collect():
            with (
                patch.object(main, "verify_token", return_value=True),
                patch.object(main, "build_context", return_value=""),
                patch.object(main, "http_client", _StreamingClient()),
            ):
                response = await main.chat_completions(request)
                return "".join([chunk async for chunk in response.body_iterator])

        body = asyncio.run(collect())
        self.assertIn("literal <<delay:5>>", body)

    def test_client_proactive_layer_is_retired(self):
        # 主动消息兼容层已删除：所有请求一律走普通聊天路径。
        source = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("is_orangechat_proactive_request", source)
        self.assertNotIn("annotate_proactive_control_signal", source)
        self.assertNotIn("get_proactive_todo_context", source)
        todos_source = (ROOT / "gateway" / "todos.py").read_text(encoding="utf-8")
        self.assertNotIn("get_proactive_todo_context", todos_source)
        self.assertFalse((ROOT / "orangechat_plugins").exists())

    def test_old_proactive_route_and_background_loops_are_absent(self):
        paths = {route.path for route in main._routes if hasattr(route, "path")}
        self.assertNotIn("/v1/proactive", paths)

        source = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("scheduler_loop", source)
        self.assertNotIn("timer_check_loop", source)
        self.assertNotIn("scheduler_running", source)
        self.assertNotIn("timer_running", source)
        self.assertEqual(source.count("track_task(daily_task_loop())"), 1)
        self.assertIn("run_continuity_digest_if_due", source)
        self.assertIn("run_heat_decay", source)

    def test_retired_runtime_files_and_database_references_are_absent(self):
        for relative in (
            "gateway/jiwen_engine.py",
            "gateway/analysis.py",
            "gateway/timer.py",
            "gateway/proactive.py",
        ):
            self.assertFalse((ROOT / relative).exists(), relative)

        runtime = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "gateway").glob("*.py")
        )
        for retired in ("jiwen_state", "proactive_messages", "busy_inbox"):
            self.assertNotIn(retired, runtime)
        self.assertNotIn('table("timers")', runtime)

    def test_health_exposes_only_retained_memory_scheduler(self):
        response = asyncio.run(main.health(None))
        payload = json.loads(response.body)
        self.assertIn("daily_running", payload)
        self.assertNotIn("scheduler_running", payload)
        self.assertNotIn("timer_running", payload)

    def test_supabase_probe_is_read_only_and_uses_retained_table(self):
        calls = []

        class Query:
            def select(self, value):
                calls.append(("select", value))
                return self

            def limit(self, value):
                calls.append(("limit", value))
                return self

            def execute(self):
                calls.append(("execute", None))

        class Client:
            def table(self, name):
                calls.append(("table", name))
                return Query()

        db._probe_client(Client())
        self.assertEqual(calls, [
            ("table", "memory_digest_runs"),
            ("select", "id"),
            ("limit", 1),
            ("execute", None),
        ])


if __name__ == "__main__":
    unittest.main()
