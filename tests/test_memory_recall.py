"""Behaviour and chat-path isolation for actual injection history."""
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import context, memory_recall, memory_search, admin_api
from gateway.config import cfg


def memory(mid=1, text="当时实际注入的截断内容"):
    return {"id": mid, "title": "标题", "continuity_type": "moment",
            "content": "不应该保存的完整正文", "injection_text": text,
            "recall_embedding": [1, 0, 0]}


class RecallWorkerTests(unittest.TestCase):
    def test_chat_never_waits_for_slow_database_and_snapshot_is_frozen(self):
        entered, release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=1) as executor:
            def persist(payload):
                entered.set()
                release.wait(3)
                self.payload = payload
            original = memory()
            try:
                with (patch.object(memory_recall, "_executor", executor),
                      patch.object(memory_recall, "_slots", BoundedSemaphore(2)),
                      patch.object(memory_recall, "_persist", side_effect=persist),
                      patch.object(memory_recall, "get_client") as get_client):
                    started = time.perf_counter()
                    memory_recall.record_memories_in_background([original])
                    elapsed = time.perf_counter() - started
                    self.assertTrue(entered.wait(1))
                    self.assertLess(elapsed, 0.5)
                    self.assertFalse(release.is_set())
                    original["injection_text"] = "later edit"
                    get_client.assert_not_called()
                    release.set()
                    executor.submit(lambda: None).result(timeout=2)
            finally:
                release.set()
        item = self.payload["p_memories"][0]
        self.assertEqual(item["injection_text"], "当时实际注入的截断内容")
        self.assertNotIn("content", item)
        self.assertNotIn("recall_embedding", item)

    def test_full_queue_returns_without_wait_or_submit(self):
        slots = BoundedSemaphore(1)
        slots.acquire()
        with (patch.object(memory_recall, "_slots", slots),
              patch.object(memory_recall, "_executor") as executor,
              self.assertLogs(memory_recall.log, level="WARNING") as logs):
            memory_recall.record_memories_in_background([memory()])
        executor.submit.assert_not_called()
        self.assertIn("队列已满", logs.output[0])
        slots.release()

    def test_submit_failure_releases_slot_and_does_not_escape(self):
        slots = BoundedSemaphore(1)
        with (patch.object(memory_recall, "_slots", slots),
              patch.object(memory_recall._executor, "submit", side_effect=RuntimeError("closed")),
              self.assertLogs(memory_recall.log, level="ERROR")):
            memory_recall.record_memories_in_background([memory()])
        self.assertTrue(slots.acquire(blocking=False))
        slots.release()

    def test_retry_reuses_batch_id_payload_and_has_no_per_memory_calls(self):
        client = MagicMock()
        client.rpc.return_value.execute.side_effect = [RuntimeError("response lost"), SimpleNamespace(data={"status": "recorded"})]
        payload = {"p_batch_id": "stable", "p_memories": [memory(), memory(2)]}
        with (patch.object(memory_recall, "get_client", return_value=client),
              self.assertLogs(memory_recall.log, level="WARNING")):
            memory_recall._persist(payload)
        self.assertEqual(client.rpc.call_count, 2)
        self.assertEqual(client.rpc.call_args_list[0], client.rpc.call_args_list[1])
        self.assertEqual(client.rpc.call_args.args[0], "record_memory_recall_batch_v1")

    def test_empty_or_invalid_input_does_not_submit(self):
        with patch.object(memory_recall, "_executor") as executor:
            memory_recall.record_memories_in_background([])
            memory_recall.record_memories_in_background([{}, "invalid", {"id": 1}])
        executor.submit.assert_not_called()


class RecallContextTests(unittest.TestCase):
    def test_upstream_failure_does_not_cancel_context_recall_record(self):
        import httpx
        from gateway import main
        upstream = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(500, json={"error": "fixture upstream failure"})))
        with (patch.object(context, "load_persona", return_value=""),
              patch.object(context, "build_timestamp_context", return_value=""),
              patch.object(context.app_settings, "is_recent_chat_injection_enabled", return_value=False),
              patch.object(context.app_settings, "is_eventide_injection_enabled", return_value=False),
              patch.object(context, "search_memories", new=AsyncMock(return_value=[memory()])),
              patch.object(context, "record_memories_in_background") as record,
              patch.object(main, "http_client", upstream),
              patch.object(cfg, "GATEWAY_TOKEN", "recall-test")):
            http = TestClient(main.app, raise_server_exceptions=False)
            response = http.post("/v1/chat/completions",
                json={"messages": [{"role": "user", "content": "聊天"}]},
                headers={"Authorization": "Bearer recall-test"})
        self.assertEqual(response.status_code, 500)
        record.assert_called_once()
        self.assertIn("当时实际注入", upstream.post.call_args.kwargs["json"]["messages"][0]["content"])

    def test_only_context_injected_items_are_scheduled_once(self):
        rows = [memory()]
        with (patch.object(context, "load_persona", return_value=""),
              patch.object(context, "build_timestamp_context", return_value=""),
              patch.object(context.app_settings, "is_recent_chat_injection_enabled", return_value=False),
              patch.object(context.app_settings, "is_eventide_injection_enabled", return_value=False),
              patch.object(context, "search_memories", new=AsyncMock(return_value=rows)),
              patch.object(context, "record_memories_in_background") as record):
            result = context.build_context("聊天")
        self.assertIn(rows[0]["injection_text"], result)
        record.assert_called_once_with(rows)

    def test_no_memory_text_means_no_records(self):
        with (patch.object(context, "load_persona", return_value=""),
              patch.object(context, "build_timestamp_context", return_value=""),
              patch.object(context.app_settings, "is_recent_chat_injection_enabled", return_value=False),
              patch.object(context.app_settings, "is_eventide_injection_enabled", return_value=False),
              patch.object(context, "search_memories", new=AsyncMock(return_value=[])),
              patch.object(context, "record_memories_in_background") as record):
            self.assertEqual(context.build_context("聊天"), "")
        record.assert_not_called()

    def test_both_channels_pass_in_memory_cooldown_setting(self):
        client = MagicMock()
        client.rpc.return_value.execute.return_value.data = []
        for minutes in (0, 60):
            with self.subTest(minutes=minutes), patch.object(cfg, "MEMORY_RECALL_COOLDOWN_MINUTES", minutes):
                with patch.object(memory_search, "get_client", return_value=client):
                    memory_search._keyword_search(["聊天"])
                    memory_search._vector_search_sync([1, 0, 0])
                for call in client.rpc.call_args_list[-2:]:
                    self.assertEqual(call.args[1]["cooldown_seconds"], minutes * 60)


class RecallReadApiTests(unittest.TestCase):
    def setUp(self):
        self.http = TestClient(Starlette(routes=admin_api.admin_api_routes))

    def test_history_is_token_protected_and_read_only(self):
        with patch.object(cfg, "GATEWAY_TOKEN", "recall-test"):
            path = "/admin/api/data/memory_recall_history"
            self.assertEqual(self.http.get(path).status_code, 401)
            headers = {"Authorization": "Bearer recall-test"}
            with patch.object(admin_api, "_execute_list", return_value={"data": []}) as listing:
                self.assertEqual(self.http.get(path + "?limit=150", headers=headers).status_code, 200)
                self.assertEqual(listing.call_args.kwargs["limit"], 150)
            for method in ("post", "delete", "patch"):
                target = path if method == "post" else path + "/1"
                response = getattr(self.http, method)(target, headers=headers)
                self.assertIn(response.status_code, (400, 405))

    def test_stable_order_precedes_the_single_range_read(self):
        client = MagicMock()
        query = client.table.return_value.select.return_value
        query.order.return_value = query
        query.range.return_value = query
        query.execute.return_value = SimpleNamespace(data=[], count=None)
        with patch.object(admin_api.db, "get_client", return_value=client):
            admin_api._execute_list("memory_recall_history", {}, select="id", eq={},
                order="recalled_at", ascending=False, offset=0, limit=150,
                search="", count_only=False)
        self.assertEqual([c.args for c in query.order.call_args_list],
                         [("recalled_at",), ("batch_id",), ("position",), ("id",)])
        query.range.assert_called_once_with(0, 149)
