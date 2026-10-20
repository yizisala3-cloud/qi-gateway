// Real admin UI + real HTTP API + isolated PG. Only race/lost-response cases
// intercept transport; every successful write still executes the real RPC.
const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const BASE='http://127.0.0.1:8026', TOKEN='local-optimization-preview';
const output=path.resolve(process.env.PLANNING_BROWSER_OUTPUT || 'tests/planning-optimization-20261016');
const deferred=()=>{ let resolve; return {promise:new Promise(r=>{resolve=r;}),resolve}; };

(async()=>{
  await fs.mkdir(output,{recursive:true});
  const fixture=JSON.parse(await fs.readFile('.venv/optimization-preview-fixture.json','utf8'));
  const ids=fixture.ids;
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:900}});
  const page=await context.newPage(), results=[], errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(token=>localStorage.setItem('qi-token',token),TOKEN);
  const api=async(suffix,method='GET',data=null)=>{
    const r=await context.request.fetch(`${BASE}/admin/api/planning/${suffix}`,{method,
      headers:{Authorization:`Bearer ${TOKEN}`},...(data?{data}:{})});
    assert.ok(r.ok(),`${method} ${suffix}: ${r.status()} ${await r.text()}`);
    return r.json();
  };
  const oid=key=>ids[key].occurrences[0], tid=key=>ids[key].task;
  const refresh=()=>page.evaluate(async()=>{
    const version=new URL(document.querySelector('script[type="module"][src*="/js/app.js"]').src).searchParams.get('v');
    const p=(await import(`/admin/js/pages/planning.js?v=${version}`)).default;
    await p.loadToday({fresh:true});
  });
  const select=async key=>{
    if(await page.locator('.detail-panel[aria-modal="true"]').isVisible()) await page.keyboard.press('Escape');
    await page.locator(`[data-occ="${oid(key)}"]`).click();
    await page.locator('.detail-title [data-act="task-detail-edit"]').waitFor();
  };
  const open=async key=>{await select(key);await page.locator('.detail-title [data-act="task-detail-edit"]').click();await page.locator('[data-edit-title]').waitFor();};
  const activate=async field=>{ const trigger=field==='name'?page.locator('.detail-title [data-act="task-detail-edit"]'):page.locator(`[data-act="task-detail-edit"][data-field="${field}"]`); if(await trigger.count()) await trigger.click(); };
  const save=async()=>{await page.locator('[data-edit-save]').click();await page.locator('[data-edit-save]').waitFor({state:'detached'});};
  const cancel=async()=>{const input=page.locator('[data-edit-title]:visible, #pe-note:visible, [data-edit-field]:visible, [data-inline-window] button:visible').first();if(await input.count())await input.press('Escape');};
  const order=async()=>page.locator('#planning-progress .plan-item').evaluateAll(rows=>rows.map(r=>Number(r.dataset.occ)));
  const drag=async(from,to)=>{
    const a=await page.locator(`[data-occ="${from}"] .plan-handle`).boundingBox();
    const b=await page.locator(`[data-occ="${to}"]`).boundingBox();
    await page.mouse.move(a.x+a.width/2,a.y+a.height/2);await page.mouse.down();
    await page.mouse.move(a.x+a.width/2,b.y+b.height-3,{steps:14});await page.mouse.up();
  };
  try{
    await page.goto(`${BASE}/admin/#/planning`);
    await page.locator('#planning-progress .plan-item').first().waitFor();
    assert.equal(await page.locator('[data-act="enter-reorder"]').count(),0);
    assert.equal(await page.locator('#planning-progress .plan-handle').count(),(await order()).length);
    await page.locator('[data-act="new-task"]').click();
    assert.equal(await page.locator('#pf-type').inputValue(),'');
    await page.locator('#pf-content').fill('普通新建必选类型');
    await page.locator('#pf-estimated').fill('30');
    await page.locator('.modal [data-ok]').click();
    await page.locator('#pf-type-error').filter({hasText:'请选择待办类型'}).waitFor();
    await page.locator('[data-retro-select="pf-type"] button').click();
    await page.locator('.retro-select-pop [role="option"]').filter({hasText:'每日'}).click();
    await page.locator('.modal [data-ok]').click();
    await page.locator('.modal').waitFor({state:'detached'});
    results.push('A01: ordinary creation requires a manual valid type');

    await page.locator('[data-tab="goals"]').click();
    await page.locator('.goal-card').first().click();
    await page.locator('[data-goal-act="new-task"]').click();
    await page.locator('.modal [data-ok]').click();
    await page.locator('#pf-type').waitFor({state:'attached'});
    assert.equal(await page.locator('#pf-type').inputValue(),'');
    await page.locator('[data-retro-select="pf-type"] button').click();
    const labels=await page.locator('.retro-select-pop [role="option"]').allTextContents();
    assert.ok(!labels.some(t=>/单次|闲时/.test(t)));
    await page.keyboard.press('Escape');
    await page.locator('.modal [data-cancel]').click();
    results.push('A02: goal creation also starts unselected and restricts types');
    await page.locator('[data-tab="all"]').click();
    await page.locator(`[data-task="${tid('ordinary')}"]`).click();
    await page.locator('[data-act="task-edit"]').click();
    assert.equal(await page.locator('#pf-type').inputValue(),'daily');
    await page.locator('.modal [data-cancel]').click();
    results.push('A03: existing task editing retains its valid type');
    await page.locator('[data-tab="today"]').click();

    await open('ordinary');
    assert.ok((await page.locator('[data-edit-save]').getAttribute('data-tooltip')).includes('当前及未来'));
    await page.locator('[data-edit-title]').fill('原位编辑后的阅读');
    await activate('duration');
    await page.locator('[data-edit-field="estimated_minutes"]').fill('60');
    await activate('note');
    await page.locator('#pe-note').fill('详情草稿，轮询不能覆盖');
    await activate('window');
    await page.locator('#pe-window-start').evaluate(input=>input._applyRetroValue('18:00',true));
    await page.locator('#pe-window-end').evaluate(input=>input._applyRetroValue('19:00',true));
    await activate('name');
    await page.evaluate(()=>{window.optimizationInput=document.querySelector('[data-edit-title]');});
    await refresh();
    assert.equal(await page.locator('[data-edit-title]').inputValue(),'原位编辑后的阅读');
    assert.ok(await page.evaluate(()=>window.optimizationInput===document.querySelector('[data-edit-title]')));
    await save();
    let current=await api(`occurrences/${oid('ordinary')}`);
    assert.equal(current.task_content,'原位编辑后的阅读');assert.equal(current.estimated_minutes,60);
    assert.equal((await api(`tasks/${tid('ordinary')}`)).estimated_minutes,60);
    assert.equal(current.est_start,current.window_start_at);
    assert.equal(current.est_end,current.window_end_at);
    assert.equal(current.is_fixed,true);
    results.push('A04/A05/A06/A18: atomic inline name/note/duration/window save with zero slack; polling preserves input');

    const before=(await api('today')).progress.map(r=>[r.id,r.sort_order]);
    await drag(oid('ordinary'),oid('second'));
    const candidate=await order();
    assert.notDeepEqual(candidate,before.map(r=>r[0]));
    assert.deepEqual((await api('today')).progress.map(r=>[r.id,r.sort_order]),before);
    await refresh();assert.deepEqual(await order(),candidate);
    await page.locator('[data-act="cancel-reorder"]').click();
    await refresh();
    assert.deepEqual(await order(),before.map(r=>r[0]));
    await drag(oid('ordinary'),oid('second'));
    await drag(oid('ordinary'),oid('started'));
    const confirmed=await order();
    await page.locator('[data-act="confirm-reorder"]').click();
    await page.locator('#planning-reorder-bar').waitFor({state:'hidden'});
    assert.deepEqual((await api('today')).progress.map(r=>r.id),confirmed);
    results.push('A10/A12: resident handles, multiple drags, no early persistence, cancel/confirm');

    await select('ordinary');
    assert.equal((await page.locator('[data-act="occ-park"]').textContent()).trim(),'沉底');
    await page.locator('[data-act="occ-park"]').click();
    await page.locator(`[data-occ="${oid('ordinary')}"][data-parked="true"]`).waitFor();
    assert.equal((await order()).at(-1),oid('ordinary'));
    assert.ok((await page.locator(`[data-occ="${oid('ordinary')}"]`).textContent()).includes('暂不排程'));
    await api('recompute','POST');await refresh();
    assert.equal((await api(`occurrences/${oid('ordinary')}`)).is_parked,true);
    assert.equal((await api(`tasks/${tid('ordinary')}`)).is_parked,true);
    assert.equal((await page.locator('[data-act="occ-park"]').textContent()).trim(),'恢复排程');
    await page.screenshot({path:path.join(output,'desktop-parked.png'),fullPage:true});
    await page.locator('[data-act="occ-park"]').click();
    await page.locator(`[data-occ="${oid('ordinary')}"][data-parked="false"]`).waitFor();
    await api('recompute','POST');await refresh();
    assert.ok((await api(`occurrences/${oid('ordinary')}`)).est_start);
    assert.equal((await api(`tasks/${tid('ordinary')}`)).is_parked,false);
    assert.equal((await page.locator('[data-act="occ-park"]').textContent()).trim(),'沉底');
    results.push('A13/A16/A19: park/restore, recompute and bottom grouping; goal count unchanged');

    await open('started');
    await activate('duration');
    assert.ok((await page.locator('[data-edit-save]').getAttribute('data-tooltip')).includes('仅未来'));
    const runningBefore=await api(`occurrences/${oid('started')}`);
    await activate('name');
    await page.locator('[data-edit-title]').fill('执行中改名');
    await activate('duration');
    await page.locator('[data-edit-field="estimated_minutes"]').fill('80');await save();
    const runningAfter=await api(`occurrences/${oid('started')}`);
    assert.equal(runningAfter.task_content,'执行中改名');
    assert.equal(runningAfter.estimated_minutes,runningBefore.estimated_minutes);
    assert.equal(runningAfter.actual_start,runningBefore.actual_start);
    assert.equal(await page.locator('[data-act="occ-park"]').count(),0);
    results.push('A07/A14: running text updates; time only future; parking absent');

    // A late context read must not replace a newer selection's draft.
    const gate=deferred(), entered=deferred(), readDone=deferred();
    const readPattern=`**/tasks/${tid('ordinary')}/detail-edit?occurrence_id=*`;
    await page.route(readPattern,async route=>{
      const response=await route.fetch();entered.resolve();await gate.promise;
      await route.fulfill({response});readDone.resolve();
    });
    await select('ordinary');await page.locator('.detail-title [data-act="task-detail-edit"]').click();await entered.promise;
    await open('second');await page.locator('[data-edit-title]').fill('较新选择的草稿');
    gate.resolve();await readDone.promise;await page.unroute(readPattern);
    await refresh();
    assert.equal(await page.locator('[data-edit-title]').inputValue(),'较新选择的草稿');await cancel();
    results.push('A18: delayed old read cannot overwrite a newer selection');

    // Commit succeeds, response is lost, then retry the exact same intent/key.
    const seen=[];let lose=true;
    const writePattern=`**/tasks/${tid('ordinary')}/detail-edit`;
    await page.route(writePattern,async route=>{
      if(route.request().method()!=='POST'){await route.continue();return;}
      seen.push({key:route.request().headers()['idempotency-key'],body:route.request().postData()});
      const response=await route.fetch();
      if(lose){lose=false;await route.abort('failed');}else await route.fulfill({response});
    });
    await open('ordinary');await page.locator('[data-edit-title]').fill('丢失回执后核实');
    await page.locator('[data-edit-save]').click();
    await page.locator('[data-edit-save]').filter({hasText:'核实保存结果'}).waitFor();
    assert.equal(await page.locator('[data-edit-title]').isDisabled(),true);
    await open('second');await page.locator('[data-edit-title]').fill('切换期间的另一个草稿');
    await open('ordinary');
    assert.equal(await page.locator('[data-edit-save]').textContent(),'核实保存结果');
    await save();
    const retries=seen.filter(x=>JSON.parse(x.body).occurrence_id===oid('ordinary'));assert.equal(retries.length,2);assert.deepEqual(retries[0],retries[1]);
    assert.equal((await api(`occurrences/${oid('ordinary')}`)).task_content,'丢失回执后核实');
    await page.unroute(writePattern);
    results.push('A18: lost successful response retries the exact same key/body');

    // A stale successful save still refreshes data without touching a new editor.
    const saveGate=deferred(), committed=deferred(), saveDone=deferred();
    await page.route(writePattern,async route=>{
      if(route.request().method()!=='POST'){await route.continue();return;}
      const response=await route.fetch();committed.resolve();await saveGate.promise;await route.fulfill({response});saveDone.resolve();
    });
    await open('ordinary');await page.locator('[data-edit-title]').fill('迟到保存的名字');
    await page.locator('[data-edit-save]').click();await committed.promise;
    await open('second');await page.locator('[data-edit-title]').fill('新条目的新草稿');
    saveGate.resolve();await saveDone.promise;await page.unroute(writePattern);await refresh();
    assert.equal(await page.locator('[data-edit-title]').inputValue(),'新条目的新草稿');await cancel();
    results.push('A18: late committed save cannot replace a new editor');

    await open('ordinary');await page.locator('[data-edit-title]').fill('并发后的草稿');
    await api(`occurrences/${oid('ordinary')}/start`,'POST');
    await api(`tasks/${tid('ordinary')}`,'PATCH',{estimated_minutes:120});
    await page.locator('[data-edit-save]').click();
    await page.locator('[data-edit-reload]').waitFor({state:'visible'});
    assert.equal(await page.locator('[data-edit-title]').inputValue(),'并发后的草稿');
    await page.locator('[data-edit-reload]').click();
    await page.locator('[data-edit-feedback]').filter({hasText:'请核对'}).waitFor();
    await activate('duration');
    assert.ok((await page.locator('[data-edit-save]').getAttribute('data-tooltip')).includes('仅未来'));
    assert.equal(await page.locator('[data-edit-field="estimated_minutes"]').inputValue(),'120');
    await activate('name');
    assert.equal(await page.locator('[data-edit-title]').inputValue(),'并发后的草稿');await save();
    assert.equal((await api(`tasks/${tid('ordinary')}`)).estimated_minutes,120);
    results.push('A18: concurrent start rejects save, preserves draft, requires scope reread');

    await open('hollow');await page.locator('[data-edit-title]').fill('先放入洗衣机');await save();
    assert.equal((await api(`occurrences/${oid('hollow')}`)).content,'先放入洗衣机·开始');
    assert.equal((await api(`tasks/${tid('hollow')}`)).hollow_start_content,'先放入洗衣机');
    results.push('A09: hollow phase names edit in place with current/future scope');

    for(const [width,height,theme] of [[1024,768,'day'],[390,844,'day'],[320,568,'night']]){
      await page.setViewportSize({width,height});
      await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
      await open('second');
      await page.locator('[data-edit-title]').fill(`窄屏草稿 ${width}`);
      assert.equal(await page.locator('[data-edit-title]').getAttribute('type'),'text');
      await activate('window');
      await page.locator('[data-retro-for="pe-window-start"] button').click();
      await page.locator('.retro-time-pop').waitFor({state:'visible'});
      await page.screenshot({path:path.join(output,`inline-${width}-${theme}.png`),fullPage:true});
      await page.keyboard.press('Escape');
      const bounds=await page.locator('.detail-panel').evaluate(el=>{
        const r=el.getBoundingClientRect(),b=el.querySelector('[data-edit-save]').getBoundingClientRect();
        return {bottom:r.bottom,top:r.top,saveBottom:b.bottom,overflow:getComputedStyle(el.querySelector('.detail-body')).overflowY};
      });
      assert.ok(bounds.bottom<=height+1 && bounds.top>=-1,JSON.stringify(bounds));
      assert.ok(bounds.saveBottom<=height,JSON.stringify(bounds));
      assert.equal(bounds.overflow,'clip');
      assert.ok(await page.locator('.detail-panel').evaluate(el=>
        [...el.querySelector('.detail-body').children].filter(child=>!child.hasAttribute('data-detail-edit-slot'))
          .every(child=>getComputedStyle(child).display!=='none')));
      await cancel();
      await page.waitForFunction(() => {
        const body=document.querySelector('.detail-panel .detail-body');
        return body && body.scrollHeight<=body.clientHeight+1;
      });
      const normal=await page.locator('.detail-panel').evaluate(el=>{
        const body=el.querySelector('.detail-body'),foot=el.querySelector('.detail-foot');
        return {bodyHeight:body.clientHeight,bodyContent:body.scrollHeight,footBottom:foot.getBoundingClientRect().bottom};
      });
      assert.ok(normal.bodyContent<=normal.bodyHeight+1,JSON.stringify({width,...normal}));
      assert.ok(normal.footBottom<=height,JSON.stringify({width,...normal}));
      await page.screenshot({path:path.join(output,`detail-${width}-${theme}.png`),fullPage:true});
      results.push(`responsive inline editor + expanded shared time control: ${width}x${height} ${theme}`);
    }
    await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:900});
    await api(`occurrences/${oid('second')}/finish`,'POST',{});await refresh();
    const historical=await api(`occurrences/${oid('second')}`);
    await page.locator('[data-tab="all"]').click();
    await page.locator(`[data-occ-all="${oid('second')}"]`).click();
    await page.locator('.detail-title [data-act="task-detail-edit"]').click();
    await page.locator('[data-edit-title]').waitFor();
    assert.ok((await page.locator('[data-edit-save]').getAttribute('data-tooltip')).includes('仅未来'));
    await page.locator('[data-edit-title]').fill('只更新未来定义');
    await activate('duration');
    await page.locator('[data-edit-field="estimated_minutes"]').fill('35');await save();
    assert.deepEqual(await api(`occurrences/${oid('second')}`),historical);
    assert.equal((await api(`tasks/${tid('second')}`)).content,'只更新未来定义');
    results.push('A08: editing from a closed record changes only future definition, preserving the full history');
    assert.equal((await api(`goals/${fixture.goal}`)).executed_count,0);
    assert.deepEqual(errors,[]);
    await fs.writeFile(path.join(output,'browser-results.json'),JSON.stringify({passed:results,errors},null,2));
    await fs.rm(path.join(output,'browser-failure.json'),{force:true});
    await fs.rm(path.join(output,'failure.png'),{force:true});
    process.stdout.write(JSON.stringify({passed:results,errors},null,2)+'\n');
  }catch(error){
    await page.screenshot({path:path.join(output,'failure.png'),fullPage:true});
    await fs.writeFile(path.join(output,'browser-failure.json'),JSON.stringify({passed:results,error:error.stack,errors},null,2));
    throw error;
  }finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
