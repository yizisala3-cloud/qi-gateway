import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from unittest import mock

from gateway import planning_commands, planning_common, planning_occurrences, planning_runtime
from tests import test_planning_latency_pgserver as latencybase

NOW = latencybase.NOW
from tests.support.planning_goal_sql import GoalSqlClient


class ConsoleCommandPostgresTests(unittest.TestCase):
    setUpClass = classmethod(latencybase.PlanningLatencyPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(latencybase.PlanningLatencyPostgresTests.tearDownClass.__func__)
    _query = classmethod(latencybase.PlanningLatencyPostgresTests._query.__func__)
    def setUp(self):
        latencybase.PlanningLatencyPostgresTests.setUp(self)
        self.conn.execute('truncate planning_occurrence_command_request')
        # The fact gate deliberately has no FK; task identity resets must clear it.
        self.conn.execute('truncate planning_task_completion_fact')
        self.conn.execute('delete from planning_recompute_state')

    def seed_occurrence(self, hollow=False):
        task = self.helper.task(hollow)
        return task, self.helper.occurrence(task, '2026-10-18', hollow)

    def command(self, occurrence, status='completed', key=None, now=NOW, **values):
        return planning_commands.occurrence_command(occurrence, {
            'status': status, 'request_key': key or str(uuid4()), **values,
        }, now)

    def test_finish_one_rpc_with_durable_replay_and_intent_conflict(self):
        task, (occurrence,) = self.seed_occurrence()
        key = str(uuid4())
        self.client.calls.clear()
        with mock.patch.object(self.client, 'rpc', wraps=self.client.rpc) as rpc:
            saved = self.command(occurrence, key=key, actual_logged_duration='1h1m1s')
        self.assertEqual(rpc.call_count, 1)
        self.assertEqual(self.client.calls, [])
        self.assertEqual(saved['actual_logged_seconds'], 3661)
        mark = self.conn.execute('select request_token from planning_recompute_state').fetchone()[0]
        replay = self.command(occurrence, key=key, now=NOW + timedelta(minutes=5), actual_logged_duration='1h1m1s')
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['handled_at'], saved['handled_at'])
        self.assertEqual(mark, self.conn.execute('select request_token from planning_recompute_state').fetchone()[0])
        with self.assertRaisesRegex(Exception, '操作身份'):
            self.command(occurrence, key=key, actual_logged_duration='2m')

    def test_simple_actions_preserve_legacy_time_and_source_semantics(self):
        for seconds, source in [(None, None), (30, 'user'), (90, 'user'), (150, 'system')]:
            with self.subTest(seconds=seconds, source=source):
                task_a, (old_id,) = self.seed_occurrence()
                task_b, (new_id,) = self.seed_occurrence()
                if seconds is not None:
                    self.conn.execute('update planning_occurrence set actual_start=%s, actual_time_source=%s where id=any(%s)',
                                      (NOW - timedelta(seconds=seconds), source, [old_id, new_id]))
                expected = planning_occurrences.finish_occurrence(old_id, {'actual_logged_duration':'45'}, NOW)
                actual = self.command(new_id, actual_logged_duration='45')
                for field in ('status', 'actual_start', 'actual_end', 'actual_minutes', 'actual_time_source',
                              'closed_at', 'handled_at', 'actual_logged_seconds'):
                    self.assertEqual(actual[field], expected[field], field)

    def test_start_partial_and_close_keep_partial_open(self):
        task, (occurrence,) = self.seed_occurrence()
        self.assertEqual(self.command(occurrence, 'in_progress')['actual_time_source'], 'user')
        partial = self.command(occurrence, 'partial', partial_note='完成一部分', now=NOW + timedelta(minutes=3))
        self.assertIsNone(partial['handled_at'])
        self.assertIsNone(partial['closed_at'])
        result = self.command(occurrence, now=NOW + timedelta(minutes=10))
        self.assertEqual(result['actual_minutes'], 10)
        self.assertEqual(result['partial_note'], '完成一部分')
        with self.assertRaisesRegex(Exception, '状态已改变'):
            self.command(occurrence, 'in_progress')

    def test_hollow_round_advances_after_completion_only_when_both_phases_handled(self):
        task, occurrences = self.seed_occurrence(True)
        self.conn.execute("update planning_task set task_type='interval',refresh_mode='after_completion',"
                          "after_completion_minutes=15,interval_days=1 where id=%s", (task,))
        self.command(occurrences[0])
        self.assertIsNone(self.conn.execute('select last_handled_at from planning_task where id=%s', (task,)).fetchone()[0])
        self.command(occurrences[1], now=NOW + timedelta(minutes=2))
        baseline, due = self.conn.execute('select last_handled_at,refresh_next_due_at from planning_task where id=%s', (task,)).fetchone()
        self.assertEqual(baseline, NOW + timedelta(minutes=2))
        self.assertEqual(due, baseline + timedelta(minutes=15))

    def test_failure_of_recompute_registration_rolls_back_entire_command(self):
        task, (occurrence,) = self.seed_occurrence()
        self.conn.execute("""create function public.test_fail_command_mark() returns trigger language plpgsql as
            $$begin raise exception 'injected mark failure'; end$$;
            create trigger test_fail_command_mark before insert or update on planning_recompute_state
            for each row execute function public.test_fail_command_mark();""")
        try:
            with self.assertRaisesRegex(Exception, 'injected mark failure'):
                self.command(occurrence)
            self.assertEqual(self.conn.execute('select status from planning_occurrence where id=%s', (occurrence,)).fetchone()[0], 'pending')
            self.assertEqual(self.conn.execute('select count(*) from planning_occurrence_command_request').fetchone()[0], 0)
        finally:
            self.conn.execute('drop trigger test_fail_command_mark on planning_recompute_state; drop function test_fail_command_mark()')

    def test_disabled_recompute_and_service_only_permissions(self):
        task, (occurrence,) = self.seed_occurrence()
        self.conn.execute("insert into app_settings(key,value) values('planning.auto_recompute_enabled','false')")
        self.command(occurrence)
        self.assertEqual(self.conn.execute('select count(*) from planning_recompute_state where requested_at is not null').fetchone()[0], 0)
        signature = 'planning_occurrence_command(bigint,text,uuid,text,bigint,timestamptz)'
        for role in ('anon', 'authenticated'):
            self.assertFalse(self.conn.execute('select has_function_privilege(%s,%s,\'EXECUTE\')', (role, signature)).fetchone()[0])

    def test_concurrent_replays_commit_one_fact_and_one_goal_contribution(self):
        import psycopg
        task, (occurrence,) = self.seed_occurrence()
        self.helper.client = self.client
        goal = self.helper.create()
        self.helper.link(goal, task)
        key = str(uuid4())
        def execute():
            with psycopg.connect(self.server.get_uri(), autocommit=True) as conn:
                return GoalSqlClient(conn).rpc('planning_occurrence_command', {
                    'p_occurrence_id': occurrence, 'p_status': 'completed', 'p_request_key': key,
                    'p_partial_note': None, 'p_logged_seconds': None, 'p_now': NOW.isoformat(),
                }).execute().data
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: execute(), range(2)))
        self.assertEqual(sum(bool(result.get('replayed')) for result in results), 1)
        self.assertEqual(self.conn.execute('select count(*) from planning_goal_contribution where task_id=%s and active', (task,)).fetchone()[0], 1)
        self.assertEqual(self.conn.execute('select count(*) from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()[0], 1)

    def test_physical_delete_erases_all_receipt_content_but_keeps_retry_identity(self):
        task, (occurrence, second_phase) = self.seed_occurrence(True)
        self.conn.execute('update planning_task set content=%s,note=%s where id=%s',
                          ('private body to erase', 'private note to erase', task))
        key = str(uuid4())
        self.command(occurrence, 'in_progress', key=key)
        second_key = str(uuid4())
        self.command(second_phase, 'in_progress', key=second_key)
        other_task, (other_occurrence,) = self.seed_occurrence()
        other_key = str(uuid4())
        self.command(other_occurrence, 'in_progress', key=other_key)
        other_before = self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (other_key,)).fetchone()[0]
        deleted = planning_runtime._discard_task_atomically(self.client, task, NOW)
        self.assertFalse(deleted['history_preserved'])
        self.assertEqual(self.conn.execute('select count(*) from planning_task where id=%s', (task,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute('select count(*) from planning_occurrence where task_id=%s', (task,)).fetchone()[0], 0)
        intent, result = self.conn.execute('select intent,result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()
        self.assertEqual(result, {'deleted': True})
        self.assertEqual(set(intent), {'sha256'})
        self.assertEqual(len(intent['sha256']), 64)
        self.assertEqual(self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (second_key,)).fetchone()[0], {'deleted': True})
        self.assertEqual(self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (other_key,)).fetchone()[0], other_before)
        for target, expected in [('in_progress', 404), ('completed', 409)]:
            with self.assertRaises(planning_common.PlanningError) as rejected:
                self.command(occurrence, target, key=key)
            self.assertEqual(rejected.exception.status_code, expected)
        with self.assertRaises(planning_common.PlanningError) as reused:
            self.command(other_occurrence, 'in_progress', key=key)
        self.assertEqual(reused.exception.status_code, 409)

    def test_history_preserving_delete_retains_receipts_and_note_intent_is_only_a_digest(self):
        for status in ('partial', 'completed'):
            with self.subTest(status=status):
                task, (occurrence,) = self.seed_occurrence()
                key = str(uuid4())
                values = {'partial_note': 'private partial note'} if status == 'partial' else {}
                self.command(occurrence, status, key=key, **values)
                intent, before = self.conn.execute('select intent,result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()
                self.assertEqual(set(intent), {'sha256'})
                deleted = planning_runtime._discard_task_atomically(self.client, task, NOW)
                self.assertTrue(deleted['history_preserved'])
                self.assertEqual(self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()[0], before)
                self.assertTrue(self.command(occurrence, status, key=key, **values)['replayed'])

    def test_receipt_erasure_rolls_back_with_a_failed_physical_delete(self):
        task, (occurrence,) = self.seed_occurrence()
        key = str(uuid4())
        self.command(occurrence, 'in_progress', key=key)
        before = self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()[0]
        self.conn.execute("""create function public.test_fail_after_receipt_erasure() returns trigger language plpgsql as
            $$begin raise exception 'injected delete failure'; end$$;
            create trigger zz_test_fail_after_receipt_erasure after delete on planning_task
            for each row execute function public.test_fail_after_receipt_erasure();""")
        try:
            with self.assertRaises(planning_common.PlanningError) as rejected:
                planning_runtime._discard_task_atomically(self.client, task, NOW)
            self.assertEqual(rejected.exception.status_code, 503)
            self.assertEqual(self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()[0], before)
            self.assertEqual(self.conn.execute('select count(*) from planning_occurrence where task_id=%s', (task,)).fetchone()[0], 1)
            self.assertTrue(self.command(occurrence, 'in_progress', key=key)['replayed'])
        finally:
            self.conn.execute('drop trigger zz_test_fail_after_receipt_erasure on planning_task; drop function test_fail_after_receipt_erasure()')

    def test_migration_replay_scrubs_legacy_orphans_without_changing_retry_digests(self):
        task, (occurrence,) = self.seed_occurrence()
        key = str(uuid4())
        self.command(occurrence, 'in_progress', key=key)
        raw_intent = {'id': occurrence, 'status': 'in_progress', 'partial_note': None, 'logged_seconds': None}
        self.conn.execute('update planning_occurrence_command_request set intent=%s::jsonb where request_key=%s',
                          (json.dumps(raw_intent), key))
        # Model an old local build which had receipts but no deletion cleanup.
        self.conn.execute('alter table planning_task disable trigger planning_erase_task_command_receipts')
        try:
            planning_runtime._discard_task_atomically(self.client, task, NOW)
        finally:
            self.conn.execute('alter table planning_task enable trigger planning_erase_task_command_receipts')
        migration = (Path(__file__).resolve().parents[1] / 'supabase/migrations/20261025010000_planning_occurrence_commands.sql').read_text(encoding='utf-8')
        self._query(migration)
        first = self.conn.execute('select intent,result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()
        self.assertEqual(first[1], {'deleted': True})
        self.assertEqual(set(first[0]), {'sha256'})
        self._query(migration)
        self.assertEqual(self.conn.execute('select intent,result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone(), first)
        with self.assertRaises(planning_common.PlanningError) as rejected:
            self.command(occurrence, 'in_progress', key=key)
        self.assertEqual(rejected.exception.status_code, 404)
