// Field editors keep the existing detail layout, selection and atomic receipt.
import { gw } from '../api.js?v=20261025-console-latency-r1';
import { esc, toast, icon } from '../ui.js?v=20261025-console-latency-r1';
import { bindNoteInput, focusEditorEnd } from './planning_notes.js?v=20261025-console-latency-r1';

export const resultUnknown = error => error?.resultUnknown || !error?.status || error.status >= 500;
const scopeText = value => value === 'current_and_future' ? '当前及未来' : '仅未来';
const clock = value => value ? new Date(new Date(value).getTime() + 8 * 3600000).toISOString().slice(11, 16) : '';
const groupFor = key => ['alarm_start', 'alarm_end', 'timer_minutes'].includes(key) ? 'alarm' : key === 'note' ? 'note' : key.includes('minutes') ? 'duration' : key.startsWith('window_') ? 'window' : 'name';

export function createPlanningDetailEdit({ getDetail, captureOwner, isCurrent, initRetroFields, onSaved, onCancel }) {
  const drafts = new Map(), pending = new Map(), parking = new Map(), uncertain = new Map();
  let session = null;
  const owns = s => session === s && isCurrent(s.owner) && s.host?.isConnected;
  const notifySaved = async (taskId, result, owner) => {
    try { await onSaved(result, owner); }
    catch (error) {
      globalThis.console?.error({ location: 'planning_detail_edit.refreshAfterSave', stack: error.stack, taskId });
      toast('待办已保存，列表更新失败，请刷新重试', 'warn');
    }
  };
  const feedback = (s, text) => { if (owns(s)) s.host.querySelector('[data-edit-feedback]').textContent = text; };
  const fields = s => {
    const values = { ...s.values };
    if (s.detachedValues) return { ...values, ...s.detachedValues };
    const name = s.title.querySelector('[data-edit-title]');
    if (name) values[s.nameField] = name.value;
    if (s.note) values.note = s.note.read();
    for (const input of s.root.querySelectorAll('[data-edit-field]')) values[input.dataset.editField] = input.value;
    for (const [id, key] of [['pe-window-start', 'window_start_tod'], ['pe-window-end', 'window_end_tod']]) {
      const input = s.root.querySelector(`#${id}`);
      if (input) values[key] = input.value || null;
    }
    const alarms = s.root.querySelector('[data-alarm-controls]');
    if (alarms) {
      values.alarm_start = alarms.querySelector('[data-alarm-start]').checked;
      values.alarm_end = alarms.querySelector('[data-alarm-end]').checked;
      values.timer_minutes = alarms.querySelector('[data-timer-input]').value.trim() || null;
    }
    return values;
  };
  const draft = s => Object.fromEntries(Object.entries(fields(s)).filter(([k, v]) => v !== s.baseline[k]));
  const preview = (s, field, target) => {
    target.innerHTML = s.originals.get(target);
    const button = target.querySelector('[data-act="task-detail-edit"]');
    const keys = field === 'name' ? [s.nameField] : field === 'note' ? ['note'] : field === 'duration' ? s.timeFields : ['window_start_tod', 'window_end_tod'];
    const dirty = keys.some(key => s.values[key] !== s.baseline[key]);
    const phase = s.nameField !== 'content' && s.context.occurrences.find(row => row.id === s.occurrenceId)?.phase;
    if (field === 'name' && button) button.innerHTML = `${esc(s.values[s.nameField])}${phase ? phase === 'start' ? '·开始' : '·结束' : ''}${icon('edit')}`;
    else if (field === 'duration' && button) button.textContent = s.timeFields.map(key => `${s.values[key]}m`).join(' / ');
    else if (field === 'window' && button) button.textContent = s.kind === 'occ'
      ? [s.values.window_start_tod ? `不早于 ${s.values.window_start_tod}` : '', s.values.window_end_tod ? `最晚完成 ${s.values.window_end_tod}` : ''].filter(Boolean).join('，') || '未设置'
      : `${s.values.window_start_tod || '无'} ～ ${s.values.window_end_tod || '无'}`;
    else if (field === 'note') {
      const text = target.querySelector('.plan-task-note-text, .plan-task-note-empty');
      if (text) {
        text.textContent = s.values.note || '点击添加备注';
        text.classList.toggle('plan-task-note-empty', !s.values.note);
        text.classList.toggle('plan-task-note-text', !!s.values.note);
      }
      const input = target.querySelector('textarea');
      if (input) input.value = s.values.note || '';
    }
    (button || target.querySelector('.plan-task-note'))?.classList.toggle('is-draft', dirty);
  };
  const restore = s => {
    for (const [el, html] of s.originals) if (el.isConnected) el.innerHTML = html;
    for (const [el, disabled] of s.blocked) if (el.isConnected) el.disabled = disabled;
    clearTimeout(s.blurTimer);
    for (const [event, handler] of Object.entries(s.listeners || {})) s.root.removeEventListener(event, handler);
    s.root.removeEventListener('keydown', s.keydown);
    s.root.classList.remove('is-inline-editing');
    s.host.innerHTML = s.originalSave;
    delete s.host.dataset.detailEditBar;
  };
  const busy = (s, locked) => {
    if (!owns(s)) return;
    s.root.querySelectorAll('[data-editor-control], [data-edit-title], [data-edit-field], #pe-note, [data-alarm-controls] input, [data-inline-window] input, [data-inline-window] button').forEach(el => {
      el.disabled = locked || el.dataset.editLocked === 'true';
    });
    for (const [el, disabled] of s.blocked) if (el.isConnected) el.disabled = locked || disabled;
    s.host.querySelector('[data-edit-save]').textContent = s.unknown ? '核实保存结果' : locked ? '正在保存…' : '保存';
  };
  const api = {
    isBlocked(taskId) { return pending.has(taskId) || session?.taskId === taskId && session.loading
      || [...uncertain.values()].some(value => value.taskId === taskId); },
    isActive(kind, id) { return !!session && isCurrent(session.owner) && session.kind === kind && session.id === id; },
    detach() {
      if (!session) return;
      const s = session;
      if (s.ready) {
        drafts.set(s.key, draft(s));
        // Starting the write captures this owner and request before selection changes.
        if (!s.failed && !s.unknown && !s.composing) void api.save(s, true);
      } else if (s.loading && s.activeFields.has('alarm')) {
        // A reminder can change before the guarded context GET finishes.
        // Capture its values while this DOM still belongs to the old selection.
        s.detachedValues = fields(s);
        drafts.set(s.key, s.detachedValues);
        pending.set(s.taskId, s);
      }
      if (session === s) { restore(s); session = null; }
    },
    async saveCurrent(taskId, occurrence = null) {
      const owner = captureOwner();
      await api.open(taskId, occurrence, 'alarm');
      const s = session;
      if (!isCurrent(owner) || !s || s.taskId !== taskId) return;
      await s.loadPromise;
      if (!isCurrent(owner)) return;
      return api.save(s);
    },
    async finish() {
      const s = session;
      if (!s) return true;
      await s.loadPromise;
      if (!owns(s) || s.unknown || s.failed) return false;
      await api.save(s, true);
      if (session === s && (s.unknown || s.failed)) return false;
      if (session && (session !== s || !owns(s))) return false;
      if (session === s) api.detach();
      return true;
    },
    async open(taskId, occurrence = null, field = 'name') {
      const kind = occurrence ? 'occ' : 'task', id = occurrence?.id ?? taskId;
      if (api.isActive(kind, id)) {
        if (session.unknown || session.loading || pending.has(taskId)) return;
        session.activeFields.add(field);
        if (session.ready) api.activate(session, field);
        await session.loadPromise;
        return;
      }
      if (pending.has(taskId)) { toast('该待办正在保存，请等待结果', 'warn'); return; }
      if ([...uncertain].some(([key, value]) => value.taskId === taskId && key !== `${kind}:${id}`)) {
        toast('请先返回原编辑轮次核实保存结果', 'warn'); return;
      }
      api.detach();
      const detail = getDetail();
      if (!detail.body.querySelector('[data-detail-edit-slot]')) return;
      const host = detail.body.querySelector('[data-detail-save-slot]');
      if (!host) return;
      const originalSave = host.innerHTML;
      host.dataset.detailEditBar = '';
      const s = { taskId, occurrenceId: occurrence?.id ?? null, kind, id, key: `${kind}:${id}`,
        owner: captureOwner(), originalSave, host, root: detail.el, title: detail.el.querySelector('.detail-title'),
        originals: new Map(), blocked: new Map(), activeFields: new Set([field]), ready: false, loading: true, unknown: false };
      session = s;
      detail.el.classList.add('is-inline-editing');
      // Keep the original facts, reminder controls and operation groups in place.
      for (const el of detail.el.querySelectorAll('.detail-foot [data-act]')) {
        s.blocked.set(el, el.disabled);
      }
      host.innerHTML = '<span role="status">正在读取生效范围…</span>';
      s.keydown = event => {
        if (!owns(s) || event.isComposing || event.keyCode === 229) return;
        if (event.key === 'Escape' && event.target.closest('[data-edit-title], [data-edit-field], #pe-note, [data-inline-window]')) {
          event.preventDefault(); event.stopPropagation(); api.cancel(s); return;
        }
        if (event.key !== 'Enter' || event.shiftKey || event.ctrlKey || event.altKey || event.metaKey
            || !window.matchMedia('(hover: hover) and (pointer: fine)').matches
            || !event.target.matches('[data-edit-title], [data-edit-field], #pe-note, [data-inline-window] .retro-time-field')) return;
        event.preventDefault(); event.stopPropagation(); void api.save(s);
      };
      const editable = '[data-edit-title], [data-edit-field], #pe-note, [data-inline-window], [data-alarm-controls] input';
      s.listeners = {
        input: () => { if (!s.needsReload) s.failed = false; },
        compositionstart: () => { s.composing = true; },
        compositionend: () => { s.composing = false; },
        focusout: event => {
          if (!event.target.closest(editable)) return;
          if (event.relatedTarget?.closest('[data-act="task-detail-edit"]')) return;
          clearTimeout(s.blurTimer);
          s.blurTimer = setTimeout(() => {
            if (!owns(s) || !s.ready || s.composing || s.failed || s.loading) return;
            const active = document.activeElement;
            if (active?.closest(`${editable}, [data-detail-save-slot], .retro-time-pop`)) return;
            void api.save(s, true);
          }, 0);
        },
      };
      for (const [event, handler] of Object.entries(s.listeners)) s.root.addEventListener(event, handler);
      s.root.addEventListener('keydown', s.keydown);
      s.loadPromise = (async () => {
      try {
        s.context = await gw(`/admin/api/planning/tasks/${taskId}/detail-edit${occurrence ? `?occurrence_id=${occurrence.id}` : ''}`);
        s.occurrenceId = s.context.occurrence_id;
        s.loading = false;
        if (!owns(s)) {
          if (s.detachedValues) {
            api.render(s, true);
            pending.delete(s.taskId);
            await api.save(s, true, true);
          }
          return;
        }
        api.render(s);
      } catch (error) {
        if (!owns(s)) {
          if (pending.get(s.taskId) === s) pending.delete(s.taskId);
          if (s.detachedValues) toast(`提醒未保存：${error.message}。草稿已保留，请返回原待办重试`, 'warn');
          return;
        }
        s.loading = false;
        host.innerHTML = `<span role="alert">读取失败：${esc(error.message)}</span><button class="btn btn-secondary btn-sm" data-edit-retry>重试</button><button class="btn btn-secondary btn-sm" data-edit-cancel>取消</button>`;
        host.querySelector('[data-edit-retry]').onclick = () => { api.detach(); api.open(taskId, occurrence, field); };
        host.querySelector('[data-edit-cancel]').onclick = () => api.cancel(s);
      }
      })();
      await s.loadPromise;
    },
    render(s, detached = false) {
      const { task, occurrences, scope } = s.context;
      const selected = occurrences.find(r => r.id === s.occurrenceId);
      s.nameField = selected?.phase && s.kind === 'occ' ? `hollow_${selected.phase}_content` : 'content';
      const currentTime = scope.time === 'current_and_future';
      s.timeFields = task.is_hollow ? ['hollow_start_minutes', 'hollow_end_minutes'] : ['estimated_minutes'];
      const initialName = s.nameField === 'content'
        ? scope.text === 'current_and_future' ? selected.task_content : task.content
        : (scope.name === 'current_and_future' ? selected.content.replace(/·(?:开始|结束)$/, '') : task[s.nameField] || task.content);
      const initial = { [s.nameField]: initialName, note: task.note ?? null,
        alarm_start: !!task.alarm_start, alarm_end: !!task.alarm_end, timer_minutes: task.timer_minutes == null ? null : String(task.timer_minutes),
        window_start_tod: currentTime ? clock(selected.window_start_at) || null : task.window_start_tod?.slice(0, 5) || null,
        window_end_tod: currentTime ? clock(selected.window_end_at) || null : task.window_end_tod?.slice(0, 5) || null };
      for (const k of s.timeFields) {
        const phase = k === 'hollow_start_minutes' ? 'start' : k === 'hollow_end_minutes' ? 'end' : null;
        initial[k] = String(currentTime ? occurrences.find(r => (r.phase ?? null) === phase)?.estimated_minutes ?? task[k] ?? '' : task[k] ?? '');
      }
      if (detached) {
        s.baseline = initial;
        s.values = { ...initial, ...s.detachedValues };
        s.ready = true;
        s.readOnly = task.task_type === 'once' && occurrences.length > 0 && scope.text === 'future';
        return;
      }
      for (const [el, html] of s.originals) el.innerHTML = html;
      s.note = null;
      s.baseline = initial;
      s.values = { ...initial, ...drafts.get(s.key) };
      for (const key of Object.keys(drafts.get(s.key) || {})) s.activeFields.add(groupFor(key));
      s.noFutureTime = task.task_type === 'once' && occurrences.length > 0 && !currentTime;
      s.readOnly = task.task_type === 'once' && occurrences.length > 0 && scope.text === 'future';
      s.resident = task.task_type === 'once' && !task.target_date;
      s.host.innerHTML = `<button class="btn btn-secondary btn-sm" data-editor-control data-edit-save>${icon('check')}保存</button>
        <button class="btn btn-secondary btn-sm" data-editor-control data-edit-reload hidden>重读生效范围</button>
        <span class="plan-detail-edit-status" data-edit-feedback role="status" aria-live="polite"></span>`;
      const unresolved = uncertain.get(s.key);
      if (unresolved) {
        s.unknown = true;
        s.request = unresolved.request;
        s.values = { ...s.values, ...s.request.body.changes };
        for (const key of Object.keys(s.request.body.changes)) s.activeFields.add(groupFor(key));
      }
      const alarmDraft = { ...drafts.get(s.key), ...unresolved?.request.body.changes };
      for (const [key, selector] of [['alarm_start', '[data-alarm-start]'], ['alarm_end', '[data-alarm-end]'], ['timer_minutes', '[data-timer-input]']]) {
        const input = s.root.querySelector(selector);
        if (!input || !(key in alarmDraft)) continue;
        if (input.type === 'checkbox') input.checked = !!s.values[key];
        else input.value = s.values[key] || '';
      }
      s.ready = true;
      const focusField = s.openField || [...s.activeFields].at(-1);
      for (const field of s.activeFields) api.activate(s, field);
      api.activate(s, focusField);
      if (s.readOnly) {
        feedback(s, '该单次待办已经结束，历史记录不可修改，也没有未来轮次。');
      }
      busy(s, !!s.unknown);
      if (s.unknown) {
        s.host.querySelector('[data-edit-save]').disabled = false;
        feedback(s, '上一次保存结果未知，请核实原请求；草稿已保留。');
      }
      s.host.querySelector('[data-edit-save]').onclick = () => api.save(s);
      s.host.querySelector('[data-edit-reload]').onclick = () => api.reload(s);
    },
    activate(s, field) {
      if (!owns(s) || !s.ready || field === 'alarm') return;
      const target = field === 'name' ? s.title : s.root.querySelector(`[data-detail-field="${field}"]`);
      if (!target) return;
      s.values = fields(s);
      for (const previous of s.activeFields) {
        if (previous === 'alarm' || previous === field) continue;
        const oldTarget = previous === 'name' ? s.title : s.root.querySelector(`[data-detail-field="${previous}"]`);
        if (oldTarget && s.originals.has(oldTarget)) preview(s, previous, oldTarget);
      }
      if (field !== 'note') s.note = null;
      s.openField = field;
      s.activeFields.add(field);
      const scope = s.context.scope;
      const scopeValue = field === 'name' ? scope.name || scope.text : field === 'note' ? scope.text : scope.time;
      s.host.querySelector('[data-edit-save]').setAttribute('data-tooltip', `${{ name: '名称', note: '备注', duration: '耗时', window: '时段' }[field]} · ${scopeText(scopeValue)}；离开编辑位置自动保存`);
      if (target.querySelector('[data-edit-title], #pe-note, [data-edit-field], [data-inline-window]')) {
        target.querySelector('input:not([type="hidden"]),textarea,button')?.focus({ preventScroll: true }); return;
      }
      if (!s.originals.has(target)) s.originals.set(target, target.innerHTML);
      const locked = s.readOnly || (s.noFutureTime && ['duration', 'window'].includes(field));
      if (s.noFutureTime && ['duration', 'window'].includes(field)) feedback(s, '该单次待办已经开始或结束，时间不可修改。');
      if (field === 'name') {
        target.innerHTML = `<input type="text" class="plan-inline-title" data-edit-title aria-label="${s.nameField === 'content' ? '待办名称' : '阶段名称'}" maxlength="${s.nameField === 'content' ? 500 : 200}" value="${esc(s.values[s.nameField])}">`;
      } else if (field === 'note') {
        target.innerHTML = `<div class="plan-task-note is-note-editing"><div class="plan-task-note-head"><label for="pe-note">备注</label><span id="pe-note-count" class="char-count"></span></div>
          <textarea id="pe-note" rows="3" aria-label="编辑备注" placeholder="点击添加备注">${esc(s.values.note || '')}</textarea></div>`;
        s.note = bindNoteInput(target, 'pe-note');
      } else if (field === 'duration') {
        const selected = s.context.occurrences.find(row => row.id === s.occurrenceId);
        const visibleFields = s.kind === 'occ' && selected?.phase ? [`hollow_${selected.phase}_minutes`] : s.timeFields;
        target.innerHTML = `<span class="plan-inline-durations">${visibleFields.map(k => `<label>${visibleFields.length > 1 ? k === 'hollow_start_minutes' ? '开始 ' : '结束 ' : ''}<input type="text" inputmode="numeric" data-edit-field="${k}" aria-label="${k === 'hollow_start_minutes' ? '开始阶段耗时' : k === 'hollow_end_minutes' ? '结束阶段耗时' : '预估耗时'}" value="${esc(s.values[k])}"><span>分钟</span></label>`).join('')}</span>`;
      } else if (field === 'window') {
        target.innerHTML = `<div class="plan-inline-window" data-inline-window><div class="retro-time" data-retro-for="pe-window-start" data-retro-mode="time" data-retro-value="${esc(s.values.window_start_tod)}" aria-label="最早开始"></div><span>～</span><div class="retro-time" data-retro-for="pe-window-end" data-retro-mode="time" data-retro-value="${esc(s.values.window_end_tod)}" aria-label="最晚完成"></div></div>`;
        initRetroFields(target);
        if (s.resident) feedback(s, '无日期单次待办常驻显示，不设可安排时段。');
      }
      target.querySelectorAll('input,textarea,button').forEach(el => {
        if (locked || field === 'window' && s.resident) { el.disabled = true; el.dataset.editLocked = 'true'; }
      });
      focusEditorEnd(target.querySelector('input:not([type="hidden"]),textarea,button'));
    },
    cancel(s) {
      if (!owns(s) || pending.has(s.taskId) || s.unknown) return;
      drafts.delete(s.key);
      restore(s);
      session = null;
      onCancel();
    },
    async reload(s) {
      if (!owns(s) || pending.has(s.taskId) || s.unknown) return;
      drafts.set(s.key, draft(s));
      s.loading = true;
      busy(s, true);
      try {
        const ctx = await gw(`/admin/api/planning/tasks/${s.taskId}/detail-edit${s.occurrenceId ? `?occurrence_id=${s.occurrenceId}` : ''}`);
        if (!owns(s)) return;
        s.context = ctx;
        s.failed = s.needsReload = false;
        api.render(s);
        feedback(s, '已重读生效范围，草稿保留。请核对范围后再次保存。');
      } catch (error) { feedback(s, `重读失败：${error.message}；草稿已保留。`); }
      finally { s.loading = false; busy(s, false); }
    },
    async save(s, automatic = false, detached = false) {
      if (!(owns(s) || detached && s.detachedValues) || !s.ready || s.loading || pending.has(s.taskId) || automatic && (s.failed || s.unknown || s.composing)) return;
      if (!s.unknown) {
        const values = fields(s);
        const changes = Object.fromEntries(Object.entries(values).filter(([k, v]) => v !== s.baseline[k]));
        if (!Object.keys(changes).length) {
          if (owns(s)) { restore(s); session = null; }
          if (!automatic) toast('没有需要保存的修改');
          return;
        }
        if (!values[s.nameField].trim()) { s.failed = true; api.activate(s, 'name'); feedback(s, '请填写待办名称'); return; }
        if ('window_start_tod' in changes || 'window_end_tod' in changes) {
          changes.window_start_tod = values.window_start_tod;
          changes.window_end_tod = values.window_end_tod;
        }
        s.request = { key: crypto.randomUUID(), body: { changes, token: s.context.token, occurrence_id: s.occurrenceId } };
      }
      pending.set(s.taskId, s);
      busy(s, true);
      feedback(s, '正在校验并保存…');
      let saved;
      try {
        saved = await gw(`/admin/api/planning/tasks/${s.taskId}/detail-edit`, {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': s.request.key },
          body: JSON.stringify(s.request.body),
        });
      } catch (error) {
        s.failed = true;
        s.needsReload = error.code === 'concurrent_modified';
        drafts.set(s.key, Object.fromEntries(Object.entries(s.request.body.changes)));
        s.unknown = resultUnknown(error);
        if (s.unknown) uncertain.set(s.key, { taskId: s.taskId, request: s.request });
        else uncertain.delete(s.key);
        if (!owns(s)) toast('详情未确认保存，草稿已保留，请返回原待办核实', 'warn');
        feedback(s, s.unknown ? '保存结果暂时无法确认。草稿与本次请求已保留，请点击「核实保存结果」。' : `未保存：${error.message}。草稿已保留。`);
        if (owns(s)) s.host.querySelector('[data-edit-reload]').hidden = error.code !== 'concurrent_modified';
        return;
      } finally {
        pending.delete(s.taskId);
        busy(s, !!s.unknown);
        if (owns(s) && s.unknown) s.host.querySelector('[data-edit-save]').disabled = false;
      }
      drafts.delete(s.key);
      uncertain.delete(s.key);
      if (owns(s)) {
        restore(s);
        session = null;
      }
      const changes = s.request.body.changes;
      const ranges = [];
      if (s.nameField in changes) ranges.push(`名称${scopeText(saved.scope.name || saved.scope.text)}`);
      if ('note' in changes) ranges.push(`备注${scopeText(saved.scope.text)}`);
      if (Object.keys(changes).some(k => k.includes('minutes') && k !== 'timer_minutes' || k.startsWith('window_'))) ranges.push(`时间${scopeText(saved.scope.time)}`);
      if (['alarm_start', 'alarm_end', 'timer_minutes'].some(k => k in changes)) ranges.push('提醒已更新');
      toast(automatic ? '已自动保存' : `待办已保存（${ranges.join('，')}）`);
      await notifySaved(s.taskId, saved, s.owner);
    },
    async park(occurrence) {
      const owner = captureOwner();
      const taskId = occurrence.task_id;
      const prior = parking.get(occurrence.id);
      if (prior?.busy || pending.has(taskId)) return;
      const op = prior || { key: crypto.randomUUID(), body: null };
      parking.set(occurrence.id, op);
      op.busy = true;
      try {
        if (!op.body) {
          const ctx = await gw(`/admin/api/planning/tasks/${taskId}/detail-edit?occurrence_id=${occurrence.id}`);
          op.body = { occurrence_id: occurrence.id, token: ctx.token, parked: !occurrence.is_parked };
        }
        const saved = await gw(`/admin/api/planning/tasks/${taskId}/detail-edit`, { method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': op.key }, body: JSON.stringify(op.body) });
        parking.delete(occurrence.id);
        toast(op.body.parked ? '当前及后续所有轮次已沉底；截止和提醒仍按原设置' : '当前及后续所有轮次已恢复排程，将按当前约束重新计算');
        await notifySaved(taskId, saved, owner);
      } catch (error) {
        if (!resultUnknown(error)) parking.delete(occurrence.id);
        toast(resultUnknown(error) ? '操作结果未知，请再次点击以核实同一次操作' : `操作未执行：${error.message}`, 'warn');
      } finally { op.busy = false; }
    },
  };
  return api;
}
