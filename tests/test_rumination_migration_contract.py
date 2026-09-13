"""Static contract tests for the rumination continuity path migration.

The migration must keep public.chat_messages read-only, gate every new RPC to
service_role with a fixed search_path, mark provenance explicitly, exclude
closed threads from default recall, and never touch the fast-path prompts.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260905010000_rumination_continuity_path.sql"

RUMINATION_RPCS = (
    "get_or_create_rumination_cursor",
    "claim_rumination_batch",
    "record_rumination_skipped",
    "commit_rumination_batch",
)


class RuminationMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.commit = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )[1]

    def test_provenance_columns_exist_on_memories_and_requests(self):
        self.assertIn("add column if not exists producer_path", self.executable)
        self.assertIn("add column if not exists maintained_by", self.executable)
        self.assertIn("check (producer_path in ('fast_path', 'rumination'))", self.executable)
        self.assertIn("check (maintained_by in ('fast_path', 'rumination'))", self.executable)

    def test_request_source_and_interaction_rule_checks_include_rumination(self):
        self.assertIn(
            "check (source in ('orangechat_plugin', 'mcp_memory', 'daily_digest', 'rumination'))",
            self.executable,
        )
        self.assertRegex(
            self.executable,
            r"memory_requests_interaction_rule_source_check[\s\S]{0,400}?"
            r"'orangechat_plugin', 'mcp_memory', 'rumination'",
        )

    def test_rumination_cursor_table_is_independent(self):
        self.assertIn("create table if not exists public.memory_rumination_cursors", self.executable)
        self.assertNotIn("memory_continuity_cursors\n", self.executable.split("memory_rumination_cursors")[0])
        self.assertIn("initialized boolean not null default false", self.executable)

    def test_handoff_audit_table_records_adoptions(self):
        self.assertIn("create table if not exists public.memory_path_handoffs", self.executable)
        self.assertIn("fast_path_memory_id integer references public.memories(id)", self.executable)

    def test_run_pipeline_and_triggers_extended(self):
        self.assertIn("('legacy', 'continuity', 'rumination')", self.executable)
        for trigger in ("rumination_scheduled", "rumination_manual", "rumination_retry"):
            self.assertIn(trigger, self.executable)

    def test_chat_messages_is_only_ever_selected(self):
        forbidden = re.findall(
            r"(insert\s+into|update|delete\s+from|alter\s+table)[\s\S]{0,120}?chat_messages",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(forbidden, [])
        self.assertIn("from public.chat_messages as message", self.executable)

    def test_rumination_rpcs_are_service_role_only_with_fixed_search_path(self):
        for rpc in RUMINATION_RPCS:
            with self.subTest(rpc=rpc):
                pattern = (
                    rf"revoke\s+all\s+on\s+function\s+public\.{rpc}[\s\S]{{0,400}}?"
                    r"from\s+public,\s*anon,\s*authenticated"
                )
                self.assertRegex(self.executable, pattern)
                grant = re.search(
                    rf"grant\s+execute\s+on\s+function\s+public\.{rpc}[\s\S]{{0,400}}?to\s+service_role",
                    self.executable,
                )
                self.assertIsNotNone(grant)

    def test_commit_rpc_is_security_definer_with_pinned_search_path(self):
        self.assertIn("security definer", self.commit)
        self.assertIn("set search_path to 'public', 'extensions'", self.commit)

    def test_commit_rpc_validates_evidence_within_batch_window(self):
        self.assertIn("memory_rumination_invalid_evidence", self.commit)
        self.assertRegex(
            self.commit,
            r"message\.id between v_run\.source_first_message_id "
            r"and v_run\.source_last_message_id",
        )

    def test_commit_rpc_only_accepts_structured_operations(self):
        for op in (
            "ignore", "create_memory", "create_tracked_thread", "adopt_thread",
            "evidence_only", "update_thread", "pause_thread", "resume_thread",
            "resolve_thread", "create_request",
        ):
            self.assertIn(f"'{op}'", self.commit)
        self.assertIn("memory_rumination_invalid_op", self.commit)
        self.assertIn("memory_rumination_too_many_operations", self.commit)

    def test_direct_write_types_exclude_thread_and_review_gated_classes(self):
        self.assertIn("v_continuity_type not in ('moment', 'inside_joke')", self.commit)
        self.assertIn(
            "v_continuity_type not in ('episode', 'profile', 'interaction_rule')",
            self.commit,
        )

    def test_thread_lifecycle_rules_are_enforced(self):
        self.assertIn("memory_rumination_invalid_state_transition", self.commit)
        self.assertIn("memory_rumination_state_change_forbidden", self.commit)
        self.assertIn("memory_rumination_memory_key_conflict", self.commit)
        self.assertIn("memory_rumination_not_fast_path", self.commit)

    def test_version_successor_keeps_key_and_continuity_id(self):
        self.assertIn("v_target.continuity_id, 1, v_continuity_data", self.commit)
        self.assertIn("supersedes_memory_id = %s" if "%s" in self.commit
                      else "supersedes_memory_id", self.commit)

    def test_resolved_threads_leave_default_recall(self):
        exclusion = (
            "memory.thread_state in ('resolved', 'dissolved', 'abandoned')"
        )
        vector = self.executable.split("create function public.match_memories", 1)[1]
        keyword = self.executable.split("create function public.search_memories_by_keywords", 1)[1]
        self.assertIn(exclusion, vector)
        self.assertIn(exclusion, keyword)
        self.assertIn("memory.is_active = true", vector)
        self.assertIn("memory.verified = 'verified'", vector)
        self.assertIn("memory.is_active = true", keyword)
        self.assertIn("memory.verified = 'verified'", keyword)

    def test_recall_functions_keep_exact_signature_and_grants(self):
        self.assertRegex(
            self.executable,
            r"drop function if exists public\.match_memories\(\s*extensions\.vector, "
            r"double precision, integer\s*\)",
        )
        self.assertRegex(
            self.executable,
            r"drop function if exists public\.search_memories_by_keywords\(text\[\], integer\)",
        )
        self.assertNotRegex(self.executable, r"\bcascade\b")

    def test_fast_path_auto_approval_gated_against_rumination_threads(self):
        self.assertIn("maintained_by = 'rumination'", self.executable)
        self.assertIn("v_rumination_thread_conflict", self.executable)
        self.assertIn("memory_thread_rumination_conflict", self.executable)
        self.assertIn("memory_thread_rumination_maintained", self.executable)

    def test_reviewed_approval_copies_provenance(self):
        self.assertIn(
            "when new.producer_path in ('fast_path', 'rumination') then new.producer_path",
            self.executable,
        )

    def test_no_memory_relations_or_retired_types_are_written(self):
        self.assertNotIn("memory_relations", self.executable)
        self.assertNotIn("proposed_relations", self.executable)
        self.assertNotIn("'relationship'", self.executable)

    def test_recall_scene_requires_embedding_at_commit(self):
        self.assertIn("memory_rumination_missing_recall_embedding", self.commit)
        self.assertRegex(
            self.commit,
            r"v_recall_scene is not null[\s\S]{0,200}?not \(v_op \? 'recall_embedding'\)",
        )

    def test_keyless_fast_path_takeover_requires_key(self):
        self.assertIn("v_target.memory_key is null", self.commit)
        self.assertRegex(
            self.commit,
            r"v_target\.maintained_by = 'fast_path'[\s\S]{0,200}?"
            r"v_target\.memory_key is null",
        )

    def test_requests_skip_when_content_in_flight_final_or_formal(self):
        self.assertIn("request.status in ('pending', 'approved', 'merged')", self.commit)
        self.assertIn("skipped_active_memory", self.commit)

    def test_migration_wraps_in_transaction(self):
        self.assertRegex(self.executable, r"\bbegin\s*;")
        self.assertRegex(self.executable, r"\bcommit\s*;")




class RuminationReviewHandoffMigrationContractTests(unittest.TestCase):
    """Static contract for 20260906010000_rumination_review_handoff.sql."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "supabase/migrations/20260906010000_rumination_review_handoff.sql"
        cls.sql = path.read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.v5 = cls.executable.split(
            "create or replace function public.review_memory_request_v5", 1
        )[1].split(
            "create or replace function public.commit_rumination_batch", 1
        )[0]
        cls.commit = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )[1]

    def test_absorption_intent_column_and_handoff_enrichment(self):
        self.assertIn(
            "add column if not exists absorbed_fast_path_memory_ids bigint[]",
            self.executable,
        )
        self.assertIn("add column if not exists request_id bigint", self.executable)
        self.assertIn("'absorbed_by_request'", self.executable)

    def test_review_v5_rebuilt_with_transactional_handoff(self):
        self.assertIn("security definer", self.v5)
        self.assertIn("set search_path to 'public'", self.v5)
        self.assertIn("memory_rumination_absorb_target_invalid", self.v5)
        # 逐项结构校验：同 assistant、fast_path 生产、verified、active、非闭合 thread
        for needle in (
            "v_absorbed.assistant_id is distinct from v_request.assistant_id",
            "v_absorbed.producer_path is distinct from 'fast_path'",
            "v_absorbed.verified is distinct from 'verified'",
            "v_absorbed.is_active is not true",
            "'resolved', 'dissolved', 'abandoned'",
        ):
            self.assertIn(needle, self.v5)
        # 仅在真实状态变更时交接（幂等重放不重复写审计）。
        self.assertIn("(v_result->>'changed')::boolean", self.v5)
        # 交接只翻转 is_active，不写入版本链字段。
        self.assertNotIn(
            "set is_active = false,\n                    superseded_at",
            self.v5,
        )
        self.assertRegex(
            self.executable,
            r"revoke\s+all\s+on\s+function\s+public\.review_memory_request_v5",
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.review_memory_request_v5"
            r"[\s\S]{0,400}?to\s+service_role",
        )

    def test_commit_rpc_rebuilt_with_target_snapshots(self):
        self.assertEqual(self.commit.count("memory_rumination_target_changed"), 4)
        for needle in (
            "target_memory_key", "target_continuity_id",
            "target_content_hash", "target_thread_state",
        ):
            with self.subTest(field=needle):
                self.assertIn(needle, self.commit)
        # 快照校验发生在 FOR UPDATE 加载目标之后。
        self.assertIn("for update", self.commit)

    def test_commit_rpc_persists_absorption_intent(self):
        self.assertIn("memory_rumination_invalid_absorb_target", self.commit)
        self.assertIn("memory_rumination_absorb_target_invalid", self.commit)
        self.assertIn("absorbed_fast_path_memory_ids", self.commit)
        self.assertIn("cardinality(v_absorbed_ids), 0) > 8", self.commit)

    def test_chat_messages_and_relations_untouched(self):
        forbidden = re.findall(
            r"(insert\s+into|update|delete\s+from|alter\s+table)[\s\S]{0,120}?chat_messages",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(forbidden, [])
        self.assertNotIn("memory_relations", self.executable)


class RuminationAbsorbClosureMigrationContractTests(unittest.TestCase):
    """Static contract for 20260907010000_rumination_absorb_closure.sql."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "supabase/migrations/20260907010000_rumination_absorb_closure.sql"
        cls.sql = path.read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.claim = cls.executable.split(
            "create or replace function public.claim_rumination_batch", 1
        )[1].split("create or replace function public.commit_rumination_batch", 1)[0]
        cls.commit = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )[1].split("create or replace function public.review_memory_request_v5", 1)[0]
        cls.v5 = cls.executable.split(
            "create or replace function public.review_memory_request_v5", 1
        )[1]

    def test_no_arbitrary_operation_cap(self):
        self.assertNotIn("memory_rumination_too_many_operations", self.executable)
        self.assertNotIn("jsonb_array_length(v_ops) > 24", self.executable)

    def test_direct_write_absorption_is_transactional(self):
        self.assertIn("'absorbed_by_direct_memory'", self.executable)
        self.assertIn("memory_rumination_invalid_absorb_target", self.commit)
        self.assertIn("memory_rumination_absorb_target_invalid", self.commit)
        # 结构复核：同 assistant、fast_path 生产、verified、active、非闭合 thread。
        for needle in (
            "v_absorbed.assistant_id is distinct from v_run.assistant_id",
            "v_absorbed.producer_path is distinct from 'fast_path'",
            "v_absorbed.verified is distinct from 'verified'",
            "v_absorbed.is_active is not true",
            "'resolved', 'dissolved', 'abandoned'",
        ):
            self.assertIn(needle, self.commit)
        # 吸收在新记忆插入之后、同事务内执行。
        self.assertIn("returning id into v_new_memory_id", self.commit)

    def test_v5_skips_merge_related_target_and_verifies_result(self):
        self.assertIn("v_absorbed_id = v_memory_id", self.v5)
        self.assertIn("v_absorbed_id = v_request.related_memory_id", self.v5)
        self.assertIn("memory_rumination_absorb_result_invalid", self.v5)
        # 交接前重读申请，拿到 v4 写入的 related_memory_id。
        self.assertIn("from public.memory_requests\n        where id = v_request.id", self.v5)

    def test_scheduled_attempt_stamped_inside_claim_lock(self):
        self.assertIn("p_trigger = 'rumination_scheduled'", self.claim)
        self.assertIn(
            "(now() at time zone 'Asia/Shanghai')::date", self.claim,
        )
        self.assertIn("returning * into v_cursor", self.claim)

    def test_security_model_unchanged(self):
        for function_name in (
            "claim_rumination_batch",
            "commit_rumination_batch",
            "review_memory_request_v5",
        ):
            with self.subTest(rpc=function_name):
                self.assertRegex(
                    self.executable,
                    rf"revoke\s+all\s+on\s+function\s+public\.{function_name}"
                    r"[\s\S]{0,400}?from\s+public,\s*anon,\s*authenticated",
                )
                self.assertRegex(
                    self.executable,
                    rf"grant\s+execute\s+on\s+function\s+public\.{function_name}"
                    r"[\s\S]{0,400}?to\s+service_role",
                )
        self.assertIn("security definer", self.commit)
        self.assertIn("set search_path to 'public', 'extensions'", self.commit)

    def test_chat_messages_and_relations_untouched(self):
        forbidden = re.findall(
            r"(insert\s+into|update|delete\s+from|alter\s+table)[\s\S]{0,120}?chat_messages",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(forbidden, [])
        self.assertNotIn("memory_relations", self.executable)


class RuminationSnapshotAndScheduleGuardContractTests(unittest.TestCase):
    """Static contract for 20260908010000_rumination_absorb_snapshot_and_schedule_guard.sql."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "supabase/migrations/20260908010000_rumination_absorb_snapshot_and_schedule_guard.sql"
        cls.sql = path.read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.claim = cls.executable.split(
            "create or replace function public.claim_rumination_batch", 1
        )[1].split("create or replace function public.commit_rumination_batch", 1)[0]
        cls.commit = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )[1].split("create or replace function public.review_memory_request_v5", 1)[0]
        cls.v5 = cls.executable.split(
            "create or replace function public.review_memory_request_v5", 1
        )[1]

    def test_snapshot_column_and_builder(self):
        self.assertIn(
            "add column if not exists absorbed_fast_path_memory_snapshots jsonb",
            self.executable,
        )
        self.assertIn(
            "create or replace function public.build_absorb_target_snapshot",
            self.executable,
        )
        for field in (
            "memory_id", "content_hash", "continuity_id", "continuity_type",
            "memory_key", "thread_state", "evidence_message_ids",
            "producer_path", "verified", "is_active",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.executable)

    def test_commit_persists_server_generated_snapshots(self):
        self.assertIn("public.build_absorb_target_snapshot(v_absorbed)", self.commit)
        self.assertIn("absorbed_fast_path_memory_snapshots", self.commit)
        # 快照在 FOR UPDATE 锁定后生成，模型无从提供。
        self.assertIn("for update", self.commit)
        self.assertNotIn("p_absorbed_snapshots", self.commit)

    def test_v5_reverifies_every_target_against_snapshot(self):
        self.assertIn("memory_rumination_absorb_target_changed", self.v5)
        for needle in (
            "v_snapshot->>'content_hash' is distinct from v_absorbed.content_hash",
            "v_snapshot->>'continuity_id' is distinct from v_absorbed.continuity_id::text",
            "v_snapshot->>'continuity_type' is distinct from v_absorbed.continuity_type",
            "v_snapshot->>'memory_key' is distinct from v_absorbed.memory_key",
            "v_snapshot->>'thread_state' is distinct from v_absorbed.thread_state",
            "v_snapshot->'evidence_message_ids'",
            "v_snapshot->>'producer_path' is distinct from v_absorbed.producer_path",
            "v_snapshot->>'verified' is distinct from v_absorbed.verified",
            "(v_snapshot->>'is_active')::boolean is distinct from v_absorbed.is_active",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, self.v5)
        # merge 主目标保护保持不变。
        self.assertIn("v_absorbed_id = v_request.related_memory_id", self.v5)
        self.assertIn("v_absorbed_id = v_memory_id", self.v5)
        # 复核前重读申请（v4 之后 related_memory_id 才存在）。
        self.assertIn("where id = v_request.id", self.v5)

    def test_claim_guard_returns_already_scheduled_today(self):
        self.assertIn("already_scheduled_today", self.claim)
        self.assertRegex(
            self.claim,
            r"last_scheduled_date >= \(now\(\) at time zone 'Asia/Shanghai'\)::date",
        )
        self.assertIn("return jsonb_build_object('status', 'already_scheduled_today')", self.claim)
        # 日期检查与写入在同一把锁内（advisory lock 在函数体前段）。
        self.assertIn("pg_advisory_xact_lock", self.claim)

    def test_security_model_unchanged(self):
        for function_name in (
            "claim_rumination_batch",
            "commit_rumination_batch",
            "review_memory_request_v5",
            "build_absorb_target_snapshot",
        ):
            with self.subTest(rpc=function_name):
                self.assertRegex(
                    self.executable,
                    rf"revoke\s+all\s+on\s+function\s+public\.{function_name}"
                    r"[\s\S]{0,400}?from\s+public,\s*anon,\s*authenticated",
                )
                self.assertRegex(
                    self.executable,
                    rf"grant\s+execute\s+on\s+function\s+public\.{function_name}"
                    r"[\s\S]{0,400}?to\s+service_role",
                )

    def test_chat_messages_and_relations_untouched(self):
        forbidden = re.findall(
            r"(insert\s+into|update|delete\s+from|alter\s+table)[\s\S]{0,120}?chat_messages",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(forbidden, [])
        self.assertNotIn("memory_relations", self.executable)


class RuminationScheduledExecutionContractTests(unittest.TestCase):
    """Static contract for 20260909010000_rumination_scheduled_execution_and_snapshot_integrity.sql."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "supabase/migrations/20260909010000_rumination_scheduled_execution_and_snapshot_integrity.sql"
        cls.sql = path.read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.claim = cls.executable.split(
            "create or replace function public.claim_rumination_batch", 1
        )[1].split("create or replace function public.commit_rumination_batch", 1)[0]

    def test_scheduled_executions_table(self):
        self.assertIn(
            "create table if not exists public.memory_rumination_scheduled_executions",
            self.executable,
        )
        self.assertIn("unique (assistant_id, execution_date)", self.executable)
        self.assertIn("check (status in ('running', 'finished'))", self.executable)

    def test_finish_function_exists_and_is_guarded(self):
        self.assertIn(
            "create or replace function public.finish_rumination_scheduled_execution",
            self.executable,
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.finish_rumination_scheduled_execution"
            r"[\s\S]{0,200}?to\s+service_role",
        )

    def test_claim_accepts_execution_identity(self):
        self.assertIn("p_scheduled_execution_id bigint default null", self.claim)
        # 首批（无 identity）走当日防重。
        self.assertIn("already_scheduled_today", self.claim)
        # 后续批次（带 identity）验证 assistant、日期和 running 状态。
        self.assertIn("invalid_scheduled_execution", self.claim)
        self.assertIn("v_execution.status is distinct from 'running'", self.claim)
        self.assertIn("v_execution.assistant_id is distinct from p_assistant_id", self.claim)
        self.assertIn("v_execution.execution_date is distinct from v_execution_date", self.claim)

    def test_run_records_scheduled_execution_id(self):
        self.assertIn("scheduled_execution_id", self.claim)
        self.assertIn(
            "add column if not exists scheduled_execution_id bigint", self.executable,
        )

    def test_old_six_arg_signature_dropped(self):
        self.assertRegex(
            self.executable,
            r"drop function if exists public\.claim_rumination_batch\("
            r"\s*text, text, bigint, bigint, bigint, boolean\s*\)",
        )

    def test_snapshot_integrity_validator_exists(self):
        self.assertIn(
            "create or replace function public.validate_absorb_snapshots",
            self.executable,
        )
        for needle in (
            "memory_id", "content_hash", "continuity_id", "continuity_type",
            "memory_key", "thread_state", "evidence_message_ids",
            "producer_path", "verified", "is_active",
        ):
            self.assertIn(needle, self.executable)

    def test_commit_resets_snapshots_per_request(self):
        commit = self.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )[1]
        self.assertIn("v_absorbed_snapshots := '[]'::jsonb", commit)
        self.assertIn("memory_rumination_absorb_snapshot_mismatch", commit)
        self.assertIn("public.validate_absorb_snapshots(v_absorbed_ids, v_absorbed_snapshots)", commit)

    def test_chat_messages_and_relations_untouched(self):
        forbidden = re.findall(
            r"(insert\s+into|update|delete\s+from|alter\s+table)[\s\S]{0,120}?chat_messages",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(forbidden, [])
        self.assertNotIn("memory_relations", self.executable)


class RuminationFinishRpcContractTests(unittest.TestCase):
    """Static contract for 20260910010000_rumination_finish_rpc_contract.sql."""

    @classmethod
    def setUpClass(cls):
        path = ROOT / "supabase/migrations/20260910010000_rumination_finish_rpc_contract.sql"
        cls.sql = path.read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)

    def test_returns_jsonb_not_void(self):
        self.assertIn("returns jsonb", self.executable)
        self.assertNotIn("returns void", self.executable)

    def test_response_contains_status_execution_id_changed(self):
        for field in ("status", "execution_id", "changed"):
            self.assertIn(field, self.executable)

    def test_security_definer_fixed_search_path(self):
        self.assertIn("security definer", self.executable)
        self.assertIn("set search_path to 'public'", self.executable)

    def test_service_role_only(self):
        self.assertRegex(
            self.executable,
            r"revoke\s+all\s+on\s+function\s+public\.finish_rumination_scheduled_execution"
            r"[\s\S]{0,200}?from\s+public,\s*anon,\s*authenticated",
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.finish_rumination_scheduled_execution"
            r"[\s\S]{0,200}?to\s+service_role",
        )

    def test_stable_error_for_missing_execution(self):
        self.assertIn("memory_rumination_scheduled_execution_not_found", self.executable)

    def test_old_void_signature_dropped(self):
        self.assertIn(
            "drop function if exists public.finish_rumination_scheduled_execution(bigint)",
            self.executable,
        )

    def test_no_touch_forbidden_tables(self):
        forbidden = re.findall(
            r"(insert\s+into|update|delete\s+from|alter\s+table)[\s\S]{0,120}?(chat_messages|memories|memory_requests|memory_rumination_cursors)",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(forbidden, [])
        self.assertNotIn("memory_relations", self.executable)


class RequestTypeExpansionContractTests(unittest.TestCase):
    """20260912010000: create_request type whitelist expands to five types.

    Forward-only follow-up: the only functional change vs the previous
    commit_rumination_batch body is the whitelist line; the parser relies on
    it to file degraded moment/inside_joke requests for human review.
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "supabase/migrations/20260912010000_rumination_request_type_expansion.sql"
        ).read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        parts = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )
        assert len(parts) == 2, "commit_rumination_batch must be rebuilt exactly once"
        cls.commit = parts[1]

    def test_rebuilds_only_commit_rumination_batch(self):
        # The migration drops and recreates the bigint/jsonb signature inside
        # a transaction; no DDL touches table structures and no other
        # function is rebuilt. (DML on memories/memory_requests inside the
        # function body is the function's contract and stays untouched.)
        self.assertIn(
            "drop function if exists public.commit_rumination_batch(bigint, jsonb)",
            self.executable,
        )
        self.assertEqual(self.executable.count("create or replace function"), 1)
        self.assertNotIn("memory_relations", self.executable)
        ddl = re.findall(
            r"(alter\s+table|drop\s+table|create\s+table|create\s+or\s+replace\s+trigger)"
            r"[\s\S]{0,120}?(chat_messages|memories|memory_requests|"
            r"memory_rumination_cursors|memory_path_handoffs)",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(ddl, [])

    def test_request_type_whitelist_expanded_thread_excluded(self):
        self.assertIn(
            "if v_continuity_type not in "
            "('moment', 'inside_joke', 'episode', 'profile', 'interaction_rule') then",
            self.commit,
        )
        self.assertIn("memory_rumination_invalid_request_type", self.commit)
        # The direct-write whitelist (create_memory) stays moment/inside_joke.
        self.assertIn(
            "if v_continuity_type not in ('moment', 'inside_joke') then",
            self.commit,
        )

    def test_security_definer_fixed_search_path_and_service_role(self):
        self.assertIn("security definer", self.commit)
        self.assertIn("set search_path to 'public', 'extensions'", self.commit)
        self.assertRegex(
            self.executable,
            r"revoke\s+all\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?from\s+public,\s*anon,\s*authenticated",
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?to\s+service_role",
        )


class MergedEvidenceLimitContractTests(unittest.TestCase):
    """20260913010000: per-op evidence ceiling raised to the merged-union bound.

    The gateway merges same-thread ops (and same-content duplicates), and the
    merged op legitimately carries the evidence union of everything it
    absorbed. The commit RPC must accept that shape while still validating
    each id against the batch window; the gateway enforces the same
    RUMINATION_BATCH_MAX * MAX_EVIDENCE_IDS = 960 bound before commit.
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "supabase/migrations/20260913010000_rumination_merged_evidence_limit.sql"
        ).read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.previous = re.sub(
            r"--[^\n]*", "",
            (
                ROOT / "supabase/migrations/20260912010000_rumination_request_type_expansion.sql"
            ).read_text(encoding="utf-8"),
        )
        parts = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )
        assert len(parts) == 2, "commit_rumination_batch must be rebuilt exactly once"
        cls.commit = parts[1]

    @staticmethod
    def _function_body(text):
        return text.split(
            "create or replace function public.commit_rumination_batch", 1
        )[1].split("$function$;", 1)[0]

    def test_rebuilds_only_commit_rumination_batch_without_ddl(self):
        self.assertIn(
            "drop function if exists public.commit_rumination_batch(bigint, jsonb)",
            self.executable,
        )
        self.assertEqual(self.executable.count("create or replace function"), 1)
        ddl = re.findall(
            r"(alter\s+table|drop\s+table|create\s+table|"
            r"create\s+or\s+replace\s+trigger)"
            r"[\s\S]{0,120}?(chat_messages|memories|memory_requests|"
            r"memory_rumination_cursors|memory_path_handoffs)",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(ddl, [])
        self.assertNotIn("memory_relations", self.executable)

    def test_body_differs_from_0912_only_in_evidence_bound(self):
        old_lines = self._function_body(self.previous).splitlines()
        new_lines = self._function_body(self.executable).splitlines()
        self.assertEqual(len(old_lines), len(new_lines))
        diffs = [
            (old, new) for old, new in zip(old_lines, new_lines) if old != new
        ]
        self.assertEqual(len(diffs), 1, diffs)
        old_line, new_line = diffs[0]
        self.assertIn("v_evidence_count not between 1 and 8", old_line)
        self.assertIn("v_evidence_count not between 1 and 960", new_line)

    def test_security_definer_fixed_search_path_and_service_role(self):
        self.assertIn("security definer", self.commit)
        self.assertIn("set search_path to 'public', 'extensions'", self.commit)
        self.assertRegex(
            self.executable,
            r"revoke\s+all\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?from\s+public,\s*anon,\s*authenticated",
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?to\s+service_role",
        )


class StructureUpdateContractTests(unittest.TestCase):
    """20260914010000: same-body structural changes update in place.

    memories.content_hash is unique, so a same-hash version row can never
    exist. "Evidence only" now requires the canonical structure (via
    jsonb_strip_nulls, matching the gateway) to be unchanged as well; a real
    structure or state change is validated and applied to the current active
    row, which also un-breaks pause/resume/resolve on an unchanged body.
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "supabase/migrations/20260914010000_rumination_structure_update.sql"
        ).read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.previous = re.sub(
            r"--[^\n]*", "",
            (
                ROOT / "supabase/migrations/20260913010000_rumination_merged_evidence_limit.sql"
            ).read_text(encoding="utf-8"),
        )
        parts = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )
        assert len(parts) == 2, "commit_rumination_batch must be rebuilt exactly once"
        cls.commit = parts[1]

    def test_rebuilds_only_commit_rumination_batch_without_ddl(self):
        self.assertIn(
            "drop function if exists public.commit_rumination_batch(bigint, jsonb)",
            self.executable,
        )
        self.assertEqual(self.executable.count("create or replace function"), 1)
        ddl = re.findall(
            r"(alter\s+table|drop\s+table|create\s+table|"
            r"create\s+or\s+replace\s+trigger)"
            r"[\s\S]{0,120}?(chat_messages|memories|memory_requests|"
            r"memory_rumination_cursors|memory_path_handoffs)",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(ddl, [])
        self.assertNotIn("memory_relations", self.executable)

    def test_same_hash_branch_validates_structure_and_updates_in_place(self):
        # "只补证据"以结构规范形未变为条件；结构变化先过校验再原地更新。
        self.assertIn("jsonb_strip_nulls(coalesce(v_continuity_data, '{}'::jsonb))", self.commit)
        self.assertIn("jsonb_strip_nulls(coalesce(v_target.continuity_data, '{}'::jsonb))", self.commit)
        self.assertIn("'structure_updated_in_place'", self.commit)
        self.assertIn(
            "if not public.validate_continuity_data('thread', v_thread_state, v_continuity_data) then",
            self.commit,
        )
        self.assertIn("set continuity_data = v_continuity_data,", self.commit)
        self.assertIn("thread_state = v_thread_state,", self.commit)
        # 旧的同哈希提前返回（仅凭正文）不复存在。
        self.assertNotIn(
            "if v_content_hash = v_target.content_hash and v_op_type = 'update_thread' then",
            self.commit,
        )
        # 未变的表达仍走 evidence_merged_unchanged。
        self.assertIn("'evidence_merged_unchanged'", self.commit)

    def test_body_differs_from_0913_only_in_same_hash_branch(self):
        def body(text):
            return text.split(
                "create or replace function public.commit_rumination_batch", 1
            )[1].split("$function$;", 1)[0]

        old_lines = body(self.previous).splitlines()
        new_lines = body(self.executable).splitlines()
        removed = [line for line in old_lines if line not in new_lines]
        added = [line for line in new_lines if line not in old_lines]
        # 被移除的只有旧的同哈希提前返回块（old_block 共 18 行，其中 12 行
        # 在新块中逐字保留：evidence 合并 SQL、evidence_only 计数、预览）。
        self.assertEqual(
            removed[0],
            "            if v_content_hash = v_target.content_hash "
            "and v_op_type = 'update_thread' then",
        )
        self.assertTrue(
            all(
                "v_content_hash = v_target.content_hash and v_op_type" in line
                or "Unchanged body" in line
                or line in (
                    "                update public.memories",
                    "                    where id = v_target.id;",
                    "                v_counts := jsonb_set(v_counts, '{evidence_only}',",
                    "                v_preview := v_preview || jsonb_build_object(",
                    "                continue;",
                    "                end if;",
                    "            end if;",
                )
                or line.strip().startswith((
                    "set evidence_message_ids",
                    "evidence_start_time",
                    "evidence_end_time",
                    "select coalesce",
                    "from unnest",
                    "),",
                    "((v_counts->>'evidence_only')",
                    "'op', 'update_thread',",
                    "'memory_id', v_target.id,",
                    "'reason', v_reason",
                ))
                for line in removed
            ),
            removed,
        )
        # 新增行只包含三分支实现的关键构造。
        self.assertTrue(any("jsonb_strip_nulls" in line for line in added))
        self.assertTrue(any("structure_updated_in_place" in line for line in added))
        self.assertTrue(any("set continuity_data = v_continuity_data," in line for line in added))
        # 证据上限（0913 的唯一改动）原样保留。
        self.assertIn("v_evidence_count not between 1 and 960", "\n".join(new_lines))

    def test_security_definer_fixed_search_path_and_service_role(self):
        self.assertIn("security definer", self.commit)
        self.assertIn("set search_path to 'public', 'extensions'", self.commit)
        self.assertRegex(
            self.executable,
            r"revoke\s+all\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?from\s+public,\s*anon,\s*authenticated",
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?to\s+service_role",
        )


class TakeoverAndBaselineContractTests(unittest.TestCase):
    """20260915010000: complete in-branch takeovers + structure baseline.

    The same-body branches now resolve memory_key / maintained_by exactly
    like the versioned path (with the handoff recording the effective key
    and state), and the gateway-attached continuity_baseline is verified
    under the row lock with the same jsonb_strip_nulls semantics as the
    in-place structure comparison.
    """

    @classmethod
    def setUpClass(cls):
        cls.sql = (
            ROOT / "supabase/migrations/20260915010000_rumination_takeover_and_baseline.sql"
        ).read_text(encoding="utf-8")
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.previous = re.sub(
            r"--[^\n]*", "",
            (
                ROOT / "supabase/migrations/20260914010000_rumination_structure_update.sql"
            ).read_text(encoding="utf-8"),
        )
        parts = cls.executable.split(
            "create or replace function public.commit_rumination_batch", 1
        )
        assert len(parts) == 2, "commit_rumination_batch must be rebuilt exactly once"
        cls.commit = parts[1]

    def test_rebuilds_only_commit_rumination_batch_without_ddl(self):
        self.assertIn(
            "drop function if exists public.commit_rumination_batch(bigint, jsonb)",
            self.executable,
        )
        self.assertEqual(self.executable.count("create or replace function"), 1)
        ddl = re.findall(
            r"(alter\s+table|drop\s+table|create\s+table|"
            r"create\s+or\s+replace\s+trigger)"
            r"[\s\S]{0,120}?(chat_messages|memories|memory_requests|"
            r"memory_rumination_cursors|memory_path_handoffs)",
            self.executable,
            re.IGNORECASE,
        )
        self.assertEqual(ddl, [])
        self.assertNotIn("memory_relations", self.executable)

    def test_same_body_branches_complete_takeover(self):
        # 两个同哈希子分支都回填 key、切换归属，交接记录实际 key 与状态。
        # （maintained_by 赋值共 4 处：adopt 原地接管、evidence_only 操作
        # 分支、原地结构更新、只补证据子分支；fast_path 门控同样 4 处。）
        self.assertEqual(self.commit.count("maintained_by = 'rumination'"), 4)
        self.assertEqual(
            self.commit.count("if v_target.maintained_by = 'fast_path' then"), 4,
        )
        self.assertGreaterEqual(self.commit.count("memory_key = v_memory_key,"), 2)
        self.assertGreaterEqual(self.commit.count("memory_rumination_memory_key_conflict"), 4)
        self.assertGreaterEqual(self.commit.count("structure update takeover; state="), 1)
        self.assertGreaterEqual(self.commit.count("evidence merge takeover; state="), 1)

    def test_structure_baseline_checked_under_lock(self):
        self.assertIn("if v_op ? 'continuity_baseline'", self.commit)
        self.assertIn(
            "jsonb_strip_nulls(coalesce(v_op->'continuity_baseline', '{}'::jsonb))",
            self.commit,
        )
        self.assertIn("raise exception 'memory_rumination_target_changed';", self.commit)
        # 基线核对位于同哈希分支之前，覆盖全部生命周期写入路径。
        baseline_pos = self.commit.find("if v_op ? 'continuity_baseline'")
        same_hash_pos = self.commit.find("if v_content_hash = v_target.content_hash then")
        self.assertLess(baseline_pos, same_hash_pos)

    def test_prior_markers_preserved(self):
        # 0913/0914 的关键语义原样保留。
        self.assertIn("v_evidence_count not between 1 and 960", self.commit)
        self.assertIn("jsonb_strip_nulls(coalesce(v_continuity_data, '{}'::jsonb))", self.commit)
        self.assertIn("'structure_updated_in_place'", self.commit)
        self.assertIn("'evidence_merged_unchanged'", self.commit)

    def test_security_definer_fixed_search_path_and_service_role(self):
        self.assertIn("security definer", self.commit)
        self.assertIn("set search_path to 'public', 'extensions'", self.commit)
        self.assertRegex(
            self.executable,
            r"revoke\s+all\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?from\s+public,\s*anon,\s*authenticated",
        )
        self.assertRegex(
            self.executable,
            r"grant\s+execute\s+on\s+function\s+public\.commit_rumination_batch\(bigint,\s*jsonb\)"
            r"[\s\S]{0,200}?to\s+service_role",
        )


if __name__ == "__main__":
    unittest.main()
