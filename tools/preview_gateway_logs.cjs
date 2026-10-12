// Local preview only: synthetic API + the unchanged production admin assets.
// tools/ is excluded from Docker. No .env, database, credentials or upstream calls.
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { once } = require('node:events');

const adminRoot = path.resolve(__dirname, '../admin');
const TOKEN = 'local-log-preview';
const specs = [
  ['ERROR', 'chat', '聊天上游暂时不可用，转发请求失败', 'upstream_http_error', 'HTTPStatusError', 18, 'chat', 'api.example.com', 'failed'],
  ['WARNING', 'memory', '连续感总结暂未完成，已保留原记忆', 'continuity_timeout', 'ReadTimeout', 7, 'continuity', 'continuity.example.com', 'degraded'],
  ['ERROR', 'planning', '待办排程保存失败，请稍后重试', 'planning_save_failed', 'APIError', 5, '', '', 'failed'],
  ['CRITICAL', 'database', '数据库连接中断，写入服务暂时不可用', 'database_unavailable', 'ConnectError', 3, 'database', 'database.example.com', 'failed'],
  ['WARNING', 'memo', '备忘录已保存，后续同步暂未完成', 'memo_side_effect_failed', 'ReadTimeout', 2, '', '', 'committed_side_effect_failed'],
  ['ERROR', 'memory', '反刍模型输出格式不正确，未写入记忆', 'rumination_invalid_output', 'ValueError', 4, 'rumination', 'rumination.example.com', 'failed'],
  ['WARNING', 'context', '应用设置读取失败，当前请求已按默认设置继续', 'app_setting_read_failed', 'APIError', 1, '', '', 'degraded'],
  ['ERROR', 'todo', '待办插件请求处理失败', 'todo_request_failed', 'APIError', 2, '', '', 'failed'],
];
const now = Date.now();
const groups = specs.map((s, i) => ({
  fingerprint: (i + 1).toString(16).padStart(16, '0'), level: s[0], module: s[1],
  message_zh: s[2], error_code: s[3], exception_type: s[4], event_count: s[5],
  credential_role: s[6], provider: s[7], outcome: s[8], operation: `${s[1]}_request`,
  first_seen_at: new Date(now - (75 + i * 12) * 60000).toISOString(),
  last_seen_at: new Date(now - (i * 7 + 1) * 60000).toISOString(),
  logger_name: `gateway.${s[1]}`, source: `${s[1]}.py:handle_request:${120 + i * 17}`,
  key_ref: s[6] ? `kh1-${(i + 1).toString(16).repeat(32)}` : '',
  instance_id: 'local-preview', release_id: 'preview-only',
}));
let enabled = false;
function json(response, body, status = 200) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(body));
}
function filtered(params) {
  return groups.filter(g => {
    if (params.get('level') && g.level !== params.get('level')) return false;
    if (params.get('module') && g.module !== params.get('module')) return false;
    if (params.get('key_ref') && g.key_ref !== params.get('key_ref')) return false;
    if (params.get('start') && Date.parse(g.last_seen_at) < Date.parse(params.get('start'))) return false;
    if (params.get('end') && Date.parse(g.first_seen_at) > Date.parse(params.get('end'))) return false;
    const search = (params.get('search') || '').toLowerCase();
    return !search || JSON.stringify(g).toLowerCase().includes(search);
  });
}
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    if (url.pathname === '/health') return json(response, { status: 'ok', phase: '本地测试预览', uptime: 7200 });
    if (url.pathname === '/status' || url.pathname.startsWith('/admin/api/')) {
      if (request.headers.authorization !== `Bearer ${TOKEN}`) return json(response, { error: '请使用本地预览令牌' }, 401);
      if (url.pathname === '/status') return json(response, {
        bg_tasks: 0, daily_running: false, upstream_base_url: 'https://api.example.com/v1',
        upstream_model: 'preview-chat-model', supabase: { access_ok: true, key_mode: '测试数据', elevated_active: false },
        rls_ready: true, last_heat_decay_date: '本地预览',
      });
      if (url.pathname === '/admin/api/logs/settings') {
        if (request.method === 'PUT') {
          const chunks = [];
          for await (const chunk of request) {
            chunks.push(chunk);
            if (chunks.reduce((n, b) => n + b.length, 0) > 1024) return json(response, { error: '请求过大' }, 413);
          }
          let payload;
          try { payload = JSON.parse(Buffer.concat(chunks).toString()); }
          catch (error) { return json(response, { error: '请求格式不正确' }, 400); }
          if (typeof payload.enabled !== 'boolean') return json(response, { error: 'enabled 必须是布尔值' }, 400);
          enabled = payload.enabled;
        }
        return json(response, { enabled, writable: true, source: 'preview', default_enabled: false });
      }
      if (url.pathname === '/admin/api/logs/meta') {
        const rows = filtered(url.searchParams);
        const modules = [...new Set(rows.map(g => g.module))].map(module => ({ module, count: rows.filter(g => g.module === module).reduce((n, g) => n + g.event_count, 0) }));
        return json(response, {
          modules, keys: rows.filter(g => g.key_ref).map(g => ({ key_ref: g.key_ref, credential_role: g.credential_role, provider: g.provider, count: g.event_count })),
          total_events: rows.reduce((n, g) => n + g.event_count, 0),
          collection: { enabled, handler_installed: enabled, key_ref_ready: true, queue_depth: 0, written_total: 42,
            dropped_total: 0, retention_days: 30, consecutive_failures: 0, spool: { enabled: false } },
        });
      }
      if (url.pathname === '/admin/api/logs') return json(response, {
        groups: filtered(url.searchParams), has_more: false, next_cursor: null,
        snapshot: new Date(now).toISOString(), visible_snapshot: '100:110:101',
      });
      if (url.pathname === '/admin/api/logs/group') {
        const group = groups.find(g => g.fingerprint === url.searchParams.get('fingerprint'));
        if (!group) return json(response, { error: '测试记录不存在' }, 404);
        const samples = [0, 1].slice(0, Math.min(group.event_count, 2)).map((_, i) => ({
          ...group, event_id: `00000000-0000-0000-0000-${(i + 1).toString().padStart(12, '0')}`,
          occurred_at: group.last_seen_at, request_id: `preview-${group.module}-${i + 1}`,
          route: `/preview/${group.module}`, method: 'POST', http_status: group.level === 'WARNING' ? 200 : 502,
          upstream_status: group.module === 'chat' ? 503 : null, truncated_fields: [],
          message_raw: `[TEST DATA] ${group.error_code}: synthetic upstream response.\nAuthorization: Bearer [REDACTED]\nThis record was generated locally for UI preview.`,
          stack_trace: group.level === 'WARNING' ? '' : `Traceback (most recent call last):\n  File "gateway/${group.module}.py", line 120, in handle_request\n    response = await client.post(endpoint)\n  File "httpx/_client.py", line 1859, in post\n    return await self.request(method, url)\n${group.exception_type}: synthetic preview error; no real request was sent`,
        }));
        return json(response, { group, samples });
      }
      if (url.pathname === '/admin/api/eventide/settings') return json(response, { inject_enabled: false });
      if (url.pathname === '/admin/api/context/settings') return json(response, { recent_chat_enabled: true, recent_chat_limit: 10, timestamp_enabled: true });
      if (url.pathname === '/admin/api/memory-continuity/status') return json(response, { analysis_model: 'preview-continuity', analysis_configured: true, status: '未运行' });
      if (url.pathname === '/admin/api/planning/cycle') return json(response, { refresh_boundary_time: '06:00', daily_refresh_enabled: true, auto_recompute_enabled: true, auto_recompute_wait_minutes: 5, pending_boundary: null });
      if (url.pathname === '/admin/api/config/credentials') return json(response, { key_ref_ready: true, credentials: groups.filter(g => g.key_ref).map((g, i) => ({
        credential_id: `preview-${i}`, role: g.credential_role, configured: false, source: '本地测试数据',
        provider: g.provider, key_ref: g.key_ref, label: `K-${g.key_ref.slice(4, 12)}`, revealable: false, fallback: false,
      })) });
      return json(response, { data: [] });
    }
    const pathname = url.pathname === '/' || url.pathname === '/admin/' ? '/admin/index.html' : url.pathname;
    if (!pathname.startsWith('/admin/')) return json(response, { error: '仅提供管理页预览' }, 404);
    const file = path.resolve(adminRoot, '.' + decodeURIComponent(pathname.slice(6)));
    if (!file.startsWith(adminRoot + path.sep)) return json(response, { error: '路径不允许' }, 404);
    let body = await fs.readFile(file);
    if (file === path.join(adminRoot, 'index.html')) {
      body = Buffer.from(body.toString().replace('<head>', `<head><script>localStorage.setItem('qi-token', '${TOKEN}');</script>`).replace('</body>',
        '<div style="position:fixed;right:18px;bottom:14px;z-index:90;padding:6px 12px;background:var(--bg-card,#f9f2e5);border:1px solid var(--line,#bca477);border-radius:6px;font:12px sans-serif;pointer-events:none">本地预览 · 测试数据</div></body>'));
    }
    const types = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg' };
    response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    response.end(body);
  } catch (error) {
    if (!response.headersSent) return json(response, { error: '预览资源读取失败' }, 404);
    response.end();
  }
});
server.on('error', error => { process.stderr.write(`Preview server failed: ${error.message}\n`); process.exitCode = 1; });
async function start() {
  const port = Number(process.argv[2] || 8766);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('预览端口必须在 1024–65535 之间');
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');
  process.stdout.write(`Local synthetic preview: http://127.0.0.1:${port}/admin/index.html#/logs\n`);
}
if (require.main === module) start().catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
module.exports = { server };
