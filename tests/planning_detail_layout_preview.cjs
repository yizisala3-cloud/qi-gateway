// Read-only layout fixture using the complete production admin DOM and assets.
// Synthetic long lists and notes exercise scroll isolation without any database.
const http = require('node:http');
const { once } = require('node:events');
const fs = require('node:fs/promises');
const path = require('node:path');
const { server: baseServer } = require('../tools/preview_planning.cjs');
const baseHandler = baseServer.listeners('request')[0];
const TOKEN = 'local-log-preview';
const now = Date.now();
const at = minutes => new Date(now + minutes * 60000).toISOString();
const date = '2026-10-13';
const notes = Array.from({ length: 24 }, (_, i) =>
  `布局验证第 ${i + 1} 段：这是一段较长的待办详情，只用于检查信息区的换行与滚动。保持每一段内容完整，并确认滚动到最后仍可以查看提醒和当前状态。`).join('\n\n');
const rows = Array.from({ length: 60 }, (_, index) => ({
  id: 1001 + index, task_id: 101 + index,
  content: index ? `布局测试待办 ${String(index + 1).padStart(2, '0')}：检查长列表中的卡片换行与独立滚动`
    : '整理网关前端改版思路，列出要调整的页面和控件',
  task_type: 'once', status: index ? 'pending' : 'in_progress', task_is_active: true,
  time_mode: 'duration', estimated_minutes: 45, sort_order: index,
  for_date: date, schedule_date: date, display_cycle_date: date,
  round_key: `layout-fixture-${index}`, est_start: at(15 + index * 45),
  est_end: at(60 + index * 45), actual_start: index ? null : at(-10), actual_end: null,
  actual_minutes: null, actual_logged_seconds: null, actual_time_source: null,
  partial_note: index ? null : notes, is_hollow: false, is_fixed: false,
  window_start_at: index ? null : at(0), window_end_at: index ? null : at(90),
  is_limited: false, alarm_start: false, alarm_end: false, timer_minutes: null,
  source: 'local_layout_fixture', schedule_label: '正常',
}));
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    if (request.method === 'GET' && ['/admin', '/admin/', '/admin/index.html'].includes(url.pathname)) {
      const html = await fs.readFile(path.join(__dirname, '../admin/index.html'), 'utf8');
      response.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store' });
      // Keep the fixture free of the base preview's floating badge, which would
      // otherwise cover part of the footer in short or mobile viewports.
      return response.end(html.replace('<head>', `<head><script>localStorage.setItem('qi-token', '${TOKEN}');</script>`));
    }
    if (url.pathname === '/admin/api/planning/today' && request.method === 'GET') {
      if (request.headers.authorization !== `Bearer ${TOKEN}`) {
        response.writeHead(401, { 'Content-Type': 'application/json' });
        return response.end(JSON.stringify({ error: '本地布局验证令牌无效' }));
      }
      response.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
      return response.end(JSON.stringify({ progress: rows, attention: [], done: [], conflicts: [],
        now: new Date().toISOString(), recompute: { enabled: false, pending: false } }));
    }
    return await baseHandler(request, response);
  } catch (error) {
    process.stderr.write(JSON.stringify({ level: 'error', operation: 'planning_layout_fixture',
      route: request.url, stack: error.stack }) + '\n');
    if (!response.headersSent) response.writeHead(500);
    response.end();
  }
});
server.on('error', error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'planning_layout_fixture_server',
    stack: error.stack }) + '\n');
  process.exitCode = 1;
});
async function start() {
  server.listen(8011, '127.0.0.1');
  await once(server, 'listening');
  process.stdout.write(JSON.stringify({ level: 'info', operation: 'planning_layout_fixture_ready',
    url: 'http://127.0.0.1:8011/admin/#/planning', rows: rows.length }) + '\n');
}
if (require.main === module) start().catch(error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'planning_layout_fixture_start',
    stack: error.stack }) + '\n');
  process.exitCode = 1;
});
module.exports = { server };
