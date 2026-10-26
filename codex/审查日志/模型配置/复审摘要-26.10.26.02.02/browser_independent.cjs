const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = 'http://127.0.0.1:8842';
const headers = { Authorization: 'Bearer local-model-preview' };
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const checks = [], errors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await context.addInitScript(() => localStorage.setItem('qi-token', 'local-model-preview'));
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    const state = async () => (await context.request.get(base + '/admin/api/model-config', { headers })).json();
    const frames = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    let snapshot = await state();
    const added = await context.request.put(base + '/admin/api/model-config/providers', { headers, data: {
      revision: snapshot.revision, value: { name: 'Independent fixture', base_url: 'https://independent.invalid/v1',
        models: ['independent-a', 'independent-b'], keys: [{ name: 'Synthetic', value: 'opaque_independent_browser_123456' }] } } });
    assert.equal(added.status(), 200);
    const addedProvider = (await added.json()).providers.filter(p => p.name === 'Independent fixture').at(-1);

    async function edit(model) {
      await page.locator('[data-model-purpose="chat"]').click();
      const dialog = page.getByRole('dialog');
      await dialog.locator('.retro-select-field').nth(0).click();
      await page.locator(`[role="option"][data-value="${addedProvider.id}"]`).click();
      await dialog.locator('.retro-select-field').nth(2).click();
      await page.getByRole('option', { name: model, exact: true }).click();
      return dialog;
    }

    // A mount's old status response must not overwrite a newer post-save overview.
    const oldStarted = deferred(), releaseOld = deferred(), oldHandled = deferred();
    let first = true;
    const oldStatus = async route => {
      if (!first) return route.continue();
      first = false;
      const response = await route.fetch();
      oldStarted.resolve();
      await releaseOld.promise;
      try { await route.fulfill({ response }); } finally { oldHandled.resolve(); }
    };
    // Authenticate before intercepting the config mount's status read.
    await page.goto(base + '/admin/index.html#/digest');
    await page.locator('#view-continuity').waitFor();
    await page.route(base + '/status', oldStatus);
    await page.goto(base + '/admin/index.html#/config?tab=api&independent=1');
    await page.locator('[data-model-purpose="chat"]').waitFor();
    await oldStarted.promise;
    let dialog = await edit('independent-a');
    await dialog.locator('[data-model-save]').click();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByRole('tab', { name: '常规配置', exact: true }).click();
    await page.locator('#config-section-1').filter({ hasText: 'independent-a' }).waitFor();
    releaseOld.resolve();
    await oldHandled.promise;
    await frames();
    assert.match(await page.locator('#config-section-1').innerText(), /independent-a/);
    await page.unroute(base + '/status', oldStatus);
    checks.push('late initial status cannot replace the post-save overview');

    // Unknown write result finishes after remounting directly into the general tab.
    await page.getByRole('tab', { name: 'API配置', exact: true }).click();
    await page.locator('[data-model-purpose="chat"]').waitFor();
    dialog = await edit('independent-b');
    const writeStarted = deferred(), releaseWrite = deferred(), writeHandled = deferred();
    const delayedWrite = async route => {
      writeStarted.resolve();
      await releaseWrite.promise;
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      try { await route.fulfill({ status: 200, contentType: 'application/json', body: '{' }); }
      finally { writeHandled.resolve(); }
    };
    await page.route('**/admin/api/model-config/routes/chat', delayedWrite);
    await dialog.locator('[data-model-save]').click();
    await writeStarted.promise;
    await page.goto(base + '/admin/index.html#/digest');
    await page.locator('#view-continuity').waitFor();
    await page.goto(base + '/admin/index.html#/config?independent=2');
    await page.locator('#config-section-1').filter({ hasText: 'independent-a' }).waitFor();
    releaseWrite.resolve();
    await writeHandled.promise;
    await page.locator('#config-section-1').filter({ hasText: 'independent-b' }).waitFor();
    await page.locator('[data-model-feedback]').filter({ hasText: '未确认' }).waitFor();
    snapshot = await state();
    assert.equal(snapshot.purposes.find(p => p.id === 'chat').current.model, 'independent-b');
    await page.unroute('**/admin/api/model-config/routes/chat', delayedWrite);
    checks.push('unknown result after remount reconciles general overview and retains honest feedback');
    await page.screenshot({ path: path.join(__dirname, 'unknown-remount-general.png'), fullPage: true });
    assert.deepEqual(errors, []);
    const result = { checks, page_errors: errors, source: 'independent synthetic fixture; real backend' };
    await fs.writeFile(path.join(__dirname, 'browser-independent-results.json'), JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
