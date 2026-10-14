// Read-only browser fixture for both native scrollbar axes using production assets.
const http = require('node:http');
const { once } = require('node:events');
const { server: baseServer } = require('../tools/preview_planning.cjs');
const baseHandler = baseServer.listeners('request')[0];
const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<link rel="stylesheet" href="/admin/css/style.css"><title>滚动条验证</title></head>
<body><main class="main" style="margin:0;padding:20px"><section class="content">
<h1>原生滚动条验证</h1><p>使用实际前端样式，分别检查纵向与横向内容完整可达。</p>
<div id="vertical" tabindex="0" style="height:160px;overflow:auto;border:1px solid var(--gold-line);padding:12px">
${Array.from({length:40},(_,i)=>`<p>纵向测试段落 ${i+1}：保留全部内容与原生滚动。</p>`).join('')}</div>
<div class="memo-md"><pre id="horizontal" tabindex="0">横向代码段：${'scrollbar_geometry_'.repeat(80)}最后一列</pre></div>
<textarea id="textarea" rows="5" style="width:100%">${Array.from({length:40},(_,i)=>`输入框测试第 ${i+1} 行`).join('\n')}</textarea>
</section></main><script type="module">
import { initScrollbars } from '/admin/js/lib/scrollbars.js'; initScrollbars();
</script></body></html>`;
const server = http.createServer(async (request, response) => {
  try {
    if (request.url === '/admin/scrollbar-fixture') {
      response.writeHead(200, { 'Content-Type':'text/html; charset=utf-8', 'Cache-Control':'no-store' });
      return response.end(html);
    }
    await baseHandler(request, response);
  } catch (error) {
    process.stderr.write(JSON.stringify({ level:'error', operation:'unified_scrollbar_fixture_request',
      route:request.url, stack:error.stack }) + '\n');
    if (!response.headersSent) response.writeHead(500);
    response.end();
  }
});
server.on('error', error => {
  process.stderr.write(JSON.stringify({ level:'error', operation:'unified_scrollbar_fixture_server', stack:error.stack }) + '\n');
  process.exitCode = 1;
});
async function start() {
  server.listen(8012, '127.0.0.1');
  await once(server, 'listening');
  process.stdout.write(JSON.stringify({ level:'info', operation:'unified_scrollbar_fixture_ready',
    url:'http://127.0.0.1:8012/admin/scrollbar-fixture' }) + '\n');
}
start().catch(error => {
  process.stderr.write(JSON.stringify({ level:'error', operation:'unified_scrollbar_fixture_start', stack:error.stack }) + '\n');
  process.exitCode = 1;
});
