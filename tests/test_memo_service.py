"""备忘录服务层单元测试（纯函数部分）。

覆盖需求验收（§8）中由服务层组装承担的语义：
- 展示字段派生：标题可空、正文首行兜底、一行正文预览（§2.3/M07）；
- 首页板块：常驻优先、空位补随笔、每板块合计 ≤5 条（M05/M06）；
- 常驻 ≥5 条时不再补随笔（§3.2）；完整 items 保留第 6 条及之后（§8.5/§8.7）；
- 随笔两种模式：latest 创建时间倒序 / manual 按 position（M10/§4.3）；
- 未分类板块（§3.1）；空标签不出板块；
- 搜索：标题命中优先、正文其次、一条记录只出现一次、匹配片段（§5/M11）。
"""

import unittest

from gateway.memo import build_board, display_fields, search_entries


def entry(id, *, kind="note", title=None, content="正文", created="2026-10-01T09:00:00+08:00",
          status="active", updated=None):
    return {
        "id": id, "kind": kind, "title": title, "content": content,
        "status": status, "created_at": created, "updated_at": updated or created,
    }


class DisplayFieldsTests(unittest.TestCase):
    def test_with_title_uses_first_line_as_excerpt(self):
        fields = display_fields("购物清单", "牛奶\n鸡蛋\n面包")
        self.assertEqual(fields["display_title"], "购物清单")
        self.assertEqual(fields["body_excerpt"], "牛奶")

    def test_without_title_falls_back_to_first_line_and_excerpt_is_second(self):
        fields = display_fields(None, "灵感：写一首诗\n关于海雾的第二行")
        self.assertEqual(fields["display_title"], "灵感：写一首诗")
        self.assertEqual(fields["body_excerpt"], "关于海雾的第二行")

    def test_untitled_single_line_has_empty_excerpt(self):
        fields = display_fields(None, "只有一行")
        self.assertEqual(fields["display_title"], "只有一行")
        self.assertEqual(fields["body_excerpt"], "")

    def test_whitespace_collapsed_and_truncated(self):
        fields = display_fields("  多   空格  ", "a" * 200)
        self.assertEqual(fields["display_title"], "多 空格")
        self.assertEqual(len(fields["body_excerpt"]), 121)  # 120 + 省略号


class BuildBoardTests(unittest.TestCase):
    def setUp(self):
        self.tags = [
            {"id": 1, "name": "购物", "position": 1},
            {"id": 2, "name": "灵感", "position": 2},
        ]
        self.groups = [
            {"id": 11, "tag_id": 1, "note_sort_mode": "latest"},
            {"id": 12, "tag_id": 2, "note_sort_mode": "manual"},
            {"id": 13, "tag_id": None, "note_sort_mode": "latest"},
        ]

    def test_pinned_first_notes_fill_remaining_slots_combined_five(self):
        entries = [
            entry(1, kind="pinned", content="常驻一"),
            entry(2, kind="pinned", content="常驻二"),
            entry(3, content="随笔一", created="2026-10-01T09:00:00+08:00"),
            entry(4, content="随笔二", created="2026-10-02T09:00:00+08:00"),
            entry(5, content="随笔三", created="2026-10-03T09:00:00+08:00"),
            entry(6, content="随笔四", created="2026-10-04T09:00:00+08:00"),
        ]
        board = build_board(
            tags=[self.tags[0]], groups=[self.groups[0]], entries=entries,
            entry_tags=[{"entry_id": i, "tag_id": 1} for i in range(1, 7)],
            positions=[],
        )
        self.assertEqual(len(board["sections"]), 1)
        section = board["sections"][0]
        preview_ids = [item["id"] for item in section["preview"]]
        # 常驻优先；剩余位置补最新模式的随笔（创建时间倒序）；合计 5 条
        self.assertEqual(preview_ids, [1, 2, 6, 5, 4])
        # 预览是展示限制：完整 items 保留第 6 条（随笔三，最旧）
        self.assertEqual(len(section["items"]), 6)
        self.assertEqual(section["items"][-1]["id"], 3)
        self.assertEqual(section["pinned_order"], [1, 2])
        self.assertEqual(section["note_order"], [6, 5, 4, 3])

    def test_five_pinned_leaves_no_room_for_notes(self):
        entries = [entry(i, kind="pinned", content=f"常驻{i}") for i in range(1, 7)]
        entries += [entry(10, content="随笔", created="2026-10-05T09:00:00+08:00")]
        board = build_board(
            tags=[self.tags[0]], groups=[self.groups[0]], entries=entries,
            entry_tags=[{"entry_id": e["id"], "tag_id": 1} for e in entries],
            positions=[{"group_id": 11, "entry_id": e["id"], "position": e["id"]} for e in entries],
        )
        section = board["sections"][0]
        self.assertEqual([item["id"] for item in section["preview"]], [1, 2, 3, 4, 5])
        self.assertEqual(len(section["items"]), 7)   # 第 6 条常驻与随笔都在完整列表
        self.assertEqual(section["note_order"], [10])

    def test_manual_mode_orders_notes_by_position(self):
        entries = [
            entry(1, content="新随笔", created="2026-10-05T09:00:00+08:00"),
            entry(2, content="旧随笔", created="2026-10-01T09:00:00+08:00"),
            entry(3, content="中随笔", created="2026-10-03T09:00:00+08:00"),
        ]
        board = build_board(
            tags=[self.tags[1]], groups=[self.groups[1]], entries=entries,
            entry_tags=[{"entry_id": i, "tag_id": 2} for i in (1, 2, 3)],
            positions=[
                {"group_id": 12, "entry_id": 2, "position": 1},
                {"group_id": 12, "entry_id": 3, "position": 2},
            ],
        )
        section = board["sections"][0]
        # 手动模式：已定位的按 position；未定位（手动模式期间新增）补末尾
        self.assertEqual([item["id"] for item in section["items"]], [2, 3, 1])

    def test_untagged_section_last_and_only_when_nonempty(self):
        entries = [
            entry(1, kind="pinned", content="购物常驻"),
            entry(9, content="无标签内容"),
        ]
        board = build_board(
            tags=self.tags, groups=self.groups, entries=entries,
            entry_tags=[{"entry_id": 1, "tag_id": 1}],
            positions=[],
        )
        self.assertEqual([s["tag"] for s in board["sections"]], [self.tags[0], None])
        self.assertEqual(board["sections"][1]["preview"][0]["id"], 9)

    def test_empty_tag_not_shown_as_section(self):
        entries = [entry(1, content="随笔", created="2026-10-01T09:00:00+08:00")]
        board = build_board(
            tags=self.tags, groups=self.groups, entries=entries,
            entry_tags=[{"entry_id": 1, "tag_id": 2}],
            positions=[],
        )
        self.assertEqual([s["tag"]["id"] for s in board["sections"]], [2])

    def test_archived_and_deleted_entries_excluded(self):
        entries = [
            entry(1, content="正常"),
            entry(2, content="已归档", status="archived"),
            entry(3, content="已删除", status="deleted"),
        ]
        board = build_board(
            tags=[], groups=[self.groups[2]], entries=entries,
            entry_tags=[], positions=[],
        )
        self.assertEqual(len(board["sections"][0]["items"]), 1)

    def test_section_orders_carry_full_ids_for_reorder(self):
        entries = [entry(i, kind="pinned", content=f"p{i}") for i in range(1, 8)]
        board = build_board(
            tags=[self.tags[0]], groups=[self.groups[0]], entries=entries,
            entry_tags=[{"entry_id": i, "tag_id": 1} for i in range(1, 8)],
            positions=[{"group_id": 11, "entry_id": i, "position": i} for i in range(1, 8)],
        )
        section = board["sections"][0]
        self.assertEqual(section["pinned_order"], [1, 2, 3, 4, 5, 6, 7])


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry(1, title="购物清单", content="买牛奶和鸡蛋"),
            entry(2, title=None, content="灵感：购物也可以是灵感的来源之一"),
            entry(3, title="读书", content="《雾海》第三章"),
        ]
        self.tags_map = {1: [1], 2: [1, 2], 3: []}
        self.tag_by_id = {1: {"id": 1, "name": "购物"}, 2: {"id": 2, "name": "灵感"}}

    def test_title_hits_rank_before_content_hits(self):
        results = search_entries(self.entries, self.tags_map, self.tag_by_id, "购物")
        self.assertEqual([r["id"] for r in results], [1, 2])   # 标题命中优先
        # 标题命中但正文无命中：片段回退正文首行，便于识别记录
        self.assertEqual(results[0]["match_fragment"], "买牛奶和鸡蛋")

    def test_entry_with_multiple_tags_appears_once_with_all_tags(self):
        results = search_entries(self.entries, self.tags_map, self.tag_by_id, "灵感")
        self.assertEqual(len(results), 1)
        self.assertEqual([t["name"] for t in results[0]["tags"]], ["购物", "灵感"])
        self.assertIn("灵感", results[0]["match_fragment"])

    def test_fragment_windows_around_match(self):
        long_content = "前" * 60 + "关键词" + "后" * 60
        results = search_entries(
            [entry(5, content=long_content)], {}, {}, "关键词")
        fragment = results[0]["match_fragment"]
        self.assertTrue(fragment.startswith("…"))
        self.assertTrue(fragment.endswith("…"))
        self.assertIn("关键词", fragment)

    def test_case_insensitive_and_no_match_empty(self):
        results = search_entries(
            [entry(6, title="TODO List", content="buy milk")], {}, {}, "todo")
        self.assertEqual(results[0]["id"], 6)
        self.assertEqual(search_entries(self.entries, self.tags_map, self.tag_by_id, "不存在的字"), [])

    def test_fragment_maps_casefold_expansion_back_to_original(self):
        # casefold 改变字符数量（ß→ss）：折叠文本中的命中下标不能直接切
        # 原文（F19）——片段必须覆盖原文实际命中位置
        content = "ß" * 100 + "NEEDLE" + "X" * 150
        results = search_entries([entry(7, content=content)], {}, {}, "needle")
        self.assertEqual(len(results), 1)
        fragment = results[0]["match_fragment"]
        self.assertIn("NEEDLE", fragment)
        self.assertTrue(fragment.startswith("…"))
        self.assertTrue(fragment.endswith("…"))

    def test_fragment_multibyte_and_newline_window(self):
        content = "中" * 60 + "\n隐藏的词\n" + "末" * 60
        results = search_entries([entry(8, content=content)], {}, {}, "隐藏的词")
        fragment = results[0]["match_fragment"]
        self.assertIn("隐藏的词", fragment)
        self.assertNotIn("\n", fragment)   # 换行折叠为空格


if __name__ == "__main__":
    unittest.main()
