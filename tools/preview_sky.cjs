// Isolated dashboard preview: switches live in memory, no .env or external I/O.
const http = require('node:http');
const { once } = require('node:events');
const { randomUUID } = require('node:crypto');
const { server: base } = require('./preview_qqbot.cjs');
const baseHandler = base.listeners('request')[0];
let enabled = false, mode = 'normal';
let pauseEpoch = '00000000-0000-0000-0000-000000000000';
const json = (res, data, status = 200) => {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(data));
};
const snapshot = () => ({ enabled, pause_epoch: pauseEpoch, available: true, writable: mode !== 'readonly', source: 'preview' });
const server = http.createServer(async (req, res) => {
  try {
    const path = new URL(req.url, 'http://localhost').pathname;
    if (!path.startsWith('/admin/api/sky/') && path !== '/v1/sky/settings') return await baseHandler(req, res);
    if (req.headers.authorization !== 'Bearer local-log-preview') return json(res, { error: 'unauthorized' }, 401);
    const parts = []; let size = 0;
    for await (const part of req) { size += part.length; if (size > 1024) return json(res, { error: 'too large' }, 413); parts.push(part); }
    if (path === '/admin/api/sky/preview-mode' && req.method === 'PUT') {
      const body = JSON.parse(Buffer.concat(parts).toString());
      if (!['normal', 'readonly', 'read-error', 'save-error', 'lost-response'].includes(body.mode)) return json(res, { error: 'invalid mode' }, 400);
      mode = body.mode; return json(res, { mode });
    }
    if (!['/admin/api/sky/settings', '/v1/sky/settings'].includes(path)) return json(res, { error: 'not found' }, 404);
    if (req.method === 'GET') return mode === 'read-error' ? json(res, { error: '测试：读取失败' }, 503) : json(res, snapshot());
    if (path === '/v1/sky/settings' || req.method !== 'PUT') return json(res, { error: 'method not allowed' }, 405);
    if (['save-error', 'readonly'].includes(mode)) return json(res, { error: '测试：保存失败' }, 503);
    const body = JSON.parse(Buffer.concat(parts).toString());
    if (!body || Array.isArray(body) || Object.keys(body).join() !== 'enabled' || typeof body.enabled !== 'boolean') return json(res, { error: 'invalid switch' }, 400);
    enabled = body.enabled;
    if (!enabled) pauseEpoch = randomUUID();
    if (mode === 'lost-response') { res.writeHead(200, { 'Content-Type': 'application/json' }); return res.end('{'); }
    return json(res, snapshot());
  } catch (error) {
    process.stderr.write(JSON.stringify({ level: 'error', operation: 'sky_preview', stack: error.stack }) + '\n');
    if (!res.headersSent) return json(res, { error: 'preview request failed' }, 500);
    res.end();
  }
});
async function start() {
  server.listen(Number(process.argv[2] || 8010), '127.0.0.1');
  await once(server, 'listening');
  process.stdout.write('Sky dashboard preview: http://127.0.0.1:' + server.address().port + '/admin/index.html#/config\n');
}
if (require.main === module) start().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
module.exports = { server };
