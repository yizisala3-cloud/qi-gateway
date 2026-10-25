"""Receipt, cancellation and lease boundaries of proactive evaluation recovery."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_proactive import QQModelHTTPError
from tests.test_qqbot import Clock, Model, Transport, event


class RecoveryBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case_number = 0
        await self.prepare_case()

    def runtime(self, config, clock, model=None, transport=None):
        runtime = QQRuntime(config, model=model or Model(), transport=transport or Transport(),
            now=clock, exporter=lambda *args: None)
        self.addAsyncCleanup(runtime.stop)
        runtime.activate()
        return runtime

    async def prepare_case(self):
        self.case_number += 1
        self.clock, self.model, self.transport = Clock(), Model(), Transport()
        self.config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.tmp.name)/f'ledger-{self.case_number}.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.bot = self.runtime(self.config, self.clock, self.model, self.transport)
        self.clock.on_advance = lambda: self.bot.store.acquire(self.bot.owner, self.clock())
        await self.bot.ingest(event(1, 'initial chat'))
        self.clock.advance(4)
        await self.bot.reply_once()
        await self.bot.send_once()
        self.clock.advance(2)
        self.transport.sent.clear()

    async def renew_once(self, bot=None):
        renewal = asyncio.create_task((bot or self.bot)._lease_loop())
        await asyncio.sleep(0)
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)

    def corrupt(self, raw):
        self.bot.store.conn.execute("INSERT OR REPLACE INTO meta VALUES('proactive_evaluation',?)", (raw,))

    async def failures(self, count):
        async def failed(*args, **kwargs):
            raise httpx.ReadTimeout('isolated timeout')
        self.model.generate = failed
        for _ in range(count):
            await self.bot.proactive_once()
            self.clock.advance(self.bot.store.evaluation_state()['next_check_at']-self.clock())

    async def test_malformed_evaluation_at_second_submit_does_not_block_private_send_or_stop(self):
        for index, raw in enumerate(['{broken', '[]', '{"failure_streak":-1}', '{"last_error_code":[]}',
                                    '{"next_check_at":'+'9'*999+'}']):
            with self.subTest(raw=raw):
                if index:
                    await self.prepare_case()
                await self.bot.proactive_once()
                entered, release = asyncio.Event(), asyncio.Event()

                async def waiting(segments):
                    entered.set()
                    await release.wait()
                    self.bot._before_submit()
                    self.fail('corrupt proactive task reached the wire')

                self.transport.send = waiting
                sending = asyncio.create_task(self.bot.send_once())
                await entered.wait()
                self.corrupt(raw)
                release.set()
                await sending
                task = self.bot.store.conn.execute("SELECT status,error FROM outbox WHERE kind='proactive'").fetchone()
                self.assertEqual(tuple(task), ('failed', 'evaluation_state_unavailable'))
                self.assertEqual(self.bot.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0], 0)
                self.assertTrue(await self.bot.ingest(event(2, 'ordinary private chat')))
                self.clock.advance(4)
                await self.bot.reply_once()
                self.transport.send = Transport.send.__get__(self.transport, Transport)
                await self.bot.send_once()
                self.assertEqual(len(self.transport.sent), 1)
                self.assertTrue(self.bot.evaluation_status()['fault_stopped'])
                self.assertEqual(self.bot.store.conn.execute("SELECT value FROM meta WHERE key='proactive_evaluation'").fetchone()[0], raw)
                await self.bot.stop()
                restarted = self.runtime(self.config, self.clock)
                self.assertTrue(restarted.evaluation_status()['fault_stopped'])

    async def test_queued_receipt_with_bad_evaluation_is_settled_on_restart(self):
        await self.bot.proactive_once()
        task = self.bot.store.claim_send(self.clock(), owner=self.bot.owner)
        self.bot.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT,'isolated write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.bot.store.finish_send(task['id'], 'queued', self.clock(), error='not_submitted')
        self.bot.store.conn.execute('DROP TRIGGER block_result')
        self.corrupt('{broken')
        copied = Path(self.tmp.name)/'queued-crash.sqlite3'
        with closing(sqlite3.connect(copied)) as destination:
            self.bot.store.conn.backup(destination)
        restored = self.runtime(replace(self.config, store_path=str(copied)), self.clock)
        # The copied live lease belongs to the old process until it expires.
        self.clock.on_advance = lambda: None
        self.clock.advance(31)
        self.assertTrue(restored.activate())
        task = restored.store.conn.execute("SELECT status,error FROM outbox WHERE kind='proactive'").fetchone()
        self.assertEqual(tuple(task), ('failed', 'evaluation_state_unavailable'))
        self.assertEqual(restored.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0], 0)
        self.assertTrue(await restored.ingest(event(2, 'ordinary private chat')))
        self.clock.on_advance = lambda: restored.store.acquire(restored.owner, self.clock())
        self.clock.advance(4)
        await restored.reply_once()
        await restored.send_once()
        self.assertEqual(len(restored.transport.sent), 1)
        self.assertTrue(restored.evaluation_status()['fault_stopped'])

    async def test_bad_evaluation_does_not_change_confirmed_unknown_receipt_accounting(self):
        for index, status in enumerate(['confirmed', 'unknown']):
            with self.subTest(status=status):
                if index:
                    await self.prepare_case()
                await self.bot.proactive_once()
                task = self.bot.store.claim_send(self.clock(), owner=self.bot.owner)
                self.bot.store.conn.execute("CREATE TRIGGER block_result BEFORE UPDATE ON outbox BEGIN SELECT RAISE(ABORT,'isolated write failure'); END")
                with self.assertRaises(sqlite3.IntegrityError):
                    self.bot.store.finish_send(task['id'], status, self.clock(), provider_id='ack' if status=='confirmed' else None, day='test-day')
                self.corrupt('{broken')
                self.bot.store.conn.execute('DROP TRIGGER block_result')
                for _ in range(3):
                    self.bot.store.retry_receipts(self.clock())
                self.assertEqual(self.bot.store.state()['count'], 1)
                history = [row for row in self.bot.store.history() if row['role']=='assistant']
                self.assertEqual(len(history), 2 if status=='confirmed' else 1)
                self.assertEqual(self.transport.sent, [])

    async def test_cancelled_crash_recovery_preserves_prior_failures_and_silent_state(self):
        for index, (failures, cancel) in enumerate([(n, action) for n in (0, 4) for action in ('input', 'settings')]):
            with self.subTest(failures=failures, cancel=cancel):
                if index:
                    await self.prepare_case()
                await self.failures(failures)
                before = self.bot.store.evaluation_state()
                before['silent_streak'] = 2
                self.bot.store._save_evaluation(before)
                entered = asyncio.Event()

                async def waiting(*args, **kwargs):
                    entered.set()
                    await asyncio.Event().wait()

                self.model.generate = waiting
                pending = asyncio.create_task(self.bot.proactive_once())
                await entered.wait()
                if cancel=='input':
                    await self.bot.ingest(event(2, 'cancel the attempt'))
                else:
                    self.bot.store.invalidate_proactive('settings_changed')
                    self.bot.proactive_generation.cancel()
                copied = Path(self.tmp.name)/f'cancel-{index}.sqlite3'
                with closing(sqlite3.connect(copied)) as destination:
                    self.bot.store.conn.backup(destination)
                await pending
                await self.bot.stop()
                self.clock.on_advance = lambda: None
                self.clock.advance(31)
                with self.assertNoLogs('gateway.qqbot', level='WARNING'):
                    restored = self.runtime(replace(self.config, store_path=str(copied)), self.clock)
                state = restored.evaluation_status()
                self.assertFalse(state['fault_stopped'])
                self.assertFalse(state['in_flight'])
                self.assertEqual(state['failure_streak'], failures)
                self.assertEqual(state['silent_streak'], 0 if cancel=='input' else 2)
                self.assertEqual(state['last_error_code'], before['last_error_code'])
                self.assertEqual(state['next_check_at']-self.clock(), 300)

    async def test_same_owner_expired_completion_is_recovered_and_future_calls_run(self):
        self.clock.on_advance = lambda: None

        async def late(*args, **kwargs):
            self.clock.advance(31)
            return {'send':False, 'text':'', 'sticker_ids':[]}

        self.model.generate = late
        await self.bot.proactive_once()
        self.assertTrue(self.bot.evaluation_status()['recovery_pending'])
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['recovery_pending'])
        self.assertEqual(state['failure_streak'], 0)
        self.assertEqual(state['silent_streak'], 1)
        self.assertEqual(state['next_check_at']-self.clock(), 600)
        self.clock.on_advance = lambda: self.bot.store.acquire(self.bot.owner, self.clock())
        self.clock.advance(600)
        self.bot.model = Model()
        self.bot.model.result = {'send':False, 'text':'', 'sticker_ids':[]}
        await self.bot.proactive_once()
        self.assertEqual(self.bot.evaluation_status()['failure_streak'], 0)
        self.assertEqual(len(self.bot.model.calls), 1)

    async def test_lease_gap_on_fifth_attempt_stops_and_allows_manual_resume(self):
        await self.failures(4)
        self.clock.on_advance = lambda: None

        async def late(*args, **kwargs):
            self.clock.advance(31)
            raise httpx.ReadTimeout('isolated late timeout')

        self.model.generate = late
        await self.bot.proactive_once()
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertTrue(state['fault_stopped'])
        self.assertEqual(state['failure_streak'], 5)
        self.bot.resume_evaluation(state['fault_version'])
        self.assertFalse(self.bot.evaluation_status()['fault_stopped'])
        self.assertEqual(self.bot.evaluation_status()['next_check_at']-self.clock(), 300)

    async def test_renewal_during_live_model_does_not_recover_that_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'send':False, 'text':'', 'sticker_ids':[]}

        self.model.generate = waiting
        pending = asyncio.create_task(self.bot.proactive_once())
        await entered.wait()
        attempt = self.bot.store.evaluation_state()['attempt_id']
        self.clock.on_advance = lambda: None
        self.clock.advance(31)
        await self.renew_once()
        self.assertEqual(self.bot.store.evaluation_state()['attempt_id'], attempt)
        self.assertTrue(self.bot.evaluation_status()['in_flight'])
        self.assertFalse(self.bot.evaluation_status()['recovery_pending'])
        self.assertEqual(self.bot.evaluation_status()['failure_streak'], 0)
        release.set()
        await pending
        self.assertFalse(self.bot.evaluation_status()['in_flight'])
        self.assertEqual(self.bot.evaluation_status()['silent_streak'], 1)

    async def test_old_owner_late_result_and_stop_do_not_recover_new_owners_live_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def late_error(*args, **kwargs):
            entered.set()
            await release.wait()
            raise QQModelHTTPError(401)

        self.model.generate = late_error
        pending = asyncio.create_task(self.bot.proactive_once())
        await entered.wait()
        self.clock.on_advance = lambda: None
        self.clock.advance(31)
        replacement = self.runtime(self.config, self.clock)
        self.clock.on_advance = lambda: replacement.store.acquire(replacement.owner, self.clock())
        self.clock.advance(300)
        active, finish = asyncio.Event(), asyncio.Event()

        async def live(*args, **kwargs):
            active.set()
            await finish.wait()
            return {'send':False, 'text':'', 'sticker_ids':[]}

        replacement.model.generate = live
        current = asyncio.create_task(replacement.proactive_once())
        await active.wait()
        attempt = replacement.store.evaluation_state()['attempt_id']
        release.set()
        await pending
        self.assertTrue(self.bot.leader)  # The cached flag has not observed lease loss yet.
        await self.bot.stop()
        self.assertEqual(replacement.store.evaluation_state()['attempt_id'], attempt)
        self.assertTrue(replacement.evaluation_status()['in_flight'])
        self.assertFalse(replacement.evaluation_status()['fault_stopped'])
        self.assertEqual(replacement.evaluation_status()['failure_streak'], 1)
        finish.set()
        await current
        self.assertFalse(replacement.evaluation_status()['in_flight'])
        self.assertEqual(replacement.evaluation_status()['failure_streak'], 0)

    async def test_foreign_active_lease_cannot_be_renewed_or_recovered_by_old_owner(self):
        self.clock.on_advance = lambda: None

        async def late(*args, **kwargs):
            self.clock.advance(31)
            return {'send':False, 'text':'', 'sticker_ids':[]}

        self.model.generate = late
        await self.bot.proactive_once()
        replacement = self.runtime(self.config, self.clock)
        before = replacement.store.evaluation_state()
        await self.renew_once(self.bot)
        self.assertFalse(self.bot.leader)
        self.assertFalse(self.bot.store.owns(self.bot.owner, self.clock()))
        self.assertEqual(replacement.store.evaluation_state(), before)
