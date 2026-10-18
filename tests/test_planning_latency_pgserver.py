import unittest
from datetime import datetime
from unittest import mock

from gateway import db, memo, planning, planning_generation, planning_runtime
from tests import test_planning_phase1r_pgserver_integration as chain
from tests import test_planning_goals_pgserver as goalbase
from tests.support.planning_goal_sql import GoalSqlClient, GoalSqlQuery

NOW = datetime.fromisoformat("2026-10-18T08:00:00+08:00")


class CountingQuery(GoalSqlQuery):
    def select(self, fields="*", count=None):
        self.exact_count = count == "exact"
        return super().select(fields)

    def range(self, start, end):
        self.bounds = start, end
        return self

    def execute(self):
        self.client.calls.append((self.table, self.operation))
        result = super().execute()
        if getattr(self, "exact_count", False):
            result.count = len(result.data)
        if hasattr(self, "bounds"):
            start, end = self.bounds
            result.data = result.data[start:end + 1]
        return result


class CountingClient(GoalSqlClient):
    def __init__(self, conn):
        super().__init__(conn)
        self.calls = []

    def table(self, table):
        return CountingQuery(self, table)

    def rpc(self, name, params, *, get=False):
        return super().rpc(name, params)


class PlanningLatencyPostgresTests(unittest.TestCase):
    setUpClass = classmethod(chain.PlanningInvariantsOnPostgresTests.setUpClass.__func__)
    tearDownClass = classmethod(chain.PlanningInvariantsOnPostgresTests.tearDownClass.__func__)
    _query = classmethod(chain.PlanningInvariantsOnPostgresTests._query.__func__)

    def setUp(self):
        self.conn.execute("set timezone='Asia/Shanghai'")
        self.conn.execute("truncate planning_task,planning_occurrence,memo_entry,memo_tag restart identity cascade")
        self.conn.execute("delete from app_settings where key like 'planning.%'")
        self.client = CountingClient(self.conn)
        self.helper = goalbase.GoalPostgresTests()
        self.helper.conn = self.conn
        for patch in (mock.patch.object(planning_runtime, "get_client", return_value=self.client),
                      mock.patch.object(db, "get_client", return_value=self.client),
                      mock.patch.object(memo, "get_client", return_value=self.client)):
            patch.start()
            self.addCleanup(patch.stop)

    def seed(self, count=50):
        for _ in range(count):
            task_id = self.helper.task()
            self.helper.occurrence(task_id, "2026-10-18")

    def test_stable_fifty_tasks_skip_writes_but_missing_round_still_uses_guarded_rpc(self):
        self.seed()
        self.client.calls.clear()
        with mock.patch.object(planning_generation, "_reconcile_task_rounds",
                               wraps=planning_generation._reconcile_task_rounds) as reconcile:
            result = planning.generate_due(NOW)
        self.assertEqual(result["created"], 0)
        reconcile.assert_not_called()
        self.assertEqual(len(self.client.calls), 4)
        self.assertTrue(all(operation == "select" for _, operation in self.client.calls))
        self.conn.execute("delete from planning_occurrence where task_id=50")
        with mock.patch.object(planning_generation, "_reconcile_task_rounds",
                               wraps=planning_generation._reconcile_task_rounds) as reconcile:
            result = planning.generate_due(NOW)
        self.assertEqual(result["created"], 1)
        self.assertEqual([call.args[1]["id"] for call in reconcile.call_args_list], [50])
        self.assertEqual(self.conn.execute("select count(*) from planning_occurrence").fetchone()[0], 50)

    def test_resume_and_legacy_save_do_not_generate_unrelated_missing_rounds(self):
        self.seed()
        self.conn.execute("delete from planning_occurrence where task_id=50")
        self.conn.execute("update planning_task set refresh_enabled=false where id=1")
        for changes in ({"refresh_enabled": True}, {"estimated_minutes": 31}):
            with mock.patch.object(planning_generation, "generate_task",
                                   wraps=planning_generation.generate_task) as generate:
                planning.update_task(1, changes, NOW)
            self.assertEqual([call.args[0] for call in generate.call_args_list], [1])
            self.assertEqual(self.conn.execute("select count(*) from planning_occurrence where task_id=50").fetchone()[0], 0)
        self.assertEqual(planning.generate_due(NOW)["created"], 1)

    def test_old_daily_and_hollow_rounds_still_expire_as_complete_pairs(self):
        self.seed(1)
        hollow = self.helper.task(hollow=True)
        self.helper.occurrence(hollow, "2026-10-18", hollow=True)
        old_ids = self.helper.occurrence(1, "2026-10-17")
        old_ids += self.helper.occurrence(hollow, "2026-10-17", hollow=True)
        result = planning.generate_due(NOW)
        self.assertEqual(result["timed_out"], 3)
        old = self.conn.execute("select status,display_cycle_date from planning_occurrence where id=any(%s)", (old_ids,)).fetchall()
        self.assertTrue(all(status == "timeout" and day.isoformat() == "2026-10-18" for status, day in old))
        self.assertEqual(self.conn.execute("select count(*) from planning_occurrence where schedule_date='2026-10-18'").fetchone()[0], 3)

    def test_parallel_memo_reads_preserve_150_rows_unicode_order_and_no_duplicates(self):
        self.conn.execute("insert into memo_entry(content,title,kind) select '正文 Straße '||n,'测试 '||n,'note' from generate_series(1,150) n")
        self.conn.execute("insert into memo_tag(name) values ('测试标签')")
        self.conn.execute("insert into memo_entry_tag(entry_id,tag_id) select id,1 from memo_entry")
        board = memo.get_board()
        self.assertEqual(sum(len(section["items"]) for section in board["sections"]), 150)
        results = memo.list_entries("active", "STRASSE")
        self.assertEqual(len(results), 150)
        self.assertEqual(len({entry["id"] for entry in results}), 150)
        self.assertTrue(all(entry["tags"][0]["name"] == "测试标签" for entry in results))
        self.assertEqual(memo.get_entry(results[0]["id"])["id"], results[0]["id"])
