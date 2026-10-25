"""Local durable inbox/outbox. One active writer per persistent SQLite file.

Canonical long-term memory stays in Supabase; this ledger also works offline.
Every outgoing action is claimed before I/O. In-flight crash recovery is unknown.
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .qqbot_proactive import initial_state, validate_state, complete

log = logging.getLogger('gateway.qqbot.store')

class QQStore:
    def __init__(self, path, scope):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._pending_receipts = {}
        self.conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS lease(id INTEGER PRIMARY KEY CHECK(id=1),owner TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY AUTOINCREMENT,message_key TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL,content TEXT NOT NULL,created REAL NOT NULL,
                exported INTEGER NOT NULL DEFAULT 0,export_after REAL NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS inbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,message_key TEXT NOT NULL UNIQUE,
                history_id INTEGER NOT NULL,created REAL NOT NULL,status TEXT NOT NULL DEFAULT 'pending');
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY AUTOINCREMENT,task_key TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL,payload TEXT NOT NULL,summary TEXT NOT NULL,
                created REAL NOT NULL,expires REAL NOT NULL,status TEXT NOT NULL DEFAULT 'queued',
                provider_id TEXT,error TEXT);
            CREATE INDEX IF NOT EXISTS inbox_pending ON inbox(status,id);
            CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(status,id);
            CREATE INDEX IF NOT EXISTS history_export ON history(exported,export_after,id);
            CREATE TABLE IF NOT EXISTS reply_inputs(
                task_key TEXT NOT NULL,history_id INTEGER NOT NULL,
                PRIMARY KEY(task_key,history_id));
            CREATE INDEX IF NOT EXISTS reply_input_history ON reply_inputs(history_id);
            CREATE TABLE IF NOT EXISTS send_receipts(
                task_id INTEGER PRIMARY KEY,receipt TEXT NOT NULL);
        """)
        with self.transaction():
            old = self.conn.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
            mismatch = bool(old and old[0] != scope)
            if not mismatch:
                namespace = self.conn.execute("SELECT value FROM meta WHERE key='export_namespace'").fetchone()
                if namespace is None:
                    # A pre-existing scope belongs to a legacy ledger: its remote
                    # receipts must retain their original keys, even when empty.
                    value = '' if old else uuid4().hex
                    self.conn.execute("INSERT INTO meta VALUES('export_namespace',?)", (value,))
                else:
                    value = namespace[0]
                self.export_namespace = value
                self.conn.execute("INSERT OR IGNORE INTO meta VALUES('scope',?)", (scope,))
                columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(inbox)')}
                if 'claim_owner' not in columns:
                    self.conn.execute('ALTER TABLE inbox ADD COLUMN claim_owner TEXT')
                outbox_columns = {r['name'] for r in self.conn.execute('PRAGMA table_info(outbox)')}
                if 'eligibility_revision' not in outbox_columns:
                    self.conn.execute('ALTER TABLE outbox ADD COLUMN eligibility_revision INTEGER')
                if not self.conn.execute("SELECT 1 FROM meta WHERE key='reply_links_v1'").fetchone():
                    # Existing local ledgers encode their input range in the durable task key.
                    for row in self.conn.execute("SELECT task_key FROM outbox WHERE kind='reply'").fetchall():
                        parts = row['task_key'].split(':')
                        if len(parts)==3 and parts[0]=='reply' and all(p.isdecimal() for p in parts[1:]):
                            self.conn.execute('INSERT OR IGNORE INTO reply_inputs SELECT ?,history_id FROM inbox WHERE id BETWEEN ? AND ?',
                                              (row['task_key'],int(parts[1]),int(parts[2])))
                        elif row['task_key'].startswith('control:'):
                            self.conn.execute('INSERT OR IGNORE INTO reply_inputs SELECT ?,history_id FROM inbox WHERE message_key=?',
                                              (row['task_key'],'qq-in:'+row['task_key'][8:]))
                    self.conn.execute("INSERT INTO meta VALUES('reply_links_v1','1')")
        if mismatch:
            self.conn.close()
            raise ValueError("QQ账本已绑定其他账号或角色，请使用独立存储文件")
        if self.export_namespace and (len(self.export_namespace) != 32 or
                any(c not in '0123456789abcdef' for c in self.export_namespace)):
            self.conn.close()
            raise ValueError("QQ账本导出命名空间损坏，请恢复账本备份")

    @contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    def close(self):
        with self.lock:
            self._journal_receipts()
            self.conn.close()

    def acquire(self, owner, now):
        with self.transaction():
            row = self.conn.execute("SELECT * FROM lease WHERE id=1").fetchone()
            if row and row["owner"] != owner and row["expires"] > now:
                return False
            self.conn.execute("INSERT OR REPLACE INTO lease VALUES(1,?,?)", (owner, now+30))
            return True

    def release(self, owner):
        with self.transaction():
            self.conn.execute("DELETE FROM lease WHERE owner=?", (owner,))

    def owns(self, owner, now):
        with self.lock:
            return bool(self.conn.execute("SELECT 1 FROM lease WHERE owner=? AND expires>?",(owner,now)).fetchone())

    def recover(self, now=0, day=""):
        try:
            self.retry_receipts(now)
        except sqlite3.Error as exc:
            # Known results remain protected by the journal (or the live cache)
            # while unrelated interrupted sends still recover as unknown.
            log.warning('QQ回执恢复等待数据库 pending=%s type=%s',len(self._pending_receipts),type(exc).__name__,exc_info=exc)
        with self.transaction():
            protected = list(self._pending_receipts)
            condition = "status='in_flight' AND NOT EXISTS(SELECT 1 FROM send_receipts r WHERE r.task_id=outbox.id)"
            if protected:
                condition += ' AND id NOT IN ('+','.join('?' for _ in protected)+')'
            uncertain = self.conn.execute("SELECT 1 FROM outbox WHERE kind='proactive' AND "+condition+' LIMIT 1',protected).fetchone()
            self.conn.execute("UPDATE inbox SET status='pending',claim_owner=NULL WHERE status='working'")
            self.conn.execute("UPDATE outbox SET status='unknown',error='restart_in_flight',payload='[]' WHERE "+condition,protected)
            if uncertain:
                state = self.state()
                state.update(last_sent=now,unanswered=min(10,state["unanswered"]+1),
                             day=day,count=(state["count"] if state["day"]==day else 0)+1)
                self._save_state(state)

    def state(self):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schedule'").fetchone()
        return json.loads(row[0]) if row else {"paused":False,"silence_until":0,"revision":0,
            "last_incoming":0,"next_check":0,"last_sent":0,"unanswered":0,"day":"","count":0}

    def _save_state(self, state):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES('schedule',?)", (json.dumps(state),))

    def evaluation_state(self):
        with self.lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key='proactive_evaluation'").fetchone()
            return validate_state(json.loads(row[0])) if row else initial_state()

    def _save_evaluation(self, state):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES('proactive_evaluation',?)",(json.dumps(state),))

    def begin_evaluation(self, now, delay, owner):
        with self.transaction():
            state, evaluation = self.state(), self.evaluation_state()
            if (not self.owns(owner,now) or evaluation['fault_stopped'] or evaluation['in_flight']
                    or evaluation['next_check_at'] > now or state['next_check'] > now
                    or state['paused'] or state['silence_until'] > now or self.pending() or self.open_proactive()):
                return None
            evaluation.update(in_flight=True,attempt_id=uuid4().hex,attempt_revision=state['revision'],
                next_check_at=now+delay)
            self._save_evaluation(evaluation)
            return dict(evaluation)

    def finish_evaluation(self, attempt, outcome, now, delay, owner, **failure):
        with self.transaction():
            evaluation = self.evaluation_state()
            if (not self.owns(owner,now) or not evaluation['in_flight']
                    or evaluation['attempt_id'] != attempt['attempt_id']
                    or evaluation['generation'] != attempt['generation']):
                return None
            if self.state()['revision'] != attempt['attempt_revision']:
                outcome, failure = 'cancelled', {}
            evaluation = complete(evaluation,outcome,now,delay,**failure)
            self._save_evaluation(evaluation)
            if evaluation['fault_stopped']:
                self._invalidate_proactive('evaluation_fault_stopped')
            return {'outcome':outcome, **evaluation}

    def recover_evaluation(self, now, delay, owner):
        with self.transaction():
            evaluation = self.evaluation_state()
            if not self.owns(owner,now) or not evaluation['in_flight']:
                return None
            cancelled = self.state()['revision'] != evaluation['attempt_revision']
            outcome = 'cancelled' if cancelled else 'error'
            evaluation = complete(evaluation,outcome,now,delay,**({} if cancelled else {'code':'interrupted'}))
            evaluation['generation'] += 1
            self._save_evaluation(evaluation)
            if evaluation['fault_stopped']:
                self._invalidate_proactive('evaluation_fault_stopped')
            return {'outcome':outcome, **evaluation}

    def resume_evaluation(self, now, delay, owner, version, force=False):
        with self.transaction():
            evaluation = self.evaluation_state()
            if not self.owns(owner,now):
                raise ValueError('当前QQBot实例尚未取得运行权，请稍后重试。')
            if evaluation['generation'] != version:
                raise ValueError('主动评估状态已变化，请刷新后重试。')
            if not evaluation['fault_stopped'] and not force:
                return evaluation
            if evaluation['in_flight'] and not force:
                raise ValueError('主动评估仍在结束，请稍后重试。')
            self._invalidate_proactive('evaluation_manually_resumed')
            evaluation.update(fault_stopped=False,failure_streak=0,silent_streak=0,
                next_check_at=now+delay,generation=evaluation['generation']+1,in_flight=False,attempt_id='')
            self._save_evaluation(evaluation)
            return evaluation

    def accept(self, message_id, text, now, *, action=None, silence_until=0, reply=None):
        if action is not None and (action not in {'pause','resume','today','status'} or not reply):
            raise ValueError('非法主人控制指令')
        key = "qq-in:" + str(message_id)
        with self.transaction():
            if self.conn.execute("SELECT 1 FROM inbox WHERE message_key=?", (key,)).fetchone():
                return False
            hid = self.conn.execute("INSERT INTO history(message_key,role,content,created) VALUES(?,'user',?,?)",
                                    (key,text,now)).lastrowid
            self.conn.execute("INSERT INTO inbox(message_key,history_id,created) VALUES(?,?,?)", (key,hid,now))
            state = self.state()
            state.update(last_incoming=now,unanswered=0,revision=state["revision"]+1)
            self._save_state(state)
            try:
                evaluation = self.evaluation_state()
            except (ValueError,TypeError,OverflowError) as exc:
                # A damaged proactive record must not reject ordinary incoming chat.
                log.error('QQ主动评估状态损坏，保留原记录并继续私聊',exc_info=exc)
            else:
                evaluation['silent_streak'] = 0
                if not evaluation['fault_stopped'] and not evaluation['failure_streak']:
                    evaluation['next_check_at'] = 0
                self._save_evaluation(evaluation)
            self.conn.execute("UPDATE outbox SET status='failed',error='new_owner_message',payload='[]' WHERE kind='proactive' AND status='queued'")
            if action is not None:
                self._control(action,now,silence_until)
                self.conn.execute("INSERT INTO outbox(task_key,kind,payload,summary,created,expires) VALUES(?,'reply',?,?,?,?)",
                    ('control:'+str(message_id),json.dumps([{'type':'text','data':{'text':reply}}],ensure_ascii=False),reply,now,now+86400))
                self.conn.execute('INSERT INTO reply_inputs VALUES(?,?)',('control:'+str(message_id),hid))
                self.conn.execute("UPDATE inbox SET status='done' WHERE message_key=?",(key,))
            return True

    def pending(self):
        with self.lock:
            return bool(self.conn.execute("SELECT 1 FROM inbox WHERE status IN ('pending','working') LIMIT 1").fetchone())

    def open_proactive(self):
        with self.lock:
            return bool(self.conn.execute("SELECT 1 FROM outbox WHERE kind='proactive' AND status IN ('queued','in_flight') LIMIT 1").fetchone())

    def _release_working(self, owner, batch=None):
        where, params = 'status=\'working\' AND claim_owner IS ?', [owner]
        if batch is not None:
            where += ' AND id IN ('+','.join('?' for _ in batch)+')'
            params.extend(r['id'] for r in batch)
        self.conn.execute("UPDATE inbox SET status=CASE WHEN EXISTS(SELECT 1 FROM reply_inputs r JOIN outbox o ON o.task_key=r.task_key WHERE r.history_id=inbox.history_id) THEN 'done' ELSE 'pending' END,claim_owner=NULL WHERE "+where,params)

    def release_batch(self, batch, owner, now):
        with self.transaction():
            if self.owns(owner,now):
                self._release_working(owner,batch)

    def batch(self, now, debounce, max_wait, owner=None):
        with self.transaction():
            if owner is not None and not self.owns(owner,now):
                return []
            # Called only under the runtime's generation lock. Reclaim a previous
            # attempt even if the database was unavailable during its cleanup.
            self._release_working(owner)
            rows = self.conn.execute("SELECT * FROM inbox WHERE status='pending' ORDER BY id LIMIT 20").fetchall()
            if not rows or (now-rows[-1]["created"] < debounce and now-rows[0]["created"] < max_wait):
                return []
            self.conn.executemany("UPDATE inbox SET status='working',claim_owner=? WHERE id=?", [(owner,r["id"]) for r in rows])
            return [dict(r) for r in rows]

    def history(self, limit=30, before_id=None):
        with self.lock:
            rows = self.conn.execute("SELECT * FROM history WHERE (? IS NULL OR id<=?) ORDER BY id DESC LIMIT ?",
                                     (before_id,before_id,limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def batch_history(self, batch, limit):
        with self.lock:
            rows = self._conversation_rows(limit,batch[0]['history_id'])
            current = self.conn.execute("SELECT * FROM history WHERE id IN ("+",".join("?" for _ in batch)+")",
                                        [r["history_id"] for r in batch]).fetchall()
            return [*rows,*(dict(r) for r in sorted(current,key=lambda r:r['id']))]

    def _conversation_rows(self, limit, before=None):
        rows = self.conn.execute("""WITH turns AS (
            SELECT h.*,COALESCE((SELECT MAX(r.history_id) FROM reply_inputs r
                WHERE h.role='assistant' AND r.task_key=substr(h.message_key,8)),h.id) AS turn_id FROM history h)
            SELECT * FROM turns WHERE (? IS NULL OR turn_id<?)
            ORDER BY turn_id DESC,CASE WHEN role='assistant' THEN 1 ELSE 0 END DESC,id DESC LIMIT ?""",
            (before,before,limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def conversation_history(self, limit):
        with self.lock:
            return self._conversation_rows(limit)

    def queue(self, key, kind, payload, summary, now, expires, batch=(), revision=None, owner=None):
        with self.transaction():
            if owner is not None and not self.owns(owner,now):
                return False
            if kind == "proactive":
                if self._send_error(dict(kind=kind,expires=expires,eligibility_revision=revision),now):
                    return False
            self.conn.execute("INSERT OR IGNORE INTO outbox(task_key,kind,payload,summary,created,expires,eligibility_revision) VALUES(?,?,?,?,?,?,?)",
                              (key,kind,json.dumps(payload,ensure_ascii=False),summary,now,expires,revision if kind=='proactive' else None))
            self.conn.executemany('INSERT OR IGNORE INTO reply_inputs VALUES(?,?)',[(key,r['history_id']) for r in batch])
            self.conn.executemany("UPDATE inbox SET status='done',claim_owner=NULL WHERE id=?", [(r["id"],) for r in batch])
            return True

    def control(self, action, now, silence_until=0):
        with self.transaction():
            self._control(action,now,silence_until)

    def _control(self, action, now, silence_until):
        state = self.state()
        state["revision"] += 1
        if action == "pause":
            state["paused"] = True
        elif action == "resume":
            state.update(paused=False,silence_until=0,next_check=now)
        elif action == "today":
            state["silence_until"] = silence_until
        self._save_state(state)
        if action in {"pause","today"}:
            self.conn.execute("UPDATE outbox SET status='failed',error='owner_paused',payload='[]' WHERE kind='proactive' AND status='queued'")

    def reserve_check(self, now, delay):
        with self.transaction():
            state = self.state()
            state["next_check"] = now+delay
            self._save_state(state)
            return state

    def invalidate_proactive(self, reason):
        with self.transaction():
            self._invalidate_proactive(reason)

    def _invalidate_proactive(self, reason):
        state = self.state()
        state['revision'] += 1
        self._save_state(state)
        self.conn.execute("UPDATE outbox SET status='failed',error=?,payload='[]' WHERE kind='proactive' AND status='queued'",(reason,))

    def claim_send(self, now, owner=None):
        with self.transaction():
            if owner is not None and not self.owns(owner,now):
                return None
            self.conn.execute("UPDATE outbox SET status='failed',error='expired',payload='[]' WHERE status='queued' AND expires<=?", (now,))
            while True:
                row = self.conn.execute("SELECT * FROM outbox WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if not row:
                    return None
                reason = self._send_error(row,now)
                if reason:
                    self.conn.execute("UPDATE outbox SET status='failed',error=?,payload='[]' WHERE id=?",(reason,row['id']))
                    continue
                self.conn.execute("UPDATE outbox SET status='in_flight' WHERE id=?", (row["id"],))
                return dict(row)

    def _send_error(self, row, now):
        if row['expires']<=now:
            return 'expired'
        if row['kind']=='proactive':
            try:
                evaluation = self.evaluation_state()
            except (ValueError,TypeError,OverflowError) as exc:
                # A malformed proactive record cancels this unsent task. It
                # must not poison receipt settlement or unrelated private chat.
                log.error('QQ主动任务因评估状态损坏已取消',exc_info=exc,extra={'evt':{
                    'module':'qqbot','operation':'proactive_submission',
                    'error_code':'proactive_state_unavailable',
                    'message_zh':'主动评估状态损坏，已取消未提交问候，私聊投递继续',
                    'outcome':'failed'}})
                return 'evaluation_state_unavailable'
            if evaluation['fault_stopped']:
                return 'evaluation_fault_stopped'
            state = self.state()
            if row['eligibility_revision'] is None or row['eligibility_revision']!=state['revision']:
                return 'stale_proactive_revision'
            if state['paused']:
                return 'owner_paused'
            if state['silence_until']>now:
                return 'owner_silenced'
            if self.pending():
                return 'owner_input_pending'
        return None

    def submission_error(self, task_id, now):
        with self.lock:
            row = self.conn.execute("SELECT * FROM outbox WHERE id=? AND status='in_flight'",(task_id,)).fetchone()
            return self._send_error(row,now) if row else 'task_not_in_flight'

    def finish_send(self, task_id, status, now, provider_id=None, error=None, day="", cooldown=3600):
        if status not in {"confirmed","failed","unknown","queued"}:
            raise ValueError("非法发送状态")
        with self.lock:
            # Keep the first observed outcome before any local I/O. A database
            # outage cannot turn this into another network send attempt.
            self._pending_receipts.setdefault(task_id,dict(task_id=task_id,status=status,now=now,
                provider_id=provider_id,error=error,day=day,cooldown=cooldown))
            self.retry_receipts(now)

    def _journal_receipts(self):
        for task_id,receipt in list(self._pending_receipts.items()):
            with self.transaction():
                row = self.conn.execute("SELECT 1 FROM outbox WHERE id=? AND status='in_flight'",(task_id,)).fetchone()
                if row:
                    self.conn.execute('INSERT OR IGNORE INTO send_receipts VALUES(?,?)',
                                      (task_id,json.dumps(receipt,ensure_ascii=False)))
            if not row:
                self._pending_receipts.pop(task_id,None)

    def retry_receipts(self, current_time=None):
        with self.lock:
            self._journal_receipts()
            rows = self.conn.execute('SELECT receipt FROM send_receipts ORDER BY task_id').fetchall()
            for row in rows:
                receipt = json.loads(row['receipt'])
                self._apply_receipt(**receipt,current_time=current_time)
                self._pending_receipts.pop(receipt['task_id'],None)

    def _apply_receipt(self, task_id, status, now, provider_id=None, error=None, day="", cooldown=3600,current_time=None):
        with self.transaction():
            row = self.conn.execute("SELECT * FROM outbox WHERE id=? AND status='in_flight'", (task_id,)).fetchone()
            if not row:
                self.conn.execute('DELETE FROM send_receipts WHERE task_id=?',(task_id,))
                return
            if status=='queued':
                reason = self._send_error(row,now if current_time is None else current_time)
                if reason:
                    status,error = 'failed',reason
            self.conn.execute("UPDATE outbox SET status=?,provider_id=?,error=?,payload=CASE WHEN ?='queued' THEN payload ELSE '[]' END WHERE id=?", (status,provider_id,error,status,task_id))
            if status == "confirmed":
                self.conn.execute("INSERT OR IGNORE INTO history(message_key,role,content,created) VALUES(?,'assistant',?,?)",
                                  ("qq-out:"+row["task_key"],row["summary"],now))
            if row["kind"] == "proactive" and status in {"confirmed","unknown"}:
                state = self.state()
                state.update(last_sent=max(now,state['last_sent']),
                             unanswered=0 if state['last_incoming']>now else min(10,state["unanswered"]+1),
                             day=day,count=(state["count"] if state["day"]==day else 0)+1)
                state["next_check"] = now+min(604800,cooldown*2**max(0,state["unanswered"]-1))
                self._save_state(state)
            self.conn.execute('DELETE FROM send_receipts WHERE task_id=?',(task_id,))

    def exports(self, now, limit=20):
        with self.lock:
            rows = self.conn.execute('SELECT * FROM history WHERE exported=0 ORDER BY id LIMIT ?',(limit,))
            ready = []
            for row in rows:
                if row['export_after']>now:
                    break
                ready.append(dict(row))
            return ready

    def export_result(self, row_id, success, now):
        with self.transaction():
            self.conn.execute("UPDATE history SET exported=?,export_after=? WHERE id=?", (int(success),now+60,row_id))

    def counts(self):
        with self.lock:
            return dict(self.conn.execute("SELECT status,count(*) FROM outbox GROUP BY status").fetchall())

    def export_key(self, message_key):
        with self.lock:
            scope = self.conn.execute("SELECT value FROM meta WHERE key='scope'").fetchone()[0]
        if self.export_namespace:
            scope += ':' + self.export_namespace
        return "qqbot:"+hashlib.sha256((scope+":"+message_key).encode()).hexdigest()
