const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:8874';
const token = 'local-model-preview';
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await context.addInitScript(value => localStorage.setItem('qi-token', value), token);
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let authCalls = 0;
    await page.route(base + '/admin/api/auth', route => ++authCalls === 1
      ? route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"synthetic temporary failure"}' })
      : route.continue());
    // A separate business failure remains visible even after identity recovery.
    await page.route(base + '/status', route => route.fulfill({ status: 409, contentType: 'application/json',
      body: '{"error":"synthetic model selection unavailable"}' }));
    await page.goto(base + '/admin/index.html#/config?tab=api');
    await page.locator('#login-error').filter({ hasText: '暂时无法验证登录' }).waitFor();
    assert.equal(await page.locator('#layout').isVisible(), false);
    assert.equal(await page.evaluate(() => localStorage.getItem('qi-token')), token);
    await page.locator('#login-input').fill(token);
    await page.locator('#login-btn').click();
    await page.locator('[data-model-purpose="chat"]').waitFor();
    assert.equal(await page.locator('#login-page').isVisible(), false);
    await page.getByRole('tab', { name: '常规配置', exact: true }).click();
    await page.getByText(/配置状态读取失败：409/).first().waitFor();
    assert.equal(await page.evaluate(() => localStorage.getItem('qi-token')), token);
    assert.equal(authCalls, 2);
    assert.deepEqual(errors, []);
    const result = { scenario: '503 identity failure retains token; manual retry succeeds despite independent status 409',
      auth_calls: authCalls, token_retained: true, business_error_visible: true, page_errors: errors };
    await fs.writeFile(path.join(__dirname, 'auth-retry-results.json'), JSON.stringify(result, null, 2));
    await page.screenshot({ path: path.join(__dirname, 'auth-retry-general.png'), fullPage: true });
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
