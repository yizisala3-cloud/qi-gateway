const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:8844';
(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await context.addInitScript(() => localStorage.setItem('qi-token', 'local-model-preview'));
    const headers = { Authorization: 'Bearer local-model-preview' };
    const management = await context.request.get(base + '/admin/api/model-config', { headers });
    const status = await context.request.get(base + '/status', { headers });
    assert.equal(management.status(), 200);
    assert.equal(status.status(), 409);
    const page = await context.newPage();
    await page.goto(base + '/admin/index.html#/config?tab=api');
    await page.locator('#login-error').filter({ hasText: '409' }).waitFor();
    const result = {
      valid_management_api_status: management.status(), status_api_status: status.status(),
      chat: (await management.json()).purposes.find(p => p.id === 'chat'),
      login_visible: await page.locator('#login-page').isVisible(),
      api_tab_count: await page.getByRole('tab', { name: 'API配置', exact: true }).count(),
      token_after_boot: await page.evaluate(() => localStorage.getItem('qi-token')),
      login_error: await page.locator('#login-error').innerText(),
      expected: 'Authenticated user can open model configuration and repair invalid chat binding'
    };
    await page.screenshot({ path: path.join(__dirname, 'invalid-chat-blocks-console.png'), fullPage: true });
    // Re-entering the valid token is also rejected by the same non-auth failure.
    await page.locator('#login-input').fill('local-model-preview');
    const retried = page.waitForResponse(base + '/status');
    await page.locator('#login-btn').click();
    await retried;
    await page.locator('#login-error').filter({ hasText: '登录失败' }).waitFor();
    result.login_error_after_retry = await page.locator('#login-error').innerText();
    result.login_visible_after_retry = await page.locator('#login-page').isVisible();
    await fs.writeFile(path.join(__dirname, 'invalid-chat-results.json'), JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    assert.equal(result.api_tab_count, 1, 'BUG-03 remains: valid management token cannot reach repair UI');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
