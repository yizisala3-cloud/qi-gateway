"""备忘录前端契约测试（2026-10-02 一期）。

覆盖：
- planning.js 的备忘录页签接入（页签顺序、区域 id、按需加载与生命周期）；
- planning_memo.js 的行为契约：自动保存状态机（保存中/已保存/保存失败）、
  卸载与关编辑器时的 flush、拖动把手、搜索结果不调整持久顺序、版本冲突
  覆盖保存路径；
- Markdown 安全渲染：解析与净化分离，依赖固定版本随项目交付；
- 资产版本链与 quickjs 可用时的真实行为执行（自动保存泵）。
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLANNING = ROOT / "admin" / "js" / "pages" / "planning.js"
MEMO_MODULE = ROOT / "admin" / "js" / "lib" / "planning_memo.js"
MEMO_MARKDOWN = ROOT / "admin" / "js" / "lib" / "memo_markdown.js"
UI = ROOT / "admin" / "js" / "ui.js"
INDEX = ROOT / "admin" / "index.html"
VENDOR_DIR = ROOT / "admin" / "assets" / "vendor"

MARKED_VENDOR = "marked-12.0.2.min.js"
PURIFY_VENDOR = "purify-3.1.6.min.js"


def _try_import_quickjs():
    try:
        import quickjs  # noqa: F401
    except ImportError:
        return None
    return quickjs


def _module_source():
    src = MEMO_MODULE.read_text(encoding="utf-8")
    src = re.sub(r"import\s[^;]*?;", "", src, flags=re.S)
    src = re.sub(r"\bexport\s+(?=(async\s+)?(function|const|let|class|var)\b)", "", src)
    return src


class PlanningMemoTabContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.page = PLANNING.read_text(encoding="utf-8")
        cls.memo = MEMO_MODULE.read_text(encoding="utf-8")

    def test_planning_imports_memo_module_with_version(self):
        self.assertIn("createPlanningMemo", self.page)
        self.assertIn("'../lib/planning_memo.js?v=", self.page)

    def test_memo_region_between_all_and_goals(self):
        self.assertIn('id="planning-memo" data-panel="memo" hidden', self.page)
        self.assertLess(self.page.index('id="planning-all"'),
                        self.page.index('id="planning-memo"'))
        self.assertLess(self.page.index('id="planning-memo"'),
                        self.page.index('id="planning-goals"'))

    def test_memo_tab_data_loaded_on_demand(self):
        # 页签切换按需加载（与「全部待办」同模式），不拖慢首屏
        self.assertIn("tab === 'memo' && !this.loadedTabs.has('memo')", self.page)
        self.assertIn("this.memo ||= createPlanningMemo()", self.page)

    def test_planning_lifecycle_wires_memo(self):
        # 切走页签 flush 未保存内容；页面卸载释放编辑器与监听
        self.assertIn("this.memo?.flushPending()", self.page)
        self.assertIn("this.memo?.dispose()", self.page)

    def test_no_planning_logic_in_memo_tab_toolbar(self):
        # 备忘录使用自己的新建/搜索/筛选操作；不出现待办排程/重算/排列入口（§2.1）
        memo_region = self.page.split('id="planning-memo"', 1)[1].split('id="planning-goals"', 1)[0]
        for forbidden in ("recompute", "enter-reorder", "new-task", "plan-subtab"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, memo_region)


class MemoModuleContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.memo = MEMO_MODULE.read_text(encoding="utf-8")
        cls.markdown = MEMO_MARKDOWN.read_text(encoding="utf-8")

    def test_autosave_states_are_explicit(self):
        for marker in ("保存中…", "已保存", "保存失败", "覆盖保存", "重试"):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.memo)

    def test_save_status_only_after_latest_change_persisted(self):
        # 泵循环：保存成功后若仍有待保存快照，先接续保存，不显示「已保存」
        self.assertIn("if (state.dirty) {", self.memo)
        self.assertIn("setStatus('pending');   // 保存期间用户继续输入：接续保存最新快照",
                      self.memo)

    def test_flush_paths_cover_close_tab_unload(self):
        self.assertIn("pagehide", self.memo)                       # 卸载页面
        self.assertIn("keepalive: true", self.memo)                # 卸载路径尽力保存
        self.assertIn("requestEditorClose", self.memo)             # 关闭编辑器
        self.assertIn("flushPending", self.memo)                   # 切页签
        self.assertIn("丢弃并关闭", self.memo)                      # 失败时不静默丢失输入

    def test_conflict_paths_keep_user_input(self):
        self.assertIn("version_conflict", self.memo)
        self.assertIn("lifecycle_conflict", self.memo)
        self.assertIn("overwriteEditor", self.memo)

    def test_drag_requires_explicit_handle(self):
        # 手机拖动使用明确把手；把手外的滚动/点按不受影响
        self.assertIn("data-drag", self.memo)
        self.assertIn("closest('[data-drag]')", self.memo)
        self.assertIn("touch-action", (ROOT / "admin" / "css" / "style.css").read_text(encoding="utf-8"))

    def test_search_results_do_not_reorder(self):
        self.assertIn("this.view === 'search'", self.memo)
        self.assertIn("搜索结果不调整持久顺序", self.memo)

    def test_search_response_guard_against_stale_overwrite(self):
        self.assertIn("searchSeq", self.memo)

    def test_creation_is_idempotent_per_editor(self):
        self.assertIn("client_request_id", self.memo)
        self.assertIn("crypto.randomUUID()", self.memo)

    def test_memo_module_pins_shared_import_versions(self):
        version = re.search(r"ASSET_VERSION = '([^']+)'", UI.read_text(encoding="utf-8")).group(1)
        for ref in re.findall(r"\?v=([0-9a-z-]+)", self.memo):
            self.assertEqual(ref, version)


class MarkdownRenderingContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.markdown = MEMO_MARKDOWN.read_text(encoding="utf-8")
        cls.memo = MEMO_MODULE.read_text(encoding="utf-8")
        cls.index = INDEX.read_text(encoding="utf-8")

    def test_sanitize_before_innerhtml(self):
        # marked.parse 的结果必须经 DOMPurify.sanitize 才返回；
        # 依赖缺失时退回 textContent 纯文本路径，绝不裸写解析结果
        self.assertIn("marked.parse", self.markdown)
        self.assertIn("purify.sanitize", self.markdown)
        self.assertIn("textContent = text", self.markdown)
        self.assertNotIn("innerHTML = html", self.markdown)

    def test_memo_module_uses_shared_renderer(self):
        self.assertIn("renderMarkdown", self.memo)
        self.assertNotIn("marked.", self.memo)      # 页面模块不直接触碰解析器
        self.assertNotIn("DOMPurify", self.memo)

    def test_vendor_dependencies_are_pinned_and_delivered(self):
        for name in (MARKED_VENDOR, PURIFY_VENDOR):
            path = VENDOR_DIR / name
            with self.subTest(vendor=name):
                self.assertTrue(path.exists(), f"{name} must ship with the project")
                content = path.read_text(encoding="utf-8")
                self.assertGreater(len(content), 10000, f"{name} looks truncated")
        self.assertIn(f"/admin/assets/vendor/{MARKED_VENDOR}", self.index)
        self.assertIn(f"/admin/assets/vendor/{PURIFY_VENDOR}", self.index)

    def test_renderer_module_has_no_version_query(self):
        # vendor 由 index.html <script> 固定；渲染封装是 ES 模块，不引第三方
        self.assertNotIn("cdn.", self.markdown)
        self.assertNotIn("http://", self.markdown)
        self.assertNotIn("https://", self.markdown)


class MemoAutosaveBehaviorTests(unittest.TestCase):
    """quickjs 真实执行 createMemoAutosave（假时钟 + 受控 send）。"""

    @classmethod
    def setUpClass(cls):
        cls.quickjs = _try_import_quickjs()

    def test_autosave_pump_behaviors(self):
        quickjs = self.quickjs
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        harness = """
            var __result = null, __error = null;
            (async () => {
              const out = {};
              const makeClock = () => ({
                seq: 0, now: 0, tasks: [],
                schedule(fn, ms) { const id = ++this.seq; this.tasks.push({ id, fn, at: this.now + ms }); return id; },
                cancel(id) { this.tasks = this.tasks.filter((t) => t.id !== id); },
                tick(ms) {
                  this.now += ms;
                  const due = this.tasks.filter((t) => t.at <= this.now);
                  this.tasks = this.tasks.filter((t) => t.at > this.now);
                  for (const t of due) t.fn();
                },
              });

              const scenario = () => {
                const clock = makeClock();
                const events = [];
                const queue = [];
                const drain = async (n = 20) => { for (let i = 0; i < n; i++) await Promise.resolve(); };
                const send = (snapshot, opts) => {
                  const p = new Promise((resolve, reject) => {
                    queue.push({
                      snapshot: JSON.parse(JSON.stringify(snapshot)),
                      opts: { keepalive: !!(opts && opts.keepalive) },
                      resolve: (v) => resolve(v || { id: 7, content_version: 2 }),
                      reject,
                    });
                  });
                  return p;
                };
                const autosave = createMemoAutosave({
                  send, schedule: clock.schedule.bind(clock), cancel: clock.cancel.bind(clock),
                  delay: 800,
                  onChange: (s) => events.push(s.status + (s.hasPending ? '*' : '')),
                });
                return { clock, events, queue, autosave, send, drain };
              };

              // A：连续输入合并 —— debounce 内多次 markDirty 只发一次，取最新快照
              {
                const env = scenario();
                env.autosave.markDirty({ entryId: null, content: '第一版' });
                env.autosave.markDirty({ entryId: null, content: '第二版' });
                env.clock.tick(800);
                await env.drain();
                out.a_calls = env.queue.length;
                env.queue[0].resolve({ id: 7, content_version: 2 });
                await env.drain();
                out.a_events = env.events.join(',');
                out.a_snapshot = env.queue[0].snapshot.content;
              }

              // B：只有最新修改真正保存成功才显示「已保存」
              {
                const env = scenario();
                env.autosave.markDirty({ entryId: 1, content: 'v1' });
                env.clock.tick(800);
                env.autosave.markDirty({ entryId: 1, content: 'v2' });  // 保存期间继续输入
                env.queue[0].resolve({ id: 1, content_version: 2 });
                await env.drain();
                out.b_saved_between = env.events.includes('saved');
                out.b_calls = env.queue.length;
                out.b_second = env.queue[1].snapshot.content;
                env.queue[1].resolve({ id: 1, content_version: 3 });
                await env.drain();
                out.b_final = env.events[env.events.length - 1];
              }

              // C：失败保留输入可重试；版本冲突上报 conflict
              {
                const env = scenario();
                env.autosave.markDirty({ entryId: 2, content: '本地输入' });
                env.clock.tick(800);
                env.queue[0].reject({ code: 'version_conflict', message: 'stale' });
                await env.drain();
                out.c_status = env.events[env.events.length - 1];
                out.c_busy = env.autosave.isBusy();
                env.autosave.retry();
                await env.drain();
                out.c_retry_calls = env.queue.length;
                out.c_retry_snapshot = env.queue[1].snapshot.content;
                env.queue[1].resolve({ id: 2, content_version: 5 });
                await env.drain();
                out.c_final = env.events[env.events.length - 1];
              }

              // D：flush 立即排空（不等 debounce），排空完成后才 settle；
              // dispose 取消未触发的保存
              {
                const env = scenario();
                env.autosave.markDirty({ entryId: 3, content: '未保存' });
                const flushed = env.autosave.flush();
                await env.drain(5);
                out.d_flush_calls = env.queue.length;
                env.queue[0].resolve({ id: 3, content_version: 2 });
                await flushed;
                out.d_flush_settled = true;
              }
              {
                const env = scenario();
                env.autosave.markDirty({ entryId: 4, content: '将被丢弃' });
                env.autosave.dispose();
                env.clock.tick(5000);
                await env.drain();
                out.d_dispose_calls = env.queue.length;
              }

              // E：卸载 keepalive 路径不等串行队列，直接以最新快照调用 send
              {
                const env = scenario();
                env.autosave.markDirty({ entryId: 5, content: '离开前' });
                env.autosave.flush({ keepalive: true });
                await Promise.resolve();
                out.e_keepalive = env.queue.length === 1 && env.queue[0].opts.keepalive === true;
                out.e_snapshot = env.queue.length ? env.queue[0].snapshot.content : null;
              }

              return out;
            })().then((v) => { __result = v; }).catch((e) => { __error = String(e && e.message || e); });
        """
        ctx = quickjs.Context()
        ctx.eval(_module_source())
        ctx.eval(harness)
        for _ in range(10000):
            if not ctx.execute_pending_job():
                break
        self.assertIsNone(ctx.eval("__error"), f"harness crashed: {ctx.eval('__error')}")
        out = json.loads(ctx.eval("JSON.stringify(__result)"))

        # A：debounce 合并 + 最新快照；完成后才上报 saved
        self.assertEqual(out["a_calls"], 1)
        self.assertEqual(out["a_snapshot"], "第二版")
        self.assertEqual(out["a_events"], "pending*,pending*,saving,saved")

        # B：保存期间的新输入接续保存，中间不上报「已保存」
        self.assertFalse(out["b_saved_between"])
        self.assertEqual(out["b_calls"], 2)
        self.assertEqual(out["b_second"], "v2")
        self.assertEqual(out["b_final"], "saved")

        # C：版本冲突 → conflict + 保留输入；重试成功 → saved
        self.assertEqual(out["c_status"], "conflict*")
        self.assertTrue(out["c_busy"])
        self.assertEqual(out["c_retry_snapshot"], "本地输入")
        self.assertEqual(out["c_final"], "saved")

        # D：flush 立即排空；dispose 后到点也不发送
        self.assertEqual(out["d_flush_calls"], 1)
        self.assertTrue(out["d_flush_settled"])
        self.assertEqual(out["d_dispose_calls"], 0)

        # E：keepalive flush 不抛异常（卸载路径尽力而为）
        self.assertTrue(out["e_keepalive"])


class MemoSourceSyntaxTests(unittest.TestCase):
    def test_js_syntax_is_parseable(self):
        quickjs = _try_import_quickjs()
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        for path in (MEMO_MODULE, MEMO_MARKDOWN):
            src = path.read_text(encoding="utf-8")
            src = re.sub(r"import\s[^;]*?;", "", src, flags=re.S)
            src = re.sub(r"\bexport\s+(?=(async\s+)?(function|const|let|class|var)\b)", "", src)
            check = quickjs.Context().eval(
                "(function(src){ try { new globalThis.Function(src); return 'ok'; }"
                " catch (e) { return e.name + ': ' + e.message; } })"
            )
            with self.subTest(file=path.name):
                self.assertEqual(check(src), "ok")


if __name__ == "__main__":
    unittest.main()
