// lib/cycle_settings.js - 规划周期设置弹窗（规划管理页与配置页共用）
// 从 pages/planning.js 原地抽出：两阶段 boundary 修改（dry-run 冲突检测 +
// 冲突就地调整 + 原子保存）与原实现一致。onSaved 在保存成功后回调（规划页
// 重拉今日看板，配置页重载整页）；demoSettings 仅用于数据库未连接的本地
// 预览——读取失败时以示例数据打开弹窗（保存会因库不可用自然报错）。
import { gw, esc } from '../api.js?v=20261003-planning-create-latency2';
import { modal, toast, errorBlock } from '../ui.js?v=20261003-planning-create-latency2';
import { createRetroTimeField } from './retro_time.js?v=20261003-planning-create-latency2';
import {
  mergeBoundaryAdjustments, rememberedAdjustment,
} from './planning_adjustments.js?v=20261003-planning-create-latency2';

function fmtDue(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(date);
}

export async function openCycleSettings({ onSaved = null, demoSettings = null, initialBoundary = '' } = {}) {
  let settings;
  try {
    settings = await gw('/admin/api/planning/cycle');
  } catch (error) {
    if (!demoSettings) {
      toast(`读取周期设置失败：${error.message}`, 'err');
      return;
    }
    settings = demoSettings;
  }
  // initialBoundary：卡片表面发起的 boundary 修改命中冲突时，弹窗按尝试的
  // 新时间预填（仅展示层；冲突比较仍以数据库现值为基准）
  const presetBoundary = initialBoundary || settings.refresh_boundary_time;
  const pending = settings.pending_boundary;
  const { root, close } = modal({
    title: '周期设置',
    wide: true,
    body: `
        <div class="field"><label>每日刷新时间（北京时间）</label>
          <div class="retro-time" data-retro-for="pf-cycle-boundary" data-retro-mode="time" data-retro-value="${esc(presetBoundary)}"></div>
          <p class="muted text-sm">新的刷新时间从下一规划周期开始生效，当前周期保持不变。</p></div>
        ${pending ? `<div class="field muted text-sm">当前已有等待生效的修改：新刷新时间 ${esc(presetBoundary)} 将于 ${esc(fmtDue(pending.effective_at))} 起生效；当前周期按原刷新时间 ${esc(pending.previous_time)} 继续走完。</div>` : ''}
        <div id="pf-boundary-conflicts"></div>
        <div class="field"><label class="inline"><input type="checkbox" id="pf-daily-refresh" ${settings.daily_refresh_enabled ? 'checked' : ''}> 每日待办自动刷新</label></div>
        <div class="field"><label class="inline"><input type="checkbox" id="pf-auto-recompute" ${settings.auto_recompute_enabled ? 'checked' : ''}> 自动重算（排列/完成等变化后等待一段时间自动重排）</label></div>
        <div class="field"><label>自动重算等待（分钟）</label>
          <input type="number" id="pf-auto-wait" min="1" max="1440" value="${esc(settings.auto_recompute_wait_minutes)}"></div>
        <div id="pf-cycle-error" hidden></div>`,
    footer: `<button class="btn btn-secondary" data-cancel>取消</button>
               <button class="btn btn-primary" data-ok>保存</button>`,
  });
  root.querySelectorAll('.retro-time[data-retro-for]').forEach((host) => {
    createRetroTimeField(host, {
      id: host.dataset.retroFor,
      value: host.dataset.retroValue || '',
      mode: host.dataset.retroMode || 'datetime',
      align: host.dataset.retroAlign || 'left',
    });
  });
  root.querySelector('[data-cancel]').onclick = close;
  const submitBtn = root.querySelector('[data-ok]');
  const conflictHost = root.querySelector('#pf-boundary-conflicts');
  const errorArea = root.querySelector('#pf-cycle-error');
  // 冲突清单就地渲染（同一流程内调整；校验一律按新 boundary 进行）
  // 批次 9 UI 修复：dry-run 只返回仍未通过的冲突——已修正的待办不再
  // 出现在下一次响应里，重绘会把 DOM 中它的调整行移除。调整以稳定
  // task_id → 值 形式留在 modal 级集合中，逐项修正互不覆盖；重绘冲突
  // 行时恢复该待办已录入的值。
  const adjustmentsState = [];
  const remembered = (taskId) => rememberedAdjustment(adjustmentsState, taskId);
  const initRetro = (scope) => {
    scope.querySelectorAll('.retro-time[data-retro-for]').forEach((host) => {
      createRetroTimeField(host, {
        id: host.dataset.retroFor,
        value: host.dataset.retroValue || '',
        mode: host.dataset.retroMode || 'datetime',
        align: host.dataset.retroAlign || 'left',
      });
    });
  };
  const showConflictList = (conflicts, boundary) => {
    conflictHost.innerHTML = conflicts.length ? `
        <div class="field">
          <label>以下待办的可安排时段跨越新刷新时间 ${esc(boundary)}，请在下方调整后再保存</label>
          ${conflicts.map((c) => {
            const prev = remembered(c.task_id);
            return `
            <div class="field" data-adjust-task="${c.task_id}">
              <label class="inline">${esc(c.content)}：当前时段 ${esc(c.window_start_tod) || '无'} ～ ${esc(c.window_end_tod) || '无'}</label>
              <div class="tag-row" style="align-items:center">
                <span class="muted text-sm">最早开始</span>
                <div class="retro-time" data-retro-for="pf-adj-start-${c.task_id}" data-retro-mode="time" data-retro-value="${esc((prev && prev.window_start_tod) || c.window_start_tod || '')}"></div>
                <span class="muted text-sm">最晚完成</span>
                <div class="retro-time" data-retro-for="pf-adj-end-${c.task_id}" data-retro-mode="time" data-retro-value="${esc((prev && prev.window_end_tod) || c.window_end_tod || '')}"></div>
              </div>
              <p class="muted text-sm">${esc(c.reason)}</p>
            </div>`;
          }).join('')}
        </div>` : '';
    if (conflicts.length) initRetro(conflictHost);
  };
  const collectAdjustments = () => {
    // 只从仍在 DOM 中的冲突行收集本次填写值；与已录入集合按 task_id
    // 合并（同 task 覆盖旧值、其余保留）——多项冲突逐个修正后，最终
    // submit 携带全部调整，不只剩最后一个。
    const collected = [...conflictHost.querySelectorAll('[data-adjust-task]')]
      .map((row) => ({
        task_id: Number(row.dataset.adjustTask),
        window_start_tod: root.querySelector(`#pf-adj-start-${row.dataset.adjustTask}`).value || null,
        window_end_tod: root.querySelector(`#pf-adj-end-${row.dataset.adjustTask}`).value || null,
      }));
    const merged = mergeBoundaryAdjustments(adjustmentsState, collected);
    adjustmentsState.length = 0;
    adjustmentsState.push(...merged);
    return adjustmentsState;
  };
  const patch = (bodyObj) => gw('/admin/api/planning/cycle', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(bodyObj),
  });
  submitBtn.onclick = async () => {
    const boundary = root.querySelector('#pf-cycle-boundary').value;
    const daily = root.querySelector('#pf-daily-refresh').checked;
    const autoEnabled = root.querySelector('#pf-auto-recompute').checked;
    const autoWait = Number(root.querySelector('#pf-auto-wait').value);
    const boundaryChanged = boundary && boundary !== settings.refresh_boundary_time;
    errorArea.hidden = true;
    submitBtn.disabled = true;
    try {
      // boundary 修改（§5.2.2 两阶段）：先 dry-run（绝对零写入）；命中冲突
      // 则在同一弹窗内列出并等待用户调整，全部通过后一次原子保存；取消
      // （关闭弹窗）则全部不保存。
      let adjustments = [];
      if (boundaryChanged) {
        const dry = await patch({
          refresh_boundary_time: boundary,
          dry_run: true,
          ...(conflictHost.querySelector('[data-adjust-task]')
            ? { task_adjustments: collectAdjustments() } : {}),
        });
        if (dry.conflicts?.length) {
          showConflictList(dry.conflicts, boundary);
          submitBtn.disabled = false;
          toast('存在跨越新刷新时间的待办，请调整其可安排时段后再保存', 'err');
          return;
        }
        adjustments = conflictHost.querySelector('[data-adjust-task]')
          ? collectAdjustments() : [];
        // 最终保存：新 boundary 与关联调整一次原子生效；modal 打开期间
        // 出现的新冲突被服务端 409 拒绝并整体不生效
        await patch({
          refresh_boundary_time: boundary,
          ...(adjustments.length ? { task_adjustments: adjustments } : {}),
        });
      }
      // 其余设置键保持原路径：一次只接受一个键，逐个下发实际变化
      const changes = [];
      if (daily !== settings.daily_refresh_enabled) changes.push({ daily_refresh_enabled: daily });
      if (autoEnabled !== settings.auto_recompute_enabled) changes.push({ auto_recompute_enabled: autoEnabled });
      if (Number.isInteger(autoWait) && autoWait >= 1 && autoWait <= 1440 && autoWait !== settings.auto_recompute_wait_minutes) {
        changes.push({ auto_recompute_wait_minutes: autoWait });
      }
      for (const change of changes) await patch(change);
      close();
      toast(boundaryChanged
        ? '已保存；新的刷新时间从下一规划周期开始生效，当前周期保持不变'
        : '周期设置已保存');
      if (onSaved) await onSaved();
    } catch (error) {
      submitBtn.disabled = false;
      errorArea.hidden = false;
      errorArea.innerHTML = errorBlock(esc(String(error.message || '')));
      toast(`保存失败：${error.message}`, 'err');
    }
  };
}
