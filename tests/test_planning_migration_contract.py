"""规划管理一期迁移契约测试（20260920010000_planning_tasks_occurrences.sql）。

窗口模型增量迁移（20260927010000_planning_window_model.sql，批次 2）的
契约见 PlanningWindowMigrationContractTests。
"""

import unittest
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260920010000_planning_tasks_occurrences.sql"
)

WINDOW_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260927010000_planning_window_model.sql"
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


class PlanningWindowMigrationContractTests(unittest.TestCase):
    """窗口模型增量迁移契约（20260927010000，批次 2；施工计划 §3）。

    数据库层只固化形状不变量：各端独立可空、双端同时非空时模板不等 /
    实例有序；boundary 跨越与剩余空间可行性属应用层领域函数，本迁移
    不得建业务触发器。既有 1A/1B 约束零改写，旧列全部不 DROP。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = WINDOW_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def test_window_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_only_planning_task_and_occurrence_are_altered(self):
        altered = set()
        for line in self.sql.splitlines():
            stripped = line.strip().casefold()
            if stripped.startswith("alter table public."):
                altered.add(stripped.split()[2])
        self.assertEqual(
            altered, {"public.planning_task", "public.planning_occurrence"},
        )

    def test_template_window_columns_added_independently_nullable(self):
        # §3.1：模板窗口两列各自独立可空（§18 四种组合），不得要求成对。
        self.assertIn(
            "add column if not exists window_start_tod time", self.folded)
        self.assertIn(
            "add column if not exists window_end_tod time", self.folded)

    def test_frozen_window_columns_added_independently_nullable(self):
        # §3.1：实例冻结窗口两列各自独立可空（单侧冻结后只有一端）。
        self.assertIn(
            "add column if not exists window_start_at timestamptz", self.folded)
        self.assertIn(
            "add column if not exists window_end_at timestamptz", self.folded)

    def test_template_shape_check_only_rejects_equal_endpoints(self):
        # §3.2：模板列仅在双端同时非空时校验不等；跨自然午夜（end < start）
        # 不被禁止；任一端为空放行（单侧约束合法且不构成区间）。
        self.assertIn("planning_task_window_tod_shape_check", self.folded)
        self.assertIn("window_start_tod <> window_end_tod", self.folded)
        self.assertIn("window_start_tod is null", self.folded)
        self.assertIn("window_end_tod is null", self.folded)
        # 不存在把跨午夜写法判非法的形状校验。
        self.assertNotIn("window_end_tod < window_start_tod", self.folded)

    def test_occurrence_order_check_only_when_both_present(self):
        # §3.2：实例列仅在双端同时非空时校验有序（绝对瞬间 end > start）。
        self.assertIn("planning_occurrence_window_at_order_check", self.folded)
        self.assertIn("window_end_at > window_start_at", self.folded)
        self.assertIn("window_start_at is null", self.folded)
        self.assertIn("window_end_at is null", self.folded)

    def test_constraint_adds_are_replay_safe(self):
        # §3.4 重复执行安全：约束经 pg_constraint 存在性判断条件添加，
        # 重复重放不报错；列用 add column if not exists。
        self.assertEqual(
            self.folded.count("select 1 from pg_constraint"), 2)
        for name in (
            "planning_task_window_tod_shape_check",
            "planning_occurrence_window_at_order_check",
        ):
            with self.subTest(name=name):
                self.assertIn(f"'{name}'", self.folded)
                self.assertIn(f"add constraint {name}", self.folded)

    def test_no_new_triggers_functions_or_rpc(self):
        # §3.2：不新增窗口业务触发器；boundary 原子 RPC 属批次 7，本批不含。
        for forbidden in (
            "create trigger", "create constraint trigger",
            "create or replace function", "create function",
            "security definer",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_never_drops_or_rewrites_legacy_columns_or_constraints(self):
        # §3.3：旧 explicit / deadline 列全部不 DROP；退役由应用层停止写入
        # 与判定承担，本迁移零改写既有结构。
        for forbidden in (
            "drop column", "drop constraint", "drop trigger", "drop index",
            "drop table", "drop function", "set not null", "set default",
            "not valid",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)
        for legacy in (
            "est_start_tod", "est_end_tod", "deadline_tod", "deadline_end_tod",
            "is_limited", "deadline_at", "is_fixed",
        ):
            with self.subTest(legacy_column=legacy):
                self.assertNotIn(f"drop column {legacy}", self.folded)

    def test_migration_never_touches_legacy_tables_or_writes_rows(self):
        # 零破坏红线：不改写任何既有数据行，不触碰旧表（§3.4 历史实例
        # 保护原则）。
        for forbidden in (
            "alter table public.todos", "alter table public.chat_messages",
            "update public.", "delete from public.", "insert into public.",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)


if __name__ == "__main__":
    unittest.main()
