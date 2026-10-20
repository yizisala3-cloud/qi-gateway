// Real admin modules in Edge; HTTP fixtures only, no production services.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../admin');
const output = path.resolve(__dirname, 'memory-recall-20261020');
const rows = Array.from({ length: 150 }, (_, i) => ({
  id: 150 - i, batch_id: `batch-${i}`, memory_id: 1, position: i % 8 + 1,
  recalled_at: new Date(Date.UTC(2026, 9, 20, 12) - i * 60000).toISOString(),
  title: i === 0 ? '快照 <历史>' : `重复召回 ${i}`,
  continuity_type: 'moment', injection_text: i === 0 ? '时间：2026-10-20 19:00｜原始截断文本 <script>window.__injected=true</script>' : `第${i}次实际注入文本`,
}));
const server = http.createServer(async (req, res) => {
  try {
    const relative = decodeURIComponent(new URL(req.url, 'http://local').pathname).replace(/^\/admin\/?/, '') || 'index.html';
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
    const data = await fs.readFile(file);
    const ext = path.extname(file);
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml' })[ext] || 'application/octet-stream');
    res.end(data);
  } catch { res.writeHead(404).end(); }
});

(async () => {
  await fs.mkdir(output, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}/admin/`;
  let browser;
  const errors = [], results = [];
  let mode = 'normal', delay = 0, reads = 0;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => localStorage.setItem('qi-token', 'local-recall-fixture'));
    await page.route('**/status', route => route.fulfill({ json: { status: 'ok' } }));
    await page.route('**/admin/api/data/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('memory_recall_history')) {
        reads++;
        assert.equal(url.searchParams.get('limit'), '150');
        const wait = delay, responseMode = mode;
        if (wait) await new Promise(resolve => setTimeout(resolve, wait));
        await route.fulfill(responseMode === 'error'
          ? { status: 503, json: { error: 'fixture database unavailable' } }
          : { json: { data: responseMode === 'empty' ? [] : rows } });
      } else await route.fulfill({ json: { data: [], count: 0 } });
    });
    await page.goto(base + '#/memories?view=recall');
    await page.locator('#recall-list .mem-card').first().waitFor();
    assert.equal(await page.locator('#recall-list .mem-card').count(), 20);
    assert.equal(reads, 1);
    await page.locator('#recall-list .mem-card').first().click();
    assert.match(await page.locator('.detail-body').innerText(), /原始截断文本 <script>/);
    assert.equal(await page.evaluate(() => window.__injected), undefined);
    results.push('snapshot content escaped; repeated memory IDs preserved; one bounded read');
    await page.waitForTimeout(450);
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    // Pager exposes numeric next page identity; exercise all eight pages.
    for (let i = 1; i < 8; i++) {
      await page.locator(`[data-act="recall-page"][data-p="${i}"]`).last().click();
    }
    assert.equal(await page.locator('#recall-list .mem-card').count(), 10);
    assert.equal(reads, 1);
    results.push('150 snapshots paginated locally into eight pages');
    await page.locator('[data-act="recall-current"]').click();
    await page.getByText('记忆不存在或已被删除', { exact: true }).waitFor();
    results.push('missing current memory gives explicit feedback');

    await page.locator('[data-view="recall"]').click();
    await page.locator('#recall-list .mem-card').first().waitFor();
    mode = 'error';
    await page.locator('[data-act="recall-refresh"]').click();
    await page.getByText(/召回记录读取失败/).waitFor();
    mode = 'empty';
    await page.locator('[data-act="recall-refresh"]').click();
    await page.getByText('暂无召回记录', { exact: true }).waitFor();
    mode = 'normal';
    await page.locator('[data-act="recall-refresh"]').click();
    await page.locator('#recall-list .mem-card').first().waitFor();
    results.push('read failure, retry and empty states');

    delay = 300;
    await page.locator('[data-act="recall-refresh"]').click();
    await page.locator('[data-view="requests"]').click();
    await page.waitForTimeout(450);
    assert.equal(await page.locator('#recall-list').count(), 0);
    results.push('late recall response does not overwrite another view');
    delay = 0;
    await page.goto(base + '#/persona?tab=profile');
    await page.getByRole('button', { name: '审核申请', exact: true }).waitFor();
    assert.equal(await page.locator('[data-view="recall"]').count(), 0);
    results.push('profile browser has no recall history tab');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + '#/memories?view=recall');
    await page.locator('#recall-list .mem-card').first().waitFor();
    await page.locator('#recall-list .mem-card').first().click();
    await page.waitForTimeout(350);
    await page.screenshot({ path: path.join(output, 'mobile.png'), fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    results.push('390px mobile detail and no horizontal overflow');
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ results, errors }, null, 2));
    process.stdout.write(JSON.stringify({ results, errors }) + '\n');
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
