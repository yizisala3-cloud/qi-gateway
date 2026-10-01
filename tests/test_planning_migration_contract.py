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

RECOMPUTE_IDENTITY_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260930010000_planning_recompute_request_identity.sql"
)

BOUNDARY_RPC_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260930020000_planning_update_cycle_boundary.sql"
)

BOUNDARY_WINDOW_GUARD_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260930030000_planning_boundary_window_guard.sql"
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


FIXED_EXPIRATION_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260928010000_planning_fixed_expiration_freeze.sql"
)


class PlanningFixedExpirationFreezeMigrationContractTests(unittest.TestCase):
    """固定到期死亡边界实例级冻结迁移契约（20260928010000，批次 5 三轮裁决）。

    固定刷新型「到达下一规则点死亡」的死亡边界 = 轴上晚于本轮 due 的下一个
    规则事件，由生成入口按**生成当时的规则**随行冻结（与 window /
    planned_minutes / content 快照同一「生成即冻结」模式）。规则后续编辑只
    影响未来未生成实例，不得追溯重解释已生成轮次的生命周期——本迁移只提供
    冻结载体：单列纯增量、可空、无回填、无触发器 / CHECK / RPC。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = FIXED_EXPIRATION_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def test_freeze_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_only_planning_occurrence_is_altered(self):
        altered = set()
        for line in self.sql.splitlines():
            stripped = line.strip().casefold()
            if stripped.startswith("alter table public."):
                altered.add(stripped.split()[2])
        self.assertEqual(altered, {"public.planning_occurrence"})

    def test_frozen_boundary_column_added_nullable(self):
        self.assertIn(
            "add column if not exists fixed_expires_at timestamptz", self.folded)

    def test_column_is_documented(self):
        self.assertIn("comment on column public.planning_occurrence.fixed_expires_at",
                      self.folded)

    def test_no_backfill_no_trigger_no_check_no_rpc(self):
        # user 裁决：存量行为 NULL、不按当前规则回算（受控部署清理，不建
        # backfill）；数据库层不建触发器 / CHECK / RPC。
        for forbidden in (
            "update public.planning_occurrence",
            "create trigger", "create constraint trigger",
            "create or replace function", "create function",
            "security definer", "add constraint",
            "set not null", "set default",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_never_drops_or_rewrites_existing_structure(self):
        for forbidden in (
            "drop column", "drop constraint", "drop trigger", "drop index",
            "drop table", "drop function",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_replay_safe(self):
        self.assertIn("add column if not exists fixed_expires_at", self.folded)

    def test_migration_never_touches_legacy_tables_or_writes_rows(self):
        for forbidden in (
            "alter table public.todos", "alter table public.chat_messages",
            "update public.", "delete from public.", "insert into public.",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)


ROUND_PATCH_RPC_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260928020000_planning_patch_occurrence_round.sql"
)


class PlanningRoundPatchRpcMigrationContractTests(unittest.TestCase):
    """hollow 同轮原子补丁 RPC 迁移契约（20260928020000，批次 6 二轮裁决）。

    user 批准的最小数据库事务能力：实现批次 6 已有的同轮原子编辑语义，
    不是新产品功能。只 CREATE OR REPLACE 单个函数——不新增表列 / 触发器、
    不改旧迁移、不回填数据、重放安全。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = ROUND_PATCH_RPC_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def _top_level_statements(self):
        """函数体（$$ 块）之外的顶层语句序列。"""
        statements, buf = [], []
        in_dollar = False
        for line in self.sql.splitlines():
            stripped = line.strip()
            if not in_dollar and stripped.casefold().startswith("create or replace function"):
                buf.append(stripped)
                in_dollar = True
                continue
            if in_dollar:
                if stripped == "$$;":
                    in_dollar = False
                continue
            if stripped:
                buf.append(stripped)
        return [item.casefold() for item in buf]

    def test_migration_is_atomic(self):
        folded = self.folded
        self.assertIn("begin;", folded)
        self.assertIn("commit;", folded)

    def test_creates_exactly_the_round_patch_function(self):
        top = self._top_level_statements()
        creating = [item for item in top if item.startswith("create")]
        self.assertEqual(creating, [
            "create or replace function "
            "public.planning_patch_occurrence_round("])

    def test_no_table_column_trigger_index_changes(self):
        for forbidden in (
            "alter table", "add column", "create trigger", "create table",
            "create index", "add constraint", "set not null", "set default",
            "comment on column",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_never_drops_or_rewrites_existing_structure(self):
        for forbidden in (
            "drop column", "drop constraint", "drop trigger", "drop index",
            "drop table", "drop function", "drop policy",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_no_data_backfill(self):
        top = [item for item in self._top_level_statements()
               if not item.startswith(("begin;", "commit;"))]
        for item in top:
            for forbidden in ("update ", "insert into", "delete from"):
                with self.subTest(statement=item, forbidden=forbidden):
                    self.assertFalse(item.startswith(forbidden))

    def test_function_is_replay_safe_by_replace(self):
        self.assertIn(
            "create or replace function public.planning_patch_occurrence_round(",
            self.folded)

    def test_function_has_hard_whitelist_and_pair_validation(self):
        # 硬白名单（未知 key 拒绝）、行锁、同轮身份校验、兄弟行白名单更小
        folded = self.folded
        self.assertIn("contains unsupported field", folded)
        self.assertIn("for update", folded)
        self.assertIn("order by id", folded)
        self.assertIn("rows are not a hollow start/end pair", folded)
        self.assertIn("occurrence rows not found", folded)
        self.assertNotIn("format(", folded)  # 无动态 SQL 拼接
        self.assertIn("sibling patch contains unsupported field", folded)
        self.assertIn("target patch contains unsupported field", folded)

    def test_function_enforces_in_lock_lifecycle_gate(self):
        # 最终修复问题 1：锁内生命周期二次校验——窗口字段永远严格门（问题 5
        # 收紧：status 不能降低保护）；仅纯开放状态流转用宽松门。拒绝统一
        # 携带固定 ERRCODE PC001（问题 6：稳定错误标识，不模糊字符串匹配）。
        folded = self.folded
        self.assertIn("only open statuses can be written", folded)
        self.assertEqual(folded.count("round is no longer editable"), 2)
        self.assertEqual(folded.count("using errcode = 'pc001'"), 3)
        self.assertIn("or not (p_target_patch ? 'status')", folded)
        self.assertIn("status not in ('pending', 'deferred')", folded)
        self.assertIn("status not in ('pending', 'in_progress', 'deferred', 'partial')", folded)
        self.assertIn("actual_start is not null", folded)
        self.assertIn("partial_at is not null", folded)
        self.assertIn("closed_at is not null", folded)
        self.assertIn("handled_at is not null", folded)

    def test_function_verifies_expected_snapshot(self):
        # 最终验收修复问题 3：重算的 expected snapshot 锁内复核——状态 /
        # 窗口 / est 预态 / 所有权任一漂移即拒绝（stale schedule 不写回）。
        folded = self.folded
        self.assertIn("p_expected jsonb default null", folded)
        self.assertIn("schedule inputs drifted", folded)
        self.assertIn("jsonb_array_elements(p_expected)", folded)
        self.assertIn("is distinct from (e.value->>'window_start_at')::timestamptz", folded)
        self.assertIn("is distinct from e.value->>'estimated_time_source'", folded)



    def test_function_enforces_window_consistency(self):
        # 最终修复问题 7：任一补丁触及窗口字段时两阶段生效后窗口必须一致。
        folded = self.folded
        self.assertIn("hollow phases disagree on window", folded)
        self.assertIn("p_sibling_patch ? 'window_start_at'", folded)
        self.assertIn("p_sibling_patch ? 'window_end_at'", folded)

    def test_function_never_writes_terminal_status(self):
        # RPC 是同轮开放生命周期的原子编辑载体：终态值一律拒绝。
        folded = self.folded
        self.assertIn(
            "coalesce(p_target_patch->>'status') not in", folded)
        self.assertIn("('pending', 'in_progress', 'deferred', 'partial')", folded)

    def test_function_does_not_execute_with_security_definer(self):
        # 与既有 planning RPC（takeover/absorb）一致：保持 invoker 语义，
        # 权限沿用网关连接，不新增 definer 提权面。
        self.assertNotIn("security definer", self.folded)


ONCE_IDENTITY_LOCKS_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260928030000_planning_once_identity_locks.sql"
)


class PlanningOnceIdentityLocksMigrationContractTests(unittest.TestCase):
    """once 身份编辑 / 生成的跨进程任务行锁守护 RPC 契约（20260928030000）。

    user 批准的最小数据库锁机制（最终修复问题 3）：编辑侧锁任务行 → 复核
    无 occurrence → 原子保存；生成侧锁同一任务行 → 校验定义未漂移 → 插入。
    仅 create or replace function；不新增锁表、不改表结构、重放安全。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = ONCE_IDENTITY_LOCKS_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def _top_level_statements(self):
        statements, buf = [], []
        in_dollar = False
        for line in self.sql.splitlines():
            stripped = line.strip()
            if not in_dollar and stripped.casefold().startswith("create or replace function"):
                buf.append(stripped)
                in_dollar = True
                continue
            if in_dollar:
                if stripped == "$$;":
                    in_dollar = False
                continue
            if stripped:
                buf.append(stripped)
        return [item.casefold() for item in buf]

    def test_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_creates_exactly_three_guard_functions(self):
        creating = [item for item in self._top_level_statements() if item.startswith("create")]
        self.assertEqual(len(creating), 3)
        self.assertIn("public.planning_update_once_task_guarded(", creating[0])
        self.assertIn("public.planning_insert_once_occurrence(", creating[1])
        self.assertIn("public.planning_insert_round_occurrence(", creating[2])

    def test_creates_round_occurrence_guard(self):
        # 明日可用 BLOCKER 2：非 once 生成的锁内复核 RPC（active / 定义漂移）。
        folded = self.folded
        self.assertIn("create or replace function public.planning_insert_round_occurrence(", folded)
        self.assertIn("planning_insert_round_occurrence: task no longer active", folded)
        self.assertIn("planning_insert_round_occurrence: task definition changed during generation", folded)
        self.assertIn("p_expected_task jsonb", folded)
        self.assertIn("'window_end_tod'", folded)
        self.assertIn("current_row.window_end_tod is distinct from", folded)
        self.assertIn("current_row.refresh_anchor_at is distinct from", folded)
        self.assertIn("drop function if exists public.planning_insert_round_occurrence(bigint, jsonb)", folded)

    def test_no_table_or_trigger_changes_and_no_backfill(self):
        for forbidden in (
            "alter table", "add column", "create trigger", "create table",
            "create index", "drop table", "insert into public.planning_task",
            "update public.planning_task set ", "delete from public.",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.folded)

    def test_edit_guard_locks_task_row_and_rechecks_occurrence(self):
        folded = self.folded
        self.assertIn("for update", folded)
        self.assertIn("once identity locked: occurrence exists", folded)
        self.assertIn("patch contains unsupported field", folded)

    def test_generation_guard_validates_definition_drift(self):
        folded = self.folded
        self.assertIn("once definition changed during generation", folded)
        self.assertIn("rows must be pending schedule occurrences", folded)
        self.assertIn("target_date is distinct from p_expected_target_date", folded)

    def test_generation_guard_verifies_all_frozen_inputs(self):
        # 最终 Debug 问题 2：锁内复核全部会冻结进 occurrence 的任务输入
        #（content / estimated_minutes / time_mode / hollow 形状配置）。
        folded = self.folded
        self.assertIn("p_expected_task jsonb default null", folded)
        self.assertIn("expected task contains unsupported field", folded)
        self.assertIn("content is distinct from p_expected_task->>'content'", folded)
        self.assertIn(
            "estimated_minutes is distinct from (p_expected_task->>'estimated_minutes')::integer",
            folded)
        self.assertIn("time_mode is distinct from p_expected_task->>'time_mode'", folded)
        self.assertIn(
            "is_hollow is distinct from (p_expected_task->>'is_hollow')::boolean", folded)


DISCARD_TASK_RPC_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260928040000_planning_discard_task_rpc.sql"
)


class PlanningDiscardTaskRpcMigrationContractTests(unittest.TestCase):
    """「废弃整个任务」原子 RPC 契约（20260928040000，最终验收修复问题 5）。

    跨 task + occurrence 的原子命令：锁任务行 → 单语句关闭全部开放
    occurrence（中空两阶段同语句命中）→ 单语句停用任务；任务停用未命中
    （并发停用）→ PC001 整体回滚。仅承载既有废弃语义，非通用事务框架。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = DISCARD_TASK_RPC_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def test_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_creates_exactly_the_discard_function(self):
        top = [ln.strip().casefold() for ln in self.sql.splitlines() if ln.strip()]
        creating = [item for item in top if item.startswith("create")]
        self.assertEqual(creating, [
            "create or replace function public.planning_discard_task("])

    def test_locks_task_and_closes_occurrences_before_deactivate(self):
        folded = self.folded
        # 锁任务行（FOR UPDATE）→ occurrence 关闭 → task 停用的顺序
        lock_pos = folded.find("for update")
        occ_pos = folded.find("update public.planning_occurrence")
        task_pos = folded.find("update public.planning_task")
        self.assertGreaterEqual(lock_pos, 0)
        self.assertLess(occ_pos, task_pos)
        # 关闭只写终态字段；命中开放状态
        self.assertIn("status = 'discarded'", folded)
        self.assertIn("status in ('pending', 'in_progress', 'deferred', 'partial')", folded)
        self.assertIn("is_active = false", folded)
        # 并发停用拒绝携带 PC001
        self.assertIn("task already inactive (concurrent change)", folded)
        self.assertIn("using errcode = 'pc001'", folded)

    def test_no_wider_than_formal_discard_semantics(self):
        folded = self.folded
        # 仅改 is_active / updated_at（任务行）与 status/closed_at/updated_at
        # + 目标行 actual 字段（occurrence 行）；不 DROP、不回填、不做任意
        # 补丁（jsonb 仅用于目标行 actual 白名单传参）。
        self.assertNotIn("drop ", folded)
        self.assertNotIn("delete from", folded)
        self.assertIn("target patch contains unsupported field", folded)

    def test_replay_safe(self):
        self.assertIn(
            "create or replace function public.planning_discard_task(", self.folded)

    def test_target_facts_write_inside_transaction(self):
        # 最终 Debug 问题 1B：目标行 actual 事实作为 RPC 输入在同一事务内
        # 写入（白名单仅 actual 字段）；不再允许 RPC 外补写关键事实。
        folded = self.folded
        self.assertIn("p_target_id bigint default null", folded)
        self.assertIn("p_target_patch jsonb default null", folded)
        self.assertIn("target patch contains unsupported field", folded)
        target_pos = folded.find("update public.planning_occurrence")
        task_pos = folded.find("update public.planning_task")
        target_facts_pos = folded.find("p_target_patch ? 'actual_end'")
        self.assertGreaterEqual(target_facts_pos, 0)
        # 目标行事实写入发生在 task 停用之前（同一事务内顺序）
        self.assertLess(target_facts_pos if target_facts_pos >= 0 else 0, task_pos)
        # 白名单仅 actual 字段
        self.assertIn(
            "'actual_start', 'actual_end', 'actual_minutes', 'updated_at'", folded)


DISCARD_INPROGRESS_FACTS_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20261001010000_planning_discard_task_inprogress_facts.sql"
)


class PlanningDiscardTaskInprogressFactsMigrationContractTests(unittest.TestCase):
    """「废弃整个任务」执行中事实补齐契约（20261001010000，清单 #16 第二轮）。

    审查 R1：耗时是起止的派生输出——执行中行的耗时必须按锁内最终采用的
    起止在事务内重算，不得接受按旧快照计算的 patch 耗时。审查 R2：任务
    详情删除（无目标 id / patch）入口同样在事务内补齐执行中行结束事实。
    pending 分支与 20260928040000 逐字等价；并发写入的事实让位（BLOCKER
    3）；函数签名不变（同签名 create or replace 仅替换函数体，不产生新
    overload）。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = DISCARD_INPROGRESS_FACTS_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def test_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_creates_exactly_the_discard_function(self):
        top = [ln.strip().casefold() for ln in self.sql.splitlines() if ln.strip()]
        creating = [item for item in top if item.startswith("create")]
        self.assertEqual(creating, [
            "create or replace function public.planning_discard_task("])

    def test_signature_is_unchanged_no_new_overload(self):
        # 同签名 create or replace：参数列表与 20260928040000 完全一致，
        # 不产生新签名 overload（清单 #15 发布核查前提）；唯一的 DROP 是
        # actual_minutes CHECK 的放开重加（第三轮 #3），不 DROP 任何函数、
        # 表或其他对象。
        folded = self.folded
        self.assertIn("p_task_id bigint,", folded)
        self.assertIn("p_now timestamptz,", folded)
        self.assertIn("p_target_id bigint default null,", folded)
        self.assertIn("p_target_patch jsonb default null", folded)
        self.assertIn("returns integer", folded)
        self.assertNotIn("drop function", folded)
        self.assertNotIn("drop table", folded)
        self.assertNotIn("drop index", folded)
        self.assertNotIn("drop trigger", folded)
        self.assertEqual(folded.count("drop constraint"), 1)
        self.assertIn(
            "drop constraint planning_occurrence_actual_minutes_check", folded)

    def test_widens_actual_minutes_check_for_long_open_executions(self):
        # 第三轮 #3：七天上限（0–10080）与需求 §12.2 / §9.4 持续开放型冲突
        # （长执行实例真实耗时合法，如八天 = 11520）——放开上界、保留非负
        # 下限；倒置区间由 RPC 语句 1c 显式拒绝。
        folded = self.folded
        self.assertIn(
            "add constraint planning_occurrence_actual_minutes_check", folded)
        self.assertIn("check (actual_minutes is null or actual_minutes >= 0)",
                      folded)
        self.assertNotIn("10080", folded)

    def test_pending_guard_is_preserved(self):
        # pending 且完全无事实：原守卫条件原样保留（行为等价）。
        folded = self.folded
        self.assertIn("status = 'pending'", folded)
        self.assertIn("actual_start is null", folded)
        self.assertIn("actual_end is null", folded)
        self.assertIn("partial_at is null", folded)
        self.assertIn("handled_at is null", folded)
        self.assertIn("closed_at is null", folded)

    def test_inprogress_loop_derives_minutes_from_final_interval(self):
        # R1 / 第三轮 #1：执行中行按锁内最终起止重算耗时并把补入的开始
        # 时间一并落库（其变化纳入更新判断）；patch 耗时只被 pending 分支
        # 消费一次，执行中循环不接受 patch 携带的耗时。倒置区间整体拒绝。
        folded = self.folded
        self.assertIn("v_row.status = 'in_progress'", folded)
        self.assertIn("and v_row.handled_at is null", folded)
        self.assertIn("and v_row.closed_at is null", folded)
        self.assertIn("for v_row in", folded)
        self.assertIn("order by id", folded)
        self.assertIn("for update", folded)
        self.assertIn("v_end := p_now", folded)
        self.assertIn("v_raw := extract(epoch from (v_end - v_start)) / 60.0", folded)
        self.assertIn("when v_frac > 0.5 then 1", folded)
        self.assertIn("when v_frac < 0.5 then 0", folded)
        self.assertIn("(v_whole::bigint % 2) = 0", folded)
        self.assertIn("greatest(0, ", folded)
        self.assertIn(
            "planning_discard_task: actual_end must not precede actual_start",
            folded)
        # 第三轮 #1：补入的开始时间落库，变化纳入更新判断
        self.assertIn("set actual_start = v_start,", folded)
        self.assertIn("v_start is distinct from v_row.actual_start", folded)
        # 目标行缺失起止以补丁值补齐（已有事实让位），但 patch 的耗时键
        # 仅由 pending 分支消费一次
        self.assertIn("p_target_patch ? 'actual_start'", folded)
        self.assertIn("p_target_patch ? 'actual_end'", folded)
        self.assertEqual(
            self.folded.count("p_target_patch ? 'actual_minutes'"), 1)

    def test_lock_scan_covers_all_open_rows(self):
        # 第三轮 #2：事实补齐的锁扫描覆盖全部开放行（for v_row in … order by
        # id 稳定锁序，行锁获取时 EPQ 以最新版本复核）——删除等待期间刚开
        # 始的行同样补齐结束事实；批量关闭与锁扫描命中同一组行。
        folded = self.folded
        scan_pos = folded.find(
            "status in ('pending', 'in_progress', 'deferred', 'partial')")
        loop_pos = folded.find("for v_row in")
        safety_pos = folded.find(
            "status in ('pending', 'in_progress', 'deferred', 'partial')",
            scan_pos + 1)
        self.assertGreaterEqual(scan_pos, 0)
        self.assertGreaterEqual(safety_pos, 0)
        # for v_row in（循环头）→ 锁扫描 WHERE → 语句 2 的批量关闭 WHERE
        self.assertLess(loop_pos, scan_pos)
        self.assertLess(scan_pos, safety_pos)

    def test_command_shape_unchanged(self):
        folded = self.folded
        # 语句顺序：锁任务行 → 白名单校验 → pending 目标补丁 → 全开放行
        # 锁扫描与执行中事实补齐 → 单语句关闭全部开放 occurrence → 单语句
        # 停用任务；并发停用拒绝仍携带 PC001。
        lock_pos = folded.find("for update")
        whitelist_pos = folded.find("target patch contains unsupported field")
        pending_pos = folded.find("status = 'pending'")
        loop_pos = folded.find("for v_row in")
        task_pos = folded.find("update public.planning_task")
        self.assertGreaterEqual(lock_pos, 0)
        self.assertGreaterEqual(whitelist_pos, 0)
        self.assertGreaterEqual(pending_pos, 0)
        self.assertGreaterEqual(loop_pos, 0)
        self.assertLess(lock_pos, whitelist_pos)
        self.assertLess(whitelist_pos, pending_pos)
        self.assertLess(pending_pos, loop_pos)
        self.assertLess(loop_pos, task_pos)
        self.assertIn("status = 'discarded'", folded)
        self.assertIn("is_active = false", folded)
        self.assertIn("task already inactive (concurrent change)", folded)
        self.assertIn("using errcode = 'pc001'", folded)
        # 白名单与未知字段拒绝不变
        self.assertIn("target patch contains unsupported field", folded)
        self.assertIn(
            "'actual_start', 'actual_end', 'actual_minutes', 'updated_at'", folded)


class PlanningRecomputeIdentityMigrationContractTests(unittest.TestCase):
    """重算请求消费身份契约（20260930010000，批次 6 收尾 A1）。

    requested_at 来自业务 now、不能充当消费身份：登记 RPC 在函数体内原子
    生成 uuid request_token；清除 RPC 只命中 token 等值行。不建通用版本
    框架；requested_at 保留展示语义。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = RECOMPUTE_IDENTITY_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()

    def test_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_adds_only_request_token_column(self):
        self.assertIn(
            "add column if not exists request_token uuid", self.folded)
        # 不 DROP、不回填、不改既有列
        self.assertNotIn("drop ", self.folded)
        self.assertNotIn("update public.planning_recompute_state set request_token",
                         self.folded.replace("set requested_at = null", ""))
        alters = [ln.strip().casefold() for ln in self.sql.splitlines()
                  if ln.strip().casefold().startswith("alter table")]
        self.assertEqual(alters, [
            "alter table public.planning_recompute_state",
        ])

    def test_creates_exactly_the_two_identity_functions(self):
        top = [ln.strip().casefold() for ln in self.sql.splitlines() if ln.strip()]
        creating = [item for item in top if item.startswith("create")]
        self.assertEqual(creating, [
            "create or replace function public.planning_request_recompute(",
            "create or replace function public.planning_clear_recompute_mark(",
        ])

    def test_request_token_generated_in_database(self):
        # 消费身份必须数据库原子生成：函数体内 gen_random_uuid，
        # 不存在 Python 读-改-写路径。
        self.assertIn("gen_random_uuid()", self.folded)
        func_pos = self.folded.find(
            "create or replace function public.planning_request_recompute(")
        token_pos = self.folded.find("gen_random_uuid()", func_pos)
        self.assertGreater(token_pos, func_pos)
        # upsert 冲突分支同样写入新 token（每次登记必然换新身份）
        self.assertIn("on conflict (id) do update", self.folded)
        self.assertIn("request_token = excluded.request_token", self.folded)

    def test_clear_only_matches_captured_token(self):
        folded = self.folded
        # 清除条件是 token 等值，而不是 requested_at 等值
        clear_pos = folded.find(
            "create or replace function public.planning_clear_recompute_mark(")
        cond_pos = folded.find("and request_token = p_request_token", clear_pos)
        self.assertGreater(cond_pos, clear_pos)
        self.assertNotIn("and requested_at = p_request_token", folded)
        # token 为 NULL（捕获时本无待处理请求）不清除
        self.assertIn("if p_request_token is null then", folded)
        # 清除同时归空展示字段与身份
        self.assertIn("requested_at = null", folded)
        self.assertIn("request_token = null", folded)

    def test_replay_safe(self):
        self.assertIn(
            "create or replace function public.planning_request_recompute(",
            self.folded)
        self.assertIn(
            "create or replace function public.planning_clear_recompute_mark(",
            self.folded)


class PlanningUpdateCycleBoundaryMigrationContractTests(unittest.TestCase):
    """boundary 原子保存 RPC 契约（20260930020000，批次 7 §5.2.2）。

    单事务：状态行 CAS → 调整项校验 → 锁内按新 boundary 全量校验启用中
    模板 → 冲突零写入返回 → 关联任务窗口更新 + 过渡状态写入；任一失败
    整体回滚。只服务 boundary 这一个业务操作（非通用配置事务框架）。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = BOUNDARY_RPC_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()
        # 函数体（去掉头部注释）——注释里提及的表名不算语句触碰
        cls.body = "\n".join(
            ln for ln in cls.sql.splitlines() if not ln.strip().startswith("--")
        ).casefold()

    def test_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_creates_exactly_the_boundary_function(self):
        top = [ln.strip().casefold() for ln in self.sql.splitlines() if ln.strip()]
        creating = [item for item in top if item.startswith("create")]
        self.assertEqual(creating, [
            "create or replace function public.planning_update_cycle_boundary("])

    def test_validation_precedes_any_write(self):
        folded = self.folded
        cas_pos = folded.find("stale_state")
        conflicts_pos = folded.find("status', 'conflicts")
        task_update_pos = folded.find("update public.planning_task")
        state_update_pos = folded.find("update public.app_settings")
        self.assertGreaterEqual(cas_pos, 0)
        self.assertGreater(conflicts_pos, cas_pos)
        # 冲突返回在任务更新与状态写入之前（零写入拒绝）
        self.assertLess(conflicts_pos, task_update_pos)
        self.assertLess(conflicts_pos, state_update_pos)

    def test_state_cas_guards_concurrent_workers(self):
        folded = self.folded
        self.assertIn("stale_state", folded)
        # 缺省行补插在提交阶段；补插后重核 CAS（并发首写不互相覆盖）
        insert_pos = folded.find("insert into public.app_settings")
        recas_pos = folded.find("stale_state", insert_pos)
        self.assertGreater(recas_pos, insert_pos)

    def test_window_validation_uses_clockwise_open_interval(self):
        folded = self.folded
        # 双侧窗口才校验；端点接触合法（严格不等）；跨午夜（start > end）覆盖
        self.assertIn("v_eff_start is not null and v_eff_end is not null", folded)
        self.assertIn("p_new_boundary > v_eff_start and p_new_boundary < v_eff_end", folded)
        self.assertIn("p_new_boundary > v_eff_start or p_new_boundary < v_eff_end", folded)
        # 锁定启用中任务（稳定 id 序）；for update 行锁
        self.assertIn("order by t.id", folded)
        self.assertIn("for update", folded)
        # start == end 形状非法拒绝
        self.assertIn("window start and end must differ", folded)

    def test_task_write_whitelist_is_window_fields_only(self):
        folded = self.body
        # 只写窗口模板字段与 updated_at；不触碰身份 / 规则 / 游标 / target_date
        self.assertIn("window_start_tod =", folded)
        self.assertIn("window_end_tod =", folded)
        self.assertIn("updated_at = now()", folded)
        self.assertNotIn("target_date =", folded)
        self.assertNotIn("refresh_generated_through", folded)
        self.assertNotIn("round_key =", folded)
        # 已生成实例零写路径：整个函数体不触碰 planning_occurrence
        self.assertNotIn("planning_occurrence", folded)
        # 只更新启用中任务
        self.assertIn("and is_active", folded)

    def test_no_drop_no_data_backfill(self):
        folded = self.folded
        self.assertNotIn("drop ", folded)
        self.assertNotIn("delete from", folded)
        self.assertNotIn("backfill", folded)

    def test_replay_safe(self):
        self.assertIn(
            "create or replace function public.planning_update_cycle_boundary(",
            self.folded)

    def test_advisory_lock_serializes_with_task_writes(self):
        # 批次 9 Review HIGH #3：boundary 修改与 active 任务创建 / 模板窗口
        # 编辑 / inactive→active 经同一把事务级 advisory lock 串行化
        folded = self.folded
        lock_at = folded.find("pg_advisory_xact_lock")
        state_read_at = folded.find("from public.app_settings")
        self.assertGreaterEqual(lock_at, 0)
        self.assertIn(
            "hashtextextended('planning.refresh_boundary_state', 0)", folded)
        # 锁先于状态行读取（步骤 0 → 步骤 1）
        self.assertLess(lock_at, state_read_at)

    def test_state_write_precedes_task_updates(self):
        # 批次 9 Review HIGH：步骤 6 状态写入先于步骤 7 任务更新——任务
        # UPDATE 触发的守卫按本事务的新 boundary 重校验；同时缺省行补插
        # 与其 CAS 重核必须仍在任何任务写入之前（拒绝路径零写入）
        folded = self.folded
        state_update_pos = folded.find("update public.app_settings")
        task_update_pos = folded.find("update public.planning_task")
        default_insert_pos = folded.find("insert into public.app_settings")
        self.assertGreater(default_insert_pos, 0)
        self.assertLess(default_insert_pos, task_update_pos)
        self.assertLess(state_update_pos, task_update_pos)


class PlanningBoundaryWindowGuardMigrationContractTests(unittest.TestCase):
    """boundary/window 写入互斥守卫契约（20260930030000，批次 9 HIGH #3/#4）。

    守卫触发器只对「真正改变 boundary/window 合法性的写入」生效：启用中
    且双侧窗口的 INSERT / UPDATE；持锁后按 configured boundary 重校验；
    跨越判定与 Python / boundary RPC 同一顺时针开区间规则。
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = BOUNDARY_WINDOW_GUARD_MIGRATION.read_text(encoding="utf-8")
        cls.folded = cls.sql.casefold()
        cls.body = "\n".join(
            ln for ln in cls.sql.splitlines() if not ln.strip().startswith("--")
        ).casefold()

    def test_migration_is_atomic(self):
        self.assertIn("begin;", self.folded)
        self.assertIn("commit;", self.folded)

    def test_creates_exactly_guard_function_and_trigger(self):
        top = [ln.strip().casefold() for ln in self.sql.splitlines() if ln.strip()]
        creating = [item for item in top if item.startswith("create")]
        self.assertEqual(creating, [
            "create or replace function public.planning_validate_window_boundary_guard()",
            "create trigger planning_boundary_window_guard",
        ])

    def test_lock_matches_boundary_rpc_key(self):
        # 与 planning_update_cycle_boundary 同一把事务级 advisory lock
        self.assertIn(
            "hashtextextended('planning.refresh_boundary_state', 0)", self.folded)
        self.assertIn("pg_advisory_xact_lock", self.folded)
        # 锁在校验（读取 app_settings）之前：持锁后读 boundary
        lock_at = self.folded.find("pg_advisory_xact_lock")
        read_at = self.folded.find("from public.app_settings")
        self.assertLess(lock_at, read_at)

    def test_scope_is_active_both_sided_windows_only(self):
        folded = self.folded
        # UPDATE 早退：窗口两端与 is_active 均未变化（生成游标等内容写入
        # 不取锁、不校验）
        self.assertIn("new.window_start_tod is not distinct from old.window_start_tod", folded)
        self.assertIn("new.window_end_tod is not distinct from old.window_end_tod", folded)
        self.assertIn("new.is_active is not distinct from old.is_active", folded)
        # 仅启用中且双侧窗口参与；start == end 交由既有 CHECK 拒绝
        self.assertIn("not new.is_active", folded)
        self.assertIn("new.window_start_tod is null", folded)
        self.assertIn("new.window_start_tod = new.window_end_tod", folded)

    def test_crossing_check_is_clockwise_open_interval(self):
        folded = self.folded
        # 与 Python / boundary RPC 同一规则：端点接触合法、跨午夜覆盖
        self.assertIn("new.window_start_tod < new.window_end_tod", folded)
        self.assertIn("v_boundary > new.window_start_tod", folded)
        self.assertIn("v_boundary < new.window_end_tod", folded)
        self.assertIn("new.window_start_tod > new.window_end_tod", folded)

    def test_reads_configured_boundary_with_default(self):
        # 校验基准 = app_settings 的 configured boundary（与 Python 创建
        # 校验同源）；行缺失回退缺省 06:00
        self.assertIn("value->>'boundary'", self.folded)
        self.assertIn("coalesce(v_boundary_text, '06:00')", self.folded)

    def test_no_occurrence_touch_no_drop_no_backfill(self):
        folded = self.body
        self.assertNotIn("planning_occurrence", folded)
        self.assertNotIn("drop ", folded)
        self.assertNotIn("delete from", folded)

    def test_replay_safe(self):
        self.assertIn(
            "create or replace function public.planning_validate_window_boundary_guard()",
            self.folded)
        self.assertIn(
            "create trigger planning_boundary_window_guard", self.folded)


if __name__ == "__main__":
    unittest.main()
