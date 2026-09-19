// app.js - shell: login, sidebar, routing, theme, mobile drawers
import { NAV, ROUTE_INDEX } from './routes.js?v=20260920-bookpage1';
import { loading, errorBlock, icon, esc } from './ui.js?v=20260920-bookpage1';
import { gw, getToken, setToken, clearToken } from './api.js?v=20260920-bookpage1';

const DEFAULT_ROUTE = 'memories';
const ASSET_VERSION = '20260920-bookpage1';

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('qi-theme', t);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.innerHTML = icon(t === 'night' ? 'sun' : 'moon');
}
function initTheme() { applyTheme(localStorage.getItem('qi-theme') === 'night' ? 'night' : 'day'); }
function isAuthed() { return !!getToken(); }

function showLogin(message = '') {
  document.getElementById('login-page').style.display = 'flex';
  document.getElementById('layout').style.display = 'none';
  document.getElementById('login-error').textContent = message;
}
function showApp() {
  document.getElementById('login-page').style.display = 'none';
  document.getElementById('layout').style.display = 'flex';
}

async function tryLogin(token) {
  setToken(token);
  try {
    await gw('/status');
    showApp(); renderSidebar();
    if (!location.hash) location.hash = '#/' + DEFAULT_ROUTE;
    await route(); refreshStatus();
  } catch (e) {
    clearToken(); showLogin(`登录失败：${e.message}`);
  }
}

function renderSidebar() {
  const nav = document.getElementById('sidebar-nav');
  nav.innerHTML = NAV.map(grp => `
    ${grp.title ? `<div class="nav-group-title">${esc(grp.title)}</div>` : ''}
    <div class="nav-group">${grp.items.map(it => `
      <a class="nav-item" href="#/${it.key}" data-key="${it.key}">
        ${icon(it.icon)}<span class="nav-label">${esc(it.label)}</span>
      </a>`).join('')}</div>`).join('');
  nav.querySelectorAll('.nav-item').forEach(a => {
    a.addEventListener('click', () => closeSidebar());
  });
}

function highlight(key) {
  document.querySelectorAll('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.key === key));
}

let currentMod = null;
let routeSeq = 0;
async function route() {
  const seq = ++routeSeq;
  const raw = location.hash.replace(/^#\/?/, '');
  const [key, qs] = raw.split('?');
  const params = Object.fromEntries(new URLSearchParams(qs || ''));
  const meta = ROUTE_INDEX[key];
  const content = document.getElementById('content');
  const root = document.getElementById('page-root');
  if (!meta) { location.hash = '#/' + DEFAULT_ROUTE; return; }
  highlight(key);
  document.getElementById('page-crumb').textContent = meta.group || 'qi-dashboard';
  document.getElementById('page-title').textContent = meta.label;
  document.getElementById('page-desc').textContent = meta.desc || '';
  document.title = `${meta.label} - qi-dashboard`;
  content.scrollTop = 0;
  root.innerHTML = loading();
  try { currentMod?.unmount?.(); } catch {}
  currentMod = null;
  try {
    const mod = (await import(`./pages/${key}.js?v=${ASSET_VERSION}`)).default;
    if (seq !== routeSeq) return;
    currentMod = mod;
    root.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'fade-in';
    root.appendChild(wrap);
    await mod.mount(wrap, params);
  } catch (e) {
    if (seq !== routeSeq) return;
    if (String(e.message).startsWith('401')) {
      clearToken(); showLogin('登录已过期，请重新输入网关 Token。');
      return;
    }
    console.error(e);
    root.innerHTML = errorBlock(`页面加载失败：${esc(e.message)}`);
  }
}

async function refreshStatus() {
  const dot = document.getElementById('status-dot');
  if (!dot) return;
  try {
    await gw('/status');
    dot.innerHTML = '<span class="dot dot-green"></span>在线';
    dot.className = 'status-chip online';
  } catch (e) {
    dot.innerHTML = '<span class="dot dot-red"></span>未授权';
    dot.className = 'status-chip offline';
    if (e.message.startsWith('401')) {
      clearToken(); showLogin('登录已过期，请重新输入网关 Token。');
    }
  }
}

function closeSidebar() {
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('sidebar-backdrop')?.classList.remove('show');
}

async function boot() {
  initTheme();
  document.getElementById('theme-btn')?.addEventListener('click', () => {
    applyTheme(document.documentElement.getAttribute('data-theme') === 'night' ? 'day' : 'night');
  });
  document.getElementById('login-btn')?.addEventListener('click', () => {
    const val = document.getElementById('login-input').value.trim(); if (val) tryLogin(val);
  });
  document.getElementById('login-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { const val = e.target.value.trim(); if (val) tryLogin(val); }
  });
  document.getElementById('menu-btn')?.addEventListener('click', () => {
    document.getElementById('sidebar')?.classList.add('open');
    document.getElementById('sidebar-backdrop')?.classList.add('show');
  });
  document.getElementById('sidebar-backdrop')?.addEventListener('click', closeSidebar);
  window.addEventListener('hashchange', route);
  if (isAuthed()) {
    try {
      await gw('/status'); showApp(); renderSidebar();
      if (!location.hash) location.hash = '#/' + DEFAULT_ROUTE;
      await route(); refreshStatus();
    } catch (e) {
      clearToken(); showLogin(`已保存的 Token 被拒绝：${e.message}`);
    }
  } else showLogin();
}
boot();
