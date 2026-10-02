// Real-browser regressions for shared controls. Called by the frontend acceptance runner.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

async function runSharedTests(browser, baseUrl, evidenceDir) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 860 }, timezoneId: 'UTC' });
  const page = await context.newPage();
  const errors = [];
  const checks = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript(() => {
    const nativeAdd = EventTarget.prototype.addEventListener;
    const nativeRemove = EventTarget.prototype.removeEventListener;
    const listeners = new Map();
    for (const target of [document, window]) {
      const entries = new Map();
      listeners.set(target, entries);
      target.addEventListener = function(type, listener, options) {
        const capture = typeof options === 'boolean' ? options : Boolean(options?.capture);
        const key = `${type}:${capture}`;
        if (!entries.has(key)) entries.set(key, new Set());
        entries.get(key).add(listener);
        return nativeAdd.call(this, type, listener, options);
      };
      target.removeEventListener = function(type, listener, options) {
        const capture = typeof options === 'boolean' ? options : Boolean(options?.capture);
        entries.get(`${type}:${capture}`)?.delete(listener);
        return nativeRemove.call(this, type, listener, options);
      };
    }
    const nativeObserver = MutationObserver;
    const observers = new Set();
    window.MutationObserver = class extends nativeObserver {
      observe(...args) { observers.add(this); return super.observe(...args); }
      disconnect() { observers.delete(this); return super.disconnect(); }
    };
    window.sharedCounters = () => ({
      listeners: [...listeners.values()].reduce((sum, entries) =>
        sum + [...entries.values()].reduce((count, entry) => count + entry.size, 0), 0),
      observers: observers.size,
    });
  });
  await page.route('**/__frontend_controls__', (route) => route.fulfill({
    contentType: 'text/html', body: `<!doctype html><html lang="zh-CN" data-theme="day"><head>
      <meta charset="utf-8"><link rel="stylesheet" href="/admin/css/style.css">
      <style>body{height:auto;overflow:auto;padding:24px}#fixture{max-width:420px;background:var(--paper-solid);padding:16px}button{font-family:inherit}</style>
      </head><body><button type="button" class="btn btn-secondary" id="before">前一个字段</button><main id="fixture"></main>
      <button type="button" class="btn btn-secondary" id="after">后一个字段</button></body></html>`,
  }));
  await page.goto(`${baseUrl}/__frontend_controls__`);
  const uiSource = await (await page.request.get(`${baseUrl}/admin/js/ui.js`)).text();
  const version = uiSource.match(/ASSET_VERSION\s*=\s*['"]([^'"]+)/)?.[1];
  assert.ok(version, 'ui asset version');
  await page.evaluate(async (version) => {
    window.ui = await import(`/admin/js/ui.js?v=${version}`);
    window.selectFields = await import(`/admin/js/lib/retro_select.js?v=${version}`);
    window.timeFields = await import(`/admin/js/lib/retro_time.js?v=${version}`);
    ui.initTooltips();
  }, version);
  const baseline = await page.evaluate(() => sharedCounters());
  const settle = () => page.evaluate(() => new Promise((resolve) => requestAnimationFrame(resolve)));
  const reset = async (html = '') => {
    await page.evaluate((html) => { document.getElementById('fixture').innerHTML = html; }, html);
    await settle();
  };
  const capture = async (name) => {
    if (!evidenceDir) return;
    await fs.mkdir(evidenceDir, { recursive: true });
    await page.screenshot({ path: path.join(evidenceDir, name), fullPage: true });
  };
  try {
    await reset(`<form id="probe-form"><div class="field"><label for="status">状态</label>
      <select id="status" name="status" required aria-describedby="status-hint" data-tooltip="选择状态">
      <option value="">请选择</option><option value="a">甲项</option><option value="blocked" disabled>不可用</option><option value="b">乙项</option></select>
      <span id="status-hint" class="field-hint">必填状态</span></div>
      <div class="field"><label for="date">日期</label><input id="date" name="date" type="date" required min="2026-10-01" max="2026-10-31" value="2026-10-02"></div></form>`);
    await page.evaluate(() => {
      selectFields.initRetroSelectFields(document.getElementById('fixture'));
      timeFields.initRetroTimeFields(document.getElementById('fixture'));
      window.events = { change: 0, input: 0 };
      document.getElementById('status').addEventListener('change', () => events.change++);
      document.getElementById('date').addEventListener('input', () => events.input++);
    });
    const form = await page.evaluate(() => ({
      valid: document.getElementById('probe-form').checkValidity(),
      labelTarget: document.querySelector('label[for="status-button"]').htmlFor,
      focused: document.activeElement.id,
      required: document.getElementById('status-button').getAttribute('aria-required'),
      error: document.querySelector('.retro-select .retro-field-error').textContent,
      described: document.getElementById('status-button').getAttribute('aria-describedby'),
      tooltip: document.getElementById('status-button').dataset.tooltip,
    }));
    assert.equal(form.valid, false);
    assert.equal(form.labelTarget, 'status-button');
    assert.equal(form.focused, 'status-button');
    assert.equal(form.required, 'true');
    assert.equal(await page.locator('#status-button').getAttribute('role'), 'combobox');
    assert.equal(form.error, '此项必填');
    assert.ok(form.described.includes('status-hint'));
    assert.equal(form.tooltip, '选择状态');
    await page.locator('#status-button').press('Enter');
    assert.equal(await page.locator('#status-button').getAttribute('aria-expanded'), 'true');
    assert.equal(await page.locator('.retro-select-pop').getAttribute('role'), 'listbox');
    await capture('shared-select-day-desktop.png');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), 'a');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), 'b');
    await page.keyboard.press('Home');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), '');
    await page.keyboard.press('End');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), 'b');
    await page.keyboard.press('Space');
    assert.equal(await page.locator('#status').inputValue(), 'b');
    assert.equal(await page.evaluate(() => events.change), 1);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'status-button');
    assert.equal(await page.evaluate(() => document.getElementById('probe-form').checkValidity()), true);
    assert.deepEqual(await page.evaluate(() => Object.fromEntries(new FormData(document.getElementById('probe-form')))), { status: 'b', date: '2026-10-02' });
    await page.evaluate(() => document.getElementById('status')._applyRetroValue('a', true));
    assert.equal(await page.locator('.retro-select-text').textContent(), '甲项');
    assert.equal(await page.evaluate(() => events.change), 1);
    await page.locator('#status-button').press('ArrowDown');
    assert.equal(await page.locator('.retro-select-option.is-selected').getAttribute('aria-selected'), 'true');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
    await page.evaluate(() => document.getElementById('status')._retroField.setDisabled(true));
    assert.equal(await page.locator('#status-button').isDisabled(), true);
    await page.evaluate(() => document.getElementById('status-button').click());
    assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
    await page.evaluate(() => document.getElementById('status')._retroField.setDisabled(false));
    await page.evaluate(() => document.getElementById('date')._applyRetroValue('2026-11-02', true));
    assert.equal(await page.evaluate(() => document.getElementById('date').checkValidity()), false);
    assert.equal(await page.locator('#date-button').getAttribute('aria-invalid'), 'true');
    await page.evaluate(() => document.getElementById('date')._applyRetroValue('2026-10-03', true));
    assert.equal(await page.evaluate(() => document.getElementById('date').checkValidity()), true);
    assert.equal(await page.locator('#date-button').getAttribute('aria-invalid'), 'false');
    await page.evaluate(() => document.getElementById('probe-form').reset());
    await settle();
    assert.equal(await page.locator('#status').inputValue(), '');
    assert.equal(await page.locator('.retro-select-text').textContent(), '请选择');
    assert.equal(await page.locator('#date').inputValue(), '2026-10-02');
    checks.push('select semantics, disabled options, keyboard, labels, silent updates, required/form reset and date range');

    await reset('<section id="redraw"><label for="redraw-select">重绘字段</label><select id="redraw-select"><option value="a">甲</option><option value="b">乙</option></select></section>');
    await page.evaluate(() => {
      const section = document.getElementById('redraw');
      selectFields.initRetroSelectFields(section);
      document.getElementById('redraw-select').addEventListener('change', () => {
        section.innerHTML = '<label for="redraw-select">重绘字段</label><select id="redraw-select"><option value="a">甲</option><option value="b" selected>乙</option></select>';
        selectFields.initRetroSelectFields(section);
      });
    });
    await page.locator('#redraw-select-button').click();
    await page.locator('.retro-select-option[data-value="b"]').click();
    assert.equal(await page.evaluate(() => document.activeElement.id), 'redraw-select-button');
    assert.equal(await page.locator('#redraw-select').inputValue(), 'b');
    checks.push('selection-triggered field redraw restores focus to the replacement business id');

    await reset(`<div class="field"><label for="stamp">记录时间</label><div class="retro-time" data-retro-for="stamp" data-retro-value="2026-10-02T08:15"></div></div>`);
    await page.evaluate(() => timeFields.initRetroTimeFields(document.getElementById('fixture')));
    const idempotent = await page.evaluate(() => {
      const original = document.getElementById('stamp');
      timeFields.initRetroTimeFields(document.getElementById('fixture'));
      return original === document.getElementById('stamp');
    });
    assert.equal(idempotent, true);
    await page.locator('#stamp-button').press('Space');
    assert.equal(await page.locator('.retro-time-grid button.is-selected').getAttribute('aria-pressed'), 'true');
    await page.keyboard.press('ArrowRight');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.day), '3');
    assert.equal(await page.locator('#stamp').inputValue(), '2026-10-02T08:15');
    await page.keyboard.press('Enter');
    await page.locator('[data-unit="hour"] .rtp-select-btn').press('ArrowDown');
    assert.equal(await page.locator('[data-unit="hour"] .rtp-select-pop').isVisible(), true);
    await page.keyboard.press('End');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), '23');
    await page.keyboard.press('Enter');
    await page.locator('[data-unit="minute"] .rtp-select-btn').press('Space');
    await page.keyboard.press('Home');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.value), '01');
    await page.keyboard.press('Space');
    await page.locator('[data-act="ok"]').click();
    assert.equal(await page.locator('#stamp').inputValue(), '2026-10-03T23:01');
    await page.locator('#stamp-button').click();
    await page.locator('[data-act="now"]').click();
    const nowShanghai = await page.evaluate(() => {
      const shifted = new Date(Date.now() + 8 * 3600000);
      return shifted.toISOString().slice(0, 16);
    });
    await page.locator('[data-act="ok"]').click();
    assert.equal(await page.locator('#stamp').inputValue(), nowShanghai);
    await page.locator('#stamp-button').click();
    await page.locator('[data-act="clear"]').click();
    assert.equal(await page.locator('#stamp').inputValue(), '');
    checks.push('datetime calendar and hour/minute keyboard, current Shanghai time, clear and idempotent mounting');

    await reset();
    await page.locator('#before').focus();
    await page.evaluate(() => {
      window.openModal = ui.modal({ title: '字段焦点测试', body: `<button type="button" class="btn btn-secondary" id="modal-before-select">下拉之前</button>
        <div class="field"><label for="modal-select">状态</label><select id="modal-select"><option value="a">甲</option><option value="b">乙</option></select></div>
        <button type="button" class="btn btn-secondary" id="modal-after-select">下拉之后/时间之前</button>
        <div class="field"><label for="modal-time">时刻</label><input type="time" id="modal-time" value="09:30:00"></div>
        <button type="button" class="btn btn-secondary" id="modal-after-time">时间之后</button>
        <div class="field"><label for="modal-datetime">日期时间</label><input type="datetime-local" id="modal-datetime" value="2026-10-02T09:30"></div>
        <button type="button" class="btn btn-secondary" id="modal-after-datetime">日期时间之后</button><input id="modal-text" type="text">`,
        footer: '<button type="button" class="btn btn-secondary" data-cancel>取消</button>' });
      selectFields.initRetroSelectFields(openModal.root);
      timeFields.initRetroTimeFields(openModal.root);
    });
    await settle();
    await page.locator('#modal-select-button').click();
    await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-after-select', 'select Tab uses the following form stop');
    assert.equal(await page.locator('.retro-select-pop').count(), 0);
    assert.equal(await page.locator('#modal-select').inputValue(), 'a', 'Tab does not commit the focused option');
    await page.locator('#modal-select-button').click();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-before-select', 'select Shift+Tab uses the preceding form stop');
    assert.equal(await page.locator('.retro-select-pop').count(), 0);

    await page.locator('#modal-time-button').click();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '分钟', 'time Tab stays in popup order');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'clear');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'ok');
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-after-time', 'last time popup stop advances to the following field');
    assert.equal(await page.locator('.retro-time-pop').count(), 0);
    await page.locator('#modal-time-button').click();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-time-button', 'first time popup stop returns to its source');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-after-select', 'leaving backward uses the field before time');
    assert.equal(await page.locator('.retro-time-pop').count(), 0);

    await page.locator('#modal-time-button').click();
    await page.locator('[data-unit="hour"] .rtp-select-btn').click();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '分钟', 'hour list Tab exits from the hour unit position');
    assert.equal(await page.locator('.rtp-select-pop:not([hidden])').count(), 0);
    assert.equal(await page.locator('.retro-time-pop').count(), 1);
    await page.locator('[data-unit="minute"] .rtp-select-btn').click();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '小时', 'minute list Shift+Tab exits to hour');
    assert.equal(await page.locator('.rtp-select-pop:not([hidden])').count(), 0);
    await page.locator('[data-unit="minute"] .rtp-select-btn').click();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.act), 'clear', 'minute list Tab exits to clear');
    assert.equal(await page.locator('#modal-time').inputValue(), '09:30:00', 'Tab leaves the committed time unchanged');
    await page.keyboard.press('Escape');

    await page.locator('#modal-datetime-button').click();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '小时', 'calendar Tab moves from the day to hour');
    await page.locator('[data-nav="year-"]').focus();
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-datetime-button');
    await page.keyboard.press('Shift+Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-after-time');
    assert.equal(await page.locator('.retro-time-pop').count(), 0);
    await page.locator('#modal-datetime-button').click();
    await page.locator('[data-act="ok"]').focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-after-datetime');
    assert.equal(await page.locator('.retro-time-pop').count(), 0);
    checks.push('modal selector Tab/Shift+Tab source order, calendar/time popup boundaries and hour/minute subpanel traversal');

    await page.locator('#modal-time-button').click();
    assert.equal(await page.locator('[data-unit="hour"] .rtp-select-text').textContent(), '09');
    assert.equal(await page.locator('[data-unit="minute"] .rtp-select-text').textContent(), '30');
    await page.locator('[data-unit="minute"] .rtp-select-btn').click();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.rtp-select-pop:not([hidden])').count(), 0);
    assert.equal(await page.locator('.retro-time-pop').count(), 1);
    assert.equal(await page.locator('.modal-mask').count(), 1);
    assert.equal(await page.evaluate(() => document.activeElement.getAttribute('aria-label')), '分钟');
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('.retro-time-pop').count(), 0);
    assert.equal(await page.locator('.modal-mask').count(), 1);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'modal-time-button');
    await page.locator('#modal-time-button').click();
    for (let index = 0; index < 12; index++) {
      await page.keyboard.press('Tab');
      assert.equal(await page.evaluate(() => Boolean(document.activeElement.closest('.modal'))
        || Boolean(document.activeElement.closest('[data-retro-overlay]'))), true);
    }
    await page.evaluate(() => openModal.close());
    await settle();
    assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
    assert.equal(await page.evaluate(() => document.activeElement.id), 'before');
    checks.push('HH:MM:SS initial time, nested Escape order, modal portal focus trap and focus restoration');

    for (const viewport of [{ width: 900, height: 760 }, { width: 390, height: 844 }, { width: 640, height: 240 }]) {
      await page.setViewportSize(viewport);
      await page.evaluate(() => document.documentElement.dataset.theme = 'night');
      await reset('<div class="field"><label for="small-time">时间</label><input id="small-time" type="datetime-local" value="2026-10-02T12:30"></div>');
      await page.evaluate(() => timeFields.initRetroTimeFields(document.getElementById('fixture')));
      await page.locator('#small-time-button').click();
      await page.locator('[data-unit="minute"] .rtp-select-btn').click();
      const geometry = await page.evaluate(() => {
        const list = document.querySelector('[data-unit="minute"] .rtp-select-pop');
        const selected = list.querySelector('.is-selected');
        const rect = list.getBoundingClientRect();
        const option = selected.getBoundingClientRect();
        return { left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
          width: innerWidth, height: innerHeight,
          visible: selected.contains(document.elementFromPoint(option.left + option.width / 2, option.top + option.height / 2)) };
      });
      assert.ok(geometry.left >= 0 && geometry.right <= geometry.width && geometry.top >= 0 && geometry.bottom <= geometry.height,
        `unit list stays in ${viewport.width}x${viewport.height}: ${JSON.stringify(geometry)}`);
      assert.equal(geometry.visible, true, 'selected minute is visible and hit-testable outside the calendar scroll viewport');
      if (viewport.width === 390) await capture('shared-time-night-mobile.png');
      await page.keyboard.press('Escape');
      await page.keyboard.press('Escape');
    }
    checks.push('night theme at drawer/mobile/short landscape sizes; minute panel stays visible and in viewport');

    await page.setViewportSize({ width: 1280, height: 860 });
    await reset();
    await settle();
    assert.deepEqual(await page.evaluate(() => sharedCounters()), baseline);
    for (let cycle = 0; cycle < 6; cycle++) {
      await reset('<section><div><div class="field"><label for="nested">嵌套字段</label><select id="nested"><option value="a">甲</option><option value="b">乙</option></select></div></div></section>');
      await page.evaluate(() => selectFields.initRetroSelectFields(document.getElementById('fixture')));
      await page.locator('#nested-button').click();
      await page.evaluate(() => document.querySelector('#fixture section > div').innerHTML = '<p>类型已切换</p>');
      await settle();
      assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
      assert.deepEqual(await page.evaluate(() => sharedCounters()), baseline);
    }
    await reset('<section id="hidden-panel"><div class="field"><label for="hidden-time">隐藏字段</label><input id="hidden-time" type="time"></div></section>');
    await page.evaluate(() => timeFields.initRetroTimeFields(document.getElementById('fixture')));
    await page.locator('#hidden-time-button').click();
    await page.evaluate(() => document.getElementById('hidden-panel').hidden = true);
    await settle();
    assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
    await page.evaluate(() => document.getElementById('hidden-panel').hidden = false);
    await page.locator('#hidden-time-button').click();
    await page.evaluate(() => dispatchEvent(new Event('resize')));
    assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
    await reset();
    assert.deepEqual(await page.evaluate(() => sharedCounters()), baseline);
    checks.push('six nested redraws release all field/portal observers and document/window listeners; hidden and resize close portals');
    assert.deepEqual(errors, [], 'no browser runtime errors');
    return { checks, errors, baseline, finalCounters: await page.evaluate(() => sharedCounters()) };
  } finally {
    await context.close();
  }
}

module.exports = { runSharedTests };
