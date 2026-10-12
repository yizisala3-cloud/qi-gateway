"""网关日志迁移 + RPC 真 PostgreSQL 验证（§10 阶段 B / 验收 L08 L09 L19 L23）。

Opt-in（启动一次性 pgserver 实例，成本较高）：

    QIGATEWAY_PG_LOG_TEST=1 python -m pytest \
        tests/test_gateway_log_pgserver_integration.py -v

被测迁移 ``20261010010000_gateway_error_events.sql`` 逐字节从磁盘执行；
测试绝不触碰任何生产数据库。覆盖：

- 迁移可执行且幂等（重复执行不报错）；
- RLS + 收权：anon / authenticated 无权读写与调用，service_role 可用；
- 事件幂等：同 ``event_id`` 的 ON CONFLICT DO NOTHING 重试不重复计数；
- 汇总查询：fingerprint 聚合、count / first / last、级别取最高；
- 稳定 cursor 分页：limit 翻页无重叠、has_more 正确；
- 筛选：级别 / 模块 / 时间区间 / 搜索；
- 组详情样本上限与单事件详情 404 语义；
- 清理：按真实 occurred_at 分批删除、advisory 锁互斥返回 skipped_locked。
"""

import os
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import pgserver
    import psycopg
    _STACK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional heavyweight stack
    _STACK_AVAILABLE = False

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "20261010010000_gateway_error_events.sql"

BASELINE_SQL = """
create role anon nologin;
create role authenticated nologin;
create role service_role nologin bypassrls;
grant usage on schema public to anon, authenticated, service_role;
"""


def _event_sql(event_id, occurred_at, *, level="ERROR", module="planning",
               operation="create", error_code="internal_error", fingerprint=None,
               message_zh="规划接口发生内部错误", exception_type="APIError",
               ingested_at="2026-10-10T11:00:00+00:00"):
    fp = fingerprint or "a" * 16
    return (
        "insert into public.gateway_error_events "
        "(event_id, occurred_at, ingested_at, level, module, operation, error_code, "
        " message_zh, exception_type, message_raw, stack_trace, logger_name, "
        " source, request_id, route, method, http_status, outcome, "
        " instance_id, fingerprint) values ("
        f"'{event_id}', '{occurred_at}', '{ingested_at}', '{level}', '{module}', "
        f"'{operation}', '{error_code}', '{message_zh}', "
        f"'{exception_type}', 'raw', 'trace', 'gateway.{module}', "
        f"'a.py:fn:1', 'r-{event_id[:4]}', '/admin/api/{module}', 'POST', "
        "500, 'failed', 'inst', "
        f"'{fp}') on conflict (event_id) do nothing"
    )


class GatewayLogPgserverIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not _STACK_AVAILABLE:
            raise unittest.SkipTest("pgserver + psycopg are not installed")
        if os.environ.get("QIGATEWAY_PG_LOG_TEST") != "1":
            raise unittest.SkipTest(
                "set QIGATEWAY_PG_LOG_TEST=1 (or run pytest --db) to run "
                "the real PostgreSQL gateway log integration test"
            )
        cls.pgdata = Path(tempfile.mkdtemp(prefix="qigate-log-pgserver-"))
        cls.server = None
        cls.conn = None
        try:
            cls.server = pgserver.get_server(cls.pgdata, cleanup_mode="stop")
            cls.conn = psycopg.connect(cls.server.get_uri(), autocommit=True)
            cls.conn.execute(BASELINE_SQL)
            # 被测迁移逐字节执行；随后原样重跑一次验证幂等
            cls.conn.execute(MIGRATION.read_text(encoding="utf-8"))
            cls.conn.execute(MIGRATION.read_text(encoding="utf-8"))
        except Exception:
            cls.tearDownClass()
            raise

    def setUp(self):
        # 每个用例独立种子（清理 / 删除类用例不污染后续断言）
        self.conn.execute("set role service_role")
        try:
            self.conn.execute("delete from public.gateway_error_events")
            self._seed()
        finally:
            self.conn.execute("reset role")

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

    @classmethod
    def _seed(cls):
        cls.conn.execute("set role service_role")
        try:
            # 同一 fingerprint 的 3 次同类故障（不同时间、不同 request_id）
            cls.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000001", "2026-10-10T01:00:00+00:00"))
            cls.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000002", "2026-10-10T02:00:00+00:00",
                level="WARNING"))
            cls.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000003", "2026-10-10T03:00:00+00:00"))
            # 同 event_id 重放：幂等，不重复计数（L08）
            cls.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000003", "2026-10-10T03:00:00+00:00"))
            # 另一模块的独立故障（不与 planning 合并，L09）
            cls.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000011", "2026-10-10T04:00:00+00:00",
                module="chat", operation="chat_completions",
                error_code="upstream_timeout", fingerprint="b" * 16,
                message_zh="聊天转发请求超时", exception_type=""))
            # 40 天前的旧事件（清理验收 L23）
            cls.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000021", "2026-08-31T00:00:00+00:00",
                fingerprint="c" * 16))
        finally:
            cls.conn.execute("reset role")

    def _query_rpc(self, sql, params=None):
        row = self.conn.execute(sql, params).fetchone()
        return row[0] if row else None

    def _as_service_role(self, fn):
        self.conn.execute("set role service_role")
        try:
            return fn()
        finally:
            self.conn.execute("reset role")

    # ── 权限（L16） ─────────────────────────────────────────────

    def test_anon_cannot_read_table_or_call_functions(self):
        self.conn.execute("set role anon")
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.conn.execute("select count(*) from public.gateway_error_events")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.conn.execute("select public.gateway_log_facets()")
        finally:
            self.conn.execute("reset role")

    def test_authenticated_cannot_write_table(self):
        self.conn.execute("set role authenticated")
        try:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.conn.execute(_event_sql(
                    "00000000-0000-0000-0000-000000000099", "2026-10-10T05:00:00+00:00"))
        finally:
            self.conn.execute("reset role")

    def test_service_role_can_read_write_and_call(self):
        def check():
            count = self.conn.execute(
                "select count(*) from public.gateway_error_events").fetchone()[0]
            self.assertGreaterEqual(count, 5)
            result = self._query_rpc("select public.gateway_log_facets()")
            self.assertGreaterEqual(result["total_events"], 5)
            return True
        self.assertTrue(self._as_service_role(check))

    # ── 事件幂等（L08） ─────────────────────────────────────────

    def test_duplicate_event_id_counts_once(self):
        def check():
            count = self.conn.execute(
                "select count(*) from public.gateway_error_events "
                "where event_id = '00000000-0000-0000-0000-000000000003'"
            ).fetchone()[0]
            return count
        self.assertEqual(self._as_service_role(check), 1)

    # ── 汇总查询（L09） ─────────────────────────────────────────

    def test_groups_aggregate_by_fingerprint(self):
        def check():
            return self._query_rpc(
                "select public.gateway_log_query(p_module => 'planning')")
        result = self._as_service_role(check)
        groups = result["groups"]
        # planning 模块含两个 fingerprint（a 与旧事件 c），互不合并
        self.assertEqual(len(groups), 2)
        group = next(g for g in groups if g["fingerprint"] == "a" * 16)
        self.assertEqual(group["event_count"], 3)
        self.assertEqual(group["first_seen_at"], "2026-10-10T01:00:00+00:00")
        self.assertEqual(group["last_seen_at"], "2026-10-10T03:00:00+00:00")
        # 组内取最高级别：含一条 WARNING 与两条 ERROR → ERROR
        self.assertEqual(group["level"], "ERROR")
        self.assertEqual(group["error_code"], "internal_error")
        self.assertEqual(group["message_zh"], "规划接口发生内部错误")

    def test_different_modules_do_not_merge(self):
        def check():
            return self._query_rpc("select public.gateway_log_query()")
        result = self._as_service_role(check)
        fingerprints = {g["fingerprint"] for g in result["groups"]}
        self.assertIn("a" * 16, fingerprints)
        self.assertIn("b" * 16, fingerprints)
        self.assertIn("c" * 16, fingerprints)

    def test_level_and_time_filters(self):
        def check():
            warning_only = self._query_rpc(
                "select public.gateway_log_query(p_level => 'WARNING')")
            recent = self._query_rpc(
                "select public.gateway_log_query("
                "p_start => '2026-10-10T03:30:00+00:00')")
            return warning_only, recent
        warning_only, recent = self._as_service_role(check)
        # WARNING 只有 planning 组里那一条
        self.assertEqual(warning_only["groups"][0]["event_count"], 1)
        # 03:30 之后只有 chat 事件
        self.assertEqual(len(recent["groups"]), 1)
        self.assertEqual(recent["groups"][0]["module"], "chat")

    def test_search_matches_error_code_and_request_id(self):
        def check():
            by_code = self._query_rpc(
                "select public.gateway_log_query(p_search => 'upstream_timeout')")
            by_request = self._query_rpc(
                "select public.gateway_log_query(p_search => 'r-0000')")
            return by_code, by_request
        by_code, by_request = self._as_service_role(check)
        self.assertEqual(len(by_code["groups"]), 1)
        self.assertEqual(by_code["groups"][0]["module"], "chat")
        self.assertGreaterEqual(len(by_request["groups"]), 1)

    # ── 稳定 cursor 分页（L19） ─────────────────────────────────

    def test_cursor_pagination_without_overlap(self):
        def check():
            page1 = self._query_rpc(
                "select public.gateway_log_query(p_limit => 2)")
            groups1 = page1["groups"]
            self.assertTrue(page1["has_more"])
            seen = ",".join(g["fingerprint"] for g in groups1)
            page2 = self._query_rpc(
                "select public.gateway_log_query(p_limit => 2, p_seen => %s)",
                (seen,),
            )
            return groups1, page2["groups"], page2["has_more"]
        groups1, groups2, has_more2 = self._as_service_role(check)
        self.assertEqual(len(groups1), 2)
        fingerprints1 = {g["fingerprint"] for g in groups1}
        fingerprints2 = {g["fingerprint"] for g in groups2}
        self.assertFalse(fingerprints1 & fingerprints2)
        # 种子共 3 组：第二页只剩最旧的一组，没有更多
        self.assertEqual(len(groups2), 1)
        self.assertFalse(has_more2)
        # 降序：第二页的 last_seen 严格早于第一页最后一组
        self.assertLess(groups2[-1]["last_seen_at"], groups1[-1]["last_seen_at"])  # ISO 字符串同格式可字典序比较

    def test_snapshot_freezes_pagination_against_new_events(self):
        """BUG-05：入库快照冻结翻页会话——快照后入库的组不进入本会话。"""
        def check():
            # 第一页：建立快照（此刻之后入库的事件不属于本会话）
            snapshot = "2026-10-10T12:00:00+00:00"
            page1 = self._query_rpc(
                "select public.gateway_log_query(p_limit => 2, p_snapshot => %s)",
                (snapshot,))
            groups1 = page1["groups"]
            self.assertTrue(page1["has_more"])
            seen = ",".join(g["fingerprint"] for g in groups1)
            # 翻页期间：C 组新增一条更晚的事件（occurred_at 晚于第一页所有组），
            # 且入库时间晚于快照（延迟补写形态）——不进入本会话
            self.conn.execute(_event_sql(
                "00000000-0000-0000-0000-000000000031", "2026-10-10T05:00:00+00:00",
                fingerprint="c" * 16, ingested_at="2026-10-10T13:00:00+00:00"))
            page2 = self._query_rpc(
                "select public.gateway_log_query("
                "p_limit => 2, p_seen => %s, p_snapshot => %s)",
                (seen, snapshot),
            )
            # 对照：不带快照的同一翻页能看到补写后的 C 组（新 last_seen）
            page2_nosnapshot = self._query_rpc(
                "select public.gateway_log_query(p_limit => 2, p_seen => %s)",
                (seen,),
            )
            return groups1, page2["groups"], page2_nosnapshot["groups"]
        groups1, groups2, groups2_nosnapshot = self._as_service_role(check)
        fingerprints1 = {g["fingerprint"] for g in groups1}
        fingerprints2 = {g["fingerprint"] for g in groups2}
        # 快照内：第二页仍能看到 C 组（旧事件），不因快照外补写消失
        self.assertIn("c" * 16, fingerprints2)
        self.assertFalse(fingerprints1 & fingerprints2)
        # 对照：无快照时补写后的 C 组以新 last_seen 进入第二页
        self.assertIn("c" * 16, {g["fingerprint"] for g in groups2_nosnapshot})

    def test_late_commit_group_appears_on_next_page(self):
        """BUG-05（核心）：INSERT 未提交 → 第一页 → 提交 → 第二页可见。

        两个真实连接驱动：C 的事务在第一页查询之前开始 INSERT（默认
        now() 的 ingested_at 早于快照）、在第一页之后提交。已看集合分页
        下，晚提交的组按排序位置落入第二页——不被 keyset 游标漏掉。
        """
        now = datetime.now(timezone.utc)

        def insert(connection, fp, occurred):
            connection.execute(
                "insert into public.gateway_error_events "
                "(event_id, occurred_at, level, module, fingerprint) "
                "values (%s,%s,'ERROR','planning',%s)",
                (uuid.uuid4(), occurred, fp))

        for fp, hours in [("a" * 16, 1), ("b" * 16, 2), ("c" * 16, 3)]:
            insert(self.conn, fp, now - timedelta(hours=hours))
        pending = psycopg.connect(self.server.get_uri(), autocommit=False)
        try:
            # C 组新事件：事务开始（ingested_at）早于第一页，提交晚于第一页
            insert(pending, "c" * 16, now - timedelta(minutes=10))
            snapshot = datetime.now(timezone.utc)

            def check():
                page1 = self._query_rpc(
                    "select public.gateway_log_query(p_limit => 2, p_snapshot => %s)",
                    (snapshot,))
                self.assertTrue(page1["has_more"])
                return page1["groups"]

            groups1 = self._as_service_role(check)
            pending.commit()
            seen = ",".join(g["fingerprint"] for g in groups1)

            def check2():
                return self._query_rpc(
                    "select public.gateway_log_query("
                    "p_limit => 2, p_seen => %s, p_snapshot => %s)",
                    (seen, snapshot))

            page2 = self._as_service_role(check2)
        finally:
            pending.close()
        fingerprints1 = {g["fingerprint"] for g in groups1}
        fingerprints2 = {g["fingerprint"] for g in page2["groups"]}
        # 晚提交的 C 组（ingested_at 不晚于快照）在第二页可见，不漏组
        self.assertIn("c" * 16, fingerprints2)
        # 已看组不重现
        self.assertFalse(fingerprints1 & fingerprints2)

    def test_seen_group_aggregation_frozen_against_late_commit(self):
        """BUG-05（残留验收）：已看组 A 的晚提交事件不改变会话内聚合。

        两个真实连接、不手填 ingested_at：A 的新事件事务在第一页前
        INSERT、第一页后 COMMIT。同一会话（p_snapshot + p_visible）内
        重查列表与详情，A 的次数 / 末次保持第一页口径；新会话（不带
        p_visible）才看到 2 次——「刷新才变化」。
        """
        now = datetime.now(timezone.utc)

        def insert(connection, fp, occurred):
            connection.execute(
                "insert into public.gateway_error_events "
                "(event_id, occurred_at, level, module, fingerprint) "
                "values (%s,%s,'ERROR','planning',%s)",
                (uuid.uuid4(), occurred, fp))

        # A 组已有 1 条已提交可见事件
        insert(self.conn, "f" * 16, now - timedelta(hours=2))
        pending = psycopg.connect(self.server.get_uri(), autocommit=False)
        try:
            # A 组新事件：事务开始早于第一页，提交晚于第一页
            insert(pending, "f" * 16, now - timedelta(minutes=5))
            snapshot = datetime.now(timezone.utc)

            def page1():
                result = self._query_rpc(
                    "select public.gateway_log_query(p_snapshot => %s)",
                    (snapshot,))
                group = next(
                    g for g in result["groups"] if g["fingerprint"] == "f" * 16)
                return result, group

            result1, group1 = self._as_service_role(page1)
            self.assertEqual(group1["event_count"], 1)
            visible = result1["visible_snapshot"]
            self.assertTrue(visible)
            pending.commit()

            def same_session():
                # 同一会话重查列表（1→2 漂移必须在会话内消失）
                relist = self._query_rpc(
                    "select public.gateway_log_query(p_snapshot => %s, p_visible => %s)",
                    (snapshot, visible))
                # 同一会话详情：次数 / 末次与列表一致
                detail = self._query_rpc(
                    "select public.gateway_log_group(%s, p_snapshot => %s, p_visible => %s)",
                    ("f" * 16, snapshot, visible))
                # 新会话（刷新）：晚提交事件可见
                fresh = self._query_rpc(
                    "select public.gateway_log_query(p_snapshot => %s)",
                    (snapshot,))
                return relist, detail, fresh

            relist, detail, fresh = self._as_service_role(same_session)
        finally:
            pending.close()
        group_relist = next(
            g for g in relist["groups"] if g["fingerprint"] == "f" * 16)
        self.assertEqual(group_relist["event_count"], 1, "会话内重查列表不漂移")
        self.assertEqual(
            group_relist["last_seen_at"], group1["last_seen_at"],
            "会话内末次时间不漂移")
        self.assertEqual(
            detail["group"]["event_count"], 1, "会话内详情次数与列表一致")
        self.assertEqual(
            detail["group"]["last_seen_at"], group1["last_seen_at"])
        group_fresh = next(
            g for g in fresh["groups"] if g["fingerprint"] == "f" * 16)
        self.assertEqual(
            group_fresh["event_count"], 2, "新会话（刷新）才看到晚提交事件")

    def test_key_ref_groups_and_filters_events(self):
        """TASK-02（L29）：同功能不同密钥分组；按完整 key_ref 筛选。"""
        def seed():
            # 同模块/操作/错误码，两把不同密钥 → 应用层产出两个 fingerprint
            self.conn.execute(
                "insert into public.gateway_error_events "
                "(event_id, occurred_at, level, module, operation, error_code, "
                " fingerprint, key_ref, credential_role, provider) values "
                "('00000000-0000-0000-0000-000000000051', "
                "'2026-10-10T07:00:00+00:00','ERROR','chat','chat_completions',"
                "'upstream_status_error','e1e1e1e1e1e1e1e1',"
                "'kh1-11111111111111111111111111111111','chat','api.example.com')")
            self.conn.execute(
                "insert into public.gateway_error_events "
                "(event_id, occurred_at, level, module, operation, error_code, "
                " fingerprint, key_ref, credential_role, provider) values "
                "('00000000-0000-0000-0000-000000000052', "
                "'2026-10-10T08:00:00+00:00','ERROR','chat','chat_completions',"
                "'upstream_status_error','e2e2e2e2e2e2e2e2',"
                "'kh1-22222222222222222222222222222222','chat','api.example.com')")
            # 未关联密钥的历史事件（key_ref 空）
            self.conn.execute(
                "insert into public.gateway_error_events "
                "(event_id, occurred_at, level, module, operation, error_code, "
                " fingerprint) values "
                "('00000000-0000-0000-0000-000000000053', "
                "'2026-10-10T09:00:00+00:00','ERROR','chat','chat_completions',"
                "'upstream_status_error','e3e3e3e3e3e3e3e3')")

        self._as_service_role(seed)

        def check():
            all_groups = self._query_rpc(
                "select public.gateway_log_query(p_module => 'chat')")
            key1 = self._query_rpc(
                "select public.gateway_log_query(p_key_ref => %s)",
                ("kh1-11111111111111111111111111111111",))
            facets = self._query_rpc("select public.gateway_log_facets()")
            detail = self._query_rpc(
                "select public.gateway_log_group(%s)",
                ("e1e1e1e1e1e1e1e1",))
            return all_groups, key1, facets, detail

        all_groups, key1, facets, detail = self._as_service_role(check)
        # chat 模块：种子 1 组（未关联密钥）+ 两把密钥各 1 组 + 未关联新事件
        # 1 组 = 4 组；三种身份（两把密钥 + 未关联）互不合并
        self.assertEqual(len(all_groups["groups"]), 4)
        # 组投影携带密钥标识 / 用途 / 上游
        by_ref = {g["key_ref"]: g for g in all_groups["groups"]}
        self.assertIn("kh1-11111111111111111111111111111111", by_ref)
        self.assertEqual(by_ref["kh1-11111111111111111111111111111111"]["credential_role"], "chat")
        self.assertEqual(by_ref["kh1-11111111111111111111111111111111"]["provider"], "api.example.com")
        # 按完整 key_ref 筛选：只返回该密钥的组
        self.assertEqual(len(key1["groups"]), 1)
        self.assertEqual(key1["groups"][0]["fingerprint"], "e1e1e1e1e1e1e1e1")
        # facets 提供密钥选项（空 key_ref 不进入选项）
        refs = {k["key_ref"] for k in facets["keys"]}
        self.assertEqual(refs, {
            "kh1-11111111111111111111111111111111",
            "kh1-22222222222222222222222222222222"})
        # 详情样本携带密钥标识
        self.assertEqual(detail["samples"][0]["key_ref"], "kh1-11111111111111111111111111111111")

    def test_new_events_after_snapshot_do_not_join_session(self):
        """BUG-05：快照后真实新增（晚 occurred_at + 晚 ingested_at）不进入会话。"""
        # 快照后新增一个更晚的组（入库时间晚于快照）
        self.conn.execute(_event_sql(
            "00000000-0000-0000-0000-000000000041", "2026-10-10T06:00:00+00:00",
            fingerprint="e" * 16, ingested_at="2026-10-10T13:00:00+00:00"))
        result = self._as_service_role(
            lambda: self._query_rpc(
                "select public.gateway_log_query(p_snapshot => %s)",
                ("2026-10-10T12:00:00+00:00",)))
        fingerprints = {g["fingerprint"] for g in result["groups"]}
        self.assertNotIn("e" * 16, fingerprints)
        # 不带快照的查询能看到新组
        result3 = self._as_service_role(
            lambda: self._query_rpc("select public.gateway_log_query()"))
        self.assertIn("e" * 16, {g["fingerprint"] for g in result3["groups"]})

    def test_list_projection_omits_raw_and_stack(self):
        """BUG-16：列表投影不含 message_raw / stack_trace 大字段。"""
        def check():
            return self._query_rpc("select public.gateway_log_query()")
        result = self._as_service_role(check)
        for group in result["groups"]:
            with self.subTest(fingerprint=group["fingerprint"]):
                self.assertNotIn("sample_message_raw", group)
                self.assertNotIn("sample_stack_trace", group)
                self.assertNotIn("sample_truncated_fields", group)
                # 标识类字段保留（小字段）
                self.assertIn("sample_event_id", group)
                self.assertIn("sample_request_id", group)

    def test_group_detail_applies_level_and_search_filters(self):
        """BUG-06：详情复用列表筛选——同组混合级别 / request_id 时口径一致。"""
        def check():
            list_warning = self._query_rpc(
                "select public.gateway_log_query(p_level => 'WARNING')")
            detail_warning = self._query_rpc(
                "select public.gateway_log_group(%s, p_level => 'WARNING')",
                ("a" * 16,))
            detail_search = self._query_rpc(
                "select public.gateway_log_group(%s, p_search => 'r-0000')",
                ("a" * 16,))
            return list_warning, detail_warning, detail_search
        list_warning, detail_warning, detail_search = self._as_service_role(check)
        # 列表 WARNING 过滤：组 a 只有 1 条
        group_list = next(
            g for g in list_warning["groups"] if g["fingerprint"] == "a" * 16)
        self.assertEqual(group_list["event_count"], 1)
        # 详情同条件：次数与级别和列表一致（不再静默扩大到整组 3 条）
        group = detail_warning["group"]
        self.assertEqual(group["event_count"], 1)
        self.assertEqual(group["level"], "WARNING")
        samples = detail_warning["samples"]
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0]["level"], "WARNING")
        # 搜索 request_id 前缀：样本全部来自匹配事件
        for sample in detail_search["samples"]:
            self.assertTrue(sample["request_id"].startswith("r-0000"))

    # ── 详情（L01 / 样本范围） ──────────────────────────────────

    def test_group_detail_returns_summary_and_limited_samples(self):
        def check():
            return self._query_rpc(
                "select public.gateway_log_group(%s, p_sample_limit => 2)",
                ("a" * 16,))
        result = self._as_service_role(check)
        group = result["group"]
        self.assertEqual(group["event_count"], 3)
        samples = result["samples"]
        self.assertEqual(len(samples), 2)
        # 样本按时间降序，且写明样本范围由调用方控制
        self.assertGreaterEqual(
            samples[0]["occurred_at"], samples[1]["occurred_at"])
        self.assertIn("stack_trace", samples[0])

    def test_group_detail_missing_fingerprint_returns_empty(self):
        def check():
            return self._query_rpc(
                "select public.gateway_log_group(%s)", ("d" * 16,))
        result = self._as_service_role(check)
        self.assertIsNone(result["group"])
        self.assertEqual(result["samples"], [])

    def test_event_detail_found_and_missing(self):
        def check():
            found = self._query_rpc(
                "select public.gateway_log_event(%s)",
                ("00000000-0000-0000-0000-000000000001",))
            missing = self._query_rpc(
                "select public.gateway_log_event(%s)",
                ("ffffffff-ffff-ffff-ffff-ffffffffffff",))
            return found, missing
        found, missing = self._as_service_role(check)
        self.assertEqual(found["event"]["module"], "planning")
        self.assertIn("stack_trace", found["event"])
        self.assertIsNone(missing)

    # ── facets ──────────────────────────────────────────────────

    def test_facets_report_modules_levels_and_range(self):
        def check():
            return self._query_rpc("select public.gateway_log_facets()")
        result = self._as_service_role(check)
        modules = {m["module"]: m["count"] for m in result["modules"]}
        self.assertEqual(modules.get("planning"), 4)
        self.assertEqual(modules.get("chat"), 1)
        levels = {l["level"]: l["count"] for l in result["levels"]}
        self.assertEqual(levels.get("WARNING"), 1)
        self.assertGreaterEqual(levels.get("ERROR"), 4)
        self.assertTrue(result["oldest_at"])
        self.assertTrue(result["newest_at"])

    # ── 清理（L23） ─────────────────────────────────────────────

    def test_cleanup_deletes_only_expired_events(self):
        def check():
            before = self.conn.execute(
                "select count(*) from public.gateway_error_events").fetchone()[0]
            result = self._query_rpc(
                "select public.gateway_log_cleanup(p_retention_days => 30)")
            after = self.conn.execute(
                "select count(*) from public.gateway_error_events").fetchone()[0]
            return before, after, result
        before, after, result = self._as_service_role(check)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["deleted"], 1)  # 只有 40 天前的旧事件
        self.assertEqual(after, before - 1)
        remaining = self._as_service_role(lambda: self.conn.execute(
            "select count(*) from public.gateway_error_events "
            "where fingerprint = %s", ("c" * 16,)).fetchone()[0])
        self.assertEqual(remaining, 0)

    def test_cleanup_skips_when_advisory_lock_held(self):
        # 模拟另一实例正在清理：持有同名 advisory xact 锁时返回 skipped_locked
        lock_conn = psycopg.connect(self.server.get_uri(), autocommit=False)
        try:
            lock_conn.execute(
                "select pg_advisory_xact_lock(hashtext('gateway_log_cleanup'))")
            def check():
                return self._query_rpc(
                    "select public.gateway_log_cleanup(p_retention_days => 30)")
            result = self._as_service_role(check)
            self.assertEqual(result["status"], "skipped_locked")
            self.assertEqual(result["deleted"], 0)
        finally:
            lock_conn.rollback()
            lock_conn.close()


if __name__ == "__main__":
    unittest.main()
