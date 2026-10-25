"""Independent review probes; production code is deliberately unchanged."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from tests.test_qqbot import Clock, Model, Transport, event


class IndependentRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.clock = Clock()
        self.model = Model()
        self.transport = Transport()
        self.config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.tmp.name)/'ledger.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.runtime = self.make_runtime(self.config)
        self.addAsyncCleanup(self.close_runtime)
        self.clock.on_advance = lambda: self.runtime.store.acquire(self.runtime.owner, self.clock())
        await self.runtime.ingest(event(1, 'initial chat'))
        self.clock.advance(4)
        await self.runtime.reply_once()
        await self.runtime.send_once()
        self.clock.advance(2)
        self.transport.sent.clear()

    def make_runtime(self, config):
        runtime = QQRuntime(config, model=self.model, transport=self.transport,
            now=self.clock, exporter=lambda *args: None)
        runtime.activate()
        return runtime

    async def close_runtime(self):
        if hasattr(self, '_cleanup_evaluation'):
            # Repair only this disposable fixture so a deliberately malformed
            # record does not prevent its SQLite handle from closing.
            self.runtime.store._save_evaluation(self._cleanup_evaluation)
        await self.runtime.stop()

    async def four_failures(self):
        async def failing(*args, **kwargs):
            raise httpx.ReadTimeout('isolated timeout')
        self.model.generate = failing
        for _ in range(4):
            await self.runtime.proactive_once()
            self.clock.advance(self.runtime.store.evaluation_state()['next_check_at'] - self.clock())
        self.assertEqual(self.runtime.evaluation_status()['failure_streak'], 4)

    async def test_corrupt_evaluation_does_not_poison_receipts_and_block_private_chat(self):
        await self.runtime.proactive_once()
        entered, release = asyncio.Event(), asyncio.Event()

        async def waiting_to_submit(segments):
            entered.set()
            await release.wait()
            # The real OneBot transport calls this again after acquiring its
            # asynchronous write lock, before writing the provider action.
            self.runtime._before_submit()
            return 'confirmed', 'review-ack', None

        self.transport.send = waiting_to_submit
        sending = asyncio.create_task(self.runtime.send_once())
        await entered.wait()
        self._cleanup_evaluation = self.runtime.store.evaluation_state()
        self.runtime.store.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES('proactive_evaluation','{broken')")
        self.assertTrue(self.runtime.evaluation_status()['fault_stopped'])
        release.set()
        result = await asyncio.gather(sending, return_exceptions=True)
        initial_error = type(result[0]).__name__ if isinstance(result[0], BaseException) else None

        self.assertTrue(await self.runtime.ingest(event(2, 'ordinary private chat')))
        self.clock.advance(4)
        await self.runtime.reply_once()
        self.transport.send = Transport.send.__get__(self.transport, Transport)
        error = None
        try:
            await self.runtime.send_once()
        except Exception as exc:
            error = type(exc).__name__
        receipt_count = self.runtime.store.conn.execute('SELECT count(*) FROM send_receipts').fetchone()[0]
        queued_replies = self.runtime.store.conn.execute(
            "SELECT count(*) FROM outbox WHERE kind='reply' AND status='queued'").fetchone()[0]
        self.assertIsNone(error, f'{error}; initial send error={initial_error}; durable receipts={receipt_count}, queued replies={queued_replies}, sent={self.transport.sent}')
        self.assertEqual(len(self.transport.sent), 1)

    async def test_cancelled_attempt_is_not_a_fifth_failure_after_crash(self):
        await self.four_failures()
        entered = asyncio.Event()

        async def waiting(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.model.generate = waiting
        pending = asyncio.create_task(self.runtime.proactive_once())
        await entered.wait()
        await self.runtime.ingest(event(2, 'cancel this background attempt'))
        # Preserve the exact durable window after accept commits and cancels
        # the model, before the awaiting evaluator gets another event-loop turn.
        copied = Path(self.tmp.name)/'crash-image.sqlite3'
        destination = sqlite3.connect(copied)
        try:
            self.runtime.store.conn.backup(destination)
        finally:
            destination.close()
        await pending
        await self.runtime.stop()
        self.clock.on_advance = lambda: None
        self.clock.advance(31)
        self.transport = Transport()
        self.runtime = self.make_runtime(replace(self.config, store_path=str(copied)))
        after = self.runtime.evaluation_status()
        self.assertFalse(after['fault_stopped'], f'recovered state={after}')
        self.assertEqual(after['failure_streak'], 4)

    async def test_expired_lease_does_not_leave_finished_evaluation_in_flight_forever(self):
        self.clock.on_advance = lambda: None

        async def finish_after_lease_expired(*args, **kwargs):
            # Simulate a process pause/event-loop stall: no competing owner,
            # but the 30-second lease expires before completion is committed.
            self.clock.advance(31)
            return {'send': False, 'text': '', 'sticker_ids': []}

        self.model.generate = finish_after_lease_expired
        await self.runtime.proactive_once()
        renewal = asyncio.create_task(self.runtime._lease_loop())
        await asyncio.sleep(0)
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        self.assertTrue(self.runtime.store.owns(self.runtime.owner, self.clock()))
        self.assertTrue(self.runtime.leader)
        self.assertIsNone(self.runtime.proactive_generation)
        state = self.runtime.evaluation_status()
        self.assertFalse(state['in_flight'], f'no model task exists after lease renewal: {state}')

