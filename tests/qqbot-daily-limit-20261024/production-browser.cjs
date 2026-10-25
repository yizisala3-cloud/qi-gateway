const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const {readFileSync,writeFileSync}=require('node:fs');
const {join,resolve}=require('node:path');
const assert=require('node:assert/strict');
(async()=>{
 const auth=JSON.parse(readFileSync(resolve(__dirname,'../../qqbot/private/daily-limit-20261024/browser-auth.json'),'utf8'));
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const context=await browser.newContext({viewport:{width:1280,height:900}});
 await context.addInitScript(token=>localStorage.setItem('qi-token',token),auth.token);
 const page=await context.newPage(),errors=[],writes=[];
 page.on('pageerror',error=>errors.push(error.message));
 page.on('request',request=>{if(request.method()==='PUT'&&request.url().includes('/qqbot/settings'))writes.push(request.postDataJSON());});
 try{
  await page.goto('https://yiziwanguan.com/admin/#/config',{timeout:60000});
  const field=page.locator('#qqbot-daily-limit-input');await field.waitFor({timeout:60000});
  assert.equal(await field.inputValue(),'30');assert.equal(await field.isEnabled(),true);
  await page.locator('[data-act="save-qqbot-daily-limit"]').click();
  await page.waitForFunction(()=>!document.querySelector('#qqbot-daily-limit-input')?.disabled,{timeout:30000});
  assert.deepEqual(writes,[{daily_limit:30}]);
  await page.reload();await field.waitFor({timeout:60000});assert.equal(await field.inputValue(),'30');
  assert.equal(await page.locator('#qqbot-proactive-tag').textContent(),'已关闭');
  assert.equal(await page.locator('#qqbot-runtime-tag').textContent(),'已连接');
  const card=page.locator('#qqbot-settings-card');await card.scrollIntoViewIfNeeded();
  await card.screenshot({path:join(__dirname,'production-card-desktop.png'),animations:'disabled'});
  await page.setViewportSize({width:390,height:844});await card.scrollIntoViewIfNeeded();
  await card.screenshot({path:join(__dirname,'production-card-mobile.png'),animations:'disabled'});
  assert.deepEqual(errors,[]);
  const result={passed:true,domain:'https://yiziwanguan.com',daily_limit:30,saved_via_real_page:true,persisted_after_reload:true,proactive_enabled:false,qq_connected:true,page_errors:errors};
  writeFileSync(join(__dirname,'production-browser-result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));
 }finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
