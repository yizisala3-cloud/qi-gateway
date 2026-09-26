// pages/config.js - 配置：真实状态展示 + 功能开关
import { gw, esc } from '../api.js?v=20260927-planning10';
import { loading, errorBlock, tag, toast, delegate, icon } from '../ui.js?v=20260927-planning10';

function boolTag(value, yes = '已配置', no = '未配置') {
  return value ? tag(yes, 'green') : tag(no, 'red');
}
function okTag(value, yes = '正常', no = '异常') {
  return value ? tag(yes, 'green') : tag(no, 'red');
}

function toggleHtml(action, enabled, title) {
  return `<button class="toggle-switch" role="switch" aria-checked="${enabled ? 'true' : 'false'}" data-act="${action}" title="${title}"><span class="toggle-knob"></span></button>`;
}

function unknownToggle() {
  return `${tag('状态未知', 'muted')} <button class="toggle-switch" disabled aria-checked="false"><span class="toggle-knob"></span></button>`;
}

export default {
  injectEnabled: null,   // true/false 已知；null = 读取失败
  recentChatEnabled: null,
  recentChatLimit: null, // 1-100 已知；null = 读取失败
  timestampEnabled: null,
  toggling: false,
  togglingRecent: false,
  togglingTimestamp: false,
  savingLimit: false,

  async mount(root) {
    this.root = root;
    root.innerHTML = loading();
    delegate(root, {
      'toggle-eventide': (el) => this.toggleEventide(el),
      'toggle-recent-chat': (el) => this.toggleRecentChat(el),
      'toggle-timestamp': (el) => this.toggleTimestamp(el),
      'save-recent-limit': () => this.saveRecentLimit(),
    });
    try {
      const [health, status, continuity, eventideSettings, contextSettings] = await Promise.all([
        gw('/health'),
        gw('/status'),
        gw('/admin/api/memory-continuity/status'),
        // 开关读取失败不拖垮整页：降级为「状态未知」
        gw('/admin/api/eventide/settings').catch(() => null),
        gw('/admin/api/context/settings').catch(() => null),
      ]);
      this.injectEnabled = eventideSettings ? eventideSettings.inject_enabled === true : null;
      this.recentChatEnabled = contextSettings ? contextSettings.recent_chat_enabled === true : null;
      this.recentChatLimit = contextSettings && Number.isInteger(contextSettings.recent_chat_limit)
        ? contextSettings.recent_chat_limit
        : null;
      this.timestampEnabled = contextSettings ? contextSettings.timestamp_enabled === true : null;
      const supabase = status.supabase || {};
      const lastDigest = status.last_digest_run;
      const uptimeMin = Math.floor((health.uptime || 0) / 60);
      const uptimeText = uptimeMin >= 60 ? `${Math.floor(uptimeMin / 60)} 小时 ${uptimeMin % 60} 分` : `${uptimeMin} 分钟`;
      const eventideControl = eventideSettings
        ? toggleHtml('toggle-eventide', this.injectEnabled, '切换身体状态注入')
        : unknownToggle();
      const recentChatControl = contextSettings
        ? toggleHtml('toggle-recent-chat', this.recentChatEnabled, '切换近期对话注入')
        : unknownToggle();
      const timestampControl = contextSettings
        ? toggleHtml('toggle-timestamp', this.timestampEnabled, '切换时间戳注入')
        : unknownToggle();
      const limitKnown = contextSettings && this.recentChatLimit !== null;
      // 这个标签保存条数后要原地改文案，不能直接用 tag()（拿不到元素），
      // 这里手写同构 span 并带上 id，textContent 更新不会破坏 tag 结构。
      const limitTag = `<span class="tag ${limitKnown ? 'tag-green' : 'tag-muted'}" id="recent-limit-tag">${limitKnown ? `当前 ${this.recentChatLimit} 条` : '状态未知'}</span>`;
      const limitControl = `${limitTag}
        <input type="number" id="recent-limit-input" min="1" max="100" step="1" ${limitKnown ? `value="${this.recentChatLimit}"` : 'disabled'} style="width:76px" aria-label="近期对话注入条数">
        <button class="btn btn-secondary btn-sm" data-act="save-recent-limit" ${limitKnown ? '' : 'disabled'}>保存</button>`;
      root.innerHTML = `
        <div class="grid grid-2">
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('gear')}网关运行状态</div></div>
            <div class="kv"><span class="k">健康状态</span><span class="v">${okTag(health.status === 'ok')}</span></div>
            <div class="kv"><span class="k">阶段</span><span class="v">${esc(health.phase || '-')}</span></div>
            <div class="kv"><span class="k">运行时长</span><span class="v">${esc(uptimeText)}</span></div>
            <div class="kv"><span class="k">后台任务</span><span class="v">${esc(status.bg_tasks ?? '-')} 个</span></div>
            <div class="kv"><span class="k">记忆调度器</span><span class="v">${status.daily_running ? tag('运行中', 'green') : tag('已停止', 'red')}</span></div>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('message')}模型与上游</div></div>
            <div class="kv"><span class="k">聊天上游 API</span><span class="v mono text-sm">${esc(status.upstream_base_url || '-')}</span></div>
            <div class="kv"><span class="k">聊天模型</span><span class="v mono text-sm">${esc(status.upstream_model || '-')}</span></div>
            <div class="kv"><span class="k">连续感模型</span><span class="v mono text-sm">${esc(continuity.analysis_model || continuity.continuity_model || '-')}</span></div>
            <div class="kv"><span class="k">连续感模型状态</span><span class="v">${boolTag(continuity.analysis_configured)}</span></div>
            <div class="kv"><span class="k">Embedding</span><span class="v">${tag('暂未接入', 'muted')}</span></div>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('layers')}Supabase 连接状态</div></div>
            <div class="kv"><span class="k">数据库访问</span><span class="v">${okTag(supabase.access_ok)}</span></div>
            <div class="kv"><span class="k">密钥模式</span><span class="v mono text-sm">${esc(supabase.key_mode || 'unknown')}</span></div>
            <div class="kv"><span class="k">提权密钥</span><span class="v">${supabase.elevated_active ? tag('启用', 'green') : tag('未启用', 'red')}</span></div>
            <div class="kv"><span class="k">安全后端访问</span><span class="v">${status.rls_ready ? tag('就绪', 'green') : tag('未就绪', 'red')}</span></div>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('check')}插件与密钥（仅显示是否配置，不显示任何密钥值）</div></div>
            <div class="kv"><span class="k">记忆插件</span><span class="v">${boolTag(status.memory_plugin_configured)}</span></div>
            <div class="kv"><span class="k">MCP 服务</span><span class="v">${boolTag(status.memory_mcp_configured)}</span></div>
            <div class="kv"><span class="k">待办插件</span><span class="v">${boolTag(status.todo_plugin_configured)}</span></div>
            <div class="kv"><span class="k">更换 API 配置</span><span class="v">
              <button class="btn btn-secondary btn-sm is-disabled" disabled title="暂未接入">更换配置</button>
              <span class="disabled-note" style="margin-left:8px">暂未接入</span>
            </span></div>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('clock')}最近一次总结</div></div>
            <div class="kv"><span class="k">上次总结运行</span><span class="v">${lastDigest ? `#${esc(lastDigest.id ?? '-')} ${esc(lastDigest.status || '')}` : '本进程启动以来尚未运行'}</span></div>
            <div class="kv"><span class="k">上次热度衰减</span><span class="v">${esc(status.last_heat_decay_date || '从未执行')}</span></div>
            <div class="kv"><span class="k">连续感运行状态</span><span class="v">${esc(continuity.status || '-')}</span></div>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('flame')}功能开关</div></div>
            <div class="kv"><span class="k">身体状态注入</span><span class="v">${eventideControl}</span></div>
            <p class="muted text-sm" style="margin:6px 0 0">关闭后聊天提示词不再注入 Eventide 身体状态卡，状态模拟暂停并冻结；重新开启后按时间分段追赶。</p>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('inbox')}上下文注入</div></div>
            <div class="kv"><span class="k">近期对话注入</span><span class="v">${recentChatControl}</span></div>
            <div class="kv"><span class="k">注入条数</span><span class="v">${limitControl}</span></div>
            <div class="kv"><span class="k">时间戳注入</span><span class="v">${timestampControl}</span></div>
            <p class="muted text-sm" style="margin:6px 0 0">近期对话每轮从数据库取最近 N 条聊天记录（user/assistant 各算一条）注入补充上下文，与客户端本次发送的历史无关；时间戳为每轮请求附上当前北京时间。改动保存后立即对下一次聊天生效。</p>
          </div>
        </div>
        <p class="muted text-sm mt16">本页全部信息来自网关真实接口（/health、/status、连续感状态、Eventide 设置、上下文注入设置）。API Key 只显示“已配置 / 未配置”，密钥值不出现在前端。</p>`;
    } catch (error) {
      root.innerHTML = errorBlock(`配置状态读取失败：${esc(error.message)}`);
    }
  },

  async toggleEventide(btn) {
    if (this.injectEnabled === null || this.toggling) return;
    this.toggling = true;
    btn.disabled = true;
    const next = !this.injectEnabled;
    btn.setAttribute('aria-checked', String(next));
    try {
      await gw('/admin/api/eventide/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ inject_enabled: next }),
      });
      this.injectEnabled = next;
      toast(next ? '已开启身体状态注入' : '已关闭身体状态注入，模拟已暂停');
    } catch (error) {
      btn.setAttribute('aria-checked', String(this.injectEnabled));
      toast(`开关保存失败：${error.message}`, 'err');
    } finally {
      btn.disabled = false;
      this.toggling = false;
    }
  },

  async putContextSetting(payload) {
    return gw('/admin/api/context/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },

  async toggleRecentChat(btn) {
    if (this.recentChatEnabled === null || this.togglingRecent) return;
    this.togglingRecent = true;
    btn.disabled = true;
    const next = !this.recentChatEnabled;
    btn.setAttribute('aria-checked', String(next));
    try {
      await this.putContextSetting({ recent_chat_enabled: next });
      this.recentChatEnabled = next;
      toast(next ? '已开启近期对话注入' : '已关闭近期对话注入');
    } catch (error) {
      btn.setAttribute('aria-checked', String(this.recentChatEnabled));
      toast(`开关保存失败：${error.message}`, 'err');
    } finally {
      btn.disabled = false;
      this.togglingRecent = false;
    }
  },

  async toggleTimestamp(btn) {
    if (this.timestampEnabled === null || this.togglingTimestamp) return;
    this.togglingTimestamp = true;
    btn.disabled = true;
    const next = !this.timestampEnabled;
    btn.setAttribute('aria-checked', String(next));
    try {
      await this.putContextSetting({ timestamp_enabled: next });
      this.timestampEnabled = next;
      toast(next ? '已开启时间戳注入' : '已关闭时间戳注入');
    } catch (error) {
      btn.setAttribute('aria-checked', String(this.timestampEnabled));
      toast(`开关保存失败：${error.message}`, 'err');
    } finally {
      btn.disabled = false;
      this.togglingTimestamp = false;
    }
  },

  async saveRecentLimit() {
    const input = this.root.querySelector('#recent-limit-input');
    if (!input || this.savingLimit) return;
    // 本地先拦一层：非空整数才发请求，明显非法的输入不值得一次往返。
    const raw = String(input.value ?? '').trim();
    const value = Number(raw);
    if (!raw || !Number.isInteger(value) || value < 1 || value > 100) {
      toast('注入条数需为 1–100 的整数', 'err');
      return;
    }
    this.savingLimit = true;
    const button = this.root.querySelector('[data-act="save-recent-limit"]');
    if (button) button.disabled = true;
    try {
      await this.putContextSetting({ recent_chat_limit: value });
      this.recentChatLimit = value;
      // 标签是 mount 时拼进 innerHTML 的静态节点，原地同步文案，
      // 否则页面上会一直显示旧条数直到重新进页。
      const limitTag = this.root.querySelector('#recent-limit-tag');
      if (limitTag) limitTag.textContent = `当前 ${value} 条`;
      toast(`已保存注入条数：${value}`);
    } catch (error) {
      toast(`注入条数保存失败：${error.message}`, 'err');
    } finally {
      if (button) button.disabled = false;
      this.savingLimit = false;
    }
  },
};
