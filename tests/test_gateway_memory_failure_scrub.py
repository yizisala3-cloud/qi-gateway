"""BUG-27：记忆总结 / 连续感 / 反刍失败记录不绕过日志清洗（L33）。

失败字段（error_message / model_output_excerpt）入库前清洗；读取投影
（_public_run）出口统一清洗——旧敏感样本展示层保护，不改写库内历史。
用模拟 client 记录 update / insert 载荷，不连接真实数据库。
"""

import unittest
from unittest import mock

from gateway import memory_extract, memory_rumination
from gateway.logging_events import scrub_error_text


class _CaptureClient:
    """记录 update / insert 载荷的模拟 Supabase 客户端。"""

    def __init__(self):
        self.updates = []
        self.inserts = []

    class _Chain:
        def __init__(self, outer, kind, payload):
            self._outer = outer
            self._kind = kind
            self._payload = payload

        def eq(self, *args, **kwargs):
            return self

        def limit(self, *args, **kwargs):
            return self

        def execute(self):
            if self._kind == "update":
                self._outer.updates.append(self._payload)
                return mock.Mock(data=[{**self._payload, "id": 1}])
            if self._kind == "insert":
                self._outer.inserts.append(self._payload)
                return mock.Mock(data=[{**self._payload, "id": 1}])
            # select：回放最近一次 update / insert 的行
            rows = [{**self._payload, "id": 1}] if self._payload else []
            return mock.Mock(data=rows)

    def table(self, name):
        self.last_table = name
        return self

    def update(self, payload):
        return self._Chain(self, "update", payload)

    def insert(self, payload):
        return self._Chain(self, "insert", payload)

    def select(self, *_args):
        return self._Chain(self, "select", self._last_payload())

    def _last_payload(self):
        if self.updates:
            return self.updates[-1]
        if self.inserts:
            return self.inserts[-1]
        return {}


SECRET = "sk-memory-failure-secret-1234567890"


class MemoryFailureRecordScrubTests(unittest.TestCase):
    def setUp(self):
        self.client = _CaptureClient()
        patches = [
            mock.patch.object(memory_extract, "get_client", lambda: self.client),
            mock.patch.object(memory_extract, "_analysis_configured", lambda: True),
            mock.patch.object(memory_extract, "resolve_assistant_id", lambda: "a1"),
            mock.patch.object(memory_extract, "_mark_stale_runs", lambda: None),
            mock.patch.object(memory_extract, "_claim_slot", lambda *a, **k: {
                "status": "claimed", "run_id": 1}),
            mock.patch.object(memory_extract, "_update_heartbeat", lambda *a: None),
            mock.patch.object(memory_extract, "_fetch_batch", lambda *a: (
                0, [{"id": 1, "role": "user", "content": "你好",
                     "created_at": "2026-10-11T10:00:00+00:00"}])),
            # 失败路径：提取请求直接抛出携带敏感文本的管线错误
            mock.patch.object(
                memory_extract, "_extract_memories",
                side_effect=memory_extract.DigestPipelineError(
                    "model_http_error",
                    f"HTTP 401 key {SECRET}",
                    f'{{"error": "Incorrect API key: {SECRET}"}}')),
            # 已配置密钥实际值参与掩码
            mock.patch.object(
                __import__("gateway.config", fromlist=["cfg"]).cfg,
                "UPSTREAM_API_KEY", SECRET),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_digest_failure_payload_is_scrubbed_before_persist(self):
        memory_extract.run_memory_digest("manual_execute", "execute")
        failed = [p for p in self.client.updates if p.get("status") == "failed"]
        self.assertTrue(failed, "失败记录必须落库")
        payload = failed[-1]
        self.assertNotIn(SECRET, payload["error_message"])
        self.assertNotIn(SECRET, payload["model_output_excerpt"])
        self.assertIn("***", payload["error_message"])

    def test_public_run_projection_scrubs_legacy_rows(self):
        # 旧敏感样本（历史行）读取出口保护，不改写库内值
        legacy = {
            "id": 7, "status": "failed",
            "error_message": f"old leak {SECRET}",
            "model_output_excerpt": f'{{"error": "{SECRET}"}}',
        }
        projected = memory_extract._public_run(legacy)
        self.assertNotIn(SECRET, projected["error_message"])
        self.assertNotIn(SECRET, projected["model_output_excerpt"])
        # 原行不被改写
        self.assertIn(SECRET, legacy["error_message"])

    def test_scrub_error_text_is_shared_helper(self):
        cleaned = scrub_error_text(f"x {SECRET}", 100)
        self.assertNotIn(SECRET, cleaned)


class RuminationFailureScrubTests(unittest.TestCase):
    def test_mark_failed_scrubs_message(self):
        client = _CaptureClient()
        with mock.patch.object(memory_rumination, "get_client", lambda: client), \
                mock.patch.object(
                    __import__("gateway.config", fromlist=["cfg"]).cfg,
                    "UPSTREAM_API_KEY", SECRET):
            memory_rumination._mark_failed(9, "pipeline_error", f"x {SECRET}")
        self.assertEqual(len(client.updates), 1)
        self.assertNotIn(SECRET, client.updates[0]["error_message"])

    def test_public_run_projection_scrubs_legacy_rows(self):
        with mock.patch.object(
                __import__("gateway.config", fromlist=["cfg"]).cfg,
                "UPSTREAM_API_KEY", SECRET):
            projected = memory_rumination._public_run({
                "id": 3, "error_message": f"old {SECRET}"})
        self.assertNotIn(SECRET, projected["error_message"])


class ContinuityFailureScrubTests(unittest.TestCase):
    def test_mark_failed_scrubs_message(self):
        from gateway import memory_continuity

        client = _CaptureClient()
        with mock.patch.object(memory_continuity, "get_client", lambda: client), \
                mock.patch.object(
                    __import__("gateway.config", fromlist=["cfg"]).cfg,
                    "UPSTREAM_API_KEY", SECRET):
            memory_continuity._mark_failed(5, "pipeline_error", f"y {SECRET}")
        self.assertEqual(len(client.updates), 1)
        self.assertNotIn(SECRET, client.updates[0]["error_message"])

    def test_public_run_projection_scrubs_legacy_rows(self):
        from gateway import memory_continuity

        with mock.patch.object(
                __import__("gateway.config", fromlist=["cfg"]).cfg,
                "UPSTREAM_API_KEY", SECRET):
            projected = memory_continuity._public_run({
                "id": 4, "error_message": f"old {SECRET}"})
        self.assertNotIn(SECRET, projected["error_message"])


if __name__ == "__main__":
    unittest.main()
