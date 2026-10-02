// Real browser regression for shared controls and all migrated page entrances.
// Uses only a local static server and intercepted, in-memory API fixtures.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const os = require('node:os');
const { chromium } = require('playwright');

const repo = path.resolve(__dirname, '..');
const evidenceDir = path.resolve(process.env.QI_FE_EVIDENCE_DIR || path.join(os.tmpdir(), 'qi-frontend-controls-evidence'));
const version = fs.readFileSync(path.join(repo, 'admin/js/ui.js'), 'utf8').match(/ASSET_VERSION = '([^']+)'/)[1];
const mime = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.png': 'image/png', '.svg': 'image/svg+xml', '.wav': 'audio/wav' };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1');
  if (url.pathname === '/__frontend_controls__') {
    res.writeHead(200, { 'Content-Type': mime['.html'], 'Cache-Control': 'no-store' });
    res.end(`<!doctype html><html lang="zh-CN" data-theme="day"><head><meta name="viewport" content="width=device-width, initial-scale=1"><link rel="stylesheet" href="/admin/css/style.css?v=${version}"></head><body><main id="test-root"></main></body></html>`);
    return;
  }
  const file = path.resolve(repo, '.' + decodeURIComponent(url.pathname));
  if (!file.startsWith(path.join(repo, 'admin') + path.sep)) { res.writeHead(404); res.end(); return; }
  fs.readFile(file, (error, data) => {
    if (error) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, { 'Content-Type': mime[path.extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(data);
  });
});

async function runModalTests(browser, baseUrl) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, timezoneId: 'Asia/Shanghai' });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.stack));
  await page.goto(baseUrl + '/__frontend_controls__');
  await page.evaluate(async v => {
    window.ui = await import('/admin/js/ui.js?v=' + v);
    document.querySelector('#test-root').innerHTML = '<button id="opener">打开确认</button><button id="outside">背景按钮</button>';
    window.ui.initTooltips();
  }, version);
  const coverage = [];
  for (const exit of ['ok', 'cancel', 'close', 'backdrop', 'escape', 'removed']) {
    await page.locator('#opener').focus();
    await page.evaluate(() => {
      window.confirmResult = 'pending';
      window.resolveCount = 0;
      window.ui.confirm('确认操作回归测试').then(result => { window.confirmResult = result; window.resolveCount += 1; });
    });
    await page.locator('.modal').waitFor();
    assert.equal(await page.locator('.modal').getAttribute('aria-modal'), 'true');
    assert.equal(await page.locator('[data-cancel]').evaluate(el => document.activeElement === el), true);
    await page.locator('#outside').evaluate(el => el.focus());
    assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.modal'))), true);
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press(i % 2 ? 'Shift+Tab' : 'Tab');
      assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.modal'))), true);
    }
    if (exit === 'ok') await page.locator('[data-ok]').click();
    else if (exit === 'cancel') await page.locator('[data-cancel]').click();
    else if (exit === 'close') await page.locator('.modal-close').click();
    else if (exit === 'backdrop') await page.locator('.modal-mask').click({ position: { x: 2, y: 2 } });
    else if (exit === 'escape') await page.keyboard.press('Escape');
    else await page.locator('.modal-mask').evaluate(el => el.remove());
    await page.waitForFunction(() => window.confirmResult !== 'pending');
    assert.equal(await page.evaluate(() => window.confirmResult), exit === 'ok');
    assert.equal(await page.evaluate(() => window.resolveCount), 1);
    assert.equal(await page.locator('#opener').evaluate(el => document.activeElement === el), true);
    coverage.push('confirm/' + exit);
  }
  await page.evaluate(() => {
    document.querySelector('#opener').focus();
    window.parentModal = window.ui.modal({ title: '父弹窗', body: '<button id="nested-opener">打开子弹窗</button>' });
  });
  await page.locator('#nested-opener').focus();
  await page.evaluate(() => window.childModal = window.ui.modal({ title: '子弹窗', body: '<input id="child-input" aria-label="内容">' }));
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.modal').count(), 1);
  assert.equal(await page.locator('#nested-opener').evaluate(el => document.activeElement === el), true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('.modal').count(), 0);
  coverage.push('modal/nested-focus-return');
  for (const state of ['hidden', 'inert', 'disabled']) {
    await page.locator('#opener').focus();
    await page.evaluate(() => window.parentModal = window.ui.modal({ title: '父弹窗', body: '<button id="nested-opener">打开子弹窗</button><button id="parent-fallback">继续编辑</button>' }));
    await page.locator('#nested-opener').focus();
    await page.evaluate(() => window.childModal = window.ui.modal({ title: '子弹窗', body: '<input aria-label="内容">' }));
    await page.evaluate(property => {
      document.getElementById('nested-opener')[property] = true;
      window.childModal.close();
    }, state);
    assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.modal'))), true);
    await page.keyboard.press('Escape');
    coverage.push(`modal/${state}-opener/parent-fallback`);
  }
  await page.evaluate(() => {
    document.querySelector('#test-root').insertAdjacentHTML('beforeend', '<h1 id="page-title">页面标题</h1>');
    document.querySelector('#opener').focus();
    window.lastModal = window.ui.modal({ title: '顶层弹窗', body: '<button>操作</button>' });
    document.querySelector('#opener').hidden = true;
    window.lastModal.close();
  });
  assert.equal(await page.locator('#page-title').evaluate(el => document.activeElement === el), true);
  await page.locator('#opener').evaluate(el => el.hidden = false);
  coverage.push('modal/hidden-opener/page-title-fallback');
  for (const theme of ['day', 'night']) {
    await page.evaluate(t => {
      document.documentElement.dataset.theme = t;
      document.querySelector('#opener').dataset.tooltip = '复古提示，支持键盘聚焦';
    }, theme);
    // Re-enter with the pointer; hovering an already hovered element emits no event.
    await page.locator('#outside').hover();
    await page.locator('#opener').hover();
    assert.equal(await page.locator('.retro-tooltip').isVisible(), true);
    await page.screenshot({ path: path.join(evidenceDir, `tooltip-${theme}.png`) });
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.retro-tooltip').isVisible(), false);
    await page.locator('#outside').focus();
    await page.locator('#opener').focus();
    assert.equal(await page.locator('.retro-tooltip').isVisible(), true);
    assert.equal(await page.locator('#opener').getAttribute('aria-describedby'), 'retro-tooltip');
    await page.locator('#outside').focus();
    assert.equal(await page.locator('#opener').getAttribute('aria-describedby'), null);
    coverage.push(`tooltip/${theme}/pointer-focus-escape`);
  }
  await page.evaluate(() => {
    document.querySelector('#test-root').insertAdjacentHTML('beforeend', '<div class="field"><label for="numeric-integer">整数范围</label><input id="numeric-integer" type="number" min="1" max="10" step="1" value="5"></div><div class="field"><label for="numeric-decimal">小数精度</label><input id="numeric-decimal" type="number" min="0" max="1" step="0.1" value="0.5"></div>');
  });
  for (const theme of ['day', 'night']) {
    await page.evaluate(t => document.documentElement.dataset.theme = t, theme);
    for (const state of ['default', 'hover', 'focus', 'disabled']) {
      const integer = page.locator('#numeric-integer');
      await integer.evaluate(el => { el.disabled = false; el.blur(); });
      await page.locator('#outside').hover();
      if (state === 'hover') await integer.hover();
      if (state === 'focus') await integer.focus();
      if (state === 'disabled') await integer.evaluate(el => el.disabled = true);
      const appearance = await integer.evaluate(el => ({
        field: getComputedStyle(el).appearance,
        spinRule: Array.from(document.styleSheets).flatMap(sheet => Array.from(sheet.cssRules))
          .some(rule => rule.selectorText?.includes('input[type="number"]::-webkit-inner-spin-button')
            && rule.selectorText.includes('input[type="number"]::-webkit-outer-spin-button')
            && rule.style.getPropertyValue('-webkit-appearance') === 'none'),
      }));
      assert.equal(appearance.field, 'textfield');
      // Chromium does not expose the internal spinner's computed style reliably.
      assert.equal(appearance.spinRule, true);
      coverage.push(`number/${theme}/${state}/no-system-spinner`);
    }
    await page.locator('#numeric-integer').evaluate(el => el.disabled = false);
    await page.locator('#numeric-integer').fill('5');
    await page.locator('#numeric-integer').press('ArrowUp');
    assert.equal(await page.locator('#numeric-integer').inputValue(), '6');
    await page.locator('#numeric-integer').fill('11');
    assert.equal(await page.locator('#numeric-integer').evaluate(el => el.validity.rangeOverflow), true);
    await page.locator('#numeric-integer').fill('');
    assert.equal(await page.locator('#numeric-integer').evaluate(el => el.validity.valid), true);
    await page.locator('#numeric-decimal').fill('0.5');
    await page.locator('#numeric-decimal').press('ArrowUp');
    assert.equal(await page.locator('#numeric-decimal').inputValue(), '0.6');
    await page.locator('#numeric-decimal').fill('0.55');
    assert.equal(await page.locator('#numeric-decimal').evaluate(el => el.validity.stepMismatch), true);
    await page.screenshot({ path: path.join(evidenceDir, `number-fields-${theme}.png`) });
    coverage.push(`number/${theme}/keyboard-range-step-empty`);
  }
  assert.deepEqual(errors, []);
  await context.close();
  return coverage;
}

(async () => {
  fs.mkdirSync(evidenceDir, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const baseUrl = 'http://127.0.0.1:' + server.address().port;
  const executablePath = process.env.QI_FE_BROWSER_PATH || (process.platform === 'win32'
    ? path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Microsoft/Edge/Application/msedge.exe') : undefined);
  let browser;
  const report = { version, baseUrl, evidenceDir, suites: {}, errors: [] };
  try {
    browser = await chromium.launch({ headless: true, ...(executablePath ? { executablePath } : {}) });
    for (const [name, run] of [
      ['modal-tooltip', runModalTests],
      ['shared-fields', require('./frontend_shared.browser.cjs').runSharedTests],
      ['memory-fields', require('./frontend_memory.browser.cjs').runMemoryTests],
      ['planning-fields', require('./frontend_planning.browser.cjs').runPlanningTests],
    ]) {
      process.stdout.write(JSON.stringify({ suite: name, status: 'running' }) + '\n');
      try { report.suites[name] = await run(browser, baseUrl, evidenceDir); }
      catch (error) { report.errors.push({ suite: name, stack: error.stack }); }
    }
    if (report.errors.length) process.exitCode = 1;
  } catch (error) {
    report.errors.push({ suite: 'launch', stack: error.stack });
    process.exitCode = 1;
  } finally {
    fs.writeFileSync(path.join(evidenceDir, 'browser-report.json'), JSON.stringify(report, null, 2));
    process.stdout.write(JSON.stringify({ version, evidenceDir,
      suites: Object.fromEntries(Object.entries(report.suites).map(([name, result]) =>
        [name, Array.isArray(result) ? result.length : result.checks?.length])), errors: report.errors }, null, 2) + '\n');
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
})();
