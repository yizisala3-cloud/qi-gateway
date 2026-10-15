"""Execute the note migration on isolated PostgreSQL, including lock races."""
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / 'supabase/migrations/20261014010000_planning_task_notes.sql'
UNLIMITED_MIGRATION = ROOT / 'supabase/migrations/20261015010000_planning_task_notes_unlimited.sql'


class TaskNotePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.environ.get('QIGATEWAY_PG_PLANNING_TEST') != '1':
            raise unittest.SkipTest('set QIGATEWAY_PG_PLANNING_TEST=1 to run real note transactions')
        import pgserver
        import psycopg
        cls.psycopg = psycopg
        cls.directory = tempfile.TemporaryDirectory(prefix='qigate-note-pg-')
        cls.server = pgserver.get_server(Path(cls.directory.name), cleanup_mode='stop')
        cls.conn = psycopg.connect(cls.server.get_uri(), autocommit=True)
        cls.conn.execute('''
            create role anon nologin;
            create role authenticated nologin;
            create role service_role nologin bypassrls;
            create table public.planning_task (
                id bigint generated always as identity primary key,
                content text not null default '测试待办',
                updated_at timestamptz not null default now());
            create table public.planning_occurrence (
                id bigint generated always as identity primary key,
                task_id bigint not null references public.planning_task(id),
                status text not null default 'pending',
                actual_start timestamptz, partial_note text,
                updated_at timestamptz not null default now());
        ''')
        cls.conn.execute(MIGRATION.read_text(encoding='utf-8'))
        cls.conn.execute(UNLIMITED_MIGRATION.read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, 'conn', None):
            cls.conn.close()
        if getattr(cls, 'server', None):
            cls.server.cleanup()
        if getattr(cls, 'directory', None):
            cls.directory.cleanup()

    def setUp(self):
        self.conn.execute('truncate planning_occurrence, planning_task restart identity cascade')
        self.task_id = self.conn.execute("insert into planning_task(note) values ('最初备注') returning id").fetchone()[0]

    def occurrence(self, status='pending'):
        return self.conn.execute('insert into planning_occurrence(task_id,status) values (%s,%s) returning id',
                                 (self.task_id, status)).fetchone()[0]

    def assert_waiting_for_lock(self, connection):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            blocked = self.conn.execute('select cardinality(pg_blocking_pids(%s))',
                                        (connection.info.backend_pid,)).fetchone()[0]
            if blocked:
                return
            time.sleep(0.01)
        self.fail('the competing connection did not reach the expected row-lock wait')

    def test_updates_only_open_and_future_notes_and_can_clear(self):
        opened = [self.occurrence(status) for status in ('pending', 'in_progress', 'partial', 'deferred')]
        history = [self.occurrence(status) for status in ('completed', 'timeout', 'discarded', 'discarded_this')]
        self.conn.execute("update planning_task set note='新备注' where id=%s", (self.task_id,))
        for row_id, expected in [(row, '新备注') for row in opened] + [(row, '最初备注') for row in history]:
            self.assertEqual(self.conn.execute('select note from planning_occurrence where id=%s', (row_id,)).fetchone()[0], expected)
        future = self.occurrence()
        self.assertEqual(self.conn.execute('select note from planning_occurrence where id=%s', (future,)).fetchone()[0], '新备注')
        self.conn.execute('update planning_task set note=null where id=%s', (self.task_id,))
        self.assertEqual(self.conn.execute('select count(*) from planning_occurrence where note is null').fetchone()[0], 5)
        self.assertEqual(self.conn.execute("select count(*) from planning_occurrence where note='最初备注'").fetchone()[0], 4)

    def test_historical_null_and_text_are_both_immutable(self):
        history = self.occurrence('completed')
        with self.assertRaises(self.psycopg.Error):
            self.conn.execute("update planning_occurrence set note='不能改历史' where id=%s", (history,))
        self.conn.execute('update planning_task set note=null where id=%s', (self.task_id,))
        blank = self.occurrence('timeout')
        self.conn.execute("update planning_task set note='未来备注' where id=%s", (self.task_id,))
        self.assertIsNone(self.conn.execute('select note from planning_occurrence where id=%s', (blank,)).fetchone()[0])

    def test_long_notes_sync_without_truncation_and_migration_is_repeatable(self):
        opened = self.occurrence()
        history = self.occurrence('completed')
        self.conn.execute(UNLIMITED_MIGRATION.read_text(encoding='utf-8'))
        for value in ('字' * 61, '🙂' * 61, '长备注🙂\n' * 10000):
            self.conn.execute('update planning_task set note=%s where id=%s', (value, self.task_id))
            self.assertEqual(self.conn.execute('select note from planning_occurrence where id=%s', (opened,)).fetchone()[0], value)
            self.assertEqual(self.conn.execute('select note from planning_occurrence where id=%s', (history,)).fetchone()[0], '最初备注')

    def test_any_snapshot_failure_rolls_back_template_and_all_snapshots(self):
        first = self.occurrence()
        second = self.occurrence()
        self.conn.execute('''create function fail_note_for_test() returns trigger language plpgsql as $$
            begin if new.note = '拒绝' and new.id = 2 then raise exception 'test failure'; end if; return new; end $$;
            create trigger fail_note_for_test before update on planning_occurrence
            for each row execute function fail_note_for_test();''')
        try:
            with self.assertRaises(self.psycopg.Error):
                self.conn.execute("update planning_task set note='拒绝' where id=%s", (self.task_id,))
            self.assertEqual(self.conn.execute('select note from planning_task').fetchone()[0], '最初备注')
            self.assertEqual(self.conn.execute('select note from planning_occurrence order by id').fetchall(), [('最初备注',), ('最初备注',)])
        finally:
            self.conn.execute('drop trigger fail_note_for_test on planning_occurrence; drop function fail_note_for_test()')

    def test_concurrent_completion_preserves_the_completed_snapshot(self):
        occurrence = self.occurrence()
        other = self.psycopg.connect(self.server.get_uri(), autocommit=True)
        self.addCleanup(other.close)
        started = threading.Event()
        errors = []
        def update_note():
            try:
                other.execute("set statement_timeout='5s'")
                started.set()
                other.execute("update planning_task set note='完成之后的新备注' where id=%s", (self.task_id,))
            except Exception as error:
                errors.append(error)
        with self.conn.transaction():
            self.conn.execute("update planning_occurrence set status='completed' where id=%s", (occurrence,))
            worker = threading.Thread(target=update_note)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assert_waiting_for_lock(other)
        worker.join(7)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.conn.execute('select note from planning_occurrence where id=%s', (occurrence,)).fetchone()[0], '最初备注')

    def test_migration_is_repeatable_without_rewriting_history(self):
        self.occurrence('completed')
        self.conn.execute("update planning_task set note='之后的备注' where id=%s", (self.task_id,))
        self.conn.execute(MIGRATION.read_text(encoding='utf-8'))
        self.assertEqual(self.conn.execute('select note from planning_occurrence').fetchone()[0], '最初备注')

    def test_insert_waits_for_current_template_instead_of_using_stale_payload(self):
        other = self.psycopg.connect(self.server.get_uri(), autocommit=True)
        self.addCleanup(other.close)
        started = threading.Event()
        values = []
        def insert():
            try:
                other.execute("set statement_timeout='5s'")
                started.set()
                values.append(other.execute('insert into planning_occurrence(task_id,note) values (%s,%s) returning note',
                                             (self.task_id, '过期的客户端文本')).fetchone()[0])
            except Exception as error:
                values.append(error)
        with self.conn.transaction():
            self.conn.execute("update planning_task set note='刚更新的模板' where id=%s", (self.task_id,))
            worker = threading.Thread(target=insert)
            worker.start()
            self.assertTrue(started.wait(2))
            self.assert_waiting_for_lock(other)
        worker.join(7)
        self.assertFalse(worker.is_alive())
        self.assertEqual(values, ['刚更新的模板'])
