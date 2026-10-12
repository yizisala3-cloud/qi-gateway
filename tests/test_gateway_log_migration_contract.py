"""网关日志迁移 SQL 契约测试（§5.2 / §7.2 / §10 阶段 B）。

静态核对迁移文件的安全与语义契约：RLS 收权、service_role 显式授权、
幂等可重复执行、事件幂等（ON CONFLICT DO NOTHING 由写入方 upsert
ignore_duplicates 实现，此处核对表主键与 RPC 存在）、清理按真实
occurred_at 分批 + advisory 锁、不触碰既有业务表。
"""

import unittest
from pathlib import Path

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20261010010000_gateway_error_events.sql"
)


class GatewayLogMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.lowered = cls.sql.casefold()

    def test_file_exists(self):
        self.assertTrue(MIGRATION.exists())

    def test_never_alters_existing_business_tables(self):
        for table in (
            "alter table public.planning_task",
            "alter table public.planning_occurrence",
            "alter table public.memories",
            "alter table public.chat_messages",
            "alter table public.memo",
            "update public.planning_task",
            "delete from public.memories",
        ):
            with self.subTest(statement=table):
                self.assertNotIn(table, self.lowered)

    def test_table_uses_idempotent_create(self):
        self.assertIn("create table if not exists public.gateway_error_events", self.lowered)
        self.assertIn("create or replace function public.gateway_log_query", self.lowered)
        self.assertIn("create or replace function public.gateway_log_event", self.lowered)
        self.assertIn("create or replace function public.gateway_log_group", self.lowered)
        self.assertIn("create or replace function public.gateway_log_facets", self.lowered)
        self.assertIn("create or replace function public.gateway_log_cleanup", self.lowered)

    def test_event_id_is_primary_key_for_idempotent_writes(self):
        self.assertIn("event_id uuid primary key", self.lowered)

    def test_level_and_outcome_are_constrained(self):
        self.assertIn("check (level in ('warning', 'error', 'critical'))", self.lowered)
        self.assertIn(
            "check (outcome in ('', 'failed', 'degraded', 'committed_side_effect_failed'))",
            self.lowered)

    def test_rls_enabled_with_explicit_lockdown(self):
        self.assertIn(
            "alter table public.gateway_error_events enable row level security", self.lowered)
        self.assertIn(
            "revoke all on table public.gateway_error_events\n    from public, anon, authenticated",
            self.lowered)
        self.assertIn(
            "grant select, insert, delete\n    on public.gateway_error_events to service_role",
            self.lowered)
        # 无策略：默认拒绝（不能出现 to anon / to authenticated 的策略）
        self.assertNotIn("create policy", self.lowered)

    def test_functions_use_security_invoker_and_pinned_search_path(self):
        for name in (
            "gateway_log_query", "gateway_log_event", "gateway_log_group",
            "gateway_log_facets", "gateway_log_cleanup",
        ):
            with self.subTest(function=name):
                self.assertIn("security invoker", self.lowered)
        self.assertEqual(self.lowered.count("set search_path = public"), 5)
        # 函数收权：不给 public / anon / authenticated
        self.assertIn("revoke all on function public.gateway_log_query", self.lowered)
        self.assertIn("grant execute on function public.gateway_log_query", self.lowered)

    def test_query_aggregates_by_fingerprint_with_stable_cursor(self):
        self.assertIn("group by e.fingerprint", self.lowered)
        self.assertIn("order by last_seen_at desc, fingerprint desc", self.lowered)
        # BUG-05：已看集合分页（快照 + 可见性快照 + p_seen），不再使用 keyset 游标
        self.assertIn("p_seen", self.lowered)
        self.assertIn("p_visible", self.lowered)
        self.assertIn("pg_visible_in_snapshot", self.lowered)
        self.assertIn("pg_current_snapshot", self.lowered)
        self.assertNotIn("p_cursor_last_seen", self.lowered)
        self.assertNotIn("p_cursor_fingerprint", self.lowered)
        # 查询上限：limit 被钳制，不允许无限翻页
        self.assertIn("least(greatest(coalesce(p_limit, 50), 1), 200)", self.lowered)

    def test_group_detail_limits_samples(self):
        self.assertIn("least(greatest(coalesce(p_sample_limit, 20), 1), 50)", self.lowered)
        # BUG-05：详情与列表共享会话可见性快照
        self.assertIn("pg_visible_in_snapshot", self.lowered)

    def test_key_identity_columns_and_filter_exist(self):
        # TASK-02：key_ref / credential_role / provider 列 + 索引 + 筛选参数。
        for column in ("key_ref", "credential_role", "provider"):
            with self.subTest(column=column):
                self.assertIn(f"{column} text not null default ''", self.lowered)
        self.assertIn("gateway_error_events_key_ref_idx", self.lowered)
        self.assertIn("p_key_ref", self.lowered)
        # commit_xid：插入事务的 64 位事务 ID（可见性快照锚点，BUG-05）
        self.assertIn("commit_xid xid8 not null default pg_current_xact_id()", self.lowered)
        # 幂等补列（本批未发布迁移的原位更新）
        self.assertIn("add column if not exists commit_xid", self.lowered)
        # 内部可见性锚点不进入公开投影
        self.assertIn("- 'commit_xid'", self.lowered)

    def test_facets_include_key_options(self):
        # TASK-02：facets 返回去重 key_ref 选项驱动日志页密钥筛选。
        self.assertIn("'keys'", self.lowered)
        self.assertIn("where key_ref <> ''", self.lowered)

    def test_cleanup_uses_real_occurred_at_with_batches_and_advisory_lock(self):
        self.assertIn("where occurred_at < v_cutoff", self.lowered)
        self.assertIn("pg_try_advisory_xact_lock", self.lowered)
        # 事务级锁（自动释放），不用会话级锁
        self.assertNotIn("pg_try_advisory_lock(", self.lowered.replace(
            "pg_try_advisory_xact_lock(", ""))
        self.assertIn("skipped_locked", self.lowered)
        # 分批删除有轮次上限
        self.assertIn("v_rounds >= 100", self.lowered)

    def test_migration_is_wrapped_in_transaction(self):
        # 文件以说明注释开头；首个实际语句必须是 begin，结尾是 commit
        stripped = "\n".join(
            line for line in self.sql.splitlines()
            if line.strip() and not line.lstrip().startswith("--")
        )
        self.assertTrue(stripped.lstrip().startswith("begin;"))
        self.assertTrue(self.lowered.rstrip().endswith("commit;"))

    def test_required_query_indexes_exist(self):
        for index in (
            "gateway_error_events_occurred_idx",
            "gateway_error_events_module_level_idx",
            "gateway_error_events_fingerprint_idx",
            "gateway_error_events_level_idx",
            "gateway_error_events_request_idx",
            "gateway_error_events_key_ref_idx",
        ):
            with self.subTest(index=index):
                self.assertIn(index, self.lowered)


if __name__ == "__main__":
    unittest.main()
