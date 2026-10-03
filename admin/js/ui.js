// ui.js - shared retro UI components: icons, tags, modal, toast, detail panel
export const ASSET_VERSION = '20261003-planning-create-latency1';

/* ---------- SVG icons (stroke, no emoji) ---------- */
const ICON_PATHS = {
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  scroll: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  heart: '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>',
  feather: '<path d="M20.24 12.24a6 6 0 0 0-8.49-8.49L5 10.5V19h8.5z"/><path d="M16 8 2 22"/><path d="M17.5 15H9"/>',
  gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
  journal: '<path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/>',
  filter: '<path d="M22 3H2l8 9.46V19l4 2v-8.54L22 3z"/>',
  sort: '<path d="M3 6h13"/><path d="M3 12h9"/><path d="M3 18h5"/><path d="M17 8v10"/><path d="m14 15 3 3 3-3"/>',
  refresh: '<path d="M23 4v6h-6"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>',
  plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
  edit: '<path d="M17 3a2.828 2.828 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  archive: '<rect x="1" y="3" width="22" height="5" rx="1"/><path d="M3 8v12a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1V8"/><path d="M10 12h4"/>',
  merge: '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>',
  copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>',
  alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  message: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  layers: '<path d="m12 2 10 5-10 5L2 7l10-5z"/><path d="m2 17 10 5 10-5"/><path d="m2 12 10 5 10-5"/>',
  'chevron-left': '<path d="m15 18-6-6 6-6"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  panel: '<rect x="3" y="3" width="18" height="18" rx="2"/><path d="M15 3v18"/>',
  menu: '<path d="M3 6h18"/><path d="M3 12h18"/><path d="M3 18h18"/>',
  moon: '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
  sun: '<circle cx="12" cy="12" r="5"/><path d="M12 1v2"/><path d="M12 21v2"/><path d="M4.22 4.22l1.42 1.42"/><path d="M18.36 18.36l1.42 1.42"/><path d="M1 12h2"/><path d="M21 12h2"/><path d="M4.22 19.78l1.42-1.42"/><path d="M18.36 5.64l1.42-1.42"/>',
  flame: '<path d="M12 3c1.2 2.8 4.5 4.4 4.5 8a4.5 4.5 0 0 1-9 0c0-1.8.8-3 1.6-4.1.4 1 1.2 1.6 1.2 1.6C10.6 6.6 11.2 4.8 12 3z"/>',
  star: '<path d="m12 2 3.09 6.26L22 9.27l-5 4.87L18.18 21 12 17.77 5.82 21 7 14.14l-5-4.87 6.91-1.01L12 2z"/>',
  inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
};

export function icon(name, cls = '') {
  const paths = ICON_PATHS[name] || ICON_PATHS.info;
  return `<svg class="ico ${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
}

/* ---------- formatters ---------- */
export function fmtDate(value) {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', { hour12: false });
}

export function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/* ---------- states ---------- */
export function loading(msg = '正在载入…') {
  return `<div class="loading-block"><span class="spinner"></span> ${esc(msg)}</div>`;
}

export function empty(msg = '暂无数据', sub = '') {
  return `<div class="empty">
    <div class="empty-ornament" aria-hidden="true"></div>
    <div class="empty-icon">${icon('feather')}</div>
    <div class="msg">${esc(msg)}</div>
    ${sub ? `<div class="sub">${esc(sub)}</div>` : ''}
  </div>`;
}

export function errorBlock(msg) {
  return `<div class="banner banner-danger"><span class="banner-ico">${icon('alert')}</span><div>${msg}</div></div>`;
}

export function banner(msg, kind = '') {
  return `<div class="banner ${kind ? 'banner-' + kind : ''}"><span class="banner-ico">${icon(kind === 'danger' ? 'alert' : 'info')}</span><div>${msg}</div></div>`;
}

/* ---------- tags ---------- */
export function tag(text, tone = 'muted') {
  return `<span class="tag tag-${tone}">${text}</span>`;
}

export function heatTag(heat) {
  const v = parseFloat(heat) || 0;
  const tone = v >= 60 ? 'red' : v >= 30 ? 'amber' : 'slate';
  return `<span class="tag tag-${tone}"><span class="dot dot-${tone}"></span>热度 ${v.toFixed(1)}</span>`;
}

export function impTag(importance) {
  const n = Number(importance) || 0;
  const tone = n >= 8 ? 'plum' : n >= 5 ? 'gold' : 'muted';
  return tag(`重要性 ${n}`, tone);
}

export function pagerHtml(page, pages, total) {
  if (pages <= 1) return total ? `<div class="pagination"><span class="page-info">共 ${total} 条</span></div>` : '';
  return `<div class="pagination">
    ${page > 0 ? `<button class="btn btn-secondary btn-sm" data-act="page" data-p="${page - 1}">上一页</button>` : ''}
    <span class="page-info">第 ${page + 1} / ${pages} 页 · 共 ${total} 条</span>
    ${page < pages - 1 ? `<button class="btn btn-secondary btn-sm" data-act="page" data-p="${page + 1}">下一页</button>` : ''}
  </div>`;
}

/* ---------- toast ---------- */
let _toastWrap = null;
export function toast(msg, type = 'ok') {
  if (!_toastWrap) {
    _toastWrap = document.createElement('div');
    _toastWrap.className = 'toast-wrap';
    document.body.appendChild(_toastWrap);
  }
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  // warn 与 err 共用警示图标；ok（成功）用对勾
  el.innerHTML = `<span class="toast-ico">${icon(type === 'ok' ? 'check' : 'alert')}</span><span></span>`;
  el.lastElementChild.textContent = msg;
  _toastWrap.appendChild(el);
  setTimeout(() => {
    el.classList.add('toast-out');
    setTimeout(() => el.remove(), 250);
  }, 3200);
}

/* ---------- modal ---------- */
const modalStack = [];
let modalId = 0;
const focusableSelector = 'a[href],button,input:not([type="hidden"]),select,textarea,[tabindex]';

function visibleFocusables(scope) {
  return [...scope.querySelectorAll(focusableSelector)].filter(el =>
    !el.disabled && !el.matches?.(':disabled') && el.tabIndex >= 0
      && !el.closest('[hidden],[inert]') && el.getClientRects().length);
}

export function modal({ title, body, footer, wide = false, draggable = false, onClose = null }) {
  const opener = document.activeElement;
  const titleId = `retro-modal-title-${++modalId}`;
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal ${wide ? 'modal-wide' : ''}" role="dialog" aria-modal="true" aria-labelledby="${titleId}" tabindex="-1">
      <div class="modal-head"><h3 id="${titleId}">${title}</h3><button type="button" class="modal-close" aria-label="关闭" data-tooltip="关闭">${icon('x')}</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>`;
  document.body.appendChild(mask);
  const root = mask.querySelector('.modal');
  let closed = false;
  const entry = { root, mask };
  modalStack.push(entry);
  const isTop = () => modalStack.at(-1) === entry;
  const overlays = () => [...document.querySelectorAll('[data-retro-overlay]')]
    .filter(pop => root.contains(pop._forInput));
  const containsFocus = el => root.contains(el) || overlays().some(pop => pop.contains(el));
  const focusFirst = () => {
    const preferred = root.querySelector('[autofocus],[data-initial-focus],[data-cancel]');
    const target = preferred && !preferred.disabled && preferred.getClientRects().length
      ? preferred : visibleFocusables(root)[0] || root;
    target.focus({ preventScroll: true });
  };
  const keydown = e => {
    if (!isTop() || e.defaultPrevented) return;
    if (e.key === 'Escape') {
      // Owned selectors consume Escape first, including their hour/minute panel.
      if (overlays().length) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      close();
    } else if (e.key === 'Tab') {
      const active = document.activeElement;
      const pop = overlays().find(panel => panel.contains(active));
      let origin = active;
      if (pop?.getAttribute('role') === 'listbox') {
        // A dropdown is one form stop: Tab leaves from its trigger's position.
        origin = pop._forInput?._retroField?.button || active;
        pop._retroClose();
      } else if (pop && active.closest('[role="listbox"]')) {
        origin = active.closest('.rtp-unit')?.querySelector('.rtp-select-btn') || active;
        pop._closeSubpanel?.();
      }
      const owned = overlays();
      const stops = visibleFocusables(root).flatMap(el => [el,
        ...owned.filter(panel => panel.dataset.retroOwner === el.id).flatMap(visibleFocusables)]);
      const current = stops.indexOf(origin);
      const next = e.shiftKey
        ? (current <= 0 ? stops.length - 1 : current - 1)
        : (current < 0 || current === stops.length - 1 ? 0 : current + 1);
      e.preventDefault();
      e.stopPropagation();
      (stops[next] || root).focus({ preventScroll: true });
    }
  };
  const focusin = e => { if (isTop() && !containsFocus(e.target)) focusFirst(); };
  const close = (result = false) => {
    if (closed) return;
    closed = true;
    root.querySelectorAll('.retro-select-value,.retro-time-value').forEach(input => input._retroField?.destroy());
    document.removeEventListener('keydown', keydown, true);
    document.removeEventListener('focusin', focusin);
    removalObserver.disconnect();
    const index = modalStack.indexOf(entry);
    if (index >= 0) modalStack.splice(index, 1);
    mask.remove();
    const parent = modalStack.at(-1);
    if (opener?.isConnected && opener !== document.body && !opener.disabled && !opener.matches?.(':disabled')
      && !opener.closest('[hidden],[inert]') && opener.getClientRects().length
      && (!parent || parent.root.contains(opener))) {
      opener.focus({ preventScroll: true });
    } else if (parent) {
      (visibleFocusables(parent.root)[0] || parent.root).focus({ preventScroll: true });
    } else {
      const fallback = document.getElementById('page-title');
      if (fallback) { fallback.tabIndex = -1; fallback.focus({ preventScroll: true }); }
    }
    onClose?.(result === true);
  };
  // A caller removing the host directly still settles confirmations and releases listeners.
  const removalObserver = new MutationObserver(() => { if (!mask.isConnected) close(); });
  removalObserver.observe(document.body, { childList: true, subtree: true });
  document.addEventListener('keydown', keydown, true);
  document.addEventListener('focusin', focusin);
  queueMicrotask(() => { if (!closed && isTop()) focusFirst(); });
  mask.querySelector('.modal-close').onclick = close;
  mask.addEventListener('click', e => { if (e.target === mask) close(); });
  if (draggable) {
    const head = mask.querySelector('.modal-head');
    head.classList.add('draggable');
    let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    head.addEventListener('pointerdown', (e) => {
      if (e.target.closest('button')) return;
      dragging = true; sx = e.clientX; sy = e.clientY;
      const rect = root.getBoundingClientRect();
      ox = rect.left; oy = rect.top;
      root.style.position = 'fixed'; root.style.margin = '0';
      head.setPointerCapture(e.pointerId);
    });
    head.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      root.style.left = `${ox + e.clientX - sx}px`;
      root.style.top = `${Math.max(0, oy + e.clientY - sy)}px`;
    });
    head.addEventListener('pointerup', () => { dragging = false; });
  }
  return { root, close };
}

export function confirm(msg, { title = '请确认', okText = '确认', cancelText = '取消', danger = true } = {}) {
  return new Promise(resolve => {
    const { root, close } = modal({
      title,
      body: `<p class="confirm-text">${msg}</p>`,
      footer: `<button class="btn btn-secondary" data-cancel>${cancelText}</button>
               <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-ok>${okText}</button>`,
      onClose: resolve,
    });
    root.querySelector('[data-cancel]').onclick = () => close();
    root.querySelector('[data-ok]').onclick = () => close(true);
  });
}

/* ---------- shared paper tooltip (delegated; dynamic fields need no listeners) ---------- */
let tooltipDispose = null;
export function initTooltips() {
  if (tooltipDispose) return tooltipDispose;
  const tip = document.createElement('div');
  tip.className = 'retro-tooltip';
  tip.id = 'retro-tooltip';
  tip.setAttribute('role', 'tooltip');
  tip.hidden = true;
  document.body.appendChild(tip);
  let anchor = null;
  const hide = () => {
    if (anchor) {
      const ids = (anchor.getAttribute('aria-describedby') || '').split(/\s+/).filter(id => id && id !== tip.id);
      if (ids.length) anchor.setAttribute('aria-describedby', ids.join(' '));
      else anchor.removeAttribute('aria-describedby');
    }
    anchor = null;
    tip.hidden = true;
  };
  const show = el => {
    hide();
    if (!el?.dataset.tooltip || !el.isConnected) return;
    anchor = el;
    tip.textContent = el.dataset.tooltip;
    tip.hidden = false;
    const ids = new Set((el.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean));
    ids.add(tip.id);
    el.setAttribute('aria-describedby', [...ids].join(' '));
    const rect = el.getBoundingClientRect();
    const size = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(8, Math.min(rect.left + (rect.width - size.width) / 2, window.innerWidth - size.width - 8))}px`;
    const below = rect.bottom + 7;
    tip.style.top = `${Math.max(8, Math.min(below + size.height <= window.innerHeight - 8 ? below : rect.top - size.height - 7, window.innerHeight - size.height - 8))}px`;
  };
  const enter = e => { const el = e.target.closest?.('[data-tooltip]'); if (el !== anchor) show(el); };
  const leave = e => {
    if (anchor && !anchor.contains(e.relatedTarget)) {
      if (anchor.contains(document.activeElement)) return;
      hide();
    }
  };
  const focusout = () => hide();
  const keydown = e => { if (e.key === 'Escape') hide(); };
  const removalObserver = new MutationObserver(() => { if (anchor && !anchor.isConnected) hide(); });
  removalObserver.observe(document.body, { childList: true, subtree: true });
  document.addEventListener('pointerover', enter);
  document.addEventListener('pointerout', leave);
  document.addEventListener('focusin', enter);
  document.addEventListener('focusout', focusout);
  document.addEventListener('keydown', keydown);
  window.addEventListener('resize', hide);
  window.addEventListener('scroll', hide, true);
  tooltipDispose = () => {
    hide();
    removalObserver.disconnect();
    document.removeEventListener('pointerover', enter);
    document.removeEventListener('pointerout', leave);
    document.removeEventListener('focusin', enter);
    document.removeEventListener('focusout', focusout);
    document.removeEventListener('keydown', keydown);
    window.removeEventListener('resize', hide);
    window.removeEventListener('scroll', hide, true);
    tip.remove();
    tooltipDispose = null;
  };
  return tooltipDispose;
}

/* ---------- event delegation ---------- */
export function delegate(root, actions) {
  root.addEventListener('click', e => {
    const el = e.target.closest('[data-act]');
    if (!el || el.disabled) return;
    const act = el.dataset.act;
    if (actions[act]) actions[act](el, e);
  });
}

/* ---------- detail panel (right column / mobile drawer) ---------- */
export function createDetailPanel(host) {
  host.insertAdjacentHTML('beforeend', `
    <aside class="detail-panel" aria-label="详情栏">
      <div class="detail-head">
        <div class="detail-titles">
          <h3 class="detail-title"></h3>
          <div class="detail-badges tag-row"></div>
        </div>
        <button type="button" class="icon-btn detail-collapse" aria-label="收起详情栏" data-tooltip="收起详情栏">${icon('chevron-right')}</button>
      </div>
      <div class="detail-body"></div>
      <div class="detail-foot"></div>
    </aside>
    <button type="button" class="detail-rail" aria-label="展开详情栏" data-tooltip="展开详情栏">${icon('chevron-left')}<span>详情</span></button>
    <div class="detail-backdrop"></div>`);

  const panel = host.querySelector('.detail-panel');
  const rail = host.querySelector('.detail-rail');
  const backdrop = host.querySelector('.detail-backdrop');
  const titleEl = panel.querySelector('.detail-title');
  const badgesEl = panel.querySelector('.detail-badges');
  const bodyEl = panel.querySelector('.detail-body');
  const footEl = panel.querySelector('.detail-foot');
  const mq = window.matchMedia('(max-width: 1100px)');

  bodyEl.innerHTML = empty('未选择条目', '点击左侧列表中的卡片查看详情');

  const expand = () => host.classList.remove('detail-collapsed');
  const openDrawer = () => { expand(); host.classList.add('detail-open'); };
  const closeDrawer = () => host.classList.remove('detail-open');

  panel.querySelector('.detail-collapse').addEventListener('click', () => {
    if (mq.matches) closeDrawer(); else host.classList.add('detail-collapsed');
  });
  rail.addEventListener('click', expand);
  backdrop.addEventListener('click', closeDrawer);

  return {
    el: panel,
    body: bodyEl,
    foot: footEl,
    render({ title = '', badges = '', html = '', actions = '' }) {
      titleEl.innerHTML = title;
      badgesEl.innerHTML = badges;
      bodyEl.innerHTML = html || empty('未选择条目', '点击左侧列表中的卡片查看详情');
      footEl.innerHTML = actions || '';
      panel.scrollTop = 0;
      expand();
      bodyEl.scrollTop = 0;
      if (mq.matches) openDrawer();
    },
    closeDrawer,
    isOpenMobile: () => host.classList.contains('detail-open'),
  };
}
