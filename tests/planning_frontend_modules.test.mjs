import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

const workspace = process.argv[2];
const group = process.argv[3];
const version = (await readFile(join(workspace, 'admin/js/ui.js'), 'utf8')).match(/ASSET_VERSION = '([^']+)'/)[1];
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
    this.attributes = new Map();
    this.tabIndex = 0;
    this.isConnected = true;
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
    if (!this.nodes.has(selector)) {
      const node = new Element();
      node.parentElement = this;
      this.nodes.set(selector, node);
    }
    return this.nodes.get(selector);
  }
  querySelectorAll(selector) { return this.lists.get(selector) || []; }
  appendChild(child) {
    this.children.push(child);
    if (child.className === 'modal-mask') masks.push(child);
  }
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
  }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  insertAdjacentHTML() {}
  scrollIntoView() {}
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { this.attributes.delete(name); }
  closest() { return null; }
  contains(node) { return node === this || [...this.nodes.values()].some(child => child.contains(node)); }
  focus() { document.activeElement = this; }
  getClientRects() { return this.hidden || this.removed ? [] : [this.getBoundingClientRect()]; }
  remove() { this.removed = true; this.isConnected = false; }
  setPointerCapture(id) { this.captured = id; }
  getBoundingClientRect() { return { top: 0, height: 40 }; }
}

class AudioFixture {
  constructor(url) { this.url = url; this.currentTime = 0; this.plays = 0; this.pauses = 0; }
  play() { this.plays += 1; return Promise.resolve(); }
  pause() { this.pauses += 1; }
}

class NotificationFixture {
  static permission = 'granted';
  static requestPermission() { return Promise.resolve(this.permission); }
  constructor(title, options) { this.title = title; this.options = options; }
}

globalThis.document = new Element();
document.body = new Element();
document.activeElement = document.body;
document.createElement = () => new Element();
document.getElementById = () => null;
// These fixtures test API retry/state behavior; real observer/focus interactions
// are exercised by frontend_controls.browser.cjs in an actual browser.
globalThis.MutationObserver = class { observe() {} disconnect() {} };
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
const ui = await import(moduleUrl('ui.js'));
const lastModal = () => masks.at(-1).querySelector('.modal');
const response = (body = {}) => ({ ok: true, json: async () => body });
const failure = (error) => ({ ok: false, status: 409, statusText: 'Conflict', json: async () => ({ error }) });
const listenerCount = (name) => windowListeners.get(name)?.size || 0;
const emptyBoard = () => ({ progress: [], attention: [], done: [], conflicts: [], recompute: {} });

const checks = {
  async graph() {
    assert.equal(typeof page.mount, 'function');
    assert.equal(typeof page.unmount, 'function');
    assert.equal(typeof formModule.openTaskForm, 'function');
    assert.equal(typeof dialogModule.createPlanningDialogs, 'function');
    assert.equal(typeof sortModule.createPlanningSort, 'function');
    assert.equal(typeof reminderModule.createPlanningReminder, 'function');
    assert.equal(ui.ASSET_VERSION, version);
    for (const file of [
      'pages/planning.js', 'lib/planning_display.js', 'lib/planning_task_form.js',
      'lib/planning_dialogs.js', 'lib/planning_sort.js', 'lib/planning_reminder.js',
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
    resolveRequest(failure('first request failed'));
    await first;
    assert.equal(button.disabled, false);
    respond = async () => response({ first_round_skipped: true });
    await button.onclick();
    assert.equal(saved, 1);
    assert.equal(button.disabled, true);
    assert.equal(masks.at(-1).removed, true);
    assert.ok(toastMessages.includes('本轮已过最晚完成，从次日起按重复规则生效'));
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
    reminders.unlockAudio();
    await Promise.resolve();
    assert.equal(reminders.alarmAudio.loop, true);
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
};

try {
  assert.ok(checks[group], `unknown test group: ${group}`);
  await checks[group]();
  process.stdout.write(`PASS ${group}\n`);
} catch (error) {
  process.stderr.write(`${group}: ${error.stack}\n`);
  process.exitCode = 1;
}
