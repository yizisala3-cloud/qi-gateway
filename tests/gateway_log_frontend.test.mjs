// gateway_log_frontend.test.mjs - 日志页前端契约（真实 ES 模块导入生产代码）
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const workspace = process.argv[2];
const group = process.argv[3];
const uiSource = await readFile(join(workspace, 'admin/js/ui.js'), 'utf8');
const version = uiSource.match(/ASSET_VERSION = '([^']+)'/)[1];

// ── 最小 DOM shim（与 planning_frontend_modules.test.mjs 同思路） ──
const masks = [];
const toastMessages = [];

class Element {
  constructor() {
    this.value = '';
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.dataset = {};
    this.innerHTML = '';
    this.children = [];
    this.nodes = new Map();
    this.attributes = new Map();
    this.parentElement = null;
    this.tabIndex = 0;
    this.listeners = new Map();
    this.onclick = null;
    this.classList = {
      add() {}, remove() {}, contains: () => false, toggle() {},
    };
    this.lastElementChild = {};
    Object.defineProperty(this.lastElementChild, 'textContent', {
      set: (value) => toastMessages.push(value),
    });
  }
  querySelector(selector) {
    if (!this.nodes.has(selector)) {
      const node = new Element();
      node.parentElement = this;
      this.nodes.set(selector, node);
    }
    return this.nodes.get(selector);
  }
  querySelectorAll() { return []; }
  appendChild(child) {
    child.parentElement = this;
    this.children.push(child);
    if (child.className === 'modal-mask') masks.push(child);
  }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
  }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  remove() { this.removed = true; this.parentElement = null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  get isConnected() { return !this.removed && (this === document.body || !!this.parentElement?.isConnected); }
  matches(selector) {
    if (selector === ':disabled') return this.disabled;
    if (selector === '[role="tooltip"]') return this.getAttribute('role') === 'tooltip';
    return selector.split(',').some(part => {
      const match = part.trim().match(/^\[([\w-]+)(?:="([^"]+)")?\]$/);
      return match && this.attributes.has(match[1]) && (!match[2] || this.getAttribute(match[1]) === match[2]);
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  contains(node) { return node === this || [...this.nodes.values(), ...this.children].some(child => child.contains(node)); }
  getClientRects() { return this.isConnected && !this.hidden ? [{}] : []; }
  focus() { if (this.isConnected && !this.closest('[inert]')) document.activeElement = this; }
  click() { if (!this.disabled) this.onclick?.({ target: this, currentTarget: this }); }
  setPointerCapture() {}
  getBoundingClientRect() { return { top: 0, height: 40 }; }
}

const documentListeners = new Map();
globalThis.document = {
  body: new Element(), createElement: () => new Element(), activeElement: null, querySelector: () => null,
  addEventListener(name, fn) {
    if (!documentListeners.has(name)) documentListeners.set(name, new Set());
    documentListeners.get(name).add(fn);
  },
  removeEventListener(name, fn) { documentListeners.get(name)?.delete(fn); },
};
globalThis.MutationObserver = class { observe() {} disconnect() {} };
globalThis.getComputedStyle = () => ({ visibility: 'visible' });
globalThis.window = { location: { origin: 'http://logs.test' } };
globalThis.localStorage = {
  store: new Map([['qi-token', 'token-1']]),
  getItem: (k) => globalThis.localStorage.store.get(k) || '',
  setItem: (k, v) => globalThis.localStorage.store.set(k, v),
  removeItem: (k) => globalThis.localStorage.store.delete(k),
};
globalThis.setTimeout = () => 0;
// Node 21+ 暴露只读 navigator 全局（BUG-19）：仅在缺失时补一个空对象，
// 不强行覆盖只读属性。页面 copyText 用可选链访问 clipboard，内置
// navigator 同样安全。
if (typeof globalThis.navigator === 'undefined') {
  Object.defineProperty(globalThis, 'navigator', {
    value: {}, configurable: true, writable: true,
  });
}

const requests = [];
let respond = async () => ({ ok: true, json: async () => ({}) });
globalThis.fetch = async (url, options = {}) => {
  const request = { url, options };
  requests.push(request);
  return respond(request);
};

const moduleUrl = (relative) =>
  pathToFileURL(join(workspace, 'admin/js', relative)).href + `?v=${version}`;
const page = (await import(moduleUrl('pages/logs.js'))).default;
const ui = await import(moduleUrl('ui.js'));

const root = () => page.root;
const list = () => root().querySelector('#log-list');
const pager = () => root().querySelector('#log-pager');
const response = (body = {}) => ({ ok: true, json: async () => body });
const failure = (status, statusText, error) => ({
  ok: false, status, statusText, json: async () => ({ error }),
});
const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
const click = (selector) => {
  // shim 不解析 innerHTML：从选择器提取 data-act / data-* 注入 dataset
  const el = root().querySelector(selector);
  const act = selector.match(/data-act="([^"]+)"/);
  if (act) el.dataset.act = act[1];
  const fp = selector.match(/data-fingerprint="([^"]+)"/);
  if (fp) el.dataset.fingerprint = fp[1];
  for (const fn of [...(root().listeners.get('click') || [])]) {
    fn({ target: { closest: () => el } });
  }
};
const change = (el, value) => {
  el.value = value;
  for (const fn of [...(el.listeners.get('change') || [])]) fn();
};
const lastRequest = () => requests.at(-1);
const requestPath = (request) => new URL(request.url).pathname;
const requestParams = (request) => new URL(request.url).searchParams;
const lastModal = () => masks.at(-1).querySelector('.modal');

const GROUP = (extra = {}) => ({
  fingerprint: 'aaaaaaaaaaaaaaaa', event_count: 3,
  first_seen_at: '2026-10-10T01:00:00+00:00',
  last_seen_at: '2026-10-10T03:00:00+00:00',
  level: 'ERROR', module: 'planning', operation: 'create',
  error_code: 'internal_error', message_zh: '规划接口发生内部错误',
  exception_type: 'APIError', logger_name: 'gateway.planning_api',
  source: 'a.py:fn:1', outcome: 'failed', instance_id: 'inst',
  sample_event_id: '00000000-0000-0000-0000-000000000001',
  sample_message_raw: 'raw', sample_stack_trace: 'trace',
  sample_request_id: 'r1', sample_truncated_fields: [], ...extra,
});

async function mountFresh() {
  page.groups = null;
  page.stale = false;
  page.page = 0;
  page.cursors = [null];
  page.busy = false;
  page.seq = 0;
  page.metaSeq = 0;
  page.frozenRange = null;
  page.snapshot = null;
  page.pageCache = {};
  page.detailSessions = [];
  page.filters = { preset: '24h', start: '', end: '', module: '', level: '', search: '' };
  requests.length = 0;
  respond = async (request) => {
    const path = requestPath(request);
    if (path === '/admin/api/logs/meta') {
      return response({
        modules: [{ module: 'planning', count: 3 }, { module: 'chat', count: 1 }],
        levels: [{ level: 'ERROR', count: 4 }],
        keys: [{ key_ref: 'kh1-abcdef1234567890abcdef1234567890',
                 credential_role: 'chat', provider: 'api.example.com', count: 3 }],
        total_events: 4,
        collection: {
          enabled: true, min_level: 'WARNING', queue_depth: 0, queue_max: 2000,
          dropped_total: 0, written_total: 9, last_write_at: '2026-10-10T03:00:00+00:00',
          consecutive_failures: 0, retention_days: 30, key_ref_ready: true,
          spool: { enabled: false, files: 0, bytes: 0, replayed_total: 0, corrupt_total: 0, dropped_total: 0 },
        },
      });
    }
    if (path === '/admin/api/logs') {
      return response({
        groups: [GROUP()], has_more: false, next_cursor: null,
        snapshot: '2026-10-10T03:00:00+00:00',
        visible_snapshot: '100:110:101',
      });
    }
    return response({});
  };
  const host = new Element();
  await page.mount(host);
  await flush();
  return host;
}

const checks = {
  async graph() {
    const host = new Element();
    respond = async () => response({
      modules: [], levels: [], total_events: 0,
      collection: { enabled: true, queue_depth: 0, dropped_total: 0, spool: { enabled: false } },
    });
    // 列表请求返回空，避免渲染分支干扰
    await page.mount(host);
    await flush();
    assert.equal(typeof page.mount, 'function');
    assert.equal(ui.ASSET_VERSION, version);
    const html = host.innerHTML;
    // 占位文案已删除；筛选与刷新控件真实存在
    assert.ok(!html.includes('暂未接入'), '不得保留“暂未接入”占位');
    assert.ok(!html.includes('报错日志与调用日志将在此展示'));
    assert.ok(html.includes('id="log-preset"'));
    assert.ok(html.includes('id="log-module"'));
    assert.ok(html.includes('id="log-level"'));
    assert.ok(html.includes('id="log-search"'));
    assert.ok(html.includes('data-act="refresh"'));
    assert.ok(html.includes('全网关 WARNING 及以上故障事件'));
    // 请求带 token（鉴权沿用 gw 封装）
    const withAuth = requests.find((r) => requestPath(r) === '/admin/api/logs');
    assert.ok(withAuth, '列表请求已发出');
    assert.equal(withAuth.options.headers.Authorization, 'Bearer token-1');
  },

  async list() {
    await mountFresh();
    const html = list().innerHTML;
    assert.ok(html.includes('规划接口发生内部错误'), '渲染中文摘要');
    assert.ok(html.includes('internal_error'), '渲染错误码');
    assert.ok(html.includes('共 3 次'), '渲染聚合次数');
    assert.ok(html.includes('data-fingerprint="aaaaaaaaaaaaaaaa"'), '行携带 fingerprint');
    assert.ok(!html.includes('&amp;'), 'esc 转义不重复叠加');
    // 无更多页 → 不渲染翻页
    assert.equal(pager().innerHTML, '');

    // has_more → 下一页携带 cursor，上一页回退
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs') {
        const cursor = requestParams(request).get('cursor');
        if (!cursor) return response({
          groups: [GROUP({ fingerprint: 'a'.repeat(16), last_seen_at: '2026-10-10T03:00:00+00:00' })],
          has_more: true, next_cursor: '2026-10-10T03:00:00+00:00|aaaaaaaaaaaaaaaa',
        });
        return response({ groups: [GROUP({ fingerprint: 'b'.repeat(16) })], has_more: false });
      }
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.refresh();
    await flush();
    assert.ok(pager().innerHTML.includes('下一页'), '有下一页按钮');
    click('[data-act="page-next"]');
    await flush();
    assert.equal(requestParams(lastRequest()).get('cursor'),
      '2026-10-10T03:00:00+00:00|aaaaaaaaaaaaaaaa', '下一页使用服务端 cursor');
    assert.ok(list().innerHTML.includes('bbbbbbbbbbbbbbbb'));
    // BUG-05：回看第一页复用会话缓存——不发新请求，快照与冻结区间不变
    const beforeBack = requests.length;
    const snapshotBefore = page.snapshot;
    click('[data-act="page-prev"]');
    await flush();
    assert.equal(requests.length, beforeBack, '回看第一页不发新请求（会话缓存）');
    assert.equal(page.snapshot, snapshotBefore, '快照不重建');
    assert.ok(list().innerHTML.includes('aaaaaaaaaaaaaaaa'), '第一页缓存内容重渲染');
    assert.ok(!list().innerHTML.includes('bbbbbbbbbbbbbbbb'), '第二页内容不再显示');
    // 再前进：仍走服务端 cursor
    click('[data-act="page-next"]');
    await flush();
    assert.equal(requestParams(lastRequest()).get('cursor'),
      '2026-10-10T03:00:00+00:00|aaaaaaaaaaaaaaaa', '再次前进使用 cursor');
  },

  async filters() {
    await mountFresh();
    // 模块筛选：请求带 module，分页重置
    change(root().querySelector('#log-module'), 'chat');
    await flush();
    assert.equal(requestParams(lastRequest()).get('module'), 'chat');
    assert.equal(page.page, 0);
    // 级别筛选
    change(root().querySelector('#log-level'), 'ERROR');
    await flush();
    assert.equal(requestParams(lastRequest()).get('level'), 'ERROR');
    // 自定义区间缺时间 → 中文提示且不发起新请求
    const before = requests.length;
    change(root().querySelector('#log-preset'), 'custom');
    await flush();
    assert.ok(root().querySelector('#log-time-hint').textContent.includes('自定义区间需要同时填写'));
    assert.equal(requests.length, before, '校验失败不发请求');
    // 填写倒置区间 → 提示且不发请求
    root().querySelector('#log-start').value = '2026-10-11T10:00';
    root().querySelector('#log-end').value = '2026-10-10T09:00';
    await page.refresh();
    await flush();
    assert.ok(root().querySelector('#log-time-hint').textContent.includes('开始时间不能晚于结束时间'));
    // 合法区间 → 请求带 start/end
    root().querySelector('#log-start').value = '2026-10-10T09:00';
    await page.refresh();
    await flush();
    const params = requestParams(lastRequest());
    assert.ok(params.get('start').startsWith('2026-10-10T09:00:00+08:00'));
    assert.ok(params.get('end').startsWith('2026-10-10T09:00:59+08:00'));
    // 搜索回车触发
    root().querySelector('#log-search').value = 'APIError';
    for (const fn of [...(root().querySelector('#log-search').listeners.get('keydown') || [])]) {
      fn({ key: 'Enter' });
    }
    await flush();
    const logsRequest = [...requests].reverse()
      .find((r) => requestPath(r) === '/admin/api/logs');
    assert.equal(requestParams(logsRequest).get('search'), 'APIError');
  },

  async errors() {
    // 查询失败：真实失败摘要 + 重试入口 + 过时标注（此时列表有旧数据）
    await mountFresh();
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs') {
        return failure(500, 'Internal Server Error', 'log_store_unavailable');
      }
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.refresh();
    await flush();
    const html = list().innerHTML;
    assert.ok(html.includes('日志查询失败'), '失败摘要');
    assert.ok(html.includes('500 Internal Server Error: log_store_unavailable'));
    assert.ok(html.includes('重试'), '重试入口');
    assert.ok(html.includes('可能已过时'), '旧数据过时标注');

    // 401：中文授权问题，不展示日志正文
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs') {
        return failure(401, 'Unauthorized', 'unauthorized');
      }
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.refresh();
    await flush();
    assert.ok(list().innerHTML.includes('登录已过期或令牌无效'));

    // 空数据（最后测：清掉旧列表后空态成立）
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs') {
        return response({ groups: [], has_more: false });
      }
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.refresh();
    await flush();
    assert.ok(list().innerHTML.includes('当前筛选下暂无报错记录'), '空态文案');
  },

  async detail() {
    await mountFresh();
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        return response({
          group: GROUP(),
          samples: [{
            event_id: '00000000-0000-0000-0000-000000000001',
            occurred_at: '2026-10-10T03:00:00+00:00', level: 'ERROR',
            module: 'planning', operation: 'create', error_code: 'internal_error',
            message_zh: '规划接口发生内部错误', exception_type: 'APIError',
            message_raw: 'raw', stack_trace: 'Traceback (most recent call last):\n  File "a.py"',
            logger_name: 'gateway.planning_api', source: 'a.py:fn:1',
            request_id: 'r1', operation_id: '', route: '/admin/api/planning/tasks',
            method: 'POST', http_status: 500, upstream_status: null,
            outcome: 'failed', instance_id: 'inst', release_id: '',
            safe_context: {}, truncated_fields: [],
          }],
        });
      }
      return response({ groups: [GROUP()], has_more: false });
    };
    await page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    const body = lastModal().querySelector('.modal-body').innerHTML;
    assert.ok(body.includes('规划接口发生内部错误'), '中文摘要');
    assert.ok(body.includes('发生次数'), '次数');
    assert.ok(body.includes('APIError'), '异常类');
    assert.ok(body.includes('request_id'), '关联请求');
    assert.ok(body.includes('Traceback'), '堆栈原文');
    assert.ok(body.includes('data-copy="stack"'), '复制堆栈按钮');
    assert.ok(body.includes('最近 1 / 共 3 次'), '样本范围写明');
    assert.ok(body.includes('不代表同一来源'), '样本范围说明');

    // 无堆栈 warning：真实提示，不伪造
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        return response({
          group: GROUP({ level: 'WARNING', exception_type: '' }),
          samples: [{
            event_id: '00000000-0000-0000-0000-000000000002',
            occurred_at: '2026-10-10T03:00:00+00:00', level: 'WARNING',
            module: 'planning', message_raw: 'raw', stack_trace: '',
            request_id: 'r2', truncated_fields: [],
          }],
        });
      }
      return response({ groups: [GROUP()], has_more: false });
    };
    await page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    const body2 = lastModal().querySelector('.modal-body').innerHTML;
    assert.ok(body2.includes('此记录未附异常堆栈'), '无堆栈真实提示');

    // 恶意文本：只按文本渲染（esc 转义）
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        return response({
          group: GROUP({ message_zh: '<script>alert(1)</script>' }),
          samples: [{ event_id: 'x', occurred_at: 't', level: 'ERROR',
            message_raw: '<img src=x onerror=alert(1)>', stack_trace: '',
            request_id: 'r3', truncated_fields: [] }],
        });
      }
      return response({ groups: [GROUP()], has_more: false });
    };
    await page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    const body3 = lastModal().querySelector('.modal-body').innerHTML;
    assert.ok(body3.includes('&lt;script&gt;'), 'script 被转义');
    assert.ok(body3.includes('&lt;img'), 'img 被转义');
    assert.ok(!body3.includes('<script>alert'), '无未转义 script');
  },

  async stale() {
    // 快速切换筛选：旧响应不能覆盖新条件结果（seq 丢弃）
    await mountFresh();
    let releaseOld;
    const oldResponse = new Promise((resolve) => { releaseOld = resolve; });
    respond = async (request) => {
      const path = requestPath(request);
      if (path === '/admin/api/logs/meta') {
        return response({ modules: [], levels: [], total_events: 0, collection: {} });
      }
      const module = requestParams(request).get('module');
      if (module === 'planning') return oldResponse.then(() =>
        response({ groups: [GROUP({ module: 'planning' })], has_more: false }));
      return response({ groups: [GROUP({ module: 'chat', message_zh: '聊天转发请求超时' })], has_more: false });
    };
    // 切到 planning：请求挂起（旧请求）
    change(root().querySelector('#log-module'), 'planning');
    await flush();
    // 立即切到 chat：supersede 取代在途请求
    change(root().querySelector('#log-module'), 'chat');
    await flush();
    assert.ok(list().innerHTML.includes('聊天转发请求超时'), '新筛选结果已渲染');
    // 旧请求此刻才返回：必须被丢弃
    releaseOld({ groups: [GROUP({ module: 'planning' })], has_more: false });
    await flush();
    assert.ok(list().innerHTML.includes('聊天转发请求超时'), '旧响应被丢弃');
    assert.ok(!list().innerHTML.includes('规划接口发生内部错误'), '旧数据未覆盖新条件');
  },

  async remount() {
    // BUG-07：离开页面再返回，控件 / 实际请求 / 页码一致；custom 不卡死
    await mountFresh();
    change(root().querySelector('#log-level'), 'ERROR');
    change(root().querySelector('#log-preset'), 'custom');
    root().querySelector('#log-start').value = '2026-10-10T09:00';
    root().querySelector('#log-end').value = '2026-10-10T10:00';
    await flush();
    assert.equal(page.page, 0);
    // 离开页面（unmount）再返回（mount）：状态统一重置
    page.unmount();
    const requestsBefore = requests.length;
    const host = new Element();
    respond = async (request) => {
      const path = requestPath(request);
      if (path === '/admin/api/logs') {
        return response({ groups: [GROUP()], has_more: false, snapshot: null });
      }
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.mount(host);
    await flush();
    // 内部状态统一重置（shim 不解析 HTML selected/hidden 属性，控件同步
    // 由真实浏览器交互验证）：preset 回默认、级别清空、页码归零
    assert.equal(page.filters.preset, '24h');
    assert.equal(page.filters.level, '');
    assert.equal(page.filters.module, '');
    assert.equal(page.page, 0);
    // 新会话的冻结区间是默认 24h 动态区间，不是残留的 custom 区间
    assert.ok(page.frozenRange && page.frozenRange.start, '新会话区间已建立');
    assert.notEqual(page.frozenRange.start, '2026-10-10T09:00:00+08:00');
    // 重挂载后的列表请求：不带残留 level，使用默认 24h 动态区间
    // （custom 空区间不再导致 0 请求卡在加载态）
    const logsRequests = requests.slice(requestsBefore)
      .filter((r) => requestPath(r) === '/admin/api/logs');
    assert.ok(logsRequests.length >= 1, '重挂载后列表正常请求');
    for (const request of logsRequests) {
      assert.equal(requestParams(request).get('level'), null);
      assert.ok(requestParams(request).get('start'), '默认 24h 区间正常携带');
      assert.notEqual(requestParams(request).get('start'), '2026-10-10T09:00:00+08:00');
    }
    // 在途请求离开时被取消：旧响应不能渲染进新会话
    let releaseHanging;
    const hanging = new Promise((resolve) => { releaseHanging = resolve; });
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs') return hanging;
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.refresh();
    await flush();
    const listEl = list();
    page.unmount();
    releaseHanging({ groups: [GROUP({ message_zh: '过时数据' })], has_more: false });
    await flush();
    assert.ok(!listEl.innerHTML.includes('过时数据'), '卸载后晚到响应不渲染');
  },

  async metaRace() {
    // BUG-08：旧 meta 响应不能覆盖新筛选，也不静默清空模块选择
    await mountFresh();
    let releaseOldMeta;
    const oldMeta = new Promise((resolve) => { releaseOldMeta = resolve; });
    let metaCalls = 0;
    respond = async (request) => {
      const path = requestPath(request);
      if (path === '/admin/api/logs/meta') {
        metaCalls += 1;
        // 第一个 meta（近1小时）挂起，晚于新请求返回
        if (metaCalls === 1) return oldMeta.then(() =>
          response({ modules: [{ module: 'chat', count: 1 }], levels: [], total_events: 1, collection: {} }));
        return response({ modules: [{ module: 'planning', count: 3 }], levels: [], total_events: 3, collection: {} });
      }
      return response({ groups: [GROUP()], has_more: false, snapshot: null });
    };
    // 近1小时（旧请求挂起）→ 立即切近7天并选 planning
    change(root().querySelector('#log-preset'), '1h');
    await flush();
    change(root().querySelector('#log-preset'), '7d');
    await flush();
    change(root().querySelector('#log-module'), 'planning');
    await flush();
    // 旧的 1 小时 meta 此刻才返回：必须被丢弃
    releaseOldMeta({ modules: [{ module: 'chat', count: 1 }], levels: [], total_events: 1, collection: {} });
    await flush();
    assert.equal(page.meta.modules.length, 1);
    assert.equal(page.meta.modules[0].module, 'planning');
    // 模块筛选不被静默清空
    assert.equal(page.filters.module, 'planning');
    assert.equal(root().querySelector('#log-module').value, 'planning');
    // facet 缺失当前模块时保留选项（不清空筛选）
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/meta') {
        return response({ modules: [{ module: 'chat', count: 1 }], levels: [], total_events: 1, collection: {} });
      }
      return response({ groups: [], has_more: false, snapshot: null });
    };
    await page.loadMeta();
    await flush();
    assert.equal(page.filters.module, 'planning', '筛选保持');
    assert.equal(root().querySelector('#log-module').value, 'planning', '控件保持');
    assert.ok(root().querySelector('#log-module').innerHTML.includes('（当前筛选）'));
  },

  async spoolLoss() {
    // BUG-15：spool 丢弃 / 损坏文件分别展示，不显示误导性的 0 丢弃
    await mountFresh();
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/meta') {
        return response({
          modules: [], levels: [], total_events: 0,
          collection: {
            enabled: true, queue_depth: 0, dropped_total: 0,
            consecutive_failures: 0, retention_days: 30,
            spool: {
              enabled: true, files: 2, corrupt_files: 2, bytes: 4096,
              replayed_total: 0, corrupt_total: 2, dropped_total: 5,
              dropped_last_at: '2026-10-10T03:00:00+00:00',
              dropped_last_reason: 'spool_capacity_rotation',
            },
          },
        });
      }
      return response({ groups: [], has_more: false, snapshot: null });
    };
    await page.loadMeta();
    await flush();
    const html = root().querySelector('#log-collection-status').innerHTML;
    assert.ok(html.includes('队列丢弃 0'), '队列丢弃分项');
    assert.ok(html.includes('缓冲丢弃 5'), 'spool 丢弃分项可见');
    assert.ok(html.includes('损坏文件 2 个'), '损坏文件按文件数展示');
    assert.ok(html.includes('容量上限被丢弃'), '丢弃警告');
    assert.ok(html.includes('无法恢复'), '损坏警告');
  },

  async detailFilters() {
    // BUG-06：详情携带列表的完整筛选与快照
    await mountFresh();
    change(root().querySelector('#log-level'), 'WARNING');
    await flush();
    assert.equal(page.snapshot, '2026-10-10T03:00:00+00:00');
    let detailParams;
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        detailParams = requestParams(request);
        return response({ group: GROUP(), samples: [] });
      }
      return response({ groups: [GROUP()], has_more: false, snapshot: '2026-10-10T03:00:00+00:00' });
    };
    await page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    assert.equal(detailParams.get('level'), 'WARNING', '详情带级别筛选');
    assert.equal(detailParams.get('snapshot'), '2026-10-10T03:00:00+00:00', '详情带快照');
    assert.ok(detailParams.get('start'), '详情带会话时间范围');
  },

  async frozenRange() {
    // BUG-05：翻页会话冻结时间范围，end 不随挂钟漂移
    await mountFresh();
    let firstEnd = null;
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs') {
        const cursor = requestParams(request).get('cursor');
        if (!cursor) {
          firstEnd = requestParams(request).get('end');
          return response({
            groups: [GROUP({ fingerprint: 'a'.repeat(16) })],
            has_more: true,
            next_cursor: '2026-10-10T03:00:00+00:00|aaaaaaaaaaaaaaaa|2026-10-10T03:00:00+00:00',
            snapshot: '2026-10-10T03:00:00+00:00',
          });
        }
        return response({ groups: [GROUP({ fingerprint: 'b'.repeat(16) })], has_more: false });
      }
      return response({ modules: [], levels: [], total_events: 0, collection: {} });
    };
    await page.refresh();
    await flush();
    click('[data-act="page-next"]');
    await flush();
    const page2 = [...requests].reverse()
      .find((r) => requestPath(r) === '/admin/api/logs' && requestParams(r).get('cursor'));
    assert.ok(page2, '第二页请求已发出');
    assert.equal(requestParams(page2).get('end'), firstEnd, '翻页复用冻结的 end');
    assert.equal(requestParams(page2).get('cursor'),
      '2026-10-10T03:00:00+00:00|aaaaaaaaaaaaaaaa|2026-10-10T03:00:00+00:00',
      'cursor 原样回传（含快照）');
    // 回看第一页：复用会话（不发请求、end / 快照不变）
    const beforeBack = requests.length;
    const endBefore = page.frozenRange && page.frozenRange.end;
    const snapshotBefore = page.snapshot;
    click('[data-act="page-prev"]');
    await flush();
    assert.equal(requests.length, beforeBack, '回看第一页不发请求（BUG-05）');
    assert.equal(page.frozenRange.end, endBefore, '冻结区间不重建（BUG-05）');
    assert.equal(page.snapshot, snapshotBefore, '快照不重建（BUG-05）');
    // 刷新建立新会话：end 重新计算
    await page.refresh();
    await flush();
    const refreshed = [...requests].reverse()
      .find((r) => requestPath(r) === '/admin/api/logs' && !requestParams(r).get('cursor'));
    assert.ok(refreshed, '刷新重新请求第一页');
    assert.ok(requestParams(refreshed).get('end'), '刷新后的第一页带 end');
  },

  async detailLifecycle() {
    // BUG-07：离开页面回收详情弹窗与在途请求；用户关闭同样清理
    await mountFresh();
    let releaseDetail;
    const detailResponse = new Promise((resolve) => { releaseDetail = resolve; });
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        return detailResponse.then(() => response({
          group: GROUP(),
          samples: [{
            event_id: '00000000-0000-0000-0000-000000000009',
            occurred_at: '2026-10-10T03:00:00+00:00', level: 'ERROR',
            module: 'planning', message_raw: 'LATE DETAIL BODY', stack_trace: '',
            request_id: 'r9', truncated_fields: [],
          }],
        }));
      }
      return response({ groups: [GROUP()], has_more: false });
    };
    // 详情请求挂起时不能整体 await openGroup：先登记、离开、再释放响应
    const detailPromise = page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    assert.equal(page.detailSessions.length, 1, '详情会话已登记');
    const modalMask = masks.at(-1);
    // 离开页面：弹窗关闭、会话清理、在途请求失效
    page.unmount();
    assert.equal(modalMask.removed, true, 'unmount 关闭详情弹窗（BUG-07）');
    assert.equal(page.detailSessions.length, 0, '详情会话已回收');
    // 晚到的详情响应不能更新已关闭的弹窗
    releaseDetail();
    await detailPromise;
    await flush();
    const body = modalMask.querySelector('.modal').querySelector('.modal-body');
    assert.ok(!body.innerHTML.includes('LATE DETAIL BODY'), '晚到详情不渲染');

    // 用户关闭（底部按钮）同样清理会话
    await mountFresh();
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        return response({
          group: GROUP(),
          samples: [{
            event_id: '00000000-0000-0000-0000-000000000010',
            occurred_at: '2026-10-10T03:00:00+00:00', level: 'ERROR',
            module: 'planning', message_raw: 'raw', stack_trace: 'trace',
            request_id: 'r10', truncated_fields: [],
          }],
        });
      }
      return response({ groups: [GROUP()], has_more: false });
    };
    await page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    assert.equal(page.detailSessions.length, 1);
    const closeButton = lastModal().querySelector('[data-close]');
    assert.equal(typeof closeButton.onclick, 'function', '关闭按钮已接线');
    closeButton.onclick();
    assert.equal(masks.at(-1).removed, true, '用户关闭移除弹窗');
    assert.equal(page.detailSessions.length, 0, '用户关闭回收会话');
  },

  async keyIdentityAndVisibleSnapshot() {
    // TASK-02：列表密钥短标识 + 用途；密钥筛选选项与请求参数；
    // BUG-05：详情携带可见性快照；详情显示完整标识；空标识如实标注。
    await mountFresh();
    page.groups = [GROUP({
      key_ref: 'kh1-abcdef1234567890abcdef1234567890',
      credential_role: 'chat', provider: 'api.example.com',
    })];
    page.renderList();
    let list = page.root.querySelector('#log-list').innerHTML;
    assert.ok(list.includes('K-abcdef12'), '列表显示密钥短标识（TASK-02）');
    assert.ok(list.includes('聊天转发'), '列表显示密钥用途（TASK-02）');
    // 密钥筛选选项来自 facets
    page.renderKeyOptions();
    const options = page.root.querySelector('#log-key').innerHTML;
    assert.ok(options.includes('K-abcdef12'), '筛选选项含短标识');
    assert.ok(options.includes('kh1-abcdef1234567890abcdef1234567890'), '选项值为完整标识');
    // 选择密钥筛选：请求带 key_ref
    change(page.root.querySelector('#log-key'), 'kh1-abcdef1234567890abcdef1234567890');
    await flush();
    const listRequest = requests.filter(r => requestPath(r) === '/admin/api/logs').at(-1);
    assert.equal(requestParams(listRequest).get('key_ref'),
      'kh1-abcdef1234567890abcdef1234567890', '列表请求带完整 key_ref（TASK-02）');
    // 可见性快照随详情回传（BUG-05）
    page.visibleSnapshot = '100:110:101';
    page.snapshot = '2026-10-10T03:00:00+00:00';
    const baseRespond = respond;
    respond = async (request) => {
      if (requestPath(request) === '/admin/api/logs/group') {
        return response({
          group: GROUP({
            key_ref: 'kh1-abcdef1234567890abcdef1234567890',
            credential_role: 'chat', provider: 'api.example.com',
          }),
          samples: [{
            event_id: '00000000-0000-0000-0000-000000000001',
            occurred_at: '2026-10-10T03:00:00+00:00', level: 'ERROR',
            module: 'planning', message_raw: 'raw', stack_trace: 'trace',
            request_id: 'r1', truncated_fields: [],
            key_ref: 'kh1-abcdef1234567890abcdef1234567890',
            credential_role: 'chat', provider: 'api.example.com',
          }],
          sample_limit: 20, filters: {}, snapshot: page.snapshot,
        });
      }
      return baseRespond(request);
    };
    await page.openGroup('aaaaaaaaaaaaaaaa');
    await flush();
    respond = baseRespond;
    const detailRequest = requests.filter(r => requestPath(r) === '/admin/api/logs/group').at(-1);
    assert.equal(requestParams(detailRequest).get('visible'), '100:110:101',
      '详情请求带可见性快照（BUG-05）');
    const modalBody = lastModal().querySelector('.modal-body').innerHTML;
    assert.ok(modalBody.includes('kh1-abcdef1234567890abcdef1234567890'), '详情显示完整标识');
    assert.ok(modalBody.includes('api.example.com'), '详情显示安全上游');
    page.detailSessions.splice(0).forEach(s => s.close());
    // 空标识：未关联 / 历史未知文案
    page.groups = [GROUP({ key_ref: '', credential_role: '', provider: '' })];
    page.renderList();
    list = page.root.querySelector('#log-list').innerHTML;
    assert.ok(list.includes('密钥未关联'), '空标识显示未关联文案');
    // 标识未就绪提示（TASK-02）
    page.meta = { collection: { enabled: true, dropped_total: 0, key_ref_ready: false,
      spool: { enabled: false, dropped_total: 0 } } };
    page.renderCollectionStatus();
    assert.ok(page.root.querySelector('#log-collection-status').innerHTML
      .includes('密钥标识未就绪'), '标识未就绪明确提示');
  },

  async purgedCorrupt() {
    // BUG-15：损坏文件被轮转清理后（当前 0），历史损失提示仍在
    await mountFresh();
    page.meta = { collection: { enabled: true, dropped_total: 0,
      spool: { enabled: true, dropped_total: 0, corrupt_files: 0,
        corrupt_total: 1, corrupt_purged_total: 1 } } };
    page.renderCollectionStatus();
    const status = page.root.querySelector('#log-collection-status').innerHTML;
    assert.ok(status.includes('损坏'), '历史损坏损失仍提示（BUG-15）');
    assert.ok(status.includes('无法恢复'), '损坏损失说明');
    assert.ok(status.includes('已随容量或保留期清理 1'), '已清理损坏文件计数可见');
    // 文件数与事件丢弃分项保留：队列丢弃 0、缓冲丢弃 0 如实显示
    assert.ok(status.includes('队列丢弃 0'), '队列丢弃分项');
    assert.ok(status.includes('缓冲丢弃 0'), '缓冲丢弃分项');
  },
};

try {
  assert.ok(checks[group], `unknown test group: ${group}`);
  await checks[group]();
  process.stdout.write(`PASS ${group}\n`);
} catch (error) {
  process.stderr.write(`${group}: ${error.stack}\n`);
  process.exitCode = 1;
}
