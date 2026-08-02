import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260802060000_atomic_memory_heat_decay.sql"
)


class MemoryHeatMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_run_is_idempotent_per_shanghai_calendar_day(self):
        self.assertIn("run_date date primary key", self.sql)
        self.assertIn("timezone('asia/shanghai'", self.sql)
        self.assertIn("on conflict (run_date) do nothing", self.sql)
        self.assertIn("'status', 'already_ran'", self.sql)

    def test_only_verified_active_non_permanent_memories_decay(self):
        self.assertIn("memory.is_active = true", self.sql)
        self.assertIn("memory.verified = 'verified'", self.sql)
        self.assertIn("memory.importance < 10", self.sql)

    def test_auto_archive_is_conservative_and_recoverable(self):
        self.assertIn("memory.layer = '碎片'", self.sql)
        self.assertIn("memory.importance <= 3", self.sql)
        self.assertIn("candidate.new_heat < 5.0", self.sql)
        self.assertIn("interval '90 days'", self.sql)
        self.assertIn("is_active = case", self.sql)
        self.assertNotIn("delete from public.memories", self.sql)

    def test_rpc_and_run_log_are_server_only(self):
        self.assertIn("alter table public.memory_heat_runs enable row level security", self.sql)
        self.assertIn("revoke all on table public.memory_heat_runs", self.sql)
        self.assertIn("grant execute on function public.run_memory_heat_decay", self.sql)
        self.assertIn("to service_role", self.sql)

    def test_chat_history_is_never_referenced(self):
        executable_sql = "\n".join(
            line for line in self.sql.splitlines()
            if not line.lstrip().startswith("--")
        )
        self.assertNotIn("chat_messages", executable_sql)


if __name__ == "__main__":
    unittest.main()

