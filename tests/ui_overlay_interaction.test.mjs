// Production ui.js, deterministic DOM/event fixture. Browser appearance is checked separately.
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';

import { Element, document, flush, key, observers } from './support/frontend_dom_fixture.mjs';

const workspace = process.argv[2] || process.cwd();
const source = await readFile(join(workspace, 'admin/js/ui.js'), 'utf8');
const version = source.match(/ASSET_VERSION = '([^']+)'/)[1];
const { modal, confirm, registerFloatingSurface } = await import(pathToFileURL(join(workspace, 'admin/js/ui.js')).href + `?v=${version}`);

const page = document.body.appendChild(new Element('main'));
const trigger = page.appendChild(new Element('button'));
const alreadyInert = document.body.appendChild(new Element());
alreadyInert.setAttribute('inert', 'inert');
document.body.style.overflow = 'auto';
trigger.focus();
const first = modal({ title: '编辑', body: '<input id="a"><input id="b"><input id="c" disabled><input type="hidden"><button hidden>隐藏</button>', footer: '<button data-cancel>取消</button><button data-ok>保存</button>' });
await flush();
assert.equal(document.activeElement, first.root.querySelector('#a'), 'entry focus moves to first visible field');
assert.equal(first.root.getAttribute('aria-modal'), 'true');
assert.equal(page.getAttribute('inert'), '');
assert.equal(document.body.style.overflow, 'hidden');
key('Tab'); assert.equal(document.activeElement, first.root.querySelector('#b'));
key('Tab', true); assert.equal(document.activeElement, first.root.querySelector('#a'), 'Shift+Tab from middle field moves to preceding field');
const localTab = event => {
  if (event.key === 'Tab') { event.preventDefault(); first.root.querySelector('#a').focus(); }
};
first.root.querySelector('#b').addEventListener('keydown', localTab);
first.root.querySelector('#b').focus(); key('Tab');
assert.equal(document.activeElement, first.root.querySelector('#a'), 'child keyboard handler can own its Tab transition');
first.root.querySelector('#b').removeEventListener('keydown', localTab);
first.root.querySelector('.modal-close').focus();
key('Tab', true); assert.equal(document.activeElement, first.root.querySelector('[data-ok]'), 'backward wrap remains inside dialog');
key('Tab'); assert.equal(document.activeElement, first.root.querySelector('.modal-close'));
let backgroundClicks = 0;
trigger.onclick = () => { backgroundClicks++; };
trigger.click(); assert.equal(backgroundClicks, 0, 'capture guard rejects synthetic background activation');
trigger.focus(); assert.notEqual(document.activeElement, trigger, 'inert background cannot receive focus');
const intruder = document.body.appendChild(new Element('button'));
await flush(); assert.equal(intruder.getAttribute('inert'), '', 'new background nodes are inert');

const anchor = first.root.querySelector('#b');
anchor.focus();
const calendar = document.body.appendChild(new Element());
const calendarButton = calendar.appendChild(new Element('button'));
let calendarClosed = 0;
const unregisterCalendar = registerFloatingSurface({ element: calendar, anchor, onEscape: () => {
  calendarClosed++; unregisterCalendar(); calendar.remove(); if (anchor.isConnected) anchor.focus();
} });
calendarButton.focus();
assert.equal(calendar.getAttribute('inert'), null, 'body portal belongs to modal focus scope');
key('Tab'); assert.ok(first.root.contains(document.activeElement), 'Tab from body portal wraps within active dialog');
const unitList = calendar.appendChild(new Element());
const option = unitList.appendChild(new Element('button'));
let unitClosed = 0;
const unregisterUnit = registerFloatingSurface({ element: unitList, anchor: calendarButton, onEscape: () => {
  unitClosed++; unregisterUnit(); unitList.remove(); calendarButton.focus();
} });
option.focus(); key('Escape');
assert.equal(unitClosed, 1); assert.equal(calendarClosed, 0); assert.ok(first.mask.isConnected);
assert.equal(document.activeElement, calendarButton);

const nested = confirm('删除?', { danger: true });
await flush();
const nestedMask = document.body.children.findLast(node => node.className === 'modal-mask');
assert.equal(first.root.getAttribute('aria-modal'), 'false');
assert.equal(calendar.getAttribute('inert'), '');
assert.ok(Number(nestedMask.style.zIndex) > Number(calendar.style.zIndex), 'child confirm covers parent body portals');
key('Escape'); assert.equal(await nested, false);
assert.equal(calendarClosed, 0, 'nested Escape cancels only confirmation');
assert.ok(first.mask.isConnected); assert.equal(calendar.getAttribute('inert'), null);
assert.equal(document.activeElement, calendarButton, 'nested close restores parent child-surface focus');
key('Escape'); assert.equal(calendarClosed, 1); assert.ok(first.mask.isConnected);
assert.equal(document.activeElement, anchor);

// Async caller override is authoritative for Escape and mask; close remains an actual removal.
let saveRequests = 0;
let completeSave;
first.root.querySelector('.modal-close').onclick = () => {
  saveRequests++;
  return new Promise(resolve => { completeSave = () => { first.close(); resolve(); }; });
};
key('Escape'); assert.equal(saveRequests, 1); assert.ok(first.mask.isConnected, 'Escape waits for caller save hook');
completeSave(); await flush();
assert.equal(document.activeElement, trigger);
assert.equal(page.getAttribute('inert'), null);
assert.equal(alreadyInert.getAttribute('inert'), 'inert', 'prior inert state survives close');
assert.equal(document.body.style.overflow, 'auto');
assert.equal(observers.size, 0, 'manager releases observer when last overlay closes');
assert.equal([...document.listeners.values()].flat().length, 0, 'manager releases global listeners');

for (const path of ['cancel', 'x', 'mask', 'escape', 'removed', 'root-removed', 'ok']) {
  trigger.focus();
  const decision = confirm('确认?', {}); await flush();
  const mask = document.body.children.findLast(node => node.className === 'modal-mask');
  if (path === 'cancel') mask.querySelector('[data-cancel]').click();
  else if (path === 'x') mask.querySelector('.modal-close').click();
  else if (path === 'mask') mask.click();
  else if (path === 'escape') key('Escape');
  else if (path === 'removed') mask.remove();
  else if (path === 'root-removed') mask.querySelector('.modal').remove();
  else mask.querySelector('[data-ok]').click();
  await flush(); assert.equal(await decision, path === 'ok', `confirm ${path} always settles`);
  assert.equal(document.activeElement, trigger); assert.equal(observers.size, 0);
}

let maskHooks = 0;
const guarded = modal({ title: '保存', body: '', onMaskClose: () => { maskHooks++; } });
guarded.mask.click(); key('Escape'); assert.equal(maskHooks, 2, 'default ×, Escape and mask share custom close hook');
assert.ok(guarded.mask.isConnected); guarded.close(); await flush();

const failedSave = modal({ title: '编辑器', body: '<textarea>未保存</textarea>' });
await flush();
let closeDecision;
failedSave.root.querySelector('.modal-close').onclick = () => {
  closeDecision = (async () => {
    if (await confirm('保存未成功。关闭后修改将丢失，确定要关闭吗？', { cancelText: '返回编辑' })) failedSave.close();
  })();
};
key('Escape'); await flush(); key('Escape'); await closeDecision;
assert.ok(failedSave.mask.isConnected, 'canceling nested failed-save decision returns to unchanged editor');
key('Escape'); await flush();
document.body.children.findLast(node => node.className === 'modal-mask').querySelector('[data-ok]').click();
await closeDecision; await flush(); assert.equal(failedSave.mask.isConnected, false, 'confirming nested close executes caller cleanup');

const detached = modal({ title: '外层', body: '<button data-anchor>时间</button>' });
await flush();
const detachedAnchor = detached.root.querySelector('[data-anchor]');
const orphan = document.body.appendChild(new Element());
let orphanCleanup = 0;
const unregisterOrphan = registerFloatingSurface({ element: orphan, anchor: detachedAnchor, onEscape: options => {
  assert.equal(options.restoreFocus, false); orphanCleanup++; unregisterOrphan(); orphan.remove();
} });
detachedAnchor.remove(); await flush(); assert.equal(orphanCleanup, 1); assert.equal(orphan.isConnected, false);
unregisterOrphan(); assert.equal(orphanCleanup, 1, 'unregister is idempotent');
detached.mask.remove(); await flush(); assert.equal(observers.size, 0);

// A passive described tooltip is accessible while a modal is active; it has Escape priority.
const tooltipModal = modal({ title: '提示', body: '<button data-anchor>名称</button>' });
await flush();
const tipAnchor = tooltipModal.root.querySelector('[data-anchor]');
const description = document.body.appendChild(new Element()); description.setAttribute('role', 'tooltip');
let hideTooltip = 0;
const unregisterTip = registerFloatingSurface({ element: description, anchor: tipAnchor, onEscape: () => {
  hideTooltip++; unregisterTip(); description.remove();
} });
await flush(); assert.equal(description.getAttribute('inert'), null);
key('Escape'); assert.equal(hideTooltip, 1); assert.ok(tooltipModal.mask.isConnected);
key('Escape'); await flush(); assert.equal(tooltipModal.mask.isConnected, false);
assert.equal(observers.size, 0);
process.stdout.write('Shared modal/surface keyboard, async-close, cancellation and cleanup regressions passed.\n');
