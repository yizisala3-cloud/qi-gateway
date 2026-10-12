// Native Edge layout, pointer capture, and async races using production modules.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const http = require('node:http');
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const repo = path.resolve(__dirname, '..');
const out = process.argv[2] || path.join(repo, 'tests', 'repair-1012-browser');
fs.mkdirSync(out, { recursive: true });
const result = { environment: 'local production JS/CSS, isolated headless Edge, mocked HTTP; no production data', checks: [], drag: [] };
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html; charset=utf-8');
    res.end(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <link rel="stylesheet" href="/admin/css/style.css"><script src="/admin/assets/vendor/marked-12.0.2.min.js"></script>
      <script src="/admin/assets/vendor/purify-3.1.6.min.js"></script></head><body>
      <main id="fixture" style="margin:140px 20px 0"></main><script type="module">
      import planning from '/admin/js/pages/planning.js'; window.planning = planning; window.ready = true;
      </script></body></html>`);
    return;
  }
  const target = path.resolve(repo, '.' + url.pathname);
  if (!target.startsWith(repo + path.sep)) { res.writeHead(403).end(); return; }
  fs.readFile(target, (err, data) => {
    if (err) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', { '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' }[path.extname(target)] || 'application/octet-stream');
    res.end(data);
  });
});
async function install(page, options = {}) {
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.waitForFunction(() => window.ready);
  await page.evaluate(async (options) => {
    localStorage.clear();
    window.calls = [];
    window.gates = {};
    window.failures = new Set();
    window.entries = Object.fromEntries([7, 8].map(id => [id, {
      id, title: `备忘录 ${id}`, content: `# 正文 ${id}\n\n内容`, kind: 'pinned', status: 'active',
      content_version: 1, tags: [{ id: 1, name: 'ABCDEFGHIJKLMNOPQRSTUVWXYZABCD' }], updated_at: '2026-10-12T12:00:00+08:00',
    }]));
    window.board = { progress: [7, 8].map((id, index) => ({ id, task_id: id, content: `待办 ${id}`, task_type: 'daily',
      status: 'pending', sort_order: index, schedule_label: '正常', estimated_minutes: 30, est_start: null, est_end: null,
      display_cycle_date: '2026-10-12', schedule_date: '2026-10-12' })),
      attention: [], done: [], conflicts: [], recompute: { enabled: true, pending: false } };
    window.baseFetch = async (raw, opts = {}) => {
      const url = new URL(String(raw), location.origin);
      const route = url.pathname;
      calls.push({ route, method: opts.method || 'GET', body: opts.body && JSON.parse(opts.body) });
      if (failures.has(route)) return Response.json({ error: '测试读取失败' }, { status: 503 });
      if (route === '/admin/api/planning/today') return Response.json(board);
      if (route === '/admin/api/memo/tags') return Response.json([{ id: 1, name: 'ABCDEFGHIJKLMNOPQRSTUVWXYZABCD' }]);
      if (route === '/admin/api/memo/board') return Response.json({ sections: [] });
      if (/\/memo\/entries\/\d+$/.test(route)) {
        const id = Number(route.split('/').at(-1));
        if (opts.method === 'PATCH') entries[id] = { ...entries[id], ...JSON.parse(opts.body), content_version: entries[id].content_version + 1 };
        return Response.json(entries[id]);
      }
      if (/\/memo\/entries\/\d+\/(archive|delete)$/.test(route)) {
        const id = Number(route.split('/').at(-2));
        entries[id].status = route.endsWith('archive') ? 'archived' : 'deleted';
        return Response.json(entries[id]);
      }
      if (route === '/admin/api/planning/reorder') return Response.json({ saved: 2, recompute_enabled: true });
      if (/\/planning\/tasks\/\d+$/.test(route)) return Response.json({});
      return Response.json([]);
    };
    window.fetch = async (raw, opts) => {
      const route = new URL(String(raw), location.origin).pathname;
      if (options.slowTags && route === '/admin/api/memo/tags') return new Promise(resolve => { gates.tags = resolve; });
      if (options.slowBoard && route === '/admin/api/memo/board') return new Promise(resolve => { gates.board = resolve; });
      return baseFetch(raw, opts);
    };
    await planning.mount(document.querySelector('#fixture'));
  }, options);
}
async function check(name, fn) { await fn(); result.checks.push({ name, passed: true }); }
async function main() {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  let browser;
  try {
    browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
    const context = await browser.newContext({ timezoneId: 'Asia/Shanghai', viewport: { width: 1280, height: 720 } });
    const page = await context.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await check('BUG-18 tags publish before slow or failed board without rebuilding editor', async () => {
      await install(page, { slowTags: true, slowBoard: true });
      await page.evaluate(() => { planning.switchTab('memo'); planning.memo.openEditor(null); });
      await page.waitForSelector('.memo-editor');
      assert.match(await page.locator('[data-editor-tags]').textContent(), /加载中/);
      await page.locator('[data-editor-title]').fill('保留标题');
      await page.locator('[data-new-tag-name]').fill('未添加输入');
      await page.evaluate(() => gates.tags(Response.json([{ id: 1, name: 'ABCDEFGHIJKLMNOPQRSTUVWXYZABCD' }])));
      await page.waitForSelector('[data-tag-chip]');
      assert.equal(await page.locator('.memo-editor').count(), 1);
      assert.equal(await page.locator('[data-editor-title]').inputValue(), '保留标题');
      assert.equal(await page.locator('[data-new-tag-name]').inputValue(), '未添加输入');
      await page.evaluate(() => gates.board(Response.json({ error: '看板失败' }, { status: 503 })));
      await page.waitForFunction(() => document.querySelector('[data-memo-status]').textContent.includes('失败'));
      assert.equal(await page.locator('[data-tag-chip]').count(), 1);
    });
    await check('#42 reset, poll, delayed alarm save and remount do not resurrect selection', async () => {
      await install(page);
      await page.evaluate(() => planning.select('occ', 7));
      await page.evaluate(() => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/tasks/7')
          ? new Promise(resolve => { gates.alarm = resolve; }) : baseFetch(raw, opts);
        planning.saveOccurrenceAlarm(7); planning.switchTab('goals');
      });
      assert.equal(await page.locator('.detail-title').textContent(), '');
      await page.evaluate(() => gates.alarm(Response.json({})));
      await page.evaluate(async () => { await planning.loadToday(); planning.switchTab('today'); });
      assert.equal(await page.locator('.detail-title').textContent(), '');
      await page.evaluate(async () => { planning.unmount(); await planning.mount(document.querySelector('#fixture')); });
      assert.equal(await page.evaluate(() => planning.selected), null);
    });
    await check('#42 delayed resume and A/B selection keep current task detail', async () => {
      await install(page);
      await page.evaluate(() => {
        planning.switchTab('all');
        planning.tasks = [7, 8].map(id => ({ id, content: `任务 ${id}`, task_type: 'daily',
          is_active: true, refresh_enabled: false, estimated_minutes: 30 }));
        planning.select('task', 7);
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/tasks/7')
          ? new Promise(resolve => { gates.resume = resolve; }) : baseFetch(raw, opts);
        planning.taskAction('resume-refresh', 7); planning.select('task', 8);
        gates.resume(Response.json({}));
      });
      await page.evaluate(() => Promise.resolve());
      assert.equal(await page.locator('.detail-title').textContent(), '任务 8');
      await page.evaluate(() => planning.switchTab('goals'));
      assert.equal(await page.locator('.detail-title').textContent(), '');
    });
    await check('BUG-18 board first, tag failure/retry/empty; stale read cannot undo new tag', async () => {
      await install(page, { slowTags: true });
      await page.evaluate(() => { planning.switchTab('memo'); planning.memo.openEditor(null); });
      await page.waitForSelector('.memo-editor');
      await page.waitForFunction(() => planning.memo.board !== null);
      await page.evaluate(() => gates.tags(Response.json({ error: '标签失败' }, { status: 503 })));
      await page.waitForSelector('[data-tags-retry]');
      await page.locator('[data-editor-title]').fill('不丢输入');
      await page.locator('[data-tags-retry]').click();
      await page.evaluate(() => gates.tags(Response.json([])));
      await page.waitForFunction(() => document.querySelector('[data-editor-tags]').textContent.includes('还没有标签'));
      assert.equal(await page.locator('[data-editor-title]').inputValue(), '不丢输入');
      await page.evaluate(() => {
        planning.memo.prefetchTags({ fresh: true }); window.oldTags = gates.tags;
        window.fetch = (raw, opts) => String(raw).endsWith('/memo/tags') && opts?.method === 'POST'
          ? new Promise(resolve => { gates.createTag = resolve; }) : baseFetch(raw, opts);
      });
      await page.locator('[data-new-tag-name]').fill('新标签');
      await page.locator('[data-editor-add-tag]').click();
      await page.locator('[data-new-tag-name]').fill('下一标签名');
      await page.evaluate(() => {
        const fallback = baseFetch;
        window.fetch = (raw, opts) => String(raw).endsWith('/memo/tags') && (!opts?.method || opts.method === 'GET')
          ? Promise.resolve(Response.json([{ id: 2, name: '新标签' }])) : fallback(raw, opts);
        gates.createTag(Response.json({ id: 2, name: '新标签' }));
        oldTags(Response.json([{ id: 1, name: '过期标签' }]));
      });
      await page.waitForSelector('[data-tag-chip][value="2"]');
      assert.equal(await page.locator('[data-tag-chip][value="2"]').isChecked(), true);
      assert.equal(await page.locator('[data-tag-chip][value="1"]').count(), 0);
      assert.equal(await page.locator('[data-new-tag-name]').inputValue(), '下一标签名');
      await page.evaluate(() => {
        window.memoBeforeLogout = planning.memo; planning.unmount();
      });
      assert.deepEqual(await page.evaluate(() => memoBeforeLogout.tags), []);
    });
    await check('UI-01 A/B and navigation guard; editor remains modal and saved reader updates', async () => {
      await install(page);
      await page.evaluate(() => {
        planning.switchTab('memo');
        window.fetch = (raw, opts) => /\/memo\/entries\/[78]$/.test(String(raw))
          ? new Promise(resolve => { gates[String(raw).slice(-1)] = resolve; }) : baseFetch(raw, opts);
        planning.memo.openReader(7); planning.memo.openReader(8);
        gates[8](Response.json(entries[8]));
      });
      await page.waitForFunction(() => document.querySelector('.detail-title').textContent.includes('8'));
      await page.evaluate(() => gates[7](Response.json(entries[7])));
      assert.equal(await page.locator('.detail-title').textContent(), '备忘录 8');
      assert.equal(await page.locator('.modal').count(), 0);
      await page.evaluate(() => { planning.memo.openReader(7); planning.switchTab('goals'); gates[7](Response.json(entries[7])); });
      assert.equal(await page.locator('.detail-title').textContent(), '');
      await page.evaluate(async () => { window.fetch = baseFetch; planning.switchTab('memo'); await planning.memo.openReader(8); });
      await page.locator('[data-reader-edit]').click();
      await page.waitForSelector('.memo-editor');
      await page.locator('[data-editor-content]').fill('# 已修改正文');
      await page.evaluate(async () => { planning.memo.markEditorDirty(); await planning.memo.editor.autosave.flush(); });
      await page.waitForFunction(() => document.querySelector('.memo-md').textContent.includes('已修改正文'));
      await page.locator('[data-editor-close]').click();
      await page.waitForFunction(() => !document.querySelector('.memo-editor'));
      await page.locator('[data-reader-archive]').click();
      await page.waitForFunction(() => planning.selected === null);
    });
    await check('#44 in-flight today read cannot replace candidate nodes', async () => {
      await install(page);
      await page.evaluate(() => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/today')
          ? new Promise(resolve => { gates.today = resolve; }) : baseFetch(raw, opts);
        planning.loadToday({ fresh: true }); planning.enterReorder();
        const list = planning.progressList;
        list.insertBefore(list.lastElementChild, list.firstElementChild);
        window.candidateNode = list.firstElementChild;
        gates.today(Response.json(board));
      });
      await page.evaluate(() => Promise.resolve());
      assert.deepEqual(await page.locator('#planning-progress .plan-item').evaluateAll(els => els.map(el => Number(el.dataset.occ))), [8, 7]);
      assert.equal(await page.evaluate(() => candidateNode === planning.progressList.firstElementChild), true);
    });
    await check('#45 single confirmation and immediate candidate display during slow/failed refresh', async () => {
      await install(page);
      await page.evaluate(() => {
        window.submitCount = 0;
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) { submitCount++; return new Promise(resolve => { gates.submit = resolve; }); }
          if (String(raw).endsWith('/planning/today')) return new Promise(resolve => { gates.refresh = resolve; });
          return baseFetch(raw, opts);
        };
        planning.enterReorder();
        const list = planning.progressList; list.insertBefore(list.lastElementChild, list.firstElementChild);
        planning.confirmReorder(); planning.confirmReorder();
        gates.submit(Response.json({ saved: 2, recompute_enabled: true }));
      });
      await page.waitForFunction(() => !planning.reorderMode);
      assert.equal(await page.evaluate(() => submitCount), 1);
      assert.deepEqual(await page.locator('#planning-progress .plan-item').evaluateAll(els => els.map(el => Number(el.dataset.occ))), [8, 7]);
      assert.match(await page.locator('#planning-progress').textContent(), /更新中/);
      await page.evaluate(() => { planning.switchTab('goals'); gates.refresh(Response.json({ error: '刷新失败' }, { status: 503 })); });
      await page.waitForFunction(() => planning.savedRefresh);
      assert.match(await page.locator('#planning-load-feedback').textContent(), /顺序已保存/);
    });
    await check('#46 conflict keeps candidate and displays Chinese reasons', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? Promise.resolve(Response.json({ error: '此顺序存在排程冲突', error_code: 'schedule_conflict',
            details: { conflicts: [{ occurrence_id: 7, reason: '可安排时段剩余空间不足' }] } }, { status: 409 })) : baseFetch(raw, opts);
        planning.enterReorder();
        await planning.confirmReorder();
      });
      assert.equal(await page.evaluate(() => planning.reorderMode), true);
      assert.match(await page.locator('[data-reorder-feedback]').textContent(), /待办 7：可安排时段/);
    });
    await check('#45 lost response retry keeps request identity; auto-off feedback', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.requests = [];
        window.fetch = (raw, opts) => {
          if (!String(raw).endsWith('/planning/reorder')) return baseFetch(raw, opts);
          requests.push(JSON.parse(opts.body));
          if (requests.length === 1) return Promise.reject(new Error('响应丢失'));
          return Promise.resolve(Response.json({ saved: 2, recompute_enabled: false }));
        };
        planning.enterReorder(); await planning.confirmReorder();
      });
      assert.equal(await page.evaluate(() => planning.reorderMode), true);
      await page.evaluate(() => planning.confirmReorder());
      assert.equal(await page.evaluate(() => requests[0].request_key === requests[1].request_key), true);
      assert.match(await page.locator('body').textContent(), /自动重算已关闭/);
    });
    await check('#42/#45 pending confirmation from previous mount leaves new controls intact', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.oldConfirm = resolve; }) : baseFetch(raw, opts);
        planning.enterReorder(); planning.confirmReorder(); planning.unmount();
        await planning.mount(document.querySelector('#fixture'));
        window.fetch = (raw, opts) => String(raw).endsWith('/planning/reorder')
          ? new Promise(resolve => { gates.newConfirm = resolve; }) : baseFetch(raw, opts);
        planning.enterReorder(); planning.confirmReorder();
        gates.oldConfirm(Response.json({ saved: 2, recompute_enabled: true }));
      });
      await page.evaluate(() => Promise.resolve());
      assert.equal(await page.locator('[data-act="confirm-reorder"]').isDisabled(), true);
      assert.equal(await page.evaluate(() => planning.reorderMode), true);
      await page.evaluate(() => gates.newConfirm(Response.json({ saved: 2, recompute_enabled: true })));
      await page.waitForFunction(() => !planning.reorderMode);
    });
    await check('sorting immediately displays committed times before slow refresh, including detail', async () => {
      await install(page);
      await page.evaluate(() => {
        planning.board.recompute.pending = true;
        planning.board.conflicts = [{ occurrence_id: 7, reason: '旧顺序的冲突' }];
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) {
            const ids = JSON.parse(opts.body).order;
            board.progress = ids.map((id, index) => ({ ...board.progress.find(o => o.id === id),
              sort_order: index, est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00`,
              est_end: index ? '2026-10-12T13:00:00+08:00' : '2026-10-12T12:30:00+08:00' }));
            board.recompute.pending = false;
            return Promise.resolve(Response.json({ saved: 2, recompute_enabled: true,
              schedule_applied: true, updated: 2, estimates: board.progress }));
          }
          if (String(raw).endsWith('/planning/today')) return new Promise(resolve => { gates.refresh = resolve; });
          return baseFetch(raw, opts);
        };
        planning.enterReorder();
        const list = planning.progressList; list.insertBefore(list.lastElementChild, list.firstElementChild);
        window.confirmation = planning.confirmReorder();
      });
      await page.waitForFunction(() => !planning.reorderMode && gates.refresh);
      assert.deepEqual(await page.evaluate(() => planning.board.progress.map(o => [o.id, o.est_start])),
        [[8, '2026-10-12T12:00:00+08:00'], [7, '2026-10-12T12:30:00+08:00']]);
      assert.equal(await page.evaluate(() => planning.board.progress.some(o => o.time_updating)), false);
      assert.deepEqual(await page.evaluate(() => planning.board.conflicts), []);
      assert.match(await page.locator('#planning-progress').textContent(), /12:00/);
      assert.doesNotMatch(await page.locator('#planning-progress').textContent(), /更新中/);
      assert.match(await page.locator('body').textContent(), /顺序和预估时间已更新/);
      await page.evaluate(() => planning.select('occ', 8));
      assert.match(await page.locator('.detail-panel').textContent(), /12:00/);
      await page.screenshot({ path: path.join(out, 'sorting-immediate-times.png'), fullPage: true });
      await page.evaluate(() => gates.refresh(Response.json(board)));
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      assert.equal(await page.evaluate(() => planning.board.recompute.pending), false);
    });
    await check('committed estimates survive failed refresh and retry never resubmits order', async () => {
      await install(page);
      await page.evaluate(() => {
        window.confirmCount = 0;
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) {
            confirmCount++;
            const ids = JSON.parse(opts.body).order;
            board.progress = ids.map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index,
              est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00` }));
            failures.add('/admin/api/planning/today');
            return Promise.resolve(Response.json({ saved: 2, recompute_enabled: true,
              schedule_applied: true, estimates: board.progress }));
          }
          return baseFetch(raw, opts);
        };
        planning.enterReorder();
        const list = planning.progressList; list.insertBefore(list.lastElementChild, list.firstElementChild);
        planning.confirmReorder();
      });
      await page.waitForFunction(() => planning.savedRefresh);
      assert.match(await page.locator('#planning-load-feedback').textContent(), /预估时间已保存，列表同步失败/);
      assert.equal(await page.evaluate(() => planning.board.progress[0].est_start), '2026-10-12T12:00:00+08:00');
      await page.evaluate(async () => { failures.clear(); await planning.retryLists(); });
      assert.equal(await page.evaluate(() => planning.savedRefresh), null);
      assert.equal(await page.evaluate(() => confirmCount), 1);
    });
    await check('confirmation after tab switch invalidates precommit read and fetches committed order', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.todayReads = 0;
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => {
            gates.commit = () => {
              const ids = JSON.parse(opts.body).order;
              board.progress = ids.map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index,
                est_start: `2026-10-12T12:${index ? '30' : '00'}:00+08:00` }));
              resolve(Response.json({ saved: 2, recompute_enabled: true, schedule_applied: true, estimates: board.progress }));
            };
          });
          if (String(raw).endsWith('/planning/today')) todayReads++;
          return baseFetch(raw, opts);
        };
        planning.enterReorder();
        const list = planning.progressList; list.insertBefore(list.lastElementChild, list.firstElementChild);
        window.confirmation = planning.confirmReorder();
        planning.switchTab('goals'); await planning.loadToday();
        gates.commit(); await confirmation;
        planning.switchTab('today');
      });
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      assert.deepEqual(await page.evaluate(() => planning.board.progress.map(o => o.id)), [8, 7]);
      assert.equal(await page.evaluate(() => todayReads >= 2), true);
    });
    await check('previous mount commit refreshes current mount without applying previous candidate UI', async () => {
      await install(page);
      await page.evaluate(async () => {
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return new Promise(resolve => {
            gates.commit = () => {
              board.progress = JSON.parse(opts.body).order.map((id, index) => ({ ...board.progress.find(o => o.id === id), sort_order: index }));
              resolve(Response.json({ saved: 2, recompute_enabled: true, schedule_applied: true, estimates: board.progress }));
            };
          });
          return baseFetch(raw, opts);
        };
        planning.enterReorder();
        const list = planning.progressList; list.insertBefore(list.lastElementChild, list.firstElementChild);
        window.confirmation = planning.confirmReorder();
        planning.unmount(); await planning.mount(document.querySelector('#fixture'));
        gates.commit(); await confirmation;
      });
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      assert.deepEqual(await page.evaluate(() => planning.board.progress.map(o => o.id)), [8, 7]);
      assert.equal(await page.evaluate(() => planning.reorderMode), false);
      assert.equal(await page.locator('#planning-reorder-btn').isDisabled(), false);
    });
    await check('replayed receipt cannot replace a newer order or times while fresh read is slow', async () => {
      await install(page);
      await page.evaluate(() => {
        window.fetch = (raw, opts) => {
          if (String(raw).endsWith('/planning/reorder')) return Promise.resolve(Response.json({
            saved: 2, recompute_enabled: true, schedule_applied: true, replayed: true,
            estimates: [{ id: 8, est_start: '2026-10-12T01:00:00+08:00' }] }));
          if (String(raw).endsWith('/planning/today')) return new Promise(resolve => { gates.refresh = resolve; });
          return baseFetch(raw, opts);
        };
        planning.enterReorder();
        const list = planning.progressList; list.insertBefore(list.lastElementChild, list.firstElementChild);
        window.confirmation = planning.confirmReorder();
      });
      await page.waitForFunction(() => gates.refresh);
      assert.deepEqual(await page.evaluate(() => planning.board.progress.map(o => o.id)), [7, 8]);
      assert.equal(await page.evaluate(() => planning.board.progress.find(o => o.id === 8).est_start), null);
      await page.evaluate(() => gates.refresh(Response.json(board)));
      await page.waitForFunction(() => !planning.reads.needsRead('today'));
      assert.deepEqual(await page.locator('#planning-progress .plan-item').evaluateAll(els => els.map(el => Number(el.dataset.occ))), [7, 8]);
    });
    for (const count of [13, 50, 100]) {
      await check(`#43 native drag ${count} variable-height cards has cached geometry and no move-time DOM inserts`, async () => {
        await install(page);
        await page.evaluate(count => {
          document.querySelector('#fixture').style.marginTop = '20px';
          planning.board.progress = Array.from({ length: count }, (_, i) => ({ ...board.progress[0], id: i + 1, task_id: i + 1,
            content: '待办 ' + (i + 1) + (i % 3 === 0 ? ' 长内容'.repeat(15) : ''), sort_order: i }));
          planning.renderBoard(); planning.enterReorder();
          window.geometryReads = 0; window.domInserts = 0;
          document.querySelectorAll('#planning-progress .plan-item').forEach(el => {
            const measure = el.getBoundingClientRect.bind(el);
            el.getBoundingClientRect = () => { geometryReads++; return measure(); };
          });
          const append = planning.progressList.appendChild.bind(planning.progressList);
          planning.progressList.appendChild = el => { domInserts++; return append(el); };
        }, count);
        const handle = page.locator('.plan-handle').first();
        const box = await handle.boundingBox();
        await page.mouse.move(box.x + 5, box.y + 10); await page.mouse.down();
        await page.mouse.move(box.x + 5, box.y + 310, { steps: 120 });
        const moving = await page.evaluate(() => ({ geometryReads, domInserts, submits: calls.filter(c => c.route.endsWith('/reorder')).length }));
        assert.equal(moving.domInserts, 0);
        assert.equal(moving.submits, 0);
        assert.ok(moving.geometryReads < count * 5, JSON.stringify(moving));
        await page.mouse.up();
        const order = await page.locator('#planning-progress .plan-item').evaluateAll(els => els.map(el => Number(el.dataset.occ)));
        assert.notEqual(order[0], 1);
        assert.equal(await page.locator('.plan-drag-copy').count(), 0);
        result.drag.push({ count, ...moving, firstAfterDrop: order[0] });
      });
    }
    await check('#43 edge scroll, handle-only capture, pointercancel, cancel restores original', async () => {
      await page.setViewportSize({ width: 390, height: 640 });
      await install(page);
      await page.evaluate(() => {
        document.querySelector('#fixture').style.marginTop = '20px';
        planning.board.progress = Array.from({ length: 50 }, (_, i) => ({ ...board.progress[0], id: i + 1, sort_order: i }));
        planning.renderBoard(); planning.enterReorder();
      });
      await page.locator('.plan-item-main').first().dispatchEvent('pointerdown', { pointerType: 'touch', pointerId: 25, clientY: 100 });
      assert.equal(await page.locator('.plan-drag-copy').count(), 0);
      const box = await page.locator('.plan-handle').first().boundingBox();
      await page.evaluate(() => document.addEventListener('pointerdown', ev => { window.dragPointerId = ev.pointerId; }, { once: true }));
      await page.mouse.move(box.x + 5, box.y + 10); await page.mouse.down();
      await page.mouse.move(box.x + 5, 630, { steps: 30 });
      await page.waitForFunction(() => scrollY > 50);
      await page.evaluate(() => document.querySelector('.plan-item.is-dragging').dispatchEvent(new PointerEvent('pointercancel', { pointerId: dragPointerId })));
      await page.mouse.up();
      assert.equal(await page.locator('.plan-drag-copy').count(), 0);
      assert.equal(await page.locator('.plan-item.is-dragging').count(), 0);
      assert.equal(await page.locator('#planning-progress .plan-item').first().getAttribute('data-occ'), '1');
      await page.evaluate(() => planning.cancelReorder());
      assert.equal(await page.evaluate(() => planning.reorderMode), false);
      assert.equal(await page.evaluate(() => calls.filter(c => c.route.endsWith('/reorder')).length), 0);
    });
    for (const viewport of [{ width: 1280, height: 550 }, ...[320, 375, 390, 414].map(width => ({ width, height: 640 })), { width: 390, height: 360 }, { width: 844, height: 390 }]) {
      await page.setViewportSize(viewport);
      await check(`#41/BUG-19/20/21 fit viewport ${viewport.width}x${viewport.height}`, async () => {
        await install(page);
        await page.evaluate(async () => { planning.switchTab('memo'); await planning.memo.openReader(7); });
        await page.waitForTimeout(300);
        const fits = await page.locator('[data-reader-edit],[data-reader-archive],[data-reader-delete],[data-reader-close]').evaluateAll(els => els.every(el => {
          const r = el.getBoundingClientRect(); const p = el.closest('.detail-panel').getBoundingClientRect();
          return r.left >= p.left && r.right <= p.right && r.bottom <= innerHeight && r.top >= 0;
        }));
        assert.equal(fits, true);
        await page.locator('[data-reader-edit]').click();
        await page.waitForSelector('.memo-editor');
        for (const status of ['idle', 'pending', 'saving', 'saved', 'error', 'conflict', 'lifecycle']) {
          await page.evaluate(status => { planning.memo._saveState = { status, error: new Error('ERROR'.repeat(80)) }; planning.memo.renderSaveStatus(); }, status);
          const layout = await page.locator('.memo-editor').evaluate(el => {
            const body = el.querySelector('.modal-body'); const button = el.querySelector('[data-editor-close]').getBoundingClientRect();
            const actions = [...el.querySelectorAll('[data-editor-close],[data-save-retry],[data-save-overwrite]')].map(el => el.getBoundingClientRect());
            return { width: body.clientWidth, scrollWidth: body.scrollWidth, height: button.height,
              fits: actions.every(r => r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight && r.top >= 0) };
          });
          assert.ok(layout.scrollWidth <= layout.width + 1, JSON.stringify(layout));
          assert.ok(layout.height < 50 && layout.fits, JSON.stringify(layout));
        }
        await page.screenshot({ path: path.join(out, `memo-editor-${viewport.width}-${viewport.height}.png`) });
        await page.evaluate(() => planning.memo.closeEditor());
        await page.evaluate(() => { planning.switchTab('today'); planning.select('occ', 7); });
        await page.waitForTimeout(300);
        assert.equal(await page.locator('.detail-foot .btn').evaluateAll(els => els.every(el => {
          const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
        })), true);
        assert.equal(await page.locator('.detail-body').evaluate(el => el.textContent.includes('改动保存到所属待办')), false);
        await page.screenshot({ path: path.join(out, `todo-detail-${viewport.width}-${viewport.height}.png`) });
      });
    }
    await check('#41/UI-01 night, enlarged text, long title/Markdown and independent scroll', async () => {
      await page.setViewportSize({ width: 375, height: 640 });
      await install(page);
      await page.evaluate(async () => {
        document.documentElement.dataset.theme = 'night';
        const style = document.createElement('style'); style.textContent = '#planning-layout { font-size:150%; } .detail-title { font-size:22px; } .detail-foot .btn { font-size:20px; }'; document.head.append(style);
        entries[7].title = '长标题'.repeat(50);
        entries[7].content = '# 标题\n\n' + '很长的 Markdown 正文\n\n'.repeat(100) + '<img src=x onerror="window.unsafe=true">';
        planning.switchTab('memo'); await planning.memo.openReader(7);
      });
      await page.waitForTimeout(250);
      assert.equal(await page.locator('[data-reader-edit],[data-reader-archive],[data-reader-delete],[data-reader-close]').evaluateAll(els => els.every(el => {
        const r = el.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth;
      })), true);
      assert.equal(await page.locator('.detail-body').evaluate(el => el.scrollHeight > el.clientHeight), true);
      assert.equal(await page.evaluate(() => window.unsafe), undefined);
      await page.evaluate(() => planning.switchTab('goals'));
      assert.equal(await page.evaluate(() => planning.detail.isOpenMobile()), false);
      assert.equal(await page.locator('.detail-title').textContent(), '');
    });
    await check('BUG-20 visual viewport shrink/offset preserves footer and cleans up listeners', async () => {
      await install(page);
      await page.evaluate(() => {
        window.testViewport = new EventTarget();
        Object.assign(testViewport, { height: 640, offsetTop: 0, offsetLeft: 0 });
        window.viewportListeners = new Set();
        const add = testViewport.addEventListener.bind(testViewport), remove = testViewport.removeEventListener.bind(testViewport);
        testViewport.addEventListener = (type, fn) => { viewportListeners.add(fn); add(type, fn); };
        testViewport.removeEventListener = (type, fn) => { viewportListeners.delete(fn); remove(type, fn); };
        Object.defineProperty(window, 'visualViewport', { configurable: true, value: testViewport });
        planning.switchTab('memo'); planning.memo.openEditor(null);
      });
      await page.waitForSelector('.memo-editor');
      await page.evaluate(() => {
        testViewport.height = 320; testViewport.offsetTop = 40;
        testViewport.dispatchEvent(new Event('resize'));
        planning.memo._saveState = { status: 'error', error: new Error('失败'.repeat(500)) }; planning.memo.renderSaveStatus();
      });
      const actions = await page.locator('[data-editor-close],[data-save-retry]').evaluateAll(els => els.map(el => {
        const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
      }));
      assert.ok(actions.every(r => r.top >= 40 && r.bottom <= 360 && r.left >= 0 && r.right <= 375), JSON.stringify(actions));
      assert.equal(await page.evaluate(() => viewportListeners.size), 1);
      await page.evaluate(() => planning.memo.closeEditor());
      assert.equal(await page.evaluate(() => viewportListeners.size), 0);
      await page.evaluate(() => { delete window.visualViewport; });
    });
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(out, 'results.json'), JSON.stringify(result, null, 2));
    process.stdout.write(JSON.stringify({ passed: result.checks.length, drag: result.drag, out }, null, 2));
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
}
main().catch(error => { fs.writeFileSync(path.join(out, 'failure.txt'), error.stack); process.stderr.write(error.stack); process.exitCode = 1; });
