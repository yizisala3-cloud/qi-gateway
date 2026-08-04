import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEARCH_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260802050000_harden_memory_search_functions.sql"
)
NATURAL_HEAT_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260804030000_natural_memory_heat_lifecycle.sql"
)


class MemorySearchMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.search_sql = SEARCH_MIGRATION.read_text(encoding="utf-8").casefold()
        cls.heat_sql = NATURAL_HEAT_MIGRATION.read_text(encoding="utf-8").casefold()
        cls.sql = f"{cls.search_sql}\n{cls.heat_sql}"

    def test_search_and_heat_only_touch_verified_active_memories(self):
        self.assertIn("memory.verified = 'verified'", self.sql)
        self.assertIn("memory.is_active = true", self.sql)
        self.assertIn("and verified = 'verified'", self.sql)
        self.assertIn("and is_active = true", self.sql)

    def test_rpcs_are_server_only_and_have_fixed_search_paths(self):
        self.assertIn("set search_path to 'public', 'extensions'", self.search_sql)
        self.assertIn("set search_path to 'public'", self.heat_sql)
        self.assertIn("from public, anon, authenticated", self.heat_sql)
        self.assertIn("to service_role", self.heat_sql)

    def test_limits_are_bounded_and_chat_messages_is_untouched(self):
        self.assertIn("limit least(greatest(coalesce(match_count, 20), 1), 50)", self.sql)
        self.assertIn("greatest(coalesce(boost_amount, 8), 0)", self.heat_sql)
        self.assertIn("1.0 - v_current_heat / 100.0", self.heat_sql)
        executable_sql = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotIn("chat_messages", executable_sql)


if __name__ == "__main__":
    unittest.main()

