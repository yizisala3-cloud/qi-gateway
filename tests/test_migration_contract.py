import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260802010000_memory_digest_assistant_provenance.sql"
)


class MemoryDigestMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_migration_never_alters_chat_messages(self):
        self.assertNotIn("alter table public.chat_messages", self.sql)
        self.assertNotIn("update public.chat_messages", self.sql)
        self.assertNotIn("delete from public.chat_messages", self.sql)

    def test_atomic_commit_preserves_all_required_provenance(self):
        for required in (
            "assistant_id",
            "digest_run_id",
            "source_first_message_id",
            "source_last_message_id",
            "content_hash",
        ):
            with self.subTest(required=required):
                self.assertIn(required, self.sql)

        self.assertIn("v_run.assistant_id", self.sql)
        self.assertIn("on conflict (content_hash) do nothing", self.sql)
        self.assertIn("last_processed_message_id = greatest", self.sql)

    def test_automatic_memories_stay_pending(self):
        self.assertIn("'pending'", self.sql)


if __name__ == "__main__":
    unittest.main()
