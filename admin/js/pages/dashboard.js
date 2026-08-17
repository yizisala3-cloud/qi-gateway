// pages/dashboard.js
import { query, count, esc } from '../api.js?v=20260728-rls1';
import { stat, loading, heatDot, errorBlock } from '../ui.js?v=20260728-rls1';

export default {
  async mount(root) {
    root.innerHTML = loading();

    try {
      const [memTotal, memActive, memPending, chatTotal, recentMems] = await Promise.all([
        count('memories'),
        count('memories', { eq: { is_active: true } }),
        count('memories', { eq: { verified: 'pending' } }),
        count('chat_messages'),
        query('memories', {
          select: 'id,title,content,heat,created_at',
          order: { col: 'created_at', asc: false },
          limit: 5,
          eq: { is_active: true },
        }),
      ]);

      root.innerHTML = `
        <div class="grid grid-4">
          ${stat('\u8BB0\u5FC6\u603B\u6570', memTotal, 'accent')}
          ${stat('\u6D3B\u8DC3\u8BB0\u5FC6', memActive, 'info')}
          ${stat('\u5F85\u5BA1\u6838', memPending, 'warn')}
          ${stat('\u5BF9\u8BDD\u6761\u6570', chatTotal, '')}
        </div>

        <div class="card mt16">
          <div class="card-head"><div class="card-title">\u6700\u8FD1\u8BB0\u5FC6</div></div>
          ${recentMems.length ? recentMems.map(m => `
            <div class="item">
              <div class="item-row">
                <div style="flex:1;min-width:0">
                  <div class="item-title">${esc(m.title || '(untitled)')}</div>
                  <div class="text-sm muted clamp2">${esc((m.content || '').slice(0, 120))}</div>
                </div>
                ${heatDot(m.heat)}
              </div>
            </div>`).join('') : '<p class="muted">No memories yet</p>'}
        </div>
      `;
    } catch (e) {
      console.error('Dashboard data load failed:', e);
      root.innerHTML = errorBlock(`Dashboard data load failed: ${esc(e.message)}`);
    }
  }
};
