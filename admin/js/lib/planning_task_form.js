// Task creation/edit form. Local submitting/committed state belongs to one opened form.
import { gw } from '../api.js?v=20261004-memo-bugfix3';
import { modal, toast, errorBlock, esc, icon } from '../ui.js?v=20261004-memo-bugfix3';
import { TASK_TYPES, TASK_TYPE_LABELS, WEEKDAY_NAMES } from './planning_display.js?v=20261004-memo-bugfix3';

export function openTaskForm(task, { occurrences, initRetroFields, onSaved }) {
  const editing = !!task?.id;
  // once 已生成当前实例 → 任务级排程身份锁定（§28.3）：目标日期与未来
  // 窗口模板禁用并提示走当前实例调整；后端 400 仍是权威兜底。
  // 批次 9 UI 修复：优先用后端随任务列表返回的 has_generated_occurrence
  // （不依赖 occurrences 列表的加载状态与过滤条件），实例列表仅作兜底。
  const onceLocked = editing && task.task_type === 'once'
    && (task.has_generated_occurrence
      || occurrences.some((o) => o.task_id === task.id));
  const value = (field, fallback = '') => (task ? (task[field] ?? fallback) : fallback);
  const typeSelectOptions = TASK_TYPES.map((t) => ({ value: t, label: TASK_TYPE_LABELS[t] }));
  const weekdayChecks = WEEKDAY_NAMES.map((name, index) => `
    <label class="inline"><input type="checkbox" data-weekday value="${index}"
      ${(value('weekdays') || []).includes(index) ? 'checked' : ''}>周${name}</label>`).join('');
  const { root, close } = modal({
    title: editing ? '编辑待办' : '新建待办',
    wide: true,
    body: `
      <div class="field"><label>内容</label>
        <input type="text" id="pf-content" value="${esc(value('content'))}" placeholder="例如：背单词"></div>
      <div class="field"><label>类型</label>
        <div class="retro-select" data-retro-select="pf-type" data-retro-value="${esc(value('task_type', 'daily'))}"></div></div>
      <div data-type-block="interval" style="display:none">
        <div class="field"><label>刷新方式</label>
          <div class="tag-row">
            <label class="inline"><input type="radio" name="pf-refresh-mode" value="after_completion" ${(value('refresh_mode', 'after_completion')) === 'after_completion' ? 'checked' : ''}> 处理后刷新（完成后起算）</label>
            <label class="inline"><input type="radio" name="pf-refresh-mode" value="fixed_interval" ${value('refresh_mode') === 'fixed_interval' ? 'checked' : ''}> 固定间隔（固定时间轴）</label>
          </div>
        </div>
        <div class="field"><label>间隔天数（1-3650）</label>
          <input type="number" id="pf-interval-days" min="1" max="3650" value="${esc(value('interval_days', 1))}"></div>
      </div>
      <div data-type-block="weekly" style="display:none">
        <div class="field"><label>每周几出现（可多选）</label><div class="tag-row">${weekdayChecks}</div></div>
      </div>
      <div data-type-block="monthly" style="display:none">
        <div class="field"><label>每月几号出现（逗号分隔，如 1,15；当月没有则跳过）</label>
          <input type="text" id="pf-month-days" value="${esc((value('month_days') || []).join(','))}"></div>
      </div>
      <div data-type-block="once" style="display:none">
        <div class="field"><label>目标日期（可选）</label>
          <div class="retro-time" data-retro-for="pf-target-date" data-retro-mode="date" data-retro-value="${esc(value('target_date'))}"></div>
          <p class="muted text-sm" id="pf-resident-note" hidden>未填日期：单次待办常驻显示，不设最早开始／最晚完成，直到你主动处理。</p></div>
        <div id="pf-once-error" hidden></div>
      </div>
      <div class="field"><label>预估耗时（分钟，或 1h30m 简写）</label>
        <input type="text" id="pf-estimated" value="${esc(value('estimated_minutes', ''))}"></div>
      <div class="field"><label>可安排时段</label>
        <div class="window-fields">
          <div class="window-field"><label>最早开始（可选）</label>
            <div class="retro-time" data-retro-for="pf-window-start" data-retro-mode="time" data-retro-align="right" data-retro-value="${esc(value('window_start_tod'))}"></div>
          </div>
          <div class="window-field"><label>最晚完成（可选）</label>
            <div class="retro-time" data-retro-for="pf-window-end" data-retro-mode="time" data-retro-align="right" data-retro-value="${esc(value('window_end_tod'))}"></div>
          </div>
        </div>
        <button type="button" id="pf-clear-window" class="btn btn-secondary btn-sm" hidden>清空残留时段</button>
      </div>
      <div id="pf-window-error" hidden></div>
      <div class="field"><label class="inline"><input type="checkbox" id="pf-hollow" ${value('is_hollow') ? 'checked' : ''}> 中空待办（开始/结束两个条目，中间可插入其他待办）</label></div>
      <div id="pf-hollow-block" style="display:none">
        <div class="field"><label>开始阶段内容</label><input type="text" id="pf-hollow-start" value="${esc(value('hollow_start_content'))}"></div>
        <div class="field"><label>开始阶段耗时（分钟）</label><input type="number" id="pf-hollow-start-min" min="1" value="${esc(value('hollow_start_minutes', ''))}"></div>
        <div class="field"><label>中间等待时长（分钟）</label><input type="number" id="pf-hollow-wait" min="1" value="${esc(value('hollow_wait_minutes', ''))}"></div>
        <div class="field"><label>中间说明（可选）</label><input type="text" id="pf-hollow-note" value="${esc(value('hollow_wait_note'))}"></div>
        <div class="field"><label>结束阶段内容</label><input type="text" id="pf-hollow-end" value="${esc(value('hollow_end_content'))}"></div>
        <div class="field"><label>结束阶段耗时（分钟）</label><input type="number" id="pf-hollow-end-min" min="1" value="${esc(value('hollow_end_minutes', ''))}"></div>
      </div>
      ${editing ? `<div class="field muted text-sm">修改规则只影响以后生成的轮次，当前已经生成的待办保持不变；提醒（闹钟/计时器）在待办详情栏设置。</div>
      <div class="field"><label class="inline"><input type="checkbox" id="pf-active" ${value('is_active') ? 'checked' : ''}> 启用中</label></div>` : ''}`,
    footer: `<button class="btn btn-secondary" data-cancel>取消</button>
             <button class="btn btn-primary" data-ok>${editing ? '保存' : '创建'}</button>`,
  });

  initRetroFields(root, { 'pf-type': typeSelectOptions });  // BUG-14：表单内日期/时刻/类型统一为复古选择器（值契约不变）
  // 复古下拉挂载后 #pf-type 才是隐藏 input，取值必须在 initRetroFields 之后
  const typeSelect = root.querySelector('#pf-type');
  if (onceLocked) {
    // 批次 9 UI 修复：复古选择器是 hidden input + 按钮——只 disable
    // input 拦不住按钮弹层改值；按钮与 input 一起禁用才算真正锁死。
    const lockRetro = (hostSelector) => {
      const host = root.querySelector(hostSelector);
      if (!host) return;
      const input = host.querySelector('input');
      const button = host.querySelector('button');
      if (input) input.disabled = true;
      if (button) {
        button.disabled = true;
        button.title = '该单次待办已生成当前实例：请在该待办详情栏使用「调整时段」';
      }
    };
    lockRetro('.retro-time[data-retro-for="pf-target-date"]');
    lockRetro('.retro-time[data-retro-for="pf-window-start"]');
    lockRetro('.retro-time[data-retro-for="pf-window-end"]');
    const lockNote = root.querySelector('[data-type-block="once"]');
    if (lockNote) lockNote.insertAdjacentHTML('beforeend',
      '<p class="muted text-sm">该单次待办已生成当前实例：任务日期与未来窗口模板已锁定，调整这一次请在该待办详情栏使用「调整时段」。</p>');
  }
  const syncBlocks = () => {
    const type = typeSelect.value;
    root.querySelectorAll('[data-type-block]').forEach((block) => {
      block.style.display = block.dataset.typeBlock === type ? '' : 'none';
    });
    root.querySelector('#pf-hollow-block').style.display =
      root.querySelector('#pf-hollow').checked ? '' : 'none';
    syncOnceWindow();
  };
  // §30.6（2026-10-01）：单次目标日期可选；未填日期时最早开始／最晚完成
  // 控件不可设置，并说明常驻语义（前后端都拒绝空日期 + 非空窗口组合）。
  const residentNote = root.querySelector('#pf-resident-note');
  const toggleRetro = (hostSelector, disabled, title) => {
    const host = root.querySelector(hostSelector);
    if (!host) return;
    const input = host.querySelector('input');
    const button = host.querySelector('button');
    if (input) input.disabled = disabled;
    if (button) {
      button.disabled = disabled;
      button.title = title;
    }
  };
  const syncOnceWindow = () => {
    if (!residentNote) return;
    // §28.3 身份锁定优先：已生成 once 的日期与窗口模板已禁用并提示，
    // 常驻联动不得重新启用（lockRetro 的禁用状态保持权威）。
    if (onceLocked) return;
    const type = typeSelect.value;
    const dateValue = root.querySelector('#pf-target-date')?.value || '';
    const resident = type === 'once' && !dateValue;
    const startInput = root.querySelector('#pf-window-start');
    const endInput = root.querySelector('#pf-window-end');
    // R5 审查修复：空日期禁止「新增」窗口不变，但残留值必须留一条
    // 明确可用的清空通路——禁用按钮同时拦住了进入选择器点「清除」，
    // 残值既删不掉也提交不了。清空按钮仅在存在残值时可见，点击即
    // user 明确确认（不默默丢弃、不提交隐藏残值）。
    const residual = resident && !!((startInput?.value) || (endInput?.value));
    residentNote.hidden = !resident;
    residentNote.textContent = residual
      ? '未填日期：单次待办常驻显示，不设最早开始／最晚完成。当前仍有残留时段值——请点击「清空残留时段」明确清除后再保存。'
      : '未填日期：单次待办常驻显示，不设最早开始／最晚完成，直到你主动处理。';
    const residentTitle = resident
      ? '无日期单次常驻显示，不能设置可安排时段' : '';
    toggleRetro('.retro-time[data-retro-for="pf-window-start"]', resident, residentTitle);
    toggleRetro('.retro-time[data-retro-for="pf-window-end"]', resident, residentTitle);
    const clearBtn = root.querySelector('#pf-clear-window');
    if (clearBtn) clearBtn.hidden = !residual;
  };
  root.querySelector('#pf-clear-window')?.addEventListener('click', () => {
    // user 明确清除残留时段：经复古选择器的编程清空接口（缺省回退直写
    // value），两端一起清；清空后重新联动（隐藏按钮、更新提示）。
    for (const selector of ['#pf-window-start', '#pf-window-end']) {
      const input = root.querySelector(selector);
      if (input?.value) {
        if (typeof input._applyRetroValue === 'function') input._applyRetroValue('', true);
        else input.value = '';
      }
    }
    syncOnceWindow();
  });
  typeSelect.addEventListener('change', syncBlocks);
  root.querySelector('#pf-hollow').addEventListener('change', syncBlocks);
  root.querySelector('#pf-target-date')?.addEventListener('input', syncOnceWindow);
  syncBlocks();

  root.querySelector('[data-cancel]').onclick = close;
  // 防重复提交（新建与编辑同一入口）：提交锁在 handler 入口同步建立，
  // 早于任何异步请求，不能只依赖按钮 disabled（按钮聚焦后按 Enter /
  // 空格仍会触发 click）。两阶段语义：
  // 提交/API 阶段失败 → 释放锁与按钮，user 可修改后重新提交；
  // 服务器保存成功 → committed 终态：此后 toast/close/onSaved 等 UI
  // 后处理无论成败，本表单都不再解锁、不再发出第二次保存请求，也
  // 不得把已成功的事实误报为「保存失败」。即使弹窗因异常未被移除，
  // 提交按钮保持禁用，重复点击也不会再发请求；关闭失败时可经取消 /
  // 右上角关闭按钮收尾。
  const submitBtn = root.querySelector('[data-ok]');
  let submitting = false;
  let committed = false;
  // 创建响应（first_round_skipped / schedule_conflict）必须声明在本
  // handler 作用域——提交 try 块内的声明在块外读取会抛 ReferenceError
  // 且被外层 catch 吞掉（R4 审查修复），两种新增提示都会失效。
  let createdTask = null;
  submitBtn.onclick = async () => {
    if (committed || submitting) return;
    submitting = true;
    submitBtn.disabled = true;
    submitBtn.textContent = editing ? '正在保存…' : '正在创建…';
    createdTask = null;
    try {
      const type = typeSelect.value;
      const body = {
        content: root.querySelector('#pf-content').value.trim(),
        task_type: type,
      };
      const estimated = root.querySelector('#pf-estimated').value.trim();
      if (estimated) body.estimated_minutes = estimated;
      // 可安排时段（排程约束，非排程结果）：四种组合均可表达。
      // 编辑模式显式发送双端清除值（null = 清除该端），不发送=没清除；
      // 创建模式只提交已填端（缺省=不约束）。
      const windowStart = root.querySelector('#pf-window-start').value;
      const windowEnd = root.querySelector('#pf-window-end').value;
      if (editing) {
        body.window_start_tod = windowStart || null;
        body.window_end_tod = windowEnd || null;
      } else {
        if (windowStart) body.window_start_tod = windowStart;
        if (windowEnd) body.window_end_tod = windowEnd;
      }
      if (type === 'interval') {
        body.interval_days = Number(root.querySelector('#pf-interval-days').value) || null;
        const refreshMode = root.querySelector('input[name="pf-refresh-mode"]:checked');
        if (refreshMode) body.refresh_mode = refreshMode.value;
      }
      if (type === 'weekly') {
        body.weekdays = [...root.querySelectorAll('[data-weekday]:checked')].map((el) => Number(el.value));
      }
      if (type === 'monthly') {
        body.month_days = root.querySelector('#pf-month-days').value
          .split(/[,，\s]+/).map((v) => Number(v)).filter((v) => Number.isInteger(v) && v > 0);
      }
      if (type === 'once') body.target_date = root.querySelector('#pf-target-date').value || null;
      if (type === 'once' && !body.target_date
          && (body.window_start_tod || body.window_end_tod)) {
        // 前端先行校验（§30.6）：空日期与非空窗口不能同时保存——切换类型
        // 时禁用控件的残留值也拦在这里；后端仍权威复核（零写入 400）。
        throw new Error('未填目标日期的单次待办不能设置可安排时段：无日期单次常驻显示，不设最早开始或最晚完成');
      }
      if (editing && task.task_type === 'once' && task.has_generated_occurrence) {
        // once 身份锁定的提交侧兜底（§28.3）：无论控件状态如何，target_date
        // / 未来窗口模板一律回传任务现值（幂等请求放行、实际变化后端拒绝）
        body.target_date = task.target_date ?? null;
        body.window_start_tod = task.window_start_tod ?? null;
        body.window_end_tod = task.window_end_tod ?? null;
      }
      if (root.querySelector('#pf-hollow').checked) {
        body.is_hollow = true;
        body.hollow_start_content = root.querySelector('#pf-hollow-start').value.trim() || body.content;
        body.hollow_start_minutes = Number(root.querySelector('#pf-hollow-start-min').value) || null;
        body.hollow_wait_minutes = Number(root.querySelector('#pf-hollow-wait').value) || null;
        body.hollow_wait_note = root.querySelector('#pf-hollow-note').value.trim() || null;
        body.hollow_end_content = root.querySelector('#pf-hollow-end').value.trim() || body.content;
        body.hollow_end_minutes = Number(root.querySelector('#pf-hollow-end-min').value) || null;
      }
      if (editing) body.is_active = root.querySelector('#pf-active').checked;

      if (editing) {
        await gw(`/admin/api/planning/tasks/${task.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      } else {
        createdTask = await gw('/admin/api/planning/tasks', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
      }
    } catch (error) {
      // 提交/API 阶段失败：先解锁恢复按钮再提示，提示自身异常不得
      // 卡死提交资格（user 仍可修改后重新提交）。错误按字段就近呈现：
      // 目标日期类错误进 once 区，可安排时段及其余错误进时段区。
      submitting = false;
      submitBtn.disabled = false;
      submitBtn.textContent = editing ? '保存' : '创建';
      const message = String(error.message || '');
      const area = root.querySelector(
        message.includes('目标日期') || message.includes('单次待办已生成')
          ? '#pf-once-error' : '#pf-window-error');
      if (area) {
        area.hidden = false;
        area.innerHTML = errorBlock(esc(message));
        area.scrollIntoView({ block: 'nearest' });
      }
      toast(`保存失败：${error.message}`, 'err');
      return;
    }
    // 服务器已保存：进入不可逆终态。此后任何 UI 后处理异常都不得
    // 重新赋予本表单提交资格，也不得误报「保存失败」。
    committed = true;
    submitting = false;
    submitBtn.textContent = editing ? '已保存' : '已创建';
    // 后处理逐项 best-effort：一步失败只影响该步，后续步骤照常执行
    try {
      toast(editing ? '待办已保存' : '待办已创建');
      // §30.6 / §18.1（2026-10-01）：创建允许与排程可行性分离——区分
      // 「本轮已截止、次日起生效」与「已创建但存在排程冲突」，两者都
      // 不改变任务已保存的事实。
      if (createdTask?.first_round_skipped) {
        toast('本轮已过最晚完成，从次日起按重复规则生效');
      } else if (createdTask?.schedule_conflict) {
        toast('待办已创建，但可安排时段剩余空间不足，存在排程冲突');
      }
    } catch (error) {
      globalThis.console?.error({ location: 'planning_task_form.savedFeedback',
        stack: error.stack, error, editing, taskId: task?.id || createdTask?.id });
    }
    try { close(); } catch (error) {
      globalThis.console?.error({ location: 'planning_task_form.closeAfterSave',
        stack: error.stack, error, editing, taskId: task?.id || createdTask?.id });
    }
    try { await onSaved(); } catch (error) {
      globalThis.console?.error({ location: 'planning_task_form.refreshAfterSave',
        stack: error.stack, error, editing, taskId: task?.id || createdTask?.id });
      toast(editing ? '待办已保存，列表更新失败，请刷新重试' : '待办已创建，列表更新失败，请刷新重试', 'warn');
    }
  };
  // 页面保留句柄：关闭弹窗不会取消已经发出的请求，仍须拦住重新打开提交。
  return { editing, isSubmitting: () => submitting };
}
