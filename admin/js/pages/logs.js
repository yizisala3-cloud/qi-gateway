// pages/logs.js - 日志：真实空状态，接口尚未接入
import { icon } from '../ui.js?v=20260925-planning9';
import { empty } from '../ui.js?v=20260925-planning9';

export default {
  async mount(root) {
    root.innerHTML = `
      <div class="toolbar">
        <select disabled title="暂未接入"><option>时间筛选（暂未接入）</option></select>
        <select disabled title="暂未接入"><option>日志类型（暂未接入）</option></select>
        <select disabled title="暂未接入"><option>错误级别（暂未接入）</option></select>
        <button class="btn btn-secondary" disabled title="暂未接入">${icon('refresh')}刷新</button>
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
  },
};
