// pages/dashboard.js
import { query, count, esc } from '../api.js';
import { stat, loading, heatDot, errorBlock } from '../ui.js';

export default {
  async mount(root) {
    root.innerHTML = loading();

    try {
      const [memTotal, memActive, memPending, chatTotal, recentMems, jiwenRows] = await Promise.all([
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
        query('jiwen_state', {
          select: 'id,connection,pride,valence,arousal,immersion,last_chat_at,last_bot_at,user_status',
          order: { col: 'id', asc: true },
          limit: 1,
        }),
      ]);

      const jiwenData = jiwenRows[0] || {};
      const axes = [
        { name: 'Connection', val: Number(jiwenData.connection ?? 0) },
        { name: 'Pride', val: Number(jiwenData.pride ?? 0) },
        { name: 'Valence', val: Number(jiwenData.valence ?? 0) },
        { name: 'Arousal', val: Number(jiwenData.arousal ?? 0) },
        { name: 'Immersion', val: Number(jiwenData.immersion ?? 0) },
      ];

      root.innerHTML = `
        <div class="grid grid-4">
          ${stat('\u8BB0\u5FC6\u603B\u6570', memTotal, 'accent')}
          ${stat('\u6D3B\u8DC3\u8BB0\u5FC6', memActive, 'info')}
          ${stat('\u5F85\u5BA1\u6838', memPending, 'warn')}
          ${stat('\u5BF9\u8BDD\u6761\u6570', chatTotal, '')}
        </div>

        <div class="card mt16">
          <div class="card-head">
            <div class="card-title">\u79EF\u6E29\u4E94\u8F74</div>
            <div class="text-sm muted">${esc(jiwenData.user_status || 'unknown')}</div>
          </div>
          <div class="jiwen-bars">
            ${axes.map(a => `
              <div class="jiwen-row">
                <span class="jiwen-label">${a.name}</span>
                <div class="jiwen-bar-bg"><div class="jiwen-bar-fill" style="width:${Math.min(100, Math.max(0, (a.val + 100) / 2))}%"></div></div>
                <span class="jiwen-val">${a.val.toFixed(1)}</span>
              </div>`).join('')}
          </div>
          <div class="text-sm muted mt16">
            Last chat: ${esc(jiwenData.last_chat_at || '-')} &nbsp; | &nbsp;
            Last reply: ${esc(jiwenData.last_bot_at || '-')}
          </div>
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
