"""In-process ASGI + simulated NapCat, exercising real runtime/WS/inbox/outbox."""
import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

from starlette.applications import Starlette
from starlette.routing import WebSocketRoute
from starlette.testclient import TestClient

from gateway.qqbot import QQRuntime, websocket_endpoint
from gateway.qqbot_config import QQConfig


class QQASGITests(unittest.TestCase):
    def test_early_owner_message_and_missing_sticker_allow_actual_text_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest=root/'stickers.json'
            manifest.write_text(json.dumps({'version':1,'stickers':[
                {'id':'cat','description':'cat','file':'missing.gif'}]}),encoding='utf-8')
            cfg=QQConfig(enabled=True,account_id='12345',owner_id='67890',assistant_id='role',
                token='x'*32,store_path=str(root/'qq.sqlite3'),sticker_manifest=str(manifest),debounce_seconds=0)
            model=type('Model',(),{})()
            model.generate=AsyncMock(return_value={'send':True,'text':'early reply','sticker_ids':[]})
            @asynccontextmanager
            async def lifespan(app):
                runtime=QQRuntime(cfg,model=model,exporter=lambda *args:None)
                app.state.qqbot=runtime;runtime.start()
                try: yield
                finally: await runtime.stop()
            app=Starlette(routes=[WebSocketRoute('/qqbot/onebot',websocket_endpoint)],lifespan=lifespan)
            with TestClient(app) as client:
                with client.websocket_connect('/qqbot/onebot',headers={'authorization':'Bearer '+cfg.token,'x-self-id':'12345'}) as ws:
                    login=ws.receive_json()
                    early={'post_type':'message','message_type':'private','self_id':12345,'user_id':67890,
                           'message_id':1,'sub_type':'friend','message':[{'type':'text','data':{'text':'early question'}}]}
                    ws.send_json(early)
                    ws.send_json({'echo':login['echo'],'status':'ok','retcode':0,'data':{'user_id':12345}})
                    status=ws.receive_json()
                    ws.send_json({'echo':status['echo'],'status':'ok','retcode':0,'data':{'online':True}})
                    async def generated():
                        async def wait():
                            while not model.generate.await_count: await asyncio.sleep(.01)
                        await asyncio.wait_for(wait(),1)
                    client.portal.call(generated)
                    reply=ws.receive_json()
                    self.assertEqual(reply['params']['message'],[{'type':'text','data':{'text':'early reply'}}])
                    self.assertEqual(model.generate.call_args.args[1],'early question')
                    self.assertEqual(app.state.qqbot.catalog.choices(),[])
                    ws.send_json({'echo':reply['echo'],'status':'ok','retcode':0,'data':{'message_id':99}})

    def test_actual_reverse_ws_flow_dedup_receipt_and_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=QQConfig(enabled=True,account_id="12345",owner_id="67890",assistant_id="existing-role",
                token="x"*32,store_path=str(Path(tmp)/"qq.sqlite3"),debounce_seconds=0)
            model=type("Model",(),{})()
            model.generate=AsyncMock(return_value={"send":True,"text":"真实链路回复","sticker_ids":[]})
            @asynccontextmanager
            async def lifespan(app):
                runtime=QQRuntime(cfg,model=model,exporter=lambda *args:None)
                app.state.qqbot=runtime;runtime.start()
                try: yield
                finally: await runtime.stop()
            app=Starlette(routes=[WebSocketRoute("/qqbot/onebot",websocket_endpoint)],lifespan=lifespan)
            with TestClient(app) as client:
                with client.websocket_connect("/qqbot/onebot",headers={"authorization":"Bearer "+cfg.token,"x-self-id":"12345"}) as ws:
                    request=ws.receive_json();self.assertEqual(request["action"],"get_login_info")
                    ws.send_json({"echo":request["echo"],"status":"ok","retcode":0,"data":{"user_id":12345}})
                    request=ws.receive_json();self.assertEqual(request["action"],"get_status")
                    ws.send_json({"echo":request["echo"],"status":"ok","retcode":0,"data":{"online":True}})
                    async def settle(): await asyncio.sleep(.25)
                    client.portal.call(settle)
                    event={"post_type":"message","message_type":"private","self_id":12345,"user_id":67890,
                           "message_id":1,"sub_type":"friend","message":[{"type":"text","data":{"text":"你好"}}]}
                    ws.send_json(event)
                    request=ws.receive_json();self.assertEqual(request["action"],"send_private_msg")
                    self.assertEqual(request["params"]["message"][0]["data"]["text"],"真实链路回复")
                    self.assertEqual(len(app.state.qqbot.store.history()),1)
                    ws.send_json({"echo":request["echo"],"status":"ok","retcode":0,"data":{"message_id":99}})
                    client.portal.call(settle)
                    self.assertEqual(len(app.state.qqbot.store.history()),2)
                    ws.send_json(event);client.portal.call(settle);self.assertEqual(model.generate.await_count,1)
                    event={**event,"message_id":2,"message":[{"type":"text","data":{"text":"暂停主动消息"}}]}
                    ws.send_json(event);request=ws.receive_json()
                    self.assertTrue(app.state.qqbot.store.state()["paused"])
                    ws.send_json({"echo":request["echo"],"status":"ok","retcode":0,"data":{"message_id":100}})
                    client.portal.call(settle)
