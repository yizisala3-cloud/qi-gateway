import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260802020000_create_memory_requests.sql"
REVIEW_MIGRATION = ROOT / "supabase" / "migrations" / "20260802030000_review_memory_requests.sql"
INDEX_MIGRATION = ROOT / "supabase" / "migrations" / "20260802040000_index_memory_request_memory.sql"
SUPERSESSION_MIGRATION = ROOT / "supabase" / "migrations" / "20260802070000_memory_supersession.sql"
SIMILARITY_REVIEW_MIGRATION = ROOT / "supabase" / "migrations" / "20260802080000_memory_similarity_review.sql"
IDEMPOTENT_SIMILARITY_REVIEW_MIGRATION = ROOT / "supabase" / "migrations" / "20260802081000_idempotent_memory_similarity_review.sql"
AUTO_DIGEST_REQUEST_MIGRATION = ROOT / "supabase" / "migrations" / "20260804020000_auto_digest_memory_requests.sql"
MANIFEST = ROOT / "orangechat_plugins" / "memory-request" / "manifest.json"
MAIN_JS = ROOT / "orangechat_plugins" / "memory-request" / "main.js"
REVIEW_PAGE = ROOT / "admin" / "js" / "pages" / "memory_requests.js"
ROUTES_JS = ROOT / "admin" / "js" / "routes.js"
MEMORY_EXTRACT = ROOT / "gateway" / "memory_extract.py"


class MemoryRequestMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_migration_never_modifies_chat_messages(self):
        for forbidden in (
            "alter table public.chat_messages",
            "insert into public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_requests_are_private_pending_and_atomically_deduplicated(self):
        self.assertIn("alter table public.memory_requests enable row level security", self.sql)
        self.assertIn("revoke all on table public.memory_requests", self.sql)
        self.assertIn("grant select, insert, update on table public.memory_requests to service_role", self.sql)
        self.assertIn("status text not null default 'pending'", self.sql)
        self.assertIn("memory_requests_active_content_idx", self.sql)
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn("memory_request_rate_limited", self.sql)
        self.assertIn("grant execute on function public.create_memory_request", self.sql)


class MemoryReviewMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = REVIEW_MIGRATION.read_text(encoding="utf-8").casefold()

    def test_review_migration_never_modifies_chat_messages(self):
        for forbidden in (
            "alter table public.chat_messages",
            "insert into public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_review_is_atomic_private_and_audited(self):
        self.assertIn("for update", self.sql)
        self.assertIn("insert into public.memories", self.sql)
        self.assertIn("drop constraint if exists memories_source_check", self.sql)
        self.assertIn("'ai_tool_request'", self.sql)
        self.assertIn("verified", self.sql)
        self.assertIn("reviewed_at = now()", self.sql)
        self.assertIn("reviewed_by", self.sql)
        self.assertIn("review_note", self.sql)
        self.assertIn("grant execute on function public.review_memory_request", self.sql)
        self.assertIn("to service_role", self.sql)
        self.assertNotIn("to authenticated;", self.sql)

    def test_duplicate_review_is_explicitly_merged(self):
        self.assertIn("v_status := 'merged'", self.sql)
        self.assertIn("status in ('approved', 'merged')", self.sql)
        self.assertIn("where status in ('pending', 'approved')", self.sql)


class MemoryRequestIndexMigrationContractTests(unittest.TestCase):
    def test_memory_foreign_key_has_a_covering_partial_index(self):
        sql = INDEX_MIGRATION.read_text(encoding="utf-8").casefold()
        self.assertIn("memory_requests_memory_id_idx", sql)
        self.assertIn("on public.memory_requests (memory_id)", sql)
        self.assertIn("where memory_id is not null", sql)
        self.assertNotIn("chat_messages", sql)


class MemorySupersessionMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = SUPERSESSION_MIGRATION.read_text(encoding="utf-8").casefold()

    def test_mutable_fact_has_one_active_version_and_soft_links(self):
        self.assertIn("memories_active_memory_key_idx", self.sql)
        self.assertIn("where memory_key is not null", self.sql)
        self.assertIn("supersedes_memory_id", self.sql)
        self.assertIn("superseded_by_memory_id", self.sql)
        self.assertIn("superseded_at", self.sql)
        self.assertIn("is_active = false", self.sql)
        self.assertNotIn("delete from public.memories", self.sql)

    def test_review_is_atomic_locked_and_rejects_stale_updates(self):
        self.assertIn("review_memory_request_v2", self.sql)
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn("for update", self.sql)
        self.assertIn("memory_request_stale_update", self.sql)

    def test_new_rpcs_are_server_only_and_history_is_read_only(self):
        self.assertIn("create_memory_request_v2", self.sql)
        self.assertIn("to service_role", self.sql)
        self.assertNotIn("to authenticated;", self.sql)
        executable_sql = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotIn("chat_messages", executable_sql)


class MemorySimilarityReviewMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = SIMILARITY_REVIEW_MIGRATION.read_text(encoding="utf-8").casefold()

    def test_relational_review_is_atomic_audited_and_soft_only(self):
        self.assertIn("review_memory_request_v3", self.sql)
        self.assertIn("memory_request_review_events", self.sql)
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn("for update", self.sql)
        self.assertIn("related_memory_id", self.sql)
        self.assertIn("superseded_by_memory_id", self.sql)
        self.assertIn("is_active = false", self.sql)
        self.assertNotIn("delete from public.memories", self.sql)

    def test_conflict_and_duplicate_do_not_write_a_new_memory(self):
        self.assertIn("v_action in ('duplicate', 'conflict')", self.sql)
        self.assertIn("memory_request_relation_disallows_edits", self.sql)
        self.assertIn("status = v_action", self.sql)

    def test_rpc_and_event_table_are_server_only(self):
        self.assertIn("grant execute on function public.review_memory_request_v3", self.sql)
        self.assertIn("grant select, insert on table public.memory_request_review_events to service_role", self.sql)
        self.assertNotIn("to authenticated;", self.sql)
        executable_sql = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotIn("chat_messages", executable_sql)


class IdempotentMemorySimilarityReviewMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = IDEMPOTENT_SIMILARITY_REVIEW_MIGRATION.read_text(encoding="utf-8").casefold()

    def test_repeated_relational_decisions_return_unchanged(self):
        self.assertIn("review_memory_request_v4", self.sql)
        self.assertIn("v_request.status = 'merged'", self.sql)
        self.assertIn("v_request.status = 'duplicate'", self.sql)
        self.assertIn("v_request.status = 'conflict'", self.sql)
        self.assertIn("'changed', false", self.sql)
        self.assertIn("review_memory_request_v3", self.sql)

    def test_wrapper_is_locked_private_and_never_touches_chat_history(self):
        self.assertIn("for update", self.sql)
        self.assertIn("to service_role", self.sql)
        self.assertNotIn("to authenticated;", self.sql)
        executable_sql = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotIn("chat_messages", executable_sql)


class AutomaticDigestMemoryRequestMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = AUTO_DIGEST_REQUEST_MIGRATION.read_text(encoding="utf-8").casefold()

    def test_chat_history_is_evidence_only(self):
        for forbidden in (
            "alter table public.chat_messages",
            "insert into public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sql)
        self.assertIn("from public.chat_messages", self.sql)

    def test_digest_commit_creates_pending_applications_not_memories(self):
        commit_sql = self.sql.split(
            "create or replace function public.commit_memory_digest_run",
            1,
        )[1]
        self.assertIn("insert into public.memory_requests", commit_sql)
        self.assertNotIn("insert into public.memories", commit_sql)
        self.assertIn("'pending'", commit_sql)
        self.assertIn("'daily_digest'", commit_sql)
        self.assertIn("on conflict do nothing", commit_sql)
        self.assertIn("memory digest item has no valid source evidence", commit_sql)
        self.assertIn("last_processed_message_id = greatest", commit_sql)

    def test_type_evidence_time_and_provenance_are_persisted(self):
        for required in (
            "memory_type",
            "confidence",
            "evidence_message_ids",
            "source_time",
            "memory_time",
            "time_precision",
            "digest_run_id",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.sql)
        self.assertIn("sync_reviewed_memory_request_metadata", self.sql)
        self.assertIn("embedding = coalesce(new.embedding, memory.embedding)", self.sql)

    def test_digest_deduplicates_tool_receipts_without_silently_merging_similarity(self):
        for required in (
            "create extension if not exists pg_trgm",
            "memory_dedupe_text_similarity",
            "pg_advisory_xact_lock",
            "request.source_message_id = any(v_evidence_ids)",
            "request.memory_key = v_memory_key",
            "skipped_existing_request",
            "possible_duplicate",
            "related_request_id",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.sql)
        self.assertIn("v_dedupe_state := 'possible_duplicate'", self.sql)
        self.assertNotIn("status = 'duplicate'", self.sql)

    def test_existing_open_todo_is_read_only_and_suppresses_goal_application(self):
        self.assertIn("from public.todos", self.sql)
        self.assertIn("v_memory_type = 'goal'", self.sql)
        self.assertIn("skipped_existing_todo", self.sql)
        for forbidden in (
            "insert into public.todos",
            "update public.todos",
            "delete from public.todos",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_extractor_prompt_avoids_items_already_written_by_tools(self):
        source = MEMORY_EXTRACT.read_text(encoding="utf-8")
        self.assertIn("已通过记忆工具提交", source)
        self.assertIn("已通过待办工具创建", source)

    def test_rpc_and_new_metadata_remain_server_only(self):
        self.assertIn("revoke all on function public.commit_memory_digest_run", self.sql)
        self.assertIn("to service_role", self.sql)
        self.assertNotIn("to authenticated;", self.sql)
        self.assertNotIn("delete from", self.sql)


class OrangeChatPluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.main_js = MAIN_JS.read_text(encoding="utf-8")
        cls.tools = {tool["name"]: tool for tool in cls.manifest["tools"]}

    def test_manifest_tool_matches_export(self):
        expected = {
            "request_memory",
            "create_todo",
            "list_today_todos",
            "complete_todo",
            "snooze_todo",
            "cancel_todo",
        }
        self.assertEqual(set(self.tools), expected)
        for name in expected:
            self.assertIn(f"exports.{name} = {name}", self.main_js)

    def test_plugin_uses_http_gateway_without_supabase_credentials(self):
        config_names = {item["name"] for item in self.manifest["config"]}
        self.assertEqual(config_names, {
            "gateway_url",
            "plugin_token",
            "assistant_id",
            "todo_plugin_token",
            "user_name",
            "ai_name",
            "timezone_offset_minutes",
        })
        self.assertIn("/v1/memory-requests", self.main_js)
        self.assertIn("/v1/todos/query", self.main_js)
        self.assertIn("fetch(", self.main_js)
        self.assertNotIn("supabase", self.main_js.casefold())
        self.assertNotIn("websocket", self.main_js.casefold())

    def test_tool_description_requires_user_review(self):
        description = self.tools["request_memory"]["description"]
        self.assertIn("pending", description)
        self.assertIn("用户审核", description)

    def test_tool_supports_explicit_mutable_fact_replacement(self):
        parameters = {
            item["name"]: item
            for item in self.tools["request_memory"]["parameters"]
        }
        self.assertIn("update_mode", parameters)
        self.assertIn("memory_key", parameters)
        self.assertIn("payload.update_mode", self.main_js)
        self.assertIn("payload.memory_key", self.main_js)
        self.assertIn("replace", self.tools["request_memory"]["description"])

    def test_integrated_todo_tools_use_separate_token_and_soft_cancel(self):
        self.assertIn("cfg.todoPluginToken", self.main_js)
        self.assertIn("user_name: cfg.userName", self.main_js)
        self.assertIn("ai_name: cfg.aiName", self.main_js)
        self.assertIn("软隐藏", self.tools["cancel_todo"]["description"])


class MemoryReviewDashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = REVIEW_PAGE.read_text(encoding="utf-8")
        cls.routes = ROUTES_JS.read_text(encoding="utf-8")

    def test_dashboard_exposes_review_queue_and_purpose_built_endpoint(self):
        self.assertIn("key: 'memory_requests'", self.routes)
        self.assertIn("/admin/api/memory-requests/", self.page)
        self.assertIn("action: 'approve'", self.page)
        self.assertIn("action: 'reject'", self.page)
        self.assertIn("review-update-mode", self.page)
        self.assertIn("review-memory-key", self.page)
        self.assertIn("openRelation(el.dataset.id, 'merge')", self.page)
        self.assertIn("openRelation(el.dataset.id, 'duplicate')", self.page)
        self.assertIn("openRelation(el.dataset.id, 'conflict')", self.page)
        self.assertIn("request-memory-type", self.page)
        self.assertIn("evidence_message_ids", self.page)
        self.assertIn("time_precision", self.page)
        self.assertIn("dedupe_state", self.page)
        self.assertIn("related_request_id", self.page)

    def test_dashboard_never_directly_mutates_request_rows(self):
        self.assertNotIn("update('memory_requests'", self.page)
        self.assertNotIn("insert('memory_requests'", self.page)


if __name__ == "__main__":
    unittest.main()
