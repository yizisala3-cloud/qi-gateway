const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const repo = path.resolve(__dirname, '..');
const out = process.argv[2] || path.join(repo, 'tests', 'state-sync-browser');
fs.mkdirSync(out, { recursive: true });
const results = { environment: 'isolated headless Edge, production modules and CSS, mocked HTTP, Asia/Shanghai browser timezone', checks: [], pageErrors: [] };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <link rel="stylesheet" href="/admin/css/style.css"><script src="/admin/assets/vendor/marked-12.0.2.min.js"></script>
      <script src="/admin/assets/vendor/purify-3.1.6.min.js"></script></head><body><main id="fixture" style="margin:140px 20px 0"></main>
      <script type="module">import planning from '/admin/js/pages/planning.js'; window.planning = planning; window.ready = true;</script></body></html>`);
    return;
  }
  const target = path.resolve(repo, '.' + decodeURIComponent(url.pathname));
  if (!target.startsWith(path.resolve(repo) + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(target, (err, data) => {
    if (err) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', { '.js': 'text/javascript', '.css': 'text/css' }[path.extname(target)] || 'application/octet-stream');
    res.end(data);
  });
});
async function install(page) {
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.waitForFunction(() => window.ready);
  await page.evaluate(async () => {
    window.calls = [];
    window.gates = {};
    window.failRead = false;
    window.board = { progress: [7, 8].map((id, index) => ({ id, task_id: id, content: `Task ${id}`, task_type: 'daily',
      status: 'pending', sort_order: index, schedule_label: 'normal', estimated_minutes: 30,
      est_start: `2026-10-12T${index ? '13' : '12'}:00:00+08:00`, est_end: `2026-10-12T${index ? '13' : '12'}:30:00+08:00`,
      display_cycle_date: '2026-10-12', schedule_date: '2026-10-12' })),
      attention: [], done: [], conflicts: [], recompute: { enabled: true, pending: false } };
    window.baseFetch = async (raw, opts = {}) => {
      const route = new URL(String(raw), location.origin).pathname;
      calls.push({ route, method: opts.method || 'GET' });
      if (route.endsWith('/planning/today')) return failRead
        ? Response.json({ error: 'Read failed' }, { status: 503 }) : Response.json(board);
      return Response.json([]);
    };
    window.fetch = baseFetch;
    await planning.mount(document.querySelector('#fixture'));
    clearInterval(planning.pollTimer);
    planning.pollTimer = null;
    window.beginSort = () => {
      planning.enterReorder();
      const list = planning.progressList;
      list.insertBefore(list.lastElementChild, list.firstElementChild);
      window.confirmation = planning.confirmReorder();
    };
    window.commit = () => {
      board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index,
        est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00`,
        est_end: index ? '2026-10-12T13:00:00+08:00' : '2026-10-12T12:30:00+08:00' }));
      gates.submit(Response.json({ saved: 2, recompute_enabled: true, schedule_applied: true, updated: 2,
        estimates: board.progress }));
    };
  });
}
async function snapshot(page) {
  return page.evaluate(() => ({
    persisted: board.progress.map(o => [o.id, o.est_start]),
    cached: planning.board.progress.map(o => [o.id, o.est_start]),
    dom: [...planning.progressList.querySelectorAll('.plan-item')].map(el => Number(el.dataset.occ)),
    detail: planning.detail.body.textContent,
    timerDraft: planning.detail.body.querySelector('[data-timer-input]')?.value,
    feedback: document.querySelector('#planning-load-feedback').textContent,
    activeTab: planning.activeTab,
    needsRead: planning.reads.needsRead('today'),
    savedRefresh: planning.savedRefresh,
  }));
}
async function check(name, fn) {
  try { await fn(); results.checks.push({ name, passed: true }); }
  catch (error) { results.checks.push({ name, passed: false, error: error.stack }); }
  fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify(results, null, 2));
}
async function setDialogDateTime(page, id, value) {
  const [year, month, day, hour, minute] = value.match(/\d+/g).map(Number);
  await page.locator(`#${id}`).locator('..').locator('.retro-time-field').click();
  const picker = page.locator('.retro-time-pop');
  for (let attempt = 0; attempt < 120; attempt++) {
    const shown = (await picker.locator('.retro-time-title').textContent()).match(/\d+/g).map(Number);
    const distance = (year - shown[0]) * 12 + month - shown[1];
    if (!distance) break;
    const unit = Math.abs(distance) >= 12 ? 'year' : 'month';
    await picker.locator(`[data-nav="${unit}${distance > 0 ? '+' : '-'}"]`).click();
  }
  assert.equal(await picker.locator('.retro-time-title').textContent(), `${year}年${month}月`);
  await picker.locator(`[data-day="${day}"]`).click();
  for (const [unit, selected] of [['hour', hour], ['minute', minute]]) {
    await picker.locator(`[data-unit="${unit}"] .rtp-select-btn`).click();
    await page.keyboard.press('Home');
    for (let index = 0; index < selected; index++) await page.keyboard.press('ArrowDown');
    await page.keyboard.press('Enter');
  }
  await picker.locator('[data-act="ok"]').click();
  assert.equal(await page.locator(`#${id}`).inputValue(), value);
}
async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
    const context = await browser.newContext({ timezoneId: 'Asia/Shanghai', viewport: { width: 1280, height: 720 } });
    const page = await context.newPage();
    page.on('pageerror', error => results.pageErrors.push(error.message));
    await check('R1: switch tab during POST; commit followed by failed GET retains committed data', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort();
        planning.switchTab('goals');
        await planning.loadToday();
        failRead = true;
        commit();
        await confirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      await page.evaluate(async () => { planning.switchTab('today'); await planning.loadToday(); });
      results.tabFailedRead = await snapshot(page);
      await page.screenshot({ path: path.join(out, 'tab-failed-read.png'), fullPage: true });
      assert.deepEqual(results.tabFailedRead.cached, results.tabFailedRead.persisted);
    });
    await check('R1: return to today before POST response; committed times appear before slow GET', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort();
        planning.switchTab('goals');
        await planning.loadToday();
        planning.switchTab('today');
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/today')
          ? new Promise(resolve => { gates.read = resolve; }) : baseFetch(raw, opts);
        commit();
        await confirmation;
      });
      await page.waitForFunction(() => gates.read);
      results.returnSlowRead = await snapshot(page);
      await page.evaluate(() => gates.read(Response.json(board)));
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      assert.deepEqual(results.returnSlowRead.cached, results.returnSlowRead.persisted);
    });
    await check('R2: selected detail updates estimates while preserving an unsaved timer draft', async () => {
      await install(page);
      await page.evaluate(async () => {
        planning.select('occ', 8);
        planning.detail.body.querySelector('[data-timer-input]').value = '45m';
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort();
        failRead = true;
        commit();
        await confirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      results.draftDetail = await snapshot(page);
      await page.screenshot({ path: path.join(out, 'draft-detail.png'), fullPage: true });
      assert.equal(results.draftDetail.timerDraft, '45m');
      assert.match(results.draftDetail.detail, /12:00/);
      assert.doesNotMatch(results.draftDetail.detail, /13:00/);
    });
    await check('selected detail without drafts and cards update before slow GET', async () => {
      await install(page);
      await page.evaluate(async () => {
        planning.select('occ', 8);
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : String(raw).endsWith('/planning/today')
            ? new Promise(resolve => { gates.read = resolve; }) : baseFetch(raw, opts);
        beginSort(); commit(); await confirmation;
      });
      await page.waitForFunction(() => gates.read);
      const state = await snapshot(page);
      assert.deepEqual(state.cached, state.persisted);
      assert.match(state.detail, /12:00/);
      await page.evaluate(() => gates.read(Response.json(board)));
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
    });
    await check('R2: successful GET still updates detail without discarding drafts', async () => {
      await install(page);
      await page.evaluate(async () => {
        planning.select('occ', 8);
        planning.detail.body.querySelector('[data-timer-input]').value = '45m';
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort(); commit(); await confirmation;
        await planning.loadToday();
      });
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      results.draftAfterGet = await snapshot(page);
      assert.equal(results.draftAfterGet.timerDraft, '45m');
      assert.match(results.draftAfterGet.detail, /12:00/);
    });
    await check('tab switch with successful GET converges to committed data', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort(); planning.switchTab('goals'); await planning.loadToday(); commit(); await confirmation;
        await planning.loadToday(); planning.switchTab('today');
      });
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      const state = await snapshot(page);
      assert.deepEqual(state.cached, state.persisted);
    });
    await check('auto-off receipt clears obsolete waiting banner when GET fails', async () => {
      await install(page);
      await page.evaluate(async () => {
        board.recompute = { enabled: true, pending: true, wait_minutes: 30, reason: 'edit' };
        await planning.loadToday();
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) {
            const ids = JSON.parse(opts.body).order;
            board.progress = ids.map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index }));
            board.recompute = { enabled: false, pending: false };
            failRead = true;
            return Promise.resolve(Response.json({ saved: 2, recompute_enabled: false, schedule_applied: false,
              updated: 0, estimates: [] }));
          }
          return baseFetch(raw, opts);
        };
        beginSort(); await confirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      results.autoOff = await page.evaluate(() => ({
        server: board.recompute, cached: planning.board.recompute,
        banner: document.querySelector('#planning-alarm-banner').textContent,
        receiptToast: document.body.textContent.includes('自动重算已关闭'),
      }));
      await page.screenshot({ path: path.join(out, 'auto-off-banner.png'), fullPage: true });
      assert.equal(results.autoOff.cached.enabled, false);
      assert.equal(results.autoOff.cached.pending, false);
      assert.equal(results.autoOff.banner.trim(), '');
    });

    await check('remount while initial GET is slow: receipt seeds cache; older GET and failed refresh cannot roll back', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.precommit = structuredClone(board);
        window.readQueue = [];
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; })
          : String(raw).endsWith('/planning/today')
            ? new Promise(resolve => readQueue.push(resolve)) : baseFetch(raw, opts);
        beginSort(); planning.unmount();
        window.remount = planning.mount(document.querySelector('#fixture'));
        commit(); await confirmation;
      });
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
      await page.evaluate(async () => {
        readQueue[0](Response.json(precommit)); await remount;
        readQueue[1](Response.json({ error: 'offline' }, { status: 503 }));
      });
      await page.waitForFunction(() => planning.savedRefresh);
      assert.deepEqual((await snapshot(page)).dom, [8, 7]);
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
      await page.evaluate(async () => { window.fetch = baseFetch; await planning.retryLists(); });
      assert.equal((await snapshot(page)).savedRefresh, null);
    });
    await check('receipt during route unmount survives later failed initial GET without reviving old controls', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort(); planning.unmount(); commit(); await confirmation;
        failRead = true; await planning.mount(document.querySelector('#fixture'));
      });
      const state = await snapshot(page);
      assert.deepEqual(state.cached, state.persisted); assert.deepEqual(state.dom, [8, 7]);
      assert.equal(await page.evaluate(() => planning.reorderMode), false);
      assert.equal(await page.locator('#planning-reorder-btn').isDisabled(), false);
    });
    await check('old mount receipt updates cache but preserves a newer unsubmitted candidate and its DOM', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort(); planning.unmount(); await planning.mount(document.querySelector('#fixture'));
        planning.enterReorder(); window.candidateNode = planning.progressList.firstElementChild;
        failRead = true; commit(); await confirmation;
      });
      const state = await snapshot(page);
      assert.deepEqual(state.cached, state.persisted); assert.deepEqual(state.dom, [7, 8]);
      assert.equal(await page.evaluate(() => planning.reorderMode && candidateNode === planning.progressList.firstElementChild), true);
      await page.evaluate(() => planning.cancelReorder());
      assert.deepEqual((await snapshot(page)).dom, [8, 7]);
    });
    await check('later confirmed order wins over delayed previous mount receipt with failed reads', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.submissions = 0;
        window.fetch = (raw, opts) => {
          if (!String(raw).endsWith('/planning/reorder')) return baseFetch(raw, opts);
          submissions++;
          const ids = JSON.parse(opts.body).order;
          board.progress = ids.map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index,
            est_start: `2026-10-12T${submissions === 1 ? '12' : '15'}:${index ? '30' : '00'}:00+08:00` }));
          const receipt = { recompute_enabled: true, schedule_applied: true, estimates: structuredClone(board.progress) };
          if (submissions === 1) return new Promise(resolve => { gates.old = () => resolve(Response.json(receipt)); });
          failRead = true; return Promise.resolve(Response.json(receipt));
        };
        beginSort(); window.oldConfirmation = confirmation;
        planning.unmount(); await planning.mount(document.querySelector('#fixture'));
        beginSort(); await confirmation; gates.old(); await oldConfirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      const state = await snapshot(page);
      assert.deepEqual(state.cached, state.persisted); assert.deepEqual(state.dom, [7, 8]);
      assert.equal(await page.evaluate(() => submissions), 2);
    });
    await check('fresh external time result cannot be replaced by an older successful receipt', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort(); planning.switchTab('goals'); await planning.loadToday();
        window.older = { recompute_enabled: true, schedule_applied: true,
          estimates: board.progress.map(o => ({ id: o.id, est_start: '2026-10-12T12:00:00+08:00' })) };
        board.progress[1].est_start = '2026-10-12T16:00:00+08:00';
        await planning.loadToday(); failRead = true;
        gates.submit(Response.json(older)); await confirmation;
        planning.switchTab('today');
      });
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
      assert.equal(await page.evaluate(() => planning.board.progress[1].est_start), '2026-10-12T16:00:00+08:00');
    });
    await check('later manual recompute invalidates a previous order receipt', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => { gates.submit = resolve; });
          if (String(raw).endsWith('/planning/recompute')) {
            board.progress[1].est_start = '2026-10-12T17:00:00+08:00';
            return Promise.resolve(Response.json({ updated: 1, conflicts: [] }));
          }
          return baseFetch(raw, opts);
        };
        beginSort(); planning.switchTab('goals'); await planning.loadToday();
        await planning.runRecompute(); failRead = true;
        gates.submit(Response.json({ recompute_enabled: false, schedule_applied: false, estimates: [] }));
        await confirmation; planning.switchTab('today');
      });
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
      assert.equal(await page.evaluate(() => planning.board.recompute.enabled), true);
    });
    await check('later instance-window save invalidates an old order receipt even without a subsequent time read', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => { gates.submit = resolve; });
          if (String(raw).endsWith('/planning/occurrences/8') && opts?.method === 'PATCH') {
            window.windowPatchBody = JSON.parse(opts.body);
            board.progress[1].window_start_at = '2026-10-12T18:00:00+08:00';
            return Promise.resolve(Response.json(board.progress[1]));
          }
          return baseFetch(raw, opts);
        };
        beginSort(); planning.switchTab('goals'); await planning.loadToday();
        window.windowRevisionBefore = planning.orderRevision;
        planning.askEditTime(8);
      });
      await setDialogDateTime(page, 'planning-adj-window-start', '2026-10-12T18:00');
      await page.locator('.modal [data-ok]').click();
      await page.waitForFunction(() => planning.board.progress[1].window_start_at);
      assert.equal(await page.evaluate(() => windowPatchBody.window_start_at), '2026-10-12T10:00:00.000Z');
      assert.equal(await page.evaluate(() => planning.orderRevision), await page.evaluate(() => windowRevisionBefore + 1));
      await page.evaluate(async () => {
        failRead = true;
        gates.submit(Response.json({ recompute_enabled: true, schedule_applied: true,
          estimates: [{ id: 8, est_start: '2026-10-12T12:00:00+08:00' }] }));
        await confirmation;
      });
      assert.equal(await page.evaluate(() => planning.board.progress[1].est_start), '2026-10-12T13:00:00+08:00');
      assert.equal(await page.evaluate(() => planning.board.progress[1].window_start_at), '2026-10-12T18:00:00+08:00');
    });
    for (const tab of ['today', 'all']) {
      await check(`${tab}: polling updates readonly facts, ownership, status and actions; retains timer/checkbox drafts and focus`, async () => {
        await install(page);
        await page.evaluate(async (tab) => {
          window.fetch = (raw, opts) => String(raw).includes('/planning/occurrences') && !opts?.method
            ? Promise.resolve(Response.json(board.progress)) : baseFetch(raw, opts);
          if (tab === 'all') { planning.switchTab(tab); await planning.loadOccurrences(); }
          planning.select('occ', 8);
          const input = planning.detail.body.querySelector('[data-timer-input]');
          window.inputNode = input; input.value = '45m'; input.focus(); input.setSelectionRange(1, 3);
          planning.detail.body.querySelector('[data-alarm-start]').checked = true;
          Object.assign(board.progress[1], { est_start: '2026-10-12T15:00:00+08:00', est_end: '2026-10-12T15:30:00+08:00',
            actual_start: '2026-10-12T14:59:00+08:00', status: 'in_progress', schedule_label: 'running',
            is_fixed: true, fixed_source: 'manual', phase: 'end', is_hollow: true });
          if (tab === 'all') await planning.loadOccurrences(); else await planning.loadToday({ silent: true });
        }, tab);
        assert.equal(await page.evaluate(() => inputNode === planning.detail.body.querySelector('[data-timer-input]')
          && document.activeElement === inputNode && inputNode.selectionStart === 1 && inputNode.selectionEnd === 3), true);
        assert.equal(await page.locator('[data-timer-input]').inputValue(), '45m');
        assert.equal(await page.locator('[data-alarm-start]').isChecked(), true);
        assert.match(await page.locator('.detail-body').textContent(), /15:00/);
        assert.match(await page.locator('.detail-body').textContent(), /running/);
        assert.equal(await page.locator('.detail-foot [data-act="occ-finish"]').count(), 1);
        assert.equal(await page.locator('.detail-foot [data-act="occ-start"]').count(), 0);
      });
    }
    await check('selected occurrence closing during polling updates lifecycle and keeps unsaved reminder nodes', async () => {
      await install(page);
      await page.evaluate(async () => {
        planning.select('occ', 8);
        planning.detail.body.querySelector('[data-timer-input]').value = '45m';
        const closed = { ...board.progress.pop(), status: 'completed', schedule_label: '已完成',
          actual_start: '2026-10-12T12:00:00+08:00', actual_end: '2026-10-12T12:30:00+08:00' };
        board.done.push(closed); await planning.loadToday();
      });
      assert.equal(await page.locator('[data-timer-input]').inputValue(), '45m');
      assert.match(await page.locator('.detail-body').textContent(), /已完成/);
      assert.equal(await page.locator('.detail-foot [data-act="occ-backfill"]').count(), 1);
      assert.equal(await page.locator('.detail-foot [data-act="occ-start"]').count(), 0);
    });
    await check('changing selected occurrence during POST preserves the new selection and its draft', async () => {
      await install(page);
      await page.evaluate(async () => {
        planning.select('occ', 8);
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort(); planning.select('occ', 7);
        const input = planning.detail.body.querySelector('[data-timer-input]');
        input.value = '90m'; input.focus(); input.setSelectionRange(1, 2);
        failRead = true; commit(); await confirmation;
      });
      assert.equal(await page.evaluate(() => planning.selected.id), 7);
      assert.equal(await page.locator('[data-timer-input]').inputValue(), '90m');
      assert.match(await page.locator('.detail-title').textContent(), /Task 7/);
      assert.match(await page.locator('.detail-body').textContent(), /12:30/);
      assert.equal(await page.evaluate(() => document.activeElement.selectionStart), 1);
    });
    await check('reminder save after synchronized estimates persists the draft; new typing during PATCH is retained', async () => {
      await install(page);
      await page.evaluate(async () => {
        planning.select('occ', 8);
        const input = planning.detail.body.querySelector('[data-timer-input]');
        input.value = '45m'; input.focus(); input.setSelectionRange(1, 3);
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/tasks/8') && opts?.method === 'PATCH') {
            window.alarmBody = JSON.parse(opts.body);
            return new Promise(resolve => { gates.alarm = () => { board.progress[1].timer_minutes = 45; resolve(Response.json({ timer_minutes: 45 })); }; });
          }
          return baseFetch(raw, opts);
        };
        window.alarmSave = planning.saveOccurrenceAlarm(8);
        input.value = '90m'; input.setSelectionRange(1, 2); gates.alarm(); await alarmSave;
      });
      assert.equal(await page.evaluate(() => alarmBody.timer_minutes), '45m');
      assert.equal(await page.locator('[data-timer-input]').inputValue(), '90m');
      assert.equal(await page.evaluate(() => document.activeElement.selectionStart), 1);
    });
    await check('auto-off remains silent after successful GET with an unconsumed database request; enabling restores waiting', async () => {
      await install(page);
      await page.evaluate(async () => {
        board.recompute = { enabled: false, pending: true, wait_minutes: 30, reason: 'edit' };
        await planning.loadToday();
      });
      assert.equal((await page.locator('#planning-alarm-banner').textContent()).trim(), '');
      assert.equal(await page.evaluate(() => planning.board.recompute.pending), true, 'read keeps backend request data');
      await page.evaluate(async () => { board.recompute.enabled = true; await planning.loadToday(); });
      assert.match(await page.locator('#planning-alarm-banner').textContent(), /等待自动重算/);
    });
    await check('unknown result retries use the same key; reading recovery never resubmits a known success', async () => {
      await install(page);
      await page.evaluate(() => {
        window.keys = [];
        window.fetch = (raw, opts) => {
          if (!String(raw).endsWith('/planning/reorder')) return baseFetch(raw, opts);
          keys.push(JSON.parse(opts.body).request_key);
          if (keys.length === 1) return Promise.resolve(new Response('{', { status: 200 }));
          board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index }));
          failRead = true;
          return Promise.resolve(Response.json({ recompute_enabled: false, schedule_applied: false, estimates: [] }));
        };
        beginSort();
      });
      await page.waitForFunction(() => document.querySelector('[data-reorder-feedback]').textContent.includes('无法确认'));
      assert.equal(await page.evaluate(() => planning.reorderMode), true);
      await page.evaluate(() => planning.confirmReorder());
      await page.waitForFunction(() => planning.savedRefresh);
      assert.equal(await page.evaluate(() => keys.length === 2 && keys[0] === keys[1]), true);
      await page.evaluate(async () => { failRead = false; await planning.retryLists(); });
      assert.equal(await page.evaluate(() => keys.length), 2);
    });

    await check('a later rejected sort cannot discard the last known commit across a failed remount', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => { gates.submit = resolve; });
          return baseFetch(raw, opts);
        };
        beginSort(); failRead = true; commit(); await confirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? Promise.resolve(Response.json({ error: 'rejected' }, { status: 409 })) : baseFetch(raw, opts);
        planning.enterReorder(); await planning.confirmReorder();
        planning.unmount(); await planning.mount(document.querySelector('#fixture'));
      });
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
      assert.deepEqual((await snapshot(page)).dom, [8, 7]);
    });

    await check('Review R1: all-tab list/detail must adopt committed estimates while retaining drafts', async () => {
      await install(page);
      results.reviewAll = await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; })
          : String(raw).includes('/planning/occurrences') && !opts?.method
            ? Promise.resolve(Response.json(board.progress)) : baseFetch(raw, opts);
        beginSort();
        planning.switchTab('all');
        await planning.loadOccurrences();
        planning.select('occ', 8);
        const input = planning.detail.body.querySelector('[data-timer-input]');
        input.value = '45m'; input.focus(); input.setSelectionRange(1, 3);
        window.allInput = input;
        commit(); await confirmation;
        await planning.loadToday();
        return { activeTab: planning.activeTab,
            persisted: board.progress.map(o => [o.id, o.est_start]),
            today: planning.board.progress.map(o => [o.id, o.est_start]),
            all: planning.occurrences.map(o => [o.id, o.est_start]),
            needsOccurrencesRead: planning.reads.needsRead('occurrences'),
            detail: planning.detail.body.textContent,
            draft: input.value, sameNode: planning.detail.body.querySelector('[data-timer-input]') === allInput,
            focused: document.activeElement === input,
            calls: calls.slice(),
        };
      });
      await page.screenshot({ path: path.join(out, 'review-all.png'), fullPage: true });
      assert.equal(results.reviewAll.draft, '45m');
      assert.equal(results.reviewAll.focused, true);
      assert.equal(results.reviewAll.all.find(o => o[0] === 8)[1],
        results.reviewAll.persisted.find(o => o[0] === 8)[1]);
      assert.match(results.reviewAll.detail, /12:00/);
    });

    await check('Review R2: rejected newer sort must not invalidate an earlier committed receipt', async () => {
      await install(page);
      await page.evaluate(async () => {
        let orderCalls = 0;
        let remountRead = true;
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/today') && remountRead) {
            remountRead = false;
            const precommit = structuredClone(board);
            // GET captured the old order; the first transaction commits before
            // this snapshot is delivered, but its POST response is still delayed.
            board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id),
              sort_order: index, est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00`,
              est_end: index ? '2026-10-12T13:00:00+08:00' : '2026-10-12T12:30:00+08:00' }));
            window.firstReceipt = { recompute_enabled: true, schedule_applied: true, estimates: structuredClone(board.progress) };
            return Promise.resolve(Response.json(precommit));
          }
          if (!String(raw).endsWith('/planning/reorder')) return baseFetch(raw, opts);
          if (++orderCalls === 1) return new Promise(resolve => { gates.submit = resolve; });
          return Promise.resolve(Response.json({ error: 'new sort rejected: expected_order is stale', error_code: 'concurrent_modified' }, { status: 409 }));
        };
        beginSort();
        window.firstConfirmation = confirmation;
        planning.unmount();
        await planning.mount(document.querySelector('#fixture'));
        clearInterval(planning.pollTimer); planning.pollTimer = null;
        failRead = true;
        beginSort(); await confirmation;
        gates.submit(Response.json(firstReceipt)); await firstConfirmation;
      });
      await page.waitForFunction(() => planning.readErrors.has('today'));
      results.reviewRejected = await snapshot(page);
      results.reviewRejected.revision = await page.evaluate(() => planning.orderRevision);
      await page.screenshot({ path: path.join(out, 'review-rejected.png'), fullPage: true });
      assert.deepEqual(results.reviewRejected.cached, results.reviewRejected.persisted);
    });

    await check('Review R3: current auto-off read matching the receipt must not prevent committed order adoption', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort();
        board.recompute.enabled = false;
        planning.switchTab('goals'); await planning.loadToday();
        board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index }));
        failRead = true;
        gates.submit(Response.json({ recompute_enabled: false, schedule_applied: false, estimates: [] }));
        await confirmation;
        planning.switchTab('today');
      });
      await page.waitForFunction(() => planning.savedRefresh);
      results.reviewAutoOffRead = await snapshot(page);
      results.reviewAutoOffRead.enabled = await page.evaluate(() => planning.board.recompute.enabled);
      assert.equal(results.reviewAutoOffRead.enabled, false);
      assert.deepEqual(results.reviewAutoOffRead.cached, results.reviewAutoOffRead.persisted);
    });

    await check('Review R4: schedule labels must agree with receipt estimates when the follow-up GET fails', async () => {
      await install(page);
      await page.evaluate(async () => {
        board.now = '2026-10-12T12:00:00+08:00';
        Object.assign(board.progress[0], { est_start: '2026-10-12T11:00:00+08:00',
          est_end: '2026-10-12T11:30:00+08:00', schedule_label: '落后' });
        await planning.loadToday(); planning.select('occ', 7);
        planning.detail.body.querySelector('[data-timer-input]').value = '45m';
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
        beginSort();
        board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index,
          est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00`,
          est_end: index ? '2026-10-12T13:00:00+08:00' : '2026-10-12T12:30:00+08:00', schedule_label: '正常' }));
        const fields = ['id', 'est_start', 'est_end', 'nominal_start', 'estimated_time_source',
          'fixed_source', 'schedule_managed', 'is_fixed'];
        const estimates = board.progress.map(o => Object.fromEntries(fields.filter(k => Object.hasOwn(o, k)).map(k => [k, o[k]])));
        failRead = true;
        gates.submit(Response.json({ recompute_enabled: true, schedule_applied: true, estimates }));
        await confirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      results.reviewLabel = await page.evaluate(() => ({
        server: board.progress.find(o => o.id === 7), cached: planning.board.progress.find(o => o.id === 7),
        detail: planning.detail.body.textContent,
      }));
      assert.equal(results.reviewLabel.cached.est_start, results.reviewLabel.server.est_start);
      assert.equal(results.reviewLabel.cached.schedule_label, results.reviewLabel.server.schedule_label);
    });


    for (const mode of ['slow', 'failed']) {
      await check(`all-tab ${mode} reads: immediate estimates, filtered/history order and input identity survive`, async () => {
        await install(page);
        await page.evaluate(async (mode) => {
          window.recordList = [structuredClone(board.progress[0]), { ...board.progress[0], id: 99, task_id: 99,
            status: 'completed', actual_start: '2026-10-11T10:00:00+08:00', window_start_at: '2026-10-11T09:00:00+08:00' },
            structuredClone(board.progress[1])];
          window.postReads = false; window.pendingReads = [];
          window.fetch = (raw, opts) => {
            if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => { gates.submit = resolve; });
            if (postReads && String(raw).includes('/planning/')) return mode === 'slow'
              ? new Promise(resolve => pendingReads.push(resolve))
              : Promise.resolve(Response.json({ error: 'offline' }, { status: 503 }));
            if (String(raw).includes('/planning/occurrences')) return Promise.resolve(Response.json(recordList));
            return baseFetch(raw, opts);
          };
          beginSort(); planning.filters.schedule_date = '2026-10-12'; planning.switchTab('all');
          await planning.loadOccurrences(); planning.select('occ', 8);
          window.retainedInput = planning.detail.body.querySelector('[data-timer-input]');
          retainedInput.value = '45m'; retainedInput.focus(); retainedInput.setSelectionRange(1, 3);
          planning.detail.body.querySelector('[data-alarm-end]').checked = true;
          postReads = true; commit(); await confirmation;
        }, mode);
        assert.equal(await page.evaluate(() => planning.occurrences.find(o => o.id === 8).est_start), '2026-10-12T12:00:00+08:00');
        assert.deepEqual(await page.evaluate(() => planning.occurrences.map(o => o.id)), [7, 99, 8]);
        assert.equal(await page.evaluate(() => planning.occurrences[1].actual_start), '2026-10-11T10:00:00+08:00');
        assert.equal(await page.evaluate(() => planning.occurrences[1].window_start_at), '2026-10-11T09:00:00+08:00');
        assert.equal(await page.evaluate(() => retainedInput === planning.detail.body.querySelector('[data-timer-input]')
          && document.activeElement === retainedInput && retainedInput.selectionStart === 1 && retainedInput.selectionEnd === 3), true);
        assert.equal(await page.locator('[data-alarm-end]').isChecked(), true);
        assert.match(await page.locator('.detail-body').textContent(), /12:00/);
        if (mode === 'slow') {
          await page.evaluate(() => pendingReads.forEach(resolve => resolve(Response.json({ error: 'offline' }, { status: 503 }))));
        }
        await page.waitForFunction(() => planning.savedRefresh);
        assert.deepEqual(await page.evaluate(() => [...planning.savedRefresh.names].sort()), ['occurrences', 'tasks', 'today']);
      });
    }
    await check('normal today polling updates an already-selected all-tab detail without rebuilding its draft', async () => {
      await install(page);
      await page.evaluate(async () => {
        board.now = '2026-10-12T12:00:00+08:00'; await planning.loadToday();
        window.fetch = (raw, opts) => String(raw).includes('/planning/occurrences')
          ? Promise.resolve(Response.json(board.progress)) : baseFetch(raw, opts);
        planning.switchTab('all'); await planning.loadOccurrences(); planning.select('occ', 8);
        window.pollInput = planning.detail.body.querySelector('[data-timer-input]');
        pollInput.value = '45m'; pollInput.focus(); pollInput.setSelectionRange(1, 2);
        Object.assign(board.progress[1], { est_start: '2026-10-12T15:00:00+08:00', est_end: '2026-10-12T15:30:00+08:00', schedule_label: '正常' });
        await planning.loadToday({ silent: true });
      });
      assert.equal(await page.evaluate(() => planning.occurrences.find(o => o.id === 8).est_start), '2026-10-12T15:00:00+08:00');
      assert.match(await page.locator('.detail-body').textContent(), /15:00/);
      assert.equal(await page.evaluate(() => document.activeElement === pollInput && pollInput.selectionStart === 1
        && pollInput === planning.detail.body.querySelector('[data-timer-input]')), true);
    });
    await check('all-tab newer lifecycle/window/time result survives an older order receipt and failed reads', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.latestList = structuredClone(board.progress);
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.submit = resolve; })
          : String(raw).includes('/planning/occurrences') ? Promise.resolve(Response.json(latestList)) : baseFetch(raw, opts);
        beginSort(); planning.switchTab('all'); await planning.loadOccurrences();
        Object.assign(latestList[1], { est_start: '2026-10-12T18:00:00+08:00', actual_start: '2026-10-12T18:00:00+08:00',
          status: 'in_progress', window_start_at: '2026-10-12T17:00:00+08:00' });
        await planning.loadOccurrences(); planning.select('occ', 8);
        planning.detail.body.querySelector('[data-timer-input]').value = '90m';
        const fetchBeforeFailure = window.fetch;
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder') ? fetchBeforeFailure(raw, opts)
          : Promise.resolve(Response.json({ error: 'offline' }, { status: 503 }));
        commit(); await confirmation;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      assert.equal(await page.evaluate(() => planning.occurrences.find(o => o.id === 8).est_start), '2026-10-12T18:00:00+08:00');
      assert.equal(await page.evaluate(() => planning.occurrences.find(o => o.id === 8).status), 'in_progress');
      assert.equal(await page.evaluate(() => planning.occurrences.find(o => o.id === 8).window_start_at), '2026-10-12T17:00:00+08:00');
      assert.equal(await page.locator('[data-timer-input]').inputValue(), '90m');
    });
    await check('older success arriving before newer success cannot block the later confirmed estimates', async () => {
      await install(page);
      await page.evaluate(async () => {
        let writes = 0; let initialRemountRead = true;
        const makeReceipt = hour => ({ recompute_enabled: true, schedule_applied: true,
          estimates: board.progress.map((o, index) => ({ ...o, est_start: `2026-10-12T${hour}:${index ? '30' : '00'}:00+08:00`,
            est_end: `2026-10-12T${index ? Number(hour) + 1 : hour}:${index ? '00' : '30'}:00+08:00` })) });
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => {
            if (++writes === 1) gates.first = resolve;
            else { window.newReceipt = makeReceipt('15'); board.progress = newReceipt.estimates; gates.second = resolve; }
          });
          if (String(raw).endsWith('/planning/today') && initialRemountRead) {
            initialRemountRead = false; const precommit = structuredClone(board);
            window.oldReceipt = makeReceipt('14'); board.progress = oldReceipt.estimates;
            return Promise.resolve(Response.json(precommit));
          }
          return baseFetch(raw, opts);
        };
        planning.enterReorder(); window.firstOrder = planning.confirmReorder();
        planning.unmount(); await planning.mount(document.querySelector('#fixture'));
        clearInterval(planning.pollTimer); planning.pollTimer = null;
        planning.enterReorder(); window.secondOrder = planning.confirmReorder();
        failRead = true; gates.first(Response.json(oldReceipt)); await firstOrder;
        gates.second(Response.json(newReceipt)); await secondOrder;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
    });
    for (const direction of ['closing', 'reopening']) {
      await check(`switch ${direction} after commit preserves the newer switch while adopting compatible order fields`, async () => {
        await install(page);
        await page.evaluate(async (direction) => {
          board.recompute = { enabled: direction === 'closing', pending: false };
          await planning.loadToday();
          window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
            ? new Promise(resolve => { gates.submit = resolve; }) : baseFetch(raw, opts);
          beginSort(); planning.switchTab('goals'); await planning.loadToday();
          board.recompute = { enabled: direction !== 'closing', pending: direction !== 'closing',
            requested_at: '2026-10-12T12:01:00+08:00', wait_minutes: 30, reason: 'later-switch' };
          await planning.loadToday();
          window.expectedSwitch = structuredClone(board.recompute);
          const enabled = direction === 'closing';
          board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index,
            ...(enabled ? { est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00` } : {}) }));
          failRead = true;
          gates.submit(Response.json({ recompute_enabled: enabled, schedule_applied: enabled,
            estimates: enabled ? board.progress : [] }));
          await confirmation; planning.switchTab('today');
        }, direction);
        await page.waitForFunction(() => planning.savedRefresh);
        assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
        assert.deepEqual(await page.evaluate(() => planning.board.recompute), await page.evaluate(() => expectedSwitch));
        assert.equal(await page.evaluate(() => planning.board.progress.some(o => o.time_updating)), false);
      });
    }
    await check('accepted earlier receipt after rejected remount attempt registers saved-refresh feedback', async () => {
      await install(page);
      await page.evaluate(async () => {
        let count = 0; let remountRead = true;
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) {
            if (++count === 1) return new Promise(resolve => { gates.submit = resolve; });
            return Promise.resolve(Response.json({ error: 'reject', error_code: 'concurrent_modified' }, { status: 409 }));
          }
          if (String(raw).endsWith('/planning/today') && remountRead) {
            remountRead = false; const before = structuredClone(board);
            board.progress = [8, 7].map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index }));
            return Promise.resolve(Response.json(before));
          }
          return baseFetch(raw, opts);
        };
        beginSort(); window.first = confirmation;
        planning.unmount(); await planning.mount(document.querySelector('#fixture'));
        failRead = true; beginSort(); await confirmation;
        gates.submit(Response.json({ recompute_enabled: false, schedule_applied: false, estimates: [] })); await first;
      });
      await page.waitForFunction(() => planning.savedRefresh);
      assert.match(await page.locator('#planning-load-feedback').textContent(), /顺序已保存/);
      assert.deepEqual((await snapshot(page)).cached, (await snapshot(page)).persisted);
    });

    assert.deepEqual(results.pageErrors, []);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify(results, null, 2));
    process.stdout.write(JSON.stringify({ checks: results.checks, pageErrors: results.pageErrors }, null, 2));
    if (results.checks.some(check => !check.passed)) process.exitCode = 1;
  }
}
main().catch(error => { process.stderr.write(error.stack); process.exitCode = 1; });
