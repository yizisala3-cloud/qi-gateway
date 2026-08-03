import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "orangechat_plugins" / "todo" / "manifest.json"
MAIN_JS = ROOT / "orangechat_plugins" / "todo" / "main.js"
TODO_PY = ROOT / "gateway" / "todos.py"
API_PY = ROOT / "gateway" / "todo_api.py"
GATEWAY_MAIN = ROOT / "gateway" / "main.py"


class TodoPluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.main_js = MAIN_JS.read_text(encoding="utf-8")

    def test_manifest_tools_match_exports(self):
        expected = {
            "create_todo",
            "list_today_todos",
            "complete_todo",
            "snooze_todo",
            "cancel_todo",
        }
        self.assertEqual({tool["name"] for tool in self.manifest["tools"]}, expected)
        for name in expected:
            self.assertIn(f"exports.{name} = {name}", self.main_js)

    def test_plugin_uses_http_gateway_and_no_database_credentials(self):
        config_names = {item["name"] for item in self.manifest["config"]}
        self.assertEqual(config_names, {
            "gateway_url",
            "plugin_token",
            "user_name",
            "ai_name",
            "timezone_offset_minutes",
        })
        self.assertIn("fetch(", self.main_js)
        self.assertIn("/v1/todos/query", self.main_js)
        self.assertNotIn("supabase", self.main_js.casefold())
        self.assertNotIn("websocket", self.main_js.casefold())

    def test_cancel_is_soft_and_role_scope_is_mandatory(self):
        cancel_description = next(
            tool["description"] for tool in self.manifest["tools"]
            if tool["name"] == "cancel_todo"
        )
        self.assertIn("软隐藏", cancel_description)
        self.assertIn("user_name", self.main_js)
        self.assertIn("ai_name", self.main_js)


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

    def test_proactive_todos_are_appended_without_identity_environment_variables(self):
        main = GATEWAY_MAIN.read_text(encoding="utf-8")
        todos = TODO_PY.read_text(encoding="utf-8")
        self.assertIn("get_proactive_todo_context", main)
        self.assertIn("append_gateway_context", main)
        self.assertIn("asyncio.wait_for", main)
        self.assertNotIn("PROACTIVE_TODO_USER_NAME", todos + main)
        self.assertNotIn("PROACTIVE_TODO_AI_NAME", todos + main)

if __name__ == "__main__":
    unittest.main()

