const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

(async () => {
  const root = process.cwd();
  const version = (await fs.readFile(path.join(root, 'admin/js/ui.js'), 'utf8')).match(/ASSET_VERSION = '([^']+)'/)[1];
  const server = http.createServer(async (req, res) => {
    if (req.url === '/') { res.setHeader('Content-Type', 'text/html'); return res.end('<!doctype html><html><body></body></html>'); }
    const target = path.resolve(root, '.' + new URL(req.url, 'http://127.0.0.1').pathname);
    if (!target.startsWith(root + path.sep)) { res.statusCode = 403; return res.end(); }
    try { res.setHeader('Content-Type', 'text/javascript'); res.end(await fs.readFile(target)); }
    catch { res.statusCode = 404; res.end(); }
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  const scenarios = [], errors = [];
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}/`);
    for (const tab of ['today', 'all']) {
      const result = await page.evaluate(async ({ version, tab }) => {
        const source = (await import(`/admin/js/pages/planning.js?v=${version}`)).default;
        const root = document.createElement('div'); document.body.appendChild(root);
        const el = document.createElement('div');
        el.innerHTML = '<div class="detail-title"></div><div class="detail-badges"></div><div data-body></div><div data-foot></div>';
        root.appendChild(el);
        const body = el.querySelector('[data-body]'), foot = el.querySelector('[data-foot]');
        const row = { id: 20, task_id: 3, task_type: 'daily', content: 'Native DOM occurrence', status: 'pending',
          task_is_active: true, estimated_minutes: 30, alarm_start: false, alarm_end: false, timer_minutes: null,
          updated_at: '2026-10-25T08:00:00Z' };
        const other = { id: 20, content: 'Unrelated task', task_type: 'daily', is_active: true, refresh_enabled: false,
          updated_at: '2026-10-25T08:00:00Z' };
        const page = Object.assign(Object.create(source), { root, activeTab: tab, tasks: [other], occurrences: [row],
          selected: { kind: 'occ', id: 20 }, board: { date: '2026-10-25', progress: [row], attention: [], done: [] },
          detailEdit: { isActive: () => false }, renderBoard() {}, renderTasks() {}, renderOccurrences() {},
          detail: { el, body, foot, render({ title, badges, html, actions }) {
            el.querySelector('.detail-title').innerHTML = title; el.querySelector('.detail-badges').innerHTML = badges;
            body.innerHTML = html; foot.innerHTML = actions;
          } },
        });
        await page.showOccurrenceDetail(row);
        const timer = body.querySelector('[data-timer-input]'), start = body.querySelector('[data-alarm-start]'), end = body.querySelector('[data-alarm-end]');
        timer.value = '17'; timer.focus(); start.checked = true;
        page.applyCommittedReceipt({ ...row, status: 'in_progress', alarm_end: true, timer_minutes: 8,
          updated_at: '2026-10-25T08:01:00Z' });
        return { timer: timer.value, timerSame: timer === body.querySelector('[data-timer-input]'), focused: document.activeElement === timer,
          dirtyAlarm: start.checked, cleanAlarm: end.checked, taskSame: page.tasks[0] === other,
          finishVisible: !!foot.querySelector('[data-act="occ-finish"]') };
      }, { version, tab });
      assert.deepEqual(result, { timer: '17', timerSame: true, focused: true, dirtyAlarm: true,
        cleanAlarm: true, taskSame: true, finishVisible: true });
      scenarios.push({ tab, ...result });
    }
    assert.deepEqual(errors, []);
    const output = { browser: 'headless msedge', scenarios, pageErrors: errors };
    await fs.writeFile(path.join(__dirname, 'native-dom-result.json'), JSON.stringify(output, null, 2));
    process.stdout.write(JSON.stringify(output, null, 2) + '\n');
  } finally {
    await browser?.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
