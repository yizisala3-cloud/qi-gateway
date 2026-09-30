"""规划管理前端契约测试。

断言 planning.js 的关键区块 id、后端端点调用、无 Emoji；侧栏第一项为
「规划管理」且现有六页不变；铃声资产与 CREDITS.md 就位；ASSET_VERSION
版本链一致。quickjs 可用时额外做一次真实 ES 语法解析。

二轮验收修复（BUG-11~16）：四区域页签化 + 今日三分区二级页签、详情栏
提醒控件、状态显示名去撞车、一行式小空态、原生 date/time 控件统一为
复古选择器（lib/retro_time.js）、详情栏术语去工程味。

2026-10-01 视觉批次：周期设置弹窗自 planning.js 抽出至 lib/cycle_settings.js
（配置页「规划周期」卡片承载入口）；可安排时段语义提示按 user 指示删除、
（可选）随字段标签；时/分下拉复古化（lib/retro_time.js 内 rtp-unit）。
"""

import json
import re
import unittest

from gateway import planning
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNING = ROOT / "admin" / "js" / "pages" / "planning.js"
RETRO_TIME = ROOT / "admin" / "js" / "lib" / "retro_time.js"
RETRO_SELECT = ROOT / "admin" / "js" / "lib" / "retro_select.js"
CYCLE_SETTINGS = ROOT / "admin" / "js" / "lib" / "cycle_settings.js"
CONFIG_PAGE = ROOT / "admin" / "js" / "pages" / "config.js"
ROUTES = ROOT / "admin" / "js" / "routes.js"
UI = ROOT / "admin" / "js" / "ui.js"
STYLE = ROOT / "admin" / "css" / "style.css"
CREDITS = ROOT / "admin" / "assets" / "audio" / "CREDITS.md"

EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F]"
)

LEGACY_NAV_KEYS = ("memories", "digest", "emotion", "persona", "config", "logs")


def _task_form_source(page):
    """提取 openTaskForm 函数体（openCycleSettings 也有同名 submitBtn 逻辑，
    全局搜索会误取；表单区契约均以本函数圈定区域）。"""
    match = re.search(r"  openTaskForm\(task\) \{(.*?)\n  \},", page, re.S)
    assert match is not None, "openTaskForm must exist"
    return match.group(1)


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

    def test_partial_status_offers_full_complete_action(self):
        # Phase 1R：部分完成保持开放，详情提供「已全部完成」收口
        self.assertIn("btn('finish', '已全部完成'", self.page)
        # partial 属于开放状态分组，与后端 OPEN_STATUSES 一致
        partial_block = self.page.split("const OPEN_STATUSES = ")[1].split(";")[0]
        self.assertIn("'partial'", partial_block)

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
        # BUG-14 + 批次 8：原生 time/date 控件统一为复古选择器。
        # 2026-10-01 起：周期设置刷新时间与 boundary 冲突调整项已随弹窗
        # 移至 lib/cycle_settings.js（配置页承载入口），planning 仅剩
        # 可安排时段双端（time ×2）与筛选/目标日期（date ×2）。
        self.assertNotIn('type="time"', self.page)
        self.assertNotIn('type="date"', self.page)
        self.assertIn("lib/retro_time.js", self.page)
        self.assertIn("createRetroTimeField", self.page)
        self.assertEqual(self.page.count('data-retro-mode="time"'), 2)
        self.assertEqual(self.page.count('data-retro-mode="date"'), 2)
        for field_id in (
            "pf-window-start", "pf-window-end",
            "pf-target-date", "planning-filter-date",
        ):
            with self.subTest(field=field_id):
                self.assertIn(f'data-retro-for="{field_id}', self.page)
        # 可安排时段双端右对齐弹层（时钟图标一侧）
        self.assertEqual(self.page.count('data-retro-align="right"'), 2)
        # 周期设置弹窗（lib）：boundary + 冲突调整项双端
        cycle_lib = CYCLE_SETTINGS.read_text(encoding="utf-8")
        self.assertEqual(cycle_lib.count('data-retro-mode="time"'), 3)
        for field_id in (
            "pf-cycle-boundary", "pf-adj-start-${c.task_id}", "pf-adj-end-${c.task_id}",
        ):
            with self.subTest(cycle_field=field_id):
                self.assertIn(f'data-retro-for="{field_id}', cycle_lib)
        # 配置页卡片表面的周期时间字段
        config = CONFIG_PAGE.read_text(encoding="utf-8")
        self.assertIn('data-retro-for="cfg-cycle-boundary"', config)
        # 复古选择器模块的三种模式齐备
        lib = RETRO_TIME.read_text(encoding="utf-8")
        for marker in ("'datetime'", "'date'", "'time'", "openRetroTimePop", "createRetroTimeField"):
            with self.subTest(marker=marker):
                self.assertIn(marker, lib)
        # 批次 9 HIGH #1：真实 PostgREST time 列形状 HH:MM:SS——time 模式的
        # 显示与弹层初始态都必须按同值接受（否则编辑已有窗口回退为空/当前时刻）
        self.assertIn(r"(\d{2}):(\d{2})(?::\d{2})?", lib)

    def test_detail_terms_are_plain_language(self):
        # BUG-15：「排列标签」→「当前状态」；生成游标 / 下次到期降为 muted 小字
        self.assertIn("<span class=\"k\">当前状态</span>", self.page)
        self.assertNotIn("排列标签", self.page)
        self.assertIn('<div class="kv muted text-sm"><span class="k">生成游标</span>', self.page)
        self.assertIn('<div class="kv muted text-sm"><span class="k">下次到期</span>', self.page)

    def test_split_dialog_daily_path_contract(self):
        # 日常路径收尾：拆分弹窗默认 1 项、添加/移除、上限 10、防双击、
        # 提交 1 项合法；文案不再出现「至少填写两部分」。
        for marker in (
            "partRow(1)",                       # 默认只渲染 1 个输入区域
            "data-add-part",
            "data-remove-part",
            "rows.length >= 10 ? 'none' : ''",  # 达到 10 个隐藏添加按钮
            "').length <= 1) return;",          # 至少保留 1 项，不允许删到 0
            "请至少填写一个待办内容",
            "if (submit.disabled) return;",     # 防双击：请求期间禁用提交
            "submit.disabled = true;",
            "submit.disabled = false;",         # 失败恢复按钮
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)
        self.assertNotIn("至少填写两部分", self.page)

    def test_adjust_window_sends_both_ends_with_clear_semantics(self):
        # 批次 8：详情栏「编辑时间」→「调整时段」——编辑当前实例冻结窗口
        #（最早开始 / 最晚完成），双端显式提交（null = 清除该端）；
        # 422/409 门控拒绝在字段附近中文呈现且保持可继续编辑
        for marker in (
            "title: '调整时段',",
            "window_start_at: start ? new Date(start).toISOString() : null,",
            "window_end_at: end ? new Date(end).toISOString() : null,",
            "pf-adj-error",
            "这一轮已带时段约束：两端都清空会取消既有约束，后端会拒绝；请保留至少一端。",
            "把时段收窄到恰好容纳预计耗时，就会把这条待办钉在该时间",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)
        # 旧 est 手动编辑入口退役
        self.assertNotIn("title: '编辑预估时间',", self.page)
        self.assertNotIn("planning-edit-start", self.page)

    def test_hollow_task_hides_early_complete_button(self):
        # 中空待办提前完成必然 409：按任务形态隐藏不适用入口
        self.assertIn("task.is_active && !task.is_hollow", self.page)

    def test_reorder_toast_respects_auto_recompute_switch(self):
        # 自动重算关闭时不得提示「等待自动重算」（需求 16.3）
        self.assertIn("this.board?.recompute?.enabled === false", self.page)
        self.assertIn("自动重算已关闭", self.page)

    def test_task_form_sends_window_clear_values(self):
        # 批次 8：可安排时段取代显式起止 / 限时 / 固定开关。编辑模式显式
        # 发送双端清除值（null = 清除该端，不发送=没清除）；创建模式只
        # 提交已填端（四种窗口组合都可表达、不强制成对填写）。
        for marker in (
            "body.window_start_tod = windowStart || null;",
            "body.window_end_tod = windowEnd || null;",
            "if (windowStart) body.window_start_tod = windowStart;",
            "if (windowEnd) body.window_end_tod = windowEnd;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def _task_form_source(self):
        match = re.search(r"  openTaskForm\(task\) \{(.*?)\n  \},", self.page, re.S)
        self.assertIsNotNone(match, "openTaskForm must exist")
        return match.group(1)

    def test_task_form_stops_submitting_legacy_fields(self):
        # C13：前端停止提交 est_start_tod / est_end_tod / deadline_tod /
        # deadline_end_tod / 任务级 is_fixed / time_mode 切换；闹钟/计时器
        # 移出新建表单（详情栏承载，能力不丢）
        form = self._task_form_source()
        for legacy in (
            "est_start_tod", "est_end_tod", "deadline_tod", "deadline_end_tod",
            "pf-fixed", "is_fixed", "time_mode", "pf-alarm-start", "pf-alarm-end",
            "pf-timer", "alarm_start", "timer_minutes",
        ):
            with self.subTest(legacy=legacy):
                self.assertNotIn(legacy, form)

    def test_task_form_window_four_combos_and_hint(self):
        # C2（2026-10-01 更新）：四种窗口组合仍可表达（payload 契约见
        # test_task_form_sends_window_clear_values）；可安排时段双端改为
        # 全宽复古时间字段，「（可选）」随字段标签；原整段语义提示按
        # user 指示删除，不得回归。
        form = self._task_form_source()
        for marker in (
            "可安排时段", "最早开始（可选）", "最晚完成（可选）",
            'data-retro-for="pf-window-start"', 'data-retro-for="pf-window-end"',
            'data-retro-align="right"',
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, form)
        for gone in (
            "可安排时段（可选）", "两端可独立留空",
            "系统在时段内寻找能完整容纳耗时的连续空闲块",
        ):
            with self.subTest(gone=gone):
                self.assertNotIn(gone, form)

    def test_task_form_inline_chinese_errors(self):
        # C3：保存失败在字段附近以中文呈现（时段区 / 目标日期区），保留
        # user 已填写内容，不误报为必填错误
        form = self._task_form_source()
        for marker in (
            "pf-window-error", "pf-once-error", "errorBlock(esc(message))",
            "message.includes('目标日期')", "message.includes('单次待办已生成')",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, form)

    def test_once_generated_locks_task_identity_ui(self):
        # C4 / §28.3：已生成 once 的任务日期与未来窗口模板禁用并提示走
        # 当前实例调整（「调整时段」）；后端 400 仍是权威兜底。
        # 批次 9 UI #1 修复：锁定判定以后端 has_generated_occurrence 为权威
        # （不依赖 occurrences 列表加载状态）；复古选择器 input + 按钮
        # 一起禁用（只禁 input 拦不住按钮弹层改值）；提示挂在
        # [data-type-block="once"]；提交侧兜底强制回传任务现值。
        form = self._task_form_source()
        for marker in (
            "onceLocked",
            "task.has_generated_occurrence",
            "o.task_id === task.id",
            "该单次待办已生成当前实例",
            "调整时段",
            "input.disabled = true;",
            "button.disabled = true;",
            '[data-type-block="once"]',
            "body.target_date = task.target_date ?? null;",
            "body.window_start_tod = task.window_start_tod ?? null;",
            "body.window_end_tod = task.window_end_tod ?? null;",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, form)

    def test_boundary_adjustments_keep_merged_state(self):
        # 批次 9 UI #2 修复：多项冲突逐个修正时，已修正者从下一次 dry-run
        # 响应消失、重绘移除其 DOM 行——调整必须留在稳定 task_id 集合中，
        # 最终 submit 携带全部调整（不能只剩最后一个）。
        # 2026-10-01：实现随周期设置弹窗移至 lib/cycle_settings.js。
        lib = CYCLE_SETTINGS.read_text(encoding="utf-8")
        for marker in (
            "mergeBoundaryAdjustments(adjustmentsState, collected)",
            "rememberedAdjustment(adjustmentsState, taskId)",
            "const remembered = (taskId) =>",
            "(prev && prev.window_start_tod) || c.window_start_tod",
            "(prev && prev.window_end_tod) || c.window_end_tod",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, lib)

    def test_cycle_settings_two_phase_boundary_flow(self):
        # C12 / §5.2.2：boundary 修改先 dry-run（零写入）→ 冲突在同一弹窗
        # 内列出并就地调整（校验按新 boundary）→ 一次原子保存（携带
        # task_adjustments）；下一周期生效提示；取消 = 不点保存即零写入。
        # 2026-10-01：弹窗实现移至 lib/cycle_settings.js；配置页卡片表面
        # 直发 boundary 修改（同样先 dry-run），命中冲突时以 initialBoundary
        # 预填弹窗接续调整。
        lib = CYCLE_SETTINGS.read_text(encoding="utf-8")
        for marker in (
            "dry_run: true",
            "task_adjustments: collectAdjustments()",
            "task_adjustments: adjustments",
            "showConflictList",
            "跨越新刷新时间",
            "新的刷新时间从下一规划周期开始生效，当前周期保持不变",
            "pf-boundary-conflicts",
            "pf-cycle-error",
            "pf-adj-start-${c.task_id}",
            "initialBoundary",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, lib)
        config = CONFIG_PAGE.read_text(encoding="utf-8")
        for marker in (
            "refresh_boundary_time: boundary, dry_run: true",
            "refresh_boundary_time: boundary }",
            "openCycleSettings(",
            "initialBoundary: boundary",
        ):
            with self.subTest(config_marker=marker):
                self.assertIn(marker, config)

    def test_manual_recompute_shows_conflict_list(self):
        # C6/C7：手动重算冲突以弹窗呈现（待办 / 约束 / 原因三要素）+ 更新数量反馈
        for marker in (
            "showConflictsModal",
            "排程冲突",
            "本轮重算整体未保存",
            "已重新计算，更新了 ${result.updated} 项待办时间",
            "esc(c.reason)",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def test_window_and_conflict_display_semantics(self):
        # C7：user 窗口与 est 排程结果分开呈现；冲突徽章 + 详情原因；
        # 限时徽章随判定源退役（存量行保留兼容 kv 展示）
        for marker in (
            "windowParts.push(`不早于 ${fmtClock(occ.window_start_at)}`);",
            "windowParts.push(`最晚完成 ${fmtClock(occ.window_end_at)}`);",
            "this.conflictOccIds?.has(occ.id)",
            "this.conflictById?.get(occ.id)",
            '<span class="k">可安排时段</span>',
            "task.window_start_tod || task.window_end_tod",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)
        self.assertNotIn("if (occ.is_limited) badges.push(tag('限时', 'red'));", self.page)

    def test_poll_does_not_overwrite_unsaved_detail_input(self):
        # 30 秒轮询：详情栏有未保存输入时跳过重绘，不覆盖用户正在编辑的内容
        self.assertIn("if (occ && !this.detailHasUnsavedInput()) this.showOccurrenceDetail(occ);", self.page)
        self.assertIn("detailHasUnsavedInput()", self.page)

    def test_pause_resume_refresh_entry_and_copy(self):
        # 需求 24：任务详情栏提供「暂停刷新 / 恢复刷新」，仅周期任务（每日/
        # 间歇/每周/每月）且任务启用时显示；暂停需确认弹窗，恢复直接操作；
        # 两个 Toast 文案与需求一致；按钮状态由服务端 refresh_enabled 驱动。
        for marker in (
            "PAUSABLE_TYPES = ['daily', 'interval', 'weekly', 'monthly']",
            "PAUSABLE_TYPES.includes(task.task_type)",
            "task.refresh_enabled === false",
            # 暂停/恢复入口由 paused 状态驱动同一模板（act 名保持 task-*-refresh）
            "'task-resume-refresh' : 'task-pause-refresh'",
            "act === 'pause-refresh' || act === 'resume-refresh'",
            "恢复刷新", "暂停刷新",
            "刷新已暂停",  # 徽标：详情栏一眼可见当前暂停状态
            # 确认弹窗：指定标题/正文/按钮文案，非危险样式
            "'暂停后不会继续生成新的周期待办，当前已经生成的待办不会受到影响。之后可以随时恢复。'",
            "{ title: '暂停刷新', okText: '暂停刷新', cancelText: '取消', danger: false }",
            # Toast 文案（需求指定）
            "toast(resuming ? '已恢复刷新' : '已暂停刷新');",
            # PATCH 只写 refresh_enabled，不借道 is_active
            "body: JSON.stringify({ refresh_enabled: resuming })",
            # in-flight guard：请求期间禁用按钮防连续点击重复 PATCH
            "if (el) el.disabled = true;",
            "if (el) el.disabled = false;",
            # 操作成功后以服务端数据重绘详情（页面刷新后状态同样来自持久化）
            "const fresh = this.tasks.find((t) => t.id === id);",
            "if (fresh) this.showTaskDetail(fresh);",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.page)

    def test_pause_refresh_not_offered_to_once_or_inactive(self):
        # 单次/闲时没有周期刷新，不提供暂停入口；已废弃任务同样不显示
        self.assertIn("if (task.is_active && PAUSABLE_TYPES.includes(task.task_type)) {", self.page)
        # 暂停/恢复走任务级 PATCH，不出现「此次不执行/完成」语义混淆文案
        self.assertNotIn("暂停即此次不执行", self.page)


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
        for path in (PLANNING, RETRO_TIME, RETRO_SELECT, CYCLE_SETTINGS, CONFIG_PAGE):
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

    def test_task_form_submit_has_double_submit_lock(self):
        # 新建/编辑表单防重复提交：提交锁在 handler 入口同步建立（先于任何
        # 异步请求），创建（POST）与编辑（PATCH）走同一把锁；不能只依赖按钮
        # disabled（按钮聚焦后按 Enter/空格仍触发 click）。两阶段语义：
        # 提交/API 阶段失败 → 解锁可重试；服务器保存成功 → committed 终态，
        # 此后 toast/close/loadAll 后处理失败不得解锁、不得误报「保存失败」。
        form = _task_form_source(self.page)
        match = re.search(
            r"const submitBtn = root\.querySelector\('\[data-ok\]'\);\s*"
            r"let submitting = false;\s*"
            r"let committed = false;\s*"
            r"submitBtn\.onclick = async \(\) => \{(.*?)\n    \};",
            form, re.S)
        self.assertIsNotNone(match, "task form submit handler must hold a submitting lock")
        block = match.group(1)
        for marker in (
            "if (committed || submitting) return;",  # 终态优先：committed 后永不再次提交
            "submitting = true;",
            "submitBtn.disabled = true;",    # 提交中立即禁用按钮
            # 创建与编辑都在锁保护的同一 handler 内
            "await gw('/admin/api/planning/tasks', {",
            "await gw(`/admin/api/planning/tasks/${task.id}`, {",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, block)
        # 锁检查先于任何请求发起
        self.assertLess(block.index("if (committed || submitting) return;"), block.index("await gw("))

        # 提交/API 阶段失败：先解锁恢复按钮再提示（提示自身异常不得卡死
        # 提交资格），且 return 不得落到 committed 置位
        catch_seg = block[block.index("} catch (error) {"):block.index("committed = true;")]
        for marker in ("submitting = false;", "submitBtn.disabled = false;", "return;"):
            with self.subTest(catch_marker=marker):
                self.assertIn(marker, catch_seg)
        self.assertLess(catch_seg.index("submitting = false;"), catch_seg.index("toast(`保存失败"))
        self.assertLess(catch_seg.index("submitBtn.disabled = false;"), catch_seg.index("toast(`保存失败"))

        # 服务器保存成功 → 终态先行，随后后处理逐项 best-effort；终态段内
        # 不得出现任何解锁动作
        post_seg = block[block.index("committed = true;"):]
        for step in (
            "try { toast(editing ? '待办已保存' : '待办已创建'); } catch {",
            "try { close(); } catch {",
            "try { await this.loadAll(); } catch {",
        ):
            with self.subTest(post_step=step):
                self.assertIn(step, post_seg)
        self.assertNotIn("submitBtn.disabled = false;", post_seg)

    def test_task_form_submit_lock_blocks_rapid_reentry(self):
        # 行为级验证：在 quickjs 中真实执行 planning.js 的提交 handler 与
        # openTaskForm（仅 mock 通用 modal/esc/icon 帮助函数）。覆盖：
        # 1) API pending 期间连点只发一次请求；2) API reject 解锁可重试
        # （失败提示自身抛异常也不得卡死提交资格）；3) payload 构建阶段同步
        # 异常可恢复；4) 成功后 toast 抛异常 → 后处理照常、旧表单终态；
        # 5) 成功后 close 抛异常（modal 未移除）→ 旧表单不可再提交；
        # 6) 成功后 loadAll 抛异常 → 不得误报「保存失败」、不得重复提交；
        # 7) 成功关闭后重新 openTaskForm 打开全新表单，相同 payload 仍可
        # 正常创建（防重身份是单次表单生命周期，不是内容判重）。
        quickjs = _try_import_quickjs()
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        # 提交 handler 必须取自 openTaskForm 块内（openCycleSettings 也有
        # 同名 submitBtn.onclick，全局搜索会误取）
        form = _task_form_source(self.page)
        handler = re.search(r"submitBtn\.onclick = (async \(\) => \{.*?\n    \});", form, re.S).group(1)
        openform = re.search(r"  openTaskForm\(task\) \{(.*?)\n  \},", self.page, re.S)
        self.assertIsNotNone(openform, "openTaskForm not found in planning.js")
        harness = """
            var __result = null, __error = null;
            (async () => {
              let cur = null;
              globalThis.loadAll = async () => {
                if (cur.failLoad) throw new Error('load boom');
                cur.loads += 1;
              };
              const scenario = (editing, task) => {
                const s = { calls: [], toasts: [], closed: 0, loads: 0,
                            inFlightDisabled: null, failToast: false,
                            failClose: false, failLoad: false, throwSel: null };
                cur = s;
                const queue = [];
                const gw = (url, opts) => new Promise((resolve, reject) => {
                  s.calls.push({ url: url, method: opts.method, body: opts.body });
                  s.inFlightDisabled = submitBtn.disabled;
                  queue.push({ resolve: resolve, reject: reject });
                });
                const toast = (msg) => {
                  if (s.failToast) throw new Error('toast boom');
                  s.toasts.push(msg);
                };
                const close = () => {
                  if (s.failClose) throw new Error('close boom');
                  s.closed += 1;
                };
                const errorBlock = (msg) => '<err>' + msg + '</err>';
                const esc = (v) => String(v == null ? '' : v);
                const stub = { value: '', checked: false, disabled: false,
                               hidden: true, innerHTML: '',
                               scrollIntoView: () => {} };
                const root = {
                  querySelector: (sel) => {
                    if (s.throwSel === sel) throw new Error('dom boom');
                    return stub;
                  },
                  querySelectorAll: () => [],
                };
                const typeSelect = { value: 'daily' };
                const submitBtn = { disabled: false };
                let submitting = false, committed = false;
                // handler 源码在此拼接：闭包必须覆盖本场景的锁与终态变量
                const onclick = __HANDLER__;
                const lastToast = () => (s.toasts.length ? s.toasts[s.toasts.length - 1] : '');
                return { s: s, queue: queue, submitBtn: submitBtn, click: onclick, lastToast: lastToast };
              };

              const out = {};

              // A：API pending 期间快速连点 → 只发一次请求；成功 → 终态不解锁
              {
                const env = scenario(false, {});
                const ps = [env.click(), env.click(), env.click()];
                out.a_round1_calls = env.s.calls.length;
                out.a_pending_disabled = env.submitBtn.disabled;
                env.queue[0].resolve({});
                await Promise.all(ps);
                out.a_success = { disabled: env.submitBtn.disabled, closed: env.s.closed,
                                  loads: env.s.loads, lastToast: env.lastToast() };
                await env.click();
                out.a_reclick_calls = env.s.calls.length;
              }

              // B：API reject → 解锁；失败提示抛异常仍解锁；随后可重试成功
              {
                const env = scenario(false, {});
                const p1 = env.click();
                env.queue[0].reject(new Error('boom'));
                await p1;
                out.b_fail = { disabled: env.submitBtn.disabled, lastToast: env.lastToast(),
                               calls: env.s.calls.length };
                env.s.failToast = true;
                const p2 = env.click();
                env.queue[1].reject(new Error('boom2'));
                await p2.catch(() => {});
                out.b_fail_toast_throws = { disabled: env.submitBtn.disabled,
                                            calls: env.s.calls.length };
                env.s.failToast = false;
                const p3 = env.click();
                env.queue[2].resolve({});
                await p3;
                out.b_retry = { disabled: env.submitBtn.disabled, calls: env.s.calls.length,
                                closed: env.s.closed };
              }

              // C：payload 构建阶段同步异常 → 解锁恢复，修复后可再次提交
              {
                const env = scenario(false, {});
                env.s.throwSel = '#pf-estimated';
                await env.click();
                out.c_build_fail = { disabled: env.submitBtn.disabled, lastToast: env.lastToast(),
                                     calls: env.s.calls.length };
                env.s.throwSel = null;
                const p = env.click();
                env.queue[0].resolve({});
                await p;
                out.c_recovered = { calls: env.s.calls.length, closed: env.s.closed };
              }

              // D：成功后 toast 抛异常 → close/loadAll 照常执行，旧表单终态
              {
                const env = scenario(false, {});
                const p = env.click();
                env.s.failToast = true;
                env.queue[0].resolve({});
                await p;
                out.d_toast_fail = { closed: env.s.closed, loads: env.s.loads,
                                     disabled: env.submitBtn.disabled, calls: env.s.calls.length };
                await env.click();
                out.d_reclick_calls = env.s.calls.length;
              }

              // E：成功后 close 抛异常（modal 未被移除）→ 旧表单不可再提交
              {
                const env = scenario(false, {});
                const p = env.click();
                env.s.failClose = true;
                env.queue[0].resolve({});
                await p;
                out.e_close_fail = { closed: env.s.closed, loads: env.s.loads,
                                     disabled: env.submitBtn.disabled, lastToast: env.lastToast() };
                await env.click();
                out.e_reclick_calls = env.s.calls.length;
              }

              // F：成功后 loadAll 抛异常 → 不误报「保存失败」、不重复提交
              {
                const env = scenario(false, {});
                const p = env.click();
                env.s.failLoad = true;
                env.queue[0].resolve({});
                await p;
                out.f_load_fail = { closed: env.s.closed, disabled: env.submitBtn.disabled,
                                    lastToast: env.lastToast() };
                await env.click();
                out.f_reclick_calls = env.s.calls.length;
              }

              // 编辑路径：pending 连点一次 PATCH；失败解锁；成功后 loadAll
              // 抛异常不误报且终态
              {
                const env = scenario(true, { id: 7, time_mode: 'duration' });
                const ps = [env.click(), env.click()];
                out.e1_round1_calls = env.s.calls.length;
                env.queue[0].reject(new Error('patch boom'));
                await Promise.all(ps);
                out.e1_fail = { disabled: env.submitBtn.disabled, lastToast: env.lastToast(),
                                method: env.s.calls[0].method, url: env.s.calls[0].url };
                const p = env.click();
                env.s.failLoad = true;
                env.queue[1].resolve({});
                await p;
                out.e1_success_load_fail = { disabled: env.submitBtn.disabled,
                                             lastToast: env.lastToast() };
                await env.click();
                out.e1_reclick_calls = env.s.calls.length;
              }

              // G：真实 openTaskForm——成功关闭后重新打开全新表单，相同
              // payload 仍可正常创建（无内容判重）
              {
                const g = { calls: [], closedForms: 0, forms: [] };
                const queue = [];
                const gw = (url, opts) => new Promise((resolve, reject) => {
                  g.calls.push({ url: url, method: opts.method, body: opts.body });
                  queue.push({ resolve: resolve, reject: reject });
                });
                const toast = (msg) => { g.toasts.push(msg); };
                const makeEl = () => {
                  const kids = {};
                  return {
                    value: '', checked: false, disabled: false,
                    style: {}, dataset: {}, addEventListener: () => {},
                    // 记忆化：openTaskForm 内部绑定的 [data-ok] 与测试取到的是同一节点
                    querySelector: (sel) => {
                      if (!kids[sel]) kids[sel] = makeEl();
                      return kids[sel];
                    },
                    querySelectorAll: () => [],
                    insertAdjacentHTML: () => {},
                  };
                };
                const modal = () => {
                  const root = makeEl();
                  const close = () => { g.closedForms += 1; };
                  g.forms.push({ root: root, close: close });
                  return { root: root, close: close };
                };
                const esc = (v) => String(v == null ? '' : v);
                const icon = () => '';
                const TASK_TYPES = ['daily', 'weekly', 'monthly', 'interval', 'once'];
                const TASK_TYPE_LABELS = { daily: '每日', weekly: '每周', monthly: '每月',
                                           interval: '间歇', once: '单次' };
                const WEEKDAY_NAMES = ['一', '二', '三', '四', '五', '六', '日'];
                const self2 = { initRetroFields: () => {}, loadAll: async () => {},
                                occurrences: [] };
                const openForm = __OPENFORM_FACTORY__(modal, esc, icon, TASK_TYPES,
                                                     TASK_TYPE_LABELS, WEEKDAY_NAMES, gw, toast);
                openForm.call(self2, {});
                const btn1 = g.forms[0].root.querySelector('[data-ok]');
                const click1 = btn1.onclick;
                const p1 = click1();
                queue[0].resolve({});
                await p1;
                out.g_form1 = { closedForms: g.closedForms, calls: g.calls.length,
                                method: g.calls[0].method };
                await click1();
                out.g_form1_reclick_calls = g.calls.length;
                openForm.call(self2, {});
                const btn2 = g.forms[1].root.querySelector('[data-ok]');
                const p2 = btn2.onclick();
                queue[1].resolve({});
                await p2;
                out.g_form2 = { closedForms: g.closedForms, calls: g.calls.length,
                                identicalPayload: g.calls[0].body === g.calls[1].body };
              }

              return out;
            })().then((v) => { __result = v; }).catch((e) => { __error = String(e); });
        """.replace("__HANDLER__", handler).replace(
            "__OPENFORM_FACTORY__",
            "(function (modal, esc, icon, TASK_TYPES, TASK_TYPE_LABELS, "
            "WEEKDAY_NAMES, gw, toast) { return function (task) {"
            + openform.group(1) + "} })")
        ctx = quickjs.Context()
        ctx.eval(harness)
        for _ in range(10000):
            if not ctx.execute_pending_job():
                break
        self.assertIsNone(ctx.eval("__error"), f"harness crashed: {ctx.eval('__error')}")
        out = json.loads(ctx.eval("JSON.stringify(__result)"))

        # A：连点只发一次；pending 期间按钮禁用；成功后终态（按钮保持禁用、
        # 不再发请求、提示成功、表单关闭并刷新）
        self.assertEqual(out["a_round1_calls"], 1, "rapid double click must send one create request")
        self.assertTrue(out["a_pending_disabled"], "button must be disabled while request is in flight")
        self.assertTrue(out["a_success"]["disabled"], "committed form must stay disabled (no unlock)")
        self.assertEqual(out["a_success"]["closed"], 1)
        self.assertEqual(out["a_success"]["loads"], 1)
        self.assertEqual(out["a_success"]["lastToast"], "待办已创建")
        self.assertEqual(out["a_reclick_calls"], 1, "committed form must never submit again")

        # B：API reject → 解锁可重试；失败提示自身抛异常也不得卡死提交资格
        self.assertFalse(out["b_fail"]["disabled"], "API failure must release the lock")
        self.assertIn("保存失败", out["b_fail"]["lastToast"])
        self.assertEqual(out["b_fail"]["calls"], 1)
        self.assertFalse(out["b_fail_toast_throws"]["disabled"],
                         "release must happen before the failure toast")
        self.assertEqual(out["b_fail_toast_throws"]["calls"], 2)
        self.assertEqual(out["b_retry"]["calls"], 3)
        self.assertTrue(out["b_retry"]["disabled"])
        self.assertEqual(out["b_retry"]["closed"], 1)

        # C：构建阶段同步异常 → 解锁恢复，修复后可再次提交
        self.assertEqual(out["c_build_fail"]["calls"], 0, "no request must fire when build throws")
        self.assertFalse(out["c_build_fail"]["disabled"])
        self.assertIn("保存失败", out["c_build_fail"]["lastToast"])
        self.assertEqual(out["c_recovered"]["calls"], 1)
        self.assertEqual(out["c_recovered"]["closed"], 1)

        # D：成功后 toast 抛异常 → close/loadAll 照常执行，终态不再提交
        self.assertEqual(out["d_toast_fail"]["closed"], 1, "close must still run when toast throws")
        self.assertEqual(out["d_toast_fail"]["loads"], 1, "loadAll must still run when toast throws")
        self.assertTrue(out["d_toast_fail"]["disabled"])
        self.assertEqual(out["d_toast_fail"]["calls"], 1)
        self.assertEqual(out["d_reclick_calls"], 1, "committed form must never submit again")

        # E：成功后 close 抛异常 → 旧表单终态不可再提交，且不误报失败
        self.assertEqual(out["e_close_fail"]["closed"], 0, "simulated modal removal failure")
        self.assertEqual(out["e_close_fail"]["loads"], 1, "loadAll must still run when close throws")
        self.assertTrue(out["e_close_fail"]["disabled"])
        self.assertEqual(out["e_close_fail"]["lastToast"], "待办已创建")
        self.assertEqual(out["e_reclick_calls"], 1, "stale form must never submit again")

        # F：成功后 loadAll 抛异常 → 不误报「保存失败」、不重复提交
        self.assertEqual(out["f_load_fail"]["closed"], 1)
        self.assertEqual(out["f_load_fail"]["lastToast"], "待办已创建")
        self.assertTrue(out["f_load_fail"]["disabled"])
        self.assertEqual(out["f_reclick_calls"], 1, "committed form must never submit again")

        # 编辑路径：连点一次 PATCH；失败解锁；成功后 loadAll 抛异常不误报
        self.assertEqual(out["e1_round1_calls"], 1, "rapid double click must send one update request")
        self.assertEqual(out["e1_fail"]["method"], "PATCH")
        self.assertEqual(out["e1_fail"]["url"], "/admin/api/planning/tasks/7")
        self.assertFalse(out["e1_fail"]["disabled"])
        self.assertIn("保存失败", out["e1_fail"]["lastToast"])
        self.assertEqual(out["e1_success_load_fail"]["lastToast"], "待办已保存")
        self.assertNotIn("保存失败", out["e1_success_load_fail"]["lastToast"])
        self.assertTrue(out["e1_success_load_fail"]["disabled"])
        self.assertEqual(out["e1_reclick_calls"], 2, "committed edit form must never submit again")

        # G：新表单是全新生命周期——相同 payload 照常创建第二个
        self.assertEqual(out["g_form1"]["calls"], 1)
        self.assertEqual(out["g_form1"]["method"], "POST")
        self.assertEqual(out["g_form1"]["closedForms"], 1)
        self.assertEqual(out["g_form1_reclick_calls"], 1, "closed form must never submit again")
        self.assertEqual(out["g_form2"]["calls"], 2, "fresh form must allow the identical create")
        self.assertEqual(out["g_form2"]["closedForms"], 2)
        self.assertTrue(out["g_form2"]["identicalPayload"],
                        "second identical create must send an identical payload")


class PlanningPayloadBackendAcceptanceTests(unittest.TestCase):
    """C14 桥接验证：前端实际发出的 payload 被当前后端接受。

    前端契约只证明「发什么」；本类用同一份 payload 驱动真实后端入口
    （create_task / update_task / patch_occurrence），证明批次 3/6/7 的
    后端白名单与校验按预期接受或拒绝。
    """

    def _context(self):
        from test_planning_phase1b import Context
        return Context()

    def test_create_four_window_combos_are_accepted(self):
        from test_planning_phase1b import at
        # 无窗口
        with self._context() as c:
            task = c.create("daily", at(24, 10), estimated_minutes=30)
            row = c.db.rows["planning_task"][0]
            assert row.get("window_start_tod") is None and row.get("window_end_tod") is None
        # 只填最早开始
        with self._context() as c:
            c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00")
            row = c.db.rows["planning_task"][0]
            assert row.get("window_start_tod") == "09:00" and row.get("window_end_tod") is None
        # 只填最晚完成
        with self._context() as c:
            c.create("daily", at(24, 10), estimated_minutes=30,
                     window_end_tod="18:00")
            row = c.db.rows["planning_task"][0]
            assert row.get("window_start_tod") is None and row.get("window_end_tod") == "18:00"
        # 两端都填（30 分钟 + 09:00–12:00 = 系统在窗口内寻找连续 30 分钟）
        with self._context() as c:
            c.create("daily", at(24, 10), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
            row = c.db.rows["planning_task"][0]
            assert (row.get("window_start_tod"), row.get("window_end_tod")) == ("09:00", "12:00")

    def test_create_30min_window_schedules_inside_window(self):
        from test_planning_phase1b import at
        with self._context() as c:
            # 08:00 创建、窗口 09:00–12:00：est = 窗口起点的连续 30 分钟
            #（09:00–09:30），绝不是占满 09:00–12:00 整段
            c.create("daily", at(24, 8), estimated_minutes=30,
                     window_start_tod="09:00", window_end_tod="12:00")
            occ = c.rows[0]
            assert occ["window_start_at"].endswith("T09:00:00+08:00")
            assert occ["window_end_at"].endswith("T12:00:00+08:00")
            assert occ["est_start"] == occ["window_start_at"]
            assert occ["est_end"] == occ["window_start_at"].replace("T09:00", "T09:30")

    def test_edit_template_patch_with_window_fields_is_accepted(self):
        from test_planning_phase1b import at
        with self._context() as c:
            task = c.create("daily", at(24, 10), estimated_minutes=30)
            planning.update_task(
                task["id"],
                {"window_start_tod": "10:00", "window_end_tod": None},
                at(24, 11))
            row = c.db.rows["planning_task"][0]
            assert (row.get("window_start_tod"), row.get("window_end_tod")) == ("10:00", None)

    def test_current_occurrence_window_edit_is_accepted(self):
        from test_planning_phase1b import at
        with self._context() as c:
            c.create("daily", at(24, 10), estimated_minutes=30)
            occ = c.rows[0]
            planning.patch_occurrence(
                occ["id"],
                {"window_start_at": at(24, 15).isoformat(),
                 "window_end_at": at(24, 19).isoformat()},
                at(24, 11))
            assert occ["window_start_at"] == at(24, 15).isoformat()
            assert occ["window_end_at"] == at(24, 19).isoformat()

    def test_backend_rejects_legacy_fields_frontend_no_longer_sends(self):
        from gateway import planning as planning_module
        from test_planning_phase1b import at
        with self._context() as c:
            task = c.create("daily", at(24, 10), estimated_minutes=30)
            for legacy in (
                {"est_start_tod": "09:00"}, {"est_end_tod": "18:00"},
                {"deadline_tod": "20:00"}, {"deadline_end_tod": "21:00"},
                {"is_fixed": True},
            ):
                with self.assertRaises(planning_module.PlanningError) as caught:
                    planning_module.update_task(task["id"], legacy, at(24, 11))
                assert caught.exception.status_code == 400
            with self.assertRaises(planning_module.PlanningError) as caught:
                planning_module.create_task(
                    {"content": "x", "task_type": "daily",
                     "estimated_minutes": 30, "est_start_tod": "09:00"},
                    at(24, 10))
            assert caught.exception.status_code == 400


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
