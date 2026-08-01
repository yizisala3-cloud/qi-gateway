// ui.js - shared UI components

export function loading() {
  return '<div class="loading-block"><span class="spinner"></span> Loading...</div>';
}

export function empty(msg = 'No data') {
  return `<div class="empty"><div class="icon">&#x1F4ED;</div><div class="msg">${msg}</div></div>`;
}

export function errorBlock(msg) {
  return `<div class="banner banner-danger"><span>&#x26A0;&#xFE0F;</span><div>${msg}</div></div>`;
}

export function badge(text, type = '') {
  return `<span class="badge ${type ? 'badge-' + type : ''}">${text}</span>`;
}

export function heatDot(heat) {
  const v = parseFloat(heat) || 0;
  const cls = v >= 60 ? 'dot-hot' : v >= 30 ? 'dot-warm' : 'dot-cold';
  return `<span class="badge badge-muted"><span class="dot ${cls}"></span>${v.toFixed(1)}</span>`;
}

export function stat(label, value, type = '') {
  return `<div class="stat"><div class="label">${label}</div><div class="value ${type}">${value}</div></div>`;
}

// Toast
let _toastWrap = null;
export function toast(msg, type = 'ok') {
  if (!_toastWrap) {
    _toastWrap = document.createElement('div');
    _toastWrap.className = 'toast-wrap';
    document.body.appendChild(_toastWrap);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.textContent = msg;
  _toastWrap.appendChild(el);
  setTimeout(() => el.remove(), 3000);
}

// Simple modal
export function modal({ title, body, footer }) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal">
      <div class="modal-head"><h3>${title}</h3><button class="modal-close">&times;</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>`;
  document.body.appendChild(mask);
  const close = () => mask.remove();
  mask.querySelector('.modal-close').onclick = close;
  mask.addEventListener('click', e => { if (e.target === mask) close(); });
  return { root: mask.querySelector('.modal'), close };
}

// Confirm dialog
export function confirm(msg) {
  return new Promise(resolve => {
    const { root, close } = modal({
      title: 'Confirm',
      body: `<p>${msg}</p>`,
      footer: `<button class="btn btn-secondary" data-cancel>Cancel</button><button class="btn btn-danger" data-ok>Confirm</button>`
    });
    root.querySelector('[data-cancel]').onclick = () => { close(); resolve(false); };
    root.querySelector('[data-ok]').onclick = () => { close(); resolve(true); };
  });
}

// Delegate events
export function delegate(root, actions) {
  root.addEventListener('click', e => {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act;
    if (actions[act]) actions[act](el, e);
  });
}

