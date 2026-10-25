"""Independent rereview boundaries, using a disposable PostgreSQL server."""
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault('QIGATEWAY_PG_PLANNING_TEST', '1')
from gateway import planning_commands, planning_common, planning_runtime
from tests import test_planning_latency_pgserver as base
from tests.support.planning_goal_sql import GoalSqlClient


class ReceiptDeletionBoundaries(unittest.TestCase):
    setUpClass = classmethod(base.PlanningLatencyPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(base.PlanningLatencyPostgresTests.tearDownClass.__func__)
    _query = classmethod(base.PlanningLatencyPostgresTests._query.__func__)

    def setUp(self):
        base.PlanningLatencyPostgresTests.setUp(self)
        self.conn.execute('truncate planning_occurrence_command_request, planning_task_completion_fact')

    def seed(self):
        task_id = self.helper.task()
        return task_id, self.helper.occurrence(task_id, '2026-10-18')[0], str(uuid4())

    def start(self, occurrence_id, key):
        return planning_commands.occurrence_command(occurrence_id,
            {'status': 'in_progress', 'request_key': key}, base.NOW)

    def test_service_role_can_scrub_receipts_and_reject_deleted_replay(self):
        task_id, occurrence_id, key = self.seed()
        # Model Supabase's table/sequence default privileges in the local fixture.
        self.conn.execute('grant all on all tables in schema public to service_role; '
                          'grant usage,select on all sequences in schema public to service_role')
        self.conn.execute('set role service_role')
        try:
            self.assertEqual(self.start(occurrence_id, key)['status'], 'in_progress')
            deleted = planning_runtime._discard_task_atomically(self.client, task_id, base.NOW)
            self.assertFalse(deleted['history_preserved'])
            self.assertEqual(self.conn.execute('select result from planning_occurrence_command_request where request_key=%s',
                                             (key,)).fetchone()[0], {'deleted': True})
            with self.assertRaises(planning_common.PlanningError) as rejected:
                self.start(occurrence_id, key)
            self.assertEqual(rejected.exception.status_code, 404)
        finally:
            self.conn.execute('reset role')

    def test_waiting_delete_erases_a_receipt_that_commits_after_delete_was_dispatched(self):
        import psycopg
        task_id, occurrence_id, key = self.seed()
        ready = threading.Event()
        worker = {}

        def delete():
            with psycopg.connect(self.server.get_uri(), autocommit=True) as conn:
                conn.execute("set statement_timeout='8s'")
                worker['pid'] = conn.info.backend_pid
                ready.set()
                return planning_runtime._discard_task_atomically(GoalSqlClient(conn), task_id, base.NOW)

        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.conn.transaction():
                self.start(occurrence_id, key)
                pending = pool.submit(delete)
                self.assertTrue(ready.wait(2))
                deadline = time.monotonic() + 3
                blocked = False
                while time.monotonic() < deadline:
                    blocked = bool(self.conn.execute('select cardinality(pg_blocking_pids(%s)) > 0',
                                                   (worker['pid'],)).fetchone()[0])
                    if blocked:
                        break
                    time.sleep(0.01)
                self.assertTrue(blocked, 'delete must actually wait on the uncommitted command task lock')
            self.assertFalse(pending.result(timeout=8)['history_preserved'])
        self.assertEqual(self.conn.execute('select result from planning_occurrence_command_request where request_key=%s',
                                         (key,)).fetchone()[0], {'deleted': True})
        self.assertEqual(self.conn.execute('select count(*) from planning_task where id=%s', (task_id,)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
