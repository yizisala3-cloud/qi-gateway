// Local synthetic dashboard only. No deployment config, QQ or database access.
const http = require('node:http');
const { once } = require('node:events');
const { server: base } = require('./preview_planning.cjs');
const baseHandler = base.listeners('request')[0];
const flags = { enabled: false, proactive_enabled: false };
let mode = 'normal';
function json(res, data, status = 200) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(data));
}
function snapshot() {
  return { ...flags, writable: true, configured: mode !== 'unconfigured',
    proactive_configured: mode !== 'unconfigured', configuration_note: mode === 'unconfigured' ? '请先配置机器人QQ号、主人QQ号和连接令牌。' : '',
    proactive_note: '', running: flags.enabled, connected: false, owner_paused: false,
    silence_until: 0, error: '', source: 'preview' };
}
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://localhost');
    if (!url.pathname.startsWith('/admin/api/qqbot/')) return await baseHandler(req, res);
    if (req.headers.authorization !== 'Bearer local-log-preview') return json(res, { error: 'unauthorized' }, 401);
    if (url.pathname === '/admin/api/qqbot/preview-mode' && req.method === 'PUT') {
      const parts = []; for await (const part of req) parts.push(part);
      const data = JSON.parse(Buffer.concat(parts).toString());
      if (!['normal', 'unconfigured', 'save-error', 'read-error', 'lost-response'].includes(data.mode)) return json(res, { error: 'invalid mode' }, 400);
      mode = data.mode; return json(res, { mode });
    }
    if (url.pathname !== '/admin/api/qqbot/settings') return json(res, { error: 'not found' }, 404);
    if (req.method === 'GET') return mode === 'read-error' ? json(res, { error: '测试：读取失败' }, 503) : json(res, snapshot());
    if (req.method !== 'PUT') return json(res, { error: 'method not allowed' }, 405);
    if (mode === 'save-error') return json(res, { error: '测试：保存失败' }, 503);
    const parts = []; let size = 0;
    for await (const part of req) { size += part.length; if (size > 1024) return json(res, { error: 'too large' }, 413); parts.push(part); }
    const data = JSON.parse(Buffer.concat(parts).toString());
    if (!data || Array.isArray(data) || !Object.keys(data).length || Object.entries(data).some(([k,v]) => !Object.hasOwn(flags, k) || typeof v !== 'boolean')) return json(res, { error: 'invalid switches' }, 400);
    if (data.proactive_enabled === true && !flags.enabled && data.enabled !== true) return json(res, { error: '请先开启QQBot总开关。' }, 409);
    Object.assign(flags, data); if (!flags.enabled) flags.proactive_enabled = false;
    if (mode === 'lost-response') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end('{'); }
    return json(res, snapshot());
  } catch (error) {
    process.stderr.write(JSON.stringify({ level: 'error', operation: 'qq_preview', stack: error.stack }) + '\n');
    if (!res.headersSent) return json(res, { error: 'preview request failed' }, 500);
    res.end();
  }
});
async function start() {
  server.listen(Number(process.argv[2] || 8001), '127.0.0.1'); await once(server, 'listening');
  process.stdout.write('QQ dashboard preview: http://127.0.0.1:' + server.address().port + '/admin/index.html#/config\n');
}
if (require.main === module) start().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
module.exports = { server };
