// pages/status.js
import { gw, esc } from '../api.js?v=20260728-rls3';
import { loading, badge } from '../ui.js?v=20260728-rls3';

export default {
  async mount(root) {
    root.innerHTML = loading();
    try {
      const [health, status] = await Promise.all([gw('/health'), gw('/status')]);
      const uptimeMin = Math.floor((health.uptime || 0) / 60);
      const supabase = status.supabase || {};
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
          <div class="card-head"><div class="card-title">Supabase & RLS Readiness</div></div>
          <div class="kv"><span class="k">Database access</span><span class="v">${supabase.access_ok ? badge('ok', 'accent') : badge('failed', 'danger')}</span></div>
          <div class="kv"><span class="k">Active key mode</span><span class="v mono">${esc(supabase.key_mode || 'unknown')}</span></div>
          <div class="kv"><span class="k">Elevated key configured</span><span class="v">${supabase.elevated_configured ? badge('yes', 'accent') : badge('no', 'warn')}</span></div>
          <div class="kv"><span class="k">Elevated key active</span><span class="v">${supabase.elevated_active ? badge('yes', 'accent') : badge('no', 'danger')}</span></div>
          <div class="kv"><span class="k">Safe to enable RLS</span><span class="v">${status.rls_ready ? badge('ready', 'accent') : badge('not ready', 'danger')}</span></div>
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
