// Boundary regression UI tests against the isolated preview fixture only.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = process.argv[2] || 'http://127.0.0.1:8820';
const output = path.resolve(process.argv[3] || 'codex/临时验证/模型配置五项补修-20261026');
const headers = { Authorization: 'Bearer local-model-preview' };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const results = [], errors = [];
  await fs.mkdir(output, { recursive: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await context.addInitScript(() => localStorage.setItem('qi-token', 'local-model-preview'));
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    const state = async () => (await context.request.get(base + '/admin/api/model-config', { headers })).json();
    let visits = 0;
    const enter = async () => {
      const loaded = page.waitForResponse(r => new URL(r.url()).pathname === '/admin/api/model-config' && r.request().method() === 'GET');
      await page.goto(base + '/admin/index.html#/config?tab=api&probe=' + (++visits));
      await loaded;
      await page.locator('[data-model-purpose="chat"]').waitFor();
    };
    const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const row = purpose => page.locator(`[data-model-purpose="${purpose}"]`).locator('..').locator('..');

    for (const mode of ['general', 'api-general-api', 'navigate']) {
      process.stdout.write('read scenario: ' + mode + '\n');
      await enter();
      const release = deferred(), started = deferred(), handled = deferred();
      let once = true;
      const hold = async route => {
        if (!once) return route.continue();
        once = false;
        const response = await route.fetch();
        started.resolve();
        await release.promise;
        try { await route.fulfill({ response }); }
        catch (error) { if (!/abort|closed|handled|intercept/i.test(error.message)) throw error; }
        finally { handled.resolve(); }
      };
      await page.route('**/admin/api/model-config', hold);
      await page.locator('[data-model-purpose="chat"]').click();
      await started.promise;
      if (mode === 'navigate') await page.goto(base + '/admin/index.html#/digest');
      else {
        await page.getByRole('tab', { name: '常规配置', exact: true }).click();
        if (mode === 'api-general-api') {
          await page.getByRole('tab', { name: 'API配置', exact: true }).click();
          await page.locator('[data-model-purpose="chat"]').waitFor();
        }
      }
      release.resolve();
      await handled.promise;
      await frames();
      assert.equal(await page.getByRole('dialog').count(), 0, mode + ' must discard the old open action');
      await page.unroute('**/admin/api/model-config', hold);
      results.push('late read discarded: ' + mode);
    }

    let current = await state();
    const created = await context.request.put(base + '/admin/api/model-config/providers', { headers, data: {
      revision: current.revision, value: { name: 'Boundary fixture', base_url: 'https://boundary.invalid/v1',
        models: ['boundary-a', 'boundary-b'], keys: [{ name: 'Boundary key', value: 'opaque_boundary_fixture_987654' }] } } });
    assert.equal(created.status(), 200);
    current = await created.json();
    const provider = current.providers.find(p => p.name === 'Boundary fixture');

    for (const mode of ['cancel', 'close', 'Escape', 'mask', 'general-pending', 'remount-before-commit', 'unknown-response', 'out-of-order']) {
      process.stdout.write('save scenario: ' + mode + '\n');
      await enter();
      current = await state();
      const target = current.purposes.find(p => p.id === 'chat').current.model === 'boundary-a' ? 'boundary-b' : 'boundary-a';
      await page.locator('[data-model-purpose="chat"]').click();
      const dialog = page.getByRole('dialog');
      await dialog.locator('.retro-select-field').nth(0).click();
      await page.getByRole('option', { name: 'Boundary fixture', exact: true }).click();
      await dialog.locator('.retro-select-field').nth(2).click();
      await page.getByRole('option', { name: target, exact: true }).click();
      const release = deferred(), started = deferred(), delivered = deferred();
      const hold = async route => {
        let response;
        if (mode !== 'remount-before-commit') response = await route.fetch();
        started.resolve();
        await release.promise;
        if (!response) response = await route.fetch();
        assert.equal(response.status(), 200);
        try {
          if (mode === 'unknown-response') await route.fulfill({ status: 200, contentType: 'application/json', body: '{' });
          else await route.fulfill({ response });
        } finally { delivered.resolve(); }
      };
      await page.route('**/admin/api/model-config/routes/chat', hold);
      await dialog.locator('[data-model-save]').click();
      await started.promise;
      if (mode === 'close') await dialog.locator('.modal-close').click();
      else if (mode === 'Escape') await page.keyboard.press('Escape');
      else if (mode === 'mask') await page.locator('.modal-mask').click({ position: { x: 4, y: 4 } });
      else if (mode === 'remount-before-commit') {
        await page.goto(base + '/admin/index.html#/digest');
        await enter();
      } else await dialog.locator('[data-model-cancel]').click();
      if (mode === 'general-pending') await page.getByRole('tab', { name: '常规配置', exact: true }).click();
      assert.match(await page.locator('[data-model-feedback]').innerText(), /保存|核对/);

      if (mode === 'out-of-order') {
        current = await state();
        await context.request.put(base + '/admin/api/model-config/routes/qq', { headers, data: {
          revision: current.revision, value: { provider_id: provider.id, key_id: provider.keys[0].id, model: target } } });
        await page.locator('[data-model-refresh]').click();
        await row('qq').filter({ hasText: target }).waitFor();
      }
      release.resolve();
      await delivered.promise;
      await page.unroute('**/admin/api/model-config/routes/chat', hold);
      if (mode === 'general-pending') await page.locator('#config-section-1').filter({ hasText: target }).waitFor();
      else await row('chat').filter({ hasText: target }).waitFor();
      if (mode === 'unknown-response') assert.match(await page.locator('[data-model-feedback]').innerText(), /未确认/);
      if (mode === 'out-of-order') assert.match(await row('qq').innerText(), new RegExp(target));
      current = await state();
      assert.equal(current.purposes.find(p => p.id === 'chat').current.model, target);
      if (mode !== 'general-pending') await page.getByRole('tab', { name: '常规配置', exact: true }).click();
      await page.locator('#config-section-1').filter({ hasText: target }).waitFor();
      results.push('closed/pending save reconciled: ' + mode);
    }
    assert.deepEqual(errors, []);
    await page.screenshot({ path: path.join(output, 'closed-save-general-fixed.png'), fullPage: true });
    await enter();
    await page.screenshot({ path: path.join(output, 'purpose-list-fixed.png'), fullPage: true });
    await fs.writeFile(path.join(output, 'browser-fixes-results.json'), JSON.stringify({ results, page_errors: errors, source: 'isolated synthetic data, real management backend' }, null, 2));
    process.stdout.write(JSON.stringify({ scenarios: results.length, page_errors: errors.length }) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
