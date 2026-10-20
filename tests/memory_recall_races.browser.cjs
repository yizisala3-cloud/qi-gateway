// Deterministic delayed HTTP responses against the real admin UI; no live services.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '../admin');
const output = path.resolve(__dirname, 'memory-recall-races-20261020');
const snapshots = [1, 2].map(id => ({ id, batch_id: `batch-${id}`, memory_id: id,
  position: id, recalled_at: '2026-10-20T12:00:00Z', title: `snapshot-${id}`,
  continuity_type: 'moment', injection_text: `injected-snapshot-${id}` }));
const memory = (id, extra = {}) => ({ id, title: `current-memory-${id}`, content: `current-body-${id}`,
  continuity_type: 'moment', tags: [], is_active: true, verified: 'verified',
  heat: 50, importance: 5, created_at: '2026-10-20T12:00:00Z', ...extra });
const request = id => ({ id, title: `request-${id}`, content: `request-body-${id}`, status: 'pending',
  continuity_type: 'moment', tags: [], importance: 5, created_at: '2026-10-20T12:00:00Z' });
const deferred = () => { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; };
const filterId = url => JSON.parse(url.searchParams.get('eq') || '{}').id;
const server = http.createServer(async (req, res) => {
  try {
    const relative = decodeURIComponent(new URL(req.url, 'http://local').pathname).replace(/^\/admin\/?/, '') || 'index.html';
    const file = path.resolve(root, relative);
    if (!file.startsWith(root + path.sep)) { res.writeHead(403).end(); return; }
    const data = await fs.readFile(file);
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css' })[path.extname(file)] || 'application/octet-stream');
    res.end(data);
  } catch { res.writeHead(404).end(); }
});

(async () => {
  await fs.mkdir(output, { recursive: true });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}/admin/`;
  const results = [], errors = [];
  let browser;
  try {
    browser = await chromium.launch({ channel: 'msedge', headless: true });
    // A fresh real app per case keeps delayed responses and route lifecycles independent.
    async function run(name, scenario, { gate, failure = false, route = '#/memories?view=recall', memories = [memory(1), memory(2)] } = {}) {
      const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
      const entered = deferred(), release = deferred(), settled = deferred();
      let gated = false, gatedRequest = null, detailReads = 0;
      const isDetail = url => ['memories', 'memory_requests'].some(table => url.pathname.endsWith('/' + table))
        && url.searchParams.get('limit') === '1' && url.searchParams.get('count') !== 'true';
      page.on('pageerror', error => errors.push({ name, message: error.message }));
      await page.addInitScript(() => localStorage.setItem('qi-token', 'local-race-fixture'));
      await page.route('**/status', handler => handler.fulfill({ json: { status: 'ok' } }));
      await page.route('**/admin/api/**', async handler => {
        const url = new URL(handler.request().url());
        if (isDetail(url)) detailReads++;
        if (!gated && gate?.(url)) {
          gated = true;
          gatedRequest = handler.request();
          entered.resolve();
          await release.promise;
          if (failure) { await handler.fulfill({ status: 503, json: { error: 'delayed-fixture-failure' } }); return; }
        }
        if (url.pathname.endsWith('/memories/1/archive')) {
          await handler.fulfill({ json: { memory_id: 1 } }); return;
        }
        const table = url.pathname.split('/').pop();
        let data = table === 'memory_recall_history' ? snapshots
          : table === 'memories' ? memories : table === 'memory_requests' ? [request(1), request(2)] : [];
        const id = filterId(url);
        if (id) data = data.filter(row => row.id === Number(id));
        await handler.fulfill({ json: { data, count: data.length } });
      });
      page.on('requestfinished', req => { if (req === gatedRequest) settled.resolve(); });
      let timeout;
      try {
        await page.goto(base + route);
        const flush = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        await Promise.race([scenario({ page, entered: entered.promise, release: async () => {
          release.resolve(); await settled.promise; await flush();
        }, reads: () => detailReads, flush }), new Promise((_, reject) => {
          timeout = setTimeout(() => reject(new Error(`Timed out: ${name}`)), 15000);
        })]);
        results.push({ name, passed: true });
        process.stdout.write(`PASS ${name}\n`);
      } finally { clearTimeout(timeout); release.resolve(); await page.close(); }
    }
    const detailGate = url => url.pathname.endsWith('/memories') && url.searchParams.get('limit') === '1'
      && Number(filterId(url)) === 1;
    const listGate = url => url.pathname.endsWith('/memories') && url.searchParams.get('limit') === '20';
    const openCurrent = async page => {
      await page.locator('[data-act="recall-open"][data-id="1"]').click();
      await page.locator('[data-act="recall-current"]').click();
    };
    // Initial URL intent starts before the initial list, even within one browser.
    // Keep these before the older matrix so the pre-fix defect fails immediately.
    for (const kind of ['memory', 'request']) {
      for (const failure of [false, true]) {
        for (const action of ['snapshot', 'tab', 'memory', 'request', 'close', 'unmount']) {
          await run(`initial ${kind}, list ${failure ? 'failure' : 'success'}, later ${action} wins`, async ({ page, entered, release, reads }) => {
            await entered;
            if (action === 'unmount') {
              await page.evaluate(() => { location.hash = '#/persona?tab=profile'; });
              await page.locator('#persona-view [data-act="open-mem"]').first().waitFor();
              await page.evaluate(() => { location.hash = '#/memories?view=recall'; });
            } else if (action === 'snapshot') await page.locator('[data-view="recall"]').click();
            if (action === 'snapshot' || action === 'unmount') {
              await page.locator('[data-act="recall-open"][data-id="2"]').click();
            } else if (action === 'memory') {
              // Replace the held list without changing views, then select another memory.
              await page.locator('[data-act="refresh"]').click();
              await page.locator('[data-act="open-mem"][data-id="2"]').click();
              await page.locator('.detail-title').filter({ hasText: 'current-memory-2' }).waitFor();
            } else if (action === 'request' || action === 'tab') {
              await page.locator('[data-view="requests"]').click();
              await page.locator('[data-act="open-req"][data-id="2"]').waitFor();
              if (action === 'request') {
                await page.locator('[data-act="open-req"][data-id="2"]').click();
                await page.locator('.detail-title').filter({ hasText: 'request-2' }).waitFor();
              }
            } else if (action === 'close') await page.locator('.detail-collapse').click();
            const before = await page.locator('.detail-panel').textContent();
            await release();
            assert.equal(await page.locator('.detail-panel').textContent(), before);
            assert.equal(reads(), ['memory', 'request'].includes(action) ? 1 : 0);
            const activeView = await page.locator('.tab.active').getAttribute('data-view');
            assert.equal(activeView, ['snapshot', 'unmount'].includes(action) ? 'recall'
              : ['request', 'tab'].includes(action) ? 'requests' : 'library');
            if (action === 'close') {
              assert.equal(await page.locator('.page-with-detail').evaluate(el => el.classList.contains('detail-collapsed')), true);
            } else if (action !== 'tab') {
              const selector = ['snapshot', 'unmount'].includes(action) ? '#recall-list' : action === 'memory' ? '#lib-list' : '#req-list';
              assert.equal(await page.locator(`${selector} .selected`).getAttribute('data-id'), '2');
            }
            if (action === 'snapshot' && !failure) await page.screenshot({ path: path.join(output, `initial-${kind}-snapshot-preserved.png`), fullPage: true });
          }, { gate: listGate, failure, route: `#/memories?${kind}=1` });
        }
        await run(`normal initial ${kind} with list ${failure ? 'failure' : 'success'}`, async ({ page, entered, release, reads }) => {
          await entered; await release();
          await page.locator('.detail-title').filter({ hasText: kind === 'memory' ? 'current-memory-1' : 'request-1' }).waitFor();
          assert.equal(await page.locator('.tab.active').getAttribute('data-view'), kind === 'memory' ? 'library' : 'requests');
          assert.equal(reads(), 1);
        }, { gate: listGate, failure, route: `#/memories?${kind}=1` });
        for (const view of ['requests', 'recall']) {
          await run(`initial ${kind} from ${view}, list ${failure ? 'failure' : 'success'}, later selection wins`, async ({ page, entered, release, reads }) => {
            await entered;
            if (view === 'requests') {
              await page.locator('[data-view="recall"]').click();
              await page.locator('[data-act="recall-open"][data-id="2"]').click();
            } else {
              await page.locator('[data-view="library"]').click();
              await page.locator('[data-act="open-mem"][data-id="2"]').click();
              await page.locator('.detail-title').filter({ hasText: 'current-memory-2' }).waitFor();
            }
            const before = await page.locator('.detail-panel').textContent();
            await release();
            assert.equal(await page.locator('.detail-panel').textContent(), before);
            assert.equal(reads(), view === 'requests' ? 0 : 1);
            assert.equal(await page.locator('.tab.active').getAttribute('data-view'), view === 'requests' ? 'recall' : 'library');
          }, { gate: url => view === 'requests'
            ? url.pathname.endsWith('/memory_requests') && url.searchParams.get('limit') === '100'
            : url.pathname.endsWith('/memory_recall_history'), failure, route: `#/memories?view=${view}&${kind}=1` });
        }
      }
    }
    for (const failure of [false, true]) {
      for (const stage of ['detail', 'library']) {
        await run(`late ${stage} ${failure ? 'failure' : 'success'} preserves later snapshot`, async ({ page, entered, release, reads }) => {
          await openCurrent(page); await entered;
          await page.locator('[data-view="recall"]').click();
          await page.locator('[data-act="recall-open"][data-id="2"]').click();
          await release();
          assert.match(await page.locator('.detail-title').innerText(), /snapshot-2/);
          assert.match(await page.locator('.detail-body').innerText(), /injected-snapshot-2/);
          assert.equal(await page.locator('.tab.active').getAttribute('data-view'), 'recall');
          assert.equal(await page.locator('#recall-list .selected').getAttribute('data-id'), '2');
          assert.equal(await page.locator('[data-act="mem-edit"]').count(), 0);
          assert.equal(reads(), stage === 'library' ? 0 : 1);
          if (stage === 'detail' && !failure) await page.screenshot({ path: path.join(output, 'snapshot-preserved.png'), fullPage: true });
        }, { gate: stage === 'detail' ? detailGate : listGate, failure });
      }
    }
    for (const failure of [false, true]) {
      await run(`late detail ${failure ? 'failure' : 'success'} preserves other tab`, async ({ page, entered, release }) => {
        await openCurrent(page); await entered;
        await page.locator('[data-view="requests"]').click();
        await page.locator('[data-act="open-req"][data-id="2"]').click();
        await page.locator('.detail-title').filter({ hasText: 'request-2' }).waitFor();
        await release();
        assert.match(await page.locator('.detail-title').innerText(), /request-2/);
        assert.match(await page.locator('.detail-body').innerText(), /request-body-2/);
      }, { gate: detailGate, failure });
      await run(`closed panel ignores late detail ${failure ? 'failure' : 'success'}`, async ({ page, entered, release }) => {
        await openCurrent(page); await entered;
        await page.locator('.detail-collapse').click();
        const before = await page.locator('.detail-panel').textContent();
        await release();
        assert.equal(await page.locator('.detail-panel').textContent(), before);
        assert.equal(await page.locator('.page-with-detail').evaluate(el => el.classList.contains('detail-collapsed')), true);
      }, { gate: detailGate, failure });
      await run(`newer memory wins over old detail ${failure ? 'failure' : 'success'}`, async ({ page, entered, release }) => {
        await openCurrent(page); await entered;
        await page.locator('[data-act="open-mem"][data-id="2"]').click();
        await page.locator('.detail-title').filter({ hasText: 'current-memory-2' }).waitFor();
        await release();
        assert.match(await page.locator('.detail-title').innerText(), /current-memory-2/);
      }, { gate: detailGate, failure });
    }
    for (const failure of [false, true]) {
      for (const stage of ['detail', 'library']) {
        await run(`unmount/remount ignores old ${stage} ${failure ? 'failure' : 'success'}`, async ({ page, entered, release }) => {
          await openCurrent(page); await entered;
          await page.evaluate(() => { location.hash = '#/persona?tab=profile'; });
          await page.locator('#persona-view [data-act="open-mem"]').first().waitFor();
          await page.evaluate(() => { location.hash = '#/memories?view=recall'; });
          await page.locator('[data-act="recall-open"][data-id="2"]').click();
          await release();
          assert.match(await page.locator('.detail-title').innerText(), /snapshot-2/);
          assert.equal(await page.getByText(/页面加载失败/).count(), 0);
        }, { gate: stage === 'detail' ? detailGate : listGate, failure });
      }
    }
    for (const failure of [false, true]) {
      await run(`request detail ${failure ? 'failure' : 'success'} cannot replace snapshot`, async ({ page, entered, release }) => {
        await page.locator('[data-act="open-req"][data-id="1"]').click(); await entered;
        await page.locator('[data-view="recall"]').click();
        await page.locator('[data-act="recall-open"][data-id="2"]').click();
        await release();
        assert.match(await page.locator('.detail-title').innerText(), /snapshot-2/);
      }, { gate: url => url.pathname.endsWith('/memory_requests') && url.searchParams.get('limit') === '1', failure, route: '#/memories?view=requests' });
    }
    for (const action of ['approve', 'reject']) {
      let hold = false;
      await run(`late ${action} form cannot replace snapshot`, async ({ page, entered, release }) => {
        await page.locator('[data-act="open-req"][data-id="1"]').click();
        await page.locator('.detail-title').filter({ hasText: 'request-1' }).waitFor();
        hold = true;
        await page.locator(`[data-act="req-${action}"]`).click(); await entered;
        await page.locator('[data-view="recall"]').click();
        await page.locator('[data-act="recall-open"][data-id="2"]').click();
        await release();
        assert.match(await page.locator('.detail-title').innerText(), /snapshot-2/);
      }, { gate: url => hold && url.pathname.endsWith('/memory_requests') && url.searchParams.get('limit') === '1', route: '#/memories?view=requests' });
    }
    await run('late review result cannot clear later snapshot', async ({ page, entered, release }) => {
      await page.locator('[data-act="open-req"][data-id="1"]').click();
      await page.locator('[data-act="req-reject"]').click();
      await page.locator('[data-act="req-reject-save"]').click(); await entered;
      await page.locator('[data-view="recall"]').click();
      await page.locator('[data-act="recall-open"][data-id="2"]').click();
      await release();
      assert.match(await page.locator('.detail-title').innerText(), /snapshot-2/);
    }, { gate: url => url.pathname.endsWith('/memory-requests/1/review'), route: '#/memories?view=requests' });
    await run('unmounted initial deep link does not resume on new page', async ({ page, entered, release, reads }) => {
      await entered;
      await page.evaluate(() => { location.hash = '#/persona?tab=profile'; });
      await page.locator('#persona-view [data-act="open-mem"]').first().waitFor();
      await page.evaluate(() => { location.hash = '#/memories?view=recall'; });
      await page.locator('[data-act="recall-open"][data-id="2"]').click();
      await release();
      assert.match(await page.locator('.detail-title').innerText(), /snapshot-2/);
      assert.equal(reads(), 0);
    }, { gate: listGate, route: '#/memories?memory=1' });
    await run('late write result preserves a newer memory selection', async ({ page, entered, release }) => {
      await openCurrent(page);
      await page.locator('[data-act="mem-archive"]').click();
      await page.locator('[data-ok]').click(); await entered;
      await page.locator('[data-act="open-mem"][data-id="2"]').click();
      await page.locator('.detail-title').filter({ hasText: 'current-memory-2' }).waitFor();
      await release();
      assert.match(await page.locator('.detail-title').innerText(), /current-memory-2/);
      assert.equal(await page.locator('#lib-list .selected').getAttribute('data-id'), '2');
    }, { gate: url => url.pathname.endsWith('/memories/1/archive') });
    for (const [name, memories, expected, action] of [
      ['active', [memory(1)], /current-memory-1/, 'mem-edit'],
      ['deleted', [], /记忆不存在或已被删除/, null],
      ['archived', [memory(1, { is_active: false })], /自然归档记忆/, 'mem-restore'],
      ['superseded', [memory(1, { is_active: false, superseded_by_memory_id: 2 })], /已被新版本替代/, null],
    ]) {
      await run(`normal current-memory navigation: ${name}`, async ({ page }) => {
        await openCurrent(page);
        await page.locator('.detail-panel').filter({ hasText: expected }).waitFor();
        if (action) assert.equal(await page.locator(`[data-act="${action}"]`).count(), 1);
        if (name === 'superseded') {
          assert.equal(await page.locator('[data-act="mem-edit"], [data-act="mem-restore"]').count(), 0);
        }
      }, { memories });
    }
    await run('persona shared browser selection and tab lifecycle', async ({ page, entered, release }) => {
      await page.locator('[data-act="open-mem"][data-id="1"]').click(); await entered;
      await page.locator('[data-act="tab"][data-tab="rule"]').click();
      await page.locator('[data-act="open-mem"][data-id="2"]').click();
      await page.locator('.detail-title').filter({ hasText: 'current-memory-2' }).waitFor();
      await release();
      assert.match(await page.locator('.detail-title').innerText(), /current-memory-2/);
      assert.equal(await page.locator('[data-view="recall"]').count(), 0);
    }, { gate: detailGate, route: '#/persona?tab=profile' });
    assert.deepEqual(errors, []);
    await fs.writeFile(path.join(output, 'results.json'), JSON.stringify({ results, errors }, null, 2));
    process.stdout.write(`${results.length} browser scenarios passed\n`);
  } finally {
    if (browser) await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
