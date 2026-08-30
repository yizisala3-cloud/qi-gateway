// pages/emotion.js - 情感：Eventide 身体状态展示区（接口尚未接入）
import { icon } from '../ui.js?v=20260830-retro1';
import { empty } from '../ui.js?v=20260830-retro1';

export default {
  async mount(root) {
    root.innerHTML = `
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${icon('heart')}身体状态</div>
            <div class="card-sub">这里将展示 Eventide 的实时身体状态；数据容器与组件结构已预留。</div>
          </div>
        </div>
        <!-- Eventide 身体状态数据容器：接入后端读取接口后由此处渲染 -->
        <div class="eventide-slot" data-component="eventide-body-status">
          ${empty('身体状态接口尚未接入', '后端提供读取接口前，这里不会显示任何模拟数值')}
        </div>
      </div>
      <div class="card">
        <div class="card-head">
          <div>
            <div class="card-title">${icon('info')}说明</div>
          </div>
        </div>
        <p class="muted" style="margin:0">情感页面第一版仅提供真实页面外壳。在现有后端补齐身体状态读取接口之前，本页不会伪造任何数据，也不会放置无效的操作按钮。</p>
      </div>`;
  },
};
