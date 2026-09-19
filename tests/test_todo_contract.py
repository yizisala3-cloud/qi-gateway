import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TODO_PY = ROOT / "gateway" / "todos.py"
API_PY = ROOT / "gateway" / "todo_api.py"
GATEWAY_MAIN = ROOT / "gateway" / "main.py"
REMINDER_MIGRATION = (
    ROOT / "supabase" / "migrations"
    / "20260804010000_atomic_proactive_todo_claim.sql"
)


class TodoGatewayContractTests(unittest.TestCase):
    def test_gateway_registers_all_todo_routes(self):
        api = API_PY.read_text(encoding="utf-8")
        main = GATEWAY_MAIN.read_text(encoding="utf-8")
        for path in (
            '"/v1/todos"',
            '"/v1/todos/query"',
            '"/v1/todos/{todo_id}/complete"',
            '"/v1/todos/{todo_id}/snooze"',
            '"/v1/todos/{todo_id}/cancel"',
        ):
            self.assertIn(path, api)
        self.assertIn("_routes.extend(todo_routes)", main)

    def test_todo_code_never_touches_chat_messages_or_deletes_rows(self):
        code = (
            TODO_PY.read_text(encoding="utf-8")
            + API_PY.read_text(encoding="utf-8")
        ).casefold()
        self.assertNotIn('table("chat_messages")', code)
        self.assertNotIn(".delete(", code)
        self.assertIn('"is_hidden": true', code)

    def test_todo_feedback_guidance_remains_in_chat_path(self):
        main = GATEWAY_MAIN.read_text(encoding="utf-8")
        self.assertIn("build_todo_feedback_guidance", main)
        self.assertIn("append_gateway_context", main)

    def test_retired_proactive_todo_layer_is_absent(self):
        main = GATEWAY_MAIN.read_text(encoding="utf-8")
        todos = TODO_PY.read_text(encoding="utf-8")
        self.assertNotIn("get_proactive_todo_context", todos + main)
        self.assertNotIn("claim_proactive_todos", todos + main)
        self.assertNotIn("PROACTIVE_TODO_USER_NAME", todos + main)
        self.assertNotIn("PROACTIVE_TODO_AI_NAME", todos + main)

    def test_proactive_claim_migration_keeps_atomic_three_hour_cooldown(self):
        sql = REMINDER_MIGRATION.read_text(encoding="utf-8").casefold()
        self.assertIn("create table if not exists public.todo_reminder_state", sql)
        self.assertIn("create or replace function public.claim_proactive_todos", sql)
        self.assertIn("for update of t skip locked", sql)
        self.assertIn("limit v_limit\n        for update of t skip locked", sql)
        self.assertIn("p_cooldown_minutes integer default 180", sql)
        self.assertIn("last_offered_at", sql)
        self.assertNotIn("daily_limit", sql)
        self.assertNotIn("offer_count", sql)
        self.assertNotIn("reminder_count", sql)

if __name__ == "__main__":
    unittest.main()

