"""规划管理前端契约测试。

断言 planning.js 的关键区块 id、后端端点调用、无 Emoji；侧栏第一项为
「规划管理」且现有六页不变；铃声资产与 CREDITS.md 就位；ASSET_VERSION
版本链一致。quickjs 可用时额外做一次真实 ES 语法解析。

二轮验收修复（BUG-11~16）：四区域页签化 + 今日三分区二级页签、详情栏
提醒控件、状态显示名去撞车、一行式小空态、原生 date/time 控件统一为
复古选择器（lib/retro_time.js）、详情栏术语去工程味。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNING = ROOT / "admin" / "js" / "pages" / "planning.js"
RETRO_TIME = ROOT / "admin" / "js" / "lib" / "retro_time.js"
ROUTES = ROOT / "admin" / "js" / "routes.js"
UI = ROOT / "admin" / "js" / "ui.js"
STYLE = ROOT / "admin" / "css" / "style.css"
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

    def test_top_tabs_four_items_with_region_icons(self):
        # BUG-16：工具栏下方一条 .tabs，四个页签沿用原区域图标，同一时刻只显示激活区域
        match = re.search(r'<div class="tabs" id="planning-tabs"[^>]*>(.*?)</div>', self.page, re.S)
        self.assertIsNotNone(match, "planning page must render the top .tabs bar")
        block = match.group(1)
        for tab_key, icon_name in (
            ("today", "calendar"), ("all", "inbox"), ("goals", "star"), ("summary", "journal"),
        ):
            with self.subTest(tab=tab_key):
                self.assertIn(f'data-tab="{tab_key}"', block)
                self.assertIn(f"icon('{icon_name}')", block)
        self.assertEqual(block.count('class="tab'), 4)
        # 默认激活「当前待办」
        self.assertIn('<button class="tab active" data-act="plan-tab" data-tab="today"', block)

    def test_today_subtabs_three_sections_default_progress(self):
        # BUG-16c：「当前待办」页签内 .subtabs 二级页签，默认进度中，切换只显示对应列表
        match = re.search(r'<div class="subtabs">(.*?)</div>', self.page, re.S)
        self.assertIsNotNone(match, "today card must render the .subtabs bar")
        block = match.group(1)
        for section, label in (
            ("progress", "进度中"), ("attention", "待处理"), ("done", "已完成"),
        ):
            with self.subTest(section=section):
                self.assertIn(f'data-section="{section}"', block)
                self.assertIn(label, block)
        self.assertIn(
            '<button class="subtab active" data-act="plan-subtab" data-section="progress"',
            block,
        )
        # 分区列表初始只显示进度中；计数移入页签
        self.assertIn('id="planning-attention" hidden', self.page)
        self.assertIn('id="planning-done" hidden', self.page)
        self.assertNotIn('id="planning-progress" hidden', self.page)
        self.assertIn("activeSection: 'progress'", self.page)

    def test_tab_panels_keep_region_ids_and_default_to_today(self):
        # BUG-16d：区域 DOM 保留原 id，只包进页签面板；初始只显示当前待办
        self.assertIn('id="planning-today" data-panel="today">', self.page)
        self.assertIn('id="planning-all" data-panel="all" hidden', self.page)
        self.assertIn('id="planning-goals" data-panel="goals" hidden', self.page)
        self.assertIn('id="planning-summary" data-panel="summary" hidden', self.page)
        self.assertIn("activeTab: 'today'", self.page)
        # 重算等待横幅移入当前待办页签内（不在页签栏与全部待办之间）
        banner_at = self.page.index('id="planning-alarm-banner"')
        self.assertGreater(banner_at, self.page.index('id="planning-today"'))
        self.assertLess(banner_at, self.page.index('id="planning-all"'))

    def test_reorder_only_available_in_today_tab(self):
        # BUG-16e：排列模式只在「当前待办」页签可用，其它页签点击给 toast
        self.assertIn("this.activeTab !== 'today'", self.page)
        self.assertIn("调整顺序只在「当前待办」页签可用", self.page)
        # 排列中切走页签自动退出排列
        self.assertIn("排列模式只在「当前待办」页签内有效，切走即退出并还原列表", self.page)

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

    def test_detail_alarm_controls_patch_task(self):
        # BUG-11：详情栏「提醒」行可操作（闹钟勾选 + 计时器文本框 + 保存提醒），
        # 保存走 PATCH /admin/api/planning/tasks/{task_id}，留空传 null，成功后刷新详情与列表
        for marker in (
            "data-alarm-controls",
            'data-act="occ-save-alarm"',
            "data-alarm-start",
            "data-alarm-end",
            "data-timer-input",
            "保存提醒",
            "saveOccurrenceAlarm",
            "`/admin/api/planning/tasks/${taskId}`",
            "timer_minutes: timer || null",
            "alarm_start: host.querySelector('[data-alarm-start]').checked",
            "if (fresh) this.showOccurrenceDetail(fresh);",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)
        # 走 occ- 委托入口
        self.assertIn("else if (act === 'save-alarm') return this.saveOccurrenceAlarm(id);", self.page)

    def test_status_display_labels_renamed(self):
        # BUG-12：pending/in_progress 显示名与分区名撞车 →「未开始 / 执行中」
        self.assertIn("pending: { label: '未开始'", self.page)
        self.assertIn("in_progress: { label: '执行中'", self.page)
        self.assertNotIn("pending: { label: '待处理'", self.page)
        self.assertNotIn("in_progress: { label: '进行中'", self.page)

    def test_today_empty_states_are_compact(self):
        # BUG-13：三分区空态改为一行式小空态（小图标 + 纯文字，高度受限）
        self.assertNotIn("empty('今天还没有待办'", self.page)
        self.assertNotIn("empty('没有需要处理的异常待办'", self.page)
        self.assertNotIn("empty('今天还没有关闭的记录'", self.page)
        # 三个调用点（另有 1 处 function miniEmpty 定义不计）
        self.assertEqual(self.page.count("miniEmpty('"), 3)
        css = STYLE.read_text(encoding="utf-8")
        self.assertIn(".plan-empty-mini", css)
        self.assertIn("min-height: 34px", css)

    def test_no_native_date_or_time_inputs_use_retro_picker(self):
        # BUG-14：原生 time/date 控件统一为复古选择器（lib/retro_time.js）
        self.assertNotIn('type="time"', self.page)
        self.assertNotIn('type="date"', self.page)
        self.assertIn("lib/retro_time.js", self.page)
        self.assertIn("createRetroTimeField", self.page)
        # 时间模式 ×4（显式开始/结束、限时截止/范围），日期模式 ×2（筛选日期、单次目标日期）
        self.assertEqual(self.page.count('data-retro-mode="time"'), 4)
        self.assertEqual(self.page.count('data-retro-mode="date"'), 2)
        # 隐藏 input 保留原 id 契约，提交逻辑无需改动
        for field_id in (
            "pf-start-tod", "pf-end-tod", "pf-deadline", "pf-deadline-end",
            "pf-target-date", "planning-filter-date",
        ):
            with self.subTest(field=field_id):
                self.assertIn(f'data-retro-for="{field_id}"', self.page)
        # 复古选择器模块的三种模式齐备
        lib = RETRO_TIME.read_text(encoding="utf-8")
        for marker in ("'datetime'", "'date'", "'time'", "openRetroTimePop", "createRetroTimeField"):
            with self.subTest(marker=marker):
                self.assertIn(marker, lib)

    def test_detail_terms_are_plain_language(self):
        # BUG-15：「排列标签」→「当前状态」；生成游标 / 下次到期降为 muted 小字
        self.assertIn("<span class=\"k\">当前状态</span>", self.page)
        self.assertNotIn("排列标签", self.page)
        self.assertIn('<div class="kv muted text-sm"><span class="k">生成游标</span>', self.page)
        self.assertIn('<div class="kv muted text-sm"><span class="k">下次到期</span>', self.page)



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
        for path in (PLANNING, RETRO_TIME):
            src = path.read_text(encoding="utf-8")
            src = _re.sub(r"import\s[^;]*?;", "", src, flags=_re.S)
            src = src.replace("export default {", "const __page__ = {")
            src = _re.sub(r"\bexport\s+(?=(async\s+)?(function|const|let|class|var)\b)", "", src)
            check = quickjs.Context().eval(
                "(function(src){ try { new globalThis.Function(src)(); return 'ok'; }"
                " catch (e) { return e.name + ': ' + e.message; } })"
            )
            with self.subTest(file=path.name):
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
