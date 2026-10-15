"""Notes through the real task API/RPC after replaying the COMPLETE migration chain.

Opt in with QIGATEWAY_PG_PLANNING_TEST=1. No external DB credentials are used.
The small client adapter translates transport calls to SQL, never emulates RPCs.
"""
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import planning_runtime as runtime, planning_tasks as tasks
from gateway.config import cfg
from gateway.planning_api import planning_api_routes
from tests import test_planning_phase1r_pgserver_integration as fullchain
from tests.support.planning_context import cst

NOW = cst(2026, 10, 14, 12)
UPGRADE = fullchain.MIGRATIONS_DIR / '20261014020000_planning_once_task_notes.sql'


class SqlQuery:
    def __init__(self, client, table):
        self.client, self.table = client, table
        self.where, self.params, self.payload = [], [], None
        self.operation, self.maximum = 'select', None

    def select(self, fields):
        assert fields == '*'
        return self

    def eq(self, key, value):
        from psycopg import sql
        self.where.append(sql.SQL('{} = %s').format(sql.Identifier(key)))
        self.params.append(value)
        return self

    def limit(self, maximum):
        self.maximum = maximum
        return self

    def insert(self, payload):
        self.operation, self.payload = 'insert', payload
        return self

    def update(self, payload):
        self.operation, self.payload = 'update', payload
        return self

    def execute(self):
        from psycopg import sql
        from psycopg.types.json import Jsonb
        table = sql.Identifier('public', self.table)
        where = sql.SQL(' where ') + sql.SQL(' and ').join(self.where) if self.where else sql.SQL('')
        params = list(self.params)
        if self.operation == 'select':
            query = sql.SQL('select * from {}{}').format(table, where)
            if self.maximum is not None:
                query += sql.SQL(' limit %s')
                params.append(self.maximum)
        else:
            values = [Jsonb(v) if isinstance(v, dict) else v for v in self.payload.values()]
            if self.operation == 'update':
                fields = sql.SQL(', ').join(sql.SQL('{} = %s').format(sql.Identifier(k)) for k in self.payload)
                query = sql.SQL('update {} set {}{} returning *').format(table, fields, where)
                params = values + params
            else:
                query = sql.SQL('insert into {} ({}) values ({}) returning *').format(
                    table, sql.SQL(', ').join(map(sql.Identifier, self.payload)),
                    sql.SQL(', ').join(sql.Placeholder() for _ in values))
                params = values
        # JSON conversion matches PostgREST's date/time/array representation.
        query = sql.SQL('select to_jsonb(r) from ({}) r').format(query) if self.operation == 'select' else (
            sql.SQL('with r as ({}) select to_jsonb(r) from r').format(query))
        return SimpleNamespace(data=[r[0] for r in self.client.conn.execute(query, params).fetchall()])


class SqlClient:
    def __init__(self, conn):
        self.conn, self.calls, self.before_rpc = conn, [], None

    def table(self, table):
        return SqlQuery(self, table)

    def rpc(self, name, params):
        assert name == 'planning_update_once_task_guarded', name
        self.calls.append((name, params))

        def execute():
            if self.before_rpc:
                callback, self.before_rpc = self.before_rpc, None
                callback()
            self.conn.execute('select planning_update_once_task_guarded(%s,%s::jsonb)',
                              (params['p_task_id'], json.dumps(params['p_patch'])))
            return SimpleNamespace(data=None)
        return SimpleNamespace(execute=execute)


class TaskNoteFullChainPostgresTests(unittest.TestCase):
    # Reuse only the full-chain cluster fixture/helpers, not its test collection.
    setUpClass = classmethod(fullchain.PlanningInvariantsOnPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(fullchain.PlanningInvariantsOnPostgresTests.tearDownClass.__func__)
    _query = classmethod(fullchain.PlanningInvariantsOnPostgresTests._query.__func__)
    _bare_once_task = fullchain.PlanningInvariantsOnPostgresTests._bare_once_task
    _once_row_json = fullchain.PlanningInvariantsOnPostgresTests._once_row_json

    def setUp(self):
        self.client = SqlClient(self.conn)
        for patch in (mock.patch.object(runtime, 'get_client', lambda: self.client),
                      mock.patch.object(runtime, '_now', return_value=NOW),
                      mock.patch.object(cfg, 'GATEWAY_TOKEN', 'notes-isolated-test'),
                      mock.patch('gateway.db.load_app_setting', return_value=None),
                      mock.patch.object(tasks, '_generate_due_quietly'),
                      mock.patch.object(tasks, '_generate_created_task_quietly')):
            patch.start()
            self.addCleanup(patch.stop)
        self.http = TestClient(Starlette(routes=list(planning_api_routes)), raise_server_exceptions=False)
        self.addCleanup(self.http.close)
        self.headers = {'Authorization': 'Bearer notes-isolated-test'}

    def once(self, note='原备注'):
        task_id = self._bare_once_task('2026-11-01')
        self.conn.execute('update planning_task set note=%s where id=%s', (note, task_id))
        return task_id

    def row(self, task_id):
        return self.conn.execute('select target_date::text,window_start_tod::text,window_end_tod::text,note '
                                 'from planning_task where id=%s', (task_id,)).fetchone()

    def occurrence(self, task_id, status, day):
        row = json.loads(self._once_row_json(task_id))[0]
        row.update(for_date=day, schedule_date=day, display_cycle_date=day,
                   round_key=f'cycle:{day}', status=status)
        if status in ('completed', 'discarded_this', 'discarded', 'timeout'):
            row['closed_at'] = f'{day}T12:00:00+08:00'
        if status in ('completed', 'discarded_this'):
            row['handled_at'] = row['closed_at']
        return self.client.table('planning_occurrence').insert(row).execute().data[0]['id']

    def test_future_once_api_date_window_and_note_presence_matrix(self):
        for fields in ({'target_date': '2026-11-02'},
                       {'window_start_tod': '09:00', 'window_end_tod': '11:00'},
                       {'target_date': '2026-11-02', 'window_start_tod': '10:00', 'window_end_tod': '12:00'}):
            for initial, note_patch, expected in (
                (None, {'note': None}, None),
                ('原备注', {'note': '新备注🙂'}, '新备注🙂'),
                ('原备注', {'note': None}, None),
                ('原备注', {'note': ''}, None),
                ('原备注', {}, '原备注'),
            ):
                with self.subTest(fields=fields, note_patch=note_patch):
                    task_id = self.once(initial)
                    response = self.http.patch(f'/admin/api/planning/tasks/{task_id}',
                                               headers=self.headers, json={**fields, **note_patch})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(self.row(task_id), (
                        fields.get('target_date', '2026-11-01'),
                        fields['window_start_tod'] + ':00' if 'window_start_tod' in fields else None,
                        fields['window_end_tod'] + ':00' if 'window_end_tod' in fields else None, expected))
                    self.assertEqual(self.client.calls[-1][0], 'planning_update_once_task_guarded')

    def test_rpc_updates_note_and_date_in_storage(self):
        task_id = self.once()
        self.client.rpc('planning_update_once_task_guarded', {
            'p_task_id': task_id, 'p_patch': {'target_date': '2026-11-02', 'note': '守护保存'},
        }).execute()
        self.assertEqual(self.row(task_id), ('2026-11-02', None, None, '守护保存'))

    def test_occurrence_inserted_after_api_precheck_still_blocks_atomic_rpc(self):
        task_id = self.once()
        self.client.before_rpc = lambda: self.conn.execute(
            'select planning_insert_once_occurrence(%s,%s,null,null,%s::jsonb)',
            (task_id, '2026-11-01', self._once_row_json(task_id)))
        response = self.http.patch(f'/admin/api/planning/tasks/{task_id}', headers=self.headers,
                                   json={'target_date': '2026-11-02', 'note': '不得写入'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('已生成当前实例', response.json()['error'])
        self.assertEqual(self.row(task_id), ('2026-11-01', None, None, '原备注'))
        self.assertEqual(self.conn.execute('select note from planning_occurrence where task_id=%s',
                                           (task_id,)).fetchone(), ('原备注',))

    def test_upgrade_from_installed_old_rpc_is_repeatable_and_keeps_guards(self):
        old = (fullchain.MIGRATIONS_DIR / '20260928030000_planning_once_identity_locks.sql').read_text(encoding='utf-8')
        old_rpc = old[old.index('create or replace function public.planning_update_once_task_guarded('):
                      old.index('-- ── 生成侧')]
        self.conn.execute(old_rpc)
        acl = self.conn.execute("select proacl from pg_proc where oid="
                                "'planning_update_once_task_guarded(bigint,jsonb)'::regprocedure").fetchone()
        try:
            task_id = self.once()
            with self.assertRaisesRegex(fullchain.psycopg.Error, 'patch contains unsupported field'):
                self.client.rpc('planning_update_once_task_guarded', {
                    'p_task_id': task_id, 'p_patch': {'note': '升级前失败'},
                }).execute()
        finally:
            if UPGRADE.exists():
                self.conn.execute(UPGRADE.read_text(encoding='utf-8'))
        self.assertTrue(UPGRADE.exists(), 'an additive upgrade migration is required')
        self.conn.execute(UPGRADE.read_text(encoding='utf-8'))
        self.assertEqual(self.conn.execute("select proacl from pg_proc where oid="
                                          "'planning_update_once_task_guarded(bigint,jsonb)'::regprocedure").fetchone(), acl)
        self.test_rpc_updates_note_and_date_in_storage()
        with self.assertRaisesRegex(fullchain.psycopg.Error, 'unsupported field'):
            self.client.rpc('planning_update_once_task_guarded', {
                'p_task_id': task_id, 'p_patch': {'request_state': 'pending', 'note': '拒绝'},
            }).execute()

    def test_full_schema_open_future_sync_closed_null_and_text_preserved(self):
        task_id = self.conn.execute(fullchain.TASK_SQL).fetchone()[0]
        self.conn.execute("update planning_task set note='历史原文' where id=%s", (task_id,))
        closed = [self.occurrence(task_id, status, f'2026-10-{i:02d}') for i, status in
                  enumerate(('completed', 'discarded_this', 'discarded', 'timeout'), 1)]
        self.conn.execute('update planning_task set note=null where id=%s', (task_id,))
        blank_history = self.occurrence(task_id, 'completed', '2026-10-05')
        opened = [self.occurrence(task_id, status, f'2026-10-{i:02d}') for i, status in
                  enumerate(('pending', 'in_progress', 'partial', 'deferred'), 6)]
        for value in ('当前及后续🙂', None):
            response = self.http.patch(f'/admin/api/planning/tasks/{task_id}',
                                       headers=self.headers, json={'note': value})
            self.assertEqual(response.status_code, 200, response.text)
            future = self.occurrence(task_id, 'pending', '2026-11-01' if value else '2026-11-02')
            opened.append(future)
            for occurrence_id in opened + closed + [blank_history]:
                expected = '历史原文' if occurrence_id in closed else None if occurrence_id == blank_history else value
                self.assertEqual(self.conn.execute('select note from planning_occurrence where id=%s',
                                                   (occurrence_id,)).fetchone(), (expected,))
        # Full-schema trigger failure: no template or snapshot can partially commit.
        self.conn.execute("""create function reject_review_note() returns trigger language plpgsql as $$
            begin if new.note='拒绝' then raise exception 'note review rollback'; end if; return new; end $$;
            create trigger reject_review_note before update of note on planning_occurrence
            for each row execute function reject_review_note();""")
        try:
            failed = self.http.patch(f'/admin/api/planning/tasks/{task_id}',
                                     headers=self.headers, json={'note': '拒绝'})
            self.assertEqual(failed.status_code, 500, failed.text)
            self.assertIsNone(self.row(task_id)[3])
            self.assertEqual(self.conn.execute('select count(*) from planning_occurrence '
                                               'where task_id=%s and note=%s', (task_id, '拒绝')).fetchone()[0], 0)
        finally:
            self.conn.execute('drop trigger reject_review_note on planning_occurrence; drop function reject_review_note()')

    def test_guarded_rpc_updates_date_window_and_long_note_together(self):
        task_id = self.once()
        long_note = '🙂' * 10000
        self.client.rpc('planning_update_once_task_guarded', {
            'p_task_id': task_id, 'p_patch': {'target_date': '2026-11-02',
                'window_start_tod': '10:00', 'window_end_tod': '12:00', 'note': long_note},
        }).execute()
        self.assertEqual(self.row(task_id), ('2026-11-02', '10:00:00', '12:00:00', long_note))

    def test_guarded_rpc_waits_for_task_lock_then_rechecks_occurrence_with_note(self):
        task_id = self.once()
        holder = fullchain.psycopg.connect(self.server.get_uri(), autocommit=True)
        writer = fullchain.psycopg.connect(self.server.get_uri(), autocommit=True)
        errors = []
        started = threading.Event()

        def write():
            try:
                started.set()
                writer.execute("set statement_timeout='5s'")
                SqlClient(writer).rpc('planning_update_once_task_guarded', {
                    'p_task_id': task_id, 'p_patch': {'target_date': '2026-11-02', 'note': '锁后禁止'},
                }).execute()
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=write)
        try:
            holder.execute('begin')
            holder.execute('select id from planning_task where id=%s for update', (task_id,))
            holder.execute('select planning_insert_once_occurrence(%s,%s,null,null,%s::jsonb)',
                            (task_id, '2026-11-01', self._once_row_json(task_id)))
            thread.start()
            self.assertTrue(started.wait(2))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if holder.execute('select cardinality(pg_blocking_pids(%s))',
                                  (writer.info.backend_pid,)).fetchone()[0]:
                    break
                time.sleep(0.01)
            else:
                self.fail('note RPC did not wait for the shared task row lock')
            holder.execute('commit')
            thread.join(6)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1, errors)
            self.assertIn('once identity locked', str(errors[0]))
            self.assertEqual(self.row(task_id), ('2026-11-01', None, None, '原备注'))
        finally:
            holder.execute('rollback')
            if thread.ident is not None:
                thread.join(6)
            holder.close()
            writer.close()

    def test_api_create_long_unicode_idempotency_edit_clear(self):
        long_note = '🙂' * 10000
        payload = {'content': '备注完整链路', 'task_type': 'once', 'target_date': '2026-11-01',
                   'estimated_minutes': 30, 'note': long_note}
        headers = {**self.headers, 'Idempotency-Key': 'note-fullchain-create'}
        first = self.http.post('/admin/api/planning/tasks', headers=headers, json=payload)
        self.assertEqual(first.status_code, 201, first.text)
        repeat = self.http.post('/admin/api/planning/tasks', headers=headers, json=payload)
        self.assertEqual(repeat.status_code, 201, repeat.text)
        task_id = first.json()['id']
        self.assertEqual(repeat.json()['id'], task_id)
        self.assertEqual(self.row(task_id)[3], long_note)
        bad = self.http.patch(f'/admin/api/planning/tasks/{task_id}', headers=self.headers,
                               json={'target_date': '2026-11-02', 'note': 123})
        self.assertEqual(bad.status_code, 400, bad.text)
        self.assertEqual(self.row(task_id), ('2026-11-01', None, None, long_note))
        for value in ('一\r\n二🙂', None):
            changed = self.http.patch(f'/admin/api/planning/tasks/{task_id}', headers=self.headers,
                                       json={'note': value})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(self.row(task_id)[3], value.replace('\r\n', '\n') if value else None)
