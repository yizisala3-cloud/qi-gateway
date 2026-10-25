// pages/memories.js - 记忆管理：记忆库 + 审核申请 + 召回记录
import { createMemoryBrowser } from './_memory_browser.js?v=20261025-console-latency-r1';

export default {
  browser: null,

  async mount(root, params = {}) {
    const browser = this.browser = createMemoryBrowser({
      host: root,
      showViewTabs: true,
      showTypeFilter: true,
      showRecallTab: true,
      defaultView: ['requests', 'recall'].includes(params.view) ? params.view : 'library',
    });
    await browser.mount({ memoryId: params.memory, requestId: params.request });
  },

  unmount() { this.browser?.destroy(); this.browser = null; },
};
