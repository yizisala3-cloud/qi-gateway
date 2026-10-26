// Run against tools/preview_model_config.py only. Synthetic credentials and storage.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const base = process.argv[2] || 'http://127.0.0.1:8797';
const output = path.resolve(process.argv[3] || 'codex/临时验证/新增配置切换-20261026');
const headers = { Authorization: 'Bearer local-model-preview' };

(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const errors = [], checks = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await context.addInitScript(() => localStorage.setItem('qi-token', 'local-model-preview'));
    const page = await context.newPage();
    const clearToasts = () => page.waitForFunction(() => !document.querySelector('.toast'));
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(base + '/admin/index.html#/config');
    await page.getByRole('tab', { name: '常规配置' }).waitFor();
    await page.locator('[data-act="cred-reveal"][data-cred="memory_plugin"]').click();
    await page.locator('[data-cred-value="memory_plugin"]').filter({ hasText: 'preview-synthetic-plugin-token' }).waitFor();
    await page.getByRole('tab', { name: 'API配置' }).click();
    await page.locator('[data-model-purpose="chat"]').waitFor();
    assert.equal((await page.content()).includes('preview-synthetic-plugin-token'), false);
    assert.equal(await page.locator('[data-model-purpose]').count(), 6);
    assert.equal(await page.locator('[data-model-purpose="vector"]').count(), 0);
    checks.push('two tabs; six independent text purposes; embedding has no switch');
    checks.push('tab switching clears revealed plugin values from the DOM');

    await page.locator('[data-model-new]').click();
    let dialog = page.getByRole('dialog');
    await dialog.locator('[data-provider-name]').fill('共享测试供应商');
    await dialog.locator('[data-provider-url]').fill('https://synthetic.invalid/v1');
    await dialog.locator('[data-provider-models]').fill('model-a\nmodel-b');
    await dialog.locator('[data-key-alias]').fill('个人账号');
    await dialog.locator('[data-key-secret]').fill('synthetic-browser-key-a');
    await dialog.locator('[data-provider-add-key]').click();
    await dialog.locator('[data-key-alias]').nth(1).fill('备用账号');
    await dialog.locator('[data-key-secret]').nth(1).fill('synthetic-browser-key-b');
    await dialog.locator('[data-model-save]').click();
    await dialog.waitFor({ state: 'hidden' });
    checks.push('add provider, two named keys, two models through real management API');

    const choose = async (dialog, index, label) => {
      await dialog.locator('.retro-select-field').nth(index).click();
      await page.getByRole('option', { name: label, exact: false }).click();
    };
    const selected = {};
    for (const [index, purpose] of ['chat', 'qq', 'sky', 'analysis', 'continuity', 'rumination'].entries()) {
      await page.locator(`[data-model-purpose="${purpose}"]`).click();
      dialog = page.getByRole('dialog');
      await choose(dialog, 0, '共享测试供应商');
      // Open and verify the shared paper dropdown; key/model changes stay pending until save.
      await choose(dialog, 1, index % 2 ? '备用账号' : '个人账号');
      await choose(dialog, 2, index % 2 ? 'model-b' : 'model-a');
      const before = await (await context.request.get(base + '/admin/api/model-config', { headers })).json();
      selected[purpose] = before.purposes.find(p => p.id === purpose).current.model;
      await dialog.locator('[data-model-save]').click();
      await dialog.waitFor({ state: 'hidden' });
    }
    const snapshot = await (await context.request.get(base + '/admin/api/model-config', { headers })).json();
    assert.equal(snapshot.purposes.find(p => p.id === 'chat').current.model, 'model-a');
    assert.equal(snapshot.purposes.find(p => p.id === 'qq').current.model, 'model-b');
    assert.equal(snapshot.vector.model, 'Qwen/Qwen3-Embedding-0.6B');
    assert.equal(snapshot.vector.base_url, 'https://example.invalid/v1');
    assert.equal(JSON.stringify(snapshot).includes('synthetic-browser-key'), false);
    checks.push('same provider selectable everywhere; independent keys/models; vector configuration unchanged');

    await clearToasts();
    await page.screenshot({ path: path.join(output, 'api-desktop.png'), fullPage: true });
    await page.locator('[data-model-purpose="chat"]').click();
    dialog = page.getByRole('dialog');
    await dialog.locator('.retro-select-field').first().click();
    await page.screenshot({ path: path.join(output, 'dropdown-desktop.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await dialog.locator('[data-model-cancel]').click();
    await page.locator('[data-model-view="providers"]').click();
    await page.locator('[data-model-edit]').click();
    dialog = page.getByRole('dialog');
    assert.equal(await dialog.locator('[data-key-secret]').first().inputValue(), '');
    await dialog.locator('[data-provider-name]').fill('共享测试供应商（已编辑）');
    await dialog.locator('[data-model-save]').click();
    await dialog.waitFor({ state: 'hidden' });
    checks.push('editing a provider preserves existing hidden keys');

    // A genuine conflicting write must display an error and keep the editor draft.
    await page.locator('[data-model-edit]').click();
    dialog = page.getByRole('dialog');
    await dialog.locator('[data-provider-name]').fill('尚未保存的草稿');
    const latest = await (await context.request.get(base + '/admin/api/model-config', { headers })).json();
    await context.request.put(base + '/admin/api/model-config/routes/sky', { headers, data: { revision: latest.revision, value: null } });
    await dialog.locator('[data-model-save]').click();
    await dialog.locator('[data-model-error]').filter({ hasText: '其他页面' }).waitFor();
    assert.equal(await dialog.locator('[data-provider-name]').inputValue(), '尚未保存的草稿');
    await dialog.locator('[data-model-cancel]').click();
    checks.push('stale save rejected; draft retained; no false success');

    await clearToasts();
    await page.getByRole('tab', { name: '常规配置' }).click();
    await page.locator('[data-config-panel="general"]').waitFor();
    await page.screenshot({ path: path.join(output, 'general-desktop.png'), fullPage: true });
    await page.goto(base + '/admin/index.html#/digest');
    await page.locator('a[href="#/config?tab=api&purpose=continuity"]').click();
    await page.getByRole('dialog').filter({ hasText: '连续感总结' }).waitFor();
    await page.getByRole('dialog').locator('[data-model-cancel]').click();
    checks.push('summary shortcut opens its matching shared configuration dialog');

    let releaseRead, readStarted;
    const heldRead = new Promise(resolve => { releaseRead = resolve; });
    const startedRead = new Promise(resolve => { readStarted = resolve; });
    const hold = async route => {
      readStarted();
      await heldRead;
      try { await route.continue(); } catch (error) {
        if (!/closed|abort|handled|intercept/i.test(error.message)) throw error;
      }
    };
    await page.route('**/admin/api/model-config', hold);
    await page.locator('[data-model-purpose="chat"]').click();
    await startedRead;
    await page.goto(base + '/admin/index.html#/digest');
    releaseRead();
    await page.unroute('**/admin/api/model-config', hold);
    await page.locator('#view-continuity').waitFor();
    assert.equal(await page.getByRole('dialog').count(), 0);
    checks.push('late configuration reads cannot reopen a dialog after navigation');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(base + '/admin/index.html#/config?tab=api');
    await page.locator('[data-model-purpose="chat"]').waitFor();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: path.join(output, 'api-mobile.png'), fullPage: true });
    await page.locator('[data-model-purpose="qq"]').click();
    dialog = page.getByRole('dialog');
    await dialog.locator('.retro-select-field').first().click();
    await page.screenshot({ path: path.join(output, 'dropdown-mobile.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await dialog.locator('[data-model-cancel]').click();
    await page.evaluate(() => document.documentElement.dataset.theme = 'night');
    await page.screenshot({ path: path.join(output, 'api-night-mobile.png'), fullPage: true });
    checks.push('390px layout, custom dropdown open states, day/night themes');
    const storage = await page.evaluate(() => JSON.stringify({ local: localStorage, session: sessionStorage }));
    assert.equal(storage.includes('synthetic-browser-key'), false);
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'browser-results.json'), JSON.stringify({ checks, page_errors: errors, data_source: 'local synthetic fixture; actual management backend' }, null, 2));
    process.stdout.write(JSON.stringify({ checks: checks.length, page_errors: errors.length }) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
