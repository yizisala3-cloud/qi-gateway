// pages/dashboard.js
import { gw, query, count, esc } from '../api.js';
import { stat, loading, heatDot } from '../ui.js';

export default {
  async mount(root) {
    root.innerHTML = loading();

    const [memTotal, memActive, memPending, chatToday, recentMems, jiwen] = await Promise.allSettled([
      count('memories'),
      count('memories', { eq: { is_active: true } }),
      count('memories', { eq: { verified: 'pending' } }),
      count('chat_messages'),
      query('memories', { select: 'id,title,content,heat,created_at', order: { col: 'created_at', asc: false }, limit: 5, eq: { is_active: true } }),
      query('jiwen_state', { limit: 1 }),
    ]);

    const v = (r) => r.status === 'fulfilled' ? r.value : 0;
    const vd = (r) => r.status === 'fulfilled' ? r.value : [];

    const jiwenData = vd(jiwen)[0] || {};
    const axes = [
      { name: 'Connection', val: jiwenData.connection || 0 },
      { name: 'Pride', val: jiwenData.pride || 0 },
      { name: 'Valence', val: jiwenData.valence || 0 },
      { name: 'Arousal', val: jiwenData.arousal || 0 },
      { name: 'Immersion', val: jiwenData.immersion || 0 },
    ];

    root.innerHTML = `
      <div class="grid grid-4">
        ${stat('\u8BB0\u5FC6\u603B\u6570', v(memTotal), 'accent')}
        ${stat('\u6D3B\u8DC3\u8BB0\u5FC6', v(memActive), 'info')}
        ${stat('\u5F85\u5BA1\u6838', v(memPending), 'warn')}
        ${stat('\u5BF9\u8BDD\u6761\u6570', v(chatToday), '')}
      </div>

      <div class="card mt16">
        <div class="card-head"><div class="card-title">\u79EF\u6E29\u4E94\u8F74</div></div>
        <div class="jiwen-bars">
          ${axes.map(a => `
            <div class="jiwen-row">
              <span class="jiwen-label">${a.name}</span>
              <div class="jiwen-bar-bg"><div class="jiwen-bar-fill" style="width:${Math.min(100, Math.max(0, (a.val + 100) / 2))}%"></div></div>
              <span class="jiwen-val">${a.val.toFixed(1)}</span>
            </div>`).join('')}
        </div>
      </div>

      <div class="card mt16">
        <div class="card-head"><div class="card-title">\u6700\u8FD1\u8BB0\u5FC6</div></div>
        ${vd(recentMems).length ? vd(recentMems).map(m => `
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
  }
};
