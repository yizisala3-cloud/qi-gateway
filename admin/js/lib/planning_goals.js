// Goal UI owns its reads, drafts and durable operation identities. Progress is
// always supplied by the server; task creation reuses the existing task form.
import { gw } from '../api.js?v=20261018-planning-latency';
import { modal, confirm, toast, esc, icon, loading, empty, errorBlock } from '../ui.js?v=20261018-planning-latency';
import { createRetroSelectField } from './retro_select.js?v=20261018-planning-latency';
import { TASK_TYPE_LABELS } from './planning_display.js?v=20261018-planning-latency';

export const GOAL_TASK_TYPES = ['daily', 'interval', 'weekly', 'monthly'];
const BASE = '/admin/api/planning/goals';
const STORE = 'qi-planning-goal-pending-v1';
export const resultUnknown = error => !error?.status || error.status >= 500 || error.resultUnknown;

export function goalProgress(goal) {
  const percentage = Number(Number(goal.percentage).toFixed(2));
  return { text: `${goal.executed_count}/${goal.planned_count}`, percentage: `${percentage}%`,
    width: Math.max(0, Math.min(100, percentage)) };
}

function progressHtml(goal) {
  const p = goalProgress(goal);
  return `<div class="goal-progress-label"><span>${esc(p.text)} 次</span><strong>${esc(p.percentage)}</strong></div>
    <div class="goal-progress" role="progressbar" aria-label="目标完成进度" aria-valuemin="0" aria-valuemax="100"
      aria-valuenow="${p.width}" aria-valuetext="${esc(p.text)} 次，${esc(p.percentage)}"><span style="width:${p.width}%"></span></div>`;
}

const description = goal => `${goal.content} · ${goal.target_value} ${goal.unit}`;
const actionButton = (action, text, id = '', style = 'secondary') =>
  `<button class="btn btn-${style} btn-sm" data-goal-act="${action}" data-goal-id="${id}">${text}</button>`;

export function createGoalOperation(action, id, payload) {
  const suffix = action === 'link' ? '/link' : action === 'adjust' ? '/progress' : '';
  return { key: crypto.randomUUID(), action, id, payload: structuredClone(payload),
    path: action === 'create' ? BASE : `${BASE}/${id}${suffix}`,
    method: action === 'create' || suffix ? 'POST' : action === 'delete' ? 'DELETE' : 'PATCH' };
}

export async function sendGoalOperation(op) {
  return gw(op.path, { method: op.method,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': op.key },
    body: JSON.stringify(op.payload) });
}

export function createPlanningGoals({ getDetail, clearSelection, openTaskForm, onTaskChanged }) {
  let root = null, shown = false, epoch = 0, readId = 0, selected = null;
  let rows = [], loaded = false, readError = '', editor = null;
  let pendingRead = null;
  let presentation = 0;
  let lifecycle = new AbortController(), actionRoot = null, actionListener = null;
  const operations = new Map(), flows = new Map(), busy = new Set();
  const operationWrites = new Map();
  const dialogs = new Set();
  const ask = async (owner, message, options = {}) => {
    if (!canPresent(owner)) return false;
    const accepted = await confirm(message, { ...options, signal: owner.signal });
    return accepted && canPresent(owner);
  };

  function persist() {
    try {
      sessionStorage.setItem(STORE, JSON.stringify({
        operations: [...operations.values()], flows: [...flows.values()],
      }));
    } catch (error) {
      console.error({ location: 'planning_goals.persist', stack: error.stack });
      toast('浏览器未能保留操作身份，请在当前页面核实保存结果后再刷新', 'warn');
    }
  }
  try {
    const stored = JSON.parse(sessionStorage.getItem(STORE) || '{}');
    for (const value of stored.operations || []) {
      if (!['create', 'edit', 'adjust', 'link', 'delete'].includes(value.action)) continue;
      // Reconstruct endpoints rather than trusting a path read from storage.
      const op = { ...createGoalOperation(value.action, value.id, value.payload), key: value.key };
      operations.set(op.key, op);
    }
    for (const flow of stored.flows || []) {
      if (flow.goalId && flow.taskKey && flow.taskBody) {
        flow.state = flow.taskId ? 'link_failed' : 'creation_unknown';
        if (flow.linkOp) flow.linkOp = { ...createGoalOperation('link', flow.goalId, flow.linkOp.payload), key: flow.linkOp.key };
        flows.set(flow.goalId, flow);
      }
    }
  } catch (error) {
    console.error({ location: 'planning_goals.restore', stack: error.stack });
    readError = '上次操作记录读取失败，请先核对目标和待办的实际保存结果';
  }

  const current = owner => root && epoch === owner;
  const beginPresentation = () => ({ epoch, serial: ++presentation, signal: lifecycle.signal });
  const canPresent = owner => !!owner && current(owner.epoch) && shown
    && owner.serial === presentation && !owner.signal.aborted;
  const find = id => rows.find(row => row.id === id);
  function dialog(options) {
    let handle;
    handle = modal({ ...options, onDismiss: () => {
      dialogs.delete(handle);
      if (editor === handle) editor = null;
      options.onDismiss?.();
    } });
    dialogs.add(handle);
    return handle;
  }

  function recoveryHtml() {
    return [...operations.values()].map(op => `<div class="banner banner-danger goal-recovery" role="status">
      <span>${operationWrites.has(op.key) ? '正在等待目标保存结果。' : '目标保存结果待核实，请按原操作确认。'}</span>
      ${actionButton('recover', '核实保存结果', op.key)}</div>`).join('')
      + [...flows.values()].map(flow => `<div class="banner ${flow.taskId ? '' : 'banner-danger'} goal-recovery" role="status">
        <span>${esc(flow.taskId ? `待办已创建（#${flow.taskId}），关联未完成` : '待办创建结果待核实')}
          ${flow.error ? `：${esc(flow.error)}` : ''}</span>
        ${actionButton('recover-task', flow.taskId ? '重试关联' : '核实创建结果', flow.goalId)}
        ${flow.taskId && !flow.linkOp ? actionButton('keep-task', '保留待办并结束', flow.goalId, 'quiet') : ''}</div>`).join('');
  }

  function render(forceDetail = false) {
    if (!root) return;
    root.innerHTML = `${recoveryHtml()}${readError ? `<div role="alert">${errorBlock(esc(readError))}
      ${actionButton('reload', '重新读取')}</div>` : ''}
      ${!loaded ? (readError ? '' : loading()) : !rows.length ? empty('还没有长期目标', '点击上方“新建目标”，保存后即可关联重复待办。') :
        `<div class="goal-list">${rows.map(goal => `<button type="button" class="goal-card ${selected === goal.id ? 'selected' : ''}"
          data-goal-act="select" data-goal-id="${goal.id}">
          <span class="goal-card-title">${esc(description(goal))}</span>
          ${progressHtml(goal)}
          <span class="goal-card-meta"><span>${goal.achieved ? `${icon('check')}已达标` : '进行中'}</span>
          <span>${goal.current_link ? `关联：${esc(goal.current_link.task_content)}` : '未关联待办'}</span></span>
        </button>`).join('')}</div>`}`;
    if (selected && shown) renderDetail(forceDetail);
  }

  function renderDetail(force = false) {
    const goal = find(selected);
    if (!goal || !shown || !root) return;
    const detail = getDetail();
    if (!force && detail?.el?.closest('.page-with-detail')?.classList.contains('detail-collapsed')) return;
    if (!force && window.matchMedia('(max-width: 1100px)').matches && !detail?.isOpenMobile()) return;
    const link = goal.current_link;
    const prior = goal.history || [];
    detail?.render({ title: `<span class="goal-heading">${esc(description(goal))}</span>`,
      badges: `<span class="tag">${goal.achieved ? '已达标' : '进行中'}</span>`,
      html: `<div class="goal-detail">${progressHtml(goal)}
        ${actionButton('full', '查看目标全文', goal.id, 'quiet')}
        <p class="muted text-sm">完整完成整轮后自动累计，达标后继续记录。</p>
        <div class="goal-link"><strong>当前关联</strong><p>${link ? esc(link.task_content) : '未关联待办'}</p>
          ${link ? `<span class="muted text-sm">${esc(TASK_TYPE_LABELS[link.task_type] || link.task_type)}${link.refresh_enabled === false ? ' · 刷新已暂停' : ''}</span>` : '<p class="muted text-sm">可以先手动修正次数，稍后再关联。</p>'}</div>
        ${prior.length ? `<p class="muted text-sm">保留 ${prior.length} 段历史关联的贡献。</p>
          ${actionButton('history', '查看关联历史', goal.id, 'quiet')}` : ''}
        </div>`,
      actions: `<div class="goal-actions">${actionButton('edit', `${icon('edit')}编辑`, goal.id)}
        ${actionButton('adjust', '修正次数', goal.id)}
        ${actionButton('link', link ? '更换关联' : '关联已有待办', goal.id)}
        ${actionButton('new-task', '新建并关联待办', goal.id)}
        ${link ? actionButton('unlink', '解除关联', goal.id, 'quiet') : ''}
        ${actionButton('delete', '删除目标', goal.id, 'danger-line')}</div>` });
  }

  async function load() {
    if (!root || !shown) return;
    const owner = epoch, sequence = ++readId;
    const job = (async () => {
      try {
        const result = await gw(BASE);
        if (!current(owner) || sequence !== readId) return;
        if (!Array.isArray(result) || result.some(goal => !goal || !Number.isSafeInteger(goal.id)
          || typeof goal.content !== 'string' || !Number.isSafeInteger(goal.executed_count)
          || goal.executed_count < 0 || !Number.isSafeInteger(goal.planned_count) || goal.planned_count <= 0
          || !Number.isFinite(Number(goal.percentage)))) {
          throw new Error('未收到有效的目标数据，请重新读取');
        }
        rows = result; loaded = true; readError = '';
        if (selected && !find(selected)) { selected = null; clearSelection(); }
      } catch (error) {
        if (!current(owner) || sequence !== readId) return;
        readError = `目标读取失败：${error.message}。已有数据显示为上次读取结果。`;
      }
      if (current(owner) && sequence === readId) render();
    })();
    pendingRead = job;
    await job;
    if (pendingRead === job) pendingRead = null;
  }

  async function changed() {
    ++readId; // Any read begun before this committed write is obsolete.
    await load();
  }

  function commit(op) {
    const pending = operationWrites.get(op.key);
    if (pending) return pending;
    operations.set(op.key, op); persist();
    ++readId;
    // Reopening recovery while the original request is pending joins its real
    // outcome. A duplicate caller must never receive an empty success result.
    const work = Promise.resolve().then(async () => {
      try {
        const result = await sendGoalOperation(op);
        operations.delete(op.key); persist();
        void changed().catch(error => {
          console.error({ location: 'planning_goals.refreshAfterCommit', goalId: op.goalId, stack: error.stack });
          toast('目标操作已保存，列表更新失败，请重新读取', 'warn');
        });
        return result;
      } catch (error) {
        if (!resultUnknown(error)) operations.delete(op.key);
        persist();
        throw error;
      } finally {
        operationWrites.delete(op.key);
        render();
      }
    });
    operationWrites.set(op.key, work);
    return work;
  }

  async function fresh(id) { return gw(`${BASE}/${id}`); }
  function countValue(value, minimum, label) {
    if (!String(value).trim() || !Number.isSafeInteger(Number(value)) || Number(value) < minimum) {
      throw new Error(`${label}须为${minimum ? '正整数' : '非负整数'}`);
    }
    return Number(value);
  }

  async function form(action, id, owner) {
    if (editor?.root.isConnected) return;
    let goal = id ? await fresh(id) : null;
    if (!canPresent(owner) || editor?.root.isConnected) return;
    let op = null, submitting = false, conflicted = false;
    const editing = action === 'edit', adjusting = action === 'adjust';
    let body;
    if (adjusting) body = `<div class="field"><label for="goal-count">已执行次数</label>
      <input type="number" id="goal-count" min="0" step="1" value="${goal.executed_count}"></div>
      <p class="muted text-sm">输入希望显示的次数。修正会保存，之后完成的整轮继续累计。</p>`;
    else body = `<div class="field"><label for="goal-content">目标内容</label>
      <input id="goal-content" type="text" value="${esc(goal?.content || '')}" placeholder="例如：看书"></div>
      <div class="goal-fields"><div class="field"><label for="goal-value">目标数值</label>
        <input id="goal-value" type="number" step="any" min="0" value="${esc(goal?.target_value || '')}" placeholder="200"></div>
      <div class="field"><label for="goal-unit">单位</label><input id="goal-unit" value="${esc(goal?.unit || '')}" placeholder="页、h、天"></div></div>
      <div class="field"><label for="goal-planned">计划执行次数</label>
        <input id="goal-planned" type="number" min="1" step="1" value="${goal?.planned_count || ''}" placeholder="例如：40"></div>
      <p class="muted text-sm" data-help-note="计划次数">次数由你填写，系统不会根据数值、单位或待办内容换算。目标可先保存，稍后关联待办。</p>`;
    const handle = dialog({ title: adjusting ? '修正已执行次数' : editing ? '编辑目标' : '新建目标',
      body: `${body}<div data-goal-error role="alert" hidden></div>`,
      footer: '<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-primary" data-save>保存</button>' });
    editor = handle;
    const save = handle.root.querySelector('[data-save]'), feedback = handle.root.querySelector('[data-goal-error]');
    handle.root.querySelector('[data-cancel]').onclick = handle.close;
    save.onclick = async () => {
      if (submitting || !handle.root.isConnected || !canPresent(owner)) return;
      submitting = true; save.disabled = true;
      try {
        if (conflicted) {
          goal = await fresh(id);
          if (!handle.root.isConnected || !canPresent(owner)) return;
          const accepted = await ask(owner, '目标已更新。保留你输入的内容，按最新目标状态再次保存？', { danger: false });
          if (!handle.root.isConnected || !accepted) return;
          conflicted = false;
        }
        if (!op) {
          let payload;
          if (adjusting) payload = { executed_count: countValue(handle.root.querySelector('#goal-count').value, 0, '已执行次数') };
          else {
            const value = handle.root.querySelector('#goal-value').value;
            if (!value || !Number.isFinite(Number(value)) || Number(value) <= 0) throw new Error('目标数值必须为正数');
            payload = { content: handle.root.querySelector('#goal-content').value.trim(), target_value: value,
              unit: handle.root.querySelector('#goal-unit').value.trim(),
              planned_count: countValue(handle.root.querySelector('#goal-planned').value, 1, '计划执行次数') };
            if (!payload.content || !payload.unit) throw new Error('请填写目标内容和单位');
          }
          if (goal) payload.expected_version = goal.version;
          op = createGoalOperation(action, id, payload);
        }
        const saved = await commit(op);
        if (!saved) return;
        const keepSelection = canPresent(owner) && handle.root.isConnected;
        op = null; handle.close(); toast(adjusting ? '次数已修正' : '目标已保存');
        if (keepSelection) { clearSelection(); selected = saved.id; render(true); }
      } catch (error) {
        const unknown = op && operations.has(op.key);
        feedback.hidden = false;
        feedback.innerHTML = errorBlock(esc(unknown ? '保存结果暂时无法确认。请核实原操作，输入内容已保留。' : error.message));
        if (!unknown) { op = null; conflicted = error.status === 409; }
        handle.root.querySelectorAll('input').forEach(input => { input.disabled = !!unknown; });
        save.textContent = unknown ? '核实保存结果' : conflicted ? '重新读取后重试' : '保存';
      } finally { submitting = false; save.disabled = false; }
    };
  }

  async function linkExisting(id, owner) {
    if (editor?.root.isConnected) return;
    const [latest, options] = await Promise.all([fresh(id), gw(`${BASE}/candidates`)]);
    let goal = latest;
    if (!canPresent(owner) || editor?.root.isConnected) return;
    const available = options.filter(task => !task.goal_id || task.goal_id === id);
    let op = null, submitting = false, conflicted = false;
    const handle = dialog({ title: goal.current_link ? '更换关联待办' : '关联已有待办',
      body: '<div class="field"><label>重复待办</label><div data-goal-candidates></div></div>'
        + '<p class="muted text-sm">只统计关联后首次完整完成的整轮。更换关联保留历史贡献。</p><div data-goal-error role="alert" hidden></div>',
      footer: '<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-primary" data-save>确认关联</button>' });
    editor = handle;
    const field = createRetroSelectField(handle.root.querySelector('[data-goal-candidates]'), {
      id: 'goal-candidate', value: '', options: [{ value: '', label: available.length ? '请选择重复待办' : '暂无可关联的重复待办' },
        ...available.map(task => ({ value: String(task.id), label: `${task.content} · ${TASK_TYPE_LABELS[task.task_type]}${task.refresh_enabled === false ? ' · 刷新已暂停' : ''}` }))],
    });
    handle.root.querySelector('[data-cancel]').onclick = handle.close;
    const save = handle.root.querySelector('[data-save]');
    save.onclick = async () => {
      if (submitting || !handle.root.isConnected || !canPresent(owner)) return;
      submitting = true; save.disabled = true;
      try {
        if (!op) {
          const taskId = countValue(field.value, 1, '待办身份');
          if (conflicted) goal = await fresh(id);
          if (!handle.root.isConnected || !canPresent(owner)) return;
          if ((goal.current_link && goal.current_link.task_id !== taskId) || conflicted) {
            if (!await ask(owner, '确认使用所选待办关联当前目标？原关联将结束，历史贡献保留。', { danger: false })) return;
            if (!handle.root.isConnected) return;
          }
          op = createGoalOperation('link', id, { task_id: taskId, expected_version: goal.version });
        }
        if (!await commit(op)) return;
        op = null; handle.close(); toast('目标关联已保存');
      } catch (error) {
        const unknown = op && operations.has(op.key);
        const feedback = handle.root.querySelector('[data-goal-error]'); feedback.hidden = false;
        feedback.innerHTML = errorBlock(esc(unknown ? '关联结果暂时无法确认，请按原操作核实。' : error.message));
        if (!unknown) { op = null; conflicted = error.status === 409; }
        field.disabled = !!unknown;
        save.textContent = unknown ? '核实关联结果' : '重试关联';
      } finally { submitting = false; save.disabled = false; }
    };
  }

  async function simple(action, id, owner) {
    const goal = await fresh(id);
    if (!canPresent(owner)) return;
    const accepted = await ask(owner, action === 'delete' ? '删除此目标？关联待办将保留。' : '解除当前关联？已有贡献与手动修正将保留。');
    if (!accepted || !canPresent(owner)) return;
    const op = createGoalOperation(action === 'delete' ? 'delete' : 'link', id,
      { expected_version: goal.version, ...(action === 'delete' ? {} : { task_id: null }) });
    await commit(op);
    toast(action === 'delete' ? '目标已删除' : '关联已解除');
  }

  async function linkCreated(flow, { retry = false, owner = null } = {}) {
    if (!flow.linkOp) {
      let version = flow.version;
      if (retry) {
        const latest = await fresh(flow.goalId);
        if (!canPresent(owner)) return;
        if (latest.current_link?.task_id === flow.taskId) { flows.delete(flow.goalId); persist(); await changed(); return; }
        if (latest.version !== version || latest.current_link) {
          if (!await ask(owner, '按目标最新状态关联已创建的待办？当前关联将结束，历史贡献保留。', { danger: false })) return;
        }
        version = latest.version;
      }
      flow.linkOp = createGoalOperation('link', flow.goalId, { task_id: flow.taskId, expected_version: version });
    }
    flow.state = 'linking'; persist();
    try {
      await sendGoalOperation(flow.linkOp);
      flows.delete(flow.goalId); persist();
      toast('待办已创建并关联');
      await changed();
    } catch (error) {
      flow.state = 'link_failed'; flow.error = error.message;
      if (!resultUnknown(error)) flow.linkOp = null;
      persist(); render();
      toast('待办已创建，关联未完成，可在目标页重试关联', 'warn');
    }
  }

  async function newTask(id, owner) {
    if (flows.has(id)) { await recoverTask(id, owner); return; }
    const goal = await fresh(id);
    if (!canPresent(owner) || editor?.root.isConnected) return;
    if (goal.current_link && !await ask(owner, '新待办创建成功后将替换当前关联，保留历史贡献。继续创建？', { danger: false })) return;
    if (!canPresent(owner)) return;
    const flow = { goalId: id, version: goal.version, state: 'input', taskId: null, taskKey: null, taskBody: null, linkOp: null };
    const formHandle = openTaskForm(null, {
      allowedTypes: GOAL_TASK_TYPES,
      createTask: async (body, key) => {
        flow.taskKey = key; flow.taskBody = structuredClone(body); flow.state = 'creating';
        flows.set(id, flow); persist();
        try {
          const task = await gw('/admin/api/planning/tasks', { method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(body) });
          if (!task.id || task.creation_request_deleted) throw Object.assign(new Error('该次创建对应的待办已删除，请重新发起创建'), { status: 409 });
          flow.taskId = task.id; flow.state = 'created'; persist();
          return task;
        } catch (error) {
          if (resultUnknown(error)) { flow.state = 'creation_unknown'; flow.error = error.message; }
          else flows.delete(id);
          persist(); render(); throw error;
        }
      },
      onCreated: async () => {
        if (!flow.taskId) return;
        busy.add(`flow:${id}`);
        try { await linkCreated(flow); }
        finally { busy.delete(`flow:${id}`); }
      },
    });
    if (formHandle) dialogs.add(formHandle);
  }

  async function recoverTask(id, owner) {
    const flow = flows.get(id), key = `flow:${id}`;
    if (!flow || busy.has(key) || flow.state === 'creating') return;
    busy.add(key);
    try {
      if (!flow.taskId) {
        const task = await gw('/admin/api/planning/tasks', { method: 'POST',
          headers: { 'Content-Type': 'application/json', 'Idempotency-Key': flow.taskKey }, body: JSON.stringify(flow.taskBody) });
        if (!task.id || task.creation_request_deleted) {
          flows.delete(id); persist(); throw new Error('该次创建对应的待办已删除，未重新创建');
        }
        flow.taskId = task.id; flow.state = 'created'; persist();
        await onTaskChanged();
      }
      await linkCreated(flow, { retry: true, owner });
    } catch (error) {
      flow.error = error.message; persist(); render(); throw error;
    } finally { busy.delete(key); }
  }

  async function action(event) {
    const button = event.target.closest('[data-goal-act]');
    if (!button || button.disabled) return;
    const kind = button.dataset.goalAct, rawId = button.dataset.goalId, id = Number(rawId);
    if (kind === 'select') { beginPresentation(); clearSelection(); selected = id; render(true); return; }
    const key = `${kind}:${rawId}`;
    if (busy.has(key)) return;
    const owner = beginPresentation();
    const originalButton = button.innerHTML;
    busy.add(key); button.disabled = true;
    button.textContent = ['edit', 'adjust', 'link', 'new-task'].includes(kind) ? '正在读取…' : '正在处理…';
    try {
      if (kind === 'reload') await load();
      else if (kind === 'create') {
        const existing = [...operations.values()].find(op => op.action === 'create');
        if (existing) await commit(existing); else await form('create', null, owner);
      } else if (kind === 'edit' || kind === 'adjust') await form(kind, id, owner);
      else if (kind === 'link') await linkExisting(id, owner);
      else if (kind === 'delete' || kind === 'unlink') await simple(kind, id, owner);
      else if (kind === 'new-task') await newTask(id, owner);
      else if (kind === 'recover') { const op = operations.get(rawId); if (op && await commit(op)) toast('原操作已确认'); }
      else if (kind === 'recover-task') await recoverTask(id, owner);
      else if (kind === 'keep-task') { flows.delete(id); persist(); render(); }
      else if (kind === 'full') {
        const goal = await fresh(id);
        if (!canPresent(owner)) return;
        dialog({ title: '目标全文', body: `<p class="goal-full-text">${esc(description(goal))}</p>`, footer: '' });
      }
      else if (kind === 'history') {
        const goal = await fresh(id);
        if (!canPresent(owner)) return;
        dialog({ title: '历史关联', body: `<div class="goal-history">${goal.history.map(link =>
          `<p>${esc(link.task_content)}<br><span class="muted text-sm">${esc(new Date(link.linked_at).toLocaleString())} ～ ${esc(new Date(link.ended_at).toLocaleString())}${link.end_reason === 'task_deleted' ? ' · 待办已删除' : ''}</span></p>`).join('')}</div>`,
          footer: '' });
      }
    } catch (error) {
      if (canPresent(owner)) toast(resultUnknown(error) && (operations.size || flows.size)
        ? '结果暂时无法确认，请在目标页按原操作核实' : error.message, 'err');
    }
    finally { busy.delete(key); if (button.isConnected) { button.disabled = false; button.innerHTML = originalButton; } }
  }

  const focus = () => { if (shown && root && !document.hidden) load(); };
  return {
    mount(host, actionsHost) {
      root = host; epoch++; presentation++; shown = false; selected = null; rows = []; loaded = false;
      lifecycle = new AbortController();
      const owner = epoch;
      actionRoot = actionsHost;
      actionListener = event => { if (current(owner)) action(event); };
      actionRoot.addEventListener('click', actionListener);
      window.addEventListener('focus', focus); document.addEventListener('visibilitychange', focus);
      render();
    },
    show() { if (lifecycle.signal.aborted) lifecycle = new AbortController(); shown = true; return load(); },
    hide() {
      shown = false; selected = null; ++readId; presentation++; lifecycle.abort();
      for (const handle of dialogs) handle.close?.();
      dialogs.clear(); editor = null;
    },
    refresh() { return shown && !pendingRead ? load() : Promise.resolve(); },
    dispose() {
      root = null; shown = false; epoch++; presentation++; ++readId;
      lifecycle.abort();
      actionRoot?.removeEventListener('click', actionListener);
      actionRoot = null; actionListener = null;
      for (const handle of dialogs) handle.close?.();
      dialogs.clear(); editor = null;
      window.removeEventListener('focus', focus); document.removeEventListener('visibilitychange', focus);
    },
  };
}
