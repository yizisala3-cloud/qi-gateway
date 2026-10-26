// Actual shell login regression; use isolated --invalid-purpose preview stores.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const bases = [process.argv[2] || 'http://127.0.0.1:8860', process.argv[3] || 'http://127.0.0.1:8861'];
const output = path.resolve(process.argv[4] || 'codex/临时验证/模型配置登录与缓存补修-20261026');
const token = 'local-model-preview';
const headers = { Authorization: 'Bearer ' + token };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const results = [], errors = [];
  await fs.mkdir(output, { recursive: true });
  try {
    const contextFor = async (base, saved) => {
      const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
      await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
      if (saved) await context.addInitScript(value => localStorage.setItem('qi-token', value), saved);
      const page = await context.newPage();
      page.on('pageerror', error => errors.push(error.message));
      return { context, page };
    };
    const enter = async (page, base) => page.goto(base + '/admin/index.html#/config?tab=api');
    const ready = async page => {
      await page.locator('[data-model-purpose="chat"]').waitFor();
      assert.equal(await page.locator('#login-page').isVisible(), false);
      assert.equal(await page.evaluate(() => localStorage.getItem('qi-token')), token);
    };
    for (const [index, base] of bases.entries()) {
      const { context, page } = await contextFor(base, token);
      const metadata = async () => (await context.request.get(base + '/admin/api/model-config', { headers })).json();
      const before = await metadata();
      const invalid = before.purposes.filter(p => p.current.available === false).map(p => p.id).sort();
      assert.deepEqual(invalid, index ? ['chat', 'qq', 'sky'] : ['chat']);
      assert.equal((await context.request.get(base + '/status', { headers })).status(), 409);
      assert.equal((await context.request.get(base + '/admin/api/auth', { headers })).status(), 200);
      await enter(page, base);
      await ready(page);
      await page.reload();
      await ready(page);
      assert.deepEqual((await metadata()).purposes.map(p => p.selection), before.purposes.map(p => p.selection));
      results.push({ scenario: 'saved token boot and refresh', invalid });
      const versions = await page.evaluate(() => ({
        css: new URL(document.querySelector('link[rel="stylesheet"]').href).searchParams.get('v'),
        script: new URL(document.querySelector('script[type="module"]').src).searchParams.get('v'),
        stylesheetLoaded: !!document.querySelector('link[rel="stylesheet"]').sheet,
      }));
      assert.equal(versions.css, versions.script);
      assert.equal(versions.stylesheetLoaded, true);
      await context.close();

      const manual = await contextFor(base);
      await enter(manual.page, base);
      await manual.page.locator('#login-page').waitFor();
      await manual.page.locator('#login-input').fill(token);
      await manual.page.locator('#login-btn').click();
      await ready(manual.page);
      await manual.page.getByRole('tab', { name: '常规配置', exact: true }).click();
      await manual.page.getByText(/配置状态读取失败：409/).first().waitFor();
      assert.equal(await manual.page.evaluate(() => localStorage.getItem('qi-token')), token);
      await manual.page.getByRole('tab', { name: 'API配置', exact: true }).click();
      await ready(manual.page);
      const state = async () => (await manual.context.request.get(base + '/admin/api/model-config', { headers })).json();
      assert.deepEqual((await state()).purposes.map(p => p.selection), before.purposes.map(p => p.selection));
      await manual.page.screenshot({ path: path.join(output, `invalid-${index ? 'chat-qq-sky' : 'chat'}-accessible.png`), fullPage: true });
      results.push({ scenario: 'manual login and visible business error', invalid, versions });
      // Restore chat last: each successful edit must leave the remaining bindings intact.
      for (const purpose of invalid.filter(p => p !== 'chat').concat('chat')) {
        const previous = await state();
        await manual.page.locator(`[data-model-purpose="${purpose}"]`).click();
        const dialog = manual.page.getByRole('dialog');
        await dialog.locator('[data-model-provider]').selectOption('', { force: true });
        const saved = manual.page.waitForResponse(r => r.url().endsWith('/routes/' + purpose) && r.request().method() === 'PUT');
        await dialog.getByRole('button', { name: '保存', exact: true }).click();
        assert.equal((await saved).status(), 200);
        await dialog.waitFor({ state: 'detached' });
        const current = await state();
        for (const p of current.purposes) {
          assert.deepEqual(p.selection, p.id === purpose ? null : previous.purposes.find(old => old.id === p.id).selection);
        }
        assert.equal((await manual.context.request.get(base + '/status', { headers })).status(), purpose === 'chat' ? 200 : 409);
      }
      await manual.page.reload();
      await ready(manual.page);
      assert.equal((await state()).purposes.some(p => p.current.available === false), false);
      results.push({ scenario: 'independent repair and reload', invalid });
      await manual.context.close();
    }

    const base = bases[0];
    for (const saved of [undefined, 'wrong-token']) {
      const { context, page } = await contextFor(base, saved);
      await enter(page, base);
      if (saved) await page.locator('#login-error').filter({ hasText: '401' }).waitFor();
      else await page.locator('#login-page').waitFor();
      assert.equal(await page.locator('#layout').isVisible(), false);
      assert.equal(await page.evaluate(() => localStorage.getItem('qi-token')), null);
      await page.locator('#login-input').fill('wrong-token');
      await page.locator('#login-btn').click();
      await page.locator('#login-error').filter({ hasText: '登录失败' }).waitFor();
      assert.equal(await page.evaluate(() => localStorage.getItem('qi-token')), null);
      results.push({ scenario: saved ? 'invalid token rejected' : 'missing token rejected' });
      await context.close();
    }
    for (const failure of ['503', '409', '403', 'network', 'malformed']) {
      const { context, page } = await contextFor(base, token);
      await page.route('**/admin/api/auth', route => failure === 'network' ? route.abort() : route.fulfill({
        status: failure === 'malformed' ? 200 : Number(failure), contentType: 'application/json',
        body: JSON.stringify(failure === 'malformed' ? {} : { error: 'Synthetic authentication failure' }),
      }));
      await enter(page, base);
      await page.locator('#login-error').filter({ hasText: failure === '403' ? 'Token 被拒绝' : '暂时无法验证登录' }).waitFor();
      assert.equal(await page.locator('#layout').isVisible(), false);
      assert.equal(await page.evaluate(() => localStorage.getItem('qi-token')), failure === '403' ? null : token);
      results.push({ scenario: 'authentication failure handling', failure });
      await context.close();
    }
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'console-auth-browser-results.json'), JSON.stringify({ results, errors }, null, 2));
    process.stdout.write(JSON.stringify({ passed: results.length, errors }, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
