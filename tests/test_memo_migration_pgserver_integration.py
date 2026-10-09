"""备忘录一期迁移与 RPC 的真 PostgreSQL 集成验证（opt-in）。

启动一次性 pgserver 实例成本较高，默认跳过：

    QIGATEWAY_PG_MEMO_TEST=1 python -m pytest \\
        tests/test_memo_migration_pgserver_integration.py -v

按顺序重放全部迁移（被测对象为 20261002060000_memo_phase1.sql），随后在
真实 PostgreSQL 上验证数据库层的业务不变量：

- 迁移重放成功；「未分类」分组单例行存在且唯一；
- 创建原子性：未知标签 → ME005 整体回滚、零写入；
- 创建幂等：同 client_request_id 重试返回首次记录；
- 乐观并发：陈旧版本 ME002；归档后迟到自动保存被拒（ME003）、状态不被复活；
- 位置语义：常驻各区末尾补位、latest 随笔不落位、改正文不改位次；
- 重排：随笔拖动翻手动模式、切回最新保留位次、再切手动恢复、成员集合
  不一致 ME004（含真实双连接锁竞争场景）、手动模式新增成员落末尾；
- 用途切换重定位；删除标签把失去最后标签的活跃记录转入未分类；
- 标签重名幂等返回既有行。

被测迁移文件逐字节从磁盘执行；测试绝不触碰任何生产数据库。
"""

import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 复用 phase1r 套件的同一基线骨架与工具（其文档要求两份 BASELINE_SQL 保持
# 一致；经模块导入共用，避免第三份副本漂移）。该模块在缺 pgserver/psycopg
# 时安全降级为 _STACK_AVAILABLE=False。
_spec = importlib.util.spec_from_file_location(
    "_planning_pg_base",
    ROOT / "tests" / "test_planning_phase1r_pgserver_integration.py",
)
_base = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_base)

_UNDER_TEST = "20261002060000_memo_phase1.sql"
NOW = "2026-10-02T09:00:00+08:00"


def _create_entry_sql(payload):
    return "select public.memo_create_entry(%s::jsonb, %s::timestamptz) as out", [
        json.dumps(payload), NOW,
    ]


def _update_entry_sql(entry_id, expected_version, patch):
    return ("select public.memo_update_entry(%s, %s, %s::jsonb, %s::timestamptz) as out",
            [entry_id, expected_version, json.dumps(patch), NOW])


def _lifecycle_sql(entry_id, action, expected_version):
    return ("select public.memo_set_entry_lifecycle(%s, %s, %s, %s::timestamptz) as out",
            [entry_id, action, expected_version, NOW])


def _lifecycle_at_sql(entry_id, action, expected_version, now):
    return ("select public.memo_set_entry_lifecycle(%s, %s, %s, %s::timestamptz) as out",
            [entry_id, action, expected_version, now])


def _reorder_group_sql(tag_id, section, order):
    scope = {"untagged": True} if tag_id is None else {"tag_id": tag_id}
    return ("select public.memo_reorder_group(%s::jsonb, %s, %s::jsonb, %s::timestamptz) as out",
            [json.dumps(scope), section, json.dumps(order), NOW])


def _note_mode_sql(tag_id, mode):
    scope = {"untagged": True} if tag_id is None else {"tag_id": tag_id}
    return "select public.memo_set_note_mode(%s::jsonb, %s, %s::timestamptz) as out", [
        json.dumps(scope), mode, NOW,
    ]


class MemoPhase1PostgresTests(unittest.TestCase):
    """在真实 PostgreSQL 上验证备忘录迁移与 RPC 不变量。"""

    @classmethod
    def setUpClass(cls):
        import psycopg

        if not _base._STACK_AVAILABLE:
            raise unittest.SkipTest("pgserver + psycopg are not installed")
        if os.environ.get("QIGATEWAY_PG_MEMO_TEST") != "1":
            raise unittest.SkipTest(
                "set QIGATEWAY_PG_MEMO_TEST=1 (or run pytest --db) to run "
                "the real PostgreSQL memo test"
            )
        cls.pgdata = Path(tempfile.mkdtemp(prefix="qigate-memo-pg-"))
        cls.server = None
        cls.conn = None
        try:
            _base._ensure_pg_timezone_data()
            cls.server = _base.pgserver.get_server(cls.pgdata, cleanup_mode="stop")
            cls.conn = psycopg.connect(cls.server.get_uri(), autocommit=True)
            cls.conn.execute(_base.BASELINE_SQL)
            replayed = False
            for path in sorted(_base.MIGRATIONS_DIR.glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                if path.name == _base.PG_TRGM_FILE:
                    assert _base.PG_TRGM_LINE in sql, "pg_trgm line drifted"
                    sql = sql.replace(_base.PG_TRGM_LINE, "-- pg_trgm stubbed (pgserver)")
                cls.conn.execute(sql)
                if path.name == _UNDER_TEST:
                    replayed = True
            assert replayed, f"{_UNDER_TEST} was not replayed"
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

    @classmethod
    def _query(cls, sql, params=None):
        with cls.conn.cursor() as cur:
            cur.execute(sql, params)
            if cur.description:
                return cur.fetchall()
            return []

    @classmethod
    def _call(cls, sql_and_params):
        sql, params = sql_and_params
        return cls._query(sql, params)[0][0]

    def _expect_errcode(self, sqlstate, sql_and_params, msg_fragment=None):
        """执行并断言以指定 SQLSTATE 失败（自定义 errcode 的映射随 psycopg
        版本可能是 RaiseException 或其他 Error 子类，统一按 sqlstate 断言）。"""
        import psycopg

        sql, params = sql_and_params
        with self.assertRaises(psycopg.Error) as caught:
            self._query(sql, params)
        self.assertEqual(caught.exception.sqlstate, sqlstate, repr(caught.exception))
        if msg_fragment is not None:
            self.assertIn(msg_fragment, str(caught.exception))
        return caught.exception

    @classmethod
    def _make_tag(cls, name):
        return cls._call(
            ("select public.memo_create_tag(%s, %s::timestamptz) as out", [name, NOW]))

    @classmethod
    def _make_entry(cls, payload):
        return cls._call(_create_entry_sql(payload))

    @classmethod
    def _positions(cls, tag_id):
        """某标签分组的 (entry_id, position) 列表，按 position 排序。"""
        return cls._query(
            """
            select p.entry_id, p.position
              from public.memo_position p
              join public.memo_group g on g.id = p.group_id
             where g.tag_id = %s
             order by p.position, p.entry_id
            """,
            (tag_id,),
        )

    @classmethod
    def _untagged_positions(cls):
        return cls._query(
            """
            select p.entry_id, p.position
              from public.memo_position p
              join public.memo_group g on g.id = p.group_id
             where g.tag_id is null
             order by p.position, p.entry_id
            """
        )

    @classmethod
    def _entry(cls, entry_id):
        from psycopg.rows import dict_row
        with cls.conn.cursor(row_factory=dict_row) as cur:
            cur.execute("select * from public.memo_entry where id = %s", (entry_id,))
            return cur.fetchone()

    @classmethod
    def _reset_untagged(cls):
        """本类共享一个数据库：清除未分类分组的内容与模式，保证断言确定性。"""
        cls._query(
            """
            delete from public.memo_entry e
             where not exists (
                select 1 from public.memo_entry_tag et where et.entry_id = e.id)
            """
        )
        cls._query(
            "update public.memo_group set note_sort_mode = 'latest' where tag_id is null"
        )

    def test_untagged_group_singleton_exists(self):
        rows = self._query("select id from public.memo_group where tag_id is null")
        self.assertEqual(len(rows), 1)

    def test_create_with_unknown_tag_rolls_back_atomically(self):
        before = self._query("select count(*) from public.memo_entry")[0][0]
        self._expect_errcode(
            "ME005",
            _create_entry_sql({"kind": "note", "content": "原子性", "tag_ids": [99999]}),
            "unknown tag",
        )
        after = self._query("select count(*) from public.memo_entry")[0][0]
        self.assertEqual(before, after)   # 违规零写入（单事务回滚）

    def test_create_entry_idempotent_by_client_request_id(self):
        first = self._make_entry({
            "kind": "note", "content": "幂等创建", "client_request_id": "crid-it-1"})
        second = self._make_entry({
            "kind": "note", "content": "幂等创建", "client_request_id": "crid-it-1"})
        self.assertEqual(first["id"], second["id"])
        count = self._query(
            "select count(*) from public.memo_entry where client_request_id = 'crid-it-1'"
        )[0][0]
        self.assertEqual(count, 1)

    def test_pinned_lands_at_end_of_each_tag_section(self):
        tag_a = self._make_tag("板块A")["id"]
        tag_b = self._make_tag("板块B")["id"]
        e1 = self._make_entry({
            "kind": "pinned", "content": "常驻一", "tag_ids": [tag_a, tag_b]})["id"]
        e2 = self._make_entry({
            "kind": "pinned", "content": "常驻二", "tag_ids": [tag_a]})["id"]
        self.assertEqual(self._positions(tag_a), [(e1, 1), (e2, 2)])
        self.assertEqual(self._positions(tag_b), [(e1, 1)])

    def test_note_in_latest_group_has_no_position(self):
        tag_a = self._make_tag("最新模式组")["id"]
        self._make_entry({"kind": "note", "content": "随笔", "tag_ids": [tag_a]})
        self.assertEqual(self._positions(tag_a), [])

    def test_stale_version_rejected_and_content_unchanged(self):
        entry = self._make_entry({"kind": "note", "content": "原始正文"})
        self._expect_errcode(
            "ME002", _update_entry_sql(entry["id"], 99, {"content": "陈旧保存"}),
            "stale content_version",
        )
        self.assertEqual(self._entry(entry["id"])["content"], "原始正文")
        self.assertEqual(self._entry(entry["id"])["content_version"], 1)

    def test_archived_entry_survives_late_autosave(self):
        entry = self._make_entry({"kind": "note", "content": "将被归档"})
        result = self._call(_lifecycle_sql(entry["id"], "archive", 1))
        self.assertEqual(result["status"], "archived")
        # 迟到的自动保存（无论携带旧版本还是碰巧正确的版本）都不能把
        # 已归档记录改回正常：状态门先于版本门生效
        self._expect_errcode(
            "ME003", _update_entry_sql(entry["id"], 1, {"content": "迟到自动保存"}))
        self._expect_errcode(
            "ME003", _update_entry_sql(entry["id"], 99, {"content": "陈旧自动保存"}))
        row = self._entry(entry["id"])
        self.assertEqual(row["status"], "archived")
        self.assertEqual(row["content"], "将被归档")
        # 恢复后版本继续递增，旧版本仍被拒绝
        self._call(_lifecycle_sql(entry["id"], "restore", result["content_version"]))
        self.assertEqual(self._entry(entry["id"])["status"], "active")
        self._expect_errcode(
            "ME002", _update_entry_sql(entry["id"], 1, {"content": "恢复前版本"}))

    def test_content_edit_keeps_manual_position(self):
        tag_a = self._make_tag("手排组")["id"]
        e1 = self._make_entry({"kind": "pinned", "content": "常驻", "tag_ids": [tag_a]})["id"]
        self._call(_reorder_group_sql(tag_a, "pinned", [e1]))
        self._call(_update_entry_sql(e1, 1, {"content": "改正文不改位次"}))
        self.assertEqual(self._positions(tag_a), [(e1, 1)])

    def test_note_drag_flips_manual_mode_and_materializes(self):
        tag_a = self._make_tag("拖动组")["id"]
        n1 = self._make_entry({
            "kind": "note", "content": "最早", "tag_ids": [tag_a],
            "client_request_id": "drag-n1"})["id"]
        n2 = self._make_entry({
            "kind": "note", "content": "中间", "tag_ids": [tag_a],
            "client_request_id": "drag-n2"})["id"]
        n3 = self._make_entry({
            "kind": "note", "content": "最新", "tag_ids": [tag_a],
            "client_request_id": "drag-n3"})["id"]
        # latest 模式下拖动：提交的顺序即手动落位，模式翻 manual
        result = self._call(_reorder_group_sql(tag_a, "note", [n3, n1, n2]))
        self.assertEqual(result["note_sort_mode"], "manual")
        self.assertEqual(self._positions(tag_a), [(n3, 1), (n1, 2), (n2, 3)])
        # 切回最新：位次保留；再次切回手动：原手动顺序恢复
        self._call(_note_mode_sql(tag_a, "latest"))
        self.assertEqual(self._positions(tag_a), [(n3, 1), (n1, 2), (n2, 3)])
        self._call(_note_mode_sql(tag_a, "manual"))
        self.assertEqual(self._positions(tag_a), [(n3, 1), (n1, 2), (n2, 3)])

    def test_new_note_in_manual_group_appends_at_end(self):
        tag_a = self._make_tag("末尾新增组")["id"]
        e1 = self._make_entry({
            "kind": "pinned", "content": "常驻", "tag_ids": [tag_a]})["id"]
        n1 = self._make_entry({"kind": "note", "content": "随笔一", "tag_ids": [tag_a]})["id"]
        self._call(_reorder_group_sql(tag_a, "note", [n1]))
        n2 = self._make_entry({"kind": "note", "content": "手动模式新随笔", "tag_ids": [tag_a]})["id"]
        self.assertEqual(self._positions(tag_a), [(e1, 1), (n1, 1), (n2, 2)])

    def test_set_note_mode_manual_materializes_latest_order_once(self):
        tag_a = self._make_tag("落位组")["id"]
        n1 = self._make_entry({
            "kind": "note", "content": "最早", "tag_ids": [tag_a],
            "client_request_id": "mat-n1"})["id"]
        n2 = self._make_entry({
            "kind": "note", "content": "中间", "tag_ids": [tag_a],
            "client_request_id": "mat-n2"})["id"]
        n3 = self._make_entry({
            "kind": "note", "content": "最新", "tag_ids": [tag_a],
            "client_request_id": "mat-n3"})["id"]
        self._call(_note_mode_sql(tag_a, "manual"))
        # 从未手排过：按当前最新顺序（创建倒序）落位
        self.assertEqual(self._positions(tag_a), [(n3, 1), (n2, 2), (n1, 3)])

    def test_reorder_member_mismatch_rejected_without_write(self):
        tag_a = self._make_tag("防陈旧组")["id"]
        n1 = self._make_entry({"kind": "note", "content": "一", "tag_ids": [tag_a]})["id"]
        n2 = self._make_entry({"kind": "note", "content": "二", "tag_ids": [tag_a]})["id"]
        self._call(_reorder_group_sql(tag_a, "note", [n2, n1]))
        self._expect_errcode(
            "ME004", _reorder_group_sql(tag_a, "note", [n2]), "member set changed")
        # 位次未被部分覆盖
        self.assertEqual(self._positions(tag_a), [(n2, 1), (n1, 2)])

    def test_multi_tag_positions_are_independent(self):
        tag_a = self._make_tag("独立A")["id"]
        tag_b = self._make_tag("独立B")["id"]
        e1 = self._make_entry({
            "kind": "pinned", "content": "多标签", "tag_ids": [tag_a, tag_b]})["id"]
        e2 = self._make_entry({"kind": "pinned", "content": "A第二", "tag_ids": [tag_a]})["id"]
        e3 = self._make_entry({"kind": "pinned", "content": "B第二", "tag_ids": [tag_b]})["id"]
        # 在 A 中把 e1 移到第二位：B 中 e1 的位置不动
        self._call(_reorder_group_sql(tag_a, "pinned", [e2, e1]))
        self.assertEqual(self._positions(tag_a), [(e2, 1), (e1, 2)])
        self.assertEqual(self._positions(tag_b), [(e1, 1), (e3, 2)])

    def test_kind_change_repositions_to_new_section_end(self):
        tag_a = self._make_tag("切换用途组")["id"]
        pinned = self._make_entry({
            "kind": "pinned", "content": "原常驻", "tag_ids": [tag_a]})["id"]
        note = self._make_entry({"kind": "note", "content": "原随笔", "tag_ids": [tag_a]})["id"]
        self._call(_reorder_group_sql(tag_a, "note", [note]))   # 手动模式，note pos1
        # pinned → note：落到随笔区末尾
        self._call(_update_entry_sql(pinned, 1, {"kind": "note"}))
        self.assertEqual(self._positions(tag_a), [(note, 1), (pinned, 2)])
        # note → pinned：常驻区此前已无成员，回到常驻区首位
        self._call(_update_entry_sql(pinned, 2, {"kind": "pinned"}))
        self.assertEqual(self._positions(tag_a), [(pinned, 1), (note, 1)])

    def test_delete_tag_moves_last_tag_losers_to_untagged(self):
        self._reset_untagged()
        tag_a = self._make_tag("将被删除")["id"]
        tag_b = self._make_tag("保留标签")["id"]
        orphan_pinned = self._make_entry({
            "kind": "pinned", "content": "孤儿常驻", "tag_ids": [tag_a]})["id"]
        orphan_note = self._make_entry({
            "kind": "note", "content": "孤儿随笔", "tag_ids": [tag_a]})["id"]
        keeper = self._make_entry({
            "kind": "pinned", "content": "仍有标签", "tag_ids": [tag_a, tag_b]})["id"]
        result = self._call(
            ("select public.memo_delete_tag(%s, %s::timestamptz) as out", [tag_a, NOW]))
        self.assertEqual(result["affected"], 2)
        # 随笔（未分类默认 latest 模式）不落位；常驻转入未分类末尾
        self.assertEqual(self._untagged_positions(), [(orphan_pinned, 1)])
        self.assertNotIn((keeper, 1), self._untagged_positions())
        # 标签被删除后不可再找到
        self.assertEqual(
            self._query("select count(*) from public.memo_tag where id = %s", (tag_a,))[0][0],
            0)

    def test_delete_tag_with_manual_untagged_group_positions_note(self):
        self._reset_untagged()
        tag_a = self._make_tag("未分类手动组")["id"]
        # 先让未分类分组进入手动模式（经一条无标签随笔触发落位）
        seed = self._make_entry({"kind": "note", "content": "未分类种子"})["id"]
        self._call(_reorder_group_sql(None, "note", [seed]))
        loser = self._make_entry({"kind": "note", "content": "失去标签", "tag_ids": [tag_a]})["id"]
        self._call(("select public.memo_delete_tag(%s, %s::timestamptz) as out", [tag_a, NOW]))
        # 手动模式下转入未分类 → 末尾补位
        self.assertEqual(self._untagged_positions(), [(seed, 1), (loser, 2)])

    def test_duplicate_tag_name_returns_existing(self):
        first = self._make_tag("重名标签")
        second = self._make_tag("重名标签")
        self.assertEqual(first["id"], second["id"])
        self.assertFalse(first["existed"])
        self.assertTrue(second["existed"])

    def test_reorder_tags_rejects_incomplete_set(self):
        tag_a = self._make_tag("板块序A")["id"]
        tag_b = self._make_tag("板块序B")["id"]
        all_ids = [row[0] for row in self._query("select id from public.memo_tag")]
        self._expect_errcode(
            "ME004",
            ("select public.memo_reorder_tags(%s::jsonb, %s::timestamptz) as out",
             [json.dumps([tag_a]), NOW]),
            "tag set changed",
        )
        # 全量一致才生效：tag_b 提到最前，其余标签保持相对顺序
        order = [tag_b, tag_a] + [i for i in all_ids if i not in (tag_a, tag_b)]
        result = self._call(
            ("select public.memo_reorder_tags(%s::jsonb, %s::timestamptz) as out",
             [json.dumps(order), NOW]))
        self.assertEqual(result["order"], order)

    def test_concurrent_reorder_detects_member_change_via_lock(self):
        """双连接：T1 持分组行锁并新增成员；T2 的重排等待后基于新快照复核
        成员集合 → ME004（不静默覆盖 T1 的并发新增）。"""
        import psycopg

        tag_a = self._make_tag("并发组")["id"]
        n1 = self._make_entry({"kind": "note", "content": "一", "tag_ids": [tag_a]})["id"]
        n2 = self._make_entry({"kind": "note", "content": "二", "tag_ids": [tag_a]})["id"]
        self._call(_reorder_group_sql(tag_a, "note", [n2, n1]))

        conn_a = psycopg.connect(self.server.get_uri())
        conn_b = psycopg.connect(self.server.get_uri())
        conn_b.execute("set statement_timeout = '10s'")
        try:
            conn_a.autocommit = False
            # T1：锁定分组行 + 新增一个成员（未提交）
            conn_a.execute("select public.memo_set_note_mode(%s::jsonb, 'manual', %s::timestamptz)",
                           (json.dumps({"tag_id": tag_a}), NOW))
            conn_a.execute(
                "select public.memo_create_entry(%s::jsonb, %s::timestamptz)",
                (json.dumps({"kind": "note", "content": "并发新增", "tag_ids": [tag_a]}), NOW))
            holder_ready = threading.Event()
            outcome = {}

            def t2_reorder():
                holder_ready.set()
                try:
                    with conn_b.cursor() as cur:
                        cur.execute(*_reorder_group_sql(tag_a, "note", [n2, n1]))
                        outcome["result"] = cur.fetchall()[0][0]
                except psycopg.Error as exc:
                    outcome["sqlstate"] = exc.sqlstate

            thread = threading.Thread(target=t2_reorder)
            thread.start()
            holder_ready.wait(5)
            time.sleep(0.3)          # 让 T2 先阻塞在分组行锁上
            self.assertNotIn("sqlstate", outcome)   # T2 尚未得到结果（在等锁）
            conn_a.commit()
            thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome.get("sqlstate"), "ME004",
                             f"unexpected outcome: {outcome}")
        finally:
            conn_a.close()
            conn_b.close()

    # ── F14：无标签创建按用途/模式在未分类分组登记末尾位次 ───────────

    def test_untagged_create_positions_pinned_and_manual_note(self):
        self._reset_untagged()
        e1 = self._make_entry({"kind": "pinned", "content": "未分类常驻"})["id"]
        n1 = self._make_entry({"kind": "note", "content": "未分类随笔"})["id"]
        self._call(_reorder_group_sql(None, "note", [n1]))   # 未分类随笔进入手动模式
        n2 = self._make_entry({"kind": "note", "content": "手动模式新随笔"})["id"]
        e2 = self._make_entry({"kind": "pinned", "content": "未分类常驻二"})["id"]
        # 常驻与手动模式随笔都有末尾位次；latest 模式随笔不落位（与标签分组一致）
        self.assertEqual(
            sorted(self._untagged_positions()),
            sorted([(e1, 1), (e2, 2), (n1, 1), (n2, 2)]),
        )

    def test_untagged_create_order_survives_late_transfer(self):
        self._reset_untagged()
        first = self._make_entry({"kind": "pinned", "content": "先建的未分类常驻"})["id"]
        tag_a = self._make_tag("F14转入标签")["id"]
        transferred = self._make_entry({
            "kind": "pinned", "content": "移除末标签转入", "tag_ids": [tag_a]})["id"]
        self._call(("select public.memo_delete_tag(%s, %s::timestamptz) as out", [tag_a, NOW]))
        # 转入记录排在既有未分类记录之后，不越过它（§4.2 末尾新增）
        self.assertEqual(self._untagged_positions(), [(first, 1), (transferred, 2)])

    # ── F15：用途切换按实际成员关系枚举分组并落新用途区末尾 ──────────

    def test_latest_note_to_pinned_keeps_position_for_future(self):
        tag_a = self._make_tag("F15转换组")["id"]
        pinned_a = self._make_entry({
            "kind": "pinned", "content": "A", "tag_ids": [tag_a]})["id"]
        note = self._make_entry({"kind": "note", "content": "B", "tag_ids": [tag_a]})["id"]
        # latest 随笔（无位次行）转常驻：在常驻区末尾获得位次
        self._call(_update_entry_sql(note, 1, {"kind": "pinned"}))
        pinned_c = self._make_entry({
            "kind": "pinned", "content": "C", "tag_ids": [tag_a]})["id"]
        # 后续新建常驻不越过转换记录：A/B/C
        self.assertEqual(self._positions(tag_a), [(pinned_a, 1), (note, 2), (pinned_c, 3)])

    def test_kind_change_covers_untagged_membership(self):
        self._reset_untagged()
        note = self._make_entry({"kind": "note", "content": "无标签随笔"})["id"]
        # 无标签（未分类）随笔转常驻：未分类分组同样获得位次
        self._call(_update_entry_sql(note, 1, {"kind": "pinned"}))
        self.assertEqual(self._untagged_positions(), [(note, 1)])

    # ── F16：不提交 kind 的合法部分 PATCH 使用记录原用途落位 ─────────

    def test_tag_only_patch_note_into_manual_group_appends_at_end(self):
        tag_a = self._make_tag("F16手动组")["id"]
        tag_b = self._make_tag("F16来源组")["id"]
        n1 = self._make_entry({"kind": "note", "content": "n1", "tag_ids": [tag_a]})["id"]
        n2 = self._make_entry({"kind": "note", "content": "n2", "tag_ids": [tag_a]})["id"]
        self._call(_reorder_group_sql(tag_a, "note", [n1, n2]))   # 手动模式 pos1/2
        mover = self._make_entry({
            "kind": "note", "content": "mover", "tag_ids": [tag_b]})["id"]
        # 不带 kind 的部分 PATCH：把手动模式分组的随笔关联进来 → 末尾补位
        self._call(_update_entry_sql(mover, 1, {"tag_ids": [tag_b, tag_a]}))
        self.assertEqual(self._positions(tag_a), [(n1, 1), (n2, 2), (mover, 3)])

    def test_tag_only_patch_pinned_into_latest_group_gets_position(self):
        tag_a = self._make_tag("F16最新组")["id"]
        tag_b = self._make_tag("F16源组")["id"]
        pinned = self._make_entry({
            "kind": "pinned", "content": "p", "tag_ids": [tag_b]})["id"]
        e1 = self._make_entry({
            "kind": "pinned", "content": "e1", "tag_ids": [tag_a]})["id"]
        # 不带 kind 的部分 PATCH：常驻关联进 latest 分组 → 常驻区末尾有位次
        self._call(_update_entry_sql(pinned, 1, {"tag_ids": [tag_b, tag_a]}))
        self.assertEqual(self._positions(tag_a), [(e1, 1), (pinned, 2)])

    # ── F17：同幂等键并发创建都取得首次记录，且只产生一条 ────────────

    def test_concurrent_same_crid_create_returns_first_record(self):
        import psycopg

        crid = "crid-concurrent-1"
        payload = {"kind": "note", "content": "并发首次", "client_request_id": crid}
        conn_a = psycopg.connect(self.server.get_uri())
        conn_b = psycopg.connect(self.server.get_uri())
        try:
            conn_b.autocommit = True
            conn_b.execute("set statement_timeout = '10s'")
            conn_a.autocommit = False
            # T1：创建已执行但未提交（幂等键已占用唯一索引）
            conn_a.execute(
                "select public.memo_create_entry(%s::jsonb, %s::timestamptz) as out",
                (json.dumps(payload), NOW))
            ready = threading.Event()
            outcome = {}

            def t2_create():
                ready.set()
                try:
                    with conn_b.cursor() as cur:
                        cur.execute(
                            "select public.memo_create_entry(%s::jsonb, %s::timestamptz) as out",
                            (json.dumps(payload), NOW))
                        outcome["result"] = cur.fetchall()[0][0]
                except psycopg.Error as exc:
                    outcome["sqlstate"] = exc.sqlstate

            thread = threading.Thread(target=t2_create)
            thread.start()
            ready.wait(5)
            time.sleep(0.3)   # T2 已抵达唯一索引等待
            self.assertNotIn("sqlstate", outcome)   # 在等首次事务，而不是报错
            conn_a.commit()
            thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("sqlstate", outcome, f"unexpected error: {outcome}")
            # 两个请求取得同一条首次记录
            first_id = self._query(
                "select id from public.memo_entry where client_request_id = %s",
                (crid,))[0][0]
            self.assertEqual(outcome["result"]["id"], first_id)
            count = self._query(
                "select count(*) from public.memo_entry where client_request_id = %s",
                (crid,))[0][0]
            self.assertEqual(count, 1)
        finally:
            conn_a.close()
            conn_b.close()

    # ── F06/F18：删除标签与编辑关联并发的锁序与成员复核 ──────────────

    def test_delete_tag_vs_add_second_tag_no_ghost_position(self):
        """设备B在删除标签期间为记录补第二标签：删除事务在记录行锁上等待、
        锁内复核成员后不再把它当孤儿——不产生未分类幽灵位次，随后移除
        最后标签也不触发 memo_position_pkey。"""
        import psycopg

        self._reset_untagged()
        tag_a = self._make_tag("F06删除A")["id"]
        tag_b = self._make_tag("F06新增B")["id"]
        # pinned：转入未分类时必须登记位次，幽灵位次场景才可观测
        entry_id = self._make_entry({
            "kind": "pinned", "content": "F06", "tag_ids": [tag_a]})["id"]

        conn_a = psycopg.connect(self.server.get_uri())
        conn_b = psycopg.connect(self.server.get_uri())
        try:
            conn_a.autocommit = True   # 删除标签事务：单 RPC 提交（跨设备语义）
            conn_a.execute("set statement_timeout = '10s'")
            # T2（另一设备）：先锁记录行，保持未提交——模拟删除标签在途时
            # 另一端的编辑请求抢先到达
            conn_b.execute("select * from public.memo_entry where id = %s for update",
                           (entry_id,))
            outcome = {}

            def t1_delete():
                try:
                    with conn_a.cursor() as cur:
                        cur.execute("select public.memo_delete_tag(%s, %s::timestamptz) as out",
                                    (tag_a, NOW))
                        outcome["delete"] = cur.fetchall()[0][0]
                except psycopg.Error as exc:
                    outcome["delete_err"] = exc.sqlstate

            thread = threading.Thread(target=t1_delete)
            thread.start()
            time.sleep(0.3)   # T1 已锁标签行、阻塞在记录行锁上（锁序：tag→entry）
            self.assertNotIn("delete", outcome)
            self.assertNotIn("delete_err", outcome)
            # T2：以完整快照补第二标签并提交（与真实前端一致）
            conn_b.execute("select public.memo_update_entry(%s, %s, %s::jsonb, %s::timestamptz) as out",
                           (entry_id, 1, json.dumps({"tag_ids": [tag_a, tag_b]}), NOW))
            conn_b.commit()
            thread.join(15)
            self.assertFalse(thread.is_alive())
            self.assertNotIn("delete_err", outcome, f"unexpected error: {outcome}")
            self.assertTrue(outcome["delete"]["deleted"])
            # 记录仍属于 B：没有未分类幽灵位次
            self.assertEqual(self._untagged_positions(), [])
            remaining = [row[0] for row in self._query(
                "select tag_id from public.memo_entry_tag where entry_id = %s",
                (entry_id,))]
            self.assertEqual(remaining, [tag_b])
            # 之后移除最后一个标签：正常转入未分类，不再 23505
            result = self._call(_update_entry_sql(entry_id, 2, {"tag_ids": []}))
            self.assertEqual(result["status"], "active")
            self.assertEqual(self._untagged_positions(), [(entry_id, 1)])
        finally:
            conn_a.close()
            conn_b.close()

    def test_delete_tag_vs_remove_last_tag_no_deadlock(self):
        """删除末标签与移除最后关联并发：一致锁序（记录行先于分组/位次）
        下任一起始顺序都不形成环——无 40P01，两条路径对未分类补位互为幂等。"""
        import psycopg

        self._reset_untagged()
        tag_a = self._make_tag("F18末标签")["id"]
        entry_id = self._make_entry({
            "kind": "pinned", "content": "F18", "tag_ids": [tag_a]})["id"]

        conn_a = psycopg.connect(self.server.get_uri())
        conn_b = psycopg.connect(self.server.get_uri())
        try:
            # 两个连接都是 autocommit：单 RPC 即事务（真实跨设备形态），
            # 谁先完成谁先提交，另一方在锁上等待而不是互相持有未提交锁
            for conn in (conn_a, conn_b):
                conn.autocommit = True
                conn.execute("set statement_timeout = '10s'")
            outcome = {}

            def run(sql_and_params, key, conn):
                sql, params = sql_and_params
                try:
                    with conn.cursor() as cur:
                        cur.execute(sql, params)
                        outcome[key] = cur.fetchall()[0][0]
                except psycopg.Error as exc:
                    outcome[f"{key}_err"] = exc.sqlstate

            t1 = threading.Thread(target=run, args=(
                ("select public.memo_delete_tag(%s, %s::timestamptz) as out", [tag_a, NOW]),
                "delete", conn_a))
            t2 = threading.Thread(target=run, args=(
                _update_entry_sql(entry_id, 1, {"tag_ids": []}), "update", conn_b))
            t1.start()
            time.sleep(0.05)
            t2.start()
            t1.join(20)
            t2.join(20)
            self.assertFalse(t1.is_alive() or t2.is_alive(),
                             f"transaction stuck: {outcome}")
            self.assertNotIn("delete_err", outcome, f"unexpected error: {outcome}")
            self.assertNotIn("update_err", outcome, f"unexpected error: {outcome}")
            # 标签已删除；记录转入未分类且恰好一条位次（两路径不重复插行）
            self.assertEqual(
                self._query("select count(*) from public.memo_tag where id = %s",
                            (tag_a,))[0][0], 0)
            self.assertEqual(self._untagged_positions(), [(entry_id, 1)])
        finally:
            conn_a.close()
            conn_b.close()

    # ── BUG-01：复合主键表按真实唯一键排序 ──────────────────────────

    def test_compound_key_tables_order_by_their_unique_keys(self):
        """gateway/memo.py 的分页排序键在真实表上必须存在且唯一稳定：
        memo_entry_tag / memo_position 没有 id 列（42703），分别按
        (entry_id, tag_id) / (group_id, entry_id) 排序。"""
        tag_a = self._make_tag("BUG01排序")["id"]
        ids = [
            self._make_entry({
                "kind": "pinned", "content": f"排序{i}", "tag_ids": [tag_a],
                "client_request_id": f"bug01-{i}"})["id"]
            for i in range(1, 6)
        ]
        group_id = self._query(
            "select g.id from public.memo_group g where g.tag_id = %s", (tag_a,))[0][0]
        # memo_position 需要非空：给两条记录落位次
        self._call(_reorder_group_sql(tag_a, "pinned", ids))

        entry_tag_keys = self._query(
            "select entry_id, tag_id from public.memo_entry_tag "
            "where tag_id = %s order by entry_id, tag_id", (tag_a,))
        self.assertEqual([k[0] for k in entry_tag_keys], ids)
        position_keys = self._query(
            "select group_id, entry_id from public.memo_position "
            "where group_id = %s order by group_id, entry_id", (group_id,))
        self.assertEqual([k[1] for k in position_keys], ids)
        # 同一排序键在真实表上唯一（分页不重不漏的前提）
        for table, keys in (("memo_entry_tag", entry_tag_keys),
                            ("memo_position", position_keys)):
            self.assertEqual(len(keys), len(set(keys)),
                             f"{table} 排序键必须唯一")

    # ── BUG-04：统一锁协议的对称并发验收 ────────────────────────────

    @classmethod
    def _new_conn(cls, autocommit):
        """独立连接：短 deadlock_timeout 让潜在 40P01 尽快显形而不是掩盖。"""
        import psycopg

        conn = psycopg.connect(cls.server.get_uri(), autocommit=autocommit)
        conn.execute("set statement_timeout = '15s'")
        conn.execute("set deadlock_timeout = '500ms'")
        if not autocommit:
            conn.commit()
        return conn

    @staticmethod
    def _run_rpc(conn, sql_and_params, key, outcome):
        import psycopg

        sql, params = sql_and_params
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                outcome[key] = cur.fetchall()[0][0]
        except psycopg.Error as exc:
            outcome[f"{key}_err"] = exc.sqlstate

    def _ghost_positions(self, tag_id=None):
        """分组内 (entry_id, position, 是否成员) 列表。
        未分类分组（tag_id=None）的成员 = 不带任何标签；标签分组的成员 =
        仍关联该标签。"""
        sql = """
            select p.entry_id, p.position,
                   case when %s::bigint is null then
                        not exists (select 1 from public.memo_entry_tag et
                                     where et.entry_id = p.entry_id)
                   else exists (select 1 from public.memo_entry_tag et
                                 where et.entry_id = p.entry_id
                                   and et.tag_id = %s)
                   end as member
              from public.memo_position p
              join public.memo_group g on g.id = p.group_id
             where (%s::bigint is null and g.tag_id is null)
                or g.tag_id = %s
             order by p.entry_id
        """
        return self._query(sql, (tag_id, tag_id, tag_id, tag_id))

    def test_bug04_reorder_vs_remove_association_no_ghost_both_directions(self):
        """标签内重排 vs 移除关联（双向）：先到者持成员行锁，后到者等锁后
        锁内复核——无幽灵位次、无持续 23505，失败回滚后可重试。"""
        # 方向一：编辑事务先持记录行锁，重排在成员行锁上等待
        tag_a = self._make_tag("BUG04重排移除")["id"]
        n1 = self._make_entry({"kind": "note", "content": "一", "tag_ids": [tag_a]})["id"]
        n2 = self._make_entry({"kind": "note", "content": "二", "tag_ids": [tag_a]})["id"]

        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute("select 1 from public.memo_entry where id = %s for update", (n1,))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _reorder_group_sql(tag_a, "note", [n1, n2]), "reorder", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("reorder", outcome)
            self.assertNotIn("reorder_err", outcome)   # 在等锁，不是失败
            # 编辑：移除 n1 的关联并提交
            conn_a.execute(*_update_entry_sql(n1, 1, {"tag_ids": []}))
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"reorder stuck: {outcome}")
            self.assertEqual(outcome.get("reorder_err"), "ME004",
                             f"锁内复核必须暴露成员变化: {outcome}")
            # 无非成员幽灵位次
            members = self._ghost_positions(tag_a)
            self.assertTrue(all(row[2] for row in members), f"ghost: {members}")
            # 再次关联成功（不 23505），重试重排成功
            self._call(_update_entry_sql(n1, 2, {"tag_ids": [tag_a]}))
            result = self._call(_reorder_group_sql(tag_a, "note", [n1, n2]))
            self.assertEqual(result["order"], [n1, n2])
        finally:
            conn_a.close()
            conn_b.close()

        # 方向二：重排事务持成员行锁未提交，移除关联等锁后正常清理
        tag_b = self._make_tag("BUG04重排移除反向")["id"]
        m1 = self._make_entry({"kind": "note", "content": "一", "tag_ids": [tag_b]})["id"]
        m2 = self._make_entry({"kind": "note", "content": "二", "tag_ids": [tag_b]})["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute(*_reorder_group_sql(tag_b, "note", [m1, m2]))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _update_entry_sql(m1, 1, {"tag_ids": []}), "update", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("update", outcome)
            self.assertNotIn("update_err", outcome)
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"update stuck: {outcome}")
            self.assertNotIn("update_err", outcome, f"unexpected: {outcome}")
            # 移除成功且顺带清掉重排写入的位次：无幽灵、可再次关联
            members = self._ghost_positions(tag_b)
            self.assertTrue(all(row[2] for row in members), f"ghost: {members}")
            self._call(_update_entry_sql(m1, 2, {"tag_ids": [tag_b]}))
            self._call(_reorder_group_sql(tag_b, "note", [m2, m1]))
        finally:
            conn_a.close()
            conn_b.close()

    def test_bug04_note_mode_vs_remove_association_no_ghost(self):
        """latest→manual 物化 vs 移除关联：物化在锁内复核成员，成员集合
        变化以 ME004 干净退出（可重试），被移出的记录不获得位次。"""
        tag_a = self._make_tag("BUG04模式移除")["id"]
        n1 = self._make_entry({"kind": "note", "content": "一", "tag_ids": [tag_a]})["id"]
        n2 = self._make_entry({"kind": "note", "content": "二", "tag_ids": [tag_a]})["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute("select 1 from public.memo_entry where id = %s for update", (n1,))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _note_mode_sql(tag_a, "manual"), "mode", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("mode", outcome)
            self.assertNotIn("mode_err", outcome)
            conn_a.execute(*_update_entry_sql(n1, 1, {"tag_ids": []}))
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"mode stuck: {outcome}")
            # 新合同：等锁期间成员被移出 → ME004 干净退出（不再期待模式
            # 在成员变化后照常成功），物化整体回滚——分组内不留任何位次
            self.assertEqual(outcome.get("mode_err"), "ME004",
                             f"unexpected: {outcome}")
            members = self._ghost_positions(tag_a)
            self.assertEqual(members, [], f"物化必须回滚: {members}")
            # n1 重新关联后基于最新数据重试：成功且全部成员按序落位
            self._call(_update_entry_sql(n1, 2, {"tag_ids": [tag_a]}))
            result = self._call(_note_mode_sql(tag_a, "manual"))
            self.assertEqual(result["note_sort_mode"], "manual")
            self.assertEqual(
                sorted(row[0] for row in self._positions(tag_a)), [n1, n2])
        finally:
            conn_a.close()
            conn_b.close()

    def test_bug04_untagged_reorder_vs_add_association_no_ghost(self):
        """未分类重排 vs 新增标签：转入标签的记录不残留未分类位次，
        之后移除末标签不 23505。"""
        self._reset_untagged()
        tag_a = self._make_tag("BUG04未分类重排")["id"]
        n1 = self._make_entry({"kind": "pinned", "content": "未分类常驻"})["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute("select 1 from public.memo_entry where id = %s for update", (n1,))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _reorder_group_sql(None, "pinned", [n1]), "reorder", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("reorder", outcome)
            self.assertNotIn("reorder_err", outcome)
            # 编辑：把 n1 加入标签（离开未分类）并提交
            conn_a.execute(*_update_entry_sql(n1, 1, {"tag_ids": [tag_a]}))
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"reorder stuck: {outcome}")
            self.assertEqual(outcome.get("reorder_err"), "ME004",
                             f"锁内复核必须暴露成员变化: {outcome}")
            # 未分类无 n1 幽灵位次；tag 分组也无幽灵
            self.assertTrue(all(row[2] for row in self._ghost_positions(None)))
            self.assertTrue(all(row[2] for row in self._ghost_positions(tag_a)))
            # 移除末标签（转回未分类）不再 23505
            self._call(_update_entry_sql(n1, 2, {"tag_ids": []}))
            self.assertTrue(all(row[2] for row in self._ghost_positions(None)))
        finally:
            conn_a.close()
            conn_b.close()

    def test_bug04_create_vs_delete_tag_no_deadlock_both_orders(self):
        """创建（tag KEY SHARE → group）vs 删除标签（entries → tag → 级联
        group）双向交错都无 40P01。"""
        # 方向一：创建先持标签 KEY SHARE，删除在标签行上等待
        tag_a = self._make_tag("BUG04创建删除")["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute(*_create_entry_sql({
                "kind": "pinned", "content": "新记录", "tag_ids": [tag_a]}))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b,
                ("select public.memo_delete_tag(%s, %s::timestamptz) as out", [tag_a, NOW]),
                "delete", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("delete", outcome)
            self.assertNotIn("delete_err", outcome)
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"delete stuck: {outcome}")
            self.assertNotIn("delete_err", outcome, f"删除不得死锁: {outcome}")
            self.assertTrue(outcome["delete"]["deleted"])
        finally:
            conn_a.close()
            conn_b.close()

        # 方向二：删除先持标签行，创建在标签 KEY SHARE 上等待；标签在
        # 等待期间被删除后，创建以 ME005 干净失败（而不是死锁或裸外键错误）
        tag_b = self._make_tag("BUG04删除创建")["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute("select 1 from public.memo_tag where id = %s for update", (tag_b,))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _create_entry_sql({
                    "kind": "note", "content": "并发创建", "tag_ids": [tag_b]}),
                "create", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("create", outcome)
            self.assertNotIn("create_err", outcome)
            # 持锁期间删除标签并提交：创建方的 KEY SHARE 等待恢复后拿不到行
            conn_a.execute("delete from public.memo_tag where id = %s", (tag_b,))
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"create stuck: {outcome}")
            self.assertEqual(outcome.get("create_err"), "ME005",
                             f"标签被并发删除应干净失败: {outcome}")
        finally:
            conn_a.close()
            conn_b.close()

    def test_bug04_kind_change_vs_reorder_no_deadlock_both_orders(self):
        """用途转换（entry → group）vs 重排（成员行 → group）双向无 40P01；
        等锁后的重排锁内复核成员，不产生幽灵位次。"""
        tag_a = self._make_tag("BUG04转换重排")["id"]
        n1 = self._make_entry({"kind": "note", "content": "目标", "tag_ids": [tag_a]})["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute("select 1 from public.memo_entry where id = %s for update", (n1,))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _reorder_group_sql(tag_a, "note", [n1]), "reorder", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("reorder", outcome)
            self.assertNotIn("reorder_err", outcome)
            conn_a.execute(*_update_entry_sql(n1, 1, {"kind": "pinned"}))
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"reorder stuck: {outcome}")
            self.assertEqual(outcome.get("reorder_err"), "ME004",
                             f"转换后 n1 已离开随笔区，复核应拒绝: {outcome}")
            self.assertTrue(all(row[2] for row in self._ghost_positions(tag_a)))
        finally:
            conn_a.close()
            conn_b.close()

        # 反向：重排持成员 + 分组锁未提交，用途转换等记录行锁后正常执行
        tag_b = self._make_tag("BUG04重排转换")["id"]
        n2 = self._make_entry({"kind": "note", "content": "目标", "tag_ids": [tag_b]})["id"]
        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute(*_reorder_group_sql(tag_b, "note", [n2]))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _update_entry_sql(n2, 1, {"kind": "pinned"}), "update", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("update", outcome)
            self.assertNotIn("update_err", outcome)
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"update stuck: {outcome}")
            self.assertNotIn("update_err", outcome, f"转换不得死锁: {outcome}")
            positions = self._positions(tag_b)
            self.assertEqual(len(positions), 1, f"位次应唯一: {positions}")
            self.assertEqual(positions[0][0], n2)
        finally:
            conn_a.close()
            conn_b.close()

    def test_bug04_concurrent_kind_changes_get_distinct_end_positions(self):
        """两条 latest 随笔并发转常驻：转换在分组行锁上串行，末尾位次
        互不相同（原实现两条都拿到 max+1 的同一值）。"""
        tag_a = self._make_tag("BUG04并发转换")["id"]
        seed = self._make_entry({
            "kind": "pinned", "content": "既有常驻", "tag_ids": [tag_a]})["id"]
        n2 = self._make_entry({"kind": "note", "content": "转换甲", "tag_ids": [tag_a]})["id"]
        n3 = self._make_entry({"kind": "note", "content": "转换乙", "tag_ids": [tag_a]})["id"]

        conn_a = self._new_conn(autocommit=False)
        conn_b = self._new_conn(autocommit=True)
        try:
            conn_a.execute(*_update_entry_sql(n2, 1, {"kind": "pinned"}))
            outcome = {}
            t = threading.Thread(target=self._run_rpc, args=(
                conn_b, _update_entry_sql(n3, 1, {"kind": "pinned"}), "update", outcome))
            t.start()
            time.sleep(0.4)
            self.assertNotIn("update", outcome)
            self.assertNotIn("update_err", outcome)
            conn_a.commit()
            t.join(20)
            self.assertFalse(t.is_alive(), f"update stuck: {outcome}")
            self.assertNotIn("update_err", outcome, f"unexpected: {outcome}")
            positions = self._positions(tag_a)
            pos_values = [row[1] for row in positions]
            self.assertEqual(
                sorted(positions),
                sorted([(seed, 1), (n2, 2), (n3, 3)]),
                f"两个转换必须获得不同的末尾位次: {positions}")
            self.assertEqual(len(pos_values), len(set(pos_values)))
        finally:
            conn_a.close()
            conn_b.close()

    def test_bug04_lock_protocol_migration_replay_cleans_ghost_positions(self):
        """增量迁移可安全重放（幂等），并清除历史并发窗口留下的非成员
        幽灵位次；合法成员位次原样保留。"""
        tag_a = self._make_tag("BUG04幽灵清理")["id"]
        keeper = self._make_entry({
            "kind": "pinned", "content": "合法成员", "tag_ids": [tag_a]})["id"]
        moved = self._make_entry({
            "kind": "pinned", "content": "已离开", "tag_ids": [tag_a]})["id"]
        group_id = self._query(
            "select g.id from public.memo_group g where g.tag_id = %s", (tag_a,))[0][0]
        # keeper 是合法成员并落位；moved 模拟坏状态：位次仍在但关联已被移走
        self._call(_reorder_group_sql(tag_a, "pinned", [keeper, moved]))
        self._query(
            "delete from public.memo_entry_tag where entry_id = %s and tag_id = %s",
            (moved, tag_a))

        migration = (ROOT / "supabase" / "migrations" /
                     "20261003000000_memo_lock_protocol.sql").read_text(encoding="utf-8")
        self.conn.execute(migration)   # 重放：函数替换 + 幽灵清理（幂等）

        remaining = self._query(
            "select entry_id, position from public.memo_position where group_id = %s "
            "order by position", (group_id,))
        self.assertEqual(remaining, [(keeper, 1)], "非成员位次必须清除，成员位次保留")
        # 清理后再次关联不再 23505
        self._call(_update_entry_sql(moved, 1, {"tag_ids": [tag_a]}))
        self.assertEqual(len(self._positions(tag_a)), 2)

        # 重放旧迁移会把已安装的函数整体降级（BUG-15）：本类共享一个数据库，
        # 版本守卫与后续全部并发测试都依赖最新函数——结束时重放该迁移之后
        # 的全部迁移，保证旧定义不残留到执行期其余部分。
        for path in sorted(_base.MIGRATIONS_DIR.glob("*.sql")):
            if path.name <= "20261003000000_memo_lock_protocol.sql":
                continue
            sql = path.read_text(encoding="utf-8")
            if path.name == _base.PG_TRGM_FILE:
                sql = sql.replace(_base.PG_TRGM_LINE, "-- pg_trgm stubbed (pgserver)")
            self.conn.execute(sql)
        guard = self._query(
            "select count(*) from pg_proc where proname = 'memo_update_entry' "
            "and prosrc like '%v_lock_tags%'")
        self.assertEqual(guard[0][0], 1, "重放后必须恢复最新版函数")

    # ── BUG-04 第二轮：分组锁序与创建路径同构 + 模式复核干净退出 ──────

    def _wait_for_lock(self, conn, key, out, deadline=5.0):
        """等待连接进入锁等待（外部行锁延长正常等待窗口，不改变 RPC）。"""
        import time
        pid = conn.info.backend_pid
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            rows = self._query(
                "select wait_event_type from pg_stat_activity where pid = %s", (pid,))
            if rows and rows[0][0] == "Lock":
                out[key] = True
                return
            time.sleep(0.01)
        raise AssertionError(f"连接 {pid} 未进入预期锁等待")

    def test_bug04_round2_new_lock_order_functions_installed(self):
        """前置守卫（BUG-15：守卫覆盖整个关键执行期的最终版本）：统一锁序
        迁移（20261003010000）、第三轮成员守卫迁移（20261004000000）与
        回收站保留期迁移（20261004010000）必须已随重放安装——update 含
        v_lock_tags（标签升序分组锁），mode 含 v_recheck（成员集合锁内
        复核），reorder 含 v_locked_members（初始受保护集合），lifecycle
        含 72 小时保留期门，清扫 RPC 已定义。"""
        update_src = self._query(
            "select prosrc from pg_proc where proname = 'memo_update_entry'")[0][0]
        mode_src = self._query(
            "select prosrc from pg_proc where proname = 'memo_set_note_mode'")[0][0]
        reorder_src = self._query(
            "select prosrc from pg_proc where proname = 'memo_reorder_group'")[0][0]
        lifecycle_src = self._query(
            "select prosrc from pg_proc where proname = 'memo_set_entry_lifecycle'")[0][0]
        purge_fn = self._query(
            "select count(*) from pg_proc where proname = 'memo_purge_expired_trash'")
        self.assertIn("v_lock_tags", update_src,
                      "统一锁序迁移未安装：update 仍是旧版")
        self.assertIn("v_recheck", mode_src,
                      "统一锁序迁移未安装：mode 仍是旧版")
        self.assertIn("v_locked_members", reorder_src,
                      "第三轮成员守卫未安装：reorder 仍是旧版")
        self.assertIn("72 hours", lifecycle_src,
                      "回收站保留期迁移未安装：lifecycle 无保留期门")
        self.assertEqual(purge_fn[0][0], 1,
                         "回收站到期清扫 RPC 未安装")

    def test_bug04_round2_create_vs_edit_reversed_group_ids_no_deadlock(self):
        """tag.id 升序与 group.id 升序相反时（先关联 B 再关联 A 的种子）：
        编辑（转常驻 + 新增标签 A）与创建 A+B 的分组加锁顺序必须同构，
        交错不形成环（BUG-04 复审场景 1）。

        确定性构造（不违反被测锁序）：预持 group_a 行锁把创建截停在分组
        锁上（创建此时已持两标签行锁）；随后编辑在标签行锁上等待创建，
        除记录行外不持任何分组锁——链式等待（编辑→创建→gate）在场时
        不得出现 40P01；gate 释放后两事务按序完成、最终状态一致。若回归
        为「编辑先持分组行再锁标签」，编辑会握住 group_b：gate 释放后
        创建等 group_b、编辑等 tag_a，立即成环 40P01。

        夹具约束（BUG-15）：连接均为 autocommit 单 RPC 事务（真实跨设备
        形态），不跨线程 commit；返回对象为 RPC 顶层 jsonb（create.id）。
        """
        import threading
        import time

        tag_a = self._make_tag("BUG04二轮A")["id"]
        tag_b = self._make_tag("BUG04二轮B")["id"]
        entry_b = self._make_entry({
            "kind": "note", "content": "B种子", "tag_ids": [tag_b]})
        self._make_entry({"kind": "note", "content": "A种子", "tag_ids": [tag_a]})
        group_a = self._query(
            "select id from public.memo_group where tag_id = %s", (tag_a,))[0][0]
        group_b = self._query(
            "select id from public.memo_group where tag_id = %s", (tag_b,))[0][0]
        # 场景前提：group.id 与 tag.id 升序相反
        self.assertLess(tag_a, tag_b)
        self.assertGreater(group_a, group_b)

        out = {}
        gate = self._new_conn(autocommit=False)
        gate.execute(
            "select 1 from public.memo_group where id = %s for update", (group_a,))
        create_conn = self._new_conn(autocommit=True)
        edit_conn = self._new_conn(autocommit=True)
        thread = threading.Thread(target=self._run_rpc, args=(
            create_conn,
            _create_entry_sql({
                "kind": "pinned", "content": "BUG04二轮创建",
                "tag_ids": [tag_a, tag_b]}),
            "create", out))
        thread.start()
        self._wait_for_lock(create_conn, "create_wait", out)
        # 创建已持两标签行锁、截停在 group_a；编辑的标签行锁必须等创建
        t2 = threading.Thread(target=self._run_rpc, args=(
            edit_conn, _update_entry_sql(
                entry_b["id"], entry_b["content_version"],
                {"kind": "pinned", "tag_ids": [tag_b, tag_a]}), "update", out))
        t2.start()
        self._wait_for_lock(edit_conn, "update_wait", out)
        time.sleep(0.2)   # 两个等待同时在场：链式等待不得产生 40P01
        self.assertNotIn("create_err", out, f"链式等待不得死锁: {out}")
        self.assertNotIn("update_err", out, f"链式等待不得死锁: {out}")
        gate.commit()   # 释放 group_a → 创建完成并提交 → 编辑随后完成
        thread.join(15)
        t2.join(15)
        self.assertFalse(thread.is_alive() or t2.is_alive(), f"卡住: {out}")
        for key in ("create", "update"):
            self.assertNotIn(f"{key}_err", out, f"{key} 不得死锁/超时: {out}")
        # 最终状态一致：编辑后的记录带双标签且为常驻；创建的记录存在且
        # 常驻（BUG-15：memo_create_entry 顶层返回 id，不再读不存在的键）
        create_id = out["create"]["id"]
        kinds = dict(self._query(
            "select id, kind from public.memo_entry where id = any(%s)",
            ([entry_b["id"], create_id],)))
        self.assertEqual(kinds[entry_b["id"]], "pinned")
        self.assertEqual(kinds[create_id], "pinned")
        # 无幽灵位次：两个常驻记录在 A、B 分组的位次都是成员位次
        for tag_id in (tag_a, tag_b):
            members = self._ghost_positions(tag_id)
            self.assertTrue(all(row[2] for row in members), f"ghost: {members}")
        gate.close()
        create_conn.close()
        edit_conn.close()

    def test_bug04_round2_missing_group_create_vs_edit_no_deadlock(self):
        """A 分组尚不存在时，创建 A+B 与「B 记录转常驻并新增关联 A」并发
        交错：统一锁序（标签行锁内 ensure 分组）下不形成环，任何一方在
        缺失分组上的 ensure 由标签行锁串行化（BUG-04 复审场景 2）。

        确定性构造：预持 tag_a 行锁把两个事务同时截停在各自的标签行锁上
        （创建的首步、编辑的标签锁步）——两个等待在场时不得出现 40P01；
        释放后按锁队列完成，最终两个分组各恰好一行。夹具为 autocommit
        单 RPC 事务，不跨线程 commit（BUG-15：不再产生未提交持锁/不可见
        结果）。"""
        import threading
        import time

        tag_a = self._make_tag("BUG04缺失组A")["id"]
        tag_b = self._make_tag("BUG04缺失组B")["id"]
        entry_b = self._make_entry({
            "kind": "note", "content": "B种子", "tag_ids": [tag_b]})
        self.assertEqual(self._query(
            "select count(*) from public.memo_group where tag_id = %s",
            (tag_a,)), [(0,)])

        out = {}
        gate = self._new_conn(autocommit=False)
        gate.execute("select 1 from public.memo_tag where id = %s for update", (tag_a,))
        create_conn = self._new_conn(autocommit=True)
        edit_conn = self._new_conn(autocommit=True)
        t1 = threading.Thread(target=self._run_rpc, args=(
            create_conn, _create_entry_sql({
                "kind": "pinned", "content": "缺失组创建",
                "tag_ids": [tag_a, tag_b]}), "create", out))
        t2 = threading.Thread(target=self._run_rpc, args=(
            edit_conn, _update_entry_sql(
                entry_b["id"], entry_b["content_version"],
                {"kind": "pinned", "tag_ids": [tag_b, tag_a]}),
            "update", out))
        t1.start()
        self._wait_for_lock(create_conn, "create_wait", out)
        t2.start()
        self._wait_for_lock(edit_conn, "update_wait", out)
        time.sleep(0.2)
        self.assertNotIn("create_err", out, f"等待在场不得死锁: {out}")
        self.assertNotIn("update_err", out, f"等待在场不得死锁: {out}")
        gate.commit()   # 释放 tag_a：创建先取锁完成，编辑随后
        t1.join(15)
        t2.join(15)
        self.assertFalse(t1.is_alive() or t2.is_alive(), f"卡住: {out}")
        for key in ("create", "update"):
            self.assertNotIn(f"{key}_err", out,
                             f"{key} 不得死锁/超时: {out}")
        rows = self._query(
            "select count(*) from public.memo_group where tag_id = any(%s)",
            ([tag_a, tag_b],))
        self.assertEqual(rows[0][0], 2, "两个分组各恰好一行")
        kinds = dict(self._query(
            "select id, kind from public.memo_entry where id = any(%s)",
            ([entry_b["id"], out["create"]["id"]],)))
        self.assertEqual(kinds[entry_b["id"]], "pinned")
        self.assertEqual(kinds[out["create"]["id"]], "pinned")
        gate.close()
        create_conn.close()
        edit_conn.close()

    def test_bug04_round2_note_mode_materializes_only_protected_members(self):
        """等分组锁期间新成员加入（已提交）：取得分组锁后成员集合复核
        不一致必须 ME004 干净退出，不为未受记录锁保护的成员物化位次
        （BUG-04 复审场景 3；旧实现在此窗口与改用途形成记录↔分组环）。"""
        import threading

        import psycopg

        tag = self._make_tag("BUG04模式复核")["id"]
        other = self._make_tag("BUG04模式旁组")["id"]
        old_note = self._make_entry({
            "kind": "note", "content": "旧成员", "tag_ids": [tag]})
        new_note = self._make_entry({
            "kind": "note", "content": "新成员", "tag_ids": [other]})
        group_id = self._query(
            "select id from public.memo_group where tag_id = %s", (tag,))[0][0]

        out = {}
        gate_conn = psycopg.connect(self.server.get_uri())
        mode_conn = psycopg.connect(self.server.get_uri())
        mode_conn.execute("set statement_timeout = '10s'")
        mode_conn.execute("set deadlock_timeout = '500ms'")
        update_conn = psycopg.connect(self.server.get_uri())
        update_conn.execute("set statement_timeout = '10s'")
        update_conn.execute("set deadlock_timeout = '500ms'")
        try:
            # 模式 RPC 的首步是锁成员记录行：在旧成员行上拦住它
            gate_conn.execute("select 1 from public.memo_entry where id = %s for update",
                              (old_note["id"],))
            thread = threading.Thread(target=self._run_rpc, args=(
                mode_conn, _note_mode_sql(tag, "manual"), "mode", out))
            thread.start()
            self._wait_for_lock(mode_conn, "mode_wait", out)
            # 等待期间另一个正常 RPC 把新成员加入该分组并提交
            self._call(_update_entry_sql(
                new_note["id"], new_note["content_version"],
                {"tag_ids": [other, tag]}))
            # 新成员的记录行被预锁（模拟改用途事务已到达行锁）
            update_conn.execute(
                "select 1 from public.memo_entry where id = %s for update",
                (new_note["id"],))
            gate_conn.commit()   # 放行模式 RPC
            thread.join(15)
            self.assertFalse(thread.is_alive(), f"mode 卡住: {out}")
            # 新代码：集合复核不一致 → ME004（可重试），不物化未保护成员
            # （BUG-15：_run_rpc 存 mode_err，失败键不从成功对象读取）
            self.assertEqual(out.get("mode_err"), "ME004",
                             f"模式必须干净退出: {out}")
            # 模式 RPC 已回滚：分组内没有新成员的位次行
            positions = self._query(
                "select entry_id from public.memo_position where group_id = %s",
                (group_id,))
            self.assertNotIn((new_note["id"],), positions)
            # 随后改用途正常完成（不再与模式互相等待）；_run_rpc 成功存
            # 平面对象、失败存 update_err，不再读 sqlstate
            self._run_rpc(update_conn, _update_entry_sql(
                new_note["id"], self._query(
                    "select content_version from public.memo_entry where id = %s",
                    (new_note["id"],))[0][0],
                {"kind": "pinned"}), "update", out)
            self.assertIn("update", out, f"改用途必须成功: {out}")
            self.assertNotIn("update_err", out, f"{out}")
            # 单 RPC 事务随提交生效（BUG-15）：工作连接必须显式 commit，
            # 否则改用途在 close 时回滚，后续断言建立在不存在的状态上
            update_conn.commit()
            # 转常驻后在常驻区末尾合法落位（BUG-15：不再要求转换后无位次）
            positions = self._query(
                "select entry_id from public.memo_position where group_id = %s",
                (group_id,))
            self.assertIn((new_note["id"],), positions)
            # 客户端基于最新数据重试：成功，模式物化完成
            result = self._call(_note_mode_sql(tag, "manual"))
            self.assertEqual(result["note_sort_mode"], "manual")
        finally:
            gate_conn.close()
            mode_conn.close()
            update_conn.close()
            thread.join(15)

    # ── BUG-04 第三轮：重排锁内复核初始受保护集合 ────────────────────

    def test_bug04_reorder_unlocked_new_member_rejected(self):
        """等锁期间新成员加入（已提交）：重排取得分组锁后必须以 ME004
        退出，不为未受本事务记录锁保护的新成员写位次（BUG-04 第三轮：
        原实现用锁后重读覆盖成员集合后照写，为位次外键反向等待新成员
        行锁，与改用途持行等组形成记录↔分组环，真实 40P01）。

        场景一（新成员仍是随笔）：初始成员 [旧]，等待期间新成员加入并
        提交——锁内集合 [旧,新] ≠ 加锁集合 [旧]，ME004 来自受保护集合
        复核。场景二（等新成员行锁期间被改用途）：新成员先行入组，重排
        在其行锁上等待且不持分组行锁——同连接的改用途取得分组锁正常
        完成（无环），随后重排以 ME004 退出。
        """
        # ── 场景一：新成员保持随笔，锁内复核受保护集合 ──
        tag = self._make_tag("BUG04重排新成员")["id"]
        other = self._make_tag("BUG04重排旁组")["id"]
        old_note = self._make_entry({
            "kind": "note", "content": "旧成员", "tag_ids": [tag]})
        new_note = self._make_entry({
            "kind": "note", "content": "新成员", "tag_ids": [other]})
        group_id = self._query(
            "select id from public.memo_group where tag_id = %s", (tag,))[0][0]

        out = {}
        gate = self._new_conn(autocommit=False)
        gate.execute(
            "select 1 from public.memo_entry where id = %s for update",
            (old_note["id"],))
        reorder_conn = self._new_conn(autocommit=True)
        try:
            thread = threading.Thread(target=self._run_rpc, args=(
                reorder_conn, _reorder_group_sql(
                    tag, "note", [old_note["id"], new_note["id"]]),
                "reorder", out))
            thread.start()
            self._wait_for_lock(reorder_conn, "reorder_wait", out)
            # 等待期间另一个正常 RPC 把新成员加入分组并提交
            self._call(_update_entry_sql(
                new_note["id"], new_note["content_version"],
                {"tag_ids": [other, tag]}))
            gate.commit()   # 放行重排：加锁集合仍是 [旧成员]
            thread.join(15)
            self.assertFalse(thread.is_alive(), f"reorder 卡住: {out}")
            self.assertEqual(out.get("reorder_err"), "ME004",
                             f"未受保护的新成员必须被拒绝: {out}")
            positions = self._query(
                "select entry_id from public.memo_position where group_id = %s",
                (group_id,))
            self.assertNotIn((new_note["id"],), positions)
            # 客户端基于最新数据重试：成功，全部成员按提交顺序落位
            result = self._call(_reorder_group_sql(
                tag, "note", [old_note["id"], new_note["id"]]))
            self.assertEqual(result["order"], [old_note["id"], new_note["id"]])
        finally:
            gate.close()
            reorder_conn.close()
            thread.join(15)

        # ── 场景二：重排等新成员行锁时不持分组锁，改用途不形成环 ──
        tag2 = self._make_tag("BUG04重排转用途")["id"]
        other2 = self._make_tag("BUG04重排转用途旁组")["id"]
        old2 = self._make_entry({
            "kind": "note", "content": "旧成员", "tag_ids": [tag2]})
        new2 = self._make_entry({
            "kind": "note", "content": "新成员", "tag_ids": [tag2]})   # 已在组内
        self._call(_reorder_group_sql(tag2, "note", [old2["id"], new2["id"]]))

        out = {}
        gate2 = self._new_conn(autocommit=False)
        gate2.execute(
            "select 1 from public.memo_entry where id = %s for update",
            (old2["id"],))
        reorder_conn2 = self._new_conn(autocommit=True)
        kind_conn = self._new_conn(autocommit=False)
        try:
            thread = threading.Thread(target=self._run_rpc, args=(
                reorder_conn2, _reorder_group_sql(
                    tag2, "note", [old2["id"], new2["id"]]),
                "reorder", out))
            thread.start()
            self._wait_for_lock(reorder_conn2, "reorder_wait", out)
            # 改用途事务先到：持有新成员记录行（未提交）
            kind_conn.execute(
                "select 1 from public.memo_entry where id = %s for update",
                (new2["id"],))
            gate2.commit()   # 放行重排：锁完旧成员后停在新成员行锁上
            self._wait_for_lock(reorder_conn2, "reorder_member_wait", out)
            # 同连接改用途：需要分组行锁——重排停在成员行锁、未持组锁，
            # 改用途必须正常完成（旧实现在此记录↔分组环 40P01）
            self._run_rpc(kind_conn, _update_entry_sql(
                new2["id"], self._query(
                    "select content_version from public.memo_entry where id = %s",
                    (new2["id"],))[0][0],
                {"kind": "pinned"}), "update", out)
            self.assertIn("update", out, f"改用途不得死锁: {out}")
            self.assertNotIn("update_err", out, f"{out}")
            kind_conn.commit()
            thread.join(15)
            self.assertFalse(thread.is_alive(), f"reorder 卡住: {out}")
            # 新成员已离开随笔区：锁内集合 [旧] ≠ 加锁集合 [旧,新] → ME004
            self.assertEqual(out.get("reorder_err"), "ME004",
                             f"集合变化必须干净退出: {out}")
            # 无幽灵位次：新成员获得位次来自改用途（常驻末尾），不是重排；
            # 旧成员的随笔位次来自先行提交的初始重排，原样保留
            positions = self._query(
                "select entry_id from public.memo_position where group_id = %s",
                (self._query(
                    "select id from public.memo_group where tag_id = %s",
                    (tag2,))[0][0],))
            self.assertIn(new2["id"], [p[0] for p in positions])
            self.assertTrue(all(row[2] for row in self._ghost_positions(tag2)))
        finally:
            gate2.close()
            kind_conn.close()
            reorder_conn2.close()
            thread.join(15)

    # ── M19：回收站保留期 72 小时 ──────────────────────────────────

    def test_m19_restore_rejected_after_retention(self):
        """M19：保留期门独立于清扫——删除满 72 小时的记录恢复被 ME003
        拒绝；未满 72 小时可恢复；归档记录的恢复不受保留期影响。"""
        entry = self._make_entry({"kind": "note", "content": "过期恢复"})
        self._call(_lifecycle_sql(entry["id"], "delete", 1))
        # 恰好 72 小时：边界含端点，到期即拒绝
        late = "2026-10-05T09:00:00+08:00"
        self._expect_errcode(
            "ME003",
            _lifecycle_at_sql(entry["id"], "restore", 2, late),
            "trash retention",
        )
        # 未满 72 小时：可恢复
        ok = "2026-10-05T08:59:00+08:00"
        result = self._call(_lifecycle_at_sql(entry["id"], "restore", 2, ok))
        self.assertEqual(result["status"], "active")
        # 再次删除后用过期时间恢复：同样拒绝（门独立于记录历史）
        self._call(_lifecycle_at_sql(entry["id"], "delete", result["content_version"], NOW))
        self._expect_errcode(
            "ME003",
            _lifecycle_at_sql(entry["id"], "restore", result["content_version"] + 1, late),
            "trash retention",
        )
        # 归档记录恢复不经过保留期门
        archived = self._make_entry({"kind": "note", "content": "归档恢复"})
        self._call(_lifecycle_sql(archived["id"], "archive", 1))
        result = self._call(_lifecycle_at_sql(archived["id"], "restore", 2, late))
        self.assertEqual(result["status"], "active")

    def test_m19_trash_purges_after_72h_and_cascades(self):
        """M19：删除满 72 小时的记录被 memo_purge_expired_trash 硬删除，
        关联与位次级联清除、幂等键不再保留；未满 72 小时保留；归档与
        正常内容不受影响；重复清扫幂等。"""
        tag = self._make_tag("M19保留期")["id"]
        expired = self._make_entry({
            "kind": "pinned", "content": "已到期", "tag_ids": [tag],
            "client_request_id": "m19-expired"})["id"]
        fresh = self._make_entry({
            "kind": "pinned", "content": "未到期", "tag_ids": [tag]})["id"]
        archived = self._make_entry({"kind": "note", "content": "归档不受影响"})["id"]
        active = self._make_entry({"kind": "note", "content": "正常不受影响"})["id"]
        self._call(_reorder_group_sql(tag, "pinned", [fresh, expired]))
        self._call(_lifecycle_sql(expired, "delete", 1))
        self._call(_lifecycle_sql(archived, "archive", 1))
        # fresh 删除到「尚未满 72 小时」的时刻点
        self._call(_lifecycle_at_sql(fresh, "delete", 1, "2026-10-05T09:00:30+08:00"))

        # 未满 72 小时：清扫 0 行，过期与未到期条目都保留
        before = self._call(
            ("select public.memo_purge_expired_trash(%s::timestamptz) as out",
             ["2026-10-05T08:59:00+08:00"]))
        self.assertEqual(before, 0)
        self.assertIsNotNone(self._entry(expired))
        self.assertIsNotNone(self._entry(fresh))

        # 满 72 小时：硬删除并级联
        purged = self._call(
            ("select public.memo_purge_expired_trash(%s::timestamptz) as out",
             ["2026-10-05T09:02:00+08:00"]))
        self.assertGreaterEqual(purged, 1)
        self.assertIsNone(self._entry(expired))
        self.assertIsNotNone(self._entry(fresh))
        self.assertEqual(self._entry(archived)["status"], "archived")
        self.assertEqual(self._entry(active)["status"], "active")
        self.assertEqual(self._query(
            "select count(*) from public.memo_entry_tag where entry_id = %s",
            (expired,))[0][0], 0)
        self.assertEqual(self._query(
            "select count(*) from public.memo_position where entry_id = %s",
            (expired,))[0][0], 0)
        self.assertEqual(self._query(
            "select count(*) from public.memo_entry where client_request_id = 'm19-expired'",
        )[0][0], 0)
        # 幂等：重复清扫不再删除
        again = self._call(
            ("select public.memo_purge_expired_trash(%s::timestamptz) as out",
             ["2026-10-05T09:02:00+08:00"]))
        self.assertEqual(again, 0)


if __name__ == "__main__":
    unittest.main()
