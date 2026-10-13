// Native production imports with deterministic DOM/events; browser styling is checked separately.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Element, document, dispatch, flush, key, UiEvent } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2] || process.cwd();
const uiSource = await readFile(join(workspace, 'admin/js/ui.js'), 'utf8');
const version = uiSource.match(/ASSET_VERSION = '([^']+)'/)[1];
const load = path => import(pathToFileURL(join(workspace, `admin/js/${path}`)).href + `?v=${version}`);
const { createRetroTimeField } = await load('lib/retro_time.js');
const { modal } = await load('ui.js');
const { createPlanningDialogs } = await load('lib/planning_dialogs.js');
const { openTaskForm } = await load('lib/planning_task_form.js');
const { createRetroSelectField } = await load('lib/retro_select.js');
const { openCycleSettings } = await load('lib/cycle_settings.js');

const actualNow = Date.now;
Date.now = () => Date.parse('2026-10-12T18:34:56Z');
globalThis.localStorage = { getItem: () => '', setItem() {}, removeItem() {} };
window.location = { origin: 'http://fixture.test' };
const requests = [];
let requestFailure = null;
let responsePayload = {};
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, ...options, body: JSON.parse(options.body || '{}') });
  if (requestFailure) return { ok: false, status: 422, statusText: 'Unprocessable Entity', json: async () => ({ error: requestFailure }) };
  return { ok: true, json: async () => responsePayload };
};

function mount(options, parent = document.body) {
  const field = parent.appendChild(new Element());
  field.className = 'field';
  field.innerHTML = '<label>日期时间</label><div class="time-host"></div>';
  const host = field.querySelector('.time-host');
  const input = createRetroTimeField(host, { id: 'test-time', ...options });
  return { field, host, input, button: host.querySelector('.retro-time-field') };
}
const picker = () => document.querySelector('.retro-time-pop');
const click = selector => picker().querySelector(selector).click();
// Buttons receive native activation on unhandled Enter/Space in real browsers.
const activate = value => { if (!key(value).defaultPrevented) document.activeElement.click(); };

const first = mount({ value: '2026-01-31T08:15' });
const same = createRetroTimeField(first.host, { id: 'test-time', value: 'ignored' });
assert.equal(same, first.input, 'repeated mounting keeps values and listeners');
assert.equal(first.button.getAttribute('aria-label'), '日期时间：2026年01月31日 08:15');
let inputs = 0;
let changes = 0;
first.input.addEventListener('input', () => { inputs++; });
first.input.addEventListener('change', () => { changes++; });
first.button.click();
assert.equal(document.activeElement.dataset.day, '31');
assert.equal(document.activeElement.getAttribute('aria-selected'), 'true');
key('ArrowRight');
assert.equal(picker().querySelector('.retro-time-title').textContent, '2026年2月');
assert.equal(document.activeElement.dataset.day, '1');
assert.equal(document.activeElement.getAttribute('aria-selected'), 'false');
assert.equal(first.input.value, '2026-01-31T08:15', 'date focus navigation cannot write a value');
key('PageDown');
assert.equal(picker().querySelector('.retro-time-title').textContent, '2026年3月');
key('Home');
assert.equal(document.activeElement.dataset.day, '23');
assert.equal(picker().querySelector('.retro-time-title').textContent, '2026年2月');
click('[data-act="ok"]');
assert.equal(first.input.value, '2026-01-31T08:15', 'unselected navigation preserves chosen date');
assert.equal(inputs, 1); assert.equal(changes, 0, 'time fields preserve input-only business events');
assert.equal(document.activeElement, first.button);

first.button.click(); key('ArrowRight'); activate('Enter');
const minute = picker().querySelector('[data-unit="minute"]').querySelector('.rtp-select-btn');
minute.click();
assert.equal(document.activeElement.dataset.value, '15');
key('End'); assert.equal(document.activeElement.dataset.value, '59');
assert.equal(first.input.value, '2026-01-31T08:15');
activate(' ');
assert.equal(minute.getAttribute('aria-expanded'), 'false');
assert.equal(document.activeElement, minute);
click('[data-act="ok"]');
assert.equal(first.input.value, '2026-02-01T08:59');
assert.equal(inputs, 2); assert.equal(changes, 0);

first.input._applyRetroValue('2026-04-03T10:20', true);
assert.equal(inputs, 2); assert.match(first.button.getAttribute('aria-label'), /10:20/);
first.button.click(); first.input._applyRetroValue('', false);
assert.equal(picker(), null, 'programmatic value changes cannot leave a stale open draft');
assert.equal(inputs, 3);
first.button.click(); click('[data-act="now"]'); click('[data-act="ok"]');
assert.equal(first.input.value, '2026-10-13T02:34', 'default clock is Shanghai in every process timezone');
let precision = 'minute';
first.input.addEventListener('input', () => { if (!first.input.value) precision = ''; });
first.button.click(); click('[data-act="clear"]');
assert.equal(first.input.value, ''); assert.equal(precision, '', 'memory clear listener still resets precision');

const local = mount({ id: 'local-time', clock: 'local' });
local.button.click(); click('[data-act="now"]'); click('[data-act="ok"]');
assert.equal(new Date(local.input.value).toISOString(), '2026-10-12T18:34:00.000Z', 'local mode preserves native planning instants');
const timeOnly = mount({ id: 'time-only', mode: 'time', value: '03:11:40' });
assert.match(timeOnly.button.getAttribute('aria-label'), /03:11/);
timeOnly.button.click();
assert.equal(picker().querySelector('.retro-time-grid'), null);
assert.equal(picker().querySelector('[data-unit="hour"]').querySelector('.rtp-select-text').textContent, '03');
key('Escape'); assert.equal(timeOnly.input.value, '03:11:40', 'opening/cancelling preserves stored seconds');
const dateOnly = mount({ id: 'date-only', mode: 'date', value: '2024-02-29' });
dateOnly.button.click(); key('PageDown', true);
assert.equal(document.activeElement.dataset.day, '28', 'year navigation clamps leap day');
activate('Enter'); click('[data-act="ok"]'); assert.equal(dateOnly.input.value, '2025-02-28');
const locked = mount({ id: 'locked', value: '2026-10-13T02:34', disabled: true });
locked.button.click(); assert.equal(picker(), null);
assert.equal(locked.input.disabled, true); assert.equal(locked.button.disabled, true);
const readonly = mount({ id: 'readonly', readonly: true });
readonly.button.click(); assert.equal(picker(), null);
const disabledGroup = document.body.appendChild(new Element('fieldset'));
const inherited = mount({ id: 'inherited-disabled' }, disabledGroup);
inherited.button.setAttribute('aria-describedby', 'persistent-lock-reason');
inherited.input.disabled = true; await flush();
assert.equal(inherited.button.getAttribute('aria-describedby'), 'persistent-lock-reason', 'input state sync preserves caller lock descriptions');
inherited.input.disabled = false; await flush(); disabledGroup.disabled = true;
inherited.button.click(); assert.equal(picker(), null, 'inherited fieldset disable blocks the visible trigger');

const outer = modal({ title: '时间', body: '<div data-host></div>', footer: '<button data-cancel>取消</button>' });
const nested = mount({ id: 'nested', value: '2026-10-13T02:34' }, outer.root.querySelector('[data-host]'));
await flush(); nested.button.click();
const hour = picker().querySelector('[data-unit="hour"]').querySelector('.rtp-select-btn');
hour.click(); key('ArrowDown');
assert.equal(document.activeElement.dataset.value, '03');
key('Escape'); assert.equal(document.activeElement, hour);
assert.ok(picker()); assert.ok(outer.mask.isConnected);
key('Tab');
assert.equal(document.activeElement.getAttribute('aria-label'), '分钟：34', 'unit Tab advances once within picker');
key('Escape'); assert.equal(picker(), null); assert.equal(document.activeElement, nested.button);
assert.ok(outer.mask.isConnected); key('Escape'); assert.equal(outer.mask.isConnected, false);
await flush();

// R1: scrollIntoView can move a short calendar as well as the focused list.
// Model that ancestor scroll event, then exercise the actual viewport listener.
const originalRect = Element.prototype.getBoundingClientRect;
const originalIntoView = Element.prototype.scrollIntoView;
const oldHeight = window.innerHeight;
const oldWidth = window.innerWidth;
let optionIntoViewCalls = 0;
Element.prototype.getBoundingClientRect = function () {
  if (this.matches('.retro-time-pop')) return { left: 24, top: 8, right: 282, bottom: 312, width: 258, height: 304 };
  if (this.matches('.rtp-select-pop')) {
    const top = Number.parseFloat(this.style.top) || 64;
    return { left: 24, top, right: 88, bottom: top + 120, width: 64, height: 120 };
  }
  if (this.matches('.rtp-select-option')) {
    const list = this.closest('.rtp-select-pop');
    const bounds = list.getBoundingClientRect();
    const top = bounds.top + 4 + Number(this.dataset.value) * 24 - list.scrollTop;
    return { left: 28, top, right: 84, bottom: top + 24, width: 56, height: 24 };
  }
  return originalRect.call(this);
};
Element.prototype.scrollIntoView = function (options) {
  if (this.matches('.rtp-select-option')) {
    optionIntoViewCalls++;
    if (this.dataset.value === '59') this.closest('.retro-time-pop').scrollTop += 12;
    return;
  }
  originalIntoView.call(this, options);
};
window.innerHeight = 320; window.innerWidth = 390;
const shortModal = modal({ title: '矮屏时分', body: '<div data-short-host></div>', footer: '<button data-cancel>取消</button>' });
const short = mount({ id: 'short-time', value: '2026-10-13T00:00' }, shortModal.root.querySelector('[data-short-host]'));
await flush(); short.button.click(); await flush();
let shortPop = picker();
let shortMinute = shortPop.querySelector('[data-unit="minute"]').querySelector('.rtp-select-btn');
let shortHour = shortPop.querySelector('[data-unit="hour"]').querySelector('.rtp-select-btn');
let shortList = shortPop.querySelector('[data-unit="minute"]').querySelector('.rtp-select-pop');
shortMinute.click(); await flush(); key('End'); await flush();
assert.equal(shortList.hidden, false, 'End remains open after real-style scroll event settlement');
assert.equal(shortPop.scrollTop, 0, 'option positioning cannot scroll the parent calendar');
assert.equal(document.activeElement.dataset.value, '59');
assert.ok(shortList.scrollTop > 0, 'End actually scrolls the options viewport');
const visible = shortList.getBoundingClientRect();
const optionBounds = document.activeElement.getBoundingClientRect();
assert.ok(optionBounds.top >= visible.top && optionBounds.bottom <= visible.bottom, 'last option is visible inside its own list');
activate('Enter'); await flush();
assert.equal(shortMinute.getAttribute('aria-label'), '分钟：59');
assert.equal(document.activeElement, shortMinute);
shortMinute.click(); key('Home'); await flush();
assert.equal(shortList.scrollTop, 4, 'Home scrolls only the list back to its first option');
key('ArrowDown'); key('End'); key('ArrowUp'); await flush();
assert.equal(document.activeElement.dataset.value, '58');
assert.equal(shortList.hidden, false); activate(' '); await flush();
assert.equal(shortMinute.getAttribute('aria-label'), '分钟：58');
assert.equal(document.activeElement, shortMinute);
shortHour.click(); key('End'); key('ArrowUp'); await flush();
assert.equal(document.activeElement.dataset.value, '22');
activate('Enter'); assert.equal(shortHour.getAttribute('aria-label'), '小时：22');
assert.equal(short.input.value, '2026-10-13T00:00', 'list selection does not submit a datetime');
click('[data-act="ok"]'); assert.equal(short.input.value, '2026-10-13T22:58');
assert.equal(optionIntoViewCalls, 0, 'keyboard and opening paths never invoke ancestor-scrolling option API');

short.button.click(); await flush(); shortPop = picker();
shortMinute = shortPop.querySelector('[data-unit="minute"]').querySelector('.rtp-select-btn');
shortHour = shortPop.querySelector('[data-unit="hour"]').querySelector('.rtp-select-btn');
shortList = shortPop.querySelector('[data-unit="minute"]').querySelector('.rtp-select-pop');
shortMinute.click(); key('End'); await flush();
shortPop.scrollTop = 12; await flush();
assert.equal(shortList.hidden, true, 'user calendar scrolling closes a fixed child list before it drifts');
assert.equal(document.activeElement, shortMinute, 'calendar scroll returns focus to the active minute trigger');
shortHour.click(); key('End'); await flush(); shortPop.scrollTop = 24; await flush();
assert.equal(document.activeElement, shortHour, 'closing sibling lists cannot steal the active hour focus');
assert.equal(shortHour.getAttribute('aria-expanded'), 'false');
assert.ok(shortModal.mask.isConnected); assert.equal(picker(), shortPop);
shortMinute.click(); key('End'); await flush();
key('Escape'); assert.equal(document.activeElement, shortMinute); assert.equal(picker(), shortPop);
key('Escape'); assert.equal(picker(), null); assert.equal(document.activeElement, short.button);
short.button.click(); picker().querySelector('[data-unit="minute"]').querySelector('.rtp-select-btn').click();
window.innerHeight = 240;
dispatch(window, new UiEvent('resize')); await flush();
assert.equal(picker(), null, 'resizing still releases the entire picker and child list');
assert.equal(document.activeElement, short.button); assert.ok(shortModal.mask.isConnected);
window.innerHeight = 320;
short.button.click(); picker().querySelector('[data-unit="hour"]').querySelector('.rtp-select-btn').click();
const beforeExternalScroll = Date.now;
Date.now = () => beforeExternalScroll() + 400;
dispatch(shortModal.root.querySelector('[data-short-host]'), new UiEvent('scroll')); await flush();
Date.now = beforeExternalScroll;
assert.equal(picker(), null, 'external form scrolling still releases calendar and child list');
assert.equal(document.activeElement, short.button); assert.equal(short.input.value, '2026-10-13T22:58');
key('Escape'); await flush(); assert.equal(shortModal.mask.isConnected, false);
Element.prototype.getBoundingClientRect = originalRect;
Element.prototype.scrollIntoView = originalIntoView;
window.innerHeight = oldHeight; window.innerWidth = oldWidth;

first.button.click(); picker().querySelector('[data-unit="hour"]').querySelector('.rtp-select-btn').click();
first.field.remove(); await flush();
assert.equal(picker(), null, 'dynamic field removal releases calendar and nested list');
assert.equal([...window.listeners.values()].flat().length, 0, 'picker viewport listeners are released');
const observed = mount({ id: 'observed', value: '2026-10-13T02:34' });
observed.button.click(); await flush(); observed.input.disabled = true; await flush();
assert.equal(picker(), null); assert.equal(observed.button.disabled, true);

const occurrence = { id: 'one', task_id: 'task', window_start_at: '2026-10-12T18:00:00Z', window_end_at: '2026-10-12T19:00:00Z', actual_start: '2026-10-12T17:00:00Z', actual_end: '2026-10-12T17:30:00Z' };
let tasks = [{ id: 'task', task_type: 'daily' }];
const dialogs = createPlanningDialogs({ findOccurrence: () => occurrence, getOccurrences: () => [occurrence], getTasks: () => tasks, openTaskForm() {}, loadToday: async () => {}, loadTasks: async () => {}, loadOccurrences: async () => {} });
const activeModal = () => document.body.children.findLast(node => node.className === 'modal-mask').querySelector('.modal');
const fieldsFor = root => root.querySelectorAll('.retro-time-value');
const cancel = () => activeModal().querySelector('[data-cancel]').click();
const testedIds = new Set();
for (const [open, ids] of [
  [() => dialogs.askRescheduleTimeout('one'), ['planning-reschedule-time']],
  [() => dialogs.askNewTime('one', 'deferred', '延后'), ['planning-new-time']],
  [() => dialogs.askEditTime('one'), ['planning-adj-window-start', 'planning-adj-window-end']],
  [() => dialogs.askBackfill('one'), ['planning-backfill-start', 'planning-backfill-end']],
]) {
  open(); await flush(); const root = activeModal();
  assert.deepEqual(fieldsFor(root).map(input => input.id), ids);
  for (const id of ids) { testedIds.add(id); root.querySelector(`#${id}`).parentElement.querySelector('button').click(); assert.ok(picker()); key('Escape'); }
  cancel(); await flush();
}
assert.equal(testedIds.size, 6);
dialogs.askEditTime('one'); await flush();
let root = activeModal();
assert.equal(new Date(root.querySelector('#planning-adj-window-start').value).toISOString(), occurrence.window_start_at.replace('Z', '.000Z'));
await root.querySelector('[data-ok]').onclick();
assert.deepEqual(requests.at(-1).body, { window_start_at: '2026-10-12T18:00:00.000Z', window_end_at: '2026-10-12T19:00:00.000Z' });
dialogs.askBackfill('one'); await flush(); root = activeModal();
fieldsFor(root).forEach(input => input._applyRetroValue('', true));
await root.querySelector('[data-ok]').onclick();
assert.deepEqual(requests.at(-1).body, { actual_start: null, actual_end: null });
tasks = [{ id: 'task', task_type: 'once', target_date: null }];
dialogs.askEditTime('one'); await flush(); root = activeModal();
assert.ok(fieldsFor(root).every(input => input.disabled && input.parentElement.querySelector('button').disabled));
cancel();
tasks = [{ id: 'task', task_type: 'daily' }];
dialogs.askEditTime('one'); await flush(); root = activeModal();
requestFailure = '最晚完成不能早于最早开始';
await root.querySelector('[data-ok]').onclick();
assert.ok(root.isConnected); assert.equal(root.querySelector('#pf-adj-error').hidden, false);
assert.equal(fieldsFor(root).length, 2, 'range rejection leaves both editable values present');
await flush();
assert.ok(fieldsFor(root).every(input => input.parentElement.querySelector('button').getAttribute('aria-invalid') === 'true'));
assert.ok(fieldsFor(root).every(input => input.parentElement.querySelector('button').getAttribute('aria-describedby').includes('pf-adj-error')));
fieldsFor(root)[1]._applyRetroValue('', false); await flush();
assert.equal(root.querySelector('#pf-adj-error').hidden, true);
assert.ok(fieldsFor(root).every(input => input.parentElement.querySelector('button').getAttribute('aria-invalid') === null));
cancel(); requestFailure = null;
await flush();
assert.equal(picker(), null);

// Names follow the endpoint's own visible label, including nested and inline layouts.
openTaskForm({ task_type: 'daily', window_start_tod: '09:00', window_end_tod: '17:00' }, {
  occurrences: [], onSaved: async () => {},
  initRetroFields(scope, options) {
    scope.querySelectorAll('.retro-time[data-retro-for]').forEach(host => createRetroTimeField(host, {
      id: host.dataset.retroFor, value: host.dataset.retroValue || '', mode: host.dataset.retroMode || 'datetime',
    }));
    scope.querySelectorAll('.retro-select[data-retro-select]').forEach(host => createRetroSelectField(host, {
      id: host.dataset.retroSelect, value: host.dataset.retroValue || '', options: options[host.dataset.retroSelect] || [],
    }));
  },
});
await flush(); root = activeModal();
assert.equal(root.querySelector('#pf-window-start').parentElement.querySelector('button').getAttribute('aria-label'), '最早开始（可选）：09:00');
assert.equal(root.querySelector('#pf-window-end').parentElement.querySelector('button').getAttribute('aria-label'), '最晚完成（可选）：17:00');
cancel(); await flush();
const inline = document.body.appendChild(new Element('label'));
inline.innerHTML = '原始规划周期<span data-inline-host></span>';
createRetroTimeField(inline.querySelector('[data-inline-host]'), { id: 'inline-filter', mode: 'date' });
assert.equal(inline.querySelector('button').getAttribute('aria-label'), '原始规划周期：未选择');
const card = document.body.appendChild(new Element()); card.className = 'kv';
card.innerHTML = '<span class="k">每日刷新时间</span><span class="v"><span data-card-host></span></span>';
createRetroTimeField(card.querySelector('[data-card-host]'), { id: 'card-boundary', mode: 'time', value: '06:00' });
assert.equal(card.querySelector('button').getAttribute('aria-label'), '每日刷新时间：06:00');
const configSource = await readFile(join(workspace, 'admin/js/pages/config.js'), 'utf8');
assert.match(configSource, /aria-label="每日刷新时间（北京时间）" data-retro-for="cfg-cycle-boundary"/);

responsePayload = { refresh_boundary_time: '06:00', daily_refresh_enabled: true, auto_recompute_enabled: true, auto_recompute_wait_minutes: 5 };
await openCycleSettings(); await flush(); root = activeModal();
const boundary = root.querySelector('#pf-cycle-boundary');
assert.equal(boundary.parentElement.querySelector('button').getAttribute('aria-label'), '每日刷新时间（北京时间）：06:00');
boundary._applyRetroValue('07:00', true);
responsePayload = { conflicts: [{ task_id: 41, content: '背单词', window_start_tod: '06:30', window_end_tod: '08:00', reason: '跨越新刷新时间' }] };
await root.querySelector('[data-ok]').onclick(); await flush();
assert.equal(root.querySelector('#pf-adj-start-41').parentElement.querySelector('button').getAttribute('aria-label'), '背单词的最早开始：06:30');
assert.equal(root.querySelector('#pf-adj-end-41').parentElement.querySelector('button').getAttribute('aria-label'), '背单词的最晚完成：08:00');
cancel(); await flush();
Date.now = actualNow;
process.stdout.write('Shared time picker and six planning datetime regressions passed.\n');
