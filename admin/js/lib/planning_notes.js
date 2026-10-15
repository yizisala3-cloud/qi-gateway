import { gw } from '../api.js?v=20261014-admin-layout-help-scrollbars';
import { esc, icon, modal, toast } from '../ui.js?v=20261014-admin-layout-help-scrollbars';

// Unicode code points match Python len() and PostgreSQL char_length().
export const TASK_NOTE_LIMIT = 60;
export const noteLength = value => Array.from(String(value || '').replace(/\r\n?/g, '\n')).length;

export function noteInputHtml(value = '', id = 'task-note-text', rows = 5) {
  return `<div class="field task-note-field">
    <label for="${id}">备注<span class="char-count" id="${id}-count" aria-live="polite"></span></label>
    <textarea id="${id}" rows="${rows}" maxlength="${TASK_NOTE_LIMIT * 2}" aria-describedby="${id}-hint ${id}-error" placeholder="添加备注（最多 ${TASK_NOTE_LIMIT} 字）">${esc(value || '')}</textarea>
    <p class="field-hint" id="${id}-hint">最多 ${TASK_NOTE_LIMIT} 字；修改仅更新未结束的待办和后续待办，历史备注保留。</p>
    <p class="task-note-error" id="${id}-error" role="alert" hidden></p>
  </div>`;
}

export function bindNoteInput(root, id = 'task-note-text', onValidity = () => {}) {
  const input = root.querySelector(`#${id}`);
  const counter = root.querySelector(`#${id}-count`);
  const error = root.querySelector(`#${id}-error`);
  const update = () => {
    const length = noteLength(input.value);
    const valid = length <= TASK_NOTE_LIMIT;
    counter.textContent = `${length}/${TASK_NOTE_LIMIT}`;
    input.setAttribute('aria-invalid', String(!valid));
    error.hidden = valid;
    error.textContent = valid ? '' : `备注最多 ${TASK_NOTE_LIMIT} 字，请删去 ${length - TASK_NOTE_LIMIT} 字后保存。`;
    onValidity(valid);
    return valid;
  };
  input.addEventListener('input', update);
  update();
  return {
    update, input,
    read() {
      if (!update()) throw new Error(`备注最多 ${TASK_NOTE_LIMIT} 字`);
      return input.value.replace(/\r\n?/g, '\n').trim() || null;
    },
  };
}

export function taskNotePreview(taskId, note, { readOnly = false } = {}) {
  return `<div class="plan-task-note" role="button" tabindex="0" data-act="task-note" data-id="${taskId}"
      aria-label="${readOnly ? '查看历史备注' : note ? '编辑待办备注' : '添加待办备注'}">
    <div class="plan-task-note-head"><span>备注</span>${icon(readOnly ? 'book' : 'edit')}</div>
    ${note ? `<div class="plan-task-note-text copyable-text">${esc(note)}</div>`
      : `<div class="plan-task-note-empty">${readOnly ? '当时未填写备注' : '点击添加备注'}</div>`}
  </div>`;
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
      editor.input.focus();
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
