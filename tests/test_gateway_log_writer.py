"""日志后台 writer 测试（§6 隔离要求）。

覆盖：批量写入成功、失败退避重试复用同一 event_id、内存重试上限与丢弃
计数、spool 落盘 / 补写 / 损坏隔离 / 容量上限、关闭 flush 有界、状态快照。
writer 的内部方法被直接驱动（不依赖线程时序）；线程启停路径单独验证。
"""

import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gateway import logging_events
from gateway.config import cfg


class FakeStore:
    def __init__(self, failures=0):
        self.failures_remaining = failures
        self.inserted: list[dict] = []
        self.insert_calls = 0
        self.cleanup_calls = 0
        self.cleanup_result = {"status": "ok", "deleted": 3}
        self.insert_lock = threading.Lock()

    def insert_events(self, events):
        with self.insert_lock:
            self.insert_calls += 1
            if self.failures_remaining > 0:
                self.failures_remaining -= 1
                raise RuntimeError("db down")
            self.inserted.extend(events)
            return len(events)

    def cleanup(self, retention_days):
        self.cleanup_calls += 1
        return self.cleanup_result


def _event(event_id="e1", level="ERROR", occurred=None):
    from datetime import datetime, timezone

    # occurred 默认取当前时刻：spool 保留期过滤（BUG-24）按真实时间判定，
    # 固定历史日期会在 30 天后让全部回放用例过期。
    if occurred is None:
        occurred = datetime.now(timezone.utc).isoformat()
    return {
        "event_id": event_id, "occurred_at": occurred, "level": level,
        "module": "planning", "operation": "create", "error_code": "internal_error",
        "message_zh": "规划接口发生内部错误", "exception_type": "APIError",
        "message_raw": "raw", "stack_trace": "trace", "logger_name": "gateway.planning",
        "source": "a.py:fn:1", "request_id": "r1", "operation_id": "", "route": "",
        "method": "", "http_status": None, "upstream_status": None,
        "outcome": "failed", "instance_id": "inst", "release_id": "",
        "safe_context": {}, "fingerprint": "f" * 16, "truncated_fields": [],
    }


class WriterBatchTests(unittest.TestCase):
    def setUp(self):
        self.queue = logging_events._EventQueue(100)

    def _writer(self, store, spool_dir=""):
        with mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", spool_dir), \
                mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 500), \
                mock.patch.object(cfg, "GATEWAY_LOG_BATCH_MAX", 50), \
                mock.patch.object(cfg, "GATEWAY_LOG_FLUSH_TIMEOUT", 1.0):
            writer = logging_events._LogWriter(store, self.queue)
            writer._cleanup_interval = 1e9  # 测试不触发清理
            return writer

    def test_successful_write_updates_stats(self):
        store = FakeStore()
        writer = self._writer(store)
        writer._write_batch([_event("a"), _event("b")])
        self.assertEqual(store.insert_calls, 1)
        self.assertEqual(writer.written_total, 2)
        self.assertTrue(writer.last_write_at)  # 成功路径填充时间
        self.assertEqual(writer._consecutive_failures, 0)

    def test_failure_retries_same_event_ids(self):
        store = FakeStore(failures=2)
        writer = self._writer(store)
        batch = [_event("same-id")]
        writer._write_batch(batch)          # 第 1 次失败 → 进重试队列
        self.assertEqual(writer._retry_pending, [batch])
        writer._retry_not_before = 0        # 跳过退避等待
        writer._flush_retry_pending()       # 第 2 次失败 → 仍在队列
        self.assertEqual(writer._retry_pending, [batch])
        writer._retry_not_before = 0
        writer._flush_retry_pending()       # 第 3 次成功
        self.assertEqual(writer._retry_pending, [])
        self.assertEqual(store.inserted[0]["event_id"], "same-id")
        self.assertEqual(writer.written_total, 1)

    def test_retry_pending_overflow_drops_oldest(self):
        store = FakeStore(failures=100)
        writer = self._writer(store)
        with mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 2):
            writer._write_batch([_event("a")])
            writer._write_batch([_event("b")])
            writer._write_batch([_event("c")])  # 超限：最旧的 a 被丢弃
        self.assertEqual(writer._retry_pending_events, 2)
        ids = [e["event_id"] for batch in writer._retry_pending for e in batch]
        self.assertEqual(ids, ["b", "c"])
        self.assertEqual(self.queue.dropped_total, 1)
        self.assertEqual(self.queue.last_drop_reason, "retry_pending_overflow")

    def test_retry_pending_zero_limit_drops_all(self):
        # BUG-23：上限 0 严格执行——失败批次全部丢弃，不再保留「至少一批」。
        store = FakeStore(failures=100)
        writer = self._writer(store)
        with mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 0):
            writer._write_batch([_event(f"e{i}") for i in range(50)])
        self.assertEqual(writer._retry_pending_events, 0)
        self.assertEqual(writer._retry_pending, [])
        self.assertEqual(self.queue.dropped_total, 50)
        self.assertEqual(self.queue.last_drop_reason, "retry_pending_overflow")

    def test_retry_pending_single_batch_trimmed_to_limit(self):
        # BUG-23：单批超过上限时按事件数裁剪（级别优先、同级取新），
        # 保留事件沿用原 event_id。
        store = FakeStore(failures=100)
        writer = self._writer(store)
        batch = [_event("old-warn", level="WARNING")]
        batch += [_event(f"err-{i}", level="ERROR") for i in range(5)]
        with mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 2):
            writer._write_batch(batch)
        self.assertEqual(writer._retry_pending_events, 2)
        kept = [e["event_id"] for item in writer._retry_pending for e in item]
        # 级别优先：两条 ERROR（较新）保留，WARNING 被裁
        self.assertEqual(kept, ["err-3", "err-4"])
        self.assertEqual(self.queue.dropped_total, 4)

    def test_final_flush_writes_remaining(self):
        store = FakeStore()
        writer = self._writer(store)
        self.queue.put(_event("x1"))
        self.queue.put(_event("x2"))
        writer._final_flush()
        self.assertEqual(
            sorted(e["event_id"] for e in store.inserted), ["x1", "x2"])
        self.assertEqual(self.queue.dropped_total, 0)

    def test_final_flush_timeout_counts_drops_without_spool(self):
        store = FakeStore(failures=1000)
        writer = self._writer(store)
        self.queue.put(_event("x1"))
        writer._final_flush()
        self.assertEqual(store.inserted, [])
        self.assertEqual(self.queue.dropped_total, 1)
        self.assertEqual(self.queue.last_drop_reason, "shutdown_flush_timeout")

    def test_maybe_cleanup_records_result(self):
        store = FakeStore()
        writer = self._writer(store)
        writer._next_cleanup_at = 0
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            writer._maybe_cleanup()
        self.assertEqual(store.cleanup_calls, 1)
        self.assertEqual(writer.last_cleanup_status, "ok")
        self.assertEqual(writer.last_cleanup_deleted, 3)

    def test_maybe_cleanup_failure_is_isolated(self):
        store = FakeStore()
        store.cleanup_result = None
        store.cleanup = lambda days: (_ for _ in ()).throw(RuntimeError("cleanup boom"))
        writer = self._writer(store)
        writer._next_cleanup_at = 0
        writer._maybe_cleanup()  # 不得抛出
        self.assertIn("failed", writer.last_cleanup_status)

    def test_snapshot_shape(self):
        writer = self._writer(FakeStore())
        snap = writer.snapshot()
        for key in (
            "written_total", "last_write_at", "last_write_error",
            "consecutive_failures", "spool", "retention_days",
            "last_cleanup_at", "last_cleanup_status", "last_cleanup_deleted",
            "instance_id", "release_id",
        ):
            self.assertIn(key, snap)


class WriterThreadTests(unittest.TestCase):
    def test_start_stop_is_bounded_and_idempotent(self):
        store = FakeStore()
        queue = logging_events._EventQueue(10)
        with mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", ""), \
                mock.patch.object(cfg, "GATEWAY_LOG_FLUSH_TIMEOUT", 1.0):
            writer = logging_events._LogWriter(store, queue)
            writer._cleanup_interval = 1e9
            writer.start()
            writer.start()  # 幂等：不重复起线程
            self.assertTrue(writer._thread.is_alive())
            queue.put(_event("t1"))
            deadline = time.monotonic() + 5
            while not store.inserted and time.monotonic() < deadline:
                time.sleep(0.02)
            writer.stop(1.0)
            self.assertFalse(writer._thread.is_alive())
        self.assertEqual([e["event_id"] for e in store.inserted], ["t1"])


class SpoolStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qigate-log-spool-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.spool = logging_events._SpoolStore(
            str(self.dir), "inst-1", max_files=5, max_bytes=1024 * 1024
        )
        self.inserted: list[dict] = []

    def _insert_ok(self, events):
        self.inserted.extend(events)

    def _insert_fail(self, events):
        raise RuntimeError("db down")

    def test_write_and_replay(self):
        self.assertTrue(self.spool.write([_event("s1"), _event("s2")]))
        self.assertEqual(len(self.spool._files()), 1)
        written = self.spool.replay(self._insert_ok)
        self.assertEqual(written, 2)
        self.assertEqual(self.spool.replayed_total, 2)
        self.assertEqual(self.spool._files(), [])  # 成功后删除
        self.assertEqual(
            sorted(e["event_id"] for e in self.inserted), ["s1", "s2"])

    def test_replay_keeps_file_when_db_still_down(self):
        self.spool.write([_event("s1")])
        # BUG-13：回放失败必须向上传播（writer 记录失败状态并退避），
        # 文件保留等下轮。
        with self.assertRaises(RuntimeError):
            self.spool.replay(self._insert_fail)
        self.assertEqual(len(self.spool._files()), 1)
        self.assertEqual(self.spool.replayed_total, 0)

    def test_corrupt_file_is_quarantined_and_counted(self):
        (self.dir / "inst-1-000000000001.jsonl").write_text(
            "not json\n", encoding="utf-8")
        self.spool.replay(self._insert_ok)
        self.assertEqual(self.spool.corrupt_total, 1)
        self.assertEqual(self.spool._files(), [])
        self.assertTrue(
            (self.dir / "inst-1-000000000001.jsonl.corrupt").exists())

    def test_other_instance_files_are_not_read(self):
        (self.dir / "other-inst-000000000001.jsonl").write_text(
            '{"event_id": "z"}\n', encoding="utf-8")
        self.assertEqual(self.spool._files(), [])
        self.spool.replay(self._insert_ok)
        self.assertEqual(self.inserted, [])

    def test_max_files_bound_drops_oldest(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-1", max_files=2, max_bytes=1024 * 1024)
        for index in range(5):
            spool.write([_event(f"e{index}")])
        files = spool._files()
        self.assertEqual(len(files), 2)
        self.assertGreaterEqual(spool.dropped_total, 3)

    def test_disabled_when_dir_empty(self):
        spool = logging_events._SpoolStore("", "inst-1", 5, 1024)
        self.assertFalse(spool.enabled)
        self.assertFalse(spool.write([_event()]))
        self.assertEqual(spool.replay(self._insert_ok), 0)

    def test_snapshot_reports_non_sensitive_state(self):
        self.spool.write([_event("s1")])
        snap = self.spool.snapshot()
        self.assertTrue(snap["enabled"])
        self.assertEqual(snap["files"], 1)
        self.assertGreater(snap["bytes"], 0)
        # 不暴露磁盘绝对路径
        self.assertNotIn(str(self.dir), str(snap))


class StartupShutdownTests(unittest.TestCase):
    def test_startup_disabled_is_noop(self):
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", False):
            logging_events.startup()
        self.assertIsNone(logging_events.get_writer())
        self.assertFalse(logging_events.handler_installed())

    def test_shutdown_without_startup_is_noop(self):
        logging_events.shutdown()  # 不得抛出
        self.assertIsNone(logging_events.get_writer())


class LoggingStore:
    """写入链会经公共 db 初始化打日志的假存储（BUG-11 复现形态）。"""

    def __init__(self, failures=0):
        self.failures_remaining = failures
        self.insert_calls = 0

    def insert_events(self, events):
        self.insert_calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            # 模拟 log_store → db.get_client 的公共依赖日志
            logging.getLogger("gateway.db").warning(
                "Supabase key 探测失败（key_mode=secret, error=APIError）")
            raise RuntimeError("db down")
        return len(events)

    def cleanup(self, retention_days):
        return {"status": "ok", "deleted": 0}


class WriterSelfCollectionTests(unittest.TestCase):
    """BUG-11：writer 线程内调用公共 db 的日志不被重新采集。"""

    def setUp(self):
        self.queue = logging_events._EventQueue(100)

    def _writer(self, store, spool_dir=""):
        with mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", spool_dir), \
                mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 500), \
                mock.patch.object(cfg, "GATEWAY_LOG_BATCH_MAX", 50):
            writer = logging_events._LogWriter(store, self.queue)
            writer._cleanup_interval = 1e9
            return writer

    def test_writer_cycle_suppresses_self_collection(self):
        store = LoggingStore(failures=4)
        writer = self._writer(store)
        for _ in range(4):
            writer._process_once()
            writer._retry_not_before = 0  # 跳过退避等待，驱动下一轮
        # 4 次失败重试：除原业务事件外，队列里不得出现 gateway.db 自生事件
        events = self.queue.drain(10000)
        self.assertEqual(len(events), 0)

    def test_business_thread_db_logs_are_still_captured(self):
        # 抑制是线程本地的：业务线程的 gateway.db 日志仍正常采集
        queue = logging_events._queue
        queue.drain(10000)
        with mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True), \
                mock.patch.object(cfg, "GATEWAY_LOG_MIN_LEVEL", "WARNING"):
            logging_events.install_handler()
            self.addCleanup(logging_events.uninstall_handler)
            self.addCleanup(queue.drain, 10000)
            store = LoggingStore()
            writer = self._writer(store)
            writer._process_once()  # writer 线程外直接驱动也处于抑制范围
            logging.getLogger("gateway.db").error("业务线程数据库错误")
            events = queue.drain(10000)
        self.assertEqual(
            [event["error_code"] for event in events], ["unlogged_error"])


class WriterBackoffTests(unittest.TestCase):
    """BUG-13：回放失败上报状态；转存后新批次不绕过退避。"""

    def setUp(self):
        self.queue = logging_events._EventQueue(100)

    def _writer(self, store, spool_dir=""):
        with mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", spool_dir), \
                mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 500), \
                mock.patch.object(cfg, "GATEWAY_LOG_BATCH_MAX", 50):
            writer = logging_events._LogWriter(store, self.queue)
            writer._cleanup_interval = 1e9
            return writer

    def test_replay_failure_reports_state_and_backoff(self):
        store = FakeStore(failures=1000)
        with tempfile.TemporaryDirectory(prefix="qigate-backoff-") as tmp_name:
            writer = self._writer(store, spool_dir=tmp_name)
            writer._spool.write([_event("s1")])
            writer._process_once()
            self.assertEqual(store.insert_calls, 1)
            self.assertEqual(writer._consecutive_failures, 1)
            self.assertEqual(writer._last_write_error, "RuntimeError")
            self.assertGreater(writer._retry_not_before, time.monotonic())
            # 退避期内：新周期不再碰数据库
            writer._process_once()
            self.assertEqual(store.insert_calls, 1)
            # 退避到期后重试（失败继续进入退避，不无限循环每 2 秒打库）
            writer._retry_not_before = 0
            writer._process_once()
            self.assertEqual(store.insert_calls, 2)

    def test_spooled_batch_new_events_respect_backoff(self):
        # 重试批次转存 spool 后，新批次在退避期内不得绕过退避
        store = FakeStore(failures=1000)
        with tempfile.TemporaryDirectory(prefix="qigate-backoff2-") as tmp_name:
            writer = self._writer(store, spool_dir=tmp_name)
            # 连续 3 次失败 → 批次转存 spool，retry_pending 清空
            for index in range(3):
                writer._write_batch([_event(f"e{index}")])
                writer._retry_not_before = 0
                writer._process_once()
            self.assertEqual(writer._retry_pending, [])
            self.assertGreaterEqual(len(writer._spool._files()), 1)
            # 新事件入队；退避期内 _process_once 不得再调存储
            self.queue.put(_event("new-1"))
            calls_before = store.insert_calls
            writer._process_once()
            self.assertEqual(store.insert_calls, calls_before)
            # 退避到期后新批次才尝试写入
            writer._retry_not_before = 0
            writer._process_once()
            self.assertEqual(store.insert_calls, calls_before + 1)

    def test_replay_success_updates_written_stats(self):
        store = FakeStore()
        with tempfile.TemporaryDirectory(prefix="qigate-replay-ok-") as tmp_name:
            writer = self._writer(store, spool_dir=tmp_name)
            writer._spool.write([_event("s1"), _event("s2")])
            writer._process_once()
            self.assertEqual(writer.written_total, 2)
            self.assertTrue(writer.last_write_at)
            self.assertEqual(writer._consecutive_failures, 0)
            self.assertEqual(writer._spool._files(), [])


class CorruptSpoolBoundsTests(unittest.TestCase):
    """BUG-14：损坏隔离文件计入容量与状态。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qigate-corrupt-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_corrupt_files_count_toward_capacity(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-c", max_files=1, max_bytes=1024)
        spool._seq = 100
        spool.write([_event("e1")])  # 101
        (self.dir / "inst-c-000000000102.jsonl.corrupt").write_text(
            "x" * 100, encoding="utf-8")
        (self.dir / "inst-c-000000000103.jsonl.corrupt").write_text(
            "x" * 100, encoding="utf-8")
        spool._enforce_bounds()
        # max_files=1 覆盖 .jsonl 与 .corrupt：只保留最新一个文件
        all_files = spool._all_files()
        self.assertEqual(all_files, ["inst-c-000000000103.jsonl.corrupt"])
        snap = spool.snapshot()
        self.assertEqual(snap["files"], 0)
        self.assertEqual(snap["corrupt_files"], 1)
        actual_bytes = sum(
            os.path.getsize(self.dir / name) for name in all_files)
        self.assertEqual(snap["bytes"], actual_bytes)

    def test_corrupt_purge_counted_separately_from_event_drops(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-d", max_files=2, max_bytes=1024 * 1024)
        spool._seq = 100
        # 损坏文件（序号更早，容量轮转时最先被删）：回放时被隔离为 .corrupt
        (self.dir / "inst-d-000000000099.jsonl").write_text(
            "not json\n", encoding="utf-8")
        spool.replay(lambda events: None)
        self.assertEqual(spool.corrupt_total, 1)
        spool.write([_event("e1")])  # 101
        spool.write([_event("e2")])  # 102 → 超过 max_files=2 → 删最旧（.corrupt）
        snap = spool.snapshot()
        # 损坏文件删除按文件计数，不冒充事件丢弃数（BUG-15）
        self.assertEqual(snap["corrupt_files"], 0)
        self.assertEqual(spool.corrupt_purged_total, 1)
        self.assertEqual(snap["files"], 2)
        self.assertEqual(snap["dropped_total"], 0)  # 有效事件未被丢弃
        self.assertNotIn(str(self.dir), str(snap))

    def test_quarantine_failure_keeps_file_bounded(self):
        # 隔离失败（改名失败）：文件保留在 .jsonl 集合内，容量仍受约束
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-e", max_files=5, max_bytes=1024 * 1024)
        corrupt = self.dir / "inst-e-000000000001.jsonl"
        corrupt.write_text("not json\n", encoding="utf-8")
        with mock.patch("os.replace", side_effect=OSError("locked")):
            spool.replay(lambda events: None)
        # 改名失败：corrupt_total 不虚增，文件仍在容量核算范围
        self.assertEqual(spool.corrupt_total, 0)
        self.assertEqual(len(spool._all_files()), 1)


class SpoolDeleteFailureTests(unittest.TestCase):
    """BUG-22：删除失败不虚增丢弃、不虚放额度；无法腾出空间则拒绝写入。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qigate-del-fail-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.queue = logging_events._EventQueue(100)

    def test_failed_delete_does_not_count_drops_or_free_quota(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-f", max_files=1, max_bytes=1024 * 1024)
        spool._seq = 100
        self.assertTrue(spool.write([_event("old")]))  # 101
        with mock.patch.object(logging_events.os, "remove",
                                side_effect=PermissionError("synthetic locked")):
            # BUG-22 新契约：额度无法腾出时**拒绝新批次**（写入前预留，
            # 不创建超额文件）；旧文件保留可回放，spool 自身不虚报丢弃，
            # 新批次由调用方按 spool_write_failed 准确计损。
            first = spool.write([_event("new-1")])
            second = spool.write([_event("new-2")])
        snap = spool.snapshot()
        self.assertFalse(first)
        self.assertFalse(second)
        # 旧文件仍在磁盘、事件可回放：不得虚报丢弃（BUG-22 根因）
        self.assertEqual(snap["dropped_total"], 0)
        self.assertEqual(snap["corrupt_purged_total"], 0)
        # 磁盘实物有界：只有旧文件，没有新增任何超额文件
        self.assertEqual(snap["files"], 1)
        self.assertEqual(
            spool._files(), ["inst-f-000000000101.jsonl"])

    def test_all_deletes_failing_keeps_capacity_bounded_over_many_rounds(self):
        # BUG-22 验收：所有删除持续失败 + 连续多批写入，实际文件数不增长
        # 超限（写入前预留额度，绝不先创建再依赖事后删除）。
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-h", max_files=1, max_bytes=1024 * 1024)
        spool._seq = 100
        self.assertTrue(spool.write([_event("old")]))  # 101
        rejected = 0
        with mock.patch.object(logging_events.os, "remove",
                                side_effect=PermissionError("synthetic locked")):
            for index in range(25):
                if not spool.write([_event(f"e{index}")]):
                    rejected += 1
        snap = spool.snapshot()
        self.assertEqual(rejected, 25, "全部新批次被明确拒绝")
        self.assertEqual(snap["files"], 1, "实际文件数保持 1（max_files=1）")
        self.assertEqual(snap["dropped_total"], 0, "spool 不虚报；调用方计损")
        # 恢复删除能力后：旧文件仍可幂等回放
        replayed = spool.replay(lambda events: len(events))
        self.assertEqual(replayed, 1)

    def test_write_rejected_when_quota_freed_but_new_file_removable(self):
        # 旧文件删除失败、新文件可删除：写入明确失败并准确计损。
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-g", max_files=1, max_bytes=1024 * 1024)
        spool._seq = 100
        self.assertTrue(spool.write([_event("old")]))  # 101
        locked = self.dir / "inst-g-000000000101.jsonl"
        real_remove = os.remove

        def selective_remove(path):
            if str(path) == str(locked):
                raise PermissionError("synthetic locked")
            real_remove(path)

        with mock.patch.object(logging_events.os, "remove", side_effect=selective_remove):
            ok = spool.write([_event("new-1")])
        self.assertFalse(ok, "无法腾出额度时写入明确失败（BUG-22）")
        # 新文件从未创建，磁盘回到旧文件单份；批次由调用方计损
        self.assertEqual(spool._files(), ["inst-g-000000000101.jsonl"])
        self.assertEqual(spool.snapshot()["dropped_total"], 0)

    def test_oversized_single_batch_is_rejected_without_disk_write(self):
        # BUG-22：单批超过字节上限直接拒绝，不落盘。
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-i", max_files=10, max_bytes=64)
        ok = spool.write([_event("x" * 200)])
        self.assertFalse(ok)
        self.assertEqual(spool.snapshot()["files"], 0)


class SpoolRetentionTests(unittest.TestCase):
    """BUG-24：spool 按真实 occurred_at 执行保留期，不依赖数据库成功。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="qigate-retention-")
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.inserted: list[dict] = []

    def _old_event(self, days: int):
        from datetime import datetime, timedelta, timezone

        occurred = datetime.now(timezone.utc) - timedelta(days=days)
        return _event(f"old-{days}", occurred=occurred.isoformat())

    def _insert_ok(self, events):
        self.inserted.extend(events)

    def test_replay_filters_expired_events(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-r", max_files=10, max_bytes=1024 * 1024)
        spool.write([self._old_event(40), _event("fresh")])
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            written = spool.replay(self._insert_ok)
        self.assertEqual(written, 1)  # 过期事件不回放
        self.assertEqual(self.inserted[0]["event_id"], "fresh")
        snap = spool.snapshot()
        self.assertEqual(snap["expired_total"], 1)
        self.assertEqual(snap["files"], 0)  # 文件移除

    def test_enforce_retention_rewrites_and_counts(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-s", max_files=10, max_bytes=1024 * 1024)
        spool.write([self._old_event(40), _event("fresh")])
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            spool._enforce_retention()
        snap = spool.snapshot()
        self.assertEqual(snap["expired_total"], 1)
        self.assertEqual(snap["files"], 1)  # 期内事件保留
        # 保留期内事件仍可正常回放
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            written = spool.replay(self._insert_ok)
        self.assertEqual(written, 1)
        self.assertEqual(self.inserted[0]["event_id"], "fresh")
        # 不重复计数
        self.assertEqual(spool.snapshot()["expired_total"], 1)

    def test_enforce_retention_runs_without_database(self):
        # 长故障场景：不依赖 insert 成功，本地到期即清理。
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-t", max_files=10, max_bytes=1024 * 1024)
        spool.write([self._old_event(40)])
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            spool._enforce_retention()
        self.assertEqual(spool.snapshot()["files"], 0)
        self.assertEqual(spool.snapshot()["expired_total"], 1)

    def test_corrupt_file_ttl_purges_after_retention(self):
        spool = logging_events._SpoolStore(
            str(self.dir), "inst-u", max_files=10, max_bytes=1024 * 1024)
        corrupt = self.dir / "inst-u-000000000001.jsonl.corrupt"
        corrupt.write_text("not json\n", encoding="utf-8")
        expired_at = time.time() - 40 * 86400
        os.utime(corrupt, (expired_at, expired_at))
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            spool._enforce_retention()
        self.assertFalse(corrupt.exists())
        self.assertEqual(spool.snapshot()["corrupt_purged_total"], 1)
        # 期内的损坏文件保留（等待容量轮转或后续 TTL）
        fresh = self.dir / "inst-u-000000000002.jsonl.corrupt"
        fresh.write_text("not json\n", encoding="utf-8")
        with mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            spool._enforce_retention()
        self.assertTrue(fresh.exists())

    def test_writer_cycle_schedules_spool_retention(self):
        # 退避期内（数据库不可用）保留期清理照常执行（BUG-24）。
        store = FakeStore(failures=1000)
        queue = logging_events._EventQueue(100)
        with tempfile.TemporaryDirectory(prefix="qigate-ret-w-") as tmp_name, \
                mock.patch.object(cfg, "GATEWAY_LOG_SPOOL_DIR", tmp_name), \
                mock.patch.object(cfg, "GATEWAY_LOG_RETRY_PENDING_MAX", 500), \
                mock.patch.object(cfg, "GATEWAY_LOG_BATCH_MAX", 50), \
                mock.patch.object(cfg, "GATEWAY_LOG_RETENTION_DAYS", 30):
            writer = logging_events._LogWriter(store, queue)
            writer._cleanup_interval = 1e9
            writer._spool.write([self._old_event(40)])
            writer._process_once()  # 首周期即执行保留期清理
            self.assertEqual(writer._spool.snapshot()["expired_total"], 1)
            self.assertEqual(writer._spool.snapshot()["files"], 0)


if __name__ == "__main__":
    unittest.main()
