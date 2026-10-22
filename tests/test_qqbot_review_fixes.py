"""Recovery, ordering and bounded-I/O regressions from QQBot review R1-R7."""
import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_model import QQModel
from gateway.qqbot_stickers import StickerCatalog
from gateway.qqbot_store import QQStore
from gateway.qqbot_transport import NotSubmitted, OneBotConnection
from tests.test_qqbot import Clock, Model, Transport, event
from tests.test_qqbot_transport import Socket
from tests.test_qqbot_context import Client


class RuntimeFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock,self.model,self.transport=Clock(),Model(),Transport()
        self.config=QQConfig(enabled=True,account_id='12345',owner_id='67890',assistant_id='role',
            token='x'*32,debounce_seconds=0,store_path=str(Path(self.tmp.name)/'ledger.sqlite3'))
        self.runtime=QQRuntime(self.config,model=self.model,transport=self.transport,now=self.clock,
                               exporter=lambda *args:None)
        self.runtime.activate();self.addCleanup(self.runtime.store.close)
        self.clock.on_advance=lambda:self.runtime.store.acquire(self.runtime.owner,self.clock())

    async def prepare(self):
        await self.runtime.ingest(event(1,'A'))
        await self.runtime.reply_once();await self.runtime.send_once()

    async def test_model_turns_are_paired_and_current_inputs_appear_once_at_end(self):
        await self.runtime.ingest(event(1,'A'))
        self.model.wait=asyncio.Event()
        first=asyncio.create_task(self.runtime.reply_once());await asyncio.sleep(0)
        await self.runtime.ingest(event(2,'B'));await self.runtime.ingest(event(3,'C'))
        self.model.result['text']='answer A';self.model.wait.set();await first
        await self.runtime.send_once();self.model.wait=None;self.model.result['text']='answer BC'
        await self.runtime.reply_once()
        history=[(r['role'],r['content']) for r in self.model.calls[-1][0]]
        self.assertEqual(history,[('user','A'),('assistant','answer A'),('user','B'),('user','C')])
        self.assertEqual(self.model.calls[-1][1],'B\nC')
        self.assertEqual([r['content'] for r in self.runtime.store.history()],['A','B','C','answer A'])
        await self.runtime.send_once()
        self.assertEqual([r['content'] for r in self.runtime.store.conversation_history(30)],
                         ['A','answer A','B','C','answer BC'])

    async def test_real_model_request_ends_with_current_batch_after_previous_answer(self):
        await self.runtime.ingest(event(1,'A'));self.model.wait=asyncio.Event()
        first=asyncio.create_task(self.runtime.reply_once());await asyncio.sleep(0)
        await self.runtime.ingest(event(2,'B'));self.model.wait.set();await first
        await self.runtime.send_once()
        client=Client({'choices':[{'message':{'content':'answer B'}}]})
        self.runtime.model=QQModel(client,self.config,StickerCatalog(),context_builder=lambda *a,**k:'memory')
        await self.runtime.reply_once()
        messages=client.calls[0][1]['json']['messages']
        self.assertEqual([(m['role'],m['content']) for m in messages if m['role']!='system'],
                         [('user','A'),('assistant','测试回复'),('user','B')])

    async def test_history_read_failure_releases_input_for_retry(self):
        await self.runtime.ingest(event(1,'A'))
        with patch.object(self.runtime.store,'batch_history',side_effect=sqlite3.OperationalError('temporary')):
            with self.assertRaises(sqlite3.OperationalError): await self.runtime.reply_once()
        await self.runtime.reply_once()
        self.assertEqual(self.runtime.store.counts(),{'queued':1})
        self.assertFalse(self.runtime.store.pending())

    async def test_failed_queue_and_failed_cleanup_recover_without_restart(self):
        await self.runtime.ingest(event(1,'A'))
        self.runtime.store.conn.execute("CREATE TRIGGER fail_queue BEFORE INSERT ON outbox BEGIN SELECT RAISE(ABORT,'temporary'); END")
        with patch.object(self.runtime.store,'release_batch',side_effect=sqlite3.OperationalError('still unavailable')):
            with self.assertRaises(sqlite3.IntegrityError): await self.runtime.reply_once()
        self.runtime.store.conn.execute('DROP TRIGGER fail_queue')
        await self.runtime.reply_once();await self.runtime.send_once();await self.runtime.reply_once()
        self.assertEqual(len(self.transport.sent),1)
        self.assertFalse(self.runtime.store.pending())

    async def test_ambiguous_queue_commit_does_not_regenerate_or_send_twice(self):
        await self.runtime.ingest(event(1,'A'))
        queue=self.runtime.store.queue
        def committed(*args,**kwargs):
            queue(*args,**kwargs)
            raise sqlite3.OperationalError('connection lost after commit')
        with patch.object(self.runtime.store,'queue',side_effect=committed):
            with self.assertRaises(sqlite3.OperationalError): await self.runtime.reply_once()
        await self.runtime.reply_once();await self.runtime.send_once();await self.runtime.send_once()
        self.assertEqual(len(self.model.calls),1);self.assertEqual(len(self.transport.sent),1)

    async def test_cancelled_generation_releases_batch(self):
        await self.runtime.ingest(event(1,'A'));self.model.wait=asyncio.Event()
        task=asyncio.create_task(self.runtime.reply_once());await asyncio.sleep(0);task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.model.wait=None;await self.runtime.reply_once()
        self.assertEqual(self.runtime.store.counts(),{'queued':1})

    async def test_export_head_backoff_blocks_newer_rows(self):
        await self.prepare();delivered=[]
        def unavailable(*args): raise ConnectionError('offline')
        self.runtime.exporter=unavailable;await self.runtime.export_once()
        await self.runtime.ingest(event(2,'B'))
        self.runtime.exporter=lambda config,key,row:delivered.append(row['content'])
        await self.runtime.export_once();self.assertEqual(delivered,[])
        self.clock.advance(61);await self.runtime.export_once()
        self.assertEqual(delivered,['A','测试回复','B'])

    async def test_export_timeout_retains_one_rpc_without_blocking_private_chat(self):
        await self.prepare();release=threading.Event();calls=[]
        self.runtime.config=replace(self.config,export_timeout=.02)
        def slow(config,key,row):
            calls.append(row['id']);release.wait(2)
        self.runtime.exporter=slow
        try:
            await asyncio.wait_for(self.runtime.export_once(),.3)
            job=self.runtime.export_pending[1]
            await self.runtime.export_once()
            self.assertIs(self.runtime.export_pending[1],job);self.assertEqual(len(calls),1)
            await self.runtime.ingest(event(2,'B'))
            await asyncio.wait_for(self.runtime.reply_once(),.3)
            self.assertEqual(self.model.calls[-1][1],'B')
        finally:
            release.set()
            if self.runtime.export_pending:
                await self.runtime.export_pending[1]
                await self.runtime.export_once()
        await self.runtime.export_once()
        self.assertEqual(calls,sorted(set(calls)))
        self.assertEqual(self.runtime.store.exports(self.clock()),[])

    async def test_background_export_does_not_block_receive_generate_or_shutdown(self):
        await self.prepare();started=asyncio.Event();release=threading.Event()
        loop=asyncio.get_running_loop()
        def slow(*args):
            loop.call_soon_threadsafe(started.set);release.wait(2)
        self.runtime.exporter=slow
        task=asyncio.create_task(self.runtime._loop())
        try:
            await asyncio.wait_for(started.wait(),.5)
            await self.runtime.ingest(event(2,'B'))
            async def generated():
                while len(self.model.calls)<2: await asyncio.sleep(.01)
            await asyncio.wait_for(generated(),.8)
            task.cancel();await asyncio.wait_for(task,.3)
        except asyncio.CancelledError:
            self.assertTrue(task.cancelled())
        finally:
            release.set();task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def test_socket_write_timeout_is_unknown_and_next_connection_can_send(self):
        connection=OneBotConnection(replace(self.config,action_timeout=.03),self.runtime.ingest)
        socket=Socket();block=asyncio.Event()
        async def blocked_write(value): await block.wait()
        socket.send_json=blocked_write
        connection.socket=socket;connection.verified=connection.online=True;connection.last_seen=connection.now()
        self.runtime.transport=connection
        await self.runtime.ingest(event(1,'A'));await self.runtime.reply_once()
        await asyncio.wait_for(self.runtime.send_once(),.3)
        self.assertEqual(self.runtime.store.counts(),{'unknown':1})
        self.assertEqual([r['role'] for r in self.runtime.store.history()],['user'])
        self.runtime.transport=self.transport
        await self.runtime.ingest(event(2,'B'));await self.runtime.reply_once()
        await self.runtime.send_once();await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(self.runtime.store.counts(),{'confirmed':1,'unknown':1})
        if connection.close_task: await connection.close_task


class LegacyLedgerTests(unittest.TestCase):
    def test_existing_ledger_adds_claims_and_backfills_reply_links_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'old.sqlite3';store=QQStore(path,'scope')
            store.accept(1,'A',100);batch=store.batch(100,0,10)
            store.accept(2,'B',101)
            store.queue('reply:1:1','reply',[],'answer A',102,200,batch)
            task=store.claim_send(103);store.finish_send(task['id'],'confirmed',104,'1')
            store.conn.execute('DROP TABLE reply_inputs')
            store.conn.execute('ALTER TABLE inbox DROP COLUMN claim_owner')
            store.conn.execute("DELETE FROM meta WHERE key='reply_links_v1'");store.close()
            for _ in range(2):
                upgraded=QQStore(path,'scope')
                try:
                    upgraded.recover();batch=upgraded.batch(105,0,10)
                    self.assertEqual([r['content'] for r in upgraded.batch_history(batch,30)],['A','answer A','B'])
                    self.assertEqual(upgraded.conn.execute('SELECT count(*) FROM reply_inputs').fetchone()[0],1)
                finally: upgraded.close()


class OptionalStickerTests(unittest.TestCase):
    def test_missing_manifest_allows_plain_text_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(StickerCatalog(Path(tmp)/'missing.json').choices(),[])

    def test_missing_or_unreadable_entries_are_excluded_but_valid_one_remains(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'ok.gif').write_bytes(b'GIF89a'+b'x'*20)
            (root/'denied.gif').write_bytes(b'GIF89a'+b'x'*20)
            manifest=root/'stickers.json'
            manifest.write_text(json.dumps({'version':1,'stickers':[
                {'id':key,'description':key,'file':key+'.gif'} for key in ['missing','denied','ok']]}),encoding='utf-8')
            read=Path.read_bytes
            def read_file(path):
                if path.name=='denied.gif': raise PermissionError('unreadable asset')
                return read(path)
            with patch.object(Path,'read_bytes',read_file): catalog=StickerCatalog(manifest)
            self.assertEqual([r['id'] for r in catalog.choices()],['ok'])
            self.assertEqual(catalog.segment('ok')['type'],'image')

    def test_missing_external_path_still_rejected_and_duplicate_ids_not_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);manifest=root/'stickers.json'
            for items in [[{'id':'bad','description':'bad','file':'../missing.gif'}],
                          [{'id':'a','description':'a','file':'missing.gif'}]*2]:
                manifest.write_text(json.dumps({'version':1,'stickers':items}),encoding='utf-8')
                with self.subTest(items=items),self.assertRaises(ValueError): StickerCatalog(manifest)


class TransportFixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config=QQConfig(token='x'*32,account_id='12345',owner_id='67890',action_timeout=1)
        self.events=AsyncMock();self.connection=OneBotConnection(self.config,self.events)

    async def test_buffered_owner_events_precede_live_events_after_verification(self):
        socket=Socket();task=asyncio.create_task(self.connection.serve(socket))
        try:
            login=await socket.output.get()
            await socket.input.put(event(1,'A'));await socket.input.put(event(2,'B'))
            await socket.input.put(event(9,'wrong owner',user_id=99999))
            await socket.ack(login,{'user_id':12345})
            status=await socket.output.get();await socket.ack(status,{'online':True})
            await socket.input.put(event(3,'C'))
            for _ in range(20):
                if self.events.await_count==3: break
                await asyncio.sleep(.005)
            self.assertEqual([c.args[0]['message_id'] for c in self.events.await_args_list],[1,2,3])
        finally:
            await socket.close(1000);await asyncio.wait_for(task,.5)

    async def test_failed_login_discards_untrusted_buffer(self):
        socket=Socket();task=asyncio.create_task(self.connection.serve(socket))
        login=await socket.output.get();await socket.input.put(event(1,'A'))
        await socket.ack(login,{'user_id':99999});await asyncio.wait_for(task,.5)
        self.events.assert_not_awaited();self.assertFalse(self.connection.ready)

    async def test_handshake_buffer_has_a_finite_limit(self):
        socket=Socket();task=asyncio.create_task(self.connection.serve(socket))
        await socket.output.get()
        for mid in range(65): await socket.input.put(event(mid,'A'))
        await asyncio.wait_for(task,.5)
        self.assertIn(1013,socket.closed);self.events.assert_not_awaited()

    async def test_write_lock_timeout_is_retryable_without_any_socket_write(self):
        socket=Socket();self.connection.config=replace(self.config,action_timeout=.02)
        self.connection.socket=socket;self.connection.verified=self.connection.online=True
        self.connection.last_seen=self.connection.now()
        await self.connection.write_lock.acquire()
        try:
            with self.assertRaises(NotSubmitted): await self.connection.send([])
            self.assertTrue(socket.output.empty());self.assertEqual(self.connection.pending,{})
            self.assertTrue(self.connection.ready)
        finally: self.connection.write_lock.release()

    async def test_one_deadline_covers_both_write_and_receipt(self):
        socket=Socket();self.connection.config=replace(self.config,action_timeout=.1)
        self.connection.socket=socket;self.connection.verified=self.connection.online=True
        self.connection.last_seen=self.connection.now()
        async def write(value):
            await asyncio.sleep(.06);await socket.output.put(value)
        socket.send_json=write
        async def late_receipt():
            request=await socket.output.get();await asyncio.sleep(.08)
            future=self.connection.pending.get(request['echo'])
            if future and not future.done(): future.set_result({'status':'ok','retcode':0,'data':{'message_id':1}})
        receipt=asyncio.create_task(late_receipt())
        try:
            with self.assertRaises(TimeoutError): await self.connection.send([])
            self.assertFalse(self.connection.ready);self.assertEqual(self.connection.pending,{})
        finally:
            await receipt
            if self.connection.close_task: await self.connection.close_task
