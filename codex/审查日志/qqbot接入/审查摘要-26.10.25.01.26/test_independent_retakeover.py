"""Independent review of repeated authority changes and deferred settlement."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_proactive import QQModelHTTPError
from tests.test_qqbot import Clock, Model, Transport, event


class ReTakeoverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.clock, self.model = Clock(), Model()
        self.config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.folder.name)/'ledger.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.bot = self.runtime(self.model)
        self.clock.on_advance = lambda: self.bot.store.acquire(self.bot.owner, self.clock())
        await self.bot.ingest(event(1, 'isolated initial chat'))
        self.clock.advance(4)
        await self.bot.reply_once()
        await self.bot.send_once()
        self.clock.advance(2)

    def runtime(self, model):
        bot = QQRuntime(self.config, model=model, transport=Transport(), now=self.clock,
            exporter=lambda *args: None)
        self.addAsyncCleanup(bot.stop)
        self.assertTrue(bot.activate())
        return bot

    async def renew_once(self, bot):
        renewal = asyncio.create_task(bot._lease_loop())
        await asyncio.sleep(0)
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)

    async def finish_in_gap(self, outcome):
        self.clock.on_advance = lambda: None
        async def late(*args, **kwargs):
            self.clock.advance(31)
            if isinstance(outcome, BaseException):
                raise outcome
            return dict(outcome)
        self.model.generate = late
        await self.bot.proactive_once()
        self.assertIsNotNone(self.bot.pending_evaluation_result)
        return self.clock()

    async def test_returning_owner_reconciles_newer_orphan_after_discarding_old_result(self):
        await self.finish_in_gap(QQModelHTTPError(401))
        old_attempt = self.bot.store.evaluation_state()['attempt_id']
        replacement_model = Model()
        replacement = self.runtime(replacement_model)
        self.assertEqual(replacement.evaluation_status()['failure_streak'], 1)
        self.assertFalse(replacement.evaluation_status()['in_flight'])
        self.clock.on_advance = lambda: replacement.store.acquire(replacement.owner, self.clock())
        self.clock.advance(300)
        entered, release = asyncio.Event(), asyncio.Event()
        async def active(*args, **kwargs):
            entered.set()
            await release.wait()
            return {'send': False, 'text': '', 'sticker_ids': []}
        replacement_model.generate = active
        pending = asyncio.create_task(replacement.proactive_once())
        await entered.wait()
        new_attempt = replacement.store.evaluation_state()['attempt_id']
        self.assertNotEqual(new_attempt, old_attempt)
        try:
            # B acquired through normal activation and began a real request.
            # Now B's lease expires, and A legitimately reacquires authority.
            self.clock.on_advance = lambda: None
            self.clock.advance(31)
            await self.renew_once(self.bot)
            self.assertTrue(self.bot.store.owns(self.bot.owner, self.clock()))
            self.assertIsNone(self.bot.proactive_generation)
            release.set()
            await pending
            self.assertIsNone(replacement.proactive_generation)
            # Allow another normal lease-loop iteration after stale A is
            # discarded. Both model tasks have ended and A is authoritative;
            # B's now-orphaned row must not remain forever.
            await self.renew_once(self.bot)
            state = self.bot.evaluation_status()
            self.assertFalse(state['fault_stopped'], f'stale 401 must not stop the new attempt: {state}')
            self.assertFalse(state['in_flight'],
                f'authoritative owner has no model task but leaves newer orphan unresolved: {state}; '
                f'pending result={self.bot.pending_evaluation_result}, pending recovery={self.bot.evaluation_recovery_pending}')
        finally:
            release.set()
            await pending

    async def test_orderly_stop_with_authority_keeps_original_rate_limit_result(self):
        completed = await self.finish_in_gap(QQModelHTTPError(429, 7200))
        self.clock.advance(120)
        self.assertTrue(self.bot.store.acquire(self.bot.owner, self.clock()))
        await self.bot.stop()
        restarted = self.runtime(Model())
        state = restarted.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertEqual(state['failure_streak'], 1)
        self.assertEqual(state['last_error_code'], 'rate_limited')
        self.assertEqual(state['last_finished_at'], completed)
        self.assertEqual(state['next_check_at'], completed+7200)

    async def test_new_input_keeps_silence_reset_when_known_silence_is_replayed(self):
        before = self.bot.store.evaluation_state()
        before['silent_streak'] = 3
        self.bot.store._save_evaluation(before)
        await self.finish_in_gap({'send': False, 'text': '', 'sticker_ids': []})
        self.assertTrue(self.bot.store.acquire(self.bot.owner, self.clock()))
        self.assertTrue(await self.bot.ingest(event(2, 'new topic cancels old silence')))
        await self.renew_once(self.bot)
        state = self.bot.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['fault_stopped'])
        self.assertEqual(state['silent_streak'], 0)
        self.assertEqual(state['failure_streak'], 0)

