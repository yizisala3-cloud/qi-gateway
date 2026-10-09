"""Real-migration execution test for 20260902010000_admin_memory_lifecycle.sql.

Opt-in because the bundle is heavyweight:

    QIGATEWAY_ADMIN_MEMORY_PG_TEST=1 python -m pytest \
        tests/test_admin_memory_lifecycle_pgserver_integration.py -v

The test never touches any production database. It starts a self-contained
PostgreSQL from the ``pgserver`` package, replays the whole migration
history (including this migration), then drives the five lifecycle RPCs as
service_role against a seeded memory chain:

* manual creation of every six-class type with server-pinned fields,
* A -> B -> C type changes keeping exactly B and C,
* undo restoring the direct parent exactly once,
* natural-archive restore resetting heat to 50 and rejecting conflicts,
* permission isolation (anon/authenticated keep no execute rights),
* chat_messages staying byte-for-byte untouched.

The baseline schema and tz bootstrap are imported from the retirement
integration test so both suites exercise the identical fixture.
"""

import json
import unittest

from tests.test_migration_pgserver_integration import (  # noqa: F401
    BASELINE_SQL,
    PG_TRGM_FILE,
    PG_TRGM_LINE,
    _ensure_pg_timezone_data,
)

try:
    import pgserver
    import psycopg
    _STACK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional heavyweight stack
    _STACK_AVAILABLE = False

import os
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"
ASSISTANT = "a-admin"

ENABLED = _STACK_AVAILABLE and os.environ.get("QIGATEWAY_ADMIN_MEMORY_PG_TEST") == "1"

MOMENT_DATA = {"scene": "聊天窗口", "event": "约定赶海", "moment_state": "standalone"}
THREAD_DATA = {"open_question": "下周三赶海是否成行", "current_state": "已约定待确认"}
EPISODE_DATA = {"beginning": "约好赶海", "development": "讨论装备",
                "outcome": "定在下周三", "closure_quality": "complete"}
JOKE_DATA = {"origin": "把防晒霜叫贝壳", "trigger_phrases": ["贝壳"],
             "shared_meaning": "防晒霜的代号"}
PROFILE_DATA = {"facet": "作息", "statement": "叶子习惯晚睡", "scope": "全局",
                "stability": "stable", "basis": "explicit_self_report"}
RULE_DATA = {"trigger": "提到赶海", "expected_behavior": "提醒防晒", "scope": "全局",
             "priority": 5, "rule_state": "active",
             "explicit_instruction": "赶海话题时提醒防晒"}


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.casefold().encode("utf-8")).hexdigest()


def _vec(text: str) -> str:
    return f"'{text}'::extensions.vector"


class AdminMemoryLifecycleOnPostgresTests(unittest.TestCase):
    """Executes the lifecycle migration and RPCs on a real, disposable database."""

    @classmethod
    def setUpClass(cls):
        if not _STACK_AVAILABLE:
            raise unittest.SkipTest("pgserver + psycopg are not installed")
        if os.environ.get("QIGATEWAY_ADMIN_MEMORY_PG_TEST") != "1":
            raise unittest.SkipTest(
                "set QIGATEWAY_ADMIN_MEMORY_PG_TEST=1 (or run pytest --db) to "
                "run the real PostgreSQL lifecycle migration test"
            )
        cls.pgdata = Path(tempfile.mkdtemp(prefix="qigate-adminmem-"))
        cls.server = None
        cls.conn = None
        try:
            _ensure_pg_timezone_data()
            cls.server = pgserver.get_server(cls.pgdata, cleanup_mode="stop")
            cls.conn = psycopg.connect(cls.server.get_uri(), autocommit=True)
            cls.conn.execute(BASELINE_SQL)
            cls._apply_history()
            cls._grant_chat_evidence_read()
            cls._seed()
            with cls.conn.cursor() as cur:
                cur.execute("select count(*) from public.chat_messages")
                cls.chat_count_before = cur.fetchone()[0]
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

    def _one(self, sql, params=None):
        rows = self._query(sql, params)
        return rows[0][0] if rows else None

    def _as_service_role(self, sql, params=None):
        self.conn.execute("set role service_role")
        try:
            with self.conn.cursor() as cur:
                cur.execute(sql, params)
                if cur.description:
                    return cur.fetchall()
                return None
        finally:
            self.conn.execute("reset role")

    def _expect_rpc_error(self, sql, params, code):
        with self.assertRaises(psycopg.errors.RaiseException) as ctx:
            self._as_service_role(sql, params)
        self.assertIn(code, str(ctx.exception))

    @classmethod
    def _apply_history(cls):
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            sql = path.read_text(encoding="utf-8")
            if path.name == PG_TRGM_FILE:
                assert PG_TRGM_LINE in sql, "pg_trgm line drifted"
                sql = sql.replace(PG_TRGM_LINE, "-- pg_trgm stubbed (pgserver)")
            cls.conn.execute(sql)

    @staticmethod
    def _grant_chat_evidence_read():
        # 生产 chat_messages 带 role 列（证据弹窗读取角色）；retire 基线是
        # 最小表结构，这里补齐该列。测试夹具专属，不涉及任何 migration。
        pass

    @classmethod
    def _seed(cls):
        cls.conn.execute(
            "alter table public.chat_messages add column if not exists role text"
        )
        cls.conn.execute(
            "insert into public.chat_messages (assistant_id, conversation_id, role, content) values"
            " (%s, 'conv-1', 'user', '我们下周三去赶海吧'),"
            " (%s, 'conv-1', 'assistant', '好，记得带防晒霜')",
            (ASSISTANT, ASSISTANT),
        )
        cls.conn.execute(
            "insert into public.memory_continuity_objects (continuity_id, assistant_id)"
            " values ('22222222-2222-2222-2222-2222222222a1', %s)",
            (ASSISTANT,),
        )
        # 一条已分类的有效 moment：类型修改链的起点 A。
        cls.conn.execute(
            """
            insert into public.memories (
                content, title, tags, heat, importance, source, verified, is_active,
                assistant_id, confidence, content_hash,
                continuity_id, continuity_type, continuity_schema_version, continuity_data,
                recall_scene, recall_tags, recall_embedding
            ) values (
                '叶子和栖约好下周三去海边赶海。', '赶海之约', '{出行}', 60, 6,
                'daily_digest', 'verified', true,
                %s, 1.0, %s,
                '22222222-2222-2222-2222-2222222222a1', 'moment', 1, %s,
                '聊到赶海时', '{赶海}', '[0.2,0.1,0.3]'
            )
            """,
            (ASSISTANT, _sha256("叶子和栖约好下周三去海边赶海。"),
             json.dumps(MOMENT_DATA, ensure_ascii=False)),
        )

    # -- 1. manual creation ------------------------------------------------

    def _create(self, content, ctype, data, *, thread_state=None, evidence=None,
                recall_scene=None, recall_tags=None, memory_time=None,
                time_precision=None):
        # psycopg 需要显式的 Postgres 数组字面量；生产经 PostgREST 传 JSON 数组。
        if evidence:
            evidence = '{' + ','.join(str(int(i)) for i in evidence) + '}'
        return self._as_service_role(
            "select public.create_admin_memory_v1(%s, %s, %s, null, %s, 6, %s, %s, %s,"
            " %s, %s, %s::extensions.vector, %s, %s, %s, %s)",
            (
                ASSISTANT, content, _sha256(content),
                ["出行"],
                None,
                memory_time, time_precision,
                recall_scene, recall_tags,
                "[0.1,0.2,0.3]" if recall_scene else None,
                ctype, thread_state,
                json.dumps(data, ensure_ascii=False),
                evidence,
            ),
        )[0][0]

    def test_manual_creation_pins_server_fields(self):
        for ctype, data, state in (
            ("moment", MOMENT_DATA, None),
            ("thread", THREAD_DATA, "open"),
            ("episode", EPISODE_DATA, None),
            ("inside_joke", JOKE_DATA, None),
            ("profile", PROFILE_DATA, None),
            ("interaction_rule", RULE_DATA, None),
        ):
            with self.subTest(type=ctype):
                result = self._create(
                    f"这条{ctype}记忆由管理后台手工写入，内容完整有效。",
                    ctype, data, thread_state=state,
                    recall_scene="聊到相关话题时", recall_tags=["手工"],
                )
                memory_id = result["memory_id"]
                db_row = self._query(
                    "select source, verified, is_active, heat, continuity_id,"
                    " continuity_schema_version, recall_embedding is not null"
                    " from public.memories where id = %s",
                    (memory_id,),
                )[0]
                self.assertEqual(db_row[0], "manual")
                self.assertEqual(db_row[1], "verified")
                self.assertTrue(db_row[2])
                self.assertEqual(db_row[3], 50.0)
                self.assertIsNotNone(db_row[4])
                self.assertEqual(db_row[5], 1)
                self.assertTrue(db_row[6])

    def test_manual_creation_without_evidence_keeps_times_null(self):
        result = self._create(
            "没有证据消息的手工记忆不伪造任何时间。", "moment", MOMENT_DATA,
        )
        started, ended, precision, source_time = self._query(
            "select evidence_start_time, evidence_end_time, evidence_time_precision, source_time"
            " from public.memories where id = %s", (result["memory_id"],),
        )[0]
        self.assertIsNone(started)
        self.assertIsNone(ended)
        self.assertIsNone(precision)
        self.assertIsNone(source_time)

    def test_manual_creation_with_evidence_derives_minute_window(self):
        evidence_ids = [row[0] for row in self._query(
            "select id from public.chat_messages order by id")]
        result = self._create(
            "带证据消息的手工记忆会推导证据时间。", "moment", MOMENT_DATA,
            evidence=evidence_ids,
        )
        started, ended, precision, ids = self._query(
            "select evidence_start_time, evidence_end_time, evidence_time_precision,"
            " evidence_message_ids from public.memories where id = %s",
            (result["memory_id"],),
        )[0]
        self.assertIsNotNone(started)
        self.assertIsNotNone(ended)
        self.assertEqual(precision, "minute")
        self.assertEqual(list(ids), sorted(evidence_ids))

    def test_sceneless_creation_never_calls_vector_and_stays_recallable_by_keyword(self):
        result = self._create("没有召回场景的手工记忆保持向量为空。", "moment", MOMENT_DATA)
        scene, embedding = self._query(
            "select recall_scene, recall_embedding from public.memories where id = %s",
            (result["memory_id"],),
        )[0]
        self.assertIsNone(scene)
        self.assertIsNone(embedding)

    def test_scene_without_vector_is_refused(self):
        self._expect_rpc_error(
            "select public.create_admin_memory_v1(%s, %s, %s, null, %s, 6, null, null, null,"
            " %s, %s, null::extensions.vector, 'moment', null, %s, null)",
            (ASSISTANT, "场景向量缺失时必须拒绝写入。", _sha256("场景向量缺失时必须拒绝写入。"),
             "{}", "聊到赶海时", "{}",
             json.dumps(MOMENT_DATA, ensure_ascii=False)),
            "admin_memory_recall_vector_missing",
        )

    # -- 2. A -> B -> C versioning ----------------------------------------

    def _memory_row(self, memory_id):
        rows = self._query(
            "select id, continuity_type, is_active, verified, source,"
            " supersedes_memory_id, superseded_by_memory_id, superseded_at, continuity_id"
            " from public.memories where id = %s", (memory_id,))
        return rows[0] if rows else None

    def _current_seed_id(self):
        return self._one(
            "select id from public.memories where content_hash = %s",
            (_sha256("叶子和栖约好下周三去海边赶海。"),),
        )

    def _change_type(self, memory_id, content, ctype, data, *, thread_state=None):
        return self._as_service_role(
            "select public.change_memory_type_v1(%s, %s, %s, null, %s, 6, null, null, null,"
            " null, %s, null::extensions.vector, %s, %s, %s, null)",
            (
                memory_id, content, _sha256(content), ["出行"],
                ["类型修改"], ctype, thread_state,
                json.dumps(data, ensure_ascii=False),
            ),
        )[0][0]

    def _own_chain_base(self, content):
        """每个版本链测试自建起点，避免用例间顺序依赖。"""
        result = self._create(content, "moment", MOMENT_DATA)
        return result["memory_id"]

    def test_type_change_keeps_exactly_two_generations(self):
        a_id = self._own_chain_base("版本链测试起点：约好一起赶海。")
        continuity_id = self._memory_row(a_id)[8]

        b_result = self._change_type(
            a_id, "赶海线索：下周三是否成行待确认。", "thread", THREAD_DATA,
            thread_state="open",
        )
        b_id = b_result["memory"]["id"]
        a_row = self._memory_row(a_id)
        b_row_db = self._memory_row(b_id)
        self.assertFalse(a_row[2])           # A 失活
        self.assertEqual(a_row[6], b_id)     # A.superseded_by = B
        self.assertTrue(b_row_db[2])         # B 生效
        self.assertEqual(b_row_db[5], a_id)  # B.supersedes = A
        self.assertEqual(b_row_db[4], "manual")
        self.assertEqual(b_row_db[8], continuity_id)  # 沿用同一连续感身份

        c_result = self._change_type(
            b_id, "赶海经历：约好下周三清晨出发。", "episode", EPISODE_DATA,
        )
        c_id = c_result["memory"]["id"]
        # A 属于更早版本，必须被物理清理；只保留 B 和 C。
        self.assertIsNone(self._memory_row(a_id))
        self.assertFalse(self._memory_row(b_id)[2])
        self.assertEqual(self._memory_row(b_id)[6], c_id)
        self.assertTrue(self._memory_row(c_id)[2])
        self.assertEqual(self._memory_row(c_id)[5], b_id)
        remaining = self._one(
            "select count(*) from public.memories where continuity_id = %s",
            (continuity_id,),
        )
        self.assertEqual(remaining, 2)

    def test_type_change_failure_rolls_back_completely(self):
        current_id = self._own_chain_base("回滚测试起点：记忆结构完整有效。")
        before = self._one("select count(*) from public.memories")
        self._expect_rpc_error(
            "select public.change_memory_type_v1(%s, %s, %s, null, '{}', 6, null, null, null,"
            " null, '{}', null::extensions.vector, 'no_such_type', null, '{}', null)",
            (current_id, "类型非法时整个事务必须回滚。", _sha256("类型非法时整个事务必须回滚。")),
            "admin_memory_invalid_type",
        )
        self.assertEqual(self._one("select count(*) from public.memories"), before)
        self.assertTrue(self._memory_row(current_id)[2])

    def test_same_content_as_other_memory_is_refused(self):
        current_id = self._own_chain_base("内容查重测试起点：原创正文。")
        self._expect_rpc_error(
            "select public.change_memory_type_v1(%s, %s, %s, null, '{}', 6, null, null, null,"
            " null, '{}', null::extensions.vector, 'episode', null, %s, null)",
            (current_id,
             "叶子和栖约好下周三去海边赶海。",
             _sha256("叶子和栖约好下周三去海边赶海。"),
             json.dumps(EPISODE_DATA, ensure_ascii=False)),
            "admin_memory_content_exists",
        )

    # -- 1b. duplicate content on manual create ------------------------------

    def test_create_rejects_duplicate_content_with_stable_error(self):
        existing = "这条正文已经存在于正式记忆库中，不能重复新增。"
        self._create(existing, "moment", MOMENT_DATA)
        rows_before = self._one("select count(*) from public.memories")
        objects_before = self._one("select count(*) from public.memory_continuity_objects")
        original_hash = self._query(
            "select content_hash from public.memories where content = %s",
            (existing,),
        )[0][0]

        self._expect_rpc_error(
            "select public.create_admin_memory_v1(%s, %s, %s, null, '{}', 6, null,"
            " null, null, null, '{}', null::extensions.vector, 'thread', 'open', %s, null)",
            (ASSISTANT, existing, _sha256(existing),
             json.dumps(THREAD_DATA, ensure_ascii=False)),
            "admin_memory_content_exists",
        )

        self.assertEqual(self._one("select count(*) from public.memories"), rows_before)
        self.assertEqual(
            self._one("select count(*) from public.memory_continuity_objects"),
            objects_before,
            "被拒绝的新增不得留下孤立的连续感对象",
        )
        self.assertEqual(
            self._query(
                "select content_hash from public.memories where content = %s",
                (existing,),
            )[0][0],
            original_hash,
            "原记忆哈希必须原样保留",
        )

        # 不同正文仍然可以正常新增。
        created = self._create("正文完全不同的新记忆可以正常写入。", "moment", MOMENT_DATA)
        self.assertIsNotNone(created["memory_id"])

    # -- 2b. unchanged-content type changes and hash handover --------------

    def test_type_change_with_unchanged_content_succeeds(self):
        content = "只改类型不改正文：哈希在同一事务内交接。"
        a_id = self._create(content, "moment", MOMENT_DATA)["memory_id"]
        real_hash = _sha256(content)

        b_result = self._change_type(
            a_id, content, "thread", THREAD_DATA, thread_state="open"
        )
        b_id = b_result["memory"]["id"]

        a_row = self._memory_row(a_id)
        self.assertFalse(a_row[2], "旧版本应失活")
        a_hash = self._query(
            "select content_hash from public.memories where id = %s", (a_id,))[0][0]
        self.assertIsNone(a_hash, "同内容改型后，失活源版本的哈希应为 NULL")
        b_hash = self._query(
            "select content_hash from public.memories where id = %s", (b_id,))[0][0]
        self.assertEqual(b_hash, real_hash, "新版本必须持有真实正文哈希")
        self.assertTrue(self._memory_row(b_id)[2])

        result = self._as_service_role(
            "select public.undo_memory_type_change_v1(%s)", (b_id,)
        )[0][0]
        self.assertTrue(result["undo_deleted"])
        self.assertIsNone(self._memory_row(b_id), "撤销后新版本被物理删除")
        a_after = self._query(
            "select is_active, content_hash from public.memories where id = %s", (a_id,)
        )[0]
        self.assertTrue(a_after[0], "撤销后上一版本恢复有效")
        self.assertEqual(a_after[1], real_hash, "恢复的上一版本收回正确正文哈希")

    def test_type_change_chain_abc_unchanged_content_keeps_two_generations(self):
        content = "A 到 B 到 C 正文始终不变的版本链。"
        real_hash = _sha256(content)
        a_id = self._create(content, "moment", MOMENT_DATA)["memory_id"]
        b_id = self._change_type(
            a_id, content, "thread", THREAD_DATA, thread_state="open"
        )["memory"]["id"]
        c_id = self._change_type(
            b_id, content, "episode", EPISODE_DATA
        )["memory"]["id"]

        self.assertIsNone(self._memory_row(a_id), "更早版本 A 必须被清理")
        continuity_id = self._memory_row(c_id)[8]
        remaining = self._query(
            "select id, is_active, content_hash from public.memories"
            " where continuity_id = %s order by id",
            (continuity_id,),
        )
        self.assertEqual([row[0] for row in remaining], [b_id, c_id])
        self.assertFalse(remaining[0][1])
        self.assertIsNone(remaining[0][2], "中间版本哈希让渡后保持 NULL")
        self.assertTrue(remaining[1][1])
        self.assertEqual(remaining[1][2], real_hash)

        self._as_service_role("select public.undo_memory_type_change_v1(%s)", (c_id,))
        b_after = self._query(
            "select is_active, content_hash from public.memories where id = %s", (b_id,)
        )[0]
        self.assertTrue(b_after[0])
        self.assertEqual(b_after[1], real_hash, "撤销后中间版本收回真实哈希")

    def test_unrelated_content_hash_occupation_still_rejected(self):
        occupied = "无关记忆已经占用的正文内容，其他记忆不能复用。"
        self._create(occupied, "moment", MOMENT_DATA)
        m_id = self._own_chain_base("内容查重测试的起点记忆，正文完全不同。")
        self._expect_rpc_error(
            "select public.change_memory_type_v1(%s, %s, %s, null, '{}', 6, null,"
            " null, null, null, '{}', null::extensions.vector, 'episode', null, %s, null)",
            (m_id, occupied, _sha256(occupied),
             json.dumps(EPISODE_DATA, ensure_ascii=False)),
            "admin_memory_content_exists",
        )

    def test_type_change_failure_rolls_back_hash_release(self):
        # 异常夹具：X 持有哈希且 supersedes 指向仍然有效的 P（非正常链状态）。
        # 同哈希改型会在释放哈希之后才撞上链冲突，整个事务必须回滚。
        content = "回滚测试：哈希释放必须在失败时一并回滚。"
        real_hash = _sha256(content)
        p_id = self._create(content, "moment", MOMENT_DATA)["memory_id"]
        self.conn.execute(
            "update public.memories set content_hash = null where id = %s", (p_id,))
        self.conn.execute(
            "insert into public.memory_continuity_objects"
            " (continuity_id, assistant_id) values"
            " ('33333333-3333-3333-3333-3333333333a2', %s)",
            (ASSISTANT,),
        )
        x_id = self._one(
            """
            insert into public.memories (
                content, source, verified, is_active, assistant_id, confidence,
                content_hash, supersedes_memory_id, continuity_id, continuity_type,
                continuity_schema_version, continuity_data
            ) values (
                %s, 'daily_digest', 'verified', true, %s, 1.0, %s, %s,
                '33333333-3333-3333-3333-3333333333a2', 'episode', 1, %s
            ) returning id
            """,
            (content, ASSISTANT, real_hash, p_id,
             json.dumps(EPISODE_DATA, ensure_ascii=False)),
        )
        self._expect_rpc_error(
            "select public.change_memory_type_v1(%s, %s, %s, null, '{}', 6, null,"
            " null, null, null, '{}', null::extensions.vector, 'thread', 'open', %s, null)",
            (x_id, content, real_hash, json.dumps(THREAD_DATA, ensure_ascii=False)),
            "admin_memory_chain_conflict",
        )
        x_row = self._query(
            "select is_active, content_hash from public.memories where id = %s", (x_id,)
        )[0]
        self.assertTrue(x_row[0], "失败回滚后源版本仍然有效")
        self.assertEqual(x_row[1], real_hash, "失败回滚后源版本哈希原样保留")

    def test_restore_rejects_when_parent_row_is_active(self):
        # X 失活且 supersedes 指向仍有效的 P：恢复 X 会造成同链双有效版本。
        # 冲突判断必须检查父版本行本身是否 active+verified。
        p_id = self._create("恢复链冲突测试的父版本记忆，保持有效。", "moment", MOMENT_DATA)["memory_id"]
        self.conn.execute(
            "insert into public.memory_continuity_objects"
            " (continuity_id, assistant_id) values"
            " ('33333333-3333-3333-3333-3333333333a3', %s)",
            (ASSISTANT,),
        )
        x_id = self._one(
            """
            insert into public.memories (
                content, source, verified, is_active, assistant_id, confidence,
                content_hash, supersedes_memory_id, continuity_id, continuity_type,
                continuity_schema_version, continuity_data
            ) values (
                '恢复链冲突测试的失活子版本。', 'daily_digest', 'verified', false,
                %s, 1.0, %s, %s, '33333333-3333-3333-3333-3333333333a3',
                'episode', 1, %s
            ) returning id
            """,
            (ASSISTANT, _sha256("恢复链冲突测试的失活子版本。"), p_id,
             json.dumps(EPISODE_DATA, ensure_ascii=False)),
        )
        self._expect_rpc_error(
            "select public.restore_archived_memory_v1(%s)", (x_id,),
            "admin_memory_chain_conflict",
        )

    # -- 2c. dedicated archive endpoint -------------------------------------

    def test_archive_current_version_and_conflicts(self):
        memory_id = self._create("归档接口测试记忆：正常归档后再恢复。", "moment", MOMENT_DATA)["memory_id"]

        row = self._as_service_role(
            "select public.archive_admin_memory_v1(%s)", (memory_id,)
        )[0][0]
        self.assertFalse(row["memory"]["is_active"])
        # 归档只翻 is_active，不动热度。
        heat = self._query("select heat from public.memories where id = %s", (memory_id,))[0][0]
        self.assertEqual(heat, 50.0)

        self._expect_rpc_error(
            "select public.archive_admin_memory_v1(%s)", (memory_id,),
            "admin_memory_already_archived",
        )
        self._as_service_role("select public.restore_archived_memory_v1(%s)", (memory_id,))
        restored = self._query(
            "select is_active, heat from public.memories where id = %s", (memory_id,)
        )[0]
        self.assertTrue(restored[0])
        self.assertEqual(restored[1], 50.0)

        # 被替代旧版本不能归档。
        b_id = self._change_type(
            memory_id, "被替代版本不能归档的新线索版本。", "thread", THREAD_DATA,
            thread_state="open",
        )["memory"]["id"]
        self._expect_rpc_error(
            "select public.archive_admin_memory_v1(%s)", (memory_id,),
            "admin_memory_superseded",
        )
        self.assertTrue(self._memory_row(b_id)[2])

        # 非正式记录（未确认）不能归档。
        self.conn.execute(
            "insert into public.memory_continuity_objects"
            " (continuity_id, assistant_id) values"
            " ('33333333-3333-3333-3333-3333333333a4', %s)",
            (ASSISTANT,),
        )
        pending_id = self._one(
            """
            insert into public.memories (
                content, source, verified, is_active, assistant_id, confidence,
                content_hash, continuity_id, continuity_type,
                continuity_schema_version, continuity_data
            ) values (
                '未确认状态的记忆不能直接归档。', 'daily_digest', 'pending', true,
                %s, 1.0, %s, '33333333-3333-3333-3333-3333333333a4',
                'moment', 1, %s
            ) returning id
            """,
            (ASSISTANT, _sha256("未确认状态的记忆不能直接归档。"),
             json.dumps(MOMENT_DATA, ensure_ascii=False)),
        )
        self._expect_rpc_error(
            "select public.archive_admin_memory_v1(%s)", (pending_id,),
            "admin_memory_not_archivable",
        )

    # -- 3. undo -----------------------------------------------------------

    def test_undo_restores_direct_parent_exactly_once(self):
        a_id = self._own_chain_base("撤销测试起点：先建立可撤销的版本对。")
        b_result = self._change_type(
            a_id, "撤销流程：先改成线索。", "thread", THREAD_DATA, thread_state="open",
        )
        b_id = b_result["memory"]["id"]
        result = self._as_service_role(
            "select public.undo_memory_type_change_v1(%s)", (b_id,)
        )[0][0]
        self.assertEqual(result["restored_memory_id"], a_id)
        self.assertTrue(result["undo_deleted"])
        self.assertIsNone(self._memory_row(b_id), "撤销后新版本应被物理删除")
        a_row = self._memory_row(a_id)
        self.assertTrue(a_row[2])
        self.assertIsNone(a_row[6])
        self.assertIsNone(a_row[7])
        # 只能撤销最近一次：A 现在没有上一版本，再次撤销被拒绝。
        self._expect_rpc_error(
            "select public.undo_memory_type_change_v1(%s)", (a_id,),
            "admin_memory_no_previous_version",
        )

    def test_undo_rejects_non_manual_versions(self):
        a_id = self._own_chain_base("非手工来源测试起点：日常总结记忆。")
        self.conn.execute("update public.memories set source = 'daily_digest' where id = %s", (a_id,))
        # 非 manual 来源即使存在替代关系也不是类型修改版本。
        self._expect_rpc_error(
            "select public.undo_memory_type_change_v1(%s)", (a_id,),
            "admin_memory_no_previous_version",
        )

    # -- 3b. undo fallback branch (defensive) --------------------------------

    def test_undo_fallback_archives_when_hard_foreign_key_blocks_delete(self):
        # 生产所有引用 memories 的外键均为 ON DELETE SET NULL，正常结构下
        # 物理删除不会被阻止；此用例在临时库显式构造一个限制删除的硬外键，
        # 验证防御分支：删除抛 foreign_key_violation -> 新版本转归档并释放
        # 哈希，上一版本恢复并收回真实哈希，全程不出现重复非 NULL 哈希。
        content = "撤销回退分支：硬外键阻止物理删除时转归档。"
        real_hash = _sha256(content)
        a_id = self._create(content, "moment", MOMENT_DATA)["memory_id"]
        b_id = self._change_type(
            a_id, content, "thread", THREAD_DATA, thread_state="open"
        )["memory"]["id"]

        self.conn.execute(
            "create table public.undo_pin (id integer primary key,"
            " pinned_memory integer not null references public.memories(id)"
            " on delete restrict)"
        )
        try:
            self.conn.execute(
                "insert into public.undo_pin values (1, %s)", (b_id,))
            result = self._as_service_role(
                "select public.undo_memory_type_change_v1(%s)", (b_id,)
            )[0][0]
            self.assertFalse(result["undo_deleted"], "物理删除被阻止时应转归档")
            b_row = self._memory_row(b_id)
            self.assertFalse(b_row[2], "新版本转归档后失活")
            self.assertIsNone(b_row[5], "转归档版本解除与上一版本的替代关系")
            b_hash = self._query(
                "select content_hash from public.memories where id = %s", (b_id,))[0][0]
            self.assertIsNone(b_hash, "归档新版本必须先释放哈希")
            a_row = self._query(
                "select is_active, content_hash from public.memories where id = %s",
                (a_id,),
            )[0]
            self.assertTrue(a_row[0], "上一版本恢复有效")
            self.assertEqual(a_row[1], real_hash, "上一版本收回真实哈希")
            duplicates = self._one(
                "select count(*) from (select content_hash from public.memories"
                " where content_hash is not null group by content_hash"
                " having count(*) > 1) as dup"
            )
            self.assertEqual(duplicates, 0, "不能出现两个非 NULL 的相同哈希")
        finally:
            self.conn.execute("drop table public.undo_pin")

    def test_undo_non_fk_failure_rolls_back_completely(self):
        # 非 foreign_key_violation 的数据库异常必须完整回滚，绝不转成归档成功。
        content = "撤销的其他数据库异常必须完整回滚。"
        a_id = self._create(content, "moment", MOMENT_DATA)["memory_id"]
        b_id = self._change_type(
            a_id, content, "thread", THREAD_DATA, thread_state="open"
        )["memory"]["id"]

        self.conn.execute(
            """
            create function public.undo_blocker() returns trigger
            language plpgsql as $fn$
            begin
                raise exception 'undo blocked by non-fk failure';
            end;
            $fn$;
            """
        )
        self.conn.execute(
            "create trigger block_undo_delete before delete on public.memories"
            " for each row execute function public.undo_blocker()"
        )
        try:
            with self.assertRaises(psycopg.errors.RaiseException):
                self._as_service_role(
                    "select public.undo_memory_type_change_v1(%s)", (b_id,)
                )
            # 完整回滚：B 仍有效并持有哈希，A 仍失活且哈希为 NULL。
            self.assertTrue(self._memory_row(b_id)[2])
            b_hash = self._query(
                "select content_hash from public.memories where id = %s", (b_id,))[0][0]
            self.assertEqual(b_hash, _sha256(content))
            self.assertFalse(self._memory_row(a_id)[2])
            self.assertIsNone(self._query(
                "select content_hash from public.memories where id = %s", (a_id,))[0][0])
        finally:
            self.conn.execute("drop trigger block_undo_delete on public.memories")
            self.conn.execute("drop function public.undo_blocker()")

    # -- 4. natural archive restore ----------------------------------------

    def test_restore_resets_heat_and_rejects_conflicts(self):
        result = self._create("归档恢复流程测试记忆：先归档再恢复。", "moment", MOMENT_DATA)
        memory_id = result["memory_id"]
        self.conn.execute(
            "update public.memories set is_active = false, heat = 3 where id = %s",
            (memory_id,),
        )
        result = self._as_service_role(
            "select public.restore_archived_memory_v1(%s)", (memory_id,)
        )[0][0]
        self.assertTrue(result["memory"]["is_active"])
        restored = self._query(
            "select is_active, heat from public.memories where id = %s", (memory_id,)
        )[0]
        self.assertTrue(restored[0])
        self.assertEqual(restored[1], 50.0)

        # 被替代旧版本禁止恢复：类型修改后原行已自动失活。
        current_id = self._own_chain_base("归档冲突测试起点：改类型后原行失活。")
        b_result = self._change_type(
            current_id, "归档冲突测试的新线索版本。", "thread", THREAD_DATA, thread_state="open",
        )
        self._expect_rpc_error(
            "select public.restore_archived_memory_v1(%s)", (current_id,),
            "admin_memory_superseded",
        )

        # 同一连续感身份出现双有效版本时拒绝恢复。
        continuity_id = self._memory_row(b_result["memory"]["id"])[8]
        self.conn.execute(
            """
            insert into public.memories (
                content, source, verified, is_active, assistant_id, confidence,
                content_hash, continuity_id, continuity_type, thread_state,
                continuity_schema_version, continuity_data
            ) values (
                '同身份的重复归档行，恢复会造成双有效版本。',
                'daily_digest', 'verified', false, %s, 1.0, %s,
                %s, 'thread', 'open', 1, %s
            )
            """,
            (ASSISTANT, _sha256("同身份的重复归档行，恢复会造成双有效版本。"),
             continuity_id, json.dumps(THREAD_DATA, ensure_ascii=False)),
        )
        duplicate_id = self._one(
            "select id from public.memories where content_hash = %s",
            (_sha256("同身份的重复归档行，恢复会造成双有效版本。"),),
        )
        self._expect_rpc_error(
            "select public.restore_archived_memory_v1(%s)", (duplicate_id,),
            "admin_memory_continuity_conflict",
        )

    # -- 4b. event time normalization ----------------------------------------

    def test_naive_memory_time_is_interpreted_as_asia_shanghai(self):
        row = self._create("无时区时间按上海规则解释的记忆。", "moment", MOMENT_DATA,
                           memory_time="2026-08-19 03:11", time_precision="minute")["memory_id"]
        shanghai_wall = self._query(
            "select to_char(memory_time at time zone 'Asia/Shanghai',"
            " 'YYYY-MM-DD HH24:MI:SS') from public.memories where id = %s",
            (row,),
        )[0][0]
        self.assertEqual(shanghai_wall, "2026-08-19 03:11:00")

    def test_explicit_offsets_are_honoured_exactly(self):
        row = self._create("带明确偏移的时间按其偏移解释。", "moment", MOMENT_DATA,
                           memory_time="2026-08-19T03:11:00+08:00",
                           time_precision="minute")["memory_id"]
        utc_wall = self._query(
            "select to_char(memory_time at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
            " from public.memories where id = %s",
            (row,),
        )[0][0]
        self.assertEqual(utc_wall, "2026-08-18 19:11:00")

        row_z = self._create("Z 结尾的时间同样按明确时刻解释。", "moment", MOMENT_DATA,
                             memory_time="2026-08-18T19:11:00Z",
                             time_precision="minute")["memory_id"]
        utc_wall_z = self._query(
            "select to_char(memory_time at time zone 'UTC', 'YYYY-MM-DD HH24:MI:SS')"
            " from public.memories where id = %s",
            (row_z,),
        )[0][0]
        self.assertEqual(utc_wall_z, "2026-08-18 19:11:00")

    def test_invalid_memory_time_returns_stable_error(self):
        self._expect_rpc_error(
            "select public.create_admin_memory_v1(%s, %s, %s, null, '{}', 6, null,"
            " %s, %s, null, '{}', null::extensions.vector, 'moment', null, %s, null)",
            (ASSISTANT, "非法时间格式必须返回稳定错误码。", _sha256("非法时间格式必须返回稳定错误码。"),
             "not-a-valid-time", "minute",
             json.dumps(MOMENT_DATA, ensure_ascii=False)),
            "admin_memory_invalid_memory_time",
        )

    def test_semantically_invalid_times_return_stable_error(self):
        # 形状合法但语义无效的日期/时间（2 月 30 日、13 月、25 点、99:99 偏移）
        # 必须映射为稳定的 admin_memory_invalid_memory_time，而不是原始 500。
        rows_before = self._one("select count(*) from public.memories")
        objects_before = self._one("select count(*) from public.memory_continuity_objects")
        for invalid_time, precision in (
            ("2026-02-30", "day"),
            ("2026-13-01", "day"),
            ("2026-09-01T25:00:00+08:00", "minute"),
            ("2026-09-01T12:00:00+99:99", "minute"),
        ):
            with self.subTest(invalid_time=invalid_time):
                self._expect_rpc_error(
                    "select public.create_admin_memory_v1(%s, %s, %s, null, '{}', 6,"
                    " null, %s, %s, null, '{}', null::extensions.vector,"
                    " 'moment', null, %s, null)",
                    (ASSISTANT, "语义无效的时间必须返回稳定错误码。",
                     _sha256(f"语义无效的时间必须返回稳定错误码。{invalid_time}"),
                     invalid_time, precision,
                     json.dumps(MOMENT_DATA, ensure_ascii=False)),
                    "admin_memory_invalid_memory_time",
                )
        self.assertEqual(self._one("select count(*) from public.memories"), rows_before)
        self.assertEqual(
            self._one("select count(*) from public.memory_continuity_objects"),
            objects_before,
            "被拒绝的新增不得留下连续感对象",
        )

        # 合法值继续正常通过：日期、无时区日期时间、+08:00、Z。
        for legal_time, precision in (
            ("2026-08-19", "day"),
            ("2026-08-19 03:11", "minute"),
            ("2026-08-19T03:11:00+08:00", "minute"),
            ("2026-08-18T19:11:00Z", "minute"),
        ):
            with self.subTest(legal_time=legal_time):
                created = self._create(
                    f"合法时间 {legal_time} 正常写入。", "moment", MOMENT_DATA,
                    memory_time=legal_time, time_precision=precision,
                )
                self.assertIsNotNone(created["memory_id"])

    def test_empty_time_forces_unknown_precision_on_every_path(self):
        # 新增：空时间 + minute -> unknown。
        created = self._create("空时间不声称分钟精度的记忆一。", "moment", MOMENT_DATA,
                               memory_time="", time_precision="minute")["memory_id"]
        self.assertEqual(
            self._query("select time_precision, memory_time from public.memories where id = %s",
                        (created,))[0],
            ("unknown", None),
        )
        # 新增：空时间 + approximate -> unknown（近似于不存在的时间同样归一）。
        created2 = self._create("空时间不声称近似精度的记忆二。", "moment", MOMENT_DATA,
                                memory_time="", time_precision="approximate")["memory_id"]
        self.assertEqual(
            self._query("select time_precision from public.memories where id = %s",
                        (created2,))[0][0],
            "unknown",
        )
        # 有时间 + minute / approximate 正常保留。
        with_time = self._create("有时间且精度为分钟的记忆。", "moment", MOMENT_DATA,
                                 memory_time="2026-08-19 03:11",
                                 time_precision="minute")["memory_id"]
        self.assertEqual(
            self._query("select time_precision from public.memories where id = %s",
                        (with_time,))[0][0],
            "minute",
        )
        with_time2 = self._create("有时间且精度为大概的记忆。", "moment", MOMENT_DATA,
                                  memory_time="2026-08-19 03:11",
                                  time_precision="approximate")["memory_id"]
        self.assertEqual(
            self._query("select time_precision from public.memories where id = %s",
                        (with_time2,))[0][0],
            "approximate",
        )

        # 类型修改：空时间 + day -> unknown。
        b_id = self._change_type(
            created, "空时间类型修改同样保存 unknown 精度。", "thread", THREAD_DATA,
            thread_state="open",
        )["memory"]["id"]
        self.assertEqual(
            self._query("select time_precision, memory_time from public.memories where id = %s",
                        (b_id,))[0],
            ("unknown", None),
        )

        # 编辑：清空时间后精度归一为 unknown；有时间 + approximate 保留。
        self.conn.execute(
            "update public.memories set memory_time = '2026-08-19 08:00+08',"
            " time_precision = 'minute' where id = %s",
            (created2,),
        )
        self._as_service_role(
            "select public.edit_admin_memory_v1(%s, %s, null, null::extensions.vector, %s)",
            (created2, json.dumps({"memory_time": None}, ensure_ascii=False), ASSISTANT),
        )
        self.assertEqual(
            self._query("select time_precision, memory_time from public.memories where id = %s",
                        (created2,))[0],
            ("unknown", None),
        )
        self._as_service_role(
            "select public.edit_admin_memory_v1(%s, %s, null, null::extensions.vector, %s)",
            (created2, json.dumps({"memory_time": "2026-08-19 09:30",
                                    "time_precision": "approximate"}, ensure_ascii=False),
             ASSISTANT),
        )
        self.assertEqual(
            self._query("select time_precision from public.memories where id = %s",
                        (created2,))[0][0],
            "approximate",
        )

    # -- 5. permissions & immutability --------------------------------------

    def test_anon_and_authenticated_cannot_execute(self):
        for role in ("anon", "authenticated"):
            with self.subTest(role=role):
                self.conn.execute(f"set role {role}")
                try:
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                        with self.conn.cursor() as cur:
                            cur.execute("select public.undo_memory_type_change_v1(1)")
                finally:
                    self.conn.execute("reset role")

    def test_chat_messages_stay_untouched(self):
        self.assertEqual(
            self._one("select count(*) from public.chat_messages"),
            self.chat_count_before,
        )


if __name__ == "__main__":
    unittest.main()
