import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


if "dotenv" not in sys.modules and importlib.util.find_spec("dotenv") is None:
    dotenv = types.ModuleType("dotenv")
    dotenv.load_dotenv = lambda: None
    sys.modules["dotenv"] = dotenv

from gateway.memory_search import (
    _hybrid_rank,
    _keyword_search,
    format_memories_for_injection,
    search_memories,
)


MODULE = "gateway.memory_search"
NOW = datetime(2026, 8, 2, tzinfo=timezone.utc)


def _memory(memory_id, content, *, heat=50, importance=5, created_at=None, **extra):
    return {
        "id": memory_id,
        "content": content,
        "title": extra.pop("title", None),
        "tags": extra.pop("tags", []),
        "heat": heat,
        "importance": importance,
        "created_at": created_at or NOW.isoformat(),
        **extra,
    }


class _MemoryQuery:
    def __init__(self, rows):
        self.rows = rows
        self.selected = None
        self.filters = []
        self.condition = None
        self.ordering = None
        self.limit_value = None

    def select(self, fields):
        self.selected = fields
        return self

    def eq(self, field, value):
        self.filters.append((field, value))
        return self

    def or_(self, condition):
        self.condition = condition
        return self

    def order(self, field, desc=False):
        self.ordering = (field, desc)
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        return SimpleNamespace(data=self.rows)


class _Client:
    def __init__(self, rows):
        self.query = _MemoryQuery(rows)
        self.table_name = None

    def table(self, name):
        self.table_name = name
        return self.query


class KeywordQueryTests(unittest.TestCase):
    def test_keyword_query_reads_only_verified_active_memories(self):
        client = _Client([_memory(1, "用户喜欢清晨散步")])
        with patch(f"{MODULE}.get_client", return_value=client):
            rows = _keyword_search(["清晨", "散步"], 30)

        self.assertEqual(len(rows), 1)
        self.assertEqual(client.table_name, "memories")
        self.assertIn("layer", client.query.selected)
        self.assertEqual(
            client.query.filters,
            [("is_active", True), ("verified", "verified")],
        )
        self.assertEqual(client.query.ordering, ("heat", True))
        self.assertEqual(client.query.limit_value, 30)


class HybridRankingTests(unittest.TestCase):
    def test_relevance_beats_unrelated_heat_and_freshness(self):
        old = (NOW - timedelta(days=120)).isoformat()
        exact = _memory(1, "用户喜欢清晨散步，也喜欢安静。", heat=10, importance=4, created_at=old)
        weak = _memory(2, "清晨偶尔会醒来。", heat=100, importance=10)

        ranked = _hybrid_rank([weak, exact], [], ["清晨", "散步"], 2, now=NOW)

        self.assertEqual([item["id"] for item in ranked], [1, 2])

    def test_vector_similarity_beats_high_heat_low_similarity(self):
        strong = _memory(1, "语义高度相关", heat=10, similarity=0.91)
        weak = _memory(2, "语义勉强相关", heat=100, importance=10, similarity=0.51)

        ranked = _hybrid_rank([], [weak, strong], [], 2, now=NOW)

        self.assertEqual([item["id"] for item in ranked], [1, 2])

    def test_agreement_between_channels_receives_a_bonus(self):
        both_keyword = _memory(1, "用户喜欢清晨散步", heat=40)
        vector_only = _memory(2, "另一个语义结果", heat=40, similarity=0.8)
        both_vector = dict(both_keyword, similarity=0.8)

        ranked = _hybrid_rank(
            [both_keyword],
            [vector_only, both_vector],
            ["清晨", "散步"],
            2,
            now=NOW,
        )

        self.assertEqual(ranked[0]["id"], 1)


class SearchFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_keyword_fallback_works_without_embedding_provider(self):
        keyword_rows = [
            _memory(1, "用户喜欢清晨散步", heat=55, importance=7, title="清晨偏好"),
            _memory(2, "用户喜欢散步", heat=20, importance=3, title="散步"),
        ]
        with (
            patch(f"{MODULE}._extract_keywords", return_value=["清晨", "散步"]),
            patch(f"{MODULE}._keyword_search", return_value=keyword_rows),
            patch(f"{MODULE}._get_embedding", new=AsyncMock(return_value=None)),
            patch(f"{MODULE}._vector_search_sync") as vector_search,
            patch(f"{MODULE}._boost_heat") as boost,
        ):
            result = await search_memories("还记得我喜欢什么时候散步吗？", top_k=1)

        vector_search.assert_not_called()
        boost.assert_called_once_with([1])
        self.assertEqual([item["id"] for item in result], [1])
        self.assertEqual(result[0]["inject_mode"], "full")
        self.assertFalse(any(key.startswith("_") for key in result[0]))

    async def test_empty_query_never_searches_or_boosts(self):
        with (
            patch(f"{MODULE}._keyword_search") as keyword_search,
            patch(f"{MODULE}._boost_heat") as boost,
        ):
            result = await search_memories("   ")

        self.assertEqual(result, [])
        keyword_search.assert_not_called()
        boost.assert_not_called()

    def test_formatter_only_injects_full_content_for_full_mode(self):
        text = format_memories_for_injection([
            {"content": "完整内容", "inject_mode": "full"},
            {"title": "只显示标题", "content": "不应注入的正文", "inject_mode": "title_only"},
        ])

        self.assertIn("完整内容", text)
        self.assertIn("(模糊) 只显示标题", text)
        self.assertNotIn("不应注入的正文", text)


if __name__ == "__main__":
    unittest.main()

