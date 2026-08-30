// pages/config.js - 配置：真实状态展示，未接入功能仅占位
import { gw, esc } from '../api.js?v=20260830-retro1';
import { loading, errorBlock, tag, delegate, icon } from '../ui.js?v=20260830-retro1';

function boolTag(value, yes = '已配置', no = '未配置') {
  return value ? tag(yes, 'green') : tag(no, 'red');
}
function okTag(value, yes = '正常', no = '异常') {
  return value ? tag(yes, 'green') : tag(no, 'red');
}

export default {
  async mount(root) {
    root.innerHTML = loading();
    delegate(root, {});
    try {
      const [health, status, continuity] = await Promise.all([
        gw('/health'),
        gw('/status'),
        gw('/admin/api/memory-continuity/status'),
      ]);
      const supabase = status.supabase || {};
      const lastDigest = status.last_digest_run;
      const uptimeMin = Math.floor((health.uptime || 0) / 60);
      const uptimeText = uptimeMin >= 60 ? `${Math.floor(uptimeMin / 60)} 小时 ${uptimeMin % 60} 分` : `${uptimeMin} 分钟`;
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
        </div>
        <p class="muted text-sm mt16">本页全部信息来自网关真实接口（/health、/status、连续感状态）。API Key 只显示“已配置 / 未配置”，密钥值不出现在前端。</p>`;
    } catch (error) {
      root.innerHTML = errorBlock(`配置状态读取失败：${esc(error.message)}`);
    }
  },
};
