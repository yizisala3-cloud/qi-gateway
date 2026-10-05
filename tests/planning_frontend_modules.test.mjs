import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const workspace = process.argv[2];
const group = process.argv[3];
const uiSource = await readFile(join(workspace, 'admin/js/ui.js'), 'utf8');
const version = uiSource.match(/ASSET_VERSION = '([^']+)'/)[1];
const masks = [];
const toastMessages = [];
const intervals = new Map();
const windowListeners = new Map();
const requests = [];
let intervalId = 0;
let keyId = 0;
let respond = async () => ({ ok: true, json: async () => ({}) });

class Element {
  constructor() {
    this.value = '';
    this.defaultValue = '';
    this.checked = false;
    this.defaultChecked = false;
    this.disabled = false;
    this.hidden = false;
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.nodes = new Map();
    this.lists = new Map();
    this.listeners = new Map();
    this.classes = new Set();
    this.classList = {
      add: (name) => this.classes.add(name),
      remove: (name) => this.classes.delete(name),
      contains: (name) => this.classes.has(name),
      toggle: (name, value) => value ? this.classes.add(name) : this.classes.delete(name),
    };
    this.lastElementChild = {};
    Object.defineProperty(this.lastElementChild, 'textContent', {
      set: (value) => toastMessages.push(value),
    });
  }
  querySelector(selector) {
    if (!this.nodes.has(selector)) this.nodes.set(selector, new Element());
    return this.nodes.get(selector);
  }
  querySelectorAll(selector) { return this.lists.get(selector) || []; }
  appendChild(child) {
    this.children.push(child);
    if (child.className === 'modal-mask') masks.push(child);
  }
  contains(node) {
    if (node === this) return true;
    for (const child of this.nodes.values()) {
      if (child === node || child.contains?.(node)) return true;
    }
    return false;
  }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
  }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  insertAdjacentHTML() {}
  scrollIntoView() {}
  remove() { this.removed = true; }
  setPointerCapture(id) { this.captured = id; }
  getBoundingClientRect() { return { top: 0, height: 40 }; }
}

class AudioFixture {
  constructor(url) {
    this.url = url; this.src = url; this.ringSource = null;
    this.currentTime = 0; this.plays = 0; this.pauses = 0;
    this.loads = 0; this.volume = 1; this.preload = '';
    this.muted = false; this.paused = true; this.loop = false;
    // behavior：'success' 立即成功 | 'blocked' 播放被拒 | 'deferred' 由用例手动结算
    this.behavior = 'success'; this.pending = []; this.calls = [];
  }
  play() {
    this.plays += 1;
    this.calls.push({ src: this.src, muted: this.muted, loop: this.loop, volume: this.volume });
    if (this.behavior === 'blocked') return Promise.reject(new Error('NotAllowedError: play blocked'));
    this.paused = false;
    if (this.behavior === 'deferred') return new Promise((resolve, reject) => this.pending.push({ resolve, reject }));
    return Promise.resolve();
  }
  pause() {
    this.pauses += 1;
    this.paused = true;
    // 原生媒体行为（R1 复现前提）：pause 会中断未完成的 play() 并使其拒绝
    for (const job of this.pending.splice(0)) {
      const error = new Error('The play() request was interrupted by a call to pause()');
      error.name = 'AbortError';
      job.reject(error);
    }
  }
  load() { this.loads += 1; }
}

class NotificationFixture {
  static permission = 'granted';
  static created = 0;
  static requestPermission() { return Promise.resolve(this.permission); }
  constructor(title, options) {
    this.title = title; this.options = options;
    NotificationFixture.created += 1;
  }
}

globalThis.document = { body: new Element(), createElement: () => new Element() };
globalThis.window = {
  location: { origin: 'http://planning.test' },
  Notification: NotificationFixture,
  matchMedia: () => ({ matches: false }),
  addEventListener(name, fn, options = {}) {
    if (!windowListeners.has(name)) windowListeners.set(name, new Map());
    windowListeners.get(name).set(fn, options);
  },
  removeEventListener(name, fn) { windowListeners.get(name)?.delete(fn); },
};
globalThis.Audio = AudioFixture;
globalThis.Notification = NotificationFixture;
globalThis.Node = { DOCUMENT_POSITION_PRECEDING: 2 };
globalThis.localStorage = { getItem: () => '', setItem() {}, removeItem() {} };
globalThis.setInterval = (fn, ms) => {
  const id = ++intervalId;
  intervals.set(id, { fn, ms });
  return id;
};
globalThis.clearInterval = (id) => intervals.delete(id);
globalThis.setTimeout = () => 0;
Object.defineProperty(globalThis, 'crypto', {
  value: { randomUUID: () => `planning-retry-${++keyId}` }, configurable: true,
});
globalThis.fetch = async (url, options = {}) => {
  const request = { url, options, body: options.body ? JSON.parse(options.body) : null };
  requests.push(request);
  return respond(request);
};

const moduleUrl = (relative) => pathToFileURL(join(workspace, 'admin/js', relative)).href + `?v=${version}`;
const page = (await import(moduleUrl('pages/planning.js'))).default;
const display = await import(moduleUrl('lib/planning_display.js'));
const formModule = await import(moduleUrl('lib/planning_task_form.js'));
const dialogModule = await import(moduleUrl('lib/planning_dialogs.js'));
const sortModule = await import(moduleUrl('lib/planning_sort.js'));
const reminderModule = await import(moduleUrl('lib/planning_reminder.js'));
const readsModule = await import(moduleUrl('lib/planning_reads.js'));
const ui = await import(moduleUrl('ui.js'));
const lastModal = () => masks.at(-1).querySelector('.modal');
const response = (body = {}) => ({ ok: true, json: async () => body });
const failure = (error) => ({ ok: false, status: 409, statusText: 'Conflict', json: async () => ({ error }) });
const listenerCount = (name) => windowListeners.get(name)?.size || 0;
// 模拟一次页面按下：按注册顺序触发全部 pointerdown 监听，once 监听触发后移除
const gesture = async (event) => {
  const registry = windowListeners.get('pointerdown');
  if (!registry) return;
  for (const [fn, options] of [...registry]) {
    if (options?.once) registry.delete(fn);
    await fn(event);
  }
};
const maskClick = (mask) => {
  for (const fn of [...(mask.listeners.get('click') || [])]) fn({ target: mask });
};
const emptyBoard = () => ({ progress: [], attention: [], done: [], conflicts: [], recompute: {} });
const boardWith = (id, content) => ({ ...emptyBoard(), progress: [{ id, content, status: 'pending' }] });
const flush = async () => { for (let count = 0; count < 12; count += 1) await Promise.resolve(); };
const fillCreate = () => {
  page.openTaskForm(null);
  const root = lastModal();
  root.querySelector('#pf-type').value = 'daily';
  root.querySelector('#pf-content').value = '创建延迟探针';
  root.querySelector('#pf-estimated').value = '30';
  return root.querySelector('[data-ok]');
};
const readPaths = () => requests.filter(({ options }) => !options.method)
  .map(({ url }) => new URL(url).pathname);

const checks = {
  async graph() {
    assert.equal(typeof page.mount, 'function');
    assert.equal(typeof page.unmount, 'function');
    assert.equal(typeof formModule.openTaskForm, 'function');
    assert.equal(typeof dialogModule.createPlanningDialogs, 'function');
    assert.equal(typeof sortModule.createPlanningSort, 'function');
    assert.equal(typeof reminderModule.createPlanningReminder, 'function');
    assert.equal(typeof readsModule.createPlanningReads, 'function');
    assert.equal(ui.ASSET_VERSION, version);
    for (const file of [
      'pages/planning.js', 'lib/planning_display.js', 'lib/planning_task_form.js',
      'lib/planning_dialogs.js', 'lib/planning_sort.js', 'lib/planning_reminder.js', 'lib/planning_reads.js',
      'lib/retro_time.js', 'lib/retro_select.js', 'lib/cycle_settings.js',
    ]) {
      const source = await readFile(join(workspace, 'admin/js', file), 'utf8');
      const imports = [...source.matchAll(/(?:import\s[^;]*?from\s*)['"]([^'"]+)['"]/g)];
      for (const [, specifier] of imports) {
        assert.equal(specifier.split('?v=')[1], version, `${file}: ${specifier}`);
        await import(new URL(specifier, moduleUrl(file)));
      }
    }
  },

  async 'B2-display'() {
    assert.equal(display.durationText({ status: 'completed', actual_logged_seconds: 3661, estimated_minutes: 90 }), '实际耗时 1h1m1s');
    assert.equal(display.durationText({ status: 'completed', actual_logged_seconds: 0, estimated_minutes: 90 }), '实际耗时 0s');
    assert.equal(display.durationText({ status: 'timeout', actual_minutes: 44, estimated_minutes: 90 }), '预估耗时 90m');
    assert.equal(display.durationText({ status: 'partial', actual_minutes: 44, estimated_minutes: 90 }), '实际耗时 44m · 预估耗时 90m');
    assert.equal(display.isClosedOcc({ status: 'partial' }), false);
    assert.equal(display.taskTypeSummary({ task_type: 'weekly', weekdays: [0, 6] }), '周一、周日');
    assert.equal(display.fmtRange(null, null), '未排时间');
    assert.equal(display.fmtClock('invalid'), '-');
    const occ = { id: 7, status: 'completed', content: '<script>', estimated_minutes: 90, partial_note: '<note>' };
    const html = display.itemHtml(occ, { closed: true }, new Set([7]));
    assert.ok(html.includes('&lt;script&gt;'));
    assert.ok(html.includes('&lt;note&gt;'));
    assert.ok(html.includes('预估耗时 90m'));
    assert.ok(html.includes('排程冲突'));
    assert.ok(!display.itemHtml(occ).includes('排程冲突'));
    assert.deepEqual(occ, { id: 7, status: 'completed', content: '<script>', estimated_minutes: 90, partial_note: '<note>' });
  },

  async 'B3-form'() {
    let saved = 0;
    const deps = {
      occurrences: [],
      initRetroFields(root) {
        root.querySelector('#pf-type').value = 'daily';
        root.querySelector('#pf-content').value = '原生导入表单探针';
      },
      onSaved: async () => { saved += 1; throw new Error('refresh failed after commit'); },
    };
    formModule.openTaskForm(null, deps);
    const button = lastModal().querySelector('[data-ok]');
    let resolveRequest;
    respond = () => new Promise((resolve) => { resolveRequest = resolve; });
    const first = button.onclick();
    await button.onclick();
    assert.equal(requests.length, 1);
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, '正在创建…');
    resolveRequest(failure('first request failed'));
    await first;
    assert.equal(button.disabled, false);
    assert.equal(button.textContent, '创建');
    respond = async () => response({ first_round_skipped: true });
    await button.onclick();
    assert.equal(saved, 1);
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, '已创建');
    assert.equal(masks.at(-1).removed, true);
    assert.ok(toastMessages.includes('本轮已过最晚完成，从次日起按重复规则生效'));
    assert.ok(toastMessages.includes('待办已创建，列表更新失败，请刷新重试'));
    await button.onclick();
    assert.equal(requests.length, 2);
    assert.equal(requests[1].body.task_type, 'daily');

    const task = { id: 9, task_type: 'once', has_generated_occurrence: true,
      target_date: '2026-10-02', window_start_tod: '09:00', window_end_tod: null };
    formModule.openTaskForm(task, {
      ...deps,
      initRetroFields(root) {
        root.querySelector('#pf-type').value = 'once';
        root.querySelector('#pf-content').value = '单次锁定';
        root.querySelector('#pf-target-date').value = '2026-10-03';
        root.querySelector('#pf-window-start').value = '15:00';
      },
      onSaved: async () => {},
    });
    const root = lastModal();
    assert.equal(root.querySelector('.retro-time[data-retro-for="pf-target-date"]').querySelector('button').disabled, true);
    await root.querySelector('[data-ok]').onclick();
    assert.equal(requests.at(-1).options.method, 'PATCH');
    assert.equal(requests.at(-1).body.target_date, task.target_date);
    assert.equal(requests.at(-1).body.window_start_tod, task.window_start_tod);
    assert.equal(requests.at(-1).body.window_end_tod, null);
  },

  async 'B4-dialogs'() {
    const refreshes = [];
    const dialogs = dialogModule.createPlanningDialogs({
      findOccurrence: () => ({ id: 7, task_id: 2, window_start_at: '2026-10-02T09:00:00+08:00' }),
      getOccurrences: () => [], getTasks: () => [], openTaskForm() {},
      loadToday: async () => { refreshes.push('today'); },
      loadTasks: async () => { refreshes.push('tasks'); },
      loadOccurrences: async () => { refreshes.push('occurrences'); },
    });
    await dialogs.askRescheduleTimeout(7);
    const root = lastModal();
    root.querySelector('#planning-reschedule-time').value = '2026-10-02T10:00';
    respond = async () => failure('retry');
    await root.querySelector('[data-ok]').onclick();
    await root.querySelector('[data-ok]').onclick();
    assert.equal(requests[0].options.headers['Idempotency-Key'], requests[1].options.headers['Idempotency-Key']);
    assert.equal(refreshes.length, 0);
    root.querySelector('#planning-reschedule-time').value = '2026-10-02T11:00';
    respond = async () => response();
    await root.querySelector('[data-ok]').onclick();
    assert.notEqual(requests[0].options.headers['Idempotency-Key'], requests[2].options.headers['Idempotency-Key']);
    assert.deepEqual(refreshes, ['today', 'occurrences']);

    dialogs.askEditTime(7);
    await lastModal().querySelector('[data-ok]').onclick();
    assert.deepEqual(requests.at(-1).body, { window_start_at: null, window_end_at: null });
    assert.equal(requests.at(-1).options.method, 'PATCH');
    dialogs.askBackfill(7);
    await lastModal().querySelector('[data-ok]').onclick();
    assert.deepEqual(requests.at(-1).body, { actual_start: null, actual_end: null });
  },

  async 'B5-sort'() {
    const root = new Element();
    const list = new Element();
    const item = new Element();
    item.dataset.occ = '7';
    list.children = [item];
    list.lists.set('.plan-item', [item]);
    let activeTab = 'all';
    let activeSection = 'attention';
    let reordered = false;
    let renders = 0;
    let loads = 0;
    const sort = sortModule.createPlanningSort({
      getRoot: () => root, getProgressList: () => list,
      getBoard: () => ({ progress: [{ id: 7 }, { id: 8 }], recompute: { enabled: false } }),
      getActiveTab: () => activeTab, getActiveSection: () => activeSection,
      selectSection: (value) => { activeSection = value; },
      renderBoard: () => { renders += 1; }, loadToday: async () => { loads += 1; },
      getReorderMode: () => reordered, setReorderMode: (value) => { reordered = value; },
    });
    sort.enterReorder();
    assert.equal(reordered, false);
    activeTab = 'today';
    sort.enterReorder();
    assert.equal(reordered, true);
    assert.equal(activeSection, 'progress');
    assert.equal(renders, 1);
    respond = async () => failure('order must include every open occurrence');
    await sort.confirmReorder();
    assert.equal(reordered, false);
    assert.equal(loads, 1);
    assert.ok(toastMessages.includes('待办列表有变化，请重新进入排列'));
    sort.enterReorder();
    respond = async () => response();
    await sort.confirmReorder();
    assert.deepEqual(requests.at(-1).body, { order: [7] });
    assert.ok(toastMessages.some((value) => value.includes('自动重算已关闭，时间未重算')));
    sort.attachDragHandlers();
    assert.equal(item.listeners.get('pointerdown').size, 1);
    sort.startDrag({ button: 0, pointerType: 'mouse', pointerId: 31, clientY: 10, preventDefault() {} }, item);
    assert.equal(item.captured, 31);
    assert.equal(item.classList.contains('is-dragging'), true);
    [...item.listeners.get('pointerup')][0]();
    assert.equal(item.classList.contains('is-dragging'), false);
    assert.equal(item.listeners.get('pointermove').size, 0);
    assert.equal(item.listeners.get('pointercancel').size, 0);
  },

  async 'B6-lifecycle'() {
    const reminders = reminderModule.createPlanningReminder();
    reminders.attach();
    reminders.listenForUnload();
    assert.equal(listenerCount('pointerdown'), 1);
    assert.equal(listenerCount('beforeunload'), 1);
    reminders.unlockAudio();  // #33：解锁直接发生在业务元素本人身上（静音素材）
    await flush();
    assert.ok(reminders.alarmAudio.calls[0].src.startsWith('data:audio/wav'), '解锁播放内联静音 WAV');
    assert.equal(reminders.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '解锁后挂真实铃声预加载');
    assert.equal(reminders.alarmAudio.loop, true, '闹钟挂源后循环');
    assert.equal(reminders.alarmAudio.loads, 1, '挂源触发预加载');
    assert.equal(reminders.timerAudio.ringSource, '/admin/assets/audio/timer-done.ogg');
    const now = Date.parse('2026-10-02T10:00:00+08:00');
    reminders.fireAlarm({ id: 1 }, 'start', '开始', '未来', new Date(now + 1000).toISOString(), now);
    assert.equal(reminders.firedKeys.size, 0);
    reminders.fireAlarm({ id: 2 }, 'start', '开始', '过期', new Date(now - 120001).toISOString(), now);
    assert.equal(reminders.firedKeys.size, 1);
    const due = new Date(now).toISOString();
    reminders.fireAlarm({ id: 3 }, 'start', '开始', '到点', due, now);
    const plays = reminders.alarmAudio.plays;
    assert.ok(reminders.ringModal);
    reminders.dispose();
    assert.equal(listenerCount('pointerdown'), 0);
    assert.equal(listenerCount('beforeunload'), 0);
    assert.equal(reminders.ringModal, null);
    reminders.attach();
    reminders.listenForUnload();
    reminders.fireAlarm({ id: 3 }, 'start', '开始', '到点', due, now);
    assert.equal(reminders.alarmAudio.plays, plays);
    reminders.dispose();

    respond = async () => response(emptyBoard());
    const pageReminder = page.reminder;
    pageReminder.firedKeys.add('persist-across-route');
    await page.mount(new Element());
    assert.equal(intervals.size, 1);
    assert.equal([...intervals.values()][0].ms, 30000);
    assert.equal(listenerCount('pointerdown'), 1);
    assert.equal(listenerCount('beforeunload'), 1);
    page.unmount();
    assert.equal(intervals.size, 0);
    assert.equal(listenerCount('pointerdown'), 0);
    assert.equal(listenerCount('beforeunload'), 0);
    assert.equal(page.root, null);
    await page.mount(new Element());
    assert.equal(page.reminder, pageReminder);
    assert.ok(page.reminder.firedKeys.has('persist-across-route'));
    // #33：重挂载（切换页面再回）后触摸同样只在业务元素本人身上做静音解锁
    // ——播放的是内联静音 WAV，绝不播出真实铃声 URL
    await gesture();
    await flush();
    assert.ok(page.reminder.alarmAudio.calls[0].src.startsWith('data:audio/wav'), '解锁播放静音素材');
    assert.notEqual(page.reminder.alarmAudio.calls[0].src, '/admin/assets/audio/alarm-clock.mp3', '手势不播出真实闹钟铃声');
    assert.notEqual(page.reminder.timerAudio.calls[0].src, '/admin/assets/audio/timer-done.ogg', '手势不播出真实计时器铃声');
    assert.equal(page.reminder.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '解锁后挂真实铃声');
    assert.equal(page.reminder.timerAudio.ringSource, '/admin/assets/audio/timer-done.ogg');

    const input = new Element();
    input.value = 'unsaved';
    page.detail.body.lists.set('input', [input]);
    page.selected = { kind: 'occ', id: 7 };
    page.board = { ...emptyBoard(), progress: [{ id: 7, status: 'pending', content: 'keep input' }] };
    const originalDetail = page.showOccurrenceDetail;
    let detailRenders = 0;
    page.showOccurrenceDetail = () => { detailRenders += 1; };
    page.renderBoard();
    assert.equal(detailRenders, 0);
    input.defaultValue = input.value;
    page.renderBoard();
    assert.equal(detailRenders, 1);
    page.showOccurrenceDetail = originalDetail;
    page.unmount();
    assert.equal(intervals.size, 0);
    assert.equal(listenerCount('pointerdown'), 0);
    assert.equal(listenerCount('beforeunload'), 0);
  },

  async 'B7-reminder-ring'() {
    const flushRings = async () => { for (let count = 0; count < 12; count += 1) await Promise.resolve(); };
    const hint = '浏览器拦截了自动响铃，点击「恢复响铃」按钮即可恢复';

    // #28/#33（ring-fix5）：无提醒配置时首次触摸只做静音解锁——解锁直接发生
    // 在业务闹钟 / 计时器元素本人身上（WebKit 按元素授权），播放的是内联
    // 静音 WAV 素材（全零采样、结构上不可听），绝不播出真实铃声 URL；无
    // 弹窗、无误登记待恢复；解锁结算后元素挂上真实铃声预加载（load 不 play）
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.checkAlarms({ progress: [{ id: 1, alarm_start: false, alarm_end: false, timer_minutes: null }] });
      await gesture();
      await flushRings();
      assert.equal(reminders.alarmAudio.plays, 1, '手势只在闹钟元素上播放静音解锁素材');
      assert.ok(reminders.alarmAudio.calls[0].src.startsWith('data:audio/wav'), '解锁素材为内联静音 WAV');
      assert.notEqual(reminders.alarmAudio.calls[0].src, '/admin/assets/audio/alarm-clock.mp3', '手势不播出真实闹钟铃声');
      assert.equal(reminders.timerAudio.plays, 1, '手势只在计时器元素上播放静音解锁素材');
      assert.ok(reminders.timerAudio.calls[0].src.startsWith('data:audio/wav'));
      assert.notEqual(reminders.timerAudio.calls[0].src, '/admin/assets/audio/timer-done.ogg', '手势不播出真实计时器铃声');
      assert.equal(reminders.ringModal, null);
      assert.equal(reminders.pendingRecovery, null, '静音解锁不进入待恢复');
      assert.equal(reminders.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '解锁后挂真实铃声');
      assert.equal(reminders.timerAudio.ringSource, '/admin/assets/audio/timer-done.ogg');
      assert.equal(reminders.alarmAudio.loads, 1, '解锁结算预加载真实铃声');
      assert.equal(reminders.alarmAudio.paused, true, '解锁结算收尾');
      assert.equal(reminders.alarmAudio.currentTime, 0);
      // 已解锁元素重复手势零播放
      const playsBefore = reminders.alarmAudio.plays;
      reminders.unlockAudio();
      await flushRings();
      assert.equal(reminders.alarmAudio.plays, playsBefore, '已解锁元素重复手势零播放');
      reminders.dispose();
    }

    // #33：按「媒体元素授权彼此不共享」的 WebKit 模型模拟——元素只有在用户
    // 手势内成功播放过才允许无手势 play，授权不跨元素共享。首个手势在业务
    // 元素本人身上完成静音解锁后，到点真实铃声必须无手势直接自动播放，
    // 不允许落到「恢复响铃」；对照：从未手势播放的元素被模型拒绝
    {
      let inGesture = false;
      const policyAudio = () => ({
        src: '', ringSource: null, unlocked: false, loop: false,
        paused: true, plays: 0, calls: [],
        play() {
          this.plays += 1;
          this.calls.push({ src: this.src });
          if (!inGesture && !this.unlocked) {
            const error = new Error('NotAllowedError: play blocked');
            error.name = 'NotAllowedError';
            return Promise.reject(error);
          }
          this.paused = false;
          if (inGesture) this.unlocked = true;
          return Promise.resolve();
        },
        pause() { this.paused = true; },
        load() {},
      });
      const reminders = reminderModule.createPlanningReminder();
      reminders.alarmAudio = policyAudio();
      reminders.timerAudio = policyAudio();
      inGesture = true;
      reminders.unlockAudio();  // 首个手势：静音解锁发生在业务元素本人身上
      inGesture = false;
      await flushRings();
      assert.equal(reminders.alarmAudio.unlocked, true, '闹钟元素已获手势授权');
      assert.equal(reminders.timerAudio.unlocked, true, '计时器元素已获手势授权');
      assert.equal(reminders.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '解锁后挂真实铃声预加载');
      const now = Date.now();
      reminders.checkAlarms({ progress: [{ id: 200, alarm_start: true, alarm_end: false, timer_minutes: null,
        est_start: new Date(now).toISOString(), content: '响铃' }] });
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false, '到点无手势直接自动播放');
      assert.equal(reminders.alarmAudio.calls.at(-1).src, '/admin/assets/audio/alarm-clock.mp3', '到点播出真实铃声');
      assert.equal(reminders.pendingRecovery, null, '不要求「恢复响铃」');
      assert.equal(toastMessages.includes(hint), false, '正常路径不弹恢复提示');
      assert.equal(reminders.alarmAudio.loop, true, '闹钟循环');
      // 计时器到点同样无手势直接自动播放（单次，不循环）
      reminders.checkAlarms({ progress: [{ id: 201, alarm_start: false, alarm_end: false,
        timer_minutes: 1, status: 'in_progress',
        actual_start: new Date(now - 60 * 1000).toISOString(), content: '计时' }] });
      await flushRings();
      assert.equal(reminders.timerAudio.paused, false, '计时器到点无手势直接自动播放');
      assert.equal(reminders.timerAudio.calls.at(-1).src, '/admin/assets/audio/timer-done.ogg', '计时器播出真实铃声');
      assert.equal(reminders.timerAudio.loop, false, '计时器单次播放');
      assert.equal(reminders.pendingRecovery, null);
      reminders.dispose();
      // 对照：从未手势播放的元素在模型下被拒绝（授权不共享）
      const stranger = policyAudio();
      await stranger.play().then(
        () => assert.fail('未授权元素不应放行'),
        (error) => assert.equal(error.name, 'NotAllowedError'));
      assert.equal(stranger.paused, true);
    }

    // #33：解锁 play 迟到结算不影响真实响铃——真实起播接管（haltAudio）使
    // 解锁令牌失效，拒绝路径静默失效；真实起播自身的音源挂载与播放不受影响
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'deferred';
      reminders.unlockAudio();  // 解锁 play 挂起（静音素材）
      reminders.showRingModal('真实响铃', '12:00');  // 到点真实起播接管
      await flushRings();
      assert.ok(reminders.alarmAudio.calls[0].src.startsWith('data:audio/wav'), '解锁播放静音素材');
      assert.equal(reminders.alarmAudio.calls[1].src, '/admin/assets/audio/alarm-clock.mp3', '真实起播挂真实铃声');
      assert.equal(reminders.alarmAudio.paused, false);
      reminders.alarmAudio.pending[0].resolve('');  // 真实起播结算
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false);
      reminders.dispose();
    }

    // #33：解锁 play 的迟到成功结算不得暂停已接管的真实响铃（显式结算替身：
    // pause 不拒绝在途 play，使「解锁成功结算晚于真实起播」可构造）
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      const controlled = () => ({
        src: '', ringSource: null, unlocked: false, loop: false,
        paused: true, plays: 0, calls: [], jobs: [],
        play() {
          this.plays += 1;
          this.calls.push({ src: this.src });
          this.paused = false;
          return new Promise((resolve) => this.jobs.push(resolve));
        },
        pause() { this.paused = true; },  // 结算由用例显式控制，不拒绝在途 play
        load() {},
      });
      reminders.alarmAudio = controlled();
      reminders.timerAudio = controlled();
      reminders.unlockAudio();  // 解锁 play 挂起
      reminders.showRingModal('真实响铃', '12:00');  // 真实起播接管（令牌失效）
      await flushRings();
      assert.equal(reminders.alarmAudio.calls[1].src, '/admin/assets/audio/alarm-clock.mp3');
      assert.equal(reminders.alarmAudio.paused, false, '真实响铃播放中');
      reminders.alarmAudio.jobs[0]();  // 迟到的解锁成功结算
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false, '迟到解锁结算不暂停真实响铃');
      reminders.dispose();
    }

    // #29：停止按钮、右上角 ×、遮罩关闭都会停止音频并清理弹窗
    for (const route of ['stop-button', 'close-button', 'mask']) {
      const reminders = reminderModule.createPlanningReminder();
      reminders.showRingModal('闹钟', '12:00');
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false);
      const mask = masks.at(-1);
      if (route === 'stop-button') lastModal().querySelector('[data-act-ring-stop]').onclick();
      else if (route === 'close-button') lastModal().querySelector('.modal-close').onclick();
      else maskClick(mask);
      await flushRings();
      assert.equal(mask.removed, true);
      assert.equal(reminders.alarmAudio.paused, true);
      assert.equal(reminders.timerAudio.paused, true);
      assert.equal(reminders.ringModal, null);
      reminders.dispose();
    }

    // #30：起播被拦截后按提示点击页面即可恢复；连续拒绝可继续重试；
    // 不重复创建弹窗或通知；轮询重查不重复触发
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      const now = Date.now();
      const occ = { id: 5, alarm_start: true, est_start: new Date(now).toISOString(), content: '响铃' };
      reminders.checkAlarms({ progress: [occ] });
      await flushRings();
      const modalCount = masks.length;
      const notifyCount = NotificationFixture.created;
      assert.equal(reminders.alarmAudio.paused, true);
      assert.equal(reminders.firedKeys.size, 1);
      assert.ok(reminders.pendingRecovery);
      assert.equal(toastMessages.at(-1), hint);
      assert.equal(listenerCount('pointerdown'), 2);  // 未消费的预热 + 恢复监听
      await gesture();  // 第一次手势仍被拒
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, true);
      assert.ok(reminders.pendingRecovery);
      reminders.alarmAudio.behavior = 'success';
      await gesture();  // 再次手势重试成功
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false);
      assert.equal(reminders.pendingRecovery, null);
      assert.equal(listenerCount('pointerdown'), 0);  // 成功后恢复监听移除
      reminders.checkAlarms({ progress: [occ] });
      await flushRings();
      assert.equal(masks.length, modalCount);
      assert.equal(NotificationFixture.created, notifyCount);
      assert.equal(reminders.alarmAudio.paused, false);
      reminders.dispose();
    }

    // #30/#33 补充：主动停止后待恢复取消；后续手势对「已挂真实铃声但未解锁」
    // 的元素（未解锁时的到点尝试先挂了铃声）安全补做静音解锁——解锁播放先把
    // 音源换回静音素材，绝不播出真实铃声，结算后恢复挂真实铃声
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      const now = Date.now();
      const occ = { id: 9, alarm_start: true, est_start: new Date(now).toISOString(), content: '响铃' };
      reminders.checkAlarms({ progress: [occ] });
      await flushRings();
      assert.equal(reminders.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '到点尝试已挂真实铃声');
      reminders.alarmAudio.behavior = 'success';
      reminders.stopRinging();
      await flushRings();
      assert.equal(reminders.pendingRecovery, null);
      assert.equal(reminders.ringModal, null);
      const playsBefore = reminders.alarmAudio.plays;
      await gesture();
      await flushRings();
      assert.equal(reminders.alarmAudio.plays, playsBefore + 1, '手势只补做一次静音解锁');
      assert.ok(reminders.alarmAudio.calls.at(-1).src.startsWith('data:audio/wav'), '解锁播放静音素材，不泄漏真实铃声');
      assert.notEqual(reminders.alarmAudio.calls.at(-1).src, '/admin/assets/audio/alarm-clock.mp3');
      assert.equal(reminders.alarmAudio.paused, true, '解锁结算收尾');
      assert.equal(reminders.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '解锁后恢复挂真实铃声');
      assert.equal(reminders.ringModal, null);
      reminders.dispose();
    }

    // #31：同刻多项提醒复用同一弹窗合并展示；先后到点、闹钟计时器交错
    // 均无孤立弹窗；卸载清理全部弹窗
    {
      const reminders = reminderModule.createPlanningReminder();
      const before = masks.length;
      reminders.showRingModal('第一项闹钟', '12:00');
      await flushRings();
      reminders.showRingModal('第二项计时', '12:01', true);
      await flushRings();
      assert.equal(masks.length - before, 1);
      const listHtml = reminders.ringModal.listEl.innerHTML;
      assert.ok(listHtml.includes('第一项闹钟'));
      assert.ok(listHtml.includes('第二项计时'));
      assert.ok(listHtml.includes('ring-divider'));
      assert.equal(reminders.alarmAudio.paused, true);   // 新提醒接管音频
      assert.equal(reminders.timerAudio.paused, false);
      lastModal().querySelector('[data-act-ring-stop]').onclick();
      assert.equal(masks.at(-1).removed, true);
      assert.equal(reminders.ringModal, null);
      assert.deepEqual(reminders.ringItems, []);
      assert.equal(reminders.timerAudio.paused, true);

      reminders.showRingModal('闹钟A', '12:00');
      await flushRings();
      reminders.showRingModal('计时B', '12:05', true);
      await flushRings();
      assert.equal(masks.length - before, 2);
      reminders.dispose();
      assert.equal(masks.slice(before).filter((mask) => !mask.removed).length, 0);
    }
  },

  async 'B8-ring-recovery-identity'() {
    const flushRings = async () => { for (let count = 0; count < 12; count += 1) await Promise.resolve(); };
    const hint = '浏览器拦截了自动响铃，点击「恢复响铃」按钮即可恢复';
    const nowIso = () => new Date(Date.now()).toISOString();
    const recoverButton = () => lastModal().querySelector('[data-act-ring-recover]');
    const alarmOcc = (id) => ({ id, alarm_start: true, alarm_end: false, timer_minutes: null,
      est_start: nowIso(), content: '响铃' });
    const hintToasts = () => toastMessages.filter((m) => m === hint).length;

    // #29-A：真实播放未结算时经过停止按钮 / × / 遮罩 / 路由卸载；pause 中断
    // 引发的迟到 AbortError 拒绝不重新登记待恢复、不重注册监听、不复活旧响铃
    for (const route of ['stop-button', 'close-button', 'mask', 'dispose']) {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'deferred';
      reminders.checkAlarms({ progress: [alarmOcc(100)] });
      await flushRings();
      assert.ok(reminders.ringModal);
      assert.equal(reminders.pendingRecovery, null, '播放未结算不预先登记待恢复');
      const mask = masks.at(-1);
      if (route === 'stop-button') lastModal().querySelector('[data-act-ring-stop]').onclick();
      else if (route === 'close-button') lastModal().querySelector('.modal-close').onclick();
      else if (route === 'mask') maskClick(mask);
      else reminders.dispose();
      await flushRings();  // pause 使未结算的 play 迟到拒绝
      reminders.alarmAudio.behavior = 'success';  // 手势补做的静音解锁可正常结算
      assert.equal(reminders.pendingRecovery, null, '迟到拒绝不登记待恢复');
      assert.equal(reminders.recoveryHandler, null, '迟到拒绝不重注册恢复监听');
      assert.equal(reminders.ringModal, null);
      assert.equal(mask.removed, true);
      assert.equal(reminders.alarmAudio.paused, true);
      assert.equal(listenerCount('pointerdown'), route === 'dispose' ? 0 : 1,
        '只剩未消费的预热手势监听');
      await gesture();  // 再次触摸页面
      await flushRings();
      if (route === 'dispose') {
        assert.equal(reminders.alarmAudio.calls.length, 1, '卸载后手势无监听，不触发任何播放');
      } else {
        assert.equal(reminders.alarmAudio.calls.length, 2, '真实起播一次 + 静音解锁一次');
        assert.ok(reminders.alarmAudio.calls[1].src.startsWith('data:audio/wav'), '手势补做的解锁播放静音素材');
        assert.notEqual(reminders.alarmAudio.calls[1].src, '/admin/assets/audio/alarm-clock.mp3', '手势不播出真实铃声');
        assert.equal(reminders.alarmAudio.ringSource, '/admin/assets/audio/alarm-clock.mp3', '解锁后恢复挂真实铃声');
      }
      assert.equal(reminders.pendingRecovery, null);
      assert.equal(reminders.ringModal, null, '旧提醒不复活');
      reminders.dispose();
    }

    // #29-B1：旧闹钟播放未结算时被新计时器接管；旧回调拒绝不改写新提醒状态，
    // 手势不播放旧音轨，不产生两条并播
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'deferred';
      reminders.showRingModal('旧闹钟', '12:00');
      reminders.showRingModal('新计时器', '12:01', true);  // 接管：pause 中断旧播放
      await flushRings();
      assert.equal(reminders.pendingRecovery, null, '旧闹钟迟到拒绝不覆盖新提醒');
      assert.equal(reminders.recoveryHandler, null);
      assert.equal(reminders.timerAudio.paused, false);
      assert.equal(reminders.alarmAudio.paused, true);
      reminders.alarmAudio.behavior = 'success';
      await gesture();  // 无待恢复：手势不播放旧闹钟
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, true, '旧闹钟不复活');
      assert.equal(reminders.timerAudio.paused, false, '新计时器持续播放，不并播');
      reminders.dispose();
    }

    // #29-B2：旧计时器播放未结算时被新闹钟接管，同样互不污染
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.ensureAudio();
      reminders.timerAudio.behavior = 'deferred';
      reminders.showRingModal('旧计时器', '12:00', true);
      reminders.showRingModal('新闹钟', '12:01');
      await flushRings();
      assert.equal(reminders.pendingRecovery, null);
      assert.equal(reminders.alarmAudio.paused, false);
      assert.equal(reminders.timerAudio.paused, true);
      reminders.dispose();
    }

    // #29-C：恢复重试在途时主动停止，迟到的重试拒绝不弹过期提示、不复活状态
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      reminders.checkAlarms({ progress: [alarmOcc(101)] });
      await flushRings();
      assert.ok(reminders.pendingRecovery);
      const toastsBefore = hintToasts();
      reminders.alarmAudio.behavior = 'deferred';
      const inFlight = gesture();
      await flushRings();  // 让恢复重试真正进入在途（play 未结算）
      reminders.stopRinging();  // pause 中断在途重试
      await flushRings();
      await inFlight;
      assert.equal(reminders.pendingRecovery, null);
      assert.equal(reminders.recoveryHandler, null);
      assert.equal(reminders.ringModal, null);
      assert.equal(hintToasts(), toastsBefore, '过期拒绝不重复弹恢复提示');
      reminders.dispose();
    }

    // #29-D：恢复重试在途时新提醒接管，旧重试结果不覆盖新提醒状态
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      reminders.checkAlarms({ progress: [alarmOcc(102)] });
      await flushRings();
      const toastsBefore = hintToasts();
      reminders.alarmAudio.behavior = 'deferred';
      const inFlight = gesture();
      await flushRings();  // 让恢复重试真正进入在途（play 未结算）
      reminders.showRingModal('新计时器', '12:01', true);  // 接管并中断旧重试
      await flushRings();
      await inFlight;
      assert.equal(reminders.pendingRecovery, null, '新计时器播放成功，旧重试拒绝不改写');
      assert.equal(reminders.timerAudio.paused, false);
      assert.equal(hintToasts(), toastsBefore, '被取代的重试失败不弹过期提示');
      reminders.dispose();
    }

    // #29-E：旧提醒被拦截后新音轨接管且慢加载——接管立即取消旧待恢复数据、
    // 监听与入口；新音轨结算前恢复入口不可用，残留入口与手势都不起播旧音轨
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      reminders.checkAlarms({ progress: [alarmOcc(106)] });
      await flushRings();
      assert.equal(recoverButton().hidden, false);
      reminders.timerAudio.behavior = 'deferred';  // 新音轨慢加载，play 未结算
      reminders.showRingModal('新计时器', '12:01', true);
      await flushRings();
      assert.equal(reminders.pendingRecovery, null, '接管立即取消旧待恢复数据');
      assert.equal(reminders.recoveryHandler, null, '接管移除旧恢复监听');
      assert.equal(recoverButton().hidden, true, '新音轨结算前恢复入口隐藏');
      recoverButton().onclick();  // 残留入口误触也不得恢复被取代音轨
      await flushRings();
      assert.equal(reminders.alarmAudio.plays, 1, '旧音轨不因残留入口起播');
      assert.equal(reminders.alarmAudio.paused, true);
      reminders.alarmAudio.behavior = 'success';
      await gesture();  // 页面手势同样不得复活被接管的旧音轨
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, true);
      reminders.timerAudio.pending[0].resolve('');  // 新音轨随后加载完成
      await flushRings();
      assert.equal(reminders.timerAudio.paused, false, '新音轨正常播放');
      assert.equal(reminders.alarmAudio.paused, true, '不并播');
      reminders.dispose();
    }

    // #29-F：接管后新音轨也被拦截——只登记当前代次的新音轨，入口仅恢复新音轨
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      reminders.checkAlarms({ progress: [alarmOcc(107)] });
      await flushRings();
      reminders.timerAudio.behavior = 'blocked';
      reminders.showRingModal('新计时器', '12:01', true);
      await flushRings();
      assert.equal(reminders.pendingRecovery?.once, true, '新音轨被拦截后登记当前代次音轨');
      assert.equal(recoverButton().hidden, false, '新音轨被拦截后入口显示');
      reminders.timerAudio.behavior = 'success';
      recoverButton().onclick();
      await flushRings();
      assert.equal(reminders.timerAudio.paused, false, '按钮只恢复新音轨');
      assert.equal(reminders.alarmAudio.paused, true, '被取代的旧音轨不并播');
      reminders.dispose();
    }

    // #29-G：反方向（旧计时器被拦截→新闹钟慢加载接管）同样成立
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.timerAudio.behavior = 'blocked';
      const occ = { id: 108, timer_minutes: 1, status: 'in_progress',
        actual_start: new Date(Date.now() - 60 * 1000).toISOString(), content: '旧计时器' };
      reminders.checkAlarms({ progress: [occ] });
      await flushRings();
      assert.equal(recoverButton().hidden, false);
      reminders.alarmAudio.behavior = 'deferred';
      reminders.showRingModal('新闹钟', '12:01');
      await flushRings();
      assert.equal(reminders.pendingRecovery, null, '接管立即取消旧待恢复数据');
      assert.equal(recoverButton().hidden, true);
      reminders.alarmAudio.pending[0].resolve('');  // 新闹钟加载完成
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false, '新闹钟正常播放');
      assert.equal(reminders.timerAudio.paused, true, '旧计时器不并播');
      reminders.dispose();
    }

    // #29-H：新待恢复已登记后，旧起播的迟到成功结算不得清空新状态。
    // 旧 / 新音轨用结算显式控制的替身（pause 不拒绝在途 play），使
    // 「接管并登记新待恢复 → 旧成功回调晚到」的时序可构造（复审 R2：
    // 原排列下旧成功先于新拒绝结算，保护被移除时断言仍通过，捕捉不到）
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      const controlled = (mode) => ({ paused: true, plays: 0, muted: false, currentTime: 0,
        src: '', ringSource: null, loop: false, jobs: [], mode,
        play() {
          this.plays += 1;
          if (this.mode === 'blocked') return Promise.reject(new Error('NotAllowedError'));
          this.paused = false;
          return new Promise((resolve, reject) => this.jobs.push({ resolve, reject }));
        },
        pause() { this.paused = true; },  // 结算由用例显式控制，不拒绝在途 play
        load() {},
      });
      reminders.alarmAudio = controlled('deferred');
      reminders.alarmAudio.loop = true;
      reminders.timerAudio = controlled('blocked');
      reminders.showRingModal('旧闹钟', '12:00');  // 旧起播 play 未结算
      reminders.showRingModal('新计时器', '12:01', true);  // 接管且新音轨被拦截
      await flushRings();
      assert.equal(reminders.pendingRecovery?.once, true, '前置：新音轨被拦截已登记当前代次待恢复');
      const newEpoch = reminders.ringEpoch;
      reminders.alarmAudio.jobs[0].resolve();  // 旧成功回调晚于新登记到达
      await flushRings();
      assert.equal(reminders.pendingRecovery?.once, true, '迟到成功不清空新提醒待恢复状态');
      assert.equal(reminders.pendingRecovery.epoch, newEpoch, '待恢复仍属于当前代次');
      assert.equal(reminders.alarmAudio.paused, true, '旧音轨保持停止');
      reminders.dispose();
    }

    // #30-A：起播被拦截后弹窗显示「恢复响铃」入口与一致文案；遮罩区域的按下
    // 不作为恢复手势（遮罩保持关闭语义，不能先恢复再停止）
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      const occ = alarmOcc(103);
      const toastsBefore = hintToasts();
      reminders.checkAlarms({ progress: [occ] });
      await flushRings();
      const notifyCount = NotificationFixture.created;
      const mask = masks.at(-1);
      assert.ok(reminders.pendingRecovery);
      assert.equal(recoverButton().hidden, false, '被拦截后显示恢复入口');
      assert.equal(lastModal().querySelector('[data-ring-hint]').hidden, false, '提示文案同步显示');
      assert.equal(hintToasts(), toastsBefore + 1, '被拦截时弹出指向恢复入口的提示');
      assert.ok(reminders.firedKeys.size >= 1);
      await gesture({ target: mask });  // 遮罩上的按下：交给关闭语义，不重试播放
      await flushRings();
      assert.equal(reminders.alarmAudio.plays, 1, '遮罩按下不先恢复');
      maskClick(mask);
      await flushRings();
      assert.equal(mask.removed, true);
      assert.equal(reminders.alarmAudio.plays, 1, '遮罩关闭只停止，不附带起播');
      assert.equal(reminders.ringModal, null);
      reminders.dispose();
    }

    // #30-B：点击「恢复响铃」恢复循环闹钟，弹窗保留；连续拒绝可继续重试；
    // 成功后入口隐藏、恢复监听移除，不重复通知
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.alarmAudio.behavior = 'blocked';
      reminders.checkAlarms({ progress: [alarmOcc(104)] });
      await flushRings();
      const notifyCount = NotificationFixture.created;
      const maskCount = masks.length;
      reminders.alarmAudio.behavior = 'blocked';
      recoverButton().onclick();  // 第一次点击恢复仍被拒
      await flushRings();
      assert.ok(reminders.pendingRecovery, '连续拒绝后保留待恢复，可继续重试');
      assert.equal(recoverButton().hidden, false);
      reminders.alarmAudio.behavior = 'success';
      recoverButton().onclick();  // 再次点击恢复成功
      await flushRings();
      assert.equal(reminders.alarmAudio.paused, false);
      assert.equal(reminders.alarmAudio.loop, true, '闹钟持续循环');
      assert.equal(reminders.pendingRecovery, null);
      assert.equal(recoverButton().hidden, true, '恢复成功后入口隐藏');
      assert.equal(lastModal().querySelector('[data-ring-hint]').hidden, true);
      assert.ok(reminders.ringModal, '恢复后弹窗保留');
      assert.equal(masks.length, maskCount, '不另开新弹窗');
      assert.equal(NotificationFixture.created, notifyCount, '不重复通知');
      assert.equal(listenerCount('pointerdown'), 1, '成功后移除恢复监听');
      reminders.dispose();
    }

    // #30-C：计时器被拦截同样经入口恢复，计时器只播一次（不循环）
    {
      const reminders = reminderModule.createPlanningReminder();
      reminders.attach();
      reminders.ensureAudio();
      reminders.timerAudio.behavior = 'blocked';
      const occ = { id: 105, timer_minutes: 1, status: 'in_progress',
        actual_start: new Date(Date.now() - 60 * 1000).toISOString(), content: '计时器' };
      reminders.checkAlarms({ progress: [occ] });
      await flushRings();
      assert.equal(reminders.pendingRecovery?.once, true, '计时器被拦截登记待恢复');
      reminders.timerAudio.behavior = 'success';
      recoverButton().onclick();
      await flushRings();
      assert.equal(reminders.timerAudio.paused, false);
      assert.equal(reminders.timerAudio.loop, false, '计时器只播一次');
      assert.equal(reminders.pendingRecovery, null);
      reminders.dispose();
    }
  },

  async 'C1-visible-refresh'() {
    respond = async ({ url, options }) => response(options.method ? { id: 10 }
      : url.includes('/today') ? emptyBoard() : []);
    await page.mount(new Element());
    requests.length = 0;
    await fillCreate().onclick();
    assert.deepEqual(readPaths(), ['/admin/api/planning/today']);
    assert.equal(page.reads.needsRead('today'), false);
    assert.equal(page.reads.needsRead('tasks'), true);
    assert.equal(page.reads.needsRead('occurrences'), true);

    requests.length = 0;
    page.switchTab('all');
    await page.refreshVisible();
    assert.deepEqual(readPaths().sort(), ['/admin/api/planning/occurrences', '/admin/api/planning/tasks']);
    assert.equal(page.reads.needsRead('tasks'), false);
    assert.equal(page.reads.needsRead('occurrences'), false);
    requests.length = 0;
    await fillCreate().onclick();
    assert.deepEqual(readPaths().sort(), ['/admin/api/planning/occurrences', '/admin/api/planning/tasks']);
    assert.equal(page.reads.needsRead('today'), true);

    requests.length = 0;
    page.switchTab('today');
    await page.refreshVisible();
    assert.deepEqual(readPaths(), ['/admin/api/planning/today']);
    page.switchTab('goals');
    requests.length = 0;
    await fillCreate().onclick();
    assert.deepEqual(readPaths(), []);
    assert.equal(page.reads.needsRead('tasks'), true);
    requests.length = 0;
    page.switchTab('all');
    await page.refreshVisible();
    assert.deepEqual(readPaths().sort(), ['/admin/api/planning/occurrences', '/admin/api/planning/tasks']);
    // The reminder keeps reading today while another tab is visible.
    requests.length = 0;
    [...intervals.values()][0].fn();
    await flush();
    assert.deepEqual(readPaths(), ['/admin/api/planning/today']);
    assert.equal([...intervals.values()][0].ms, 30000);
    page.unmount();
  },

  async 'C2-stale-responses'() {
    respond = async () => response(emptyBoard());
    await page.mount(new Element());
    requests.length = 0;
    const queue = [];
    respond = ({ options }) => options.method ? Promise.resolve(response({ id: 20 }))
      : new Promise((resolve) => queue.push(resolve));
    const oldPoll = page.loadToday({ silent: true });
    const samePoll = page.loadToday({ silent: true });
    assert.equal(queue.length, 1, 'identical in-flight polling shares a read');
    const button = fillCreate();
    const saved = button.onclick();
    await flush();
    assert.equal(queue.length, 2, 'a successful write immediately begins a new read');
    const joinedFresh = page.loadToday({ silent: true });
    assert.equal(queue.length, 2, 'polling after the write shares the fresh read');
    queue[1](response(boardWith(20, '新列表')));
    await Promise.all([saved, joinedFresh]);
    const newHtml = page.root.querySelector('#planning-progress').innerHTML;
    assert.equal(page.board.progress[0].id, 20);
    queue[0](response(boardWith(1, '旧列表')));
    await Promise.all([oldPoll, samePoll]);
    assert.equal(page.board.progress[0].id, 20);
    assert.equal(page.root.querySelector('#planning-progress').innerHTML, newHtml);

    // Changing filters is also a new revision, even when the older request is slow.
    queue.length = 0;
    page.filters.status = 'pending';
    const oldFilter = page.loadOccurrences();
    const sameFilter = page.loadOccurrences();
    assert.equal(queue.length, 1);
    page.filters.status = 'completed';
    const newFilter = page.loadOccurrences();
    assert.equal(queue.length, 2);
    queue[1](response([{ id: 22, content: '已完成', status: 'completed' }]));
    await newFilter;
    queue[0](failure('old filter failed'));
    await Promise.all([oldFilter, sameFilter]);
    assert.equal(page.occurrences[0].id, 22);
    assert.equal(page.readErrors.has('occurrences'), false);
    page.unmount();
  },

  async 'C3-committed-refresh-retry'() {
    respond = async () => response(boardWith(1, '已有列表'));
    await page.mount(new Element());
    const previousHtml = page.root.querySelector('#planning-progress').innerHTML;
    requests.length = 0;
    respond = async ({ options }) => options.method ? response({ id: 30, schedule_conflict: true })
      : failure('refresh failed');
    const button = fillCreate();
    await button.onclick();
    assert.equal(button.disabled, true);
    assert.equal(button.textContent, '已创建');
    assert.equal(page.root.querySelector('#planning-progress').innerHTML, previousHtml);
    const feedback = page.root.querySelector('#planning-load-feedback');
    assert.equal(feedback.hidden, false);
    assert.ok(feedback.innerHTML.includes('待办已创建，列表更新失败'));
    assert.ok(feedback.innerHTML.includes('data-act="retry-lists"'));
    assert.ok(toastMessages.includes('待办已创建，但可安排时段剩余空间不足，存在排程冲突'));
    assert.ok(!toastMessages.some((message) => message.startsWith('保存失败')));
    await button.onclick();
    assert.equal(requests.filter(({ options }) => options.method === 'POST').length, 1);
    respond = async () => response(boardWith(30, '重试后列表'));
    await page.retryLists();
    assert.equal(feedback.hidden, true);
    assert.equal(page.board.progress[0].id, 30);
    assert.equal(button.disabled, true);
    assert.equal(requests.filter(({ options }) => options.method === 'POST').length, 1);
    assert.equal(page.reads.needsRead('today'), false);
    page.unmount();
  },

  async 'C4-inflight-unmount'() {
    let finishMount;
    respond = () => new Promise((resolve) => { finishMount = resolve; });
    const oldRoot = new Element();
    const mounting = page.mount(oldRoot);
    page.unmount();
    const before = oldRoot.querySelector('#planning-progress').innerHTML;
    finishMount(response(boardWith(1, '已卸载页面')));
    await mounting;
    assert.equal(oldRoot.querySelector('#planning-progress').innerHTML, before);
    assert.equal(intervals.size, 0, 'an unmounted initial read cannot recreate the poll timer');
    assert.equal(listenerCount('beforeunload'), 0);

    respond = async () => response(emptyBoard());
    await page.mount(new Element());
    let finishOld;
    respond = () => new Promise((resolve) => { finishOld = resolve; });
    const old = page.loadToday();
    page.unmount();
    respond = async () => response(boardWith(44, '重新挂载'));
    await page.mount(new Element());
    const currentHtml = page.root.querySelector('#planning-progress').innerHTML;
    finishOld(failure('old route failed'));
    await old;
    assert.equal(page.board.progress[0].id, 44);
    assert.equal(page.root.querySelector('#planning-progress').innerHTML, currentHtml);
    assert.equal(page.root.querySelector('#planning-load-feedback').hidden, true);
    assert.equal(intervals.size, 1);
    page.unmount();
  },

  async 'C5-closed-pending-form'() {
    respond = async () => response(emptyBoard());
    await page.mount(new Element());
    let finishCreate;
    let created = false;
    respond = ({ url, options }) => options.method === 'POST'
      ? new Promise((resolve) => { finishCreate = resolve; }) : Promise.resolve(response(
        url.includes('/today') ? created ? boardWith(55, '重挂载后创建成功') : emptyBoard() : []));
    const button = fillCreate();
    const save = button.onclick();
    lastModal().querySelector('[data-cancel]').onclick();
    const count = masks.length;
    page.openTaskForm(null);
    assert.equal(masks.length, count, 'closing a pending form cannot open another create');
    assert.ok(toastMessages.includes('待办正在创建，请等待完成'));
    page.unmount();
    await page.mount(new Element());
    page.openTaskForm(null);
    assert.equal(masks.length, count, 'route remount keeps the pending create guard');
    page.switchTab('all');
    await page.refreshVisible();
    assert.equal(page.reads.needsRead('tasks'), false);
    page.switchTab('today');
    await page.refreshVisible();
    created = true;
    finishCreate(response({ id: 55 }));
    await save;
    assert.equal(page.board.progress[0].id, 55, 'the committed write refreshes the currently mounted page');
    assert.equal(page.reads.needsRead('tasks'), true);
    assert.equal(page.reads.needsRead('occurrences'), true);
    page.openTaskForm(null);
    assert.equal(masks.length, count + 1, 'a committed create may be followed by a new form');
    page.unmount();
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
