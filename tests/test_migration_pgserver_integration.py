"""Real-migration execution test on a throwaway PostgreSQL + pgvector instance.

Opt-in because the bundle is heavyweight:

    QIGATEWAY_PG_MIGRATION_TEST=1 python -m pytest \
        tests/test_migration_pgserver_integration.py -v

The test never touches any production database. It starts a self-contained
PostgreSQL from the ``pgserver`` package in a temp directory, replays the
whole migration history, seeds every memory shape that exists in production
(six continuity types, unclassified legacy rows, an incomplete row, an
archived row, pending/rejected requests), executes
20260831010000_retire_memory_metadata_fields.sql for real, and asserts the
retirement contract against the live database.

pgserver bundles pgvector but not pg_trgm, so the single ``create extension``
line in 20260804020000 is replaced with a comment before replay and a
deterministic ``extensions.similarity()`` stub is created up front. Only
trigram fuzzy strength in duplicate detection differs; every DDL effect under
test is unchanged.
"""

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
NEW_MIGRATION = MIGRATIONS_DIR / "20260831010000_retire_memory_metadata_fields.sql"
PG_TRGM_LINE = "create extension if not exists pg_trgm with schema extensions;"
PG_TRGM_FILE = "20260804020000_auto_digest_memory_requests.sql"

try:
    import pgserver
    import psycopg
    _STACK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional heavyweight stack
    _STACK_AVAILABLE = False

ENABLED = _STACK_AVAILABLE and os.environ.get("QIGATEWAY_PG_MIGRATION_TEST") == "1"


def _ensure_pg_timezone_data():
    """pgserver 的 PostgreSQL 没有附带 IANA 时区文件。

    迁移历史里的函数体（如 create_memory_request_v4 的证据时间换算）使用
    ``at time zone 'Asia/Shanghai'``，没有时区库会直接报错。PostgreSQL 读取
    的就是标准 IANA tzfile，与 Python tzdata 包的二进制文件同源，直接复制。
    """
    pg_install = Path(pgserver.__file__).parent / "pginstall"
    tzdir = pg_install / "share" / "postgresql" / "timezone"
    if (tzdir / "Asia" / "Shanghai").exists():
        return
    try:
        import tzdata
    except ImportError:  # pragma: no cover - depends on environment
        raise unittest.SkipTest("Python tzdata package unavailable")
    src = Path(tzdata.__file__).parent / "zoneinfo"
    tzdir.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, tzdir, dirs_exist_ok=True)

DROPPED_MEMORY_COLUMNS = ("layer", "emotion_weight", "subject", "participants",
                          "continuity_value", "retention_class")
DROPPED_REQUEST_COLUMNS = ("subject", "participants", "continuity_value",
                           "retention_class")
RETAINED_MEMORY_COLUMNS = ("source_type", "importance", "continuity_type",
                           "continuity_data", "thread_state", "recall_scene",
                           "recall_tags", "recall_embedding", "embedding",
                           "heat", "verified", "is_active", "content",
                           "memory_key", "evidence_message_ids")
RETAINED_REQUEST_COLUMNS = ("source_type", "importance", "continuity_type",
                            "continuity_data", "thread_state", "recall_scene",
                            "recall_tags", "embedding", "status", "source")

MOMENT_DATA = {
    "scene": "聊天窗口", "event": "约定赶海", "response": "栖记下了",
    "outcome": "定在下周三", "moment_state": "standalone",
    "salience_reason": "明确的出行约定",
}
THREAD_DATA = {
    "open_question": "下周三赶海是否成行", "current_state": "已约定待确认天气",
    "next_expected": "周三前确认天气", "closure_criteria": ["成行或改期"],
    "closure_summary": "", "closure_reason": "", "opened_at": "2026-08-25",
    "closed_at": "", "abstract_retrieval_hints": ["赶海"],
    "concrete_retrieval_hints": ["天气"],
}
EPISODE_DATA = {
    "beginning": "约好赶海", "development": "讨论装备与时间",
    "turning_point": "查了潮汐表", "outcome": "定在下周三清晨",
    "aftereffect": "开始期待", "episode_start_time": "2026-08-25",
    "episode_end_time": "", "closure_quality": "complete",
}
JOKE_DATA = {
    "origin": "把防晒霜叫贝壳", "trigger_phrases": ["贝壳"],
    "shared_meaning": "防晒霜的代号", "usage_context": ["赶海话题"],
    "avoid_context": [], "response_style": "轻松",
    "first_seen_at": "2026-08-25", "last_reinforced_at": "",
    "reinforcement_count": 1,
}
PROFILE_DATA = {
    "facet": "作息", "statement": "叶子习惯晚睡，通常凌晨一点后休息",
    "scope": "全局", "effective_from": "", "effective_until": "",
    "stability": "stable", "exceptions": [],
    "basis": "explicit_self_report",
}
RULE_DATA = {
    "trigger": "提到赶海", "expected_behavior": "提醒防晒",
    "forbidden_behavior": [], "scope": "全局", "priority": 5,
    "rule_state": "active", "effective_from": "", "effective_until": "",
    "exceptions": [], "explicit_instruction": "叶子要求赶海话题时提醒防晒",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Deterministic continuity object for the classified seed request (quoted SQL).
REQUEST_OBJECT_SQL = "'11111111-1111-1111-1111-1111111111a1'"


def _json_literal(data) -> str:
    return "'" + json.dumps(data, ensure_ascii=False) + "'::jsonb"


BASELINE_SQL = """
-- Roles referenced by every migration's grants/revokes. service_role 在生产
-- Supabase 里带 bypassrls 属性，RLS 策略对它不生效。
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;

create schema if not exists extensions;
create extension if not exists vector with schema extensions;

-- pgserver does not ship pg_trgm; deterministic stand-in so that
-- public.memory_dedupe_text_similarity can be created and executed.
create function extensions.similarity(p_left text, p_right text)
returns double precision
language sql
immutable
parallel safe
as $stub$
    select case when p_left = p_right then 1.0 else 0.0 end
$stub$;

grant usage on schema public, extensions to anon, authenticated, service_role;

-- Pre-existing production tables that the migration history expects to find.
create table public.chat_messages (
    id bigint generated by default as identity primary key,
    assistant_id text not null,
    conversation_id text,
    content text,
    created_at timestamptz not null default now()
);

create table public.memories (
    id integer generated by default as identity primary key,
    content text not null,
    title text,
    tags text[],
    heat double precision not null default 50,
    importance integer,
    layer text,
    embedding extensions.vector,
    source text,
    verified text not null default 'verified',
    is_active boolean not null default true,
    emotion_weight double precision,
    recall_count integer not null default 0,
    digest_run_id bigint,
    source_first_message_id bigint,
    source_last_message_id bigint,
    confidence double precision,
    -- 生产 memories 表先于本仓库的迁移历史存在；历史函数（如
    -- review_memory_request_v2 的 on conflict (content_hash)）依赖其唯一约束。
    content_hash text unique,
    created_at timestamptz not null default now(),
    last_recalled_at timestamptz
);

create table public.memory_digest_runs (
    id bigint generated by default as identity primary key,
    assistant_id text not null,
    mode text not null default 'execute',
    status text not null default 'running',
    trigger text,
    source_first_message_id bigint,
    source_last_message_id bigint,
    started_at timestamptz not null default now(),
    completed_at timestamptz,
    message_count integer,
    extracted_count integer,
    inserted_count integer,
    preview_memories jsonb,
    error_code text,
    error_message text
);

create table public.memory_digest_cursors (
    id bigint generated by default as identity primary key,
    assistant_id text not null unique,
    last_processed_message_id bigint not null default 0,
    last_success_at timestamptz,
    updated_at timestamptz not null default now()
);

-- 生产 Supabase 库由 default privileges 把表权限授予 service_role
-- （本仓库的历史迁移从不直接对 memories 授权）。临时库手工补齐同等权限，
-- 否则非 security definer 的 match_memories / search_memories_by_keywords /
-- run_memory_heat_decay 以 service_role 调用时会因表权限不足而失败。
grant select, update on public.memories to service_role;
"""


class MigrationExecutionOnPostgresTests(unittest.TestCase):
    """Executes the retirement migration on a real, disposable database."""

    @classmethod
    def setUpClass(cls):
        if not _STACK_AVAILABLE:
            raise unittest.SkipTest("pgserver + psycopg are not installed")
        if os.environ.get("QIGATEWAY_PG_MIGRATION_TEST") != "1":
            raise unittest.SkipTest(
                "set QIGATEWAY_PG_MIGRATION_TEST=1 to run the real "
                "PostgreSQL migration execution test"
            )
        cls.pgdata = Path(tempfile.mkdtemp(prefix="qigate-pgserver-"))
        cls.server = None
        cls.conn = None
        try:
            _ensure_pg_timezone_data()
            cls.server = pgserver.get_server(cls.pgdata, cleanup_mode="stop")
            cls.conn = psycopg.connect(cls.server.get_uri(), autocommit=True)
            cls.conn.execute(BASELINE_SQL)
            cls._apply_history()
            cls._seed()
            cls.before = cls._snapshots()
            cls._assert_retired_columns_had_real_data()
            # The file under test runs byte-for-byte from disk.
            cls.conn.execute(NEW_MIGRATION.read_text(encoding="utf-8"))
            cls.after = cls._snapshots()
            cls._assert_structure()
        except Exception:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        conn = getattr(cls, "conn", None)
        if conn is not None:
            conn.close()
            cls.conn = None
        server = getattr(cls, "server", None)
        if server is not None:
            server.cleanup()
            cls.server = None
        pgdata = getattr(cls, "pgdata", None)
        if pgdata is not None and pgdata.exists():
            shutil.rmtree(pgdata, ignore_errors=True)

    # -- helpers ----------------------------------------------------------

    def _query(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _query_one(self, sql, params=None):
        rows = self._query(sql, params)
        return rows[0][0] if rows else None

    def _call_as_service_role(self, sql, params=None):
        self.conn.execute("set role service_role")
        try:
            return self._query(sql, params)
        finally:
            self.conn.execute("reset role")

    @classmethod
    def _apply_history(cls):
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.name == NEW_MIGRATION.name:
                continue
            sql = path.read_text(encoding="utf-8")
            if path.name == PG_TRGM_FILE:
                assert PG_TRGM_LINE in sql, "pg_trgm line drifted"
                sql = sql.replace(PG_TRGM_LINE, "-- pg_trgm stubbed (pgserver)")
            cls.conn.execute(sql)

    @classmethod
    def _seed(cls):
        # One continuity object per classified seed row (continuity_id is
        # foreign-keyed to memory_continuity_objects).
        cls.conn.execute(
            "insert into public.memory_continuity_objects "
            "(continuity_id, assistant_id) values "
            + ", ".join(
                f"('11111111-1111-1111-1111-1111111111{mid % 100:02d}', 'a-seed')"
                for mid in range(101, 107)
            )
            + ", ('11111111-1111-1111-1111-1111111111a1', 'a-seed')"
        )
        memories = [
            # (id, content, layer, emotion, subject, participants, cvalue,
            #  rclass, source_type, ctype, thread_state, cdata, embedding,
            #  heat, importance, active, memory_key, recall_scene, recall_tags,
            #  recall_embedding)
            (101, "叶子和栖约好下周三去海边赶海。", "碎片", 0.6, "shared",
             "{yezi}", 7, "normal", "natural_chat", "moment", None,
             MOMENT_DATA, "[0.1,0.2,0.3]", 80.0, 6, True, None,
             "聊到赶海或海边出行时", "{赶海,出行}", "[0.2,0.1,0.3]"),
            (102, "赶海线程：下周三是否成行待确认天气。", "场景", 0.5, "qi",
             "{yezi,qi}", 8, "core", "document", "thread", "open",
             THREAD_DATA, "[0.2,0.2,0.2]", 70.0, 7, True,
             "integration-thread", "确认赶海行程时", "{线程}", "[0.2,0.2,0.2]"),
            (103, "第一次赶海从约定到成行的完整经过。", "场景", 0.7, "shared",
             "{yezi}", 6, "normal", "roleplay", "episode", None,
             EPISODE_DATA, "[0.1,0.3,0.2]", 65.0, 6, True, None,
             "回顾赶海经历时", "{回顾}", "[0.1,0.3,0.2]"),
            (104, "叶子和栖把防晒霜叫作贝壳的梗。", "碎片", 0.8, "shared",
             "{yezi,qi}", 5, "normal", "natural_chat", "inside_joke", None,
             JOKE_DATA, "[0.3,0.1,0.2]", 60.0, 5, True, None,
             "出现贝壳或防晒话题时", "{梗}", "[0.3,0.1,0.2]"),
            (105, "叶子习惯晚睡，通常凌晨一点后休息。", "场景", 0.3, "yezi",
             "{yezi}", 6, "core", "persona_prompt", "profile", None,
             PROFILE_DATA, "[0.2,0.3,0.1]", 55.0, 6, True, None,
             "安排作息或深夜聊天时", "{作息}", "[0.2,0.3,0.1]"),
            (106, "提到赶海时栖要提醒叶子防晒。", "场景", 0.4, "shared",
             "{yezi,qi}", 7, "core", "system_meta", "interaction_rule", None,
             RULE_DATA, "[0.1,0.1,0.3]", 50.0, 7, True, None,
             "赶海话题出现时", "{规则}", "[0.1,0.1,0.3]"),
            (107, "叶子喜欢在海边散步放松。", "场景", 0.4, "yezi", "{yezi}",
             5, "normal", None, None, None, None, "[0.3,0.2,0.1]", 55.0, 5,
             True, None, "聊到海边散步时", None, "[0.3,0.2,0.1]"),
            (108, "旧的未完成记忆条目，缺少嵌入与结构。", "碎片", None, None,
             None, None, None, None, None, None, None, None, 30.0, 3, True,
             None, None, None, None),
            (109, "已归档的历史记忆条目。", "碎片", 0.2, "yezi", None, 4,
             "normal", None, None, None, None, None, 10.0, 4, False, None,
             None, None, None),
        ]
        values = []
        for row in memories:
            (mid, content, layer, emotion, subject, participants, cvalue,
             rclass, source_type, ctype, thread_state, cdata, embedding,
             heat, importance, active, memory_key, recall_scene, recall_tags,
             recall_embedding) = row
            continuity_id = (
                "null" if cdata is None
                else f"'11111111-1111-1111-1111-1111111111{mid % 100:02d}'"
            )
            values.append(
                f"({mid}, {_literal(content)}, null, '{{}}'::text[], {heat}, "
                f"{importance}, {_literal(layer)}, "
                f"{_vector(embedding)}, 'ai_tool_request', 'verified', "
                f"{'true' if active else 'false'}, "
                f"{emotion if emotion is not None else 'null'}, 0, "
                f"0.9, '{_sha256(content)}', "
                f"now() - interval '{120 - mid} days', "
                f"{_literal(ctype)}, {_literal(thread_state)}, "
                f"{'null' if cdata is None else _json_literal(cdata)}, "
                f"{continuity_id}, "
                f"{'1' if cdata is not None else 'null'}, "
                f"{_literal(subject)}, {_literal(source_type)}, "
                f"{_literal(participants)}, "
                f"{cvalue if cvalue is not None else 'null'}, "
                f"{_literal(rclass)}, "
                f"{_literal(memory_key)}, {_literal(recall_scene)}, "
                f"{_literal(recall_tags)}, {_vector(recall_embedding)}, "
                f"'{{{mid}}}'::bigint[], "
                f"now() - interval '30 days', now() - interval '30 days', "
                f"'minute', null, 'unknown', null, {mid}, {mid}, 'a-seed')"
            )
        cls.conn.execute(
            """
            insert into public.memories (
                id, content, title, tags, heat, importance, layer, embedding,
                source, verified, is_active, emotion_weight, recall_count,
                confidence, content_hash, created_at, continuity_type,
                thread_state, continuity_data, continuity_id,
                continuity_schema_version, subject, source_type,
                participants, continuity_value, retention_class, memory_key,
                recall_scene, recall_tags, recall_embedding,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision, memory_time, time_precision,
                source_time, source_first_message_id, source_last_message_id,
                assistant_id
            ) values
            """ + ",\n".join(values)
        )

        requests = [
            ("叶子和栖约定周五检查部署窗口。", "pending", "mcp_memory",
             "project", "{yezi}", 8, "core", "natural_chat", "moment",
             MOMENT_DATA, "seed-req-1"),
            ("一条早已被拒绝的历史记忆申请。", "rejected", "orangechat_plugin",
             "yezi", None, 5, "normal", None, None, None, "seed-req-2"),
        ]
        request_values = []
        for (content, status, source, subject, participants, cvalue, rclass,
             source_type, ctype, cdata, idem) in requests:
            request_values.append(
                f"('a-seed', 'c-seed', 1, {_literal(content)}, null, "
                f"'{{\"种子\"}}'::text[], 6, 'integration seed request', "
                f"'{_sha256(content)}', '{idem}', '{status}', '{source}', "
                f"{_literal(ctype)}, null, "
                f"{'null' if cdata is None else _json_literal(cdata)}, "
                f"{'null' if cdata is None else REQUEST_OBJECT_SQL}, "
                f"{'1' if cdata is not None else 'null'}, "
                f"{_literal(subject)}, {_literal(source_type)}, "
                f"{_literal(participants)}, {cvalue}, {_literal(rclass)}, "
                f"'{{1}}'::bigint[], now() - interval '2 days', "
                f"now() - interval '2 days', 'minute', 'append', 0.9, null)"
            )
        cls.conn.execute(
            """
            insert into public.memory_requests (
                assistant_id, conversation_id, source_message_id, content,
                title, tags, importance, reason, content_hash,
                idempotency_key, status, source, continuity_type,
                thread_state, continuity_data, continuity_id,
                continuity_schema_version, subject, source_type,
                participants, continuity_value, retention_class,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision, update_mode, confidence, memory_key
            ) values
            """ + ",\n".join(request_values)
        )

        messages = [
            (1, "a-seed", "c-seed", "用户说：我们下周三去海边赶海。"),
            (2, "a-seed", "c-seed", "栖回答：好，记下了。"),
            (3, "a-seed", "c-seed", "用户说：记得带防晒。"),
            (4, "a-seed", "c-seed", "栖回答：已记。"),
            (5, "a-cont", "c-cont", "用户说：周五的发布窗口定在上午十点。"),
            (6, "a-cont", "c-cont", "栖回答：已确认发布窗口。"),
            (7, "a-null", "c-null", "用户说：希望记住这次确认。"),
            (8, "a-quote", "c-quote", "用户说：这段话原样记下来。"),
            (9, "a-merge", "c-merge", "用户说：更新一下赶海线程。"),
            (10, "a-direct", "c-direct", "用户说：这条直接写入。"),
        ]
        cls.conn.execute(
            "insert into public.chat_messages "
            "(id, assistant_id, conversation_id, content, created_at) values "
            + ",\n".join(
                f"({mid}, '{assistant}', '{conversation}', {_literal(content)}, "
                f"now() - interval '{20 - mid} hours')"
                for mid, assistant, conversation, content in messages
            )
        )
        cls.conn.execute(
            "alter table public.memories alter column id restart with 1000"
        )
        cls.conn.execute(
            "alter table public.chat_messages alter column id restart with 100"
        )

    @classmethod
    def _snapshots(cls):
        return {
            "memories": cls.conn.execute(
                """
                select coalesce(jsonb_agg(to_jsonb(t) order by t.id), '[]')
                from (
                    select m.id, m.content, m.title, m.tags, m.heat,
                           m.importance, m.source, m.verified, m.is_active,
                           m.recall_count, m.confidence, m.content_hash,
                           m.continuity_type, m.thread_state,
                           m.continuity_id, m.continuity_schema_version,
                           m.continuity_data,
                           m.source_type, m.memory_time, m.time_precision,
                           m.memory_key, m.source_time,
                           m.evidence_start_time, m.evidence_end_time,
                           m.evidence_time_precision, m.recall_scene,
                           m.recall_tags, m.embedding::text as embedding,
                           m.recall_embedding::text as recall_embedding,
                           m.created_at, m.last_recalled_at, m.assistant_id,
                           m.digest_run_id, m.source_first_message_id,
                           m.source_last_message_id
                    from public.memories m
                ) t
                """
            ).fetchone()[0],
            "requests": cls.conn.execute(
                """
                select coalesce(jsonb_agg(to_jsonb(t) order by t.id), '[]')
                from (
                    select r.id, r.assistant_id, r.conversation_id,
                           r.source_message_id, r.content, r.title, r.tags,
                           r.importance, r.reason, r.content_hash,
                           r.idempotency_key, r.status, r.source, r.memory_id,
                           r.continuity_type, r.thread_state,
                           r.continuity_schema_version, r.continuity_data,
                           r.source_type, r.recall_scene, r.recall_tags,
                           r.embedding::text as embedding,
                           r.evidence_message_ids, r.evidence_start_time,
                           r.evidence_end_time, r.evidence_time_precision,
                           r.memory_time, r.time_precision, r.source_time,
                           r.memory_key, r.update_mode, r.confidence,
                           r.created_at, r.updated_at
                    from public.memory_requests r
                ) t
                """
            ).fetchone()[0],
            "chat_messages": cls.conn.execute(
                """
                select coalesce(jsonb_agg(to_jsonb(t) order by t.id), '[]')
                from (
                    select * from public.chat_messages
                ) t
                """
            ).fetchone()[0],
        }

    @classmethod
    def _assert_retired_columns_had_real_data(cls):
        counts = cls.conn.execute(
            """
            select
                count(*) filter (where layer is not null),
                count(*) filter (where emotion_weight is not null),
                count(*) filter (where subject is not null),
                count(*) filter (where participants is not null),
                count(*) filter (where continuity_value is not null),
                count(*) filter (where retention_class is not null)
            from public.memories
            """
        ).fetchone()
        assert all(value >= 5 for value in counts), (
            f"seeded memories must carry real retired values, got {counts}"
        )
        request_counts = cls.conn.execute(
            """
            select
                count(*) filter (where subject is not null),
                count(*) filter (where participants is not null),
                count(*) filter (where continuity_value is not null),
                count(*) filter (where retention_class is not null)
            from public.memory_requests
            """
        ).fetchone()
        assert all(value >= 1 for value in request_counts), (
            f"seeded requests must carry real retired values, "
            f"got {request_counts}"
        )

    @classmethod
    def _assert_structure(cls):
        for table, dropped in (
            ("memories", DROPPED_MEMORY_COLUMNS),
            ("memory_requests", DROPPED_REQUEST_COLUMNS),
        ):
            columns = {
                row[0]
                for row in cls.conn.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema = 'public' and table_name = %s",
                    (table,),
                ).fetchall()
            }
            still = sorted(set(dropped) & columns)
            assert not still, f"{table} still has dropped columns: {still}"
        memory_columns = {
            row[0]
            for row in cls.conn.execute(
                "select column_name from information_schema.columns "
                "where table_schema = 'public' and table_name = 'memories'"
            ).fetchall()
        }
        missing = sorted(set(RETAINED_MEMORY_COLUMNS) - memory_columns)
        assert not missing, f"memories lost retained columns: {missing}"

        assert cls.before["memories"] == cls.after["memories"], (
            "existing memories changed beyond dropping retired columns"
        )
        assert cls.before["requests"] == cls.after["requests"], (
            "existing memory_requests changed beyond dropping retired columns"
        )
        assert cls.before["chat_messages"] == cls.after["chat_messages"], (
            "public.chat_messages was modified by the migration"
        )

        for function, forbidden in (
            ("create_memory_request_v4",
             ("p_subject", "p_participants", "p_continuity_value",
              "p_retention_class")),
            ("write_memory_direct_v1",
             ("p_subject", "p_participants", "p_continuity_value",
              "p_retention_class")),
        ):
            overloads = cls.conn.execute(
                "select proargnames from pg_proc p "
                "join pg_namespace n on p.pronamespace = n.oid "
                "where n.nspname = 'public' and p.proname = %s",
                (function,),
            ).fetchall()
            assert len(overloads) == 1, (
                f"{function} should have exactly one signature, "
                f"found {len(overloads)}"
            )
            args = overloads[0][0] or []
            leaked = sorted(set(forbidden) & set(args))
            assert not leaked, f"{function} still accepts {leaked}"

    # -- behaviour --------------------------------------------------------

    def test_columns_dropped_and_retained_columns_present(self):
        for table, dropped, retained in (
            ("memories", DROPPED_MEMORY_COLUMNS, RETAINED_MEMORY_COLUMNS),
            ("memory_requests", DROPPED_REQUEST_COLUMNS,
             RETAINED_REQUEST_COLUMNS),
        ):
            columns = {
                row[0]
                for row in self._query(
                    "select column_name from information_schema.columns "
                    "where table_schema = 'public' and table_name = %s",
                    (table,),
                )
            }
            with self.subTest(table=table):
                self.assertFalse(set(dropped) & columns)
                self.assertTrue(set(retained) <= columns)

    def test_existing_rows_and_chat_messages_are_untouched(self):
        self.assertEqual(self.before["memories"], self.after["memories"])
        self.assertEqual(self.before["requests"], self.after["requests"])
        self.assertEqual(self.before["chat_messages"],
                         self.after["chat_messages"])
        # 迁移本身不删行；后续行为测试会新增数据，因此用下限断言。
        self.assertGreaterEqual(
            self._query_one("select count(*) from public.memories"),
            len(self.before["memories"]))
        self.assertGreaterEqual(
            self._query_one("select count(*) from public.memory_requests"),
            len(self.before["requests"]))
        self.assertEqual(
            self._query_one("select count(*) from public.chat_messages"),
            len(self.before["chat_messages"]))
        # The unclassified legacy row keeps recalling through the vector
        # channel even though every retired column is gone.
        self.assertEqual(
            self._query_one(
                "select continuity_type from public.memories where id = 107"),
            None)

    def test_keyword_and_vector_recall_work_for_legacy_rows(self):
        vector_rows = self._call_as_service_role(
            "select id from public.match_memories("
            "'[0.1,0.2,0.3]'::extensions.vector, 0.1, 20)")
        vector_ids = {row[0] for row in vector_rows}
        self.assertIn(101, vector_ids)
        self.assertIn(107, vector_ids)  # unclassified legacy row
        self.assertNotIn(108, vector_ids)  # no embedding
        self.assertNotIn(109, vector_ids)  # archived

        keyword_rows = self._call_as_service_role(
            "select id from public.search_memories_by_keywords("
            "array['赶海'], 20)")
        keyword_ids = {row[0] for row in keyword_rows}
        self.assertIn(101, keyword_ids)
        self.assertNotIn(109, keyword_ids)

        row = self._query(
            "select source_type, continuity_type, recall_scene "
            "from public.match_memories("
            "'[0.2,0.1,0.3]'::extensions.vector, 0.9, 5) limit 1")[0]
        self.assertIsNotNone(row)

    def test_create_memory_request_v4_accepts_null_source_type(self):
        content = "叶子和栖确认了新网关的发布窗口。"
        result = self._call_as_service_role(
            "select public.create_memory_request_v4("
            "p_assistant_id => 'a-null', p_conversation_id => 'c-null', "
            "p_source_message_id => 7, p_content => %s, p_title => null, "
            "p_tags => array['发布'], p_importance => 6, "
            "p_reason => '用户明确要求记录', p_content_hash => %s, "
            "p_idempotency_key => 'it-null-1', p_rate_limit => 10, "
            "p_memory_key => null, p_update_mode => 'append', "
            "p_continuity_type => 'moment', p_thread_state => null, "
            "p_continuity_schema_version => 1::smallint, p_continuity_data => %s, "
            "p_source_type => null, p_source => 'mcp_memory', "
            "p_recall_scene => '安排发布窗口时', "
            "p_recall_tags => array['发布'])",
            (content, _sha256(content), json.dumps(MOMENT_DATA)),
        )[0][0]
        self.assertTrue(result["created"])
        self.assertIsNone(result["request"]["source_type"])
        request_id = result["request"]["id"]

        # v5 收尾用调用方传入的最终 recall 值覆盖记忆（审核编辑优先，
        # 否则取申请自身值）——网关总是传解析后的最终值，这里照做。
        review = self._call_as_service_role(
            "select public.review_memory_request_v5("
            "p_request_id => %s, p_action => 'approve', "
            "p_recall_scene => '安排发布窗口时', "
            "p_recall_tags => array['发布'])", (request_id,)
        )[0][0]
        memory_id = review["request"]["memory_id"]
        memory = self._query(
            "select source_type, continuity_type, thread_state, "
            "continuity_data, recall_scene from public.memories "
            "where id = %s", (memory_id,))[0]
        self.assertIsNone(memory[0])  # NULL survives into the formal memory
        self.assertEqual(memory[1], "moment")
        self.assertIsNone(memory[2])
        self.assertEqual(memory[3], MOMENT_DATA)
        self.assertEqual(memory[4], "安排发布窗口时")
        self.assertEqual(
            self._query_one(
                "select status from public.memory_requests where id = %s",
                (request_id,)),
            "approved")

    def test_legal_source_type_is_preserved_through_approve(self):
        content = "叶子要求原样保存这句话作为引用。"
        result = self._call_as_service_role(
            "select public.create_memory_request_v4("
            "p_assistant_id => 'a-quote', p_conversation_id => 'c-quote', "
            "p_source_message_id => 8, p_content => %s, p_title => null, "
            "p_tags => array['引用'], p_importance => 5, "
            "p_reason => '用户要求原样记录', p_content_hash => %s, "
            "p_idempotency_key => 'it-quote-1', p_rate_limit => 10, "
            "p_memory_key => null, p_update_mode => 'append', "
            "p_continuity_type => 'moment', p_thread_state => null, "
            "p_continuity_schema_version => 1::smallint, p_continuity_data => %s, "
            "p_source_type => 'quote', p_source => 'mcp_memory', "
            "p_recall_scene => null, p_recall_tags => null)",
            (content, _sha256(content), json.dumps(MOMENT_DATA)),
        )[0][0]
        self.assertEqual(result["request"]["source_type"], "quote")
        request_id = result["request"]["id"]

        review = self._call_as_service_role(
            "select public.review_memory_request_v5("
            "p_request_id => %s, p_action => 'approve')", (request_id,)
        )[0][0]
        memory_id = review["request"]["memory_id"]
        source_type = self._query_one(
            "select source_type from public.memories where id = %s",
            (memory_id,))
        self.assertEqual(source_type, "quote")

    def test_merge_preserves_existing_source_type(self):
        content = "赶海线程更新：天气确认，周三清晨出发。"
        result = self._call_as_service_role(
            "select public.create_memory_request_v4("
            # v5 的 merge 校验要求目标记忆与申请同属一个 assistant。
            "p_assistant_id => 'a-seed', p_conversation_id => 'c-seed', "
            "p_source_message_id => 3, p_content => %s, p_title => null, "
            "p_tags => array['线程'], p_importance => 7, "
            "p_reason => '线程更新', p_content_hash => %s, "
            "p_idempotency_key => 'it-merge-1', p_rate_limit => 10, "
            "p_memory_key => 'integration-thread', "
            "p_update_mode => 'replace', "
            "p_continuity_type => 'thread', p_thread_state => 'open', "
            "p_continuity_schema_version => 1::smallint, p_continuity_data => %s, "
            "p_source_type => null, p_source => 'mcp_memory', "
            "p_recall_scene => null, p_recall_tags => null)",
            (content, _sha256(content), json.dumps(THREAD_DATA)),
        )[0][0]
        request_id = result["request"]["id"]

        review = self._call_as_service_role(
            "select public.review_memory_request_v5("
            "p_request_id => %s, p_action => 'merge', "
            # merge 不会自行推断措辞，必须由调用方提供合并后的内容与哈希。
            "p_content => %s, p_content_hash => %s, "
            "p_related_memory_id => 102)",
            (request_id, content, _sha256(content))
        )[0][0]
        self.assertEqual(
            self._query_one(
                "select status from public.memory_requests where id = %s",
                (request_id,)),
            "merged")
        # merge 用合并措辞创建新记忆；目标被软停用并链接到结果。
        result_memory_id = review["request"]["memory_id"]
        self.assertIsNotNone(result_memory_id)
        self.assertNotEqual(result_memory_id, 102)
        self.assertEqual(
            self._query_one(
                "select related_memory_id from public.memory_requests "
                "where id = %s", (request_id,)),
            102)
        target = self._query(
            "select is_active, source_type, superseded_by_memory_id "
            "from public.memories where id = 102")[0]
        self.assertFalse(target[0])  # soft-deactivated
        # The target memory keeps its own source_type instead of being
        # cleared by the merging request's NULL.
        self.assertEqual(target[1], "document")
        self.assertEqual(target[2], result_memory_id)
        # 申请为 NULL 时合并结果也不被编造出 source_type。
        self.assertIsNone(
            self._query_one(
                "select source_type from public.memories where id = %s",
                (result_memory_id,)))

    def test_write_memory_direct_v1_new_signature_works(self):
        content = "叶子确认这条记忆可以直接写入。"
        result = self._call_as_service_role(
            "select public.write_memory_direct_v1("
            "p_assistant_id => 'a-direct', p_conversation_id => 'c-direct', "
            "p_source_message_id => 10, p_content => %s, p_title => null, "
            "p_tags => array['直写'], p_importance => 6, "
            "p_reason => '用户确认直写', p_content_hash => %s, "
            "p_idempotency_key => 'it-direct-1', p_rate_limit => 10, "
            "p_memory_key => null, p_update_mode => 'append', "
            "p_continuity_type => 'moment', p_thread_state => null, "
            "p_continuity_schema_version => 1::smallint, p_continuity_data => %s, "
            "p_source_type => null, p_source => 'mcp_memory', "
            "p_reviewed_by => 'integration-test', "
            "p_recall_scene => '验证直写通道时', "
            "p_recall_tags => array['直写'], "
            "p_recall_embedding => '[0.1,0.2,0.3]'::extensions.vector)",
            (content, _sha256(content), json.dumps(MOMENT_DATA)),
        )[0][0]
        request_id = result["request"]["id"]
        memory = self._query(
            "select m.source_type, m.verified, m.is_active, m.recall_scene "
            "from public.memories m "
            "join public.memory_requests r on r.memory_id = m.id "
            "where r.id = %s", (request_id,))[0]
        self.assertIsNone(memory[0])
        self.assertEqual(memory[1], "verified")
        self.assertTrue(memory[2])
        self.assertEqual(memory[3], "验证直写通道时")

    def test_continuity_commit_accepts_null_and_legal_source_type(self):
        null_content = "叶子和栖确认了周五上午十点的发布窗口。"
        legal_content = "叶子在排练里说过想保留这句台词。"
        # 陈旧批次守卫按游标判定：活跃助手存在游标行，本批窗口在其前方。
        self.conn.execute(
            "insert into public.memory_continuity_cursors "
            "(assistant_id, last_processed_message_id) values ('a-cont', 4)"
        )
        run_id = self._query_one(
            "insert into public.memory_digest_runs "
            "(assistant_id, mode, status, trigger, "
            "source_first_message_id, source_last_message_id, pipeline, "
            "started_at) values ('a-cont', 'execute', 'running', "
            "'continuity_manual', 5, 6, 'continuity', "
            "now() - interval '5 minutes') returning id")
        candidates = [
            {
                "continuity_type": "moment", "thread_state": None,
                "continuity_schema_version": 1,
                "continuity_data": MOMENT_DATA,
                "content": null_content,
                "content_hash": _sha256(null_content),
                "embedding": "[0.1,0.2,0.3]",
                "evidence_message_ids": [5, 6],
                "importance": 6, "confidence": 0.9,
                "update_mode": "append", "memory_key": None,
                "source_type": None,
                "recall_scene": "安排发布窗口时",
                "recall_tags": ["发布"],
                "recall_embedding": "[0.1,0.2,0.3]",
            },
            {
                "continuity_type": "moment", "thread_state": None,
                "continuity_schema_version": 1,
                "continuity_data": MOMENT_DATA,
                "content": legal_content,
                "content_hash": _sha256(legal_content),
                "embedding": "[0.2,0.1,0.3]",
                "evidence_message_ids": [5],
                "importance": 5, "confidence": 0.8,
                "update_mode": "append", "memory_key": None,
                "source_type": "roleplay",
                "recall_scene": "聊到排练台词时",
                "recall_tags": ["排练"],
                "recall_embedding": "[0.2,0.1,0.3]",
            },
        ]
        inserted = self._call_as_service_role(
            "select public.commit_memory_continuity_run(%s, %s)",
            (run_id, json.dumps(candidates)),
        )[0][0]
        self.assertEqual(inserted, 2)

        statuses = dict(self._query(
            "select r.source_type, r.status "
            "from public.memory_requests r where r.assistant_id = 'a-cont'"))
        self.assertEqual(statuses.get(None), "approved")
        self.assertEqual(statuses.get("roleplay"), "approved")
        memory_types = dict(self._query(
            "select m.source_type, m.verified "
            "from public.memories m "
            "join public.memory_requests r on r.memory_id = m.id "
            "where r.assistant_id = 'a-cont'"))
        self.assertEqual(memory_types.get(None), "verified")
        self.assertEqual(memory_types.get("roleplay"), "verified")
        self.assertEqual(
            self._query_one(
                "select status from public.memory_digest_runs where id = %s",
                (run_id,)),
            "succeeded")

    def test_heat_decay_only_decays_heat_and_never_archives(self):
        heat_before = self._query_one(
            "select heat from public.memories where id = 101")
        archived_heat_before = self._query_one(
            "select heat from public.memories where id = 109")
        result = self._call_as_service_role(
            "select public.run_memory_heat_decay()")[0][0]
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["archived_count"], 0)
        self.assertGreaterEqual(result["updated_count"], 1)

        heat_after = self._query_one(
            "select heat from public.memories where id = 101")
        self.assertLess(heat_after, heat_before)
        self.assertEqual(
            self._query_one("select heat from public.memories where id = 109"),
            archived_heat_before)
        self.assertEqual(
            self._query_one(
                "select count(*) from public.memories where is_active = false"),
            1)
        run = self._query(
            "select archived_count, updated_count "
            "from public.memory_heat_runs "
            "order by run_date desc limit 1")[0]
        self.assertEqual(run[0], 0)
        self.assertGreaterEqual(run[1], 1)

        again = self._call_as_service_role(
            "select public.run_memory_heat_decay()")[0][0]
        self.assertEqual(again["status"], "already_ran")
        self.assertEqual(again["archived_count"], 0)

    def test_permissions_anon_denied_service_role_allowed(self):
        self.conn.execute("set role anon")
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self._query(
                    "select public.match_memories("
                    "'[0.1,0.2,0.3]'::extensions.vector, 0.1, 5)")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self._query(
                    "select public.search_memories_by_keywords("
                    "array['赶海'], 5)")
        finally:
            self.conn.execute("reset role")

        rows = self._call_as_service_role(
            "select count(*) from public.search_memories_by_keywords("
            "array['赶海'], 5)")
        self.assertGreaterEqual(rows[0][0], 1)
        rows = self._call_as_service_role(
            "select public.create_memory_request_v4("
            "p_assistant_id => 'a-perm', p_conversation_id => null, "
            "p_source_message_id => null, p_content => '权限检查用的记忆申请。', "
            "p_title => null, p_tags => array['权限'], p_importance => 5, "
            "p_reason => '权限检查', p_content_hash => %s, "
            "p_idempotency_key => 'it-perm-1', p_rate_limit => 10, "
            "p_memory_key => null, p_update_mode => 'append', "
            "p_continuity_type => 'moment', p_thread_state => null, "
            "p_continuity_schema_version => 1::smallint, p_continuity_data => %s, "
            "p_source_type => null, p_source => 'mcp_memory', "
            "p_recall_scene => null, p_recall_tags => null)",
            (_sha256("权限检查用的记忆申请。"), json.dumps(MOMENT_DATA)))
        self.assertTrue(rows[0][0]["created"])


def _literal(value):
    if value is None:
        return "null"
    escaped = str(value).replace("'", "''")
    return f"'{escaped}'"


def _vector(value):
    if value is None:
        return "null"
    return f"'{value}'::extensions.vector"


if __name__ == "__main__":
    unittest.main()
