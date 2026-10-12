// 真实 admin 页面 + 本地合成 API：统一下拉、动态表单与键盘回归。
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const { server } = require('../tools/preview_gateway_logs.cjs');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const out = path.resolve(process.argv[2] || '.');

async function open(page, selector) {
  await page.locator(selector).locator('..').locator(':scope > .retro-select-field').click();
  await page.locator('.retro-select-pop').waitFor();
}
async function choose(page, selector, value) {
  await open(page, selector);
  await page.locator(`.retro-select-pop [data-value="${value}"]`).click();
}
async function menuFits(page) {
  const bounds = await page.locator('.retro-select-pop').boundingBox();
  const viewport = page.viewportSize();
  assert.ok(bounds.x >= 7 && bounds.y >= 7 && bounds.x + bounds.width <= viewport.width - 7 && bounds.y + bounds.height <= viewport.height - 7);
  assert.equal(await page.locator('.retro-select-pop [aria-selected="true"]').count(), 1);
}

async function verify() {
  await fs.mkdir(out, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const result = { data_source: 'local synthetic API only', checked: [] };
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${origin}/admin/index.html#/logs`);
    await page.locator('#log-list .mem-card').nth(7).waitFor();
    for (const id of ['log-preset', 'log-module', 'log-level', 'log-key']) {
      await open(page, `#${id}`);
      await menuFits(page);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator(`#${id}`).locator('..').locator('button').evaluate(el => el === document.activeElement), true);
    }
    await open(page, '#log-preset');
    await page.screenshot({ path: path.join(out, 'select-day.png') });
    await page.keyboard.press('End');
    assert.equal(await page.locator(':focus').getAttribute('data-value'), 'custom');
    await page.keyboard.press('Home');
    assert.equal(await page.locator(':focus').getAttribute('data-value'), '1h');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#log-preset').inputValue(), '24h');
    await open(page, '#log-preset');
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('#log-preset').inputValue(), '7d');
    await choose(page, '#log-preset', '24h');
    await open(page, '#log-preset');
    await page.keyboard.press('Tab');
    assert.equal(await page.locator(':focus').getAttribute('data-select-for'), 'log-module');
    await choose(page, '#log-module', 'chat');
    await page.waitForFunction(() => document.querySelectorAll('#log-list .mem-card').length === 1);
    await choose(page, '#log-module', '');
    await page.waitForFunction(() => document.querySelectorAll('#log-list .mem-card').length === 8);
    await page.locator('#theme-btn').click();
    await open(page, '#log-preset');
    await page.screenshot({ path: path.join(out, 'select-night.png') });
    await page.keyboard.press('Escape');
    await page.locator('#theme-btn').click();
    result.checked.push('four log filters', 'day/night', 'Home/End/Arrows/Enter/Escape/Tab', 'module filtering');

    await page.evaluate(async () => {
      const { initRetroSelects } = await import('/admin/js/lib/retro_select.js?v=20261012-unified-selects');
      const before = document.querySelectorAll('.retro-select-field').length;
      initRetroSelects(); initRetroSelects();
      if (document.querySelectorAll('.retro-select-field').length !== before) throw Error('Duplicate select initialization');
    });
    await open(page, '#log-preset');
    await page.goto(`${origin}/admin/index.html#/planning`);
    await page.locator('[data-act="plan-tab"][data-tab="all"]').click();
    assert.equal(await page.locator('.retro-select-pop').count(), 0);
    for (const id of ['planning-filter-type', 'planning-filter-status']) {
      await open(page, `#${id}`);
      await page.keyboard.press('End');
      await page.keyboard.press('Enter');
      assert.notEqual(await page.locator(`#${id}`).inputValue(), '');
    }
    await page.locator('[data-act="clear-filters"]').click();
    assert.equal(await page.locator('#planning-filter-type').inputValue(), '');
    assert.equal(await page.locator('#planning-filter-status').inputValue(), '');
    assert.equal(await page.locator('[data-select-for="planning-filter-type"] .retro-select-text').innerText(), '全部');
    await open(page, '#planning-filter-type');
    await page.screenshot({ path: path.join(out, 'select-planning.png') });
    await page.keyboard.press('Escape');
    result.checked.push('planning filters and clear', 'route cleanup/idempotent initialization');

    // 真实共享浏览器的两个视图、审核编辑和六种动态记忆表单。
    const request = { id: 41, status: 'pending', title: 'TEST DATA', content: 'synthetic request', continuity_type: 'profile',
      continuity_data: { facet: 'test', statement: 'test', scope: 'test', stability: 'stable', basis: 'explicit_self_report' },
      evidence_message_ids: [], tags: [], recall_tags: [], importance: 5, created_at: new Date().toISOString() };
    await page.route('**/admin/api/data/memory_requests*', route => route.fulfill({ json: { data: [request], count: 1 } }));
    await page.goto(`${origin}/admin/index.html#/memories`);
    for (const id of ['lib-type', 'lib-sort']) {
      await open(page, `#${id}`); await page.keyboard.press('End'); await page.keyboard.press('Enter');
    }
    await page.locator('[data-act="view"][data-view="requests"]').click();
    for (const id of ['req-status', 'req-type']) {
      await open(page, `#${id}`); await page.keyboard.press('Escape');
    }
    await page.locator('[data-act="open-req"]').first().click();
    await page.locator('[data-act="req-approve"]').click();
    await choose(page, '#rv-update-mode', 'replace');
    await choose(page, '#rv-evidence-precision', 'hour');
    assert.equal(await page.locator('#rv-update-mode').inputValue(), 'replace');
    assert.equal(await page.locator('#rv-evidence-precision').inputValue(), 'hour');
    await page.screenshot({ path: path.join(out, 'select-review.png') });
    await page.locator('[data-act="view"][data-view="library"]').click();
    await page.locator('[data-act="mem-create"]').click();
    for (const type of ['moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule']) {
      await choose(page, '#mf-type', type);
      await page.waitForFunction(() => [...document.querySelectorAll('.modal select')].every(el => el.classList.contains('retro-select-source')));
      const ids = await page.locator('.modal select').evaluateAll(selects => selects.filter(el => !el.disabled).map(el => el.id));
      for (const id of ids) { await open(page, `#${id}`); await page.keyboard.press('Escape'); }
    }
    await choose(page, '#mf-type', 'profile');
    await choose(page, '#cf-stability', 'contextual');
    assert.equal(await page.locator('#cf-stability').inputValue(), 'contextual');
    await open(page, '#cf-basis');
    await page.screenshot({ path: path.join(out, 'select-memory-form.png') });
    await page.keyboard.press('Escape');
    await page.locator('[data-retro-for="mf-memory-time"] .retro-time-field').click();
    await open(page, '.rtp-hour');
    await page.waitForTimeout(350);
    await page.keyboard.press('End');
    assert.equal(await page.locator('.retro-time-pop').count(), 1);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.rtp-hour').inputValue(), '23');
    await open(page, '.rtp-minute');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.retro-time-pop').count(), 1);
    await page.keyboard.press('Escape');
    await open(page, '#mf-source-type');
    await page.locator('.modal [data-cancel]').click();
    assert.equal(await page.locator('.retro-select-pop').count(), 0);
    result.checked.push('memory browser/review', 'six memory types and dynamic fields', 'nested hour/minute selection and Escape', 'modal removal cleanup');

    const presets = { moment: {}, thread: { thread_state: 'paused' }, episode: { closure_quality: 'partial' }, inside_joke: {},
      profile: { stability: 'contextual', basis: 'repeated_observation' }, interaction_rule: { rule_state: 'revoked' } };
    for (const [type, data] of Object.entries(presets)) {
      await page.evaluate(async ({ type, data }) => {
        const forms = await import('/admin/js/pages/_memory_form.js?v=20261012-unified-selects');
        await forms.openMemoryForm({ mode: 'edit', memory: { id: 7, continuity_type: type, continuity_data: data,
          thread_state: data.thread_state, source_type: 'document', time_precision: 'day', content: 'TEST DATA', tags: [], recall_tags: [] } });
      }, { type, data });
      await page.locator('[data-select-for="mf-source-type"]').waitFor();
      assert.equal(await page.locator('#mf-source-type').inputValue(), 'document');
      assert.equal(await page.locator('#mf-time-precision').inputValue(), 'day');
      for (const [key, value] of Object.entries(data)) {
        assert.equal(await page.locator(`#cf-${key}`).inputValue(), value);
        await open(page, `#cf-${key}`);
        assert.equal(await page.locator('.retro-select-pop [aria-selected="true"]').getAttribute('data-value'), value);
        await page.keyboard.press('Escape');
      }
      await page.locator('.modal [data-cancel]').click();
    }
    await page.evaluate(async () => {
      const forms = await import('/admin/js/pages/_memory_form.js?v=20261012-unified-selects');
      await forms.openMemoryForm({ mode: 'change', memory: { id: 7, continuity_type: 'moment', content: 'TEST DATA', tags: [], recall_tags: [], source_type: 'document', time_precision: 'day' } });
    });
    await choose(page, '#mf-type', 'thread');
    await choose(page, '#cf-thread_state', 'paused');
    assert.equal(await page.locator('#cf-thread_state').inputValue(), 'paused');
    await page.locator('[data-retro-for="cf-opened_at"] .retro-time-field').click();
    await open(page, '.rtp-hour');
    await page.evaluate(() => document.querySelector('#mf-continuity').innerHTML = '动态重绘');
    await page.waitForFunction(() => !document.querySelector('.retro-time-pop, .retro-select-pop'));
    await page.locator('.modal [data-cancel]').click();
    result.checked.push('six edit types preserve original selections', 'change type field values', 'nested calendar/submenu removal');

    // 原手动构造函数与共享时间字段仍可复用，不生成第二套下拉。
    await page.evaluate(async () => {
      const { createRetroSelectField } = await import('/admin/js/lib/retro_select.js?v=20261012-unified-selects');
      const { createRetroTimeField } = await import('/admin/js/lib/retro_time.js?v=20261012-unified-selects');
      const fixture = document.createElement('div'); fixture.id = 'manual-fixture';
      fixture.style.cssText = 'position:fixed;inset:100px auto auto 350px;z-index:2200;width:250px;background:var(--paper-solid);padding:16px';
      fixture.innerHTML = '<div class="field"><label>手动字段</label><div id="manual-host"></div></div><div id="time-host"></div>';
      document.body.appendChild(fixture);
      const input = createRetroSelectField(fixture.querySelector('#manual-host'), { id: 'manual-value', value: 'a', options: [{ value: 'a', label: 'Alpha' }, { value: 'b', label: 'Beta' }] });
      input._applyRetroValue('b', true);
      createRetroTimeField(fixture.querySelector('#time-host'), { id: 'manual-time', value: '2026-10-12T10:00' });
    });
    await open(page, '#manual-value');
    assert.equal(await page.locator('.retro-select-pop [aria-selected="true"]').getAttribute('data-value'), 'b');
    await page.keyboard.press('Home'); await page.keyboard.press('Space');
    assert.equal(await page.locator('#manual-value').inputValue(), 'a');
    await open(page, '#manual-value');
    await page.locator('#manual-host').evaluate(el => el.remove());
    await page.waitForFunction(() => !document.querySelector('.retro-select-pop'));
    await page.locator('#time-host .retro-time-field').click();
    await page.locator('#time-host').evaluate(el => el.remove());
    await page.waitForFunction(() => !document.querySelector('.retro-time-pop'));
    await page.locator('#manual-fixture').evaluate(el => el.remove());
    result.checked.push('manual select value/Space/removal', 'shared calendar nested removal');

    // 原生契约：赋值、重复 value、禁用组、fieldset、reset、标签点击及嵌套重绘。
    await page.evaluate(() => {
      const fixture = document.createElement('div'); fixture.id = 'select-fixture';
      fixture.style.cssText = 'position:fixed;inset:100px auto auto 350px;z-index:2200;width:250px;background:var(--paper-solid);padding:16px';
      fixture.innerHTML = '<form id="fixture-form"><fieldset><label for="fixture-select">测试字段</label><select id="fixture-select" name="sample" required><option value="">请选择</option><option value="same">Alpha</option><option value="same">Beta</option><optgroup disabled label="禁用组"><option value="disabled">Disabled</option></optgroup><option value="gamma">Gamma</option></select><button type="button" id="fixture-next">后续按钮</button></fieldset></form>';
      document.body.appendChild(fixture);
      window.fixtureEvents = [];
      fixture.querySelector('select').addEventListener('change', () => fixtureEvents.push('change'));
    });
    await open(page, '#fixture-select');
    assert.equal(await page.locator('.retro-select-pop [data-value="disabled"]').isDisabled(), true);
    await page.locator('.retro-select-pop [data-index="2"]').click();
    assert.equal(await page.locator('#fixture-select').evaluate(el => el.selectedIndex), 2);
    assert.deepEqual(await page.evaluate(() => fixtureEvents), ['change']);
    assert.equal(await page.evaluate(() => new FormData(document.querySelector('#fixture-form')).get('sample')), 'same');
    await page.evaluate(() => document.querySelector('#fixture-select').value = 'gamma');
    assert.equal(await page.locator('[data-select-for="fixture-select"] .retro-select-text').innerText(), 'Gamma');
    await page.evaluate(() => document.querySelector('#fixture-form').reset());
    await page.waitForFunction(() => document.querySelector('[data-select-for="fixture-select"] .retro-select-text').textContent === '请选择');
    assert.equal(await page.locator('#fixture-select').evaluate(el => el.validity.valueMissing), true);
    await page.locator('label[for="fixture-select"]').click();
    await page.locator('.retro-select-pop').waitFor();
    await page.keyboard.press('Escape');
    await page.evaluate(() => document.querySelector('#fixture-form fieldset').disabled = true);
    assert.equal(await page.locator('[data-select-for="fixture-select"]').isDisabled(), true);
    await page.evaluate(() => document.querySelector('#fixture-form fieldset').disabled = false);
    await open(page, '#fixture-select');
    await page.keyboard.press('g');
    assert.equal(await page.locator(':focus').getAttribute('data-value'), 'gamma');
    await page.keyboard.press('Escape');
    await page.evaluate(() => { const el = document.querySelector('#fixture-select'); el.innerHTML = '<option value="new">New option</option>'; el.value = 'new'; });
    assert.equal(await page.locator('[data-select-for="fixture-select"] .retro-select-text').innerText(), 'New option');
    await open(page, '#fixture-select');
    await page.evaluate(() => document.querySelector('#fixture-form').innerHTML = '字段已重绘');
    await page.waitForFunction(() => !document.querySelector('.retro-select-pop'));
    await page.locator('#select-fixture').evaluate(el => el.remove());
    result.checked.push('value/selectedIndex/options sync', 'one change per selection', 'duplicate option values', 'disabled options/fieldset', 'FormData/required/reset', 'label activation/typeahead', 'nested redraw cleanup');

    const mobile = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true });
    mobile.on('pageerror', error => errors.push(error.message));
    await mobile.goto(`${origin}/admin/index.html#/logs`);
    await mobile.locator('#log-list .mem-card').nth(7).waitFor();
    await open(mobile, '#log-key'); await menuFits(mobile);
    await mobile.screenshot({ path: path.join(out, 'select-mobile.png') });
    assert.equal(await mobile.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await mobile.keyboard.press('Escape');
    await open(mobile, '#log-preset');
    await mobile.setViewportSize({ width: 375, height: 812 });
    await mobile.waitForFunction(() => !document.querySelector('.retro-select-pop'));
    await open(mobile, '#log-preset');
    await mobile.waitForTimeout(350);
    await mobile.locator('#content').evaluate(el => el.scrollTop = 100);
    await mobile.waitForFunction(() => !document.querySelector('.retro-select-pop'));
    result.checked.push('390px menus/no overflow', 'resize/scroll cleanup');
    result.page_errors = errors;
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(out, 'select-browser.json'), JSON.stringify(result, null, 2));
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
}
verify().catch(error => { process.stderr.write(`${error.stack}\n`); process.exitCode = 1; });
