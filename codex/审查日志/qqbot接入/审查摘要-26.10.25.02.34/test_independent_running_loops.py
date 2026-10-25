"""Independent checks of the running lease loop and private-chat isolation."""
import asyncio
from pathlib import Path
import tempfile
import unittest

import httpx

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from tests.test_qqbot import Clock, Model, Transport, event


class RunningLoopTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock, self.model, self.transport = Clock(), Model(), Transport()
        config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.folder.name)/'ledger.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.bot = QQRuntime(config, model=self.model, transport=self.transport,
            now=self.clock, exporter=lambda *args: None)
        self.addAsyncCleanup(self.bot.stop)
        self.assertTrue(self.bot.activate())
        self.clock.on_advance = lambda: self.bot.store.acquire(self.bot.owner, self.clock())
        await self.bot.ingest(event(1, 'initial isolated chat'))
        self.clock.advance(4)
        await self.bot.reply_once()
        await self.bot.send_once()
        self.clock.advance(2)
        self.transport.sent.clear()

    async def test_running_background_renewal_does_not_interrupt_its_live_model(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def waiting(*args, **kwargs):
            calls.append(kwargs.get('proactive'))
            entered.set()
            await release.wait()
            return {'send': False, 'text': '', 'sticker_ids': []}
        self.model.generate = waiting
        self.bot.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            before = self.bot.store.evaluation_state()
            self.assertTrue(before['in_flight'])
            # Run the unmodified five-second background loop, rather than
            # manually invoking a single renewal or altering asyncio.sleep.
            await asyncio.sleep(5.2)
            self.assertEqual(self.bot.store.evaluation_state(), before)
            self.assertFalse(self.bot.proactive_generation.done())
            release.set()
            for _ in range(100):
                if not self.bot.evaluation_status()['in_flight']:
                    break
                await asyncio.sleep(.01)
            state = self.bot.evaluation_status()
            self.assertFalse(state['in_flight'])
            self.assertFalse(state['fault_stopped'])
            self.assertEqual(state['failure_streak'], 0)
            self.assertEqual(state['silent_streak'], 1)
            self.assertEqual(calls, [True])
            self.assertEqual(self.transport.sent, [])
        finally:
            release.set()

    async def test_orphan_recovery_during_private_generation_preserves_the_reply(self):
        async def failed(*args, **kwargs):
            raise httpx.ReadTimeout('isolated failure')
        self.model.generate = failed
        for _ in range(4):
            await self.bot.proactive_once()
            self.clock.advance(self.bot.evaluation_status()['next_check_at']-self.clock())
        attempt = self.bot.store.begin_evaluation(self.clock(), 300, self.bot.owner)
        self.assertIsNotNone(attempt)
        self.assertTrue(await self.bot.ingest(event(2, 'new private input')))
        self.clock.advance(4)
        entered, release = asyncio.Event(), asyncio.Event()
        async def private_reply(*args, **kwargs):
            self.assertFalse(kwargs.get('proactive', False))
            entered.set()
            await release.wait()
            return {'send': True, 'text': 'fresh private reply', 'sticker_ids': []}
        self.model.generate = private_reply
        reply = asyncio.create_task(self.bot.reply_once())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertTrue(self.bot.generate_lock.locked())
            renewal = asyncio.create_task(self.bot._lease_loop())
            await asyncio.sleep(0)
            renewal.cancel()
            await asyncio.gather(renewal, return_exceptions=True)
            state = self.bot.evaluation_status()
            self.assertFalse(state['in_flight'])
            self.assertFalse(state['fault_stopped'])
            self.assertEqual(state['failure_streak'], 4)
            self.assertFalse(reply.done())
            release.set()
            await reply
            await self.bot.send_once()
            self.assertEqual(self.transport.sent, [[{'type': 'text', 'data': {'text': 'fresh private reply'}}]])
        finally:
            release.set()
            await reply

