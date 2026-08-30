import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20260830010000_memory_recall_scene.sql"

OLD_CREATE_V4 = (
    "create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,"
    "integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text)"
)
OLD_WRITE_V1 = (
    "write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,"
    "integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text)"
)
OLD_REVIEW_V5 = (
    "review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,"
    "text,integer)"
)
NEW_CREATE_V4 = OLD_CREATE_V4[:-1] + ",text,text[])"
NEW_WRITE_V1 = OLD_WRITE_V1[:-1] + ",text,text[],extensions.vector)"
NEW_REVIEW_V5 = OLD_REVIEW_V5[:-1] + ",extensions.vector)"


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def function_body(section: str) -> str:
    return section.split("as $function$", 1)[1].split("$function$;", 1)[0]


class RecallSceneMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8").lower()
        cls.executable = re.sub(r"--[^\n]*", "", cls.sql)
        cls.flat = squeezed(cls.executable)

    def test_is_forward_only_and_transactional(self):
        self.assertTrue(self.executable.lstrip().startswith("begin;"))
        self.assertTrue(self.executable.rstrip().endswith("commit;"))
        self.assertNotIn("cascade", self.executable)

    def test_adds_recall_columns_without_backfilling_history(self):
        self.assertIn(
            "alter table public.memory_requests\n"
            "    add column if not exists recall_scene text,\n"
            "    add column if not exists recall_tags text[],\n"
            "    add column if not exists evidence_time_precision text;",
            self.sql,
        )
        self.assertIn(
            "alter table public.memories\n"
            "    add column if not exists recall_scene text,\n"
            "    add column if not exists recall_tags text[],\n"
            "    add column if not exists evidence_time_precision text,\n"
            "    add column if not exists recall_embedding extensions.vector;",
            self.sql,
        )
        for forbidden in (
            "update public.memories set recall_scene",
            "update public.memories set recall_tags",
            "update public.memories set recall_embedding",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.executable)

    def test_recall_embedding_requires_a_non_blank_scene(self):
        self.assertIn("memories_recall_embedding_scene_check", self.sql)
        self.assertIn("recall_embedding is null", self.sql)

    def test_time_precision_checks_allow_hour_on_both_tables(self):
        for table in ("memory_requests", "memories"):
            with self.subTest(table=table):
                self.assertIn(
                    f"alter table public.{table}\n"
                    "    drop constraint if exists "
                    f"{table}_time_precision_values;",
                    self.sql,
                )
                self.assertIn(
                    f"alter table public.{table}\n"
                    f"    add constraint {table}_time_precision_values\n"
                    "        check (time_precision in ('minute', 'hour', 'day', 'approximate', 'unknown'));",
                    self.sql,
                )
        # The widened enum is additive; nothing rewrites historical rows.
        self.assertNotIn("update public.memories set time_precision", self.executable)

    def test_evidence_time_precision_is_a_separate_stored_field(self):
        for table in ("memory_requests", "memories"):
            with self.subTest(table=table):
                self.assertIn(
                    f"alter table public.{table}\n"
                    "    drop constraint if exists "
                    f"{table}_evidence_time_precision_values;",
                    self.sql,
                )
                self.assertIn(
                    f"alter table public.{table}\n"
                    f"    add constraint {table}_evidence_time_precision_values\n"
                    "        check (evidence_time_precision in ('minute', 'hour', 'day', 'approximate', 'unknown'));",
                    self.sql,
                )
        # The evidence precision is stored independently; no historical rows
        # are backfilled and no precision is guessed from existing columns.
        self.assertNotIn("update public.memories set evidence_time_precision", self.executable)
        self.assertNotRegex(
            self.executable,
            r"evidence_time_precision[\s\S]{0,120}coalesce\(new\.time_precision",
        )

    def test_vector_channel_returns_evidence_precision(self):
        vector = function_body(
            self.executable.split("create function public.match_memories", 1)[1]
        )
        self.assertIn("memory.evidence_time_precision", vector)

    def test_direct_writer_records_minute_precision_for_message_evidence(self):
        create = self.executable.split("create or replace function public.create_memory_request_v4", 1)[1]
        section = create.split("create or replace function public.write_memory_direct_v1", 1)[0]
        body = function_body(section)
        self.assertIn("v_evidence_precision := case when v_evidence_time is null then null else 'minute' end", body)
        self.assertIn("v_evidence_time,v_evidence_time,v_evidence_time,v_evidence_precision", body)

    def test_continuity_writer_stages_evidence_precision_from_candidates(self):
        writer = self.executable.split("create or replace function public.store_continuity_candidate", 1)[1]
        body = function_body(writer.split("create or replace function public.commit_memory_digest_run", 1)[0])
        self.assertIn(
            "when p_item->>'evidence_time_precision' in ('minute','hour','day','approximate','unknown')",
            body,
        )
        self.assertIn("v_evidence_precision", body)

    def test_metadata_trigger_copies_evidence_precision(self):
        trigger = function_body(
            self.executable.split("create or replace function public.sync_reviewed_memory_request_metadata", 1)[1]
        )
        self.assertIn(
            "evidence_time_precision = coalesce(new.evidence_time_precision,memory.evidence_time_precision)",
            trigger,
        )

    def test_chat_messages_remains_select_only(self):
        self.assertIn("from public.chat_messages", self.executable)
        self.assertNotRegex(
            self.executable,
            r"(?:alter\s+table|insert\s+into|update|delete\s+from|truncate)[\s\S]{0,80}public\.chat_messages",
        )

    def test_vector_channel_rebuilds_on_recall_embedding(self):
        for old_signature in (
            "match_memories(extensions.vector, double precision, integer)",
        ):
            with self.subTest(signature=old_signature):
                self.assertIn(
                    f"dropfunctionifexistspublic.{squeezed(old_signature)};",
                    self.flat,
                )
        vector = function_body(
            self.executable.split("create function public.match_memories", 1)[1]
        )
        self.assertIn("memory.recall_embedding <=> query_embedding", vector)
        self.assertIn("memory.recall_embedding is not null", vector)
        self.assertNotIn("memory.embedding", vector)
        self.assertIn("memory.is_active = true", vector)
        self.assertIn("memory.verified = 'verified'", vector)
        for field in (
            "recall_scene", "recall_tags", "evidence_end_time",
            "evidence_start_time", "source_time", "time_precision",
            "continuity_id", "continuity_type", "continuity_data", "similarity",
        ):
            with self.subTest(field=field):
                self.assertIn(field, vector)

    def test_old_rpc_signatures_are_dropped_exactly_before_rebuild(self):
        for old_signature in (OLD_CREATE_V4, OLD_WRITE_V1, OLD_REVIEW_V5):
            with self.subTest(signature=old_signature.split("(")[0]):
                self.assertIn(
                    f"dropfunctionifexistspublic.{squeezed(old_signature)};",
                    self.flat,
                )

    def test_request_writer_persists_recall_fields_and_evidence_time(self):
        create = self.executable.split("create or replace function public.create_memory_request_v4", 1)[1]
        section = create.split("create or replace function public.write_memory_direct_v1", 1)[0]
        body = function_body(section)
        self.assertIn("p_recall_scene text, p_recall_tags text[]", section)
        self.assertIn("v_recall_scene,v_recall_tags,v_evidence_time,v_evidence_time,v_evidence_time", body)
        # The event time is the cited source message's own clock; a missing or
        # mismatched message leaves it null instead of guessing.
        self.assertIn("message.assistant_id = p_assistant_id", body)
        self.assertIn("message.conversation_id = nullif(trim(coalesce(p_conversation_id,'')),'')", body)
        self.assertIn("at time zone 'asia/shanghai'", body)

    def test_direct_writer_threads_recall_scene_tags_and_embedding(self):
        direct = self.executable.split("create or replace function public.write_memory_direct_v1", 1)[1]
        section = direct.split("create or replace function public.review_memory_request_v5", 1)[0]
        body = function_body(section)
        self.assertIn("p_recall_scene text, p_recall_tags text[],", section)
        self.assertIn("p_recall_embedding extensions.vector", section)
        self.assertIn("p_participants,p_source,p_recall_scene,p_recall_tags", body)

    def test_review_wrapper_applies_embedding_after_metadata_copy(self):
        review = self.executable.split("create or replace function public.review_memory_request_v5", 1)[1]
        section = review.split("create or replace function public.store_continuity_candidate", 1)[0]
        body = function_body(section)
        self.assertIn("p_recall_embedding extensions.vector default null", section)
        self.assertIn("update public.memories\n            set recall_embedding = p_recall_embedding", body)

    def test_continuity_writer_stages_recall_fields_and_blocks_blank_scene_embedding(self):
        writer = self.executable.split("create or replace function public.store_continuity_candidate", 1)[1]
        writer = function_body(writer.split("create or replace function public.commit_memory_digest_run", 1)[0])
        self.assertIn("v_recall_scene := nullif(btrim(coalesce(p_item->>'recall_scene','')),'')", writer)
        self.assertIn("v_recall_scene,v_recall_tags", writer)
        self.assertIn("when v_recall_scene is null then null", writer)
        self.assertIn("v_recall_embedding", writer)

    def test_commit_runs_strip_recall_embedding_from_previews(self):
        for name in ("commit_memory_digest_run", "commit_memory_continuity_run"):
            body = function_body(
                self.executable.split(f"create or replace function public.{name}", 1)[1]
            )
            with self.subTest(function=name):
                self.assertIn("- 'embedding' - 'content_hash' - 'recall_embedding'", body)

    def test_metadata_trigger_copies_recall_fields_from_request_to_memory(self):
        trigger = function_body(
            self.executable.split("create or replace function public.sync_reviewed_memory_request_metadata", 1)[1]
        )
        self.assertIn("recall_scene = coalesce(new.recall_scene,memory.recall_scene)", trigger)
        self.assertIn("recall_tags = coalesce(new.recall_tags,memory.recall_tags)", trigger)

    def test_keyword_channel_is_left_untouched(self):
        self.assertNotIn("search_memories_by_keywords", self.executable)

    def test_recall_entry_points_stay_service_role_only(self):
        for signature in (NEW_CREATE_V4, NEW_WRITE_V1, NEW_REVIEW_V5):
            with self.subTest(signature=signature.split("(")[0]):
                self.assertIn(
                    f"revokeallonfunctionpublic.{squeezed(signature)}frompublic,anon,authenticated;",
                    self.flat,
                )
                self.assertIn(
                    f"grantexecuteonfunctionpublic.{squeezed(signature)}toservice_role;",
                    self.flat,
                )
        for signature in (
            "store_continuity_candidate(public.memory_digest_runs,jsonb)",
            "match_memories(extensions.vector, double precision, integer)",
        ):
            with self.subTest(signature=signature.split("(")[0]):
                self.assertIn(
                    f"revokeallonfunctionpublic.{squeezed(signature)}frompublic,anon,authenticated;",
                    self.flat,
                )
                self.assertIn(
                    f"grantexecuteonfunctionpublic.{squeezed(signature)}toservice_role;",
                    self.flat,
                )
        for statement in (
            "revokeallonfunctionpublic.commit_memory_digest_run(bigint,jsonb),"
            "public.commit_memory_continuity_run(bigint,jsonb)frompublic,anon,authenticated;",
            "grantexecuteonfunctionpublic.commit_memory_digest_run(bigint,jsonb),"
            "public.commit_memory_continuity_run(bigint,jsonb)toservice_role;",
        ):
            with self.subTest(statement=statement[:48]):
                self.assertIn(statement, self.flat)


if __name__ == "__main__":
    unittest.main()
