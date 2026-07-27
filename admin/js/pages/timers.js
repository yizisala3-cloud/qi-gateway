// pages/timers.js
import { query, update, esc } from '../api.js';
import { loading, empty, badge, toast, delegate } from '../ui.js';

export default {
  async mount(root) {
    this.root = root;
    root.innerHTML = loading();
    delegate(root, { cancel: (el) => this.doCancel(el.dataset.id) });
    await this.loadList();
  },

  async loadList() {
    const data = await query('timers', { order: { col: 'created_at', asc: false }, limit: 50 });
    if (!data.length) { this.root.innerHTML = empty('No timers'); return; }
    this.root.innerHTML = data.map(t => {
      const done = t.executed || t.cancelled;
      return `
      <div class="item ${done ? 'item-dim' : ''}">
        <div class="item-row">
          <div style="flex:1;min-width:0">
            <div class="item-title">${badge(t.type, 'info')} ${esc(t.summary || '')}</div>
            <div class="text-sm muted mt8">
              Expire: ${esc(t.expire_at || '-')} |
              ${t.executed ? badge('Executed', 'accent') : t.cancelled ? badge('Cancelled', 'danger') : badge('Pending', 'warn')}
            </div>
          </div>
          ${!done ? `<button class="btn btn-xs btn-danger-soft" data-act="cancel" data-id="${t.id}">Cancel</button>` : ''}
        </div>
      </div>`;
    }).join('');
  },

  async doCancel(id) {
    try {
      await update('timers', id, { cancelled: true });
      toast('Cancelled');
      this.loadList();
    } catch (e) { toast('Error: ' + e.message, 'err'); }
  },
};
