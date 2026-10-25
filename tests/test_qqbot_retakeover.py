"""Exercise repeated authority changes as full runtime and provider state chains."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_proactive import QQModelHTTPError
from tests.test_qqbot import Clock, Model, Transport, event


SILENT = {'send': False, 'text': '', 'sticker_ids': []}
READY = {'send': True, 'text': 'obsolete greeting draft', 'sticker_ids': []}


class RetakeoverStateChainTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.case_number = 0
        await self.prepare_case()

    async def prepare_case(self):
        self.case_number += 1
        self.clock = Clock()
        self.config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.directory.name)/f'ledger-{self.case_number}.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.first = self.actor()
        self.keep_authority(self.first)
        self.assertTrue(await self.first.ingest(event(1, 'isolated initial chat')))
        self.clock.advance(4)
        await self.first.reply_once()
        await self.first.send_once()
        self.clock.advance(2)
        self.first.transport.sent.clear()

    def actor(self):
        bot = QQRuntime(self.config, model=Model(), transport=Transport(),
            now=self.clock, exporter=lambda *args: None)
        self.addAsyncCleanup(bot.stop)
        self.assertTrue(bot.activate())
        return bot

    def keep_authority(self, bot):
        self.clock.on_advance = lambda: bot.store.acquire(bot.owner, self.clock())

    async def renew_once(self, bot):
        renewal = asyncio.create_task(bot._lease_loop())
        await asyncio.sleep(0)
        renewal.cancel()
        await asyncio.gather(renewal, return_exceptions=True)

    async def renew_repeatedly(self, bot, count=3):
        before = bot.store.evaluation_state()
        for _ in range(count):
            await self.renew_once(bot)
            self.assertEqual(bot.store.evaluation_state(), before)

    def expire(self):
        self.clock.on_advance = lambda: None
        self.clock.advance(31)

    async def known_result_in_gap(self, bot, outcome):
        self.clock.on_advance = lambda: None

        async def late(*args, **kwargs):
            self.clock.advance(31)
            if isinstance(outcome, BaseException):
                raise outcome
            return dict(outcome)

        bot.model.generate = late
        await bot.proactive_once()
        self.assertIsNone(bot.proactive_generation)
        self.assertTrue(bot.evaluation_status()['in_flight'])
        self.assertFalse(bot.store.owns(bot.owner, self.clock()))

    async def start_live_attempt(self, bot, outcome=SILENT):
        self.keep_authority(bot)
        due = bot.evaluation_status()['next_check_at']
        self.clock.advance(max(0, due-self.clock()))
        entered, release = asyncio.Event(), asyncio.Event()

        async def active(*args, **kwargs):
            entered.set()
            await release.wait()
            if isinstance(outcome, BaseException):
                raise outcome
            return dict(outcome)

        bot.model.generate = active
        pending = asyncio.create_task(bot.proactive_once())
        self.addAsyncCleanup(self.cancel_task, pending)
        await entered.wait()
        self.assertTrue(bot.evaluation_status()['in_flight'])
        return release, pending

    async def next_call_is_real_and_silent(self, bot):
        self.keep_authority(bot)
        self.clock.advance(max(0, bot.evaluation_status()['next_check_at']-self.clock()))
        bot.transport.ready = True
        bot.model = Model()
        bot.model.result = dict(SILENT)
        await bot.proactive_once()
        self.assertEqual(len(bot.model.calls), 1)
        self.assertTrue(bot.model.calls[0][2])
        self.assertFalse(bot.evaluation_status()['in_flight'])
        self.assertFalse(bot.evaluation_status()['fault_stopped'])
        self.assertEqual(bot.evaluation_status()['failure_streak'], 0)
        self.assertEqual(bot.evaluation_status()['silent_streak'], 1)
        self.assertFalse(bot.store.open_proactive())
        await bot.send_once()
        self.assertEqual(bot.transport.sent, [])
        self.assertFalse(any('obsolete greeting draft' in row['content'] for row in bot.store.history()))

    async def transfer_live_attempt(self, old, returning=None, outcome=SILENT):
        release, pending = await self.start_live_attempt(old, outcome)
        prior = old.evaluation_status()['failure_streak']
        self.expire()
        new = returning or self.actor()
        if returning:
            await self.renew_once(new)
        self.assertTrue(new.store.owns(new.owner, self.clock()))
        release.set()
        await pending
        self.assertIsNone(old.proactive_generation)
        # An old known result can be discarded on the first iteration. The
        # next independent authority check must still reconcile this ledger.
        await self.renew_once(new)
        state = new.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['fault_stopped'])
        self.assertEqual(state['failure_streak'], prior+1)
        self.assertEqual(state['last_error_code'], 'interrupted')
        await self.renew_repeatedly(new)
        return new

    async def test_a_b_a_b_a_four_retakeovers_converge_then_call_model_again(self):
        a = self.first
        await self.known_result_in_gap(a, QQModelHTTPError(401))
        b = self.actor()
        self.assertEqual(b.evaluation_status()['failure_streak'], 1)
        a = await self.transfer_live_attempt(b, a)
        b = await self.transfer_live_attempt(a, b, QQModelHTTPError(429, 7200))
        a = await self.transfer_live_attempt(b, a, READY)
        self.assertEqual(a.evaluation_status()['failure_streak'], 4)
        await self.next_call_is_real_and_silent(a)

    async def test_a_b_c_a_round_trip_converges_then_calls_model_again(self):
        a = self.first
        await self.known_result_in_gap(a, SILENT)
        b = self.actor()
        c = await self.transfer_live_attempt(b)
        a = await self.transfer_live_attempt(c, a, QQModelHTTPError(401))
        self.assertEqual(a.evaluation_status()['failure_streak'], 3)
        await self.next_call_is_real_and_silent(a)

    async def test_stale_silent_auth_rate_limit_and_ready_never_apply_to_new_attempt(self):
        outcomes = [SILENT, QQModelHTTPError(401), QQModelHTTPError(429, 7200), READY]
        for index, outcome in enumerate(outcomes):
            with self.subTest(outcome=type(outcome).__name__, index=index):
                if index:
                    await self.prepare_case()
                a = self.first
                await self.known_result_in_gap(a, outcome)
                old_attempt = a.store.evaluation_state()['attempt_id']
                b = self.actor()
                release, pending = await self.start_live_attempt(b, READY)
                self.assertNotEqual(b.store.evaluation_state()['attempt_id'], old_attempt)
                self.expire()
                await self.renew_once(a)
                self.assertTrue(a.store.owns(a.owner, self.clock()))
                release.set()
                await pending
                await self.renew_once(a)
                state = a.evaluation_status()
                self.assertFalse(state['in_flight'])
                self.assertFalse(state['fault_stopped'])
                self.assertEqual(state['failure_streak'], 2)
                self.assertEqual(state['silent_streak'], 0)
                self.assertEqual(state['last_error_code'], 'interrupted')
                self.assertEqual(state['next_check_at']-self.clock(), 900)
                self.assertFalse(a.store.open_proactive())
                await self.renew_repeatedly(a)
                await self.next_call_is_real_and_silent(a)

    async def test_new_orphan_cancelled_by_input_or_settings_does_not_add_failure(self):
        for index, cancel in enumerate(['input', 'settings']):
            with self.subTest(cancel=cancel):
                if index:
                    await self.prepare_case()
                a = self.first
                await self.known_result_in_gap(a, QQModelHTTPError(401))
                b = self.actor()
                before = b.evaluation_status()
                release, pending = await self.start_live_attempt(b)
                self.expire()
                await self.renew_once(a)
                self.assertTrue(a.store.owns(a.owner, self.clock()))
                if cancel=='input':
                    self.assertTrue(await a.ingest(event(2, 'cancel the newer orphan')))
                else:
                    a.store.invalidate_proactive('settings_changed')
                release.set()
                await pending
                await self.renew_once(a)
                state = a.evaluation_status()
                self.assertFalse(state['in_flight'])
                self.assertFalse(state['fault_stopped'])
                self.assertEqual(state['failure_streak'], before['failure_streak'])
                self.assertEqual(state['last_error_code'], before['last_error_code'])
                self.assertEqual(state['last_error_at'], before['last_error_at'])
                await self.renew_repeatedly(a)

    async def test_repeated_renewal_never_recovers_its_own_live_model_request(self):
        a = self.first
        release, pending = await self.start_live_attempt(a)
        self.expire()
        initial = a.store.evaluation_state()
        for _ in range(4):
            await self.renew_once(a)
            self.assertEqual(a.store.evaluation_state(), initial)
            self.assertTrue(a.evaluation_status()['in_flight'])
            self.assertEqual(a.evaluation_status()['failure_streak'], 0)
        release.set()
        await pending
        self.assertFalse(a.evaluation_status()['in_flight'])
        self.assertEqual(a.evaluation_status()['silent_streak'], 1)
        await self.renew_repeatedly(a)

    async def test_external_valid_owner_blocks_all_old_owner_ledger_writes(self):
        a = self.first
        await self.known_result_in_gap(a, QQModelHTTPError(401))
        b = self.actor()
        release, pending = await self.start_live_attempt(b)
        before = b.store.evaluation_state()
        schedule = b.store.state()
        for _ in range(3):
            await self.renew_once(a)
            self.assertFalse(a.store.owns(a.owner, self.clock()))
            self.assertEqual(b.store.evaluation_state(), before)
            self.assertEqual(b.store.state(), schedule)
        release.set()
        await pending
        self.assertFalse(b.evaluation_status()['in_flight'])
        self.assertEqual(b.evaluation_status()['failure_streak'], 0)
        self.assertEqual(b.evaluation_status()['silent_streak'], 1)

    async def test_ordinary_owned_renewal_reconciles_orphan_without_pending_markers(self):
        a = self.first
        self.expire()
        b = self.actor()
        release, pending = await self.start_live_attempt(b)
        self.expire()
        # Acquire directly so the following lease-loop iteration sees a
        # continuously valid own lease and an unchanged cached leader flag.
        self.assertTrue(a.store.acquire(a.owner, self.clock()))
        release.set()
        await pending
        self.assertIsNone(a.proactive_generation)
        self.assertIsNone(b.proactive_generation)
        self.assertIsNone(a.pending_evaluation_result)
        self.assertFalse(a.evaluation_recovery_pending)
        self.assertTrue(a.leader)
        self.assertTrue(a.store.evaluation_state()['in_flight'])
        await self.renew_once(a)
        state = a.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertEqual(state['failure_streak'], 1)
        self.assertEqual(state['last_error_code'], 'interrupted')
        await self.renew_repeatedly(a)
        await self.next_call_is_real_and_silent(a)

    async def test_normal_finish_stale_preserves_independent_new_orphan_reconciliation(self):
        a = self.first
        release_a, pending_a = await self.start_live_attempt(a, QQModelHTTPError(401))
        old_attempt = a.store.evaluation_state()['attempt_id']
        self.expire()
        b = self.actor()
        release_b, pending_b = await self.start_live_attempt(b, READY)
        new_attempt = b.store.evaluation_state()['attempt_id']
        self.assertNotEqual(new_attempt, old_attempt)
        self.expire()
        await self.renew_once(a)
        # A reacquires while its original local provider request is still
        # alive, so it cannot yet reconcile B's persisted attempt.
        self.assertTrue(a.store.owns(a.owner, self.clock()))
        self.assertIsNotNone(a.proactive_generation)
        self.assertEqual(a.store.evaluation_state()['attempt_id'], new_attempt)
        release_a.set()
        await pending_a
        self.assertIsNone(a.proactive_generation)
        self.assertIsNone(a.pending_evaluation_result)
        self.assertFalse(a.evaluation_status()['fault_stopped'])
        self.assertEqual(a.evaluation_status()['failure_streak'], 1)
        release_b.set()
        await pending_b
        self.assertIsNone(b.proactive_generation)
        await self.renew_once(a)
        state = a.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['fault_stopped'])
        self.assertEqual(state['failure_streak'], 2)
        self.assertEqual(state['last_error_code'], 'interrupted')
        self.assertFalse(a.store.open_proactive())
        await self.renew_repeatedly(a)
        await self.next_call_is_real_and_silent(a)

    async def test_five_genuine_unknown_takeovers_still_stop_without_double_counting(self):
        current = self.first
        for count in range(1, 6):
            release, pending = await self.start_live_attempt(current)
            self.expire()
            successor = self.actor()
            release.set()
            await pending
            state = successor.evaluation_status()
            self.assertFalse(state['in_flight'])
            self.assertEqual(state['failure_streak'], count)
            self.assertEqual(state['fault_stopped'], count==5)
            self.assertEqual(state['last_error_code'], 'interrupted')
            await self.renew_repeatedly(successor)
            current = successor
        current.model = Model()
        await current.proactive_once()
        self.assertEqual(current.model.calls, [])
        state = current.evaluation_status()
        current.resume_evaluation(state['fault_version'])
        self.assertFalse(current.evaluation_status()['fault_stopped'])
        await self.next_call_is_real_and_silent(current)

    @staticmethod
    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
