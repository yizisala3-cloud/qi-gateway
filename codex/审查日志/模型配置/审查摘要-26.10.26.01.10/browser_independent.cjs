const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const base = 'http://127.0.0.1:8816';
const output = __dirname;
const headers = { Authorization: 'Bearer local-model-preview' };

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const results = [], pageErrors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    await context.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort());
    await context.addInitScript(() => localStorage.setItem('qi-token', 'local-model-preview'));
    const page = await context.newPage();
    page.on('pageerror', error => pageErrors.push(error.message));
    await page.goto(base + '/admin/index.html#/config?tab=api');
    await page.locator('[data-model-purpose="chat"]').waitFor();

    // Hold a purpose-open read, then leave only the API tab.
    let releaseRead, startedRead;
    const heldRead = new Promise(resolve => { releaseRead = resolve; });
    const readStarted = new Promise(resolve => { startedRead = resolve; });
    const holdRead = async route => {
      const response = await route.fetch();
      startedRead();
      await heldRead;
      await route.fulfill({ response });
    };
    await page.route('**/admin/api/model-config', holdRead);
    await page.locator('[data-model-purpose="chat"]').click();
    await readStarted;
    await page.getByRole('tab', { name: '常规配置', exact: true }).click();
    releaseRead();
    await page.getByRole('dialog').waitFor();
    const lateDialogs = await page.getByRole('dialog').count();
    results.push({ scenario: 'purpose read completes after leaving API tab',
      expected_dialogs: 0, actual_dialogs: lateDialogs,
      active_tab: await page.getByRole('tab', { selected: true }).innerText() });
    await page.screenshot({ path: path.join(output, 'late-read-general-tab.png'), fullPage: true });
    await page.getByRole('dialog').locator('[data-model-cancel]').click();
    await page.unroute('**/admin/api/model-config', holdRead);

    // Add one synthetic provider through the actual management API.
    let state = await (await context.request.get(base + '/admin/api/model-config', { headers })).json();
    state = await (await context.request.put(base + '/admin/api/model-config/providers', { headers,
      data: { revision: state.revision, value: { name: 'Review provider',
        base_url: 'https://synthetic.invalid/v1', models: ['review-runtime-model'],
        keys: [{ name: 'Review key', value: 'synthetic-browser-review-key' }] } } })).json();
    await page.getByRole('tab', { name: 'API配置', exact: true }).click();
    await page.locator('[data-model-purpose="chat"]').waitFor();
    await page.locator('[data-model-purpose="chat"]').click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('.retro-select-field').first().click();
    await page.getByRole('option', { name: 'Review provider', exact: true }).click();

    // The server commits, while its response is intentionally delayed.
    let releaseSave, startedSave;
    const heldSave = new Promise(resolve => { releaseSave = resolve; });
    const saveStarted = new Promise(resolve => { startedSave = resolve; });
    const holdSave = async route => {
      const response = await route.fetch();
      assert.equal(response.status(), 200);
      startedSave();
      await heldSave;
      await route.fulfill({ response });
    };
    await page.route('**/admin/api/model-config/routes/chat', holdSave);
    await dialog.locator('[data-model-save]').click();
    await saveStarted;
    await dialog.locator('[data-model-cancel]').click();
    const delivered = page.waitForResponse(base + '/admin/api/model-config/routes/chat');
    releaseSave();
    await delivered;
    await page.unroute('**/admin/api/model-config/routes/chat', holdSave);
    state = await (await context.request.get(base + '/admin/api/model-config', { headers })).json();
    // Give the application's async response handler a complete event-loop turn.
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const rowText = await page.locator('[data-model-purpose="chat"]').locator('..').locator('..').innerText();
    const actualModel = state.purposes.find(p => p.id === 'chat').current.model;
    await page.screenshot({ path: path.join(output, 'dismissed-save-stale-api.png'), fullPage: true });
    await page.getByRole('tab', { name: '常规配置', exact: true }).click();
    const overviewText = await page.locator('#config-section-1').innerText();
    await page.screenshot({ path: path.join(output, 'dismissed-save-stale-general.png'), fullPage: true });
    results.push({ scenario: 'close dialog after server commit but before save response',
      actual_server_model: actualModel, api_row_text: rowText,
      general_overview_text: overviewText,
      expected_both_views_include: actualModel,
      api_view_matches: rowText.includes(actualModel),
      general_view_matches: overviewText.includes(actualModel) });

    await fs.writeFile(path.join(output, 'browser-independent-results.json'),
      JSON.stringify({ results, page_errors: pageErrors, source: 'local synthetic fixture; real management backend' }, null, 2));
    process.stdout.write(JSON.stringify({ results, page_errors: pageErrors }, null, 2) + '\n');
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
