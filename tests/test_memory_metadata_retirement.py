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
import json
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
            "    text, text[], text, text, text, text[], extensions.vector\n)",
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

    def test_source_type_is_optional_and_null_safe_in_sql(self):
        create = self.executable.split(
            "create or replace function public.create_memory_request_v4(", 1
        )[1].split("create or replace function public.write_memory_direct_v1", 1)[0]
        store = self.executable.split(
            "create or replace function public.store_continuity_candidate(", 1
        )[1].split("create or replace function public.sync_reviewed_memory_request_metadata", 1)[0]
        # 空串统一为 NULL，绝不补 natural_chat / unknown。
        self.assertIn("nullif(btrim(coalesce(p_source_type,'')),'')", create)
        self.assertIn("nullif(btrim(coalesce(p_item->>'source_type','')),'')", store)
        for section in (create, store):
            self.assertNotIn("'natural_chat'", section)
            # 'unknown' 允许出现在 time_precision 等无关位置，但 source_type
            # 的取值表达式不得回落到任何字面量默认值。
            self.assertNotRegex(
                section,
                r"coalesce\([^()]*source_type[^()]*,\s*'(?:natural_chat|unknown)'\)",
            )
        # 不为 source_type 新增默认值、NOT NULL 或任何 DDL 改动。
        self.assertNotRegex(self.executable, r"alter\s+table[^;]*\bsource_type\b")
        self.assertNotIn("default 'natural_chat'", self.executable)
        self.assertNotIn("default 'unknown'", self.executable)

    def test_metadata_trigger_preserves_existing_source_type(self):
        trigger = self.executable.split(
            "create or replace function public.sync_reviewed_memory_request_metadata", 1
        )[1].split("$function$;", 1)[0]
        # 新申请 NULL → 新记忆保持 NULL；合法值正常复制；空申请值不清空
        # 目标记忆已有来源类型（coalesce 回落的是记忆自身的值）。
        self.assertIn("source_type = case when v_replace_continuity", trigger)
        self.assertIn("then coalesce(new.source_type,memory.source_type)", trigger)
        self.assertIn("else memory.source_type end", trigger)

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

    def test_rebuilt_heat_decay_only_decays_heat_and_never_archives(self):
        decay = self.executable.split(
            "create or replace function public.run_memory_heat_decay(", 1
        )[1].split("$function$;", 1)[0]
        for token in ("emotion_weight", "layer", "碎片", "核心"):
            with self.subTest(token=token):
                self.assertNotIn(token, decay)
        # 只衰减热度：候选筛选只读 is_active，不出现任何 is_active 写入或替代归档规则。
        self.assertNotIn("is_active = false", decay)
        self.assertNotRegex(decay, r"\bset\s+is_active\b")
        self.assertNotIn("importance <= 3", decay)
        self.assertNotIn("interval '30 days'", decay)
        # importance 仍参与衰减速度。
        self.assertIn("coalesce(memory.importance, 5)", decay)
        self.assertIn("power(", decay)
        # 输出与运行记录中的 archived_count 固定为 0。
        self.assertEqual(decay.count("'archived_count', 0"), 2)
        self.assertIn("archived_count = 0,", decay)
        self.assertIn("memory.is_active = true", decay)
        self.assertIn("memory.verified = 'verified'", decay)
        self.assertIn("abs(candidate.new_heat - memory.heat) >= 0.005", decay)
        # 与 digest/continuity 提交等内部维护 RPC 一样以 security definer 执行：
        # memories 的 CHECK 约束用调用者权限求值 validate_continuity_data，
        # 而 service_role 对它没有 EXECUTE。
        self.assertIn("security definer", decay)

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
            "create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text[])",
            "write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text,text[],extensions.vector)",
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

    def test_grant_signatures_match_created_function_signatures(self):
        # 权限语句的类型列表必须与本次 CREATE 的参数类型逐一吻合：
        # REVOKE 指向不存在的签名会在真实库上直接报错并回滚整个迁移。
        created = {}
        for match in re.finditer(
            r"create(?:\s+or\s+replace)?\s+function\s+public\.(\w+)\s*\(([\s\S]*?)\)\s*returns",
            self.executable,
        ):
            types = []
            for part in match.group(2).split(","):
                tokens = part.split()
                if not tokens:
                    continue
                if "default" in tokens:
                    tokens = tokens[: tokens.index("default")]
                types.append(squeezed(" ".join(tokens[1:])))
            created[match.group(1)] = ",".join(types)
        self.assertIn("create_memory_request_v4", created)
        self.assertEqual(created["create_memory_request_v4"].count(",") + 1, 21)
        self.assertIn("write_memory_direct_v1", created)
        self.assertEqual(created["write_memory_direct_v1"].count(",") + 1, 23)

        statements = re.findall(
            r"(?:revoke\s+all|grant\s+execute)\s+on\s+function\s+"
            r"public\.(\w+)\s*\(([^)]*)\)",
            self.executable,
        )
        self.assertGreaterEqual(len(statements), 6)
        for name, arg_types in statements:
            if name not in created:
                continue
            with self.subTest(function=name):
                self.assertEqual(
                    squeezed(arg_types),
                    created[name],
                    f"grant/revoke signature for {name} must match its CREATE parameters",
                )

    def test_dropped_signatures_match_latest_historical_creates(self):
        # DROP IF EXISTS 指向不存在的签名只会静默跳过，旧函数残留成重载。
        # 因此被 DROP 的签名必须与历史迁移中最后一次 CREATE 的参数类型一致。
        history = [
            path for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
            if path.name < MIGRATION.name
        ]
        self.assertGreaterEqual(len(history), 10)
        for function in (
            "match_memories",
            "search_memories_by_keywords",
            "create_memory_request_v4",
            "write_memory_direct_v1",
        ):
            latest = None
            for path in history:
                text = executable_sql(path.read_text(encoding="utf-8"))
                for match in re.finditer(
                    rf"create(?:\s+or\s+replace)?\s+function\s+public\.{function}\s*\(([\s\S]*?)\)\s*returns",
                    text,
                ):
                    latest = match.group(1)
            self.assertIsNotNone(latest, function)
            types = []
            for part in latest.split(","):
                tokens = part.split()
                if not tokens:
                    continue
                if "default" in tokens:
                    tokens = tokens[: tokens.index("default")]
                types.append(squeezed(" ".join(tokens[1:])))
            expected = f"{function}({','.join(types)})"
            with self.subTest(function=function):
                self.assertIn(
                    f"dropfunctionifexistspublic.{expected};",
                    self.flat,
                    f"must drop the exact latest historical signature of {function}",
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

    def test_mcp_source_type_is_optional_with_enum_or_none(self):
        from gateway.memory_mcp import memory_mcp

        tools = {tool.name: tool for tool in asyncio.run(memory_mcp.list_tools())}
        for name, tool in tools.items():
            if name not in {"remember_moment", "remember_thread", "remember_inside_joke",
                            "propose_episode", "propose_profile", "propose_interaction_rule"}:
                continue
            schema = tool.input_schema["properties"]["source_type"]
            with self.subTest(tool=name):
                # 参数可省略：source_type 不在任何工具的 required 列表中。
                self.assertNotIn("source_type", tool.input_schema.get("required", []))
                # 类型允许固定枚举或 None。
                variants = schema.get("anyOf") or [schema]
                kinds = {variant.get("type") for variant in variants}
                self.assertIn("null", kinds)
                self.assertTrue(
                    any("enum" in variant for variant in variants),
                    "source_type 必须暴露固定枚举",
                )

    def test_gateway_source_type_normalizes_blank_to_none(self):
        from gateway.memory_requests import validate_memory_request

        base = {
            "assistant_id": "a",
            "content": "一条用于校验来源类型的记忆内容。",
            "reason": "校验 source_type 归一化。",
            "continuity_type": "moment",
            "continuity_data": {"scene": "窗口", "event": "确认", "moment_state": "standalone"},
        }
        for variant in (
            {},
            {"source_type": None},
            {"source_type": ""},
            {"source_type": "   "},
        ):
            with self.subTest(variant=variant):
                normalized = validate_memory_request({**base, **variant})
                self.assertIsNone(normalized["source_type"])

        normalized = validate_memory_request({**base, "source_type": "PERSONA_PROMPT"})
        self.assertEqual(normalized["source_type"], "persona_prompt")

        for variant in (
            {"source_type": "guess"},
            {"source_type": 123},
            {"source_type": ["natural_chat"]},
        ):
            with self.subTest(variant=variant):
                with self.assertRaises(Exception):
                    validate_memory_request({**base, **variant})

    def test_continuity_pipeline_accepts_null_source_type(self):
        # 自动总结/连续感候选缺少或为 null 的 source_type 不得丢弃整条候选；
        # 非空非法值仍然拒绝。
        from gateway.memory_continuity_shadow import parse_shadow_output

        candidate = {
            "content": "叶子和栖约好下次继续讨论旅行计划。",
            "continuity_type": "thread",
            "continuity_data": {
                "open_question": "旅行计划", "current_state": "讨论中",
                "closure_criteria": [], "abstract_retrieval_hints": [],
                "concrete_retrieval_hints": [],
            },
            "thread_state": "open",
            "importance": 5,
            "confidence": 0.9,
            "evidence_message_ids": [11],
            "memory_time": None,
            "time_precision": "unknown",
            "title": "旅行计划待续",
            "reason": "下个窗口需要继续。",
        }
        parsed = parse_shadow_output(
            "```json\n" + json.dumps({"candidates": [candidate]}, ensure_ascii=False) + "\n```",
            {11: None},
        )
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0]["source_type"])

        invalid = dict(candidate, source_type="made_up", evidence_message_ids=[12])
        parsed_invalid = parse_shadow_output(
            "```json\n" + json.dumps({"candidates": [invalid]}, ensure_ascii=False) + "\n```",
            {12: None},
        )
        self.assertEqual(parsed_invalid, [])

        legal = dict(candidate, source_type="quote", evidence_message_ids=[13])
        parsed_legal = parse_shadow_output(
            "```json\n" + json.dumps({"candidates": [legal]}, ensure_ascii=False) + "\n```",
            {13: None},
        )
        self.assertEqual(len(parsed_legal), 1)
        self.assertEqual(parsed_legal[0]["source_type"], "quote")

    def test_extract_pipeline_accepts_null_source_type(self):
        from gateway.memory_extract import _parse_model_output

        raw = json.dumps({"memories": [{
            "content": "叶子和栖确认了网关上线时间。",
            "continuity_type": "moment",
            "continuity_data": {"scene": "窗口", "event": "确认", "moment_state": "standalone"},
            "thread_state": None,
            "update_mode": "append",
            "memory_key": None,
            "importance": 6,
            "confidence": 0.9,
            "evidence_message_ids": [12, 13],
            "memory_time": None,
            "time_precision": "unknown",
        }]}, ensure_ascii=False)

        parsed = _parse_model_output(raw, source_times={12: None, 13: None})
        self.assertEqual(len(parsed), 1)
        self.assertIsNone(parsed[0]["source_type"])

        invalid = json.dumps({"memories": [{
            "content": "叶子和栖确认了网关上线时间。",
            "continuity_type": "moment",
            "continuity_data": {"scene": "窗口", "event": "确认", "moment_state": "standalone"},
            "source_type": "not_a_type",
        }]}, ensure_ascii=False)
        self.assertEqual(_parse_model_output(invalid, source_times={}), [])

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
        self.assertIn("20260902-adminmem2", index_html)
        self.assertIn("20260902-adminmem2", app_js)


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
