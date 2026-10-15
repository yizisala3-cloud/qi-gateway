import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { Element, document, flush } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2];
const source = await readFile(join(workspace, 'admin/js/ui.js'), 'utf8');
const version = source.match(/ASSET_VERSION = '([^']+)'/)[1];
const notes = await import(pathToFileURL(join(workspace, 'admin/js/lib/planning_notes.js')).href + `?v=${version}`);
globalThis.localStorage = { getItem: () => 'note-test-token' };
window.location = { origin: 'http://localhost' };
const requests = [];
let respond = async () => ({ id: 1, note: '服务端最新备注' });
globalThis.fetch = async (url, options) => {
  const request = { url, options, body: options.body ? JSON.parse(options.body) : null };
  requests.push(request);
  const body = await respond(request);
  return { ok: true, json: async () => body };
};
const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { resolve, promise }; };
const page = document.body.appendChild(new Element('main'));
page.innerHTML = '<button>当前待办列表</button>';

assert.equal(notes.noteLength('🙂'.repeat(60)), 60);
assert.equal(notes.noteLength('一\r\n二'), 3);
assert.ok(notes.noteInputHtml('</textarea><script>bad()</script>').includes('&lt;/textarea&gt;'));
assert.ok(notes.taskNotePreview(1, '<img src=x onerror=bad()>').includes('&lt;img'));

const readOnly = notes.openTaskNote({ taskId: 1, note: '历史原文', readOnly: true });
await flush();
assert.equal(requests.length, 0, 'history must not fetch a newer task note');
assert.equal(readOnly.root.querySelector('.plan-note-full').textContent, '历史原文');
assert.equal(readOnly.root.querySelector('textarea'), null);
assert.equal(readOnly.root.querySelector('[data-save]'), null);
assert.ok(!readOnly.root.textContent.includes('当前待办列表'), 'the dialog contains only its own note');
readOnly.close();

let saved = 0;
const longNote = '长备注🙂\n'.repeat(2000).trim();
const session = notes.openTaskNote({ taskId: 1, note: '旧缓存', onSaved: async result => {
  saved += 1; assert.equal(result.note, longNote);
} });
await flush();
const input = session.root.querySelector('#task-note-text');
const save = session.root.querySelector('[data-save]');
assert.equal(input.value, '服务端最新备注', 'editing refreshes the task note before enabling save');
assert.equal(save.disabled, false);
assert.equal(input.getAttribute('maxlength'), null);
input.value = longNote;
input.dispatchEvent(new Event('input'));
assert.equal(save.disabled, false, 'long notes remain editable and saveable');
assert.equal(session.root.querySelector('#task-note-text-count').textContent, `${notes.noteLength(longNote)} 字`);
let failed = true;
respond = async request => {
  if (request.body && failed) { failed = false; throw new Error('连接失败'); }
  return { id: 1, note: request.body?.note };
};
await save.onclick();
assert.equal(input.value, longNote, 'failed writes retain the full long draft');
assert.equal(save.disabled, false);
assert.equal(session.root.isConnected, true);
const pending = defer();
respond = async () => pending.promise;
const saving = save.onclick();
await save.onclick();
assert.equal(requests.filter(row => row.body).length, 2, 'double clicks do not start a second in-flight write');
pending.resolve({ id: 1, note: longNote });
await saving;
assert.equal(saved, 1);
assert.equal(session.root.isConnected, false);
assert.deepEqual(requests.at(-1).body, { note: longNote });
assert.equal(requests.at(-1).options.headers.Authorization, 'Bearer note-test-token');

const slow = defer();
respond = () => slow.promise;
const abandoned = notes.openTaskNote({ taskId: 1 });
abandoned.close();
respond = async () => ({ id: 2, note: '另一个备注' });
const next = notes.openTaskNote({ taskId: 2 });
await flush();
slow.resolve({ id: 1, note: '迟到结果' });
await flush();
assert.equal(next.root.querySelector('#task-note-text').value, '另一个备注', 'late responses cannot overwrite a later dialog');
next.close();

// A committed write belongs to the application, even after its modal was removed.
respond = async () => ({ id: 1, note: '原备注' });
let detachedSaved = 0;
const detached = notes.openTaskNote({ taskId: 1, onSaved: async () => { detachedSaved += 1; } });
await flush();
const delayedWrite = defer();
respond = () => delayedWrite.promise;
const detachedSaving = detached.root.querySelector('[data-save]').onclick();
detached.close();
delayedWrite.resolve({ id: 1, note: '保存成功' });
await detachedSaving;
assert.equal(detachedSaved, 1, 'successful detached writes must still invalidate application data');
// Tablet edits stay inside the original note and retain their draft on failure.
const inlineRoot = page.appendChild(new Element('div'));
inlineRoot.innerHTML = notes.taskNotePreview(1, '缓存');
const inlineNote = inlineRoot.querySelector('[data-act="task-note"]');
respond = async () => ({ id: 1, note: '最新原文' });
let inlineSaved = 0;
const inlineSession = notes.editTaskNoteInline(inlineNote, {
  taskId: 1, onSaved: async () => { inlineSaved += 1; },
});
await flush();
assert.equal(document.querySelector('.task-note-modal'), null, 'inline editing must not create a dialog');
const inlineInput = inlineNote.querySelector('[data-inline-note]');
assert.equal(inlineInput.value, '最新原文');
assert.equal(inlineInput.getAttribute('maxlength'), null);
inlineInput.value = longNote;
respond = async () => { throw new Error('保存连接失败'); };
await inlineNote.querySelector('[data-save]').onclick();
assert.equal(inlineInput.value, longNote, 'a failed inline save preserves the full draft');
assert.equal(inlineInput.disabled, false);
const inlineWrite = defer();
respond = () => inlineWrite.promise;
const inlineSaveButton = inlineNote.querySelector('[data-save]');
const writesBefore = requests.length;
const inlineSaving = inlineSaveButton.onclick();
await inlineSaveButton.onclick();
assert.equal(requests.length, writesBefore + 1, 'inline saves are single-flight');
inlineRoot.remove();
inlineWrite.resolve({ id: 1, note: longNote });
await inlineSaving;
assert.equal(inlineSaved, 1, 'inline writes still refresh the application after selection changes');
assert.equal(inlineSession.isActive(), false);
process.stdout.write('task note behavior passed\n');
