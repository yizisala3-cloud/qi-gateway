"""2026-10-04 修复轮回归：BUG-03/05/06/08/14 + M19 回收站保留期展示。

被测对象是 ``admin/js/lib/planning_memo.js`` 的真实源码（quickjs 执行，
imports 剥离后注入受控假件，复用 test_memo_editor_behavior 的夹具骨架）：

- BUG-03 会话身份升级：新建首次保存拿到真实 id 后，明确丢弃的修改在
  关闭时同样清槽，重开不再恢复已丢弃正文、不再自动重提；
- BUG-03 反馈结算：keepalive 成功且无新输入时，bfcache 返回后状态是
  「已保存」，不再停留「待保存」，也不发第二个同版本请求；
- BUG-05 动态排空：完成等待期间新增的标签操作也被关闭流程等待并随
  保存提交；迟到落定只作用于本会话，不给新编辑器发额外保存；
- BUG-06 队列空闲：第二项在途时第三项必须排队，最终按最后有效选择
  提交；写请求网络失败 = 提交结果未知，保持缓存不可用并暂停拖动；
- BUG-08 重返刷新：归档读取在途时切出再返回，show() 按当前视图重发
  读取，不再停留「正在载入」；
- BUG-14 重试收尾：刷新重试失败恢复按钮与提示、可重复点击，成功才
  清提示并解锁拖动；
- M19 展示：回收站条目按 72 小时保留期显示剩余时间，到期显示即将清除。

所有断言基于真实执行的模块代码，不接受「源码包含字符串」式替代。
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

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


class MemoBugfix20261004Tests(unittest.TestCase):
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

    # ── BUG-03：会话身份升级与保存反馈 ────────────────────────────

    def test_bug03_explicit_discard_after_create_id_transition(self):
        """新建 A 首次保存成功（获得 id 42）后继续输入 B，PATCH 失败并
        明确「丢弃并关闭」：草稿槽按升级后的会话身份清掉，重开 42 只见
        服务端内容，不再恢复 B、不再自动重提（网络恢复也不覆盖 A）。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async () => [];
          m.openEditor(null);
          await drain();
          setContent('A的正文'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.post_sent = fetches.length === 1
              && fetches[0].options.method === 'POST';
          fetches[0].resolve({ ok: true, json: async () => ({
            id: 42, title: null, content: 'A的正文', kind: 'pinned',
            content_version: 1, tags: [],
            created_at: '2026-10-04T09:00:00+08:00',
            updated_at: '2026-10-04T09:00:00+08:00',
            archived_at: null, deleted_at: null }) });
          await drain();
          out.id_upgraded = m.editor.entryId === 42;
          // 继续输入 B：落槽身份已是真实 id
          setContent('明确丢弃的B'); m.markEditorDirty();
          clock.tick(800); await drain();
          const draftNow = JSON.parse(store['qi-memo-editor-draft'] || 'null');
          out.draft_tracks_real_id = !!draftNow && draftNow.entryId === 42;
          out.patch_sent = fetches.length === 2
              && fetches[1].options.method === 'PATCH';
          fetches[1].reject(new Error('network lost'));
          await drain();
          out.status_error = m._saveState.status === 'error';
          confirmResult = true;   // 明确「丢弃并关闭」
          const closing = m.requestEditorClose();
          await drain(20);
          if (fetches[2]) fetches[2].reject(new Error('still down'));
          await closing;
          await drain();
          out.closed = m.editor === null;
          out.draft_cleared = !store['qi-memo-editor-draft'];
          // 重开 42：无草稿恢复、显示服务端内容，网络「恢复」也不重提
          gwApi = async (url) => url === '/admin/api/memo/entries/42'
            ? { id: 42, title: null, content: 'A的正文', kind: 'pinned',
                status: 'active', content_version: 1, tags: [],
                created_at: '2026-10-04T09:00:00+08:00',
                updated_at: '2026-10-04T09:00:00+08:00',
                archived_at: null, deleted_at: null } : [];
          m.openEditor(42);
          await drain();
          out.server_content_shown = m.editor
              && m.editor.modal.root.fields['[data-editor-content]'].value === 'A的正文';
          clock.tick(2000); await drain();
          out.no_resubmit = fetches.length === 3;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["post_sent"])
        self.assertTrue(out["id_upgraded"])
        self.assertTrue(out["draft_tracks_real_id"])
        self.assertTrue(out["patch_sent"])
        self.assertTrue(out["status_error"])
        self.assertTrue(out["closed"])
        self.assertTrue(out["draft_cleared"],
                        "明确丢弃必须在身份升级后仍然清掉本会话草稿")
        self.assertTrue(out["server_content_shown"])
        self.assertTrue(out["no_resubmit"],
                        "已丢弃正文不得在重开后自动重提")

    def test_bug03_keepalive_success_settles_saved_after_resume(self):
        """keepalive 成功且无新输入：bfcache 返回后保存反馈是「已保存」，
        不停留「待保存」，也不再发第二个同版本请求。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          setContent('卸载前输入'); m.markEditorDirty();
          m.editor.autosave.flush({ keepalive: true });
          out.pending_before_hide = m._saveState.status === 'pending';
          out.busy_inflight = m.editor.autosave.isBusy();
          // 页面冻结期间 keepalive 成功；随后 bfcache 返回
          fetches[0].resolve({ ok: true, json: async () => ({
            id: 7, title: null, content: '卸载前输入', kind: 'note',
            content_version: 2, tags: [] }) });
          await drain();
          m.editor.autosave.resume();
          await drain();
          out.status_saved = m._saveState.status === 'saved';
          out.busy_clear = !m.editor.autosave.isBusy();
          clock.tick(2000); await drain();
          out.no_second_request = fetches.length === 1;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["pending_before_hide"])
        self.assertTrue(out["busy_inflight"])
        self.assertTrue(out["status_saved"],
                        "keepalive 成功后 resume 的反馈必须是已保存")
        self.assertTrue(out["busy_clear"])
        self.assertTrue(out["no_second_request"])

    # ── BUG-05：关闭排空动态新增标签操作 ──────────────────────────

    def test_bug05_close_drains_tag_added_during_wait(self):
        """添加标签一后点完成，等待期间继续添加标签二：关闭流程循环排空，
        两个标签都随保存提交；只有一次关闭决策，不留孤立标签。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          let release1; let release2;
          const gate1 = new Promise((r) => { release1 = r; });
          const gate2 = new Promise((r) => { release2 = r; });
          let tagCount = 0;
          const createdTags = [];
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/tags' && opts && opts.method === 'POST') {
              tagCount += 1;
              const n = tagCount;
              await (n === 1 ? gate1 : gate2);
              const resp = { id: n === 1 ? 5 : 6, name: n === 1 ? '标签一' : '标签二',
                             position: n, existed: false };
              createdTags.push(resp);
              return resp;
            }
            return url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          };
          m.openEditor(7);
          await drain();
          editorFields()['[data-new-tag-name]'].value = '标签一';
          m.addEditorTag();
          const closing = m.requestEditorClose();
          await drain();
          out.not_closed_yet = !!m.editor && confirmCalls.length === 0;
          // 完成等待期间继续添加标签二
          editorFields()['[data-new-tag-name]'].value = '标签二';
          m.addEditorTag();
          out.two_pending = m.editor.pendingOps.size === 2;
          release1();
          await drain(10);
          // 标签一落定，标签二仍在途：关闭流程必须继续等待
          out.still_waiting = !!m.editor && confirmCalls.length === 0;
          release2();
          await drain(10);
          const patch = fetches.find((f) => f.url === '/admin/api/memo/entries/7');
          out.patch_has_both = !!patch
              && (patch.body.tag_ids || []).includes(5)
              && patch.body.tag_ids.includes(6);
          patch.resolve({ ok: true, json: async () => ({
            id: 7, title: null, content: 'server-7', kind: 'note',
            content_version: 2,
            tags: [{ id: 5, name: '标签一' }, { id: 6, name: '标签二' }] }) });
          await closing;
          await drain();
          out.closed = m.editor === null;
          out.no_confirm = confirmCalls.length === 0;
          out.tags_registered = createdTags.length === 2;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["not_closed_yet"])
        self.assertTrue(out["two_pending"])
        self.assertTrue(out["still_waiting"],
                        "动态新增的标签操作必须被关闭流程继续等待")
        self.assertTrue(out["patch_has_both"], "后加标签必须随保存提交")
        self.assertTrue(out["closed"])
        self.assertTrue(out["no_confirm"])
        self.assertTrue(out["tags_registered"])

    def test_bug05_late_tag_callback_does_not_touch_new_editor(self):
        """编辑器关闭后标签创建才落定：迟到回调只作用于本会话，不给新
        编辑器标记脏、不发额外保存。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          let release;
          const gate = new Promise((r) => { release = r; });
          const tagResponse = { id: 5, name: '迟到标签', position: 1, existed: false };
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/tags' && opts && opts.method === 'POST') {
              await gate;
              return tagResponse;
            }
            return url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          };
          m.openEditor(7);
          await drain();
          editorFields()['[data-new-tag-name]'].value = '迟到标签';
          const op = m.addEditorTag();
          // 不等落定直接丢弃会话（硬卸载等价状态），另开记录 8
          m.editor = null;
          modals.pop();
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/tags' && opts && opts.method === 'POST') {
              await gate;
              return tagResponse;
            }
            return url === '/admin/api/memo/entries/8'
              ? entryPayload(8, { content: 'server-8' }) : [];
          };
          m.openEditor(8);
          await drain();
          out.editor8_open = m.editor && m.editor.entryId === 8;
          const fetchCountBefore = fetches.length;
          release();
          await drain(20);
          await op.catch(() => {});
          clock.tick(2000); await drain();
          out.no_extra_save = fetches.length === fetchCountBefore;
          out.status_not_pending = m._saveState.status !== 'pending';
          out.editor8_intact = m.editor && m.editor.entryId === 8;
          out.editor8_draft_untouched = !(store['qi-memo-editor-draft'] || '')
              .includes('server-8');
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["editor8_open"])
        self.assertTrue(out["no_extra_save"],
                        "迟到标签回调不得给新编辑器发保存")
        self.assertTrue(out["status_not_pending"])
        self.assertTrue(out["editor8_intact"])

    # ── BUG-06：队列空闲与提交结果未知 ────────────────────────────

    def test_bug06_third_write_queues_behind_inflight_second(self):
        """第一项完成后第二项仍在途：第三次模式选择必须排队，不越过在途
        写入直接派发；最终按点击顺序 manual→latest→manual 提交。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.activeTagId = 1;
          const modes = () => gwCalls
            .filter((c) => c.url === '/admin/api/memo/note-mode')
            .map((c) => JSON.parse(c.opts.body).mode);
          let release2;
          const gate2 = new Promise((r) => { release2 = r; });
          let modeCalls = 0;
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/note-mode') {
              modeCalls += 1;
              if (modeCalls === 2) await gate2;   // 第二项长时间在途
              return [];
            }
            return url === '/admin/api/memo/board' ? { sections: [] } : [];
          };
          m.setNoteMode('manual');
          m.setNoteMode('latest');
          await drain(10);            // 第一项完成，第二项在途
          out.second_inflight = modes().length === 2;
          m.setNoteMode('manual');    // 第三次点击：必须排队
          await drain(5);
          out.third_queued = modes().length === 2;
          release2();
          await drain(40);
          out.final_modes = modes().join(',');
          out.queue_drained = m._writeIdle === true
              && m.reorderBusy === false;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["second_inflight"])
        self.assertTrue(out["third_queued"],
                        "队列未排空时第三次写入不得直接派发")
        self.assertEqual(out["final_modes"], "manual,latest,manual")
        self.assertTrue(out["queue_drained"])

    def test_bug06_reorder_network_failure_keeps_cache_unavailable(self):
        """重排请求网络失败 = 提交结果未知：不按「服务端未变更」恢复视图，
        保持拖动暂停并给出重试入口；下一次拖动被阻止。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          const section = { tag: { id: 1, name: '购物', position: 1 },
            note_sort_mode: 'latest', preview: [], items: [],
            pinned_order: [11, 12], note_order: [], pinned_count: 2, note_count: 0 };
          m.tags = [{ id: 1, name: '购物', position: 1 }];
          m.board = { sections: [section] };
          gwApi = async (url) => {
            if (url === '/admin/api/memo/reorder') {
              return Promise.reject(new TypeError('Failed to fetch'));
            }
            return url === '/admin/api/memo/board'
              ? { sections: [section] }
              : url === '/admin/api/memo/tags' ? m.tags : [];
          };
          const drag = { dataset: { entryId: '12', kind: 'pinned' } };
          const list = {
            dataset: { dragList: 'entries' },
            closest: (sel) => sel === '.memo-section' ? { dataset: { tagId: '1' } } : null,
            querySelectorAll: () => [drag],
          };
          const reorderCalls = () => gwCalls
            .filter((c) => c.url === '/admin/api/memo/reorder').length;
          m.commitEntryOrder(list, drag);
          await drain(40);
          out.busy_held = m.reorderBusy === true;
          out.retry_notice = String(m.statusHost._html).includes('刷新重试');
          out.no_assumed_clean_reload = !gwCalls.some(
            (c) => c.url === '/admin/api/memo/board');
          m.commitEntryOrder(list, drag);
          out.second_blocked = reorderCalls() === 1;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["busy_held"])
        self.assertTrue(out["retry_notice"])
        self.assertTrue(out["no_assumed_clean_reload"],
                        "提交结果未知时不得立即按未变更恢复视图")
        self.assertTrue(out["second_blocked"])

    # ── BUG-08：页签重返刷新当前视图 ──────────────────────────────

    def test_bug08_show_rereads_inflight_archived_view(self):
        """归档读取在途时切出再返回（真实路径 memo.show()）：旧响应被
        失效后必须重发当前视图读取，页面不再停留「正在载入」。"""
        scenario = r"""
        {
          const { m, body } = buildMemo();
          let release;
          const gate = new Promise((r) => { release = r; });
          let archivedCalls = 0;
          const archivedList = [{ id: 9, kind: 'note',
            display_title: '归档九', body_excerpt: '', tags: [],
            archived_at: '2026-10-03T09:00:00+08:00', deleted_at: null }];
          gwApi = async (url) => {
            if (url.includes('status=archived')) {
              archivedCalls += 1;
              if (archivedCalls === 1) await gate;   // 第一次读取长时间在途
              return archivedList;
            }
            return url === '/admin/api/memo/board'
              ? { sections: [] } : url === '/admin/api/memo/tags' ? [] : [];
          };
          m.switchView('archived');
          const first = m.renderLifecycleList('archived');
          await drain(5);
          out.first_pending = archivedCalls === 1
              && String(body._html).includes('LOADING');
          // 切到其他页签再返回备忘录：planning.switchTab → memo.show()
          const showing = m.show();
          await drain(10);
          release();                 // 迟到的旧响应
          await first.catch(() => {});
          await showing;
          await drain(10);
          out.reread_issued = archivedCalls === 2;
          out.rendered = String(body._html).includes('归档九');
          out.not_stuck_loading = !String(body._html).includes('LOADING');
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["first_pending"])
        self.assertTrue(out["reread_issued"],
                        "重返必须按当前视图重发读取")
        self.assertTrue(out["rendered"])
        self.assertTrue(out["not_stuck_loading"])

    # ── BUG-14：刷新重试失败后按钮恢复 ────────────────────────────

    def test_bug14_retry_button_recovers_after_failure(self):
        """刷新重试再次失败：按钮与提示恢复、可重复点击；第三次成功后
        清提示并解锁拖动。"""
        scenario = r"""
        {
          const { m, statusHost } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          const section = { tag: { id: 1, name: '购物', position: 1 },
            note_sort_mode: 'latest', preview: [], items: [],
            pinned_order: [11, 12], note_order: [], pinned_count: 2, note_count: 0 };
          m.tags = [{ id: 1, name: '购物', position: 1 }];
          m.board = { sections: [section] };
          let boardOk = false;
          let reorderOk = true;
          gwApi = async (url) => {
            if (url === '/admin/api/memo/reorder') {
              return reorderOk ? [] : Promise.reject(new TypeError('net'));
            }
            return url === '/admin/api/memo/board'
              ? (boardOk ? { sections: [section] }
                         : Promise.reject(new Error('down')))
              : url === '/admin/api/memo/tags' ? m.tags : [];
          };
          const drag = { dataset: { entryId: '12', kind: 'pinned' } };
          const list = {
            dataset: { dragList: 'entries' },
            closest: (sel) => sel === '.memo-section' ? { dataset: { tagId: '1' } } : null,
            querySelectorAll: () => [drag],
          };
          // 用可追踪的最新按钮假件替换 statusHost.querySelector：
          // 真实 DOM 每次重渲染都是新按钮
          let lastBtn = null;
          statusHost.querySelector = (sel) => {
            lastBtn = { disabled: false, onclick: null };
            return lastBtn;
          };
          m.commitEntryOrder(list, drag);
          await drain(40);          // 重排成功但刷新失败 → 重试入口
          out.notice_shown = String(statusHost._html).includes('刷新重试');
          out.button_enabled = !lastBtn.disabled;
          // 第一次重试：仍失败 → 按钮恢复、提示保留
          await lastBtn.onclick();
          await drain(20);
          out.notice_kept_1 = String(statusHost._html).includes('刷新重试');
          out.reenabled_1 = !lastBtn.disabled;
          out.busy_held = m.reorderBusy === true;
          // 第二次重试：仍失败 → 仍可重试
          await lastBtn.onclick();
          await drain(20);
          out.reenabled_2 = !lastBtn.disabled;
          // 第三次重试：刷新成功 → 提示清除、拖动解锁
          boardOk = true;
          await lastBtn.onclick();
          await drain(40);
          out.notice_cleared = String(statusHost._html) === '';
          out.released = m.reorderBusy === false && m._pendingRefresh === false;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["notice_shown"])
        self.assertTrue(out["button_enabled"])
        self.assertTrue(out["notice_kept_1"])
        self.assertTrue(out["reenabled_1"],
                        "重试失败后按钮必须恢复可点击")
        self.assertTrue(out["busy_held"])
        self.assertTrue(out["reenabled_2"],
                        "连续失败后仍必须可重试")
        self.assertTrue(out["notice_cleared"])
        self.assertTrue(out["released"])

    # ── M19：回收站保留期展示 ─────────────────────────────────────

    def test_m19_trash_list_shows_remaining_time(self):
        """回收站条目按 72 小时保留期显示剩余时间；已到期显示即将清除。"""
        scenario = r"""
        {
          const { m, body } = buildMemo();
          const hoursAgo = (h) => new Date(Date.now() - h * 3600 * 1000).toISOString();
          const items = [
            { id: 1, kind: 'note', display_title: '快到期', body_excerpt: '',
              tags: [], archived_at: null, deleted_at: hoursAgo(71) },
            { id: 2, kind: 'note', display_title: '已到期', body_excerpt: '',
              tags: [], archived_at: null, deleted_at: hoursAgo(73) },
          ];
          gwApi = async (url) => url.includes('status=deleted')
            ? items
            : (url === '/admin/api/memo/board' ? { sections: [] }
               : url === '/admin/api/memo/tags' ? [] : []);
          m.view = 'trash';
          await m.renderLifecycleList('deleted');
          const html = String(body._html);
          out.kept_remaining = html.includes('约 1 小时后清除');
          out.expired_hint = html.includes('已到期，即将清除');
          out.empty_hint_kept = true;
          m.body = body;
          const items2 = [];
          gwApi = async (url) => url.includes('status=deleted')
            ? items2
            : (url === '/admin/api/memo/board' ? { sections: [] }
               : url === '/admin/api/memo/tags' ? [] : []);
          await m.renderLifecycleList('deleted');
          out.empty_hint_72h = String(body._html).includes('保留 72 小时');
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["kept_remaining"], "未到期条目必须显示剩余时间")
        self.assertTrue(out["expired_hint"], "已到期条目必须显示即将清除")
        self.assertTrue(out["empty_hint_72h"], "回收站空态文案应说明保留期")


if __name__ == "__main__":
    unittest.main()
