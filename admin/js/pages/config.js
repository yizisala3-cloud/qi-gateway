// pages/config.js - 配置：真实状态展示 + 功能开关
import { gw, esc } from '../api.js?v=20261016-planning-optimization';
import { loading, errorBlock, tag, toast, delegate, icon } from '../ui.js?v=20261016-planning-optimization';
import { openCycleSettings } from '../lib/cycle_settings.js?v=20261016-planning-optimization';
import { createRetroTimeField } from '../lib/retro_time.js?v=20261016-planning-optimization';

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

function toggleHtml(action, enabled, label) {
  return `<button class="toggle-switch" role="switch" aria-label="${esc(label)}" aria-checked="${enabled ? 'true' : 'false'}" data-act="${action}" data-tooltip="${esc(label)}"><span class="toggle-knob"></span></button>`;
}

function unknownToggle(label) {
  return `${tag('状态未知', 'muted')} <button class="toggle-switch" role="switch" aria-label="${esc(label)}" disabled aria-checked="false"><span class="toggle-knob"></span></button><span class="control-note">读取失败，暂不可修改</span>`;
}

export default {
  injectEnabled: null,   // true/false 已知；null = 读取失败
  recentChatEnabled: null,
  recentChatLimit: null, // 1-100 已知；null = 读取失败
  timestampEnabled: null,
  logCollectionEnabled: null,
  logCollectionWritable: false,
  savingLogCollection: false,
  logMountToken: null,
  toggling: false,
  togglingRecent: false,
  togglingTimestamp: false,
  savingLimit: false,
  cycle: null,
  cycleIsDemo: false,
  togglingCycle: false,
  savingCycleBoundary: false,
  savingCycleWait: false,
  // TASK-01：密钥按项查看状态。初始只取元数据（无任何真实值）；点击
  // 「显示」后按项请求真实值；隐藏 / 离开页面清除并取消在途请求。
  credentials: [],
  keyRefReady: null,
  credSessions: {},

  async mount(root) {
    this.root = root;
    this.logMountToken = Symbol('config-log-settings');
    root.innerHTML = loading();
    // 重新进入页面默认全部隐藏（不保留「显示状态」）；旧会话取消。
    for (const credentialId of Object.keys(this.credSessions)) {
      this._closeCredentialSession(credentialId);
    }
    this.credSessions = {};
    delegate(root, {
      'toggle-eventide': (el) => this.toggleEventide(el),
      'toggle-recent-chat': (el) => this.toggleRecentChat(el),
      'toggle-timestamp': (el) => this.toggleTimestamp(el),
      'toggle-log-collection': (el) => this.toggleLogCollection(el),
      'save-recent-limit': () => this.saveRecentLimit(),
      'toggle-daily-refresh': (el) => this.toggleCycleSwitch(el, 'daily_refresh_enabled', '每日待办自动刷新'),
      'toggle-auto-recompute': (el) => this.toggleCycleSwitch(el, 'auto_recompute_enabled', '自动重算'),
      'save-cycle-boundary': () => this.saveCycleBoundary(),
      'save-cycle-wait': () => this.saveCycleWait(),
      'toggle-divider': (el) => this.toggleDivider(el),
      'cred-reveal': (el) => this.revealCredential(el.dataset.cred),
      'cred-hide': (el) => this.hideCredential(el.dataset.cred),
    });
    try {
      const [health, status, continuity, eventideSettings, contextSettings, cycleSettings, credentials, logSettings] = await Promise.all([
        gw('/health'),
        gw('/status'),
        // 连续感状态读取失败不拖垮整页：降级为「状态未知」
        gw('/admin/api/memory-continuity/status').catch(() => null),
        // 开关读取失败不拖垮整页：降级为「状态未知」
        gw('/admin/api/eventide/settings').catch(() => null),
        gw('/admin/api/context/settings').catch(() => null),
        // 规划周期读取失败降级为示例数据（数据库未连接时的本地预览）
        gw('/admin/api/planning/cycle').catch(() => null),
        // TASK-01：密钥元数据（只含用途/来源/标识，绝无真实值）；失败降级
        gw('/admin/api/config/credentials').catch(() => null),
        gw('/admin/api/logs/settings').catch(() => null),
      ]);
      const cycle = cycleSettings || DEMO_CYCLE;
      const cycleIsDemo = !cycleSettings;
      this.cycle = cycle;
      this.cycleIsDemo = cycleIsDemo;
      this.credentials = (credentials && credentials.credentials) || [];
      this.keyRefReady = credentials ? credentials.key_ref_ready === true : null;
      this.injectEnabled = eventideSettings ? eventideSettings.inject_enabled === true : null;
      this.recentChatEnabled = contextSettings ? contextSettings.recent_chat_enabled === true : null;
      this.recentChatLimit = contextSettings && Number.isInteger(contextSettings.recent_chat_limit)
        ? contextSettings.recent_chat_limit
        : null;
      this.timestampEnabled = contextSettings ? contextSettings.timestamp_enabled === true : null;
      this.logCollectionEnabled = typeof logSettings?.enabled === 'boolean' ? logSettings.enabled : null;
      this.logCollectionWritable = logSettings?.writable === true;
      const supabase = status.supabase || {};
      const lastDigest = status.last_digest_run;
      const uptimeMin = Math.floor((health.uptime || 0) / 60);
      const uptimeText = uptimeMin >= 60 ? `${Math.floor(uptimeMin / 60)} 小时 ${uptimeMin % 60} 分` : `${uptimeMin} 分钟`;
      const eventideControl = eventideSettings
        ? toggleHtml('toggle-eventide', this.injectEnabled, '切换身体状态注入')
        : unknownToggle('身体状态注入');
      const recentChatControl = contextSettings
        ? toggleHtml('toggle-recent-chat', this.recentChatEnabled, '切换近期对话注入')
        : unknownToggle('近期对话注入');
      const timestampControl = contextSettings
        ? toggleHtml('toggle-timestamp', this.timestampEnabled, '切换时间戳注入')
        : unknownToggle('时间戳注入');
      const limitKnown = contextSettings && this.recentChatLimit !== null;
      // 这个标签保存条数后要原地改文案，不能直接用 tag()（拿不到元素），
      // 这里手写同构 span 并带上 id，textContent 更新不会破坏 tag 结构。
      const limitTag = `<span class="tag ${limitKnown ? 'tag-green' : 'tag-muted'}" id="recent-limit-tag">${limitKnown ? `当前 ${this.recentChatLimit} 条` : '状态未知'}</span>`;
      const limitControl = `${limitTag}
        <input type="number" id="recent-limit-input" min="1" max="100" step="1" ${limitKnown ? `value="${this.recentChatLimit}"` : 'disabled'} style="width:76px" aria-label="近期对话注入条数">
        <button class="btn btn-secondary btn-sm" data-act="save-recent-limit" ${limitKnown ? '' : 'disabled'}>保存</button>`;
      // 分隔线三种样式（与 app.js 的 DIVIDER_VARIANTS 对应）
      const dividerVariant = ['wisteria', 'lily3', 'straight'].includes(localStorage.getItem('qi-divider'))
        ? localStorage.getItem('qi-divider')
        : 'wisteria';
      const dividerName = { wisteria: '紫藤花枝', lily3: '铃兰花枝', straight: '直线' }[dividerVariant];
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
            <div class="card-head"><div class="card-title">${icon('key')}模型与插件密钥（默认隐藏，点击后按项显示）</div></div>
            <div id="cred-list">${this.renderCredentialRows()}</div>
            ${this.keyRefReady === false ? '<p class="muted text-sm" style="margin:6px 0 0;color:var(--amber)">密钥标识未就绪（GATEWAY_KEY_REF_SECRET 缺失或短于 32 字节）：日志暂不能按密钥区分故障，密钥值查看不受影响。</p>' : ''}
            <p class="muted text-sm" style="margin:6px 0 0" data-help-note="密钥查看">密钥值只在点击「显示」后按项从服务端获取（响应不缓存）；隐藏或离开本页即清除，不写入浏览器存储。标识 K-… 与日志页一致，用于按密钥区分故障。</p>
            <div class="kv" style="margin-top:8px"><span class="k">更换 API 配置</span><span class="v">
              <button class="btn btn-secondary btn-sm is-disabled" disabled aria-describedby="config-change-note">更换配置</button>
              <span class="control-note" id="config-change-note">暂未接入</span>
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
            <p class="muted text-sm" style="margin:6px 0 0" data-help-note="身体状态开关">关闭后聊天提示词不再注入 Eventide 身体状态卡，状态模拟暂停并冻结；重新开启后按时间分段追赶。</p>
            <div class="kv" style="margin-top:16px"><span class="k">日志收集</span><span class="v">
              <span id="log-collection-tag" class="tag ${this.logCollectionEnabled === true ? 'tag-green' : 'tag-muted'}">${this.logCollectionEnabled === null ? '状态未知' : this.logCollectionEnabled ? '已开启' : '已关闭'}</span>
              <button class="toggle-switch" role="switch" aria-label="日志收集" aria-checked="${this.logCollectionEnabled === true}" data-act="toggle-log-collection" ${this.logCollectionWritable && this.logCollectionEnabled !== null ? '' : 'disabled'}><span class="toggle-knob"></span></button>
            </span></div>
            <p class="muted text-sm" style="margin:6px 0 0" data-help-note="日志收集">默认关闭。关闭后停止收集新的报错，已保存的历史日志仍可查看；设置保存后生效，重启后保留。</p>
            <p id="log-collection-note" class="muted text-sm" style="margin:6px 0 0">${this.logCollectionWritable ? '' : '暂不能保存开关，请检查数据库连接或重新进入本页。'}</p>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('inbox')}上下文注入</div></div>
            <div class="kv"><span class="k">注入条数</span><span class="v">${limitControl}</span></div>
            <div class="kv"><span class="k">近期对话注入</span><span class="v">${recentChatControl}</span></div>
            <div class="kv"><span class="k">时间戳注入</span><span class="v">${timestampControl}</span></div>
            <p class="muted text-sm" style="margin:6px 0 0" data-help-note="上下文注入">近期对话每轮从数据库取最近 N 条聊天记录（user/assistant 各算一条）注入补充上下文，与客户端本次发送的历史无关；时间戳为每轮请求附上当前北京时间。改动保存后立即对下一次聊天生效。</p>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('calendar')}规划周期</div></div>
            <div class="kv"><span class="k">每日刷新时间</span><span class="v">
              <span class="retro-time retro-time-inline" aria-label="每日刷新时间（北京时间）" data-retro-for="cfg-cycle-boundary" data-retro-mode="time" data-retro-align="right" data-retro-value="${esc(cycle.refresh_boundary_time || '')}"></span>
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
            <p class="muted text-sm" style="margin:6px 0 0" data-help-note="规划周期">新的刷新时间从下一规划周期开始生效，当前周期保持不变；若有待办跨越新刷新时间，保存时会列出供逐个调整。</p>
          </div>
          <div class="card">
            <div class="card-head"><div class="card-title">${icon('feather')}页眉花饰</div></div>
            <div class="kv"><span class="k">分隔线样式</span><span class="v">
              <span class="tag tag-green" id="divider-tag">${dividerName}</span>
              <button class="btn btn-secondary btn-sm" data-act="toggle-divider" style="margin-left:8px">切换样式</button>
            </span></div>
            <p class="muted text-sm" style="margin:6px 0 0" data-help-note="页眉花饰">切换每页标题下的分隔线：紫藤花枝 / 铃兰花枝 / 直线。点一下立即生效，选择保存在本机浏览器。</p>
          </div>
        </div>
        <p class="muted text-sm mt16" data-help-note="配置信息来源">本页全部信息来自网关真实接口（/health、/status、连续感状态、Eventide 设置、上下文注入设置、日志收集设置、规划周期、密钥元数据）。密钥默认隐藏，点击某一项「显示」后才从服务端按项获取真实值；隐藏或离开本页即清除。</p>`;
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

  unmount() {
    this.logMountToken = null;
    // TASK-01（L27）：离开页面清除全部可见密钥与页面状态，取消在途查看
    // 请求；晚到响应不得重新显示。真实值只存在于当前页面的必要状态中。
    for (const credentialId of Object.keys(this.credSessions)) {
      this._closeCredentialSession(credentialId);
    }
    this.credSessions = {};
  },

  /* ---------- 密钥按项查看（TASK-01，§5.3/§9.4） ---------- */

  renderCredentialRows() {
    if (!this.credentials.length) {
      return '<p class="muted text-sm" style="margin:6px 0 0">密钥元数据读取失败，暂无法显示配置状态。</p>';
    }
    return this.credentials.map((c) => {
      const state = this.credSessions[c.credential_id] || {};
      const label = c.label ? `<span class="tag tag-slate mono">${esc(c.label)}</span>` : '';
      const fallback = c.fallback ? ` <span class="tag tag-amber">回退</span>` : '';
      const head = `<span class="k">${esc(c.role)}${fallback}</span>`;
      const meta = `${esc(c.source)}${c.provider ? ` · ${esc(c.provider)}` : ''}`;
      if (!c.configured) {
        return `<div class="kv"><span class="k">${esc(c.role)}</span><span class="v">${tag('未配置', 'red')} <span class="muted text-sm mono">${esc(meta)}</span></span></div>`;
      }
      if (state.visible && state.value !== undefined) {
        return `<div class="kv"><span class="k">${head}</span><span class="v mono text-sm" style="word-break:break-all">
          <span data-cred-value="${esc(c.credential_id)}"></span>
          <button class="btn btn-secondary btn-sm" data-act="cred-hide" data-cred="${esc(c.credential_id)}" style="margin-left:8px">隐藏</button>
        </span></div>
        <div class="kv"><span class="k">来源 / 标识</span><span class="v muted text-sm mono">${esc(meta)}${label ? ` · ${label}` : ''}</span></div>`;
      }
      return `<div class="kv"><span class="k">${head}</span><span class="v">
        ${label} <span class="mono" data-cred-mask="${esc(c.credential_id)}">••••••••</span>
        <button class="btn btn-secondary btn-sm" data-act="cred-reveal" data-cred="${esc(c.credential_id)}" style="margin-left:8px"${state.loading ? ' disabled' : ''}>${state.loading ? '获取中…' : '显示'}</button>
      </span></div>
      <div class="kv"><span class="k">来源 / 标识</span><span class="v muted text-sm mono">${esc(meta)}${label ? ` · ${label}` : ''}</span></div>`;
    }).join('');
  },

  _closeCredentialSession(credentialId) {
    const session = this.credSessions[credentialId];
    if (!session) return;
    if (session.controller) { try { session.controller.abort(); } catch { /* 已结束 */ } }
    session.done = true; // 晚到响应失效
    delete this.credSessions[credentialId];
  },

  async revealCredential(credentialId) {
    if (!credentialId || this.credSessions[credentialId]) return;
    const controller = new AbortController();
    const session = { controller, done: false, loading: true };
    this.credSessions[credentialId] = session;
    this._refreshCredentialCard(credentialId);
    try {
      const data = await gw(`/admin/api/config/credentials/${encodeURIComponent(credentialId)}/reveal`, {
        method: 'POST',
        signal: controller.signal,
      });
      if (session.done) return; // 已隐藏 / 已离开：晚到响应不显示
      session.loading = false;
      session.visible = true;
      // 只存当前展示所需；元数据标识同步为服务端返回的实际值（配置变化
      // 后展示匹配实际返回标识，不把新密钥挂到旧标签）。
      session.value = data.value;
      const meta = this.credentials.find((c) => c.credential_id === credentialId);
      if (meta && data.key_ref) {
        meta.key_ref = data.key_ref;
        meta.label = data.label || meta.label;
        meta.source = data.source || meta.source;
      }
      this._refreshCredentialCard(credentialId);
      // 真实值用文本节点写入（恶意字符不可能成为脚本）
      const holder = this.root.querySelector(`[data-cred-value="${CSS.escape(credentialId)}"]`);
      if (holder) holder.textContent = session.value;
    } catch (error) {
      if (session.done || error.name === 'AbortError') return;
      session.loading = false;
      this._closeCredentialSession(credentialId);
      this._refreshCredentialCard(credentialId);
      toast(`密钥查看失败：${error.message}`, 'err');
    }
  },

  hideCredential(credentialId) {
    this._closeCredentialSession(credentialId);
    this._refreshCredentialCard(credentialId);
  },

  _refreshCredentialCard(credentialId) {
    const box = this.root?.querySelector('#cred-list');
    if (!box) return;
    box.innerHTML = this.renderCredentialRows();
    // 已显示项的真实值重新用文本节点写入（重渲染后 span 为空）
    for (const id of Object.keys(this.credSessions)) {
      const session = this.credSessions[id];
      if (session.visible && session.value !== undefined) {
        const holder = this.root.querySelector(`[data-cred-value="${CSS.escape(id)}"]`);
        if (holder) holder.textContent = session.value;
      }
    }
  },

  async toggleLogCollection(btn) {
    if (this.logCollectionEnabled === null || !this.logCollectionWritable || this.savingLogCollection) return;
    const mountToken = this.logMountToken;
    const next = !this.logCollectionEnabled;
    this.savingLogCollection = true;
    btn.disabled = true;
    try {
      const result = await gw('/admin/api/logs/settings', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      });
      if (mountToken !== this.logMountToken) return;
      if (typeof result.enabled !== 'boolean') throw new Error('未收到有效的开关状态');
      this.logCollectionEnabled = result.enabled;
      this.logCollectionWritable = result.writable === true;
      toast(result.enabled ? '已开启日志收集' : '已关闭日志收集，历史日志仍可查看');
    } catch (error) {
      if (mountToken !== this.logMountToken) return;
      // 保存结果不确定时重新读服务端，避免只把按钮回滚而实际已保存。
      try {
        const current = await gw('/admin/api/logs/settings');
        if (mountToken !== this.logMountToken) return;
        this.logCollectionEnabled = typeof current.enabled === 'boolean' ? current.enabled : null;
        this.logCollectionWritable = current.writable === true;
      } catch (readError) {
        if (mountToken !== this.logMountToken) return;
        this.logCollectionEnabled = null;
        this.logCollectionWritable = false;
      }
      toast(`日志收集设置保存未成功确认：${error.message}`, 'err');
    } finally {
      this.savingLogCollection = false;
      if (mountToken === this.logMountToken) {
        btn.setAttribute('aria-checked', String(this.logCollectionEnabled === true));
        btn.disabled = !this.logCollectionWritable || this.logCollectionEnabled === null;
        const label = this.root.querySelector('#log-collection-tag');
        label.textContent = this.logCollectionEnabled === null ? '状态未知' : this.logCollectionEnabled ? '已开启' : '已关闭';
        label.className = `tag ${this.logCollectionEnabled === true ? 'tag-green' : 'tag-muted'}`;
        this.root.querySelector('#log-collection-note').textContent = this.logCollectionWritable ? '' : '暂不能保存开关，请检查数据库连接或重新进入本页。';
      }
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

  // 页眉分隔线切换：紫藤花枝 → 铃兰花枝 → 直线循环，纯本地偏好。
  // 规则元素在 index.html 静态区（.page-head-rule），改 data-variant 即换素材；
  // 启动时 app.js 的 initDivider() 会按 localStorage 恢复上次选择。
  toggleDivider() {
    const order = ['wisteria', 'lily3', 'straight'];
    const names = { wisteria: '紫藤花枝', lily3: '铃兰花枝', straight: '直线' };
    const cur = order.includes(localStorage.getItem('qi-divider')) ? localStorage.getItem('qi-divider') : 'wisteria';
    const next = order[(order.indexOf(cur) + 1) % order.length];
    localStorage.setItem('qi-divider', next);
    const rule = document.querySelector('.page-head-rule');
    if (rule) rule.dataset.variant = next;
    const dividerTag = this.root.querySelector('#divider-tag');
    if (dividerTag) dividerTag.textContent = names[next];
    toast(`已切换为${names[next]}`);
  },
};
