// Real admin modules in Edge, against the local synthetic preview server only.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const { server } = require('../tools/preview_gateway_logs.cjs');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const out = path.resolve(process.argv[2] || '.');
async function settle(page) {
  await page.evaluate(async () => {
    const finite = document.getAnimations().filter(a => Number.isFinite(a.effect.getComputedTiming().iterations));
    await Promise.all(finite.map(a => a.finished.catch(() => null)));
  });
}

async function verify() {
  await fs.mkdir(out, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const result = { data_source: 'local synthetic API; no production connection' };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${origin}/admin/index.html#/logs`);
    await page.locator('#log-list .mem-card').nth(7).waitFor();
    assert.equal(await page.locator('#log-list .mem-card').count(), 8);
    assert.ok(await page.getByText('日志收集已关闭', { exact: false }).count());
    await settle(page);
    await page.screenshot({ path: path.join(out, 'logs-desktop.png'), fullPage: true });
    await page.locator('#log-module').selectOption('chat');
    await page.waitForFunction(() => document.querySelectorAll('#log-list .mem-card').length === 1);
    assert.ok((await page.locator('#log-list').innerText()).includes('聊天上游'));
    await page.locator('#log-module').selectOption('');
    await page.waitForFunction(() => document.querySelectorAll('#log-list .mem-card').length === 8);
    await page.locator('#log-list [data-act="detail"]').first().click();
    await page.getByText('[TEST DATA]', { exact: false }).first().waitFor();
    await page.screenshot({ path: path.join(out, 'logs-detail.png'), fullPage: true });
    assert.equal(await page.locator('.modal').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    await page.locator('.modal [data-close]').click();
    await page.locator('#theme-btn').click();
    await page.screenshot({ path: path.join(out, 'logs-night.png'), fullPage: true });
    await page.locator('#theme-btn').click();

    await page.locator('#sidebar-nav a[data-key="config"]').click();
    const toggle = page.getByRole('switch', { name: '日志收集', exact: true });
    await toggle.waitFor();
    assert.equal(await toggle.getAttribute('aria-checked'), 'false');
    await toggle.click();
    await page.waitForFunction(() => document.querySelector('[data-act="toggle-log-collection"]').getAttribute('aria-checked') === 'true');
    await page.reload();
    await toggle.waitFor();
    assert.equal(await toggle.getAttribute('aria-checked'), 'true');
    await toggle.click();
    await page.waitForFunction(() => document.querySelector('[data-act="toggle-log-collection"]').getAttribute('aria-checked') === 'false');
    await settle(page);
    await page.screenshot({ path: path.join(out, 'config-switch.png'), fullPage: true });

    // Failed save must read back the unchanged server state and re-enable the button.
    await page.route('**/admin/api/logs/settings', async route => {
      if (route.request().method() === 'PUT') await route.fulfill({ status: 503, json: { error: 'synthetic save failure' } });
      else await route.continue();
    });
    await toggle.click();
    await page.getByText('synthetic save failure', { exact: false }).waitFor();
    assert.equal(await toggle.getAttribute('aria-checked'), 'false');
    assert.equal(await toggle.isEnabled(), true);
    await page.unroute('**/admin/api/logs/settings');

    // Saved response lost/truncated: read back what actually committed.
    await page.route('**/admin/api/logs/settings', async route => {
      if (route.request().method() === 'PUT') {
        await route.fetch();
        await route.fulfill({ status: 200, contentType: 'application/json', body: '{' });
      } else await route.continue();
    });
    await toggle.click();
    await page.waitForFunction(() => document.querySelector('[data-act="toggle-log-collection"]').getAttribute('aria-checked') === 'true');
    assert.equal(await toggle.isEnabled(), true);
    await page.unroute('**/admin/api/logs/settings');
    await toggle.click();
    await page.waitForFunction(() => document.querySelector('[data-act="toggle-log-collection"]').getAttribute('aria-checked') === 'false');

    // Settings read failure remains unknown and non-editable, rather than looking off.
    await page.route('**/admin/api/logs/settings', route => route.fulfill({ status: 503, json: { error: 'synthetic read failure' } }));
    await page.reload();
    await page.locator('#log-collection-tag').waitFor();
    assert.equal(await page.locator('#log-collection-tag').innerText(), '状态未知');
    assert.equal(await toggle.isEnabled(), false);
    await page.unroute('**/admin/api/logs/settings');

    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true });
    mobile.on('pageerror', error => errors.push(error.message));
    await mobile.goto(`${origin}/admin/index.html#/logs`);
    await mobile.locator('#log-list .mem-card').nth(7).waitFor();
    await settle(mobile);
    await mobile.screenshot({ path: path.join(out, 'logs-mobile.png'), fullPage: true });
    result.mobile_document_overflow = await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth);
    assert.equal(result.mobile_document_overflow, false);
    await mobile.locator('#log-list [data-act="detail"]').first().click();
    await mobile.getByText('[TEST DATA]', { exact: false }).first().waitFor();
    await mobile.screenshot({ path: path.join(out, 'logs-mobile-detail.png'), fullPage: true });
    assert.equal(await mobile.locator('.modal').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    result.checked = ['8 groups', 'module filtering', 'details', 'day/night', 'default off', 'save on/off', 'refresh persistence', 'failed save rollback', 'truncated save readback', 'read failure unknown', '390px list/details'];
    result.page_errors = errors;
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(out, 'log-settings-browser.json'), JSON.stringify(result, null, 2));
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
verify().catch(error => { process.stderr.write(error.stack + '\n'); server.close(); process.exitCode = 1; });
