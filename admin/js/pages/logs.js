// pages/logs.js - 全网关报错日志：真实查询 / 筛选 / 分页 / 详情（§36 批次）
// 数据全部来自 /admin/api/logs* 只读接口；无 mock fallback。
import { gw, esc } from '../api.js?v=20261016-planning-optimization';
import {
  loading, empty, errorBlock, tag, toast, modal, delegate, icon,
} from '../ui.js?v=20261016-planning-optimization';

const LEVEL_META = {
  WARNING: { label: '警告', tone: 'amber' },
  ERROR: { label: '错误', tone: 'red' },
  CRITICAL: { label: '严重', tone: 'red' },
};
const MODULE_LABELS = {
  planning: '规划管理', memo: '备忘录', memory: '记忆', context: '上下文',
  chat: '聊天转发', database: '数据库', system: '系统', todo: '待办插件',
  eventide: '身体状态', persona: '人设', other: '其他组件',
};
const OUTCOME_LABELS = {
  degraded: { label: '已降级', tone: 'amber' },
  committed_side_effect_failed: { label: '主操作已成功，后置步骤失败', tone: 'amber' },
};
// TASK-02：密钥用途标签（与后端 credential_role 对应）
const ROLE_LABELS = {
  chat: '聊天转发', analysis: '记忆总结', continuity: '连续感',
  rumination: '反刍', memory: '记忆', database: '数据库',
};
const TIME_PRESETS = [
  { key: '1h', label: '近 1 小时', hours: 1 },
  { key: '24h', label: '近 24 小时', hours: 24 },
  { key: '7d', label: '近 7 天', hours: 24 * 7 },
  { key: '30d', label: '近 30 天', hours: 24 * 30 },
  { key: 'all', label: '全部时间' },
  { key: 'custom', label: '自定义区间' },
];
const PAGE_SIZE = 50;

function levelTag(level) {
  const meta = LEVEL_META[level] || { label: level || '-', tone: 'muted' };
  return tag(meta.label, meta.tone);
}

function moduleLabel(module) {
  return MODULE_LABELS[module] || module || '-';
}

function outcomeTag(outcome) {
  const meta = OUTCOME_LABELS[outcome];
  return meta ? tag(meta.label, meta.tone) : '';
}

// TASK-02：密钥短标识。同一批数据里 8 位前缀相撞时延长显示，避免误读；
// 聚合 / 筛选始终用完整 key_ref，不按短标识。
function keyLabel(ref, allRefs = []) {
  if (!ref) return '';
  const prefix = ref.slice(4);
  const collided = allRefs.some((other) => other !== ref && other.slice(4, 12) === prefix.slice(0, 8));
  return `K-${collided ? prefix.slice(0, 12) : prefix.slice(0, 8)}`;
}

function roleLabel(role) {
  return ROLE_LABELS[role] || role || '';
}

// 页面统一按 Asia/Shanghai 显示（数据库 UTC 存储）。
function fmtCST(value) {
  if (!value) return '-';
  try {
    return new Intl.DateTimeFormat('zh-CN', {
      timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    }).format(new Date(value));
  } catch { return String(value); }
}

function preBlock(text, { emptyText = '（无内容）' } = {}) {
  const body = text ? esc(text) : `<span class="muted">${esc(emptyText)}</span>`;
  return `<pre class="log-pre" style="white-space:pre-wrap;word-break:break-all;max-height:320px;overflow:auto;margin:4px 0 0;padding:8px 10px;background:rgba(0,0,0,.04);border-radius:6px;font-size:12px;line-height:1.5">${body}</pre>`;
}

export default {
  busy: false,
  stale: false,
  groups: null,
  meta: null,
  page: 0,
  cursors: [null],
  seq: 0,
  metaSeq: 0,
  abort: null,
  metaAbort: null,
  // 翻页会话的冻结时间范围与入库快照（BUG-05）：第一页建立，后续页与
  // 详情复用；刷新 / 筛选变更建立新会话。
  frozenRange: null,
  snapshot: null,
  // BUG-05：会话的事务可见性快照（服务端返回，随详情请求回传）——
  // 已看组的聚合在会话内冻结。
  visibleSnapshot: null,
  // TASK-02：当前密钥筛选（完整 key_ref；空 = 全部）。
  keyRef: '',
  // 会话内已获取页的结果缓存（BUG-05）：回看上一页直接复用会话数据，
  // 不再发请求——第一页的查询身份（快照 + 冻结区间）不因回看而重建。
  pageCache: {},
  // 本页打开的详情弹窗会话（BUG-07）：unmount 时统一关闭弹窗、取消在途
  // 详情请求并失效晚到回调；用户关闭同样清理。
  detailSessions: [],
  filters: { preset: '24h', start: '', end: '', module: '', level: '', search: '' },

  async mount(root) {
    this.root = root;
    // 每次进入页面统一重置查询状态（BUG-07）：页面单例在离开期间保留的
    // filters / page / cursors 会让重建的控件与实际请求不一致。重置后
    // 控件、请求参数与列表数据同源；在途旧请求按代次丢弃。
    this._resetState();
    root.innerHTML = `
      <div class="toolbar" style="flex-wrap:wrap;gap:8px">
        <label class="inline">时间
          <select id="log-preset">
            ${TIME_PRESETS.map((p) => `<option value="${p.key}"${p.key === '24h' ? ' selected' : ''}>${p.label}</option>`).join('')}
          </select>
        </label>
        <label class="inline" id="log-custom-start" hidden>开始
          <input id="log-start" type="datetime-local" style="width:180px">
        </label>
        <label class="inline" id="log-custom-end" hidden>结束
          <input id="log-end" type="datetime-local" style="width:180px">
        </label>
        <label class="inline">模块
          <select id="log-module"><option value="">全部模块</option></select>
        </label>
        <label class="inline">级别
          <select id="log-level">
            <option value="">全部级别</option>
            <option value="WARNING">警告</option>
            <option value="ERROR">错误</option>
            <option value="CRITICAL">严重错误</option>
          </select>
        </label>
        <label class="inline">密钥
          <select id="log-key"><option value="">全部密钥</option></select>
        </label>
        <label class="inline">搜索
          <input id="log-search" type="text" placeholder="错误码 / 请求ID / 异常类" style="width:170px" maxlength="100">
        </label>
        <span class="grow"></span>
        <button class="btn btn-secondary" data-act="refresh">${icon('refresh')}刷新</button>
      </div>
      <p class="muted text-sm" id="log-time-hint" style="margin:2px 0 6px"></p>
      <div id="log-collection-status"></div>
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${icon('journal')}报错日志</div>
            <div class="card-sub" data-help-note="报错日志">全网关 WARNING 及以上故障事件，按相同错误聚合；时间显示为 Asia/Shanghai。</div>
          </div>
        </div>
        <div id="log-list">${loading()}</div>
        <div id="log-pager"></div>
      </div>`;
    delegate(root, {
      refresh: (el) => { if (!el.disabled) this.refresh(); },
      'page-next': () => this.nextPage(),
      'page-prev': () => this.prevPage(),
      detail: (el) => this.openGroup(el.dataset.fingerprint),
    });
    // delegate 只捕 click；select / 输入框直接监听 change 与回车。
    root.querySelector('#log-preset').addEventListener('change', () => this.onPresetChange());
    root.querySelector('#log-module').addEventListener('change', () => this.onFilterChange());
    root.querySelector('#log-level').addEventListener('change', () => this.onFilterChange());
    root.querySelector('#log-key').addEventListener('change', () => this.onFilterChange());
    const search = root.querySelector('#log-search');
    search.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') this.onFilterChange();
    });
    await this.loadMeta();
    await this.fetchList();
  },

  unmount() {
    // 离开页面（BUG-07）：取消在途请求并失效代次，旧响应不能在新会话渲染。
    this.seq += 1;
    this.metaSeq += 1;
    if (this.abort) { try { this.abort.abort(); } catch { /* 已结束 */ } this.abort = null; }
    if (this.metaAbort) { try { this.metaAbort.abort(); } catch { /* 已结束 */ } this.metaAbort = null; }
    // BUG-07：详情弹窗独立挂在 body 上，页面卸载必须一并回收——关闭弹窗、
    // 取消在途详情请求并失效晚到回调，旧弹窗不得遮挡切换后的新页面。
    for (const session of this.detailSessions.splice(0)) {
      session.close();
    }
    this.busy = false;
    this.root = null;
  },

  _resetState() {
    this.busy = false;
    this.stale = false;
    this.groups = null;
    this.meta = null;
    this.page = 0;
    this.cursors = [null];
    this.seq += 1;      // 使在途旧请求全部失效
    this.metaSeq += 1;
    if (this.abort) { try { this.abort.abort(); } catch { /* 已结束 */ } this.abort = null; }
    if (this.metaAbort) { try { this.metaAbort.abort(); } catch { /* 已结束 */ } this.metaAbort = null; }
    for (const session of this.detailSessions.splice(0)) {
      session.close();
    }
    this.filters = { preset: '24h', start: '', end: '', module: '', level: '', search: '' };
    this.frozenRange = null;
    this.snapshot = null;
    this.visibleSnapshot = null;
    this.keyRef = '';
    this.pageCache = {};
  },

  // ── 筛选状态 ──────────────────────────────────────────────

  onPresetChange() {
    this.filters.preset = this.root.querySelector('#log-preset').value;
    const custom = this.filters.preset === 'custom';
    this.root.querySelector('#log-custom-start').hidden = !custom;
    this.root.querySelector('#log-custom-end').hidden = !custom;
    this.updateTimeHint();
    this.onFilterChange();
  },

  onFilterChange() {
    this.filters.module = this.root.querySelector('#log-module').value;
    this.filters.level = this.root.querySelector('#log-level').value;
    this.filters.search = this.root.querySelector('#log-search').value.trim();
    this.keyRef = this.root.querySelector('#log-key')?.value || '';
    this.resetPaging();
    // 自定义区间非法：提示后不发任何请求（列表与 meta 都不刷新）
    if (!this.validateCustomRange()) return;
    // 筛选变更取代在途请求（旧响应按 seq 丢弃），不能被 busy 守卫挡掉。
    this.fetchList({ supersede: true });
    this.loadMeta();
  },

  resetPaging() {
    this.page = 0;
    this.cursors = [null];
    // 新查询会话：时间范围、入库快照与可见性快照在第一页重建（BUG-05）
    this.frozenRange = null;
    this.snapshot = null;
    this.visibleSnapshot = null;
    this.pageCache = {};
  },

  refresh() {
    // 刷新保留当前筛选，重新获取第一页（建立新快照）。
    this.resetPaging();
    this.fetchList();
    this.loadMeta();
  },

  timeRange() {
    const preset = TIME_PRESETS.find((p) => p.key === this.filters.preset);
    if (!preset) return { start: '', end: '' };
    if (preset.key === 'all') return { start: '', end: '' };
    if (preset.key === 'custom') {
      const start = this.root.querySelector('#log-start').value;
      const end = this.root.querySelector('#log-end').value;
      return { start: start ? `${start}:00+08:00` : '', end: end ? `${end}:59+08:00` : '' };
    }
    const end = new Date();
    const start = new Date(end.getTime() - preset.hours * 3600 * 1000);
    return { start: start.toISOString(), end: end.toISOString() };
  },

  _activeRange() {
    // 翻页会话冻结时间范围（BUG-05）：会话内只取一次——回看第一页不再
    // 重新取 now()，end 漂移会让未查看的组跳出窗口、快照口径漂移。
    if (!this.frozenRange) {
      this.frozenRange = this.timeRange();
    }
    return this.frozenRange;
  },

  validateCustomRange() {
    if (this.filters.preset !== 'custom') return true;
    const start = this.root.querySelector('#log-start').value;
    const end = this.root.querySelector('#log-end').value;
    if (!start || !end) {
      this.showTimeHint('自定义区间需要同时填写开始与结束时间。');
      return false;
    }
    if (new Date(start) > new Date(end)) {
      this.showTimeHint('开始时间不能晚于结束时间。');
      return false;
    }
    this.showTimeHint('');
    return true;
  },

  updateTimeHint() {
    if (this.filters.preset === 'custom') {
      this.showTimeHint('自定义区间按 Asia/Shanghai 解释；留空边界表示不限制。');
    } else {
      this.showTimeHint('');
    }
  },

  showTimeHint(text) {
    const el = this.root.querySelector('#log-time-hint');
    if (el) el.textContent = text || '';
  },

  // ── 数据获取（旧请求不覆盖新条件） ────────────────────────

  async fetchList({ supersede = false } = {}) {
    if (this.busy && !supersede) return;
    if (!this.validateCustomRange()) return;
    const seq = ++this.seq;
    if (this.abort) this.abort.abort();
    const controller = new AbortController();
    this.abort = controller;
    this.busy = true;
    this.syncRefreshButton();
    const range = this._activeRange();
    const params = new URLSearchParams();
    if (this.filters.module) params.set('module', this.filters.module);
    if (this.filters.level) params.set('level', this.filters.level);
    if (this.keyRef) params.set('key_ref', this.keyRef);
    if (this.filters.search) params.set('search', this.filters.search);
    if (range.start) params.set('start', range.start);
    if (range.end) params.set('end', range.end);
    const cursor = this.cursors[this.page];
    if (cursor) params.set('cursor', cursor);
    params.set('limit', String(PAGE_SIZE));
    const list = this.root.querySelector('#log-list');
    if (!this.groups) list.innerHTML = loading();
    try {
      const data = await gw(`/admin/api/logs?${params.toString()}`, { signal: controller.signal });
      if (seq !== this.seq) return;
      this.groups = data.groups || [];
      this.stale = false;
      // 会话页缓存（BUG-05）：回看已翻过的页直接复用，不重建查询身份
      this.pageCache[this.page] = this.groups;
      // 入库快照与可见性快照随会话保存（BUG-05/06）：详情请求与列表口径一致
      if (data.snapshot) this.snapshot = data.snapshot;
      if (data.visible_snapshot) this.visibleSnapshot = data.visible_snapshot;
      if (data.has_more && data.next_cursor) {
        this.cursors[this.page + 1] = data.next_cursor;
        this.cursors.length = this.page + 2;
      } else {
        this.cursors.length = this.page + 1;
      }
      this.renderList();
      this.renderPager();
    } catch (error) {
      if (seq !== this.seq || error.name === 'AbortError') return;
      this.stale = true;
      this.renderListError(error);
      this.renderPager();
    } finally {
      if (seq === this.seq) {
        this.busy = false;
        this.syncRefreshButton();
      }
    }
  },

  async loadMeta() {
    // 元数据请求有自己的代次与取消（BUG-08）：旧范围的晚到响应不能覆盖
    // 新筛选的 facets。
    const seq = ++this.metaSeq;
    if (this.metaAbort) this.metaAbort.abort();
    const controller = new AbortController();
    this.metaAbort = controller;
    const range = this.timeRange();
    const params = new URLSearchParams();
    if (this.filters.module) params.set('module', this.filters.module);
    if (this.filters.level) params.set('level', this.filters.level);
    if (range.start) params.set('start', range.start);
    if (range.end) params.set('end', range.end);
    try {
      const data = await gw(`/admin/api/logs/meta?${params.toString()}`, { signal: controller.signal });
      if (seq !== this.metaSeq) return;
      this.meta = data;
      this.renderModuleOptions();
      this.renderKeyOptions();
      this.renderCollectionStatus();
    } catch (error) {
      if (seq !== this.metaSeq || error.name === 'AbortError') return;
      if (String(error.message).startsWith('401')) return; // 列表会给出授权提示
      this.renderCollectionStatusError(error);
    }
  },

  syncRefreshButton() {
    const btn = this.root.querySelector('[data-act="refresh"]');
    if (btn) btn.disabled = this.busy;
  },

  // ── 渲染 ──────────────────────────────────────────────────

  renderModuleOptions() {
    const select = this.root.querySelector('#log-module');
    if (!select || !this.meta) return;
    const current = this.filters.module;
    const modules = this.meta.modules || [];
    const options = modules.map((m) => {
      const label = `${moduleLabel(m.module)}（${m.count}）`;
      return `<option value="${esc(m.module)}">${esc(label)}</option>`;
    }).join('');
    // 当前筛选的模块不在 facet 里时保留该选项（BUG-08）：不得静默清空
    // 有效筛选；列表会按该模块查询并如实显示空态。
    const known = modules.some((m) => m.module === current);
    const extra = (current && !known)
      ? `<option value="${esc(current)}">${esc(moduleLabel(current))}（当前筛选）</option>`
      : '';
    select.innerHTML = `<option value="">全部模块</option>${extra}${options}`;
    select.value = current || '';
  },

  renderKeyOptions() {
    // TASK-02：密钥筛选选项来自 facets 的完整 key_ref；短标签相撞时延长。
    const select = this.root.querySelector('#log-key');
    if (!select || !this.meta) return;
    const keys = this.meta.keys || [];
    const refs = keys.map((k) => k.key_ref);
    const options = keys.map((k) => {
      const role = roleLabel(k.credential_role);
      const label = `${keyLabel(k.key_ref, refs)}${role ? ` · ${role}` : ''}（${k.count}）`;
      return `<option value="${esc(k.key_ref)}">${esc(label)}</option>`;
    }).join('');
    const current = this.keyRef;
    const known = keys.some((k) => k.key_ref === current);
    const extra = (current && !known)
      ? `<option value="${esc(current)}">${esc(keyLabel(current, [current]))}（当前筛选）</option>`
      : '';
    select.innerHTML = `<option value="">全部密钥</option>${extra}${options}`;
    select.value = current || '';
  },

  renderCollectionStatus() {
    const box = this.root.querySelector('#log-collection-status');
    if (!box) return;
    const c = this.meta?.collection || {};
    const spool = c.spool || {};
    const queueDropped = c.dropped_total ?? 0;
    const spoolDropped = spool.dropped_total ?? 0;
    const corruptFiles = spool.corrupt_files ?? 0;
    // BUG-15：损坏损失看累计口径（隔离过的文件总数 + 已清理数）——当前
    // 隔离数归零不能清除已确认的历史损失提示；文件数不冒充事件数。
    const corruptTotal = spool.corrupt_total ?? 0;
    const corruptPurged = spool.corrupt_purged_total ?? 0;
    const expiredTotal = spool.expired_total ?? 0;
    const parts = [
      c.enabled ? '采集已启用' : '采集未启用',
      `队列 ${c.queue_depth ?? 0}`,
      `已写入 ${c.written_total ?? 0}`,
      `队列丢弃 ${queueDropped}`,
    ];
    if (spool.enabled) {
      parts.push(`缓冲丢弃 ${spoolDropped}`);
      if (corruptFiles) parts.push(`损坏文件 ${corruptFiles} 个`);
    }
    if (c.last_write_at) parts.push(`最近写入 ${fmtCST(c.last_write_at)}`);
    parts.push(`本地缓冲 ${spool.enabled ? '已启用' : '未启用'}`);
    parts.push(`保留 ${c.retention_days ?? '-'} 天`);
    const warnings = [];
    if (!c.enabled) warnings.push('日志收集已关闭，可在配置页开启；这里仍可查看已保存的历史记录。');
    if (queueDropped > 0) {
      warnings.push(`已有 ${queueDropped} 条事件因队列上限被丢弃（最近原因：${esc(c.dropped_last_reason || '-')}）。`);
    }
    if (spoolDropped > 0) {
      const when = spool.dropped_last_at ? `，最近 ${fmtCST(spool.dropped_last_at)}` : '';
      warnings.push(`本地缓冲已有 ${spoolDropped} 条事件因容量上限被丢弃${when}（原因：${esc(spool.dropped_last_reason || '-')}）。`);
    }
    if (corruptTotal > 0) {
      warnings.push(`本地缓冲历史上有 ${corruptTotal} 个损坏文件被隔离（当前仍隔离 ${corruptFiles} 个、已随容量或保留期清理 ${corruptPurged} 个），其中的事件无法恢复。`);
    } else if (corruptFiles > 0) {
      warnings.push(`本地缓冲有 ${corruptFiles} 个损坏文件被隔离，其中的事件无法恢复。`);
    }
    if (expiredTotal > 0) {
      const when = spool.expired_last_at ? `，最近 ${fmtCST(spool.expired_last_at)}` : '';
      warnings.push(`本地缓冲已有 ${expiredTotal} 条事件超过保留期被清理${when}。`);
    }
    if ((c.consecutive_failures ?? 0) > 0) {
      warnings.push(`日志写入连续失败 ${c.consecutive_failures} 次，正在退避重试。`);
    }
    // TASK-02：标识秘密未就绪——密钥区分不可用，明确提示（不伪造可用）。
    if (c.key_ref_ready === false) {
      warnings.push('密钥标识未就绪（GATEWAY_KEY_REF_SECRET 缺失或短于 32 字节）：日志暂不能按密钥区分故障，业务与日志采集不受影响。');
    }
    box.innerHTML = `
      <p class="muted text-sm" style="margin:0 0 6px">${esc(parts.join(' · '))}</p>
      ${warnings.map((w) => `<div class="banner" style="margin-bottom:6px"><span class="banner-ico">${icon('alert')}</span><div class="text-sm">${w}</div></div>`).join('')}`;
  },

  renderCollectionStatusError(error) {
    const box = this.root.querySelector('#log-collection-status');
    if (!box) return;
    box.innerHTML = `<p class="muted text-sm" style="margin:0 0 6px;color:var(--amber)">采集状态读取失败：${esc(error.message)}</p>`;
  },

  renderList() {
    const box = this.root.querySelector('#log-list');
    if (!this.groups.length) {
      box.innerHTML = empty('当前筛选下暂无报错记录', '开启日志收集后，新的警告和错误会在这里显示');
      return;
    }
    const refs = this.groups.map((g) => g.key_ref).filter(Boolean);
    box.innerHTML = this.groups.map((g) => `
      <div class="mem-card" data-act="detail" data-fingerprint="${esc(g.fingerprint)}">
        <div class="card-top">
          <div class="card-main">
            <div class="mem-title">${levelTag(g.level)} ${tag(esc(moduleLabel(g.module)), 'slate')} <span class="copyable-text">${esc(g.message_zh || '(无摘要)')}</span></div>
            <div class="card-meta">
              ${esc(g.error_code || '-')} · ${esc(g.operation || '-')}
              · 共 ${esc(g.event_count ?? 0)} 次
              · 首次 ${esc(fmtCST(g.first_seen_at))}
              · 最近 ${esc(fmtCST(g.last_seen_at))}
              ${g.exception_type ? ` · ${esc(g.exception_type)}` : ''}
              ${g.key_ref ? ` · 密钥 ${esc(keyLabel(g.key_ref, refs))}${g.credential_role ? `（${esc(roleLabel(g.credential_role))}）` : ''}` : ' · 密钥未关联'}
              ${outcomeTag(g.outcome)}
            </div>
          </div>
          <div class="card-side"><button class="btn btn-quiet btn-sm">${icon('info')}详情</button></div>
        </div>
      </div>`).join('');
  },

  renderListError(error) {
    const box = this.root.querySelector('#log-list');
    const message = String(error.message || '');
    if (message.startsWith('401')) {
      box.innerHTML = errorBlock('登录已过期或令牌无效，请重新登录后再查询日志。');
      return;
    }
    const staleNote = this.groups?.length
      ? `<div class="banner" style="margin-bottom:8px"><span class="banner-ico">${icon('alert')}</span><div class="text-sm">下方为上次成功获取的数据，可能已过时。</div></div>`
      : '';
    box.innerHTML = `
      ${staleNote}
      ${errorBlock(`日志查询失败：${esc(message)}`)}
      <div style="margin-top:8px"><button class="btn btn-secondary btn-sm" data-act="refresh">${icon('refresh')}重试</button></div>
      ${this.groups?.length ? this.renderGroupCards() : ''}`;
  },

  renderGroupCards() {
    return this.groups.map((g) => `
      <div class="mem-card" style="opacity:.65" data-act="detail" data-fingerprint="${esc(g.fingerprint)}">
        <div class="mem-title">${levelTag(g.level)} <span class="copyable-text">${esc(g.message_zh || '(无摘要)')}</span></div>
        <div class="card-meta">${esc(g.error_code || '-')} · 共 ${esc(g.event_count ?? 0)} 次 · 最近 ${esc(fmtCST(g.last_seen_at))}</div>
      </div>`).join('');
  },

  renderPager() {
    const box = this.root.querySelector('#log-pager');
    if (!box) return;
    const hasPrev = this.page > 0;
    const hasNext = this.cursors.length > this.page + 1;
    if (!hasPrev && !hasNext) {
      box.innerHTML = '';
      return;
    }
    box.innerHTML = `
      <div class="pagination">
        ${hasPrev ? `<button class="btn btn-secondary btn-sm" data-act="page-prev">上一页</button>` : ''}
        <span class="page-info">第 ${this.page + 1} 页${this.stale ? ' · 数据可能已过时' : ''}</span>
        ${hasNext ? `<button class="btn btn-secondary btn-sm" data-act="page-next">下一页</button>` : ''}
      </div>`;
  },

  nextPage() {
    if (this.busy || this.cursors.length <= this.page + 1) return;
    this.page += 1;
    this.fetchList();
  },

  prevPage() {
    if (this.busy || this.page <= 0) return;
    this.page -= 1;
    const cached = this.pageCache[this.page];
    if (cached) {
      // BUG-05：回看已翻过的页复用会话内已获取的结果——不发请求，快照与
      // 冻结区间保持不变；只有刷新 / 筛选变更才建立新会话。
      this.groups = cached;
      this.renderList();
      this.renderPager();
      return;
    }
    this.fetchList();
  },

  // ── 详情（汇总组 + 有限样本） ─────────────────────────────

  async openGroup(fingerprint) {
    if (!fingerprint || !this.root) return;
    // BUG-06：详情复用列表会话的完整筛选与快照——次数与样本和列表一致，
    // 不静默扩大到整组。BUG-05：可见性快照随详情回传，已看组聚合冻结。
    const range = this.frozenRange || this.timeRange();
    const params = new URLSearchParams({ fingerprint });
    if (range.start) params.set('start', range.start);
    if (range.end) params.set('end', range.end);
    if (this.filters.module) params.set('module', this.filters.module);
    if (this.filters.level) params.set('level', this.filters.level);
    if (this.filters.search) params.set('search', this.filters.search);
    if (this.keyRef) params.set('key_ref', this.keyRef);
    if (this.snapshot) params.set('snapshot', this.snapshot);
    if (this.visibleSnapshot) params.set('visible', this.visibleSnapshot);
    // BUG-07：详情弹窗与请求登记到页面生命周期——离开页面关闭弹窗、取消
    // 在途请求并失效晚到回调；用户关闭（X / 遮罩 / 底部按钮）同样清理。
    const controller = new AbortController();
    const session = { done: false, controller, close: null };
    const { root: mroot, close, mask } = modal({
      title: '报错详情',
      body: loading(),
      footer: '<button class="btn btn-secondary" data-close>关闭</button>',
      wide: true,
      draggable: true,
      onMaskClose: () => session.close(),
    });
    session.close = () => {
      if (session.done) return;
      session.done = true;
      try { controller.abort(); } catch { /* 已结束 */ }
      this.detailSessions = this.detailSessions.filter((item) => item !== session);
      close();
    };
    mask.querySelector('.modal-close').onclick = session.close;
    mroot.querySelector('[data-close]').onclick = session.close;
    this.detailSessions.push(session);
    try {
      const data = await gw(`/admin/api/logs/group?${params.toString()}`, { signal: controller.signal });
      if (session.done) return; // 已关闭或页面已离开：晚到响应不渲染
      mroot.querySelector('.modal-body').innerHTML = this.renderGroupDetail(data);
      // 弹窗挂在 body 上（页面 delegate 捕获不到），复制按钮单独接线。
      const latest = (data.samples || [])[0] || {};
      const group = data.group || {};
      mroot.querySelectorAll('[data-copy]').forEach((btn) => {
        btn.onclick = () => this.copyText(btn.dataset.copy, latest, group);
      });
    } catch (error) {
      if (session.done || error.name === 'AbortError') return;
      const message = String(error.message || '').startsWith('401')
        ? '登录已过期或令牌无效。'
        : `详情读取失败：${esc(error.message)}`;
      mroot.querySelector('.modal-body').innerHTML = errorBlock(message);
    }
  },

  renderGroupDetail(data) {
    const g = data.group || {};
    const samples = data.samples || [];
    const latest = samples[0] || {};
    const truncatedNote = (latest.truncated_fields || []).length
      ? `<div class="banner" style="margin:8px 0"><span class="banner-ico">${icon('info')}</span><div class="text-sm">以下原文/堆栈已截断（${esc((latest.truncated_fields || []).join('、'))}），完整内容以数据库上限为准。</div></div>`
      : '';
    // TASK-02：密钥标识与实际来源（与配置页同一标识）；未关联 / 历史未知
    // 如实标注，不冒充当前密钥。
    const keyRow = g.key_ref
      ? `<div class="kv"><span class="k">关联密钥</span><span class="v mono text-sm">${esc(keyLabel(g.key_ref, [g.key_ref]))} · ${esc(g.key_ref)}${g.credential_role ? ` · ${esc(roleLabel(g.credential_role))}` : ''}${g.provider ? ` · ${esc(g.provider)}` : ''} <button class="btn btn-quiet btn-sm" data-copy="key">${icon('copy')}复制标识</button></span></div>`
      : `<div class="kv"><span class="k">关联密钥</span><span class="v">${tag('未关联密钥（或历史记录：密钥未知）', 'muted')}</span></div>`;
    const sampleRows = samples.map((s) => `
      <div class="mem-card" style="cursor:default">
        <div class="mem-title">${levelTag(s.level)} ${esc(fmtCST(s.occurred_at))}</div>
        <div class="card-meta">
          request_id <span class="mono">${esc(s.request_id || '-')}</span>
          ${s.operation_id ? ` · operation_id <span class="mono">${esc(s.operation_id)}</span>` : ''}
          ${s.route ? ` · ${esc(s.method || '')} ${esc(s.route)}` : ''}
          ${s.http_status ? ` · HTTP ${esc(s.http_status)}` : ''}
          ${s.upstream_status ? ` · 上游 ${esc(s.upstream_status)}` : ''}
          ${s.key_ref ? ` · 密钥 ${esc(keyLabel(s.key_ref, [s.key_ref]))}` : ''}
          ${outcomeTag(s.outcome)}
        </div>
        ${s.source ? `<div class="card-meta mono">${esc(s.source)}</div>` : ''}
      </div>`).join('');
    return `
      <div class="kv"><span class="k">中文摘要</span><span class="v copyable-text">${esc(g.message_zh || '-')}</span></div>
      <div class="kv"><span class="k">级别</span><span class="v">${levelTag(g.level)}</span></div>
      <div class="kv"><span class="k">模块 / 操作</span><span class="v">${esc(moduleLabel(g.module))} · ${esc(g.operation || '-')}</span></div>
      <div class="kv"><span class="k">错误码</span><span class="v mono">${esc(g.error_code || '-')}</span></div>
      <div class="kv"><span class="k">异常类</span><span class="v mono">${esc(g.exception_type || '（无异常对象）')}</span></div>
      <div class="kv"><span class="k">发生次数</span><span class="v">${esc(g.event_count ?? 0)} 次（首次 ${esc(fmtCST(g.first_seen_at))} ～ 最近 ${esc(fmtCST(g.last_seen_at))}）</span></div>
      ${keyRow}
      <div class="kv"><span class="k">logger / 来源</span><span class="v mono text-sm">${esc(g.logger_name || '-')} · ${esc(g.source || '-')}</span></div>
      ${g.instance_id ? `<div class="kv"><span class="k">实例</span><span class="v mono">${esc(g.instance_id)}</span></div>` : ''}
      <div class="section-title">脱敏原文（最近一次事件）</div>
      ${preBlock(latest.message_raw, { emptyText: '（无原文）' })}
      <div style="margin-top:4px"><button class="btn btn-quiet btn-sm" data-copy="raw">${icon('copy')}复制原文</button></div>
      <div class="section-title">异常堆栈（最近一次事件）</div>
      ${latest.stack_trace ? preBlock(latest.stack_trace) : '<p class="muted text-sm">此记录未附异常堆栈（warning 或无异常对象的记录没有堆栈）。</p>'}
      ${latest.stack_trace ? `<div style="margin-top:4px"><button class="btn btn-quiet btn-sm" data-copy="stack">${icon('copy')}复制堆栈</button></div>` : ''}
      ${truncatedNote}
      <div class="section-title">关联样本（最近 ${esc(samples.length)} / 共 ${esc(g.event_count ?? 0)} 次）</div>
      <p class="muted text-sm" data-help-note="日志样本">样本只列出最近事件；不同 request_id 的其余发生不在样本内，不代表同一来源。</p>
      ${sampleRows || '<p class="muted">没有可显示的样本。</p>'}`;
  },

  copyText(target, latest, group) {
    let text;
    if (target === 'stack') text = latest?.stack_trace || '';
    else if (target === 'raw') text = latest?.message_raw || '';
    else if (target === 'key') text = group?.key_ref || '';
    if (!text) { toast('没有可复制的内容', 'err'); return; }
    const done = () => toast('已复制到剪贴板');
    const fail = () => toast('复制失败，请手动选择文本复制', 'err');
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done, fail);
    } else { fail(); }
  },
};
