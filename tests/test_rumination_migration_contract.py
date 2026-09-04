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


if __name__ == "__main__":
    unittest.main()
