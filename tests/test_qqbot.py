"""Durable QQ inbox/outbox, owner boundaries, pacing and sticker scenarios."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from gateway.qqbot import QQRuntime, control_action, multiple_stickers
from gateway.qqbot_config import QQConfig
from gateway.qqbot_stickers import StickerCatalog
from gateway.qqbot_store import QQStore
from gateway.qqbot_transport import NotSubmitted


class Clock:
    def __init__(self):
        self.value = datetime(2026,10,21,12,tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    def __call__(self): return self.value
    def advance(self,seconds):
        self.value+=seconds
        if hasattr(self,"on_advance"): self.on_advance()


class Transport:
    ready=True
    def __init__(self): self.sent=[];self.result=("confirmed","qq-ack",None);self.error=None
    async def send(self,segments):
        self.sent.append(segments)
        if self.error: raise self.error
        return self.result
    async def close(self): self.ready=False


class Model:
    def __init__(self): self.calls=[];self.result={"send":True,"text":"测试回复","sticker_ids":[]};self.wait=None
    async def generate(self,history,query,proactive=False):
        self.calls.append((history,query,proactive))
        if self.wait: await self.wait.wait()
        return dict(self.result)


def event(mid=1,text="你好",**extra):
    return {"post_type":"message","message_type":"private","self_id":12345,"user_id":67890,
            "sub_type":"friend","message_id":mid,"message":[{"type":"text","data":{"text":text}}],**extra}


class ConfigTests(unittest.TestCase):
    def valid(self):
        return {"QQBOT_ENABLED":"true","QQBOT_TOKEN":"test-token-"*4,"QQBOT_ACCOUNT_ID":"12345",
                "QQBOT_OWNER_ID":"67890","QQBOT_ASSISTANT_ID":"existing-role"}
    def test_disabled_ignores_unconfigured_fields(self):
        self.assertFalse(QQConfig.from_env({"QQBOT_ENABLED":"false","QQBOT_TIMEZONE":"bad"}).enabled)
    def test_explicit_identity_and_token_required(self):
        for key,value in [("QQBOT_TOKEN",""),("QQBOT_OWNER_ID","12345"),("QQBOT_ACCOUNT_ID","x"),("QQBOT_ACCOUNT_ID","1１２３４"),("QQBOT_ASSISTANT_ID","")]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                QQConfig.from_env({**self.valid(),key:value})
    def test_role_must_match_phone_when_configured(self):
        with self.assertRaises(ValueError): QQConfig.from_env({**self.valid(),"MEMORY_ASSISTANT_ID":"other"})
    def test_enabling_proactive_requires_explicit_zone_and_window(self):
        with self.assertRaises(ValueError): QQConfig.from_env({**self.valid(),"QQBOT_PROACTIVE_ENABLED":"true"})
        cfg=QQConfig.from_env({**self.valid(),"QQBOT_PROACTIVE_ENABLED":"true","QQBOT_TIMEZONE":"America/Los_Angeles","QQBOT_ACTIVE_START":"09:00","QQBOT_ACTIVE_END":"22:00"})
        self.assertTrue(cfg.proactive_enabled)
    def test_blank_example_window_does_not_break_private_only(self):
        self.assertTrue(QQConfig.from_env({**self.valid(),"QQBOT_TIMEZONE":"","QQBOT_ACTIVE_START":""}).enabled)
    def test_control_requires_full_actual_request(self):
        self.assertEqual(control_action("暂停主动消息！"),"pause")
        for text in ["不要暂停主动消息","他说暂停主动消息","教我如何恢复主动消息"]:
            self.assertIsNone(control_action(text))
        self.assertTrue(multiple_stickers("给我连发表情包"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=str(Path(self.tmp.name)/"state.sqlite3")
        self.store=QQStore(self.path,"scope");self.addCleanup(self.store.close)
    def test_dedup_durable_current_input_and_restart(self):
        self.assertTrue(self.store.accept(1,"本条输入",100))
        self.assertFalse(self.store.accept(1,"本条输入",101))
        second=QQStore(self.path,"scope")
        try: self.assertEqual(second.history()[0]["content"],"本条输入")
        finally: second.close()
    def test_scope_cannot_be_reused_for_other_owner(self):
        with self.assertRaises(ValueError): QQStore(self.path,"other")
    def test_control_state_and_reply_are_committed_with_input(self):
        self.assertTrue(self.store.accept(1,'暂停主动消息',100,action='pause',reply='好'))
        second=QQStore(self.path,'scope')
        try:
            second.recover(101,'today')
            self.assertTrue(second.state()['paused'])
            self.assertFalse(second.pending())
            self.assertEqual(second.counts(),{'queued':1})
            self.assertFalse(second.accept(1,'暂停主动消息',102,action='pause',reply='好'))
            self.assertEqual(second.counts(),{'queued':1})
        finally: second.close()
    def test_control_failure_rolls_back_input_state_and_reply_together(self):
        self.store.conn.execute("CREATE TRIGGER fail_control BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.accept(1,'暂停主动消息',100,action='pause',reply='好')
        self.assertEqual(self.store.history(),[])
        self.assertFalse(self.store.state()['paused'])
        self.assertFalse(self.store.pending())
    def test_lease_and_recovery(self):
        other=QQStore(self.path,"scope")
        try:
            self.assertTrue(self.store.acquire("a",100));self.assertFalse(other.acquire("b",101))
            self.assertTrue(other.acquire("b",131))
        finally: other.close()
    def test_debounce_and_max_wait(self):
        self.store.accept(1,"a",100);self.store.accept(2,"b",102)
        self.assertEqual(self.store.batch(103,3,10),[])
        self.assertEqual(len(self.store.batch(105,3,10)),2)
        self.store.recover()
        self.assertEqual(len(self.store.batch(111,100,10)),2)
    def test_history_budget_never_drops_current_batch(self):
        for i in range(5): self.store.accept(i,str(i),100+i)
        batch=self.store.batch(110,0,10)
        self.assertEqual(len(self.store.batch_history(batch,2)),5)
    def test_only_confirmed_send_becomes_assistant_history(self):
        self.store.queue("task","reply",[],"真的发出",100,200)
        task=self.store.claim_send(101)
        self.assertEqual(self.store.history(),[])
        self.store.finish_send(task["id"],"confirmed",102,"1")
        self.store.finish_send(task["id"],"confirmed",103,"1")
        self.assertEqual(len(self.store.history()),1)
        self.assertEqual(self.store.history()[0]["content"],"真的发出")
    def test_crash_marks_all_sent_in_flight_unknown_without_requeue(self):
        self.store.queue("task","reply",[],"草稿",100,200)
        self.store.claim_send(101);self.store.recover(102,"day")
        self.assertEqual(self.store.counts(),{"unknown":1})
        self.assertIsNone(self.store.claim_send(103));self.assertEqual(self.store.history(),[])
    def test_proactive_crash_reserves_backoff_slot(self):
        self.store.queue("p","proactive",[],"草稿",100,200,revision=0)
        self.store.claim_send(101);self.store.recover(102,"today")
        self.assertEqual(self.store.state()["unanswered"],1)
        self.assertEqual(self.store.state()["last_sent"],102)
    def test_long_configured_proactive_cooldown_is_preserved(self):
        self.store.queue('p','proactive',[],'问候',100,200,revision=0)
        task=self.store.claim_send(101)
        self.store.finish_send(task['id'],'confirmed',102,'1',day='today',cooldown=604800)
        self.assertEqual(self.store.state()['next_check'],102+604800)
    def test_expiry_and_pause_are_persistent(self):
        self.store.queue("p","proactive",[],"问候",100,200,revision=0)
        self.store.control("pause",101)
        self.assertTrue(self.store.state()["paused"]);self.assertEqual(self.store.counts(),{"failed":1})
        self.store.queue("r","reply",[],"正常聊天",100,101)
        self.assertIsNone(self.store.claim_send(102))
    def test_stale_proactive_revision_cannot_queue(self):
        self.store.control("pause",100);self.store.control("resume",101)
        self.assertFalse(self.store.queue("old","proactive",[],"过期模型结果",102,200,revision=0))
    def test_exports_keep_key_and_retry_after_ambiguous_result(self):
        self.store.accept(1,"真实输入",100)
        row=self.store.exports(100)[0];key=self.store.export_key(row["message_key"])
        self.store.export_result(row["id"],False,100)
        self.assertEqual(self.store.exports(101),[])
        self.assertEqual(self.store.export_key(self.store.exports(161)[0]["message_key"]),key)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock=Clock();self.transport=Transport();self.model=Model()
        self.cfg=QQConfig(enabled=True,account_id="12345",owner_id="67890",assistant_id="role",
            token="x"*32,store_path=str(Path(self.tmp.name)/"state.sqlite3"),proactive_enabled=True,
            debounce_seconds=3,cooldown_seconds=60,quiet_seconds=10,check_seconds=60)
        self.runtime=QQRuntime(self.cfg,model=self.model,transport=self.transport,now=self.clock,exporter=lambda *a:None)
        self.runtime.activate();self.addCleanup(self.runtime.store.close)
        self.clock.on_advance=lambda:self.runtime.store.acquire(self.runtime.owner,self.clock())
    async def prepare(self,text="你好",mid=1):
        await self.runtime.ingest(event(mid,text));self.clock.advance(4);await self.runtime.reply_once();await self.runtime.send_once()
    async def test_only_owner_private_real_bot_events_enter(self):
        for changes in [{"user_id":99999},{"message_type":"group"},{"group_id":4},{"self_id":99999},{"sub_type":"group"},{"post_type":"notice"}]:
            self.assertFalse(await self.runtime.ingest(event(**changes)))
        self.assertEqual(self.runtime.store.history(),[])
    async def test_current_input_sent_without_memory_database(self):
        await self.prepare("当前输入")
        self.assertEqual(self.model.calls[0][1],"当前输入")
        self.assertEqual([r["role"] for r in self.runtime.store.history()],["user","assistant"])
    async def test_merge_and_replay_create_one_reply(self):
        await self.runtime.ingest(event(1,"第一句"));await self.runtime.ingest(event(2,"补充"))
        self.assertFalse(await self.runtime.ingest(event(1,"第一句")))
        self.clock.advance(4);await self.runtime.reply_once();await self.runtime.reply_once();await self.runtime.send_once()
        self.assertEqual(len(self.model.calls),1);self.assertIn("补充",self.model.calls[0][1])
        self.assertEqual(len(self.transport.sent),1)
    async def test_new_message_during_generation_waits_next_round(self):
        await self.runtime.ingest(event(1,"第一问"));self.clock.advance(4)
        self.model.wait=asyncio.Event();task=asyncio.create_task(self.runtime.reply_once());await asyncio.sleep(0)
        await self.runtime.ingest(event(2,"第二问"));self.model.wait.set();await task
        self.clock.advance(4);await self.runtime.send_once();await self.runtime.reply_once()
        self.assertEqual(len(self.model.calls),2)
        self.assertEqual(self.model.calls[1][1],"第二问")
        self.assertIn("测试回复",str(self.model.calls[1][0]))
    async def test_generation_failure_produces_safe_feedback(self):
        async def fail(*a,**k): raise ValueError("model unavailable")
        self.model.generate=fail;await self.prepare()
        self.assertIn("暂时",self.transport.sent[0][0]["data"]["text"])
    async def test_unknown_receipt_does_not_become_history_or_retry(self):
        self.transport.result=("unknown",None,"async")
        await self.prepare();await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual([r["role"] for r in self.runtime.store.history()],["user"])
    async def test_not_submitted_can_retry_after_reconnect(self):
        self.transport.error=NotSubmitted("offline");await self.prepare()
        self.assertEqual(self.runtime.store.counts(),{"queued":1})
        self.transport.error=None;await self.runtime.send_once()
        self.assertEqual(self.runtime.store.counts(),{"confirmed":1})
    async def test_proactive_respects_online_quiet_and_empty_history(self):
        await self.runtime.proactive_once();self.assertEqual(self.model.calls,[])
        await self.prepare();self.transport.ready=False;self.clock.advance(100)
        await self.runtime.proactive_once();self.assertEqual(len(self.model.calls),1)
        self.transport.ready=True;await self.runtime.proactive_once();await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent),2)
    async def test_pause_during_generation_and_resume_drop_old_proactive(self):
        await self.prepare();self.clock.advance(100)
        self.model.wait=asyncio.Event();task=asyncio.create_task(self.runtime.proactive_once());await asyncio.sleep(0)
        await self.runtime.ingest(event(2,"暂停主动消息"));await self.runtime.ingest(event(3,"恢复主动消息"))
        self.model.wait.set();await task
        self.assertFalse(self.runtime.store.open_proactive())
    async def test_new_owner_message_invalidates_proactive_draft(self):
        await self.prepare();self.clock.advance(100)
        self.model.wait=asyncio.Event();task=asyncio.create_task(self.runtime.proactive_once());await asyncio.sleep(0)
        await self.runtime.ingest(event(2,"我回来了"));self.model.wait.set();await task
        self.assertFalse(self.runtime.store.open_proactive())
    async def test_new_message_cancels_proactive_generation_without_waiting_for_model(self):
        await self.prepare();self.clock.advance(100)
        self.model.wait=asyncio.Event()
        task=asyncio.create_task(self.runtime.proactive_once())
        await asyncio.sleep(0);await asyncio.sleep(0)
        self.assertEqual(len(self.model.calls),2)
        await self.runtime.ingest(event(2,'我回来了'))
        await asyncio.wait_for(task,0.5)
        self.assertFalse(self.runtime.generate_lock.locked())
        self.assertFalse(self.runtime.store.open_proactive())
        self.model.wait=None;self.clock.advance(4)
        await self.runtime.reply_once();await self.runtime.send_once()
        self.assertEqual(self.model.calls[-1][1],'我回来了')
    async def test_today_control_status_reports_silence(self):
        await self.runtime.ingest(event(1,'今天先别找我'))
        await self.runtime.ingest(event(2,'查看主动消息状态'))
        await self.runtime.send_once();await self.runtime.send_once()
        self.assertIn('今天已静默',self.transport.sent[-1][0]['data']['text'])
    async def test_silent_check_never_creates_fake_user_history(self):
        await self.prepare();before=self.runtime.store.history();self.clock.advance(100)
        self.model.result={"send":False,"text":"","sticker_ids":[]}
        await self.runtime.proactive_once()
        self.assertEqual(self.runtime.store.history(),before)
    async def test_unanswered_backoff_and_incoming_reset(self):
        await self.prepare();self.clock.advance(100)
        await self.runtime.proactive_once();await self.runtime.send_once()
        state=self.runtime.store.state();self.assertEqual(state["unanswered"],1)
        self.clock.advance(61);await self.runtime.proactive_once();await self.runtime.send_once()
        state=self.runtime.store.state();self.assertEqual(state["unanswered"],2)
        self.assertEqual(state["next_check"]-self.clock(),120)
        await self.runtime.ingest(event(2,"收到啦"));self.assertEqual(self.runtime.store.state()["unanswered"],0)
    async def test_cross_midnight_window_and_message_expiry(self):
        self.runtime.config=replace(self.cfg,active_start="22:00",active_end="08:00")
        at=datetime(2026,10,21,23,tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
        self.assertTrue(self.runtime._active(at)[0]);self.assertFalse(self.runtime._active(self.clock())[0])
        self.runtime.config=replace(self.cfg,active_end="12:01",proactive_ttl=1800)
        self.assertEqual(self.runtime._proactive_expiry(self.clock())-self.clock(),60)
    async def test_today_pause_persists_until_local_midnight(self):
        await self.runtime.ingest(event(1,"今天先别找我"))
        self.assertGreater(self.runtime.store.state()["silence_until"],self.clock())
        self.assertFalse(self.runtime.store.state()["paused"])
    async def test_memory_export_uses_stable_keys_and_only_real_messages(self):
        await self.prepare();rows=[]
        self.runtime.exporter=lambda config,key,row:rows.append((key,row["role"],row["content"]))
        await self.runtime.export_once();await self.runtime.export_once()
        self.assertEqual(len(rows),2);self.assertEqual({r[1] for r in rows},{"user","assistant"})
    async def test_same_sticker_can_be_sent_in_consecutive_rounds_without_cooldown(self):
        root=Path(self.tmp.name);(root/"cat.gif").write_bytes(b"GIF89a"+b"x"*20)
        (root/"stickers.json").write_text(json.dumps({"version":1,"stickers":[{"id":"cat","description":"猫猫震惊","file":"cat.gif"}]}),encoding="utf-8")
        self.runtime.catalog=StickerCatalog(root/"stickers.json")
        self.model.result={"send":True,"text":"","sticker_ids":["cat","cat"]}
        await self.prepare("好离谱",1);await self.prepare("更离谱了",2)
        self.assertEqual([len(s) for s in self.transport.sent],[1,1])
        self.assertTrue(all(s[0]["type"]=="image" for s in self.transport.sent))
        self.assertIn("猫猫震惊",self.runtime.store.history()[-1]["content"])
        await self.prepare("多发表情包",3);self.assertEqual(len(self.transport.sent[-1]),2)

    async def test_lost_lease_rejects_late_reply_and_new_ingress(self):
        await self.runtime.ingest(event(1,"第一问"));self.clock.advance(4)
        self.model.wait=asyncio.Event();task=asyncio.create_task(self.runtime.reply_once());await asyncio.sleep(0)
        self.clock.value+=31
        self.assertTrue(self.runtime.store.acquire("new-worker",self.clock()))
        self.model.wait.set();await task
        self.assertEqual(self.runtime.store.counts(),{})
        self.assertFalse(await self.runtime.ingest(event(2,"第二问")))
