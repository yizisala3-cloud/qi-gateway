// pages/persona.js - 人设与规则：人设 / 用户资料 / 互动规则
import { query, update, insert, esc } from '../api.js?v=20261002-frontend-controls1';
import { loading, empty, tag, toast, modal, confirm, delegate, icon } from '../ui.js?v=20261002-frontend-controls1';
import { createMemoryBrowser } from './_memory_browser.js?v=20261002-frontend-controls1';

const VIEW_TABS = [
  { key: 'persona', label: '人设' },
  { key: 'profile', label: '用户资料' },
  { key: 'rule', label: '互动规则' },
];

export default {
  view: 'persona',
  browser: null,

  async mount(root, params = {}) {
    this.root = root;
    this.browser = null;
    const initial = { persona: 'persona', profile: 'profile', rule: 'rule' }[params.tab] || 'persona';
    this.renderShell(initial);
    delegate(this.root, {
      tab: async (el) => {
        this.renderShell(el.dataset.tab);
        await this.renderView();
      },
      add: () => this.openEditor(null),
      edit: (el) => this.openEditor(el.dataset.id),
      toggle: (el) => this.toggleActive(el.dataset.id, el.dataset.active === 'true'),
    });
    await this.renderView();
  },

  renderShell(view) {
    this.view = view;
    this.root.innerHTML = `
      <div class="toolbar" style="margin-bottom:14px">
        <div class="tabs" role="tablist">
          ${VIEW_TABS.map((t) => `<button class="tab ${this.view === t.key ? 'active' : ''}" data-act="tab" data-tab="${t.key}">${t.label}</button>`).join('')}
        </div>
      </div>
      <div id="persona-view"></div>`;
  },

  async renderView() {
    const host = this.root.querySelector('#persona-view');
    if (this.view === 'persona') {
      this.browser = null;
      host.classList.remove('page-with-detail');
      await this.renderPersonaList(host);
    } else {
      this.browser = createMemoryBrowser({
        host,
        lockedType: this.view === 'profile' ? 'profile' : 'interaction_rule',
        showTypeFilter: false,
        defaultView: 'library',
      });
      await this.browser.mount();
    }
  },

  /* ----- 人设 ----- */
  async renderPersonaList(host) {
    host.innerHTML = loading();
    let data;
    try {
      data = await query('persona', { order: { col: 'id', asc: true } });
    } catch (error) {
      host.innerHTML = `<div class="banner banner-danger">人设列表读取失败：${esc(error.message)}</div>`;
      return;
    }
    host.innerHTML = `
      <div class="toolbar">
        <span class="grow"></span>
        <button class="btn btn-secondary is-disabled" disabled>前端写入 persona（暂未接入）</button>
        <button class="btn btn-primary" data-act="add">${icon('plus')}新增人设</button>
      </div>
      <p class="muted text-sm" style="margin:-6px 0 14px">“前端写入”与提示词注入开关暂未接入，按钮仅作占位。</p>
      ${data.length ? data.map((p) => `
        <div class="mem-card" style="cursor:default">
          <div class="card-top">
            <div class="card-main">
              <div class="mem-title">${esc(p.name)} ${p.is_active ? tag('启用中', 'green') : tag('已停用', 'muted')}</div>
              <div class="mem-snippet">${esc((p.content || '').slice(0, 200))}</div>
              <div class="card-meta">更新于 ${esc(fmtUpdatedAt(p.updated_at))} · system prompt 独立存储，不与用户资料 / 互动规则记忆混用</div>
            </div>
            <div class="card-side">
              <button class="btn btn-secondary btn-sm" data-act="toggle" data-id="${p.id}" data-active="${p.is_active}">${p.is_active ? '停用' : '启用'}</button>
              <button class="btn btn-secondary btn-sm" data-act="edit" data-id="${p.id}">${icon('edit')}编辑</button>
            </div>
          </div>
        </div>`).join('') : empty('还没有人设', '点击右上角“新增人设”创建第一条 system prompt')}
      <div class="toolbar mt16">
        <span class="disabled-note">提示词注入开关：暂未接入</span>
      </div>`;
  },

  async openEditor(id) {
    let p = {};
    if (id) {
      const rows = await query('persona', { eq: { id: Number(id) }, limit: 1 });
      p = rows[0] || {};
    }
    const isNew = !id;
    const { root, close } = modal({
      title: isNew ? '新增人设' : `编辑人设：${p.name || ''}`,
      body: `
        <div class="field"><label>名称</label><input type="text" id="ed-name" value="${esc(p.name || '')}"></div>
        <div class="field"><label>内容（System Prompt）</label><textarea id="ed-content" rows="14" class="mono">${esc(p.content || '')}</textarea></div>`,
      footer: `<button class="btn btn-secondary" data-cancel>取消</button><button class="btn btn-primary" data-save>保存</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-save]').onclick = async (event) => {
      const row = {
        name: root.querySelector('#ed-name').value.trim(),
        content: root.querySelector('#ed-content').value.trim(),
      };
      if (!row.name || !row.content) { toast('名称和内容均为必填', 'err'); return; }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        if (isNew) await insert('persona', { ...row, is_active: false });
        else await update('persona', id, row);
        toast(isNew ? '人设已创建（默认停用，可手动启用）' : '人设已更新');
        close();
        await this.renderView();
      } catch (error) {
        toast(`保存失败：${error.message}`, 'err');
        button.disabled = false;
      }
    };
  },

  async toggleActive(id, current) {
    try {
      await update('persona', id, { is_active: !current });
      toast(current ? '已停用' : '已启用');
      await this.renderView();
    } catch (error) {
      toast(`操作失败：${error.message}`, 'err');
    }
  },

  unmount() { this.browser = null; },
};

function fmtUpdatedAt(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
}
