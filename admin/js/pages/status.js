// pages/status.js
import { gw, esc } from '../api.js';
import { loading, badge } from '../ui.js';

export default {
  async mount(root) {
    root.innerHTML = loading();
    try {
      const [health, status] = await Promise.all([gw('/health'), gw('/status')]);
      const uptimeMin = Math.floor((health.uptime || 0) / 60);
      root.innerHTML = `
        <div class="card">
          <div class="card-head"><div class="card-title">Gateway Health</div></div>
          <div class="kv"><span class="k">Status</span><span class="v">${badge('ok', 'accent')}</span></div>
          <div class="kv"><span class="k">Phase</span><span class="v">${esc(health.phase)}</span></div>
          <div class="kv"><span class="k">Uptime</span><span class="v">${uptimeMin} min</span></div>
          <div class="kv"><span class="k">Scheduler</span><span class="v">${health.scheduler_running ? badge('running', 'accent') : badge('stopped', 'danger')}</span></div>
          <div class="kv"><span class="k">Timer loop</span><span class="v">${health.timer_running ? badge('running', 'accent') : badge('stopped', 'danger')}</span></div>
          <div class="kv"><span class="k">Daily task</span><span class="v">${health.daily_running ? badge('running', 'accent') : badge('stopped', 'danger')}</span></div>
        </div>
        <div class="card mt16">
          <div class="card-head"><div class="card-title">Config</div></div>
          <div class="kv"><span class="k">Upstream</span><span class="v mono text-sm">${esc(status.upstream_base_url)}</span></div>
          <div class="kv"><span class="k">Model</span><span class="v mono text-sm">${esc(status.upstream_model)}</span></div>
          <div class="kv"><span class="k">BG tasks</span><span class="v">${status.bg_tasks}</span></div>
          <div class="kv"><span class="k">Last digest</span><span class="v">${esc(status.last_digest_date || 'never')}</span></div>
        </div>
      `;
    } catch (e) {
      root.innerHTML = `<div class="banner banner-danger">Failed to fetch status: ${esc(e.message)}</div>`;
    }
  }
};
