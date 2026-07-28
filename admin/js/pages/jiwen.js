// pages/jiwen.js
import { query, update, esc } from '../api.js?v=20260728-rls1';
import { loading, toast, confirm, delegate } from '../ui.js?v=20260728-rls1';

export default {
  async mount(root) {
    this.root = root;
    root.innerHTML = loading();
    delegate(root, { reset: () => this.doReset() });
    await this.load();
  },

  async load() {
    const data = await query('jiwen_state', { limit: 1 });
    const j = data[0] || {};
    const axes = [
      { name: 'Connection', val: Number(j.connection ?? 0) },
      { name: 'Pride', val: Number(j.pride ?? 0) },
      { name: 'Valence', val: Number(j.valence ?? 0) },
      { name: 'Arousal', val: Number(j.arousal ?? 0) },
      { name: 'Immersion', val: Number(j.immersion ?? 0) },
    ];
    this.root.innerHTML = `
      <div class="card">
        <div class="card-head"><div class="card-title">Five Axes</div></div>
        <div class="jiwen-bars">
          ${axes.map(a => `
            <div class="jiwen-row">
              <span class="jiwen-label">${a.name}</span>
              <div class="jiwen-bar-bg"><div class="jiwen-bar-fill" style="width:${Math.min(100, Math.max(0, (a.val + 100) / 2))}%"></div></div>
              <span class="jiwen-val">${a.val.toFixed(2)}</span>
            </div>`).join('')}
        </div>
      </div>
      <div class="card mt16">
        <div class="card-head"><div class="card-title">State Info</div></div>
        <div class="kv"><span class="k">User status</span><span class="v">${esc(j.user_status || '-')}</span></div>
        <div class="kv"><span class="k">Last chat at</span><span class="v">${esc(j.last_chat_at || '-')}</span></div>
        <div class="kv"><span class="k">Last bot reply</span><span class="v">${esc(j.last_bot_at || '-')}</span></div>
        <div class="kv"><span class="k">Last tick</span><span class="v">${esc(j.last_tick_at || '-')}</span></div>
      </div>
      <button class="btn btn-danger mt16" data-act="reset">Reset All Axes to 0</button>
    `;
  },

  async doReset() {
    if (!(await confirm('Reset all jiwen axes to 0?'))) return;
    try {
      await update('jiwen_state', 1, { connection: 0, pride: 0, valence: 0, arousal: 0, immersion: 0 });
      toast('Reset done');
      this.load();
    } catch (e) { toast('Error: ' + e.message, 'err'); }
  },
};
