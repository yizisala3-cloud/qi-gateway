"""Re-run the original review against current code, without game/network I/O."""
import asyncio
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import httpx
import pytest

from test_sky_companion import (
    b, config, private_files, line, screen, sender, FakeDevice,
)


@pytest.mark.parametrize("raw", [
    "叶子：你好-朋友甲", "叶子：我到了-叶子",
])
def test_actual_sender_suffix_takes_precedence_over_body_prefix(config, raw):
    messages = b.chat_messages([line(raw)], config["chat_region"], ["叶子"])
    expected = [] if raw.endswith("朋友甲") else [{"sender": "叶子", "text": "叶子：我到了"}]
    assert messages == expected


def test_peer_can_reply_with_text_the_ai_previously_used(config):
    companion = b.Companion(config, FakeDevice(), None,
                            reader=lambda _: screen(history=["好的-叶子"]))
    companion.sent.append(b.normal("好的"))
    assert companion.observe()["messages"] == [{"sender": "叶子", "text": "好的"}]


def test_new_echo_is_accepted_when_old_identical_echo_scrolls_out(config):
    before = screen(history=["好的"])
    after = screen()
    after["lines"].insert(0, line("好的", y=740))
    companion, device = sender(config, [before, screen("好的"), after])
    assert companion.send("好的")["confirmed"]
    assert len(device.keys) == 1


def test_receipt_four_seconds_late_is_still_inside_five_second_budget(config, monkeypatch):
    class Clock:
        now = 0.0

        def monotonic(self):
            return self.now

        def time(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    clock = Clock()
    monkeypatch.setattr(b, "time", clock)
    device = FakeDevice()
    reads = 0

    def reader(_):
        nonlocal reads
        reads += 1
        clock.now += 0.2
        if reads == 1:
            return screen()
        if reads == 2:
            return screen("测试")
        return screen(history=["测试"] if clock.now >= 4.4 else [])

    companion = b.Companion(config, device, None, reader=reader)
    try:
        result = companion.send("测试")
    except b.BridgeError as exc:
        pytest.fail(f"Stopped at simulated t={clock.now:.2f}s, before the 5s receipt deadline: {exc}")
    assert result["confirmed"] and len(device.keys) == 1


@pytest.mark.parametrize("reply", ["这只是预览", "字" * 36])
def test_preview_or_rejected_reply_is_not_saved_as_delivered_chat(config, monkeypatch, reply):
    # Never load deployment credentials or connect to production services.
    import dotenv
    # The local pinned python-dotenv predates PYTHON_DOTENV_DISABLED.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **kw: False)
    for key in list(os.environ):
        if key.startswith(("SUPABASE_", "UPSTREAM_", "ANALYSIS_", "CONTINUITY_",
                           "RUMINATION_", "QQBOT_")) or key in {
            "GATEWAY_TOKEN", "MCP_MEMORY_TOKEN", "MEMORY_PLUGIN_TOKEN",
            "TODO_PLUGIN_TOKEN", "MEMORY_ASSISTANT_ID",
        }:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("QQBOT_ENABLED", "false")
    monkeypatch.setenv("GATEWAY_LOG_ENABLED", "false")
    from gateway import main as gm

    saved = []
    monkeypatch.setattr(gm, "verify_token", lambda _: True)
    monkeypatch.setattr(gm, "build_context", lambda *a, **kw: "")
    monkeypatch.setattr(gm.sky_settings, "snapshot", lambda **_: {"enabled": True, "available": True,
                        "pause_epoch":gm.sky_settings.INITIAL_EPOCH})
    monkeypatch.setattr(gm, "resolve_assistant_id", lambda: "review-assistant")
    monkeypatch.setattr(gm.db, "save_chat_message", lambda role, text, *a: saved.append((role, text)))

    class Upstream:
        async def post(self, *a, **kw):
            return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]})

    monkeypatch.setattr(gm, "http_client", Upstream())

    def respond(request):
        class Request:
            headers = request.headers
            async def json(self):
                return json.loads(request.content)

        async def scenario():
            routes = {"/v1/chat/completions": gm.chat_completions,
                      "/v1/sky/chat/completions": gm.sky_chat_preview}
            response = await routes[request.url.path](Request())
            while gm._background_tasks:
                await asyncio.gather(*list(gm._background_tasks))
            return httpx.Response(response.status_code, content=response.body)

        return asyncio.run(scenario())

    gateway = b.Gateway(config, transport=httpx.MockTransport(respond))
    try:
        if len(reply) > config["reply_chars"]:
            with pytest.raises(b.BridgeError):
                gateway.generate("预览测试")
        else:
            assert gateway.generate("预览测试") == reply
    finally:
        gateway.close()
    assert saved == [], f"Nothing was sent to the game, but chat rows were persisted: {saved!r}"
