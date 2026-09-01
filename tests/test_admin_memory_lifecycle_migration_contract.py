"""Static contract tests for 20260902010000_admin_memory_lifecycle.sql.

The migration must be forward-only and transactional, must never touch
public.chat_messages, must keep every new RPC on a fixed search_path behind
service_role-only grants, and must not introduce delete-pending state,
scheduled cleanup fields, or hardcoded production IDs.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260902010000_admin_memory_lifecycle.sql"

RPC_NAMES = (
    "create_admin_memory_v1",
    "edit_admin_memory_v1",
    "change_memory_type_v1",
    "undo_memory_type_change_v1",
    "restore_archived_memory_v1",
)

HELPER_NAMES = (
    "admin_memory_is_current",
    "admin_memory_reap_continuity_object",
    "admin_memory_resolve_evidence",
    "admin_memory_normalize_event_time",
    "admin_memory_tags_ok",
)


def squeezed(text: str) -> str:
    return re.sub(r"\s+", " ", text)


class AdminMemoryLifecycleMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.no_comments = re.sub(r"--[^\n]*", "", cls.sql)
        cls.flat = squeezed(cls.no_comments).casefold()

    def test_is_forward_only_and_transactional(self):
        self.assertTrue(self.no_comments.lstrip().startswith("begin;"))
        self.assertTrue(self.no_comments.rstrip().endswith("commit;"))
        self.assertNotIn("cascade", self.flat)
        self.assertNotIn("drop table", self.flat)

    def test_migration_never_modifies_chat_messages(self):
        for forbidden in (
            "alter table public.chat_messages",
            "insert into public.chat_messages",
            "update public.chat_messages",
            "delete from public.chat_messages",
            "drop table public.chat_messages",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.flat)

    def test_adds_no_columns_and_no_delete_state(self):
        # 版本链只靠既有列（supersedes/superseded_by/is_active），本 migration
        # 不新增任何列，也不引入待删除状态或定时清理字段。
        self.assertNotIn("add column", self.flat)
        for forbidden in ("delete_pending", "purge", "cleanup_at", "retire_at"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.flat)

    def test_five_lifecycle_rpcs_are_security_definer_with_fixed_search_path(self):
        for name in RPC_NAMES:
            with self.subTest(rpc=name):
                signature = self.sql.find(f"function public.{name}(")
                self.assertNotEqual(signature, -1, f"{name} not defined")
                body_start = self.sql.find("$function$", signature)
                header = self.sql[signature:body_start].casefold()
                self.assertIn("security definer", header)
                self.assertIn("set search_path to 'public'", header)

    def test_helpers_are_also_pinned_to_fixed_search_path(self):
        for name in HELPER_NAMES:
            with self.subTest(helper=name):
                self.assertIn(f"function public.{name}(", self.sql)
                signature = self.sql.find(f"function public.{name}(")
                body_start = self.sql.find("$function$", signature)
                header = self.sql[signature:body_start].casefold()
                self.assertIn("set search_path to", header)

    def test_rpcs_granted_only_to_service_role(self):
        for name in RPC_NAMES:
            with self.subTest(rpc=name):
                self.assertRegex(
                    self.no_comments.casefold(),
                    rf"revoke all on function public\.{name}\([^)]*\) from public, anon, authenticated",
                )
                self.assertRegex(
                    self.no_comments.casefold(),
                    rf"grant execute on function public\.{name}\([^)]*\) to service_role",
                )

    def test_no_hardcoded_production_ids(self):
        uuid_literal = re.compile(
            r"'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
            r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'"
        )
        self.assertIsNone(uuid_literal.search(self.no_comments))
        self.assertIsNone(re.search(r"'1[0-9]{8}'", self.no_comments))

    def test_continuity_validation_is_reused_not_duplicated(self):
        # 创建与类型修改都必须复用既有 public.validate_continuity_data。
        create_body = self._section("create_admin_memory_v1")
        change_body = self._section("change_memory_type_v1")
        for name, body in (("create", create_body), ("change", change_body)):
            with self.subTest(flow=name):
                self.assertIn("public.validate_continuity_data(", body)

    def test_manual_writes_are_pinned_server_side(self):
        create_body = self._section("create_admin_memory_v1")
        change_body = self._section("change_memory_type_v1")
        for name, body in (("create", create_body), ("change", change_body)):
            with self.subTest(flow=name):
                self.assertIn("'manual', 'verified', true", body)
                self.assertIn("50.0", body)
        # 前端伪造身份与服务端固定值都必须被拒绝。
        self.assertIn("admin_memory_assistant_required", self.flat)
        self.assertIn("admin_memory_recall_vector_missing", self.flat)

    def test_recall_vector_required_before_transaction_for_live_scene(self):
        create_body = self._section("create_admin_memory_v1")
        change_body = self._section("change_memory_type_v1")
        for name, body in (("create", create_body), ("change", change_body)):
            with self.subTest(flow=name):
                self.assertIn("admin_memory_recall_vector_missing", body)

    def test_change_type_versions_and_cleans_generations(self):
        body = self._section("change_memory_type_v1")
        self.assertIn("admin_memory_type_unchanged", body)
        self.assertIn("supersedes_memory_id, superseded_by_memory_id, superseded_at", body)
        self.assertIn("is_active = false", body)
        self.assertIn("delete from public.memories where id = v_earlier.id", body)
        self.assertIn("admin_memory_reap_continuity_object", body)
        self.assertIn("pg_advisory_xact_lock", body)

    def test_reaper_never_deletes_objects_still_in_use(self):
        body = self._section("admin_memory_reap_continuity_object")
        self.assertIn("from public.memories as memory", body)
        self.assertIn("from public.memory_relations as relation", body)
        # 追加式审核历史不删除；只解除 memory_requests 的可空引用。
        self.assertIn("set continuity_id = null", body)
        self.assertNotIn("delete from public.memory_requests", self.flat)
        self.assertNotIn("delete from public.memory_request_review_events", self.flat)

    def test_undo_is_restricted_to_the_last_manual_type_change(self):
        body = self._section("undo_memory_type_change_v1")
        self.assertIn("admin_memory_no_previous_version", body)
        self.assertIn("admin_memory_previous_conflict", body)
        self.assertIn("v_current.source is distinct from 'manual'", body)
        # 物理删除优先，且只有引用约束阻止时才转归档。
        self.assertIn("when foreign_key_violation", body)
        self.assertLess(
            body.find("delete from public.memories where id = v_current.id"),
            body.find("update public.memories"),
        )

    def test_restore_checks_every_double_active_conflict(self):
        body = self._section("restore_archived_memory_v1")
        self.assertIn("admin_memory_superseded", body)
        self.assertIn("admin_memory_continuity_conflict", body)
        self.assertIn("admin_memory_key_conflict", body)
        self.assertIn("admin_memory_chain_conflict", body)
        self.assertIn("heat = 50.0", body)

    def test_edit_refuses_non_current_rows_and_type_switches(self):
        body = self._section("edit_admin_memory_v1")
        self.assertIn("admin_memory_not_editable", body)
        self.assertIn("admin_memory_type_change_forbidden", body)
        # 未分类旧记忆只编辑普通字段时不触发连续感校验。
        self.assertIn("admin_memory_class_required", body)

    def test_content_hash_is_server_maintained(self):
        # 哈希只从服务端参数落入写路径；前端无法经 generic PATCH 绕过。
        create_body = self._section("create_admin_memory_v1")
        self.assertIn("p_content_hash", create_body)
        self.assertIn("admin_memory_invalid_content_hash", self.flat)

    def test_evidence_times_derived_only_from_real_messages(self):
        body = self._section("admin_memory_resolve_evidence")
        self.assertIn("from public.chat_messages as message", body)
        self.assertIn("message.assistant_id = p_assistant_id", body)
        # 没有证据时全部时间为 NULL，不使用 created_at 兜底。
        self.assertIn("'ids', null::bigint[]", body)
        self.assertNotIn("coalesce(message.created_at", self.flat)
        self.assertNotIn("now()", body)

    def test_no_new_duplicate_memory_table(self):
        self.assertNotIn("create table", self.flat)

    def _section(self, name: str) -> str:
        start = self.sql.find(f"function public.{name}(")
        self.assertNotEqual(start, -1, f"{name} not found")
        end = self.sql.find("$function$;", start)
        self.assertNotEqual(end, -1)
        return self.sql[start:end].casefold()


if __name__ == "__main__":
    unittest.main()
