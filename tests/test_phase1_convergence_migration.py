from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260820090617_converge_phase1_continuity_mcp.sql"


class Phase1ConvergenceMigrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()

    def function_body(self, name: str, next_name: str | None = None) -> str:
        start = self.sql.index(f"create or replace function public.{name}")
        if next_name is None:
            return self.sql[start:]
        end = self.sql.index(f"create or replace function public.{next_name}", start)
        return self.sql[start:end]

    def test_is_forward_only_and_transactional(self) -> None:
        self.assertTrue(self.sql.lstrip().startswith("-- converge"))
        self.assertIn("begin;", self.sql)
        self.assertTrue(self.sql.rstrip().endswith("commit;"))
        self.assertNotIn("create table public.memory_continuity_objects", self.sql)
        self.assertNotIn("create table public.memory_relations", self.sql)
        self.assertNotRegex(self.sql, r"add\s+column\s+continuity_(?:id|schema_version|data)")
        self.assertNotIn("cascade", self.sql)

    def test_chat_messages_is_select_only(self) -> None:
        self.assertIn("from public.chat_messages", self.sql)
        prohibited = re.compile(
            r"(?:alter\s+table|insert\s+into|update|delete\s+from|truncate|policy|row\s+level\s+security)"
            r"[^;]*public\.chat_messages",
            re.DOTALL,
        )
        self.assertIsNone(prohibited.search(self.sql))

    def test_creates_missing_validation_and_core_rpcs(self) -> None:
        for name in (
            "continuity_text_ok",
            "continuity_object_keys_ok",
            "continuity_integer_ok",
            "continuity_string_array_ok",
            "validate_continuity_data",
            "create_memory_request_v4",
            "write_memory_direct_v1",
            "review_memory_request_v5",
        ):
            self.assertIn(f"create or replace function public.{name}", self.sql)

    def test_pending_identity_is_null_but_shape_is_required(self) -> None:
        create = self.function_body("create_memory_request_v4", "store_continuity_candidate")
        self.assertIn("p_continuity_type,p_thread_state,null,1,p_continuity_data", create)
        request_v1 = self.sql.split("add constraint memory_requests_continuity_v1_check", 1)[1]
        request_v1 = request_v1.split("add constraint memory_requests_automatic_type_check", 1)[0]
        self.assertIn("continuity_schema_version = 1", request_v1)
        self.assertIn("validate_continuity_data", request_v1)
        self.assertNotIn("continuity_id is not null", request_v1)

    def test_formal_v1_requires_identity_and_dissolved_is_legal(self) -> None:
        memories = self.sql.split("alter table public.memories", 1)[1]
        self.assertIn("continuity_id is not null", memories)
        self.assertIn("'open','paused','resolved','dissolved','abandoned','unknown'", self.sql)
        validator = self.function_body("validate_continuity_data", "allocate_memory_continuity_id")
        self.assertIn("v_closed := p_thread_state in ('resolved','dissolved','abandoned')", validator)

    def test_rejects_unknown_fields_and_matches_numeric_bounds(self) -> None:
        validator = self.function_body("validate_continuity_data", "allocate_memory_continuity_id")
        self.assertIn("continuity_object_keys_ok", validator)
        self.assertGreaterEqual(validator.count("return coalesce(("), 5)
        self.assertIn("if p_thread_state is null", validator)
        self.assertIn("continuity_integer_ok(p_data,'priority',1,10,true)", validator)
        self.assertIn("continuity_integer_ok(p_data,'reinforcement_count',0,2147483647,false)", validator)
        self.assertIn("jsonb_array_length(p_data->p_key) > 8", self.sql)
        self.assertIn("not between 1 and 120", self.sql)

    def test_open_and_closed_thread_rules_match_python(self) -> None:
        validator = self.function_body("validate_continuity_data", "allocate_memory_continuity_id")
        for field in ("closure_summary", "closure_reason", "closed_at"):
            self.assertIn(f"continuity_text_ok(p_data,'{field}',true", validator)
            self.assertIn(f"coalesce(btrim(p_data->>'{field}'),'') = ''", validator)

    def test_retired_columns_are_removed(self) -> None:
        self.assertIn("drop column if exists memory_type", self.sql)
        self.assertIn("drop column if exists proposed_relations", self.sql)
        runtime = self.sql.split("create or replace function public.allocate_memory_continuity_id", 1)[1]
        runtime_without_comments = re.sub(r"--[^\n]*", "", runtime)
        self.assertNotIn("proposed_relations", runtime_without_comments)
        self.assertNotRegex(runtime_without_comments, r"\bmemory_type\b")

    def test_idempotency_and_exact_dedupe_precede_one_minute_limit(self) -> None:
        create = self.function_body("create_memory_request_v4", "store_continuity_candidate")
        idempotency = create.index("idempotency_key = p_idempotency_key")
        exact = create.index("content_hash = p_content_hash")
        rate = create.index("interval '1 minute'")
        self.assertLess(idempotency, rate)
        self.assertLess(exact, rate)

    def test_three_low_risk_types_auto_approve(self) -> None:
        direct = self.function_body("write_memory_direct_v1")
        self.assertIn("p_continuity_type not in ('moment','thread','inside_joke')", direct)
        self.assertIn("review_memory_request_v5", direct)
        writer = self.function_body("store_continuity_candidate", "commit_memory_digest_run")
        self.assertIn("v_type in ('moment','thread','inside_joke')", writer)
        self.assertIn("review_memory_request_v5", writer)

    def test_high_weight_types_remain_pending(self) -> None:
        writer = self.function_body("store_continuity_candidate", "commit_memory_digest_run")
        self.assertIn("v_type not in ('moment','thread','episode','inside_joke')", writer)
        automatic_block = writer.split("if v_delta = 1", 1)[1]
        self.assertNotIn("episode", automatic_block)
        create = self.function_body("create_memory_request_v4", "store_continuity_candidate")
        self.assertIn("'pending'", create)

    def test_runtime_never_writes_relations(self) -> None:
        runtime = self.sql.split("create or replace function public.allocate_memory_continuity_id", 1)[1]
        self.assertNotRegex(
            runtime,
            r"(?:insert\s+into|update|delete\s+from)\s+public\.memory_relations",
        )

    def test_recall_is_rebuilt_only_where_incompatible(self) -> None:
        self.assertNotIn("drop function if exists public.match_memories", self.sql)
        self.assertNotIn("create or replace function public.match_memories", self.sql)
        self.assertIn("drop function if exists public.search_memories_by_keywords", self.sql)
        keyword = self.sql.split("create function public.search_memories_by_keywords", 1)[1]
        self.assertNotIn("memory_type", keyword)
        self.assertIn("memory.is_active = true", keyword)
        self.assertIn("memory.verified = 'verified'", keyword)
        self.assertIn("input.position <= 5", keyword)
        self.assertIn("result_limit,20),1),50", keyword)

    def test_new_entry_points_are_service_role_only(self) -> None:
        for signature in (
            "create_memory_request_v4",
            "write_memory_direct_v1",
            "review_memory_request_v5",
            "commit_memory_digest_run",
            "commit_memory_continuity_run",
            "search_memories_by_keywords",
        ):
            self.assertRegex(self.sql, rf"revoke all on function public\.{signature}\([^;]+from public,anon,authenticated;")
            self.assertRegex(self.sql, rf"grant execute on function public\.{signature}\([^;]+to service_role;")


if __name__ == "__main__":
    unittest.main()
