import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260802050000_harden_memory_search_functions.sql"
)


class MemorySearchMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_search_and_heat_only_touch_verified_active_memories(self):
        self.assertIn("memory.verified = 'verified'", self.sql)
        self.assertIn("memory.is_active = true", self.sql)
        self.assertIn("and verified = 'verified'", self.sql)
        self.assertIn("and is_active = true", self.sql)

    def test_rpcs_are_server_only_and_have_fixed_search_paths(self):
        self.assertIn("set search_path to 'public', 'extensions'", self.sql)
        self.assertIn("set search_path to 'public'", self.sql)
        self.assertIn("from public, anon, authenticated", self.sql)
        self.assertEqual(self.sql.count("to service_role"), 2)

    def test_limits_are_bounded_and_chat_messages_is_untouched(self):
        self.assertIn("limit least(greatest(coalesce(match_count, 20), 1), 50)", self.sql)
        self.assertIn("least(greatest(coalesce(boost_amount, 15), 0), 25)", self.sql)
        self.assertNotIn("chat_messages", self.sql.replace(
            "-- chat_messages remains an immutable read-only source and is not modified.",
            "",
        ))


if __name__ == "__main__":
    unittest.main()

