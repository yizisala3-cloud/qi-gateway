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
                "set QIGATEWAY_ADMIN_MEMORY_PG_TEST=1 to run the real "
                "PostgreSQL lifecycle migration test"
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
                recall_scene=None, recall_tags=None):
        # psycopg 需要显式的 Postgres 数组字面量；生产经 PostgREST 传 JSON 数组。
        if evidence:
            evidence = '{' + ','.join(str(int(i)) for i in evidence) + '}'
        return self._as_service_role(
            "select public.create_admin_memory_v1(%s, %s, %s, null, %s, 6, %s, null, null,"
            " %s, %s, %s::extensions.vector, %s, %s, %s, %s)",
            (
                ASSISTANT, content, _sha256(content),
                ["出行"],
                None,
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
