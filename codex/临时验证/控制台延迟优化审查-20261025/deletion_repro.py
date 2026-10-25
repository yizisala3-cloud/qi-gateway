"""Independent local PostgreSQL probe; never connects to production."""
import os
import sys
import unittest
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
os.environ.setdefault('QIGATEWAY_PG_PLANNING_TEST', '1')

from gateway import planning_commands, planning_runtime
from tests import test_planning_latency_pgserver as latency_base

NOW = latency_base.NOW


class DeletionReceiptReview(unittest.TestCase):
    setUpClass = classmethod(latency_base.PlanningLatencyPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(latency_base.PlanningLatencyPostgresTests.tearDownClass.__func__)
    _query = classmethod(latency_base.PlanningLatencyPostgresTests._query.__func__)

    def setUp(self):
        latency_base.PlanningLatencyPostgresTests.setUp(self)
        self.conn.execute('truncate planning_occurrence_command_request')

    def test_delete_of_never_completed_task_removes_business_body_from_command_receipts(self):
        task_id = self.helper.task()
        occurrence_id = self.helper.occurrence(task_id, '2026-10-18')[0]
        self.conn.execute('update planning_task set content=%s, note=%s where id=%s',
                          ('private task body to delete', 'private task note to delete', task_id))
        key = str(uuid4())
        planning_commands.occurrence_command(occurrence_id,
            {'status': 'in_progress', 'request_key': key}, NOW)
        deleted = planning_runtime._discard_task_atomically(self.client, task_id, NOW)
        self.assertFalse(deleted['history_preserved'])
        self.assertEqual(self.conn.execute('select count(*) from planning_task where id=%s', (task_id,)).fetchone()[0], 0)
        self.assertEqual(self.conn.execute('select count(*) from planning_occurrence where task_id=%s', (task_id,)).fetchone()[0], 0)
        retained = self.conn.execute('select result from planning_occurrence_command_request where request_key=%s', (key,)).fetchone()
        self.assertFalse(retained and ('private task body to delete' in str(retained) or 'private task note to delete' in str(retained)),
                         'Physical deletion retained the complete task body and note in the new command receipt')


if __name__ == '__main__':
    unittest.main(verbosity=2)
