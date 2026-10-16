// Actual admin UI + actual goal/task HTTP handlers on disposable PostgreSQL.
// Route interception below injects lost responses/association failures only.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const BASE = 'http://127.0.0.1:8021';
const TOKEN = 'local-goals-preview';
const output = path.resolve(process.argv[2] || 'tests/goals-20261016');

(async () => {
  await fs.mkdir(output, { recursive: true });
  const fixture = JSON.parse(await fs.readFile('.venv/goals-preview-fixture.json', 'utf8'));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [], results = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(token => localStorage.setItem('qi-token', token), TOKEN);
  const api = async (suffix, method = 'GET', data = null, key = null) => {
    const response = await context.request.fetch(`${BASE}/admin/api/planning/${suffix}`, {
      method, headers: { Authorization: `Bearer ${TOKEN}`, ...(key ? { 'Idempotency-Key': key } : {}) },
      ...(data ? { data } : {}),
    });
    assert.ok(response.ok(), `${method} ${suffix}: ${response.status()} ${await response.text()}`);
    return response.json();
  };
  const showGoals = async () => {
    await page.locator('[data-act="plan-tab"][data-tab="goals"]').click();
    await page.locator('.goal-card').first().waitFor();
  };
  const choose = async text => {
    await page.locator('.goal-card').filter({ hasText: text }).click();
    await page.locator('.goal-actions').waitFor();
  };
  const accept = async () => { await page.locator('.modal:visible [data-ok]').click(); };
  const makeGoal = async (name, count = '40') => {
    await page.locator('[data-goal-act="create"]').click();
    await page.locator('#goal-content').fill(name);
    await page.locator('#goal-value').fill('200.25');
    await page.locator('#goal-unit').fill('页');
    await page.locator('#goal-planned').fill(count);
    await page.locator('.modal [data-save]').click();
    await page.locator('.goal-card').filter({ hasText: name }).waitFor();
  };
  try {
    await page.goto(`${BASE}/admin/#/planning`);
    await showGoals();
    assert.equal(await page.locator('[data-goal-act="create"]').isVisible(), true);
    await choose('完成阅读计划');
    await page.locator('[data-goal-act="adjust"]').click();
    await page.locator('#goal-count').fill('19');
    await page.locator('.modal [data-save]').click();
    await page.locator('.goal-card').filter({ hasText: '19/40' }).waitFor();
    await api(`occurrences/${fixture.ordinary}/finish`, 'POST', {});
    await page.locator('[data-act="plan-tab"][data-tab="today"]').click();
    await showGoals();
    await page.locator('.goal-card').filter({ hasText: '20/40' }).waitFor();
    results.push('manual correction + actual completion + tab re-entry = 20/40');

    const longName = '浏览器验收：' + '阅读与长期积累，保留完整目标描述。'.repeat(8);
    await makeGoal(longName);
    const created = (await api('goals')).find(goal => goal.content === longName);
    await choose(longName);
    await page.locator('[data-goal-act="adjust"]').click();
    await page.locator('#goal-count').fill('-1');
    await page.locator('.modal [data-save]').click();
    await page.getByText('已执行次数须为非负整数', { exact: true }).waitFor();
    assert.equal(await page.locator('#goal-count').inputValue(), '-1');
    await page.locator('#goal-count').fill('44');
    await page.locator('.modal [data-save]').click();
    await page.locator('.goal-card').filter({ hasText: '44/40' }).waitFor();
    assert.equal((await api(`goals/${created.id}`)).percentage, 110);
    assert.equal(await page.locator('.goal-card').filter({ hasText: longName }).locator('[role="progressbar"]').getAttribute('aria-valuenow'), '100');
    await page.screenshot({ path: path.join(output, 'desktop.png'), fullPage: true });
    results.push('decimal value, long content, validation, 44/40 = 110% with full bar');

    // Create succeeds, association definitively fails. Retrying never creates.
    let failLink = true;
    await page.route('**/admin/api/planning/goals/*/link', async route => {
      if (failLink) { failLink = false; return route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: '验收注入：关联冲突' }) }); }
      return route.continue();
    });
    const beforeTasks = await api('tasks');
    await page.locator('[data-goal-act="new-task"]').click();
    await page.locator('#pf-content').fill('目标内新建的每日阅读');
    const typeButton = page.locator('[data-retro-select="pf-type"] button').first();
    await typeButton.click();
    assert.equal(await page.locator('.retro-select-pop:visible [role="option"]').count(), 4);
    await page.screenshot({ path: path.join(output, 'task-types.png'), fullPage: true });
    await page.keyboard.press('Escape');
    await page.locator('#pf-estimated').fill('20');
    await page.locator('.modal [data-ok]').click();
    await page.locator('[data-goal-act="recover-task"]').waitFor();
    assert.equal((await api('tasks')).length, beforeTasks.length + 1);
    await page.locator('[data-goal-act="recover-task"]').click();
    await page.locator('[data-goal-act="recover-task"]').waitFor({ state: 'detached' });
    assert.equal((await api('tasks')).length, beforeTasks.length + 1);
    assert.ok((await api(`goals/${created.id}`)).current_link);
    await page.unroute('**/admin/api/planning/goals/*/link');
    results.push('task created + association failure + retry keeps exactly one task');

    // An already-created task loses its response. Persisted recovery after reload
    // must confirm the original key/body, then link that same task.
    await makeGoal('创建结果未知恢复');
    const unknownGoal = (await api('goals')).find(goal => goal.content === '创建结果未知恢复');
    await choose('创建结果未知恢复');
    const unknownBefore = (await api('tasks')).length;
    let lostCreate = false;
    await page.route('**/admin/api/planning/tasks', async route => {
      if (route.request().method() === 'POST' && !lostCreate) {
        lostCreate = true;
        const response = await route.fetch(); assert.equal(response.status(), 201);
        await route.abort('failed');
      } else await route.continue();
    });
    await page.locator('[data-goal-act="new-task"]').click();
    await page.locator('#pf-content').fill('响应丢失但已经落库的待办');
    await page.locator('#pf-estimated').fill('10');
    await page.locator('.modal [data-ok]').click();
    await page.getByRole('button', { name: '核实创建结果', exact: true }).last().waitFor();
    await page.reload();
    await showGoals();
    await page.locator('[data-goal-act="recover-task"]').click();
    await page.locator('[data-goal-act="recover-task"]').waitFor({ state: 'detached' });
    assert.equal((await api('tasks')).length, unknownBefore + 1);
    assert.ok((await api(`goals/${unknownGoal.id}`)).current_link);
    await page.unroute('**/admin/api/planning/tasks');
    results.push('task response lost after commit, reload/recover uses same task');

    // Goal creation lost response has the same durable operation recovery.
    let lostGoal = false;
    const beforeGoals = (await api('goals')).length;
    await page.route('**/admin/api/planning/goals', async route => {
      if (route.request().method() === 'POST' && !lostGoal) {
        lostGoal = true; const response = await route.fetch(); assert.equal(response.status(), 201);
        await route.abort('failed');
      } else await route.continue();
    });
    await page.locator('[data-goal-act="create"]').click();
    await page.locator('#goal-content').fill('目标响应丢失后恢复');
    await page.locator('#goal-value').fill('1.25');
    await page.locator('#goal-unit').fill('h');
    await page.locator('#goal-planned').fill('5');
    await page.locator('.modal [data-save]').click();
    await page.getByRole('button', { name: '核实保存结果', exact: true }).last().waitFor();
    await page.reload(); await showGoals();
    await page.locator('[data-goal-act="recover"]').click();
    await page.locator('[data-goal-act="recover"]').waitFor({ state: 'detached' });
    assert.equal((await api('goals')).length, beforeGoals + 1);
    await page.unroute('**/admin/api/planning/goals');
    results.push('goal response lost after commit, reload/recover does not duplicate');

    // Existing-task selector and all responsive detail layouts, including a
    // long heading and the expanded paper selector in both themes.
    for (const [width, height, label] of [[1440, 1000, 'desktop'], [900, 800, 'tablet'], [390, 844, 'mobile'], [320, 640, 'narrow'], [390, 568, 'short']]) {
      await page.setViewportSize({ width, height });
      await page.evaluate(theme => document.documentElement.setAttribute('data-theme', theme),
        ['narrow', 'short'].includes(label) ? 'night' : 'day');
      await choose(longName);
      // Wait for the shared drawer's real entry/viewport transition to settle.
      await page.waitForFunction(() => {
        const r = document.querySelector('.detail-panel')?.getBoundingClientRect();
        return r && r.left >= -1 && r.top >= -1 && r.right <= innerWidth + 1 && r.bottom <= innerHeight + 1;
      });
      const box = await page.locator('.detail-panel').boundingBox();
      assert.ok(box && box.x >= -1 && box.y >= -1 && box.x + box.width <= width + 1 && box.y + box.height <= height + 1, `${label}: detail outside viewport ${JSON.stringify(box)}`);
      const title = await page.locator('.detail-titles').boundingBox();
      const badges = await page.locator('.detail-badges').boundingBox();
      assert.ok(badges.y + badges.height <= title.y + title.height + 1, `${label}: achievement badge clipped by long heading`);
      const buttons = await page.locator('.goal-actions button').evaluateAll(elements => elements.map(el => {
        const r = el.getBoundingClientRect(); return { text: el.textContent, top: r.top, bottom: r.bottom, left: r.left, right: r.right };
      }));
      for (const button of buttons) assert.ok(button.top >= 0 && button.bottom <= height + 1 && button.left >= 0 && button.right <= width + 1, `${label}: inaccessible action ${JSON.stringify(button)}`);
      assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), `${label}: horizontal overflow`);
      await page.locator('[data-goal-act="full"]').click();
      await page.locator('.goal-full-text').waitFor();
      assert.ok((await page.locator('.goal-full-text').innerText()).includes(longName));
      await page.locator('.modal:visible .modal-close').click();
      await page.screenshot({ path: path.join(output, `${label}-detail.png`), fullPage: true });
      await page.locator('[data-goal-act="link"]').click();
      await page.locator('[data-goal-candidates] button').first().click();
      const menu = await page.locator('.retro-select-pop:visible').boundingBox();
      assert.ok(menu.x >= -1 && menu.x + menu.width <= width + 1 && menu.y >= -1 && menu.y + menu.height <= height + 1, `${label}: menu outside viewport`);
      await page.screenshot({ path: path.join(output, `${label}-selector.png`), fullPage: true });
      await page.keyboard.press('Escape');
      await page.locator('.modal [data-cancel]').click();
      if (width <= 1100) await page.keyboard.press('Escape');
    }
    results.push('1440/900/390/320 widths, short viewport, full long content and day/night expanded selectors');
    await page.setViewportSize({ width: 1440, height: 1000 });
    await choose(longName);
    const linkedTask = (await api(`goals/${created.id}`)).current_link.task_id;
    await page.locator('[data-goal-act="delete"]').click(); await accept();
    await page.locator('.goal-card').filter({ hasText: longName }).waitFor({ state: 'detached' });
    assert.equal((await api(`tasks/${linkedTask}`)).id, linkedTask);
    results.push('confirmed goal deletion retains the linked task');
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ results, errors }, null, 2));
    process.stdout.write(JSON.stringify({ results, errors }) + '\n');
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png'), fullPage: true });
    await fs.writeFile(path.join(output, 'failure.json'), JSON.stringify({ results, errors, error: error.stack, body: await page.locator('body').innerText() }, null, 2));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(JSON.stringify({ location: 'planning_goals.browser', stack: error.stack }) + '\n'); process.exitCode = 1; });
