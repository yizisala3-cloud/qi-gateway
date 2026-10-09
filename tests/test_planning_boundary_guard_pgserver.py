"""批次 9 Review HIGH #2/#3/#4 真库权威验证（boundary/window 写入互斥守卫）。

Opt-in（启动一次性 pgserver 实例，成本较高）：

    QIGATEWAY_PG_PLANNING_TEST=1 python -m pytest \
        tests/test_planning_boundary_guard_pgserver.py -v

按顺序重放全部迁移（含 20260930020000 boundary RPC 与 20260930030000
守卫），随后在真实 PostgreSQL 双连接上证明：

- HIGH #2：首次 boundary 修改（无状态行）与并发首写交错 → 请求被拒绝
  （stale_state）时任务模板调整**零写入**——拒绝路径严格先于任何任务
  UPDATE；fresh migration 初始态下 stale / conflicts / 校验异常三条拒绝
  路径同样零写入；
- HIGH #3：active 任务创建与 boundary 修改经同一 advisory lock 串行化——
  真实交错下要么任务基于新 boundary 被守卫拒绝、要么 boundary 全量校验
  发现任务并整体拒绝，绝不允许「新 boundary + 非法 active 任务」组合；
- HIGH #4：inactive 任务重新启用由守卫按当前 configured boundary 重校验，
  非法拒绝、零写入；修正窗口后重新启用成功；
- 守卫不破坏 boundary RPC 携带调整的成功路径（调整后窗口合法落库）。

被测迁移文件逐字节从磁盘执行；测试绝不触碰任何生产数据库。
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"

try:
    import pgserver
    import psycopg
    from test_planning_phase1r_pgserver_integration import (
        BASELINE_SQL, _ensure_pg_timezone_data,
    )
    _STACK_AVAILABLE = True
except ImportError:  # pragma: no cover - optional heavyweight stack
    _STACK_AVAILABLE = False

BOUNDARY_KEY = "planning.refresh_boundary_state"
DEFAULT_EXPECTED = '{"boundary": "06:00", "transition": null}'
LOCK_SQL = (
    "select pg_advisory_xact_lock("
    "hashtextextended('planning.refresh_boundary_state', 0))"
)

ADD_TASK_SQL = """
insert into public.planning_task (
    content, task_type, time_mode, estimated_minutes, is_active,
    refresh_mode, refresh_enabled, created_at, updated_at,
    window_start_tod, window_end_tod
) values (
    %s, 'daily', 'duration', 30, %s,
    'daily', true, '2026-09-23T07:00:00+08:00', '2026-09-23T07:00:00+08:00',
    %s, %s
) returning id
"""

CROSSING_INSERT_SQL = """
insert into public.planning_task (
    content, task_type, time_mode, estimated_minutes, is_active,
    refresh_mode, refresh_enabled, created_at, updated_at,
    window_start_tod, window_end_tod
) values (
    %s, 'daily', 'duration', 30, true,
    'daily', true, '2026-09-23T07:00:00+08:00', '2026-09-23T07:00:00+08:00',
    %s, %s
)
"""

BOUNDARY_RPC_SQL = """
select public.planning_update_cycle_boundary(
    %s::time, %s::jsonb, null::jsonb, '[]'::jsonb, %s::jsonb)
"""


class BoundaryWindowGuardOnPostgresTests(unittest.TestCase):
    """真实 PostgreSQL 上的 boundary/window 写入互斥不变量。"""

    @classmethod
    def setUpClass(cls):
        if not _STACK_AVAILABLE:
            raise unittest.SkipTest("pgserver + psycopg are not installed")
        if os.environ.get("QIGATEWAY_PG_PLANNING_TEST") != "1":
            raise unittest.SkipTest(
                "set QIGATEWAY_PG_PLANNING_TEST=1 (or run pytest --db) to run "
                "the real PostgreSQL boundary guard test"
            )
        cls.pgdata = Path(tempfile.mkdtemp(prefix="qigate-boundary-guard-"))
        cls.server = None
        cls.conn = None
        try:
            _ensure_pg_timezone_data()
            cls.server = pgserver.get_server(cls.pgdata, cleanup_mode="stop")
            cls.conn = psycopg.connect(cls.server.get_uri(), autocommit=True)
            cls.conn.execute(BASELINE_SQL)
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                sql = path.read_text(encoding="utf-8")
                if "pg_trgm" in sql:
                    sql = sql.replace(
                        "create extension if not exists pg_trgm with schema extensions;",
                        "-- pg_trgm stubbed (pgserver)")
                cls.conn.execute(sql)
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

    def setUp(self):
        # 每个用例从 deterministic 初始态开始：boundary 06:00、空任务表
        self.conn.execute("delete from public.planning_occurrence")
        self.conn.execute("delete from public.planning_task")
        self.conn.execute(
            "update public.app_settings set value = %s where key = %s",
            ('{"boundary": "06:00", "transition": null, "absorbed": []}',
             BOUNDARY_KEY))

    def _add_task(self, content, start, end, active=True):
        return self.conn.execute(
            ADD_TASK_SQL, (content, active, start, end)).fetchone()[0]

    def _task_window(self, task_id):
        row = self.conn.execute(
            "select window_start_tod, window_end_tod from public.planning_task"
            " where id = %s", (task_id,)).fetchone()
        return tuple(str(v) if v is not None else None for v in row)

    def _boundary(self):
        return self.conn.execute(
            "select value->>'boundary' from public.app_settings where key = %s",
            (BOUNDARY_KEY,)).fetchone()[0]

    def _call_rpc(self, conn, new_boundary, expected, adjustments="[]"):
        return conn.execute(
            BOUNDARY_RPC_SQL, (new_boundary, expected, adjustments)).fetchone()[0]

    def _crossing_insert(self, conn, start, end, content):
        """插入跨越当前 configured boundary 的任务；返回异常消息（无异常
        时返回 None——调用方必须断言结果方向）。"""
        try:
            conn.execute(CROSSING_INSERT_SQL, (content, start, end))
            conn.commit()
            return None
        except psycopg.errors.RaiseException as exc:
            conn.rollback()
            return str(exc)

    def _rpc_connection(self):
        return psycopg.connect(self.server.get_uri())

    # -- HIGH #2：拒绝路径零写入 -----------------------------------------

    def test_high2_first_write_race_stale_rejection_leaves_tasks_untouched(self):
        # 无状态行 + 并发首写：B（携带任务调整）阻塞在缺省行补插上；A 提交
        # 非缺省状态行；B 恢复 → 补插 no-op → 重核 stale → 零任务写入。
        self.conn.execute(
            "delete from public.app_settings where key = %s", (BOUNDARY_KEY,))
        t1 = self._add_task("背单词", "10:00", "14:00")
        a = self._rpc_connection()
        a.execute("begin")
        a.execute(
            "insert into public.app_settings (key, value) values (%s, %s)",
            (BOUNDARY_KEY,
             '{"boundary": "15:00", "transition": null, "absorbed": []}'))
        holder = {}

        def worker():
            b = self._rpc_connection()
            try:
                holder["r"] = self._call_rpc(
                    b, "05:00", DEFAULT_EXPECTED,
                    json.dumps([{"task_id": t1, "window_start_tod": "16:00",
                                 "window_end_tod": "18:00"}]))
                b.commit()
            finally:
                b.close()

        th = threading.Thread(target=worker)
        th.start()
        try:
            time.sleep(1.0)
            blocked = self.conn.execute(
                "select count(*) from pg_stat_activity"
                " where wait_event_type = 'Lock'").fetchone()[0]
            self.assertTrue(blocked, "B 未阻塞在缺省行补插上")
            a.commit()  # A 提交非缺省状态行 → B 重核 stale
        finally:
            th.join(timeout=20)
            a.close()
        self.assertFalse(th.is_alive())
        self.assertEqual(holder["r"]["status"], "stale_state")
        self.assertEqual(self._task_window(t1), ("10:00:00", "14:00:00"))
        self.assertEqual(self._boundary(), "15:00")

    def test_high2_stale_rejection_zero_writes_on_fresh_initial_state(self):
        t1 = self._add_task("背单词", "10:00", "14:00")
        b = self._rpc_connection()
        r = self._call_rpc(
            b, "05:00", '{"boundary": "07:00", "transition": null}')
        b.commit()
        b.close()
        self.assertEqual(r["status"], "stale_state")
        self.assertEqual(self._task_window(t1), ("10:00:00", "14:00:00"))
        self.assertEqual(self._boundary(), "06:00")

    def test_high2_conflict_rejection_leaves_adjusted_task_untouched(self):
        t1 = self._add_task("任务A", "10:00", "14:00")
        cross = self._add_task("任务B-跨新边界", "10:00", "14:00")
        b = self._rpc_connection()
        r = self._call_rpc(
            b, "12:00", DEFAULT_EXPECTED,
            json.dumps([{"task_id": t1, "window_start_tod": "16:00",
                         "window_end_tod": "18:00"}]))
        b.commit()
        b.close()
        self.assertEqual(r["status"], "conflicts")
        conflict_ids = {c["task_id"] for c in r["conflicts"]}
        self.assertIn(cross, conflict_ids)
        self.assertNotIn(t1, conflict_ids)  # 调整者按提交值校验：16–18 不跨 12:00
        self.assertEqual(self._task_window(t1), ("10:00:00", "14:00:00"))
        self.assertEqual(self._task_window(cross), ("10:00:00", "14:00:00"))
        self.assertEqual(self._boundary(), "06:00")

    def test_high2_validation_exception_rolls_back_whole_request(self):
        t1 = self._add_task("背单词", "10:00", "14:00")
        b = self._rpc_connection()
        with self.assertRaises(psycopg.errors.DataError):
            self._call_rpc(
                b, "05:00", DEFAULT_EXPECTED,
                json.dumps([{"task_id": t1, "window_start_tod": "25:00",
                             "window_end_tod": None}]))
        b.rollback()
        b.close()
        self.assertEqual(self._task_window(t1), ("10:00:00", "14:00:00"))
        self.assertEqual(self._boundary(), "06:00")

    # -- HIGH #3：并发创建不能穿过 boundary 校验窗口 ----------------------

    def test_high3_concurrent_create_rejected_under_new_boundary(self):
        # A 持守卫锁（boundary RPC 关键段等价物）→ B 插入跨越「准备生效的
        # 新 boundary」的任务 → B 阻塞 → A 在持锁下提交 boundary RPC →
        # B 恢复：守卫按新 boundary 重校验拒绝。终态无非法组合。
        a = self._rpc_connection()
        a.execute("begin")
        a.execute(LOCK_SQL)
        holder = {}

        def worker():
            b = self._rpc_connection()
            try:
                holder["r"] = self._crossing_insert(b, "10:00", "14:00", "并发任务")
            finally:
                b.close()

        th = threading.Thread(target=worker)
        th.start()
        try:
            time.sleep(1.0)
            blocked = self.conn.execute(
                "select count(*) from pg_stat_activity"
                " where wait_event_type = 'Lock'").fetchone()[0]
            self.assertTrue(blocked, "B 未被守卫锁阻塞")
            r = a.execute(
                BOUNDARY_RPC_SQL, ("12:00", DEFAULT_EXPECTED, "[]")).fetchone()[0]
            self.assertEqual(r["status"], "ok")
            a.commit()
        finally:
            th.join(timeout=20)
            a.close()
        self.assertFalse(th.is_alive())
        self.assertIsNotNone(holder["r"])
        self.assertIn("crosses the daily refresh boundary 12:00", holder["r"])
        self.assertEqual(
            self.conn.execute(
                "select count(*) from public.planning_task where content = '并发任务'"
            ).fetchone()[0], 0)
        self.assertEqual(self._boundary(), "12:00")

    def test_high3_committed_task_blocks_boundary_final_save(self):
        # 反向交错：任务先提交 → boundary 最终保存全量校验发现并整体拒绝。
        self._crossing_insert(self.conn, "10:00", "14:00", "先到任务")
        a = self._rpc_connection()
        r = self._call_rpc(a, "12:00", DEFAULT_EXPECTED)
        a.commit()
        a.close()
        self.assertEqual(r["status"], "conflicts")
        self.assertEqual(self._boundary(), "06:00")

    # -- HIGH #4：重新启用按当前 boundary 重校验（数据库守卫） ------------

    def test_high4_reenable_rejected_by_guard_and_stays_inactive(self):
        tid = self._add_task("重启用任务", "10:00", "14:00")
        self.conn.execute(
            "update public.planning_task set is_active = false where id = %s",
            (tid,))
        # inactive 不参与扫描：boundary 06:00 → 12:00 保存成功
        a = self._rpc_connection()
        r = self._call_rpc(a, "12:00", DEFAULT_EXPECTED)
        a.commit()
        a.close()
        self.assertEqual(r["status"], "ok")
        # 重新启用：守卫拒绝，任务保持 inactive、零写入
        with self.assertRaises(psycopg.errors.RaiseException) as caught:
            self.conn.execute(
                "update public.planning_task"
                " set is_active = true, updated_at = now() where id = %s", (tid,))
        self.assertIn("crosses the daily refresh boundary 12:00",
                      str(caught.exception))
        row = self.conn.execute(
            "select is_active, window_start_tod, window_end_tod"
            " from public.planning_task where id = %s", (tid,)).fetchone()
        self.assertEqual((row[0], str(row[1]), str(row[2])),
                         (False, "10:00:00", "14:00:00"))

    def test_high4_window_fixed_then_reenable_succeeds(self):
        tid = self._add_task("重启用任务", "10:00", "14:00")
        self.conn.execute(
            "update public.planning_task set is_active = false where id = %s",
            (tid,))
        a = self._rpc_connection()
        self.assertEqual(
            self._call_rpc(a, "12:00", DEFAULT_EXPECTED)["status"], "ok")
        a.commit()
        a.close()
        self.conn.execute(
            "update public.planning_task set window_start_tod = '14:00',"
            " window_end_tod = '16:00' where id = %s", (tid,))
        self.conn.execute(
            "update public.planning_task set is_active = true, updated_at = now()"
            " where id = %s", (tid,))
        self.assertTrue(self.conn.execute(
            "select is_active from public.planning_task where id = %s",
            (tid,)).fetchone()[0])

    # -- 守卫不破坏 boundary RPC 成功路径 ---------------------------------

    def test_guard_does_not_break_boundary_rpc_success_with_adjustment(self):
        t1 = self._add_task("调整任务", "10:00", "14:00")  # 跨新 boundary 12:00
        a = self._rpc_connection()
        r = self._call_rpc(
            a, "12:00", DEFAULT_EXPECTED,
            json.dumps([{"task_id": t1, "window_start_tod": "14:00",
                         "window_end_tod": "16:00"}]))
        a.commit()
        a.close()
        self.assertEqual(r["status"], "ok")
        self.assertEqual(r["updated_tasks"], 1)
        self.assertEqual(self._task_window(t1), ("14:00:00", "16:00:00"))
        self.assertEqual(self._boundary(), "12:00")


if __name__ == "__main__":
    unittest.main()
