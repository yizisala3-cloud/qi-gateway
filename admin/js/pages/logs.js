// pages/logs.js - 日志：真实空状态，接口尚未接入
import { icon, empty } from '../ui.js?v=20261002-frontend-controls1';
import { initRetroSelectFields } from '../lib/retro_select.js?v=20261002-frontend-controls1';

export default {
  async mount(root) {
    root.innerHTML = `
      <div class="toolbar">
        <select id="logs-filter-time" disabled aria-label="时间筛选" aria-describedby="logs-unavailable-note"><option>时间筛选（暂未接入）</option></select>
        <select id="logs-filter-type" disabled aria-label="日志类型" aria-describedby="logs-unavailable-note"><option>日志类型（暂未接入）</option></select>
        <select id="logs-filter-level" disabled aria-label="错误级别" aria-describedby="logs-unavailable-note"><option>错误级别（暂未接入）</option></select>
        <button class="btn btn-secondary" disabled aria-label="刷新日志" aria-describedby="logs-unavailable-note">${icon('refresh')}刷新</button>
        <span class="disabled-note" id="logs-unavailable-note">暂未接入</span>
      </div>
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${icon('journal')}日志</div>
            <div class="card-sub">报错日志与调用日志将在此展示；日志查询接口尚未接入。</div>
          </div>
        </div>
        ${empty('日志接口尚未接入', '当前后端没有日志查询接口；页面仅保留筛选与详情结构，不会展示任何模拟日志')}
      </div>
      <p class="muted text-sm">上方筛选控件为预留占位，保持禁用状态；接入真实日志接口后启用，不会伪造日志记录。</p>`;
    initRetroSelectFields(root);
  },
};
