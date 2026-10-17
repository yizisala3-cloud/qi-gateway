"""Review #57/#60/#61 regressions on the complete disposable PG migration chain."""
import unittest
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest import mock
from uuid import uuid4

from tests import test_planning_optimization_pgserver as base
from gateway import planning_detail_edit as edit, planning_generation as generation
from gateway import planning_schedule as schedule

NOW = base.NOW


class DetailReviewPostgresTests(unittest.TestCase):
    setUpClass = classmethod(base.OptimizationPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(base.OptimizationPostgresTests.tearDownClass.__func__)
    _query = classmethod(base.OptimizationPostgresTests._query.__func__)
    setUp = base.OptimizationPostgresTests.setUp
    row = base.OptimizationPostgresTests.row
    task = base.OptimizationPostgresTests.task
    payload = base.OptimizationPostgresTests.payload
    save = base.OptimizationPostgresTests.save

    def once(self, hollow=False):
        self.task_id = self.helper.task(hollow)
        self.conn.execute("update planning_task set task_type='once',refresh_mode='none',target_date='2026-10-16' where id=%s", (self.task_id,))

    def test_once_generation_after_validation_rejects_without_half_write(self):
        for hollow in (False, True):
            with self.subTest(hollow=hollow):
                self.once(hollow)
                ctx = edit.get_context(self.task_id, now=NOW)
                before = self.task()
                real_rpc = self.client.rpc
                def raced_rpc(name, params):
                    if name == 'planning_commit_detail_edit':
                        generation._create_occurrences(self.client, before, date(2026, 10, 16), NOW)
                    return real_rpc(name, params)
                payload = {'occurrence_id': None, 'token': ctx['token'], 'changes': {
                    'content': '必须整体拒绝', 'window_start_tod': '08:00', 'window_end_tod': '12:00'}}
                key = str(uuid4())
                with mock.patch.object(self.client, 'rpc', side_effect=raced_rpc):
                    with self.assertRaises(edit.common.ConcurrencyRejected):
                        self.save(payload, key)
                self.assertEqual(self.task(), before)
                rows = self.conn.execute('select to_jsonb(o) from planning_occurrence o where task_id=%s', (self.task_id,)).fetchall()
                self.assertEqual(len(rows), 2 if hollow else 1)
                self.assertTrue(all(r[0]['window_start_at'] is None for r in rows))
                with self.assertRaises(edit.common.PlanningError):
                    self.save(payload, key)
                self.assertEqual(self.conn.execute('select count(*) from planning_detail_edit_request where request_key=%s', (key,)).fetchone()[0], 0)
                fresh = edit.get_context(self.task_id, now=NOW)
                self.occ_id = fresh['occurrence_id']
                result = self.save({'occurrence_id': self.occ_id, 'token': fresh['token'], 'changes': payload['changes']}, key)
                self.assertEqual(len(result['occurrences']), len(rows))
                self.assertEqual(self.row()['window_start_at'], '2026-10-16T08:00:00+08:00')

    def test_once_edit_before_generation_and_receipt_replay(self):
        for hollow in (False, True):
            with self.subTest(hollow=hollow):
                self.once(hollow)
                stale = self.task()
                ctx = edit.get_context(self.task_id, now=NOW)
                payload = {'occurrence_id': None, 'token': ctx['token'], 'changes': {
                    'window_start_tod': '08:00', 'window_end_tod': '12:00'}}
                key = str(uuid4())
                self.save(payload, key)
                self.assertEqual(generation._create_occurrences(self.client, stale, date(2026, 10, 16), NOW), 0)
                generation._create_occurrences(self.client, self.task(), date(2026, 10, 16), NOW)
                rows = self.conn.execute('select window_start_at from planning_occurrence where task_id=%s', (self.task_id,)).fetchall()
                self.assertEqual(len(rows), 2 if hollow else 1)
                self.assertTrue(all(r[0].hour == 8 for r in rows))
                self.assertTrue(self.save(payload, key)['replayed'])

    def test_once_generation_task_lock_serializes_other_connection_save(self):
        for hollow in (False, True):
            with self.subTest(hollow=hollow):
                self.once(hollow)
                ctx = edit.get_context(self.task_id, now=NOW)
                before = self.task()
                payload = {'occurrence_id': None, 'token': ctx['token'], 'changes': {
                    'content': '不应半写', 'window_start_tod': '08:00', 'window_end_tod': '12:00'}}
                with base.chain.psycopg.connect(self.conn.info.dsn, autocommit=True) as generator_conn:
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        with generator_conn.transaction():
                            generation._create_occurrences(base.GoalSqlClient(generator_conn), before, date(2026, 10, 16), NOW)
                            saving = pool.submit(self.save, payload)
                            deadline = time.monotonic() + 5
                            while True:
                                waiting = generator_conn.execute('select wait_event_type from pg_stat_activity where pid=%s', (self.conn.info.backend_pid,)).fetchone()[0]
                                if waiting == 'Lock':
                                    break
                                if saving.done() or time.monotonic() >= deadline:
                                    self.fail('detail save did not wait on the generation task lock')
                                time.sleep(.01)
                        with self.assertRaises(edit.common.ConcurrencyRejected):
                            saving.result(timeout=5)
                self.assertEqual(self.task(), before)

    def test_parked_automatic_window_can_move_and_restore_recomputes(self):
        for estimated in (False, True):
            with self.subTest(estimated=estimated):
                if estimated:
                    self.conn.execute("update planning_occurrence set est_start='2026-10-16T08:00+08',est_end='2026-10-16T08:30+08',estimated_time_source='automatic' where id=%s", (self.occ_id,))
                self.save(self.payload(parked=True))
                self.save(self.payload({'window_start_tod': '10:00', 'window_end_tod': '12:00'}))
                self.assertTrue(self.row()['is_parked'])
                self.assertEqual(schedule.compute_schedule([self.row()], {self.task_id: self.task()}, NOW).placed, {})
                self.save(self.payload(parked=False))
                self.assertIsNone(self.row()['est_start'])
                start, end = schedule.compute_schedule([self.row()], {self.task_id: self.task()}, NOW).placed[self.occ_id]
                self.assertEqual(start.hour, 10)
                self.assertLessEqual(end.hour, 12)

    def test_parked_real_manual_rule_and_deferred_anchors_remain_protected(self):
        for source, status in (('manual', 'pending'), ('rule', 'pending'), ('manual', 'deferred')):
            with self.subTest(source=source, status=status):
                self.conn.execute("update planning_occurrence set status=%s,is_parked=false,est_start='2026-10-16T08:00+08',est_end='2026-10-16T08:30+08',nominal_start='2026-10-16T08:00+08',estimated_time_source=%s,fixed_source=%s,is_fixed=true,schedule_managed=%s where id=%s", (status, source, source, source == 'rule', self.occ_id))
                self.save(self.payload(parked=True))
                before_task, before_row = self.task(), self.row()
                with self.assertRaises(edit.common.PlanningError):
                    self.save(self.payload({'window_start_tod': '10:00', 'window_end_tod': '12:00'}))
                self.assertEqual(self.task(), before_task)
                self.assertEqual(self.row(), before_row)

    def test_parked_hollow_automatic_window_move_restores_both_stages(self):
        self.task_id = self.helper.task(True)
        self.occ_id, end_id = self.helper.occurrence(self.task_id, hollow=True)
        self.conn.execute("""update planning_occurrence set planned_minutes=5,
            planned_wait_minutes=case when phase='end' then 10 else null end,
            est_start=case when phase='start' then '2026-10-16T08:00+08'::timestamptz else '2026-10-16T08:15+08'::timestamptz end,
            est_end=case when phase='start' then '2026-10-16T08:05+08'::timestamptz else '2026-10-16T08:20+08'::timestamptz end,
            estimated_time_source='automatic' where task_id=%s""", (self.task_id,))
        self.save(self.payload(parked=True))
        self.save(self.payload({'window_start_tod': '10:00', 'window_end_tod': '12:00'}))
        self.assertTrue(self.row()['is_parked'] and self.row(end_id)['is_parked'])
        self.assertEqual(self.row()['window_start_at'], self.row(end_id)['window_start_at'])
        self.save(self.payload(parked=False))
        placed = schedule.compute_schedule([self.row(), self.row(end_id)], {self.task_id: self.task()}, NOW).placed
        self.assertEqual(placed[self.occ_id][0].hour, 10)
        self.assertEqual(placed[end_id][0] - placed[self.occ_id][1], timedelta(minutes=10))

    def test_fixed_context_reconciles_old_axis_before_freezing_edit(self):
        definitions = (
            "task_type='weekly',refresh_mode='fixed_weekday',weekdays=array[4]",
            "task_type='monthly',refresh_mode='fixed_monthday',month_days=array[16]",
            "task_type='interval',refresh_mode='fixed_interval',interval_days=7,refresh_anchor_at='2026-10-16T06:00+08'",
        )
        for definition in definitions:
            for already_inserted in (False, True):
                with self.subTest(definition=definition, already_inserted=already_inserted):
                    self.task_id = self.helper.task()
                    self.conn.execute(f"update planning_task set {definition},created_at='2026-10-16T06:00+08',refresh_generated_through=null where id=%s", (self.task_id,))
                    if already_inserted:
                        edit.tasks._close_out_recurrence_before_switch(self.client, self.task(), NOW)
                        self.conn.execute('update planning_task set refresh_generated_through=null where id=%s', (self.task_id,))
                    ctx = edit.get_context(self.task_id, now=NOW)
                    reconciled = self.task()
                    self.assertEqual(reconciled['refresh_generated_through'], '2026-10-16')
                    old_rows = self.conn.execute('select to_jsonb(o) from planning_occurrence o where task_id=%s', (self.task_id,)).fetchall()
                    self.assertTrue(old_rows)
                    payload = {'occurrence_id': None, 'token': ctx['token'], 'changes': {
                        'content': '首次保存成功', 'window_start_tod': '08:00', 'window_end_tod': '12:00'}}
                    result = self.save(payload)
                    self.assertEqual(result['task']['content'], '首次保存成功')
                    self.assertEqual(self.task()['refresh_generated_through'], reconciled['refresh_generated_through'])
                    self.assertEqual(self.conn.execute('select to_jsonb(o) from planning_occurrence o where task_id=%s', (self.task_id,)).fetchall(), old_rows)

    def test_fixed_context_still_rejects_external_task_change_without_maintenance_write(self):
        self.conn.execute("update planning_task set task_type='weekly',refresh_mode='fixed_weekday',weekdays=array[4],created_at='2026-10-16T06:00+08' where id=%s", (self.task_id,))
        payload = self.payload({'content': '过期草稿', 'window_start_tod': '08:00', 'window_end_tod': '12:00'})
        self.conn.execute("update planning_task set content='另一设备' where id=%s", (self.task_id,))
        before = self.task()
        with self.assertRaises(edit.common.ConcurrencyRejected):
            self.save(payload)
        self.assertEqual(self.task(), before)
