const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {join}=require('node:path');
const assert=require('node:assert/strict');
(async()=>{
 const port=JSON.parse(readFileSync(join(__dirname,'preview-server.json'),'utf8')).port;
 const origin=`http://127.0.0.1:${port}`;
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:1000}});
 await context.addInitScript(()=>localStorage.setItem('qi-token','qqbot-recovery-preview-only'));
 const page=await context.newPage(),errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 try{
  const seeded=await context.request.post(origin+'/__test/fault');assert.equal(seeded.ok(),true);
  await page.goto(origin+'/admin/#/config');
  const card=page.locator('#qqbot-settings-card'),resume=page.locator('[data-act="resume-qqbot-evaluation"]');
  await resume.waitFor();assert.equal(await resume.isEnabled(),true);
  assert.equal(await page.locator('#qqbot-evaluation-tag').textContent(),'故障停用');
  assert.equal(await page.locator('#qqbot-failure-streak').textContent(),'5 次');
  const images=[];
  for(const width of [1280,390])for(const theme of ['day','night']){
   await page.setViewportSize({width,height:1000});
   await page.evaluate(theme=>document.documentElement.setAttribute('data-theme',theme),theme);
   await card.scrollIntoViewIfNeeded();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1),false);
   const file=`fault-${width}-${theme}.png`;await card.screenshot({path:join(__dirname,file),animations:'disabled'});images.push(file);
  }
  await page.setViewportSize({width:1280,height:1000});await resume.click();
  await page.waitForFunction(()=>document.querySelector('#qqbot-evaluation-tag')?.textContent==='未启用');
  assert.equal(await resume.isDisabled(),true);
  assert.equal(await page.locator('#qqbot-proactive-tag').textContent(),'已关闭');
  assert.equal(await page.locator('#qqbot-daily-limit-input').inputValue(),'30');
  await page.reload();await resume.waitFor();assert.equal(await resume.isDisabled(),true);
  assert.equal(await page.locator('#qqbot-failure-streak').textContent(),'0 次');
  await card.screenshot({path:join(__dirname,'recovered.png'),animations:'disabled'});
  const help=card.locator('summary[aria-label="主动评估与故障恢复说明"]');await help.click();
  assert.equal(await help.evaluate(el=>el.parentElement.open),true);
  await card.screenshot({path:join(__dirname,'recovery-help-open.png'),animations:'disabled'});
  await page.goto(origin+'/admin/#/logs');
  await page.locator('.mem-card[data-act="detail"]').first().waitFor();
  const module=page.locator('#log-module');
  assert.ok((await module.locator('option').allTextContents()).some(text=>text.includes('QQBot')));
  await module.locator('..').locator('.retro-select-field').click();
  await page.locator('.retro-select-pop [data-value="qqbot"]').waitFor();
  await page.screenshot({path:join(__dirname,'qqbot-log-dropdown.png'),animations:'disabled'});
  await page.locator('.retro-select-pop [data-value="qqbot"]').click();
  await page.locator('.mem-card[data-act="detail"]').first().waitFor();
  await page.screenshot({path:join(__dirname,'qqbot-log-list.png'),fullPage:true,animations:'disabled'});
  await page.locator('.mem-card[data-act="detail"]').filter({hasText:'已停止调用'}).first().click();
  const detail=page.locator('.modal-body');
  await detail.getByText('proactive_calls_stopped',{exact:true}).waitFor();
  await detail.getByText('QQBot · proactive_evaluation',{exact:true}).waitFor();
  await page.screenshot({path:join(__dirname,'qqbot-log-detail.png'),animations:'disabled'});
  assert.deepEqual(errors,[]);
  const result={passed:true,isolated_only:true,real_settings_and_logs_api:true,fake_provider_and_transport:true,
    fault_stopped_after_five:true,manual_recovery:true,persisted_on_reload:true,proactive_stays_off:true,
    daily_limit_stays_30:true,log_module_filter:true,error_detail:true,themes_and_viewports:images,page_errors:errors};
  writeFileSync(join(__dirname,'browser-result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
