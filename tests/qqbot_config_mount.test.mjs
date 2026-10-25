// Real config.js and its imports; only fetch and the DOM are test substitutes.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { document, flush } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2], group = process.argv[3];
const version = (await readFile(join(workspace, 'admin/js/ui.js'), 'utf8')).match(/ASSET_VERSION = '([^']+)'/)[1];
globalThis.document = document;
globalThis.window = { location: { origin: 'http://qqbot.test' }, listeners: new Map() };
globalThis.localStorage = { getItem: key => key === 'qi-token' ? 'test-only' : '' };
globalThis.MutationObserver = class { observe() {} disconnect() {} };
const timeout = globalThis.setTimeout;
globalThis.setTimeout = (...args) => { const timer = timeout(...args); timer.unref(); return timer; };
const page = (await import(pathToFileURL(join(workspace, 'admin/js/pages/config.js')).href + '?v=' + version)).default;
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a;reject=b; }); return { promise,resolve,reject }; };
const snapshot = flags => ({ daily_limit:3, timezone:'Asia/Shanghai',active_start:'08:00',active_end:'24:00',
  ...flags, configured:true, proactive_configured:true, running:flags.enabled,
  writable:true, connected:false, error:'' });
const response = body => ({ ok:true, json:async () => body });
const opposite = key => ({ enabled:key === 'proactive_enabled', proactive_enabled:false });
const button = (root,key) => root.querySelector(`[data-act="${key === 'enabled' ? 'toggle-qqbot' : 'toggle-qqbot-proactive'}"]`);

function setup(flags, { delayed='qq', sameRoot=false } = {}) {
  page.unmount();document.body.innerHTML='';
  const oldRead=deferred(), put=deferred(), writes=[];
  let visit=0, holdPut=false, server={...flags};
  globalThis.fetch = async (url, options={}) => {
    const path=new URL(url).pathname;
    if (path==='/health') visit++;
    if (path==='/admin/api/qqbot/proactive/resume') {
      const payload=JSON.parse(options.body);writes.push(payload);
      if (holdPut) await put.promise;
      server={...server,proactive_evaluation:{...server.proactive_evaluation,fault_stopped:false,failure_streak:0,
        fault_version:server.proactive_evaluation.fault_version+1}};
      return response(snapshot(server));
    }
    if (path==='/admin/api/qqbot/settings') {
      if (options.method==='PUT') {
        const payload=JSON.parse(options.body);writes.push(payload);
        if (holdPut) await put.promise;
        server={...server,...payload};if (!server.enabled) server.proactive_enabled=false;
        return response(snapshot(server));
      }
      if (visit===1 && delayed==='qq') return oldRead.promise.then(response);
      return response(snapshot(server));
    }
    if (path==='/health') {
      if (visit===1 && delayed==='health') return oldRead.promise.then(response);
      return response({status:'ok',uptime:0});
    }
    if (path==='/status') return response({upstream_model:'model-'+visit});
    if (path==='/admin/api/context/settings') return response({recent_chat_limit:visit});
    if (path==='/admin/api/planning/cycle') return response({refresh_boundary_time:visit===1?'06:00':'08:00'});
    if (path==='/admin/api/logs/settings') return response({enabled:visit>1,writable:true});
    return response({});
  };
  const oldRoot=document.createElement('div');document.body.appendChild(oldRoot);
  const currentRoot=sameRoot?oldRoot:document.createElement('div');
  if (!sameRoot) document.body.appendChild(currentRoot);
  return {oldRead,oldRoot,currentRoot,writes,
    hold:() => {holdPut=true;}, release:() => put.resolve(), failPut:() => put.reject(new Error('old save failed')),
    state:() => server};
}

function currentUnchanged(f, key, html) {
  assert.equal(page.qqSettings[key],true,'stale mount must not change action state');
  assert.equal(f.currentRoot.innerHTML,html,'stale mount must not redraw the current page');
  assert.equal(page.recentChatLimit,2,'all initial shared state belongs to the current mount');
  assert.equal(page.logCollectionEnabled,true);
  assert.equal(page.cycle.refresh_boundary_time,'08:00');
}

if (group==='stale-reads') {
  for (const key of ['enabled','proactive_enabled']) for (const timing of ['before','during','after']) for (const sameRoot of [false,true]) {
    test(`${key}: stale read ${timing} save; reused root=${sameRoot}`,async () => {
      const initial=timing==='after'?opposite(key):{enabled:true,proactive_enabled:key==='proactive_enabled'};
      const f=setup(initial,{sameRoot});const oldMount=page.mount(f.oldRoot);page.unmount();
      await page.mount(f.currentRoot);
      if (timing==='after') await page.toggleQQSetting(key);
      assert.equal(button(f.currentRoot,key).getAttribute('aria-checked'),'true');
      let saving;
      if (timing==='during') {f.hold();saving=page.toggleQQSetting(key);}
      const html=f.currentRoot.innerHTML;
      f.oldRead.resolve(snapshot(opposite(key)));await oldMount;
      currentUnchanged(f,key,html);
      if (timing==='during') {assert.equal(page.savingQQSettings,true);f.release();await saving;}
      else await page.toggleQQSetting(key);
      assert.equal(f.state()[key],false);
      assert.deepEqual(f.writes.at(-1),{[key]:false},'the visible ON control must submit OFF');
      assert.equal(button(f.currentRoot,key).getAttribute('aria-checked'),'false');
      page.unmount();
    });
  }
}

if (group==='failures') {
  for (const delayed of ['qq','health']) for (const sameRoot of [false,true]) {
    test(`late ${delayed} failure; reused root=${sameRoot}`,async () => {
      const f=setup({enabled:true,proactive_enabled:true},{delayed,sameRoot});
      const oldMount=page.mount(f.oldRoot);page.unmount();await page.mount(f.currentRoot);
      const html=f.currentRoot.innerHTML;f.oldRead.reject(new Error('obsolete request failed'));await oldMount;
      currentUnchanged(f,'enabled',html);
      assert.equal(page.qqSettings.proactive_enabled,true);page.unmount();
    });
  }
}

if (group==='discarded') {
  for (const failure of [false,true]) test(`unmount without replacement, failure=${failure}`,async () => {
    const f=setup({enabled:false,proactive_enabled:false},{delayed:failure?'health':'qq'});
    const pending=page.mount(f.oldRoot);page.unmount();const html=f.oldRoot.innerHTML;
    if (failure) f.oldRead.reject(new Error('obsolete health failure'));else f.oldRead.resolve(snapshot({enabled:true,proactive_enabled:true}));
    await pending;
    assert.equal(page.qqSettings,null);assert.equal(f.oldRoot.innerHTML,html);
    assert.equal(f.oldRoot.querySelector('.retro-time-field'),null,'discarded mount must not initialize controls');
  });
  test('replacement mount without unmount still owns the state',async () => {
    const f=setup({enabled:true,proactive_enabled:true});const old=page.mount(f.oldRoot);
    await page.mount(f.currentRoot);const html=f.currentRoot.innerHTML;
    f.oldRead.resolve(snapshot({enabled:false,proactive_enabled:false}));await old;
    currentUnchanged(f,'enabled',html);assert.equal(f.oldRoot.querySelector('.retro-time-field'),null);page.unmount();
  });
  test('root ownership also invalidates an abandoned load',async () => {
    const f=setup({enabled:true,proactive_enabled:false});const pending=page.mount(f.oldRoot);
    const html=f.oldRoot.innerHTML;page.root=f.currentRoot;
    f.oldRead.resolve(snapshot({enabled:true,proactive_enabled:false}));await pending;
    assert.equal(page.qqSettings,null);assert.equal(f.oldRoot.innerHTML,html);page.unmount();
  });
}

if (group==='saves') {
  for (const failure of [false,true]) test(`old save response after remount, failure=${failure}`,async () => {
    const f=setup({enabled:true,proactive_enabled:true},{delayed:'none'});await page.mount(f.oldRoot);
    f.hold();const saving=page.toggleQQSetting('proactive_enabled');
    page.unmount();await page.mount(f.currentRoot);const html=f.currentRoot.innerHTML;
    if (failure) f.failPut();else f.release();
    await saving;
    currentUnchanged(f,'enabled',html);assert.equal(page.qqSettings.proactive_enabled,true);
    assert.equal(page.savingQQSettings,false);page.unmount();
  });
}

if (group==='normal') {
  test('current fatal read error is shown and a later mount recovers',async () => {
    const f=setup({enabled:true,proactive_enabled:true},{delayed:'health'});
    const pending=page.mount(f.oldRoot);f.oldRead.reject(new Error('current health failure'));await pending;
    assert.match(f.oldRoot.textContent,/配置状态读取失败.*current health failure/);
    page.unmount();await page.mount(f.currentRoot);
    assert.equal(page.qqSettings.enabled,true);assert.ok(f.currentRoot.querySelector('.retro-time-field'));page.unmount();
  });
  test('current optional read error renders unknown without blocking other settings',async () => {
    const f=setup({enabled:false,proactive_enabled:false});const pending=page.mount(f.oldRoot);
    f.oldRead.reject(new Error('current qq failure'));await pending;
    assert.equal(page.qqSettings,null);assert.ok(button(f.oldRoot,'enabled').disabled);
    assert.match(f.oldRoot.querySelector('#qqbot-settings-panel').textContent,/状态未知/);
    assert.ok(f.oldRoot.querySelector('.retro-time-field'));page.unmount();
  });
  test('delegated clicks still switch on and off with the current snapshot',async () => {
    const f=setup({enabled:false,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    for (const key of ['enabled','proactive_enabled','proactive_enabled','enabled']) {
      button(f.oldRoot,key).click();await flush();
    }
    assert.deepEqual(f.writes,[{enabled:true},{proactive_enabled:true},{proactive_enabled:false},{enabled:false}]);
    assert.deepEqual(f.state(),{enabled:false,proactive_enabled:false});page.unmount();
  });
}

if (group==='daily-limit') {
  test('limit saves independently while QQ is disabled and survives remount',async () => {
    const f=setup({enabled:false,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    const field=f.oldRoot.querySelector('#qqbot-daily-limit-input');
    assert.equal(field.disabled,false);assert.equal(field.value,'3');field.value='30';
    f.oldRoot.querySelector('[data-act="save-qqbot-daily-limit"]').click();await flush();
    assert.deepEqual(f.writes,[{daily_limit:30}]);assert.equal(f.state().enabled,false);
    assert.equal(page.qqSettings.daily_limit,30);
    page.unmount();await page.mount(f.currentRoot);
    assert.equal(f.currentRoot.querySelector('#qqbot-daily-limit-input').value,'30');page.unmount();
  });
  for (const value of ['', '0','101','2.5']) test(`invalid limit does not submit: ${value}`,async () => {
    const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    f.oldRoot.querySelector('#qqbot-daily-limit-input').value=value;
    await page.saveQQDailyLimit();assert.deepEqual(f.writes,[]);page.unmount();
  });
  test('saving locks all QQ actions and sends the captured value once',async () => {
    const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    f.oldRoot.querySelector('#qqbot-daily-limit-input').value='30';f.hold();
    const pending=page.saveQQDailyLimit();
    assert.equal(f.oldRoot.querySelector('#qqbot-daily-limit-input').disabled,true);
    assert.equal(button(f.oldRoot,'enabled').disabled,true);
    await page.saveQQDailyLimit();assert.deepEqual(f.writes,[{daily_limit:30}]);
    f.release();await pending;assert.equal(page.qqSettings.daily_limit,30);page.unmount();
  });
  for (const failure of [false,true]) test(`late limit response cannot redraw another mount, failure=${failure}`,async () => {
    const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    f.oldRoot.querySelector('#qqbot-daily-limit-input').value='30';f.hold();const pending=page.saveQQDailyLimit();
    page.unmount();await page.mount(f.currentRoot);const html=f.currentRoot.innerHTML;
    if(failure)f.failPut();else f.release();await pending;
    assert.equal(page.qqSettings.daily_limit,3);assert.equal(f.currentRoot.innerHTML,html);
    assert.equal(page.savingQQSettings,false);page.unmount();
  });
}

if (group==='active-window') {
  test('window saves while disabled and survives remount without changing switches',async () => {
    const f=setup({enabled:false,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    assert.equal(f.oldRoot.querySelector('#qqbot-active-start-input').value,'08:00');
    assert.equal(f.oldRoot.querySelector('#qqbot-active-end-input').value,'24:00');
    f.oldRoot.querySelector('#qqbot-active-start-input').value='22:30';
    f.oldRoot.querySelector('#qqbot-active-end-input').value='07:15';
    f.oldRoot.querySelector('[data-act="save-qqbot-active-window"]').click();await flush();
    assert.deepEqual(f.writes,[{active_start:'22:30',active_end:'07:15'}]);
    assert.equal(f.state().enabled,false);assert.equal(f.state().proactive_enabled,false);
    page.unmount();await page.mount(f.currentRoot);
    assert.equal(f.currentRoot.querySelector('#qqbot-active-start-input').value,'22:30');page.unmount();
  });
  for (const [start,end] of [['','24:00'],['24:00','24:00'],['8.00','24:00'],['08:00','24:01'],['08:00','25:00']]) {
    test(`invalid window never submits: ${start}/${end}`,async () => {
      const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
      f.oldRoot.querySelector('#qqbot-active-start-input').value=start;
      f.oldRoot.querySelector('#qqbot-active-end-input').value=end;
      await page.saveQQActiveWindow();assert.deepEqual(f.writes,[]);page.unmount();
    });
  }
  test('window save locks QQ controls and duplicate saves',async () => {
    const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);f.hold();
    const pending=page.saveQQActiveWindow();
    assert.equal(f.oldRoot.querySelector('#qqbot-active-start-input').disabled,true);
    assert.equal(f.oldRoot.querySelector('#qqbot-daily-limit-input').disabled,true);
    await page.saveQQActiveWindow();assert.equal(f.writes.length,1);f.release();await pending;page.unmount();
  });
  for (const failure of [false,true]) test(`late window response leaves current mount unchanged, failure=${failure}`,async () => {
    const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    f.oldRoot.querySelector('#qqbot-active-start-input').value='10:00';f.hold();const pending=page.saveQQActiveWindow();
    page.unmount();await page.mount(f.currentRoot);const html=f.currentRoot.innerHTML;
    if(failure)f.failPut();else f.release();await pending;
    assert.equal(page.qqSettings.active_start,'08:00');assert.equal(f.currentRoot.innerHTML,html);page.unmount();
  });
}

if (group==='evaluation-recovery') {
  const stopped=()=>({available:true,fault_stopped:true,failure_streak:5,silent_streak:0,fault_version:3,
    next_check_at:0,in_flight:false,last_error_message:'主动评估请求超时'});
  test('manual recovery posts only the displayed fault version and keeps proactive off',async()=>{
    const f=setup({enabled:true,proactive_enabled:false,proactive_evaluation:stopped()},{delayed:'none'});
    await page.mount(f.oldRoot);
    assert.equal(f.oldRoot.querySelector('#qqbot-evaluation-tag').textContent,'故障停用');
    f.oldRoot.querySelector('[data-act="resume-qqbot-evaluation"]').click();await flush();
    assert.deepEqual(f.writes,[{fault_version:3}]);assert.equal(f.state().proactive_enabled,false);
    assert.equal(page.qqSettings.proactive_evaluation.fault_stopped,false);
    assert.equal(f.oldRoot.querySelector('[data-act="resume-qqbot-evaluation"]').disabled,true);page.unmount();
  });
  for(const failure of [false,true])test(`late recovery response is isolated, failure=${failure}`,async()=>{
    const f=setup({enabled:true,proactive_enabled:false,proactive_evaluation:stopped()},{delayed:'none'});
    await page.mount(f.oldRoot);f.hold();const pending=page.resumeQQEvaluation();
    assert.equal(f.oldRoot.querySelector('[data-act="resume-qqbot-evaluation"]').disabled,true);
    await page.resumeQQEvaluation();assert.equal(f.writes.length,1);
    page.unmount();await page.mount(f.currentRoot);const html=f.currentRoot.innerHTML;
    if(failure)f.failPut();else f.release();await pending;
    assert.equal(f.currentRoot.innerHTML,html);assert.equal(page.qqSettings.proactive_evaluation.fault_stopped,true);
    page.unmount();
  });
  test('unknown evaluation state does not offer recovery',async()=>{
    const f=setup({enabled:true,proactive_enabled:false},{delayed:'none'});await page.mount(f.oldRoot);
    assert.equal(f.oldRoot.querySelector('[data-act="resume-qqbot-evaluation"]').disabled,true);
    await page.resumeQQEvaluation();assert.equal(f.writes.length,0);page.unmount();
  });
  for(const mode of ['recovering','live','stopped'])test(`evaluation state explains lease recovery, mode=${mode}`,async()=>{
    const e={...stopped(),fault_stopped:mode==='stopped',failure_streak:mode==='stopped'?5:0,
      in_flight:mode!=='stopped',recovery_pending:mode==='recovering'};
    const f=setup({enabled:true,proactive_enabled:true,proactive_evaluation:e},{delayed:'none'});
    await page.mount(f.oldRoot);
    assert.equal(f.oldRoot.querySelector('#qqbot-evaluation-tag').textContent,
      mode==='recovering'?'等待评估恢复':mode==='live'?'AI评估中':'故障停用');
    assert.equal(f.oldRoot.querySelector('#qqbot-next-evaluation').textContent,
      mode==='recovering'?'取得运行权后恢复':mode==='live'?'本轮结束后计算':'等待手动恢复');
    assert.equal(f.oldRoot.querySelector('[data-act="resume-qqbot-evaluation"]').disabled,mode!=='stopped');
    page.unmount();
  });
}
