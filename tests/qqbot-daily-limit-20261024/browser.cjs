const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {join}=require('node:path');
const assert=require('node:assert/strict');

(async()=>{
 const base=__dirname,port=JSON.parse(readFileSync(join(base,'preview-server.json'),'utf8')).port;
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:900}});
 await context.addInitScript(()=>localStorage.setItem('qi-token','daily-limit-preview-only'));
 const page=await context.newPage(),errors=[],writes=[];
 const reset=await context.request.put(`http://127.0.0.1:${port}/admin/api/qqbot/settings`,{data:{daily_limit:3},headers:{Authorization:'Bearer daily-limit-preview-only'}});
 assert.equal(reset.status(),200);
 page.on('pageerror',error=>errors.push(error.message));
 page.on('request',request=>{if(request.method()==='PUT'&&request.url().includes('/qqbot/settings'))writes.push(request.postDataJSON());});
 try{
  await page.goto(`http://127.0.0.1:${port}/admin/#/config`);
  const field=page.locator('#qqbot-daily-limit-input'),save=page.locator('[data-act="save-qqbot-daily-limit"]');
  await field.waitFor();assert.equal(await field.inputValue(),'3');
  assert.equal(await field.getAttribute('min'),'1');assert.equal(await field.getAttribute('max'),'100');
  await field.fill('30');await field.press('ArrowUp');assert.equal(await field.inputValue(),'31');
  await field.fill('30');await save.click();
  await page.waitForFunction(()=>document.querySelector('#qqbot-daily-limit-input')?.value==='30'&&!document.querySelector('#qqbot-daily-limit-input')?.disabled);
  assert.deepEqual(writes,[{daily_limit:30}]);
  assert.equal(await page.locator('#qqbot-proactive-tag').textContent(),'已关闭');
  await page.reload();await field.waitFor();assert.equal(await field.inputValue(),'30');
  for(const invalid of ['101','0','2.5','']){await field.fill(invalid);await save.click();}
  assert.equal(writes.length,1);await field.fill('30');
  const snapshots=[];
  for(const width of [1280,390])for(const theme of ['day','night']){
   await page.setViewportSize({width,height:900});
   await page.evaluate(theme=>document.documentElement.setAttribute('data-theme',theme),theme);
   const card=page.locator('#qqbot-settings-card');await card.scrollIntoViewIfNeeded();
   assert.ok(await field.isVisible());assert.ok(await save.isVisible());
   const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1);
   assert.equal(overflow,false,`horizontal overflow: ${width}/${theme}`);
   const appearance=await field.evaluate(el=>getComputedStyle(el).appearance);
   assert.equal(appearance,'textfield');
   const screenshot=`card-${width}-${theme}.png`;await card.screenshot({path:join(base,screenshot),animations:'disabled'});snapshots.push(screenshot);
  }
  await page.setViewportSize({width:1280,height:900});
  const help=page.locator('#qqbot-settings-card summary[aria-label="每日主动消息上限说明"]');
  await help.click();
  assert.equal(await help.evaluate(el=>el.parentElement.open),true);
  await page.locator('#qqbot-settings-card').screenshot({path:join(base,'card-help-open.png'),animations:'disabled'});
  assert.deepEqual(errors,[]);
  const result={passed:true,real_settings_api:true,production_candidate_frontend:true,persistence_after_reload:true,invalid_values_rejected:4,keyboard_step:true,viewports_and_themes:snapshots,proactive_remains_off:true,page_errors:errors};
  writeFileSync(join(base,'browser-result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
