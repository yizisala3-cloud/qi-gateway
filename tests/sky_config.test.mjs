// Execute the real configuration module, replacing only fetch and the test DOM.
import assert from 'node:assert/strict';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { document, flush } from './support/frontend_dom_fixture.mjs';

globalThis.document=document;
globalThis.window={location:{origin:'http://sky.test'},listeners:new Map()};
globalThis.localStorage={getItem:key=>key==='qi-token'?'test-only':''};
globalThis.MutationObserver=class {observe(){} disconnect(){}};
const timeout=globalThis.setTimeout;
globalThis.setTimeout=(...args)=>{const timer=timeout(...args);timer.unref();return timer;};
const page=(await import(pathToFileURL(join(process.argv[2],'admin/js/pages/config.js')).href)).default;
const response=(data,status=200)=>({ok:status<300,status,statusText:'test',json:async()=>data});
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve};};
const snapshot=(enabled=false,extra={})=>({enabled,available:true,writable:true,source:'test',...extra});

function setup(mode='normal') {
  const state={enabled:false,writes:[],gets:0,held:deferred(),read:deferred()};
  globalThis.fetch=async (url,opts={})=>{
    const path=new URL(url).pathname;
    if(path==='/admin/api/sky/settings') {
      if(opts.method==='PUT') {
        const body=JSON.parse(opts.body);state.writes.push(body);
        if(['save-error','reconcile-error'].includes(mode))return response({error:'save failed'},503);
        state.enabled=body.enabled;
        if(mode==='hold-save')return state.held.promise;
        if(mode==='lost-response')return {...response(null),json:async()=>{throw new SyntaxError('truncated');}};
        if(mode==='bad-response')return response({enabled:'yes'});
        return response(snapshot(state.enabled));
      }
      state.gets++;
      if(mode==='hold-read' && state.gets===1)return state.read.promise;
      if(mode==='read-error' || (mode==='reconcile-error' && state.gets>1))return response({error:'read failed'},503);
      return response(snapshot(state.enabled,{writable:mode!=='readonly',available:mode!=='unavailable'}));
    }
    if(path==='/health')return response({status:'ok',uptime:60});
    if(path==='/status')return response({upstream_model:'test-model'});
    if(path==='/admin/api/context/settings')return response({recent_chat_limit:10});
    if(path==='/admin/api/planning/cycle')return response({refresh_boundary_time:'06:00'});
    if(path==='/admin/api/logs/settings')return response({enabled:false,writable:true});
    return response({});
  };
  const root=document.createElement('div');document.body.appendChild(root);
  return {root,state};
}
const button=root=>root.querySelector('[data-act="toggle-sky"]');

test('real delegated click toggles on/off and re-entry reads saved value',async()=>{
  const {root,state}=setup();await page.mount(root);
  assert.equal(button(root).getAttribute('aria-checked'),'false');
  button(root).click();await flush();assert.equal(page.skySettings.enabled,true);
  button(root).click();await flush();assert.equal(page.skySettings.enabled,false);
  assert.deepEqual(state.writes,[{enabled:true},{enabled:false}]);
  page.unmount();await page.mount(root);assert.equal(page.skySettings.enabled,false);page.unmount();
});

test('pending save disables repeat writes',async()=>{
  const {root,state}=setup('hold-save');await page.mount(root);
  const pending=page.toggleSkySetting();await flush();
  assert.equal(button(root).disabled,true);await page.toggleSkySetting();assert.equal(state.writes.length,1);
  state.held.resolve(response(snapshot(true)));await pending;
  assert.equal(page.skySettings.enabled,true);assert.equal(button(root).disabled,false);page.unmount();
});

test('failed save reads back and keeps previous actual state',async()=>{
  const {root,state}=setup('save-error');await page.mount(root);await page.toggleSkySetting();
  assert.equal(page.skySettings.enabled,false);assert.equal(page.savingSkySettings,false);
  assert.equal(state.gets,2);assert.equal(button(root).disabled,false);page.unmount();
});

test('failed save and failed reconciliation show unknown instead of pretending off',async()=>{
  const {root,state}=setup('reconcile-error');await page.mount(root);await page.toggleSkySetting();
  assert.equal(state.writes.length,1);assert.equal(page.skySettings,null);
  assert.equal(button(root).disabled,true);assert.match(root.querySelector('#sky-settings-panel').textContent,/状态未知/);
  page.unmount();
});

for(const mode of ['lost-response','bad-response'])test(mode+' is reconciled without a blind second write',async()=>{
  const {root,state}=setup(mode);await page.mount(root);await page.toggleSkySetting();
  assert.equal(state.writes.length,1);assert.equal(page.skySettings.enabled,true);
  assert.equal(state.gets,2);assert.equal(button(root).getAttribute('aria-checked'),'true');page.unmount();
});

for(const mode of ['read-error','readonly','unavailable'])test(mode+' disables only unavailable Sky control',async()=>{
  const {root,state}=setup(mode);await page.mount(root);
  assert.equal(button(root).disabled,true);await page.toggleSkySetting();assert.equal(state.writes.length,0);
  assert.ok(root.querySelector('.retro-time-field'));page.unmount();
});

test('late save after re-entry cannot clobber current settings or root',async()=>{
  const {root,state}=setup('hold-save');await page.mount(root);
  const pending=page.toggleSkySetting();await flush();page.unmount();
  const current=document.createElement('div');document.body.appendChild(current);await page.mount(current);
  const html=current.innerHTML;assert.equal(page.skySettings.enabled,true);
  state.held.resolve(response(snapshot(false)));await pending;
  assert.equal(page.skySettings.enabled,true);assert.equal(current.innerHTML,html);page.unmount();
});

test('late failed save cannot trigger reconciliation against a new visit',async()=>{
  const {root,state}=setup('hold-save');await page.mount(root);
  const pending=page.toggleSkySetting();await flush();page.unmount();
  const current=document.createElement('div');document.body.appendChild(current);await page.mount(current);
  const gets=state.gets;const html=current.innerHTML;
  state.held.resolve(response({error:'old request failed'},503));await pending;
  assert.equal(state.gets,gets);assert.equal(page.skySettings.enabled,true);assert.equal(current.innerHTML,html);
  page.unmount();
});

test('late initial read cannot overwrite a later visit',async()=>{
  const {root,state}=setup('hold-read');const pending=page.mount(root);await flush();page.unmount();
  state.enabled=true;
  const current=document.createElement('div');document.body.appendChild(current);await page.mount(current);
  const html=current.innerHTML;
  state.read.resolve(response(snapshot(false)));await pending;
  assert.equal(page.skySettings.enabled,true);assert.equal(current.innerHTML,html);page.unmount();
});
