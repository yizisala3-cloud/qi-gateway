import json
import re
import unittest
from pathlib import Path

from gateway.memory_continuity_schema import (
    ContinuityDataError,
    validate_continuity_data,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260818010000_phase1_continuity_objects_relations.sql"


def good_payloads():
    return {
        "moment": (None, {"scene": "聊天窗口", "event": "一起确定计划", "moment_state": "standalone"}),
        "thread": ("open", {"open_question": "下一步是什么", "current_state": "等待确认", "closure_criteria": [],
                              "abstract_retrieval_hints": [], "concrete_retrieval_hints": []}),
        "episode": (None, {"beginning": "开始讨论", "development": "比较方案", "outcome": "选定方案", "closure_quality": "complete"}),
        "inside_joke": (None, {"origin": "一次口误", "trigger_phrases": ["小橘子"], "shared_meaning": "共同玩笑",
                                     "usage_context": [], "avoid_context": [], "reinforcement_count": 0}),
        "profile": (None, {"facet": "偏好", "statement": "喜欢安静清晨", "scope": "日常", "stability": "stable",
                           "exceptions": [], "basis": "explicit_preference"}),
        "interaction_rule": (None, {"trigger": "用户明确求助", "expected_behavior": "先给结论", "forbidden_behavior": [],
                                    "scope": "对话", "priority": 8, "rule_state": "active", "exceptions": [],
                                    "explicit_instruction": "用户明确要求以后先给结论"}),
    }


class ContinuitySchemaTests(unittest.TestCase):
    def test_all_six_types_validate(self):
        for kind, (state, data) in good_payloads().items():
            with self.subTest(kind=kind):
                self.assertIsInstance(validate_continuity_data(kind, state, data), dict)

    def test_relationship_is_rejected(self):
        with self.assertRaises(ContinuityDataError):
            validate_continuity_data("relationship", None, {})

    def test_automatic_sources_only_allow_four_types(self):
        for kind in ("profile", "interaction_rule"):
            state, data = good_payloads()[kind]
            with self.subTest(kind=kind), self.assertRaises(ContinuityDataError):
                validate_continuity_data(kind, state, data, automatic=True)

    def test_dissolved_requires_closure_fields(self):
        data = good_payloads()["thread"][1]
        with self.assertRaises(ContinuityDataError):
            validate_continuity_data("thread", "dissolved", data)
        data.update({"closure_summary": "前提失效", "closure_reason": "条件已取消", "closed_at": "2026-08-18T20:00+08:00"})
        self.assertEqual(validate_continuity_data("thread", "dissolved", data)["closure_reason"], "条件已取消")

    def test_open_thread_rejects_closure_fields(self):
        data = good_payloads()["thread"][1] | {"closure_summary": "不应出现"}
        with self.assertRaises(ContinuityDataError):
            validate_continuity_data("thread", "open", data)

    def test_required_fields_and_array_limits_are_enforced(self):
        with self.assertRaises(ContinuityDataError):
            validate_continuity_data("moment", None, {"event": "缺 scene", "moment_state": "standalone"})
        data = good_payloads()["inside_joke"][1] | {"trigger_phrases": [str(i) for i in range(9)]}
        with self.assertRaises(ContinuityDataError):
            validate_continuity_data("inside_joke", None, data)

    def test_interaction_rule_requires_direct_instruction(self):
        data = good_payloads()["interaction_rule"][1].copy()
        data.pop("explicit_instruction")
        with self.assertRaises(ContinuityDataError):
            validate_continuity_data("interaction_rule", None, data)


class Phase1MigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()

    def test_migration_is_new_and_does_not_use_cascade(self):
        self.assertTrue(MIGRATION.exists())
        self.assertNotIn(" cascade", self.sql)

    def test_chat_messages_is_select_only(self):
        for verb in ("insert into public.chat_messages", "update public.chat_messages", "delete from public.chat_messages", "alter table public.chat_messages"):
            self.assertNotIn(verb, self.sql)
        self.assertIn("from public.chat_messages", self.sql)

    def test_stable_object_registry_and_non_cascading_fks_exist(self):
        self.assertIn("create table public.memory_continuity_objects", self.sql)
        self.assertIn("references public.memory_continuity_objects(continuity_id)", self.sql)
        self.assertIn("memories_one_active_version_per_continuity", self.sql)

    def test_memory_type_is_dropped_and_not_returned_by_recall(self):
        self.assertIn("drop column if exists memory_type", self.sql)
        vector = self.sql.split("create function public.match_memories", 1)[1]
        keyword = self.sql.split("create function public.search_memories_by_keywords", 1)[1]
        self.assertNotIn("memory_type", vector)
        self.assertNotIn("memory_type", keyword)

    def test_six_types_and_dissolved_are_database_values(self):
        for value in ("moment", "thread", "episode", "inside_joke", "profile", "interaction_rule", "dissolved"):
            self.assertIn(f"'{value}'", self.sql)
        self.assertNotIn("'relationship'", self.sql)

    def test_append_allocates_and_replace_inherits_identity(self):
        allocator = self.sql.split("create or replace function public.allocate_memory_continuity_id", 1)[1]
        self.assertIn("if p_update_mode = 'replace'", allocator)
        self.assertIn("select id, continuity_id", allocator)
        self.assertIn("insert into public.memory_continuity_objects", allocator)
        review = self.sql.split("create or replace function public.review_memory_request_v5", 1)[1]
        self.assertIn("v_id:=public.allocate_memory_continuity_id", review)
        self.assertIn("continuity_id=v_id,update_mode=v_mode,memory_key=v_key", review)

    def test_pending_candidates_never_create_formal_relations(self):
        writer = self.sql.split("create or replace function public.store_continuity_candidate", 1)[1].split("create or replace function public.commit_memory_digest_run", 1)[0]
        self.assertIn("insert into public.memory_requests", writer)
        self.assertNotIn("insert into public.memory_relations", writer)
        self.assertNotIn("insert into public.memory_relations", self.sql)

    def test_relation_types_and_direction_trigger_are_bounded(self):
        for value in ("part_of", "advances", "resolves", "dissolves", "origin_of", "evokes", "supports", "contradicts", "governed_by"):
            self.assertIn(f"'{value}'", self.sql)
        self.assertNotIn("'related_to'", self.sql)
        self.assertIn("memory_relation_invalid_direction", self.sql)
        self.assertIn("memory_relation_endpoint_not_active", self.sql)
        self.assertIn("new.relation_type='evokes' and v_from in ('moment','episode') and v_to='inside_joke'", self.sql)
        self.assertNotIn("new.relation_type='evokes' and v_from='inside_joke'", self.sql)

    def test_automatic_writes_reject_profile_and_interaction_rule(self):
        self.assertIn("memory_requests_automatic_type_check", self.sql)
        self.assertIn("v_type not in ('moment','thread','episode','inside_joke')", self.sql)

    def test_legacy_nulls_remain_allowed_but_new_writes_are_complete(self):
        self.assertIn("continuity_schema_version is null and continuity_data is null", self.sql)
        request_constraint = self.sql.split("memory_requests_continuity_v1_check", 1)[1].split("memory_requests_automatic_type_check", 1)[0]
        self.assertIn("continuity_schema_version = 1 and public.validate_continuity_data", request_constraint)
        self.assertNotIn("continuity_id is not null", request_constraint)
        self.assertIn("memory_request_unclassified_legacy", self.sql)

    def test_review_sync_preserves_non_null_values_when_request_fields_are_null(self):
        sync = self.sql.split("create or replace function public.sync_reviewed_memory_request_metadata", 1)[1]
        for field in ("subject", "source_type", "thread_state", "continuity_value", "retention_class", "participants"):
            self.assertIn(f"coalesce(new.{field},memory.{field})", sync)
        for field in ("evidence_message_ids", "source_time", "memory_time", "time_precision", "evidence_start_time", "evidence_end_time"):
            self.assertIn(f"coalesce(new.{field},memory.{field})", sync)

    def test_review_sync_only_replaces_complete_valid_continuity_data_for_same_identity(self):
        sync = self.sql.split("create or replace function public.sync_reviewed_memory_request_metadata", 1)[1]
        self.assertIn("v_existing_continuity_id is null or v_existing_continuity_id=new.continuity_id", sync)
        self.assertIn("new.continuity_data is not null", sync)
        self.assertIn("public.validate_continuity_data(new.continuity_type,new.thread_state,new.continuity_data)", sync)
        self.assertIn("continuity_data=case when v_replace_continuity", sync)
        self.assertIn("then new.continuity_data else memory.continuity_data end", sync)

    def test_plugin_request_source_is_preserved_until_review(self):
        create = self.sql.split("create or replace function public.create_memory_request_v4", 1)[1].split("create or replace function public.store_continuity_candidate", 1)[0]
        self.assertIn("'pending',p_source", create)
        self.assertIn("p_source not in ('orangechat_plugin','mcp_memory')", create)
        self.assertIn("continuity_type is distinct from 'interaction_rule' or source in ('orangechat_plugin','mcp_memory')", self.sql)
        self.assertIn("source=case when new.source='daily_digest' then 'daily_digest' else memory.source end", self.sql)

    def test_relation_proposals_are_absent_from_phase1(self):
        self.assertNotIn("proposed_relations", self.sql)
        self.assertNotIn("validate_proposed_relations", self.sql)

    def test_low_risk_direct_writer_and_high_risk_pending_boundary(self):
        direct = self.sql.split("create or replace function public.write_memory_direct_v1", 1)[1].split("create table public.memory_relations", 1)[0]
        self.assertIn("p_continuity_type not in ('moment','thread','inside_joke')", direct)
        self.assertIn("review_memory_request_v5", direct)
        writer = self.sql.split("create or replace function public.store_continuity_candidate", 1)[1].split("create or replace function public.commit_memory_digest_run", 1)[0]
        self.assertIn("v_type in ('moment','thread','inside_joke')", writer)
        self.assertIn("v_type not in ('moment','thread','episode','inside_joke')", writer)

    def test_pending_does_not_allocate_formal_identity(self):
        create = self.sql.split("create or replace function public.create_memory_request_v4", 1)[1].split("create or replace function public.store_continuity_candidate", 1)[0]
        self.assertIn("p_continuity_type,p_thread_state,null,1,p_continuity_data", create)
        self.assertNotIn("allocate_memory_continuity_id", create)

    def test_first_replace_creates_identity_instead_of_reporting_stale(self):
        allocator = self.sql.split("create or replace function public.allocate_memory_continuity_id", 1)[1].split("create or replace function public.create_memory_request_v4", 1)[0]
        missing = allocator.split("if v_memory_id is null then", 1)[1].split("end if;", 1)[0]
        self.assertIn("insert into public.memory_continuity_objects", missing)
        self.assertIn("return v_id", missing)
        self.assertNotIn("memory_request_stale_update", missing)

    def test_idempotency_and_content_dedupe_run_before_one_minute_rate_limit(self):
        create = self.sql.split("create or replace function public.create_memory_request_v4", 1)[1].split("create or replace function public.store_continuity_candidate", 1)[0]
        self.assertLess(create.index("idempotency_key=p_idempotency_key"), create.index("select count(*) into v_recent_count"))
        self.assertLess(create.index("content_hash=p_content_hash"), create.index("select count(*) into v_recent_count"))
        self.assertIn("now()-interval '1 minute'", create)

    def test_store_candidate_preserves_dedupe_and_evidence_guards(self):
        writer = self.sql.split("create or replace function public.store_continuity_candidate", 1)[1].split("create or replace function public.commit_memory_digest_run", 1)[0]
        for contract in (
            "status in ('pending','approved','merged','duplicate','conflict','rejected')",
            "memory_dedupe_text_similarity(content,v_content)>=.72",
            "content_hash=v_content_hash",
            "memory_key=v_key",
            "memory_dedupe_text_similarity(content,v_content)>=.86",
            "1-(embedding<=>v_embedding)>=.94",
            "is_active=true and verified='verified'",
            "v_requested_evidence_count not between 1 and 8",
            "cardinality(v_ids)<>v_requested_evidence_count",
        ):
            self.assertIn(contract, writer)
        self.assertIn("not (v_mode='replace' and v_key is not null and memory_key=v_key)", writer)

    def test_python_rpc_payload_names_match_current_signatures(self):
        source = (ROOT / "gateway" / "memory_requests.py").read_text(encoding="utf-8")
        # 六个通用元数据字段退役后，写入路径的当前签名以最新的前向 migration
        # 为准；subject/participants/continuity_value/retention_class 不再出现。
        create_expected = {
            "p_assistant_id", "p_conversation_id", "p_source_message_id", "p_content",
            "p_title", "p_tags", "p_importance", "p_reason", "p_content_hash",
            "p_idempotency_key", "p_rate_limit", "p_memory_key", "p_update_mode",
            "p_continuity_type", "p_thread_state", "p_continuity_schema_version",
            "p_continuity_data", "p_source_type", "p_source",
            "p_recall_scene", "p_recall_tags",
        }
        current = (
            ROOT / "supabase" / "migrations"
            / "20260831010000_retire_memory_metadata_fields.sql"
        ).read_text(encoding="utf-8")
        signature = current.split("create or replace function public.create_memory_request_v4(", 1)[1].split("returns jsonb", 1)[0]
        sql_names = set(re.findall(r"\b(p_[a-z_]+)\s+", signature))
        self.assertEqual(sql_names, create_expected)
        for name in create_expected:
            self.assertIn(f'"{name}"', source)
        for retired in ("p_subject", "p_continuity_value", "p_retention_class", "p_participants"):
            self.assertNotIn(f'"{retired}"', source)
        direct = current.split("create or replace function public.write_memory_direct_v1(", 1)[1].split("returns jsonb", 1)[0]
        self.assertEqual(
            set(re.findall(r"\b(p_[a-z_]+)\s+", direct)),
            create_expected | {"p_reviewed_by", "p_recall_embedding"},
        )

    def test_sql_validator_rejects_unknown_keys_and_fractional_integers_safely(self):
        self.assertIn("continuity_object_keys_ok", self.sql)
        self.assertIn("p_data is null or jsonb_typeof(p_data) <> 'object'", self.sql)
        self.assertIn("v_text !~ '^-?[0-9]+$'", self.sql)
        self.assertIn("exception when others then return false", self.sql)

    def test_recall_returns_new_structure_and_preserves_limits(self):
        for field in ("continuity_id uuid", "continuity_schema_version smallint", "continuity_data jsonb"):
            self.assertGreaterEqual(self.sql.count(field), 2)
        self.assertIn("limit least(greatest(coalesce(match_count,20),1),50)", self.sql)
        self.assertIn("where n<=5", self.sql)

    def test_new_tables_are_private_and_service_role_only(self):
        self.assertIn("enable row level security", self.sql)
        self.assertIn("revoke all on table public.memory_continuity_objects,public.memory_relations from anon,authenticated", self.sql)
        self.assertIn("to service_role", self.sql)


if __name__ == "__main__":
    unittest.main()
