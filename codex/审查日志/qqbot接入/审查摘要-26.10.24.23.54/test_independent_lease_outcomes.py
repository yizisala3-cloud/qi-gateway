"""Re-review probes for outcomes already returned during a same-owner lease gap."""
import asyncio
from pathlib import Path
import tempfile
import unittest

import httpx

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_proactive import QQModelHTTPError
from tests.test_qqbot import Clock, Model, Transport, event


class LeaseOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock, self.model, self.transport = Clock(), Model(), Transport()
        self.config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.folder.name)/'ledger.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.bot = QQRuntime(self.config, model=self.model, transport=self.transport,
            now=self.clock, exporter=lambda *args: None)
        self.bot.activate()
        self.addAsyncCleanup(self.bot.stop)
        self.clock.on_advance = lambda: self.bot.store.acquire(self.bot.owner, self.clock())
        await self.bot.ingest(event(1, 'isolated initial chat'))
        self.clock.advance(4)
        await self.bot.reply_once()
        await self.bot.send_once()
        self.clock.advance(2)

    async def fail_four_times(self):
        async def failed(*args, **kwargs):
            raise httpx.ReadTimeout('isolated provider timeout')
        self.model.generate = failed
        for _ in range(4):
            await self.bot.proactive_once()
            self.clock.advance(self.bot.evaluation_status()['next_check_at']-self.clock())
        self.assertEqual(self.bot.evaluation_status()['failure_streak'], 4)

    async def returned_during_gap(self, outcome):
        # No competing owner, revision change, process restart or storage
        # failure: the same in-memory request returns before the lease renews.
        self.clock.on_advance = lambda: None
        async def late(*args, **kwargs):
            self.clock.advance(31)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome
        self.model.generate = late
        await self.bot.proactive_once()
        self.assertIsNone(self.bot.proactive_generation)
        renewal = asyncio.create_task(self.bot._lease_loop())
        await asyncio.sleep(0)
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)
        self.assertTrue(self.bot.store.owns(self.bot.owner, self.clock()))
        self.assertFalse(self.bot.evaluation_status()['in_flight'])
        return self.bot.evaluation_status()

    async def test_successful_silence_after_four_failures_resets_instead_of_stopping(self):
        await self.fail_four_times()
        state = await self.returned_during_gap({'send': False, 'text': '', 'sticker_ids': []})
        self.assertFalse(state['fault_stopped'], f'known successful silence became a fifth failure: {state}')
        self.assertEqual(state['failure_streak'], 0)
        self.assertEqual(state['silent_streak'], 1)
        self.assertEqual(state['next_check_at']-self.clock(), 600)

    async def test_authentication_failure_still_stops_immediately_after_lease_gap(self):
        state = await self.returned_during_gap(QQModelHTTPError(401))
        self.assertTrue(state['fault_stopped'], f'known 401 was converted to a retryable interruption: {state}')
        self.assertEqual(state['last_error_code'], 'auth_failed')
        self.assertEqual(state['failure_streak'], 1)

    async def test_rate_limit_preserves_provider_retry_after_after_lease_gap(self):
        state = await self.returned_during_gap(QQModelHTTPError(429, 7200))
        self.assertGreaterEqual(state['next_check_at']-self.clock(), 7200,
            f'known provider Retry-After was lost: {state}')
        self.assertEqual(state['last_error_code'], 'rate_limited')
        self.assertFalse(state['fault_stopped'])

