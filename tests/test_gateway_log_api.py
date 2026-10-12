"""日志只读查询 API 契约测试（§8）。

鉴权只认 Bearer GATEWAY_TOKEN（未配置 token 时拒绝）；错误返回
``{"error", "error_code"[, "request_id"]}``；存储未就绪返回中文错误而非
``200 + []``；参数白名单与 cursor 校验；静态路由不被参数路由吞掉。
"""

import unittest
from unittest import mock

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.testclient import TestClient

from gateway.logging_events import GatewayLogMiddleware

from gateway import log_api, log_store
from gateway.config import cfg

GATEWAY_TOKEN = "gateway-token-log-test"


def _group(**overrides):
    base = {
        "fingerprint": "a" * 16, "event_count": 3,
        "first_seen_at": "2026-10-10T00:00:00+00:00",
        "last_seen_at": "2026-10-10T01:00:00+00:00",
        "level": "ERROR", "module": "planning", "operation": "create",
        "error_code": "internal_error", "message_zh": "规划接口发生内部错误",
        "exception_type": "APIError", "logger_name": "gateway.planning_api",
        "source": "a.py:fn:1", "outcome": "failed",
        "instance_id": "inst", "sample_event_id":
            "00000000-0000-0000-0000-000000000001",
        "sample_request_id": "r1",
    }
    base.update(overrides)
    return base


class LogApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(
            routes=list(log_api.log_api_routes),
            middleware=[Middleware(GatewayLogMiddleware)],
        )

    def setUp(self):
        self.groups = [
            _group(fingerprint="a" * 16, last_seen_at="2026-10-10T01:00:00+00:00"),
            _group(fingerprint="b" * 16, last_seen_at="2026-10-10T00:30:00+00:00"),
        ]
        self.query_result = {
            "groups": self.groups,
            "has_more": True,
            # BUG-05：RPC 返回本事务的可见性快照，API 编入 cursor。
            "visible_snapshot": "100:110:101,105",
        }
        self.facets_result = {
            "modules": [{"module": "planning", "count": 3}],
            "levels": [{"level": "ERROR", "count": 3}],
            "total_events": 3,
            "oldest_at": "2026-10-10T00:00:00+00:00",
            "newest_at": "2026-10-10T01:00:00+00:00",
        }
        self.group_result = {
            "group": _group(),
            "samples": [{
                "event_id": "00000000-0000-0000-0000-000000000001",
                "occurred_at": "2026-10-10T01:00:00+00:00",
                "level": "ERROR", "module": "planning",
                "message_raw": "raw", "stack_trace": "trace",
                "request_id": "r1", "truncated_fields": [],
            }],
        }
        self.event_result = {"event": {
            "event_id": "00000000-0000-0000-0000-000000000001",
            "level": "ERROR", "module": "planning",
            "message_raw": "raw", "stack_trace": "trace",
        }}
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(log_store, "query_groups", self._fake_query),
            mock.patch.object(log_store, "facets", self._fake_facets),
            mock.patch.object(log_store, "get_group", self._fake_group),
            mock.patch.object(log_store, "get_event", self._fake_event),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.http = TestClient(self.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def _fake_query(self, **kwargs):
        self.query_kwargs = kwargs
        return self.query_result

    def _fake_facets(self, start=None, end=None):
        return self.facets_result

    def _fake_group(self, fingerprint, **kwargs):
        self.group_kwargs = kwargs
        return self.group_result

    def _fake_event(self, event_id):
        return self.event_result["event"] if event_id.startswith("00000000") else None

    # ── 鉴权 ──

    def test_token_is_required(self):
        for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                response = self.http.get("/admin/api/logs", headers=headers)
                self.assertEqual(response.status_code, 401)
                body = response.json()
                self.assertEqual(body["error_code"], "unauthorized")

    def test_unconfigured_token_refuses_access(self):
        with mock.patch.object(cfg, "GATEWAY_TOKEN", ""):
            response = self.http.get("/admin/api/logs", headers=self.auth)
            self.assertEqual(response.status_code, 401)

    # ── 列表 ──

    def test_list_returns_groups_and_cursor(self):
        response = self.http.get("/admin/api/logs", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["groups"]), 2)
        self.assertTrue(body["has_more"])
        # BUG-05：cursor = 快照|可见性快照|已看集合（本页组并入已看）；
        # 响应同时返回快照与可见性快照供详情复用
        snapshot, visible, seen = body["next_cursor"].split("|", 2)
        self.assertTrue(snapshot)
        self.assertRegex(visible, r"^\d+:\d+:(\d+(,\d+)*)?$")
        self.assertEqual(seen.split(","), ["a" * 16, "b" * 16])
        self.assertEqual(body["snapshot"], snapshot)
        self.assertEqual(body["visible_snapshot"], visible)
        self.assertIn("request_id", body)

    def test_list_passes_filters_to_store(self):
        self.http.get(
            "/admin/api/logs?level=ERROR&module=planning"
            "&start=2026-10-10T00:00:00%2B08:00&end=2026-10-11T00:00:00%2B08:00"
            "&search=APIError&limit=30",
            headers=self.auth)
        kwargs = self.query_kwargs
        self.assertEqual(kwargs["level"], "ERROR")
        self.assertEqual(kwargs["module"], "planning")
        # 无时区时间按 Asia/Shanghai 解释并归一化为 UTC
        self.assertEqual(kwargs["start"], "2026-10-09T16:00:00+00:00")
        self.assertEqual(kwargs["end"], "2026-10-10T16:00:00+00:00")
        self.assertEqual(kwargs["search"], "APIError")
        self.assertEqual(kwargs["limit"], 30)

    def test_list_rejects_invalid_params(self):
        cases = [
            ("level=INFO", "level"),
            ("module=NotValid", "module"),
            ("start=not-a-time", "start"),
            ("limit=0", "limit"),
            ("limit=101", "limit"),
            ("cursor=broken", "cursor"),
            ("start=2026-10-11T00:00:00%2B08:00&end=2026-10-10T00:00:00%2B08:00", "start"),
        ]
        for query, name in cases:
            with self.subTest(query=query):
                response = self.http.get(f"/admin/api/logs?{query}", headers=self.auth)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error_code"], "invalid_request")

    def test_cursor_round_trip(self):
        # BUG-05：cursor = snapshot|visible|fp,fp（快照 + 可见性 + 已看集合）
        cursor = (
            "2026-10-10T02:00:00+00:00|100:110:101,105|"
            + "a" * 16 + "," + "b" * 16
        )
        self.http.get(f"/admin/api/logs?cursor={cursor.replace('+', '%2B')}", headers=self.auth)
        self.assertEqual(self.query_kwargs["seen"], ["a" * 16, "b" * 16])
        self.assertEqual(self.query_kwargs["snapshot"], "2026-10-10T02:00:00+00:00")
        self.assertEqual(self.query_kwargs["visible"], "100:110:101,105")

    def test_cursor_rejects_invalid_fingerprint(self):
        cursor = "2026-10-10T02:00:00+00:00|100:110:|not-a-fingerprint"
        response = self.http.get(
            f"/admin/api/logs?cursor={cursor.replace('+', '%2B')}", headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_request")

    def test_cursor_rejects_invalid_visible_snapshot(self):
        cursor = "2026-10-10T02:00:00+00:00|not-a-snapshot|" + "a" * 16
        response = self.http.get(
            f"/admin/api/logs?cursor={cursor.replace('+', '%2B')}", headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_request")

    def test_cursor_rejects_oversized_seen_set(self):
        # BUG-05：已看集合有上限（查询有界）；超出引导刷新
        seen = ",".join(f"{index:016x}" for index in range(1001))
        cursor = "2026-10-10T02:00:00+00:00|100:110:|" + seen
        response = self.http.get(
            f"/admin/api/logs?cursor={cursor.replace('+', '%2B')}", headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertIn("翻页会话过长", response.json()["error"])

    def test_legacy_two_part_cursor_is_rejected(self):
        # 旧两段式 snapshot|fp,fp 不再合法（可见性快照必须随会话回传）
        cursor = "2026-10-10T02:00:00+00:00|" + "a" * 16
        response = self.http.get(
            f"/admin/api/logs?cursor={cursor.replace('+', '%2B')}", headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_first_page_establishes_snapshot(self):
        # 无 cursor 的第一页由服务端建立入库快照并传给存储层
        self.http.get("/admin/api/logs", headers=self.auth)
        snapshot = self.query_kwargs["snapshot"]
        self.assertTrue(snapshot)
        from datetime import datetime, timezone

        parsed = datetime.fromisoformat(snapshot)
        self.assertIsNotNone(parsed.tzinfo)
        self.assertLess(
            abs((datetime.now(timezone.utc) - parsed).total_seconds()), 60
        )

    def test_no_more_pages_omits_cursor(self):
        self.query_result = {"groups": self.groups, "has_more": False}
        response = self.http.get("/admin/api/logs", headers=self.auth)
        self.assertIsNone(response.json()["next_cursor"])

    # ── meta / 静态路由优先 ──

    def test_meta_returns_facets_and_collection_status(self):
        response = self.http.get("/admin/api/logs/meta", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["modules"][0]["module"], "planning")
        self.assertIn("collection", body)
        self.assertIn("queue_depth", body["collection"])
        self.assertIn("spool", body["collection"])

    def test_static_routes_are_not_swallowed(self):
        # meta / group / event 均为独立路径，不被列表路由吞掉
        self.assertEqual(
            self.http.get("/admin/api/logs/meta", headers=self.auth).status_code, 200)
        self.assertEqual(
            self.http.get(
                "/admin/api/logs/group?fingerprint=" + "a" * 16,
                headers=self.auth).status_code, 200)
        self.assertEqual(
            self.http.get(
                "/admin/api/logs/event/00000000-0000-0000-0000-000000000001",
                headers=self.auth).status_code, 200)

    # ── 详情 ──

    def test_event_detail_returns_event(self):
        response = self.http.get(
            "/admin/api/logs/event/00000000-0000-0000-0000-000000000001",
            headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event"]["module"], "planning")

    def test_event_detail_missing_returns_real_404(self):
        response = self.http.get(
            "/admin/api/logs/event/11111111-1111-1111-1111-111111111111",
            headers=self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error_code"], "not_found")

    def test_event_detail_rejects_malformed_id(self):
        response = self.http.get("/admin/api/logs/event/not-a-uuid", headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_group_detail_returns_summary_and_samples(self):
        response = self.http.get(
            f"/admin/api/logs/group?fingerprint={'a' * 16}", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["group"]["event_count"], 3)
        self.assertEqual(len(body["samples"]), 1)
        self.assertEqual(body["sample_limit"], 20)

    def test_group_detail_reuses_list_filters_and_snapshot(self):
        # BUG-06：详情复用列表的完整筛选与快照，不静默扩大到整组
        response = self.http.get(
            "/admin/api/logs/group?fingerprint=" + "a" * 16
            + "&level=WARNING&module=planning&search=wanted-request"
            + "&start=2026-10-10T00:00:00%2B08:00&end=2026-10-11T00:00:00%2B08:00"
            + "&snapshot=2026-10-10T02:00:00%2B00:00",
            headers=self.auth)
        self.assertEqual(response.status_code, 200)
        kwargs = self.group_kwargs
        self.assertEqual(kwargs["level"], "WARNING")
        self.assertEqual(kwargs["module"], "planning")
        self.assertEqual(kwargs["search"], "wanted-request")
        self.assertEqual(kwargs["start"], "2026-10-09T16:00:00+00:00")
        self.assertEqual(kwargs["end"], "2026-10-10T16:00:00+00:00")
        self.assertEqual(kwargs["snapshot"], "2026-10-10T02:00:00+00:00")

    def test_group_detail_rejects_invalid_snapshot(self):
        response = self.http.get(
            f"/admin/api/logs/group?fingerprint={'a' * 16}&snapshot=not-a-time",
            headers=self.auth)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_request")

    def test_group_detail_missing_returns_404(self):
        with mock.patch.object(
                log_store, "get_group", lambda fp, **kw: {"group": {}, "samples": []}):
            response = self.http.get(
                f"/admin/api/logs/group?fingerprint={'a' * 16}", headers=self.auth)
        self.assertEqual(response.status_code, 404)

    def test_group_detail_rejects_malformed_fingerprint(self):
        response = self.http.get(
            "/admin/api/logs/group?fingerprint=XYZ", headers=self.auth)
        self.assertEqual(response.status_code, 400)

    # ── 存储未就绪：中文错误，不伪装成功 ──

    def test_store_unavailable_returns_503_chinese_error(self):
        with mock.patch.object(
                log_store, "query_groups",
                side_effect=log_store.LogStoreUnavailable("no client")):
            response = self.http.get("/admin/api/logs", headers=self.auth)
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body["error_code"], "log_store_unavailable")
        self.assertIn("日志查询暂不可用", body["error"])
        self.assertIn("request_id", body)

    def test_schema_missing_returns_503_specific_code(self):
        with mock.patch.object(
                log_store, "query_groups",
                side_effect=log_store.LogSchemaMissing("PGRST204")):
            response = self.http.get("/admin/api/logs", headers=self.auth)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "log_schema_missing")

    def test_unexpected_store_error_returns_500(self):
        with mock.patch.object(
                log_store, "facets", side_effect=RuntimeError("boom")):
            response = self.http.get("/admin/api/logs/meta", headers=self.auth)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error_code"], "internal_error")

    # ── BUG-12：查询失败不随查询次数放大 ────────────────────────

    def test_repeated_query_failures_do_not_spam_boundary_events(self):
        # 查询链自身失败（含限频被抑制的请求）一律置位：本请求 5xx 不再
        # 触发边界兜底；60 秒窗口内只有一条内部事件。
        from gateway import logging_events

        # 采集现在默认关闭；本用例验证开启后的限频与边界去重。
        enabled = mock.patch.object(cfg, "GATEWAY_LOG_ENABLED", True)
        enabled.start()
        self.addCleanup(enabled.stop)

        queue = logging_events._queue
        queue.drain(10000)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(queue.drain, 10000)
        log_api._self_log_last = 0.0
        self.addCleanup(setattr, log_api, "_self_log_last", 0.0)
        with mock.patch.object(
                log_store, "query_groups",
                side_effect=RuntimeError("store down")):
            for _ in range(5):
                response = self.http.get("/admin/api/logs", headers=self.auth)
                self.assertEqual(response.status_code, 500)
        events = queue.drain(10000)
        # 1 条限频内部事件 + 0 条边界兜底（此前是 1 + 4）
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["logger_name"], "gateway.log_api")
        self.assertNotIn("http_5xx_boundary", [e["error_code"] for e in events])

    def test_store_unavailable_also_suppresses_boundary(self):
        # 已知 503 分支（客户端不可用 / 缺迁移）同样不触发边界兜底
        from gateway import logging_events

        queue = logging_events._queue
        queue.drain(10000)
        logging_events.install_handler()
        self.addCleanup(logging_events.uninstall_handler)
        self.addCleanup(queue.drain, 10000)
        log_api._self_log_last = 0.0
        self.addCleanup(setattr, log_api, "_self_log_last", 0.0)
        with mock.patch.object(
                log_store, "query_groups",
                side_effect=log_store.LogStoreUnavailable("no client")):
            for _ in range(3):
                response = self.http.get("/admin/api/logs", headers=self.auth)
                self.assertEqual(response.status_code, 503)
        events = queue.drain(10000)
        self.assertEqual(
            [e["error_code"] for e in events].count("http_5xx_boundary"), 0)


class SchemaClassificationTests(unittest.TestCase):
    """BUG-17：只按明确缺失证据识别「迁移未执行」，权限错误不误诊。"""

    def test_permission_denied_is_not_schema_missing(self):
        class FakeAPIError(Exception):
            code = "42501"

        exc = FakeAPIError(
            'permission denied for table gateway_error_events')
        self.assertFalse(log_store._is_schema_missing(exc))
        self.assertIsInstance(log_store._wrap(exc), FakeAPIError)

    def test_undefined_table_code_is_schema_missing(self):
        class FakeAPIError(Exception):
            code = "42P01"

        exc = FakeAPIError('relation "gateway_error_events" does not exist')
        self.assertTrue(log_store._is_schema_missing(exc))
        self.assertIsInstance(log_store._wrap(exc), log_store.LogSchemaMissing)

    def test_undefined_function_and_schema_cache_are_schema_missing(self):
        class PGRST204(Exception):
            code = "PGRST204"

        class PGRST202(Exception):
            code = "PGRST202"

        self.assertTrue(log_store._is_schema_missing(PGRST204("cache miss")))
        self.assertTrue(log_store._is_schema_missing(PGRST202("cache miss")))

    def test_timeout_and_connection_errors_are_not_schema_missing(self):
        self.assertFalse(log_store._is_schema_missing(RuntimeError(
            "connection refused while querying gateway_log_query")))
        self.assertFalse(log_store._is_schema_missing(RuntimeError(
            "request to gateway_error_events timed out")))

    def test_object_name_alone_is_not_missing_evidence(self):
        # 对象名出现在错误文本里不构成缺失证据（BUG-17 根因）
        self.assertFalse(log_store._is_schema_missing(RuntimeError(
            "lock timeout on gateway_error_events")))

    def test_missing_semantics_without_code_still_detected(self):
        self.assertTrue(log_store._is_schema_missing(RuntimeError(
            'relation "gateway_error_events" does not exist')))
        self.assertTrue(log_store._is_schema_missing(RuntimeError(
            "Could not find the function public.gateway_log_query")))


if __name__ == "__main__":
    unittest.main()
