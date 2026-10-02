// pages/planning.js - 规划管理：四类型待办 + 时间排程 + 排列模式 + 浏览器闹钟/计时器
// 四区域以页签切换（复用记忆管理 .tabs/.tab），「当前待办」内再以 .subtabs 三分区切换；
// 数据按需加载：今日看板 30 秒轮询，全部待办首次切到该页签时才拉取。
import { gw } from '../api.js?v=20260930-planning11';
import {
  loading, empty, errorBlock, tag, toast, modal, confirm, delegate, icon, fmtDate, esc,
  createDetailPanel,
} from '../ui.js?v=20260930-planning11';
import { createRetroTimeField } from '../lib/retro_time.js?v=20260930-planning11';
import { createRetroSelectField } from '../lib/retro_select.js?v=20260930-planning11';

const TASK_TYPE_LABELS = {
  daily: '每日', interval: '间歇', weekly: '每周', monthly: '每月', once: '单次', idle: '闲时',
};
const TASK_TYPES = Object.keys(TASK_TYPE_LABELS);
const WEEKDAY_NAMES = ['一', '二', '三', '四', '五', '六', '日'];
const STATUS_META = {
  // BUG-12：pending/in_progress 显示名与今日分区名（待处理/进度中）撞车，改为「未开始/执行中」
  pending: { label: '未开始', tone: 'muted' },
  in_progress: { label: '执行中', tone: 'amber' },
  completed: { label: '已完成', tone: 'green' },
  partial: { label: '部分完成', tone: 'gold' },
  deferred: { label: '已延后', tone: 'slate' },
  discarded_this: { label: '此次废弃', tone: 'muted' },
  discarded: { label: '已删除', tone: 'red' },
  timeout: { label: '已超时', tone: 'red' },
};
// 部分完成属于开放生命周期：实例仍在「进度中」，直到「已全部完成」才关闭
const CLOSED_STATUSES = ['completed', 'discarded_this', 'discarded'];
const OPEN_STATUSES = ['pending', 'in_progress', 'deferred', 'partial'];
// 暂停/恢复刷新只面向周期任务（需求 24）；单次/闲时没有周期刷新，不提供该入口
const PAUSABLE_TYPES = ['daily', 'interval', 'weekly', 'monthly'];
// 闹钟错过太久就静默跳过（只对未来 2 分钟内与刚过期的情况响铃）
const ALARM_GRACE_MS = 2 * 60 * 1000;
const POLL_MS = 30 * 1000;

const ALARM_URL = '/admin/assets/audio/alarm-clock.mp3';
const TIMER_URL = '/admin/assets/audio/timer-done.ogg';

function statusTag(status) {
  const meta = STATUS_META[status] || { label: status, tone: 'muted' };
  return tag(esc(meta.label), meta.tone);
}

function fmtClock(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '-';
  return new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date);
}

function fmtRange(start, end) {
  if (!start && !end) return '未排时间';
  return `${fmtClock(start)} ～ ${fmtClock(end)}`;
}

function fmtDue(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

function taskTypeSummary(task) {
  switch (task.task_type) {
    case 'daily': return '每天出现';
    case 'interval': return `每 ${task.interval_days || '?'} 天（完成后起算）`;
    case 'weekly': {
      const days = (task.weekdays || []).map((d) => `周${WEEKDAY_NAMES[d] ?? d}`);
      return days.length ? days.join('、') : '每周（未选星期）';
    }
    case 'monthly': {
      const days = task.month_days || [];
      return days.length ? `每月 ${days.join('、')} 日` : '每月（未选日期）';
    }
    case 'once': return `单次 · ${task.target_date || '未定日期'}`;
    case 'idle': return '闲时处理，沉底显示';
    default: return '';
  }
}

function typeSummaryTag(task) {
  return tag(esc(taskTypeSummary(task)), 'slate');
}

// BUG-13：三分区空态改为一行式小空态（小图标 + 纯文字，高度受限）
function miniEmpty(msg) {
  return `<div class="plan-empty-mini">${icon('feather')}<span>${esc(msg)}</span></div>`;
}

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
  loadedTabs: null,
  pollTimer: null,
  alarmAudio: null,
  timerAudio: null,
  ringModal: null,
  firedKeys: new Set(),
  permissionNoticeShown: false,

  async mount(root) {
    this.root = root;
    this.activeTab = 'today';
    this.activeSection = 'progress';
    this.loadedTabs = new Set();
    root.innerHTML = `
      <div class="page-with-detail" id="planning-layout">
        <div class="page-main">
          <div class="tabs" id="planning-tabs" style="margin-bottom:14px">
            <button class="tab active" data-act="plan-tab" data-tab="today">${icon('calendar')}当前待办</button>
            <button class="tab" data-act="plan-tab" data-tab="all">${icon('inbox')}全部待办</button>
            <button class="tab" data-act="plan-tab" data-tab="goals">${icon('star')}长期目标</button>
            <button class="tab" data-act="plan-tab" data-tab="summary">${icon('journal')}每日总结</button>
          </div>

          <div class="toolbar" style="margin-bottom:14px">
            <button class="btn btn-primary" data-act="new-task">${icon('plus')}新建待办</button>
            <button class="btn btn-secondary" data-act="recompute">${icon('refresh')}重新计算时间</button>
            <button class="btn btn-secondary" data-act="enter-reorder" id="planning-reorder-btn">${icon('sort')}调整顺序</button>
          </div>
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

    this.detail = createDetailPanel(root.querySelector('#planning-layout'));
    this.progressList = root.querySelector('#planning-progress');
    root.addEventListener('click', (e) => this.handleItemClick(e));
    this.bindDetailActions();
    this.initRetroFields(root);

    delegate(root, {
      'new-task': () => this.openTaskForm(null),
      recompute: () => this.runRecompute(),
      'enter-reorder': () => this.enterReorder(),
      'confirm-reorder': () => this.confirmReorder(),
      'cancel-reorder': () => this.cancelReorder(),
      'clear-filters': () => this.clearFilters(),
      stop: () => this.stopRinging(),
      'plan-tab': (el) => this.switchTab(el.dataset.tab),
      'plan-subtab': (el) => this.switchSection(el.dataset.section),
    });
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

    this.requestNotificationPermission();
    // 首次用户手势时静音解锁音频，规避浏览器 autoplay 策略拦截首次响铃（BUG-9）
    this.audioUnlockHandler = () => this.unlockAudio();
    window.addEventListener('pointerdown', this.audioUnlockHandler, { once: true });
    this.loadedTabs.add('today');
    await this.loadToday();
    this.pollTimer = setInterval(() => {
      if (this.reorderMode) return;  // 排列中不重绘，避免打断拖拽
      this.loadToday({ silent: true });
    }, POLL_MS);
    window.addEventListener('beforeunload', this.onUnload = () => this.stopRinging());
  },

  /* ---------- 页签切换（BUG-16） ---------- */

  switchTab(tab) {
    if (!tab || tab === this.activeTab || !this.root) return;
    if (this.reorderMode) {
      // 排列模式只在「当前待办」页签内有效，切走即退出并还原列表
      this.exitReorder();
      this.loadToday();
    }
    this.activeTab = tab;
    this.root.querySelectorAll('#planning-tabs .tab').forEach((el) => {
      el.classList.toggle('active', el.dataset.tab === tab);
    });
    this.root.querySelectorAll('.plan-region[data-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.panel !== tab;
    });
    if (tab === 'all' && !this.loadedTabs.has('all')) {
      this.loadedTabs.add('all');
      this.loadTasks();
      this.loadOccurrences();
    }
    // 长期目标 / 每日总结为占位页签，无数据需要加载
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
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.stopRinging();
    window.removeEventListener('beforeunload', this.onUnload);
    window.removeEventListener('pointerdown', this.audioUnlockHandler);
    this.detail = null;
    this.loadedTabs = null;
    this.root = null;
  },

  unlockAudio() {
    // 静音 play + pause 预热一次，之后到点的响铃不再被 autoplay 策略拦下
    this.ensureAudio();
    const warm = (audio) => {
      audio.play().then(() => {
        audio.pause();
        audio.currentTime = 0;
      }).catch(() => {});
    };
    warm(this.alarmAudio);
    warm(this.timerAudio);
  },

  /* ---------- 数据加载 ---------- */

  async loadAll() {
    await Promise.all([this.loadToday(), this.loadTasks(), this.loadOccurrences()]);
  },

  async loadToday({ silent = false } = {}) {
    try {
      const board = await gw('/admin/api/planning/today');
      this.board = board;
      this.renderBoard();
      this.checkAlarms(board);
    } catch (error) {
      if (!silent) {
        this.root.querySelector('#planning-progress').innerHTML =
          errorBlock(`当前待办读取失败：${esc(error.message)}`);
      }
    }
  },

  async loadTasks() {
    try {
      const tasks = await gw('/admin/api/planning/tasks?include_inactive=true');
      this.tasks = tasks;
      this.renderTasks();
    } catch (error) {
      this.root.querySelector('#planning-tasks').innerHTML =
        errorBlock(`任务定义读取失败：${esc(error.message)}`);
    }
  },

  async loadOccurrences() {
    try {
      const params = new URLSearchParams();
      if (this.filters.task_type) params.set('task_type', this.filters.task_type);
      if (this.filters.status) params.set('status', this.filters.status);
      if (this.filters.schedule_date) params.set('schedule_date', this.filters.schedule_date);
      const suffix = params.toString();
      const occurrences = await gw(`/admin/api/planning/occurrences${suffix ? `?${suffix}` : ''}`);
      this.occurrences = occurrences;
      this.renderOccurrences();
    } catch (error) {
      this.root.querySelector('#planning-occurrences').innerHTML =
        errorBlock(`出现记录读取失败：${esc(error.message)}`);
    }
  },

  /* ---------- 渲染 ---------- */

  itemMeta(occ) {
    const parts = [];
    if (occ.est_start || occ.est_end) parts.push(`预估 ${fmtRange(occ.est_start, occ.est_end)}`);
    if (occ.actual_start || occ.actual_end) {
      parts.push(`实际 ${fmtRange(occ.actual_start, occ.actual_end)}`);
    }
    const duration = this.durationText(occ);
    if (duration) parts.push(duration);
    // 可安排时段是 user 排程约束，与系统预估起止是两套独立语义，分开呈现
    const windowParts = [];
    if (occ.window_start_at) windowParts.push(`不早于 ${fmtClock(occ.window_start_at)}`);
    if (occ.window_end_at) windowParts.push(`最晚完成 ${fmtClock(occ.window_end_at)}`);
    if (windowParts.length) parts.push(`时段 ${windowParts.join('，')}`);
    if (occ.partial_note) parts.push(`说明：${esc(occ.partial_note)}`);
    return parts.join(' · ');
  },

  /* ---------- 耗时展示口径（2026-10-01 确认，§12.3） ----------
     已完成 / 已删除（含历史超时）记录：手填实际耗时优先并标注
     「实际耗时」；未手填展示预估并标注「预估耗时」，自动计算的实际
     经过时间不再是默认展示值。开放实例保持既有展示（自动实际耗时仅在
     已有事实时出现）。 */
  isClosedOcc(occ) {
    return CLOSED_STATUSES.includes(occ.status) || occ.status === 'timeout';
  },

  formatLoggedDuration(seconds) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    const parts = [];
    if (h) parts.push(`${h}h`);
    if (m) parts.push(`${m}m`);
    if (s || !parts.length) parts.push(`${s}s`);
    return parts.join('');
  },

  durationText(occ) {
    if (this.isClosedOcc(occ)) {
      if (occ.actual_logged_seconds != null) {
        return `实际耗时 ${this.formatLoggedDuration(occ.actual_logged_seconds)}`;
      }
      return occ.estimated_minutes ? `预估耗时 ${occ.estimated_minutes}m` : '';
    }
    const parts = [];
    if (occ.actual_minutes != null) parts.push(`实际耗时 ${occ.actual_minutes}m`);
    if (occ.estimated_minutes) parts.push(`预估耗时 ${occ.estimated_minutes}m`);
    return parts.join(' · ');
  },

  durationDetailRows(occ) {
    if (this.isClosedOcc(occ)) {
      if (occ.actual_logged_seconds != null) {
        return '<div class="kv"><span class="k">实际耗时</span><span class="v">'
          + `${this.formatLoggedDuration(occ.actual_logged_seconds)}</span></div>`;
      }
      return '<div class="kv"><span class="k">预估耗时</span><span class="v">'
        + `${occ.estimated_minutes ? occ.estimated_minutes + 'm' : '-'}</span></div>`;
    }
    return '<div class="kv"><span class="k">实际耗时</span><span class="v">'
      + `${occ.actual_minutes != null ? occ.actual_minutes + 'm' : '-'}</span></div>`
      + '<div class="kv"><span class="k">预估耗时</span><span class="v">'
      + `${occ.estimated_minutes ? occ.estimated_minutes + 'm' : '-'}</span></div>`;
  },

  itemBadges(occ) {
    const badges = [statusTag(occ.status)];
    if (occ.schedule_label && occ.schedule_label !== '正常') {
      badges.push(tag(esc(occ.schedule_label), occ.schedule_label === '超时' ? 'red' : 'amber'));
    }
    if (occ.is_fixed) badges.push(tag('固定', 'slate'));
    if (occ.phase === 'start') badges.push(tag('开始阶段', 'plum'));
    if (occ.phase === 'end') badges.push(tag('结束阶段', 'plum'));
    if (this.conflictOccIds?.has(occ.id)) badges.push(tag('排程冲突', 'red'));
    if (occ.source === 'early') badges.push(tag('提前完成', 'muted'));
    return badges.join('');
  },

  itemHtml(occ, { draggable = false, closed = false, idle = false } = {}) {
    return `
      <div class="plan-item ${closed ? 'is-closed' : ''} ${idle ? 'is-idle' : ''}"
           data-occ="${occ.id}" role="button" tabindex="0">
        ${draggable ? `<span class="plan-handle" aria-hidden="true">${icon('menu')}</span>` : ''}
        <div class="plan-item-main">
          <div class="plan-item-title">${esc(occ.content)}</div>
          <div class="plan-item-meta"><span>${this.itemMeta(occ)}</span></div>
        </div>
        <div class="plan-item-side"><div class="tag-row">${this.itemBadges(occ)}</div></div>
      </div>`;
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
    if (this.reorderMode) return;
    if (this.activeTab !== 'today') {
      // BUG-16：排列模式只在「当前待办」页签内可用
      toast('调整顺序只在「当前待办」页签可用');
      return;
    }
    const items = this.board?.progress || [];
    if (items.length < 2) {
      toast('至少两个待办才能调整顺序');
      return;
    }
    if (this.activeSection !== 'progress') this.switchSection('progress');  // 可拖拽列表在「进度中」
    this.reorderMode = true;
    this.root.querySelector('#planning-reorder-bar').style.display = '';
    this.root.querySelector('#planning-reorder-btn').disabled = true;
    this.renderBoard();
  },

  exitReorder() {
    this.reorderMode = false;
    if (!this.root) return;
    this.root.querySelector('#planning-reorder-bar').style.display = 'none';
    const btn = this.root.querySelector('#planning-reorder-btn');
    if (btn) btn.disabled = false;
  },

  async confirmReorder() {
    const ids = [...this.progressList.querySelectorAll('.plan-item')]
      .map((el) => Number(el.dataset.occ)).filter(Boolean);
    try {
      await gw('/admin/api/planning/reorder', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ order: ids }),
      });
      // 自动重算关闭时不得提示「等待自动重算」（需求 16.3）：按配置给准确文案
      toast(this.board?.recompute?.enabled === false
        ? '顺序已保存；自动重算已关闭，时间未重算，可随时手动「重新计算时间」'
        : '顺序已保存，等待自动重算');
      this.exitReorder();
      await this.loadToday();
    } catch (error) {
      if (String(error.message).includes('order must include every open occurrence')) {
        // 排列期间后台新生成了实例（如拆分 / 规则变更），确认被拒：
        // 刷新列表退出排列，让用户基于最新列表重新进入（BUG-10）
        toast('待办列表有变化，请重新进入排列');
        this.exitReorder();
        await this.loadToday();
        return;
      }
      toast(`保存顺序失败：${error.message}`, 'err');
    }
  },

  async cancelReorder() {
    this.exitReorder();
    await this.loadToday();
    toast('已撤销本次排列');
  },

  attachDragHandlers() {
    if (!this.progressList) return;
    this.progressList.classList.add('reordering');
    this.progressList.querySelectorAll('.plan-item').forEach((el) => {
      el.addEventListener('pointerdown', this.onDragStart = (e) => this.startDrag(e, el));
    });
  },

  startDrag(e, item) {
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    e.preventDefault();
    const list = this.progressList;
    item.classList.add('is-dragging');
    item.setPointerCapture(e.pointerId);
    let anchorY = e.clientY;

    const onMove = (ev) => {
      item.style.transform = `translate(0, ${ev.clientY - anchorY}px)`;
      const rect = item.getBoundingClientRect();
      const mid = rect.top + rect.height / 2;
      for (const sib of [...list.children]) {
        if (sib === item) continue;
        const sr = sib.getBoundingClientRect();
        const sibMid = sr.top + sr.height / 2;
        const itemAfterSib = sib.compareDocumentPosition(item) & Node.DOCUMENT_POSITION_PRECEDING;
        if (mid < sibMid && !itemAfterSib) {
          list.insertBefore(item, sib);
          anchorY = ev.clientY;
          item.style.transform = '';
          break;
        }
        if (mid > sibMid && itemAfterSib) {
          list.insertBefore(item, sib.nextSibling);
          anchorY = ev.clientY;
          item.style.transform = '';
          break;
        }
      }
    };
    const onEnd = () => {
      item.classList.remove('is-dragging');
      item.style.transform = '';
      item.removeEventListener('pointermove', onMove);
      item.removeEventListener('pointerup', onEnd);
      item.removeEventListener('pointercancel', onEnd);
    };
    item.addEventListener('pointermove', onMove);
    item.addEventListener('pointerup', onEnd);
    item.addEventListener('pointercancel', onEnd);
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
    const { root, close } = modal({
      title: '完成待办',
      body: `
        <div class="field">
          <label>实际耗时（可留空）</label>
          <input type="text" data-actual-duration placeholder="如 45、1h30m、1h1m1s">
          <p class="muted text-sm" style="margin:4px 0 0">无后缀按分钟计，可组合时/分/秒；留空则不记录手填耗时。</p>
        </div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>完成</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const submit = root.querySelector('[data-ok]');
      if (submit.disabled) return;
      submit.disabled = true;
      const text = root.querySelector('[data-actual-duration]').value.trim();
      const body = {};
      if (text) body.actual_logged_duration = text;
      try {
        await post('/finish', body);
        close();
        toast('已完成');
        await Promise.all([this.loadToday(), this.loadOccurrences()]);
      } catch (error) {
        submit.disabled = false;
        toast(`操作失败：${error.message}`, 'err');
      }
    };
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
    const { root, close } = modal({
      title: '部分完成',
      body: `<div class="field"><label>完成了哪些部分（会保存为说明）</label>
             <textarea id="planning-partial-note" rows="3" placeholder="例如：背完了前 20 页"></textarea></div>
             <div class="field muted text-sm">记录后待办保持开放，之后可点「已全部完成」收口。</div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>保存</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const note = root.querySelector('#planning-partial-note').value.trim();
      if (!note) { toast('请填写完成说明', 'err'); return; }
      try {
        await gw(`/admin/api/planning/occurrences/${id}/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: 'partial', partial_note: note }),
        });
        close();
        toast('已记录部分完成；待办保持开放，可继续处理');
        await this.loadToday();
        await this.loadOccurrences();
      } catch (error) {
        toast(`操作失败：${error.message}`, 'err');
      }
    };
  },

  async askRescheduleTimeout(id) {
    // B3/N2：同一次重排操作的幂等键在首次提交时生成；失败后不修改时间
    // 再次提交复用同一键（后端幂等收敛）；用户修改执行时间即视为新的
    // 请求，改用新键提交，不得拿旧键 + 新时间静默拿回旧结果
    let idempotencyKey = null;
    let lastSubmittedTime = null;
    const { root, close } = modal({
      title: '重新安排执行时间',
      body: `<div class="field muted text-sm">重新安排当前待办的执行时间，已有进度会保留；原超时记录保留。</div>
             <div class="field"><label>新的执行时间</label>
             <input type="datetime-local" id="planning-reschedule-time"></div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>保存</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const value = root.querySelector('#planning-reschedule-time').value;
      if (!value) { toast('请选择时间', 'err'); return; }
      if (idempotencyKey === null || lastSubmittedTime !== value) {
        idempotencyKey = crypto.randomUUID();
        lastSubmittedTime = value;
      }
      try {
        await gw(`/admin/api/planning/occurrences/${id}/reschedule-timeout`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
          body: JSON.stringify({ est_start: new Date(value).toISOString() }),
        });
        close();
        toast('已保存新的执行时间；原超时记录保留');
        await this.loadToday();
        await this.loadOccurrences();
      } catch (error) {
        toast(`操作失败：${error.message}`, 'err');
      }
    };
  },

  askNewTime(id, targetStatus, title) {
    const { root, close } = modal({
      title,
      body: `<div class="field"><label>新的执行时间</label>
             <input type="datetime-local" id="planning-new-time"></div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>确认</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const value = root.querySelector('#planning-new-time').value;
      if (!value) { toast('请选择时间', 'err'); return; }
      try {
        await gw(`/admin/api/planning/occurrences/${id}/status`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ status: targetStatus, est_start: new Date(value).toISOString() }),
        });
        close();
        toast(targetStatus === 'deferred' ? '已延后' : '已重新安排');
        await this.loadToday();
        await this.loadOccurrences();
      } catch (error) {
        toast(`操作失败：${error.message}`, 'err');
      }
    };
  },

  askEditTime(id) {
    // 调整时段（批次 8）：编辑当前实例的冻结窗口约束（最早开始 / 最晚完成），
    // 不是编辑预估排程结果——收窄到恰好容纳耗时即钉住该时间；只有尚未开始
    // 且开放的实例允许（后端 422/409 中文拒绝时在字段附近呈现并保持可改）。
    const occ = this.findOccurrence(id)
      || this.occurrences.find((o) => o.id === id)
      || {};
    const toLocal = (iso) => {
      if (!iso) return '';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    const hasWindow = !!(occ.window_start_at || occ.window_end_at);
    // §28.3（2026-10-01）：无日期单次常驻显示、不设时间窗口——当前实例
    // 编辑不能为它新增窗口端（后端权威拒绝，这里同步禁用输入并说明）。
    const occTask = this.tasks.find((t) => t.id === occ.task_id);
    const residentOnce = !!occTask && occTask.task_type === 'once' && !occTask.target_date;
    const disabledAttr = residentOnce ? 'disabled' : '';
    const { root, close } = modal({
      title: '调整时段',
      body: `
        <p class="muted text-sm">调整当前这一轮的可安排时段（最早开始 / 最晚完成，两端可独立留空）。把时段收窄到恰好容纳预计耗时，就会把这条待办钉在该时间，不再被自动重算移动。</p>
        <div class="field"><label>最早开始（可选）</label><input type="datetime-local" id="planning-adj-window-start" value="${toLocal(occ.window_start_at)}" ${disabledAttr}></div>
        <div class="field"><label>最晚完成（可选，越过即超时）</label><input type="datetime-local" id="planning-adj-window-end" value="${toLocal(occ.window_end_at)}" ${disabledAttr}></div>
        ${residentOnce ? '<p class="muted text-sm">未指定日期的单次待办常驻显示、不设可安排时段：不能为它的当前实例新增时间窗口。</p>' : ''}
        ${hasWindow ? '<p class="muted text-sm">这一轮已带时段约束：两端都清空会取消既有约束，后端会拒绝；请保留至少一端。</p>' : ''}
        <div id="pf-adj-error" hidden></div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>保存</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const start = root.querySelector('#planning-adj-window-start').value;
      const end = root.querySelector('#planning-adj-window-end').value;
      try {
        await gw(`/admin/api/planning/occurrences/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            window_start_at: start ? new Date(start).toISOString() : null,
            window_end_at: end ? new Date(end).toISOString() : null,
          }),
        });
        close();
        toast('时段已更新；待办时间将按新时段重新安排');
        await this.loadToday();
        await this.loadOccurrences();
      } catch (error) {
        const message = String(error.message || '');
        const area = root.querySelector('#pf-adj-error');
        if (area) {
          area.hidden = false;
          area.innerHTML = errorBlock(esc(message));
          area.scrollIntoView({ block: 'nearest' });
        }
        toast(`保存失败：${error.message}`, 'err');
      }
    };
  },

  askBackfill(id) {
    const occ = this.findOccurrence(id) || {};
    const toLocal = (iso) => {
      if (!iso) return '';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    const { root, close } = modal({
      title: '补填实际时间',
      body: `
        <div class="field"><label>实际开始</label><input type="datetime-local" id="planning-backfill-start" value="${toLocal(occ.actual_start)}"></div>
        <div class="field"><label>实际结束</label><input type="datetime-local" id="planning-backfill-end" value="${toLocal(occ.actual_end)}"></div>
        <p class="muted text-sm">留空即清除该时间；同时有起止时自动计算实际耗时，预估耗时独立保留。</p>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>保存</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const start = root.querySelector('#planning-backfill-start').value;
      const end = root.querySelector('#planning-backfill-end').value;
      // 始终提交两个字段：留空 → null 即清除（BUG-6），actual_minutes 随之清空
      const body = {
        actual_start: start ? new Date(start).toISOString() : null,
        actual_end: end ? new Date(end).toISOString() : null,
      };
      try {
        await gw(`/admin/api/planning/occurrences/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        close();
        toast('实际时间已更新');
        await this.loadToday();
      } catch (error) {
        toast(`保存失败：${error.message}`, 'err');
      }
    };
  },

  askSplit(id) {
    // 拆分 = 结束当前轮 + 创建 1～10 个新的单次待办（可选辅助功能，
    // partial → 已全部完成才是主流程）。默认只显示 1 个输入区域，用户
    // 点「添加待办」逐个增加，最多 10 个；至少保留 1 个，不允许删到 0。
    const partRow = (index) => `
      <div class="field" data-part-row>
        <label>待办 ${index}</label>
        <input type="text" data-part-content placeholder="内容">
        <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
          <label style="white-space:nowrap">耗时</label>
          <input type="text" data-part-minutes value="30" placeholder="分钟或 1h30m" style="width:130px">
          <button type="button" class="btn btn-quiet btn-sm" data-remove-part title="移除这一项">${icon('x')}移除</button>
        </div>
      </div>`;
    const { root, close } = modal({
      title: '拆分待办',
      body: `
        <p class="muted text-sm">结束当前这一轮，并把剩余工作拆成新的单次待办（今天执行）；原条目按「此次不执行」留痕，已有进度保留。</p>
        <div id="planning-split-parts">
          ${partRow(1)}
        </div>
        <button type="button" class="btn btn-quiet btn-sm" data-add-part>${icon('plus')}添加待办</button>
        <p class="muted text-sm" style="margin-top:6px">最多 10 个；只填 1 个也可以提交。</p>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>拆分</button>`,
    });
    const addBtn = root.querySelector('[data-add-part]');
    const syncRows = () => {
      const rows = root.querySelectorAll('[data-part-row]');
      rows.forEach((rowEl, index) => {
        rowEl.querySelector('label').textContent = `待办 ${index + 1}`;
        rowEl.querySelector('[data-remove-part]').style.display =
          rows.length > 1 ? '' : 'none';
      });
      addBtn.style.display = rows.length >= 10 ? 'none' : '';
    };
    addBtn.onclick = () => {
      const host = root.querySelector('#planning-split-parts');
      if (host.querySelectorAll('[data-part-row]').length >= 10) return;
      host.insertAdjacentHTML('beforeend', partRow(host.querySelectorAll('[data-part-row]').length + 1));
      syncRows();
    };
    root.querySelector('#planning-split-parts').addEventListener('click', (event) => {
      const remove = event.target.closest('[data-remove-part]');
      if (!remove) return;
      const host = root.querySelector('#planning-split-parts');
      if (host.querySelectorAll('[data-part-row]').length <= 1) return;
      remove.closest('[data-part-row]').remove();
      syncRows();
    });
    syncRows();
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const parts = [...root.querySelectorAll('[data-part-row]')]
        .map((rowEl) => ({
          content: rowEl.querySelector('[data-part-content]').value.trim(),
          estimated_minutes: rowEl.querySelector('[data-part-minutes]').value.trim() || '30',
        }))
        .filter((part) => part.content);
      if (parts.length < 1) { toast('请至少填写一个待办内容', 'err'); return; }
      // 防双击：请求进行中禁用提交按钮；失败恢复以便重试（后端仍有
      // 「已关闭实例拒绝再次拆分」的业务兜底）
      const submit = root.querySelector('[data-ok]');
      if (submit.disabled) return;
      submit.disabled = true;
      try {
        await gw(`/admin/api/planning/occurrences/${id}/split`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ parts }),
        });
        close();
        toast('已拆分：原待办按「此次不执行」收口，新待办已创建');
        await Promise.all([this.loadToday(), this.loadTasks(), this.loadOccurrences()]);
      } catch (error) {
        submit.disabled = false;
        toast(`拆分失败：${error.message}`, 'err');
      }
    };
  },

  askRemaining(occ) {
    const source = typeof occ === 'object' ? occ : this.findOccurrence(occ);
    if (!source) return;
    this.openTaskForm({
      content: `${source.content}（剩余部分）`,
      task_type: 'once',
      estimated_minutes: source.estimated_minutes || 30,
      target_date: source.display_cycle_date || source.schedule_date || source.for_date,
    });
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
    const editing = !!task?.id;
    // once 已生成当前实例 → 任务级排程身份锁定（§28.3）：目标日期与未来
    // 窗口模板禁用并提示走当前实例调整；后端 400 仍是权威兜底。
    // 批次 9 UI 修复：优先用后端随任务列表返回的 has_generated_occurrence
    // （不依赖 occurrences 列表的加载状态与过滤条件），实例列表仅作兜底。
    const onceLocked = editing && task.task_type === 'once'
      && (task.has_generated_occurrence
        || this.occurrences.some((o) => o.task_id === task.id));
    const value = (field, fallback = '') => (task ? (task[field] ?? fallback) : fallback);
    const typeSelectOptions = TASK_TYPES.map((t) => ({ value: t, label: TASK_TYPE_LABELS[t] }));
    const weekdayChecks = WEEKDAY_NAMES.map((name, index) => `
      <label class="inline"><input type="checkbox" data-weekday value="${index}"
        ${(value('weekdays') || []).includes(index) ? 'checked' : ''}>周${name}</label>`).join('');
    const { root, close } = modal({
      title: editing ? '编辑待办' : '新建待办',
      wide: true,
      body: `
        <div class="field"><label>内容</label>
          <input type="text" id="pf-content" value="${esc(value('content'))}" placeholder="例如：背单词"></div>
        <div class="field"><label>类型</label>
          <div class="retro-select" data-retro-select="pf-type" data-retro-value="${esc(value('task_type', 'daily'))}"></div></div>
        <div data-type-block="interval" style="display:none">
          <div class="field"><label>刷新方式</label>
            <div class="tag-row">
              <label class="inline"><input type="radio" name="pf-refresh-mode" value="after_completion" ${(value('refresh_mode', 'after_completion')) === 'after_completion' ? 'checked' : ''}> 处理后刷新（完成后起算）</label>
              <label class="inline"><input type="radio" name="pf-refresh-mode" value="fixed_interval" ${value('refresh_mode') === 'fixed_interval' ? 'checked' : ''}> 固定间隔（固定时间轴）</label>
            </div>
          </div>
          <div class="field"><label>间隔天数（1-3650）</label>
            <input type="number" id="pf-interval-days" min="1" max="3650" value="${esc(value('interval_days', 1))}"></div>
        </div>
        <div data-type-block="weekly" style="display:none">
          <div class="field"><label>每周几出现（可多选）</label><div class="tag-row">${weekdayChecks}</div></div>
        </div>
        <div data-type-block="monthly" style="display:none">
          <div class="field"><label>每月几号出现（逗号分隔，如 1,15；当月没有则跳过）</label>
            <input type="text" id="pf-month-days" value="${esc((value('month_days') || []).join(','))}"></div>
        </div>
        <div data-type-block="once" style="display:none">
          <div class="field"><label>目标日期（可选）</label>
            <div class="retro-time" data-retro-for="pf-target-date" data-retro-mode="date" data-retro-value="${esc(value('target_date'))}"></div>
            <p class="muted text-sm" id="pf-resident-note" hidden>未填日期：单次待办常驻显示，不设最早开始／最晚完成，直到你主动处理。</p></div>
          <div id="pf-once-error" hidden></div>
        </div>
        <div class="field"><label>预估耗时（分钟，或 1h30m 简写）</label>
          <input type="text" id="pf-estimated" value="${esc(value('estimated_minutes', ''))}"></div>
        <div class="field"><label>可安排时段</label>
          <div class="window-fields">
            <div class="window-field"><label>最早开始（可选）</label>
              <div class="retro-time" data-retro-for="pf-window-start" data-retro-mode="time" data-retro-align="right" data-retro-value="${esc(value('window_start_tod'))}"></div>
            </div>
            <div class="window-field"><label>最晚完成（可选）</label>
              <div class="retro-time" data-retro-for="pf-window-end" data-retro-mode="time" data-retro-align="right" data-retro-value="${esc(value('window_end_tod'))}"></div>
            </div>
          </div>
          <button type="button" id="pf-clear-window" class="btn btn-secondary btn-sm" hidden>清空残留时段</button>
        </div>
        <div id="pf-window-error" hidden></div>
        <div class="field"><label class="inline"><input type="checkbox" id="pf-hollow" ${value('is_hollow') ? 'checked' : ''}> 中空待办（开始/结束两个条目，中间可插入其他待办）</label></div>
        <div id="pf-hollow-block" style="display:none">
          <div class="field"><label>开始阶段内容</label><input type="text" id="pf-hollow-start" value="${esc(value('hollow_start_content'))}"></div>
          <div class="field"><label>开始阶段耗时（分钟）</label><input type="number" id="pf-hollow-start-min" min="1" value="${esc(value('hollow_start_minutes', ''))}"></div>
          <div class="field"><label>中间等待时长（分钟）</label><input type="number" id="pf-hollow-wait" min="1" value="${esc(value('hollow_wait_minutes', ''))}"></div>
          <div class="field"><label>中间说明（可选）</label><input type="text" id="pf-hollow-note" value="${esc(value('hollow_wait_note'))}"></div>
          <div class="field"><label>结束阶段内容</label><input type="text" id="pf-hollow-end" value="${esc(value('hollow_end_content'))}"></div>
          <div class="field"><label>结束阶段耗时（分钟）</label><input type="number" id="pf-hollow-end-min" min="1" value="${esc(value('hollow_end_minutes', ''))}"></div>
        </div>
        ${editing ? `<div class="field muted text-sm">修改规则只影响以后生成的轮次，当前已经生成的待办保持不变；提醒（闹钟/计时器）在待办详情栏设置。</div>
        <div class="field"><label class="inline"><input type="checkbox" id="pf-active" ${value('is_active') ? 'checked' : ''}> 启用中</label></div>` : ''}`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>${editing ? '保存' : '创建'}</button>`,
    });

    this.initRetroFields(root, { 'pf-type': typeSelectOptions });  // BUG-14：表单内日期/时刻/类型统一为复古选择器（值契约不变）
    // 复古下拉挂载后 #pf-type 才是隐藏 input，取值必须在 initRetroFields 之后
    const typeSelect = root.querySelector('#pf-type');
    if (onceLocked) {
      // 批次 9 UI 修复：复古选择器是 hidden input + 按钮——只 disable
      // input 拦不住按钮弹层改值；按钮与 input 一起禁用才算真正锁死。
      const lockRetro = (hostSelector) => {
        const host = root.querySelector(hostSelector);
        if (!host) return;
        const input = host.querySelector('input');
        const button = host.querySelector('button');
        if (input) input.disabled = true;
        if (button) {
          button.disabled = true;
          button.title = '该单次待办已生成当前实例：请在该待办详情栏使用「调整时段」';
        }
      };
      lockRetro('.retro-time[data-retro-for="pf-target-date"]');
      lockRetro('.retro-time[data-retro-for="pf-window-start"]');
      lockRetro('.retro-time[data-retro-for="pf-window-end"]');
      const lockNote = root.querySelector('[data-type-block="once"]');
      if (lockNote) lockNote.insertAdjacentHTML('beforeend',
        '<p class="muted text-sm">该单次待办已生成当前实例：任务日期与未来窗口模板已锁定，调整这一次请在该待办详情栏使用「调整时段」。</p>');
    }
    const syncBlocks = () => {
      const type = typeSelect.value;
      root.querySelectorAll('[data-type-block]').forEach((block) => {
        block.style.display = block.dataset.typeBlock === type ? '' : 'none';
      });
      root.querySelector('#pf-hollow-block').style.display =
        root.querySelector('#pf-hollow').checked ? '' : 'none';
      syncOnceWindow();
    };
    // §30.6（2026-10-01）：单次目标日期可选；未填日期时最早开始／最晚完成
    // 控件不可设置，并说明常驻语义（前后端都拒绝空日期 + 非空窗口组合）。
    const residentNote = root.querySelector('#pf-resident-note');
    const toggleRetro = (hostSelector, disabled, title) => {
      const host = root.querySelector(hostSelector);
      if (!host) return;
      const input = host.querySelector('input');
      const button = host.querySelector('button');
      if (input) input.disabled = disabled;
      if (button) {
        button.disabled = disabled;
        button.title = title;
      }
    };
    const syncOnceWindow = () => {
      if (!residentNote) return;
      // §28.3 身份锁定优先：已生成 once 的日期与窗口模板已禁用并提示，
      // 常驻联动不得重新启用（lockRetro 的禁用状态保持权威）。
      if (onceLocked) return;
      const type = typeSelect.value;
      const dateValue = root.querySelector('#pf-target-date')?.value || '';
      const resident = type === 'once' && !dateValue;
      const startInput = root.querySelector('#pf-window-start');
      const endInput = root.querySelector('#pf-window-end');
      // R5 审查修复：空日期禁止「新增」窗口不变，但残留值必须留一条
      // 明确可用的清空通路——禁用按钮同时拦住了进入选择器点「清除」，
      // 残值既删不掉也提交不了。清空按钮仅在存在残值时可见，点击即
      // user 明确确认（不默默丢弃、不提交隐藏残值）。
      const residual = resident && !!((startInput?.value) || (endInput?.value));
      residentNote.hidden = !resident;
      residentNote.textContent = residual
        ? '未填日期：单次待办常驻显示，不设最早开始／最晚完成。当前仍有残留时段值——请点击「清空残留时段」明确清除后再保存。'
        : '未填日期：单次待办常驻显示，不设最早开始／最晚完成，直到你主动处理。';
      const residentTitle = resident
        ? '无日期单次常驻显示，不能设置可安排时段' : '';
      toggleRetro('.retro-time[data-retro-for="pf-window-start"]', resident, residentTitle);
      toggleRetro('.retro-time[data-retro-for="pf-window-end"]', resident, residentTitle);
      const clearBtn = root.querySelector('#pf-clear-window');
      if (clearBtn) clearBtn.hidden = !residual;
    };
    root.querySelector('#pf-clear-window')?.addEventListener('click', () => {
      // user 明确清除残留时段：经复古选择器的编程清空接口（缺省回退直写
      // value），两端一起清；清空后重新联动（隐藏按钮、更新提示）。
      for (const selector of ['#pf-window-start', '#pf-window-end']) {
        const input = root.querySelector(selector);
        if (input?.value) {
          if (typeof input._applyRetroValue === 'function') input._applyRetroValue('', true);
          else input.value = '';
        }
      }
      syncOnceWindow();
    });
    typeSelect.addEventListener('change', syncBlocks);
    root.querySelector('#pf-hollow').addEventListener('change', syncBlocks);
    root.querySelector('#pf-target-date')?.addEventListener('input', syncOnceWindow);
    syncBlocks();

    root.querySelector('[data-cancel]').onclick = close;
    // 防重复提交（新建与编辑同一入口）：提交锁在 handler 入口同步建立，
    // 早于任何异步请求，不能只依赖按钮 disabled（按钮聚焦后按 Enter /
    // 空格仍会触发 click）。两阶段语义：
    // 提交/API 阶段失败 → 释放锁与按钮，user 可修改后重新提交；
    // 服务器保存成功 → committed 终态：此后 toast/close/loadAll 等 UI
    // 后处理无论成败，本表单都不再解锁、不再发出第二次保存请求，也
    // 不得把已成功的事实误报为「保存失败」。即使弹窗因异常未被移除，
    // 提交按钮保持禁用，重复点击也不会再发请求；关闭失败时可经取消 /
    // 右上角关闭按钮收尾。
    const submitBtn = root.querySelector('[data-ok]');
    let submitting = false;
    let committed = false;
    // 创建响应（first_round_skipped / schedule_conflict）必须声明在本
    // handler 作用域——提交 try 块内的声明在块外读取会抛 ReferenceError
    // 且被外层 catch 吞掉（R4 审查修复），两种新增提示都会失效。
    let createdTask = null;
    submitBtn.onclick = async () => {
      if (committed || submitting) return;
      submitting = true;
      submitBtn.disabled = true;
      createdTask = null;
      try {
        const type = typeSelect.value;
        const body = {
          content: root.querySelector('#pf-content').value.trim(),
          task_type: type,
        };
        const estimated = root.querySelector('#pf-estimated').value.trim();
        if (estimated) body.estimated_minutes = estimated;
        // 可安排时段（排程约束，非排程结果）：四种组合均可表达。
        // 编辑模式显式发送双端清除值（null = 清除该端），不发送=没清除；
        // 创建模式只提交已填端（缺省=不约束）。
        const windowStart = root.querySelector('#pf-window-start').value;
        const windowEnd = root.querySelector('#pf-window-end').value;
        if (editing) {
          body.window_start_tod = windowStart || null;
          body.window_end_tod = windowEnd || null;
        } else {
          if (windowStart) body.window_start_tod = windowStart;
          if (windowEnd) body.window_end_tod = windowEnd;
        }
        if (type === 'interval') {
          body.interval_days = Number(root.querySelector('#pf-interval-days').value) || null;
          const refreshMode = root.querySelector('input[name="pf-refresh-mode"]:checked');
          if (refreshMode) body.refresh_mode = refreshMode.value;
        }
        if (type === 'weekly') {
          body.weekdays = [...root.querySelectorAll('[data-weekday]:checked')].map((el) => Number(el.value));
        }
        if (type === 'monthly') {
          body.month_days = root.querySelector('#pf-month-days').value
            .split(/[,，\s]+/).map((v) => Number(v)).filter((v) => Number.isInteger(v) && v > 0);
        }
        if (type === 'once') body.target_date = root.querySelector('#pf-target-date').value || null;
        if (type === 'once' && !body.target_date
            && (body.window_start_tod || body.window_end_tod)) {
          // 前端先行校验（§30.6）：空日期与非空窗口不能同时保存——切换类型
          // 时禁用控件的残留值也拦在这里；后端仍权威复核（零写入 400）。
          throw new Error('未填目标日期的单次待办不能设置可安排时段：无日期单次常驻显示，不设最早开始或最晚完成');
        }
        if (editing && task.task_type === 'once' && task.has_generated_occurrence) {
          // once 身份锁定的提交侧兜底（§28.3）：无论控件状态如何，target_date
          // / 未来窗口模板一律回传任务现值（幂等请求放行、实际变化后端拒绝）
          body.target_date = task.target_date ?? null;
          body.window_start_tod = task.window_start_tod ?? null;
          body.window_end_tod = task.window_end_tod ?? null;
        }
        if (root.querySelector('#pf-hollow').checked) {
          body.is_hollow = true;
          body.hollow_start_content = root.querySelector('#pf-hollow-start').value.trim() || body.content;
          body.hollow_start_minutes = Number(root.querySelector('#pf-hollow-start-min').value) || null;
          body.hollow_wait_minutes = Number(root.querySelector('#pf-hollow-wait').value) || null;
          body.hollow_wait_note = root.querySelector('#pf-hollow-note').value.trim() || null;
          body.hollow_end_content = root.querySelector('#pf-hollow-end').value.trim() || body.content;
          body.hollow_end_minutes = Number(root.querySelector('#pf-hollow-end-min').value) || null;
        }
        if (editing) body.is_active = root.querySelector('#pf-active').checked;

        if (editing) {
          await gw(`/admin/api/planning/tasks/${task.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
        } else {
          createdTask = await gw('/admin/api/planning/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
        }
      } catch (error) {
        // 提交/API 阶段失败：先解锁恢复按钮再提示，提示自身异常不得
        // 卡死提交资格（user 仍可修改后重新提交）。错误按字段就近呈现：
        // 目标日期类错误进 once 区，可安排时段及其余错误进时段区。
        submitting = false;
        submitBtn.disabled = false;
        const message = String(error.message || '');
        const area = root.querySelector(
          message.includes('目标日期') || message.includes('单次待办已生成')
            ? '#pf-once-error' : '#pf-window-error');
        if (area) {
          area.hidden = false;
          area.innerHTML = errorBlock(esc(message));
          area.scrollIntoView({ block: 'nearest' });
        }
        toast(`保存失败：${error.message}`, 'err');
        return;
      }
      // 服务器已保存：进入不可逆终态。此后任何 UI 后处理异常都不得
      // 重新赋予本表单提交资格，也不得误报「保存失败」。
      committed = true;
      submitting = false;
      // 后处理逐项 best-effort：一步失败只影响该步，后续步骤照常执行
      try {
        toast(editing ? '待办已保存' : '待办已创建');
        // §30.6 / §18.1（2026-10-01）：创建允许与排程可行性分离——区分
        // 「本轮已截止、次日起生效」与「已创建但存在排程冲突」，两者都
        // 不改变任务已保存的事实。
        if (createdTask?.first_round_skipped) {
          toast('本轮已过最晚完成，从次日起按重复规则生效');
        } else if (createdTask?.schedule_conflict) {
          toast('待办已创建，但可安排时段剩余空间不足，存在排程冲突');
        }
      } catch { /* 不误报失败 */ }
      try { close(); } catch { /* 旧表单保持终态（按钮已禁用 + committed 拦截） */ }
      try { await this.loadAll(); } catch { /* 刷新失败不改变已保存事实 */ }
    };
  },

  /* ---------- 闹钟 / 计时器 ---------- */

  requestNotificationPermission() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') {
      Notification.requestPermission().then((permission) => {
        if (permission === 'denied') toast('通知权限被拒，到点只在页面内响铃');
      });
    } else if (Notification.permission === 'denied' && !this.permissionNoticeShown) {
      this.permissionNoticeShown = true;
      toast('通知权限被拒，到点只在页面内响铃');
    }
  },

  ensureAudio() {
    if (!this.alarmAudio) {
      this.alarmAudio = new Audio(ALARM_URL);
      this.alarmAudio.loop = true;
    }
    if (!this.timerAudio) {
      this.timerAudio = new Audio(TIMER_URL);
    }
  },

  notify(title, body) {
    try {
      if ('Notification' in window && Notification.permission === 'granted') {
        new Notification(title, { body });
      }
    } catch { /* 通知失败不影响页面内响铃 */ }
  },

  checkAlarms(board) {
    const now = Date.now();
    for (const occ of board.progress) {
      if (occ.alarm_start && occ.est_start) {
        this.fireAlarm(occ, 'start', '待办开始', occ.content, occ.est_start, now);
      }
      if (occ.alarm_end && occ.est_end) {
        this.fireAlarm(occ, 'end', '待办时间到', occ.content, occ.est_end, now);
      }
      if (occ.timer_minutes && occ.status === 'in_progress' && occ.actual_start) {
        const due = new Date(occ.actual_start).getTime() + occ.timer_minutes * 60 * 1000;
        this.fireAlarm(occ, 'timer', '计时器时间到', occ.content, new Date(due).toISOString(), now, true);
      }
    }
  },

  fireAlarm(occ, kind, title, body, dueIso, nowMs, once = false) {
    const key = `${occ.id}:${kind}:${dueIso}`;
    if (this.firedKeys.has(key)) return;
    const due = new Date(dueIso).getTime();
    if (Number.isNaN(due)) return;
    if (nowMs - due > ALARM_GRACE_MS) {
      this.firedKeys.add(key);  // 错过太久，静默跳过
      return;
    }
    if (due > nowMs) return;  // 还没到点，等下次轮询
    this.firedKeys.add(key);
    this.notify(`${title}：${body}`, fmtClock(dueIso));
    this.showRingModal(`${title}：${body}`, fmtClock(dueIso), once);
  },

  showRingModal(message, timeText, once = false) {
    this.ensureAudio();
    this.stopRinging({ keepModal: true });
    const { root, close } = modal({
      title: '提醒',
      body: `<p class="confirm-text">${esc(message)}</p><p class="muted text-sm">${esc(timeText)}${once ? ' · 计时结束' : ' · 将循环响铃直到点掉'}</p>`,
      footer: `<button class="btn btn-danger" data-act-ring-stop>${icon('x')}停止响铃</button>`,
    });
    root.querySelector('[data-act-ring-stop]').onclick = () => {
      this.stopRinging();
      close();
    };
    this.ringModal = { root, close };
    if (once) {
      this.timerAudio.currentTime = 0;
      this.timerAudio.play().catch(() => toast('浏览器拦截了自动响铃，点一下页面即可恢复'));
    } else {
      this.alarmAudio.play().catch(() => toast('浏览器拦截了自动响铃，点一下页面即可恢复'));
    }
  },

  stopRinging({ keepModal = false } = {}) {
    this.ensureAudio();
    try { this.alarmAudio.pause(); } catch { /* ignore */ }
    try { this.timerAudio.pause(); } catch { /* ignore */ }
    this.alarmAudio.currentTime = 0;
    if (!keepModal && this.ringModal) {
      this.ringModal.close();
      this.ringModal = null;
    }
  },

  /* ---------- 事件委托（详情动作） ---------- */

  bindDetailActions() {
    this.root.addEventListener('click', (e) => {
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
