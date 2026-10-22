"""Real QQ model/runtime regressions for copied historical sticker summaries."""
import base64
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_model import QQModel
from gateway.qqbot_stickers import StickerCatalog
from tests.test_qqbot import Clock, Model, Transport, event
from tests.test_qqbot_context import Client


class StickerOutputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.images = {"cat_094": b"GIF89a" + b"a" * 20,
                       "cat_095": b"GIF89a" + b"b" * 20}
        entries = []
        for key, data in self.images.items():
            (root / (key + ".gif")).write_bytes(data)
            entries.append({"id": key, "file": key + ".gif", "description": "小猫轻轻摆动小爪"})
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"version": 1, "stickers": entries}), encoding="utf-8")
        self.catalog = StickerCatalog(manifest)
        self.clock, self.transport, self.model = Clock(), Transport(), Model()
        self.config = QQConfig(enabled=True, account_id="12345", owner_id="67890",
                               assistant_id="role", debounce_seconds=0,
                               store_path=str(root / "ledger.sqlite3"))
        self.runtime = QQRuntime(self.config, model=self.model, catalog=self.catalog,
                                 transport=self.transport, exporter=lambda *a: None, now=self.clock)
        self.runtime.activate()
        self.addCleanup(self.runtime.store.close)

    async def reply(self, mid, text):
        await self.runtime.ingest(event(mid, text))
        await self.runtime.reply_once()
        await self.runtime.send_once()
        return self.transport.sent[-1]

    def assert_images(self, segments, keys):
        images = [s for s in segments if s["type"] == "image"]
        self.assertEqual([base64.b64decode(s["data"]["file"][9:]) for s in images],
                         [self.images[key] for key in keys])

    async def test_screenshot_marker_becomes_real_image_for_json_and_plain_responses(self):
        marker = "[表情包 cat_094：小猫轻轻摆动小爪]"
        for raw in [json.dumps({"text": "我挑的，能不可爱？\n" + marker, "sticker_ids": []}),
                    "我挑的，能不可爱？\n" + marker]:
            with self.subTest(raw=raw):
                client = Client({"choices": [{"message": {"content": raw}}]})
                self.runtime.model = QQModel(client, self.config, self.catalog,
                                             context_builder=lambda *a, **k: "")
                sent = await self.reply(len(self.transport.sent) + 1, "哎哟小表情真可爱")
                self.assert_images(sent, ["cat_094"])
                self.assertEqual(sent[0], {"type": "text", "data": {"text": "我挑的，能不可爱？"}})
                self.assertEqual(self.runtime.store.history()[-1]["content"].count(marker), 1)

    async def test_previous_confirmed_sticker_is_structured_in_next_model_request(self):
        self.model.result = {"text": "看好了", "sticker_ids": ["cat_094"]}
        await self.reply(1, "发一张表情")
        previous = self.runtime.store.history()[-1]["content"]
        client = Client({"choices": [{"message": {"content": '{"text":"可爱吧","sticker_ids":[]}'}}]})
        self.runtime.model = QQModel(client, self.config, self.catalog,
                                     context_builder=lambda *a, **k: "")
        await self.reply(2, "小表情真可爱")
        messages = client.calls[0][1]["json"]["messages"]
        assistant = next(m for m in messages if m["role"] == "assistant")
        saved = json.loads(assistant["content"])
        self.assertEqual(saved["text"], "看好了")
        self.assertEqual(saved["sticker_ids"], ["cat_094"])
        self.assertEqual(saved["sent_stickers"][0]["description"], "小猫轻轻摆动小爪")
        self.assertNotIn("[表情包 ", assistant["content"])
        self.assertEqual(self.runtime.store.history()[1]["content"], previous)
        self.assertEqual(messages[-1], {"role": "user", "content": "小表情真可爱"})

    async def test_marker_only_plain_reply_and_malformed_json_boundaries(self):
        marker = self.catalog.summary("cat_094")
        for raw in [marker, marker + "\n" + marker]:
            with self.subTest(raw=raw):
                client = Client({"choices": [{"message": {"content": raw}}]})
                self.runtime.model = QQModel(client, self.config, self.catalog,
                                             context_builder=lambda *a, **k: "")
                sent = await self.reply(len(self.transport.sent) + 1, "发一张猫猫")
                self.assert_images(sent, ["cat_094"])
                self.assertEqual(len(sent), 1)
        for raw in ["[invalid", "{invalid", '["' + marker + '",invalid]',
                    marker + "\n[invalid", "```text\n" + marker + "\n```"]:
            with self.subTest(raw=raw):
                client = Client({"choices": [{"message": {"content": raw}}]})
                model = QQModel(client, self.config, self.catalog, context_builder=lambda *a, **k: "")
                with self.assertRaises(ValueError):
                    await model.generate([{"role": "user", "content": "你好"}], "你好")
        client = Client({"choices": [{"message": {"content": marker}}]})
        model = QQModel(client, self.config, self.catalog, context_builder=lambda *a, **k: "")
        with self.assertRaises(ValueError):
            await model.generate([], "", proactive=True)

    async def test_markers_and_explicit_ids_do_not_duplicate_but_requested_repeats_work(self):
        marker = self.catalog.summary("cat_094")
        for query, keys, text, expected in [
            ("可爱", ["cat_094"], marker, ["cat_094"]),
            ("多发表情包", ["cat_094"], marker * 2, ["cat_094", "cat_094"]),
            ("多发表情包", ["cat_094", "cat_094"], marker, ["cat_094", "cat_094"]),
            ("可爱", [], marker + self.catalog.summary("cat_095"), ["cat_094"]),
        ]:
            with self.subTest(query=query, keys=keys, text=text):
                sent, summary = self.runtime._segments({"text": text, "sticker_ids": keys}, query)
                self.assert_images(sent, expected)
                self.assertEqual(summary.count(marker), expected.count("cat_094"))

    async def test_unknown_or_missing_sticker_never_sends_a_placeholder(self):
        sent, summary = self.runtime._segments(
            {"text": "先聊文字\n[表情包 removed：旧素材]", "sticker_ids": []}, "你好")
        self.assertEqual(sent, [{"type": "text", "data": {"text": "先聊文字"}}])
        self.assertEqual(summary, "先聊文字")
        with self.assertRaises(ValueError):
            self.runtime._segments({"text": "[表情包 removed：旧素材]", "sticker_ids": []}, "你好")
        (Path(self.tmp.name) / "cat_094.gif").unlink()
        self.model.result = {"text": self.catalog.summary("cat_094"), "sticker_ids": []}
        sent = await self.reply(1, "猫猫呢")
        self.assert_images(sent, [])
        self.assertNotIn("[表情包", str(sent))
        self.assertNotIn("[表情包", self.runtime.store.history()[-1]["content"])

    async def test_plain_user_text_and_recall_input_are_preserved(self):
        marker = self.catalog.summary("cat_094")
        history = [{"role": "user", "content": marker},
                   {"role": "assistant", "content": "普通文字[备注]，表情包很可爱"},
                   {"role": "user", "content": "继续"}]
        original = [dict(row) for row in history]
        builder = Mock(return_value="长期记忆")
        client = Client({"choices": [{"message": {"content": '{"text":"自然回复","sticker_ids":[]}'}}]})
        model = QQModel(client, self.config, self.catalog, context_builder=builder)
        await model.generate(history, "继续")
        messages = client.calls[0][1]["json"]["messages"]
        self.assertEqual([m for m in messages if m["role"] != "system"], history)
        self.assertEqual(history, original)
        self.assertEqual(builder.call_args.args[1], [(marker, history[1]["content"])])
        self.assertEqual(builder.call_args.kwargs, {"include_eventide": False, "include_recent_chat": False})

    async def test_proactive_path_uses_images_and_silence_stays_silent(self):
        self.model.result = {"text": "普通回复", "sticker_ids": []}
        await self.reply(1, "你好")
        self.runtime.config = replace(self.config, proactive_enabled=True, quiet_seconds=1,
                                      cooldown_seconds=60)
        self.clock.advance(2)
        self.model.result = {"send": True, "text": "[表情包 cat_095: 小猫轻轻摆动小爪]", "sticker_ids": []}
        await self.runtime.proactive_once()
        await self.runtime.send_once()
        self.assert_images(self.transport.sent[-1], ["cat_095"])
        self.clock.advance(400)
        self.runtime.store.acquire(self.runtime.owner, self.clock())
        self.model.result = {"send": False, "text": self.catalog.summary("cat_094"), "sticker_ids": []}
        before = len(self.transport.sent)
        await self.runtime.proactive_once()
        await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent), before)
