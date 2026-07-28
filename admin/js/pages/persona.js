// pages/persona.js
import { query, update, insert, esc } from '../api.js?v=20260728-rls1';
import { loading, empty, badge, toast, modal, delegate } from '../ui.js?v=20260728-rls1';

export default {
  async mount(root) {
    this.root = root;
    root.innerHTML = loading();
    delegate(root, {
      add: () => this.openEditor(null),
      edit: (el) => this.openEditor(el.dataset.id),
      toggle: (el) => this.toggleActive(el.dataset.id, el.dataset.active === 'true'),
    });
    await this.loadList();
  },

  async loadList() {
    const data = await query('persona', { order: { col: 'id', asc: true } });
    if (!data.length) { this.root.innerHTML = empty('No persona found') + `<button class="btn btn-primary mt16" data-act="add">+ Add Persona</button>`; return; }
    this.root.innerHTML = `
      <div class="toolbar"><span style="flex:1"></span><button class="btn btn-primary" data-act="add">+ Add Persona</button></div>
      ${data.map(p => `
        <div class="item">
          <div class="item-row">
            <div style="flex:1;min-width:0">
              <div class="item-title">${esc(p.name)} ${p.is_active ? badge('Active', 'accent') : badge('Inactive', 'muted')}</div>
              <div class="text-sm muted clamp2 mt8">${esc((p.content || '').slice(0, 200))}</div>
            </div>
            <div class="item-actions">
              <button class="btn btn-xs btn-secondary" data-act="toggle" data-id="${p.id}" data-active="${p.is_active}">${p.is_active ? 'Deactivate' : 'Activate'}</button>
              <button class="btn btn-xs btn-secondary" data-act="edit" data-id="${p.id}">Edit</button>
            </div>
          </div>
        </div>`).join('')}
    `;
  },

  async openEditor(id) {
    let p = {};
    if (id) {
      const rows = await query('persona', { eq: { id: Number(id) }, limit: 1 });
      p = rows[0] || {};
    }
    const isNew = !id;
    const { root, close } = modal({
      title: isNew ? 'New Persona' : `Edit: ${p.name || ''}`,
      body: `
        <div class="field"><label>Name</label><input type="text" id="ed-name" value="${esc(p.name || '')}"></div>
        <div class="field"><label>Content (System Prompt)</label><textarea id="ed-content" rows="16" class="mono">${esc(p.content || '')}</textarea></div>`,
      footer: `<button class="btn btn-secondary" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save</button>`,
    });
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-save]').onclick = async () => {
      const row = {
        name: root.querySelector('#ed-name').value.trim(),
        content: root.querySelector('#ed-content').value.trim(),
      };
      if (!row.name || !row.content) { toast('Name and content required', 'err'); return; }
      try {
        if (isNew) await insert('persona', { ...row, is_active: false });
        else await update('persona', id, row);
        toast(isNew ? 'Created' : 'Updated');
        close(); this.loadList();
      } catch (e) { toast('Error: ' + e.message, 'err'); }
    };
  },

  async toggleActive(id, current) {
    try {
      await update('persona', id, { is_active: !current });
      toast(current ? 'Deactivated' : 'Activated');
      this.loadList();
    } catch (e) { toast('Error: ' + e.message, 'err'); }
  },
};
