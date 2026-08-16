from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260816010000_memory_continuity_pipeline.sql"


class ContinuityMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()

    def test_independent_cursor_starts_at_177(self):
        self.assertIn("create table if not exists public.memory_continuity_cursors", self.sql)
        self.assertIn("last_processed_message_id bigint not null default 177", self.sql)
        self.assertNotRegex(self.sql, r"(?:insert into|update)\s+public\.memory_digest_cursors")

    def test_continuity_fields_exist_on_requests_and_memories(self):
        for table in ("public.memory_requests", "public.memories"):
            section = self.sql[self.sql.index(f"alter table {table}"):]
            for field in (
                "continuity_type", "subject", "source_type", "thread_state",
                "continuity_value", "retention_class", "participants",
                "evidence_start_time", "evidence_end_time",
            ):
                self.assertIn(field, section)

    def test_pipeline_and_atomic_rpcs_are_declared_and_service_role_only(self):
        for function in (
            "get_or_create_memory_continuity_cursor",
            "pause_memory_continuity_empty",
            "commit_memory_continuity_run",
            "skip_memory_continuity_blocked",
        ):
            self.assertIn(f"function public.{function}", self.sql)
            self.assertRegex(self.sql, rf"grant execute on function public\.{function}\([^;]+\)\s+to service_role")
        self.assertIn("pipeline in ('legacy', 'continuity')", self.sql)

    def test_commit_validates_evidence_and_writes_pending(self):
        self.assertIn("message.assistant_id = v_run.assistant_id", self.sql)
        self.assertIn("message.id between v_run.source_first_message_id and v_run.source_last_message_id", self.sql)
        self.assertIn("'pending', 'daily_digest'", self.sql)
        self.assertIn("possible_duplicate", self.sql)
        self.assertIn("memory_continuity_missing_embedding", self.sql)

    def test_pause_retry_skip_and_cooldowns_are_independent(self):
        self.assertIn("status = 'paused_empty'", self.sql)
        self.assertIn("pause_reason = 'empty_candidates'", self.sql)
        self.assertIn("interval '10 seconds'", self.sql)
        self.assertIn("interval '1 hour'", self.sql)
        self.assertIn("blocked_last_message_id", self.sql)
        self.assertIn("'continuity_skip'", self.sql)
        self.assertGreaterEqual(
            self.sql.count("pg_advisory_xact_lock(hashtextextended(p_assistant_id, 1))"),
            2,
        )
        self.assertIn("memory_continuity_already_running", self.sql)

    def test_review_trigger_copies_all_continuity_metadata(self):
        trigger_start = self.sql.rindex("create or replace function public.sync_reviewed_memory_request_metadata")
        trigger_sql = self.sql[trigger_start:]
        self.assertIn("continuity_type = coalesce(new.continuity_type, memory.continuity_type)", trigger_sql)
        for field in (
            "subject", "source_type", "thread_state", "continuity_value",
            "retention_class", "participants", "evidence_start_time", "evidence_end_time",
        ):
            self.assertIn(f"then new.{field} else memory.{field} end", trigger_sql)
        self.assertIn("new.status in ('approved', 'merged')", trigger_sql)

    def test_chat_messages_is_select_only(self):
        self.assertIn("from public.chat_messages", self.sql)
        self.assertNotRegex(
            self.sql,
            r"(?:insert\s+into|update|delete\s+from|alter\s+table)\s+public\.chat_messages",
        )
        module = (ROOT / "gateway" / "memory_continuity.py").read_text(encoding="utf-8").lower()
        self.assertIn('.table("chat_messages")', module)
        self.assertNotRegex(
            module,
            r"table\(\"chat_messages\"\)[\s\S]{0,100}\.(?:insert|update|delete|upsert)\(",
        )

    def test_legacy_run_history_is_filtered_from_continuity(self):
        source = (ROOT / "gateway" / "memory_extract.py").read_text(encoding="utf-8")
        self.assertIn('"pipeline": "legacy"', source)
        self.assertIn('.eq("pipeline", "legacy")', source)


class ContinuitySchedulerContractTests(unittest.TestCase):
    def test_scheduler_uses_continuity_and_keeps_heat_decay(self):
        source = (ROOT / "gateway" / "main.py").read_text(encoding="utf-8")
        self.assertIn("run_continuity_digest_if_due", source)
        self.assertNotIn("from .memory_extract import run_scheduled_digest_if_due", source)
        self.assertNotIn("run_in_executor(bg_executor, run_scheduled_digest_if_due)", source)
        self.assertIn("run_in_executor(bg_executor, run_heat_decay)", source)


if __name__ == "__main__":
    unittest.main()
