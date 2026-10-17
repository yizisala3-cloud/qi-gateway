// Focus and append checks against the running local preview; no business writes.
const {chromium}=require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');
const fs=require('node:fs/promises'),path=require('node:path'),assert=require('node:assert/strict');
const output=path.resolve('tests/planning-caret-end-20261017');
(async()=>{
 const fixture=JSON.parse(await fs.readFile('.venv/optimization-preview-fixture.json','utf8'));
 const browser=await chromium.launch({channel:'msedge',headless:true}),passed=[],errors=[];
 let writes=0;
 try{
  for(const mobile of [false,true]){
   const context=await browser.newContext({viewport:mobile?{width:390,height:844}:{width:1440,height:1000},hasTouch:mobile,isMobile:mobile});
   await context.addInitScript(()=>localStorage.setItem('qi-token','local-optimization-preview'));
   const page=await context.newPage();page.on('pageerror',e=>errors.push(e.stack));
   page.on('request',r=>{if(['POST','PATCH','DELETE'].includes(r.method()))writes++;});
   await page.goto('http://127.0.0.1:8026/admin/#/planning');
   await page.locator(`[data-occ="${fixture.ids.ordinary.occurrences[0]}"]`).click();
   const activate=field=>page.locator(field==='name'?'.detail-title [data-act="task-detail-edit"]':`[data-act="task-detail-edit"][data-field="${field}"]`).click();
   const atEnd=async selector=>{
    const el=page.locator(selector);await el.waitFor();
    const position=await el.evaluate(e=>({start:e.selectionStart,end:e.selectionEnd,length:e.value.length,focused:document.activeElement===e}));
    assert.equal(position.start,position.length);assert.equal(position.end,position.length);assert.equal(position.focused,true);
    return el;
   };
   for(const [field,selector] of [['note','#pe-note'],['name','[data-edit-title]'],['duration','[data-edit-field="estimated_minutes"]']]){
    if(mobile && !await page.locator('.detail-title [data-act="task-detail-edit"]').isVisible()) await page.locator(`[data-occ="${fixture.ids.ordinary.occurrences[0]}"]`).click();
    await activate(field);const input=await atEnd(selector),original=await input.inputValue();
    await input.pressSequentially('5');assert.equal(await input.inputValue(),original+'5');
    await input.fill(original);
    await input.press('Escape');passed.push(`${mobile?'touch':'desktop'} ${field}: entry caret at end and typing appends`);
   }
   if(mobile && !await page.locator('.detail-title [data-act="task-detail-edit"]').isVisible()) await page.locator(`[data-occ="${fixture.ids.ordinary.occurrences[0]}"]`).click();
   await activate('note');const originalNote=await page.locator('#pe-note').inputValue();await page.locator('#pe-note').fill('第一行🙂\n最后一行🌿');
   await activate('duration');await activate('note');const note=await atEnd('#pe-note');
   await note.pressSequentially('末');assert.equal(await note.inputValue(),'第一行🙂\n最后一行🌿末');
   await note.press('Control+Home');await note.pressSequentially('首');assert.equal(await note.inputValue(),'首第一行🙂\n最后一行🌿末');
   await page.screenshot({path:path.join(output,mobile?'touch-edit.png':'desktop-edit.png')});
   await note.fill(originalNote);
   await note.press('Escape');passed.push(`${mobile?'touch':'desktop'} multiline Unicode draft returns at end; manual caret movement remains usable`);
   await context.close();
  }
  assert.equal(writes,0);assert.deepEqual(errors,[]);
 }catch(error){errors.push(error.stack);}
 await fs.writeFile(path.join(output,'browser-results.json'),JSON.stringify({passed,errors,businessWrites:writes},null,2));
 await browser.close();if(errors.length){process.stderr.write(errors.join('\n'));process.exitCode=1;}else process.stdout.write(`Passed ${passed.length} caret groups; zero business writes\n`);
})();
