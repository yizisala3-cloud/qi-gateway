"""Real disposable PostgreSQL tests of the canonical-memory export migration."""
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from datetime import datetime, timezone

try:
    import pgserver
    import psycopg
    AVAILABLE=True
except ImportError:
    AVAILABLE=False

from tests.test_migration_pgserver_integration import _ensure_pg_timezone_data

ROOT=Path(__file__).resolve().parents[1]
SQL=ROOT/"supabase/migrations/20261021010000_qqbot_chat_export.sql"
KEY="qqbot:"+"a"*64


@unittest.skipUnless(AVAILABLE and os.environ.get("QIGATEWAY_PG_MIGRATION_TEST")=="1","run pytest --db for disposable PostgreSQL")
class QQExportPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_pg_timezone_data()
        cls.directory=Path(tempfile.mkdtemp(prefix="qigate-qqbot-"))
        cls.server=pgserver.get_server(cls.directory,cleanup_mode="stop")
        cls.uri=cls.server.get_uri();cls.conn=psycopg.connect(cls.uri,autocommit=True)
        cls.conn.execute("""create role anon;create role authenticated;create role service_role bypassrls;
            grant usage on schema public to anon,authenticated,service_role;
            create table chat_messages(id bigint generated always as identity primary key,
              assistant_id text,conversation_id text,role text,content text,created_at timestamp);""")
        cls.conn.execute(SQL.read_text(encoding="utf-8"))
    @classmethod
    def tearDownClass(cls):
        cls.conn.close();cls.server.cleanup()
    def setUp(self):
        self.conn.execute("truncate qqbot_chat_exports,chat_messages restart identity")
    def publish(self,conn=None,content="真实QQ消息",key=KEY,role="user"):
        return (conn or self.conn).execute("select qqbot_publish_chat_message(%s,%s,%s,%s,%s,%s)",
            (key,"role","qq:12345:private:67890",role,content,datetime(2026,10,21,10,tzinfo=timezone.utc))).fetchone()[0]
    def test_idempotence_cst_timestamp_and_existing_history_preserved(self):
        self.conn.execute("insert into chat_messages(content) values ('手机原文')")
        first=self.publish();self.assertEqual(self.publish(),first)
        rows=self.conn.execute("select content,created_at from chat_messages order by id").fetchall()
        self.assertEqual(len(rows),2);self.assertEqual(rows[0][0],"手机原文")
        self.assertEqual(rows[1][1].hour,18)
    def test_payload_mismatch_rejected(self):
        self.publish()
        with self.assertRaises(psycopg.errors.InvalidParameterValue): self.publish(content="换内容")
        self.assertEqual(self.conn.execute("select count(*) from chat_messages").fetchone()[0],1)
    def test_concurrent_retry_writes_one_message(self):
        def call(_):
            with psycopg.connect(self.uri,autocommit=True) as conn: return self.publish(conn)
        with ThreadPoolExecutor(max_workers=4) as pool: ids=list(pool.map(call,range(8)))
        self.assertEqual(len(set(ids)),1)
        self.assertEqual(self.conn.execute("select count(*) from chat_messages").fetchone()[0],1)
    def test_private_table_and_rpc_only_service_role(self):
        for role in ("anon","authenticated"):
            self.conn.execute("set role "+role)
            try:
                with self.assertRaises(psycopg.errors.InsufficientPrivilege): self.publish()
                with self.assertRaises(psycopg.errors.InsufficientPrivilege): self.conn.execute("select * from qqbot_chat_exports")
            finally: self.conn.execute("reset role")
        self.conn.execute("set role service_role")
        try: self.assertIsInstance(self.publish(),int)
        finally: self.conn.execute("reset role")
    def test_invalid_key_and_role_rejected_without_rows(self):
        for key,role in [("bad","user"),(KEY,"system")]:
            with self.assertRaises(psycopg.errors.InvalidParameterValue): self.publish(key=key,role=role)
        self.assertEqual(self.conn.execute("select count(*) from chat_messages").fetchone()[0],0)
