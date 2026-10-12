// 修复批次浏览器验证：真实 admin 外壳 + 本地只读假 API（无生产连接）。
// 验证 BUG-05/06/07/08/15 修复后的桌面与移动端行为；数据为合成标记值。
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');

const repository = path.resolve(process.argv[2]);
const adminRoot = path.join(repository, 'admin');
const outDir = path.resolve(process.argv[3] || '.');
const requests = [];
const group = {
  fingerprint: 'aaaaaaaaaaaaaaaa', event_count: 2, level: 'ERROR', module: 'planning',
  operation: 'fix_verify', error_code: 'FIXVERIFY_SYNTHETIC_ERROR',
  message_zh: 'FIXVERIFY SYNTHETIC DATA', first_seen_at: '2026-10-10T01:00:00Z',
  last_seen_at: '2026-10-10T02:00:00Z', exception_type: 'VerifyError', outcome: 'failed',
  sample_event_id: '00000000-0000-0000-0000-000000000001', sample_request_id: 'verify-id',
  key_ref: 'kh1-abcdef1234567890abcdef1234567890', credential_role: 'chat',
  provider: 'api.example.com',
};
const json = (response, body) => {
  response.writeHead(200, { 'Content-Type': 'application/json' });
  response.end(JSON.stringify(body));
};
const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, 'http://localhost');
    requests.push(url.pathname + url.search);
    if (url.pathname === '/status') return json(response, {});
    if (url.pathname === '/admin/api/logs/meta') return json(response, {
      modules: [{ module: 'planning', count: 2 }, { module: 'chat', count: 1 }],
      keys: [{ key_ref: 'kh1-abcdef1234567890abcdef1234567890',
               credential_role: 'chat', provider: 'api.example.com', count: 2 }],
      collection: {
        enabled: true, queue_depth: 0, written_total: 3, queue_dropped: 0,
        dropped_total: 0, retention_days: 30, consecutive_failures: 0,
        spool: {
          enabled: true, files: 2, corrupt_files: 1, bytes: 2048,
          replayed_total: 0, corrupt_total: 1, dropped_total: 4,
          dropped_last_at: '2026-10-10T02:30:00Z',
          dropped_last_reason: 'spool_capacity_rotation',
        },
      },
    });
    if (url.pathname === '/admin/api/logs') return json(response, {
      groups: [group], has_more: false, next_cursor: null,
      snapshot: '2026-10-10T03:00:00Z',
      visible_snapshot: '100:110:101',
    });
    if (url.pathname === '/admin/api/logs/group') return json(response, {
      group, samples: [{
        event_id: '00000000-0000-0000-0000-000000000001',
        occurred_at: '2026-10-10T02:00:00Z', level: 'ERROR', module: 'planning',
        message_raw: 'FIXVERIFY SYNTHETIC RAW',
        stack_trace: 'VerifyError: synthetic error\n' + 'X'.repeat(300),
        request_id: 'verify-id', route: '/verify', method: 'GET',
        key_ref: 'kh1-abcdef1234567890abcdef1234567890', credential_role: 'chat',
        provider: 'api.example.com',
        truncated_fields: [],
      }],
    });
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

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}/admin/index.html#/logs`;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const result = { data_source: 'local fake read-only API; no production connection' };
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    await context.addInitScript(() => localStorage.setItem('qi-token', 'fixverify-token'));
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(url);
    await page.getByText('FIXVERIFY SYNTHETIC DATA', { exact: false }).first().waitFor();
    // BUG-15：spool 丢弃 / 损坏文件分项可见
    await page.getByText('缓冲丢弃 4', { exact: false }).first().waitFor();
    await page.getByText('损坏文件 1 个', { exact: false }).first().waitFor();
    await page.screenshot({ path: path.join(outDir, 'desktop.png'), fullPage: true });

    // BUG-07（筛选残留）：选 ERROR → 离开 → 返回，控件与请求一致
    await page.locator('#log-level').selectOption('ERROR');
    await page.waitForResponse(r => r.url().includes('/admin/api/logs?') && r.url().includes('level=ERROR'));
    await page.locator('#sidebar-nav a[data-key="persona"]').click();
    await page.waitForURL(u => u.hash === '#/persona');
    await page.locator('#sidebar-nav a[data-key="logs"]').click();
    await page.getByText('FIXVERIFY SYNTHETIC DATA', { exact: false }).first().waitFor();
    const afterReturn = {
      visible_level: await page.locator('#log-level').inputValue(),
      last_request: requests.filter(x => x.startsWith('/admin/api/logs?')).at(-1),
    };
    assert.equal(afterReturn.visible_level, '', '返回后控件回默认（BUG-07）');
    assert.ok(!afterReturn.last_request.includes('level=ERROR'), '返回后请求不带残留 level（BUG-07）');

    // BUG-07（custom 卡死）：自定义区间 → 离开 → 返回，列表正常加载
    await page.locator('#log-preset').selectOption('custom');
    await page.locator('#log-start').fill('2026-10-10T00:00');
    await page.locator('#log-end').fill('2026-10-11T00:00');
    const customResponse = page.waitForResponse(r => r.url().includes('/admin/api/logs?') && r.url().includes('2026-10-10T00'));
    await page.locator('[data-act="refresh"]').first().click();
    await customResponse;
    await page.getByText('FIXVERIFY SYNTHETIC DATA', { exact: false }).first().waitFor();
    const before = requests.filter(x => x.startsWith('/admin/api/logs?')).length;
    await page.locator('#sidebar-nav a[data-key="persona"]').click();
    await page.waitForURL(u => u.hash === '#/persona');
    await page.locator('#sidebar-nav a[data-key="logs"]').click();
    await page.getByText('FIXVERIFY SYNTHETIC DATA', { exact: false }).first().waitFor();
    const remountCustom = {
      list_requests_after_return:
        requests.filter(x => x.startsWith('/admin/api/logs?')).length - before,
      visible_preset: await page.locator('#log-preset').inputValue(),
      custom_start_hidden: !(await page.locator('#log-start').isVisible()),
      time_hint: await page.locator('#log-time-hint').innerText(),
    };
    assert.ok(remountCustom.list_requests_after_return >= 1, '返回后列表正常请求（BUG-07 custom 不卡死）');
    assert.equal(remountCustom.visible_preset, '24h', '返回后 preset 回默认');
    assert.equal(remountCustom.custom_start_hidden, true, 'custom 输入隐藏');
    assert.equal(remountCustom.time_hint, '', '无残留校验错误');
    await page.screenshot({ path: path.join(outDir, 'remount.png'), fullPage: true });
    result.remount = { filter: afterReturn, custom: remountCustom };

    // BUG-06：详情携带级别筛选与快照；BUG-05：可见性快照随详情回传
    await page.locator('#log-level').selectOption('ERROR');
    const detailResponse = page.waitForResponse(r => r.url().includes('/admin/api/logs/group'));
    await page.locator('#log-list [data-act="detail"]').first().click();
    await detailResponse;
    await page.getByText('FIXVERIFY SYNTHETIC RAW', { exact: false }).waitFor();
    const detailUrl = requests.filter(x => x.startsWith('/admin/api/logs/group?')).at(-1);
    assert.ok(detailUrl.includes('level=ERROR'), '详情带级别筛选（BUG-06）');
    assert.ok(detailUrl.includes('snapshot='), '详情带快照（BUG-05/06）');
    assert.ok(detailUrl.includes('visible=100%3A110%3A101'), '详情带可见性快照（BUG-05）');
    // TASK-02：密钥短标识与用途在列表 / 详情可见
    await page.locator('#log-list').getByText('K-abcdef12', { exact: false }).first().waitFor();
    await page.locator('.modal').getByText('kh1-abcdef1234567890abcdef1234567890', { exact: false }).first().waitFor();
    const keyOptions = await page.locator('#log-key option').allTextContents();
    assert.ok(keyOptions.some(o => o.includes('K-')), '密钥筛选选项含短标识（TASK-02）');
    await page.screenshot({ path: path.join(outDir, 'detail.png'), fullPage: true });
    result.detail_url = detailUrl;
    // BUG-07（弹窗生命周期）：打开详情后经 hash 导航离开日志页（弹窗遮罩
    // 覆盖全屏，物理点击被拦截，浏览器返回/前进同属 hash 导航）——unmount
    // 必须关闭挂在 body 上的详情弹窗并取消在途请求，旧弹窗不得遮挡新页面。
    assert.equal(await page.locator('.modal').count(), 1, '详情弹窗已打开');
    await page.evaluate(() => { location.hash = '#/persona'; });
    await page.waitForURL(u => u.hash === '#/persona');
    assert.equal(await page.locator('.modal').count(), 0, '离开后详情弹窗已回收（BUG-07）');
    await page.screenshot({ path: path.join(outDir, 'modal-after-navigation.png'), fullPage: true });
    await page.evaluate(() => { location.hash = '#/logs'; });
    await page.getByText('FIXVERIFY SYNTHETIC DATA', { exact: false }).first().waitFor();

    // 移动端：列表 / 详情 / 长堆栈不越界
    const mobile = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true });
    await mobile.addInitScript(() => localStorage.setItem('qi-token', 'fixverify-token'));
    const phone = await mobile.newPage();
    const phoneErrors = [];
    phone.on('pageerror', error => phoneErrors.push(error.message));
    await phone.goto(url);
    await phone.getByText('FIXVERIFY SYNTHETIC DATA', { exact: false }).first().waitFor();
    await phone.screenshot({ path: path.join(outDir, 'mobile.png'), fullPage: true });
    await phone.locator('#log-list [data-act="detail"]').first().click();
    await phone.getByText('FIXVERIFY SYNTHETIC RAW', { exact: false }).waitFor();
    await phone.screenshot({ path: path.join(outDir, 'mobile-detail.png'), fullPage: true });
    const widths = await phone.evaluate(() => ({
      viewport: innerWidth, document: document.documentElement.scrollWidth,
      modal: document.querySelector('.modal').getBoundingClientRect().width,
    }));
    assert.ok(widths.document <= widths.viewport + 1, '移动端文档不横向越界');
    result.mobile_widths = widths;
    result.page_errors = errors;
    result.mobile_page_errors = phoneErrors;
    assert.deepEqual(errors, [], '桌面无 pageerror');
    assert.deepEqual(phoneErrors, [], '移动端无 pageerror');
    await context.close();
    await mobile.close();
  } finally {
    await browser.close();
    server.close();
  }
  await fs.writeFile(path.join(outDir, 'browser-verify.json'), JSON.stringify(result, null, 2) + '\n');
  console.log(JSON.stringify(result, null, 2));
})().catch(error => {
  console.error('FAILED — captured requests:');
  for (const request of requests) console.error('  ', request);
  console.error(error);
  process.exit(1);
});
