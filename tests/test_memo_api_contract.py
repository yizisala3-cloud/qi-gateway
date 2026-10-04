"""备忘录 API 契约测试（HTTP 层，参照 test_planning_api_contract.py 惯例）。

鉴权只认 Bearer GATEWAY_TOKEN；错误返回 ``{"error"[, "error_code"]}`` 配
400/401/404/409/500。RPC 语义本身由 pgserver 集成测试在真实 PostgreSQL 上
验证，这里用最小 fake 验证路由、鉴权、错误映射与载荷传递。
"""

import unittest
from unittest import mock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from gateway import memo
from gateway.config import cfg
from gateway.memo_api import memo_api_routes

GATEWAY_TOKEN = "gateway-token-test"

ENTRY = {"id": 3, "title": None, "content": "正文", "kind": "note",
         "status": "active", "content_version": 2, "tags": [{"id": 1, "name": "购物"}],
         "created_at": "2026-10-02T09:00:00+08:00", "updated_at": "2026-10-02T10:00:00+08:00",
         "archived_at": None, "deleted_at": None}


class RpcError(Exception):
    def __init__(self, sqlstate, message):
        super().__init__(message)
        self.code = sqlstate


class FakeQuery:
    def __init__(self, rows, table):
        self.rows = rows
        self.table = table
        self.filters = []
        self.orders = []
        self.max_rows = None
        self.range_window = None
        self.count_method = None

    def select(self, *columns, count=None, head=None):
        # 与真实 postgrest ≥1.0 一致：count 在 select 阶段声明
        self.count_method = count
        return self

    def eq(self, field, value):
        self.filters.append(("eq", field, value))
        return self

    def in_(self, field, values):
        self.filters.append(("in", field, list(values)))
        return self

    def order(self, field, desc=False):
        self.orders.append((field, desc))
        return self

    def limit(self, value):
        self.max_rows = value
        return self

    def range(self, start, end):
        self.range_window = (start, end)
        return self

    def execute(self):
        # 与真实 SDK 一致：execute() 不接收任何参数。若服务层回退成
        # execute(count="exact")（BUG-01/R01），这里会立即 TypeError 而不是
        # 被假件默默容纳。
        from types import SimpleNamespace
        matched = list(self.rows.setdefault(self.table, []))
        for kind, field, value in self.filters:
            if kind == "eq":
                matched = [r for r in matched if r.get(field) == value]
            else:
                matched = [r for r in matched if r.get(field) in value]
        for field, desc in reversed(self.orders):
            matched.sort(key=lambda r: r.get(field) or 0, reverse=desc)
        if self.max_rows is not None:
            matched = matched[: self.max_rows]
        if self.range_window is not None:
            start, end = self.range_window
            total = len(matched)
            matched = matched[start: end + 1]
            return SimpleNamespace(data=matched, count=total if self.count_method else None)
        return SimpleNamespace(data=matched, count=None)


class FakeClient:
    """记录 RPC 调用并按预设脚本返回；表查询直接落在内存行上。"""

    def __init__(self, rpc_results=None, rpc_errors=None):
        self.rows = {
            "memo_entry": [dict(ENTRY)],
            "memo_tag": [{"id": 1, "name": "购物", "position": 1,
                          "created_at": "2026-10-01T09:00:00+08:00"}],
            "memo_group": [],
            "memo_entry_tag": [{"entry_id": 3, "tag_id": 1,
                                "created_at": "2026-10-01T09:00:00+08:00"}],
            "memo_position": [],
        }
        self.rpc_calls = []
        self.queries = []
        self.rpc_results = rpc_results or {}
        self.rpc_errors = rpc_errors or {}

    def table(self, name):
        query = FakeQuery(self.rows, name)
        self.queries.append(query)
        return query

    def rpc(self, fn, params=None):
        self.rpc_calls.append((fn, params))
        from types import SimpleNamespace
        if fn in self.rpc_errors:
            raise self.rpc_errors[fn]
        result = self.rpc_results.get(fn)
        return SimpleNamespace(execute=lambda: SimpleNamespace(data=result))


class MemoApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = Starlette(routes=list(memo_api_routes))

    def setUp(self):
        self.fake = FakeClient()
        patches = [
            mock.patch.object(cfg, "GATEWAY_TOKEN", GATEWAY_TOKEN),
            mock.patch.object(memo, "get_client", lambda: self.fake),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.http = TestClient(self.app, raise_server_exceptions=False)
        self.auth = {"Authorization": f"Bearer {GATEWAY_TOKEN}"}

    # ── 鉴权 ──

    def test_gateway_token_is_required(self):
        for headers in ({}, {"Authorization": ""}, {"Authorization": "Bearer wrong"}):
            with self.subTest(headers=headers):
                response = self.http.get("/admin/api/memo/board", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["error_code"], "unauthorized")

    # ── 读取 ──

    def test_board_returns_sections(self):
        response = self.http.get("/admin/api/memo/board", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["sections"]), 1)
        section = body["sections"][0]
        self.assertEqual(section["tag"]["name"], "购物")
        self.assertEqual(section["note_sort_mode"], "latest")
        self.assertEqual([item["id"] for item in section["items"]], [3])
        self.assertEqual(section["pinned_count"], 0)
        self.assertEqual(section["note_count"], 1)

    def test_entry_detail_merges_tags(self):
        response = self.http.get("/admin/api/memo/entries/3", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["content_version"], 2)
        self.assertEqual(body["tags"], [{"id": 1, "name": "购物"}])

    def test_entry_detail_404(self):
        response = self.http.get("/admin/api/memo/entries/999", headers=self.auth)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error_code"], "not_found")

    def test_list_entries_archived_and_search(self):
        response = self.http.get("/admin/api/memo/entries?status=archived", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        response = self.http.get(
            "/admin/api/memo/entries?status=active&q=%E8%B4%AD%E7%89%A9", headers=self.auth)
        self.assertEqual(response.status_code, 200)

    def test_list_entries_rejects_unknown_status(self):
        response = self.http.get("/admin/api/memo/entries?status=bogus", headers=self.auth)
        self.assertEqual(response.status_code, 400)

    # ── 创建（载荷传递 + 201） ──

    def test_create_entry_returns_201_and_forwards_payload(self):
        self.fake.rpc_results["memo_create_entry"] = ENTRY
        response = self.http.post(
            "/admin/api/memo/entries",
            json={"kind": "note", "title": "", "content": "正文", "tag_ids": [1],
                  "client_request_id": "crid-1"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 201)
        fn, params = self.fake.rpc_calls[0]
        self.assertEqual(fn, "memo_create_entry")
        self.assertEqual(params["p_payload"]["kind"], "note")
        self.assertIsNone(params["p_payload"]["title"])   # 空标题归一为 NULL
        self.assertEqual(params["p_payload"]["client_request_id"], "crid-1")

    def test_create_entry_requires_content(self):
        response = self.http.post(
            "/admin/api/memo/entries", json={"kind": "note", "content": "  "},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_payload")
        self.assertEqual(self.fake.rpc_calls, [])   # 校验先行，零写入

    def test_create_entry_rejects_unknown_kind(self):
        response = self.http.post(
            "/admin/api/memo/entries", json={"kind": "memo", "content": "正文"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)

    # ── 编辑（版本冲突 / 生命周期冲突映射 409） ──

    def test_update_forwards_expected_version(self):
        self.fake.rpc_results["memo_update_entry"] = ENTRY
        response = self.http.patch(
            "/admin/api/memo/entries/3",
            json={"expected_version": 2, "content": "新正文"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        fn, params = self.fake.rpc_calls[0]
        self.assertEqual(fn, "memo_update_entry")
        self.assertEqual(params["p_entry_id"], 3)
        self.assertEqual(params["p_expected_version"], 2)
        self.assertEqual(params["p_patch"], {"content": "新正文"})

    def test_stale_version_maps_to_409_version_conflict(self):
        self.fake.rpc_errors["memo_update_entry"] = RpcError(
            "ME002", "memo_update_entry: stale content_version")
        response = self.http.patch(
            "/admin/api/memo/entries/3",
            json={"expected_version": 1, "content": "旧设备正文"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertEqual(body["error_code"], "version_conflict")
        self.assertIn("其他设备", body["error"])

    def test_archived_entry_edit_maps_to_409_lifecycle_conflict(self):
        self.fake.rpc_errors["memo_update_entry"] = RpcError(
            "ME003", "memo_update_entry: entry is archived")
        response = self.http.patch(
            "/admin/api/memo/entries/3",
            json={"expected_version": 2, "content": "迟到自动保存"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "lifecycle_conflict")

    def test_unmapped_rpc_failure_is_503_not_fake_success(self):
        self.fake.rpc_errors["memo_update_entry"] = RuntimeError("connection reset")
        response = self.http.patch(
            "/admin/api/memo/entries/3",
            json={"expected_version": 2, "content": "正文"},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "database_unavailable")

    def test_update_requires_expected_version(self):
        response = self.http.patch(
            "/admin/api/memo/entries/3", json={"content": "正文"}, headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)

    # ── 生命周期 ──

    def test_lifecycle_routes_forward_action_and_version(self):
        self.fake.rpc_results["memo_set_entry_lifecycle"] = ENTRY
        for action in ("archive", "delete", "restore"):
            self.fake.rpc_calls.clear()
            response = self.http.post(
                f"/admin/api/memo/entries/3/{action}",
                json={"expected_version": 2},
                headers=self.auth,
            )
            self.assertEqual(response.status_code, 200, action)
            # 生命周期入口先做回收站到期清扫（M19），随后才是业务 RPC
            self.assertEqual(self.fake.rpc_calls[0][0], "memo_purge_expired_trash")
            fn, params = self.fake.rpc_calls[1]
            self.assertEqual(fn, "memo_set_entry_lifecycle")
            self.assertEqual(params["p_action"], action)
            self.assertEqual(params["p_expected_version"], 2)

    def test_read_and_lifecycle_entrypoints_purge_expired_trash_first(self):
        """M19：看板 / 列表 / 详情入口均先调用到期清扫 RPC；清扫失败不
        阻塞读取（恢复路径由数据库保留期门独立兜底）。"""
        for path in ("/admin/api/memo/board",
                     "/admin/api/memo/entries?status=deleted",
                     "/admin/api/memo/entries/3"):
            self.fake.rpc_calls.clear()
            response = self.http.get(path, headers=self.auth)
            self.assertEqual(response.status_code, 200, path)
            self.assertEqual(self.fake.rpc_calls[0][0], "memo_purge_expired_trash",
                             path)
            self.assertIn("p_now", self.fake.rpc_calls[0][1])
        self.fake.rpc_errors["memo_purge_expired_trash"] = RuntimeError("db down")
        response = self.http.get("/admin/api/memo/board", headers=self.auth)
        self.assertEqual(response.status_code, 200)

    # ── 标签 ──

    def test_tags_list_and_create(self):
        response = self.http.get("/admin/api/memo/tags", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["name"], "购物")

        self.fake.rpc_results["memo_create_tag"] = {
            "id": 2, "name": "书影音", "position": 2, "existed": False,
            "created_at": "2026-10-02T09:00:00+08:00"}
        response = self.http.post(
            "/admin/api/memo/tags", json={"name": " 书影音 "}, headers=self.auth)
        self.assertEqual(response.status_code, 201)
        fn, params = self.fake.rpc_calls[0]
        self.assertEqual(params["p_name"], " 书影音 ")   # 原文交给 RPC 权威 trim

    def test_create_tag_rejects_blank(self):
        response = self.http.post(
            "/admin/api/memo/tags", json={"name": "   "}, headers=self.auth)
        self.assertEqual(response.status_code, 400)

    def test_delete_tag_route(self):
        self.fake.rpc_results["memo_delete_tag"] = {"deleted": True, "affected": 1}
        response = self.http.post("/admin/api/memo/tags/1/delete", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["affected"], 1)

    # ── 排序与模式 ──

    def test_reorder_group_forwards_scope_and_section(self):
        self.fake.rpc_results["memo_reorder_group"] = {"group_id": 11, "order": [3]}
        response = self.http.post(
            "/admin/api/memo/reorder",
            json={"scope": "group", "tag_id": 1, "section": "pinned", "order": [3]},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        fn, params = self.fake.rpc_calls[0]
        self.assertEqual(fn, "memo_reorder_group")
        self.assertEqual(params["p_scope"], {"tag_id": 1})
        self.assertEqual(params["p_section"], "pinned")

    def test_reorder_untagged_uses_untagged_scope(self):
        self.fake.rpc_results["memo_reorder_group"] = {"group_id": 13, "order": []}
        response = self.http.post(
            "/admin/api/memo/reorder",
            json={"scope": "group", "tag_id": None, "section": "note", "order": []},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        _, params = self.fake.rpc_calls[0]
        self.assertEqual(params["p_scope"], {"untagged": True})

    def test_reorder_rejects_unknown_scope(self):
        response = self.http.post(
            "/admin/api/memo/reorder", json={"scope": "everything", "order": []},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 400)

    def test_concurrent_modified_maps_to_409(self):
        self.fake.rpc_errors["memo_reorder_group"] = RpcError(
            "ME004", "memo_reorder_group: member set changed (concurrent change)")
        response = self.http.post(
            "/admin/api/memo/reorder",
            json={"scope": "group", "tag_id": 1, "section": "note", "order": [3]},
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error_code"], "concurrent_modified")

    def test_note_mode_route(self):
        self.fake.rpc_results["memo_set_note_mode"] = {"group_id": 12, "note_sort_mode": "manual"}
        response = self.http.post(
            "/admin/api/memo/note-mode",
            json={"tag_id": 2, "mode": "manual"}, headers=self.auth,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["note_sort_mode"], "manual")

    def test_invalid_json_body_is_400(self):
        response = self.http.post(
            "/admin/api/memo/entries", content=b"not-json",
            headers={**self.auth, "Content-Type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error_code"], "invalid_json")

    # ── F07：完整读取（分页）与 F20：HEAD 读取语义 ──────────────────

    def test_reads_beyond_single_request_cap_are_complete(self):
        """2500 条记录 + 关联在假件上超过 Supabase 默认单请求上限的行为等价：
        服务层必须分页取全，看板/列表/搜索不得截断或误归未分类。"""
        from gateway import memo as memo_mod
        fake = self.fake
        entries = [
            {"id": i, "title": None, "content": f"body{i}", "kind": "note",
             "status": "active", "content_version": 1,
             "created_at": f"2026-10-01T00:00:00+08:00",
             "updated_at": f"2026-10-01T00:00:00+08:00",
             "archived_at": None, "deleted_at": None}
            for i in range(1, 2501)
        ]
        # 前 2499 条挂在 tag 1，最后一条同时挂 tag 1/2（多标签不误归未分类）
        entry_tags = [{"entry_id": i, "tag_id": 1} for i in range(1, 2501)]
        entry_tags.append({"entry_id": 2500, "tag_id": 2})
        fake.rows["memo_entry"] = entries
        fake.rows["memo_entry_tag"] = entry_tags
        fake.rows["memo_tag"] = [
            {"id": 1, "name": "甲", "position": 1, "created_at": "2026-10-01T00:00:00+08:00"},
            {"id": 2, "name": "乙", "position": 2, "created_at": "2026-10-01T00:00:00+08:00"},
        ]
        fake.rows["memo_group"] = [
            {"id": 11, "tag_id": 1, "note_sort_mode": "latest"},
            {"id": 12, "tag_id": 2, "note_sort_mode": "latest"},
        ]
        fake.rows["memo_position"] = []

        with mock.patch.object(memo_mod, "get_client", lambda: fake):
            board = memo_mod.get_board()
        sections = {s["tag"]["id"]: s for s in board["sections"]}
        self.assertNotIn(None, sections, "多标签记录不得被截断成未分类")
        self.assertEqual(len(sections[1]["items"]), 2500)
        self.assertEqual(len(sections[2]["items"]), 1)
        self.assertIn(2500, [item["id"] for item in sections[1]["items"]])

        with mock.patch.object(memo_mod, "get_client", lambda: fake):
            listed = memo_mod.list_entries("active")
        self.assertEqual(len(listed), 2500)

        with mock.patch.object(memo_mod, "get_client", lambda: fake):
            hits = memo_mod.list_entries("active", query="body2500")
        self.assertEqual([h["id"] for h in hits], [2500],
                         "分页前的单次读取会漏掉第 2500 条")

    def test_compound_key_tables_order_by_their_unique_keys(self):
        """BUG-01/R02 回归锚：memo_entry_tag / memo_position 是复合主键表，
        没有 id 列——分页排序必须落在真实唯一键上，而不是默认 id
        （真实 PostgreSQL 会以 42703 拒绝）。"""
        with mock.patch.object(memo, "get_client", lambda: self.fake):
            memo.get_board()
        order_by_table = {}
        for query in self.fake.queries:
            order_by_table.setdefault(query.table, query.orders)
        self.assertEqual(order_by_table["memo_entry_tag"],
                         [("entry_id", False), ("tag_id", False)])
        self.assertEqual(order_by_table["memo_position"],
                         [("group_id", False), ("entry_id", False)])
        # 详情页的关联读取同样不能按 id 排序
        self.fake.queries.clear()
        with mock.patch.object(memo, "get_client", lambda: self.fake):
            memo.get_entry(3)
        detail_tag_orders = [q.orders for q in self.fake.queries
                             if q.table == "memo_entry_tag"]
        self.assertTrue(detail_tag_orders)
        for orders in detail_tag_orders:
            self.assertIn(("entry_id", False), orders)

    def test_head_requests_use_read_semantics(self):
        """GET 路由自动接收 HEAD（F20）：带正文的 HEAD 不得进入写 RPC，
        无正文 HEAD 也不是 400。"""
        body_headers = {**self.auth, "Content-Type": "application/json"}
        self.http.head("/admin/api/memo/entries", headers=self.auth)
        self.http.request("HEAD", "/admin/api/memo/entries",
                          content=b'{"kind":"note","content":"x"}', headers=body_headers)
        self.http.request("HEAD", "/admin/api/memo/entries/3",
                          content=b'{"content":"x"}', headers=body_headers)
        self.http.request("HEAD", "/admin/api/memo/tags",
                          content=b'{"name":"x"}', headers=body_headers)
        write_rpcs = [fn for fn, _ in self.fake.rpc_calls]
        self.assertEqual(write_rpcs, [], "HEAD 不得触发任何写 RPC")
        plain = self.http.head("/admin/api/memo/entries", headers=self.auth)
        self.assertEqual(plain.status_code, 200)


if __name__ == "__main__":
    unittest.main()
