// Deterministic test-only DOM, HTML parser, capture/bubble events and observers.
const observers = new Set();
let mutationPending = false;
const mutation = (target, type = 'childList', attributeName = null) => {
  for (const observer of observers) {
    const observed = [...observer.targets].some(([root, options]) => {
      if (root !== target && !(options.subtree && root.contains(target))) return false;
      if (type === 'childList') return options.childList;
      return options.attributes && (!options.attributeFilter || options.attributeFilter.includes(attributeName));
    });
    if (observed) observer.records.push({ target, type, attributeName });
  }
  if (mutationPending || ![...observers].some(observer => observer.records.length)) return;
  mutationPending = true;
  queueMicrotask(() => {
    mutationPending = false;
    for (const observer of [...observers]) {
      const records = observer.records.splice(0);
      if (observer.active && records.length) observer.callback(records, observer);
    }
  });
};
class Observer {
  constructor(callback) { this.callback = callback; this.active = false; this.targets = new Map(); this.records = []; }
  observe(target, options) { this.active = true; this.targets.set(target, options); observers.add(this); }
  disconnect() { this.active = false; this.targets.clear(); this.records = []; observers.delete(this); }
}
class UiEvent {
  constructor(type, options = {}) { Object.assign(this, options); this.type = type; this.defaultPrevented = false; }
  preventDefault() { this.defaultPrevented = true; }
  stopPropagation() { this.stopped = true; }
  stopImmediatePropagation() { this.stopped = true; this.immediate = true; }
}
class Element {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentElement = null;
    this.attributes = new Map(); this.listeners = new Map(); this.style = {}; this.onclick = null;
    this._text = '';
    this._scrollTop = 0;
  }
  setAttribute(name, value) { this.attributes.set(name, String(value)); mutation(this, 'attributes', name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  removeAttribute(name) { if (this.attributes.delete(name)) mutation(this, 'attributes', name); }
  get className() { return this.getAttribute('class') || ''; }
  set className(value) { this.setAttribute('class', value); }
  get hidden() { return this.attributes.has('hidden'); }
  set hidden(value) { value ? this.setAttribute('hidden', '') : this.removeAttribute('hidden'); }
  get disabled() { return this.attributes.has('disabled'); }
  set disabled(value) { value ? this.setAttribute('disabled', '') : this.removeAttribute('disabled'); }
  get readOnly() { return this.attributes.has('readonly'); }
  set readOnly(value) { value ? this.setAttribute('readonly', '') : this.removeAttribute('readonly'); }
  get id() { return this.getAttribute('id') || ''; }
  set id(value) { this.setAttribute('id', value); }
  get value() { return this._value ?? this.getAttribute('value') ?? ''; }
  set value(value) { this._value = String(value); }
  get dataset() {
    const attribute = key => 'data-' + String(key).replace(/[A-Z]/g, char => '-' + char.toLowerCase());
    return new Proxy({}, {
      get: (_, key) => this.getAttribute(attribute(key)) ?? undefined,
      set: (_, key, value) => { this.setAttribute(attribute(key), value); return true; },
    });
  }
  get childNodes() { return this.children; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children.at(-1) || null; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set textContent(value) {
    for (const child of this.children) child.parentElement = null;
    this.children = []; this._text = String(value); mutation(this);
  }
  get tabIndex() {
    const explicit = this.getAttribute('tabindex');
    if (explicit !== null) return Number(explicit);
    if (this.tagName === 'INPUT' && this.getAttribute('type') === 'hidden') return -1;
    return ['BUTTON', 'INPUT', 'SELECT', 'TEXTAREA'].includes(this.tagName) || this.getAttribute('href') !== null ? 0 : -1;
  }
  set tabIndex(value) { this.setAttribute('tabindex', value); }
  get classList() {
    const names = () => new Set(this.className.split(/\s+/).filter(Boolean));
    return {
      contains: name => names().has(name),
      add: (...values) => { this.className = [...new Set([...names(), ...values])].join(' '); },
      remove: (...values) => { this.className = [...names()].filter(name => !values.includes(name)).join(' '); },
      toggle: (name, force) => {
        const next = force ?? !names().has(name);
        next ? this.classList.add(name) : this.classList.remove(name);
        return next;
      },
    };
  }
  get isConnected() { return this === document.body || !!this.parentElement?.isConnected; }
  appendChild(child) { child.parentElement = this; this.children.push(child); mutation(this); return child; }
  remove() {
    const parent = this.parentElement;
    if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(node => node !== this);
    this.parentElement = null; if (parent) mutation(parent);
  }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  matches(selector) {
    return selector.split(',').some(part => {
      const s = part.trim();
      if (s === ':disabled') return this.disabled || !!this.closest('fieldset[disabled]');
      if (s.endsWith(':not(:disabled)')) return this.matches(s.replace(/:not\(:disabled\)$/, '')) && !this.matches(':disabled');
      const tag = s.match(/^[\w-]+/)?.[0];
      if (tag && this.tagName !== tag.toUpperCase()) return false;
      const id = s.match(/#([\w-]+)/)?.[1];
      if (id && this.getAttribute('id') !== id) return false;
      for (const match of s.matchAll(/\.([\w-]+)/g)) if (!this.classList.contains(match[1])) return false;
      for (const match of s.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)) {
        if (!this.attributes.has(match[1]) || (match[2] !== undefined && this.getAttribute(match[1]) !== match[2])) return false;
      }
      return !!tag || s.startsWith('.') || s.startsWith('#') || s.startsWith('[');
    });
  }
  closest(selector) { return this.matches(selector) ? this : this.parentElement?.closest(selector) || null; }
  querySelectorAll(selector) {
    const found = [];
    const visit = node => { for (const child of node.children) { if (child.matches(selector)) found.push(child); visit(child); } };
    visit(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  set innerHTML(value) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this._text = '';
    const stack = [this];
    for (const token of String(value).match(/<[^>]+>|[^<]+/g) || []) {
      if (token.startsWith('</')) { if (stack.length > 1) stack.pop(); continue; }
      if (!token.startsWith('<')) { stack.at(-1)._text += token; continue; }
      const tag = token.match(/^<([\w-]+)/)?.[1];
      if (!tag) continue;
      const element = new Element(tag);
      for (const match of token.slice(tag.length + 1, -1).matchAll(/([\w-]+)(?:="([^"]*)"|='([^']*)'|=([^\s>]+))?/g)) {
        element.setAttribute(match[1], match[2] ?? match[3] ?? match[4] ?? '');
      }
      stack.at(-1).appendChild(element);
      if (!['INPUT', 'BR', 'HR', 'IMG', 'PATH'].includes(element.tagName) && !token.endsWith('/>')) stack.push(element);
    }
    mutation(this);
  }
  getClientRects() {
    return this.isConnected && !this.closest('[hidden]') && this.style.display !== 'none'
      && !this.parentElement?.closest('[data-display-none]') ? [{}] : [];
  }
  getBoundingClientRect() {
    return { left: 20, top: 20, right: 220, bottom: 60, width: 200, height: 40 };
  }
  get clientTop() { return 0; }
  get clientHeight() { return this.getBoundingClientRect().height; }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(value) {
    const next = Math.max(0, Number(value) || 0);
    if (next === this._scrollTop) return;
    this._scrollTop = next;
    queueMicrotask(() => dispatch(this, new UiEvent('scroll')));
  }
  scrollIntoView() {}
  dispatchEvent(event) { dispatch(this, event); return !event.defaultPrevented; }
  focus() {
    if (!this.isConnected || this.closest('[inert]') || this.matches(':disabled')) return;
    document.activeElement = this; dispatch(this, new UiEvent('focusin'));
  }
  click() { if (!this.disabled) dispatch(this, new UiEvent('click')); }
  addEventListener(type, fn, capture = false) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({ fn, capture: capture === true });
  }
  removeEventListener(type, fn) { this.listeners.set(type, (this.listeners.get(type) || []).filter(item => item.fn !== fn)); }
}
const document = {
  body: new Element('body'), activeElement: null, listeners: new Map(),
  createElement: tag => new Element(tag),
  querySelector: selector => document.body.querySelector(selector),
  querySelectorAll: selector => document.body.querySelectorAll(selector),
  contains: node => document.body.contains(node),
  addEventListener: Element.prototype.addEventListener,
  removeEventListener: Element.prototype.removeEventListener,
};
function dispatch(target, event) {
  event.target = target;
  const path = [];
  for (let node = target; node; node = node.parentElement) path.push(node);
  if (target !== window && !path.includes(document)) path.push(document);
  if (!path.includes(window)) path.push(window);
  const invoke = (node, capture) => {
    event.currentTarget = node;
    for (const item of [...(node.listeners.get(event.type) || [])]) {
      if (item.capture !== capture) continue;
      item.fn(event); if (event.immediate) break;
    }
  };
  for (const node of [...path].reverse()) { invoke(node, true); if (event.stopped) return event; }
  for (const node of path) {
    if (event.type === 'click' && node === target) node.onclick?.(event);
    invoke(node, false); if (event.stopped) break;
  }
  return event;
}
globalThis.document = document;
globalThis.MutationObserver = Observer;
globalThis.getComputedStyle = element => ({ visibility: element.style.visibility || 'visible' });
globalThis.Event = UiEvent;
globalThis.Node = { ELEMENT_NODE: 1 };
globalThis.window = {
  innerWidth: 1200, innerHeight: 800, listeners: new Map(),
  addEventListener: Element.prototype.addEventListener,
  removeEventListener: Element.prototype.removeEventListener,
};
globalThis.innerWidth = window.innerWidth;
globalThis.innerHeight = window.innerHeight;
const flush = async () => { for (let n = 0; n < 10; n++) await Promise.resolve(); };
const key = (value, shiftKey = false) => dispatch(document.activeElement || document.body, new UiEvent('keydown', { key: value, shiftKey }));

export { Element, UiEvent, document, dispatch, flush, key, observers };
