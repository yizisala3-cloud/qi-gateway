"""Goal acceptance against the complete migration chain and real connections.

No production database configuration is used. Run with pytest --db.
"""
import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from unittest import mock
from uuid import uuid4

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning_goals as goals, planning_runtime as runtime
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests import test_planning_phase1r_pgserver_integration as chain
from tests.support.planning_goal_sql import GoalSqlClient


class GoalPostgresTests(unittest.TestCase):
    setUpClass = classmethod(chain.PlanningInvariantsOnPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(chain.PlanningInvariantsOnPostgresTests.tearDownClass.__func__)
    _query = classmethod(chain.PlanningInvariantsOnPostgresTests._query.__func__)

    def setUp(self):
        self.client = GoalSqlClient(self.conn)
        for patch in (mock.patch.object(runtime, 'get_client', return_value=self.client),
                      mock.patch.object(cfg, 'GATEWAY_TOKEN', 'goals-isolated-test')):
            patch.start()
            self.addCleanup(patch.stop)
        self.http = TestClient(Starlette(routes=list(planning_api_routes)), raise_server_exceptions=False)
        self.addCleanup(self.http.close)
        self.headers = {'Authorization': 'Bearer goals-isolated-test'}

    def command(self, action, goal=None, payload=None, key=None, conn=None):
        return GoalSqlClient(conn or self.conn).rpc('planning_goal_command', {
            'p_action': action, 'p_goal_id': goal, 'p_payload': payload or {},
            'p_request_key': key or str(uuid4()),
        }).execute().data

    def create(self, **changes):
        return self.command('create', payload={
            'content': '看书5页', 'target_value': '200.25', 'unit': '页', 'planned_count': 40, **changes})

    def get(self, goal):
        return self.client.rpc('planning_goal_read', {'p_goal_id': goal['id']}).execute().data

    def link(self, goal, task_id):
        latest = self.get(goal)
        return self.command('link', goal['id'], {'task_id': task_id, 'expected_version': latest['version']})

    def task(self, hollow=False):
        task_id = self.conn.execute(chain.TASK_SQL).fetchone()[0]
        if hollow:
            self.conn.execute('update planning_task set is_hollow=true, hollow_start_minutes=5, '
                              'hollow_wait_minutes=10, hollow_end_minutes=5 where id=%s', (task_id,))
        return task_id

    def occurrence(self, task_id, day='2026-10-16', hollow=False):
        if not hollow:
            return [self.conn.execute(chain.OCC_SQL.replace('2026-09-24', day), {'task_id': task_id}).fetchone()[0]]
        with self.conn.transaction():
            values = []
            for phase in ('start', 'end'):
                values.append(self.conn.execute('''insert into planning_occurrence (
                    task_id,for_date,round_key,schedule_date,display_cycle_date,display_reason,
                    phase,phase_group,status,sort_order,is_fixed,estimated_time_source,schedule_managed,
                    is_limited,source,content_snapshot,display_content,time_mode_snapshot
                ) values (%s,%s,%s,%s,%s,'initial',%s,md5(%s)::uuid,'pending',10,false,
                    'unassigned',true,false,'schedule','阶段','阶段','duration') returning id''',
                    (task_id, day, 'cycle:' + day, day, day, phase, f'{task_id}:cycle:{day}')).fetchone()[0])
        return values

    def status(self, occurrence_id, status, conn=None):
        (conn or self.conn).execute('''update planning_occurrence set status=%s,
            closed_at=case when %s in ('completed','discarded_this','discarded','timeout') then coalesce(closed_at,now()) else null end,
            handled_at=case when %s in ('completed','discarded_this') then coalesce(handled_at,now()) else handled_at end
            where id=%s''', (status, status, status, occurrence_id))

    def assertCount(self, goal, expected):
        actual = self.get(goal)
        self.assertEqual(actual['executed_count'], expected, actual)
        return actual

    def test_crud_decimal_unassociated_and_delete_never_changes_task(self):
        goal = self.create()
        self.assertEqual(goal['target_value'], '200.25')
        self.assertIsNone(goal['current_link'])
        self.assertCount(goal, 0)
        task = self.task()
        goal = self.link(goal, task)
        original = self.conn.execute('select to_jsonb(t) from planning_task t where id=%s', (task,)).fetchone()[0]
        edited = self.command('edit', goal['id'], {'content': '新的内容', 'target_value': '0.125',
            'unit': '任意单位h', 'planned_count': 10, 'expected_version': goal['version']})
        self.assertEqual(edited['unit'], '任意单位h')
        self.command('delete', goal['id'], {'expected_version': edited['version']})
        self.assertIsNone(self.get(goal))
        self.assertEqual(self.conn.execute('select to_jsonb(t) from planning_task t where id=%s', (task,)).fetchone()[0], original)
        self.assertEqual(self.conn.execute('select count(*) from planning_goal_link where goal_id=%s', (goal['id'],)).fetchone()[0], 0)

    def test_bind_does_not_backfill_and_current_open_round_counts(self):
        task, goal = self.task(), self.create()
        old = self.occurrence(task, '2026-10-15')[0]
        self.status(old, 'completed')
        current = self.occurrence(task)[0]
        self.link(goal, task)
        self.assertCount(goal, 0)
        self.status(old, 'discarded_this')
        self.status(old, 'completed')
        self.assertCount(goal, 0)
        self.status(current, 'completed')
        self.assertCount(goal, 1)
        self.status(current, 'completed')
        self.assertCount(goal, 1)

    def test_only_complete_rounds_count_and_hollow_is_one(self):
        task, goal = self.task(True), self.create()
        self.link(goal, task)
        start, end = self.occurrence(task, hollow=True)
        self.status(start, 'completed')
        self.assertCount(goal, 0)
        for state in ('in_progress', 'partial', 'deferred'):
            self.status(end, state)
            self.assertCount(goal, 0)
        self.status(end, 'discarded_this')
        self.assertCount(goal, 0)
        self.status(end, 'completed')
        self.assertCount(goal, 1)
        # Redefining the future task as ordinary cannot reinterpret these phases.
        self.conn.execute('update planning_task set is_hollow=false where id=%s', (task,))
        self.conn.execute('update planning_occurrence set actual_end=now() where id=%s', (end,))
        self.assertCount(goal, 1)
        self.status(start, 'discarded_this')
        self.assertCount(goal, 0)
        self.status(start, 'completed')
        self.assertCount(goal, 1)

    def test_partial_deferred_discarded_and_timeout_never_count(self):
        task, goal = self.task(), self.create()
        self.link(goal, task)
        for i, state in enumerate(('partial', 'deferred', 'discarded_this', 'discarded', 'timeout')):
            occurrence = self.occurrence(task, str(date(2026, 10, 1) + timedelta(days=i)))[0]
            self.status(occurrence, state)
        self.assertCount(goal, 0)

    def test_manual_17_to_19_then_20_survives_new_connection(self):
        task, goal = self.task(), self.create()
        self.link(goal, task)
        for i in range(17):
            self.status(self.occurrence(task, str(date(2026, 9, 1) + timedelta(days=i)))[0], 'completed')
        goal = self.assertCount(goal, 17)
        goal = self.command('adjust', goal['id'], {'executed_count': 19, 'expected_version': goal['version']})
        self.assertEqual(goal['manual_adjustment'], 2)
        self.status(self.occurrence(task)[0], 'completed')
        self.assertCount(goal, 20)
        with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as other:
            self.assertEqual(GoalSqlClient(other).rpc('planning_goal_read', {'p_goal_id': goal['id']}).execute().data['executed_count'], 20)

    def test_corrections_keep_delta_after_unlink_and_nonnegative_display(self):
        task, goal = self.task(), self.create()
        self.link(goal, task)
        occurrence = self.occurrence(task)[0]
        self.status(occurrence, 'completed')
        latest = self.get(goal)
        self.command('adjust', goal['id'], {'executed_count': 0, 'expected_version': latest['version']})
        self.link(goal, None)
        self.status(occurrence, 'discarded_this')
        corrected = self.assertCount(goal, 0)
        self.assertEqual(corrected['manual_adjustment'], -1)
        self.status(occurrence, 'completed')
        self.assertCount(goal, 0)
        self.assertEqual(self.conn.execute('select count(*) from planning_goal_contribution where goal_id=%s', (goal['id'],)).fetchone()[0], 1)

    def test_unlink_rebind_and_switch_keep_history_without_gap_backfill(self):
        first, second, goal = self.task(), self.task(), self.create()
        self.link(goal, first)
        occurrence = self.occurrence(first)[0]
        self.status(occurrence, 'completed')
        self.link(goal, None)
        gap = self.occurrence(first, '2026-10-17')[0]
        self.status(gap, 'completed')
        self.link(goal, first)
        self.status(gap, 'completed')
        self.assertCount(goal, 1)
        self.link(goal, second)
        self.status(self.occurrence(first, '2026-10-18')[0], 'completed')
        self.assertCount(goal, 1)
        self.status(self.occurrence(second)[0], 'completed')
        self.assertCount(goal, 2)
        self.status(occurrence, 'discarded_this')
        self.assertCount(goal, 1)

    def test_task_deletion_and_pause_preserve_progress(self):
        task, goal = self.task(), self.create()
        self.link(goal, task)
        self.conn.execute('update planning_task set refresh_enabled=false where id=%s', (task,))
        self.assertIsNotNone(self.get(goal)['current_link'])
        self.status(self.occurrence(task)[0], 'completed')
        self.conn.execute('select planning_discard_task(%s,now(),null,null,null)', (task,))
        after = self.assertCount(goal, 1)
        self.assertIsNone(after['current_link'])
        self.assertEqual(after['history'][0]['end_reason'], 'task_deleted')
        empty_task, empty_goal = self.task(), self.create()
        self.link(empty_goal, empty_task)
        self.conn.execute('select planning_discard_task(%s,now(),null,null,null)', (empty_task,))
        self.assertIsNone(self.get(empty_goal)['current_link'])

    def test_excess_and_edit_denominator(self):
        goal = self.create()
        goal = self.command('adjust', goal['id'], {'executed_count': 44, 'expected_version': goal['version']})
        self.assertEqual(goal['percentage'], 110)
        self.assertTrue(goal['achieved'])
        edited = self.command('edit', goal['id'], {'content': '看书', 'target_value': '200', 'unit': '页',
            'planned_count': 80, 'expected_version': goal['version']})
        self.assertEqual(edited['executed_count'], 44)
        self.assertEqual(edited['percentage'], 55)
        self.assertFalse(edited['achieved'])

    def test_retry_concurrency_and_optimistic_relation_version(self):
        goal, task = self.create(), self.task()
        key, payload = str(uuid4()), {'task_id': task, 'expected_version': goal['version']}
        results = []
        def bind():
            with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as conn:
                results.append(self.command('link', goal['id'], payload, key, conn))
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: bind(), range(5)))
        self.assertEqual(len(results), 5)
        self.assertEqual(self.conn.execute('select count(*) from planning_goal_link where goal_id=%s', (goal['id'],)).fetchone()[0], 1)
        with self.assertRaises(chain.psycopg.Error) as caught:
            self.command('link', goal['id'], {'task_id': None, 'expected_version': goal['version']})
        self.assertEqual(caught.exception.sqlstate, 'PG409')
        other_goal = self.create()
        with self.assertRaises(chain.psycopg.Error) as caught:
            self.link(other_goal, task)
        self.assertEqual(caught.exception.sqlstate, 'PG409')

    def test_simultaneous_goals_cannot_steal_one_task(self):
        task, goals_pair = self.task(), [self.create(), self.create()]
        barrier = threading.Barrier(2)
        def bind(goal):
            with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as conn:
                barrier.wait(timeout=5)
                try:
                    self.command('link', goal['id'], {'task_id': task, 'expected_version': goal['version']}, conn=conn)
                    return 'ok'
                except chain.psycopg.Error as error:
                    return error.sqlstate
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(bind, goals_pair)), ['ok', 'PG409'])

    def test_simultaneous_hollow_phases_complete_once(self):
        task, goal = self.task(True), self.create()
        self.link(goal, task)
        phases = self.occurrence(task, hollow=True)
        barrier = threading.Barrier(2)
        def finish(occurrence_id):
            with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as conn:
                conn.execute("set statement_timeout='5s'")
                barrier.wait(timeout=5)
                self.status(occurrence_id, 'completed', conn)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(finish, phases))
        self.assertCount(goal, 1)

    def test_completion_and_manual_correction_follow_commit_order(self):
        for completion_first in (True, False):
            goal, task = self.create(), self.task()
            goal = self.link(goal, task)
            occurrence = self.occurrence(task)[0]
            with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as first, \
                 chain.psycopg.connect(self.server.get_uri(), autocommit=True) as second:
                first.execute('begin')
                if completion_first:
                    self.status(occurrence, 'completed', first)
                    job = lambda: self.command('adjust', goal['id'], {'executed_count': 19, 'expected_version': goal['version']}, conn=second)
                else:
                    self.command('adjust', goal['id'], {'executed_count': 19, 'expected_version': goal['version']}, conn=first)
                    job = lambda: self.status(occurrence, 'completed', second)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(job)
                    first.execute('commit')
                    future.result(timeout=5)
            self.assertCount(goal, 19 if completion_first else 20)

    def test_server_aggregate_exceeds_list_limits(self):
        goal, task = self.create(), self.task()
        self.link(goal, task)
        # More than PostgREST's default max_rows. This statement also exercises
        # row triggers seeing all changes from a multi-row INSERT.
        self.conn.execute('''insert into planning_occurrence
            (task_id,for_date,round_key,schedule_date,display_cycle_date,display_reason,status,
             closed_at,handled_at,estimated_time_source,schedule_managed,content_snapshot,display_content,time_mode_snapshot)
            select %s, d::date, 'cycle:'||d::date::text, d::date, d::date, 'initial','completed',now(),now(),
                'unassigned',true,'大列表','大列表','duration'
            from generate_series(date '2020-01-01',date '2023-01-06', interval '1 day') d''', (task,))
        self.assertGreater(self.get(goal)['executed_count'], 1000)

    def test_api_auth_validation_deleted_and_raw_sql_validation(self):
        self.assertEqual(self.http.get('/admin/api/planning/goals').status_code, 401)
        headers = {**self.headers, 'Idempotency-Key': str(uuid4())}
        valid = {'content': '阅读', 'target_value': '0.5', 'unit': 'h', 'planned_count': 40}
        for bad in (None, 0, -1, 1.5, True, ''):
            response = self.http.post('/admin/api/planning/goals', headers=headers, json={**valid, 'planned_count': bad})
            self.assertEqual(response.status_code, 400, response.text)
        for value in ('NaN', 'Infinity', '-1', '0', None, True):
            response = self.http.post('/admin/api/planning/goals', headers=headers, json={**valid, 'target_value': value})
            self.assertEqual(response.status_code, 400, response.text)
        created = self.http.post('/admin/api/planning/goals', headers=headers, json=valid)
        self.assertEqual(created.status_code, 201, created.text)
        goal = created.json()
        self.assertEqual(self.http.post('/admin/api/planning/goals', headers=headers, json=valid).json()['id'], goal['id'])
        self.assertEqual(self.http.post('/admin/api/planning/goals', headers=headers, json={**valid, 'content': 'different'}).status_code, 409)
        for bad in (-1, 1.5, True, None):
            response = self.http.post(f"/admin/api/planning/goals/{goal['id']}/progress", headers=headers,
                json={'executed_count': bad, 'expected_version': goal['version']})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.http.get('/admin/api/planning/goals/99999999', headers=self.headers).status_code, 404)
        with self.assertRaises(chain.psycopg.Error) as caught:
            self.command('create', payload={**valid, 'planned_count': None})
        self.assertEqual(caught.exception.sqlstate, 'PG400')

    def test_private_objects_and_rpc_permissions(self):
        self.conn.execute('set role anon')
        try:
            with self.assertRaises(chain.psycopg.Error):
                self.conn.execute('select planning_goal_read(null)')
            with self.assertRaises(chain.psycopg.Error):
                self.conn.execute('select * from planning_goal_contribution')
        finally:
            self.conn.execute('reset role')
        self.conn.execute('set role service_role')
        try:
            self.create()
            self.assertIsInstance(self.client.rpc('planning_goal_read', {'p_goal_id': None}).execute().data, list)
        finally:
            self.conn.execute('reset role')

    def test_real_finish_api_updates_goal_without_goal_page_request(self):
        goal, task = self.create(), self.task()
        self.link(goal, task)
        occurrence = self.occurrence(task)[0]
        with mock.patch('gateway.planning_occurrences.recompute.request_recompute'):
            response = self.http.post(f'/admin/api/planning/occurrences/{occurrence}/finish', headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertCount(goal, 1)
        with mock.patch('gateway.planning_occurrences.recompute.request_recompute'):
            response = self.http.post(f'/admin/api/planning/occurrences/{occurrence}/status',
                                      headers=self.headers, json={'status': 'discarded_this'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertCount(goal, 0)

    def test_binding_and_completion_use_serialized_order(self):
        for completion_first in (True, False):
            goal, task = self.create(), self.task()
            occurrence = self.occurrence(task)[0]
            with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as first, \
                 chain.psycopg.connect(self.server.get_uri(), autocommit=True) as second:
                first.execute('begin')
                if completion_first:
                    self.status(occurrence, 'completed', first)
                    job = lambda: self.command('link', goal['id'], {'task_id': task, 'expected_version': goal['version']}, conn=second)
                else:
                    self.command('link', goal['id'], {'task_id': task, 'expected_version': goal['version']}, conn=first)
                    job = lambda: self.status(occurrence, 'completed', second)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(job)
                    first.execute('commit')
                    future.result(timeout=5)
            self.assertCount(goal, 0 if completion_first else 1)

    def test_unlink_and_completion_use_serialized_order(self):
        for completion_first in (True, False):
            goal, task = self.create(), self.task()
            goal = self.link(goal, task)
            occurrence = self.occurrence(task)[0]
            with chain.psycopg.connect(self.server.get_uri(), autocommit=True) as first, \
                 chain.psycopg.connect(self.server.get_uri(), autocommit=True) as second:
                first.execute('begin')
                if completion_first:
                    self.status(occurrence, 'completed', first)
                    job = lambda: self.command('link', goal['id'], {'task_id': None, 'expected_version': goal['version']}, conn=second)
                else:
                    self.command('link', goal['id'], {'task_id': None, 'expected_version': goal['version']}, conn=first)
                    job = lambda: self.status(occurrence, 'completed', second)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(job)
                    first.execute('commit')
                    future.result(timeout=5)
            self.assertCount(goal, 1 if completion_first else 0)

    def test_new_single_tasks_and_legacy_records_never_inherit_progress(self):
        goal, task = self.create(), self.task()
        self.link(goal, task)
        self.conn.execute('''insert into planning_occurrence(task_id,for_date,status)
                             values (%s,'2026-09-01','completed')''', (task,))
        single = self.conn.execute("insert into planning_task(content,task_type,refresh_mode) values ('拆分或重排的新待办','once','none') returning id").fetchone()[0]
        self.conn.execute('''insert into planning_occurrence(task_id,for_date,status)
                             values (%s,'2026-09-01','completed')''', (single,))
        self.assertCount(goal, 0)
        self.assertNotIn(single, [row['id'] for row in goals.candidates()])
        with self.assertRaises(chain.psycopg.Error) as caught:
            self.link(goal, single)
        self.assertEqual(caught.exception.sqlstate, 'PG400')

    def test_achievement_does_not_stop_future_completions(self):
        goal, task = self.create(planned_count=1), self.task()
        self.link(goal, task)
        for day in ('2026-10-15', '2026-10-16'):
            self.status(self.occurrence(task, day)[0], 'completed')
        self.assertEqual(self.assertCount(goal, 2)['percentage'], 200)
        self.assertEqual(self.conn.execute('select is_active,refresh_enabled from planning_task where id=%s', (task,)).fetchone(), (True, True))

    def test_upgrade_baseline_excludes_already_completed_rounds(self):
        # A separate database models installation over existing stable history.
        # Cluster roles already exist; all schema/functions/migrations are real.
        from psycopg import sql
        database = 'goal_upgrade_' + uuid4().hex
        self.conn.execute(sql.SQL('create database {}').format(sql.Identifier(database)))
        upgrade = None
        try:
            uri = chain.psycopg.conninfo.make_conninfo(self.server.get_uri(), dbname=database)
            upgrade = chain.psycopg.connect(uri, autocommit=True)
            baseline = chain.BASELINE_SQL
            for statement in ('create role anon nologin;', 'create role authenticated nologin;', 'create role service_role nologin bypassrls;'):
                baseline = baseline.replace(statement, '')
            upgrade.execute(baseline)
            for migration in sorted(chain.MIGRATIONS_DIR.glob('*.sql')):
                if migration.name == '20261016010000_planning_goals.sql':
                    continue
                text = migration.read_text(encoding='utf-8')
                if migration.name == chain.PG_TRGM_FILE:
                    text = text.replace(chain.PG_TRGM_LINE, '-- pg_trgm stubbed (pgserver)')
                upgrade.execute(text)
            task = upgrade.execute(chain.TASK_SQL).fetchone()[0]
            old = upgrade.execute(chain.OCC_SQL, {'task_id': task}).fetchone()[0]
            pending = upgrade.execute(chain.OCC_SQL.replace('2026-09-24', '2026-09-25'), {'task_id': task}).fetchone()[0]
            self.status(old, 'completed', upgrade)
            upgrade.execute((chain.MIGRATIONS_DIR / '20261016010000_planning_goals.sql').read_text(encoding='utf-8'))
            goal = self.command('create', payload={'content': '升级目标', 'target_value': '20.5', 'unit': '页', 'planned_count': 10}, conn=upgrade)
            self.command('link', goal['id'], {'task_id': task, 'expected_version': goal['version']}, conn=upgrade)
            self.status(old, 'discarded_this', upgrade)
            self.status(old, 'completed', upgrade)
            read = lambda: GoalSqlClient(upgrade).rpc('planning_goal_read', {'p_goal_id': goal['id']}).execute().data
            self.assertEqual(read()['executed_count'], 0)
            self.status(pending, 'completed', upgrade)
            self.assertEqual(read()['executed_count'], 1)
        finally:
            if upgrade: upgrade.close()
            self.conn.execute(sql.SQL('drop database {}').format(sql.Identifier(database)))

    def test_migration_replay_preserves_history_and_manual_adjustment(self):
        goal, task = self.create(), self.task()
        self.link(goal, task)
        occurrence = self.occurrence(task)[0]
        self.status(occurrence, 'completed')
        latest = self.get(goal)
        goal = self.command('adjust', goal['id'], {'executed_count': 7, 'expected_version': latest['version']})
        self.status(occurrence, 'discarded_this')
        before = self.get(goal)
        migration = (chain.MIGRATIONS_DIR / '20261016010000_planning_goals.sql').read_text(encoding='utf-8')
        self.conn.execute(migration)
        self.assertEqual(self.get(goal), before)
        self.assertCount(goal, 6)
        self.status(occurrence, 'completed')
        self.assertCount(goal, 7)
