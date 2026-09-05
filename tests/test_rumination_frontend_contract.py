"""Frontend contract tests for the rumination section of the digest page.

The rumination lane lives inside the existing "记忆总结" page (no new top-level
navigation), reuses the page's status/run-card components, and exposes manual
execution plus per-run operation details.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIGEST = ROOT / "admin" / "js" / "pages" / "digest.js"

EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F]"
)


class RuminationFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = DIGEST.read_text(encoding="utf-8")

    def test_rumination_section_lives_on_the_existing_digest_page(self):
        self.assertIn("反刍连续感", self.page)
        self.assertIn("id=\"rumination-status\"", self.page)
        self.assertIn("id=\"rumination-runs\"", self.page)
        self.assertIn("id=\"rumination-overview\"", self.page)

    def test_status_and_runs_use_the_purpose_built_endpoints(self):
        self.assertIn("gw('/admin/api/memory-rumination/status')", self.page)
        self.assertIn("gw('/admin/api/memory-rumination/execute', { method: 'POST' })", self.page)

    def test_manual_execute_respects_batch_thresholds(self):
        self.assertIn("backlog < 60", self.page)
        self.assertIn("仍要执行吗", self.page)

    def test_run_cards_show_op_counters(self):
        for counter in (
            "created_threads", "adopted_threads", "updated_versions",
            "evidence_only", "resolved", "created_memories", "created_requests",
            "skipped_duplicates",
        ):
            with self.subTest(counter=counter):
                self.assertIn(counter, self.page)

    def test_no_emoji_icons(self):
        self.assertIsNone(EMOJI_PATTERN.search(self.page))

    def test_no_chat_or_memory_content_is_rendered_from_runs(self):
        # Run details render op metadata only (ids, reasons, counters) and
        # never memory/request bodies.
        self.assertNotIn("item.content", self.page)
        self.assertNotIn("run.preview_memories.map((item) => item.content", self.page)


class MemoryBrowserAbsorptionContractTests(unittest.TestCase):
    """审核界面必须清楚展示反刍申请的交接影响范围。"""

    @classmethod
    def setUpClass(cls):
        cls.browser = (ROOT / "admin" / "js" / "pages" / "_memory_browser.js").read_text(
            encoding="utf-8"
        )

    def test_request_fields_include_lane_and_absorption(self):
        self.assertIn("producer_path", self.browser)
        self.assertIn("absorbed_fast_path_memory_ids", self.browser)

    def test_request_detail_shows_rumination_lane(self):
        self.assertIn("反刍路径申请", self.browser)

    def test_absorb_targets_are_rendered_for_review(self):
        self.assertIn("拟交接目标", self.browser)
        self.assertIn("通过后不会停用其他正式记忆", self.browser)
        self.assertIn("absorbed_fast_path_memory_ids", self.browser)
        # 详情与通过表单都水合影响范围。
        self.assertIn("hydrateAbsorbTargets", self.browser)
        self.assertIn("hydrateAbsorbImpact", self.browser)
        self.assertIn("通过后停用", self.browser)

    def test_target_changes_surface_before_submit(self):
        # 影响范围提示改由纯函数视图标记，不再使用固定字符串拼接。
        self.assertIn("absorbImpactViews", self.browser)
        self.assertIn("view.changed", self.browser)
        self.assertIn("本次通过会被拒绝", self.browser)
        self.assertIn("absorbImpactSummary", self.browser)

    def test_no_emoji_icons(self):
        self.assertIsNone(EMOJI_PATTERN.search(self.browser))


if __name__ == "__main__":
    unittest.main()
