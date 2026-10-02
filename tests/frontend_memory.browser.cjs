const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');

const VERSION = '20261002-frontend-controls1';
const TYPES = ['moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'];
const ENUMS = {
  moment: { moment_state: 'standalone' }, thread: { thread_state: 'open' },
  episode: { closure_quality: 'complete' }, inside_joke: {},
  profile: { stability: 'stable', basis: 'explicit_self_report' },
  interaction_rule: { rule_state: 'active' },
};
const DATA = {
  moment: { scene: '海边', event: '一起散步', moment_state: 'standalone' },
  thread: { open_question: '下次去哪', current_state: '正在计划', opened_at: '2026-09-20T15:22:45+08:00' },
  episode: { beginning: '约好出发', development: '一起旅行', outcome: '平安回来', closure_quality: 'complete', episode_start_time: '2026-09-20T15:22:45+08:00' },
  inside_joke: { origin: '一次对话', trigger_phrases: ['贝壳'], shared_meaning: '共同暗号', first_seen_at: '2026-09-20T15:22:45+08:00' },
  profile: { facet: '作息', statement: '习惯早起', scope: '日常', stability: 'stable', basis: 'explicit_self_report', effective_from: '2026-09-20T15:22:45+08:00' },
  interaction_rule: { trigger: '出门时', expected_behavior: '提醒防晒', scope: '日常', priority: 5, rule_state: 'active', explicit_instruction: '出门提醒我防晒', effective_from: '2026-09-20T15:22:45+08:00' },
};

function memory(type, id = TYPES.indexOf(type) + 1) {
  return {
    id, title: `${type} 测试记忆`, content: '这是一条用于前端控件验收的记忆正文。',
    tags: ['测试'], recall_tags: ['测试'], recall_scene: '谈到出门时', importance: 5,
    source: 'manual', source_type: 'document', verified: 'verified', is_active: true,
    continuity_type: type, continuity_data: DATA[type], thread_state: type === 'thread' ? 'open' : null,
    memory_time: '2026-09-20T15:22:45+08:00', time_precision: 'hour',
    evidence_message_ids: [], created_at: '2026-09-20T15:22:45+08:00', heat: 50,
  };
}

async function choose(page, id, value) {
  await page.locator(`#${id}-button`).click();
  const pop = page.locator('.retro-select-pop');
  assert.equal(await pop.count(), 1, `${id}: shared list is open`);
  assert.equal(await pop.getAttribute('role'), 'listbox');
  await pop.locator(`.retro-select-option[data-value="${value}"]`).click();
  assert.equal(await page.locator(`#${id}`).inputValue(), value, `${id}: submitted value`);
  assert.equal(await page.locator('.retro-select-pop').count(), 0, `${id}: list is closed`);
}

async function sharedOnly(page, selector = '.modal') {
  const host = page.locator(selector);
  assert.equal(await host.locator('select').count(), 0, 'no visible native select remains after render');
  assert.equal(await host.locator('input[type="date"]:not(.retro-validation-input), input[type="time"]:not(.retro-validation-input), input[type="datetime-local"]:not(.retro-validation-input)').count(), 0);
  const unnamed = await host.locator('.retro-select-field, .retro-time-field').evaluateAll((buttons) => buttons.filter((button) => !button.getAttribute('aria-label') && !button.getAttribute('aria-labelledby')).length);
  assert.equal(unnamed, 0, 'shared fields have accessible names');
}

async function screenshot(page, evidenceDir, name) {
  await page.screenshot({ path: path.join(evidenceDir, `${name}.png`), fullPage: true });
}

async function exerciseTime(page, id, evidenceDir, name) {
  await page.locator(`#${id}-button`).click();
  const pop = page.locator('.retro-time-pop');
  await pop.waitFor();
  assert.equal(await pop.locator('select').count(), 0, 'time panel uses custom hour/minute lists');
  await pop.locator('[data-day="12"]').click();
  if (name) await screenshot(page, evidenceDir, `${name}-calendar`);
  for (const [unit, value] of [['hour', '15'], ['minute', '34']]) {
    const field = pop.locator(`[data-unit="${unit}"]`);
    await field.locator('.rtp-select-btn').click();
    assert.equal(await field.locator('.rtp-select-pop').isVisible(), true);
    if (name) await screenshot(page, evidenceDir, `${name}-${unit}`);
    await field.locator(`.rtp-select-option[data-value="${value}"]`).click();
  }
  await pop.locator('[data-act="ok"]').click();
  assert.match(await page.locator(`#${id}`).inputValue(), /^\d{4}-\d{2}-12T15:34$/);
  assert.equal(await page.locator('.retro-time-pop').count(), 0);
}

async function openForm(page, mode, type) {
  await page.evaluate(async ({ mode, type, fixture, version }) => {
    const form = await import(`/admin/js/pages/_memory_form.js?v=${version}`);
    window.memoryForm = await form.openMemoryForm({ mode, memory: mode === 'create' ? null : fixture });
  }, { mode, type, fixture: memory(type), version: VERSION });
  await sharedOnly(page);
}

async function fillStructure(page, type) {
  for (const [key, value] of Object.entries(DATA[type])) {
    if (Object.hasOwn(ENUMS[type], key)) continue;
    const field = page.locator(`#cf-${key}`);
    if (String(value).includes('T') && String(value).includes('+08:00')) {
      await field.evaluate((input, local) => input._applyRetroValue(local, true), String(value).slice(0, 16));
    } else {
      await field.fill(Array.isArray(value) ? value.join('，') : String(value));
    }
  }
  for (const [key, value] of Object.entries(ENUMS[type])) await choose(page, `cf-${key}`, value);
}

async function submitForm(page, writes, expectedPath) {
  const before = writes.length;
  await page.locator('.modal [data-submit]').click();
  await page.waitForFunction(() => !document.querySelector('.modal-mask'));
  assert.equal(writes.length, before + 1, 'one form submission');
  const last = writes.at(-1);
  assert.match(last.pathname, expectedPath);
  return last.payload;
}

async function mountBrowser(page, lockedType = '', view = 'library') {
  await page.evaluate(async ({ lockedType, view, version }) => {
    document.body.innerHTML = '<main class="content" id="memory-browser-host" style="height:100dvh;overflow:auto"></main>';
    const module = await import(`/admin/js/pages/_memory_browser.js?v=${version}`);
    window.memoryBrowser = module.createMemoryBrowser({
      host: document.querySelector('#memory-browser-host'), lockedType,
      showViewTabs: !lockedType, showTypeFilter: !lockedType, defaultView: view,
    });
    await window.memoryBrowser.mount();
  }, { lockedType, view, version: VERSION });
  await sharedOnly(page, '#memory-browser-host');
}

async function runMemoryTests(browser, baseUrl, evidenceDir) {
  await fs.mkdir(evidenceDir, { recursive: true });
  const coverage = [];
  for (const [layout, viewport] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    for (const theme of ['day', 'night']) {
      const context = await browser.newContext({ viewport, timezoneId: 'Asia/Shanghai' });
      const page = await context.newPage();
      const writes = [];
      const reads = [];
      const pageErrors = [];
      page.on('pageerror', (error) => pageErrors.push(error.message));
      const rows = TYPES.map((type) => memory(type));
      const request = {
        ...memory('profile', 81), status: 'pending', update_mode: 'append', memory_key: null,
        evidence_time_precision: 'day', evidence_end_time: '2026-09-20T15:22:45+08:00',
        absorbed_fast_path_memory_ids: [],
      };
      await page.route('**/admin/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        if (req.method() !== 'GET') {
          writes.push({ pathname: url.pathname, payload: req.postDataJSON() });
          await route.fulfill({ json: { memory_id: 1, memory: { id: 1 } } });
          return;
        }
        reads.push({ pathname: url.pathname, params: Object.fromEntries(url.searchParams) });
        const eq = JSON.parse(url.searchParams.get('eq') || '{}');
        let data = url.pathname.endsWith('/memory_requests') ? [request] : rows;
        data = data.filter((row) => Object.entries(eq).every(([key, value]) => row[key] === value));
        await route.fulfill({ json: url.searchParams.get('count') === 'true' ? { count: data.length } : { data } });
      });
      try {
        await page.goto(`${baseUrl}/__frontend_controls__`);
        await page.evaluate(async ({ theme, version }) => {
          document.documentElement.dataset.theme = theme;
          document.body.innerHTML = '<main class="content" style="height:100dvh">记忆控件验收</main>';
          const style = document.createElement('link');
          style.rel = 'stylesheet'; style.href = `/admin/css/style.css?v=${version}`;
          const styleReady = new Promise((resolve, reject) => { style.onload = resolve; style.onerror = reject; });
          document.head.appendChild(style);
          const ui = await import(`/admin/js/ui.js?v=${version}`);
          ui.initTooltips?.();
          await styleReady;
        }, { theme, version: VERSION });
        const suffix = `${layout}-${theme}`;

        for (const type of TYPES) {
          await openForm(page, 'create', type);
          await choose(page, 'mf-type', type);
          await sharedOnly(page);
          await fillStructure(page, type);
          await page.locator('#mf-content').fill('这是一条新增记忆，用来检查动态表单的提交值。');
          await page.locator('#mf-recall-tags .tag-input-editor').fill('验收');
          await page.locator('#mf-recall-tags .tag-input-editor').press('Enter');
          await choose(page, 'mf-source-type', 'document');
          await exerciseTime(page, 'mf-memory-time', evidenceDir, type === 'moment' ? `memory-${suffix}` : null);
          await choose(page, 'mf-time-precision', 'minute');
          const timeFields = page.locator('#mf-continuity .retro-time-value');
          const timeField = await timeFields.count() ? await timeFields.first().getAttribute('id') : null;
          if (timeField) await exerciseTime(page, timeField, evidenceDir, null);
          const payload = await submitForm(page, writes, /\/memories\/manual$/);
          assert.equal(payload.continuity_type, type);
          assert.equal(payload.source_type, 'document');
          assert.equal(payload.time_precision, 'minute');
          assert.match(payload.memory_time, /T15:34:00\+08:00$/);
          if (timeField) assert.match(payload.continuity_data[timeField.slice(3)], /T15:34:00\+08:00$/);
          coverage.push(`${suffix}: create ${type} shared fields and Shanghai submission`);

          await openForm(page, 'edit', type);
          assert.equal(await page.locator('#mf-memory-time').inputValue(), '2026-09-20T15:22');
          assert.equal(await page.locator('#mf-time-precision').inputValue(), 'hour');
          await choose(page, 'mf-source-type', 'document');
          await choose(page, 'mf-time-precision', 'hour');
          for (const [key, value] of Object.entries(ENUMS[type])) await choose(page, `cf-${key}`, value);
          await page.locator('#mf-title').fill(`${type} 修改标题`);
          const edited = await submitForm(page, writes, /\/memories\/\d+\/edit$/);
          assert.deepEqual(edited, { title: `${type} 修改标题` }, 'unchanged stored times and precision stay out of edit patch');
          coverage.push(`${suffix}: edit ${type} preserves stored seconds and precision`);

          const sourceType = type === 'moment' ? 'profile' : 'moment';
          await openForm(page, 'change', sourceType);
          await choose(page, 'mf-type', type);
          await fillStructure(page, type);
          await sharedOnly(page);
          const changed = await submitForm(page, writes, /\/memories\/\d+\/change-type$/);
          assert.equal(changed.continuity_type, type);
          assert.equal(changed.memory_time, '2026-09-20T15:22:00+08:00');
          for (const [key, value] of Object.entries(ENUMS[type])) assert.equal(key === 'thread_state' ? changed.thread_state : changed.continuity_data[key], value);
          coverage.push(`${suffix}: change to ${type} dynamic controls`);
        }

        await openForm(page, 'edit', 'thread');
        await page.locator('#cf-opened_at-button').click();
        await page.evaluate(() => document.querySelector('#cf-thread_state')._applyRetroValue('resolved'));
        await page.waitForFunction(() => !document.querySelector('.retro-time-pop'));
        assert.equal(await page.locator('#cf-closed_at-button').getAttribute('aria-required'), 'true');
        await choose(page, 'cf-thread_state', 'paused');
        assert.equal(await page.locator('#cf-closed_at').count(), 0);
        assert.equal(await page.evaluate(() => document.activeElement.id), 'cf-thread_state-button', 'state redraw retains focus on the rebuilt field');
        await page.evaluate(() => window.memoryForm.close());
        assert.equal(await page.locator('[data-retro-overlay]').count(), 0, 'nested redraw and modal removal reclaim overlays');
        coverage.push(`${suffix}: thread state redraw and nested field cleanup`);

        await openForm(page, 'edit', 'moment');
        await page.locator('#mf-memory-time-button').click();
        await page.locator('.retro-time-pop [data-act="clear"]').click();
        assert.equal(await page.locator('#mf-memory-time').inputValue(), '');
        assert.equal(await page.locator('#mf-time-precision').inputValue(), '');
        assert.equal(await page.locator('#mf-time-precision-button .retro-select-text').textContent(), '未指定');
        const cleared = await submitForm(page, writes, /\/edit$/);
        assert.deepEqual(cleared, { memory_time: null, time_precision: 'unknown' });
        coverage.push(`${suffix}: time clear synchronizes precision text and saved unknown`);

        for (const lockedType of ['', 'profile', 'interaction_rule']) {
          await mountBrowser(page, lockedType);
          await choose(page, 'lib-sort', 'importance');
          assert.equal(await page.evaluate(() => window.memoryBrowser.state.sort), 'importance');
          if (!lockedType) {
            await choose(page, 'lib-type', 'episode');
            assert.equal(await page.evaluate(() => window.memoryBrowser.state.type), 'episode');
            await choose(page, 'lib-type', '');
          } else {
            assert.equal(await page.locator('#lib-type').count(), 0);
            assert.equal(await page.evaluate(() => window.memoryBrowser.state.type), lockedType);
          }
          await page.locator('#lib-sort-button').click();
          if (lockedType === 'profile') await screenshot(page, evidenceDir, `profile-sort-${suffix}`);
          await page.keyboard.press('Escape');
          assert.equal(await page.locator('.retro-select-pop').count(), 0);
          assert.equal(reads.some((read) => read.params.order === 'importance'), true);
          coverage.push(`${suffix}: ${lockedType || 'library'} shared sorting and filter`);
        }

        await mountBrowser(page, '', 'requests');
        await choose(page, 'req-status', '');
        await choose(page, 'req-type', 'profile');
        await page.evaluate(() => window.memoryBrowser.openRequest(81));
        await page.locator('[data-act="req-approve"]').click();
        await page.locator('#rv-update-mode-button').waitFor();
        await sharedOnly(page, '.detail-panel');
        assert.equal(await page.locator('#rv-evidence-precision').inputValue(), 'day');
        await choose(page, 'rv-update-mode', 'replace');
        await choose(page, 'rv-update-mode', 'append');
        await page.locator('#rv-evidence-precision-button').click();
        await screenshot(page, evidenceDir, `review-precision-${suffix}`);
        await page.locator('.retro-select-pop [data-value="hour"]').click();
        const before = writes.length;
        await page.locator('[data-act="req-approve-save"]').click();
        await page.waitForFunction(() => !document.querySelector('#rv-update-mode'));
        assert.equal(writes.length, before + 1);
        assert.equal(writes.at(-1).payload.evidence_time_precision, 'hour');
        assert.equal(writes.at(-1).payload.update_mode, 'append');
        assert.equal(writes.at(-1).payload.action, 'approve');
        coverage.push(`${suffix}: request filters and review precision initial value / submission`);
        assert.deepEqual(pageErrors, [], `${suffix}: browser runtime errors`);
      } finally {
        await context.close();
      }
    }
  }
  return coverage;
}

module.exports = { runMemoryTests };
