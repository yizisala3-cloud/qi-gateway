"""QQ shares long-term recall but never submits Eventide or global history jobs."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import httpx

from gateway import context
from gateway.qqbot_config import QQConfig
from gateway.qqbot_model import QQModel
from gateway.qqbot_stickers import StickerCatalog
from gateway.qqbot_proactive import QQModelHTTPError


class CatalogTests(unittest.TestCase):
    def test_empty_catalog_is_valid_without_materials(self):
        self.assertEqual(StickerCatalog().choices(),[])
    def test_catalog_rejects_traversal_duplicates_and_fake_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest=root/"stickers.json"
            (root/"ok.gif").write_bytes(b"GIF89a"+b"x"*20)
            cases=[
                [{"id":"a","description":"a","file":"../outside.gif"}],
                [{"id":"a","description":"a","file":"ok.gif"}]*2,
                [{"id":"a","description":"a","file":"bad.png"}],
            ]
            (root/"bad.png").write_bytes(b"not an image")
            for items in cases:
                manifest.write_text(json.dumps({"version":1,"stickers":items}),encoding="utf-8")
                with self.subTest(items=items),self.assertRaises((ValueError,OSError)):
                    StickerCatalog(manifest)
    def test_material_snapshot_has_no_model_paths_and_sends_base64(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"ok.gif").write_bytes(b"GIF89a"+b"x"*20)
            (root/"stickers.json").write_text(json.dumps({"version":1,"stickers":[{"id":"cat","description":"猫猫","file":"ok.gif","tags":["开心"]}]}),encoding="utf-8")
            catalog=StickerCatalog(root/"stickers.json")
            self.assertNotIn("file",catalog.choices()[0]);self.assertTrue(catalog.segment("cat")["data"]["file"].startswith("base64://"))


class ContextTests(unittest.TestCase):
    def test_qq_flags_do_not_even_read_eventide_or_global_history_switches(self):
        class Future:
            def result(self,timeout=None): return "共同人格"
        with patch.object(context,"_submit",return_value=Future()) as submit, \
             patch.object(context,"build_timestamp_context",return_value="当前时间"), \
             patch.object(context.app_settings,"is_eventide_injection_enabled",side_effect=AssertionError("Eventide read")), \
             patch.object(context.app_settings,"is_recent_chat_injection_enabled",side_effect=AssertionError("global history read")):
            result=context.build_context("",include_eventide=False,include_recent_chat=False)
            self.assertEqual(result,"当前时间\n\n共同人格")
            self.assertEqual(submit.call_count,1)
    def test_qq_flags_keep_long_term_search_and_actual_recall_recording(self):
        class Future:
            def result(self,timeout=None): return "人格"
        async def search(*args,**kwargs): return [{"id":1}]
        with patch.object(context,"_submit",return_value=Future()), \
             patch.object(context,"build_timestamp_context",return_value=""), \
             patch.object(context,"search_memories",side_effect=search) as recall, \
             patch.object(context,"format_memories_for_injection",return_value="相关长期记忆"), \
             patch.object(context,"record_memories_in_background") as record:
            result=context.build_context("当前问题",include_eventide=False,include_recent_chat=False)
            self.assertIn("相关长期记忆",result);self.assertEqual(recall.call_args.args[0],"当前问题")
            record.assert_called_once_with([{"id":1}])


class Client:
    def __init__(self,result): self.result=result;self.calls=[]
    async def post(self,url,**kwargs):
        self.calls.append((url,kwargs))
        return type("Response",(),{"status_code":200,"json":lambda _:self.result})()


class ModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_status_and_retry_header_are_preserved_without_response_body(self):
        class ErrorClient:
            async def post(self,*args,**kwargs):
                return httpx.Response(429,headers={'Retry-After':'7200'},text='private upstream body')
        model=QQModel(ErrorClient(),QQConfig(),StickerCatalog(),context_builder=Mock(return_value=''))
        with self.assertRaises(QQModelHTTPError) as raised:
            await model.generate([{'role':'user','content':'test'}],'test',True)
        self.assertEqual(raised.exception.status_code,429)
        self.assertEqual(raised.exception.retry_after,7200)
        self.assertNotIn('private upstream body',str(raised.exception))
    async def generate(self,content,proactive=False,tool_calls=None):
        message={"content":content}
        if tool_calls: message["tool_calls"]=tool_calls
        client=Client({"choices":[{"message":message}]})
        builder=Mock(return_value="相关记忆")
        model=QQModel(client,QQConfig(),StickerCatalog(),context_builder=builder)
        result=await model.generate([{"role":"user","content":"当前输入"}],"当前输入",proactive)
        return result,client,builder
    async def test_structured_result_uses_shared_model_context_without_emotion(self):
        result,client,builder=await self.generate('{"send":true,"text":"你好","sticker_ids":[]}')
        self.assertEqual(result["text"],"你好")
        self.assertEqual(builder.call_args.kwargs,{"include_eventide":False,"include_recent_chat":False})
        body=client.calls[0][1]["json"]
        self.assertEqual(body["tools"],[]);self.assertEqual(body["tool_choice"],"none")
        self.assertIn("当前输入",str(body["messages"]))
    async def test_plain_text_fallback_is_only_allowed_for_direct_chat(self):
        result,_,_=await self.generate("自然回复")
        self.assertEqual(result["text"],"自然回复")
        with self.assertRaises(ValueError): await self.generate("自然回复",proactive=True)
    async def test_silent_proactive_result_and_invalid_control_output(self):
        result,_,_=await self.generate('{"send":false,"text":"","sticker_ids":[]}',proactive=True)
        self.assertFalse(result["send"])
        for raw in ['{"send":"false"}','{"text":1}','{"text":"a","sticker_ids":[{}]}','{invalid']:
            with self.subTest(raw=raw),self.assertRaises(ValueError): await self.generate(raw)
    async def test_tool_calls_are_never_executed(self):
        with self.assertRaises(ValueError): await self.generate("",tool_calls=[{"function":{"name":"anything"}}])

    async def test_memory_failure_keeps_current_question_and_local_history(self):
        client=Client({"choices":[{"message":{"content":"你好"}}]})
        model=QQModel(client,QQConfig(),StickerCatalog(),context_builder=Mock(side_effect=RuntimeError("offline memory")))
        result=await model.generate([{"role":"user","content":"本条问题"}],"本条问题")
        self.assertEqual(result["text"],"你好")
        self.assertIn("本条问题",str(client.calls[0][1]["json"]["messages"]))
