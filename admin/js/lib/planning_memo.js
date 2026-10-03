// lib/planning_memo.js - 备忘录模块（规划管理页签内，一期）
//
// 需求依据《备忘录一期完整需求/需求规范.md》（2026-10-02 确认）：
// - 首页按标签分板块纵向排列，每板块预览 ≤5 条（常驻优先、空位补当前
//   模式下的随笔），条目显示标题 + 一行正文（溢出省略），点击阅读完整
//   Markdown 正文；
// - 标签详情 = 完整列表（不受 5 条预览限制），常驻/随笔分区可分别拖动；
// - 随笔默认创建时间倒序，拖动进入手动模式，可切回最新并保留手动顺序，
//   首页补位跟随当前模式；手动模式新增成员放末尾；
// - 标签筛选保留板块展示；文字搜索显示去重匹配片段，清除后恢复原排列；
// - 自动保存：保存中 / 已保存 / 保存失败明确反馈，失败保留输入可重试，
//   只有最新修改真正保存成功才显示「已保存」；
// - 归档 / 删除作用于整条记录（所有标签同步生效），回收站承接恢复。
//
// 一期不实现清单勾选、待办互通与 AI 读写（F01–F03 仅预留稳定身份与
// 可复用基础接口）。

import { gw } from '../api.js?v=20261003-memo-review-fixes1';
import {
  loading, empty, errorBlock, tag, toast, modal, confirm, icon, esc,
} from '../ui.js?v=20261003-memo-review-fixes1';
import { createRetroSelectField } from './retro_select.js?v=20261003-memo-review-fixes1';
import { renderMarkdown } from './memo_markdown.js?v=20261003-memo-review-fixes1';

const KIND_LABELS = { pinned: '常驻备忘', note: '随笔' };
const AUTOSAVE_DELAY_MS = 800;
const SEARCH_DEBOUNCE_MS = 300;
// 视图 → 生命周期列表 API 的 status（F09：回收站恢复后不能用 view 名当 status）
const VIEW_STATUS = { archived: 'archived', trash: 'deleted' };

/* ═══════════════ 自动保存泵（独立工厂，quickjs 行为测试直接执行） ═══════════════
 *
 * 连续输入合并为一次保存（debounce）；保存串行化——同一时刻至多一个在途
 * 请求，请求乱序不可能发生；保存期间的新修改在本次成功后基于最新版本继续
 * 发送。只有「没有遗留修改的成功保存」才上报 saved。
 *
 * send(snapshot, { keepalive }) => Promise<result>，result 约定：
 *   - 正常对象（含 id/content_version）：本次内容已持久化；
 *   - { applied: false }：同幂等键返回了首次记录，本次草稿没有入库
 *     （F02）——泵保留快照继续走 PATCH；
 *   - { suppressSaved: true }：保存动作已完成但当前草稿不需要「已保存」
 *     反馈（如正文被清空后清理，F08）——泵停止并不上报 saved。
 *
 * schedule/cancel 可注入（测试用假时钟）；默认全局 setTimeout/clearTimeout。
 */
export function createMemoAutosave({
  send, schedule = (fn, ms) => setTimeout(fn, ms),
  cancel = (handle) => clearTimeout(handle), delay = AUTOSAVE_DELAY_MS,
  onChange = () => {},
} = {}) {
  const state = {
    status: 'idle',            // idle | pending | saving | saved | error | conflict | lifecycle
    pendingSnapshot: null,     // 最新未保存快照（连续输入只保留最新一份）
    savingSnapshot: null,      // 在途保存的快照
    error: null,
    dirty: false,
    running: false,
    timer: null,
    flushWaiters: [],
    emptied: false,            // cancelPending 撤下快照后不误报 saved（F08）
    suppressFailed: false,     // 撤下后在途保存失败：失败快照不代表当前草稿
    stopping: false,           // 卸载路径接管后泵不再另发请求（F03）
  };

  function setStatus(status, error = null) {
    state.status = status;
    state.error = error;
    onChange({ status, error, hasPending: state.dirty });
  }

  function resolveFlushWaiters() {
    const waiters = state.flushWaiters;
    state.flushWaiters = [];
    for (const resolve of waiters) resolve();
  }

  async function pump() {
    if (state.running || state.stopping) return;
    state.running = true;
    try {
      while (true) {
        if (!state.dirty || !state.pendingSnapshot) {
          if (!state.dirty) setStatus(state.emptied ? 'idle' : 'saved');
          break;
        }
        const snapshot = state.pendingSnapshot;
        state.pendingSnapshot = null;
        state.dirty = false;   // 取走最新快照；期间的新输入会再次置位
        state.emptied = false;
        state.savingSnapshot = snapshot;
        setStatus('saving');
        try {
          const result = await send(snapshot, {});
          state.savingSnapshot = null;
          if (result && result.suppressSaved) {
            // 保存链路已按当前草稿处理完毕（如清空后的清理，F08）：
            // 不上报「已保存」；期间若有新输入则继续排空
            if (state.dirty) {
              setStatus('pending');
              continue;
            }
            setStatus('idle');
            break;
          }
          if (result && result.applied === false) {
            // 幂等键返回了首次记录：记录已存在但本次草稿没有入库（F02），
            // 保留最新快照，下一轮基于已采纳的记录走 PATCH
            state.pendingSnapshot = state.pendingSnapshot
              ? { ...snapshot, ...state.pendingSnapshot }
              : snapshot;
            state.dirty = true;
            setStatus('pending');
            continue;
          }
          if (state.dirty) {
            setStatus('pending');   // 保存期间用户继续输入：接续保存最新快照
            continue;
          }
        } catch (error) {
          state.savingSnapshot = null;
          if (state.suppressFailed) {
            // 快照已被撤下（用户清空正文）：失败的是旧输入，不代表当前
            // 草稿，不复活、不报错（F08）
            state.suppressFailed = false;
            setStatus('idle');
            break;
          }
          // 失败保留输入：快照合并回待保存集合（期间的新输入优先），等待重试
          const failed = snapshot;
          state.pendingSnapshot = state.pendingSnapshot
            ? { ...failed, ...state.pendingSnapshot }
            : failed;
          state.dirty = true;
          const code = error && error.code;
          if (code === 'version_conflict') setStatus('conflict', error);
          else if (code === 'lifecycle_conflict') setStatus('lifecycle', error);
          else setStatus('error', error);
          break;
        }
      }
    } finally {
      state.running = false;
      resolveFlushWaiters();
    }
  }

  const autosave = {
    markDirty(snapshot) {
      state.emptied = false;
      state.suppressFailed = false;
      state.pendingSnapshot = snapshot;
      state.dirty = true;
      if (state.status !== 'saving') setStatus('pending');
      if (state.timer != null) cancel(state.timer);
      state.timer = schedule(() => {
        state.timer = null;
        return pump();
      }, delay);
    },

    /** 撤下尚未发出的待保存快照（F08：新建正文被清空时不触发创建）。
     *  在途保存不受影响——其结果由 send 侧按当前草稿裁决。 */
    cancelPending() {
      if (state.timer != null) {
        cancel(state.timer);
        state.timer = null;
      }
      state.pendingSnapshot = null;
      state.dirty = false;
      state.emptied = true;
      if (state.running) state.suppressFailed = true;
      else setStatus('idle');
    },

    /** 立即排空队列（关编辑器 / 切页签）；resolve 时队列已排空或停在失败态。 */
    flush({ keepalive = false } = {}) {
      if (state.timer != null) {
        cancel(state.timer);
        state.timer = null;
      }
      if (keepalive) {
        // 卸载路径（F03）：不能与在途保存并发提交两个同版本的不同草稿——
        // 那是版本竞争，输的一方丢稿。在途保存存在时不另发 keepalive，
        // 最新草稿由本地草稿槽持久保存、重开后恢复；无在途时原子取走
        // 待保存快照立即发送，并置 stopping 让泵不再发出第二个请求。
        state.stopping = true;
        if (!state.savingSnapshot && state.pendingSnapshot) {
          const snapshot = state.pendingSnapshot;
          state.pendingSnapshot = null;
          state.dirty = false;
          // keepalive 的结果不在卸载路径上等待；传输失败（含 bfcache 冻结
          // 中的中止）不能变成悬空 rejection——草稿槽已兜底，重开后恢复
          try {
            Promise.resolve(send(snapshot, { keepalive: true }))
              .catch(() => { /* 卸载路径尽力而为 */ });
          } catch { /* 卸载路径尽力而为 */ }
        }
        return Promise.resolve();
      }
      if (state.stopping) return Promise.resolve();
      if (state.running) {
        return new Promise((resolve) => state.flushWaiters.push(resolve));
      }
      if (!state.dirty) return Promise.resolve();
      return pump();
    },

    /** bfcache 返回（pageshow persisted=true）：解除 pagehide 的停机标记
     *  并继续排空未发出的待保存快照（BUG-03/R04）。返回不代表卸载——
     *  pagehide 只能暂停保存泵，pageshow 必须恢复它。在途保存若在冻结
     *  期间被中止，其失败由泵的失败路径接管（快照并回、状态置错），这里
     *  不重复处理；有遗留待保存快照时立即重新调度。 */
    resume() {
      if (!state.stopping) return;
      state.stopping = false;
      if (!state.running && state.dirty && state.pendingSnapshot) {
        state.timer = schedule(() => {
          state.timer = null;
          return pump();
        }, 0);
      }
    },

    /** 手动重试（含「覆盖保存」：调用方先把 expected_version 对齐服务端）。 */
    retry() {
      if (state.timer != null) {
        cancel(state.timer);
        state.timer = null;
      }
      if (state.running || state.stopping) return;
      state.emptied = false;
      return pump();
    },

    isBusy() {
      return state.running || state.dirty;
    },

    dispose() {
      if (state.timer != null) cancel(state.timer);
      state.timer = null;
      state.pendingSnapshot = null;
      state.dirty = false;
      resolveFlushWaiters();
    },
  };
  return autosave;
}

/* ═══════════════ 本地草稿槽（硬卸载后最新未确认草稿的恢复路径，F03） ═══════════════
 *
 * 每次输入同步落 localStorage；刷新/关页时若在途保存竞争导致最新草稿没有
 * 入库，重开编辑器时按条目身份与版本恢复。关闭编辑器（含确认丢弃）即清除。
 */
const DRAFT_KEY = 'qi-memo-editor-draft';
const DRAFT_TTL_MS = 24 * 60 * 60 * 1000;

function readStoredDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_KEY);
    if (!raw) return null;
    const draft = JSON.parse(raw);
    if (!draft || typeof draft !== 'object') return null;
    if (!draft.savedAt || Date.now() - draft.savedAt > DRAFT_TTL_MS) return null;
    return draft;
  } catch { return null; }
}

function writeStoredDraft(draft) {
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); } catch { /* 隐私模式：仅失去恢复路径 */ }
}

function clearStoredDraft() {
  try { localStorage.removeItem(DRAFT_KEY); } catch { /* 同上 */ }
}

/* ═══════════════ 把手拖拽（手机用明确把手；把手外的页面正常滚动/点按） ═══════════════ */

function startHandleDrag(e, handle, list, item, onMoved) {
  e.preventDefault();
  item.classList.add('is-dragging');
  // 捕获让 pointermove 在指针离开把手时仍送达；合成指针等场景捕获可能
  // 失败或中途释放（见下），由 window 级兜底保证收尾
  try { handle.setPointerCapture(e.pointerId); } catch { /* 无捕获也能在本行内拖动 */ }
  let finished = false;
  let anchorY = e.clientY;
  let moved = false;

  const cleanup = () => {
    item.classList.remove('is-dragging');
    item.style.transform = '';
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', onEnd);
    window.removeEventListener('pointercancel', onCancel);
    handle.removeEventListener('lostpointercapture', onEnd);
    document.body.classList.remove('memo-dragging');
  };
  const finish = () => {
    if (finished) return;
    finished = true;
    cleanup();
    if (moved) onMoved(item);
  };
  const onMove = (ev) => {
    item.style.transform = `translate(0, ${ev.clientY - anchorY}px)`;
    const rect = item.getBoundingClientRect();
    const mid = rect.top + rect.height / 2;
    // 只与同列表的直接成员比较（F05）：板块列表下的嵌套条目不参与板块移动
    for (const sib of [...list.querySelectorAll(':scope > [data-drag-item]')]) {
      if (sib === item) continue;
      const sr = sib.getBoundingClientRect();
      const sibMid = sr.top + sr.height / 2;
      const itemAfterSib = sib.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_PRECEDING;
      if (mid < sibMid && !itemAfterSib) {
        list.insertBefore(item, sib);
        anchorY = ev.clientY;
        item.style.transform = '';
        moved = true;
        break;
      }
      if (mid > sibMid && itemAfterSib) {
        list.insertBefore(item, sib.nextSibling);
        anchorY = ev.clientY;
        item.style.transform = '';
        moved = true;
        break;
      }
    }
  };
  const onEnd = () => finish();
  const onCancel = () => finish();
  document.body.classList.add('memo-dragging');
  // move/up 统一挂 window：指针捕获生效时事件经冒泡到达，捕获意外释放
  // （DOM 移动 / 系统手势）时也照常到达——不依赖捕获是否存活
  window.addEventListener('pointermove', onMove);
  window.addEventListener('pointerup', onEnd);
  window.addEventListener('pointercancel', onCancel);
  handle.addEventListener('lostpointercapture', onEnd);
}

/* ═══════════════ 主模块 ═══════════════ */

export function createPlanningMemo() {
  const memo = {
    root: null,
    body: null,
    statusHost: null,
    filterInput: null,
    view: 'board',            // board | tag | search | archived | trash
    board: null,
    tags: [],
    filterTagId: '',          // '' 全部 | 'untagged' | 数字
    activeTagId: null,        // 标签详情当前标签（'untagged' 或数字）
    searchQuery: '',
    searchResults: [],
    seq: 0,
    searchSeq: 0,
    searchTimer: null,
    suppressClick: false,
    pagehideHandler: null,
    pageshowHandler: null,
    dragPointerHandler: null,
    reorderBusy: false,       // 重排保存/刷新期间暂停下一次拖动（F11）
    _openingEditor: false,    // 编辑器打开在途去重（F01 连点）
    editor: null,
    _saveState: { status: 'idle', error: null },

    async mount(root) {
      this.root = root;
      root.innerHTML = `
        <div class="memo-toolbar">
          <button class="btn btn-primary" data-act="memo-new">${icon('plus')}新建备忘录</button>
          <span class="retro-select" data-memo-filter></span>
          <div class="search-box">
            ${icon('search')}
            <input type="text" data-memo-search placeholder="搜索标题或正文" aria-label="搜索备忘录">
          </div>
          <button class="btn btn-quiet btn-sm" data-act="memo-archived">${icon('archive')}归档</button>
          <button class="btn btn-quiet btn-sm" data-act="memo-trash">${icon('x')}回收站</button>
        </div>
        <div data-memo-status></div>
        <div class="memo-body" data-memo-body>${loading()}</div>`;

      this.body = root.querySelector('[data-memo-body]');
      this.statusHost = root.querySelector('[data-memo-status]');

      root.addEventListener('click', (e) => this.handleClick(e));
      root.querySelector('[data-memo-search]').addEventListener('input', (e) => {
        const value = e.target.value;
        if (this.searchTimer) clearTimeout(this.searchTimer);
        this.searchTimer = setTimeout(() => {
          this.searchTimer = null;
          this.searchQuery = value.trim();
          if (this.searchQuery) {
            this.switchView('search');
            this.runSearch();
          } else {
            // 清除搜索恢复原排列（§5）
            this.switchView('board');
            this.render();
          }
        }, SEARCH_DEBOUNCE_MS);
      });

      // 拖拽：只在「拖动把手」上接管指针；页面其余部分正常滚动与点按
      this.dragPointerHandler = (e) => this.handleDragPointerDown(e);
      root.addEventListener('pointerdown', this.dragPointerHandler);

      this.pagehideHandler = () => {
        if (this.editor) this.editor.autosave.flush({ keepalive: true });
      };
      window.addEventListener('pagehide', this.pagehideHandler);
      // bfcache 往返（BUG-03/R04）：pagehide 会把保存泵置为停机；页面从
      // 历史缓存恢复时必须解除停机并继续排空待保存快照，否则返回后的新
      // 输入永远不会保存、关闭时还会把未确认草稿当作已排空清掉。
      this.pageshowHandler = (event) => {
        if (event.persisted && this.editor) this.editor.autosave.resume();
      };
      window.addEventListener('pageshow', this.pageshowHandler);

      await this.reload();
    },

    /** 切到备忘录页签时由 planning 页调用（数据按需加载）。 */
    async show() {
      if (!this.root) return;
      await this.reload();
    },

    dispose() {
      if (this.searchTimer) clearTimeout(this.searchTimer);
      this.searchTimer = null;
      if (this.pagehideHandler) {
        window.removeEventListener('pagehide', this.pagehideHandler);
        this.pagehideHandler = null;
      }
      if (this.pageshowHandler) {
        window.removeEventListener('pageshow', this.pageshowHandler);
        this.pageshowHandler = null;
      }
      if (this.editor) {
        const editor = this.editor;
        this.editor = null;
        // 卸载前尽力保存未落库内容（keepalive；在途竞争时由草稿槽兜底）
        editor.autosave.flush({ keepalive: true });
        editor.autosave.dispose();
        try { editor.modal.close(); } catch { /* 已移除 */ }
      }
      this.root = null;
      this.body = null;
      this.statusHost = null;
      this.board = null;
    },

    /* ---------- 数据 ---------- */

    /** 视图切换统一入口：使在途的旧视图加载失效（F10）。
     *  reload / renderLifecycleList 以 seq 识别过期响应。 */
    switchView(view) {
      this.view = view;
      this.seq += 1;
    },

    async reload({ silent = false } = {}) {
      const seq = ++this.seq;
      try {
        const [board, tags] = await Promise.all([
          gw('/admin/api/memo/board'),
          gw('/admin/api/memo/tags'),
        ]);
        if (seq !== this.seq || !this.root) return;
        this.board = board;
        this.tags = tags;
        this.mountFilter();
        if (this.view === 'search' || this.view === 'archived' || this.view === 'trash') {
          return;   // 非看板视图由各自流程刷新（F12：搜索按当前关键词重查）
        }
        this.render();
      } catch (error) {
        if (seq !== this.seq || !this.root || silent) return;
        this.statusHost.innerHTML = errorBlock(`备忘录读取失败：${esc(error.message)}`);
      }
    },

    /** 写操作（编辑关闭/归档/删除/恢复）后的统一刷新：
     *  看板与标签详情由 reload 渲染，搜索按当前关键词重查（F12），
     *  生命周期列表按正确的 status 重读（F09）。 */
    async refreshAfterWrite() {
      await this.reload({ silent: true });
      if (!this.root) return;
      if (this.view === 'search') {
        await this.runSearch();
      } else if (this.view === 'archived' || this.view === 'trash') {
        this.renderLifecycleList(VIEW_STATUS[this.view]);
      }
    },

    async runSearch() {
      const seq = ++this.searchSeq;
      try {
        const results = await gw(
          `/admin/api/memo/entries?status=active&q=${encodeURIComponent(this.searchQuery)}`);
        if (seq !== this.searchSeq || !this.root) return;   // 旧响应不覆盖新页面
        this.searchResults = results;
        this.render();
      } catch (error) {
        if (seq !== this.searchSeq || !this.root) return;
        this.searchResults = [];
        this.statusHost.innerHTML = errorBlock(`搜索失败：${esc(error.message)}`);
      }
    },

    /* ---------- 渲染 ---------- */

    mountFilter() {
      const host = this.root?.querySelector('[data-memo-filter]');
      if (!host) return;
      const options = [
        { value: '', label: '全部标签' },
        { value: 'untagged', label: '未分类' },
        ...this.tags.map((t) => ({ value: String(t.id), label: t.name })),
      ];
      host.innerHTML = '';
      const input = createRetroSelectField(host, {
        id: 'memo-filter-tag',
        value: String(this.filterTagId),
        options,
      });
      host.querySelector('button').setAttribute('aria-label', '标签筛选');
      input.addEventListener('change', () => {
        const value = input.value;
        this.filterTagId = value === '' ? '' : (value === 'untagged' ? 'untagged' : Number(value));
        this.switchView('board');
        this.render();
      });
      this.filterInput = input;
    },

    currentSections() {
      if (!this.board) return [];
      let sections = this.board.sections || [];
      if (this.filterTagId === 'untagged') {
        sections = sections.filter((s) => s.tag === null);
      } else if (this.filterTagId !== '') {
        sections = sections.filter((s) => s.tag && s.tag.id === this.filterTagId);
      }
      return sections;
    },

    render() {
      if (!this.root || !this.body) return;
      if (this.view === 'board') return this.renderBoard();
      if (this.view === 'tag') return this.renderTagDetail();
      if (this.view === 'search') return this.renderSearch();
      if (this.view === 'archived') return this.renderLifecycleList(VIEW_STATUS.archived);
      if (this.view === 'trash') return this.renderLifecycleList(VIEW_STATUS.trash);
    },

    sectionLabel(section) {
      return section.tag ? section.tag.name : '未分类';
    },

    renderBoard() {
      const sections = this.currentSections();
      if (!sections.length) {
        // 空标签也要有可达的删除入口（F13）：标签可能只有归档/回收站内容
        // 或尚未关联正常内容，不构成看板板块，此前无处可删。
        const filterTagId = this.filterTagId;
        const deletableTag = (filterTagId !== '' && filterTagId !== 'untagged')
          ? this.tags.find((t) => t.id === Number(filterTagId))
          : null;
        this.body.innerHTML = empty(
          '还没有备忘录内容',
          this.filterTagId === '' ? '点击「新建备忘录」开始记录' : '该标签下暂无内容',
        ) + (deletableTag ? `
          <div class="memo-empty-actions">
            <button class="btn btn-danger-line btn-sm" data-act="memo-delete-tag" data-tag-id="${deletableTag.id}">${icon('x')}删除标签「${esc(deletableTag.name)}」</button>
          </div>` : '');
        return;
      }
      const tagDragEnabled = this.filterTagId === '';
      // 板块节点声明 data-drag-item（F05）：把手才能找到可拖动祖先。
      // 未分类固定在末尾、不参与板块拖动：既无把手也不可作拖动成员。
      this.body.innerHTML = `<div class="memo-board" data-drag-list="tags">${sections.map((section) => `
        <section class="memo-section" ${section.tag ? `data-drag-item data-tag-id="${section.tag.id}"` : 'data-untagged="1"'}>
          <header class="memo-section-head">
            ${tagDragEnabled && section.tag ? `<button class="icon-btn memo-handle" data-drag="tag" aria-label="拖动调整板块顺序">${icon('grip')}</button>` : '<span class="memo-handle-spacer"></span>'}
            <button class="memo-section-title" data-act="memo-view-tag"
                    data-tag-id="${section.tag ? section.tag.id : 'untagged'}">
              ${esc(this.sectionLabel(section))}
              <span class="plan-count">${section.pinned_count + section.note_count}</span>
            </button>
            <span class="grow"></span>
            <button class="btn btn-quiet btn-sm" data-act="memo-view-tag"
                    data-tag-id="${section.tag ? section.tag.id : 'untagged'}">查看全部</button>
          </header>
          ${section.preview.length ? `
            <div class="memo-list" data-drag-list="entries">
              ${section.preview.map((item) => this.itemHtml(item)).join('')}
            </div>` : ''}
        </section>`).join('')}</div>`;
    },

    itemHtml(item, { fragment = false } = {}) {
      const chips = (item.tags || [])
        .map((t) => tag(esc(typeof t === 'object' ? t.name : t), 'slate'))
        .join('');
      const excerpt = fragment ? item.match_fragment : item.body_excerpt;
      return `
        <div class="memo-item" data-drag-item data-entry-id="${item.id}" data-kind="${item.kind}" role="button" tabindex="0">
          <button class="icon-btn memo-handle memo-item-handle" data-drag="entry" aria-label="拖动排序">${icon('grip')}</button>
          <div class="memo-item-main">
            <div class="memo-item-title">${esc(item.display_title || '（无标题）')}</div>
            ${excerpt ? `<div class="memo-item-excerpt">${esc(excerpt)}</div>` : ''}
          </div>
          <div class="memo-item-side"><div class="tag-row">
            ${tag(KIND_LABELS[item.kind] || item.kind, item.kind === 'pinned' ? 'gold' : 'muted')}
            ${chips}
          </div></div>
        </div>`;
    },

    renderTagDetail() {
      const section = (this.board?.sections || []).find(
        (s) => (this.activeTagId === 'untagged' ? s.tag === null : s.tag && s.tag.id === this.activeTagId));
      const tagName = this.activeTagId === 'untagged'
        ? '未分类'
        : ((this.tags.find((t) => t.id === this.activeTagId) || {}).name || '标签');
      // 删除入口按标签身份提供（F13）：标签只剩归档/回收站内容时不构成
      // 板块，此前详情里没有删除按钮。
      const tagExists = this.activeTagId !== 'untagged'
        && this.tags.some((t) => t.id === this.activeTagId);
      const head = `
        <div class="memo-detail-head">
          <button class="btn btn-quiet btn-sm" data-act="memo-back">${icon('chevron-left')}返回</button>
          <h3 class="memo-detail-title">${esc(tagName)}</h3>
          <span class="grow"></span>
          ${tagExists ? `<button class="btn btn-danger-line btn-sm" data-act="memo-delete-tag" data-tag-id="${this.activeTagId}">${icon('x')}删除标签</button>` : ''}
        </div>`;
      if (!section) {
        this.body.innerHTML = head + empty('该标签下暂无内容', '新建备忘录时可选择该标签');
        return;
      }
      const mode = section.note_sort_mode;
      const pinnedItems = section.items.filter((item) => item.kind === 'pinned');
      const noteItems = section.items.filter((item) => item.kind === 'note');
      this.body.innerHTML = head + `
        <div class="subtabs memo-mode-tabs">
          <button class="subtab ${mode === 'latest' ? 'active' : ''}" data-act="memo-note-mode" data-mode="latest">最新排序</button>
          <button class="subtab ${mode === 'manual' ? 'active' : ''}" data-act="memo-note-mode" data-mode="manual">手动排序</button>
        </div>
        <div class="plan-group-title">常驻备忘 <span class="plan-count">${section.pinned_count}</span></div>
        ${pinnedItems.length
          ? `<div class="memo-list" data-drag-list="entries" data-section="pinned">${pinnedItems.map((item) => this.itemHtml(item)).join('')}</div>`
          : empty('暂无常驻备忘')}
        <div class="plan-group-title">随笔 <span class="plan-count">${section.note_count}</span></div>
        ${noteItems.length
          ? `<div class="memo-list" data-drag-list="entries" data-section="note">${noteItems.map((item) => this.itemHtml(item)).join('')}</div>`
          : empty('暂无随笔', mode === 'manual' ? '手动模式下新建的随笔会排在末尾' : '')}`;
    },

    renderSearch() {
      const results = this.searchResults || [];
      this.body.innerHTML = `
        <div class="memo-detail-head">
          <span class="memo-detail-title">搜索「${esc(this.searchQuery)}」 · ${results.length} 条结果</span>
          <span class="grow"></span>
          <button class="btn btn-quiet btn-sm" data-act="memo-clear-search">${icon('x')}清除搜索</button>
        </div>
        ${results.length
          ? `<div class="memo-list">${results.map((item) => this.itemHtml(item, { fragment: true })).join('')}</div>`
          : empty('没有匹配的标题或正文', '搜索结果不影响已保存的排列顺序')}
        <p class="muted text-sm">搜索结果按标题匹配优先、正文匹配其次；同一条记录只显示一次，并展示所属标签。</p>`;
    },

    async renderLifecycleList(status) {
      const seq = ++this.seq;
      this.body.innerHTML = loading();
      try {
        const items = await gw(`/admin/api/memo/entries?status=${status}`);
        // 视图已切走（筛选/返回/看板）时旧响应不得覆盖当前内容区（F10）
        if (seq !== this.seq || !this.root || VIEW_STATUS[this.view] !== status) return;
        const archived = status === 'archived';
        const head = `
          <div class="memo-detail-head">
            <button class="btn btn-quiet btn-sm" data-act="memo-back">${icon('chevron-left')}返回</button>
            <h3 class="memo-detail-title">${archived ? '已归档' : '回收站'}</h3>
          </div>`;
        this.body.innerHTML = head + (items.length ? `<div class="memo-list">${items.map((item) => `
          <div class="memo-item" data-entry-id="${item.id}" data-kind="${item.kind}" role="button" tabindex="0">
            <div class="memo-item-main">
              <div class="memo-item-title">${esc(item.display_title || '（无标题）')}</div>
              ${item.body_excerpt ? `<div class="memo-item-excerpt">${esc(item.body_excerpt)}</div>` : ''}
            </div>
            <div class="memo-item-side"><div class="tag-row">
              <span class="muted text-sm">${archived ? '归档于' : '删除于'} ${esc(((archived ? item.archived_at : item.deleted_at) || '').slice(0, 10))}</span>
              <button class="btn btn-secondary btn-sm" data-act="memo-restore" data-id="${item.id}">${icon('refresh')}恢复</button>
              ${archived ? `<button class="btn btn-danger-line btn-sm" data-act="memo-lifecycle-delete" data-id="${item.id}">${icon('x')}移入回收站</button>` : ''}
            </div></div>
          </div>`).join('')}</div>`
          : empty(archived ? '没有已归档的备忘录' : '回收站是空的',
              archived ? '归档的内容随时可以恢复' : '删除的内容会保留在这里，随时可恢复'));
      } catch (error) {
        if (seq !== this.seq || !this.root) return;
        this.body.innerHTML = errorBlock(`读取失败：${esc(error.message)}`);
      }
    },

    /* ---------- 事件 ---------- */

    handleClick(e) {
      if (this.suppressClick) {
        this.suppressClick = false;
        return;
      }
      const el = e.target.closest('[data-act]');
      if (el && !el.disabled) {
        const act = el.dataset.act;
        if (act === 'memo-new') return this.openEditor(null);
        if (act === 'memo-view-tag') {
          this.switchView('tag');
          this.activeTagId = el.dataset.tagId === 'untagged' ? 'untagged' : Number(el.dataset.tagId);
          return this.render();
        }
        if (act === 'memo-back') {
          this.switchView('board');
          return this.render();
        }
        if (act === 'memo-clear-search') {
          const searchInput = this.root.querySelector('[data-memo-search]');
          if (searchInput) searchInput.value = '';
          this.searchQuery = '';
          this.switchView('board');
          return this.render();
        }
        if (act === 'memo-archived') {
          this.switchView('archived');
          return this.renderLifecycleList(VIEW_STATUS.archived);
        }
        if (act === 'memo-trash') {
          this.switchView('trash');
          return this.renderLifecycleList(VIEW_STATUS.trash);
        }
        if (act === 'memo-note-mode') return this.setNoteMode(el.dataset.mode);
        if (act === 'memo-delete-tag') return this.deleteTag(Number(el.dataset.tagId));
        if (act === 'memo-restore') return this.restoreEntry(Number(el.dataset.id));
        if (act === 'memo-lifecycle-delete') return this.lifecycleEntry(Number(el.dataset.id), 'delete');
        return;
      }
      // 拖动把手不触发阅读；列表项点击 = 阅读完整正文（§2.3/M07）
      if (e.target.closest('[data-drag]')) return;
      if (this.view === 'archived' || this.view === 'trash') return;
      const item = e.target.closest('[data-entry-id][role="button"]');
      if (item) this.openReader(Number(item.dataset.entryId));
    },

    /* ---------- 阅读 / 生命周期 ---------- */

    fetchEntry(id) {
      return gw(`/admin/api/memo/entries/${id}`);
    },

    async openReader(id) {
      let entry;
      try {
        entry = await this.fetchEntry(id);
      } catch (error) {
        toast(`读取失败：${error.message}`, 'err');
        return;
      }
      const tagsHtml = (entry.tags || []).map((t) => tag(esc(t.name), 'slate')).join('');
      const { root, close } = modal({
        title: esc(entry.title || '备忘录'),
        wide: true,
        body: `
          <div class="tag-row" style="margin-bottom:10px">
            ${tag(KIND_LABELS[entry.kind] || entry.kind, entry.kind === 'pinned' ? 'gold' : 'muted')}
            ${tagsHtml}
            <span class="muted text-sm">更新于 ${esc((entry.updated_at || '').replace('T', ' ').slice(0, 16))}</span>
          </div>
          <div class="memo-md">${renderMarkdown(entry.content || '')}</div>`,
        footer: `
          <button class="btn btn-primary" data-reader-edit data-id="${entry.id}">${icon('edit')}编辑</button>
          ${entry.status === 'active' ? `
            <button class="btn btn-secondary" data-reader-archive data-id="${entry.id}">${icon('archive')}归档</button>
            <button class="btn btn-danger-line" data-reader-delete data-id="${entry.id}">${icon('x')}删除</button>` : ''}
          <button class="btn btn-quiet" data-reader-close>关闭</button>`,
      });
      root.querySelector('[data-reader-close]').onclick = close;
      root.addEventListener('click', (ev) => {
        const el = ev.target.closest('[data-reader-edit],[data-reader-archive],[data-reader-delete]');
        if (!el) return;
        if (el.hasAttribute('data-reader-edit')) {
          close();
          this.openEditor(entry.id);
        } else if (el.hasAttribute('data-reader-archive')) {
          this.lifecycleEntry(entry.id, 'archive', close);
        } else if (el.hasAttribute('data-reader-delete')) {
          this.lifecycleEntry(entry.id, 'delete', close);
        }
      });
    },

    async lifecycleEntry(id, action, closeReader = null) {
      if (action === 'delete' && !(await confirm(
        '确认删除这条备忘录？可以在回收站恢复。',
        { danger: true, okText: '删除', cancelText: '取消' },
      ))) return;
      try {
        const entry = await this.fetchEntry(id);
        await gw(`/admin/api/memo/entries/${id}/${action}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ expected_version: entry.content_version }),
        });
        toast(action === 'archive' ? '已归档，可在「归档」中找回' : '已删除，可在「回收站」恢复');
        if (closeReader) closeReader();
        await this.refreshAfterWrite();
      } catch (error) {
        toast(`${action === 'archive' ? '归档' : '删除'}失败：${error.message}`, 'err');
      }
    },

    async restoreEntry(id) {
      try {
        const entry = await this.fetchEntry(id);
        await gw(`/admin/api/memo/entries/${id}/restore`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ expected_version: entry.content_version }),
        });
        toast('已恢复');
        await this.refreshAfterWrite();
      } catch (error) {
        toast(`恢复失败：${error.message}`, 'err');
      }
    },

    /* ---------- 标签操作 ---------- */

    async setNoteMode(mode) {
      const payload = { mode, tag_id: this.activeTagId === 'untagged' ? null : this.activeTagId };
      try {
        await gw('/admin/api/memo/note-mode', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        toast(mode === 'manual'
          ? '已切换到手动排序'
          : '已切回最新排序；原手动顺序已保留');
        await this.reload({ silent: true });
        this.render();
      } catch (error) {
        toast(`切换排序模式失败：${error.message}`, 'err');
      }
    },

    async deleteTag(tagId) {
      if (!(await confirm(
        '确认删除该标签？只解除分类关系，内容不会被删除；失去标签的内容会进入「未分类」。',
        { danger: true, okText: '删除标签', cancelText: '取消' },
      ))) return;
      try {
        await gw(`/admin/api/memo/tags/${tagId}/delete`, { method: 'POST' });
        toast('标签已删除');
        this.switchView('board');
        this.activeTagId = null;
        await this.reload({ silent: true });
      } catch (error) {
        toast(`删除标签失败：${error.message}`, 'err');
      }
    },

    /* ---------- 拖拽（把手） ---------- */

    handleDragPointerDown(e) {
      // 上一轮重排尚未保存/刷新完时不开新拖动（F11）：连续拖动若从旧顺序
      // 构造请求，后一次会撤回前一次的调整。
      if (this.reorderBusy) return;
      const handle = e.target.closest('[data-drag]');
      if (!handle || handle.disabled) return;
      if (this.view === 'search') return;   // 搜索结果不调整持久顺序（§5）
      const item = handle.closest('[data-drag-item]');
      const list = handle.closest('[data-drag-list]');
      if (!item || !list) return;
      startHandleDrag(e, handle, list, item, (dragged) => {
        this.suppressClick = true;
        setTimeout(() => { this.suppressClick = false; }, 0);
        if (list.dataset.dragList === 'tags') this.commitTagOrder(list);
        else this.commitEntryOrder(list, dragged);
      });
    },

    commitTagOrder(list) {
      if (this.reorderBusy) return;
      // 只枚举本列表的直接板块成员（F05）：嵌套的条目 data-drag-item
      // 不进入板块顺序收集。
      const visibleOrder = [...list.querySelectorAll(':scope > [data-drag-item]')]
        .map((el) => Number(el.dataset.tagId))
        .filter(Boolean);
      // 筛选视图下板块把手不渲染，这里提交的始终是全部标签的完整顺序
      const allTags = this.tags.map((t) => t.id);
      const merged = [...visibleOrder, ...allTags.filter((id) => !visibleOrder.includes(id))];
      if (!merged.length) return;
      this.reorderBusy = true;
      gw('/admin/api/memo/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scope: 'tags', order: merged }),
      }).then(() => {
        toast('板块顺序已保存');
        return this.reload({ silent: true });
      }).catch((error) => {
        toast(`保存板块顺序失败：${error.message}`, 'err');
        return this.reload({ silent: true });
      }).finally(() => {
        this.reorderBusy = false;
      });
    },

    /** 板块内条目重排：拖动只改变它所属用途区（常驻/随笔）的顺序；
     *  提交该区全量 id（第 6 条及之后的位置随看板数据一并提交，§8.7）。 */
    commitEntryOrder(list, dragged) {
      if (this.reorderBusy || !dragged) return;
      const section = this.sectionOfList(list);
      if (!section) return;
      const draggedId = Number(dragged.dataset.entryId);
      const kind = dragged.dataset.kind === 'pinned' ? 'pinned' : 'note';
      const visual = [...list.querySelectorAll(':scope > [data-drag-item]')];
      let rank = 0;
      for (const el of visual) {
        if (Number(el.dataset.entryId) === draggedId) break;
        if (el.dataset.kind === kind) rank += 1;
      }
      const fullOrder = (kind === 'pinned' ? section.pinned_order : section.note_order)
        .filter((id) => id !== draggedId);
      fullOrder.splice(rank, 0, draggedId);
      const willEnterManual = kind === 'note' && section.note_sort_mode === 'latest';
      this.reorderBusy = true;
      gw('/admin/api/memo/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scope: 'group',
          tag_id: section.tag ? section.tag.id : null,
          section: kind,
          order: fullOrder,
        }),
      }).then(() => {
        toast(willEnterManual ? '顺序已保存；随笔已进入手动排序模式' : '顺序已保存');
        return this.reload({ silent: true });
      }).catch((error) => {
        toast(`保存顺序失败：${error.message}`, 'err');
        return this.reload({ silent: true });
      }).finally(() => {
        this.reorderBusy = false;
      });
    },

    sectionOfList(list) {
      const sectionEl = list.closest('.memo-section');
      if (!sectionEl) {
        // 标签详情视图：列表不带 .memo-section 包裹层，分组由当前详情标签决定
        if (!list.dataset.section) return null;
        if (this.activeTagId === 'untagged') {
          return (this.board?.sections || []).find((s) => s.tag === null);
        }
        return (this.board?.sections || []).find((s) => s.tag && s.tag.id === this.activeTagId);
      }
      if (sectionEl.dataset.untagged) {
        return (this.board?.sections || []).find((s) => s.tag === null);
      }
      const tagId = Number(sectionEl.dataset.tagId);
      return (this.board?.sections || []).find((s) => s.tag && s.tag.id === tagId);
    },

    /* ---------- 编辑器（自动保存；每个编辑会话独立实例） ---------- */

    openEditor(entryId) {
      // 单实例守卫必须覆盖「打开在途」：连点新建/编辑只产出第一个编辑器，
      // 两次 fetch 各建一个弹窗会让模块只管住后一个（F01）。
      if (this.editor || this._openingEditor) return;
      this._openingEditor = true;
      this.fetchEntryOrBlank(entryId).then((seed) => {
        this._openingEditor = false;
        if (seed && this.root && !this.editor) {
          // 构建期间（含草稿恢复决策的确认）继续挡住连点
          this._openingEditor = true;
          this.buildEditor(seed).finally(() => { this._openingEditor = false; });
        }
      });
    },

    async fetchEntryOrBlank(entryId) {
      if (!entryId) {
        return {
          id: null, title: '', content: '', kind: 'note',
          tag_ids: [], content_version: 0, status: 'active',
        };
      }
      try {
        const entry = await this.fetchEntry(entryId);
        if (entry.status !== 'active') {
          toast('该记录已归档或已删除，恢复后才能编辑', 'warn');
          return null;
        }
        return { ...entry, tag_ids: (entry.tags || []).map((t) => t.id) };
      } catch (error) {
        toast(`读取失败：${error.message}`, 'err');
        return null;
      }
    },

    _sameTagIdSet(tags, tagIds) {
      const a = (tags || [])
        .map((t) => (typeof t === 'object' ? t.id : t))
        .sort((x, y) => x - y);
      const b = [...(tagIds || [])].sort((x, y) => x - y);
      return a.length === b.length && a.every((v, i) => v === b[i]);
    },

    _draftDiffers(draft, seed) {
      if ((draft.content || '') !== (seed.content || '')) return true;
      if ((draft.title || '') !== (seed.title || '')) return true;
      if ((draft.kind || 'note') !== seed.kind) return true;
      const a = [...(draft.tag_ids || [])].sort((x, y) => x - y);
      const b = [...(seed.tag_ids || [])].sort((x, y) => x - y);
      return a.length !== b.length || a.some((v, i) => v !== b[i]);
    },

    async buildEditor(seed) {
      // 草稿槽恢复（F03）：同一条目、版本相邻且内容确有差异才自动采用。
      // 恢复后以草稿自身版本为基准继续保存——服务端若已被推进（如卸载时
      // 在途保存竞争的提交），首次保存会被版本门拦下走冲突确认，不静默
      // 覆盖。
      const draft = readStoredDraft();
      let restored = false;
      let baseVersion = seed.content_version;
      if (draft && draft.entryId === seed.id) {
        const versionOk = seed.id == null
          || draft.baseVersion === seed.content_version
          || draft.baseVersion === seed.content_version - 1;
        if (this._draftDiffers(draft, seed)) {
          if (versionOk) {
            baseVersion = draft.baseVersion ?? baseVersion;
            seed = {
              ...seed,
              title: draft.title || '',
              content: draft.content || '',
              kind: draft.kind || seed.kind,
              tag_ids: draft.tag_ids || [],
            };
            restored = true;
          } else {
            // 版本差距过大：服务端已被其他设备明显推进。版本差距只能阻止
            // 「自动恢复后直接覆盖」，不能作为销毁未确认输入的依据
            // （BUG-03/R05）——恢复还是丢弃交给用户明确决定；恢复后保存
            // 会命中版本门，走冲突确认，不会静默覆盖服务端新内容。
            const restore = await confirm(
              '本机有这条内容未保存的草稿，但内容已在其他设备更新过。'
              + '「恢复草稿」继续编辑本机版本（保存时需确认覆盖）；'
              + '「丢弃草稿」清除本机草稿并显示服务端最新内容。',
              { okText: '恢复草稿', cancelText: '丢弃草稿' },
            );
            if (restore) {
              baseVersion = draft.baseVersion ?? baseVersion;
              seed = {
                ...seed,
                title: draft.title || '',
                content: draft.content || '',
                kind: draft.kind || seed.kind,
                tag_ids: draft.tag_ids || [],
              };
              restored = true;
            } else {
              clearStoredDraft();   // 用户明确丢弃后才清槽
            }
          }
        } else {
          clearStoredDraft();   // 草稿与服务端一致：无需恢复
        }
      }

      const editor = {
        entryId: seed.id,
        version: baseVersion,
        // 新建恢复沿用原 crid：首次创建若已提交，幂等键能把已落库的记录
        // 找回来，而不是再建一条（F02/F03 联动）
        crid: seed.id ? null : (restored && draft.crid ? draft.crid : crypto.randomUUID()),
        kind: seed.kind,
        selectedTagIds: new Set(seed.tag_ids || []),
        autosave: null,
      };
      const { root, close } = modal({
        title: seed.id ? '编辑备忘录' : '新建备忘录',
        wide: true,
        // 遮罩点击与关闭按钮走同一保存/确认/清理流程（F04）：
        // 直接移除会留下不可见编辑器，之后新建和编辑入口全部失效
        onMaskClose: () => this.requestEditorClose(),
        body: `
          <div class="field">
            <label>用途</label>
            <span class="retro-select" data-editor-kind></span>
            <p class="field-hint">常驻备忘优先展示在板块前列；随笔默认按创建时间倒序。用途由你明确选择，不会随内容自动改变。</p>
          </div>
          <div class="field">
            <label>标签（可多选，可不选）</label>
            <div class="memo-tag-chips" data-editor-tags></div>
            <div class="memo-new-tag">
              <input type="text" data-new-tag-name maxlength="30" placeholder="新标签名称" aria-label="新标签名称">
              <button type="button" class="btn btn-secondary btn-sm" data-editor-add-tag>${icon('plus')}添加</button>
            </div>
          </div>
          <div class="field">
            <label for="memo-editor-title">标题（可选，留空时以正文首行显示）</label>
            <input type="text" id="memo-editor-title" data-editor-title maxlength="200" value="${esc(seed.title || '')}">
          </div>
          <div class="field">
            <label for="memo-editor-content">正文（Markdown，必填）</label>
            <textarea id="memo-editor-content" data-editor-content rows="12" maxlength="50000"
              placeholder="支持普通 Markdown：标题、列表、引用、代码等">${esc(seed.content || '')}</textarea>
          </div>`,
        footer: `
          <span class="memo-save-status" data-save-status></span>
          <span class="grow"></span>
          <button class="btn btn-primary" data-editor-close>${icon('check')}完成</button>`,
      });
      editor.modal = { root, close };
      this.editor = editor;
      this._saveState = { status: 'idle', error: null };

      // 每个编辑会话独立的自动保存泵：send 与 onChange 都闭包绑定本会话，
      // 请求自带身份、响应只回写本会话——旧编辑器的迟到保存改不了新
      // 编辑器（F01），状态反馈也不会串台。
      editor.autosave = createMemoAutosave({
        send: (snapshot, opts) => this.sendEditorSnapshot(editor, snapshot, opts),
        onChange: (stateSnapshot) => {
          if (this.editor !== editor) return;
          this._saveState = stateSnapshot;
          this.renderSaveStatus();
        },
      });

      const kindHost = root.querySelector('[data-editor-kind]');
      const kindInput = createRetroSelectField(kindHost, {
        id: 'memo-editor-kind',
        value: editor.kind,
        options: [
          { value: 'pinned', label: '常驻备忘' },
          { value: 'note', label: '随笔' },
        ],
      });
      kindHost.querySelector('button').setAttribute('aria-label', '备忘录用途');
      kindInput.addEventListener('change', () => {
        editor.kind = kindInput.value;
        this.markEditorDirty();
      });

      this.renderEditorTags();

      root.querySelector('[data-editor-title]').addEventListener('input', () => this.markEditorDirty());
      root.querySelector('[data-editor-content]').addEventListener('input', () => this.markEditorDirty());

      root.querySelector('[data-editor-add-tag]').addEventListener('click', () => this.addEditorTag());
      root.querySelector('[data-new-tag-name]').addEventListener('keydown', (ev) => {
        if (ev.key === 'Enter') {
          ev.preventDefault();
          this.addEditorTag();
        }
      });

      root.querySelector('[data-editor-close]').onclick = () => this.requestEditorClose();
      root.querySelector('.modal-close').onclick = () => this.requestEditorClose();

      this.renderSaveStatus();
      if (restored) {
        toast('已恢复上次未保存的草稿');
        this.markEditorDirty();   // 恢复即排程保存；服务端已推进时走冲突确认
      }
    },

    renderEditorTags() {
      const editor = this.editor;
      if (!editor) return;
      const host = editor.modal.root.querySelector('[data-editor-tags]');
      host.innerHTML = this.tags.length ? this.tags.map((t) => `
        <label class="memo-chip ${editor.selectedTagIds.has(t.id) ? 'is-checked' : ''}">
          <input type="checkbox" data-tag-chip value="${t.id}" ${editor.selectedTagIds.has(t.id) ? 'checked' : ''}>
          <span>${esc(t.name)}</span>
        </label>`).join('')
        : '<span class="muted text-sm">还没有标签，可在下方添加</span>';
      host.querySelectorAll('[data-tag-chip]').forEach((chip) => {
        chip.addEventListener('change', () => {
          const id = Number(chip.value);
          if (chip.checked) editor.selectedTagIds.add(id);
          else editor.selectedTagIds.delete(id);
          chip.closest('.memo-chip').classList.toggle('is-checked', chip.checked);
          this.markEditorDirty();
        });
      });
    },

    async addEditorTag() {
      const editor = this.editor;
      if (!editor) return;
      const input = editor.modal.root.querySelector('[data-new-tag-name]');
      const name = input.value.trim();
      if (!name) {
        toast('请输入标签名称', 'warn');
        return;
      }
      try {
        const created = await gw('/admin/api/memo/tags', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name }),
        });
        if (!this.tags.some((t) => t.id === created.id)) this.tags.push(created);
        editor.selectedTagIds.add(created.id);
        input.value = '';
        this.renderEditorTags();
        this.mountFilter();
        if (created.existed) toast(`标签「${created.name}」已存在，已直接选用`);
        this.markEditorDirty();
      } catch (error) {
        toast(`添加标签失败：${error.message}`, 'err');
      }
    },

    editorSnapshot(editor = this.editor) {
      if (!editor) return null;
      const rootEl = editor.modal.root;
      return {
        entryId: editor.entryId,
        kind: editor.kind,
        title: rootEl.querySelector('[data-editor-title]').value.trim(),
        content: rootEl.querySelector('[data-editor-content]').value,
        tag_ids: [...editor.selectedTagIds],
      };
    },

    markEditorDirty() {
      const editor = this.editor;
      if (!editor) return;
      const snapshot = this.editorSnapshot(editor);
      // 每次输入同步落草稿槽（F03）：硬卸载后最新输入可恢复
      writeStoredDraft({
        entryId: editor.entryId,
        crid: editor.crid,
        kind: snapshot.kind,
        title: snapshot.title,
        content: snapshot.content,
        tag_ids: snapshot.tag_ids,
        baseVersion: editor.version,
        savedAt: Date.now(),
      });
      if (editor.entryId == null && !snapshot.content.trim()) {
        // 新建且正文为空：撤下尚未发出的旧创建快照，不触发创建（F08）；
        // 已在途的创建由 send 侧按当前草稿裁决
        editor.autosave.cancelPending();
        return;
      }
      editor.autosave.markDirty(snapshot);
    },

    async sendEditorSnapshot(editor, snapshot, { keepalive = false } = {}) {
      // 请求绑定发起它的编辑会话（F01）：路径、版本、幂等键全部取自闭包
      // 里的 editor，响应也只回写该会话——A 的迟到响应改不了 B 的身份，
      // B 的内容也绝不会被存进 A 的记录。
      const isNew = editor.entryId == null;
      const path = isNew
        ? '/admin/api/memo/entries'
        : `/admin/api/memo/entries/${editor.entryId}`;
      const body = isNew
        ? {
          kind: snapshot.kind,
          title: snapshot.title || null,
          content: snapshot.content,
          tag_ids: snapshot.tag_ids,
          client_request_id: editor.crid ?? null,
        }
        : {
          expected_version: editor.version,
          kind: snapshot.kind,
          title: snapshot.title || null,
          content: snapshot.content,
          tag_ids: snapshot.tag_ids,
        };
      const resp = await fetch(window.location.origin + path, {
        method: isNew ? 'POST' : 'PATCH',
        keepalive: !!keepalive,
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${localStorage.getItem('qi-token') || ''}`,
        },
        body: JSON.stringify(body),
      });
      if (!resp.ok) {
        let message = `${resp.status}`;
        let code = null;
        try {
          const errBody = await resp.json();
          if (errBody.error) message = `${resp.status}: ${errBody.error}`;
          code = errBody.error_code || null;
        } catch { /* 保留状态码信息 */ }
        const error = new Error(message);
        error.code = code;
        throw error;
      }
      const saved = await resp.json();
      if (!isNew) {
        // 成功的写响应必须推进发起会话的版本（BUG-02/R03）：服务器已把
        // content_version 推进到本次响应值，下一次 PATCH 若仍用旧版本会被
        // 误判为跨设备冲突。响应只回写闭包里的本会话，旧会话的迟到响应
        // 改不到新编辑器（F01 隔离保持）。
        editor.version = saved.content_version;
        return saved;
      }

      // 创建返回：先核对本次草稿是否真的入库（同幂等键的重试可能拿到
      // 首次记录——那不是最新内容，不能当作已保存，F02），再采纳记录身份。
      const applied = saved.content === snapshot.content
        && (saved.title || null) === (snapshot.title || null)
        && saved.kind === snapshot.kind
        && this._sameTagIdSet(saved.tags, snapshot.tag_ids);
      editor.entryId = saved.id;
      editor.version = saved.content_version;
      editor.crid = null;

      // 保存期间正文被清空（F08）：刚落库的内容不符合用户当前意图——
      // 删除刚创建的记录并把会话复位为全新草稿；删除失败则保留记录。
      // 两种情况都不上报「已保存」。
      if (!this.editorSnapshot(editor).content.trim()) {
        const removed = await this._discardJustCreated(saved);
        editor.entryId = null;
        editor.version = 0;
        editor.crid = crypto.randomUUID();
        if (!removed) toast('正文已清空；删除刚保存的记录失败，记录已保留', 'warn');
        return { suppressSaved: true };
      }
      return applied ? saved : { ...saved, applied: false };
    },

    async _discardJustCreated(saved) {
      try {
        const resp = await fetch(
          window.location.origin + `/admin/api/memo/entries/${saved.id}/delete`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              Authorization: `Bearer ${localStorage.getItem('qi-token') || ''}`,
            },
            body: JSON.stringify({ expected_version: saved.content_version }),
          },
        );
        return resp.ok;
      } catch {
        return false;
      }
    },

    renderSaveStatus() {
      const editor = this.editor;
      const statusEl = editor?.modal?.root?.querySelector('[data-save-status]');
      if (!editor || !statusEl) return;
      const { status, error } = this._saveState;
      // 空正文永远不显示「已保存」（F08）：保存反馈必须与当前输入一致
      if (status === 'saved'
          && !editor.modal.root.querySelector('[data-editor-content]').value.trim()) {
        statusEl.innerHTML = '';
        return;
      }
      if (status === 'saving') {
        statusEl.innerHTML = `<span class="memo-save memo-save-saving">${icon('refresh')}保存中…</span>`;
      } else if (status === 'pending') {
        statusEl.innerHTML = `<span class="memo-save memo-save-pending">${icon('edit')}待保存</span>`;
      } else if (status === 'saved') {
        statusEl.innerHTML = `<span class="memo-save memo-save-ok">${icon('check')}已保存</span>`;
      } else if (status === 'conflict') {
        statusEl.innerHTML = `
          <span class="memo-save memo-save-err">${icon('alert')}内容已在其他设备更新，保存被拒绝</span>
          <button type="button" class="btn btn-secondary btn-sm" data-save-overwrite>覆盖保存</button>`;
      } else if (status === 'lifecycle') {
        statusEl.innerHTML = `<span class="memo-save memo-save-err">${icon('alert')}${esc(error?.message || '该记录已归档或已删除，不能继续编辑')}</span>`;
      } else if (status === 'error') {
        statusEl.innerHTML = `
          <span class="memo-save memo-save-err">${icon('alert')}保存失败：${esc(error?.message || '未知错误')}</span>
          <button type="button" class="btn btn-secondary btn-sm" data-save-retry>重试</button>`;
      } else {
        statusEl.innerHTML = '';
      }
      const overwriteBtn = statusEl.querySelector('[data-save-overwrite]');
      if (overwriteBtn) overwriteBtn.onclick = () => this.overwriteEditor();
      const retryBtn = statusEl.querySelector('[data-save-retry]');
      if (retryBtn) retryBtn.onclick = () => editor.autosave.retry();
    },

    async overwriteEditor() {
      const editor = this.editor;
      if (!editor || !editor.entryId) return;
      try {
        const fresh = await this.fetchEntry(editor.entryId);
        if (fresh.status !== 'active') {
          toast('该记录已归档或删除，不能继续编辑', 'warn');
          return;
        }
        editor.version = fresh.content_version;   // 对齐服务端版本后覆盖保存
        editor.autosave.retry();
      } catch (error) {
        toast(`读取最新内容失败：${error.message}`, 'err');
      }
    },

    async requestEditorClose() {
      const editor = this.editor;
      if (!editor) return;
      // 排空在途/待保存后再关（F01/F04）：正文被清空导致请求失败的情形
      // 同样要走完失败确认，不能静默丢弃用户的其他修改。关闭入口只有在
      // 「未确认输入已确认入库」或「用户明确丢弃」后才允许清稿
      // （BUG-03/R04）：flush 提前返回（队列未真正排空）视同失败确认。
      if (editor.autosave.isBusy()) {
        await editor.autosave.flush();   // resolve 时队列已排空或停在失败态
        const drained = !editor.autosave.isBusy();
        const failed = this._saveState.status === 'error'
          || this._saveState.status === 'conflict'
          || this._saveState.status === 'lifecycle';
        if (!drained || failed) {
          const force = await confirm(
            '保存未成功。关闭后未保存的修改将丢失，确定要关闭吗？',
            { danger: true, okText: '丢弃并关闭', cancelText: '返回编辑' },
          );
          if (!force) return;
        }
      }
      this.closeEditor();
    },

    closeEditor() {
      const editor = this.editor;
      if (!editor) return;
      this.editor = null;
      // 关闭即放弃本地草稿槽：保留还是丢弃已由用户在关闭流程中决定
      clearStoredDraft();
      editor.autosave.dispose();
      try { editor.modal.close(); } catch { /* 已移除 */ }
      this.refreshAfterWrite();
    },

    /* ---------- 由 planning 页调用的生命周期钩子 ---------- */

    /** 切走备忘录页签：未保存内容立即保存（编辑器保持打开）。 */
    flushPending() {
      if (this.editor) this.editor.autosave.flush();
    },
  };

  return memo;
}
