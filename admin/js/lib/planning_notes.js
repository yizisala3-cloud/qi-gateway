import { gw } from '../api.js?v=20261017-hide-deleted-tasks';
import { esc, icon, modal, toast } from '../ui.js?v=20261017-hide-deleted-tasks';

// Count Unicode code points for display without limiting note length.
export const noteLength = value => Array.from(String(value || '').replace(/\r\n?/g, '\n')).length;

export function focusEditorEnd(input) {
  if (!input || input.disabled) return;
  input.focus({ preventScroll: true });
  const end = input.value?.length;
  if (typeof end === 'number') input.setSelectionRange?.(end, end);
}

export function noteInputHtml(value = '', id = 'task-note-text', rows = 5) {
  return `<div class="field task-note-field">
    <label for="${id}">备注<span class="char-count" id="${id}-count" aria-live="polite"></span></label>
    <textarea id="${id}" rows="${rows}" aria-describedby="${id}-hint" placeholder="添加备注">${esc(value || '')}</textarea>
    <p class="field-hint" id="${id}-hint" data-help-note="备注">修改仅更新未结束的待办和后续待办，历史备注保留。</p>
  </div>`;
}

export function bindNoteInput(root, id = 'task-note-text', onValidity = () => {}) {
  const input = root.querySelector(`#${id}`);
  const counter = root.querySelector(`#${id}-count`);
  const update = () => {
    counter.textContent = `${noteLength(input.value)} 字`;
    onValidity(true);
    return true;
  };
  input.addEventListener('input', update);
  update();
  return {
    update, input,
    read() {
      return input.value.replace(/\r\n?/g, '\n').trim() || null;
    },
  };
}

const notePreviewBody = (note, readOnly) => `
    <div class="plan-task-note-head"><span>备注</span>${icon(readOnly ? 'book' : 'edit')}</div>
    ${note ? `<div class="plan-task-note-text copyable-text">${esc(note)}</div>`
      : `<div class="plan-task-note-empty">${readOnly ? '当时未填写备注' : '点击添加备注'}</div>`}
    <textarea class="plan-task-note-input-preview" rows="3" readonly tabindex="-1" aria-label="备注全文"
      placeholder="${readOnly ? '当时未填写备注' : '点击添加备注'}">${esc(note || '')}</textarea>`;

export function taskNotePreview(taskId, note, { readOnly = false } = {}) {
  return `<div class="plan-task-note" role="button" tabindex="0" data-act="task-note" data-id="${taskId}"
      aria-label="${readOnly ? '查看历史备注' : note ? '编辑待办备注' : '添加待办备注'}">
    ${notePreviewBody(note, readOnly)}
  </div>`;
}

export function editTaskNoteInline(root, { taskId, onSaved = async () => {} }) {
  const original = root.innerHTML;
  const label = root.getAttribute('aria-label');
  root.removeAttribute('data-act');
  root.removeAttribute('role');
  root.removeAttribute('tabindex');
  root.setAttribute('data-note-editor', '');
  root.innerHTML = `<label class="plan-task-note-head" for="inline-task-note-${taskId}">备注</label>
    <textarea id="inline-task-note-${taskId}" data-inline-note rows="3" aria-label="编辑备注" disabled></textarea>
    <div class="plan-note-inline-actions">
      <span data-note-feedback role="status">正在读取备注…</span>
      <button type="button" class="btn btn-quiet btn-sm" data-cancel>取消</button>
      <button type="button" class="btn btn-secondary btn-sm" data-retry hidden>重新读取</button>
      <button type="button" class="btn btn-primary btn-sm" data-save disabled>保存</button>
    </div>`;
  const input = root.querySelector('[data-inline-note]');
  const save = root.querySelector('[data-save]');
  const cancel = root.querySelector('[data-cancel]');
  const retry = root.querySelector('[data-retry]');
  const feedback = root.querySelector('[data-note-feedback]');
  let active = true;
  let ready = false;
  let saving = false;
  const current = () => active && root.isConnected;
  const restore = html => {
    active = false;
    if (!root.isConnected) return;
    root.removeAttribute('data-note-editor');
    root.setAttribute('data-act', 'task-note');
    root.setAttribute('role', 'button');
    root.setAttribute('tabindex', '0');
    root.setAttribute('aria-label', label);
    root.innerHTML = html;
  };
  const close = () => { if (!saving) restore(original); };
  cancel.onclick = close;
  const load = async () => {
    ready = false;
    retry.hidden = true;
    save.disabled = true;
    feedback.textContent = '正在读取备注…';
    try {
      const task = await gw(`/admin/api/planning/tasks/${taskId}`);
      if (!current()) return;
      input.value = task.note || '';
      input.disabled = false;
      ready = true;
      save.disabled = false;
      feedback.textContent = '';
      focusEditorEnd(input);
    } catch (error) {
      if (!current()) return;
      feedback.textContent = `备注读取失败：${error.message}`;
      retry.hidden = false;
    }
  };
  retry.onclick = load;
  save.onclick = async () => {
    if (!current() || !ready || saving) return;
    const value = input.value.replace(/\r\n?/g, '\n').trim() || null;
    saving = true;
    input.disabled = save.disabled = cancel.disabled = true;
    feedback.textContent = '正在保存…';
    let result;
    try {
      result = await gw(`/admin/api/planning/tasks/${taskId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: value }),
      });
    } catch (error) {
      saving = false;
      if (!current()) return;
      input.disabled = save.disabled = cancel.disabled = false;
      feedback.textContent = error.resultUnknown
        ? '未收到保存结果，请重试确认。' : `备注保存失败：${error.message}`;
      return;
    }
    const connected = current();
    restore(notePreviewBody(result.note ?? value, false));
    if (connected) toast('备注已保存');
    try { await onSaved(result); } catch (error) {
      console.error({ location: 'planning_notes.refreshInlineNote', taskId, stack: error.stack });
      if (connected) toast('备注已保存，列表更新失败，请刷新重试', 'warn');
    }
  };
  void load();
  return { root, close, isActive: () => current(), focus: () => input.focus({ preventScroll: true }) };
}

export function openTaskNote({ taskId, note = '', readOnly = false, onSaved = async () => {} }) {
  const session = modal({
    title: readOnly ? '历史备注' : '待办备注',
    body: readOnly
      ? `<div class="plan-note-full${note ? ' copyable-text' : ' muted'}">${esc(note || '当时未填写备注')}</div>
         <p class="field-hint">这条待办已经结束，保留当时的备注。</p>`
      : `${noteInputHtml(note)}<p data-note-feedback class="field-hint" role="status">正在读取备注…</p>`,
    footer: `<button class="btn btn-secondary" data-cancel>${readOnly ? '关闭' : '取消'}</button>
      ${readOnly ? '' : '<button class="btn btn-secondary" data-retry hidden>重新读取</button><button class="btn btn-primary" data-save disabled>保存</button>'}`,
  });
  const { root, close } = session;
  root.classList.add('task-note-modal');
  root.querySelector('[data-cancel]').onclick = close;
  if (readOnly) return session;

  const save = root.querySelector('[data-save]');
  const cancel = root.querySelector('[data-cancel]');
  const dismiss = root.querySelector('.modal-close');
  const retry = root.querySelector('[data-retry]');
  const feedback = root.querySelector('[data-note-feedback]');
  let ready = false;
  let saving = false;
  let committed = false;
  const editor = bindNoteInput(root, 'task-note-text', valid => {
    save.disabled = !ready || saving || committed || !valid;
  });
  editor.input.disabled = true;
  const load = async () => {
    ready = false;
    retry.hidden = true;
    feedback.textContent = '正在读取备注…';
    editor.update();
    try {
      const task = await gw(`/admin/api/planning/tasks/${taskId}`);
      if (!root.isConnected) return;
      editor.input.value = task.note || '';
      editor.input.disabled = false;
      ready = true;
      feedback.textContent = '';
      editor.update();
      focusEditorEnd(editor.input);
    } catch (error) {
      if (!root.isConnected) return;
      feedback.textContent = `备注读取失败：${error.message}`;
      retry.hidden = false;
    }
  };
  retry.onclick = load;
  save.onclick = async () => {
    if (!ready || saving || committed) return;
    let value;
    try { value = editor.read(); } catch (error) {
      feedback.textContent = error.message;
      return;
    }
    saving = true;
    cancel.disabled = true;
    dismiss.disabled = true;
    save.textContent = '正在保存…';
    feedback.textContent = '';
    editor.input.disabled = true;
    editor.update();
    let result;
    try {
      result = await gw(`/admin/api/planning/tasks/${taskId}`, {
        method: 'PATCH', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ note: value }),
      });
    } catch (error) {
      saving = false;
      if (!root.isConnected) return;
      editor.input.disabled = false;
      cancel.disabled = false;
      dismiss.disabled = false;
      save.textContent = '保存';
      feedback.textContent = error.resultUnknown
        ? '未收到保存结果，请重试确认。' : `备注保存失败：${error.message}`;
      editor.update();
      return;
    }
    committed = true;
    const wasConnected = root.isConnected;
    if (wasConnected) {
      close();
      toast('备注已保存');
    }
    // The write outlives its modal: notify the application even after navigation.
    try { await onSaved(result); } catch (error) {
      console.error({ location: 'planning_notes.refreshAfterSave', taskId, stack: error.stack });
      if (wasConnected) toast('备注已保存，列表更新失败，请刷新重试', 'warn');
    }
  };
  void load();
  return session;
}
