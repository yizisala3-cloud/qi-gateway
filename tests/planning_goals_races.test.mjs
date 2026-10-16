import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Element, document, flush } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2];
const version = (await readFile(join(workspace, 'admin/js/ui.js'), 'utf8')).match(/ASSET_VERSION = '([^']+)'/)[1];
window.location = { origin: 'http://localhost' };
window.matchMedia = () => ({ matches: false });
globalThis.localStorage = { getItem: () => 'goals-race-test-token' };
const storage = new Map();
globalThis.sessionStorage = { getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value) };
const goals = await import(pathToFileURL(join(workspace, 'admin/js/lib/planning_goals.js')).href + `?v=${version}`);
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { resolve, promise }; };
const ok = body => ({ ok: true, json: async () => body });
const conflict = () => ({ ok: false, status: 409, statusText: 'Conflict', json: async () => ({ error: '目标已被修改' }) });
const goal = (id = 1, content = '短目标') => ({ id, content, target_value: '1.5', unit: '页', planned_count: 10,
  executed_count: 0, percentage: 0, achieved: false, version: 1,
  current_link: { task_id: 5, task_type: 'daily', task_content: '每日阅读' }, history: [] });
const modalTitles = () => document.querySelectorAll('.modal-head').map(el => el.querySelector('h3').textContent);
const toasts = () => document.querySelectorAll('.toast').map(el => el.textContent);
const settle = async () => { await flush(); await flush(); };

function fixture(saved = null) {
  storage.clear();
  if (saved) storage.set('qi-planning-goal-pending-v1', JSON.stringify(saved));
  for (const el of document.querySelectorAll('.toast')) el.remove();
  const parent = document.body.appendChild(new Element('main'));
  parent.className = 'page-with-detail';
  const host = parent.appendChild(new Element('section'));
  const requests = [];
  let detail = null, taskForms = 0;
  let respond = request => ok(request.url.endsWith('/goals') ? [goal(), goal(2, '其它目标')] :
    request.url.endsWith('/candidates') ? [{ id: 5, goal_id: 1, content: '每日阅读', task_type: 'daily' }] : goal());
  globalThis.fetch = async (url, options = {}) => {
    const request = { url, options, body: options.body ? JSON.parse(options.body) : null };
    requests.push(request); return respond(request);
  };
  const controller = goals.createPlanningGoals({
    getDetail: () => ({ el: parent, isOpenMobile: () => false, render: value => { detail = value; } }),
    clearSelection() { detail = null; }, openTaskForm() { taskForms++; }, onTaskChanged() {},
  });
  controller.mount(host, parent);
  return {
    parent, host, requests, controller, get detail() { return detail; }, get taskForms() { return taskForms; },
    setRespond(fn) { respond = fn; },
    fire(action, id = 1) {
      const button = parent.appendChild(new Element('button'));
      button.setAttribute('data-goal-act', action); button.setAttribute('data-goal-id', String(id)); button.click();
      return button;
    },
    dispose() { controller.dispose(); parent.remove(); },
  };
}

// One old request must not resurrect any dialog after returning to the same tab.
for (const action of ['edit', 'adjust', 'full', 'history', 'link', 'new-task', 'delete', 'unlink']) {
  const f = fixture(); await f.controller.show();
  const read = deferred();
  f.setRespond(request => request.url.endsWith('/goals/1') ? read.promise : ok([goal()]));
  f.fire(action); await settle();
  f.controller.hide(); await f.controller.show();
  f.fire('create'); await settle();
  document.querySelector('#goal-content').value = '较新的草稿';
  read.resolve(ok(goal())); await settle();
  assert.deepEqual(modalTitles(), ['新建目标'], `${action}: old tab intent must stay canceled`);
  assert.equal(document.querySelector('#goal-content').value, '较新的草稿');
  assert.equal(f.taskForms, 0);
  f.dispose();
}

// Staying on the same tab still requires latest-action ownership.
for (const next of ['create', 'select']) {
  const f = fixture(); await f.controller.show();
  const read = deferred();
  f.setRespond(request => request.url.endsWith('/goals/1') ? read.promise : ok([goal(), goal(2)]));
  f.fire('full'); await settle(); f.fire(next, 2); await settle();
  read.resolve(ok(goal())); await settle();
  assert.deepEqual(modalTitles(), next === 'create' ? ['新建目标'] : [], 'newer intent supersedes old full-text read');
  f.dispose();
}

// Hidden and disposed readers cannot open dialogs, including after remount.
for (const action of ['full', 'history', 'edit', 'link']) for (const remount of [false, true]) {
  const f = fixture(); await f.controller.show();
  const read = deferred(); f.setRespond(() => read.promise);
  f.fire(action); await settle();
  if (remount) {
    f.controller.dispose(); f.controller.mount(f.host, f.parent);
    f.setRespond(() => ok([goal()])); await f.controller.show();
    f.fire('create'); await settle();
  } else f.controller.hide();
  read.resolve(ok(goal())); await settle();
  assert.deepEqual(modalTitles(), remount ? ['新建目标'] : [], `${action}: hidden/remounted intent is obsolete`);
  f.dispose();
}

// Candidate loading is a second await with its own obsolete-result boundary.
{
  const f = fixture(); await f.controller.show(); const read = deferred();
  f.setRespond(request => request.url.endsWith('/candidates') ? read.promise : ok(goal()));
  f.fire('link'); await settle();
  assert.ok(f.requests.some(request => request.url.endsWith('/candidates')));
  f.controller.hide(); f.setRespond(() => ok([goal()])); await f.controller.show();
  f.fire('create'); await settle();
  read.resolve(ok([{ id: 5, goal_id: 1, content: '每日阅读', task_type: 'daily' }])); await settle();
  assert.deepEqual(modalTitles(), ['新建目标']);
  f.dispose();
}

// A version-conflict reread must check its dismissed editor BEFORE confirming.
{
  const f = fixture(); await f.controller.show(); f.fire('edit'); await settle();
  const editor = document.querySelector('.modal');
  f.setRespond(request => request.options.method ? conflict() : ok(goal()));
  await editor.querySelector('[data-save]').onclick();
  const read = deferred(); f.setRespond(() => read.promise);
  const retry = editor.querySelector('[data-save]').onclick(); await settle();
  editor.querySelector('[data-cancel]').onclick(); f.fire('create'); await settle();
  document.querySelector('#goal-content').value = '冲突读取期间打开的新草稿';
  read.resolve(ok(goal())); await retry; await settle();
  assert.deepEqual(modalTitles(), ['新建目标']);
  assert.equal(document.querySelector('#goal-content').value, '冲突读取期间打开的新草稿');
  f.dispose();
}

// A dismissed association conflict retry cannot open a replacement confirmation.
{
  const f = fixture(); await f.controller.show(); f.fire('link'); await settle();
  const editor = document.querySelector('.modal');
  editor.querySelector('#goal-candidate').value = '5';
  f.setRespond(() => conflict()); await editor.querySelector('[data-save]').onclick();
  const read = deferred(); f.setRespond(() => read.promise);
  const retry = editor.querySelector('[data-save]').onclick(); await settle();
  editor.querySelector('[data-cancel]').onclick(); f.fire('create'); await settle();
  read.resolve(ok(goal())); await retry; await settle();
  assert.deepEqual(modalTitles(), ['新建目标']);
  f.dispose();
}

// Retrying a previously created task must also guard its latest-goal reread.
{
  const f = fixture({ operations: [], flows: [{ goalId: 1, version: 1, taskId: 5,
    taskKey: 'original-task-key', taskBody: { content: '已创建待办' }, linkOp: null }] });
  await f.controller.show(); const read = deferred(); f.setRespond(() => read.promise);
  f.fire('recover-task'); await settle();
  f.controller.hide(); f.setRespond(() => ok([goal()])); await f.controller.show();
  f.fire('create'); await settle(); read.resolve(ok({ ...goal(), version: 2 })); await settle();
  assert.deepEqual(modalTitles(), ['新建目标']);
  assert.equal(f.requests.filter(request => request.options.method).length, 0);
  assert.equal(JSON.parse(storage.get('qi-planning-goal-pending-v1')).flows[0].taskId, 5);
  f.dispose();
}

// Original save and recovery share the SAME pending outcome: no speculative
// success toast and no second POST, even after its originating modal is closed.
for (const outcome of ['success', 'unknown', 'rejected']) {
  const f = fixture(); await f.controller.show(); f.fire('create'); await settle();
  const editor = document.querySelector('.modal');
  for (const [selector, value] of [['#goal-content', '回执等待'], ['#goal-value', '1.5'], ['#goal-unit', 'h'], ['#goal-planned', '5']]) editor.querySelector(selector).value = value;
  const write = deferred(); let acknowledged = false;
  f.setRespond(request => request.options.method ? write.promise
    : ok([goal(), goal(2, '其它目标'), ...(acknowledged ? [goal(3, '回执等待')] : [])]));
  const saving = editor.querySelector('[data-save]').onclick(); await settle();
  editor.querySelector('[data-cancel]').onclick();
  f.controller.hide(); await f.controller.show();
  f.host.querySelector('[data-goal-act="recover"]').click(); await settle();
  f.host.querySelector('[data-goal-act="recover"]').click(); await settle();
  assert.equal(f.requests.filter(r => r.options.method).length, 1);
  assert.ok(!toasts().some(text => text.includes('原操作已确认')), 'no acknowledgement exists while the network is pending');
  if (outcome === 'success') f.fire('select', 2);
  acknowledged = outcome !== 'rejected';
  write.resolve(outcome === 'unknown' ? { ok: true, json: async () => { throw new TypeError('lost response'); } }
    : outcome === 'rejected' ? conflict() : ok(goal(3, '回执等待')));
  await saving; await settle();
  if (outcome !== 'success') assert.ok(!toasts().some(text => text.includes('原操作已确认')));
  else {
    assert.equal(toasts().filter(text => text.includes('原操作已确认')).length, 1);
    assert.ok(f.detail.title.includes('其它目标'), 'the real receipt must not take selection back from a newer user action');
  }
  if (outcome === 'unknown') {
    assert.ok(!toasts().some(text => text.includes('原操作已确认')), 'an unknown result cannot become a success');
    const original = f.requests.find(r => r.options.method);
    f.setRespond(request => request.options.method ? ok(goal(3)) : ok([goal(3)]));
    f.host.querySelector('[data-goal-act="recover"]').click(); await settle();
    const writes = f.requests.filter(r => r.options.method);
    assert.equal(writes.length, 2);
    assert.equal(writes[1].options.headers['Idempotency-Key'], original.options.headers['Idempotency-Key']);
    assert.deepEqual(writes[1].body, original.body);
  }
  assert.equal(JSON.parse(storage.get('qi-planning-goal-pending-v1')).operations.length, 0);
  f.dispose();
}

// Short headings can be clipped by responsive CSS, so length never removes
// the full-text action; the browser suite verifies actual narrow-screen clips.
{
  const f = fixture(); await f.controller.show(); f.fire('select');
  assert.ok(f.detail.html.includes('data-goal-act="full"'));
  f.dispose();
}
process.stdout.write('goal race regressions passed\n');
