"""Contract tests for the retirement of six generic memory metadata fields.

Retired and dropped from production:
    memories.layer / emotion_weight / subject / participants /
    continuity_value / retention_class
    memory_requests.subject / participants / continuity_value / retention_class

Retained and untouched:
    source_type, importance, continuity_type, continuity_data, recall_scene,
    recall_tags, recall_embedding. public.chat_messages stays select-only.
"""
import asyncio
import re
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from gateway.memory_continuity_schema import validate_continuity_data
from gateway.memory_search import (
    MAX_INJECTION_CHARS,
    MEMORY_CONTEXT_HEADER,
    _hybrid_rank,
    _select_memories_for_injection,
    format_memories_for_injection,
)
from gateway.memory_continuity_shadow import SHADOW_SYSTEM_PROMPT


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (
    ROOT / "supabase" / "migrations"
    / "20260831010000_retire_memory_metadata_fields.sql"
)

RETIRED_MEMORY_COLUMNS = (
    "layer", "emotion_weight", "subject", "participants",
    "continuity_value", "retention_class",
)
RETIRED_REQUEST_COLUMNS = (
    "subject", "participants", "continuity_value", "retention_class",
)
RETIRED_CONTINUITY_GENERIC = (
    "subject", "participants", "continuity_value", "retention_class",
)
RETAINED_FIELDS = (
    "source_type", "importance", "continuity_type", "continuity_data",
    "recall_scene", "recall_tags", "recall_embedding",
)

REBUILT_FUNCTIONS = (
    "match_memories",
    "search_memories_by_keywords",
    "run_memory_heat_decay",
    "review_memory_request_v2",
    "review_memory_request_v3",
    "create_memory_request_v4",
    "write_memory_direct_v1",
    "store_continuity_candidate",
    "sync_reviewed_memory_request_metadata",
)

RUNTIME_MODULES = (
    "gateway/memory_requests.py",
    "gateway/memory_review.py",
    "gateway/memory_extract.py",
    "gateway/memory_continuity_shadow.py",
    "gateway/memory_search.py",
    "gateway/memory_mcp.py",
    "gateway/admin_api.py",
)


def squeezed(text: str) -> str:
    return re.sub(r"\s+", "", text)


def executable_sql(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text)


class RetirementMigrationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = MIGRATION.read_text(encoding="utf-8")
        cls.executable = executable_sql(cls.sql)
        cls.flat = squeezed(cls.executable)

    def test_migration_is_forward_transactional_and_cascade_free(self):
        self.assertTrue(self.executable.lstrip().startswith("begin;"))
        self.assertTrue(self.executable.rstrip().endswith("commit;"))
        self.assertNotIn("cascade", self.executable)

    def test_migration_never_touches_chat_messages(self):
        self.assertNotRegex(
            self.executable,
            r"(?:alter\s+table|insert\s+into|update|delete\s+from|truncate)"
            r"[\s\S]{0,80}public\.chat_messages",
        )

    def test_all_retired_columns_are_dropped(self):
        memories_section = self.executable.split(
            "alter table public.memories", 1
        )[1].split("alter table public.memory_requests", 1)[0]
        requests_section = self.executable.split(
            "alter table public.memory_requests", 1
        )[1]
        for column in RETIRED_MEMORY_COLUMNS:
            with self.subTest(table="memories", column=column):
                self.assertRegex(
                    memories_section, rf"drop column {column}[,;]"
                )
        for column in RETIRED_REQUEST_COLUMNS:
            with self.subTest(table="memory_requests", column=column):
                self.assertRegex(
                    requests_section, rf"drop column {column}[,;]"
                )
        # No replacement column is created for any retired field.
        self.assertNotIn("add column", self.executable)

    def test_retained_fields_are_never_dropped(self):
        for field in RETAINED_FIELDS:
            with self.subTest(field=field):
                self.assertNotRegex(
                    self.executable, rf"drop\s+column\s+{field}\b"
                )

    def test_dependent_functions_are_rebuilt_before_the_drop(self):
        for function in REBUILT_FUNCTIONS:
            with self.subTest(function=function):
                self.assertRegex(
                    self.executable,
                    rf"create(?: or replace)? function public\.{function}\b",
                )
        # Every function definition precedes the first column drop.
        drop_index = self.executable.index("drop column layer,")
        for match in re.finditer(r"create (?:or replace )?function", self.executable):
            self.assertLess(match.start(), drop_index)

    def test_old_rpc_signatures_are_dropped_exactly_without_cascade(self):
        for signature in (
            "match_memories(extensions.vector, double precision, integer)",
            "search_memories_by_keywords(text[], integer)",
            "create_memory_request_v4(\n    text, text, bigint, text, text, text[], integer, text, text, text,\n"
            "    integer, text, text, text, text, smallint, jsonb, text, text, integer,\n"
            "    text, text[], text, text, text[]\n)",
            "write_memory_direct_v1(\n    text, text, bigint, text, text, text[], integer, text, text, text,\n"
            "    integer, text, text, text, text, smallint, jsonb, text, text, integer,\n"
            "    text, text[], text, text, text[], extensions.vector\n)",
        ):
            with self.subTest(signature=squeezed(signature)[:60]):
                self.assertIn(
                    f"dropfunctionifexistspublic.{squeezed(signature)};",
                    self.flat,
                )

    def test_rebuilt_recall_functions_no_longer_return_retired_fields(self):
        for function, retained in (
            ("match_memories", ("source_type", "continuity_type", "continuity_data",
                                "recall_scene", "recall_tags", "evidence_time_precision")),
            ("search_memories_by_keywords", ("source_type", "continuity_type",
                                             "continuity_data", "evidence_time_precision")),
        ):
            section = self.executable.split(
                f"create function public.{function}(", 1
            )[1]
            section = section.split("$function$;", 1)[0]
            with self.subTest(function=function):
                for column in RETIRED_MEMORY_COLUMNS:
                    self.assertNotRegex(section, rf"\b{column}\b")
                for field in retained:
                    self.assertIn(field, section)
        keyword = self.executable.split(
            "create function public.search_memories_by_keywords(", 1
        )[1].split("$function$;", 1)[0]
        self.assertIn("memory.is_active = true", keyword)
        self.assertIn("memory.verified = 'verified'", keyword)

    def test_rebuilt_write_path_has_no_retired_parameters_or_inserts(self):
        create = self.executable.split(
            "create or replace function public.create_memory_request_v4(", 1
        )[1].split("create or replace function public.write_memory_direct_v1", 1)[0]
        direct = self.executable.split(
            "create or replace function public.write_memory_direct_v1(", 1
        )[1].split("create or replace function public.store_continuity_candidate", 1)[0]
        store = self.executable.split(
            "create or replace function public.store_continuity_candidate(", 1
        )[1].split("create or replace function public.sync_reviewed_memory_request_metadata", 1)[0]
        for name, section in (
            ("create_memory_request_v4", create),
            ("write_memory_direct_v1", direct),
            ("store_continuity_candidate", store),
        ):
            with self.subTest(function=name):
                for column in RETIRED_REQUEST_COLUMNS:
                    self.assertNotIn(f"p_{column}", section)
                    self.assertNotRegex(section, rf"\b{column}\b")
        # The evidence-time pipeline and recall fields stay intact.
        self.assertIn("v_evidence_precision := case when v_evidence_time is null then null else 'minute' end", create)
        self.assertIn("recall_scene,recall_tags", create)
        self.assertIn("v_recall_scene,v_recall_tags", store)

    def test_rebuilt_metadata_trigger_no_longer_copies_retired_fields(self):
        trigger = self.executable.split(
            "create or replace function public.sync_reviewed_memory_request_metadata", 1
        )[1].split("$function$;", 1)[0]
        for column in RETIRED_REQUEST_COLUMNS:
            with self.subTest(column=column):
                self.assertNotIn(f"{column} =", trigger)
                self.assertNotIn(f"new.{column}", trigger)
                self.assertNotIn(f"memory.{column}", trigger)
        self.assertIn("recall_scene = coalesce(new.recall_scene,memory.recall_scene)", trigger)
        self.assertIn("recall_tags = coalesce(new.recall_tags,memory.recall_tags)", trigger)

    def test_rebuilt_heat_decay_has_no_retired_field_logic(self):
        decay = self.executable.split(
            "create or replace function public.run_memory_heat_decay(", 1
        )[1].split("$function$;", 1)[0]
        for token in ("emotion_weight", "layer", "碎片", "核心"):
            with self.subTest(token=token):
                self.assertNotIn(token, decay)
        self.assertIn("memory.importance <= 3", decay)
        self.assertIn("candidate.new_heat < 5.0", decay)
        self.assertIn("interval '30 days'", decay)
        self.assertIn("memory.is_active = true", decay)

    def test_rebuilt_review_functions_preserve_supersession_rules(self):
        v2 = self.executable.split(
            "create or replace function public.review_memory_request_v2(", 1
        )[1].split("create or replace function public.review_memory_request_v3", 1)[0]
        v3 = self.executable.split(
            "create or replace function public.review_memory_request_v3(", 1
        )[1].split("drop function if exists public.create_memory_request_v4", 1)[0]
        for name, section in (("v2", v2), ("v3", v3)):
            with self.subTest(function=name):
                self.assertNotIn("emotion_weight", section)
                self.assertNotIn("layer", section)
        self.assertIn("on conflict (content_hash) do update set", v2)
        self.assertIn("memory_request_merge_unchanged", v3)

    def test_rebuilt_functions_keep_service_role_only_grants(self):
        for signature in (
            "match_memories(extensions.vector,doubleprecision,integer)",
            "search_memories_by_keywords(text[],integer)",
            "create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text,text[])",
            "write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text,text,text[],extensions.vector)",
        ):
            with self.subTest(signature=signature[:48]):
                self.assertIn(
                    f"revokeallonfunctionpublic.{signature}frompublic,anon,authenticated;",
                    self.flat,
                )
                self.assertIn(
                    f"grantexecuteonfunctionpublic.{signature}toservice_role;",
                    self.flat,
                )


class RuntimeRetirementTests(unittest.TestCase):
    def test_runtime_python_no_longer_references_retired_fields(self):
        for module in RUNTIME_MODULES:
            source = (ROOT / module).read_text(encoding="utf-8")
            for token in ("emotion_weight", "continuity_value", "retention_class"):
                with self.subTest(module=module, token=token):
                    self.assertNotIn(token, source)
            for token in RETIRED_CONTINUITY_GENERIC:
                with self.subTest(module=module, token=token):
                    self.assertNotRegex(source, rf"\b{token}\b")

    def test_mcp_public_parameters_exclude_retired_continuity_generic_fields(self):
        from gateway.memory_mcp import memory_mcp

        tools = {tool.name: tool for tool in asyncio.run(memory_mcp.list_tools())}
        for name, tool in tools.items():
            properties = tool.input_schema.get("properties", {})
            for token in RETIRED_CONTINUITY_GENERIC:
                with self.subTest(tool=name, token=token):
                    self.assertNotIn(token, properties)

    def test_orangechat_plugin_manifest_excludes_retired_parameters(self):
        manifest = (
            ROOT / "orangechat_plugins" / "memory-request" / "manifest.json"
        ).read_text(encoding="utf-8")
        for token in RETIRED_CONTINUITY_GENERIC:
            with self.subTest(token=token):
                self.assertNotRegex(manifest, rf'"name":\s*"{token}"')

    def test_orangechat_plugin_payload_excludes_retired_fields(self):
        source = (
            ROOT / "orangechat_plugins" / "memory-request" / "main.js"
        ).read_text(encoding="utf-8")
        for token in RETIRED_CONTINUITY_GENERIC:
            with self.subTest(token=token):
                self.assertNotRegex(source, rf"{token}:")

    def test_continuity_prompt_and_output_schema_exclude_retired_fields(self):
        for token in RETIRED_CONTINUITY_GENERIC:
            with self.subTest(token=token):
                self.assertNotRegex(SHADOW_SYSTEM_PROMPT, rf"\b{token}\b")
        extract = (ROOT / "gateway" / "memory_extract.py").read_text(encoding="utf-8")
        self.assertIn("EXTRACT_SYSTEM_PROMPT", extract)
        for token in RETIRED_CONTINUITY_GENERIC:
            with self.subTest(token=token):
                self.assertNotRegex(extract, rf'"{token}"')

    def test_admin_api_whitelists_exclude_retired_fields_and_keep_retained(self):
        from gateway.admin_api import _TABLES

        for table, retired in (
            ("memories", RETIRED_MEMORY_COLUMNS),
            ("memory_requests", RETIRED_REQUEST_COLUMNS),
        ):
            read_fields = _TABLES[table]["read"]
            for token in retired:
                with self.subTest(table=table, token=token):
                    self.assertNotIn(token, read_fields)
        for field in ("source_type", "importance", "continuity_type", "continuity_data"):
            with self.subTest(field=field):
                self.assertIn(field, _TABLES["memories"]["read"])
                self.assertIn(field, _TABLES["memory_requests"]["read"])
        for token in ("layer", "emotion_weight"):
            with self.subTest(write_token=token):
                self.assertNotIn(token, _TABLES["memories"]["write"])

    def test_frontend_no_longer_queries_displays_or_edits_retired_fields(self):
        browser = (
            ROOT / "admin" / "js" / "pages" / "_memory_browser.js"
        ).read_text(encoding="utf-8")
        digest = (ROOT / "admin" / "js" / "pages" / "digest.js").read_text(encoding="utf-8")
        index_html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")

        # 查询字段白名单不再包含退役字段。
        memory_fields = re.search(
            r"const MEMORY_FIELDS = '([^']+)'", browser
        ).group(1).split(",")
        for token in RETIRED_MEMORY_COLUMNS:
            with self.subTest(list="MEMORY_FIELDS", token=token):
                self.assertNotIn(token, memory_fields)
        self.assertIn("source_type", memory_fields)
        self.assertIn("continuity_data", memory_fields)
        self.assertIn("recall_scene", memory_fields)

        # 详情展示、卡片、编辑表单不再读取或写入退役字段。
        for name, source in (("_memory_browser.js", browser), ("digest.js", digest)):
            for token in RETIRED_MEMORY_COLUMNS:
                for accessor in ("m.", "r.", "candidate.", "memory."):
                    with self.subTest(file=name, accessor=accessor, token=token):
                        self.assertNotIn(f"{accessor}{token}", source)
        for edit_id in ("#ed-layer", "#ed-emo"):
            with self.subTest(edit_id=edit_id):
                self.assertNotIn(edit_id, browser)
        self.assertNotRegex(index_html, r"\blayer\b|\bemotion_weight\b")

    def test_frontend_asset_version_is_refreshed(self):
        index_html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "admin" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("20260830-retro1", index_html)
        self.assertNotIn("20260830-retro1", app_js)
        self.assertIn("20260831-retire1", index_html)
        self.assertIn("20260831-retire1", app_js)


class ContinuitySchemaStillValidTests(unittest.TestCase):
    def test_all_six_continuity_types_still_validate_after_retirement(self):
        payloads = {
            "moment": (None, {"scene": "窗口", "event": "确认", "moment_state": "standalone"}),
            "thread": ("open", {"open_question": "下一步", "current_state": "等待",
                                "closure_criteria": [], "abstract_retrieval_hints": [],
                                "concrete_retrieval_hints": []}),
            "episode": (None, {"beginning": "开始", "development": "推进", "outcome": "结束",
                               "closure_quality": "complete"}),
            "inside_joke": (None, {"origin": "口误", "trigger_phrases": ["小橘子"],
                                   "shared_meaning": "共同玩笑", "usage_context": [],
                                   "avoid_context": [], "reinforcement_count": 0}),
            "profile": (None, {"facet": "偏好", "statement": "喜欢安静清晨", "scope": "日常",
                               "stability": "stable", "exceptions": [],
                               "basis": "explicit_preference"}),
            "interaction_rule": (None, {"trigger": "明确求助", "expected_behavior": "先给结论",
                                        "forbidden_behavior": [], "scope": "对话",
                                        "priority": 8, "rule_state": "active", "exceptions": [],
                                        "explicit_instruction": "用户明确要求以后先给结论"}),
        }
        for kind, (state, data) in payloads.items():
            with self.subTest(kind=kind):
                self.assertIsInstance(validate_continuity_data(kind, state, data), dict)


class RecallWithoutRetiredFieldsTests(unittest.TestCase):
    NOW = None

    def _memory(self, memory_id, content, **extra):
        return {"id": memory_id, "content": content, "tags": [], "heat": 50,
                "importance": 5, **extra}

    def test_structurally_complete_memories_still_rank_and_inject(self):
        # 旧结构完整记忆：带 evidence/recall 元数据、不带六个退役字段。
        memory = self._memory(
            1,
            "叶子和栖约好继续做网关。",
            title="网关计划",
            created_at="2026-08-02T00:00:00+00:00",
            continuity_type="thread",
            thread_state="open",
            source_type="natural_chat",
            evidence_end_time="2026-08-01T08:05:00+08:00",
            evidence_time_precision="minute",
        )

        ranked = _hybrid_rank([memory], [], ["网关"], 5)
        selected = _select_memories_for_injection(ranked, 5)
        rendered = format_memories_for_injection(selected)

        self.assertEqual([item["id"] for item in selected], [1])
        self.assertIn("时间：2026-08-01 08:05｜叶子和栖约好继续做网关。", rendered)

    def test_unclassified_and_incomplete_rows_never_break_reading(self):
        # 3 条无 continuity_type + 13 条结构不完整的旧记忆形状：字段缺失也不报错。
        rows = [
            self._memory(1, "没有类型的旧记忆。"),
            self._memory(2, "只有类型的旧记忆。", continuity_type="moment"),
            self._memory(3, "有空来源类型的旧记忆。", source_type=None),
        ]

        ranked = _hybrid_rank(rows, [], ["旧记忆"], 5)
        selected = _select_memories_for_injection(ranked, 5)
        rendered = format_memories_for_injection(selected)

        self.assertEqual(len(selected), 3)
        self.assertIn("没有类型的旧记忆。", rendered)

    def test_ranking_ignores_legacy_values_of_retired_fields(self):
        base = self._memory(1, "网关记忆", created_at="2026-08-02T00:00:00+00:00")
        with_legacy = dict(base, layer="核心", continuity_value=10, retention_class="core")

        plain = _hybrid_rank([dict(base)], [], ["网关"], 1)
        legacy = _hybrid_rank([with_legacy], [], ["网关"], 1)

        self.assertAlmostEqual(
            plain[0]["_retrieval_score"], legacy[0]["_retrieval_score"]
        )

    def test_injection_respects_top_k_and_global_char_budget(self):
        ranked = [
            self._memory(i, "很长的记忆内容" * 200, _retrieval_score=0.95)
            for i in range(1, 11)
        ]

        selected = _select_memories_for_injection(ranked, 4)
        rendered = format_memories_for_injection(selected)

        self.assertLessEqual(len(selected), 4)
        self.assertLessEqual(len(rendered), MAX_INJECTION_CHARS)
        self.assertTrue(rendered.startswith(MEMORY_CONTEXT_HEADER))

    def test_injection_output_contains_no_layer_labels(self):
        ranked = [
            self._memory(1, "旧层级数据不应出现在注入里。", _retrieval_score=0.9),
        ]

        rendered = format_memories_for_injection(_select_memories_for_injection(ranked, 5))

        for label in ("碎片", "场景", "核心", "·线索"):
            with self.subTest(label=label):
                self.assertNotIn(label, rendered)

    def test_both_recall_channels_keep_their_rpc_names(self):
        from gateway.memory_search import _keyword_search, _vector_search_sync
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        client = MagicMock()

        def rpc(name, args):
            return SimpleNamespace(execute=lambda: SimpleNamespace(data=[]))

        client.rpc.side_effect = rpc
        with patch("gateway.memory_search.get_client", return_value=client):
            _keyword_search(["清晨"], 20)
            self.assertEqual(client.rpc.call_args.args[0], "search_memories_by_keywords")
            _vector_search_sync([0.1, 0.2], 20)
            self.assertEqual(client.rpc.call_args.args[0], "match_memories")
            self.assertEqual(
                client.rpc.call_args.args[1]["query_embedding"], [0.1, 0.2]
            )


if __name__ == "__main__":
    unittest.main()
