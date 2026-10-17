// Review #58/#59: real UI/API/PG, transport loss only is intercepted.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const BASE = 'http://127.0.0.1:8026', TOKEN = 'local-optimization-preview';
const output = path.resolve(process.env.PLANNING_BROWSER_OUTPUT || 'tests/planning-fixes-20261017/browser');

(async () => {
  await fs.mkdir(output, { recursive: true });
  const fixture = JSON.parse(await fs.readFile('.venv/optimization-preview-fixture.json', 'utf8'));
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage(), passed = [], errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(token => localStorage.setItem('qi-token', token), TOKEN);
  const api = async (suffix, method = 'GET', data) => {
    const response = await context.request.fetch(`${BASE}/admin/api/planning/${suffix}`, {
      method, headers: { Authorization: `Bearer ${TOKEN}` }, ...(data ? { data } : {}),
    });
    assert.ok(response.ok(), `${suffix}: ${response.status()} ${await response.text()}`);
    return response.json();
  };
  const open = async (kind, id) => {
    await page.locator(`[data-${kind}="${id}"]`).click();
    await page.locator('.detail-title [data-act="task-detail-edit"]').click();
    await page.locator('[data-edit-title]').waitFor();
  };
  const activate = async field => page.locator(`[data-act="task-detail-edit"][data-field="${field}"]`).click();
  const cancel = () => page.locator('[data-edit-title]:visible, #pe-note:visible, [data-edit-field]:visible, [data-inline-window] button:visible').first().press('Escape');
  try {
    await page.goto(`${BASE}/admin/#/planning`);
    await page.locator('#planning-progress .plan-item').first().waitFor();
    for (const status of [409, 400]) {
      const ids = fixture.ids[status === 409 ? 'started' : 'ordinary'];
      await open('occ', ids.occurrences[0]);
      const draft = `拒绝后保留草稿 ${status}`;
      await page.locator('[data-edit-title]').fill(draft);
      if (status === 400) { await activate('duration'); await page.locator('[data-edit-field="estimated_minutes"]').fill('0'); }
      const requests = [];
      let abortNext = true;
      const routeHandler = async route => {
        if (route.request().method() === 'POST') {
          requests.push({ key: route.request().headers()['idempotency-key'], body: route.request().postData() });
          if (abortNext) { abortNext = false; return route.abort('failed'); }
        }
        return route.continue();
      };
      await page.route('**/tasks/*/detail-edit', routeHandler);
      await page.locator('[data-edit-save]').click();
      await page.locator('[data-edit-feedback]').filter({ hasText: '暂时无法确认' }).waitFor();
      const activeInput = page.locator(status === 409 ? '[data-edit-title]' : '[data-edit-field="estimated_minutes"]');
      assert.equal(await activeInput.isDisabled(), true);
      if (status === 409) await api(`tasks/${ids.task}`, 'PATCH', { content: '其他设备已修改' });
      const rejection = page.waitForResponse(r => r.url().endsWith('/detail-edit') && r.request().method() === 'POST');
      await page.locator('[data-edit-save]').click();
      assert.equal((await rejection).status(), status);
      await page.locator('[data-edit-feedback]').filter({ hasText: '未保存' }).waitFor();
      assert.equal(await activeInput.isDisabled(), false);
      assert.equal(await activeInput.inputValue(), status === 409 ? draft : '0');
      assert.deepEqual(requests[0], requests[1]);
      await page.unroute('**/tasks/*/detail-edit', routeHandler);
      await cancel();
      await page.locator('.detail-title [data-act="task-detail-edit"]').click();
      await page.locator('[data-edit-title]').waitFor();
      assert.equal(await page.locator('[data-edit-title]').isDisabled(), false);
      assert.ok(!(await page.locator('[data-edit-feedback]').textContent()).includes('结果未知'));
      await cancel();
      await page.locator('[data-tab="all"]').click();
      await open('task', ids.task);
      assert.equal(await page.locator('[data-edit-title]').isDisabled(), false);
      await page.locator('[data-edit-title]').fill(`新请求保存成功 ${status}`);
      await page.locator('[data-edit-save]').click();
      await page.locator('[data-edit-save]').waitFor({ state: 'detached' });
      assert.equal((await api(`tasks/${ids.task}`)).content, `新请求保存成功 ${status}`);
      await page.locator('[data-tab="today"]').click();
      passed.push(`#58 unknown → real ${status}: same request, draft retained, cancel/reopen and task entry usable`);
    }

    // Hash navigation unmounts/remounts planning without recreating its singleton.
    await page.evaluate(() => { location.hash = '/config'; });
    await page.locator('#planning-layout').waitFor({ state: 'detached' });
    await page.evaluate(() => { location.hash = '/planning'; });
    await page.locator('#planning-progress .plan-item').first().waitFor();
    await page.locator('[data-tab="all"]').click();
    await open('task', fixture.ids.started.task);
    assert.equal(await page.locator('[data-edit-title]').isDisabled(), false);
    await cancel();
    passed.push('#58 known rejection stays resolved after unmount/remount');

    const board = await api('today');
    const futureDay = new Date(new Date(board.now).getTime() + 32 * 3600000).toISOString().slice(0, 10);
    for (const target_date of [futureDay, null]) {
      const task = await api('tasks', 'POST', { content: `未生成单次 ${target_date}`, task_type: 'once',
        target_date, time_mode: 'duration', estimated_minutes: 30 });
      assert.equal((await api(`tasks/${task.id}/detail-edit`)).occurrences.length, 0);
      await page.locator('[data-tab="today"]').click();
      await page.locator('[data-tab="all"]').click();
      // The fixture was created out of band; explicitly refresh the cached list.
      await page.evaluate(async () => (await import('/admin/js/pages/planning.js?v=20261016-planning-optimization')).default.loadTasks({ fresh: true }));
      await open('task', task.id);
      await activate('duration');
      assert.equal(await page.locator('[data-edit-field="estimated_minutes"]').isDisabled(), false);
      await activate('window');
      assert.equal(await page.locator('[data-retro-for="pe-window-start"] button').first().isDisabled(), !target_date);
      assert.ok(!(await page.locator('[data-edit-save]').getAttribute('data-tooltip')).includes('已开始或结束'));
      await activate('duration');
      await page.locator('[data-edit-field="estimated_minutes"]').fill('45');
      if (target_date) {
        await activate('window');
        await page.locator('#pe-window-start').evaluate(input => input._applyRetroValue('10:00', true));
        await page.locator('#pe-window-end').evaluate(input => input._applyRetroValue('12:00', true));
      }
      await page.screenshot({ path: path.join(output, target_date ? 'future-once-edit.png' : 'resident-once-edit.png') });
      await page.locator('[data-edit-save]').click();
      await page.locator('[data-edit-save]').waitFor({ state: 'detached' });
      const saved = await api(`tasks/${task.id}`);
      assert.equal(saved.estimated_minutes, 45);
      assert.equal(saved.window_start_tod, target_date ? '10:00:00' : null);
      passed.push(`#59 ungenerated ${target_date ? 'dated once duration/window' : 'resident once duration only'} saved through actual UI`);
    }
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'browser-results.json'), JSON.stringify({ passed, errors }, null, 2));
    await fs.rm(path.join(output, 'browser-failure.json'), { force: true });
    process.stdout.write(JSON.stringify({ passed, errors }, null, 2) + '\n');
  } catch (error) {
    await page.screenshot({ path: path.join(output, 'failure.png') });
    await fs.writeFile(path.join(output, 'browser-failure.json'), JSON.stringify({ passed, errors, error: error.stack }, null, 2));
    throw error;
  } finally { await browser.close(); }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
