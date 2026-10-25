"""Keep known provider outcomes distinct from unknown crashes during lease recovery."""
import asyncio
from contextlib import closing
from dataclasses import replace
import logging
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import httpx

from gateway import logging_events
from gateway.qqbot import QQRuntime
from gateway.qqbot_config import QQConfig
from gateway.qqbot_proactive import QQModelHTTPError
from tests.test_qqbot import Clock, Model, Transport, event


SILENT = {'send': False, 'text': '', 'sticker_ids': []}
READY = {'send': True, 'text': 'old proactive draft', 'sticker_ids': []}


class LeaseOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.case_number = 0
        await self.prepare_case()

    def make_runtime(self, config, clock, model=None, transport=None):
        bot = QQRuntime(config, model=model or Model(), transport=transport or Transport(),
            now=clock, exporter=lambda *args: None)
        self.addAsyncCleanup(bot.stop)
        bot.activate()
        return bot

    async def prepare_case(self):
        self.case_number += 1
        self.clock, self.model, self.transport = Clock(), Model(), Transport()
        self.config = QQConfig(enabled=True, proactive_enabled=True,
            account_id='12345', owner_id='67890', assistant_id='role', token='x'*32,
            store_path=str(Path(self.folder.name)/f'ledger-{self.case_number}.sqlite3'),
            active_start='00:00', active_end='00:00', quiet_seconds=1,
            cooldown_seconds=60, check_seconds=300)
        self.bot = self.make_runtime(self.config, self.clock, self.model, self.transport)
        self.clock.on_advance = lambda: self.bot.store.acquire(self.bot.owner, self.clock())
        await self.bot.ingest(event(1, 'isolated initial chat'))
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

    async def failures(self, count):
        async def failed(*args, **kwargs):
            raise httpx.ReadTimeout('isolated provider timeout')
        self.model.generate = failed
        for _ in range(count):
            await self.bot.proactive_once()
            self.clock.advance(self.bot.evaluation_status()['next_check_at']-self.clock())
        self.assertEqual(self.bot.evaluation_status()['failure_streak'], count)

    async def finish_during_gap(self, outcome):
        self.clock.on_advance = lambda: None

        async def late(*args, **kwargs):
            self.clock.advance(31)
            if isinstance(outcome, BaseException):
                raise outcome
            return dict(outcome)

        self.model.generate = late
        await self.bot.proactive_once()
        self.assertIsNone(self.bot.proactive_generation)
        self.assertFalse(self.bot.store.owns(self.bot.owner, self.clock()))
        return self.clock()

    async def test_successful_silence_after_four_failures_clears_failure_streak(self):
        await self.failures(4)
        completed = await self.finish_during_gap(SILENT)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['fault_stopped'])
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['recovery_pending'])
        self.assertEqual(state['failure_streak'], 0)
        self.assertEqual(state['silent_streak'], 1)
        self.assertEqual(state['last_finished_at'], completed)
        self.assertEqual(state['next_check_at'], completed+600)

    async def test_authentication_and_permission_failures_still_stop(self):
        for index, status in enumerate([401, 403]):
            with self.subTest(status=status):
                if index:
                    await self.prepare_case()
                completed = await self.finish_during_gap(QQModelHTTPError(status))
                await self.renew_once()
                state = self.bot.evaluation_status()
                self.assertTrue(state['fault_stopped'])
                self.assertFalse(state['in_flight'])
                self.assertEqual(state['last_error_code'], 'auth_failed')
                self.assertEqual(state['last_error_at'], completed)
                self.assertEqual(state['failure_streak'], 1)
                self.bot.resume_evaluation(state['fault_version'])
                self.assertFalse(self.bot.evaluation_status()['fault_stopped'])

    async def test_rate_limit_keeps_provider_retry_after_and_original_completion_time(self):
        completed = await self.finish_during_gap(QQModelHTTPError(429, 7200))
        self.clock.advance(900)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['fault_stopped'])
        self.assertEqual(state['last_error_code'], 'rate_limited')
        self.assertEqual(state['last_finished_at'], completed)
        self.assertEqual(state['last_error_at'], completed)
        self.assertEqual(state['next_check_at'], completed+7200)
        self.assertEqual(state['next_check_at']-self.clock(), 6300)

    async def test_actual_timeout_keeps_its_error_class_and_completion_deadline(self):
        completed = await self.finish_during_gap(httpx.ReadTimeout('isolated timeout'))
        self.clock.advance(120)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertEqual(state['last_error_code'], 'timeout')
        self.assertEqual(state['failure_streak'], 1)
        self.assertFalse(state['fault_stopped'])
        self.assertEqual(state['last_finished_at'], completed)
        self.assertEqual(state['next_check_at'], completed+300)

    async def test_deferred_failure_logs_real_http_status_and_trace_once(self):
        records = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = Capture()
        logger = logging.getLogger('gateway.qqbot')
        logger.addHandler(handler)
        try:
            await self.finish_during_gap(QQModelHTTPError(401))
            self.assertEqual(records, [])
            await self.renew_once()
            await self.renew_once()
        finally:
            logger.removeHandler(handler)
        rows = [logging_events._build_event_from_record(record) for record in records]
        failures = [row for row in rows if row and row['operation']=='proactive_evaluation']
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0]['error_code'], 'proactive_calls_stopped')
        self.assertEqual(failures[0]['upstream_status'], 401)
        self.assertEqual(failures[0]['exception_type'], 'QQModelHTTPError')
        self.assertEqual(failures[0]['safe_context']['reason_code'], 'auth_failed')
        self.assertIn('test_qqbot_lease_outcomes.py', failures[0]['stack_trace'])

    async def test_replay_losing_lease_inside_finish_preserves_known_result_for_next_renewal(self):
        completed = await self.finish_during_gap(QQModelHTTPError(429, 7200))
        original = self.bot.store.finish_evaluation

        def lose_authority(*args, **kwargs):
            # The runtime read authority successfully, but the transaction
            # below must independently reject a lease that is no longer held.
            self.bot.store.release(self.bot.owner)
            return original(*args, **kwargs)

        with patch.object(self.bot.store, 'finish_evaluation', side_effect=lose_authority):
            await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['fault_stopped'])
        self.assertTrue(state['in_flight'])
        self.assertTrue(state['recovery_pending'])
        self.assertNotEqual(state['last_error_code'], 'state_unavailable')
        self.clock.advance(120)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['fault_stopped'])
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['recovery_pending'])
        self.assertEqual(state['last_error_code'], 'rate_limited')
        self.assertEqual(state['last_finished_at'], completed)
        self.assertEqual(state['next_check_at'], completed+7200)

    async def test_resume_noop_does_not_discard_unstopped_pending_result(self):
        completed = await self.finish_during_gap(QQModelHTTPError(429, 7200))
        self.assertTrue(self.bot.store.acquire(self.bot.owner, self.clock()))
        version = self.bot.evaluation_status()['fault_version']
        # The store permits an idempotent resume on a state that is not
        # stopped. That no-op cannot turn the pending completion into unknown.
        self.bot.resume_evaluation(version)
        state = self.bot.evaluation_status()
        self.assertFalse(state['fault_stopped'])
        self.assertTrue(state['in_flight'])
        self.assertTrue(state['recovery_pending'])
        self.assertEqual(state['fault_version'], version)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertFalse(state['recovery_pending'])
        self.assertEqual(state['last_error_code'], 'rate_limited')
        self.assertEqual(state['last_finished_at'], completed)
        self.assertEqual(state['next_check_at'], completed+7200)

    async def test_cancelled_pending_auth_results_do_not_log_authentication_failure(self):
        for index, cancel in enumerate(['input', 'settings']):
            with self.subTest(cancel=cancel):
                if index:
                    await self.prepare_case()
                await self.failures(4)
                with self.assertNoLogs('gateway.qqbot', level='WARNING'):
                    await self.finish_during_gap(QQModelHTTPError(401))
                    self.assertTrue(self.bot.store.acquire(self.bot.owner, self.clock()))
                    if cancel=='input':
                        self.assertTrue(await self.bot.ingest(event(2, 'cancel old auth result')))
                    else:
                        self.bot.store.invalidate_proactive('settings_changed')
                    await self.renew_once()
                    await self.renew_once()
                state = self.bot.evaluation_status()
                self.assertFalse(state['fault_stopped'])
                self.assertEqual(state['failure_streak'], 4)

    async def test_silent_deadline_already_elapsed_is_not_restarted_at_renewal(self):
        completed = await self.finish_during_gap(SILENT)
        self.clock.advance(1200)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertEqual(state['next_check_at'], completed+600)
        self.assertLess(state['next_check_at'], self.clock())
        self.assertEqual(state['failure_streak'], 0)

    async def test_ready_result_resets_policy_without_resurrecting_old_draft(self):
        await self.failures(2)
        completed = await self.finish_during_gap(READY)
        await self.renew_once()
        state = self.bot.evaluation_status()
        self.assertFalse(state['fault_stopped'])
        self.assertEqual(state['failure_streak'], 0)
        self.assertEqual(state['silent_streak'], 0)
        self.assertEqual(state['next_check_at'], completed+300)
        self.assertFalse(self.bot.store.open_proactive())
        self.assertEqual(self.bot.store.state()['count'], 0)
        await self.bot.send_once()
        self.assertEqual(self.transport.sent, [])
        self.assertFalse(any('old proactive draft' in row['content'] for row in self.bot.store.history()))

    async def test_input_or_settings_cancellation_discards_pending_known_auth_failure(self):
        for index, cancel in enumerate(['input', 'settings']):
            with self.subTest(cancel=cancel):
                if index:
                    await self.prepare_case()
                await self.failures(4)
                before = self.bot.evaluation_status()
                completed = await self.finish_during_gap(QQModelHTTPError(401))
                self.assertTrue(self.bot.store.acquire(self.bot.owner, self.clock()))
                if cancel=='input':
                    self.assertTrue(await self.bot.ingest(event(2, 'cancel old background result')))
                else:
                    self.bot.store.invalidate_proactive('settings_changed')
                await self.renew_once()
                state = self.bot.evaluation_status()
                self.assertFalse(state['fault_stopped'])
                self.assertFalse(state['in_flight'])
                self.assertEqual(state['failure_streak'], 4)
                self.assertEqual(state['last_error_code'], before['last_error_code'])
                self.assertEqual(state['last_error_at'], before['last_error_at'])
                self.assertGreaterEqual(state['last_finished_at'], completed)

    async def test_other_owner_settlement_cannot_be_overwritten_when_old_owner_returns(self):
        await self.finish_during_gap(QQModelHTTPError(401))
        replacement = self.make_runtime(self.config, self.clock)
        self.assertTrue(replacement.leader)
        settled = replacement.store.evaluation_state()
        self.assertFalse(settled['in_flight'])
        self.assertEqual(settled['last_error_code'], 'interrupted')
        replacement.store.release(replacement.owner)
        await self.renew_once()
        self.assertTrue(self.bot.store.owns(self.bot.owner, self.clock()))
        self.assertEqual(self.bot.store.evaluation_state(), settled)
        self.assertFalse(self.bot.evaluation_status()['fault_stopped'])

    async def test_old_result_cannot_touch_new_owners_active_attempt(self):
        await self.finish_during_gap(QQModelHTTPError(401))
        replacement = self.make_runtime(self.config, self.clock)
        self.clock.on_advance = lambda: replacement.store.acquire(replacement.owner, self.clock())
        self.clock.advance(300)
        entered, release = asyncio.Event(), asyncio.Event()

        async def active(*args, **kwargs):
            entered.set()
            await release.wait()
            return dict(SILENT)

        replacement.model.generate = active
        current = asyncio.create_task(replacement.proactive_once())
        await entered.wait()
        self.addAsyncCleanup(self.cancel_task, current)
        before = replacement.store.evaluation_state()
        await self.renew_once()
        self.assertFalse(self.bot.store.owns(self.bot.owner, self.clock()))
        self.assertEqual(replacement.store.evaluation_state(), before)
        self.assertTrue(replacement.evaluation_status()['in_flight'])
        release.set()
        await current
        settled = replacement.store.evaluation_state()
        replacement.store.release(replacement.owner)
        await self.renew_once()
        self.assertEqual(self.bot.store.evaluation_state(), settled)
        self.assertFalse(self.bot.evaluation_status()['fault_stopped'])

    async def test_live_request_is_not_recovered_before_its_known_result_returns(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def active(*args, **kwargs):
            entered.set()
            await release.wait()
            return dict(SILENT)

        self.model.generate = active
        current = asyncio.create_task(self.bot.proactive_once())
        self.addAsyncCleanup(self.cancel_task, current)
        await entered.wait()
        self.clock.on_advance = lambda: None
        self.clock.advance(31)
        await self.renew_once()
        self.assertTrue(self.bot.evaluation_status()['in_flight'])
        self.assertEqual(self.bot.evaluation_status()['failure_streak'], 0)
        release.set()
        await current
        self.assertFalse(self.bot.evaluation_status()['in_flight'])
        self.assertEqual(self.bot.evaluation_status()['silent_streak'], 1)

    async def test_restart_without_known_in_memory_outcome_still_counts_unknown_interruption(self):
        await self.failures(4)
        await self.finish_during_gap(SILENT)
        copied = Path(self.folder.name)/'lost-process.sqlite3'
        with closing(sqlite3.connect(copied)) as destination:
            self.bot.store.conn.backup(destination)
        restarted = self.make_runtime(replace(self.config, store_path=str(copied)), self.clock)
        state = restarted.evaluation_status()
        self.assertFalse(state['in_flight'])
        self.assertTrue(state['fault_stopped'])
        self.assertEqual(state['failure_streak'], 5)
        self.assertEqual(state['last_error_code'], 'interrupted')

    @staticmethod
    async def cancel_task(task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
