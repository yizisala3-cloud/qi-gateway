"""规划管理前端契约测试。

断言 planning.js 的关键区块 id、后端端点调用、无 Emoji；侧栏第一项为
「规划管理」且现有六页不变；铃声资产与 CREDITS.md 就位；ASSET_VERSION
版本链一致。quickjs 可用时额外做一次真实 ES 语法解析。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNING = ROOT / "admin" / "js" / "pages" / "planning.js"
ROUTES = ROOT / "admin" / "js" / "routes.js"
UI = ROOT / "admin" / "js" / "ui.js"
CREDITS = ROOT / "admin" / "assets" / "audio" / "CREDITS.md"

EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F]"
)

LEGACY_NAV_KEYS = ("memories", "digest", "emotion", "persona", "config", "logs")


def _try_import_quickjs():
    try:
        import quickjs  # noqa: F401
    except ImportError:
        return None
    return quickjs


class PlanningPageContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = PLANNING.read_text(encoding="utf-8")

    def test_four_regions_exist(self):
        for region_id in (
            "planning-today", "planning-all", "planning-goals", "planning-summary",
        ):
            with self.subTest(region=region_id):
                self.assertIn(f'id="{region_id}"', self.page)

    def test_today_board_has_three_sections(self):
        for section_id in (
            "planning-progress", "planning-attention", "planning-done",
            "planning-progress-count", "planning-attention-count", "planning-done-count",
        ):
            with self.subTest(section=section_id):
                self.assertIn(f'id="{section_id}"', self.page)

    def test_all_section_has_filters_and_two_lists(self):
        for marker in (
            "planning-filter-type", "planning-filter-status", "planning-filter-date",
            "planning-tasks", "planning-occurrences",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def test_placeholder_regions_are_marked_not_available(self):
        # 长期目标 = 二期占位空状态；每日总结 = 三按钮禁用 + 暂未接入。
        self.assertIn("二期接入", self.page)
        self.assertIn("三期接入", self.page)
        self.assertIn("暂未接入", self.page)
        self.assertEqual(self.page.count('<button class="btn btn-secondary" disabled>'), 3)

    def test_page_calls_planning_endpoints(self):
        for call in (
            "gw('/admin/api/planning/today')",
            "gw('/admin/api/planning/tasks?include_inactive=true')",
            "/admin/api/planning/occurrences",
            "gw('/admin/api/planning/recompute', { method: 'POST' })",
            "gw('/admin/api/planning/reorder'",
        ):
            with self.subTest(call=call):
                self.assertIn(call, self.page)

    def test_page_calls_occurrence_action_endpoints(self):
        for marker in (
            "post('/start')",
            "post('/finish')",
            "post('/status'",
            "`/admin/api/planning/occurrences/${id}/split`",
            "`/admin/api/planning/tasks/${id}/complete-early`",
            "`/admin/api/planning/tasks/${task.id}`",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def test_six_statuses_are_handled(self):
        for status in (
            "pending", "in_progress", "completed", "partial",
            "deferred", "discarded_this", "discarded", "timeout",
        ):
            with self.subTest(status=status):
                self.assertIn(f"'{status}'", self.page)

    def test_reorder_mode_with_confirm_and_cancel(self):
        self.assertIn("enter-reorder", self.page)
        self.assertIn("confirm-reorder", self.page)
        self.assertIn("cancel-reorder", self.page)
        self.assertIn("is-dragging", self.page)
        self.assertIn("setPointerCapture", self.page)

    def test_browser_alarm_and_timer_implementation(self):
        self.assertIn("Notification", self.page)
        self.assertIn("/admin/assets/audio/alarm-clock.mp3", self.page)
        self.assertIn("/admin/assets/audio/timer-done.ogg", self.page)
        self.assertIn("loop", self.page)
        self.assertIn("只在页面内响铃", self.page)
        self.assertIn("30 * 1000", self.page)

    def test_backfill_can_clear_actual_times(self):
        # BUG-6：补填弹窗始终提交两个字段，留空 → null 即清除
        self.assertIn("留空即清除该时间", self.page)
        self.assertNotIn("请至少填写一个时间", self.page)

    def test_split_parts_have_individual_durations(self):
        # BUG-7：每部分有独立耗时输入，支持简写，不再写死 30m
        self.assertIn("data-part-minutes", self.page)
        self.assertIn("data-part-row", self.page)

    def test_partial_status_offers_complete_action(self):
        # BUG-8：部分完成条目可直接改「已完成」
        partial_block = self.page.split("occ.status === 'partial'")[1]
        self.assertIn("btn('finish', '已完成'", partial_block)

    def test_audio_unlock_on_first_pointerdown(self):
        # BUG-9：首次手势静音解锁音频；播放被拦时给出提示
        self.assertIn("unlockAudio", self.page)
        self.assertIn("pointerdown", self.page)
        self.assertIn("浏览器拦截了自动响铃，点一下页面即可恢复", self.page)

    def test_reorder_conflict_recovers_gracefully(self):
        # BUG-10：排列期间列表变化导致确认被拒时，自动刷新并退出排列
        self.assertIn("order must include every open occurrence", self.page)
        self.assertIn("待办列表有变化，请重新进入排列", self.page)

    def test_manual_recompute_button(self):
        self.assertIn("重新计算时间", self.page)
        self.assertIn("等待自动重算", self.page)

    def test_no_emoji_icons(self):
        self.assertIsNone(EMOJI_PATTERN.search(self.page))



class PlanningNavigationContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = ROUTES.read_text(encoding="utf-8")
        cls.ui = UI.read_text(encoding="utf-8")
        cls.page = PLANNING.read_text(encoding="utf-8")

    def test_planning_is_first_nav_item(self):
        first_item = re.search(r"items:\s*\[\s*\{([^}]*)\}", self.routes).group(1)
        self.assertIn("key: 'planning'", first_item)
        self.assertIn("规划管理", first_item)

    def test_legacy_six_pages_are_unchanged(self):
        for key in LEGACY_NAV_KEYS:
            with self.subTest(key=key):
                self.assertIn(f"key: '{key}'", self.routes)

    def test_planning_icon_is_inline_svg(self):
        self.assertIn("calendar:", self.ui)

    def test_asset_version_chain_is_consistent(self):
        version = re.search(r"ASSET_VERSION = '([^']+)'", self.ui).group(1)
        version_refs = re.findall(r"\?v=([0-9a-z-]+)", self.routes + self.page)
        # routes.js 不带版本串；planning.js 的 import 版本必须与 ui.js 一致
        self.assertTrue(version_refs, "planning.js should pin import versions")
        for ref in version_refs:
            self.assertEqual(ref, version)
        index_html = (ROOT / "admin" / "index.html").read_text(encoding="utf-8")
        self.assertIn(f"?v={version}", index_html)
        app_js = (ROOT / "admin" / "js" / "app.js").read_text(encoding="utf-8")
        self.assertIn(f"'{version}'", app_js)

    def test_js_syntax_is_parseable(self):
        quickjs = _try_import_quickjs()
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        import re as _re
        src = self.page
        src = _re.sub(r"import\s[^;]*?;", "", src, flags=_re.S)
        src = src.replace("export default {", "const __page__ = {")
        src = _re.sub(r"\bexport\s+(?=(async\s+)?(function|const|let|class|var)\b)", "", src)
        check = quickjs.Context().eval(
            "(function(src){ try { new globalThis.Function(src)(); return 'ok'; }"
            " catch (e) { return e.name + ': ' + e.message; } })"
        )
        self.assertEqual(check(src), "ok")


class PlanningAudioAssetTests(unittest.TestCase):
    def test_audio_files_and_credits_exist(self):
        audio_dir = ROOT / "admin" / "assets" / "audio"
        self.assertTrue((audio_dir / "alarm-clock.mp3").is_file())
        self.assertTrue((audio_dir / "timer-done.ogg").is_file())
        credits = CREDITS.read_text(encoding="utf-8")
        self.assertIn("CC0", credits)
        self.assertIn("Calm Piano 1", credits)
        self.assertIn("Slow Piano Intermission", credits)
        self.assertIn("opengameart.org", credits)


if __name__ == "__main__":
    unittest.main()
