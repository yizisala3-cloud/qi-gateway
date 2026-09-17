"""Frontend contract tests for the admin memory lifecycle UI.

The dynamic form module owns the six-class Chinese forms and the two
independent tag controls; the browser wires create/edit/type-change/undo/
restore to the purpose-built endpoints and keeps version display limited
to the current version and its direct parent.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BROWSER = ROOT / "admin" / "js" / "pages" / "_memory_browser.js"
FORM = ROOT / "admin" / "js" / "pages" / "_memory_form.js"
INDEX_HTML = ROOT / "admin" / "index.html"
ASSET_VERSION = "20260903-retrotime1"

EMOJI_PATTERN = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F600-\U0001F64F]"
)


class MemoryFormContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.form = FORM.read_text(encoding="utf-8")
        cls.browser = BROWSER.read_text(encoding="utf-8")

    def test_six_types_have_chinese_dynamic_fields(self):
        for ctype in ("moment", "thread", "episode", "inside_joke", "profile", "interaction_rule"):
            with self.subTest(type=ctype):
                self.assertIn(f"{ctype}:", self.form)
        self.assertIn("近期片段", self.form)
        self.assertIn("互动规则", self.form)

    def test_required_marks_hints_and_enum_options_exist(self):
        self.assertIn("req: true", self.form)
        self.assertIn("field-hint", self.form)
        for state in ("standalone", "linked", "absorbed", "open", "paused",
                      "resolved", "dissolved", "abandoned", "complete", "partial",
                      "uncertain", "stable", "contextual", "provisional",
                      "explicit_self_report", "repeated_observation",
                      "active", "revoked", "superseded"):
            with self.subTest(enum=state):
                self.assertIn(state, self.form)

    def test_thread_closure_fields_are_conditional(self):
        self.assertIn("CLOSED_THREAD_STATES", self.form)
        self.assertIn("线索结束后必填", self.form)
        self.assertIn("不允许填写结束字段", self.form)

    def test_tag_controls_are_two_independent_instances(self):
        self.assertIn("mf-tags", self.form)
        self.assertIn("mf-recall-tags", self.form)
        # 即时阻止重复与超长标签（每条最多 200 字符）。
        self.assertIn("同一组内不允许重复", self.form)
        self.assertIn("maxLen = 200", self.form)

    def test_recall_tag_confirmation_modal(self):
        self.assertIn("缺少召回标签可能降低这条记忆被准确召回的机会", self.form)

    def test_source_type_uses_fixed_chinese_options(self):
        for value in ("natural_chat", "persona_prompt", "code", "document",
                      "quote", "roleplay", "tool_result", "system_meta", "unknown"):
            with self.subTest(value=value):
                self.assertIn(f"'{value}'", self.form)

    def test_no_emoji_icons(self):
        for name, text in (("form", self.form), ("browser", self.browser)):
            with self.subTest(file=name):
                self.assertIsNone(EMOJI_PATTERN.search(text), "emoji used as an icon")

    def test_submit_button_disables_while_in_flight(self):
        self.assertIn("submitBtn.disabled = true", self.form)
        self.assertIn("el.disabled = true", self.browser)


class MemoryBrowserContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.browser = BROWSER.read_text(encoding="utf-8")
        cls.form = FORM.read_text(encoding="utf-8")

    def test_create_entry_button_wired(self):
        self.assertIn("data-act=\"mem-create\"", self.browser)
        self.assertIn("新增记忆", self.browser)
        self.assertIn("openMemoryForm({ mode: 'create'", self.browser)

    def test_lifecycle_endpoints_are_purpose_built(self):
        self.assertIn("/undo-type-change", self.browser)
        self.assertIn("/restore", self.browser)
        self.assertIn("mode: 'change'", self.browser)
        self.assertIn("mode: 'edit'", self.browser)

    def test_undo_only_for_manual_type_change_versions(self):
        self.assertIn("m.source === 'manual' && m.supersedes_memory_id", self.browser)
        self.assertIn("撤销最近一次类型修改", self.browser)

    def test_archived_and_superseded_rows_hide_actions(self):
        self.assertIn("自然归档记忆：可查看与恢复，不能直接编辑或修改类型", self.browser)
        self.assertIn("可以查看，但不能编辑、恢复或参与召回", self.browser)

    def test_version_area_shows_only_direct_parent(self):
        self.assertIn("直接上一版本", self.browser)
        self.assertNotIn("同主题键版本链", self.browser)

    def test_manual_source_labelled_in_chinese(self):
        self.assertIn("用户手工写入", self.browser)

    def test_old_panel_edit_form_is_gone(self):
        self.assertNotIn("ed-recall-scene", self.browser)
        self.assertNotIn("ed-evidence-time", self.browser)

    def test_archive_uses_dedicated_endpoint(self):
        self.assertIn("/archive", self.browser)
        self.assertIn("data-act=\"mem-archive\"", self.browser)
        # 通用 PATCH 不再承载归档：is_active 不允许经 generic PATCH 修改。
        self.assertNotIn("{ is_active: false }", self.browser)

    def test_form_passes_result_to_on_saved(self):
        form = FORM.read_text(encoding="utf-8")
        self.assertIn("const result = await gw(url", form)
        self.assertIn("await onSaved(result)", form)

    def test_same_type_edit_accompanies_continuity_type(self):
        form = FORM.read_text(encoding="utf-8")
        self.assertIn("patch.continuity_type = type;", form)

    def test_picker_cleanup_watches_body_not_modal(self):
        # modal 关闭是被其父节点（body）整体移除，modal 内部无 childList
        # 变化；清理观察必须挂在 body 上（辅助断言，主验为浏览器验收）。
        form = FORM.read_text(encoding="utf-8")
        self.assertIn("rootObserver.observe(document.body, { childList: true });", form)
        self.assertNotIn("observer.observe(host,", form)
        self.assertIn("if (!document.contains(anchor)) closeRetroTimePop();", form)

    def test_no_native_datetime_inputs_remain(self):
        # 原生 datetime-local 的浏览器弹窗与复古视觉体系不符，
        # 一律使用自绘复古时间选择器。
        form = FORM.read_text(encoding="utf-8")
        self.assertNotIn("datetime-local", form)
        self.assertIn("retro-time-field", form)
        self.assertIn("retro-time-pop", form)
        css = (ROOT / "admin" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn(".retro-time-pop", css)
        self.assertNotIn("#3b82f6", css.lower())

    def test_edit_patch_logic_is_a_pure_tested_module(self):
        assert (ROOT / "admin" / "js" / "pages" / "_memory_patch.js").exists()
        form = FORM.read_text(encoding="utf-8")
        self.assertIn("buildEditPatch(memory, values)", form)

    def test_conflicts_use_modal_not_alert(self):
        self.assertNotIn("window.alert", self.browser)
        self.assertNotIn("window.alert", self.form)
        self.assertIn("当前无法撤销", self.browser)


class AssetVersionContractTests(unittest.TestCase):
    def test_index_html_loads_current_assets(self):
        html = INDEX_HTML.read_text(encoding="utf-8")
        self.assertIn(f"style.css?v={ASSET_VERSION}", html)
        self.assertIn(f"app.js?v={ASSET_VERSION}", html)


if __name__ == "__main__":
    unittest.main()


class LongContentContractTests(unittest.TestCase):
    """长正文路径：审核、合并与编辑入口支持 3000 字符。

    通用短字段输入（_memory_form.js 的默认分支）保持 600，不属于长正文
    路径；标题、备注、召回场景等其他字段限制不变。
    """

    def setUp(self):
        self.browser = BROWSER.read_text(encoding="utf-8")
        self.form = FORM.read_text(encoding="utf-8")

    def test_review_and_merge_inputs_support_3000(self):
        self.assertIn('id="rv-content" rows="7" maxlength="3000"', self.browser)
        self.assertIn('id="merge-content" rows="8" maxlength="3000"', self.browser)
        self.assertEqual(self.browser.count('maxlength="600"'), 0)

    def test_edit_form_supports_3000(self):
        self.assertIn('id="mf-content" rows="6" maxlength="3000"', self.form)
        self.assertIn("5 到 3000 个字符", self.form)
        self.assertIn("/3000", self.form)
        self.assertIn("正文长度必须在 5 到 3000 个字符之间", self.form)
        # 仅通用短字段输入保留 600。
        self.assertEqual(self.form.count('maxlength="600"'), 1)
