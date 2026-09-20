// pages/planning.js - 规划管理：四类型待办 + 时间排程 + 排列模式 + 浏览器闹钟/计时器
import { gw } from '../api.js?v=20260921-ctx1';
import {
  loading, empty, errorBlock, tag, toast, modal, confirm, delegate, icon, fmtDate, esc,
  createDetailPanel,
} from '../ui.js?v=20260921-ctx1';

const TASK_TYPE_LABELS = {
  daily: '每日', interval: '间歇', weekly: '每周', monthly: '每月', once: '单次', idle: '闲时',
};
const TASK_TYPES = Object.keys(TASK_TYPE_LABELS);
const WEEKDAY_NAMES = ['一', '二', '三', '四', '五', '六', '日'];
const STATUS_META = {
  pending: { label: '待处理', tone: 'muted' },
  in_progress: { label: '进行中', tone: 'amber' },
  completed: { label: '已完成', tone: 'green' },
  partial: { label: '部分完成', tone: 'gold' },
  deferred: { label: '已延后', tone: 'slate' },
  discarded_this: { label: '此次废弃', tone: 'muted' },
  discarded: { label: '已废弃', tone: 'red' },
  timeout: { label: '已超时', tone: 'red' },
};
const CLOSED_STATUSES = ['completed', 'partial', 'discarded_this', 'discarded'];
const OPEN_STATUSES = ['pending', 'in_progress', 'deferred'];
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

export default {
  board: null,
  tasks: [],
  occurrences: [],
  filters: { task_type: '', status: '', for_date: '' },
  detail: null,
  selected: null,
  reorderMode: false,
  pollTimer: null,
  alarmAudio: null,
  timerAudio: null,
  ringModal: null,
  firedKeys: new Set(),
  permissionNoticeShown: false,

  async mount(root) {
    this.root = root;
    root.innerHTML = `
      <div class="page-with-detail" id="planning-layout">
        <div class="page-main">
          <div class="toolbar" style="margin-bottom:14px">
            <button class="btn btn-primary" data-act="new-task">${icon('plus')}新建待办</button>
            <button class="btn btn-secondary" data-act="recompute">${icon('refresh')}重新计算时间</button>
            <button class="btn btn-secondary" data-act="enter-reorder" id="planning-reorder-btn">${icon('sort')}调整顺序</button>
            <span class="grow"></span>
            <button class="btn btn-secondary" data-act="refresh">${icon('refresh')}刷新</button>
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
          <div id="planning-alarm-banner"></div>

          <div class="plan-region" id="planning-today">
            <div class="card">
              <div class="plan-region-head">
                <div class="plan-region-title">${icon('calendar')}当前待办</div>
                <span class="plan-region-sub">只显示今天的待办；已完成的记录可改状态、补时间</span>
              </div>
              <div class="plan-group-title">进度中 <span class="plan-count" id="planning-progress-count"></span></div>
              <div class="plan-list" id="planning-progress">${loading()}</div>
              <div class="plan-group-title">待处理 <span class="plan-count" id="planning-attention-count"></span></div>
              <div class="plan-list" id="planning-attention"></div>
              <div class="plan-group-title">已完成 <span class="plan-count" id="planning-done-count"></span></div>
              <div class="plan-list" id="planning-done"></div>
            </div>
          </div>

          <div class="plan-region" id="planning-all">
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
                <label class="inline">日期 <input type="date" id="planning-filter-date" style="width:auto"></label>
                <button class="btn btn-quiet btn-sm" data-act="clear-filters">清除筛选</button>
              </div>
              <div class="plan-group-title">任务定义 <span class="plan-count" id="planning-tasks-count"></span></div>
              <div id="planning-tasks">${loading()}</div>
              <div class="plan-group-title">出现记录 <span class="plan-count" id="planning-occ-count"></span></div>
              <div id="planning-occurrences">${loading()}</div>
            </div>
          </div>

          <div class="plan-region" id="planning-goals">
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

          <div class="plan-region" id="planning-summary">
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

    delegate(root, {
      'new-task': () => this.openTaskForm(null),
      recompute: () => this.runRecompute(),
      refresh: () => this.loadAll(),
      'enter-reorder': () => this.enterReorder(),
      'confirm-reorder': () => this.confirmReorder(),
      'cancel-reorder': () => this.cancelReorder(),
      'clear-filters': () => this.clearFilters(),
      stop: () => this.stopRinging(),
    });
    root.querySelector('#planning-filter-type').addEventListener('change', (e) => {
      this.filters.task_type = e.target.value;
      this.loadOccurrences();
    });
    root.querySelector('#planning-filter-status').addEventListener('change', (e) => {
      this.filters.status = e.target.value;
      this.loadOccurrences();
    });
    root.querySelector('#planning-filter-date').addEventListener('change', (e) => {
      this.filters.for_date = e.target.value;
      this.loadOccurrences();
    });

    this.requestNotificationPermission();
    // 首次用户手势时静音解锁音频，规避浏览器 autoplay 策略拦截首次响铃（BUG-9）
    this.audioUnlockHandler = () => this.unlockAudio();
    window.addEventListener('pointerdown', this.audioUnlockHandler, { once: true });
    await this.loadAll();
    this.pollTimer = setInterval(() => {
      if (this.reorderMode) return;  // 排列中不重绘，避免打断拖拽
      this.loadToday({ silent: true });
    }, POLL_MS);
    window.addEventListener('beforeunload', this.onUnload = () => this.stopRinging());
  },

  unmount() {
    if (this.pollTimer) clearInterval(this.pollTimer);
    this.pollTimer = null;
    this.stopRinging();
    window.removeEventListener('beforeunload', this.onUnload);
    window.removeEventListener('pointerdown', this.audioUnlockHandler);
    this.detail = null;
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
      if (this.filters.for_date) params.set('for_date', this.filters.for_date);
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
    if (occ.actual_minutes != null) parts.push(`实际耗时 ${occ.actual_minutes}m`);
    if (occ.estimated_minutes) parts.push(`预估耗时 ${occ.estimated_minutes}m`);
    if (occ.is_limited && occ.deadline_at) parts.push(`截止 ${fmtClock(occ.deadline_at)}`);
    if (occ.partial_note) parts.push(`说明：${esc(occ.partial_note)}`);
    return parts.join(' · ');
  },

  itemBadges(occ) {
    const badges = [statusTag(occ.status)];
    if (occ.schedule_label && occ.schedule_label !== '正常') {
      badges.push(tag(esc(occ.schedule_label), occ.schedule_label === '超时' ? 'red' : 'amber'));
    }
    if (occ.is_fixed) badges.push(tag('固定', 'slate'));
    if (occ.phase === 'start') badges.push(tag('开始阶段', 'plum'));
    if (occ.phase === 'end') badges.push(tag('结束阶段', 'plum'));
    if (occ.is_limited) badges.push(tag('限时', 'red'));
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
    const progress = this.root.querySelector('#planning-progress');
    const attention = this.root.querySelector('#planning-attention');
    const done = this.root.querySelector('#planning-done');
    const inReorder = this.reorderMode;

    progress.innerHTML = board.progress.length
      ? board.progress.map((occ) => this.itemHtml(occ, {
        draggable: inReorder,
        idle: occ.task_type === 'idle',
      })).join('')
      : empty('今天还没有待办', '新建一个待办，或等待 0 点刷新');
    attention.innerHTML = board.attention.length
      ? board.attention.map((occ) => this.itemHtml(occ)).join('')
      : empty('没有需要处理的异常待办');
    done.innerHTML = board.done.length
      ? board.done.map((occ) => this.itemHtml(occ, { closed: true })).join('')
      : empty('今天还没有关闭的记录');

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
      if (occ) this.showOccurrenceDetail(occ);
    }
    if (inReorder) this.attachDragHandlers();
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
            ${task.time_mode === 'explicit' ? `<span>固定 ${task.est_start_tod || ''}～${task.est_end_tod || ''}</span>` : ''}
            ${task.is_hollow ? '<span>中空待办</span>' : ''}
            ${task.next_due ? `<span>下次到期 ${fmtDue(task.next_due)}</span>` : ''}
            ${task.timer_minutes ? `<span>计时器 ${task.timer_minutes}m</span>` : ''}
          </div>
        </div>
        <div class="plan-item-side"><div class="tag-row">
          ${tag(esc(TASK_TYPE_LABELS[task.task_type] || task.task_type), 'gold')}
          ${task.is_limited ? tag('限时', 'red') : ''}
          ${task.is_fixed ? tag('固定', 'slate') : ''}
          ${task.alarm_start || task.alarm_end ? tag('闹钟', 'plum') : ''}
          ${task.is_active ? '' : tag('已废弃', 'red')}
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
            <span>${esc(occ.for_date)}</span>
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
        <div class="kv"><span class="k">所属日期</span><span class="v">${esc(occ.for_date)}</span></div>
        <div class="kv"><span class="k">预估时间</span><span class="v">${fmtRange(occ.est_start, occ.est_end)}</span></div>
        <div class="kv"><span class="k">实际时间</span><span class="v">${fmtRange(occ.actual_start, occ.actual_end)}</span></div>
        <div class="kv"><span class="k">实际耗时</span><span class="v">${occ.actual_minutes != null ? occ.actual_minutes + 'm' : '-'}</span></div>
        <div class="kv"><span class="k">预估耗时</span><span class="v">${occ.estimated_minutes ? occ.estimated_minutes + 'm' : '-'}</span></div>
        ${occ.is_limited ? `<div class="kv"><span class="k">限时截止</span><span class="v">${fmtClock(occ.deadline_at)}</span></div>` : ''}
        ${occ.partial_note ? `<div class="kv kv-block"><span class="k">部分完成说明</span><span class="v">${esc(occ.partial_note)}</span></div>` : ''}
        <div class="kv"><span class="k">提醒</span><span class="v">${[
          occ.alarm_start ? '开始闹钟' : '',
          occ.alarm_end ? '结束闹钟' : '',
          occ.timer_minutes ? `计时器 ${occ.timer_minutes}m` : '',
        ].filter(Boolean).join('、') || '无'}</span></div>
        <div class="kv"><span class="k">排列标签</span><span class="v">${esc(occ.schedule_label)}</span></div>`,
      actions,
    });
  },

  occurrenceActions(occ) {
    const btn = (act, label, icon_name = 'check', cls = 'btn-secondary') =>
      `<button class="btn ${cls} btn-sm" data-act="occ-${act}" data-id="${occ.id}">${icon(icon_name)}${label}</button>`;
    const repeatable = occ.task_type !== 'once';
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
      parts.push(btn('partial', '部分完成'));
      parts.push(btn('defer', '延后', 'clock'));
      if (repeatable) parts.push(btn('discard-this', '此次废弃'));
      parts.push(btn('discard', '废弃', 'x', 'btn-danger-line'));
    }
    if (occ.status === 'timeout') {
      parts.push(btn('reschedule', '重新安排', 'clock', 'btn-primary'));
      if (repeatable) parts.push(btn('discard-this', '此次废弃'));
      parts.push(btn('discard', '废弃', 'x', 'btn-danger-line'));
    }
    if (occ.status === 'partial') {
      // 部分完成 → 已完成：直接转完整完成（后端允许 partial → completed）
      parts.push(btn('finish', '已完成', 'check', 'btn-primary'));
    }
    if (CLOSED_STATUSES.includes(occ.status)) {
      parts.push(btn('reopen', '改回待办', 'refresh'));
      parts.push(btn('backfill', '补填实际时间', 'edit'));
    }
    if (OPEN_STATUSES.includes(occ.status)) {
      parts.push(btn('edit-time', '编辑时间', 'edit'));
      parts.push(btn('split', '拆分待办', 'layers'));
    }
    if (occ.status === 'partial') {
      parts.push(btn('spawn-remaining', '剩余部分生成新待办', 'plus'));
    }
    return parts.join('');
  },

  async showTaskDetail(task) {
    this.markSelected(`[data-task="${task.id}"]`);
    const parts = [];
    if (task.is_active && task.task_type === 'interval') {
      parts.push(`<button class="btn btn-primary btn-sm" data-act="task-early" data-id="${task.id}">${icon('check')}提前完成</button>`);
    }
    parts.push(`<button class="btn btn-secondary btn-sm" data-act="task-edit" data-id="${task.id}">${icon('edit')}编辑</button>`);
    if (task.is_active) {
      parts.push(`<button class="btn btn-danger-line btn-sm" data-act="task-discard" data-id="${task.id}">${icon('x')}废弃任务</button>`);
    } else {
      parts.push(`<button class="btn btn-secondary btn-sm" data-act="task-enable" data-id="${task.id}">${icon('refresh')}重新启用</button>`);
    }
    this.detail.render({
      title: esc(task.content),
      badges: `<div class="tag-row">${tag(esc(TASK_TYPE_LABELS[task.task_type] || ''), 'gold')}${task.is_active ? '' : tag('已废弃', 'red')}</div>`,
      html: `
        <div class="kv"><span class="k">重复规则</span><span class="v">${esc(taskTypeSummary(task))}</span></div>
        <div class="kv"><span class="k">时间模式</span><span class="v">${task.time_mode === 'explicit'
          ? `固定 ${esc(task.est_start_tod || '')}～${esc(task.est_end_tod || '')}`
          : '仅预估耗时（可自动排程）'}</span></div>
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
        ${task.next_due ? `<div class="kv"><span class="k">下次到期</span><span class="v">${fmtDue(task.next_due)}</span></div>` : ''}
        <div class="kv"><span class="k">生成游标</span><span class="v">${esc(task.cursor_date || '-')}</span></div>`,
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
    const items = this.board?.progress || [];
    if (items.length < 2) {
      toast('至少两个待办才能调整顺序');
      return;
    }
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
      toast('顺序已保存，等待自动重算');
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
      toast(`已重新计算 ${result.updated} 项待办时间`);
      await this.loadToday();
    } catch (error) {
      toast(`重算失败：${error.message}`, 'err');
    }
  },

  async occurrenceAction(act, id) {
    const post = (path, body) => gw(`/admin/api/planning/occurrences/${id}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    try {
      if (act === 'start') await post('/start');
      else if (act === 'finish' || act === 'complete') await post('/finish');
      else if (act === 'partial') return this.askPartial(id);
      else if (act === 'defer') return this.askNewTime(id, 'deferred', '延后到什么时间？');
      else if (act === 'reschedule') return this.askNewTime(id, 'pending', '重新安排到什么时间？');
      else if (act === 'discard-this') {
        if (!(await confirm('确认「此次废弃」？只废弃这一次出现，不影响后续刷新。', { danger: false }))) return;
        await post('/status', { status: 'discarded_this' });
      } else if (act === 'discard') {
        if (!(await confirm('确认废弃？该待办后续不再自动出现。'))) return;
        await post('/status', { status: 'discarded' });
      } else if (act === 'reopen') await post('/status', { status: 'pending' });
      else if (act === 'edit-time') return this.askEditTime(id);
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

  async taskAction(act, id) {
    try {
      if (act === 'early') {
        await gw(`/admin/api/planning/tasks/${id}/complete-early`, { method: 'POST' });
        toast('已提前完成，下一次出现时间已重置');
      } else if (act === 'edit') {
        const task = this.tasks.find((t) => t.id === id);
        if (task) this.openTaskForm(task);
        return;
      } else if (act === 'discard') {
        if (!(await confirm('废弃整个任务？后续不再刷新，当天未完成的实例也会关闭。'))) return;
        await gw(`/admin/api/planning/tasks/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_active: false }),
        });
        toast('任务已废弃');
      } else if (act === 'enable') {
        await gw(`/admin/api/planning/tasks/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_active: true }),
        });
        toast('任务已重新启用');
      }
      await Promise.all([this.loadTasks(), this.loadToday()]);
    } catch (error) {
      toast(`操作失败：${error.message}`, 'err');
    }
  },

  askPartial(id) {
    const { root, close } = modal({
      title: '部分完成',
      body: `<div class="field"><label>完成了哪些部分（会保存为说明）</label>
             <textarea id="planning-partial-note" rows="3" placeholder="例如：背完了前 20 页"></textarea></div>`,
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
        toast('已记录部分完成');
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
    const occ = this.findOccurrence(id) || {};
    const toLocal = (iso) => {
      if (!iso) return '';
      const d = new Date(iso);
      if (Number.isNaN(d.getTime())) return '';
      const pad = (n) => String(n).padStart(2, '0');
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    const { root, close } = modal({
      title: '编辑预估时间',
      body: `
        <div class="field"><label>预估开始</label><input type="datetime-local" id="planning-edit-start" value="${toLocal(occ.est_start)}"></div>
        <div class="field"><label>预估结束</label><input type="datetime-local" id="planning-edit-end" value="${toLocal(occ.est_end)}"></div>
        <p class="muted text-sm">手动编辑后该条目按此时间固定，不再被自动重算移动。</p>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>保存</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const start = root.querySelector('#planning-edit-start').value;
      const end = root.querySelector('#planning-edit-end').value;
      if (!start) { toast('请填写开始时间', 'err'); return; }
      try {
        await gw(`/admin/api/planning/occurrences/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            est_start: new Date(start).toISOString(),
            est_end: end ? new Date(end).toISOString() : null,
          }),
        });
        close();
        toast('时间已更新');
        await this.loadToday();
      } catch (error) {
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
    const partRow = (index) => `
      <div class="field" data-part-row>
        <label>部分 ${index} 内容</label>
        <input type="text" data-part-content>
        <label style="margin-top:6px">耗时（分钟或 1h30m 简写）</label>
        <input type="text" data-part-minutes value="30" style="width:120px">
      </div>`;
    const { root, close } = modal({
      title: '拆分待办',
      body: `
        <p class="muted text-sm">把这条待办拆成多个新的单次待办（今天执行），原条目会废弃留痕。</p>
        <div id="planning-split-parts">
          ${partRow(1)}
          ${partRow(2)}
        </div>
        <button class="btn btn-quiet btn-sm" data-add-part>${icon('plus')}再加一部分</button>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>拆分</button>`,
    });
    root.querySelector('[data-add-part]').onclick = () => {
      const host = root.querySelector('#planning-split-parts');
      host.insertAdjacentHTML('beforeend', partRow(host.querySelectorAll('[data-part-row]').length + 1));
    };
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const parts = [...root.querySelectorAll('[data-part-row]')]
        .map((rowEl) => ({
          content: rowEl.querySelector('[data-part-content]').value.trim(),
          estimated_minutes: rowEl.querySelector('[data-part-minutes]').value.trim() || '30',
        }))
        .filter((part) => part.content);
      if (parts.length < 2) { toast('至少填写两部分', 'err'); return; }
      try {
        await gw(`/admin/api/planning/occurrences/${id}/split`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ parts }),
        });
        close();
        toast('已拆分');
        await Promise.all([this.loadToday(), this.loadTasks(), this.loadOccurrences()]);
      } catch (error) {
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
      target_date: source.for_date,
    });
  },

  clearFilters() {
    this.filters = { task_type: '', status: '', for_date: '' };
    this.root.querySelector('#planning-filter-type').value = '';
    this.root.querySelector('#planning-filter-status').value = '';
    this.root.querySelector('#planning-filter-date').value = '';
    this.loadOccurrences();
  },

  /* ---------- 新建 / 编辑任务表单 ---------- */

  openTaskForm(task) {
    const editing = !!task?.id;
    const value = (field, fallback = '') => (task ? (task[field] ?? fallback) : fallback);
    const typeOptions = TASK_TYPES.map((t) =>
      `<option value="${t}" ${value('task_type', 'daily') === t ? 'selected' : ''}>${TASK_TYPE_LABELS[t]}</option>`).join('');
    const weekdayChecks = WEEKDAY_NAMES.map((name, index) => `
      <label class="inline"><input type="checkbox" data-weekday value="${index}"
        ${(value('weekdays') || []).includes(index) ? 'checked' : ''}>周${name}</label>`).join('');
    const { root, close } = modal({
      title: editing ? '编辑待办' : '新建待办',
      wide: true,
      body: `
        <div class="field"><label>内容</label>
          <input type="text" id="pf-content" value="${esc(value('content'))}" placeholder="例如：背单词"></div>
        <div class="field"><label>类型</label><select id="pf-type">${typeOptions}</select></div>
        <div data-type-block="interval" style="display:none">
          <div class="field"><label>完成后间隔天数（1-3650）</label>
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
          <div class="field"><label>目标日期</label>
            <input type="date" id="pf-target-date" value="${esc(value('target_date'))}"></div>
        </div>
        <div class="field"><label>预估耗时（分钟，或 1h30m 简写）</label>
          <input type="text" id="pf-estimated" value="${esc(value('estimated_minutes', ''))}"></div>
        <div class="field"><label>显式开始时间（可选，填写后不参与自动移动）</label>
          <input type="time" id="pf-start-tod" value="${esc(value('est_start_tod'))}"></div>
        <div class="field"><label>显式结束时间（可选）</label>
          <input type="time" id="pf-end-tod" value="${esc(value('est_end_tod'))}"></div>
        <div class="field"><label class="inline"><input type="checkbox" id="pf-fixed" ${value('is_fixed') ? 'checked' : ''}> 固定待办（不因重算移动）</label></div>
        <div class="field"><label>限时截止（可选，当日时刻）</label>
          <input type="time" id="pf-deadline" value="${esc(value('deadline_tod'))}"></div>
        <div class="field"><label>限时范围结束（可选，需先填截止）</label>
          <input type="time" id="pf-deadline-end" value="${esc(value('deadline_end_tod'))}"></div>
        <div class="field"><label class="inline"><input type="checkbox" id="pf-hollow" ${value('is_hollow') ? 'checked' : ''}> 中空待办（开始/结束两个条目，中间可插入其他待办）</label></div>
        <div id="pf-hollow-block" style="display:none">
          <div class="field"><label>开始阶段内容</label><input type="text" id="pf-hollow-start" value="${esc(value('hollow_start_content'))}"></div>
          <div class="field"><label>开始阶段耗时（分钟）</label><input type="number" id="pf-hollow-start-min" min="1" value="${esc(value('hollow_start_minutes', ''))}"></div>
          <div class="field"><label>中间等待时长（分钟）</label><input type="number" id="pf-hollow-wait" min="1" value="${esc(value('hollow_wait_minutes', ''))}"></div>
          <div class="field"><label>中间说明（可选）</label><input type="text" id="pf-hollow-note" value="${esc(value('hollow_wait_note'))}"></div>
          <div class="field"><label>结束阶段内容</label><input type="text" id="pf-hollow-end" value="${esc(value('hollow_end_content'))}"></div>
          <div class="field"><label>结束阶段耗时（分钟）</label><input type="number" id="pf-hollow-end-min" min="1" value="${esc(value('hollow_end_minutes', ''))}"></div>
        </div>
        <div class="field"><label>提醒</label>
          <div class="tag-row">
            <label class="inline"><input type="checkbox" id="pf-alarm-start" ${value('alarm_start') ? 'checked' : ''}> 开始闹钟</label>
            <label class="inline"><input type="checkbox" id="pf-alarm-end" ${value('alarm_end') ? 'checked' : ''}> 结束闹钟</label>
            <label class="inline">计时器 <input type="text" id="pf-timer" placeholder="如 30m / 1h" value="${esc(value('timer_minutes', ''))}" style="width:90px"></label>
          </div>
          <p class="muted text-sm">开始/结束闹钟到点循环响铃；计时器从点「开始」起倒计时，结束播一次。页面关闭时不提醒。</p>
        </div>
        ${editing ? `<div class="field"><label class="inline"><input type="checkbox" id="pf-active" ${value('is_active') ? 'checked' : ''}> 启用中</label></div>` : ''}`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>${editing ? '保存' : '创建'}</button>`,
    });

    const typeSelect = root.querySelector('#pf-type');
    const syncBlocks = () => {
      const type = typeSelect.value;
      root.querySelectorAll('[data-type-block]').forEach((block) => {
        block.style.display = block.dataset.typeBlock === type ? '' : 'none';
      });
      root.querySelector('#pf-hollow-block').style.display =
        root.querySelector('#pf-hollow').checked ? '' : 'none';
    };
    typeSelect.addEventListener('change', syncBlocks);
    root.querySelector('#pf-hollow').addEventListener('change', syncBlocks);
    syncBlocks();

    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-ok]').onclick = async () => {
      const type = typeSelect.value;
      const body = {
        content: root.querySelector('#pf-content').value.trim(),
        task_type: type,
      };
      const estimated = root.querySelector('#pf-estimated').value.trim();
      if (estimated) body.estimated_minutes = estimated;
      const startTod = root.querySelector('#pf-start-tod').value;
      const endTod = root.querySelector('#pf-end-tod').value;
      if (startTod) body.est_start_tod = startTod;
      if (endTod) body.est_end_tod = endTod;
      if (root.querySelector('#pf-fixed').checked) body.is_fixed = true;
      const deadline = root.querySelector('#pf-deadline').value;
      if (deadline) body.deadline_tod = deadline;
      const deadlineEnd = root.querySelector('#pf-deadline-end').value;
      if (deadlineEnd) body.deadline_end_tod = deadlineEnd;
      if (type === 'interval') body.interval_days = Number(root.querySelector('#pf-interval-days').value) || null;
      if (type === 'weekly') {
        body.weekdays = [...root.querySelectorAll('[data-weekday]:checked')].map((el) => Number(el.value));
      }
      if (type === 'monthly') {
        body.month_days = root.querySelector('#pf-month-days').value
          .split(/[,，\s]+/).map((v) => Number(v)).filter((v) => Number.isInteger(v) && v > 0);
      }
      if (type === 'once') body.target_date = root.querySelector('#pf-target-date').value || null;
      if (root.querySelector('#pf-hollow').checked) {
        body.is_hollow = true;
        body.hollow_start_content = root.querySelector('#pf-hollow-start').value.trim() || body.content;
        body.hollow_start_minutes = Number(root.querySelector('#pf-hollow-start-min').value) || null;
        body.hollow_wait_minutes = Number(root.querySelector('#pf-hollow-wait').value) || null;
        body.hollow_wait_note = root.querySelector('#pf-hollow-note').value.trim() || null;
        body.hollow_end_content = root.querySelector('#pf-hollow-end').value.trim() || body.content;
        body.hollow_end_minutes = Number(root.querySelector('#pf-hollow-end-min').value) || null;
      }
      if (root.querySelector('#pf-alarm-start').checked) body.alarm_start = true;
      if (root.querySelector('#pf-alarm-end').checked) body.alarm_end = true;
      const timer = root.querySelector('#pf-timer').value.trim();
      if (timer) body.timer_minutes = timer;
      if (editing) body.is_active = root.querySelector('#pf-active').checked;

      try {
        if (editing) {
          await gw(`/admin/api/planning/tasks/${task.id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          toast('待办已保存');
        } else {
          await gw('/admin/api/planning/tasks', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          toast('待办已创建');
        }
        close();
        await this.loadAll();
      } catch (error) {
        toast(`保存失败：${error.message}`, 'err');
      }
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
        this.taskAction(act.slice(5), id);
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
