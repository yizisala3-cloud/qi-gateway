import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASE_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260802060000_atomic_memory_heat_decay.sql"
)
NATURAL_HEAT_MIGRATION = (
    ROOT
    / "supabase"
    / "migrations"
    / "20260804030000_natural_memory_heat_lifecycle.sql"
)
MAIN = ROOT / "gateway" / "main.py"


class MemoryHeatMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = "\n".join(
            migration.read_text(encoding="utf-8")
            for migration in (BASE_MIGRATION, NATURAL_HEAT_MIGRATION)
        ).casefold()
        cls.latest_sql = NATURAL_HEAT_MIGRATION.read_text(encoding="utf-8").casefold()
        cls.main = MAIN.read_text(encoding="utf-8").casefold()

    def test_run_is_idempotent_per_shanghai_calendar_day(self):
        self.assertIn("run_date date primary key", self.sql)
        self.assertIn("timezone('asia/shanghai'", self.sql)
        self.assertIn("on conflict (run_date) do nothing", self.sql)
        self.assertIn("'status', 'already_ran'", self.sql)

    def test_only_verified_active_non_core_memories_decay(self):
        self.assertIn("memory.is_active = true", self.latest_sql)
        self.assertIn("memory.verified = 'verified'", self.latest_sql)
        self.assertIn("coalesce(memory.layer, '碎片') <> '核心'", self.latest_sql)
        self.assertNotIn("memory.importance < 10", self.latest_sql)

    def test_importance_and_emotion_slow_but_never_stop_decay(self):
        self.assertIn("coalesce(memory.emotion_weight, 0.5)", self.latest_sql)
        self.assertIn("coalesce(memory.importance, 5)", self.latest_sql)
        self.assertIn("power(", self.latest_sql)

    def test_auto_archive_is_conservative_and_recoverable(self):
        self.assertIn("memory.layer = '碎片'", self.latest_sql)
        self.assertIn("memory.importance <= 3", self.latest_sql)
        self.assertIn("candidate.new_heat < 5.0", self.latest_sql)
        self.assertIn("interval '30 days'", self.latest_sql)
        self.assertIn("is_active = case", self.latest_sql)
        self.assertNotIn("delete from public.memories", self.latest_sql)

    def test_recollection_boost_has_diminishing_returns(self):
        self.assertIn("1.0 - v_current_heat / 100.0", self.latest_sql)
        self.assertIn("recall_count = coalesce(recall_count, 0) + 1", self.latest_sql)

    def test_decay_is_checked_every_day_without_a_one_hour_window(self):
        self.assertIn("if _last_heat_decay_date != today", self.main)
        self.assertNotIn("3 <= now_cst.hour < 4", self.main)

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

