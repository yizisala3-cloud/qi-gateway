// pages/planning.js - 规划管理：四类型待办 + 时间排程 + 排列模式 + 浏览器闹钟/计时器
// 四区域以页签切换（复用记忆管理 .tabs/.tab），「当前待办」内再以 .subtabs 三分区切换；
// 数据按需加载：今日看板保留 30 秒提醒轮询，首次/失效切入时刷新可见列表。
import { gw } from '../api.js?v=20261004-ring-fix1';
import {
  loading, empty, errorBlock, tag, toast, modal, confirm, delegate, icon, esc,
  createDetailPanel,
} from '../ui.js?v=20261004-ring-fix1';
import { createRetroTimeField } from '../lib/retro_time.js?v=20261004-ring-fix1';
import { createRetroSelectField } from '../lib/retro_select.js?v=20261004-ring-fix1';
import {
  TASK_TYPE_LABELS, TASK_TYPES, STATUS_META, CLOSED_STATUSES,
  fmtClock, fmtRange, fmtDue, taskTypeSummary, miniEmpty,
  itemMeta, isClosedOcc, formatLoggedDuration, durationText, durationDetailRows,
  itemBadges, itemHtml,
} from '../lib/planning_display.js?v=20261004-ring-fix1';
import { openTaskForm } from '../lib/planning_task_form.js?v=20261004-ring-fix1';
import { createPlanningDialogs } from '../lib/planning_dialogs.js?v=20261004-ring-fix1';
import { createPlanningSort } from '../lib/planning_sort.js?v=20261004-ring-fix1';
import { createPlanningReminder } from '../lib/planning_reminder.js?v=20261004-ring-fix1';
import { createPlanningMemo } from '../lib/planning_memo.js?v=20261004-ring-fix1';
import { createPlanningReads } from '../lib/planning_reads.js?v=20261004-ring-fix1';

// 部分完成属于开放生命周期：实例仍在「进度中」，直到「已全部完成」才关闭
const OPEN_STATUSES = ['pending', 'in_progress', 'deferred', 'partial'];
// 暂停/恢复刷新只面向周期任务（需求 24）；单次/闲时没有周期刷新，不提供该入口
const PAUSABLE_TYPES = ['daily', 'interval', 'weekly', 'monthly'];
const POLL_MS = 30 * 1000;

export default {
  board: null,
  tasks: [],
  occurrences: [],
  filters: { task_type: '', status: '', schedule_date: '' },
  detail: null,
  selected: null,
  reorderMode: false,
  activeTab: 'today',
  activeSection: 'progress',
  reads: null,
  readErrors: null,
  savedRefresh: null,
  pollTimer: null,
  reminder: createPlanningReminder(),
  dialogs: null,
  sort: null,
  memo: null,
  taskForm: null,
  loadedTabs: null, // 仅用于备忘录的按需挂载

  async mount(root) {
    this.root = root;
    this.reads = createPlanningReads();
    this.readErrors = new Map();
    this.savedRefresh = null;
    this.loadedTabs = new Set();
    const mountedReads = this.reads;
    this.dialogs ||= createPlanningDialogs({
      findOccurrence: (id) => this.findOccurrence(id),
      getOccurrences: () => this.occurrences,
      getTasks: () => this.tasks,
      openTaskForm: (task) => this.openTaskForm(task),
      loadToday: () => this.loadToday({ fresh: true }),
      loadTasks: () => this.loadTasks({ fresh: true }),
      loadOccurrences: () => this.loadOccurrences({ fresh: true }),
    });
    this.sort ||= createPlanningSort({
      getRoot: () => this.root,
      getProgressList: () => this.progressList,
      getBoard: () => this.board,
      getActiveTab: () => this.activeTab,
      getActiveSection: () => this.activeSection,
      selectSection: (section) => this.switchSection(section),
      renderBoard: () => this.renderBoard(),
      loadToday: () => this.loadToday({ fresh: true }),
      getReorderMode: () => this.reorderMode,
      setReorderMode: (enabled) => { this.reorderMode = enabled; },
    });
    this.activeTab = 'today';
    this.activeSection = 'progress';
    root.innerHTML = `
      <div class="page-with-detail" id="planning-layout">
        <div class="page-main">
          <div class="tabs" id="planning-tabs" style="margin-bottom:14px">
            <button class="tab active" data-act="plan-tab" data-tab="today">${icon('calendar')}当前待办</button>
            <button class="tab" data-act="plan-tab" data-tab="all">${icon('inbox')}全部待办</button>
            <button class="tab" data-act="plan-tab" data-tab="memo">${icon('feather')}备忘录</button>
            <button class="tab" data-act="plan-tab" data-tab="goals">${icon('star')}长期目标</button>
            <button class="tab" data-act="plan-tab" data-tab="summary">${icon('journal')}每日总结</button>
          </div>

          <div class="toolbar" style="margin-bottom:14px">
            <button class="btn btn-primary" data-act="new-task" data-toolbar-tab="today">${icon('plus')}新建待办</button>
            <button class="btn btn-secondary" data-act="recompute" data-toolbar-tab="todo">${icon('refresh')}重新计算时间</button>
            <button class="btn btn-secondary" data-act="enter-reorder" id="planning-reorder-btn" data-toolbar-tab="todo">${icon('sort')}调整顺序</button>
            <button class="btn btn-primary" data-act="memo-new" data-toolbar-tab="memo" hidden>${icon('plus')}新建备忘录</button>
          </div>
          <div id="planning-load-feedback" role="alert" hidden></div>
          <div id="planning-reorder-bar" style="display:none;margin-bottom:12px">
            <div class="plan-alarm-bar">
              ${icon('sort')}
              <div>排列模式：拖动待办调整顺序。</div>
              <span class="grow"></span>
              <button class="btn btn-primary btn-sm" data-act="confirm-reorder">${icon('check')}确认</button>
              <button class="btn btn-danger-line btn-sm" data-act="cancel-reorder">${icon('x')}撤销</button>
            </div>
          </div>

          <div class="plan-region" id="planning-today" data-panel="today">
            <div class="card">
              <div class="plan-region-head">
                <div class="plan-region-title">${icon('calendar')}当前待办</div>
                <span class="plan-region-sub">只显示今天的待办；已完成的记录可改状态、补时间</span>
              </div>
              <div id="planning-alarm-banner"></div>
              <div class="subtabs">
                <button class="subtab active" data-act="plan-subtab" data-section="progress">进度中 <span class="plan-count" id="planning-progress-count"></span></button>
                <button class="subtab" data-act="plan-subtab" data-section="attention">待处理 <span class="plan-count" id="planning-attention-count"></span></button>
                <button class="subtab" data-act="plan-subtab" data-section="done">已完成 <span class="plan-count" id="planning-done-count"></span></button>
              </div>
              <div class="plan-list" id="planning-progress">${loading()}</div>
              <div class="plan-list" id="planning-attention" hidden></div>
              <div class="plan-list" id="planning-done" hidden></div>
            </div>
          </div>

          <div class="plan-region" id="planning-all" data-panel="all" hidden>
            <div class="card">
              <div class="plan-region-head">
                <div class="plan-region-title">${icon('inbox')}全部待办</div>
                <span class="plan-region-sub">所有任务定义与出现记录，可筛选、编辑、提前完成</span>
              </div>
              <div class="toolbar" style="margin-bottom:6px">
                <label class="inline">类型
                  <select id="planning-filter-type">
                    <option value="">全部</option>
                    ${TASK_TYPES.map((t) => `<option value="${t}">${TASK_TYPE_LABELS[t]}</option>`).join('')}
                  </select>
                </label>
                <label class="inline">状态
                  <select id="planning-filter-status">
                    <option value="">全部</option>
                    ${Object.entries(STATUS_META).map(([k, v]) => `<option value="${k}">${v.label}</option>`).join('')}
                  </select>
                </label>
                <label class="inline">原始规划周期
                  <span class="retro-time retro-time-inline" data-retro-for="planning-filter-date" data-retro-mode="date"></span>
                </label>
                <button class="btn btn-quiet btn-sm" data-act="clear-filters">清除筛选</button>
              </div>
              <div class="plan-group-title">任务定义 <span class="plan-count" id="planning-tasks-count"></span></div>
              <div id="planning-tasks">${loading()}</div>
              <div class="plan-group-title">出现记录 <span class="plan-count" id="planning-occ-count"></span></div>
              <div id="planning-occurrences">${loading()}</div>
            </div>
          </div>

          <div class="plan-region" id="planning-memo" data-panel="memo" hidden>
            <div class="card">
              <div class="plan-region-head">
                <div class="plan-region-title">${icon('feather')}备忘录</div>
                <span class="plan-region-sub">常驻与随笔 · 自动保存</span>
              </div>
              <div data-memo-root></div>
            </div>
          </div>

          <div class="plan-region" id="planning-goals" data-panel="goals" hidden>
            <div class="card">
              <div class="plan-region-head">
                <div class="plan-region-title">${icon('star')}长期目标</div>
                <span class="plan-region-sub disabled-note">二期接入</span>
              </div>
              <div class="plan-goal-empty">
                <div class="empty-ornament" aria-hidden="true"></div>
                <div>长期目标将在二期接入：目标内容 + 数值 + 单位，并按关联待办的完整完成次数推进进度。</div>
              </div>
            </div>
          </div>

          <div class="plan-region" id="planning-summary" data-panel="summary" hidden>
            <div class="card">
              <div class="plan-region-head">
                <div class="plan-region-title">${icon('journal')}每日总结</div>
                <span class="plan-region-sub disabled-note">三期接入</span>
              </div>
              <div class="plan-summary-empty">
                <div class="plan-summary-actions">
                  <button class="btn btn-secondary" disabled>日记</button>
                  <button class="btn btn-secondary" disabled>周记</button>
                  <button class="btn btn-secondary" disabled>月记</button>
                </div>
                <div class="disabled-note">暂未接入</div>
              </div>
            </div>
          </div>
        </div>
      </div>`;

    const actionRoot = root.querySelector('#planning-layout');
    this.detail = createDetailPanel(actionRoot);
    this.progressList = root.querySelector('#planning-progress');
    actionRoot.addEventListener('click', (e) => {
      if (this.reads === mountedReads) this.handleItemClick(e);
    });
    this.bindDetailActions(actionRoot, mountedReads);
    this.initRetroFields(root);
    this.syncToolbarForTab(this.activeTab);

    const actions = {
      'new-task': () => this.openTaskForm(null),
      recompute: () => this.runRecompute(),
      // 工具栏「新建备忘录」（2026-10-03 移位）：委托给备忘录模块；模块
      // 自身的单实例守卫覆盖连点与在途打开
      'memo-new': () => this.memo?.openEditor(null),
      'enter-reorder': () => this.enterReorder(),
      'confirm-reorder': () => this.confirmReorder(),
      'cancel-reorder': () => this.cancelReorder(),
      'clear-filters': () => this.clearFilters(),
      'retry-lists': () => this.retryLists(),
      'memo-new': () => this.memo?.openEditor(null),
      stop: () => this.reminder.stopRinging(),
      'plan-tab': (el) => this.switchTab(el.dataset.tab),
      'plan-subtab': (el) => this.switchSection(el.dataset.section),
    };
    // 路由外层 root 会复用；委托只绑本次创建的布局，避免重挂载重复开表单。
    delegate(actionRoot, Object.fromEntries(Object.entries(actions).map(([name, action]) => [
      name, (...args) => { if (this.reads === mountedReads) return action(...args); },
    ])));
    root.querySelector('#planning-filter-type').addEventListener('change', (e) => {
      this.filters.task_type = e.target.value;
      this.loadOccurrences();
    });
    root.querySelector('#planning-filter-status').addEventListener('change', (e) => {
      this.filters.status = e.target.value;
      this.loadOccurrences();
    });
    // 复古日期选择器（BUG-14）：值变化走隐藏 input 的 input 事件
    root.querySelector('#planning-filter-date').addEventListener('input', (e) => {
      this.filters.schedule_date = e.target.value;
      this.loadOccurrences();
    });

    this.reminder.attach();
    await this.loadToday();
    if (this.reads !== mountedReads || !this.root) return;
    this.pollTimer = setInterval(() => {
      if (this.reorderMode) return;  // 排列中不重绘，避免打断拖拽
      this.loadToday({ silent: true });
    }, POLL_MS);
    this.reminder.listenForUnload();
  },

  /* ---------- 页签切换（BUG-16） ---------- */

  switchTab(tab) {
    if (!tab || tab === this.activeTab || !this.root) return;
    if (this.reorderMode) {
      // 排列模式只在「当前待办」页签内有效，切走即退出并还原列表
      this.exitReorder();
      this.loadToday();
    }
    if (this.activeTab === 'memo') this.memo?.flushPending();   // 切走前保存未落库内容
    this.activeTab = tab;
    this.root.querySelectorAll('#planning-tabs .tab').forEach((el) => {
      el.classList.toggle('active', el.dataset.tab === tab);
    });
    this.root.querySelectorAll('.plan-region[data-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.panel !== tab;
    });
    this.renderReadFeedback();
    this.refreshVisible({ onlyInvalid: true });
    this.syncToolbarForTab(tab);
    if (tab === 'memo') {
      if (!this.loadedTabs.has('memo')) {
        // 备忘录数据按需加载；编辑器中的未保存内容由模块自身生命周期承接
        this.loadedTabs.add('memo');
        this.memo ||= createPlanningMemo();
        this.memo.mount(this.root.querySelector('[data-memo-root]'));
      } else {
        // 已挂载过的备忘录重返（BUG-08）：首次读取失败不能被当成已加载，
        // 重返必须重读（含读取其他设备的更新），否则失败态一直挂着
        this.memo?.show();
      }
    }
    // 长期目标 / 每日总结为占位页签，无数据需要加载
  },

  /** 工具栏按钮按页签显隐（2026-10-03 确认）：「新建待办」只在当前待办；
   *  「重新计算时间」「调整顺序」在当前待办与全部待办；备忘录页签顶部
   *  放「新建备忘录」（从备忘录区块工具栏移入，打开后由模块单实例守卫）。 */
  syncToolbarForTab(tab) {
    if (!this.root) return;
    this.root.querySelectorAll('[data-toolbar-tab]').forEach((el) => {
      const scope = el.dataset.toolbarTab;
      el.hidden = scope === 'today' ? tab !== 'today'
        : scope === 'memo' ? tab !== 'memo'
          : !(tab === 'today' || tab === 'all');
    });
  },

  switchSection(section) {
    if (!section || section === this.activeSection || !this.root) return;
    this.activeSection = section;
    this.root.querySelectorAll('#planning-today .subtab').forEach((el) => {
      el.classList.toggle('active', el.dataset.section === section);
    });
    for (const key of ['progress', 'attention', 'done']) {
      const el = this.root.querySelector(`#planning-${key}`);
      if (el) el.hidden = key !== section;
    }
  },

  /** 把 .retro-time 宿主初始化为复古选择器（mode：datetime/date/time）。 */
  initRetroFields(scope, selectOptionsById = {}) {
    scope.querySelectorAll('.retro-time[data-retro-for]').forEach((host) => {
      createRetroTimeField(host, {
        id: host.dataset.retroFor,
        value: host.dataset.retroValue || '',
        mode: host.dataset.retroMode || 'datetime',
        align: host.dataset.retroAlign || 'left',
      });
    });
    scope.querySelectorAll('.retro-select[data-retro-select]').forEach((host) => {
      createRetroSelectField(host, {
        id: host.dataset.retroSelect,
        value: host.dataset.retroValue || '',
        options: selectOptionsById[host.dataset.retroSelect] || [],
      });
    });
  },

  unmount() {
    this.reads?.dispose();
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.memo?.dispose();
    this.memo = null;
    this.loadedTabs = null;
    this.reminder.dispose();
    this.detail = null;
    this.reads = null;
    this.readErrors = null;
    this.savedRefresh = null;
    this.root = null;
  },


  /* ---------- 数据加载 ---------- */

  async loadList(name, { key = '', request, apply, silent = false, fresh = false }) {
    const reads = this.reads;
    if (!reads || !this.root) return true;
    if (fresh) reads.invalidate([name]);
    const result = await reads.read(name, { key, request, apply });
    if (this.reads !== reads || !this.root || result.ignored) return true;
    if (result.error) {
      if (!silent) {
        this.readErrors.set(name, result.error.message);
        // 首次加载失败不继续显示“正在加载”；已有列表留在原位供用户查看。
        if (!reads.hasData(name)) {
          const selector = { today: '#planning-progress', tasks: '#planning-tasks',
            occurrences: '#planning-occurrences' }[name];
          this.root.querySelector(selector).innerHTML = errorBlock('列表读取失败，请刷新重试');
        }
        this.renderReadFeedback();
      }
      return false;
    }
    this.readErrors.delete(name);
    if (this.savedRefresh?.names.every((list) => !reads.needsRead(list))) this.savedRefresh = null;
    this.renderReadFeedback();
    return true;
  },

  loadToday(options = {}) {
    return this.loadList('today', {
      ...options,
      request: () => gw('/admin/api/planning/today'),
      apply: (board) => {
        this.board = board;
        this.renderBoard();
        this.reminder.checkAlarms(board);
      },
    });
  },

  loadTasks(options = {}) {
    return this.loadList('tasks', {
      ...options,
      request: () => gw('/admin/api/planning/tasks?include_inactive=true'),
      apply: (tasks) => { this.tasks = tasks; this.renderTasks(); },
    });
  },

  loadOccurrences(options = {}) {
    const params = new URLSearchParams();
    if (this.filters.task_type) params.set('task_type', this.filters.task_type);
    if (this.filters.status) params.set('status', this.filters.status);
    if (this.filters.schedule_date) params.set('schedule_date', this.filters.schedule_date);
    const suffix = params.toString();
    return this.loadList('occurrences', {
      ...options, key: suffix,
      request: () => gw(`/admin/api/planning/occurrences${suffix ? `?${suffix}` : ''}`),
      apply: (occurrences) => { this.occurrences = occurrences; this.renderOccurrences(); },
    });
  },

  visibleLists() {
    return this.activeTab === 'today' ? ['today']
      : this.activeTab === 'all' ? ['tasks', 'occurrences'] : [];
  },

  async refreshVisible({ onlyInvalid = false } = {}) {
    const names = this.visibleLists();
    const load = { today: () => this.loadToday(), tasks: () => this.loadTasks(),
      occurrences: () => this.loadOccurrences() };
    const results = await Promise.all(names.filter((name) => !onlyInvalid || this.reads?.needsRead(name))
      .map((name) => load[name]()));
    return results.every(Boolean);
  },

  async taskSaved(editing) {
    // 保存前发出的慢轮询自此失效；隐藏列表仅标记，下一次切入再读取。
    // 表单关闭/路由重挂载不会撤销业务写入；成功时刷新当前挂载的页面。
    const mountedReads = this.reads;
    if (!mountedReads || !this.root) return;
    mountedReads.invalidate();
    const names = this.visibleLists();
    const ok = await this.refreshVisible();
    if (this.reads !== mountedReads || !this.root || ok) return;
    this.savedRefresh = { names, message: editing ? '待办已保存，列表更新失败' : '待办已创建，列表更新失败' };
    this.renderReadFeedback();
    toast(`${this.savedRefresh.message}，请刷新重试`, 'warn');
  },

  renderReadFeedback() {
    if (!this.root || !this.readErrors) return;
    const host = this.root.querySelector('#planning-load-feedback');
    const labels = { today: '当前待办', tasks: '任务定义', occurrences: '出现记录' };
    const visibleErrors = this.visibleLists().filter((name) => this.readErrors.has(name));
    const message = this.savedRefresh?.message || visibleErrors
      .map((name) => `${labels[name]}读取失败：${this.readErrors.get(name)}`).join('；');
    host.hidden = !message;
    host.innerHTML = message ? errorBlock(`${esc(message)} <button type="button" class="btn btn-secondary btn-sm" data-act="retry-lists">${icon('refresh')}刷新重试</button>`) : '';
  },

  async retryLists() {
    const reads = this.reads;
    const names = new Set([...this.visibleLists(), ...(this.savedRefresh?.names || [])]);
    const load = { today: () => this.loadToday(), tasks: () => this.loadTasks(),
      occurrences: () => this.loadOccurrences() };
    await Promise.all([...names].map((name) => load[name]()));
    if (this.reads === reads && this.root) this.renderReadFeedback();
  },

  /* ---------- 渲染 ---------- */

  itemMeta(occ) {
    return itemMeta(occ);
  },

  /* ---------- 耗时展示口径（2026-10-01 确认，§12.3） ----------
     已完成 / 已删除（含历史超时）记录：手填实际耗时优先并标注
     「实际耗时」；未手填展示预估并标注「预估耗时」，自动计算的实际
     经过时间不再是默认展示值。开放实例保持既有展示（自动实际耗时仅在
     已有事实时出现）。 */
  isClosedOcc(occ) {
    return isClosedOcc(occ);
  },

  formatLoggedDuration(seconds) {
    return formatLoggedDuration(seconds);
  },

  durationText(occ) {
    return durationText(occ);
  },

  durationDetailRows(occ) {
    return durationDetailRows(occ);
  },

  itemBadges(occ) {
    return itemBadges(occ, this.conflictOccIds);
  },

  itemHtml(occ, options = {}) {
    return itemHtml(occ, options, this.conflictOccIds);
  },

  renderBoard() {
    const board = this.board;
    if (!board || !this.root) return;
    // 排程冲突是派生结果（不落库）：看板读取时由后端同一纯函数派生，
    // 前端仅以徽章 + 详情原因呈现
    this.conflictOccIds = new Set((board.conflicts || []).map((c) => c.occurrence_id));
    this.conflictById = new Map((board.conflicts || []).map((c) => [c.occurrence_id, c]));
    const progress = this.root.querySelector('#planning-progress');
    const attention = this.root.querySelector('#planning-attention');
    const done = this.root.querySelector('#planning-done');
    const inReorder = this.reorderMode;

    progress.innerHTML = board.progress.length
      ? board.progress.map((occ) => this.itemHtml(occ, {
        draggable: inReorder,
        idle: occ.task_type === 'idle',
      })).join('')
      : miniEmpty('今天还没有待办，新建一个或等 0 点刷新');
    attention.innerHTML = board.attention.length
      ? board.attention.map((occ) => this.itemHtml(occ)).join('')
      : miniEmpty('没有需要处理的异常待办');
    done.innerHTML = board.done.length
      ? board.done.map((occ) => this.itemHtml(occ, { closed: true })).join('')
      : miniEmpty('今天还没有关闭的记录');

    this.root.querySelector('#planning-progress-count').textContent = `${board.progress.length}`;
    this.root.querySelector('#planning-attention-count').textContent = `${board.attention.length}`;
    this.root.querySelector('#planning-done-count').textContent = `${board.done.length}`;

    const banner = this.root.querySelector('#planning-alarm-banner');
    if (board.recompute?.pending) {
      banner.innerHTML = `
        <div class="plan-alarm-bar">
          ${icon('clock')}
          <div>列表有变化，等待自动重算（约 ${board.recompute.wait_minutes ?? '?'} 分钟后执行）。手动「重新计算时间」可立即执行。</div>
        </div>`;
    } else {
      banner.innerHTML = '';
    }

    if (this.selected?.kind === 'occ') {
      const occ = this.findOccurrence(this.selected.id);
      // 轮询保护：详情栏内有用户修改过但尚未保存的输入（闹钟/计时器）时，
      // 跳过本轮详情重绘，不覆盖未保存内容（保存/切换后自然恢复刷新）
      if (occ && !this.detailHasUnsavedInput()) this.showOccurrenceDetail(occ);
    }
    if (inReorder) this.attachDragHandlers();
  },

  detailHasUnsavedInput() {
    const body = this.detail?.body;
    if (!body) return false;
    return [...body.querySelectorAll('input')].some((el) => {
      if (el.type === 'checkbox') return el.checked !== el.defaultChecked;
      return el.value !== el.defaultValue;
    });
  },

  findOccurrence(id) {
    const b = this.board;
    if (!b) return null;
    for (const key of ['progress', 'attention', 'done']) {
      const found = b[key].find((occ) => occ.id === id);
      if (found) return found;
    }
    return null;
  },

  renderTasks() {
    const host = this.root.querySelector('#planning-tasks');
    this.root.querySelector('#planning-tasks-count').textContent = `${this.tasks.length}`;
    if (!this.tasks.length) {
      host.innerHTML = empty('还没有任务定义', '点击「新建待办」创建');
      return;
    }
    host.innerHTML = this.tasks.map((task) => `
      <div class="plan-item ${task.is_active ? '' : 'is-closed'}" data-task="${task.id}" role="button" tabindex="0">
        <div class="plan-item-main">
          <div class="plan-item-title">${esc(task.content)}</div>
          <div class="plan-item-meta">
            <span>${taskTypeSummary(task)}</span>
            ${task.estimated_minutes ? `<span>预估耗时 ${task.estimated_minutes}m</span>` : ''}
            ${(task.window_start_tod || task.window_end_tod) ? `<span>时段 ${task.window_start_tod || '无'}～${task.window_end_tod || '无'}</span>` : ''}
            ${task.is_hollow ? '<span>中空待办</span>' : ''}
            ${task.next_due ? `<span>下次到期 ${fmtDue(task.next_due)}</span>` : ''}
            ${task.timer_minutes ? `<span>计时器 ${task.timer_minutes}m</span>` : ''}
          </div>
        </div>
        <div class="plan-item-side"><div class="tag-row">
          ${tag(esc(TASK_TYPE_LABELS[task.task_type] || task.task_type), 'gold')}
          ${(task.window_start_tod || task.window_end_tod) ? tag('时段', 'slate') : ''}
          ${task.is_fixed ? tag('固定', 'slate') : ''}
          ${task.alarm_start || task.alarm_end ? tag('闹钟', 'plum') : ''}
          ${task.is_active ? '' : tag('已删除', 'red')}
        </div></div>
      </div>`).join('');
  },

  renderOccurrences() {
    const host = this.root.querySelector('#planning-occurrences');
    this.root.querySelector('#planning-occ-count').textContent = `${this.occurrences.length}`;
    if (!this.occurrences.length) {
      host.innerHTML = empty('没有符合筛选条件的出现记录');
      return;
    }
    host.innerHTML = this.occurrences.map((occ) => `
      <div class="plan-item ${CLOSED_STATUSES.includes(occ.status) ? 'is-closed' : ''}"
           data-occ-all="${occ.id}" role="button" tabindex="0">
        <div class="plan-item-main">
          <div class="plan-item-title">${esc(occ.content)}</div>
          <div class="plan-item-meta">
            <span>${esc(occ.schedule_date || occ.for_date)}</span>
            <span>${this.itemMeta(occ)}</span>
          </div>
        </div>
        <div class="plan-item-side"><div class="tag-row">
          ${tag(esc(TASK_TYPE_LABELS[occ.task_type] || ''), 'gold')}
          ${this.itemBadges(occ)}
        </div></div>
      </div>`).join('');
  },

  /* ---------- 详情栏 ---------- */

  select(kind, id) {
    this.selected = { kind, id };
    this.root.querySelectorAll('.plan-item').forEach((el) => el.classList.remove('selected'));
    if (kind === 'occ') {
      const occ = this.findOccurrence(id) || this.occurrences.find((o) => o.id === id);
      if (occ) this.showOccurrenceDetail(occ);
    } else {
      const task = this.tasks.find((t) => t.id === id);
      if (task) this.showTaskDetail(task);
    }
  },

  async showOccurrenceDetail(occ) {
    this.markSelected(`[data-occ="${occ.id}"], [data-occ-all="${occ.id}"]`);
    const actions = this.occurrenceActions(occ);
    this.detail.render({
      title: esc(occ.content),
      badges: `<div class="tag-row">${tag(esc(TASK_TYPE_LABELS[occ.task_type] || ''), 'gold')}${this.itemBadges(occ)}</div>`,
      html: `
        <div class="kv"><span class="k">原始规划周期</span><span class="v">${esc(occ.schedule_date || occ.for_date)}</span></div>
        ${occ.display_cycle_date ? `<div class="kv"><span class="k">当前展示周期</span><span class="v">${esc(occ.display_cycle_date)}</span></div>` : ''}
        <div class="kv"><span class="k">预估时间</span><span class="v">${fmtRange(occ.est_start, occ.est_end)}</span></div>
        ${(occ.window_start_at || occ.window_end_at) ? `<div class="kv"><span class="k">可安排时段</span><span class="v">${[
          occ.window_start_at ? `不早于 ${fmtClock(occ.window_start_at)}` : '',
          occ.window_end_at ? `最晚完成 ${fmtClock(occ.window_end_at)}` : '',
        ].filter(Boolean).join('，')}</span></div>` : ''}
        <div class="kv"><span class="k">实际时间</span><span class="v">${fmtRange(occ.actual_start, occ.actual_end)}</span></div>
        ${this.durationDetailRows(occ)}
        ${occ.is_limited ? `<div class="kv"><span class="k">限时截止</span><span class="v">${fmtClock(occ.deadline_at)}</span></div>` : ''}
        ${occ.partial_note ? `<div class="kv kv-block"><span class="k">部分完成说明</span><span class="v">${esc(occ.partial_note)}</span></div>` : ''}
        ${occ.partial_at ? `<div class="kv"><span class="k">部分完成时间</span><span class="v">${fmtDue(occ.partial_at)}</span></div>` : ''}
        <div class="kv kv-block"><span class="k">提醒</span><span class="v">
          <div class="tag-row" data-alarm-controls data-task-id="${occ.task_id}">
            <label class="inline"><input type="checkbox" data-alarm-start ${occ.alarm_start ? 'checked' : ''}> 开始闹钟</label>
            <label class="inline"><input type="checkbox" data-alarm-end ${occ.alarm_end ? 'checked' : ''}> 结束闹钟</label>
            <input type="text" data-timer-input placeholder="计时器，如 1h30m" value="${esc(occ.timer_minutes ?? '')}" style="width:130px">
            <button class="btn btn-secondary btn-sm" data-act="occ-save-alarm" data-id="${occ.id}">${icon('check')}保存提醒</button>
          </div>
          <p class="muted text-sm" style="margin:4px 0 0">改动保存到所属待办；计时器支持 1h30m 简写，留空保存即清除。</p>
        </span></div>
        <div class="kv"><span class="k">当前状态</span><span class="v">${esc(occ.schedule_label)}</span></div>
        ${this.conflictById?.get(occ.id) ? `<div class="kv kv-block"><span class="k">排程冲突</span><span class="v">${esc(this.conflictById.get(occ.id).reason)}</span></div>` : ''}`,
      actions,
    });
  },

  /** BUG-11：详情栏「保存提醒」→ PATCH 所属任务定义的闹钟/计时器字段。 */
  async saveOccurrenceAlarm(occId) {
    const host = this.detail?.body?.querySelector('[data-alarm-controls]');
    if (!host) return;
    const taskId = Number(host.dataset.taskId);
    const timer = host.querySelector('[data-timer-input]').value.trim();
    try {
      await gw(`/admin/api/planning/tasks/${taskId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          alarm_start: host.querySelector('[data-alarm-start]').checked,
          alarm_end: host.querySelector('[data-alarm-end]').checked,
          timer_minutes: timer || null,
        }),
      });
      toast('提醒已保存');
      this.reads?.invalidate();
      await this.loadToday();
      await this.loadOccurrences();
      const fresh = this.findOccurrence(occId) || this.occurrences.find((o) => o.id === occId);
      if (fresh) this.showOccurrenceDetail(fresh);
    } catch (error) {
      toast(`保存提醒失败：${error.message}`, 'err');
    }
  },

  occurrenceActions(occ) {
    const btn = (act, label, icon_name = 'check', cls = 'btn-secondary') =>
      `<button class="btn ${cls} btn-sm" data-act="occ-${act}" data-id="${occ.id}">${icon(icon_name)}${label}</button>`;
    const parts = [];
    if (occ.status === 'pending') {
      parts.push(btn('start', '开始', 'check', 'btn-primary'));
      parts.push(btn('complete', '已完成'));
    }
    if (occ.status === 'in_progress') {
      parts.push(btn('finish', '结束', 'check', 'btn-primary'));
      parts.push(btn('complete', '已完成'));
    }
    if (OPEN_STATUSES.includes(occ.status)) {
      // 部分完成 = 记录一次部分完成事实，实例保持开放、可继续处理
      parts.push(btn('partial', occ.status === 'partial' ? '更新部分完成说明' : '部分完成'));
      parts.push(btn('defer', '延后', 'clock'));
      parts.push(btn('discard-this', '此次不执行'));
      parts.push(btn('edit-time', '调整时段', 'edit'));
      parts.push(btn('spawn-remaining', '剩余另建待办', 'plus'));
      parts.push(btn('split', '拆分待办', 'layers'));
      parts.push(btn('discard', '删除待办', 'x', 'btn-danger-line'));
    }
    if (occ.status === 'partial') {
      // 已全部完成：最终补完时间入账，处理后刷新型以此为下一轮基准
      parts.push(btn('finish', '已全部完成', 'check', 'btn-primary'));
    }
    if (occ.status === 'timeout') {
      // 超时实例不复活：重新安排 = 保留超时历史 + 新建单次待办
      parts.push(btn('reschedule-timeout', '重新安排', 'clock', 'btn-primary'));
      parts.push(btn('discard-this', '此次不执行'));
      parts.push(btn('discard', '删除待办', 'x', 'btn-danger-line'));
    }
    if (CLOSED_STATUSES.includes(occ.status)) {
      // 已关闭历史不复活（过去不重写）：只允许补填实际时间
      parts.push(btn('backfill', '补填实际时间', 'edit'));
    }
    return parts.join('');
  },

  async showTaskDetail(task) {
    this.markSelected(`[data-task="${task.id}"]`);
    const parts = [];
    if (task.is_active && !task.is_hollow
        && ['interval', 'weekly', 'monthly'].includes(task.task_type)) {
      // 中空待办的提前处理由完整轮次承载，提前完成按钮必然 409——隐藏不适用入口
      const earlyHint = task.refresh_mode === 'after_completion'
        ? '提前完成：记录本次完成，并从现在重新计算下一次刷新时间'
        : '提前完成：记录本次额外完成，不影响后续固定刷新';
      parts.push(`<button class="btn btn-primary btn-sm" data-act="task-early" data-id="${task.id}" title="${esc(earlyHint)}">${icon('check')}提前完成</button>`);
    }
    // 暂停刷新 = 只阻止未来周期轮次生成（需求 24）：任务、规则、当前实例、
    // 历史事实与固定时间轴都不动；refresh_enabled 缺失视为开启（与后端一致）
    if (task.is_active && PAUSABLE_TYPES.includes(task.task_type)) {
      const paused = task.refresh_enabled === false;
      parts.push(`<button class="btn btn-secondary btn-sm" data-act="${paused ? 'task-resume-refresh' : 'task-pause-refresh'}" data-id="${task.id}">${icon(paused ? 'refresh' : 'clock')}${paused ? '恢复刷新' : '暂停刷新'}</button>`);
    }
    parts.push(`<button class="btn btn-secondary btn-sm" data-act="task-edit" data-id="${task.id}">${icon('edit')}编辑</button>`);
    if (task.is_active) {
      parts.push(`<button class="btn btn-danger-line btn-sm" data-act="task-discard" data-id="${task.id}">${icon('x')}删除待办</button>`);
    } else if (task.request_state === 'superseded') {
      // H2/I6：被取代的重排请求为终态，不提供重新启用入口
      parts.push(`<span class="muted text-sm">已被取代的重排请求</span>`);
    } else {
      parts.push(`<button class="btn btn-secondary btn-sm" data-act="task-enable" data-id="${task.id}">${icon('refresh')}重新启用</button>`);
    }
    this.detail.render({
      title: esc(task.content),
      badges: `<div class="tag-row">${tag(esc(TASK_TYPE_LABELS[task.task_type] || ''), 'gold')}${task.refresh_enabled === false && task.is_active ? tag('刷新已暂停', 'slate') : ''}${task.is_active ? '' : tag('已删除', 'red')}</div>`,
      html: `
        <div class="kv"><span class="k">重复规则</span><span class="v">${esc(taskTypeSummary(task))}</span></div>
        <div class="kv"><span class="k">可安排时段</span><span class="v">${(task.window_start_tod || task.window_end_tod)
          ? `${esc(task.window_start_tod || '无')} ～ ${esc(task.window_end_tod || '无')}${(task.window_start_tod && task.window_end_tod && task.window_end_tod < task.window_start_tod) ? '（结束在次日）' : ''}`
          : '未设置（正常自动排程）'}</span></div>
        ${task.estimated_minutes ? `<div class="kv"><span class="k">预估耗时</span><span class="v">${task.estimated_minutes}m</span></div>` : ''}
        ${task.is_hollow ? `<div class="kv kv-block"><span class="k">中空待办</span><span class="v">
          开始：${esc(task.hollow_start_content || task.content)}（${task.hollow_start_minutes}m）
          → 中间 ${task.hollow_wait_minutes}m${task.hollow_wait_note ? `（${esc(task.hollow_wait_note)}）` : ''}
          → 结束：${esc(task.hollow_end_content || task.content)}（${task.hollow_end_minutes}m）</span></div>` : ''}
        ${task.is_limited ? `<div class="kv"><span class="k">限时截止</span><span class="v">${esc(task.deadline_tod)}${task.deadline_end_tod ? `～${esc(task.deadline_end_tod)}` : ''}</span></div>` : ''}
        <div class="kv"><span class="k">提醒</span><span class="v">${[
          task.alarm_start ? '开始闹钟' : '',
          task.alarm_end ? '结束闹钟' : '',
          task.timer_minutes ? `计时器 ${task.timer_minutes}m` : '',
        ].filter(Boolean).join('、') || '无'}</span></div>
        ${task.next_due ? `<div class="kv muted text-sm"><span class="k">下次到期</span><span class="v">${fmtDue(task.next_due)}</span></div>` : ''}
        <div class="kv muted text-sm"><span class="k">生成游标</span><span class="v">${esc(task.cursor_date || '-')}</span></div>`,
      actions: parts.join(''),
    });
  },

  markSelected(selector) {
    this.root.querySelectorAll('.plan-item').forEach((el) => el.classList.remove('selected'));
    const el = this.root.querySelector(selector);
    if (el) el.classList.add('selected');
  },

  /* ---------- 排列模式（指针拖拽） ---------- */

  enterReorder() {
    return this.sort.enterReorder();
  },

  exitReorder() {
    return this.sort.exitReorder();
  },

  async confirmReorder() {
    return this.sort.confirmReorder();
  },

  async cancelReorder() {
    return this.sort.cancelReorder();
  },

  attachDragHandlers() {
    return this.sort.attachDragHandlers();
  },

  startDrag(e, item) {
    return this.sort.startDrag(e, item);
  },

  /* ---------- 动作 ---------- */

  async runRecompute() {
    try {
      const result = await gw('/admin/api/planning/recompute', { method: 'POST' });
      if (result.conflicts?.length) {
        // 冲突 = 本轮整体未生效（既有 est 保留）：三要素逐条呈现
        this.showConflictsModal(result.conflicts, result.updated);
      } else {
        toast(`已重新计算，更新了 ${result.updated} 项待办时间`);
      }
      this.reads?.invalidate(['today', 'occurrences']);
      await this.loadToday();
    } catch (error) {
      toast(`重算失败：${error.message}`, 'err');
    }
  },

  showConflictsModal(conflicts, updated) {
    const contentOf = (c) => {
      const occ = this.findOccurrence(c.occurrence_id);
      return occ?.content || `待办 #${c.occurrence_id}`;
    };
    const { root, close } = modal({
      title: '排程冲突',
      body: `
        <p class="muted text-sm">本轮重算整体未保存（已更新 ${updated ?? 0} 项，既有时间保持不变）：以下待办无法同时满足列表顺序与硬约束。请调整顺序或时段后重试。</p>
        ${conflicts.map((c) => `
          <div class="kv kv-block"><span class="k">${esc(contentOf(c))}${c.phase ? `（${c.phase === 'start' ? '开始阶段' : '结束阶段'}）` : ''}</span>
          <span class="v">${esc(c.reason)}</span></div>`).join('')}`,
      footer: `<button class="btn btn-primary" data-cancel>知道了</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
  },

  /* 完成耗时手填（2026-10-01 确认，§12.3）：点「完成」/「结束」弹出实际
     耗时输入框——h / m / s 后缀（无后缀默认分钟，可组合如 1h1m1s），
     可留空（留空不是错误，展示回退预估并标注预估）。原始文本交给后端
     权威解析，前端不做二次口径判断。 */
  askCompleteDuration(id, post) {
    return this.dialogs.askCompleteDuration(id, post);
  },

  async occurrenceAction(act, id) {
    const post = (path, body) => gw(`/admin/api/planning/occurrences/${id}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    try {
      if (act === 'start') await post('/start');
      else if (act === 'finish' || act === 'complete') return this.askCompleteDuration(id, post);
      else if (act === 'partial') return this.askPartial(id);
      else if (act === 'defer') return this.askNewTime(id, 'deferred', '延后到什么时间？');
      else if (act === 'reschedule-timeout') return this.askRescheduleTimeout(id);
      else if (act === 'reschedule') return this.askNewTime(id, 'pending', '重新安排到什么时间？');
      else if (act === 'save-alarm') return this.saveOccurrenceAlarm(id);
      else if (act === 'discard-this') {
        if (!(await confirm('确认「此次不执行」？只关闭这一次出现，不影响后续刷新。', { danger: false }))) return;
        await post('/status', { status: 'discarded_this' });
      } else if (act === 'discard') {
        if (!(await confirm('确认删除？该待办后续不再自动出现。'))) return;
        await post('/status', { status: 'discarded' });
      } else if (act === 'edit-time') return this.askEditTime(id);
      else if (act === 'backfill') return this.askBackfill(id);
      else if (act === 'split') return this.askSplit(id);
      else if (act === 'spawn-remaining') return this.askRemaining(id);
      toast('已更新');
      this.reads?.invalidate();
      await this.loadToday();
      await this.loadOccurrences();
    } catch (error) {
      toast(`操作失败：${error.message}`, 'err');
    }
  },

  async taskAction(act, id, el = null) {
    try {
      if (act === 'early') {
        const task = this.tasks.find((t) => t.id === id);
        const result = await gw(`/admin/api/planning/tasks/${id}/complete-early`, {
          method: 'POST', headers: { 'Idempotency-Key': crypto.randomUUID() },
        });
        // 30 分钟防重复窗口（后端权威）：命中时本次不新增完成记录，提示
        // 距上次完成多久、还剩多久可再次提前完成；字段全部来自服务端
        // 持久化完成事实，前端不依赖客户端时钟判断重复
        if (result && result.duplicate_within_window) {
          const elapsedMin = Math.max(1, Math.round((result.elapsed_seconds || 0) / 60));
          const remainMin = Math.max(1, Math.round((result.retry_after_seconds || 0) / 60));
          toast(`你在约 ${elapsedMin} 分钟前刚完成过这个待办，本次操作不会重复记录；约 ${remainMin} 分钟后可再次提前完成`, 'warn');
        } else {
          // 固定刷新型只记录额外完成，时间轴不动；处理后刷新型才重置基准
          toast(task && task.refresh_mode === 'after_completion'
            ? '已记录本次完成，下一次将从现在重新计算'
            : '已记录本次额外完成，后续固定刷新不受影响');
        }
      } else if (act === 'edit') {
        const task = this.tasks.find((t) => t.id === id);
        if (task) this.openTaskForm(task);
        return;
      } else if (act === 'discard') {
        if (!(await confirm('删除整个待办？后续不再刷新，当天未完成的实例也会关闭。'))) return;
        await gw(`/admin/api/planning/tasks/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_active: false }),
        });
        toast('待办已删除');
      } else if (act === 'enable') {
        await gw(`/admin/api/planning/tasks/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_active: true }),
        });
        toast('任务已重新启用');
      } else if (act === 'pause-refresh' || act === 'resume-refresh') {
        // 暂停刷新 ≠ 删除待办 / 此次不执行 / 完成：只写 refresh_enabled，
        // 任务定义、周期规则、当前实例与历史事实全部保持原样（需求 24）
        const resuming = act === 'resume-refresh';
        if (!resuming && !(await confirm(
          '暂停后不会继续生成新的周期待办，当前已经生成的待办不会受到影响。之后可以随时恢复。',
          { title: '暂停刷新', okText: '暂停刷新', cancelText: '取消', danger: false },
        ))) return;
        if (el) el.disabled = true;  // in-flight guard：请求期间防连续点击重复 PATCH
        try {
          await gw(`/admin/api/planning/tasks/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ refresh_enabled: resuming }),
          });
          toast(resuming ? '已恢复刷新' : '已暂停刷新');
        } finally {
          if (el) el.disabled = false;
        }
      }
      this.reads?.invalidate();
      await Promise.all([this.loadTasks(), this.loadToday()]);
      if (act === 'pause-refresh' || act === 'resume-refresh') {
        // 按钮状态只来自服务端持久化数据，刷新页面后同样正确（需求 24H）
        const fresh = this.tasks.find((t) => t.id === id);
        if (fresh) this.showTaskDetail(fresh);
      }
    } catch (error) {
      toast(`操作失败：${error.message}`, 'err');
    }
  },

  askPartial(id) {
    return this.dialogs.askPartial(id);
  },

  async askRescheduleTimeout(id) {
    return this.dialogs.askRescheduleTimeout(id);
  },

  askNewTime(id, targetStatus, title) {
    return this.dialogs.askNewTime(id, targetStatus, title);
  },

  askEditTime(id) {
    return this.dialogs.askEditTime(id);
  },

  askBackfill(id) {
    return this.dialogs.askBackfill(id);
  },

  askSplit(id) {
    return this.dialogs.askSplit(id);
  },

  askRemaining(occ) {
    return this.dialogs.askRemaining(occ);
  },

  clearFilters() {
    this.filters = { task_type: '', status: '', schedule_date: '' };
    this.root.querySelector('#planning-filter-type').value = '';
    this.root.querySelector('#planning-filter-status').value = '';
    const dateInput = this.root.querySelector('#planning-filter-date');
    if (dateInput._applyRetroValue) dateInput._applyRetroValue('', true);  // 静默清空，避免触发 input 再拉一次
    else dateInput.value = '';
    this.loadOccurrences();
  },

  /* ---------- 新建 / 编辑任务表单 ---------- */

  openTaskForm(task) {
    if (this.taskForm?.isSubmitting()) {
      toast(this.taskForm.editing ? '待办正在保存，请等待完成' : '待办正在创建，请等待完成', 'warn');
      return;
    }
    this.taskForm = openTaskForm(task, {
      occurrences: this.occurrences,
      initRetroFields: (scope, options) => this.initRetroFields(scope, options),
      onSaved: () => this.taskSaved(!!task?.id),
    });
    return this.taskForm;
  },

  /* ---------- 事件委托（详情动作） ---------- */

  bindDetailActions(scope, mountedReads) {
    scope.addEventListener('click', (e) => {
      if (this.reads !== mountedReads) return;
      const el = e.target.closest('[data-act]');
      if (!el || el.disabled) return;
      const act = el.dataset.act;
      const id = Number(el.dataset.id);
      if (act.startsWith('occ-')) {
        e.stopPropagation();
        this.occurrenceAction(act.slice(4), id);
      } else if (act.startsWith('task-')) {
        e.stopPropagation();
        this.taskAction(act.slice(5), id, el);
      }
    });
  },

  handleItemClick(e) {
    if (this.reorderMode) return;
    const occEl = e.target.closest('[data-occ]');
    if (occEl) { this.select('occ', Number(occEl.dataset.occ)); return; }
    const occAllEl = e.target.closest('[data-occ-all]');
    if (occAllEl) { this.select('occ', Number(occAllEl.dataset.occAll)); return; }
    const taskEl = e.target.closest('[data-task]');
    if (taskEl) this.select('task', Number(taskEl.dataset.task));
  },
};
