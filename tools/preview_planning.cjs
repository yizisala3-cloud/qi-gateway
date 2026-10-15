// Local UI preview. Task creation/notes mutate synthetic in-memory data only;
// never loads .env, connects to a database, or ships in Docker.
const http = require('node:http');
const { once } = require('node:events');
const { server: baseServer } = require('./preview_gateway_logs.cjs');
const baseHandler = baseServer.listeners('request')[0];
const TOKEN = 'local-log-preview';
const startedAt = Date.now();
const clock = minutes => new Date(startedAt + minutes * 60000).toISOString();
const cycleDate = new Intl.DateTimeFormat('en-CA', {
  timeZone: 'America/Los_Angeles', year: 'numeric', month: '2-digit', day: '2-digit',
}).format(new Date(startedAt));

const examples = [
  { content: '整理网关前端改版思路，列出要调整的页面和控件', task_type: 'once',
    status: 'in_progress', estimated_minutes: 45, est_start: clock(-15),
    est_end: clock(30), actual_start: clock(-10) },
  { content: '阅读 20 分钟，记下今天最有启发的一句话', task_type: 'daily',
    status: 'pending', estimated_minutes: 20, est_start: clock(45), est_end: clock(65) },
  { content: '调整规划管理页的间距、按钮和卡片样式，检查长标题的显示效果', task_type: 'once',
    status: 'pending', estimated_minutes: 45, est_start: clock(75), est_end: clock(120),
    is_fixed: true, fixed_source: 'manual',
    window_start_at: clock(60), window_end_at: clock(150) },
  { content: '整理本周的笔记与备忘录', task_type: 'weekly', status: 'partial',
    estimated_minutes: 30, est_start: clock(135), est_end: clock(165),
    partial_note: '已整理工作笔记，生活记录还剩一半', actual_minutes: 15 },
  { content: '有空时挑一张新的桌面壁纸', task_type: 'idle', status: 'pending',
    estimated_minutes: 10 },
  { content: '把书桌收拾干净，归位散落的小物件', task_type: 'daily', status: 'deferred',
    estimated_minutes: 15, est_start: clock(-60), est_end: clock(-45) },
  { content: '散步并做一组肩颈拉伸', task_type: 'daily', status: 'completed',
    estimated_minutes: 25, est_start: clock(-110), est_end: clock(-85),
    actual_start: clock(-108), actual_end: clock(-82), actual_minutes: 26,
    actual_logged_seconds: 1560, actual_time_source: 'user', handled_at: clock(-82) },
];
const tasks = examples.map((row, index) => ({
  id: 101 + index, content: row.content, task_type: row.task_type, time_mode: 'duration',
  estimated_minutes: row.estimated_minutes, is_active: true, refresh_enabled: true,
  is_fixed: row.is_fixed || false, is_hollow: false, alarm_start: false, alarm_end: false,
  timer_minutes: null, weekdays: [0, 2, 4], target_date: cycleDate,
  has_generated_occurrence: true, created_at: clock(-1440), updated_at: clock(-60),
}));
const occurrences = examples.map((row, index) => ({
  id: 1001 + index, task_id: tasks[index].id, for_date: cycleDate, schedule_date: cycleDate,
  display_cycle_date: cycleDate, round_key: `preview-${index + 1}-${cycleDate}`,
  task_content: row.content, task_is_active: true, time_mode: 'duration',
  sort_order: index, is_hollow: false, is_limited: false, is_fixed: false,
  alarm_start: false, alarm_end: false, timer_minutes: null,
  est_start: null, est_end: null, actual_start: null, actual_end: null,
  actual_time_source: null, actual_logged_seconds: null, actual_minutes: null,
  partial_note: null, window_start_at: null, window_end_at: null,
  schedule_label: row.status === 'deferred' ? '落后' : '正常', source: 'local_preview',
  ...row,
}));
tasks[0].note = '先确定布局和交互，再逐项调整。完成后检查手机和桌面显示。';
occurrences[0].note = tasks[0].note;
tasks[1].note = '阅读后留下一个值得记住的想法。';
occurrences[1].note = tasks[1].note;
occurrences.push({ ...occurrences[1], id: 2002, status: 'completed',
  round_key: 'preview-previous-reading', note: '上次阅读的备注，后续修改时保留。',
  actual_minutes: 20, actual_logged_seconds: 1200 });
const creationKeys = new Map();

async function payloadOf(request) {
  const chunks = [];
  for await (const chunk of request) {
    chunks.push(chunk);
  }
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('请求必须是对象');
  if (payload.note != null && typeof payload.note !== 'string') {
    throw new Error('备注须为文本');
  }
  return payload;
}

function json(response, body, status = 200) {
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store',
  });
  response.end(JSON.stringify(body));
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    if (url.pathname.startsWith('/admin/api/planning/') || url.pathname.startsWith('/admin/api/memo/')) {
      if (request.headers.authorization !== `Bearer ${TOKEN}`) {
        return json(response, { error: '请使用本地预览令牌' }, 401);
      }
      const taskMatch = url.pathname.match(/^\/admin\/api\/planning\/tasks\/(\d+)$/);
      if (taskMatch) {
        const task = tasks.find(row => row.id === Number(taskMatch[1]));
        if (!task) return json(response, { error: '测试待办不存在' }, 404);
        if (request.method === 'GET') return json(response, task);
        if (request.method === 'PATCH') {
          let payload;
          try { payload = await payloadOf(request); }
          catch (error) { return json(response, { error: error.message }, 400); }
          if (!Object.hasOwn(payload, 'note')) return json(response, { error: '此预览仅支持编辑备注' }, 405);
          task.note = payload.note?.trim() || null;
          for (const row of occurrences) {
            if (row.task_id === task.id && ['pending', 'in_progress', 'deferred', 'partial'].includes(row.status)) row.note = task.note;
          }
          return json(response, task);
        }
      }
      if (request.method === 'POST' && url.pathname === '/admin/api/planning/tasks') {
        let payload;
        try { payload = await payloadOf(request); }
        catch (error) { return json(response, { error: error.message }, 400); }
        if (!payload.content?.trim()) return json(response, { error: '请填写内容' }, 400);
        const key = request.headers['idempotency-key'];
        if (key && creationKeys.has(key)) return json(response, creationKeys.get(key), 201);
        const id = Math.max(...tasks.map(row => row.id)) + 1;
        const task = { ...tasks[0], ...payload, id, note: payload.note?.trim() || null,
          task_type: payload.task_type || 'daily', estimated_minutes: Number(payload.estimated_minutes) || 30,
          created_at: clock(0), updated_at: clock(0) };
        tasks.push(task);
        occurrences.push({ ...occurrences[1], ...task, id: id + 1000, task_id: id,
          task_content: task.content, status: 'pending', round_key: `preview-created-${id}`,
          sort_order: id, actual_start: null, actual_end: null, actual_minutes: null,
          actual_logged_seconds: null, partial_note: null, est_start: null, est_end: null });
        if (key) creationKeys.set(key, task);
        return json(response, task, 201);
      }
      if (request.method !== 'GET') {
        return json(response, { error: '本地预览中的测试数据仅供查看，暂不支持保存修改' }, 405);
      }
      if (url.pathname === '/admin/api/planning/today') {
        return json(response, {
          cycle_date: cycleDate, now: new Date().toISOString(),
          progress: occurrences.filter(row => ['pending', 'in_progress', 'partial'].includes(row.status)),
          attention: occurrences.filter(row => row.status === 'deferred'),
          done: occurrences.filter(row => row.status === 'completed'), conflicts: [],
          recompute: { enabled: false, pending: false, wait_minutes: 5 },
        });
      }
      if (url.pathname === '/admin/api/planning/tasks') return json(response, tasks);
      if (url.pathname === '/admin/api/planning/occurrences') {
        return json(response, occurrences.filter(row => ['task_type', 'status', 'schedule_date']
          .every(key => !url.searchParams.get(key) || row[key] === url.searchParams.get(key))));
      }
      if (url.pathname === '/admin/api/planning/cycle') return json(response, {
        refresh_boundary_time: '06:00', daily_refresh_enabled: true,
        auto_recompute_enabled: false, auto_recompute_wait_minutes: 5, pending_boundary: null,
      });
      if (url.pathname === '/admin/api/memo/tags') return json(response, []);
      if (url.pathname === '/admin/api/memo/board') return json(response, { sections: [] });
      return json(response, { error: '此预览没有对应的测试记录' }, 404);
    }
    return await baseHandler(request, response);
  } catch (error) {
    process.stderr.write(JSON.stringify({ level: 'error', operation: 'planning_preview_request',
      method: request.method, route: request.url, stack: error.stack }) + '\n');
    if (!response.headersSent) return json(response, { error: '本地规划预览读取失败' }, 500);
    response.end();
  }
});
server.on('error', error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'planning_preview_server',
    stack: error.stack }) + '\n');
  process.exitCode = 1;
});

async function start() {
  const port = Number(process.argv[2] || 8000);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid preview port');
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');
  process.stdout.write(JSON.stringify({ level: 'info', operation: 'planning_preview_ready',
    url: `http://127.0.0.1:${port}/admin/`, sample_tasks: tasks.length }) + '\n');
}
if (require.main === module) start().catch(error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'planning_preview_start',
    stack: error.stack }) + '\n');
  process.exitCode = 1;
});
module.exports = { server };
