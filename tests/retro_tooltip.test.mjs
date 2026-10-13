import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Deterministic DOM/timer boundary; the real tooltip module supplies all behaviour.
class Events {
  listeners = new Map();
  addEventListener(name, fn) {
    if (!this.listeners.has(name)) this.listeners.set(name, new Set());
    this.listeners.get(name).add(fn);
  }
  removeEventListener(name, fn) { this.listeners.get(name)?.delete(fn); }
  emit(name, event = {}) { for (const fn of [...(this.listeners.get(name) || [])]) fn(event); }
}
class Element extends Events {
  constructor(tagName, doc) {
    super();
    this.tagName = tagName.toUpperCase();
    this.nodeType = 1;
    this.ownerDocument = doc;
    this.attributes = new Map();
    this.children = [];
    this.style = {};
    this.hidden = false;
    this.rect = { left: 350, top: 80, right: 380, bottom: 110, width: 30, height: 30 };
  }
  get id() { return this.getAttribute('id') || ''; }
  set id(value) { this.setAttribute('id', value); }
  get isConnected() { return this.ownerDocument.contains(this); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  removeAttribute(name) { this.attributes.delete(name); }
  hasAttribute(name) { return this.attributes.has(name); }
  appendChild(child) { child.parentElement = this; this.children.push(child); return child; }
  remove() {
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(child => child !== this);
    this.parentElement = null;
  }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  matches(selectors) {
    return selectors.split(',').some(selector => {
      selector = selector.trim();
      if (selector === ':disabled') return this.disabled === true;
      if (selector.startsWith('[')) {
        const [, name, value] = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/) || [];
        return value === undefined ? this.hasAttribute(name) : this.getAttribute(name) === value;
      }
      return this.tagName.toLowerCase() === selector;
    });
  }
  closest(selector) {
    for (let element = this; element; element = element.parentElement) if (element.matches(selector)) return element;
    return null;
  }
  querySelectorAll(selector) {
    return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
  }
  getClientRects() { return this.isConnected && !this.hidden ? [this.rect] : []; }
  getBoundingClientRect() {
    if (this.className === 'retro-tooltip') return { width: 160, height: 45 };
    return this.rect;
  }
}
const timers = new Map();
let timerId = 0;
const win = new Events();
Object.assign(win, { innerWidth: 390, innerHeight: 180,
  setTimeout(fn, delay) { timers.set(++timerId, { fn, delay }); return timerId; },
  clearTimeout(id) { timers.delete(id); }, visualViewport: null });
const doc = new Events();
Object.assign(doc, { nodeType: 9, defaultView: win,
  createElement(tag) { return new Element(tag, doc); },
  contains(node) { return doc.body.contains(node); },
  querySelectorAll(selector) { return doc.body.querySelectorAll(selector); } });
doc.body = doc.createElement('body');
globalThis.document = doc;
let observer;
globalThis.MutationObserver = class {
  constructor(callback) { this.callback = callback; observer = this; }
  observe() { this.connected = true; }
  disconnect() { this.connected = false; }
  emit(mutations) { if (this.connected) this.callback(mutations); }
};
const surfaces = new Set();
globalThis.__tooltipUI = {
  registerFloatingSurface(surface) { surfaces.add(surface); return () => surfaces.delete(surface); },
};
const source = (await readFile(new URL('../admin/js/lib/retro_tooltip.js', import.meta.url), 'utf8'))
  .replace(/import \{ registerFloatingSurface \} from '[^']+';/,
    'const { registerFloatingSurface } = globalThis.__tooltipUI;');
const { initRetroTooltips } = await import(`data:text/javascript,${encodeURIComponent(source)}`);
const runTimers = () => {
  const pending = [...timers.values()];
  timers.clear();
  for (const timer of pending) timer.fn();
};
const attrMutation = target => observer.emit([{ type: 'attributes', target }]);
const findTip = anchor => {
  const id = anchor.getAttribute('aria-describedby').split(' ').find(value => value.startsWith('retro-tooltip-'));
  return doc.body.children.find(element => element.id === id);
};

const anchor = doc.body.appendChild(doc.createElement('button'));
anchor.setAttribute('title', '辅助说明');
anchor.setAttribute('aria-label', '切换主题');
anchor.setAttribute('aria-describedby', 'existing-description');
const disconnect = initRetroTooltips();
assert.equal(initRetroTooltips(), disconnect, 'same root initializer is idempotent');
assert.equal(anchor.hasAttribute('title'), false, 'native title metadata is migrated');
assert.equal(anchor.getAttribute('aria-label'), '切换主题', 'auxiliary hint never replaces accessible name');
assert.equal(anchor.getAttribute('data-tooltip'), '辅助说明');
const tip = findTip(anchor);
assert.equal(tip.hidden, true);
assert.equal(tip.getAttribute('role'), 'tooltip');
assert.equal(tip.textContent, '辅助说明');
assert.ok(anchor.getAttribute('aria-describedby').startsWith('existing-description '), 'description exists before focus');

doc.emit('pointerover', { target: anchor, relatedTarget: null, pointerType: 'mouse' });
assert.equal(tip.hidden, true, 'hover waits briefly');
runTimers();
assert.equal(tip.hidden, false);
assert.equal(surfaces.size, 1);
assert.ok(Number.parseFloat(tip.style.left) >= 8);
assert.ok(Number.parseFloat(tip.style.left) + 160 <= 382, 'right-edge hint stays within narrow viewport');
assert.ok(Number.parseFloat(tip.style.top) + 45 <= 172);
doc.emit('pointerout', { target: anchor, relatedTarget: tip });
doc.emit('pointerover', { target: tip, relatedTarget: anchor, pointerType: 'mouse' });
runTimers();
assert.equal(tip.hidden, false, 'pointer can cross to the plain-text hint');
doc.emit('pointerout', { target: tip, relatedTarget: doc.body });
runTimers();
assert.equal(tip.hidden, true);
assert.equal(surfaces.size, 0, 'hidden hint releases Escape priority');

doc.emit('focusin', { target: anchor });
assert.equal(tip.hidden, false, 'keyboard focus opens immediately');
[...surfaces][0].onEscape({ reason: 'escape', restoreFocus: true });
assert.equal(tip.hidden, true);
assert.equal(surfaces.size, 0);
assert.equal(anchor.getAttribute('aria-label'), '切换主题');
anchor.setAttribute('data-tooltip', '更新说明');
attrMutation(anchor);
assert.equal(tip.textContent, '更新说明');
doc.emit('focusin', { target: anchor });
anchor.disabled = true;
attrMutation(anchor);
assert.equal(tip.hidden, true, 'disabled anchor closes active hint');

const dynamic = doc.body.appendChild(doc.createElement('button'));
dynamic.setAttribute('data-tooltip', '动态说明');
observer.emit([{ type: 'childList', addedNodes: [dynamic] }]);
const dynamicTip = findTip(dynamic);
doc.emit('focusin', { target: dynamic });
assert.equal(dynamicTip.hidden, false);
dynamic.remove();
observer.emit([{ type: 'childList', addedNodes: [] }]);
assert.equal(dynamicTip.isConnected, false, 'subtree removal removes descriptions and active surface');
assert.equal(surfaces.size, 0);
assert.equal(dynamic.hasAttribute('aria-describedby'), false);

anchor.disabled = false;
doc.emit('pointerover', { target: anchor, relatedTarget: null });
disconnect();
runTimers();
assert.equal(observer.connected, false);
assert.equal(tip.isConnected, false);
assert.equal(anchor.getAttribute('aria-describedby'), 'existing-description', 'cleanup preserves foreign description');
assert.equal(surfaces.size, 0);
assert.equal([...doc.listeners.values()].reduce((total, listeners) => total + listeners.size, 0), 0);
assert.equal([...win.listeners.values()].reduce((total, listeners) => total + listeners.size, 0), 0);
console.log('PASS tooltip: naming, hover/focus, viewport, Escape, dynamic cleanup');
