import assert from 'node:assert/strict';
import { cp, mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { document } from '../../../tests/support/frontend_dom_fixture.mjs';

const repository = resolve(process.argv[2] || '.');
const workspace = await mkdtemp(join(tmpdir(), 'console-latency-review-'));
await cp(join(repository, 'admin'), join(workspace, 'admin'), { recursive: true });
await writeFile(join(workspace, 'package.json'), '{"type":"module"}');
const version = (await readFile(join(workspace, 'admin/js/ui.js'), 'utf8')).match(/ASSET_VERSION = '([^']+)'/)[1];
const pageModule = (await import(pathToFileURL(join(workspace, 'admin/js/pages/planning.js')).href + '?v=' + version)).default;

function createPage() {
  const root = document.createElement('div');
  document.body.appendChild(root);
  return Object.assign(Object.create(pageModule), {
    root, activeTab: 'today', tasks: [], occurrences: [], selected: null,
    board: { date: '2026-10-25', progress: [], attention: [], done: [] },
    detailEdit: { isActive: () => false }, renderBoard() {},
  });
}

test('an occurrence receipt must not replace an unrelated task with the same numeric id', () => {
  const page = createPage();
  const otherTask = { id: 8, content: 'Other task', task_type: 'daily', is_active: true,
    refresh_enabled: false, updated_at: '2026-10-25T08:00:00Z' };
  page.tasks = [otherTask];
  page.applyCommittedReceipt({ id: 8, task_id: 3, task_type: 'daily', content: 'Occurrence of task 3',
    status: 'in_progress', task_is_active: true, updated_at: '2026-10-25T08:01:00Z' });
  assert.deepEqual(page.tasks[0], otherTask);
});

test('a command receipt must preserve a timer draft being edited while the request is pending', async () => {
  const page = createPage();
  const body = document.createElement('div');
  page.root.appendChild(body);
  const foot = document.createElement('div');
  page.root.appendChild(foot);
  page.detail = { body, foot, render({ html, actions }) { body.innerHTML = html; foot.innerHTML = actions; } };
  const occurrence = { id: 20, task_id: 3, task_type: 'daily', content: 'Selected occurrence',
    status: 'pending', task_is_active: true, estimated_minutes: 30, timer_minutes: null,
    updated_at: '2026-10-25T08:00:00Z' };
  page.selected = { kind: 'occ', id: 20 };
  page.board.progress = [occurrence];
  await page.showOccurrenceDetail(occurrence);
  const input = body.querySelector('[data-timer-input]');
  input.value = '17';
  input.focus();
  page.applyCommittedReceipt({ ...occurrence, status: 'in_progress', updated_at: '2026-10-25T08:01:00Z' });
  assert.equal(body.querySelector('[data-timer-input]').value, '17');
  assert.equal(body.querySelector('[data-timer-input]'), input);
});
