"""Contract tests for 20260921010000_seed_context_injection_settings.sql."""

import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260921010000_seed_context_injection_settings.sql"
)

EXPECTED_KEYS = (
    "'recent_chat.inject_enabled'",
    "'recent_chat.inject_limit'",
    "'timestamp.inject_enabled'",
)


class ContextInjectionMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_runs_after_the_app_settings_migration(self):
        self.assertTrue(MIGRATION.name > "20260920010000_create_app_settings.sql")

    def test_seeds_three_keys_with_defaults(self):
        for key in EXPECTED_KEYS:
            self.assertIn(key, self.sql)
        self.assertIn("'true'::jsonb", self.sql)
        self.assertIn("'10'::jsonb", self.sql)

    def test_seed_is_idempotent(self):
        self.assertIn("insert into public.app_settings", self.sql)
        self.assertIn("on conflict (key) do nothing", self.sql)

    def test_only_seeds_never_alters_schema(self):
        self.assertNotIn("create table", self.sql)
        self.assertNotIn("alter table", self.sql)
        self.assertNotIn("drop ", self.sql)

    def test_never_touches_chat_messages(self):
        # chat_messages 对网关只读是硬约束：可执行语句里不得出现该表
        # （文件头注释中允许出现说明性文字）。
        executable = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotIn("chat_messages", executable)
        self.assertNotIn("alter table public.chat_messages", self.sql)
        self.assertNotIn("update public.chat_messages", self.sql)
        self.assertNotIn("delete from public.chat_messages", self.sql)


if __name__ == "__main__":
    unittest.main()
