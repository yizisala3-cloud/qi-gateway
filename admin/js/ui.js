// ui.js - shared retro UI components: icons, tags, modal, toast, detail panel
export const ASSET_VERSION = '20261007-planning-batch3';

/* ---------- SVG icons (stroke, no emoji; object entries = fill icons with own viewBox) ---------- */
const ICON_PATHS = {
  calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4"/><path d="M8 2v4"/><path d="M3 10h18"/>',
  bell: '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/>',
  scroll: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/>',
  heart: '<path d="M20.84 4.61a5.5 5.5 0 0 0-7.78 0L12 5.67l-1.06-1.06a5.5 5.5 0 0 0-7.78 7.78l1.06 1.06L12 21.23l7.78-7.78 1.06-1.06a5.5 5.5 0 0 0 0-7.78z"/>',
  feather: {
    vb: '217 200 826 957',
    fill: true,
    paths: '<path d="M239.9 1155.7C230.1 1152.9 222.5 1146.2 218.5 1136.6C216.1 1130.9 216.6 1119.5 219.4 1113.5C222.0 1107.8 222.8 1106.7 292.9 1013.7C307.0 995.0 320.8 976.7 323.5 973.0C326.2 969.2 330.5 963.6 333.0 960.4C335.4 957.1 341.1 949.5 345.7 943.5L354.0 932.4L350.4 923.5C337.3 890.1 330.5 856.7 329.3 820.5C327.3 755.8 345.1 697.5 381.9 648.2C391.3 635.6 410.3 615.0 412.6 615.0C413.9 615.0 414.2 615.9 418.0 631.0C421.4 644.6 426.8 661.5 432.2 675.2C434.9 682.3 437.3 688.0 437.4 688.0C437.6 688.0 437.6 676.2 437.6 661.8C437.5 625.2 440.1 603.4 448.5 572.0C453.9 551.8 463.9 525.6 472.2 510.0C487.7 480.9 496.0 468.4 513.7 447.9C522.5 437.7 524.2 436.3 525.9 438.4C527.1 439.8 547.0 487.5 547.0 488.9C547.0 489.5 547.4 490.1 547.9 490.4C548.4 490.7 550.7 485.7 553.0 479.2C567.0 440.7 589.6 408.1 629.6 368.9C688.5 311.1 764.0 264.2 846.7 234.1C902.4 213.8 964.2 200.8 1006.0 200.6C1021.1 200.5 1022.8 200.7 1026.5 202.6C1031.8 205.5 1037.5 211.1 1040.3 216.3C1043.6 222.5 1043.3 240.9 1039.7 257.5C1027.9 312.2 1003.2 370.2 969.6 421.8C963.4 431.4 943.3 458.4 934.4 469.0C917.7 488.9 893.5 512.2 871.9 529.2C855.2 542.3 803.6 579.3 783.6 592.5C780.4 594.7 778.2 596.6 778.8 596.8C781.5 597.7 825.5 589.2 856.0 582.0C875.7 577.3 877.0 577.2 877.0 580.2C876.9 586.1 855.9 623.2 840.3 644.9C823.2 668.6 793.2 700.6 767.0 723.1C734.0 751.5 693.1 776.7 649.5 795.5C643.5 798.1 638.3 800.5 638.0 800.8C637.2 801.6 666.0 800.1 682.3 798.4C690.7 797.6 705.1 795.8 714.4 794.4C725.9 792.8 731.7 792.3 732.4 793.0C735.1 795.7 716.8 823.8 698.4 845.0C662.3 886.6 618.9 915.6 561.5 936.5C523.4 950.3 468.0 961.5 419.0 965.2L403.0 966.4L400.4 970.5C397.7 974.5 391.6 983.0 376.5 1003.5C372.0 1009.5 365.9 1017.9 362.9 1022.0C359.9 1026.1 347.4 1043.0 335.0 1059.5C322.6 1076.0 310.7 1091.8 308.7 1094.5C306.6 1097.2 302.1 1103.3 298.5 1108.0C295.0 1112.7 287.1 1123.2 280.9 1131.5C274.8 1139.8 268.6 1147.5 267.1 1148.8C259.8 1155.3 248.6 1158.1 239.9 1155.7ZM419.9 1107.6C411.6 1104.6 404.3 1098.1 399.8 1089.8C396.6 1083.8 396.6 1069.7 399.7 1062.8C402.5 1056.6 409.3 1049.4 415.5 1046.2L420.5 1043.5L693.2 1043.2L965.9 1043.0L971.6 1045.1C978.9 1047.9 986.1 1054.5 989.7 1061.9C992.1 1066.7 992.5 1068.7 992.4 1076.0C992.4 1082.9 991.9 1085.5 990.0 1089.5C986.9 1095.9 979.4 1103.4 973.3 1106.3L968.5 1108.5L696.0 1108.7C474.9 1108.9 422.8 1108.7 419.9 1107.6ZM420.7 899.7C431.7 885.6 444.7 868.2 451.6 858.4C453.2 856.1 458.6 848.4 463.6 841.4C472.2 829.2 478.9 819.5 495.5 795.0C530.8 743.2 533.9 738.8 555.8 707.7C582.7 669.6 622.2 617.2 644.8 589.5C677.4 549.7 678.8 548.0 688.5 536.8C718.6 502.0 757.3 460.7 784.7 433.9C817.2 402.1 838.6 382.7 882.3 345.1C887.1 341.0 890.8 337.5 890.6 337.3C890.0 336.7 874.6 344.6 863.8 351.1C844.0 362.9 828.9 373.4 802.6 393.3C793.5 400.1 768.6 420.5 758.5 429.3C747.0 439.4 705.7 478.6 698.0 486.6C694.5 490.4 686.2 499.1 679.5 506.0C619.1 569.0 554.5 653.1 496.9 743.8C459.3 803.0 427.3 864.1 407.2 914.9C405.9 918.2 409.6 914.1 420.7 899.7Z"/>',
  },
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
  const entry = ICON_PATHS[name] || ICON_PATHS.info;
  const paths = typeof entry === 'string' ? entry : entry.paths;
  const viewBox = typeof entry === 'string' ? '0 0 24 24' : (entry.vb || '0 0 24 24');
  const body = typeof entry === 'string'
    ? paths
    : `<g fill="${entry.fill ? 'currentColor' : 'none'}" stroke="${entry.fill ? 'none' : 'currentColor'}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${paths}</g>`;
  return `<svg class="ico ${cls}" viewBox="${viewBox}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
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
export function modal({ title, body, footer, wide = false, draggable = false, onMaskClose = null }) {
  const mask = document.createElement('div');
  mask.className = 'modal-mask';
  mask.innerHTML = `
    <div class="modal ${wide ? 'modal-wide' : ''}" role="dialog" aria-label="${esc(title)}">
      <div class="modal-head"><h3>${title}</h3><button class="modal-close" aria-label="关闭">${icon('x')}</button></div>
      <div class="modal-body">${body}</div>
      ${footer ? `<div class="modal-foot">${footer}</div>` : ''}
    </div>`;
  document.body.appendChild(mask);
  const root = mask.querySelector('.modal');
  const close = () => mask.remove();
  mask.querySelector('.modal-close').onclick = close;
  mask.addEventListener('click', e => {
    if (e.target !== mask) return;
    // onMaskClose：调用方自有的关闭流程（含保存确认/清理）；不提供时保持
    // 原默认直接移除
    if (onMaskClose) { onMaskClose(); return; }
    close();
  });
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
  // mask 一并返回：调用方可识别“弹窗遮罩内”的按下目标（如提醒恢复入口的
  // 排除逻辑），不改变既有 root/close 语义
  return { root, close, mask };
}

export function confirm(msg, { title = '请确认', okText = '确认', cancelText = '取消', danger = true } = {}) {
  return new Promise(resolve => {
    const { root, close } = modal({
      title,
      body: `<p class="confirm-text">${msg}</p>`,
      footer: `<button class="btn btn-secondary" data-cancel>${cancelText}</button>
               <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-ok>${okText}</button>`,
      // 遮罩点击同样按取消结算（BUG-11）：只移除节点不结束 Promise 会让
      // 背后的保存/删除流程永远等待
      onMaskClose: () => { close(); resolve(false); },
    });
    // 确认 / 取消 / × / 遮罩四个入口都只结算一次 Promise；×与遮罩按取消
    // 处理（BUG-11）。重复 resolve 本身无害，显式覆盖 × 的默认 close。
    root.querySelector('[data-cancel]').onclick = () => { close(); resolve(false); };
    root.querySelector('[data-ok]').onclick = () => { close(); resolve(true); };
    root.querySelector('.modal-close').onclick = () => { close(); resolve(false); };
  });
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
        <button class="icon-btn detail-collapse" title="收起详情栏">${icon('chevron-right')}</button>
      </div>
      <div class="detail-body"></div>
      <div class="detail-foot"></div>
    </aside>
    <button class="detail-rail" title="展开详情栏">${icon('chevron-left')}<span>详情</span></button>
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
