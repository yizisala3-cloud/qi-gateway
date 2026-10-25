"""Ledger replacement must not reuse retained Supabase export receipts."""
import hashlib
import shutil
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier

from gateway.qqbot_store import QQStore


class QQExportNamespaceTests(unittest.TestCase):
    scope = '["bot","owner","role"]'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def legacy_key(self, message_key):
        return 'qqbot:' + hashlib.sha256((self.scope + ':' + message_key).encode()).hexdigest()

    def open_store(self, name='ledger.sqlite3'):
        store = QQStore(self.root / name, self.scope)
        self.addCleanup(store.close)
        return store

    def reply(self, store, text):
        store.accept(101, text, 100)
        batch = store.batch(104, 0, 0)
        store.queue('reply:1:1', 'reply', [], 'reply to ' + text, 104, 200, batch)
        task = store.claim_send(105)
        store.finish_send(task['id'], 'confirmed', 106, 'ack')
        return store.exports(107)

    def test_replacement_ledger_can_export_against_retained_old_receipts(self):
        old_receipt = self.legacy_key('qq-out:reply:1:1')
        receipts = {old_receipt: ('assistant', 'old reply')}
        seen = []
        for name, text in [('one.sqlite3', 'first'), ('two.sqlite3', 'second')]:
            store = self.open_store(name)
            rows = self.reply(store, text)
            for row in rows:
                key = store.export_key(row['message_key'])
                payload = (row['role'], row['content'])
                self.assertNotIn(key, receipts, 'fresh ledger reused a retained remote key')
                receipts[key] = payload
                seen.append(key)
        self.assertEqual(len(set(seen)), 4)
        self.assertEqual(receipts[old_receipt], ('assistant', 'old reply'))

    def test_restart_and_copy_keep_keys_for_ambiguous_retry(self):
        path = self.root / 'original.sqlite3'
        first = QQStore(path, self.scope)
        try:
            rows = self.reply(first, 'current')
            keys = [first.export_key(r['message_key']) for r in rows]
            first.export_result(rows[0]['id'], False, 110)
        finally:
            first.close()
        shutil.copy2(path, self.root / 'copied.sqlite3')
        for name in ['original.sqlite3', 'copied.sqlite3']:
            reopened = self.open_store(name)
            self.assertEqual(reopened.exports(111), [])
            retry = reopened.exports(171)
            self.assertEqual([reopened.export_key(r['message_key']) for r in retry], keys)

    def test_legacy_empty_and_populated_ledgers_keep_exact_old_keys(self):
        for populated in [False, True]:
            with self.subTest(populated=populated):
                path = self.root / ('legacy-' + str(populated) + '.sqlite3')
                with closing(sqlite3.connect(path)) as conn, conn:
                    conn.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                    conn.execute('INSERT INTO meta VALUES(?,?)', ('scope', self.scope))
                    if populated:
                        conn.execute('''CREATE TABLE history(id INTEGER PRIMARY KEY AUTOINCREMENT,
                            message_key TEXT NOT NULL UNIQUE,role TEXT NOT NULL,content TEXT NOT NULL,
                            created REAL NOT NULL,exported INTEGER NOT NULL DEFAULT 0,
                            export_after REAL NOT NULL DEFAULT 0)''')
                        conn.execute("INSERT INTO history(message_key,role,content,created) VALUES('qq-out:reply:1:1','assistant','old',100)")
                for unused in range(2):
                    store = QQStore(path, self.scope)
                    try:
                        for key in ['qq-in:101', 'qq-out:reply:1:1', 'qq-out:reply:2:2']:
                            self.assertEqual(store.export_key(key), self.legacy_key(key))
                        if populated:
                            self.assertEqual(store.history()[0]['content'], 'old')
                    finally:
                        store.close()

    def test_concurrent_first_open_persists_one_namespace(self):
        path = self.root / 'concurrent.sqlite3'
        with closing(sqlite3.connect(path)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
        barrier = Barrier(2)

        def open_and_key(unused):
            barrier.wait(timeout=5)
            store = QQStore(path, self.scope)
            try:
                return store.export_key('qq-out:reply:1:1')
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            keys = list(pool.map(open_and_key, range(2)))
        self.assertEqual(keys[0], keys[1])
        self.assertNotEqual(keys[0], self.legacy_key('qq-out:reply:1:1'))

    def test_invalid_persisted_namespace_fails_without_regenerating(self):
        path = self.root / 'damaged.sqlite3'
        store = QQStore(path, self.scope)
        store.conn.execute("INSERT OR REPLACE INTO meta VALUES('export_namespace','broken')")
        store.close()
        with self.assertRaisesRegex(ValueError, '命名空间'):
            unexpected = QQStore(path, self.scope)
            unexpected.close()
        with closing(sqlite3.connect(path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='export_namespace'").fetchone()[0], 'broken')
