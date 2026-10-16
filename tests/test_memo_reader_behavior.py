"""Execute production memo reads with controlled transport and selection ownership."""
import importlib.util
import json
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "memo_reader_base", Path(__file__).with_name("test_memo_editor_behavior.py"))
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)

READER = r"""
  class AbortController {
    constructor() { this.signal = { aborted: false }; }
    abort() { this.signal.aborted = true; }
  }
  globalThis.AbortController = AbortController;
  const requests = [], presentations = [];
  let selected = null;
  const m = createPlanningMemo({
    beginReader(id) { m.cancelReader(); return selected = { id }; },
    isReaderCurrent: owner => selected === owner,
    showReader(owner, content) {
      if (selected !== owner) return null;
      const nodes = new Map();
      const root = { querySelector(selector) {
        if (!nodes.has(selector)) nodes.set(selector, {});
        return nodes.get(selector);
      }, querySelectorAll: () => [] };
      const reader = { content, root, close() { if (selected === owner) selected = null; } };
      presentations.push(reader);
      return reader;
    },
  });
  m.root = makeEl(); m.body = makeEl(); m.statusHost = makeEl();
  gwApi = (url, options) => new Promise((resolve, reject) => requests.push({ url, options, resolve, reject }));
  const last = () => presentations[presentations.length - 1];
"""


class MemoReaderBehaviorTests(unittest.TestCase):
    def run_case(self, scenario):
        quickjs = base._try_import_quickjs()
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        ctx = quickjs.Context()
        ctx.eval(base._module_source())
        ctx.eval(base.HARNESS_PRELUDE + READER + scenario + base.HARNESS_EPILOGUE)
        for _ in range(100000):
            if not ctx.execute_pending_job():
                break
        self.assertIsNone(ctx.eval("__error"))
        return json.loads(ctx.eval("JSON.stringify(__result)"))

    def test_loading_is_immediate_and_same_entry_pending_is_shared(self):
        out = self.run_case(r"""
          const a = m.openReader(7), b = m.openReader(7);
          out.shared = a === b; out.calls = requests.length;
          out.loading = last().content.html.includes('LOADING');
          requests[0].resolve(entryPayload(7)); await a;
          out.body = last().content.html.includes('server-7');
        """)
        self.assertEqual(out, {"shared": True, "calls": 1, "loading": True, "body": True})

    def test_fast_switch_aborts_previous_and_ignores_late_success(self):
        out = self.run_case(r"""
          const a = m.openReader(7), b = m.openReader(8);
          out.aborted = requests[0].options.signal.aborted;
          requests[1].resolve(entryPayload(8)); await b;
          requests[0].resolve(entryPayload(7)); await a;
          out.body = last().content.html.includes('server-8');
          out.id = selected.id; out.calls = requests.length;
        """)
        self.assertEqual(out, {"aborted": True, "body": True, "id": 8, "calls": 2})

    def test_failed_read_has_retry_and_closing_pending_read_prevents_reopen(self):
        out = self.run_case(r"""
          const a = m.openReader(7);
          requests[0].reject(new Error('offline')); await a;
          out.error = last().content.html.includes('offline');
          last().root.querySelector('[data-reader-retry]').onclick();
          await drain(); out.retry = requests.length === 2;
          const pending = m._readerRequest.promise;
          last().root.querySelector('[data-reader-close]').onclick();
          out.cancelled = requests[1].options.signal.aborted && selected === null;
          const count = presentations.length;
          requests[1].resolve(entryPayload(7)); await pending;
          out.closed = presentations.length === count;
        """)
        self.assertEqual(out, {"error": True, "retry": True, "cancelled": True, "closed": True})

    def test_navigation_and_dispose_abort_without_stale_toast(self):
        out = self.run_case(r"""
          const a = m.openReader(7); m.flushPending();
          requests[0].reject(new Error('cancelled')); await a;
          const b = m.openReader(8); m.dispose();
          requests[1].resolve(entryPayload(8)); await b;
          out.aborted = requests.every(r => r.options.signal.aborted);
          out.toasts = toasts.length; out.root = m.root;
        """)
        self.assertEqual(out, {"aborted": True, "toasts": 0, "root": None})

    def test_reader_lifecycle_reuses_displayed_version_and_skips_tag_refresh(self):
        out = self.run_case(r"""
          let refresh;
          m.refreshAfterWrite = async options => { refresh = options; };
          const write = m.lifecycleEntry(7, 'archive', null, 9);
          out.calls = requests.length;
          out.path = requests[0].url;
          out.version = JSON.parse(requests[0].options.body).expected_version;
          requests[0].resolve(entryPayload(7, { status: 'archived', content_version: 10 }));
          await write; out.refreshTags = refresh.refreshTags;
        """)
        self.assertEqual(out, {"calls": 1, "path": "/admin/api/memo/entries/7/archive",
                               "version": 9, "refreshTags": False})

    def test_dismiss_pending_aborts_but_dismiss_loaded_keeps_reader_actions(self):
        out = self.run_case(r"""
          const a = m.openReader(7);
          out.pending = m.cancelPendingReader();
          out.aborted = requests[0].options.signal.aborted;
          requests[0].resolve(entryPayload(7)); await a;
          const b = m.openReader(8);
          requests[1].resolve(entryPayload(8)); await b;
          out.loaded = m.cancelPendingReader();
          out.current = m._readerCurrent();
          m.dispose(); await m.openReader(9);
          out.calls = requests.length;
        """)
        self.assertEqual(out, {"pending": True, "aborted": True, "loaded": False,
                               "current": True, "calls": 2})

    def test_dismiss_saved_refresh_ignores_late_success_and_keeps_loaded_reader(self):
        out = self.run_case(r"""
          const read = m.openReader(7);
          requests[0].resolve(entryPayload(7)); await read;
          m.refreshReader(7);
          const count = presentations.length;
          out.resetSelection = m.cancelPendingReader();
          out.aborted = requests[1].options.signal.aborted;
          requests[1].resolve(entryPayload(7, { content: 'late-saved' }));
          await drain();
          out.noReopen = presentations.length === count;
          out.current = !!m._readerCurrent();
          out.id = selected.id;
          m.refreshReader(7);
          requests[2].resolve(entryPayload(7, { content: 'next-saved' }));
          await drain();
          out.nextRefresh = last().content.html.includes('next-saved');
        """)
        self.assertEqual(out, {"resetSelection": False, "aborted": True,
                               "noReopen": True, "current": True, "id": 7,
                               "nextRefresh": True})

    def test_dismiss_saved_refresh_ignores_late_failure_and_preserves_new_selection(self):
        out = self.run_case(r"""
          const read = m.openReader(7);
          requests[0].resolve(entryPayload(7)); await read;
          m.refreshReader(7); m.cancelPendingReader();
          const next = m.openReader(8);
          requests[2].resolve(entryPayload(8)); await next;
          requests[1].reject(new Error('late-failure')); await drain();
          out.id = selected.id;
          out.body = last().content.html.includes('server-8');
          out.toasts = toasts.length;
          out.aborted = requests[1].options.signal.aborted;
        """)
        self.assertEqual(out, {"id": 8, "body": True, "toasts": 0, "aborted": True})
