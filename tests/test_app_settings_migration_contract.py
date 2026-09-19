"""Contract tests for 20260920010000_create_app_settings.sql."""

import unittest
from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20260920010000_create_app_settings.sql"
)


class AppSettingsMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_creates_app_settings_table(self):
        self.assertIn("create table if not exists public.app_settings", self.sql)
        self.assertIn("key text primary key", self.sql)
        self.assertIn("value jsonb not null", self.sql)
        self.assertIn("updated_at timestamptz not null default now()", self.sql)

    def test_seeds_eventide_switch_idempotently(self):
        self.assertIn("'eventide.inject_enabled'", self.sql)
        self.assertIn("'true'::jsonb", self.sql)
        self.assertIn("on conflict (key) do nothing", self.sql)

    def test_locks_table_to_service_role(self):
        self.assertIn("enable row level security", self.sql)
        self.assertIn(
            "revoke all on table public.app_settings from anon, authenticated", self.sql
        )
        self.assertIn(
            "grant select, insert, update on table public.app_settings to service_role",
            self.sql,
        )

    def test_never_alters_chat_messages(self):
        self.assertNotIn("alter table public.chat_messages", self.sql)
        self.assertNotIn("update public.chat_messages", self.sql)
        self.assertNotIn("delete from public.chat_messages", self.sql)


if __name__ == "__main__":
    unittest.main()
