// Instance dialogs: form-local inputs and timeout retry identity stay in their closures.
import { gw } from '../api.js?v=20261004-memo-bugfix3';
import { modal, toast, errorBlock, esc, icon } from '../ui.js?v=20261004-memo-bugfix3';

export function createPlanningDialogs({
  findOccurrence, getOccurrences, getTasks, openTaskForm,
  loadToday, loadTasks, loadOccurrences,
}) {
  return {
    askCompleteDuration(id, post) {
      const { root, close } = modal({
        title: '完成待办',
        body: `
          <div class="field">
            <label>实际耗时（可留空）</label>
            <input type="text" data-actual-duration placeholder="如 45、1h30m、1h1m1s">
            <p class="muted text-sm" style="margin:4px 0 0">无后缀按分钟计，可组合时/分/秒；留空则不记录手填耗时。</p>
          </div>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>完成</button>`,
      });
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const submit = root.querySelector('[data-ok]');
        if (submit.disabled) return;
        submit.disabled = true;
        const text = root.querySelector('[data-actual-duration]').value.trim();
        const body = {};
        if (text) body.actual_logged_duration = text;
        try {
          await post('/finish', body);
          close();
          toast('已完成');
          await Promise.all([loadToday(), loadOccurrences()]);
        } catch (error) {
          submit.disabled = false;
          toast(`操作失败：${error.message}`, 'err');
        }
      };
    },

    askPartial(id) {
      const { root, close } = modal({
        title: '部分完成',
        body: `<div class="field"><label>完成了哪些部分（会保存为说明）</label>
               <textarea id="planning-partial-note" rows="3" placeholder="例如：背完了前 20 页"></textarea></div>
               <div class="field muted text-sm">记录后待办保持开放，之后可点「已全部完成」收口。</div>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>保存</button>`,
      });
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const note = root.querySelector('#planning-partial-note').value.trim();
        if (!note) { toast('请填写完成说明', 'err'); return; }
        try {
          await gw(`/admin/api/planning/occurrences/${id}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: 'partial', partial_note: note }),
          });
          close();
          toast('已记录部分完成；待办保持开放，可继续处理');
          await loadToday();
          await loadOccurrences();
        } catch (error) {
          toast(`操作失败：${error.message}`, 'err');
        }
      };
    },

    async askRescheduleTimeout(id) {
      // B3/N2：同一次重排操作的幂等键在首次提交时生成；失败后不修改时间
      // 再次提交复用同一键（后端幂等收敛）；用户修改执行时间即视为新的
      // 请求，改用新键提交，不得拿旧键 + 新时间静默拿回旧结果
      let idempotencyKey = null;
      let lastSubmittedTime = null;
      const { root, close } = modal({
        title: '重新安排执行时间',
        body: `<div class="field muted text-sm">重新安排当前待办的执行时间，已有进度会保留；原超时记录保留。</div>
               <div class="field"><label>新的执行时间</label>
               <input type="datetime-local" id="planning-reschedule-time"></div>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>保存</button>`,
      });
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const value = root.querySelector('#planning-reschedule-time').value;
        if (!value) { toast('请选择时间', 'err'); return; }
        if (idempotencyKey === null || lastSubmittedTime !== value) {
          idempotencyKey = crypto.randomUUID();
          lastSubmittedTime = value;
        }
        try {
          await gw(`/admin/api/planning/occurrences/${id}/reschedule-timeout`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey },
            body: JSON.stringify({ est_start: new Date(value).toISOString() }),
          });
          close();
          toast('已保存新的执行时间；原超时记录保留');
          await loadToday();
          await loadOccurrences();
        } catch (error) {
          toast(`操作失败：${error.message}`, 'err');
        }
      };
    },

    askNewTime(id, targetStatus, title) {
      const { root, close } = modal({
        title,
        body: `<div class="field"><label>新的执行时间</label>
               <input type="datetime-local" id="planning-new-time"></div>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>确认</button>`,
      });
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const value = root.querySelector('#planning-new-time').value;
        if (!value) { toast('请选择时间', 'err'); return; }
        try {
          await gw(`/admin/api/planning/occurrences/${id}/status`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status: targetStatus, est_start: new Date(value).toISOString() }),
          });
          close();
          toast(targetStatus === 'deferred' ? '已延后' : '已重新安排');
          await loadToday();
          await loadOccurrences();
        } catch (error) {
          toast(`操作失败：${error.message}`, 'err');
        }
      };
    },

    askEditTime(id) {
      // 调整时段（批次 8）：编辑当前实例的冻结窗口约束（最早开始 / 最晚完成），
      // 不是编辑预估排程结果——收窄到恰好容纳耗时即钉住该时间；只有尚未开始
      // 且开放的实例允许（后端 422/409 中文拒绝时在字段附近呈现并保持可改）。
      const occ = findOccurrence(id)
        || getOccurrences().find((o) => o.id === id)
        || {};
      const toLocal = (iso) => {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
      };
      const hasWindow = !!(occ.window_start_at || occ.window_end_at);
      // §28.3（2026-10-01）：无日期单次常驻显示、不设时间窗口——当前实例
      // 编辑不能为它新增窗口端（后端权威拒绝，这里同步禁用输入并说明）。
      const occTask = getTasks().find((t) => t.id === occ.task_id);
      const residentOnce = !!occTask && occTask.task_type === 'once' && !occTask.target_date;
      const disabledAttr = residentOnce ? 'disabled' : '';
      const { root, close } = modal({
        title: '调整时段',
        body: `
          <p class="muted text-sm">调整当前这一轮的可安排时段（最早开始 / 最晚完成，两端可独立留空）。把时段收窄到恰好容纳预计耗时，就会把这条待办钉在该时间，不再被自动重算移动。</p>
          <div class="field"><label>最早开始（可选）</label><input type="datetime-local" id="planning-adj-window-start" value="${toLocal(occ.window_start_at)}" ${disabledAttr}></div>
          <div class="field"><label>最晚完成（可选，越过即超时）</label><input type="datetime-local" id="planning-adj-window-end" value="${toLocal(occ.window_end_at)}" ${disabledAttr}></div>
          ${residentOnce ? '<p class="muted text-sm">未指定日期的单次待办常驻显示、不设可安排时段：不能为它的当前实例新增时间窗口。</p>' : ''}
          ${hasWindow ? '<p class="muted text-sm">这一轮已带时段约束：两端都清空会取消既有约束，后端会拒绝；请保留至少一端。</p>' : ''}
          <div id="pf-adj-error" hidden></div>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>保存</button>`,
      });
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const start = root.querySelector('#planning-adj-window-start').value;
        const end = root.querySelector('#planning-adj-window-end').value;
        try {
          await gw(`/admin/api/planning/occurrences/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              window_start_at: start ? new Date(start).toISOString() : null,
              window_end_at: end ? new Date(end).toISOString() : null,
            }),
          });
          close();
          toast('时段已更新；待办时间将按新时段重新安排');
          await loadToday();
          await loadOccurrences();
        } catch (error) {
          const message = String(error.message || '');
          const area = root.querySelector('#pf-adj-error');
          if (area) {
            area.hidden = false;
            area.innerHTML = errorBlock(esc(message));
            area.scrollIntoView({ block: 'nearest' });
          }
          toast(`保存失败：${error.message}`, 'err');
        }
      };
    },

    askBackfill(id) {
      const occ = findOccurrence(id) || {};
      const toLocal = (iso) => {
        if (!iso) return '';
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return '';
        const pad = (n) => String(n).padStart(2, '0');
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
      };
      const { root, close } = modal({
        title: '补填实际时间',
        body: `
          <div class="field"><label>实际开始</label><input type="datetime-local" id="planning-backfill-start" value="${toLocal(occ.actual_start)}"></div>
          <div class="field"><label>实际结束</label><input type="datetime-local" id="planning-backfill-end" value="${toLocal(occ.actual_end)}"></div>
          <p class="muted text-sm">留空即清除该时间；同时有起止时自动计算实际耗时，预估耗时独立保留。</p>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>保存</button>`,
      });
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const start = root.querySelector('#planning-backfill-start').value;
        const end = root.querySelector('#planning-backfill-end').value;
        // 始终提交两个字段：留空 → null 即清除（BUG-6），actual_minutes 随之清空
        const body = {
          actual_start: start ? new Date(start).toISOString() : null,
          actual_end: end ? new Date(end).toISOString() : null,
        };
        try {
          await gw(`/admin/api/planning/occurrences/${id}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
          });
          close();
          toast('实际时间已更新');
          await loadToday();
        } catch (error) {
          toast(`保存失败：${error.message}`, 'err');
        }
      };
    },

    askSplit(id) {
      // 拆分 = 结束当前轮 + 创建 1～10 个新的单次待办（可选辅助功能，
      // partial → 已全部完成才是主流程）。默认只显示 1 个输入区域，用户
      // 点「添加待办」逐个增加，最多 10 个；至少保留 1 个，不允许删到 0。
      const partRow = (index) => `
        <div class="field" data-part-row>
          <label>待办 ${index}</label>
          <input type="text" data-part-content placeholder="内容">
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px">
            <label style="white-space:nowrap">耗时</label>
            <input type="text" data-part-minutes value="30" placeholder="分钟或 1h30m" style="width:130px">
            <button type="button" class="btn btn-quiet btn-sm" data-remove-part title="移除这一项">${icon('x')}移除</button>
          </div>
        </div>`;
      const { root, close } = modal({
        title: '拆分待办',
        body: `
          <p class="muted text-sm">结束当前这一轮，并把剩余工作拆成新的单次待办（今天执行）；原条目按「此次不执行」留痕，已有进度保留。</p>
          <div id="planning-split-parts">
            ${partRow(1)}
          </div>
          <button type="button" class="btn btn-quiet btn-sm" data-add-part>${icon('plus')}添加待办</button>
          <p class="muted text-sm" style="margin-top:6px">最多 10 个；只填 1 个也可以提交。</p>`,
        footer: `<button class="btn btn-secondary" data-cancel>取消</button>
                 <button class="btn btn-primary" data-ok>拆分</button>`,
      });
      const addBtn = root.querySelector('[data-add-part]');
      const syncRows = () => {
        const rows = root.querySelectorAll('[data-part-row]');
        rows.forEach((rowEl, index) => {
          rowEl.querySelector('label').textContent = `待办 ${index + 1}`;
          rowEl.querySelector('[data-remove-part]').style.display =
            rows.length > 1 ? '' : 'none';
        });
        addBtn.style.display = rows.length >= 10 ? 'none' : '';
      };
      addBtn.onclick = () => {
        const host = root.querySelector('#planning-split-parts');
        if (host.querySelectorAll('[data-part-row]').length >= 10) return;
        host.insertAdjacentHTML('beforeend', partRow(host.querySelectorAll('[data-part-row]').length + 1));
        syncRows();
      };
      root.querySelector('#planning-split-parts').addEventListener('click', (event) => {
        const remove = event.target.closest('[data-remove-part]');
        if (!remove) return;
        const host = root.querySelector('#planning-split-parts');
        if (host.querySelectorAll('[data-part-row]').length <= 1) return;
        remove.closest('[data-part-row]').remove();
        syncRows();
      });
      syncRows();
      root.querySelector('[data-cancel]').onclick = close;
      root.querySelector('[data-ok]').onclick = async () => {
        const parts = [...root.querySelectorAll('[data-part-row]')]
          .map((rowEl) => ({
            content: rowEl.querySelector('[data-part-content]').value.trim(),
            estimated_minutes: rowEl.querySelector('[data-part-minutes]').value.trim() || '30',
          }))
          .filter((part) => part.content);
        if (parts.length < 1) { toast('请至少填写一个待办内容', 'err'); return; }
        // 防双击：请求进行中禁用提交按钮；失败恢复以便重试（后端仍有
        // 「已关闭实例拒绝再次拆分」的业务兜底）
        const submit = root.querySelector('[data-ok]');
        if (submit.disabled) return;
        submit.disabled = true;
        try {
          await gw(`/admin/api/planning/occurrences/${id}/split`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ parts }),
          });
          close();
          toast('已拆分：原待办按「此次不执行」收口，新待办已创建');
          await Promise.all([loadToday(), loadTasks(), loadOccurrences()]);
        } catch (error) {
          submit.disabled = false;
          toast(`拆分失败：${error.message}`, 'err');
        }
      };
    },

    askRemaining(occ) {
      const source = typeof occ === 'object' ? occ : findOccurrence(occ);
      if (!source) return;
      openTaskForm({
        content: `${source.content}（剩余部分）`,
        task_type: 'once',
        estimated_minutes: source.estimated_minutes || 30,
        target_date: source.display_cycle_date || source.schedule_date || source.for_date,
      });
    },
  };
}
