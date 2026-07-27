// pages/memories.js
import { query, update, insert, remove, esc, count } from '../api.js';
import { loading, empty, heatDot, badge, toast, modal, confirm, delegate } from '../ui.js';

const PAGE_SIZE = 20;

export default {
  state: { page: 0, sort: 'created_at', filter: '', search: '' },

  async mount(root) {
    this.root = root;
    this.renderShell();
    delegate(root, {
      add: () => this.openEditor(null),
      edit: (el) => this.openEditor(el.dataset.id),
      del: (el) => this.doDelete(el.dataset.id),
      verify: (el) => this.setVerified(el.dataset.id, 'verified'),
      reject: (el) => this.setVerified(el.dataset.id, 'rejected'),
      page: (el) => { this.state.page = Number(el.dataset.p); this.loadList(); },
      search: () => { this.state.search = root.querySelector('#mem-search').value.trim(); this.state.page = 0; this.loadList(); },
    });
    root.querySelector('#mem-search')?.addEventListener('keydown', e => { if (e.key === 'Enter') { this.state.search = e.target.value.trim(); this.state.page = 0; this.loadList(); }});
    root.querySelector('#flt-verified')?.addEventListener('change', e => { this.state.filter = e.target.value; this.state.page = 0; this.loadList(); });
    root.querySelector('#flt-sort')?.addEventListener('change', e => { this.state.sort = e.target.value; this.state.page = 0; this.loadList(); });
    await this.loadList();
  },

  renderShell() {
    this.root.innerHTML = `
      <div class="toolbar">
        <input type="search" id="mem-search" class="grow" placeholder="Search memories...">
        <button class="btn btn-secondary" data-act="search">Search</button>
        <select id="flt-verified" style="width:130px">
          <option value="">All status</option>
          <option value="pending">Pending</option>
          <option value="verified">Verified</option>
          <option value="rejected">Rejected</option>
        </select>
        <select id="flt-sort" style="width:140px">
          <option value="created_at">Newest</option>
          <option value="heat">By heat</option>
          <option value="importance">By importance</option>
        </select>
        <span style="flex:1"></span>
        <button class="btn btn-primary" data-act="add">+ Add Memory</button>
      </div>
      <div id="mem-list">${loading()}</div>
      <div id="mem-pager" class="pagination"></div>
    `;
  },

  async loadList() {
    const listEl = this.root.querySelector('#mem-list');
    listEl.innerHTML = loading();
    try {
      const filters = [];
      if (this.state.filter) filters.push(q => q.eq('verified', this.state.filter));
      if (this.state.search) filters.push(q => q.or(`title.ilike.%${this.state.search}%,content.ilike.%${this.state.search}%`));
      filters.push(q => q.eq('is_active', true));

      const data = await query('memories', {
        select: 'id,title,content,heat,importance,tags,verified,source,layer,created_at',
        order: { col: this.state.sort, asc: false },
        limit: PAGE_SIZE,
        filters: [q => { let r = q; for (const f of filters) r = f(r); return r.range(this.state.page * PAGE_SIZE, (this.state.page + 1) * PAGE_SIZE - 1); }],
      });

      if (!data.length) { listEl.innerHTML = empty('No memories found'); return; }

      listEl.innerHTML = data.map(m => `
        <div class="item">
          <div class="item-row">
            <div style="flex:1;min-width:0">
              <div class="item-title">${esc(m.title || '(untitled)')}</div>
              <div class="text-sm muted clamp2">${esc((m.content || '').slice(0, 150))}</div>
              <div class="btn-row mt8">
                ${heatDot(m.heat)}
                ${badge('imp:' + m.importance, m.importance >= 8 ? 'purple' : m.importance >= 5 ? 'accent' : 'muted')}
                ${badge(m.verified || 'pending', m.verified === 'verified' ? 'accent' : m.verified === 'rejected' ? 'danger' : 'warn')}
                ${badge(m.layer || '-', 'muted')}
                ${(m.tags || []).slice(0, 3).map(t => badge(t, 'info')).join('')}
              </div>
            </div>
            <div class="item-actions">
              ${m.verified === 'pending' ? `<button class="btn btn-xs btn-soft" data-act="verify" data-id="${m.id}">Approve</button>` : ''}
              <button class="btn btn-xs btn-secondary" data-act="edit" data-id="${m.id}">Edit</button>
              <button class="btn btn-xs btn-danger-soft" data-act="del" data-id="${m.id}">Del</button>
            </div>
          </div>
        </div>`).join('');

    } catch (e) {
      listEl.innerHTML = `<div class="banner banner-danger">${esc(e.message)}</div>`;
    }
  },

  async openEditor(id) {
    let mem = {};
    if (id) {
      const rows = await query('memories', { eq: { id }, limit: 1 });
      mem = rows[0] || {};
    }
    const isNew = !id;
    const { root, close } = modal({
      title: isNew ? 'Add Memory' : `Edit #${id}`,
      body: `
        <div class="field"><label>Title</label><input type="text" id="ed-title" value="${esc(mem.title || '')}"></div>
        <div class="field"><label>Content</label><textarea id="ed-content" rows="6">${esc(mem.content || '')}</textarea></div>
        <div class="field"><label>Tags (comma separated)</label><input type="text" id="ed-tags" value="${esc((mem.tags || []).join(', '))}"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
          <div class="field"><label>Importance (1-10)</label><input type="number" id="ed-imp" min="1" max="10" value="${mem.importance || 5}"></div>
          <div class="field"><label>Layer</label><select id="ed-layer"><option value="碎片">碎片</option><option value="场景">场景</option><option value="核心">核心</option></select></div>
          <div class="field"><label>Emotion weight</label><input type="number" id="ed-emo" min="0" max="1" step="0.1" value="${mem.emotion_weight ?? 0.5}"></div>
        </div>`,
      footer: `<button class="btn btn-secondary" data-cancel>Cancel</button><button class="btn btn-primary" data-save>Save</button>`,
    });
    if (mem.layer) root.querySelector('#ed-layer').value = mem.layer;
    root.querySelector('[data-cancel]').onclick = close;
    root.querySelector('[data-save]').onclick = async () => {
      const row = {
        title: root.querySelector('#ed-title').value.trim(),
        content: root.querySelector('#ed-content').value.trim(),
        tags: root.querySelector('#ed-tags').value.split(',').map(s => s.trim()).filter(Boolean),
        importance: Number(root.querySelector('#ed-imp').value) || 5,
        layer: root.querySelector('#ed-layer').value,
        emotion_weight: Number(root.querySelector('#ed-emo').value) || 0.5,
      };
      if (!row.content) { toast('Content required', 'err'); return; }
      try {
        if (isNew) await insert('memories', row);
        else await update('memories', id, row);
        toast(isNew ? 'Created' : 'Updated');
        close();
        this.loadList();
      } catch (e) { toast('Error: ' + e.message, 'err'); }
    };
  },

  async doDelete(id) {
    if (!(await confirm('Archive this memory? (is_active=false)'))) return;
    try {
      await update('memories', id, { is_active: false });
      toast('Archived');
      this.loadList();
    } catch (e) { toast('Error: ' + e.message, 'err'); }
  },

  async setVerified(id, status) {
    try {
      await update('memories', id, { verified: status });
      toast(`Marked ${status}`);
      this.loadList();
    } catch (e) { toast('Error: ' + e.message, 'err'); }
  },
};
