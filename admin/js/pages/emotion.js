// pages/emotion.js - 情感：Eventide 身体状态真实展示（只读）
import { gw, esc } from '../api.js?v=20260920-planning-mobile1';
import { loading, empty, errorBlock, banner, tag, icon, fmtDate, delegate } from '../ui.js?v=20260920-planning-mobile1';

// 等级 → tag 色调：低→green、中低/中→slate、中高→amber、高→red
const LEVEL_TONES = { '低': 'green', '中低': 'slate', '中': 'slate', '中高': 'amber', '高': 'red' };

function fieldRow(field) {
  const value = Number(field.value);
  const hasValue = Number.isFinite(value);
  const pct = hasValue ? Math.max(0, Math.min(100, value)) : 0;
  const tone = LEVEL_TONES[field.level] || 'slate';
  return `
    <div class="body-field">
      <div class="body-field-head">
        <span class="body-field-name">${esc(field.label || field.key || '-')}</span>
        <span class="body-field-track" aria-hidden="true"><span class="body-field-fill" style="width:${pct}%"></span></span>
        <span class="body-field-value mono">${hasValue ? value : '-'}</span>
        ${field.level ? tag(esc(field.level), tone) : ''}
      </div>
      ${field.description ? `<div class="body-field-desc">${esc(field.description)}</div>` : ''}
    </div>`;
}

function summaryCells(data) {
  const cycle = data.cycle || {};
  const event = data.event || {};
  return `
    <div class="grid grid-2 body-summary">
      <div class="stat">
        <div class="label">当前周期</div>
        <div class="value accent">${cycle.label ? esc(cycle.label) : '-'}</div>
        <div class="body-stat-sub">${cycle.remaining_text ? esc(cycle.remaining_text) : ''}</div>
      </div>
      <div class="stat">
        <div class="label">当前事件</div>
        <div class="value ${event.label ? 'warn' : ''}">${event.label ? esc(event.label) : '无'}</div>
        <div class="body-stat-sub">${event.remaining_text ? esc(event.remaining_text) : ''}</div>
      </div>
    </div>`;
}

export default {
  busy: false,

  async mount(root) {
    this.root = root;
    this.renderActions();
    delegate(root, { refresh: () => this.load() });
    await this.load();
  },

  unmount() {
    const actions = document.getElementById('page-head-actions');
    if (actions) actions.innerHTML = '';
  },

  renderActions() {
    const actions = document.getElementById('page-head-actions');
    if (actions) {
      actions.innerHTML = `<button class="btn btn-secondary" data-act="refresh" ${this.busy ? 'disabled' : ''}>${icon('refresh')}刷新</button>`;
    }
  },

  async load() {
    if (this.busy) return;
    this.busy = true;
    this.renderActions();
    this.root.innerHTML = loading('正在读取身体状态…');
    try {
      const data = await gw('/admin/api/eventide/body');
      this.render(data);
    } catch (error) {
      this.root.innerHTML = errorBlock(`身体状态读取失败：${esc(error.message)}`);
    } finally {
      this.busy = false;
      this.renderActions();
    }
  },

  render(data) {
    const off = data.inject_enabled === false;
    const bannerHtml = off ? banner('身体状态注入已关闭 · 模拟已暂停') : '';

    let bodyHtml;
    if (!data.initialized) {
      bodyHtml = empty('身体状态尚未初始化', '开启注入后首次聊天时自动创建');
    } else {
      const fields = Array.isArray(data.fields) ? data.fields : [];
      bodyHtml = `
        ${summaryCells(data)}
        <div class="body-fields">
          ${fields.length ? fields.map(fieldRow).join('') : '<p class="muted">暂无数值。</p>'}
        </div>
        ${data.updated_at ? `<p class="muted text-sm body-updated">更新于 ${esc(fmtDate(data.updated_at))}</p>` : ''}`;
    }

    this.root.innerHTML = `
      ${bannerHtml}
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${icon('heart')}身体状态</div>
            <div class="card-sub">由 Eventide 在聊天时自动推进；本页只读展示，不提供手动调整。</div>
          </div>
        </div>
        ${bodyHtml}
      </div>`;
  },
};
