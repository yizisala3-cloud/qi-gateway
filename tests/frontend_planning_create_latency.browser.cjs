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
  let todayFailures = 0;
  let todayOverride = null;
  let holdToday = false;
  let holdCreate = false;
  const heldToday = [];
  const heldCreates = [];
  const heldWaiters = [];
  const waitHeld = (list, count) => list.length >= count ? Promise.resolve()
    : new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`Expected ${count} held requests, found ${list.length}`)), 10000);
      heldWaiters.push({ list, count, resolve: () => { clearTimeout(timer); resolve(); } });
    });
  const publishHeld = () => {
    for (let index = heldWaiters.length - 1; index >= 0; index -= 1) {
      const waiter = heldWaiters[index];
      if (waiter.list.length >= waiter.count) {
        heldWaiters.splice(index, 1);
        waiter.resolve();
      }
    }
  };
  const fulfill = (route, data, status = 200) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(data),
  });
  page.on('pageerror', error => errors.push(error.stack || String(error)));
  await page.route('**/admin/api/planning/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const body = request.postDataJSON();
    const record = { pathname: url.pathname, search: url.search, method: request.method(),
      headers: request.headers(), body };
    requests.push(record);
    if (request.method() === 'GET' && url.pathname.endsWith('/today') && holdToday) {
      heldToday.push(route); publishHeld(); return;
    }
    if (request.method() === 'POST' && url.pathname.endsWith('/tasks') && holdCreate) {
      heldCreates.push(route); publishHeld(); return;
    }
    let data = {};
    let status = 200;
    if (request.method() === 'GET') {
      if (url.pathname.endsWith('/today') && todayFailures > 0) {
        todayFailures -= 1; status = 503; data = { error: '验收：列表更新暂时失败' };
      } else if (url.pathname.endsWith('/today')) data = todayOverride || {
        progress: [progress, resident], attention: [timeout], done: [done], conflicts: [], recompute: {},
      };
      else if (url.pathname.endsWith('/tasks')) data = tasks;
      else if (url.pathname.endsWith('/occurrences')) data = occurrences.filter(item =>
        (!url.searchParams.get('task_type') || item.task_type === url.searchParams.get('task_type'))
        && (!url.searchParams.get('status') || item.status === url.searchParams.get('status'))
        && (!url.searchParams.get('schedule_date') || item.schedule_date === url.searchParams.get('schedule_date')));

    }
    await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(data) });
  });
  await fs.mkdir(evidenceDir, { recursive: true });
  const assertWithin = async selector => {
    const box = await page.locator(selector).boundingBox();
    assert.ok(box, `${selector} must be visible`);
    const viewport = page.viewportSize();
    assert.ok(box.x >= -1 && box.y >= -1 && box.x + box.width <= viewport.width + 1
      && box.y + box.height <= viewport.height + 1, `${selector} must fit the viewport`);
  };
  try {
    await page.goto(`${baseUrl}/__planning_latency__`);
    await page.addStyleTag({ url: `${baseUrl}/admin/css/style.css?v=20261005-ring-fix5` });
    await page.evaluate(async () => {
      document.documentElement.dataset.theme = 'day';
      const root = document.createElement('main');
      root.id = 'planning-fixture'; root.style.padding = '20px';
      document.body.append(root);
      window.__planning = (await import('/admin/js/pages/planning.js?v=20261005-ring-fix5')).default;
      await window.__planning.mount(root);
    });

    const createTask = async () => {
      await page.locator('[data-act="new-task"]').click();
      await page.locator('#pf-content').fill('创建延迟验收');
      await page.locator('#pf-estimated').fill('30m');
      await page.locator('.modal [data-ok]').click();
      await page.locator('.modal').waitFor({ state: 'detached' });
    };
    const remount = () => page.evaluate(async () => {
      const root = document.getElementById('planning-fixture');
      window.__planning.unmount(); root.replaceChildren();
      await window.__planning.mount(root);
    });
    const switchAndRead = tab => page.evaluate(async tab => {
      window.__planning.switchTab(tab);
      await window.__planning.refreshVisible();
    }, tab);
    const getReads = start => requests.slice(start).filter(request => request.method === 'GET');
    const currentBoard = content => ({
      progress: [{ ...progress, content }], attention: [timeout], done: [done], conflicts: [], recompute: {},
    });

    let before = requests.length;
    await createTask();
    await page.waitForFunction(() => !window.__planning.reads.needsRead('today'));
    assert.deepEqual(getReads(before).map(request => request.pathname), ['/admin/api/planning/today']);
    assert.equal(await page.evaluate(() => window.__planning.reads.needsRead('tasks')), true);
    before = requests.length;
    await switchAndRead('all');
    assert.deepEqual(getReads(before).map(request => request.pathname).sort(), [
      '/admin/api/planning/occurrences', '/admin/api/planning/tasks',
    ]);
    before = requests.length;
    await createTask();
    await page.waitForFunction(() => !window.__planning.reads.needsRead('tasks')
      && !window.__planning.reads.needsRead('occurrences'));
    assert.deepEqual(getReads(before).map(request => request.pathname).sort(), [
      '/admin/api/planning/occurrences', '/admin/api/planning/tasks',
    ]);
    assert.equal(await page.evaluate(() => window.__planning.reads.needsRead('today')), true);
    before = requests.length;
    await switchAndRead('today');
    assert.deepEqual(getReads(before).map(request => request.pathname), ['/admin/api/planning/today']);
    await switchAndRead('goals');
    before = requests.length;
    await createTask();
    assert.deepEqual(getReads(before), []);
    before = requests.length;
    await switchAndRead('all');
    assert.deepEqual(getReads(before).map(request => request.pathname).sort(), [
      '/admin/api/planning/occurrences', '/admin/api/planning/tasks',
    ]);
    covered.push('Creation: today reads only today; all reads tasks/occurrences; placeholder saves defer reads; invalidated tabs refresh on re-entry');

    await switchAndRead('today');
    const keptHtml = await page.locator('#planning-progress').innerHTML();
    todayFailures = 1;
    before = requests.length;
    await createTask();
    await page.locator('#planning-load-feedback').waitFor({ state: 'visible' });
    assert.ok((await page.locator('#planning-load-feedback').innerText()).includes('待办已创建，列表更新失败'));
    assert.equal(await page.locator('#planning-progress').innerHTML(), keptHtml);
    for (const [theme, width] of [['day', 1440], ['night', 390]]) {
      await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
      await page.setViewportSize({ width, height: 1000 });
      await page.evaluate(() => {
        window.__planning.detail.closeDrawer();
        document.querySelectorAll('.toast').forEach(toast => toast.remove());
      });
      if (width < 1100) await page.waitForFunction(() =>
        document.querySelector('#planning-layout .detail-panel').getBoundingClientRect().left >= window.innerWidth);
      await assertWithin('#planning-load-feedback [data-act="retry-lists"]');
      await page.screenshot({ path: path.join(evidenceDir, `planning-create-refresh-failure-${theme}-${width}.png`) });
    }
    await page.locator('[data-act="retry-lists"]').click();
    await page.locator('#planning-load-feedback').waitFor({ state: 'hidden' });
    assert.equal(requests.slice(before).filter(request => request.method === 'POST').length, 1);
    await page.setViewportSize({ width: 1440, height: 1000 });
    covered.push('Creation: refresh failure keeps the existing list, reports saved state with styled retry in day/night desktop/phone; retry sends GET only');

    await remount();
    holdToday = true;
    heldToday.length = 0;
    await page.evaluate(() => {
      window.__oldPoll = window.__planning.loadToday({ silent: true });
      window.__samePoll = window.__planning.loadToday({ silent: true });
    });
    await waitHeld(heldToday, 1);
    assert.equal(heldToday.length, 1);
    await createTask();
    await waitHeld(heldToday, 2);
    await page.evaluate(() => { window.__newPoll = window.__planning.loadToday({ silent: true }); });
    assert.equal(heldToday.length, 2);
    await fulfill(heldToday[1], currentBoard('创建后新列表'));
    await page.evaluate(() => window.__newPoll);
    assert.ok((await page.locator('#planning-progress').innerText()).includes('创建后新列表'));
    await fulfill(heldToday[0], currentBoard('创建前旧列表'));
    await page.evaluate(() => Promise.all([window.__oldPoll, window.__samePoll]));
    assert.ok((await page.locator('#planning-progress').innerText()).includes('创建后新列表'));
    assert.ok(!(await page.locator('#planning-progress').innerText()).includes('创建前旧列表'));
    holdToday = false;
    covered.push('Creation: in-flight polls coalesce; saving starts a fresh read; slow pre-save response cannot overwrite the fresh board');

    holdCreate = true;
    heldCreates.length = 0;
    before = requests.length;
    await page.locator('[data-act="new-task"]').click();
    await page.locator('#pf-content').fill('关闭在途表单验收');
    await page.locator('#pf-estimated').fill('30m');
    await page.locator('.modal [data-ok]').evaluate(button => { window.__pendingCreateButton = button; });
    await page.locator('.modal [data-ok]').click();
    await waitHeld(heldCreates, 1);
    assert.equal(await page.locator('.modal [data-ok]').innerText(), '正在创建…');
    assert.equal(await page.locator('.modal [data-ok]').isDisabled(), true);
    await page.locator('.modal [data-cancel]').click();
    await page.locator('[data-act="new-task"]').click();
    assert.equal(await page.locator('.modal').count(), 0, 'closing the pending form must not allow another create');
    assert.ok((await page.locator('.toast').last().innerText()).includes('待办正在创建，请等待完成'));
    await page.evaluate(() => window.__pendingCreateButton.onclick());
    assert.equal(heldCreates.length, 1);
    await remount();
    await switchAndRead('all');
    await switchAndRead('today');
    await page.locator('[data-act="new-task"]').click();
    assert.equal(await page.locator('.modal').count(), 0, 'route remount keeps the pending create guard');
    todayOverride = currentBoard('重挂载后创建成功');
    const freshAfterRemount = page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/planning/today'));
    await fulfill(heldCreates[0], { id: 200 });
    await freshAfterRemount;
    await page.waitForFunction(() => window.__pendingCreateButton.textContent === '已创建');
    await page.waitForFunction(() => window.__planning.reads.needsRead('tasks')
      && window.__planning.reads.needsRead('occurrences')
      && !window.__planning.reads.needsRead('today'));
    assert.ok((await page.locator('#planning-progress').innerText()).includes('重挂载后创建成功'));
    await page.evaluate(() => window.__pendingCreateButton.onclick());
    assert.equal(requests.slice(before).filter(request => request.method === 'POST').length, 1);
    holdCreate = false;
    todayOverride = null;
    covered.push('Creation: button reports creating; close/remount retains submit lock; committed write refreshes the new page and invalidates hidden lists; no second POST');

    holdToday = true;
    heldToday.length = 0;
    await page.evaluate(() => { window.__unmountRead = window.__planning.loadToday(); });
    await waitHeld(heldToday, 1);
    await page.evaluate(() => window.__planning.unmount());
    const unmountedHtml = await page.locator('#planning-fixture').innerHTML();
    await fulfill(heldToday[0], currentBoard('卸载后不应显示'));
    await page.evaluate(() => window.__unmountRead);
    assert.equal(await page.locator('#planning-fixture').innerHTML(), unmountedHtml);
    assert.equal(await page.evaluate(() => window.__planning.pollTimer), null);
    holdToday = false;
    await remount();
    covered.push('Creation: a pending read finishing after unmount cannot write DOM or restart polling');


    // Integration guard: memo's lazy mount and flush survive the reads refactor.
    await page.evaluate(() => {
      window.__memoEvents = [];
      window.__planning.memo = {
        mount: root => window.__memoEvents.push(['mount', Boolean(root)]),
        // show() 参与页签重返读取（BUG-08）；假件缺 show 会让第二次
        // switchTab('memo') 调用不存在的方法
        show: async () => window.__memoEvents.push(['show']),
        flushPending: () => window.__memoEvents.push(['flush']),
        dispose: () => window.__memoEvents.push(['dispose']),
      };
      window.__planning.switchTab('memo');
      window.__planning.switchTab('today');
      window.__planning.switchTab('memo');
      window.__planning.unmount();
    });
    assert.deepEqual(await page.evaluate(() => window.__memoEvents), [
      ['mount', true], ['flush'], ['show'], ['dispose'],
    ]);
    covered.push('Integration: memo mounts once on entry, flushes on exit and disposes on route unmount');
    assert.deepEqual(errors, [], 'No uncaught browser errors');
    return covered;
  } finally {
    await context.close();
  }
}

module.exports = { runPlanningTests };
