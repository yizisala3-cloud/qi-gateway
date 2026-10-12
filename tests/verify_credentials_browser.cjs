// TASK-01 浏览器验证：配置页密钥默认隐藏、按项显示/隐藏、离开清理、
// 晚到不恢复（L26/L27）。真实 admin 外壳 + 本地只读假 API（无生产连接）；
// 合成密钥值仅用于检测字符串是否出现。
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

const repository = path.resolve(process.argv[2]);
const adminRoot = path.join(repository, 'admin');
const outDir = path.resolve(process.argv[3] || '.');
const requests = [];
const aborted = [];

const REAL_VALUE = 'sk-credverify-real-value-1234567890';
const credentials = [
  { credential_id: 'upstream', role: '聊天转发模型', configured: true,
    source: 'UPSTREAM_API_KEY', fallback: false, provider: 'api.example.com',
    key_ref: 'kh1-abcdef1234567890abcdef1234567890', label: 'K-abcdef12',
    revealable: true },
  { credential_id: 'mcp_memory', role: 'MCP 服务', configured: false,
    source: 'MCP_MEMORY_TOKEN', fallback: false, provider: '',
    key_ref: '', label: '', revealable: true },
];

const json = (response, body, headers = {}) => {
  response.writeHead(200, {
    'Content-Type': 'application/json',
    'Cache-Control': 'no-store',
    ...headers,
  });
  response.end(JSON.stringify(body));
};

let slowReveal = false;
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    requests.push(request.method + ' ' + url.pathname);
    if (url.pathname === '/status') return json(response, {});
    if (url.pathname === '/health') return json(response, { status: 'ok', uptime: 5 });
    if (url.pathname === '/admin/api/config/credentials') {
      return json(response, { credentials, key_ref_ready: true });
    }
    if (url.pathname === '/admin/api/config/credentials/upstream/reveal' && request.method === 'POST') {
      if (slowReveal) {
        request.on('close', () => {
          if (request.destroyed) aborted.push(url.pathname);
        });
        await new Promise(resolve => setTimeout(resolve, 1500));
        if (request.destroyed) return;
      }
      return json(response, {
        credential_id: 'upstream', role: '聊天转发模型',
        source: 'UPSTREAM_API_KEY', fallback: false, provider: 'api.example.com',
        key_ref: 'kh1-abcdef1234567890abcdef1234567890', label: 'K-abcdef12',
        value: REAL_VALUE,
      });
    }
    if (url.pathname === '/admin/api/config/credentials/mcp_memory/reveal' && request.method === 'POST') {
      return json(response, { error: '该密钥项当前未配置', error_code: 'credential_not_configured' }, { status: 409, ...{} });
    }
    if (url.pathname.startsWith('/admin/api/')) return json(response, { data: [] });
    const file = path.resolve(adminRoot, '.' + url.pathname.replace(/^\/admin/, ''));
    if (!file.startsWith(adminRoot + path.sep)) { response.writeHead(404).end(); return; }
    const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css',
      '.png': 'image/png', '.svg': 'image/svg+xml', '.jpg': 'image/jpeg' };
    response.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' });
    response.end(await fs.readFile(file));
  } catch (error) {
    if (!response.headersSent) response.writeHead(404);
    response.end();
  }
});

const revealCount = () => requests.filter(x => x === 'POST /admin/api/config/credentials/upstream/reveal').length;

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}/admin/index.html`;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const result = { data_source: 'local fake read-only API; no production connection' };
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.addInitScript(() => localStorage.setItem('qi-token', 'credverify-token'));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));

    // L26：初次打开只有元数据请求；DOM / 存储无真实值
    await page.goto(base + '#/config');
    await page.locator('#cred-list').waitFor();
    await page.getByText('聊天转发模型').first().waitFor();
    const initial = {
      reveal_requests: revealCount(),
      dom_contains_value: (await page.content()).includes(REAL_VALUE),
      mask_visible: await page.getByText('••••••••').first().isVisible(),
      local_storage: await page.evaluate(() => JSON.stringify(localStorage)),
      session_storage: await page.evaluate(() => JSON.stringify(sessionStorage)),
    };
    assert.equal(initial.reveal_requests, 0, '初次加载不发起任何 reveal 请求（L26）');
    assert.equal(initial.dom_contains_value, false, '初次 DOM 无真实值（L26）');
    assert.ok(initial.mask_visible, '默认固定隐藏占位（L26）');
    assert.ok(!initial.local_storage.includes(REAL_VALUE), 'localStorage 无真实值');
    assert.ok(!initial.session_storage.includes(REAL_VALUE), 'sessionStorage 无真实值');

    // L27：点击显示 → 只请求点击项；成功显示完整实际值与匹配标识
    const revealResponse = page.waitForResponse(r => r.url().includes('/reveal'));
    await page.locator('[data-act="cred-reveal"][data-cred="upstream"]').click();
    await revealResponse;
    await page.getByText(REAL_VALUE).first().waitFor();
    const shown = {
      reveal_requests: revealCount(),
      label_visible: await page.getByText('K-abcdef12').first().isVisible(),
      hide_button: await page.locator('[data-act="cred-hide"][data-cred="upstream"]').count(),
    };
    assert.equal(shown.reveal_requests, 1, '只请求点击项（L27）');
    assert.ok(shown.label_visible, '显示匹配标识（L27）');
    assert.ok(shown.hide_button >= 1, '按钮切换为隐藏（L27）');
    await page.screenshot({ path: path.join(outDir, 'cred-shown.png'), fullPage: true });

    // L27：点击隐藏 → DOM 清除真实值
    await page.locator('[data-act="cred-hide"][data-cred="upstream"]').click();
    await page.locator('[data-act="cred-reveal"][data-cred="upstream"]').first().waitFor();
    assert.equal((await page.content()).includes(REAL_VALUE), false, '隐藏后 DOM 无真实值（L27）');

    // L27：慢响应期间离开页面 → 请求取消、晚到不恢复
    slowReveal = true;
    await page.locator('[data-act="cred-reveal"][data-cred="upstream"]').click();
    await page.locator('#sidebar-nav a[data-key="persona"]').click();
    await page.waitForURL(u => u.hash === '#/persona');
    await new Promise(resolve => setTimeout(resolve, 1900));
    const afterLeave = {
      persona_shows_value: (await page.content()).includes(REAL_VALUE),
      aborted: aborted.length > 0,
    };
    assert.equal(afterLeave.persona_shows_value, false, '离开后页面无真实值（L27）');
    assert.ok(afterLeave.aborted, '在途查看请求被取消（L27）');

    // L27：重新进入默认隐藏，不保留显示状态
    await page.locator('#sidebar-nav a[data-key="config"]').click();
    await page.locator('#cred-list').waitFor();
    await page.getByText('聊天转发模型').first().waitFor();
    await new Promise(resolve => setTimeout(resolve, 300));
    const reentered = {
      dom_contains_value: (await page.content()).includes(REAL_VALUE),
      mask_visible: await page.getByText('••••••••').first().isVisible(),
      reveal_requests: revealCount(),
    };
    assert.equal(reentered.dom_contains_value, false, '重新进入无真实值（L27）');
    assert.ok(reentered.mask_visible, '重新进入默认隐藏（L27）');

    // L28：未配置项显示明确状态，不出现可显示的假值按钮（409 路径由
    // API 契约测试覆盖）
    const unconfigured = {
      shows_state: await page.getByText('未配置').first().isVisible(),
      reveal_buttons: await page.locator('[data-act="cred-reveal"][data-cred="mcp_memory"]').count(),
    };
    assert.ok(unconfigured.shows_state, '未配置项显示明确状态（L28）');
    assert.equal(unconfigured.reveal_buttons, 0, '未配置项无显示按钮（L28）');
    assert.equal((await page.content()).includes(REAL_VALUE), false, '未配置项不显示值（L28）');

    assert.deepEqual(errors, [], '无 pageerror');
    result.initial = initial;
    result.shown = shown;
    result.after_leave = afterLeave;
    result.reentered = reentered;
    result.page_errors = errors;
  } finally {
    await browser.close();
    server.close();
  }
  fs.mkdir(outDir, { recursive: true }).then(() =>
    fs.writeFile(path.join(outDir, 'cred-browser-verify.json'),
      JSON.stringify(result, null, 2) + '\n'));
  console.log(JSON.stringify(result, null, 2));
})().catch(error => { console.error(error); process.exit(1); });
