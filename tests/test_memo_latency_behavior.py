import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "memo_latency_base", Path(__file__).with_name("test_memo_editor_behavior.py"))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)


class MemoLatencyBehaviorTests(unittest.TestCase):
    def run_case(self, scenario):
        quickjs = base._try_import_quickjs()
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        context = quickjs.Context()
        context.eval(base._module_source())
        context.eval(base.HARNESS_PRELUDE + scenario + base.HARNESS_EPILOGUE)
        for _ in range(100000):
            if not context.execute_pending_job():
                break
        self.assertIsNone(context.eval("__error"))
        self.assertNotEqual(context.eval("__result"), None, "scenario must finish")
        return json.loads(context.eval("JSON.stringify(__result)"))

    def test_nonboard_refresh_invalidates_old_board_before_visible_results(self):
        for trigger in ("refreshAfterWrite", "show"):
            for view in ("search", "archived", "trash"):
                for failed_old_read in (False, True):
                    with self.subTest(trigger=trigger, view=view, failed_old_read=failed_old_read):
                        result = self.run_case(r"""
                          const { m, body } = buildMemo();
                          m.view = VIEW; m.searchQuery = 'server';
                          const board = title => ({ sections: [{ tag: null,
                            preview: [entryPayload(1, { display_title: title })],
                            items: [entryPayload(1, { display_title: title })] }] });
                          m.board = board('before-save');
                          m._pendingRefresh = true; m.reorderBusy = true;
                          let resolveOld, rejectOld, resolveFresh, boardReads = 0;
                          gwApi = url => {
                            if (url.endsWith('/tags')) return Promise.resolve([]);
                            if (url.endsWith('/board')) {
                              boardReads++;
                              return boardReads === 1
                                ? new Promise((resolve, reject) => { resolveOld = resolve; rejectOld = reject; })
                                : new Promise(resolve => { resolveFresh = resolve; });
                            }
                            return Promise.resolve([entryPayload(1, { display_title: 'server-new' })]);
                          };
                          const oldRead = m.reload({ silent: true, refreshTags: false });
                          await m.TRIGGER({ refreshTags: false });
                          out.visible = body.innerHTML.includes('server-new');
                          out.deferred = boardReads === 1 && m.boardDirty;
                          if (FAILED) rejectOld(new Error('old read failed')); else resolveOld(board('server-old'));
                          out.oldApplied = await oldRead;
                          out.oldIgnored = m.board.sections[0].preview[0].display_title === 'before-save'
                            && m.boardDirty && m._pendingRefresh && m.reorderBusy;
                          m.switchView('board'); m.render(); await drain();
                          out.waitsForFresh = body.innerHTML.includes('LOADING') && m._pendingRefresh && m.reorderBusy;
                          out.boardReads = boardReads;
                          if (resolveFresh) { resolveFresh(board('server-new')); await drain(); }
                          out.fresh = body.innerHTML.includes('server-new') && !m.boardDirty
                            && !m._pendingRefresh && !m.reorderBusy;
                        """.replace("VIEW", json.dumps(view)).replace("TRIGGER", trigger)
                             .replace("FAILED", str(failed_old_read).lower()))
                        self.assertEqual(result, {"visible": True, "deferred": True, "oldApplied": False,
                                                  "oldIgnored": True, "waitsForFresh": True, "boardReads": 2,
                                                  "fresh": True})

    def test_sort_confirmation_read_cannot_overwrite_a_later_search_save(self):
        for operation in ("mode", "reorder"):
            with self.subTest(operation=operation):
                result = self.run_case(r"""
                  const { m, body } = buildMemo();
                  m.view = 'search'; m.searchQuery = 'server';
                  const board = title => ({ sections: [{ tag: null,
                    preview: [entryPayload(1, { display_title: title })], items: [] }] });
                  m.board = board('before-save');
                  let resolveOld, boardReads = 0;
                  gwApi = (url, options = {}) => {
                    if (options.method) return Promise.resolve({});
                    if (url.endsWith('/board')) {
                      boardReads++;
                      return boardReads === 1 ? new Promise(resolve => { resolveOld = resolve; })
                        : Promise.resolve(board('server-new'));
                    }
                    return Promise.resolve([entryPayload(1, { display_title: 'server-new' })]);
                  };
                  const sorting = OPERATION;
                  await drain();
                  await m.refreshAfterWrite({ refreshTags: false });
                  resolveOld(board('server-old')); await sorting;
                  out.isolated = m.boardDirty && m.board.sections[0].preview[0].display_title === 'before-save';
                  out.locked = m._pendingRefresh && m.reorderBusy;
                  m.switchView('board'); m.render(); await drain();
                  out.fresh = body.innerHTML.includes('server-new') && !m.boardDirty;
                  out.unlocked = !m._pendingRefresh && !m.reorderBusy;
                  out.boardReads = boardReads;
                """.replace("OPERATION", "m.setNoteMode('manual')" if operation == "mode"
                             else "m.commitReorderWrite({ scope: 'tags', order: [] }, 'saved', 'failed')"))
                self.assertEqual(result, {"isolated": True, "locked": True, "fresh": True,
                                          "unlocked": True, "boardReads": 2})

    def test_lifecycle_refresh_does_not_wait_for_hidden_board_and_invalidates_it(self):
        result = self.run_case(r"""
          const { m, body } = buildMemo(), requests = [];
          m.view = 'archived'; m.board = { sections: [{ tag: null, items: [entryPayload(1)] }] };
          gwApi = async url => {
            requests.push(url);
            if (url.includes('status=archived')) return [entryPayload(2, { status: 'archived', display_title: 'server-2' })];
            if (url.endsWith('/board')) return { sections: [{ tag: null, preview: [entryPayload(3, { display_title: 'server-3' })] }] };
            throw new Error('unexpected read ' + url);
          };
          await m.refreshAfterWrite({ refreshTags: false });
          out.visible = body.innerHTML.includes('server-2');
          out.paths = requests.slice(); out.dirty = m.boardDirty;
          m.switchView('board'); m.render();
          out.loading = body.innerHTML.includes('LOADING'); await drain();
          out.fresh = !m.boardDirty && body.innerHTML.includes('server-3');
          out.boardReads = requests.filter(url => url.endsWith('/board')).length;
        """)
        self.assertEqual(result, {"visible": True, "paths": ["/admin/api/memo/entries?status=archived"],
                                  "dirty": True, "loading": True, "fresh": True, "boardReads": 1})

    def test_returning_to_search_does_not_wait_for_tags_or_board(self):
        result = self.run_case(r"""
          const { m, body } = buildMemo(), requests = [];
          m.view = 'search'; m.searchQuery = 'server';
          gwApi = url => {
            requests.push(url);
            return url.includes('q=') ? Promise.resolve([entryPayload(7, { display_title: 'server-7' })]) : new Promise(() => {});
          };
          await m.show();
          out.visible = body.innerHTML.includes('server-7');
          out.paths = requests; out.dirty = m.boardDirty;
        """)
        self.assertTrue(result["visible"] and result["dirty"])
        self.assertEqual(sorted(result["paths"]), ["/admin/api/memo/entries?status=active&q=server", "/admin/api/memo/tags"])

    def test_search_feedback_abort_and_late_results(self):
        result = self.run_case(r"""
          const { m, statusHost } = buildMemo(), requests = [];
          m.view = 'search'; m.searchQuery = 'old';
          gwApi = (url, options) => new Promise(resolve => requests.push({ url, options, resolve }));
          const old = m.runSearch(); out.loading = statusHost.innerHTML.includes('LOADING');
          m.searchQuery = 'new'; const fresh = m.runSearch();
          out.aborted = requests[0].options.signal.aborted;
          requests[1].resolve([entryPayload(2)]); await fresh;
          requests[0].resolve([entryPayload(1)]); await old;
          out.id = m.searchResults[0].id; out.status = statusHost.innerHTML;
        """)
        self.assertEqual(result, {"loading": True, "aborted": True, "id": 2, "status": ""})

    def test_restore_feedback_and_deduplication_cover_read_write_and_refresh(self):
        result = self.run_case(r"""
          const { m } = buildMemo(), requests = [];
          const button = makeEl({ disabled: false }); button.innerHTML = '恢复';
          let resolveWrite, refreshes = 0;
          m.refreshAfterWrite = async () => { refreshes++; };
          gwApi = (url, options = {}) => {
            requests.push({ url, options });
            return options.method ? new Promise(resolve => { resolveWrite = resolve; }) : Promise.resolve(entryPayload(7));
          };
          const first = m.restoreEntry(7, button), duplicate = m.restoreEntry(7, button);
          out.busy = button.disabled && button.textContent === '正在恢复…';
          await drain(); out.calls = requests.length;
          resolveWrite(entryPayload(7)); await Promise.all([first, duplicate]);
          out.refreshes = refreshes; out.restored = !button.disabled && button.innerHTML === '恢复';
        """)
        self.assertEqual(result, {"busy": True, "calls": 2, "refreshes": 1, "restored": True})
