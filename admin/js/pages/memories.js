// pages/memories.js - 记忆管理：记忆库 + 审核申请
import { createMemoryBrowser } from './_memory_browser.js?v=20261002-frontend-controls1';

export default {
  browser: null,

  async mount(root, params = {}) {
    this.browser = createMemoryBrowser({
      host: root,
      showViewTabs: true,
      showTypeFilter: true,
      defaultView: params.view === 'requests' ? 'requests' : 'library',
    });
    await this.browser.mount();
    if (params.memory) await this.browser.openMemory(params.memory);
    else if (params.request) await this.browser.openRequest(params.request);
  },

  unmount() { this.browser = null; },
};
