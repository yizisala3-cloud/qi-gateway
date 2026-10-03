"""BUG-01 读取链路验收：项目真实安装的 Supabase/PostgREST 请求构造器 +
受控离线 httpx transport（不连任何网络/数据库）。

覆盖审查报告 R01/R02/R09 的三个根因，全部经真实 ``postgrest`` 客户端
发出请求（仅传输层换成 MockTransport）：

- R01 ``execute(count=...)`` 不符合真实 SDK 签名：看板、标签、详情、
  搜索、归档、回收站全部读取入口必须发出合法请求并得到 200；
- R02 复合主键表没有 id 列：实际生成的 ``order`` 参数必须落在
  (entry_id, tag_id) / (group_id, entry_id)；
- R09 分页进度与终止：服务端 Max Rows 把每页截小时不得跳行漏页；
  跨页匹配行减少时空页必须有限终止，不允许无限请求。

不产生任何写 RPC（HEAD 读取语义保持）。
"""

import unittest
from unittest import mock

import httpx
from postgrest import SyncPostgrestClient
from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import memo
from gateway.config import cfg
from gateway.memo_api import memo_api_routes

GATEWAY_TOKEN = "memo-transport-test"


def build_offline_client(handler):
    """真实 SyncPostgrestClient，仅把 HTTP 传输替换为受控 handler。"""
    client = SyncPostgrestClient("http://memo-review.invalid")
    client.session.close()
    client.session = httpx.Client(
        base_url="http://memo-review.invalid", transport=httpx.MockTransport(handler))
    return client


class RequestLog:
    """记录每个离线请求的方法/URL/头，供断言。"""

    def __init__(self):
        self.calls = []

    def record(self, request):
        self.calls.append({
            "method": request.method,
            "url": str(request.url),
            "query": dict(request.url.params),
            "prefer": request.headers.get("prefer", ""),
            "path": request.url.path,
        })


class MemoRealTransportReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(memo_api_routes))

    def setUp(self):
        self.log = RequestLog()
        self.client = build_offline_client(self._handler)
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(memo, "get_client", lambda: self.client),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = TestClient(self.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    def tearDown(self):
        self.client.session.close()

    def _handler(self, request):
        raise NotImplementedError

    # ── R01：全部读取入口在真实 SDK 签名下可用 ─────────────────────

    def test_all_read_endpoints_issue_valid_requests(self):
        def handler(request):
            self.log.record(request)
            return httpx.Response(200, json=[], headers={"Content-Range": "*/0"})

        self.client.session._transport = httpx.MockTransport(handler)
        endpoints = (
            "/admin/api/memo/board",
            "/admin/api/memo/tags",
            "/admin/api/memo/entries",
            "/admin/api/memo/entries?status=archived",
            "/admin/api/memo/entries?status=deleted",
            "/admin/api/memo/entries?status=active&q=%E8%B4%AD%E7%89%A9",
        )
        for path in endpoints:
            with self.subTest(path=path):
                response = self.http.get(path, headers=self.auth)
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertIn(body, ([], {"sections": []}),
                              f"空库读取应返回空集合：{path} → {body}")
        self.assertTrue(self.log.calls, "读取必须真实发出 HTTP 请求")
        for call in self.log.calls:
            self.assertNotIn("count=", call["url"],
                             "count 是 select/Prefer 语义，不是查询参数")

    def test_entry_detail_and_composite_key_order_params(self):
        rows = {
            "memo_entry": [{"id": 7, "title": None, "content": "正文", "kind": "note",
                            "status": "active", "content_version": 1,
                            "created_at": "2026-10-02T09:00:00+08:00",
                            "updated_at": "2026-10-02T09:00:00+08:00",
                            "archived_at": None, "deleted_at": None}],
            "memo_entry_tag": [{"entry_id": 7, "tag_id": 1},
                               {"entry_id": 7, "tag_id": 2}],
            "memo_tag": [{"id": 1, "name": "购物", "position": 1},
                         {"id": 2, "name": "灵感", "position": 2}],
        }

        def handler(request):
            self.log.record(request)
            table = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json=rows.get(table, []),
                                  headers={"Content-Range": f"*/{len(rows.get(table, []))}"})

        self.client.session._transport = httpx.MockTransport(handler)
        response = self.http.get("/admin/api/memo/entries/7", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([t["id"] for t in response.json()["tags"]], [1, 2])
        orders = {call["path"].rsplit("/", 1)[-1]: call["query"].get("order")
                  for call in self.log.calls}
        self.assertEqual(orders["memo_entry_tag"], "entry_id.asc,tag_id.asc")
        self.assertEqual(orders["memo_tag"], "id.asc")

    def test_board_reads_composite_key_tables_with_unique_key_order(self):
        def handler(request):
            self.log.record(request)
            return httpx.Response(200, json=[], headers={"Content-Range": "*/0"})

        self.client.session._transport = httpx.MockTransport(handler)
        response = self.http.get("/admin/api/memo/board", headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        orders = {call["path"].rsplit("/", 1)[-1]: call["query"].get("order")
                  for call in self.log.calls}
        self.assertEqual(orders["memo_entry_tag"], "entry_id.asc,tag_id.asc",
                         "关联表必须按 (entry_id, tag_id) 唯一键排序")
        self.assertEqual(orders["memo_position"], "group_id.asc,entry_id.asc",
                         "位置表必须按 (group_id, entry_id) 唯一键排序")
        for call in self.log.calls:
            self.assertIn("count=exact", call["prefer"],
                          "计数必须在 select 阶段声明（Prefer: count=exact）")

    # ── R09：服务端每页上限与跨页集合变化 ──────────────────────────

    def _rows_via_service(self, table, handler):
        """直接调用服务层 _rows，返回（结果行，请求 offset 序列）。"""
        self.client.session._transport = httpx.MockTransport(handler)
        with mock.patch.object(memo, "get_client", lambda: self.client):
            return memo._rows(self.client, table)

    def test_server_page_cap_does_not_skip_rows(self):
        """静态 1500 行、服务端 Max Rows=500（请求页长 1000 被截半）：
        offset 必须按实际返回行数推进（0/500/1000），完整取回 1500 行。"""
        offsets = []

        def handler(request):
            offset = int(request.url.params.get("offset", "0"))
            offsets.append(offset)
            end = min(offset + 500, 1500)   # 服务端把每页截到 500
            page = [{"id": i} for i in range(offset, end)]
            return httpx.Response(
                200, json=page,
                headers={"Content-Range": f"{offset}-{end - 1}/1500"})

        rows = self._rows_via_service("memo_entry", handler)
        self.assertEqual([r["id"] for r in rows], list(range(1500)),
                         "每页被服务端截小时不得跳行漏页")
        self.assertEqual(offsets, [0, 500, 1000],
                         "offset 按实际返回行数推进，且凑满 count 后有限终止")

    def test_matching_rows_shrink_terminates_on_empty_page(self):
        """首页 count=1500，第二页时匹配行缩到 1000：空页必须终止，
        不允许拿旧 total 无限请求。"""
        offsets = []

        def handler(request):
            offset = int(request.url.params.get("offset", "0"))
            offsets.append(offset)
            if offset == 0:
                page = [{"id": i} for i in range(1000)]
                return httpx.Response(200, json=page,
                                      headers={"Content-Range": "0-999/1500"})
            return httpx.Response(200, json=[], headers={"Content-Range": "*/1000"})

        rows = self._rows_via_service("memo_entry", handler)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(offsets, [0, 1000], "空页后立即终止")

    def test_page_size_boundaries_terminate(self):
        """count 可用且恰好凑满（1500 = 1000 + 500）时在末页后终止，
        不多发一次请求。"""
        offsets = []

        def handler(request):
            offset = int(request.url.params.get("offset", "0"))
            offsets.append(offset)
            end = min(offset + 1000, 1500)
            page = [{"id": i} for i in range(offset, end)]
            return httpx.Response(
                200, json=page,
                headers={"Content-Range": f"{offset}-{end - 1}/1500"})

        rows = self._rows_via_service("memo_entry", handler)
        self.assertEqual(len(rows), 1500)
        self.assertEqual(offsets, [0, 1000])

    # ── BUG-01：集合缩减后的 416/PGRST103 范围越界 ─────────────────

    def test_shrunk_collection_416_terminates_with_collected_rows(self):
        """首页 1000 条（count=1500），下一页前匹配数缩到 999：offset=1000
        越过最新总数，真实服务端形态 = 416 + {"code": "PGRST103"}——按
        已声明的分页一致性边界终止，已取行即全量（BUG-01）。"""
        offsets = []

        def handler(request):
            offset = int(request.url.params.get("offset", "0"))
            offsets.append(offset)
            if offset == 0:
                page = [{"id": i} for i in range(1000)]
                return httpx.Response(200, json=page,
                                      headers={"Content-Range": "0-999/1500"})
            # 集合已缩到 999：范围起点 1000 不可满足 → PostgREST 416
            return httpx.Response(416, json={
                "code": "PGRST103",
                "message": "Requested range not satisfiable on public.memo_entry",
                "hint": None, "details": None,
            }, headers={"Content-Range": "*/999"})

        rows = self._rows_via_service("memo_entry", handler)
        self.assertEqual([r["id"] for r in rows], list(range(1000)),
                         "416 时已取行即全量，不得报内部错误")
        self.assertEqual(offsets, [0, 1000], "416 后立即有限终止")

    def test_legacy_416_without_code_field_terminates_too(self):
        """旧版 PostgREST 的 416 响应无 code 字段：SDK 以数字状态码兜底，
        同样按范围越界终止，不伪装成其他数据库错误。"""
        offsets = []

        def handler(request):
            offset = int(request.url.params.get("offset", "0"))
            offsets.append(offset)
            if offset == 0:
                return httpx.Response(
                    200, json=[{"id": i} for i in range(1000)],
                    headers={"Content-Range": "0-999/1500"})
            return httpx.Response(416, text="Range Not Satisfiable",
                                  headers={"Content-Range": "*/999"})

        rows = self._rows_via_service("memo_entry", handler)
        self.assertEqual(len(rows), 1000)
        self.assertEqual(offsets, [0, 1000])

    def test_other_database_errors_still_fail_during_pagination(self):
        """非范围错误（如 42P01 关系缺失）不得被吞成部分成功——原样上抛，
        由 API 层返回明确失败（BUG-01 验收：与真实范围错误明确区分）。"""

        def handler(request):
            offset = int(request.url.params.get("offset", "0"))
            if offset == 0:
                return httpx.Response(
                    200, json=[{"id": i} for i in range(1000)],
                    headers={"Content-Range": "0-999/1500"})
            return httpx.Response(500, json={
                "code": "42P01", "message": "relation does not exist"})

        with self.assertRaises(Exception) as caught:
            self._rows_via_service("memo_entry", handler)
        # 上抛的是 SDK 的 APIError，不是被吞掉后的假成功；且不得被当成
        # 范围越界终止（真实安装的 postgrest 以数字状态码为 APIError.code，
        # body 里的 42P01 只出现在 details 原文中）
        self.assertEqual(getattr(caught.exception, "code", None), 500)
        self.assertFalse(memo._is_range_not_satisfiable(caught.exception),
                         "非范围错误不得按 416 终止")

    def test_head_read_does_not_invoke_write_rpc(self):
        """HEAD 读取语义（F20）在真实传输下保持：API 层以 HEAD 进入读取
        分支，SDK 层只发出无正文的读请求，不触发任何写路径。"""
        def handler(request):
            self.log.record(request)
            return httpx.Response(200, json=[], headers={"Content-Range": "*/0"})

        self.client.session._transport = httpx.MockTransport(handler)
        response = self.http.head("/admin/api/memo/entries", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.log.calls)
        for call in self.log.calls:
            self.assertNotIn(call["method"], ("POST", "PATCH"),
                             "HEAD 不得触发写请求")


if __name__ == "__main__":
    unittest.main()
