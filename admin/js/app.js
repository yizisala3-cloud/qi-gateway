// app.js - router shell, theme, sidebar, auth
import { NAV, ROUTE_INDEX } from './routes.js?v=20260802-memory-review3';
import { loading, errorBlock } from './ui.js?v=20260802-memory-review3';
import { gw, getToken, setToken, clearToken } from './api.js?v=20260802-memory-review3';

const DEFAULT_ROUTE = 'dashboard';
const ASSET_VERSION = '20260804-digest-time1';

function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('qi-theme', t);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = t === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
}
function initTheme() { applyTheme(localStorage.getItem('qi-theme') || 'dark'); }
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
    clearToken(); showLogin(`Login failed: ${e.message}`);
  }
}
function renderSidebar() {
  const nav = document.getElementById('sidebar-nav');
  nav.innerHTML = NAV.map(grp => `
    ${grp.title ? `<div class="nav-group-title">${grp.title}</div>` : ''}
    <div class="nav-group">${grp.items.map(it => `
      <a class="nav-item" href="#/${it.key}" data-key="${it.key}">
        <span class="ico">${it.icon}</span><span class="nav-label">${it.label}</span>
      </a>`).join('')}</div>`).join('');
}
function highlight(key) {
  document.querySelectorAll('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.key === key));
}
let currentMod = null;
async function route() {
  const key = (location.hash.replace(/^#\/?/, '') || DEFAULT_ROUTE).split('?')[0];
  const meta = ROUTE_INDEX[key];
  const content = document.getElementById('content');
  if (!meta) { location.hash = '#/' + DEFAULT_ROUTE; return; }
  highlight(key);
  document.getElementById('page-title').textContent = `${meta.icon} ${meta.label}`;
  document.getElementById('page-crumb').textContent = meta.group || 'qi-dashboard';
  document.title = `${meta.label} - qi-dashboard`;
  content.scrollTop = 0;
  content.innerHTML = loading();
  try { currentMod?.unmount?.(); } catch {}
  currentMod = null;
  try {
    const mod = (await import(`./pages/${key}.js?v=${ASSET_VERSION}`)).default;
    currentMod = mod;
    content.innerHTML = '';
    const wrap = document.createElement('div');
    wrap.className = 'fade-in';
    content.appendChild(wrap);
    await mod.mount(wrap);
  } catch (e) {
    console.error(e);
    content.innerHTML = errorBlock(`Page load failed: ${e.message}`);
  }
}
async function refreshStatus() {
  const dot = document.getElementById('status-dot');
  try {
    await gw('/status');
    if (dot) { dot.textContent = 'online'; dot.className = 'badge badge-accent'; }
  } catch (e) {
    if (dot) { dot.textContent = 'unauthorized'; dot.className = 'badge badge-danger'; }
    if (e.message.startsWith('401')) {
      clearToken(); showLogin('Session expired. Please enter the gateway token again.');
    }
  }
}
async function boot() {
  initTheme();
  document.getElementById('theme-btn')?.addEventListener('click', () => {
    applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });
  document.getElementById('login-btn')?.addEventListener('click', () => {
    const val = document.getElementById('login-input').value.trim(); if (val) tryLogin(val);
  });
  document.getElementById('login-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') { const val = e.target.value.trim(); if (val) tryLogin(val); }
  });
  document.getElementById('menu-btn')?.addEventListener('click', () => document.getElementById('sidebar')?.classList.toggle('open'));
  window.addEventListener('hashchange', route);
  if (isAuthed()) {
    try {
      await gw('/status'); showApp(); renderSidebar();
      if (!location.hash) location.hash = '#/' + DEFAULT_ROUTE;
      await route(); refreshStatus();
    } catch (e) {
      clearToken(); showLogin(`Saved token rejected: ${e.message}`);
    }
  } else showLogin();
}
boot();

