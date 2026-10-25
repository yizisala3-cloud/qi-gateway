import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { document, flush } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2];
const version = (await readFile(join(workspace, 'admin/js/ui.js'), 'utf8')).match(/ASSET_VERSION = '([^']+)'/)[1];
const load = name => import(pathToFileURL(join(workspace, 'admin/js', name)).href + '?v=' + version);
window.location = { origin: 'http://latency.test' };
globalThis.localStorage = { getItem: () => '' };
const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
const response = data => ({ ok: true, json: async () => data });
const root = () => { const el = document.createElement('div'); document.body.appendChild(el); return el; };

test('config renders ready sections while continuity waits, preserving edits and node identity', async () => {
  const config = (await load('pages/config.js')).default;
  const slow = deferred();
  globalThis.fetch = async url => {
    const path = new URL(url).pathname;
    if (path.endsWith('memory-continuity/status')) return slow.promise.then(response);
    if (path.endsWith('context/settings')) return response({ recent_chat_enabled: true, recent_chat_limit: 20, timestamp_enabled: true });
    return response({ status: 'ok' });
  };
  const host = root();
  const mounting = config.mount(host);
  await flush();
  const input = host.querySelector('#recent-limit-input');
  assert.ok(input, 'context card is usable before continuity has returned');
  input.value = '77';
  assert.ok(host.querySelector('#config-section-1').textContent.includes('正在载入'), 'slow card keeps a loading state');
  slow.resolve({ analysis_configured: true });
  await mounting;
  assert.equal(host.querySelector('#recent-limit-input'), input);
  assert.equal(input.value, '77');
  config.unmount();
});

test('digest reads independent states concurrently and ignores responses from an old mount', async () => {
  const digest = (await load('pages/digest.js')).default;
  const pending = [];
  globalThis.fetch = async url => { const value = deferred(); pending.push({ path: new URL(url).pathname, ...value }); return value.promise.then(response); };
  const first = digest.mount(root());
  await flush();
  assert.equal(pending.length, 2);
  assert.ok(pending.some(item => item.path.includes('memory-rumination')));
  digest.unmount();
  const secondRoot = root();
  const second = digest.mount(secondRoot);
  await flush();
  for (const item of pending.slice(2)) item.resolve({ analysis_configured: false, configured: false, recent_runs: [], cursor: 22 });
  await second;
  const before = secondRoot.textContent;
  for (const item of pending.slice(0, 2)) item.resolve({ analysis_configured: true, configured: true, recent_runs: [], cursor: 1 });
  await first;
  assert.equal(secondRoot.textContent, before);
  assert.equal(digest.data.cursor, 22);
  assert.equal(digest.rumination.cursor, 22);
  digest.unmount();
});

test('log metadata and list start together with an identical frozen range', async () => {
  const logs = (await load('pages/logs.js')).default;
  const pending = [];
  globalThis.fetch = async url => { const value = deferred(); pending.push({ url: new URL(url), ...value }); return value.promise.then(response); };
  const mounting = logs.mount(root());
  await flush();
  assert.equal(pending.length, 2);
  assert.equal(pending[0].url.searchParams.get('start'), pending[1].url.searchParams.get('start'));
  assert.equal(pending[0].url.searchParams.get('end'), pending[1].url.searchParams.get('end'));
  for (const item of pending) item.resolve({ groups: [], modules: [], keys: [] });
  await mounting;
  logs.unmount();
});

test('lost command response retries the same identity and blocks a changed uncertain intent', async () => {
  const { sendOccurrenceCommand, pendingOccurrenceCommand } = await load('lib/planning_commands.js');
  const sent = [];
  globalThis.fetch = async (url, options) => {
    sent.push(JSON.parse(options.body));
    if (sent.length === 1) throw new TypeError('connection lost');
    return response({ id: 999, status: 'completed' });
  };
  await assert.rejects(sendOccurrenceCommand(999, 'completed', { actual_logged_duration: '3m' }), /结果未确认/);
  assert.equal(pendingOccurrenceCommand(999, 'completed').actual_logged_duration, '3m');
  await assert.rejects(sendOccurrenceCommand(999, 'completed', { actual_logged_duration: '4m' }), /原内容/);
  assert.equal(sent.length, 1);
  await sendOccurrenceCommand(999, 'completed', { actual_logged_duration: '3m' });
  assert.equal(sent[0].request_key, sent[1].request_key);
  assert.equal(pendingOccurrenceCommand(999, 'completed'), null);
});

test('a receipt moves the row immediately and a newer local fact rejects an older replay', async () => {
  const { mergeOccurrenceReceipt } = await load('lib/planning_sync.js');
  const row = { id: 3, status: 'pending', sort_order: 0, updated_at: '2026-10-25T08:00:00.000001Z' };
  const board = { date: '2026-10-25', progress: [row], attention: [], done: [] };
  const receipt = { ...row, status: 'completed', closed_at: '2026-10-25T08:01:00Z', updated_at: '2026-10-25T08:01:00Z' };
  const saved = mergeOccurrenceReceipt(board, receipt);
  assert.equal(saved.progress.length, 0);
  assert.equal(saved.done[0], receipt);
  assert.equal(mergeOccurrenceReceipt(saved, row), saved);
  const created = { ...row, id: 4, display_cycle_date: board.date };
  assert.equal(mergeOccurrenceReceipt(board, created, { allowInsert: true }).progress.length, 2);
  assert.equal(mergeOccurrenceReceipt(board, { ...created, display_cycle_date: '2026-10-26' }, { allowInsert: true }), board);
});

test('queryPage requests one filtered page with exact count and summary', async () => {
  const { queryPage } = await load('api.js');
  const urls = [];
  globalThis.fetch = async url => { urls.push(new URL(url)); return response({ data: [{ id: 1 }], count: 51 }); };
  const page = await queryPage('memories', { select: 'id,title,content', limit: 20, offset: 20, eq: { is_active: true }, search: '测试', summary: true });
  assert.equal(urls.length, 1);
  assert.equal(urls[0].searchParams.get('with_count'), 'true');
  assert.equal(urls[0].searchParams.get('summary'), 'true');
  assert.equal(urls[0].searchParams.get('offset'), '20');
  assert.equal(page.count, 51);
});

async function receiptPage(tab = 'today') {
  const module = (await load('pages/planning.js')).default;
  return Object.assign(Object.create(module), {
    root: root(), activeTab: tab, tasks: [], occurrences: [], selected: null, orderRevision: 0,
    board: { date: '2026-10-25', progress: [], attention: [], done: [] },
    detailEdit: { isActive: () => false }, renderBoard() {}, renderTasks() {}, renderOccurrences() {},
  });
}

test('occurrence receipts cannot overwrite tasks with colliding ids; explicit task receipts still apply', async () => {
  const page = await receiptPage();
  const task = { id: 3, content: 'Parent task', task_type: 'daily', is_active: true, refresh_enabled: true,
    updated_at: '2026-10-25T08:00:00Z' };
  const other = { ...task, id: 8, content: 'Other task', refresh_enabled: false };
  page.tasks = [task, other];
  const occurrence = { id: 8, task_id: 3, task_type: 'daily', content: 'Occurrence of parent',
    status: 'in_progress', updated_at: '2026-10-25T08:01:00Z' };
  page.board.progress = [{ ...occurrence, status: 'pending' }];
  page.applyCommittedReceipt(occurrence);
  assert.equal(page.tasks[0], task);
  assert.equal(page.tasks[1], other);
  assert.equal(page.board.progress[0], occurrence);
  const saved = { ...task, content: 'Saved parent', updated_at: '2026-10-25T08:02:00Z' };
  page.applyCommittedReceipt(saved);
  assert.equal(page.tasks[0], saved);
  const nested = { ...other, refresh_enabled: true, updated_at: '2026-10-25T08:02:00Z' };
  page.applyCommittedReceipt({ task: nested, occurrences: [] });
  assert.equal(page.tasks[1], nested);
  page.applyCommittedReceipt(task);
  assert.equal(page.tasks[0], saved, 'an older task receipt remains rejected');
});

test('command responses preserve timer and alarm drafts entered while saving on both planning tabs', async () => {
  for (const tab of ['today', 'all']) {
    const page = await receiptPage(tab);
    const el = document.createElement('div');
    el.innerHTML = '<div class="detail-title"></div><div class="detail-badges"></div><div data-body></div><div data-foot></div>';
    page.root.appendChild(el);
    const body = el.querySelector('[data-body]');
    const foot = el.querySelector('[data-foot]');
    page.detail = { el, body, foot, render({ title, badges, html, actions }) {
      el.querySelector('.detail-title').innerHTML = title;
      el.querySelector('.detail-badges').innerHTML = badges;
      body.innerHTML = html; foot.innerHTML = actions;
    } };
    const occurrence = { id: 20, task_id: 3, task_type: 'daily', content: 'Selected occurrence',
      status: 'pending', task_is_active: true, estimated_minutes: 30, timer_minutes: null,
      updated_at: '2026-10-25T08:00:00Z' };
    page.selected = { kind: 'occ', id: 20 };
    page.board.progress = [occurrence];
    page.occurrences = [occurrence];
    await page.showOccurrenceDetail(occurrence);
    const pending = deferred();
    globalThis.fetch = async () => pending.promise.then(response);
    const saving = page.occurrenceAction('start', 20);
    await flush();
    const timer = body.querySelector('[data-timer-input]');
    timer.value = '17';
    timer.focus();
    const alarm = body.querySelector('[data-alarm-start]');
    alarm.checked = true;
    pending.resolve({ ...occurrence, status: 'in_progress', updated_at: '2026-10-25T08:01:00Z' });
    await saving;
    assert.equal(page.findOccurrence(20).status, 'in_progress');
    assert.equal(body.querySelector('[data-timer-input]'), timer, tab);
    assert.equal(timer.value, '17', tab);
    assert.equal(document.activeElement, timer, tab);
    assert.equal(body.querySelector('[data-alarm-start]'), alarm, tab);
    assert.equal(alarm.checked, true, tab);
    assert.ok(foot.querySelector('[data-act="occ-finish"]'), 'committed action state still updates');
  }
});
