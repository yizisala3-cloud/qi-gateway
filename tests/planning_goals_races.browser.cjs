// Deterministic regressions for stale goal dialogs, pending-write receipts and
// short-but-clipped mobile headings. Requests run against the disposable local
// PostgreSQL preview; route gates control delivery without time-based sleeps.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const BASE = 'http://127.0.0.1:8021';
const phase = process.argv[2] || 'after';
const output = path.resolve(process.argv[3] || 'tests/goals-races-20261016');
const defer = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };

(async () => {
  await fs.mkdir(output, { recursive: true });
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const results = [], errors = [], gates = [], ownGoals = [];
  const api = async (suffix, method = 'GET', data = null) => {
    const response = await context.request.fetch(`${BASE}/admin/api/planning/${suffix}`, {
      method, headers: { Authorization: 'Bearer local-goals-preview', 'Idempotency-Key': randomUUID() },
      ...(data ? { data } : {}),
    });
    assert.ok(response.ok(), `${method} ${suffix}: ${response.status()} ${await response.text()}`);
    return response.json();
  };
  const create = async (content, fields = {}) => {
    const goal = await api('goals', 'POST', { content, target_value: '1.5', unit: '页', planned_count: 10, ...fields });
    ownGoals.push(goal.id); return goal;
  };
  const pageFor = async (goal, viewport = null) => {
    const page = await context.newPage();
    page.on('pageerror', error => errors.push(error.message));
    if (viewport) await page.setViewportSize(viewport);
    await page.goto(`${BASE}/admin/#/planning`);
    await page.locator('[data-tab="goals"]').click();
    await page.locator(`.goal-card[data-goal-id="${goal.id}"]`).click();
    return page;
  };
  const frames = page => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const titles = page => page.locator('.modal:visible .modal-head h3').allTextContents();
  const holdGoal = async (page, goalId) => {
    const entered = defer(), release = defer(); gates.push(release);
    let held = false;
    await page.route(`**/admin/api/planning/goals/${goalId}`, async route => {
      if (held || route.request().method() !== 'GET') return route.continue();
      held = true;
      const response = await route.fetch();
      entered.resolve(); await release.promise;
      await route.fulfill({ response });
    });
    return { entered, release };
  };
  try {
    const goal = await create('异步回归目标-' + randomUUID().slice(0, 8) + '：' + '阅读目标完整内容。'.repeat(8));
    const candidates = await api('goals/candidates');
    const task = candidates.find(row => !row.goal_id);
    assert.ok(task, 'preview needs one free recurring task');
    let fresh = await api(`goals/${goal.id}`);
    await api(`goals/${goal.id}/link`, 'POST', { task_id: task.id, expected_version: fresh.version });
    fresh = await api(`goals/${goal.id}`);
    await api(`goals/${goal.id}/link`, 'POST', { task_id: null, expected_version: fresh.version });
    fresh = await api(`goals/${goal.id}`);
    await api(`goals/${goal.id}/link`, 'POST', { task_id: task.id, expected_version: fresh.version });

    // Every dialog-opening action shares the same invalidation requirement.
    for (const action of ['edit', 'adjust', 'full', 'history', 'link', 'new-task', 'delete', 'unlink']) {
      const page = await pageFor(goal);
      const gate = await holdGoal(page, goal.id);
      await page.locator(`[data-goal-act="${action}"]`).click();
      await gate.entered.promise;
      await page.locator('[data-tab="today"]').click();
      await page.locator('[data-tab="goals"]').click();
      await page.locator('[data-goal-act="create"]').click();
      await page.locator('#goal-content').fill('刚打开的新草稿');
      const response = page.waitForResponse(item => item.url().endsWith(`/goals/${goal.id}`));
      gate.release.resolve(); await response; await frames(page);
      const modalTitles = await titles(page);
      const preserved = await page.locator('#goal-content').first().inputValue();
      const passed = modalTitles.length === 1 && modalTitles[0] === '新建目标' && preserved === '刚打开的新草稿';
      results.push({ name: `stale-${action}-after-tab-return`, passed, modalTitles, preserved });
      if (action === 'edit') await page.screenshot({ path: path.join(output, `${phase}-stale-edit.png`), fullPage: true });
      await page.close();
    }

    // The candidate list arrives after a separate goal read has already ended.
    const candidatesPage = await pageFor(goal);
    const candidatesEntered = defer(), candidatesRelease = defer(); gates.push(candidatesRelease);
    await candidatesPage.route('**/admin/api/planning/goals/candidates', async route => {
      const response = await route.fetch(); candidatesEntered.resolve(); await candidatesRelease.promise;
      await route.fulfill({ response });
    });
    await candidatesPage.locator('[data-goal-act="link"]').click(); await candidatesEntered.promise;
    await candidatesPage.locator('[data-tab="today"]').click();
    await candidatesPage.locator('[data-tab="goals"]').click();
    await candidatesPage.locator('[data-goal-act="create"]').click();
    await candidatesPage.locator('#goal-content').fill('候选读取期间的新草稿');
    const candidateResponse = candidatesPage.waitForResponse(item => item.url().endsWith('/goals/candidates'));
    candidatesRelease.resolve(); await candidateResponse; await frames(candidatesPage);
    const candidateTitles = await titles(candidatesPage);
    results.push({ name: 'stale-candidates-after-tab-return', passed: candidateTitles.length === 1 && candidateTitles[0] === '新建目标'
      && await candidatesPage.locator('#goal-content').inputValue() === '候选读取期间的新草稿', modalTitles: candidateTitles });
    await candidatesPage.close();

    // A newer action on the same tab also supersedes an older outstanding read.
    const superseded = await pageFor(goal);
    const older = await holdGoal(superseded, goal.id);
    await superseded.locator('[data-goal-act="full"]').click(); await older.entered.promise;
    await superseded.locator('[data-goal-act="create"]').click();
    await superseded.locator('#goal-content').fill('同页较新的草稿');
    const oldResponse = superseded.waitForResponse(item => item.url().endsWith(`/goals/${goal.id}`));
    older.release.resolve(); await oldResponse; await frames(superseded);
    const newerTitles = await titles(superseded);
    results.push({ name: 'stale-full-after-newer-action', passed: newerTitles.length === 1 && newerTitles[0] === '新建目标', modalTitles: newerTitles });
    await superseded.close();

    const pendingPage = await pageFor(goal);
    const pendingReceived = defer(), pendingRelease = defer(); gates.push(pendingRelease);
    let posts = 0, serverResponded = false;
    await pendingPage.route('**/admin/api/planning/goals', async route => {
      if (route.request().method() !== 'POST') return route.continue();
      posts++; pendingReceived.resolve(); await pendingRelease.promise;
      const response = await route.fetch(); serverResponded = true;
      await route.fulfill({ response });
    });
    await pendingPage.locator('[data-goal-act="create"]').click();
    const pendingName = '等待回执-' + randomUUID().slice(0, 8);
    await pendingPage.locator('#goal-content').fill(pendingName);
    await pendingPage.locator('#goal-value').fill('5');
    await pendingPage.locator('#goal-unit').fill('页');
    await pendingPage.locator('#goal-planned').fill('10');
    await pendingPage.locator('.modal [data-save]').click(); await pendingReceived.promise;
    await pendingPage.locator('.modal [data-cancel]').click();
    await pendingPage.locator('[data-tab="today"]').click();
    await pendingPage.locator('[data-tab="goals"]').click();
    await pendingPage.locator('[data-goal-act="recover"]').click(); await frames(pendingPage);
    const premature = await pendingPage.locator('.toast').filter({ hasText: '原操作已确认' }).count();
    const beforeAck = { serverResponded, premature, posts };
    await pendingPage.screenshot({ path: path.join(output, `${phase}-pending-write.png`), fullPage: true });
    pendingRelease.resolve();
    await pendingPage.locator('.goal-card').filter({ hasText: pendingName }).waitFor();
    await pendingPage.locator('[data-goal-act="recover"]').waitFor({ state: 'detached' });
    const createdPending = (await api('goals')).filter(row => row.content === pendingName);
    ownGoals.push(...createdPending.map(row => row.id));
    results.push({ name: 'pending-recovery-waits-for-real-ack', passed: premature === 0 && !beforeAck.serverResponded && posts === 1 && createdPending.length === 1,
      beforeAck, finalPosts: posts, createdCount: createdPending.length });
    await pendingPage.close();

    const shortGoal = await create('手机窄屏验证的目标文字'.repeat(4));
    const phone = await pageFor(shortGoal, { width: 320, height: 640 });
    await phone.waitForFunction(() => {
      const rect = document.querySelector('.detail-panel')?.getBoundingClientRect();
      return rect && rect.top >= 0 && rect.bottom <= innerHeight + 1;
    });
    const clipping = await phone.locator('.goal-heading').evaluate(el => ({ length: el.textContent.length,
      scrollHeight: el.scrollHeight, clientHeight: el.clientHeight, clipped: el.scrollHeight > el.clientHeight }));
    const fullCount = await phone.locator('[data-goal-act="full"]:visible').count();
    if (fullCount) {
      await phone.locator('[data-goal-act="full"]').click();
      await phone.locator('.goal-full-text').waitFor();
      assert.ok((await phone.locator('.goal-full-text').innerText()).includes(shortGoal.content));
      await phone.locator('.modal .modal-close').click();
    }
    results.push({ name: 'short-clipped-mobile-heading-has-full-text', passed: clipping.length <= 60 && clipping.clipped && fullCount === 1, clipping, fullCount });
    await phone.screenshot({ path: path.join(output, `${phase}-short-heading.png`), fullPage: true });
    await phone.close();

    // The full-text entry survives width changes and character/number widths.
    const descriptions = [
      { content: '目标'.repeat(10) },
      { content: '目标阅读Study '.repeat(4) },
      { content: '积累Read '.repeat(8) + '完成目标' },
      { content: '每天积累阅读和运动记录', target_value: '123456789012345.123456789', unit: '累计有效阅读分钟与页数' },
    ];
    for (const fields of descriptions) {
      const item = await create(fields.content, fields);
      const responsive = await pageFor(item, { width: 320, height: 740 });
      for (const width of [320, 390]) {
        await responsive.setViewportSize({ width, height: 740 }); await frames(responsive);
        const heading = await responsive.locator('.goal-heading').evaluate(el => ({ text: el.textContent,
          clipped: el.scrollHeight > el.clientHeight, length: el.textContent.length }));
        await responsive.locator('[data-goal-act="full"]').click();
        await responsive.locator('.goal-full-text').waitFor();
        const fullText = await responsive.locator('.goal-full-text').textContent();
        results.push({ name: `responsive-full-text-${fields.content.length}-chars-${width}px`,
          passed: fullText === heading.text, heading });
        await responsive.locator('.modal .modal-close').click();
      }
      await responsive.close();
    }

    const report = { phase, results, errors };
    await fs.writeFile(path.join(output, `${phase}.json`), JSON.stringify(report, null, 2));
    process.stdout.write(JSON.stringify(report) + '\n');
    assert.deepEqual(errors, []);
    assert.ok(results.every(result => result.passed), `${results.filter(result => !result.passed).length} regressions reproduced`);
  } finally {
    for (const gate of gates) gate.resolve();
    for (const id of ownGoals) {
      try { const goal = await api(`goals/${id}`); await api(`goals/${id}`, 'DELETE', { expected_version: goal.version }); }
      catch (error) { process.stderr.write(JSON.stringify({ location: 'goal-race-fixture-cleanup', goalId: id, stack: error.stack }) + '\n'); }
    }
    await browser.close();
  }
})().catch(error => { process.stderr.write(JSON.stringify({ location: 'planning_goals_races.browser', stack: error.stack }) + '\n'); process.exitCode = 1; });
