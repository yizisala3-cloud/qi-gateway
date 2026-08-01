import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260802020000_create_memory_requests.sql"
REVIEW_MIGRATION = ROOT / "supabase" / "migrations" / "20260802030000_review_memory_requests.sql"
MANIFEST = ROOT / "orangechat_plugins" / "memory-request" / "manifest.json"
MAIN_JS = ROOT / "orangechat_plugins" / "memory-request" / "main.js"
REVIEW_PAGE = ROOT / "admin" / "js" / "pages" / "memory_requests.js"
ROUTES_JS = ROOT / "admin" / "js" / "routes.js"


class MemoryRequestMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").casefold()

    def test_migration_never_modifies_chat_messages(self):
        for forbidden in (
            "alter table public.chat_messages",
            "insert into public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_requests_are_private_pending_and_atomically_deduplicated(self):
        self.assertIn("alter table public.memory_requests enable row level security", self.sql)
        self.assertIn("revoke all on table public.memory_requests", self.sql)
        self.assertIn("grant select, insert, update on table public.memory_requests to service_role", self.sql)
        self.assertIn("status text not null default 'pending'", self.sql)
        self.assertIn("memory_requests_active_content_idx", self.sql)
        self.assertIn("pg_advisory_xact_lock", self.sql)
        self.assertIn("memory_request_rate_limited", self.sql)
        self.assertIn("grant execute on function public.create_memory_request", self.sql)


class MemoryReviewMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = REVIEW_MIGRATION.read_text(encoding="utf-8").casefold()

    def test_review_migration_never_modifies_chat_messages(self):
        for forbidden in (
            "alter table public.chat_messages",
            "insert into public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.sql)

    def test_review_is_atomic_private_and_audited(self):
        self.assertIn("for update", self.sql)
        self.assertIn("insert into public.memories", self.sql)
        self.assertIn("verified", self.sql)
        self.assertIn("reviewed_at = now()", self.sql)
        self.assertIn("reviewed_by", self.sql)
        self.assertIn("review_note", self.sql)
        self.assertIn("grant execute on function public.review_memory_request", self.sql)
        self.assertIn("to service_role", self.sql)
        self.assertNotIn("to authenticated;", self.sql)

    def test_duplicate_review_is_explicitly_merged(self):
        self.assertIn("v_status := 'merged'", self.sql)
        self.assertIn("status in ('approved', 'merged')", self.sql)
        self.assertIn("where status in ('pending', 'approved')", self.sql)


class OrangeChatPluginContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.main_js = MAIN_JS.read_text(encoding="utf-8")

    def test_manifest_tool_matches_export(self):
        tool_names = {tool["name"] for tool in self.manifest["tools"]}
        self.assertEqual(tool_names, {"request_memory"})
        self.assertIn("exports.request_memory = request_memory", self.main_js)

    def test_plugin_uses_http_gateway_without_supabase_credentials(self):
        config_names = {item["name"] for item in self.manifest["config"]}
        self.assertEqual(config_names, {"gateway_url", "plugin_token", "assistant_id"})
        self.assertIn("/v1/memory-requests", self.main_js)
        self.assertIn("fetch(", self.main_js)
        self.assertNotIn("supabase", self.main_js.casefold())
        self.assertNotIn("websocket", self.main_js.casefold())

    def test_tool_description_requires_user_review(self):
        description = self.manifest["tools"][0]["description"]
        self.assertIn("pending", description)
        self.assertIn("用户审核", description)


class MemoryReviewDashboardContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = REVIEW_PAGE.read_text(encoding="utf-8")
        cls.routes = ROUTES_JS.read_text(encoding="utf-8")

    def test_dashboard_exposes_review_queue_and_purpose_built_endpoint(self):
        self.assertIn("key: 'memory_requests'", self.routes)
        self.assertIn("/admin/api/memory-requests/", self.page)
        self.assertIn("action: 'approve'", self.page)
        self.assertIn("action: 'reject'", self.page)

    def test_dashboard_never_directly_mutates_request_rows(self):
        self.assertNotIn("update('memory_requests'", self.page)
        self.assertNotIn("insert('memory_requests'", self.page)


if __name__ == "__main__":
    unittest.main()

