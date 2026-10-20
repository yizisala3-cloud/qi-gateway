// Local visual preview only: real admin assets, synthetic data, no .env or database.
const http = require('node:http');
const { once } = require('node:events');
const { server: baseServer } = require('./preview_planning.cjs');
const baseHandler = baseServer.listeners('request')[0];
const TOKEN = 'local-log-preview';
const now = Date.now();
const clock = minutes => new Date(now + minutes * 60000).toISOString();
const themes = [
  ['moment', '雨天在窗边整理照片', '下午下了一场小雨，窗边的光线柔和了许多。我们一边整理旧照片，一边聊起去年秋天那次没有计划的出行。', ['生活片段', '照片', '下雨天']],
  ['thread', '还没决定的周末短途旅行', '想找一个离城市不太远、可以慢慢散步的地方。已经记下湖边步道和山里的小镇，等周五看看天气，再一起决定行程。', ['旅行', '周末', '待决定']],
  ['episode', '第一次一起把网关调通', '从一个接口报错开始，逐个检查配置、请求和返回值。傍晚终于看到第一条正常回复时，决定先去吃饭，把后面的美化留到第二天。', ['开发', '共同经历', '网关']],
  ['inside_joke', '“明天一定”现在叫“小步慢走”', '每当计划写得太满，就会提醒一句“小步慢走”。它的意思是先完成手边最小的一件事，再决定下一步，不急着把整张清单做完。', ['内部梗', '计划', '小步慢走']],
  ['profile', '沟通时喜欢先听结论，再展开细节', '讨论技术方案时，更喜欢先看到明确结论和关键取舍；需要深入时，再看实现细节、验证结果和具体代码位置。', ['沟通偏好', '技术讨论', '资料']],
  ['interaction_rule', '修改代码前先确认需求边界', '用户明确说“先不改代码”时，先做需求审查和方案讨论；得到实施授权后，再开始修改，并保留可供后续维护和审查的记录。', ['协作约定', '需求确认', '规则']],
];
const variations = ['', ' · 后来补充的细节', ' · 最近一次聊起', ' · 留到下周再继续', ' · 一段值得保存的回忆'];
const memories = Array.from({ length: 30 }, (_, i) => {
  const [type, title, content, tags] = themes[i % themes.length];
  const active = i < 24 && i !== 17;
  return {
    id: 501 + i, title: title + variations[Math.floor(i / 6)],
    content: content + (i === 0 ? '\n\n后来又补充了一个细节：把最喜欢的三张照片单独放进相册，留到周末慢慢写下旁边的故事。' : ''),
    continuity_type: type, continuity_schema_version: 1, continuity_data: {},
    continuity_id: `preview-continuity-${i + 1}`, tags, heat: 92 - i * 2.3,
    importance: 8 - i % 5, verified: i === 9 ? 'pending' : i === 15 ? 'rejected' : 'verified',
    is_active: active, source: i % 3 ? 'daily_digest' : 'manual',
    source_type: 'natural_chat', created_at: clock(-180 - i * 135),
    last_recalled_at: clock(-3 - i * 23), recall_count: 18 + i * 3,
    memory_time: clock(-120 - i * 180), source_time: clock(-125 - i * 180),
    time_precision: 'day', evidence_time_precision: 'minute',
    recall_scene: `当话题涉及${tags.slice(0, 2).join('、')}，或需要延续之前聊过的感受与安排时。`,
    recall_tags: [...tags, '自然聊天'], evidence_message_ids: [],
    memory_key: type === 'profile' ? `profile.preference-${i}` : null,
    thread_state: type === 'thread' ? 'open' : null,
    supersedes_memory_id: i === 11 ? 518 : null,
    superseded_by_memory_id: i === 17 ? 512 : null,
    superseded_at: i === 17 ? clock(-300) : null,
  };
});
memories[12].title = '一个还没讲完的下午：从整理旧照片聊到旅行计划，再绕回那些很小、却一直记得的生活片段';
memories[12].content = Array.from({ length: 5 }, (_, i) => [
  `第 ${i + 1} 段 · 留一点时间给日常`,
  '那些值得记住的时刻，往往没有特别隆重的开场。可能是一起走过一段安静的路，可能是把一个棘手的小问题终于处理好，也可能只是很普通的一句“今天过得怎么样”。',
  '想把这些零散的片段慢慢收好。等以后再提起时，还能记起当时窗外的天气、桌上的那杯茶，以及说到一半时没有继续下去的话。',
].join('\n')).join('\n\n');

const history = Array.from({ length: 150 }, (_, i) => {
  const memory = memories[i % memories.length];
  const oldContent = themes[i % themes.length][2];
  return {
    id: 150 - i, batch_id: `preview-batch-${Math.floor(i / 3)}`,
    memory_id: i === 5 ? 599 : memory.id,
    recalled_at: clock(-2 - Math.floor(i / 3) * 11), position: i % 3 + 1,
    title: i === 5 ? '那家已经关门、却还记得味道的小面馆' : memory.title,
    continuity_type: memory.continuity_type,
    injection_text: i === 5 ? '时间：上个冬天\n曾经一起去过街角的小面馆。后来店铺搬走了，偶尔聊起吃什么时，还是会想起热汤和窗边的那个座位。'
      : `时间：${new Date(now - (i + 1) * 86400000).toLocaleDateString('zh-CN')}｜${memory.title}\n\n${i === 12 ? memory.content : oldContent}${i % 7 === 0 ? '\n\n当时记下的是这一段。新的补充内容可以通过“查看当前记忆”对照阅读。' : ''}`,
  };
});
const statuses = ['pending', 'pending', 'conflict', 'approved', 'rejected', 'merged', 'duplicate'];
const requests = Array.from({ length: 14 }, (_, i) => ({
  id: 801 + i, title: themes[i % themes.length][1], content: themes[i % themes.length][2],
  continuity_type: themes[i % themes.length][0], tags: themes[i % themes.length][3],
  continuity_data: {}, continuity_schema_version: 1, source: 'daily_digest',
  source_type: 'natural_chat', reason: '这段对话具有可延续的偏好、约定或共同经历，建议保留以便后续自然衔接。',
  status: statuses[i % statuses.length], importance: 7 - i % 3, confidence: 0.94 - i % 4 * 0.07,
  update_mode: 'append', producer_path: i % 2 ? 'rumination' : 'fast_path',
  absorbed_fast_path_memory_ids: [], evidence_message_ids: [],
  source_message_id: 2001 + i, conversation_id: 'local-visual-preview',
  recall_scene: '当用户再次聊起相关话题，或需要延续之前的计划与感受时。',
  recall_tags: themes[i % themes.length][3], created_at: clock(-30 - i * 45),
  reviewed_at: i % 7 > 2 ? clock(-15 - i * 30) : null,
  review_note: i % 7 === 4 ? '本次信息不足，暂不写入正式记忆。' : null,
  memory_id: i % 7 === 3 ? 501 + i : null,
  related_memory_id: i % 7 === 2 ? 501 : null,
}));

function json(response, value, status = 200) {
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  response.end(JSON.stringify(value));
}
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://127.0.0.1');
    if (!url.pathname.startsWith('/admin/api/data/')) return await baseHandler(request, response);
    if (request.headers.authorization !== `Bearer ${TOKEN}`) return json(response, { error: '请使用本地预览令牌' }, 401);
    if (request.method !== 'GET') return json(response, { error: '此页面为本地视觉预览，测试记录仅供查看。' }, 405);
    const [, table, id] = url.pathname.match(/^\/admin\/api\/data\/([^/]+)(?:\/(\d+))?$/) || [];
    let rows = table === 'memories' ? memories : table === 'memory_recall_history' ? history
      : table === 'memory_requests' ? requests : table === 'persona' ? [{ id: 1, name: '日常陪伴',
        content: '自然地延续之前聊过的话题，认真回应每一次分享；有需要时一起把复杂问题拆成可以慢慢完成的小步骤。', is_active: true, updated_at: clock(-300) }] : [];
    const eq = JSON.parse(url.searchParams.get('eq') || '{}');
    if (id) eq.id = Number(id);
    rows = rows.filter(row => Object.entries(eq).every(([key, value]) => row[key] === value));
    const search = (url.searchParams.get('search') || '').trim().toLowerCase();
    if (search) rows = rows.filter(row => `${row.title || ''}\n${row.content || ''}\n${row.tags || []}`.toLowerCase().includes(search));
    if (url.searchParams.get('count') === 'true') return json(response, { count: rows.length });
    const order = url.searchParams.get('order');
    if (order && table !== 'memory_recall_history') {
      const asc = url.searchParams.get('asc') === 'true';
      rows = [...rows].sort((a, b) => (a[order] > b[order] ? 1 : a[order] < b[order] ? -1 : 0) * (asc ? 1 : -1));
    }
    const offset = Math.max(0, Number(url.searchParams.get('offset')) || 0);
    const limit = Math.min(150, Math.max(1, Number(url.searchParams.get('limit')) || 50));
    return json(response, { data: rows.slice(offset, offset + limit) });
  } catch (error) {
    process.stderr.write(JSON.stringify({ level: 'error', operation: 'memory_preview_request', route: request.url, stack: error.stack }) + '\n');
    if (!response.headersSent) return json(response, { error: '本地记忆预览读取失败' }, 500);
    response.end();
  }
});
server.on('error', error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'memory_preview_server', stack: error.stack }) + '\n');
  process.exitCode = 1;
});
async function start() {
  const port = Number(process.argv[2] || 8768);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid preview port');
  server.listen(port, '127.0.0.1');
  await once(server, 'listening');
  process.stdout.write(JSON.stringify({ level: 'info', operation: 'memory_preview_ready',
    url: `http://127.0.0.1:${port}/admin/#/memories?view=recall`, memories: memories.length,
    recall_history: history.length, requests: requests.length }) + '\n');
}
if (require.main === module) start().catch(error => {
  process.stderr.write(JSON.stringify({ level: 'error', operation: 'memory_preview_start', stack: error.stack }) + '\n');
  process.exitCode = 1;
});
module.exports = { server };
