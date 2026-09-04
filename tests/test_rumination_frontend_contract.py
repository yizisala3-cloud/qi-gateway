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


if __name__ == "__main__":
    unittest.main()
