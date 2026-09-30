// pages/config.js - 配置：真实状态展示 + 功能开关
import { gw, esc } from '../api.js?v=20260927-planning10';
import { loading, errorBlock, tag, toast, delegate, icon } from '../ui.js?v=20260927-planning10';
import { openCycleSettings } from '../lib/cycle_settings.js?v=20260930-planning11';
import { createRetroTimeField } from '../lib/retro_time.js?v=20260930-planning11';

// 规划数据库不可用时的示例数据（仅本地预览；保存会因库不可用自然报错）
const DEMO_CYCLE = {
  refresh_boundary_time: '06:00',
  daily_refresh_enabled: true,
  auto_recompute_enabled: true,
  auto_recompute_wait_minutes: 5,
  pending_boundary: null,
};

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
  cycle: null,
  cycleIsDemo: false,
  togglingCycle: false,
  savingCycleBoundary: false,
  savingCycleWait: false,

  async mount(root) {
    this.root = root;
    root.innerHTML = loading();
    delegate(root, {
      'toggle-eventide': (el) => this.toggleEventide(el),
      'toggle-recent-chat': (el) => this.toggleRecentChat(el),
      'toggle-timestamp': (el) => this.toggleTimestamp(el),
      'save-recent-limit': () => this.saveRecentLimit(),
      'toggle-daily-refresh': (el) => this.toggleCycleSwitch(el, 'daily_refresh_enabled', '每日待办自动刷新'),
      'toggle-auto-recompute': (el) => this.toggleCycleSwitch(el, 'auto_recompute_enabled', '自动重算'),
      'save-cycle-boundary': () => this.saveCycleBoundary(),
      'save-cycle-wait': () => this.saveCycleWait(),
    });
    try {
      const [health, status, continuity, eventideSettings, contextSettings, cycleSettings] = await Promise.all([
        gw('/health'),
        gw('/status'),
        // 连续感状态读取失败不拖垮整页：降级为「状态未知」
        gw('/admin/api/memory-continuity/status').catch(() => null),
        // 开关读取失败不拖垮整页：降级为「状态未知」
        gw('/admin/api/eventide/settings').catch(() => null),
        gw('/admin/api/context/settings').catch(() => null),
        // 规划周期读取失败降级为示例数据（数据库未连接时的本地预览）
        gw('/admin/api/planning/cycle').catch(() => null),
      ]);
      const cycle = cycleSettings || DEMO_CYCLE;
      const cycleIsDemo = !cycleSettings;
      this.cycle = cycle;
      this.cycleIsDemo = cycleIsDemo;
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
            <div class="kv"><span class="k">连续感模型</span><span class="v mono text-sm">${esc(continuity?.analysis_model || continuity?.continuity_model || '-')}</span></div>
            <div class="kv"><span class="k">连续感模型状态</span><span class="v">${continuity ? boolTag(continuity.analysis_configured) : tag('状态未知', 'muted')}</span></div>
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
            <div class="kv"><span class="k">连续感运行状态</span><span class="v">${esc(continuity?.status || '-')}</span></div>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('flame')}功能开关</div></div>
            <div class="kv"><span class="k">身体状态注入</span><span class="v">${eventideControl}</span></div>
            <p class="muted text-sm" style="margin:6px 0 0">关闭后聊天提示词不再注入 Eventide 身体状态卡，状态模拟暂停并冻结；重新开启后按时间分段追赶。</p>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('inbox')}上下文注入</div></div>
            <div class="kv"><span class="k">注入条数</span><span class="v">${limitControl}</span></div>
            <div class="kv"><span class="k">近期对话注入</span><span class="v">${recentChatControl}</span></div>
            <div class="kv"><span class="k">时间戳注入</span><span class="v">${timestampControl}</span></div>
            <p class="muted text-sm" style="margin:6px 0 0">近期对话每轮从数据库取最近 N 条聊天记录（user/assistant 各算一条）注入补充上下文，与客户端本次发送的历史无关；时间戳为每轮请求附上当前北京时间。改动保存后立即对下一次聊天生效。</p>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('calendar')}规划周期</div></div>
            <div class="kv"><span class="k">每日刷新时间</span><span class="v">
              <span class="retro-time retro-time-inline" data-retro-for="cfg-cycle-boundary" data-retro-mode="time" data-retro-align="right" data-retro-value="${esc(cycle.refresh_boundary_time || '')}"></span>
              <button class="btn btn-secondary btn-sm" data-act="save-cycle-boundary" style="margin-left:8px">保存</button>
            </span></div>
            <div class="kv"><span class="k">重算等待</span><span class="v">
              <input type="number" id="cycle-wait-input" min="1" max="1440" step="1" value="${esc(cycle.auto_recompute_wait_minutes ?? 5)}" style="width:76px" aria-label="自动重算等待分钟数">
              <button class="btn btn-secondary btn-sm" data-act="save-cycle-wait">保存</button>
            </span></div>
            <div class="kv"><span class="k">每日待办自动刷新</span><span class="v">${toggleHtml('toggle-daily-refresh', cycle.daily_refresh_enabled, '切换每日待办自动刷新')}</span></div>
            <div class="kv"><span class="k">自动重算</span><span class="v">${toggleHtml('toggle-auto-recompute', cycle.auto_recompute_enabled, '切换自动重算')}</span></div>
            <div class="kv"><span class="k">待生效修改</span><span class="v">${cycle.pending_boundary ? tag(`将改为 ${esc(cycle.refresh_boundary_time || '')}`, 'amber') : tag('无', 'muted')}</span></div>
            ${cycleIsDemo ? '<p class="muted text-sm" style="margin:6px 0 0">规划数据库未连接，以上为示例数据，不可修改；连接后自动显示真实值。</p>' : ''}
            <p class="muted text-sm" style="margin:6px 0 0">新的刷新时间从下一规划周期开始生效，当前周期保持不变；若有待办跨越新刷新时间，保存时会列出供逐个调整。</p>
          </div>
        </div>
        <p class="muted text-sm mt16">本页全部信息来自网关真实接口（/health、/status、连续感状态、Eventide 设置、上下文注入设置、规划周期）。API Key 只显示“已配置 / 未配置”，密钥值不出现在前端。</p>`;
    } catch (error) {
      root.innerHTML = errorBlock(`配置状态读取失败：${esc(error.message)}`);
    }
    // 规划周期的复古时间字段（卡片表面）
    root.querySelectorAll('.retro-time[data-retro-for]').forEach((host) => {
      createRetroTimeField(host, {
        id: host.dataset.retroFor,
        value: host.dataset.retroValue || '',
        mode: host.dataset.retroMode || 'datetime',
        align: host.dataset.retroAlign || 'left',
      });
    });
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

  /* ---------- 规划周期（控件直接铺在卡片表面） ---------- */

  patchCycle(bodyObj) {
    return gw('/admin/api/planning/cycle', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(bodyObj),
    });
  },

  async toggleCycleSwitch(btn, key, label) {
    if (this.cycleIsDemo) {
      toast('规划数据库未连接，示例数据不可修改', 'err');
      return;
    }
    if (this.togglingCycle) return;
    this.togglingCycle = true;
    btn.disabled = true;
    const next = !this.cycle[key];
    btn.setAttribute('aria-checked', String(next));
    try {
      await this.patchCycle({ [key]: next });
      this.cycle[key] = next;
      toast(next ? `已开启${label}` : `已关闭${label}`);
    } catch (error) {
      btn.setAttribute('aria-checked', String(this.cycle[key]));
      toast(`开关保存失败：${error.message}`, 'err');
    } finally {
      btn.disabled = false;
      this.togglingCycle = false;
    }
  },

  async saveCycleBoundary() {
    if (this.cycleIsDemo) {
      toast('规划数据库未连接，示例数据不可修改', 'err');
      return;
    }
    if (this.savingCycleBoundary) return;
    const boundary = this.root.querySelector('#cfg-cycle-boundary')?.value || '';
    if (!boundary) {
      toast('请先选择刷新时间', 'err');
      return;
    }
    if (boundary === this.cycle.refresh_boundary_time) {
      toast('刷新时间未变化');
      return;
    }
    this.savingCycleBoundary = true;
    const btn = this.root.querySelector('[data-act="save-cycle-boundary"]');
    if (btn) btn.disabled = true;
    try {
      // 与弹窗一致的两阶段语义：先 dry-run（零写入），命中冲突则弹窗内逐个调整
      const dry = await this.patchCycle({ refresh_boundary_time: boundary, dry_run: true });
      if (dry.conflicts?.length) {
        toast(`有 ${dry.conflicts.length} 个待办跨越新刷新时间，请在弹窗中调整其可安排时段`, 'err');
        openCycleSettings({
          onSaved: () => this.mount(this.root),
          initialBoundary: boundary,
        });
        return;
      }
      await this.patchCycle({ refresh_boundary_time: boundary });
      this.cycle.refresh_boundary_time = boundary;
      toast('已保存；新的刷新时间从下一规划周期开始生效，当前周期保持不变');
      this.mount(this.root);  // 待生效修改一行需要重拉
    } catch (error) {
      toast(`保存失败：${error.message}`, 'err');
    } finally {
      this.savingCycleBoundary = false;
      if (btn) btn.disabled = false;
    }
  },

  async saveCycleWait() {
    if (this.cycleIsDemo) {
      toast('规划数据库未连接，示例数据不可修改', 'err');
      return;
    }
    if (this.savingCycleWait) return;
    const value = Number(this.root.querySelector('#cycle-wait-input')?.value);
    if (!Number.isInteger(value) || value < 1 || value > 1440) {
      toast('重算等待需为 1-1440 的整数（分钟）', 'err');
      return;
    }
    if (value === this.cycle.auto_recompute_wait_minutes) {
      toast('重算等待未变化');
      return;
    }
    this.savingCycleWait = true;
    const btn = this.root.querySelector('[data-act="save-cycle-wait"]');
    if (btn) btn.disabled = true;
    try {
      await this.patchCycle({ auto_recompute_wait_minutes: value });
      this.cycle.auto_recompute_wait_minutes = value;
      toast('重算等待已保存');
    } catch (error) {
      toast(`保存失败：${error.message}`, 'err');
    } finally {
      this.savingCycleWait = false;
      if (btn) btn.disabled = false;
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
