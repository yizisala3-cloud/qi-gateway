"""备忘录编辑器与列表行为的 quickjs 真实执行测试（F01–F05、F08–F13）。

被测对象是 ``admin/js/lib/planning_memo.js`` 的真实源码（imports 剥离后注入
受控 gw/fetch/DOM/localStorage 假件），把审查报告的缺陷复现固化为回归：

- F01 编辑器身份：旧会话的迟到保存响应不改写新会话；连点打开只建一个编辑器；
- F02 首次创建衔接：创建在途的后续输入 PATCH 同一条记录；幂等重试取得
  首次记录后继续提交最新草稿，完成后才「已保存」；
- F03 卸载竞争与草稿恢复：在途保存存在时不并发 keepalive；最新草稿落
  localStorage 并在重开时恢复（版本已推进时走冲突确认，不静默覆盖）；
- F04 关闭流程：遮罩与按钮走同一保存/确认/清理流程，失败可确认丢弃或返回；
- F05 板块拖动标识：板块节点携带 data-drag-item，未分类不可拖；顺序收集
  只取对应列表的直接成员；
- F08 清空正文：未发送的创建快照被撤下；在途创建被清空后清理记录并不误报；
- F09/F10/F12 列表刷新：恢复按正确 status 重读；视图切换使旧响应失效；
  搜索中的写入按当前关键词重查；
- F11 连续拖动：重排在途时不开新拖动；
- F13 空标签删除入口：仅剩归档/回收站内容的标签也能从看板空态删除。

所有断言基于真实执行的模块代码与保存泵，不接受「源码包含字符串」式替代。
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MEMO_MODULE = ROOT / "admin" / "js" / "lib" / "planning_memo.js"


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


HARNESS_PRELUDE = r"""
var __result = null, __error = null;
(async () => {
  const out = {};

  // ── 基础假件 ──────────────────────────────────────────────────
  const drain = async (n = 40) => { for (let i = 0; i < n; i++) await Promise.resolve(); };

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
  const clock = makeClock();

  let cridSeq = 0;
  const crypto = { randomUUID: () => 'crid-' + (++cridSeq) };

  const store = {};
  const localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
  };

  const toasts = [];
  const toast = (msg, type) => toasts.push({ msg, type: type || 'ok' });

  // ui.js 展示函数的最小假件（渲染输出仍可被字符串断言）
  const icon = () => '';
  const tag = (name) => `<span class="tag">${name}</span>`;
  const loading = () => '<div class="loading">LOADING</div>';
  const empty = (t, s) => `<div class="empty">${t}${s ? `<p>${s}</p>` : ''}</div>`;
  const errorBlock = (s) => `<div class="error">${s}</div>`;

  let confirmResult = true;
  const confirmCalls = [];
  const confirm = (msg) => { confirmCalls.push(msg); return Promise.resolve(confirmResult); };

  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // 共享 modal 假件：从 body 模板解析标题/正文预填值（与浏览器渲染一致）
  const modals = [];
  const makeField = (value) => ({
    value: value || '',
    handlers: {},
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
    set onclick(fn) { this.handlers.click = fn; },
    get onclick() { return this.handlers.click; },
  });
  const makePlainEl = () => ({
    innerHTML: '',
    style: {},
    setAttribute() {},
    addEventListener() {},
    querySelector: () => makePlainEl(),
    querySelectorAll: () => [],
  });
  const modal = (opts) => {
    const titleMatch = /data-editor-title maxlength="200" value="([^"]*)"/.exec(opts.body || '');
    const contentMatch = /<textarea id="memo-editor-content"[^>]*>([\s\S]*?)<\/textarea>/
      .exec(opts.body || '');
    const decode = (s) => String(s || '')
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
    const fields = {
      '[data-editor-kind]': makePlainEl(),
      '[data-editor-tags]': { innerHTML: '', querySelectorAll: () => [] },
      '[data-editor-title]': makeField(decode(titleMatch ? titleMatch[1] : '')),
      '[data-editor-content]': makeField(decode(contentMatch ? contentMatch[1] : '')),
      '[data-save-status]': { innerHTML: '', querySelector: () => null },
      '[data-new-tag-name]': makeField(''),
      '[data-editor-add-tag]': makeField(),
      '[data-editor-close]': makeField(),
      '.modal-close': makeField(),
      'button': makePlainEl(),
    };
    const root = {
      fields,
      querySelector: (sel) => fields[sel] || makePlainEl(),
      querySelectorAll: () => [],
    };
    const record = { opts, root, closed: false };
    record.close = () => { record.closed = true; };
    modals.push(record);
    return { root, close: record.close };
  };

  // gw 队列（模块数据面）
  let gwApi = async () => [];
  const gwCalls = [];
  const gw = (...args) => { gwCalls.push({ url: args[0], opts: args[1] || null }); return gwApi(...args); };

  // fetch 队列（编辑器保存面）；url 统一去掉 origin，断言只比较路径
  const fetches = [];
  let fetchQueue = [];
  const fetch = (rawUrl, options) => new Promise((resolve, reject) => {
    const url = String(rawUrl).replace(window.location.origin, '');
    const item = {
      url, options,
      body: options.body ? JSON.parse(options.body) : null,
      resolve, reject,
    };
    fetches.push(item);
    fetchQueue.push(item);
  });

  const retroSelect = (host, opts) => ({
    value: opts.value,
    addEventListener() {},
  });

  // DOM 假件（页面容器）
  const makeEl = (props = {}) => ({
    _html: '',
    style: {},
    dataset: {},
    handlers: {},
    ...props,
    set innerHTML(v) { this._html = String(v); },
    get innerHTML() { return this._html; },
    setAttribute() {},
    addEventListener(ev, fn) { this.handlers[ev] = fn; },
    querySelector: () => makeEl(),
    querySelectorAll: () => [],
  });

  const buildMemo = () => {
    const memoRoot = makeEl();
    const body = makeEl();
    const statusHost = makeEl();
    const filterHost = makeEl();
    const searchInput = makeField('');
    memoRoot.querySelector = (sel) => ({
      '[data-memo-body]': body,
      '[data-memo-status]': statusHost,
      '[data-memo-filter]': filterHost,
      '[data-memo-search]': searchInput,
    }[sel] || makeEl());
    const m = createPlanningMemo();
    m.root = memoRoot;
    m.body = body;
    m.statusHost = statusHost;
    m._harness = { filterHost, searchInput, memoRoot, body, statusHost };
    return { m, body, statusHost };
  };

  const editorFields = () => modals[modals.length - 1].root.fields;
  const setContent = (v) => { editorFields()['[data-editor-content]'].value = v; };
  const setTitle = (v) => { editorFields()['[data-editor-title]'].value = v; };

  const entryPayload = (id, extra = {}) => ({
    id, title: null, content: 'server-' + id, kind: 'note', status: 'active',
    content_version: 1, tags: [], created_at: '2026-10-02T09:00:00+08:00',
    updated_at: '2026-10-02T09:00:00+08:00',
    archived_at: null, deleted_at: null, ...extra,
  });

  // ══════════════════════════════════════════════════════════════
  // 模块源码在独立 eval 中解析，跨脚本只能看到 globalThis——
  // 假件在此统一挂为全局（模块函数体在调用时才解析这些标识符）。
  const window = {
    location: { origin: 'http://test' },
    listeners: {},
    addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); },
    removeEventListener(name, fn) {
      this.listeners[name] = (this.listeners[name] || []).filter((f) => f !== fn);
    },
  };
  Object.assign(globalThis, {
    icon, tag, loading, empty, errorBlock, toast, confirm, modal, esc,
    createRetroSelectField: retroSelect, renderMarkdown: (s) => s || '',
    gw, localStorage, crypto, fetch, window,
    setTimeout: clock.schedule.bind(clock), clearTimeout: clock.cancel.bind(clock),
  });
"""

HARNESS_EPILOGUE = """
  return out;
})().then((v) => { __result = v; }).catch((e) => { __error = String(e && e.stack || e); });
"""


class MemoEditorBehaviorTests(unittest.TestCase):
    """quickjs 真实执行 planning_memo.js：编辑器生命周期与列表行为。"""

    @classmethod
    def setUpClass(cls):
        cls.quickjs = _try_import_quickjs()

    def _run(self, scenario):
        quickjs = self.quickjs
        if quickjs is None:
            self.skipTest("quickjs is not installed")
        ctx = quickjs.Context()
        ctx.eval(_module_source())
        # prelude + 场景 + 收尾必须一次性求值（三者共同构成一个可解析单元）
        ctx.eval(HARNESS_PRELUDE + scenario + HARNESS_EPILOGUE)
        for _ in range(100000):
            if not ctx.execute_pending_job():
                break
        err = ctx.eval("__error")
        self.assertIsNone(err, f"harness crashed: {err}")
        return json.loads(ctx.eval("JSON.stringify(__result)"))

    # ── F01：编辑器身份与异步隔离 ─────────────────────────────────

    def test_f01_stale_save_response_does_not_rewrite_new_editor(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/42' ? entryPayload(42) : [];
          m.openEditor(42);
          await drain();
          out.a_open = !!m.editor && m.editor.entryId === 42;
          setContent('A的正文'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.a_sent = fetches.length === 1;

          // A 的保存仍在途时编辑会话被替换（dispose/关闭路径的等价状态）
          m.editor = null;
          modals.pop();
          gwApi = async (url) => url === '/admin/api/memo/entries/7'
            ? entryPayload(7, { content: 'B原文' }) : [];
          m.openEditor(7);
          await drain();
          out.b_open = !!m.editor && m.editor.entryId === 7;

          // A 的迟到响应到达：不得改写 B 的身份
          fetches[0].resolve({ ok: true, json: async () => ({
            id: 42, content_version: 2, content: 'A的正文', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          out.b_identity_intact = m.editor.entryId === 7
              && m.editor.version === 1
              && m.editor.crid === null;

          // B 继续编辑：必须 PATCH /entries/7，而不是把 B 的内容存进 A
          setContent('B的最新正文'); m.markEditorDirty();
          clock.tick(800); await drain();
          const save = fetches[fetches.length - 1];
          out.b_patch_url = save.url;
          out.b_patch_version = save.body.expected_version;
          out.b_patch_content = save.body.content;
          save.resolve({ ok: true, json: async () => ({
            id: 7, content_version: 2, content: 'B的最新正文', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          out.b_final_status = m._saveState.status;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["a_open"])
        self.assertTrue(out["b_open"])
        self.assertTrue(out["b_identity_intact"],
                        "迟到响应不得改写新编辑器的 id/version/crid")
        self.assertEqual(out["b_patch_url"], "/admin/api/memo/entries/7")
        self.assertEqual(out["b_patch_version"], 1)
        self.assertEqual(out["b_patch_content"], "B的最新正文")
        self.assertEqual(out["b_final_status"], "saved")

    def test_f01_double_open_builds_single_editor(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          gwApi = async (url) => url === '/admin/api/memo/entries/9' ? entryPayload(9) : [];
          m.openEditor(9);
          m.openEditor(9);      // 打开在途时连点
          await drain();
          m.openEditor(9);      // 打开后再点（单实例守卫）
          await drain();
          out.modals = modals.length;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["modals"], 1, "连点打开只建一个编辑器")

    # ── F02：首次创建与后续自动保存衔接 ───────────────────────────

    def test_f02_input_during_first_create_patches_same_record(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          m.openEditor(null);
          await drain();
          setContent('首版'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.first_is_post = fetches[0].options.method === 'POST';
          out.first_crid = fetches[0].body.client_request_id;
          // 首次创建在途时继续输入
          setContent('首版+续写'); m.markEditorDirty();
          // 首次创建返回（完整序列化记录）
          fetches[0].resolve({ ok: true, json: async () => ({
            id: 7, content_version: 1, content: '首版', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          // 后续快照必须 PATCH 同一条记录，而不是第二次 POST
          out.calls = fetches.length;
          out.second_method = fetches[1] ? fetches[1].options.method : null;
          out.second_url = fetches[1] ? fetches[1].url : null;
          out.second_content = fetches[1] ? fetches[1].body.content : null;
          fetches[1].resolve({ ok: true, json: async () => ({
            id: 7, content_version: 2, content: '首版+续写', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          out.final_status = m._saveState.status;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["first_is_post"])
        self.assertEqual(out["first_crid"], "crid-1")
        self.assertEqual(out["calls"], 2, "创建在途的输入接续到同一条记录")
        self.assertEqual(out["second_method"], "PATCH")
        self.assertEqual(out["second_url"], "/admin/api/memo/entries/7")
        self.assertEqual(out["second_content"], "首版+续写")
        self.assertEqual(out["final_status"], "saved")

    def test_f02_idempotent_retry_continues_with_latest_draft(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          m.openEditor(null);
          await drain();
          setContent('first'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.first_crid = fetches[0].body.client_request_id;
          // 续写后传输丢失首次响应
          setContent('latest'); m.markEditorDirty();
          fetches[0].reject(new Error('network response lost'));
          await drain();
          out.error_status = m._saveState.status;
          // 幂等重试：同 crid，携带最新草稿
          m.editor.autosave.retry();
          await drain();
          out.retry_crid = fetches[1].body.client_request_id;
          out.retry_content = fetches[1].body.content;
          // 服务端返回首次记录（内容不是最新草稿）
          fetches[1].resolve({ ok: true, json: async () => ({
            id: 7, content_version: 1, content: 'first', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          // 前端必须继续把最新草稿 PATCH 上去，不能误报已保存
          out.followup_method = fetches[2] ? fetches[2].options.method : null;
          out.followup_content = fetches[2] ? fetches[2].body.content : null;
          out.status_before_saved = m._saveState.status;
          fetches[2].resolve({ ok: true, json: async () => ({
            id: 7, content_version: 2, content: 'latest', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          out.final_status = m._saveState.status;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["error_status"], "error")
        self.assertEqual(out["retry_crid"], out["first_crid"],
                         "幂等重试沿用同一 client_request_id")
        self.assertEqual(out["retry_content"], "latest")
        self.assertEqual(out["followup_method"], "PATCH")
        self.assertEqual(out["followup_content"], "latest")
        self.assertNotEqual(out["status_before_saved"], "saved",
                            "取得首次记录时最新草稿尚未入库，不得显示已保存")
        self.assertEqual(out["final_status"], "saved")

    # ── F08：清空新建正文 ─────────────────────────────────────────

    def test_f08_erase_before_debounce_cancels_pending_create(self):
        scenario = r"""
        {
          const { m, statusHost } = buildMemo();
          m.editor = null;
          m.openEditor(null);
          await drain();
          setContent('accidental text'); m.markEditorDirty();
          // 800ms debounce 触发前全部清空
          setContent(''); m.markEditorDirty();
          clock.tick(5000); await drain();
          out.fetches = fetches.length;
          out.status_html = statusHost._html;
          out.store_cleared = store['qi-memo-editor-draft']
            ? JSON.parse(store['qi-memo-editor-draft']).content : null;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["fetches"], 0, "清空后不创建任何记录")
        self.assertNotIn('已保存', out["status_html"], "空正文不误报已保存")
        self.assertEqual(out["store_cleared"], "")

    def test_f08_erase_while_create_in_flight_discards_record(self):
        scenario = r"""
        {
          const { m, statusHost } = buildMemo();
          m.editor = null;
          m.openEditor(null);
          await drain();
          setContent('accidental text'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.create_sent = fetches.length === 1;
          // 创建在途时清空正文
          setContent(''); m.markEditorDirty();
          fetches[0].resolve({ ok: true, json: async () => ({
            id: 7, content_version: 1, content: 'accidental text', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          // 刚创建的记录被删除（生命周期删除请求）
          const del = fetches[1];
          out.delete_url = del ? del.url : null;
          out.delete_version = del ? del.body.expected_version : null;
          del.resolve({ ok: true, json: async () => ({
            id: 7, content_version: 2, status: 'deleted' }) });
          await drain();
          out.editor_reset_new = m.editor.entryId === null
              && m.editor.crid === 'crid-2';
          out.status_html = statusHost._html;
          out.not_saving = m._saveState.status !== 'saved';
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["create_sent"])
        self.assertEqual(out["delete_url"], "/admin/api/memo/entries/7/delete")
        self.assertEqual(out["delete_version"], 1)
        self.assertTrue(out["editor_reset_new"], "清理后编辑会话复位为全新草稿")
        self.assertTrue(out["not_saving"], "空正文不得显示已保存")
        self.assertNotIn('已保存', out["status_html"])

    # ── F03：卸载竞争与草稿恢复 ───────────────────────────────────

    def test_f03_no_concurrent_keepalive_while_save_in_flight(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          setContent('v1内容'); m.markEditorDirty();
          clock.tick(800); await drain();       // PATCH 在途（expected_version=1）
          setContent('最新内容'); m.markEditorDirty();
          // 模拟 pagehide：卸载 keepalive 路径
          m.editor.autosave.flush({ keepalive: true });
          await drain(5);
          // 只有在途 PATCH 一条请求，没有同版本的并发 keepalive
          out.fetch_count = fetches.length;
          // 最新草稿已落本地草稿槽
          const draft = JSON.parse(store['qi-memo-editor-draft']);
          out.draft_content = draft.content;
          out.draft_entry = draft.entryId;
          out.draft_base_version = draft.baseVersion;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["fetch_count"], 1,
                         "在途保存存在时不并发提交同版本 keepalive")
        self.assertEqual(out["draft_content"], "最新内容")
        self.assertEqual(out["draft_entry"], 7)
        self.assertEqual(out["draft_base_version"], 1)

    def test_f03_keepalive_sends_latest_draft_when_idle(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          setContent('离开前输入'); m.markEditorDirty();
          // 无在途保存：keepalive 立即携带最新快照
          m.editor.autosave.flush({ keepalive: true });
          await drain(5);
          out.fetch_count = fetches.length;
          out.keepalive = fetches[0] ? fetches[0].options.keepalive === true : false;
          out.content = fetches[0] ? fetches[0].body.content : null;
          out.version = fetches[0] ? fetches[0].body.expected_version : null;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["fetch_count"], 1)
        self.assertTrue(out["keepalive"])
        self.assertEqual(out["content"], "离开前输入")
        self.assertEqual(out["version"], 1)

    def test_f03_draft_restored_on_reopen_with_conflict_gate(self):
        scenario = r"""
        {
          // 会话1：编辑记录7（服务端 v1），输入最新内容后硬卸载（keepalive 丢失）
          {
            const { m } = buildMemo();
            m.editor = null;
            gwApi = async (url) => url === '/admin/api/memo/entries/7'
              ? entryPayload(7, { content_version: 1 }) : [];
            m.openEditor(7);
            await drain();
            setContent('我卸载前的最新输入'); m.markEditorDirty();
            // 不 resolve 保存请求 = 传输丢失；会话被丢弃
          }
          fetches.length = 0; fetchQueue = [];
          // 会话2：重开记录7（服务端仍是 v1）——草稿恢复
          {
            const { m } = buildMemo();
            m.editor = null;
            gwApi = async (url) => url === '/admin/api/memo/entries/7'
              ? entryPayload(7, { content_version: 1 }) : [];
            m.openEditor(7);
            await drain();
            out.restored_content = editorFields()['[data-editor-content]'].value;
            out.restore_toast = toasts.some((t) => t.msg.includes('已恢复'));
            out.editor_version = m.editor.version;
            // 恢复即排程保存：服务端版本未推进时应直接保存成功
            clock.tick(800); await drain();
            const save = fetches[fetches.length - 1];
            out.save_url = save.url;
            out.save_version = save.body.expected_version;
            out.save_content = save.body.content;
            save.resolve({ ok: true, json: async () => ({
              id: 7, content_version: 2, content: '我卸载前的最新输入', title: null,
              kind: 'note', tags: [] }) });
            await drain();
            out.final_status = m._saveState.status;
          }
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["restored_content"], "我卸载前的最新输入")
        self.assertTrue(out["restore_toast"])
        self.assertEqual(out["editor_version"], 1)
        self.assertEqual(out["save_url"], "/admin/api/memo/entries/7")
        self.assertEqual(out["save_version"], 1)
        self.assertEqual(out["save_content"], "我卸载前的最新输入")
        self.assertEqual(out["final_status"], "saved")

    def test_bug03_far_version_draft_requires_user_decision_and_can_be_restored(self):
        """服务端推进两个版本后，本机未保存草稿不得被静默销毁（BUG-03/R05）：
        必须给出明确的恢复/丢弃决策；恢复后以草稿版本为基准保存，命中
        版本门走冲突确认，不自动覆盖服务端新内容。"""
        scenario = r"""
        {
          {
            const { m } = buildMemo();
            m.editor = null;
            gwApi = async (url) => url === '/admin/api/memo/entries/7'
              ? entryPayload(7, { content_version: 1 }) : [];
            m.openEditor(7);
            await drain();
            setContent('宝贵草稿'); m.markEditorDirty();
          }
          fetches.length = 0; fetchQueue = [];
          {
            const { m } = buildMemo();
            m.editor = null;
            // 服务端已被其他设备推进两个版本：确认框（默认选择恢复草稿）
            gwApi = async (url) => url === '/admin/api/memo/entries/7'
              ? entryPayload(7, { content_version: 3 }) : [];
            m.openEditor(7);
            await drain();
            out.confirm_asked = confirmCalls.length === 1
              && confirmCalls[0].includes('草稿');
            out.restored_content = editorFields()['[data-editor-content]'].value;
            out.editor_version = m.editor.version;
            out.draft_kept = !!store['qi-memo-editor-draft'];
            // 恢复即排程保存：expected_version 用草稿基准 v1 → 命中版本门
            clock.tick(800); await drain();
            const save = fetches[fetches.length - 1];
            out.save_version = save.body.expected_version;
            save.resolve({ ok: false, status: 409, json: async () => ({
              error: 'stale', error_code: 'version_conflict' }) });
            await drain();
            out.conflict_not_overwrite = m._saveState.status === 'conflict';
          }
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["confirm_asked"],
                        "版本差距过大时必须给出恢复/丢弃决策，不得静默处理")
        self.assertEqual(out["restored_content"], "宝贵草稿")
        self.assertEqual(out["editor_version"], 1)
        self.assertTrue(out["draft_kept"], "确认前草稿槽必须保留")
        self.assertEqual(out["save_version"], 1)
        self.assertTrue(out["conflict_not_overwrite"],
                        "恢复的草稿保存必须走冲突确认，不能自动覆盖服务端")

    def test_bug03_far_version_draft_discarded_only_by_explicit_choice(self):
        """用户明确选择丢弃后草稿槽才清除，编辑器显示服务端最新内容。"""
        scenario = r"""
        {
          {
            const { m } = buildMemo();
            m.editor = null;
            gwApi = async (url) => url === '/admin/api/memo/entries/7'
              ? entryPayload(7, { content_version: 1 }) : [];
            m.openEditor(7);
            await drain();
            setContent('将被明确丢弃的草稿'); m.markEditorDirty();
          }
          fetches.length = 0; fetchQueue = [];
          confirmResult = false;   // 用户选择「丢弃草稿」
          {
            const { m } = buildMemo();
            m.editor = null;
            gwApi = async (url) => url === '/admin/api/memo/entries/7'
              ? entryPayload(7, { content_version: 3 }) : [];
            m.openEditor(7);
            await drain();
            out.confirm_asked = confirmCalls.length === 1;
            out.server_content_shown = editorFields()['[data-editor-content]'].value
              === 'server-7';
            out.fresh_version = m.editor.version === 3;
            out.draft_cleared = !store['qi-memo-editor-draft'];
            out.no_restore_toast = !toasts.some((t) => t.msg.includes('已恢复'));
          }
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["confirm_asked"])
        self.assertTrue(out["server_content_shown"])
        self.assertTrue(out["fresh_version"])
        self.assertTrue(out["draft_cleared"], "丢弃只发生在用户明确选择之后")
        self.assertTrue(out["no_restore_toast"])

    def test_bug02_consecutive_patches_advance_session_version(self):
        """同一条记录连续三次 PATCH：每次 expected_version 都取最新已确认
        版本（BUG-02/R03），最终正文与最后输入一致。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7); await drain();
          const bodies = [];
          setContent('第一次修改'); m.markEditorDirty();
          clock.tick(800); await drain();
          bodies.push(fetches[0].body);
          fetches[0].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '第一次修改', content_version: 2 }) });
          await drain();
          out.version_after_first = m.editor.version;
          out.status_after_first = m._saveState.status;

          setContent('第二次修改'); m.markEditorDirty();
          clock.tick(800); await drain();
          bodies.push(fetches[1].body);
          fetches[1].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '第二次修改', content_version: 3 }) });
          await drain();
          out.version_after_second = m.editor.version;

          setContent('第三次修改'); m.markEditorDirty();
          clock.tick(800); await drain();
          bodies.push(fetches[2].body);
          fetches[2].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '第三次修改', content_version: 4 }) });
          await drain();
          out.version_after_third = m.editor.version;
          out.final_status = m._saveState.status;
          out.expected_versions = bodies.map((b) => b.expected_version);
          out.contents = bodies.map((b) => b.content);
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["expected_versions"], [1, 2, 3],
                         "每次 PATCH 都必须使用最新已确认版本")
        self.assertEqual(out["contents"],
                         ["第一次修改", "第二次修改", "第三次修改"])
        self.assertEqual(out["version_after_first"], 2)
        self.assertEqual(out["version_after_second"], 3)
        self.assertEqual(out["version_after_third"], 4)
        self.assertEqual(out["status_after_first"], "saved")
        self.assertEqual(out["final_status"], "saved")

    def test_bug03_pageshow_after_pagehide_resumes_saving(self):
        """真实注册的 pagehide(persisted=true) 停机后，pageshow(persisted=true)
        必须恢复保存泵：返回后的遗留待保存快照继续排空，之后完成关闭只在
        确认入库后清稿（BUG-03/R04）。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          await m.mount(m._harness.memoRoot);
          out.pageshow_registered = (window.listeners.pageshow || []).length === 1;
          m.openEditor(7); await drain();
          setContent('返回前输入'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.save_sent = fetches.length === 1;
          // pagehide：保存泵停机（keepalive 语义由独立用例覆盖，这里不 resolve）
          window.listeners.pagehide[0]({ persisted: true });
          await drain(5);
          // 返回（bfcache）：pageshow 恢复保存泵
          window.listeners.pageshow[0]({ persisted: true });
          await drain();
          fetches[0].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '返回前输入', content_version: 2 }) });
          await drain();
          out.status_restored = m._saveState.status;
          // 恢复后继续输入仍能正常保存
          setContent('返回后的新输入'); m.markEditorDirty();
          clock.tick(800); await drain();
          out.new_save_sent = fetches.length === 2;
          out.new_save_version = fetches[1] ? fetches[1].body.expected_version : null;
          fetches[1].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '返回后的新输入', content_version: 3 }) });
          await drain();
          // 确认入库后完成关闭：无确认框、草稿槽清除
          await m.requestEditorClose(); await drain();
          out.closed = m.editor === null;
          out.no_close_confirm = confirmCalls.length === 0;
          out.draft_cleared = !store['qi-memo-editor-draft'];
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["pageshow_registered"])
        self.assertTrue(out["save_sent"])
        self.assertEqual(out["status_restored"], "saved",
                         "pageshow 后遗留快照必须真正保存而不是停机")
        self.assertTrue(out["new_save_sent"])
        self.assertEqual(out["new_save_version"], 2)
        self.assertTrue(out["closed"])
        self.assertTrue(out["no_close_confirm"])
        self.assertTrue(out["draft_cleared"])

    def test_bug03_close_with_unresumed_pagehide_cannot_silently_drop_draft(self):
        """pagehide 停机后未经 pageshow（无法恢复的环境）直接关闭：
        队列没有真正排空时必须走失败确认，不得静默清掉未确认草稿。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          await m.mount(m._harness.memoRoot);
          m.openEditor(7); await drain();
          setContent('未确认输入'); m.markEditorDirty();
          clock.tick(800); await drain();
          window.listeners.pagehide[0]({ persisted: true });
          await drain(5);
          setContent('停机后的新输入'); m.markEditorDirty();
          await drain();
          out.pending_no_request = fetches.length === 1;   // 停机中不发送
          confirmResult = false;   // 用户选择「返回编辑」
          await m.requestEditorClose(); await drain();
          out.confirm_asked = confirmCalls.length === 1;
          out.editor_still_open = m.editor !== null;
          out.draft_kept = !!store['qi-memo-editor-draft']
            && JSON.parse(store['qi-memo-editor-draft']).content === '停机后的新输入';
          // pageshow 恢复后：在途保存完成 → 泵接续提交最新输入
          window.listeners.pageshow[0]({ persisted: true });
          await drain();
          fetches[0].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '未确认输入', content_version: 2 }) });
          await drain();
          out.followup_sent = fetches.length === 2;
          out.followup_version = fetches[1] ? fetches[1].body.expected_version : null;
          fetches[1].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '停机后的新输入', content_version: 3 }) });
          await drain();
          out.saved_after_resume = m._saveState.status === 'saved';
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["pending_no_request"])
        self.assertTrue(out["confirm_asked"], "未排空的关闭必须确认，不得静默丢稿")
        self.assertTrue(out["editor_still_open"])
        self.assertTrue(out["draft_kept"])
        self.assertTrue(out["followup_sent"])
        self.assertEqual(out["followup_version"], 2)
        self.assertTrue(out["saved_after_resume"])

    def test_bug03_pageshow_resume_drains_leftover_after_inflight_pagehide(self):
        """在途保存期间 pagehide（不并发 keepalive）→ pageshow：遗留的
        待保存快照由恢复后的泵继续提交，最新草稿不丢。"""
        scenario = r"""
        {
          const { m } = buildMemo();
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          await m.mount(m._harness.memoRoot);
          m.openEditor(7); await drain();
          setContent('在途保存的正文'); m.markEditorDirty();
          clock.tick(800); await drain();
          setContent('最新输入'); m.markEditorDirty();
          window.listeners.pagehide[0]({ persisted: true });   // 在途存在：不发 keepalive
          await drain(5);
          out.no_concurrent_keepalive = fetches.length === 1;
          window.listeners.pageshow[0]({ persisted: true });
          await drain();
          // 在途保存完成：泵接续提交最新输入
          fetches[0].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '在途保存的正文', content_version: 2 }) });
          await drain();
          clock.tick(0); await drain();
          out.leftover_sent = fetches.length === 2;
          out.leftover_version = fetches[1] ? fetches[1].body.expected_version : null;
          out.leftover_content = fetches[1] ? fetches[1].body.content : null;
          fetches[1].resolve({ ok: true, json: async () => entryPayload(7, {
            content: '最新输入', content_version: 3 }) });
          await drain();
          out.final_status = m._saveState.status;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["no_concurrent_keepalive"])
        self.assertTrue(out["leftover_sent"], "pageshow 后遗留快照必须继续排空")
        self.assertEqual(out["leftover_version"], 2)
        self.assertEqual(out["leftover_content"], "最新输入")
        self.assertEqual(out["final_status"], "saved")

    # ── F04：关闭流程完整 ─────────────────────────────────────────

    def test_f04_mask_close_runs_full_close_flow(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          const modalRec = modals[modals.length - 1];
          out.mask_close_wired = typeof modalRec.opts.onMaskClose === 'function';
          // 遮罩点击 = onMaskClose：未保存内容 → 保存 → 关闭
          setContent('遮罩关闭前的输入'); m.markEditorDirty();
          clock.tick(800); await drain();
          modalRec.opts.onMaskClose();
          await drain();
          const save = fetches[fetches.length - 1];
          save.resolve({ ok: true, json: async () => ({
            id: 7, content_version: 2, content: '遮罩关闭前的输入', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          out.editor_closed = m.editor === null;
          out.modal_removed = modalRec.closed;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["mask_close_wired"], "遮罩关闭进入编辑器关闭流程")
        self.assertTrue(out["editor_closed"], "关闭流程释放单实例守卫，可再次新建/编辑")
        self.assertTrue(out["modal_removed"])

    def test_f04_failed_save_close_asks_confirm_and_can_return_to_edit(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.editor = null;
          gwApi = async (url) => url === '/admin/api/memo/entries/7' ? entryPayload(7) : [];
          m.openEditor(7);
          await drain();
          const modalRec = modals[modals.length - 1];
          setContent('保存会失败的内容'); m.markEditorDirty();
          clock.tick(800); await drain();
          confirmResult = false;          // 用户选择「返回编辑」
          modalRec.opts.onMaskClose();
          await drain();
          fetches[0].resolve({ ok: false, status: 500, json: async () => ({
            error: 'db down' }) });
          await drain();
          // flush 停在失败态 → confirm(false) → 编辑器保持打开
          out.confirm_asked = confirmCalls.length === 1;
          out.editor_still_open = m.editor !== null;
          out.modal_still_visible = !modalRec.closed;
          out.retry_visible = modals[modals.length - 1].root.fields['[data-save-status]']
            .innerHTML.includes('重试');
          // 重试成功后再关
          confirmResult = true;
          m.editor.autosave.retry();
          await drain();
          fetches[1].resolve({ ok: true, json: async () => ({
            id: 7, content_version: 2, content: '保存会失败的内容', title: null,
            kind: 'note', tags: [] }) });
          await drain();
          modalRec.opts.onMaskClose();
          await drain();
          out.closed_after_retry = m.editor === null;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["confirm_asked"], "保存失败关闭前必须确认")
        self.assertTrue(out["editor_still_open"])
        self.assertTrue(out["modal_still_visible"], "不留下不可见编辑器")
        self.assertTrue(out["retry_visible"])
        self.assertTrue(out["closed_after_retry"])

    # ── F05：板块拖动标识 ─────────────────────────────────────────

    def test_f05_board_sections_carry_drag_identity(self):
        scenario = r"""
        {
          const { m, body } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          m.tags = [{ id: 1, name: '购物', position: 1 }, { id: 2, name: '灵感', position: 2 }];
          m.board = { sections: [
            { tag: { id: 1, name: '购物', position: 1 }, note_sort_mode: 'latest',
              preview: [], items: [], pinned_order: [], note_order: [],
              pinned_count: 0, note_count: 0 },
            { tag: { id: 2, name: '灵感', position: 2 }, note_sort_mode: 'latest',
              preview: [], items: [], pinned_order: [], note_order: [],
              pinned_count: 0, note_count: 0 },
            { tag: null, note_sort_mode: 'latest', preview: [], items: [],
              pinned_order: [], note_order: [], pinned_count: 0, note_count: 0 },
          ] };
          m.render();
          const html = body._html;
          out.tag_sections_draggable = (html.match(/<section class="memo-section" data-drag-item data-tag-id=/g) || []).length;
          out.untagged_not_draggable = !/data-untagged="1"[^>]*data-drag-item/.test(html)
              && !(html.split('data-untagged="1"')[1] || '').includes('data-drag="tag"');
          out.handles = (html.match(/data-drag="tag"/g) || []).length;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["tag_sections_draggable"], 2,
                         "标签板块节点必须带 data-drag-item（F05）")
        self.assertTrue(out["untagged_not_draggable"], "未分类固定末尾，不参与板块拖动")
        self.assertEqual(out["handles"], 2)

    def test_f05_commit_tag_order_collects_direct_members_only(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.tags = [{ id: 1, name: '购物', position: 1 }, { id: 2, name: '灵感', position: 2 }];
          let seenSelector = null;
          const list = {
            dataset: { dragList: 'tags' },
            querySelectorAll(sel) { seenSelector = sel; return [
              { dataset: { tagId: '2' } }, { dataset: { tagId: '1' } }]; },
          };
          m.commitTagOrder(list);
          await drain();
          out.selector = seenSelector;
          out.order = gwCalls[0] ? JSON.parse(gwCalls[0].opts.body).order : null;
          out.scope = gwCalls[0] ? JSON.parse(gwCalls[0].opts.body).scope : null;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["selector"], ":scope > [data-drag-item]",
                         "顺序收集只枚举对应列表的直接成员（F05）")
        self.assertEqual(out["order"], [2, 1])
        self.assertEqual(out["scope"], "tags")

    # ── F11：连续拖动 ─────────────────────────────────────────────

    def test_f11_no_new_drag_while_reorder_in_flight(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          const section = { tag: { id: 1, name: '购物', position: 1 },
            note_sort_mode: 'latest', preview: [], items: [],
            pinned_order: [11, 12], note_order: [], pinned_count: 2, note_count: 0 };
          m.tags = [{ id: 1, name: '购物', position: 1 }];
          m.board = { sections: [section] };
          // reload 始终取回同一看板数据，保持第三次提交可定位板块
          gwApi = async (url) => url === '/admin/api/memo/board'
            ? { sections: [section] } : url === '/admin/api/memo/tags' ? m.tags : [];
          const drag = { dataset: { entryId: '12', kind: 'pinned' } };
          const list = {
            dataset: { dragList: 'entries' },
            closest: (sel) => sel === '.memo-section' ? { dataset: { tagId: '1' } } : null,
            querySelectorAll: () => [drag],
          };
          const reorderCalls = () => gwCalls.filter((c) => c.url === '/admin/api/memo/reorder').length;
          // 第一次重排提交（保存与刷新在途）
          m.commitEntryOrder(list, drag);
          out.first_call = reorderCalls() === 1;
          out.first_order = JSON.parse(gwCalls[0].opts.body).order;
          out.busy = m.reorderBusy === true;
          // 保存/刷新未完成时：第二次拖动提交被阻止
          m.commitEntryOrder(list, drag);
          out.second_blocked = reorderCalls() === 1;
          await drain(60);   // 第一次保存返回并完成刷新
          out.busy_released_after_refresh = m.reorderBusy === false;
          // 解除后可再次提交
          m.commitEntryOrder(list, drag);
          out.third_allowed = reorderCalls() === 2;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["first_call"])
        self.assertEqual(out["first_order"], [12, 11])
        self.assertTrue(out["busy"])
        self.assertTrue(out["second_blocked"], "重排在途时不开新拖动（F11）")
        self.assertTrue(out["busy_released_after_refresh"])
        self.assertTrue(out["third_allowed"])

    # ── F09/F10/F12：列表刷新与旧响应隔离 ─────────────────────────

    def test_f09_restore_from_trash_refetches_with_deleted_status(self):
        scenario = r"""
        {
          const { m, body } = buildMemo();
          m.view = 'trash';
          m.tags = [];
          m.board = { sections: [] };
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/entries/5') return entryPayload(5);
            if (url === '/admin/api/memo/entries/5/restore' && opts)
              return entryPayload(5);
            if (url === '/admin/api/memo/entries?status=deleted') return [entryPayload(5)];
            if (url === '/admin/api/memo/board') return { sections: [] };
            if (url === '/admin/api/memo/tags') return [];
            return [];
          };
          await m.restoreEntry(5);
          await drain();
          const listCalls = gwCalls.filter((c) => c.url.includes('status='));
          out.refetch_status = listCalls.length
            ? listCalls[listCalls.length - 1].url.split('status=')[1] : null;
          out.body_has_item = body._html.includes('memo-item');
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["refetch_status"], "deleted",
                         "回收站恢复后必须以 status=deleted 重读（F09）")
        self.assertTrue(out["body_has_item"])

    def test_f10_stale_lifecycle_response_does_not_overwrite_board(self):
        scenario = r"""
        {
          const { m, body } = buildMemo();
          m.view = 'board'; m.filterTagId = '';
          m.tags = [];
          m.board = { sections: [
            { tag: null, note_sort_mode: 'latest', preview: [], items: [],
              pinned_order: [], note_order: [], pinned_count: 0, note_count: 0 },
          ] };
          m.render();
          const boardHtml = body._html;
          let releaseArchived;
          gwApi = async (url) => new Promise((resolve) => {
            if (url.includes('status=archived')) releaseArchived = resolve;
            else resolve([]);
          });
          m.renderLifecycleList('archived');   // 慢的归档列表请求（无 switchView）
          await drain(5);
          // 用户切回看板（switchView 使在途视图请求失效）
          m.switchView('board');
          m.render();
          out.board_rendered = body._html === boardHtml;
          releaseArchived([{ id: 1, kind: 'note' }]);
          await drain();
          out.still_board = body._html === boardHtml;
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["board_rendered"])
        self.assertTrue(out["still_board"],
                        "切走视图后旧列表响应不得覆盖当前内容区（F10）")

    def test_f12_archive_from_search_refreshes_results(self):
        scenario = r"""
        {
          const { m } = buildMemo();
          m.view = 'search';
          m.searchQuery = '关键词';
          m.tags = [];
          m.board = { sections: [] };
          let searchCalls = 0;
          gwApi = async (url, opts) => {
            if (url === '/admin/api/memo/entries/7') return entryPayload(7);
            if (url === '/admin/api/memo/entries/7/archive' && opts)
              return entryPayload(7, { status: 'archived' });
            if (url === '/admin/api/memo/entries?status=active&q='
                + encodeURIComponent('关键词')) {
              searchCalls += 1;
              return searchCalls === 1 ? [entryPayload(7)] : [];
            }
            if (url === '/admin/api/memo/board') return { sections: [] };
            if (url === '/admin/api/memo/tags') return [];
            return [];
          };
          await m.runSearch();
          await drain();
          out.initial_results = m.searchResults.length;
          // 从搜索结果归档记录 7
          await m.lifecycleEntry(7, 'archive');
          await drain();
          out.search_refreshed = searchCalls >= 2;
          out.results_updated = m.searchResults.length === 0;
        }
        """
        out = self._run(scenario)
        self.assertEqual(out["initial_results"], 1)
        self.assertTrue(out["search_refreshed"], "写入后按当前关键词重查（F12）")
        self.assertTrue(out["results_updated"], "已归档记录离开搜索结果")

    # ── F13：空标签删除入口 ───────────────────────────────────────

    def test_f13_empty_tag_deletable_from_board_empty_state(self):
        scenario = r"""
        {
          const { m, body } = buildMemo();
          m.view = 'board';
          m.filterTagId = 3;
          m.tags = [{ id: 3, name: '只用过一次', position: 1 }];
          m.board = { sections: [] };
          m.render();
          out.delete_button = body._html.includes('data-act="memo-delete-tag"')
              && body._html.includes('data-tag-id="3"');
          out.name_shown = body._html.includes('只用过一次');
          // 未分类空态不提供删除
          m.filterTagId = 'untagged';
          m.render();
          out.untagged_no_delete = !body._html.includes('memo-delete-tag');
        }
        """
        out = self._run(scenario)
        self.assertTrue(out["delete_button"], "空标签的看板空态必须提供删除入口（F13）")
        self.assertTrue(out["name_shown"])
        self.assertTrue(out["untagged_no_delete"])


if __name__ == "__main__":
    unittest.main()
