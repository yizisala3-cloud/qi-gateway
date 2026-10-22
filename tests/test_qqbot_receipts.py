"""R8: retry receipt persistence only, never replay a completed network action."""
import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_store import QQStore
from gateway.qqbot_transport import NotSubmitted
from tests.test_qqbot import Clock, Model, Transport, event


class ReceiptRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.clock,self.model,self.transport=Clock(),Model(),Transport()
        self.config=QQConfig(enabled=True,account_id='12345',owner_id='67890',assistant_id='role',
            token='x'*32,store_path=str(Path(self.tmp.name)/'ledger.sqlite3'),debounce_seconds=0,
            proactive_enabled=True,quiet_seconds=10,cooldown_seconds=60,check_seconds=60)
        self.runtime=self.make_runtime()
        self.addCleanup(self.close_store)
        self.clock.on_advance=lambda:self.runtime.store.acquire(self.runtime.owner,self.clock())

    def make_runtime(self):
        runtime=QQRuntime(self.config,model=self.model,transport=self.transport,now=self.clock,exporter=lambda *a:None)
        runtime.activate();return runtime

    def close_store(self):
        if not self.runtime.stopped: self.runtime.store.close()

    async def queue_reply(self,mid=1):
        await self.runtime.ingest(event(mid,str(mid)));await self.runtime.reply_once()

    def fail_outcome(self,status='confirmed'):
        self.runtime.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox WHEN OLD.status='in_flight' AND NEW.status='"+status+"' BEGIN SELECT RAISE(ABORT,'temporary result write failure'); END")

    def unblock(self): self.runtime.store.conn.execute('DROP TRIGGER block_result')

    async def test_all_submitted_outcomes_recover_offline_without_resend(self):
        expected={}
        for mid,status in enumerate(['confirmed','failed','unknown'],1):
            with self.subTest(status=status):
                self.transport.ready=True
                self.transport.result=(status,'ack-'+str(mid) if status=='confirmed' else None,'reason')
                await self.queue_reply(mid);self.fail_outcome(status)
                received=self.clock()
                with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
                self.assertEqual(len(self.transport.sent),mid)
                self.transport.ready=False;self.clock.advance(60);self.unblock()
                for _ in range(3): await self.runtime.send_once()
                expected[status]=expected.get(status,0)+1
                self.assertEqual(self.runtime.store.counts(),expected)
                self.assertEqual(len(self.transport.sent),mid)
                self.assertEqual(self.runtime.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0],0)
                history=[r for r in self.runtime.store.history() if r['role']=='assistant']
                self.assertEqual(len(history),1)
                if status=='confirmed': self.assertEqual(history[0]['created'],received)

    async def test_not_submitted_result_recovers_to_queue_before_single_real_send(self):
        attempts,wire=[],[]
        async def send(payload):
            attempts.append(payload)
            if len(attempts)==1: raise NotSubmitted('not written')
            wire.append(payload);return 'confirmed','ack',None
        self.transport.send=send
        await self.queue_reply();self.fail_outcome('queued')
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.unblock();self.transport.ready=False;await self.runtime.send_once()
        self.assertEqual(self.runtime.store.counts(),{'queued':1})
        self.assertEqual(wire,[]);self.assertEqual(len(attempts),1)
        self.transport.ready=True
        for _ in range(3): await self.runtime.send_once()
        self.assertEqual(len(wire),1);self.assertEqual(len(attempts),2)
        self.assertEqual(self.runtime.store.counts(),{'confirmed':1})

    async def test_receipt_failure_blocks_new_network_actions_until_local_repair(self):
        await self.queue_reply();self.fail_outcome()
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        await self.queue_reply(2)
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual(self.runtime.store.counts(),{'in_flight':1,'queued':1})
        self.unblock()
        for _ in range(3): await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent),2)
        self.assertEqual(self.runtime.store.counts(),{'confirmed':2})
        self.assertEqual(len([r for r in self.runtime.store.history() if r['role']=='assistant']),2)

    async def test_proactive_receipt_keeps_original_day_time_and_single_count(self):
        await self.queue_reply();await self.runtime.send_once();self.clock.advance(120)
        await self.runtime.proactive_once();self.fail_outcome();received=self.clock()
        day=datetime.fromtimestamp(received,ZoneInfo(self.config.timezone)).date().isoformat()
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.clock.advance(2*86400);self.unblock();self.transport.ready=False
        for _ in range(3): await self.runtime.send_once()
        state=self.runtime.store.state()
        self.assertEqual((state['day'],state['count'],state['last_sent'],state['next_check']),
                         (day,1,received,received+60))
        self.assertFalse(self.runtime.store.open_proactive());self.assertEqual(len(self.transport.sent),2)
        self.transport.ready=True;await self.runtime.proactive_once();await self.runtime.send_once()
        self.assertEqual(len(self.transport.sent),3);self.assertEqual(self.runtime.store.state()['count'],1)

    async def test_owner_response_after_receipt_keeps_unanswered_reset(self):
        await self.queue_reply();await self.runtime.send_once();self.clock.advance(120)
        await self.runtime.proactive_once();self.fail_outcome()
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.clock.advance(1);await self.runtime.ingest(event(2,'received'))
        self.unblock();self.transport.ready=False;await self.runtime.send_once()
        self.assertEqual(self.runtime.store.state()['count'],1)
        self.assertEqual(self.runtime.store.state()['unanswered'],0)

    async def test_journal_write_failure_retains_live_receipt_and_recovery_does_not_downgrade(self):
        await self.queue_reply()
        self.runtime.store.conn.execute("CREATE TRIGGER block_journal BEFORE INSERT ON send_receipts BEGIN SELECT RAISE(ABORT,'temporary journal failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        self.runtime.store.recover(self.clock(),'today')
        self.assertEqual(self.runtime.store.counts(),{'in_flight':1})
        self.assertEqual(self.runtime.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0],0)
        self.runtime.store.conn.execute('DROP TRIGGER block_journal')
        self.transport.ready=False;await self.runtime.send_once()
        self.assertEqual(self.runtime.store.counts(),{'confirmed':1});self.assertEqual(len(self.transport.sent),1)

    async def test_durable_receipt_survives_stop_and_restart_while_finalization_is_blocked(self):
        await self.queue_reply();self.fail_outcome()
        with self.assertRaises(sqlite3.IntegrityError): await self.runtime.send_once()
        await self.runtime.stop()
        # The outbox trigger still blocks finalization; activation must preserve
        # the known result instead of replacing it with restart_in_flight.
        self.runtime=self.make_runtime()
        self.assertEqual(self.runtime.store.counts(),{'in_flight':1})
        self.assertEqual(self.runtime.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0],1)
        self.unblock();await self.runtime.send_once()
        self.assertEqual(self.runtime.store.counts(),{'confirmed':1})
        self.assertEqual(len([r for r in self.runtime.store.history() if r['role']=='assistant']),1)
        self.assertEqual(len(self.transport.sent),1)

    async def test_cancelled_network_send_can_repair_unknown_receipt_without_resend(self):
        started=asyncio.Event()
        async def blocked(payload):
            self.transport.sent.append(payload);started.set();await asyncio.Event().wait()
        self.transport.send=blocked
        await self.queue_reply();self.fail_outcome('unknown')
        task=asyncio.create_task(self.runtime.send_once());await started.wait();task.cancel()
        with self.assertRaises(sqlite3.IntegrityError): await task
        self.unblock();self.transport.ready=False;await self.runtime.send_once()
        self.assertEqual(self.runtime.store.counts(),{'unknown':1})
        self.assertEqual(len(self.transport.sent),1)
        self.assertEqual([r['role'] for r in self.runtime.store.history()],['user'])


class ReceiptTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=QQStore(Path(self.tmp.name)/'ledger.sqlite3','scope');self.addCleanup(self.store.close)

    def claim(self,kind='reply',key='task'):
        self.store.queue(key,kind,[],'actual response',100,300,revision=0)
        return self.store.claim_send(101)['id']

    def test_assistant_insert_failure_rolls_back_terminal_and_replays_once(self):
        task=self.claim()
        self.store.conn.execute("CREATE TRIGGER block_history BEFORE INSERT ON history WHEN NEW.role='assistant' BEGIN SELECT RAISE(ABORT,'temporary history failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.finish_send(task,'confirmed',102,'ack')
        self.assertEqual(self.store.counts(),{'in_flight':1});self.assertEqual(self.store.history(),[])
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0],1)
        self.store.conn.execute('DROP TRIGGER block_history')
        for _ in range(3): self.store.retry_receipts()
        self.assertEqual(self.store.counts(),{'confirmed':1});self.assertEqual(len(self.store.history()),1)
        self.assertEqual(self.store.history()[0]['created'],102)

    def test_proactive_state_failure_keeps_history_and_count_atomic(self):
        task=self.claim('proactive')
        self.store.conn.execute("CREATE TRIGGER block_state BEFORE INSERT ON meta WHEN NEW.key='schedule' BEGIN SELECT RAISE(ABORT,'temporary schedule failure'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.finish_send(task,'confirmed',102,'ack',day='today',cooldown=60)
        self.assertEqual(self.store.counts(),{'in_flight':1});self.assertEqual(self.store.history(),[])
        self.assertEqual(self.store.state()['count'],0)
        self.store.conn.execute('DROP TRIGGER block_state')
        for _ in range(3): self.store.retry_receipts()
        self.assertEqual(len(self.store.history()),1)
        self.assertEqual((self.store.state()['count'],self.store.state()['next_check']),(1,162))

    def test_post_commit_error_is_idempotent_for_history_and_proactive_count(self):
        task=self.claim('proactive');apply=self.store._apply_receipt
        def committed(**receipt):
            apply(**receipt);raise sqlite3.OperationalError('lost local commit response')
        with patch.object(self.store,'_apply_receipt',side_effect=committed):
            with self.assertRaises(sqlite3.OperationalError): self.store.finish_send(task,'confirmed',102,'ack',day='today')
        for _ in range(3): self.store.retry_receipts()
        self.assertEqual(self.store.counts(),{'confirmed':1});self.assertEqual(len(self.store.history()),1)
        self.assertEqual(self.store.state()['count'],1)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0],0)

    def test_first_observed_receipt_is_not_overwritten_by_a_later_retry(self):
        task=self.claim()
        self.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT,'temporary'); END")
        for status,mid,now in [('confirmed','original',102),('unknown',None,120)]:
            with self.subTest(status=status),self.assertRaises(sqlite3.IntegrityError):
                self.store.finish_send(task,status,now,mid)
        self.store.conn.execute('DROP TRIGGER block_result');self.store.retry_receipts()
        row=self.store.conn.execute('SELECT status,provider_id FROM outbox').fetchone()
        self.assertEqual(tuple(row),('confirmed','original'));self.assertEqual(self.store.history()[0]['created'],102)

    def test_recovery_preserves_known_receipt_but_marks_unrelated_interruption_unknown(self):
        known=self.claim('proactive','known');self.claim('proactive','interrupted')
        self.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox WHEN NEW.status='confirmed' BEGIN SELECT RAISE(ABORT,'temporary'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.store.finish_send(known,'confirmed',102,'ack',day='today')
        for _ in range(2): self.store.recover(103,'today')
        self.assertEqual(self.store.counts(),{'in_flight':1,'unknown':1})
        self.assertEqual(self.store.state()['count'],1)
        self.store.conn.execute('DROP TRIGGER block_result');self.store.retry_receipts()
        self.assertEqual(self.store.counts(),{'confirmed':1,'unknown':1})
        self.assertEqual(self.store.state()['count'],2);self.assertEqual(self.store.state()['last_sent'],103)
