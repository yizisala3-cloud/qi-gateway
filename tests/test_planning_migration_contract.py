"""规划管理一期迁移契约测试（20260920010000_planning_tasks_occurrences.sql）。"""

import unittest
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260920010000_planning_tasks_occurrences.sql"
)


class PlanningMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def test_migration_is_atomic(self):
        text = self.folded
        begin_positions = [i for i in range(len(text)) if text.startswith("begin;", i)]
        self.assertTrue(begin_positions)
        self.assertIn("commit;", text)

    def test_creates_three_planning_tables(self):
        for table in ("planning_task", "planning_occurrence", "planning_recompute_state"):
            with self.subTest(table=table):
                self.assertIn(f"create table public.{table}", self.folded)
                self.assertIn(
                    f"alter table public.{table} enable row level security", self.folded,
                )

    def test_task_definition_covers_required_capabilities(self):
        for required in (
            "task_type", "interval_days", "weekdays", "month_days", "target_date",
            "time_mode", "estimated_minutes", "est_start_tod", "est_end_tod",
            "is_fixed", "deadline_tod", "deadline_end_tod",
            "is_hollow", "hollow_wait_minutes",
            "alarm_start", "alarm_end", "timer_minutes",
            "is_active", "cursor_date", "next_due",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.folded)

    def test_occurrence_covers_required_capabilities(self):
        for required in (
            "task_id", "for_date", "phase", "est_start", "est_end",
            "actual_start", "actual_end", "actual_minutes",
            "'pending'", "'in_progress'", "'completed'", "'partial'",
            "'deferred'", "'discarded_this'", "'discarded'", "'timeout'",
            "partial_note", "sort_order", "is_fixed", "is_limited",
            "closed_at", "source",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.folded)

    def test_schedule_slot_uniqueness_is_cursored_not_existence_based(self):
        # 生成竞态保护只覆盖排程来源；提前完成的记录不受约束。
        self.assertIn("planning_occurrence_schedule_slot_uq", self.folded)
        self.assertIn("where source = 'schedule'", self.folded)

    def test_recompute_wait_state_is_singleton(self):
        self.assertIn("planning_recompute_state_singleton", self.folded)
        self.assertIn("insert into public.planning_recompute_state (id) values (1)", self.folded)

    def test_migration_never_touches_legacy_tables(self):
        # 零破坏红线：旧 todos 表与聊天原文表绝不改动。
        for forbidden in (
            "alter table public.todos",
            "update public.todos",
            "delete from public.todos",
            "alter table public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_discard_retention_support(self):
        # 72 小时清理依赖 closed_at；已完成/部分完成/延后记录永久保留由
        # 状态枚举与清理实现共同保证（见 tests/test_planning.py）。
        self.assertIn("closed_at", self.folded)


if __name__ == "__main__":
    unittest.main()
