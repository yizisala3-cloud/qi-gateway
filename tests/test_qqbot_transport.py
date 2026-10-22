"""OneBot handshake, action echoes, failed receipts and disabled route tests."""
import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import AsyncMock

from starlette.websockets import WebSocketDisconnect

from gateway.qqbot_config import QQConfig
from gateway.qqbot_transport import OneBotConnection, NotSubmitted
from gateway.qqbot import websocket_endpoint


class Socket:
    def __init__(self,token="x"*32,account="12345"):
        self.headers={"authorization":"Bearer "+token,"x-self-id":account}
        self.input=asyncio.Queue();self.output=asyncio.Queue();self.closed=[];self.accepted=False
    async def accept(self): self.accepted=True
    async def close(self,code):
        self.closed.append(code);await self.input.put(None)
    async def receive_text(self):
        value=await self.input.get()
        if value is None: raise WebSocketDisconnect()
        return json.dumps(value)
    async def send_json(self,value): await self.output.put(value)
    async def ack(self,request,data=None,status="ok",retcode=0):
        await self.input.put({"echo":request["echo"],"status":status,"retcode":retcode,"data":data or {}})


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config=QQConfig(enabled=True,token="x"*32,account_id="12345",owner_id="67890",action_timeout=1)
        self.events=AsyncMock();self.connection=OneBotConnection(self.config,self.events)
    async def connect(self):
        socket=Socket();task=asyncio.create_task(self.connection.serve(socket))
        login=await asyncio.wait_for(socket.output.get(),1)
        self.assertEqual(login["action"],"get_login_info")
        await socket.ack(login,{"user_id":12345})
        status=await asyncio.wait_for(socket.output.get(),1);self.assertEqual(status["action"],"get_status")
        await socket.ack(status,{"online":True})
        for _ in range(4): await asyncio.sleep(0)
        self.assertTrue(self.connection.ready)
        return socket,task
    async def finish(self,socket,task):
        await socket.close(1000);await asyncio.wait_for(task,1)
    async def test_auth_and_real_account_headers_required(self):
        for token,account in [("wrong","12345"),("x"*32,"99999"),("非ASCII","12345")]:
            socket=Socket(token,account);await self.connection.serve(socket)
            self.assertEqual(socket.closed,[1008]);self.assertFalse(socket.accepted)
    async def test_disabled_route_does_not_construct_runtime(self):
        socket=Socket();socket.app=type("App",(),{"state":type("State",(),{})()})()
        await websocket_endpoint(socket);self.assertEqual(socket.closed,[1008])
    async def test_bare_token_is_not_a_bearer_header(self):
        socket=Socket();socket.headers['authorization']='x'*32
        await self.connection.serve(socket)
        self.assertEqual(socket.closed,[1008]);self.assertFalse(socket.accepted)
    async def test_write_is_not_success_until_matching_echo_and_id_arrives(self):
        socket,task=await self.connect()
        send=asyncio.create_task(self.connection.send([{"type":"text","data":{"text":"你好"}}]))
        request=await asyncio.wait_for(socket.output.get(),1)
        self.assertEqual(request["params"]["user_id"],67890);self.assertFalse(send.done())
        await socket.input.put({"echo":"wrong","status":"ok","retcode":0,"data":{"message_id":1}})
        await asyncio.sleep(0);self.assertFalse(send.done())
        await socket.ack(request,{"message_id":42})
        self.assertEqual(await send,("confirmed","42",None));await self.finish(socket,task)
    async def test_async_missing_id_and_failed_receipts(self):
        socket,task=await self.connect()
        for status,retcode,data,expected in [("async",0,{},"unknown"),("ok",0,{},"unknown"),("failed",100,{},"failed")]:
            send=asyncio.create_task(self.connection.send([]));request=await socket.output.get()
            await socket.ack(request,data,status,retcode);self.assertEqual((await send)[0],expected)
        await self.finish(socket,task)
    async def test_disconnect_after_submission_is_unknown_to_caller(self):
        socket,task=await self.connect()
        send=asyncio.create_task(self.connection.send([]));await socket.output.get()
        await self.finish(socket,task)
        with self.assertRaises(ConnectionError): await send
    async def test_wrong_login_account_never_becomes_ready(self):
        socket=Socket();task=asyncio.create_task(self.connection.serve(socket))
        login=await socket.output.get();await socket.ack(login,{"user_id":99999})
        await asyncio.wait_for(task,1);self.assertFalse(self.connection.ready);self.assertIn(1008,socket.closed)
    async def test_wrong_event_account_and_self_notices_are_filtered(self):
        socket,task=await self.connect()
        await socket.input.put({"self_id":99999,"post_type":"message"})
        await socket.input.put({"self_id":12345,"post_type":"meta_event","meta_event_type":"heartbeat","status":{"online":False}})
        for _ in range(3): await asyncio.sleep(0)
        self.events.assert_not_called();self.assertFalse(self.connection.ready)
        await self.finish(socket,task)
    async def test_offline_action_not_submitted(self):
        with self.assertRaises(NotSubmitted): await self.connection.send([])
