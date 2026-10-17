const { chromium } = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const BASE='http://127.0.0.1:8026',TOKEN='local-optimization-preview';
const output=path.resolve(process.env.PLANNING_BROWSER_OUTPUT || 'tests/planning-field-edit-20261017/time-cards');
const local=iso=>{const d=new Date(iso),p=n=>String(n).padStart(2,'0');return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;};
(async()=>{
  await fs.mkdir(output,{recursive:true});
  const fixture=JSON.parse(await fs.readFile('.venv/optimization-preview-fixture.json','utf8'));
  const browser=await chromium.launch({channel:'msedge',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:900}}),page=await context.newPage(),passed=[],errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(token=>localStorage.setItem('qi-token',token),TOKEN);
  const api=async suffix=>{const r=await context.request.get(`${BASE}/admin/api/planning/${suffix}`,{headers:{Authorization:`Bearer ${TOKEN}`}});assert.ok(r.ok(),await r.text());return r.json();};
  const oid=fixture.ids.ordinary.occurrences[0],started=fixture.ids.started.occurrences[0];
  const select=async id=>{
    await page.keyboard.press('Escape');
    await page.locator(`[data-occ="${id}"]`).click();
    await page.locator('.detail-title [data-act="task-detail-edit"]').waitFor();
  };
  const setTime=(id,value)=>page.locator(`#${id}`).evaluate((input,value)=>input._applyRetroValue(value,true),value);
  try{
    await page.goto(`${BASE}/admin/#/planning`);await select(oid);
    assert.equal(await page.locator('.detail-foot [data-act="occ-edit-detail"],.detail-foot [data-act="task-detail-edit"]').count(),0);
    assert.equal(await page.locator('.detail-title [data-act="task-detail-edit"]').count(),1);
    const group=page.locator('.plan-detail-actions-split');
    assert.deepEqual(await group.locator('[data-act]').evaluateAll(els=>els.map(el=>el.dataset.act)),['occ-spawn-remaining','occ-split','occ-park']);
    passed.push('Bulk edit button removed; only the header edits the name; parking is third in the auxiliary row');
    await page.locator('[data-act="task-detail-edit"][data-field="duration"]').click();
    await page.locator('[data-edit-field="estimated_minutes"]').click();
    assert.equal(await page.locator('.modal').count(),0);
    await page.locator('[data-edit-title]:visible, #pe-note:visible, [data-edit-field]:visible, [data-inline-window] button:visible').first().press('Escape');

    await page.locator('.plan-time-edit[data-act="occ-edit-estimate"]').click();
    await page.locator('.modal').filter({hasText:'修改预估时间'}).waitFor();
    await page.locator('[data-retro-for="planning-estimate-start"] button').press('Space');
    await page.locator('.retro-time-pop [role="grid"]').waitFor();
    await page.locator('.retro-time-pop [data-unit="hour"] button').first().click();
    await page.locator('.retro-time-pop [data-unit="hour"] [role="listbox"]').waitFor();
    await page.screenshot({path:path.join(output,'estimated-picker.png')});
    await page.keyboard.press('Escape');await page.keyboard.press('Escape');
    const now=new Date((await api('today')).now),start=new Date(+now+3600000),end=new Date(+start+30*60000);
    await setTime('planning-estimate-start',local(start));await setTime('planning-estimate-end',local(end));
    await page.locator('[data-retro-for="planning-estimate-start"] button').press('Enter');
    await page.locator('.modal').waitFor({state:'detached'});
    const fixed=await api(`occurrences/${oid}`);
    assert.equal(+new Date(fixed.est_start),+start);assert.equal(+new Date(fixed.est_end),+end);assert.equal(fixed.is_fixed,true);
    passed.push('Estimated range opens the shared calendar/hour/minute selector; Enter saves a real manual estimate');

    await select(started);
    await page.locator('.plan-time-edit[data-act="occ-backfill"]').click();
    await page.locator('#planning-backfill-start').waitFor({state:'attached'});
    await setTime('planning-backfill-start',local(new Date(+now-30*60000)));
    await setTime('planning-backfill-end',local(new Date(+now-15*60000)));
    await page.locator('[data-retro-for="planning-backfill-start"] button').press('Enter');
    await page.locator('.modal').waitFor({state:'detached'});
    const actual=await api(`occurrences/${started}`);
    assert.equal(actual.status,'in_progress');assert.equal(actual.actual_minutes,15);
    passed.push('Actual range opens its existing correction fields and saves real execution times without changing status');

    await page.locator('.plan-time-edit[data-act="occ-edit-estimate"]').click();
    await setTime('planning-estimate-start',local(start));await setTime('planning-estimate-end',local(end));
    await page.locator('.modal [data-ok]').click();
    await page.locator('[data-time-feedback]').filter({hasText:'未保存'}).waitFor();
    assert.equal((await api(`occurrences/${started}`)).est_start,actual.est_start);
    await page.locator('.modal [data-cancel]').click();
    passed.push('A running round still rejects estimate changes and retains the time draft');

    await select(oid);await page.locator('.detail-title [data-act="task-detail-edit"]').click();
    await page.locator('[data-edit-title]').fill('保留的名称草稿');
    await page.locator('.plan-time-edit[data-act="occ-backfill"]').click();
    await page.locator('.modal [data-cancel]').click();
    await page.locator('.detail-title [data-act="task-detail-edit"]').click();
    assert.equal(await page.locator('[data-edit-title]').inputValue(),'保留的名称草稿');
    await page.locator('[data-edit-title]:visible, #pe-note:visible, [data-edit-field]:visible, [data-inline-window] button:visible').first().press('Escape');
    await page.locator('.plan-detail-actions-split [data-act="occ-park"]').click();
    await page.locator(`[data-occ="${oid}"][data-parked="true"]`).waitFor();
    assert.equal((await api(`occurrences/${oid}`)).is_parked,true);
    await page.locator('.plan-detail-actions-split [data-act="occ-park"]').click();
    await page.locator(`[data-occ="${oid}"][data-parked="false"]`).waitFor();
    assert.equal((await api(`goals/${fixture.goal}`)).executed_count,0);
    passed.push('Time controls preserve a name draft; the relocated park/restore button works without goal contributions');

    for(const [width,height,theme] of [[1440,900,'day'],[1024,768,'day'],[390,844,'day'],[320,568,'night']]){
      await page.keyboard.press('Escape');await page.setViewportSize({width,height});
      await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);await select(oid);
      await page.waitForFunction(()=>{
        const panel=document.querySelector('.detail-panel'),r=panel.getBoundingClientRect();
        return r.top>=-1 && r.bottom<=window.innerHeight+1;
      });
      const bounds=await page.locator('.detail-panel').evaluate(panel=>{
        const body=panel.querySelector('.detail-body');
        return {top:panel.getBoundingClientRect().top,bottom:panel.getBoundingClientRect().bottom,bodyHeight:body.clientHeight,bodyContent:body.scrollHeight,
          buttons:[...panel.querySelectorAll('.plan-detail-actions-split .btn')].map(el=>{const r=el.getBoundingClientRect();return {width:r.width,top:r.top,bottom:r.bottom,left:r.left};})};
      });
      assert.ok(bounds.top>=-1&&bounds.bottom<=height+1,JSON.stringify({width,...bounds}));
      assert.ok(bounds.bodyContent<=bounds.bodyHeight+1,JSON.stringify({width,...bounds}));
      assert.equal(bounds.buttons.length,3);
      assert.ok(bounds.buttons.every(r=>Math.abs(r.width-bounds.buttons[0].width)<1&&Math.abs(r.top-bounds.buttons[0].top)<1&&r.bottom<=height+1),JSON.stringify({width,...bounds}));
      await page.screenshot({path:path.join(output,`time-cards-${width}-${theme}.png`)});
      passed.push(`Time cards and three equal auxiliary buttons fit ${width}x${height} ${theme}`);
    }
    assert.deepEqual(errors,[]);
    await fs.writeFile(path.join(output,'browser-results.json'),JSON.stringify({passed,errors},null,2));
    await fs.rm(path.join(output,'browser-failure.json'),{force:true});
    process.stdout.write(JSON.stringify({passed,errors},null,2)+'\n');
  }catch(error){await page.screenshot({path:path.join(output,'failure.png')});await fs.writeFile(path.join(output,'browser-failure.json'),JSON.stringify({passed,errors,error:error.stack},null,2));throw error;}
  finally{await browser.close();}
})().catch(error=>{process.stderr.write(error.stack+'\n');process.exitCode=1;});
