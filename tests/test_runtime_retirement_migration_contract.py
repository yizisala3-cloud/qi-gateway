import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase/migrations/20260817010000_drop_retired_legacy_runtime.sql"


class RuntimeRetirementMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.executable_sql = re.sub(r"--[^\n]*", "", cls.sql)

    def test_only_the_four_retired_runtime_tables_are_dropped(self):
        dropped = re.findall(
            r"drop\s+table\s+if\s+exists\s+public\.([a-z_]+)",
            self.executable_sql,
        )
        self.assertEqual(
            dropped,
            ["busy_inbox", "timers", "proactive_messages", "jiwen_state"],
        )

    def test_migration_is_atomic_and_does_not_use_cascade(self):
        self.assertRegex(self.executable_sql, r"\bbegin\s*;")
        self.assertRegex(self.executable_sql, r"\bcommit\s*;")
        self.assertNotRegex(self.executable_sql, r"\bcascade\b")

    def test_chat_messages_and_retained_system_tables_are_untouched(self):
        for retained in (
            "chat_messages",
            "eventide_state",
            "memories",
            "memory_requests",
            "memory_digest_runs",
            "memory_digest_cursors",
            "memory_continuity_cursors",
            "todos",
        ):
            self.assertNotRegex(
                self.executable_sql,
                rf"(?:drop|alter|truncate|delete\s+from|update|insert\s+into)\s+(?:table\s+)?(?:public\.)?{retained}\b",
            )


if __name__ == "__main__":
    unittest.main()
