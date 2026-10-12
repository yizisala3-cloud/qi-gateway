"""Real PostgreSQL proof of #45: full rollback, membership guards and lock contention."""
import json
import time
import unittest
from copy import deepcopy
from datetime import datetime
from uuid import uuid4

from tests import test_planning_phase1r_pgserver_integration as pgbase
from gateway import planning_recompute as service


class PlanningOrderTransactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pgbase.PlanningInvariantsOnPostgresTests.setUpClass.__func__(cls)
        # Supabase supplies these default privileges; pgserver's minimal baseline
        # does not. Exercise the invoker RPC as the real gateway role.
        cls._query('grant select, update on public.planning_task, public.planning_occurrence, '
                   'public.app_settings, public.planning_recompute_state to service_role')
    tearDownClass = classmethod(pgbase.PlanningInvariantsOnPostgresTests.tearDownClass.__func__)
    _query = classmethod(pgbase.PlanningInvariantsOnPostgresTests._query.__func__)

    def setUp(self):
        self._query('truncate public.planning_occurrence, public.planning_task, '
                    'public.planning_reorder_request restart identity cascade')
        self._query("update public.planning_recompute_state set requested_at=null, reason=null, request_token=null")
        self.ids = []
        for _ in range(2):
            task = self._query(pgbase.TASK_SQL)[0][0]
            self.ids.append(self._query(pgbase.OCC_SQL, {'task_id': task})[0][0])

    def params(self, enabled=True):
        expected = self._query('select to_jsonb(o) from public.planning_occurrence o order by id')
        tasks = self._query('select to_jsonb(t) from public.planning_task t order by id')
        settings = dict(self._query("select key,value from public.app_settings where key in "
            "('planning.refresh_boundary_state','planning.auto_recompute_enabled')"))
        for key in ('planning.refresh_boundary_state', 'planning.auto_recompute_enabled'):
            settings.setdefault(key, None)
        return (json.dumps(self.ids[::-1]), '2026-09-24', json.dumps([r[0] for r in expected]),
                json.dumps([r[0] for r in tasks]), json.dumps(settings),
                '2026-09-24T07:00:00+08:00', str(uuid4()), enabled)

    def save(self, params):
        return self._query('select public.planning_save_order(%s::jsonb,%s::date,%s::jsonb,'
                           '%s::jsonb,%s::jsonb,%s::timestamptz,%s::uuid,%s::boolean)', params)[0][0]

    def order(self):
        return self._query('select id,sort_order from public.planning_occurrence order by id')

    def assert_rejected(self, params):
        before = self.order()
        with self.assertRaises(pgbase.psycopg.Error) as error:
            self.save(params)
        self.assertEqual(error.exception.sqlstate, 'PC001')
        self.assertEqual(self.order(), before)

    def test_success_and_identity_replay_do_not_reapply_after_new_order(self):
        params = self.params()
        result = self.save(params)
        self.assertEqual(result, {'saved': 2, 'recompute_enabled': True})
        self.assertEqual(self.order(), [(self.ids[0], 1), (self.ids[1], 0)])
        token = self._query('select request_token from public.planning_recompute_state')[0][0]
        self._query('update public.planning_occurrence set sort_order=sort_order+10')
        before = self.order()
        self.assertEqual(self.save(params), result)
        self.assertEqual(self.order(), before)
        self.assertEqual(self._query('select request_token from public.planning_recompute_state')[0][0], token)

    def test_late_row_failure_rolls_back_whole_statement(self):
        before = self.order()
        self._query("create function public.order_test_fail() returns trigger language plpgsql as $$"
                    "begin if new.sort_order = 1 then raise exception 'injected row failure'; end if; return new; end $$")
        self._query('create trigger order_test_fail before update on public.planning_occurrence '
                    'for each row execute function public.order_test_fail()')
        try:
            with self.assertRaises(pgbase.psycopg.Error):
                self.save(self.params())
            self.assertEqual(self.order(), before)
            self.assertIsNone(self._query('select requested_at from public.planning_recompute_state')[0][0])
            self.assertEqual(self._query('select count(*) from public.planning_reorder_request')[0][0], 0)
        finally:
            self._query('drop trigger order_test_fail on public.planning_occurrence')
            self._query('drop function public.order_test_fail()')

    def test_mark_failure_rolls_back_order_and_request(self):
        before = self.order()
        self._query("create function public.order_mark_fail() returns trigger language plpgsql as $$"
                    "begin raise exception 'injected mark failure'; end $$")
        self._query('create trigger order_mark_fail before update on public.planning_recompute_state '
                    'for each row execute function public.order_mark_fail()')
        try:
            with self.assertRaises(pgbase.psycopg.Error):
                self.save(self.params())
            self.assertEqual(self.order(), before)
            self.assertEqual(self._query('select count(*) from public.planning_reorder_request')[0][0], 0)
        finally:
            self._query('drop trigger order_mark_fail on public.planning_recompute_state')
            self._query('drop function public.order_mark_fail()')

    def test_new_member_rejects_original_complete_snapshot(self):
        params = self.params()
        task = self._query(pgbase.TASK_SQL)[0][0]
        self._query(pgbase.OCC_SQL, {'task_id': task})
        self.assert_rejected(params)

    def test_member_moved_out_rejects(self):
        params = self.params()
        self._query("update public.planning_occurrence set display_cycle_date=display_cycle_date+1, "
                    "display_reason='carryover' where id=%s", (self.ids[0],))
        self.assert_rejected(params)

    def test_all_schedule_inputs_are_guarded(self):
        for sql in (
            'update public.planning_occurrence set planned_minutes=60 where id=1',
            'update public.planning_occurrence set sort_order=999 where id=1',
            "update public.planning_occurrence set status='in_progress',actual_start='2026-09-24T07:00:00+08:00' where id=1",
            'update public.planning_task set estimated_minutes=60 where id=1',
            "insert into public.app_settings(key,value) values('planning.auto_recompute_enabled','false'::jsonb) "
            'on conflict(key) do update set value=excluded.value',
        ):
            with self.subTest(sql=sql):
                params = self.params()
                self._query(sql)
                self.assert_rejected(params)

    def test_active_writer_is_rejected_without_wait_or_deadlock(self):
        params = self.params()
        with pgbase.psycopg.connect(self.server.get_uri()) as writer:
            writer.execute('update public.planning_task set estimated_minutes=60 where id=1')
            start = time.monotonic()
            self.assert_rejected(params)
            self.assertLess(time.monotonic() - start, 1)
            writer.rollback()
        self.assertEqual(self.save(params)['saved'], 2)

    def test_select_for_update_before_write_is_rejected_without_lock_upgrade_deadlock(self):
        params = self.params()
        with pgbase.psycopg.connect(self.server.get_uri()) as writer:
            writer.execute('select id from public.planning_occurrence where id=1 for update')
            start = time.monotonic()
            self.assert_rejected(params)
            self.assertLess(time.monotonic() - start, 1)
            writer.rollback()
        self.assertEqual(self.save(params)['saved'], 2)

    def test_disabled_mark_and_service_role_permissions(self):
        self._query('set role service_role')
        try:
            self.assertEqual(self.save(self.params(enabled=False))['recompute_enabled'], False)
            self.assertIsNone(self._query('select requested_at from public.planning_recompute_state')[0][0])
            self.assertEqual(self._query('select count(*) from public.planning_reorder_request')[0][0], 1)
        finally:
            self._query('reset role')
        for role in ('anon', 'authenticated'):
            self.assertFalse(self._query("select has_function_privilege(%s,"
                "'public.planning_save_order(jsonb,date,jsonb,jsonb,jsonb,timestamptz,uuid,boolean)','execute')", (role,))[0][0])
            self.assertFalse(self._query("select has_table_privilege(%s,'public.planning_reorder_request','select')", (role,))[0][0])


class PlanningImmediateOrderTransactionTests(unittest.TestCase):
    """Real estimates and order share rollback, concurrency and replay identity."""
    setUpClass = classmethod(PlanningOrderTransactionTests.setUpClass.__func__)
    tearDownClass = PlanningOrderTransactionTests.__dict__['tearDownClass']
    _query = PlanningOrderTransactionTests.__dict__['_query']
    setUp = PlanningOrderTransactionTests.setUp
    order = PlanningOrderTransactionTests.order
    assert_rejected = PlanningOrderTransactionTests.assert_rejected
    _seed_completion_fact = pgbase.PlanningInvariantsOnPostgresTests._seed_completion_fact

    def params(self, enabled=True, order=None):
        base = list(PlanningOrderTransactionTests.params(self, enabled))
        if order is not None:
            base[0] = json.dumps(order)
        positions = {occ_id: index for index, occ_id in enumerate(json.loads(base[0]))}
        rows = [row for row in json.loads(base[2]) if row['id'] in positions]
        tasks = {row['id']: row for row in json.loads(base[3]) if row['id'] in {occ['task_id'] for occ in rows}}
        base[2] = json.dumps(rows)
        base[3] = json.dumps(list(tasks.values()))
        now = datetime.fromisoformat(base[5])
        schedule = service.scheduler.compute_schedule(
            [{**row, 'sort_order': positions[row['id']]} for row in rows], tasks, now)
        self.assertFalse(schedule.conflicts)
        _, singles, rounds = service._recompute_write_payload(rows, schedule.placed, now) if enabled else (0, [], [])
        return tuple(base) + (json.dumps(singles), json.dumps(rounds))

    def save(self, params):
        return self._query('select public.planning_save_order_with_schedule(%s::jsonb,%s::date,%s::jsonb,'
            '%s::jsonb,%s::jsonb,%s::timestamptz,%s::uuid,%s::boolean,%s::jsonb,%s::jsonb)', params)[0][0]

    def snapshot(self):
        return self._query('select to_jsonb(o) from public.planning_occurrence o order by id')

    def test_order_and_estimates_are_available_at_commit_without_wait(self):
        self._query("select public.planning_request_recompute('edit','2026-09-24T07:00:00+08:00')")
        params = self.params()
        result = self.save(params)
        self.assertTrue(result['schedule_applied'])
        self.assertEqual(result['updated'], 2)
        self.assertEqual(self.order(), [(self.ids[0], 1), (self.ids[1], 0)])
        self.assertEqual([row['id'] for row in result['estimates']], self.ids[::-1])
        self.assertEqual(datetime.fromisoformat(result['estimates'][0]['est_start']), datetime.fromisoformat(params[5]))
        self.assertEqual(self._query('select requested_at,request_token from public.planning_recompute_state')[0], (None, None))
        self._query("select public.planning_request_recompute('edit','2026-09-24T07:00:00+08:00')")
        self.assertIsNotNone(self._query('select request_token from public.planning_recompute_state')[0][0])

    def test_time_write_failure_rolls_back_order_times_mark_and_identity(self):
        self._query("select public.planning_request_recompute('edit','2026-09-24T07:00:00+08:00')")
        params = self.params()
        before = self.snapshot()
        mark = self._query('select to_jsonb(s) from public.planning_recompute_state s')
        self._query("create function public.order_time_fail() returns trigger language plpgsql as $$"
            "begin if new.est_start is not null and new.sort_order = 1 then raise exception 'injected time failure'; end if; return new; end $$")
        self._query('create trigger order_time_fail before update on public.planning_occurrence '
            'for each row execute function public.order_time_fail()')
        try:
            with self.assertRaises(pgbase.psycopg.Error):
                self.save(params)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self._query('select to_jsonb(s) from public.planning_recompute_state s'), mark)
            self.assertEqual(self._query('select count(*) from public.planning_reorder_request')[0][0], 0)
        finally:
            self._query('drop trigger order_time_fail on public.planning_occurrence')
            self._query('drop function public.order_time_fail()')

    def test_clear_failure_rolls_back_already_written_order_and_times(self):
        self._query("select public.planning_request_recompute('edit','2026-09-24T07:00:00+08:00')")
        params = self.params()
        before = self.snapshot()
        self._query("create function public.order_clear_fail() returns trigger language plpgsql as $$"
            "begin raise exception 'injected clear failure'; end $$")
        self._query('create trigger order_clear_fail before update on public.planning_recompute_state '
            'for each row execute function public.order_clear_fail()')
        try:
            with self.assertRaises(pgbase.psycopg.Error):
                self.save(params)
            self.assertEqual(self.snapshot(), before)
            self.assertIsNotNone(self._query('select request_token from public.planning_recompute_state')[0][0])
            self.assertEqual(self._query('select count(*) from public.planning_reorder_request')[0][0], 0)
        finally:
            self._query('drop trigger order_clear_fail on public.planning_recompute_state')
            self._query('drop function public.order_clear_fail()')

    def test_replay_returns_old_receipt_without_overwriting_later_times_or_mark(self):
        first = self.params()
        receipt = self.save(first)
        self.save(self.params(order=self.ids))
        self._query("select public.planning_request_recompute('edit','2026-09-24T07:00:00+08:00')")
        before = self.snapshot()
        token = self._query('select request_token from public.planning_recompute_state')[0][0]
        self.assertEqual(self.save(first), {**receipt, 'replayed': True})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self._query('select request_token from public.planning_recompute_state')[0][0], token)

    def test_frozen_slot_and_zero_changed_rows_keep_existing_times(self):
        self._query("update public.planning_occurrence set est_start='2026-09-24T09:00:00+08:00', "
            "est_end='2026-09-24T09:30:00+08:00', nominal_start='2026-09-24T09:00:00+08:00', "
            "is_fixed=true, fixed_source='manual', estimated_time_source='manual', schedule_managed=false where id=%s", (self.ids[0],))
        frozen = deepcopy(self.snapshot()[0][0])
        self.assertEqual(self.save(self.params())['updated'], 1)
        row = self.snapshot()[0][0]
        for field in ('est_start', 'est_end', 'nominal_start', 'fixed_source', 'estimated_time_source', 'is_fixed'):
            self.assertEqual(row[field], frozen[field])
        self.assertEqual(self.save(self.params())['updated'], 0)

    def test_disabled_recompute_saves_only_order_without_consuming_prior_request(self):
        self._query("select public.planning_request_recompute('edit','2026-09-24T07:00:00+08:00')")
        token = self._query('select request_token from public.planning_recompute_state')[0][0]
        result = self.save(self.params(enabled=False))
        self.assertFalse(result['schedule_applied'])
        self.assertEqual(result['updated'], 0)
        self.assertEqual(result['estimates'], [])
        self.assertEqual(self._query('select est_start from public.planning_occurrence'), [(None,), (None,)])
        self.assertEqual(self._query('select request_token from public.planning_recompute_state')[0][0], token)

    def test_hollow_pair_commits_alongside_ordinary_estimates(self):
        start, end = pgbase.PlanningInvariantsOnPostgresTests._hollow_pair(self)
        self.ids = [start, *self.ids, end]
        params = self.params(order=self.ids)
        self.assertEqual(len(json.loads(params[-1])), 1)
        result = self.save(params)
        self.assertEqual(result['updated'], 4)
        times = {row['id']: datetime.fromisoformat(row['est_start']) for row in result['estimates']}
        self.assertEqual((times[end] - times[start]).total_seconds(), 90 * 60)

    def test_late_hollow_failure_rolls_back_ordinary_times_and_order(self):
        start, end = pgbase.PlanningInvariantsOnPostgresTests._hollow_pair(self)
        self.ids = [start, *self.ids, end]
        params = self.params(order=self.ids)
        before = self.snapshot()
        self._query("create function public.order_round_fail() returns trigger language plpgsql as $$"
            "begin if new.phase = 'end' and new.est_start is not null then raise exception 'injected round failure'; end if; return new; end $$")
        self._query('create trigger order_round_fail before update on public.planning_occurrence '
            'for each row execute function public.order_round_fail()')
        try:
            with self.assertRaises(pgbase.psycopg.Error):
                self.save(params)
            self.assertEqual(self.snapshot(), before)
            self.assertEqual(self._query('select count(*) from public.planning_reorder_request')[0][0], 0)
        finally:
            self._query('drop trigger order_round_fail on public.planning_occurrence')
            self._query('drop function public.order_round_fail()')

    test_new_member_rejects_original_complete_snapshot = PlanningOrderTransactionTests.test_new_member_rejects_original_complete_snapshot
    test_all_schedule_inputs_are_guarded = PlanningOrderTransactionTests.test_all_schedule_inputs_are_guarded
    test_active_writer_is_rejected_without_wait_or_deadlock = PlanningOrderTransactionTests.test_active_writer_is_rejected_without_wait_or_deadlock
    test_select_for_update_before_write_is_rejected_without_lock_upgrade_deadlock = PlanningOrderTransactionTests.test_select_for_update_before_write_is_rejected_without_lock_upgrade_deadlock

    def test_new_rpc_can_be_called_only_by_service_role(self):
        self._query('set role service_role')
        try:
            self.assertTrue(self.save(self.params())['schedule_applied'])
        finally:
            self._query('reset role')
        for role in ('anon', 'authenticated'):
            self.assertFalse(self._query("select has_function_privilege(%s,"
                "'public.planning_save_order_with_schedule(jsonb,date,jsonb,jsonb,jsonb,timestamptz,uuid,boolean,jsonb,jsonb)',"
                "'execute')", (role,))[0][0])
