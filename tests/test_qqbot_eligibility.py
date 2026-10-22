"""R9: owner intent remains authoritative across all unsubmitted-send recovery."""
import asyncio
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_store import QQStore
from gateway.qqbot_transport import NotSubmitted
from tests.test_qqbot import Clock, Model, Transport, event
from tests.test_qqbot_transport import Socket


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock,self.model,self.transport=Clock(),Model(),Transport()
        self.config=QQConfig(enabled=True,account_id='12345',owner_id='67890',assistant_id='role',
            token='x'*32,store_path=str(Path(self.tmp.name)/'ledger.sqlite3'),debounce_seconds=0,
            proactive_enabled=True,quiet_seconds=10,cooldown_seconds=60,check_seconds=60)
        self.runtime=self.make_runtime();self.addCleanup(self.close_store)
        self.clock.on_advance=lambda:self.runtime.store.acquire(self.runtime.owner,self.clock())
        self.wire=[];self.attempts=0

    def make_runtime(self,real=False):
        runtime=QQRuntime(self.config,model=self.model,transport=None if real else self.transport,
                          now=self.clock,exporter=lambda *a:None)
        runtime.activate();return runtime

    def close_store(self):
        if not self.runtime.stopped: self.runtime.store.close()

    async def queued_greeting(self):
        await self.runtime.ingest(event(1,'A'));await self.runtime.reply_once();await self.runtime.send_once()
        self.clock.advance(120);self.model.result['text']='old greeting'
        await self.runtime.proactive_once()
        row=self.runtime.store.conn.execute("SELECT id FROM outbox WHERE kind='proactive'").fetchone()
        return row['id']

    async def unsubmitted_receipt(self,block='outbox'):
        task=await self.queued_greeting()
        async def send(payload):
            self.attempts+=1
            if self.attempts==1: raise NotSubmitted('not written')
            self.wire.append(payload);return 'confirmed','ack',None
        self.transport.send=send
        if block=='outbox':
            self.runtime.store.conn.execute("CREATE TRIGGER block_retry BEFORE UPDATE ON outbox WHEN OLD.status='in_flight' AND NEW.status='queued' BEGIN SELECT RAISE(ABORT,'temporary'); END")
        else:
            self.runtime.store.conn.execute("CREATE TRIGGER block_retry BEFORE INSERT ON send_receipts BEGIN SELECT RAISE(ABORT,'temporary'); END")
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.assertEqual(self.wire,[])
        return task

    def old_task(self,task):
        return dict(self.runtime.store.conn.execute('SELECT * FROM outbox WHERE id=?',(task,)).fetchone())

    async def drain(self):
        for _ in range(5): await self.runtime.send_once()

    def assert_cancelled(self,task):
        row=self.old_task(task)
        self.assertEqual(row['status'],'failed');self.assertEqual(row['payload'],'[]')
        self.assertFalse(any(s['type']=='text' and s['data']['text']=='old greeting'
            for payload in self.wire for s in payload))
        self.assertFalse(self.runtime.store.open_proactive())
        self.assertEqual(self.runtime.store.state()['count'],0)
        self.assertEqual(self.runtime.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0],0)

    async def test_pause_then_resume_does_not_resurrect_recovered_greeting(self):
        task=await self.unsubmitted_receipt()
        self.clock.advance(1);await self.runtime.ingest(event(2,'暂停主动消息'))
        await self.runtime.ingest(event(3,'恢复主动消息'))
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assert_cancelled(task);self.assertFalse(self.runtime.store.state()['paused'])
        self.assertEqual(len(self.wire),2)

    async def test_today_silence_then_resume_does_not_resurrect_greeting(self):
        task=await self.unsubmitted_receipt()
        self.clock.advance(1);await self.runtime.ingest(event(2,'今天先别找我'))
        await self.runtime.ingest(event(3,'恢复主动消息'))
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assert_cancelled(task);self.assertEqual(self.runtime.store.state()['silence_until'],0)
        self.assertEqual(len(self.wire),2)

    async def test_completed_new_input_still_invalidates_old_generation(self):
        task=await self.unsubmitted_receipt()
        self.clock.advance(1);await self.runtime.ingest(event(2,'new question'))
        self.model.result['text']='new answer';await self.runtime.reply_once()
        self.assertFalse(self.runtime.store.pending())
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assert_cancelled(task)
        self.assertEqual(self.wire,[[{'type':'text','data':{'text':'new answer'}}]])

    async def restart_after_change(self,text,processed=False):
        task=await self.unsubmitted_receipt()
        self.clock.advance(1);await self.runtime.ingest(event(2,text))
        if processed:
            self.model.result['text']='new answer';await self.runtime.reply_once()
        await self.runtime.stop();self.runtime=self.make_runtime();self.transport.ready=True
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assert_cancelled(task);self.assertEqual(len(self.wire),1)

    async def test_restart_does_not_restore_unsubmitted_greeting_after_owner_pause(self):
        await self.restart_after_change('暂停主动消息')

    async def test_restart_does_not_restore_unsubmitted_greeting_after_silence(self):
        await self.restart_after_change('今天先别找我')

    async def test_restart_does_not_restore_greeting_after_completed_new_question(self):
        await self.restart_after_change('new question',processed=True)

    async def test_missing_journal_uses_live_revision_when_db_returns(self):
        task=await self.unsubmitted_receipt('journal')
        self.clock.advance(1);await self.runtime.ingest(event(2,'new question'))
        self.model.result['text']='new answer';await self.runtime.reply_once()
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assert_cancelled(task);self.assertEqual(len(self.wire),1)

    async def test_expired_unsubmitted_receipt_is_not_requeued_on_recovery(self):
        task=await self.unsubmitted_receipt()
        self.clock.advance(self.config.proactive_ttl+1)
        self.runtime.store.conn.execute('DROP TRIGGER block_retry')
        self.transport.ready=False;await self.runtime.send_once()
        self.assert_cancelled(task);self.assertEqual(self.old_task(task)['error'],'expired')
        self.transport.ready=True;await self.drain();self.assertEqual(self.wire,[])

    async def test_valid_unsubmitted_greeting_retries_once_without_owner_changes(self):
        task=await self.unsubmitted_receipt()
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assertEqual(self.old_task(task)['status'],'confirmed')
        self.assertEqual(len(self.wire),1);self.assertEqual(self.attempts,2)
        self.assertEqual(self.runtime.store.state()['count'],1)

    async def test_restart_valid_receipt_keeps_generation_revision(self):
        task=await self.unsubmitted_receipt()
        revision=self.old_task(task)['eligibility_revision']
        await self.runtime.stop();self.runtime=self.make_runtime();self.transport.ready=True
        self.runtime.store.conn.execute('DROP TRIGGER block_retry');await self.drain()
        self.assertEqual(self.old_task(task)['eligibility_revision'],revision)
        self.assertEqual(self.old_task(task)['status'],'confirmed');self.assertEqual(len(self.wire),1)

    async def test_submitted_confirmed_or_unknown_keep_truth_after_owner_pause(self):
        task=await self.queued_greeting()
        self.transport.result=('confirmed','actual-ack',None)
        self.runtime.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT,'temporary'); END")
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.clock.advance(1);await self.runtime.ingest(event(2,'暂停主动消息'))
        self.runtime.store.conn.execute('DROP TRIGGER block_result');self.transport.ready=False
        await self.runtime.send_once()
        self.assertEqual(self.old_task(task)['status'],'confirmed')
        self.assertEqual(self.runtime.store.history()[-1]['content'],'old greeting')
        self.assertEqual(self.runtime.store.state()['count'],1);self.assertEqual(len(self.transport.sent),2)

    async def test_unknown_receipt_is_not_cancelled_or_replayed_after_new_input(self):
        task=await self.queued_greeting();self.transport.result=('unknown',None,'async')
        self.runtime.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox WHEN NEW.status='unknown' BEGIN SELECT RAISE(ABORT,'temporary'); END")
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.clock.advance(1);await self.runtime.ingest(event(2,'new input'))
        self.runtime.store.conn.execute('DROP TRIGGER block_result');self.transport.ready=False
        await self.runtime.send_once()
        self.assertEqual(self.old_task(task)['status'],'unknown')
        self.assertEqual(self.runtime.store.state()['count'],1);self.assertEqual(len(self.transport.sent),2)
        self.assertEqual(len([r for r in self.runtime.store.history() if r['role']=='assistant']),1)

    async def with_write_lock(self,change,expected_count=0):
        await self.runtime.stop();self.runtime=self.make_runtime(real=True)
        # Establish history using the fake transport, then exercise the real
        # OneBot write lock and its last-moment permission callback.
        connection=self.runtime.transport;self.runtime.transport=self.transport;self.transport.ready=True
        task=await self.queued_greeting();self.runtime.transport=connection
        socket=Socket();connection.socket=socket;connection.verified=connection.online=True;connection.last_seen=self.clock()
        await connection.write_lock.acquire()
        sending=asyncio.create_task(self.runtime.send_once());await asyncio.sleep(0)
        self.assertEqual(self.old_task(task)['status'],'in_flight')
        try:
            await change()
        finally: connection.write_lock.release()
        await asyncio.wait_for(sending,.5)
        self.assertEqual(self.old_task(task)['status'],'failed')
        self.assertTrue(socket.output.empty());self.assertTrue(connection.ready)
        self.assertEqual(self.old_task(task)['payload'],'[]')
        self.assertEqual(self.runtime.store.state()['count'],expected_count)

    async def test_pause_while_waiting_for_write_lock_prevents_socket_write(self):
        async def change(): await self.runtime.ingest(event(2,'暂停主动消息'))
        await self.with_write_lock(change)

    async def test_new_input_processed_during_write_lock_prevents_old_greeting(self):
        async def change():
            await self.runtime.ingest(event(2,'new question'))
            self.model.result['text']='new answer';await self.runtime.reply_once()
            self.assertFalse(self.runtime.store.pending())
        await self.with_write_lock(change)

    async def test_expiry_while_waiting_for_write_lock_prevents_socket_write(self):
        async def change():
            self.clock.advance(self.config.proactive_ttl+1)
            # The protocol peer remains alive and continues heartbeats while its
            # writer is blocked; isolate message expiry from heartbeat expiry.
            self.runtime.transport.last_seen=self.clock()
        await self.with_write_lock(change)

    async def test_window_end_while_waiting_for_write_lock_prevents_socket_write(self):
        async def change(): self.runtime.config=replace(self.config,active_start='01:00',active_end='02:00')
        await self.with_write_lock(change)

    async def test_disable_proactive_while_waiting_for_lock_prevents_socket_write(self):
        async def change(): self.runtime.config=replace(self.config,proactive_enabled=False)
        await self.with_write_lock(change)

    async def test_quiet_period_changed_before_submission_is_rechecked(self):
        async def change(): self.runtime.config=replace(self.config,quiet_seconds=86400)
        await self.with_write_lock(change)

    async def test_daily_limit_changed_before_submission_is_rechecked(self):
        async def change():
            self.runtime.config=replace(self.config,daily_limit=1)
            with self.runtime.store.transaction():
                state=self.runtime.store.state()
                state.update(count=1,day=datetime.fromtimestamp(self.clock(),ZoneInfo(self.config.timezone)).date().isoformat())
                self.runtime.store._save_state(state)
        await self.with_write_lock(change,expected_count=1)

    async def submitted_before_pause(self,outcome):
        await self.runtime.stop();self.runtime=self.make_runtime(real=True)
        connection=self.runtime.transport;self.runtime.transport=self.transport;self.transport.ready=True
        task=await self.queued_greeting();self.runtime.transport=connection
        socket=Socket();connection.socket=socket;connection.verified=connection.online=True;connection.last_seen=self.clock()
        packets=[]
        async def write(packet):
            packets.append(packet)
            self.clock.advance(1);await self.runtime.ingest(event(2,'暂停主动消息'))
            connection.pending[packet['echo']].set_result({'status':'ok' if outcome=='confirmed' else 'async',
                'retcode':0,'data':{'message_id':42} if outcome=='confirmed' else {}})
        socket.send_json=write
        await self.runtime.send_once()
        self.assertEqual(len(packets),1);self.assertEqual(self.old_task(task)['status'],outcome)
        self.assertTrue(self.runtime.store.state()['paused']);self.assertEqual(self.runtime.store.state()['count'],1)
        assistants=[r for r in self.runtime.store.history() if r['role']=='assistant']
        self.assertEqual(len(assistants),2 if outcome=='confirmed' else 1)

    async def test_confirmed_after_socket_submission_retains_actual_history(self):
        await self.submitted_before_pause('confirmed')

    async def test_async_unknown_after_socket_submission_is_not_revoked_or_retried(self):
        await self.submitted_before_pause('unknown')


class ClaimEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'ledger.sqlite3'
        self.store=QQStore(self.path,'scope');self.addCleanup(self.store.close)

    def test_claim_discards_silenced_task_and_continues_with_private_reply(self):
        self.store.queue('old','proactive',[],'old',100,300,revision=0)
        # Simulate an old writer restoring a row without respecting controls.
        self.store.control('today',101,200)
        self.store.conn.execute("UPDATE outbox SET status='queued' WHERE task_key='old'")
        self.store.queue('reply','reply',[],'normal reply',102,300)
        task=self.store.claim_send(103)
        self.assertEqual(task['task_key'],'reply')
        self.assertEqual(self.store.conn.execute("SELECT status FROM outbox WHERE task_key='old'").fetchone()[0],'failed')

    def test_legacy_unknown_revision_never_revives_but_known_confirmation_survives(self):
        self.store.queue('queued','proactive',[],'old queued',100,300,revision=0)
        self.store.queue('sent','proactive',[],'already sent',100,300,revision=0)
        self.store.conn.execute("UPDATE outbox SET status='in_flight' WHERE task_key='sent'")
        sent=self.store.conn.execute("SELECT id FROM outbox WHERE task_key='sent'").fetchone()[0]
        self.store.conn.execute('ALTER TABLE outbox DROP COLUMN eligibility_revision')
        # A second connection upgrades in place; no missing snapshot is invented.
        upgraded=QQStore(self.path,'scope')
        try:
            self.assertIsNone(upgraded.claim_send(102))
            upgraded.finish_send(sent,'confirmed',103,'ack')
            self.assertEqual(upgraded.counts(),{'failed':1,'confirmed':1})
            self.assertEqual(upgraded.history()[0]['content'],'already sent')
        finally: upgraded.close()
