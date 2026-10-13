// Local control acceptance fixture; production assets, synthetic responses, no .env or upstream.
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const { server: baseServer } = require('./preview_gateway_logs.cjs');
const baseHandler = baseServer.listeners('request')[0];
const root = path.resolve(__dirname, '..');
const requests = [];
let failNext = false;

function json(response, value, status = 200) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(value));
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    if (url.pathname === '/controls-check') {
      const version = (await fs.readFile(path.join(root, 'admin/js/ui.js'), 'utf8'))
        .match(/ASSET_VERSION = '([^']+)'/)[1];
      const html = await fs.readFile(path.join(root, 'tests/fixtures/shared_controls.html'), 'utf8');
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      response.end(html.replaceAll('__ASSET_VERSION__', version));
      return;
    }
    if (url.pathname === '/controls-requests') return json(response, requests);
    if (request.method === 'GET' && url.pathname === '/admin/api/planning/today') {
      return json(response, { progress: [], attention: [], done: [], conflicts: [],
        recompute: { enabled: true, pending: false, wait_minutes: 5 } });
    }
    if (request.method === 'GET' && ['/admin/api/planning/tasks', '/admin/api/planning/occurrences',
      '/admin/api/memo/tags'].includes(url.pathname)) return json(response, []);
    if (request.method === 'GET' && url.pathname === '/admin/api/memo/board') return json(response, { sections: [] });
    if (url.pathname === '/controls-fail-next' && request.method === 'POST') {
      failNext = true;
      return json(response, { configured: true });
    }
    if (url.pathname.startsWith('/admin/api/planning/occurrences/') && request.method !== 'GET') {
      const chunks = [];
      let size = 0;
      for await (const chunk of request) {
        size += chunk.length;
        if (size > 16384) return json(response, { error: '测试请求过大' }, 413);
        chunks.push(chunk);
      }
      const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      requests.push({ path: url.pathname, method: request.method, body,
        idempotency_key: request.headers['idempotency-key'] || null });
      if (failNext) {
        failNext = false;
        return json(response, { error: '测试范围错误：最晚完成必须晚于最早开始' }, 422);
      }
      return json(response, { saved: true });
    }
    return baseHandler(request, response);
  } catch (error) {
    process.stderr.write(JSON.stringify({ level: 'error', operation: 'controls_preview_request',
      route: request.url, stack: error.stack }) + '\n');
    if (!response.headersSent) return json(response, { error: '控件测试响应失败' }, 500);
    response.end();
  }
});
server.on('error', error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'controls_preview_server', stack: error.stack }) + '\n');
  process.exitCode = 1;
});
const port = Number(process.argv[2] || 8767);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid preview port');
server.listen(port, '127.0.0.1', () => {
  process.stdout.write(JSON.stringify({ level: 'info', operation: 'controls_preview_ready',
    url: `http://127.0.0.1:${port}/controls-check` }) + '\n');
});
