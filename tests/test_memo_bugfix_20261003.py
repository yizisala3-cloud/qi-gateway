"""2026-10-03 修复轮回归：BUG-03/05/06/07/09/11/12 + 新需求确认。

被测对象是 ``admin/js/lib/planning_memo.js`` 与 ``admin/js/ui.js`` 的真实
源码（quickjs 执行，imports 剥离后注入受控假件，复用
test_memo_editor_behavior 的夹具骨架）：

- BUG-03 keepalive 结果跟踪：失败并回队列、恢复后完成必须确认，不再
  无确认清稿；keepalive 在途时不开第二个同版本请求；关闭 B 不清 A 的草稿；
- BUG-05 标签创建等待关闭流程 + 关闭决策不重入；
- BUG-06 模式切换按点击顺序提交；重排刷新未确认前不开新拖动，可重试；
- BUG-07 去抖清除/切视图后旧 timer 失效；迟到搜索回调不污染提示区；
- BUG-09 删除当前筛选标签后筛选同步重置；
- BUG-12 草稿恢复确认在途时卸载：确认返回不创建旧编辑框、不清草稿；
- 新需求：新建默认用途=常驻备忘；看板板块标题不带条数徽标。

所有断言基于真实执行的模块代码，不接受「源码包含字符串」式替代。
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMO_MODULE = ROOT / "admin" / "js" / "lib" / "planning_memo.js"
UI_MODULE = ROOT / "admin" / "js" / "ui.js"

# 复用行为测试套件的夹具骨架（prelude/源码剥离/quickjs 探测）
_spec = None
try:
    import importlib.util
    _spec = importlib.util.spec_from_file_location(
        "_memo_behavior_base", ROOT / "tests" / "test_memo_editor_behavior.py")
    _base = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_base)
except Exception:   # pragma: no cover - quickjs 缺失时由 _run 跳过
    _base = None


def _try_import_quickjs():
    try:
        import quickjs  # noqa: F401
    except ImportError:
        return None
    return quickjs


def _ui_source():
    src = UI_MODULE.read_text(encoding="utf-8")
    src = re.sub(r"import\s[^;]*?;", "", src, flags=re.S)
    src = re.sub(r"\bexport\s+(?=(async\s+)?(function|const|let|class|var)\b)", "", src)
    return src


class MemoBugfix20261003Tests(unittest.TestCase):
    """quickjs 真实执行 planning_memo.js：本轮修复的回归场景。"""

    @classmethod
    def setUpClass(cls):
        cls.quickjs = _try_import_quickjs()

    def _run(self, scenario):
        quickjs = self.quickjs
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        ctx = quickjs.Context()
        ctx.eval(_base._module_source())
        ctx.eval(_base.HARNESS_PRELUDE + scenario + _base.HARNESS_EPILOGUE)
        for _ in range(100000):
            if not ctx.execute_pending_job():
                break
        err = ctx.eval("__error")
        self.assertIsNone(err, f"harness crashed: {err}")
        return json.loads(ctx.eval("JSON.stringify(__result)"))

    # ── BUG-03：keepalive 结果跟踪 ────────────────────────────────

    def test_bug03_keepalive_failure_then_resume_requires_confirm(self):
        """keepalive 在途时硬卸载，传输失败后 bfcache 返回：保存泵把
        失败快照并回队列并置失败态——完成按钮必须确认，不能把未入库
        草稿当已排空清掉；也不并发第二个同版本请求。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          setContent('卸载前输入'); m.markEditorDirty();
          // 无在途保存：pagehide keepalive 携带最新快照发出
          m.editor.autosave.flush({ keepalive: true });
          out.keepalive_sent = fetches.length === 1
              && fetches[0].options.keepalive === true;
          out.busy_while_inflight = m.editor.autosave.isBusy();
          // bfcache 冻结期间传输失败；页面恢复（pageshow persisted）
          fetches[0].reject(new Error('network lost'));
          m.editor.autosave.resume();
          await drain();
          clock.tick(1);          // 失败回调经 setTimeout(0) 续排泵
          await drain();
          // 失败并回队列：泵自动重试（普通 PATCH，非 keepalive）
          out.requeued_retry = fetches.length === 2
              && fetches[1].options.keepalive !== true
              && fetches[1].body.content === '卸载前输入';
          out.retry_inflight_busy = m.editor.autosave.isBusy();
          // 重试仍失败：完成按钮必须走确认，不再无确认清稿
          fetches[1].reject(new Error('still down'));
          await drain();
          out.status_error = m._saveState.status === 'error';
          confirmResult = false;   // 第一次决策：返回编辑
          const closing = m.requestEditorClose();
          await drain(10);
          out.close_flush_retried = fetches.length === 3;
          fetches[2].reject(new Error('down again'));
          await closing;
          await drain();
          out.confirm_asked = confirmCalls.length === 1;
          out.still_open = !!m.editor;
          out.draft_kept = !!(store['qi-memo-editor-draft']);
          // 用户明确丢弃后才允许清稿关闭（重试再失败 → 确认丢弃）
          confirmResult = true;
          const closing2 = m.requestEditorClose();
          await drain(10);
          fetches[3].reject(new Error('down'));
          await closing2;
          await drain();
          out.closed_after_discard = m.editor === null;
          out.draft_cleared_after_discard = !store['qi-memo-editor-draft'];
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["keepalive_sent"])
        self.assertTrue(out["busy_while_inflight"])
        self.assertTrue(out["requeued_retry"],
                        "keepalive 失败必须并回队列重试（普通请求）")
        self.assertTrue(out["retry_inflight_busy"])
        self.assertTrue(out["status_error"])
        self.assertTrue(out["confirm_asked"], "失败后完成必须确认，不无确认清稿")
        self.assertTrue(out["still_open"])
        self.assertTrue(out["draft_kept"])
        self.assertTrue(out["closed_after_discard"])
        self.assertTrue(out["draft_cleared_after_discard"])

    def test_bug03_unrelated_editor_close_keeps_other_draft(self):
        """entry 7 留有未确认草稿时，打开 entry 8 查看（不修改）并关闭：
        entry 7 的草稿仍可恢复（清理条件核对会话归属）。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          setContent('7的未保存草稿'); m.markEditorDirty();
          m.editor.autosave.flush({ keepalive: true });   // 硬卸载模拟
          const draftBefore = JSON.parse(store['qi-memo-editor-draft']);
          out.draft_is_7 = draftBefore.entryId === 7;
          // 不经关闭流程清槽（硬卸载路径）：直接丢掉编辑器对象
          m.editor = null;
          // 会话2：打开 entry 8（服务端内容与草稿无关），不修改就完成
          gwApi = async (url) => url === '/admin/api/memo/entries/8'
            ? entryPayload(8, { content: 'server-8' }) : [];
          m.openEditor(8);
          await drain();
          out.no_restore_for_8 = !!m.editor && m.editor.entryId === 8;
          m.requestEditorClose();
          await drain();
          out.entry8_closed = m.editor === null;
          const draftAfter = JSON.parse(store['qi-memo-editor-draft'] || 'null');
          out.draft_survives = !!draftAfter && draftAfter.entryId === 7
              && draftAfter.content === '7的未保存草稿';
          // 重开 entry 7：草稿仍可恢复
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          out.restored_on_reopen = m.editor
              && m.editor.modal.root.fields['[data-editor-content]'].value === '7的未保存草稿';
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["draft_is_7"])
        self.assertTrue(out["no_restore_for_8"])
        self.assertTrue(out["entry8_closed"])
        self.assertTrue(out["draft_survives"],
                        "关闭 entry8 不得清掉 entry7 的未确认草稿")
        self.assertTrue(out["restored_on_reopen"])

    # ── BUG-05：标签创建等待 + 关闭不重入 ─────────────────────────

    def test_bug05_close_waits_for_pending_tag_creation(self):
        """添加标签后立即点完成：关闭流程等待标签创建落定，最终关联
        确实随保存提交；不再留下孤立标签。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          let releaseTag;
          const tagGate = new Promise((resolve) => { releaseTag = resolve; });
          let tagList = [];
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/tags') {
              if (opts && opts.method === 'POST') {
                await tagGate;
                tagList = [{ id: 5, name: '新标签', position: 1 }];
                return { id: 5, name: '新标签', position: 1, existed: false };
              }
              return tagList;
            }
            return url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          };
          m.openEditor(7);
          await drain();
          // 触发标签创建（在途）
          editorFields()['[data-new-tag-name]'].value = '新标签';
          const op = m.addEditorTag();
          out.tag_pending = m.editor.pendingOps.size === 1;
          // 立即点完成：关闭流程必须等待标签落定
          const closing = m.requestEditorClose();
          await drain();
          out.not_closed_yet = !!m.editor && confirmCalls.length === 0;
          releaseTag();
          await drain(10);
          // 标签落定后关闭流程排空保存：PATCH 已带新标签发出
          const patch = fetches.find((f) => f.url === '/admin/api/memo/entries/7');
          out.patch_has_tag = !!patch && (patch.body.tag_ids || []).includes(5);
          if (patch) {
            patch.resolve({ ok: true, json: async () => ({
              id: 7, title: null, content: 'server-7', kind: 'note',
              content_version: 2, tags: [{ id: 5, name: '新标签' }] }) });
          }
          await closing;
          await drain();
          out.closed = m.editor === null;
          out.tag_registered = m.tags.some((t) => t.id === 5);
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["tag_pending"])
        self.assertTrue(out["not_closed_yet"],
                        "标签创建在途时完成不得立即关闭")
        self.assertTrue(out["patch_has_tag"], "标签关联必须随保存提交")
        self.assertTrue(out["closed"])
        self.assertTrue(out["tag_registered"])

    def test_bug05_double_complete_makes_one_close_decision(self):
        """保存失败时连点完成：只出现一次确认框；返回编辑后可再次关闭。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          setContent('会失败的内容'); m.markEditorDirty();
          // 保存失败：flush 停在失败态
          m.editor.autosave.flush().catch(() => {});
          fetches[0].reject(new Error('down'));
          await drain();
          out.status_error = m._saveState.status === 'error';
          // 连点完成（两次 requestEditorClose 并行）；确认被预置为
          // 「返回编辑」，验证只出现一次决策且编辑器保持打开
          confirmResult = false;
          const first = m.requestEditorClose();
          const second = m.requestEditorClose();
          out.same_promise = first === second;
          await drain(10);
          out.close_flush_retried = fetches.length === 2;
          fetches[1].reject(new Error('down'));
          await Promise.all([first.catch(() => {}), second.catch(() => {})]);
          await drain();
          out.one_confirm = confirmCalls.length === 1;
          out.still_open = !!m.editor;
          out.guard_released = m.editor
              && (m.editor.closeInFlight === null || m.editor.closeInFlight === undefined);
          // 返回编辑后可再次发起关闭（确认「丢弃并关闭」）
          confirmResult = true;
          const third = m.requestEditorClose();
          await drain(10);
          if (fetches[2]) fetches[2].reject(new Error('down'));
          await third.catch(() => {});
          await drain();
          out.closed_after_return = m.editor === null;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["status_error"])
        self.assertTrue(out["same_promise"], "连点完成共享同一次关闭决策")
        self.assertTrue(out["one_confirm"],
                        f"只应出现一次确认框，实际 {out['one_confirm']}")
        self.assertTrue(out["still_open"])
        self.assertTrue(out["guard_released"])
        self.assertTrue(out["closed_after_return"])

    # ── BUG-06：模式顺序与重排刷新确认 ────────────────────────────

    def test_bug06_rapid_mode_switches_commit_in_click_order(self):
        """快速 manual→latest：两个写请求按点击顺序提交，最终服务端
        状态与最后一次有效选择一致。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.activeTagId = 1;
          const modes = () => gwCalls
            .filter((c) => c.url === '/admin/api/memo/note-mode')
            .map((c) => JSON.parse(c.opts.body).mode);
          gwApi = async () => [];
          m.setNoteMode('manual');
          out.first_sync = modes()[0] === 'manual';
          m.setNoteMode('latest');   // 首个在途：排队，按点击顺序提交
          const queued = modes().length === 1;
          await drain(40);
          out.final_modes = modes().join(',');
          out.second_queued_not_dropped = queued;
          out.reorder_busy_released = m.reorderBusy === false;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["first_sync"])
        self.assertTrue(out["second_queued_not_dropped"])
        self.assertEqual(out["final_modes"], "manual,latest",
                         "最终服务端状态必须符合最后有效选择")
        self.assertTrue(out["reorder_busy_released"])

    def test_bug06_reorder_refresh_failure_blocks_next_drag(self):
        """重排成功但刷新失败：拖动保持暂停并给出重试入口；第二次拖动
        不得从旧 board 构造撤回请求；刷新成功后自动恢复。"""
        scenario = r"""
        {
          const { m, body } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          const section = { tag: { id: 1, name: '购物', position: 1 },
            note_sort_mode: 'latest', preview: [], items: [],
            pinned_order: [11, 12], note_order: [], pinned_count: 2, note_count: 0 };
          m.tags = [{ id: 1, name: '购物', position: 1 }];
          m.board = { sections: [section] };
          let boardOk = false;   // 首次重排后的刷新失败（stale 窗口）
          gwApi = async (url) => url === '/admin/api/memo/board'
            ? (boardOk ? { sections: [section] } : Promise.reject(new Error('down')))
            : url === '/admin/api/memo/tags' ? m.tags : [];
          const drag = { dataset: { entryId: '12', kind: 'pinned' } };
          const list = {
            dataset: { dragList: 'entries' },
            closest: (sel) => sel === '.memo-section' ? { dataset: { tagId: '1' } } : null,
            querySelectorAll: () => [drag],
          };
          const reorderCalls = () => gwCalls.filter((c) => c.url === '/admin/api/memo/reorder').length;
          m.commitEntryOrder(list, drag);
          await drain(60);
          out.first_committed = reorderCalls() === 1;
          out.busy_held_on_stale = m.reorderBusy === true;
          out.retry_notice = String(m.statusHost._html).includes('刷新重试');
          // 刷新失败期间的第二次拖动被阻止（不得撤回首次顺序）
          m.commitEntryOrder(list, drag);
          out.second_blocked = reorderCalls() === 1;
          // 服务恢复：重试读取成功 → 自动解锁
          boardOk = true;
          m.reload({ silent: true });
          await drain(60);
          out.released_after_retry = m.reorderBusy === false
              && m._pendingRefresh === false;
          out.notice_cleared = String(m.statusHost._html) === '';
          m.commitEntryOrder(list, drag);
          out.third_allowed = reorderCalls() === 2;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["first_committed"])
        self.assertTrue(out["busy_held_on_stale"])
        self.assertTrue(out["retry_notice"])
        self.assertTrue(out["second_blocked"],
                        "刷新未确认前不得放行下一次拖动")
        self.assertTrue(out["released_after_retry"])
        self.assertTrue(out["notice_cleared"])
        self.assertTrue(out["third_allowed"])

    # ── BUG-07：搜索去抖与迟到回调 ────────────────────────────────

    def test_bug07_clear_within_debounce_stays_board(self):
        """输入后 300ms 内清除搜索：旧 timer 失效，不再把视图拖回搜索；
        切走后旧 timer 也不能切回搜索。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          gwApi = async (url) => url === '/admin/api/memo/board'
            ? { sections: [] } : url === '/admin/api/memo/tags' ? [] : [];
          await m.mount(m._harness.memoRoot);   // 注册真实输入监听
          const searchInput = m._harness.searchInput;
          searchInput.handlers.input({ target: { value: '关键词' } });
          // 300ms 内点「清除搜索」：真实路径 = 清值 + switchView('board')
          searchInput.value = '';
          m.switchView('board');   // 清除搜索路径
          clock.tick(600);
          await drain(30);
          out.stays_board = m.view === 'board';
          out.no_search_call = !gwCalls.some((c) => c.url.includes('q='));
          // 切走后旧 timer 也不能切回搜索
          searchInput.handlers.input({ target: { value: '第二条' } });
          m.switchView('tag');
          clock.tick(600);
          await drain(30);
          out.stays_tag = m.view === 'tag';
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["stays_board"], "清除搜索后旧去抖不得切回搜索视图")
        self.assertTrue(out["no_search_call"])
        self.assertTrue(out["stays_tag"], "切走后旧去抖不得把视图拖回搜索")

    def test_bug07_stale_search_failure_isolated_after_switch(self):
        """搜索在途切走：迟到的失败/成功都不写当前视图与提示区。"""
        scenario = r"""
        {
          const { m, statusHost } = buildMemo();
          m.view = 'search'; m.searchQuery = '关键词';
          m.tags = [];
          m.board = { sections: [] };
          let release;
          const gate = new Promise((resolve) => { release = resolve; });
          gwApi = async (url) => {
            if (url.includes('/admin/api/memo/entries?')) await gate;
            return url === '/admin/api/memo/board' ? { sections: [] } : [];
          };
          const run = m.runSearch();
          await drain(5);
          m.switchView('board');          // 用户切走
          m.render();
          release();                       // 迟到的失败返回
          await run.catch(() => {});
          await drain(30);
          out.status_clean = String(statusHost._html) === '';
          out.body_clean = !String(m.body._html).includes('搜索失败');
          // 迟到的成功同样不重绘已离开的搜索视图
          let release2;
          const gate2 = new Promise((resolve) => { release2 = resolve; });
          gwApi = async (url) => {
            if (url.includes('/admin/api/memo/entries?')) await gate2;
            return url === '/admin/api/memo/board' ? { sections: [] } : [];
          };
          const run2 = m.runSearch();
          await drain(5);
          m.switchView('archived');
          release2([{ id: 1 }]);
          await drain(30);
          out.still_lifecycle = m.view === 'archived';
          out.no_search_redraw = !String(m.body._html).includes('条结果');
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["status_clean"], "迟到的搜索失败不得污染提示区")
        self.assertTrue(out["body_clean"])
        self.assertTrue(out["still_lifecycle"])
        self.assertTrue(out["no_search_redraw"])

    # ── BUG-09：删除当前筛选标签后重置筛选 ────────────────────────

    def test_bug09_delete_active_filter_tag_resets_filter(self):
        """删除的正是当前筛选项：筛选身份与控件同步重置；其他标签的
        删除不影响当前筛选。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.tags = [{ id: 5, name: '被删标签', position: 1 },
                    { id: 6, name: '其他标签', position: 2 }];
          m.board = { sections: [] };
          m.filterTagId = 5;
          gwApi = async (url) => url === '/admin/api/memo/board'
            ? { sections: [] } : url === '/admin/api/memo/tags' ? m.tags : [];
          // 先删非当前筛选标签：筛选保持
          await m.deleteTag(6);
          out.filter_kept = m.filterTagId === 5;
          // 再删当前筛选标签：筛选同步重置
          await m.deleteTag(5);
          out.filter_reset = m.filterTagId === '';
          out.view_board = m.view === 'board';
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["filter_reset"], "删除当前筛选项必须同步清掉筛选")
        self.assertTrue(out["view_board"])
        self.assertTrue(out["filter_kept"])

    # ── BUG-12：卸载后的草稿确认作废 ──────────────────────────────

    def test_bug12_draft_confirm_after_dispose_creates_nothing(self):
        """草稿恢复确认在途时离开页面（dispose）：确认返回后不创建旧
        编辑框、不发旧会话保存请求、不误清草稿；恢复与丢弃两个分支都
        不得改写新会话。"""
        scenario = r"""
        {
          // 服务端 v3，本机草稿 baseVersion=1：版本差距触发恢复确认
          store['qi-memo-editor-draft'] = JSON.stringify({
            entryId: 7, crid: null, kind: 'note', title: '',
            content: '本机未保存草稿', tag_ids: [], baseVersion: 1,
            savedAt: Date.now(),
          });
          const { m } = buildMemo();
          m.editor = null;
          let release;
          const gate = new Promise((resolve) => { release = resolve; });
          globalThis.confirm = () => gate;   // 挂起恢复/丢弃决策
          gwApi = async (url) => url === '/admin/api/memo/entries/7'
            ? entryPayload(7, { content: 'server-7', content_version: 3 }) : [];
          m.openEditor(7);
          await drain(10);
          out.confirm_pending = confirmCalls.length === 0 && !!m._openingEditor;
          // 用户离开规划页面：模块卸载
          m.dispose();
          // 之后才点「恢复草稿」
          release(true);
          await drain(30);
          out.no_editor_after_dispose = m.editor === null;
          out.no_save_requests = !fetches.some((f) => f.options && f.options.method === 'PATCH');
          out.draft_not_cleared = !!store['qi-memo-editor-draft'];
          // 「丢弃草稿」路径同样不清槽
          store['qi-memo-editor-draft'] = JSON.stringify({
            entryId: 7, crid: null, kind: 'note', title: '',
            content: '本机未保存草稿', tag_ids: [], baseVersion: 1,
            savedAt: Date.now(),
          });
          const { m: m2 } = buildMemo();
          m2.editor = null;
          let release2;
          const gate2 = new Promise((resolve) => { release2 = resolve; });
          globalThis.confirm = () => gate2;
          gwApi = async (url) => url === '/admin/api/memo/entries/7'
            ? entryPayload(7, { content: 'server-7', content_version: 3 }) : [];
          m2.openEditor(7);
          await drain(10);
          m2.dispose();
          release2(false);
          await drain(30);
          out.discard_keeps_draft = !!store['qi-memo-editor-draft'];
          out.no_editor_after_discard = m2.editor === null;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["confirm_pending"])
        self.assertTrue(out["no_editor_after_dispose"],
                        "卸载后的恢复确认不得创建旧编辑框")
        self.assertTrue(out["no_save_requests"])
        self.assertTrue(out["draft_not_cleared"])
        self.assertTrue(out["discard_keeps_draft"])
        self.assertTrue(out["no_editor_after_discard"])

    # ── 2026-10-03 新需求确认 ─────────────────────────────────────

    def test_new_memo_defaults_to_pinned_kind(self):
        """新建备忘录默认用途 = 常驻备忘（2026-10-03 确认）。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async () => [];
          m.openEditor(null);
          await drain();
          out.default_kind = m.editor ? m.editor.kind : null;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["default_kind"], "pinned")

    def test_board_section_title_has_no_count_badge(self):
        """板块标题不再显示条数徽标（2026-10-03 确认）；标签详情的
        常驻/随笔分组计数保留。"""
        scenario = r"""
        {
          const { m, body } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          m.tags = [{ id: 1, name: '购物', position: 1 }];
          m.board = { sections: [{ tag: { id: 1, name: '购物', position: 1 },
            note_sort_mode: 'latest', preview: [], items: [],
            pinned_order: [], note_order: [], pinned_count: 2, note_count: 3 }] };
          m.render();
          const sectionHtml = body._html.split('memo-section-title')[1] || '';
          out.no_badge = !sectionHtml.includes('plan-count');
          out.name_kept = body._html.includes('购物');
          // 标签详情分组计数仍在
          m.view = 'tag'; m.activeTagId = 1;
          m.render();
          out.detail_counts_kept = (body._html.match(/plan-count/g) || []).length >= 2;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["no_badge"], "板块标题不应再有 plan-count 徽标")
        self.assertTrue(out["name_kept"])
        self.assertTrue(out["detail_counts_kept"])


class UiConfirmResolveTests(unittest.TestCase):
    """BUG-11：ui.js confirm 的 ×/遮罩必须结算 Promise（取消语义）。"""

    def test_confirm_x_and_mask_resolve_false_real_dom(self):
        quickjs = _try_import_quickjs()
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        ui_src = _ui_source()
        harness = r"""
        const icon = () => '';
        const nodes = [];
        const makeNode = () => {
          const n = {
            _click: null, removed: false, children: [],
            appendChild(c) { this.children.push(c); },
            remove() { this.removed = true; },
            addEventListener() {}, setAttribute() {},
          };
          n.querySelector = (sel) => {
            const stub = { set onclick(fn) { n['click:' + sel] = fn; },
                           get onclick() { return n['click:' + sel]; } };
            return stub;
          };
          return n;
        };
        const modal = (opts) => {
          const mask = makeNode();
          mask._onMaskClose = opts.onMaskClose || null;
          const root = makeNode();
          document.body.appendChild(mask);
          nodes.push({ mask, root, opts });
          return { root, close: () => { mask.removed = true; } };
        };
        const document = { createElement: () => makeNode(), body: makeNode() };
        """
        scenario = r"""
        ;(async () => {
          const out = {};
          // 遮罩：按取消结算
          let settled = 'pending';
          const p = confirm('q', {});
          p.then((v) => { settled = v; });
          const rec = nodes[nodes.length - 1];
          out.mask_wired = typeof rec.mask._onMaskClose === 'function';
          rec.mask._onMaskClose();
          await p;
          out.mask_resolved_false = settled === false;
          out.mask_removed = rec.mask.removed;
          // ×：按取消结算
          settled = 'pending';
          const p2 = confirm('q', {});
          p2.then((v) => { settled = v; });
          const rec2 = nodes[nodes.length - 1];
          out.x_wired = typeof rec2.root['click:.modal-close'] === 'function';
          rec2.root['click:.modal-close']();
          await p2;
          out.x_resolved_false = settled === false;
          out.x_removed = rec2.mask.removed;
          // 确认按钮：resolve(true)
          settled = 'pending';
          const p3 = confirm('q', {});
          p3.then((v) => { settled = v; });
          const rec3 = nodes[nodes.length - 1];
          rec3.root['click:[data-ok]']();
          await p3;
          out.ok_resolved_true = settled === true;
          // 取消按钮：resolve(false)
          settled = 'pending';
          const p4 = confirm('q', {});
          p4.then((v) => { settled = v; });
          const rec4 = nodes[nodes.length - 1];
          rec4.root['click:[data-cancel]']();
          await p4;
          out.cancel_resolved_false = settled === false;
          __result = out;
        })().catch((e) => { __error = String(e && e.stack || e); });
        """
        ctx = quickjs.Context()
        m = re.search(r"(function confirm\(msg.*?\n\})", ui_src, re.S)
        self.assertIsNotNone(m, "ui.js 中必须存在 confirm")
        ctx.eval("var __error = null, __result = null;")
        ctx.eval(harness + "\n" + m.group(1) + "\n" + scenario)
        for _ in range(100000):
            if not ctx.execute_pending_job():
                break
        err = ctx.eval("__error")
        self.assertIsNone(err, f"harness crashed: {err}")
        out = json.loads(ctx.eval("JSON.stringify(__result)"))
        self.assertTrue(out["mask_wired"])
        self.assertTrue(out["mask_resolved_false"], "遮罩必须按取消结算")
        self.assertTrue(out["mask_removed"])
        self.assertTrue(out["x_wired"])
        self.assertTrue(out["x_resolved_false"], "×必须按取消结算")
        self.assertTrue(out["x_removed"])
        self.assertTrue(out["ok_resolved_true"])
        self.assertTrue(out["cancel_resolved_false"])


if __name__ == "__main__":
    unittest.main()
