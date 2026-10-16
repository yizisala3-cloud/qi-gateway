import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Element, document, flush } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2];
const source = await readFile(join(workspace, 'admin/js/ui.js'), 'utf8');
const version = source.match(/ASSET_VERSION = '([^']+)'/)[1];
window.location = { origin: 'http://localhost' };
window.matchMedia = () => ({ matches: false });
globalThis.localStorage = { getItem: () => 'goals-test-token' };
const savedStorage = new Map();
globalThis.sessionStorage = { getItem: key => savedStorage.get(key), setItem: (key, value) => savedStorage.set(key, value) };
const goals = await import(pathToFileURL(join(workspace, 'admin/js/lib/planning_goals.js')).href + `?v=${version}`);
const requests = [];
let respond;
globalThis.fetch = async (url, options = {}) => {
  const request = { url, options, body: options.body ? JSON.parse(options.body) : null };
  requests.push(request);
  return { ok: true, json: async () => respond(request) };
};
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { resolve, promise }; };
const data = (content, count = 0) => ({ id: 1, content, target_value: '200.25', unit: '页', planned_count: 40,
  executed_count: count, percentage: count / 40 * 100, achieved: count >= 40, version: 1, current_link: null, history: [] });
const parent = document.body.appendChild(new Element('main'));
parent.className = 'page-with-detail';
const host = parent.appendChild(new Element('section'));
let detailRenders = 0;
const detail = { el: parent, render() { detailRenders++; }, isOpenMobile: () => false };
const controller = goals.createPlanningGoals({ getDetail: () => detail, clearSelection() {}, openTaskForm() {}, onTaskChanged() {} });
controller.mount(host, parent);

assert.deepEqual(goals.goalProgress(data('超额', 44)), { text: '44/40', percentage: '110%', width: 100 });
assert.deepEqual(goals.GOAL_TASK_TYPES, ['daily', 'interval', 'weekly', 'monthly']);
const payload = { executed_count: 19, expected_version: 1 };
const op = goals.createGoalOperation('adjust', 1, payload);
payload.executed_count = 999;
respond = () => data('已修正', 19);
await goals.sendGoalOperation(op);
await goals.sendGoalOperation(op);
assert.equal(requests.at(-1).body.executed_count, 19, 'the submitted intent is immutable');
assert.equal(requests.at(-1).options.headers['Idempotency-Key'], requests.at(-2).options.headers['Idempotency-Key']);
assert.equal(requests.at(-1).options.headers.Authorization, 'Bearer goals-test-token');

// A slow older read may not overwrite a newer authoritative result.
const old = deferred();
respond = () => old.promise;
const pending = controller.show();
respond = () => [data('较新的结果', 20)];
await controller.show();
old.resolve([data('过期结果', 0)]);
await pending;
assert.ok(host.textContent.includes('较新的结果'));
assert.ok(!host.textContent.includes('过期结果'));
host.querySelector('.goal-card').click();
const beforeCollapsedRead = detailRenders;
parent.classList.add('detail-collapsed');
await controller.show();
assert.equal(detailRenders, beforeCollapsedRead, 'polling must preserve the user-collapsed PC detail panel');
parent.classList.remove('detail-collapsed');

// Read failure has its own visible state and cannot become zero/empty data.
respond = () => { throw new TypeError('connection lost'); };
await controller.show();
assert.ok(host.textContent.includes('目标读取失败'));
assert.ok(host.textContent.includes('20/40'));

// A detached read and stale events cannot mutate a new mount.
const abandoned = deferred();
respond = () => abandoned.promise;
const former = controller.show();
controller.dispose();
const nextHost = parent.appendChild(new Element('section'));
controller.mount(nextHost, parent);
respond = () => [data('重新挂载', 21)];
await controller.show();
abandoned.resolve([data('旧页面迟到')]);
await former;
assert.ok(nextHost.textContent.includes('重新挂载'));
assert.ok(!nextHost.textContent.includes('旧页面迟到'));

// Unknown goal creation persists its exact request, surviving dialog dismissal.
const create = parent.appendChild(new Element('button'));
create.setAttribute('data-goal-act', 'create');
create.click(); await flush();
let modal = document.querySelector('.modal');
modal.querySelector('#goal-content').value = '未知结果的目标';
modal.querySelector('#goal-value').value = '1.5';
modal.querySelector('#goal-unit').value = 'h';
modal.querySelector('#goal-planned').value = '2';
respond = request => {
  if (request.options.method) throw new TypeError('network delivery unknown');
  return [data('重新挂载', 21)];
};
await modal.querySelector('[data-save]').onclick();
assert.ok(modal.querySelector('#goal-content').disabled);
const firstWrite = requests.at(-1);
modal.querySelector('[data-cancel]').onclick();
const stored = JSON.parse(savedStorage.get('qi-planning-goal-pending-v1'));
assert.equal(stored.operations.length, 1);
assert.equal(stored.operations[0].payload.content, '未知结果的目标');
respond = request => request.options.method ? data('未知结果的目标') : [data('未知结果的目标')];
parent.querySelector('[data-goal-act="recover"]').click(); await flush(); await flush();
const replay = requests.filter(request => request.options.method).at(-1);
assert.deepEqual(replay.body, firstWrite.body);
assert.equal(replay.options.headers['Idempotency-Key'], firstWrite.options.headers['Idempotency-Key']);
assert.equal(JSON.parse(savedStorage.get('qi-planning-goal-pending-v1')).operations.length, 0);
controller.dispose();
process.stdout.write('goal frontend behavior passed\n');
