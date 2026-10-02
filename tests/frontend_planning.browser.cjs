const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs/promises');

/** Real browser checks against the production module graph, using local API fixtures. */
async function runPlanningTests(browser, baseUrl, evidenceDir) {
  const context = await browser.newContext({
    timezoneId: 'Asia/Shanghai', viewport: { width: 1440, height: 1000 },
    permissions: ['notifications'],
  });
  const page = await context.newPage();
  const errors = [];
  const requests = [];
  const covered = [];
  const tasks = [
    { id: 1, content: '每日检查', task_type: 'daily', is_active: true, estimated_minutes: 30 },
    { id: 2, content: '无日期常驻待办', task_type: 'once', target_date: null, is_active: true, has_generated_occurrence: true },
    { id: 3, content: '已生成单次待办', task_type: 'once', target_date: '2026-10-03', is_active: true,
      has_generated_occurrence: true, window_start_tod: '09:00', window_end_tod: '12:00' },
  ];
  const occurrence = (id, taskId, status, extra = {}) => ({
    id, task_id: taskId, task_type: taskId === 2 ? 'once' : 'daily', status,
    content: `验收待办 ${id}`, schedule_date: '2026-10-02', display_cycle_date: '2026-10-02',
    schedule_label: '正常', estimated_minutes: 30, ...extra,
  });
  const progress = occurrence(101, 1, 'pending', {
    window_start_at: '2026-10-03T09:00:00+08:00', window_end_at: '2026-10-03T12:00:00+08:00',
  });
  const timeout = occurrence(102, 1, 'timeout');
  const done = occurrence(103, 1, 'completed', {
    actual_start: '2026-10-02T09:00:00+08:00', actual_end: '2026-10-02T09:30:00+08:00',
  });
  const resident = occurrence(104, 2, 'pending');
  const occurrences = [progress, timeout, done, resident];
  let rescheduleFailures = 0;
  let rangeFailure = false;
  page.on('pageerror', error => errors.push(error.stack || String(error)));
  await page.route('**/admin/api/planning/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postDataJSON();
    const record = { pathname: url.pathname, search: url.search, method: request.method(),
      headers: request.headers(), body };
    requests.push(record);
    let data = {};
    let status = 200;
    if (request.method() === 'GET') {
      if (url.pathname.endsWith('/today')) data = {
        progress: [progress, resident], attention: [timeout], done: [done], conflicts: [], recompute: {},
      };
      else if (url.pathname.endsWith('/tasks')) data = tasks;
      else if (url.pathname.endsWith('/occurrences')) data = occurrences.filter(item =>
        (!url.searchParams.get('task_type') || item.task_type === url.searchParams.get('task_type'))
        && (!url.searchParams.get('status') || item.status === url.searchParams.get('status'))
        && (!url.searchParams.get('schedule_date') || item.schedule_date === url.searchParams.get('schedule_date')));
    } else if (url.pathname.endsWith('/reschedule-timeout') && rescheduleFailures > 0) {
      rescheduleFailures -= 1; status = 409; data = { error: '验收重试：请求未保存' };
    } else if (request.method() === 'PATCH' && /\/occurrences\/\d+$/.test(url.pathname) && rangeFailure) {
      rangeFailure = false; status = 422; data = { error: '最晚完成必须晚于最早开始' };
    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
  });
  await fs.mkdir(evidenceDir, { recursive: true });
  const button = id => page.locator(`#${id}-button`);
  const value = id => page.locator(`#${id}`).inputValue();
  const noPop = () => page.waitForFunction(() => !document.querySelector('[data-retro-overlay]'));
  const setValue = (id, next, silent = true) => page.evaluate(({ id, next, silent }) =>
    document.getElementById(id)._applyRetroValue(next, silent), { id, next, silent });
  const closeModal = async () => {
    if (await page.locator('.modal').count()) await page.locator('.modal-close').click();
    await noPop();
  };
  const assertWithin = async selector => {
    const box = await page.locator(selector).boundingBox();
    assert.ok(box, `${selector} must be visible`);
    const viewport = page.viewportSize();
    assert.ok(box.x >= -1 && box.y >= -1 && box.x + box.width <= viewport.width + 1
      && box.y + box.height <= viewport.height + 1, `${selector} must fit the viewport`);
  };
  const inspectDatetime = async (id, captureName = null) => {
    assert.equal(await page.locator(`#${id}`).getAttribute('type'), 'hidden');
    assert.ok(await button(id).getAttribute('aria-label'), `${id} needs an accessible label`);
    await button(id).click();
    await page.locator('.retro-time-pop').waitFor({ state: 'visible' });
    assert.ok(await page.locator('.retro-time-grid button[data-day]').count());
    assert.equal(await page.locator('.retro-time-pop select').count(), 0);
    await assertWithin('.retro-time-pop');
    await page.locator('.retro-time-pop [data-unit="hour"] .rtp-select-btn').click();
    assert.equal(await page.locator('[data-unit="hour"] .rtp-select-option').count(), 24);
    await assertWithin('[data-unit="hour"] .rtp-select-pop');
    await page.locator('[data-unit="hour"] .rtp-select-option[data-value="10"]').click();
    await page.locator('.retro-time-pop [data-unit="minute"] .rtp-select-btn').click();
    assert.equal(await page.locator('[data-unit="minute"] .rtp-select-option').count(), 60);
    await assertWithin('[data-unit="minute"] .rtp-select-pop');
    // Capture the viewport: full-page screenshots resize a tall fixture and correctly close its overlays.
    if (captureName) await page.screenshot({ path: path.join(evidenceDir, captureName) });
    await page.locator('[data-unit="minute"] .rtp-select-option[data-value="15"]').click();
    await page.locator('.retro-time-grid [data-day="3"]').click();
    await page.locator('.retro-time-pop [data-act="ok"]').click();
    await noPop();
    assert.match(await value(id), /^\d{4}-\d{2}-03T10:15$/);
  };
  try {
    await page.goto(`${baseUrl}/__frontend_controls__`);
    await page.addStyleTag({ url: `${baseUrl}/admin/css/style.css?v=20261002-frontend-controls1` });
    await page.evaluate(async () => {
      document.documentElement.dataset.theme = 'day';
      const root = document.createElement('main');
      root.id = 'planning-fixture'; root.style.padding = '20px';
      document.body.append(root);
      window.__planning = (await import('/admin/js/pages/planning.js?v=20261002-frontend-controls1')).default;
      const ui = await import('/admin/js/ui.js?v=20261002-frontend-controls1');
      ui.initTooltips();
      await window.__planning.mount(root);
    });
    await page.locator('[data-act="plan-tab"][data-tab="all"]').click();
    await page.locator('[data-occ-all="101"]').waitFor();
    assert.equal(await page.locator('#planning-all select').count(), 0);
    for (const [id, chosen] of [
      ['planning-filter-type', 'once'], ['planning-filter-status', 'pending'],
    ]) {
      await button(id).click();
      await page.locator('.retro-select-pop').waitFor({ state: 'visible' });
      await assertWithin('.retro-select-pop');
      await page.locator(`.retro-select-option[data-value="${chosen}"]`).click();
      await noPop();
      assert.equal(await value(id), chosen);
      assert.equal(await button(id).getAttribute('aria-expanded'), 'false');
    }
    await page.locator('[data-occ-all="104"]').waitFor();
    assert.equal(await page.locator('[data-occ-all]').count(), 1);
    await setValue('planning-filter-date', '2026-10-02', false);
    await page.waitForFunction(() => window.__planning.filters.schedule_date === '2026-10-02');
    await page.locator('[data-act="clear-filters"]').click();
    await page.locator('[data-occ-all="101"]').waitFor();
    for (const id of ['planning-filter-type', 'planning-filter-status', 'planning-filter-date']) {
      assert.equal(await value(id), '');
    }
    assert.equal((await button('planning-filter-type').innerText()).trim(), '全部');
    assert.equal((await button('planning-filter-status').innerText()).trim(), '全部');
    await button('planning-filter-type').click();
    await page.evaluate(() => window.__planning.switchTab('today'));
    await noPop();
    await page.evaluate(() => window.__planning.switchTab('all'));
    await setValue('planning-filter-type', 'once', false);
    await page.locator('[data-occ-all="104"]').waitFor();
    await button('planning-filter-status').click();
    await page.evaluate(async () => {
      const root = document.getElementById('planning-fixture');
      window.__planning.unmount(); root.replaceChildren();
      await window.__planning.mount(root); window.__planning.switchTab('all');
    });
    await noPop();
    assert.equal(await value('planning-filter-type'), 'once');
    assert.equal((await button('planning-filter-type').innerText()).trim(), '单次');
    await page.locator('[data-act="clear-filters"]').click();
    covered.push('FE-01: two filters, change/query, clear labels, tab close, route remount and retained state');

    await page.evaluate(() => window.__planning.askRescheduleTimeout(102));
    await inspectDatetime('planning-reschedule-time', 'planning-reschedule-calendar-hour-minute-day.png');
    await setValue('planning-reschedule-time', '2026-10-03T10:15');
    rescheduleFailures = 2;
    const retriesBefore = requests.length;
    await page.locator('.modal [data-ok]').click();
    await page.locator('.toast-err').last().waitFor();
    await page.locator('.modal [data-ok]').click();
    await page.waitForFunction(() => document.querySelectorAll('.toast-err').length >= 2);
    const attempts = requests.slice(retriesBefore).filter(request => request.pathname.endsWith('/reschedule-timeout'));
    assert.equal(attempts.length, 2);
    assert.ok(attempts[0].headers['idempotency-key']);
    assert.equal(attempts[0].headers['idempotency-key'], attempts[1].headers['idempotency-key']);
    assert.equal(attempts[0].body.est_start, '2026-10-03T02:15:00.000Z');
    await setValue('planning-reschedule-time', '2026-10-03T11:15');
    await page.locator('.modal [data-ok]').click();
    await page.locator('.modal').waitFor({ state: 'detached' });
    const third = requests.filter(request => request.pathname.endsWith('/reschedule-timeout')).at(-1);
    assert.notEqual(third.headers['idempotency-key'], attempts[0].headers['idempotency-key']);
    assert.equal(third.body.est_start, '2026-10-03T03:15:00.000Z');
    covered.push('FE-05: timeout retry retains identity; changed time gets a fresh key; Asia/Shanghai ISO payload');

    await page.evaluate(() => window.__planning.askNewTime(101, 'deferred', '延后到什么时间？'));
    await inspectDatetime('planning-new-time');
    await setValue('planning-new-time', '2026-10-03T10:15');
    await page.locator('.modal [data-ok]').click();
    await page.locator('.modal').waitFor({ state: 'detached' });
    const deferred = requests.filter(request => request.pathname.endsWith('/101/status')).at(-1);
    assert.deepEqual(deferred.body, { status: 'deferred', est_start: '2026-10-03T02:15:00.000Z' });
    await page.evaluate(() => window.__planning.askEditTime(101));
    assert.equal(await value('planning-adj-window-start'), '2026-10-03T09:00');
    assert.equal(await value('planning-adj-window-end'), '2026-10-03T12:00');
    await inspectDatetime('planning-adj-window-start');
    await inspectDatetime('planning-adj-window-end');
    await setValue('planning-adj-window-start', '2026-10-03T12:00');
    await setValue('planning-adj-window-end', '2026-10-03T09:00');
    rangeFailure = true;
    await page.locator('.modal [data-ok]').click();
    await page.locator('#pf-adj-error').waitFor({ state: 'visible' });
    assert.ok((await page.locator('#pf-adj-error').innerText()).includes('最晚完成必须晚于最早开始'));
    assert.equal(await value('planning-adj-window-start'), '2026-10-03T12:00');
    await setValue('planning-adj-window-start', '2026-10-03T09:00');
    await button('planning-adj-window-end').click();
    await page.locator('.retro-time-pop [data-act="clear"]').click();
    assert.equal(await value('planning-adj-window-end'), '');
    await page.locator('.modal [data-ok]').click();
    await page.locator('.modal').waitFor({ state: 'detached' });
    const adjusted = requests.filter(request => request.method === 'PATCH' && request.pathname.endsWith('/occurrences/101')).at(-1);
    assert.deepEqual(adjusted.body, { window_start_at: '2026-10-03T01:00:00.000Z', window_end_at: null });
    await page.evaluate(() => window.__planning.askEditTime(104));
    for (const id of ['planning-adj-window-start', 'planning-adj-window-end']) {
      assert.equal(await button(id).isDisabled(), true);
      assert.equal(await page.locator(`#${id}`).isDisabled(), true);
    }
    assert.ok(await page.locator('#planning-adj-note').isVisible());
    await closeModal();
    await page.evaluate(() => window.__planning.askBackfill(103));
    assert.equal(await value('planning-backfill-start'), '2026-10-02T09:00');
    assert.equal(await value('planning-backfill-end'), '2026-10-02T09:30');
    await inspectDatetime('planning-backfill-start');
    await inspectDatetime('planning-backfill-end');
    for (const id of ['planning-backfill-start', 'planning-backfill-end']) {
      await button(id).click();
      await page.locator('.retro-time-pop [data-act="clear"]').click();
      assert.equal(await value(id), '');
      assert.equal((await button(id).innerText()).trim(), '');
    }
    await page.locator('.modal [data-ok]').click();
    await page.locator('.modal').waitFor({ state: 'detached' });
    const backfilled = requests.filter(request => request.method === 'PATCH' && request.pathname.endsWith('/occurrences/103')).at(-1);
    assert.deepEqual(backfilled.body, { actual_start: null, actual_end: null });
    covered.push('FE-05: all six datetime calendars/hour/minute lists, existing values, clear/null, range rejection, resident disabled');

    await page.evaluate(() => window.__planning.openTaskForm(window.__planning.tasks.find(task => task.id === 3)));
    assert.ok(await page.locator('#pf-locked-note').isVisible());
    for (const id of ['pf-target-date', 'pf-window-start', 'pf-window-end']) {
      assert.equal(await button(id).isDisabled(), true);
      assert.ok((await button(id).getAttribute('aria-describedby')).includes('pf-locked-note'));
      assert.equal(await button(id).getAttribute('title'), null);
    }
    await closeModal();
    await page.evaluate(() => window.__planning.openTaskForm(null));
    await setValue('pf-type', 'once', false);
    assert.equal(await button('pf-window-start').isDisabled(), true);
    assert.ok(await page.locator('#pf-resident-note').isVisible());
    await setValue('pf-target-date', '2026-10-03', false);
    assert.equal(await button('pf-window-start').isDisabled(), false);
    assert.ok(!(await button('pf-window-start').getAttribute('aria-describedby')).includes('pf-resident-note'));
    await closeModal();
    covered.push('FE-08: generated once lock and resident reason stay visible; disabled state and descriptions synchronize');

    for (const theme of ['day', 'night']) {
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      for (const [width, label] of [[1440, 'desktop'], [1000, 'drawer'], [390, 'phone']]) {
        await page.setViewportSize({ width, height: 1000 });
        await page.evaluate(() => window.__planning.switchTab('all'));
        await button('planning-filter-status').click();
        await assertWithin('.retro-select-pop');
        await page.screenshot({ path: path.join(evidenceDir, `planning-filter-${theme}-${label}.png`) });
        await page.keyboard.press('Escape');
        await noPop();
        await page.evaluate(() => window.__planning.askBackfill(103));
        await inspectDatetime('planning-backfill-start', `planning-calendar-${theme}-${label}.png`);
        await closeModal();
        if (width === 1000) {
          await page.evaluate(() => window.__planning.select('occ', 101));
          assert.equal(await page.locator('#planning-layout').evaluate(root => root.classList.contains('detail-open')), true);
          await page.screenshot({ path: path.join(evidenceDir, `planning-detail-${theme}-${label}.png`), fullPage: true });
          await page.evaluate(() => window.__planning.detail.closeDrawer());
        }
      }
    }
    covered.push('Visual: day/night at 1440, 1000 drawer and 390 phone; open lists/calendar/hour/minute fit viewport; screenshots saved');

    await page.evaluate(async () => {
      window.__planning.unmount();
      const logs = (await import('/admin/js/pages/logs.js?v=20261002-frontend-controls1')).default;
      await logs.mount(document.getElementById('planning-fixture'));
    });
    await noPop();
    assert.equal(await page.locator('#planning-fixture select').count(), 0);
    for (const id of ['logs-filter-time', 'logs-filter-type', 'logs-filter-level']) {
      assert.equal(await button(id).isDisabled(), true);
      assert.equal(await page.locator(`#${id}`).isDisabled(), true);
      assert.equal(await button(id).getAttribute('aria-disabled'), 'true');
      await button(id).dispatchEvent('click');
      assert.equal(await page.locator('[data-retro-overlay]').count(), 0);
    }
    assert.ok(await page.locator('#logs-unavailable-note').isVisible());
    assert.equal(await page.locator('#planning-fixture [title]').count(), 0);
    assert.ok((await page.locator('#planning-fixture').innerText()).includes('日志接口尚未接入'));
    for (const theme of ['day', 'night']) {
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      await page.screenshot({ path: path.join(evidenceDir, `logs-disabled-${theme}-phone.png`), fullPage: true });
    }
    covered.push('FE-06: all three selectors disabled by mouse/keyboard semantics, permanent note, truthful empty logs, day/night phone screenshots');
    assert.deepEqual(errors, [], 'No uncaught browser errors');
    return covered;
  } finally {
    await context.close();
  }
}

module.exports = { runPlanningTests };
