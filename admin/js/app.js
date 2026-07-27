// app.js - router shell, theme, sidebar, auth
import { NAV, ROUTE_INDEX } from './routes.js';
import { loading, errorBlock } from './ui.js';
import { gw, getToken, setToken, clearToken } from './api.js';

const DEFAULT_ROUTE = 'dashboard';

// --- Theme ---
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('qi-theme', t);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = t === 'dark' ? '\u2600\uFE0F' : '\uD83C\uDF19';
}
function initTheme() {
  const saved = localStorage.getItem('qi-theme') || 'dark';
  applyTheme(saved);
}

// --- Auth ---
function isAuthed() { return !!getToken(); }

function showLogin() {
  document.getElementById('login-page').style.display = 'flex';
  document.getElementById('layout').style.display = 'none';
}

function showApp() {
  document.getElementById('login-page').style.display = 'none';
  document.getElementById('layout').style.display = 'flex';
}

async function tryLogin(token) {
  setToken(token);
  try {
    await gw('/health');
    showApp();
    renderSidebar();
    route();
    refreshStatus();
  } catch {
    clearToken();
    document.getElementById('login-error').textContent = 'Invalid token or gateway offline';
  }
}

// --- Sidebar ---
function renderSidebar() {
  const nav = document.getElementById('sidebar-nav');
  nav.innerHTML = NAV.map(grp => `
    ${grp.title ? `<div class="nav-group-title">${grp.title}</div>` : ''}
    <div class="nav-group">
      ${grp.items.map(it => `
        <a class="nav-item" href="#/${it.key}" data-key="${it.key}">
          <span class="ico">${it.icon}</span><span class="nav-label">${it.label}</span>
        </a>`).join('')}
    </div>`).join('');
}

function highlight(key) {
  document.querySelectorAll('.nav-item').forEach(a =>
    a.classList.toggle('active', a.dataset.key === key));
}

// --- Router ---
let currentMod = null;

async function route() {
  const key = (location.hash.replace(/^#\/?/, '') || DEFAULT_ROUTE).split('?')[0];
  const meta = ROUTE_INDEX[key];
  const content = document.getElementById('content');
  const titleEl = document.getElementById('page-title');
  const crumbEl = document.getElementById('page-crumb');

  if (!meta) { location.hash = '#/' + DEFAULT_ROUTE; return; }

  highlight(key);
  titleEl.textContent = `${meta.icon} ${meta.label}`;
  crumbEl.textContent = meta.group || 'qi-dashboard';
  document.title = `${meta.label} - qi-dashboard`;
  content.scrollTop = 0;
  content.innerHTML = loading();

  try { currentMod?.unmount?.(); } catch {}
  currentMod = null;

  try {
    const mod = (await import(`./pages/${key}.js`)).default;
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

// --- Status dot ---
async function refreshStatus() {
  const dot = document.getElementById('status-dot');
  try {
    const s = await gw('/health');
    if (dot) { dot.textContent = 'online'; dot.className = 'badge badge-accent'; }
  } catch {
    if (dot) { dot.textContent = 'offline'; dot.className = 'badge badge-danger'; }
  }
}

// --- Boot ---
function boot() {
  initTheme();

  // Theme toggle
  document.getElementById('theme-btn')?.addEventListener('click', () => {
    applyTheme(document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
  });

  // Login
  document.getElementById('login-btn')?.addEventListener('click', () => {
    const val = document.getElementById('login-input').value.trim();
    if (val) tryLogin(val);
  });
  document.getElementById('login-input')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      const val = e.target.value.trim();
      if (val) tryLogin(val);
    }
  });

  // Mobile menu
  document.getElementById('menu-btn')?.addEventListener('click', () => {
    document.getElementById('sidebar')?.classList.toggle('open');
  });

  // Route change
  window.addEventListener('hashchange', route);

  // Check auth
  if (isAuthed()) {
    showApp();
    renderSidebar();
    if (!location.hash) location.hash = '#/' + DEFAULT_ROUTE;
    route();
    refreshStatus();
  } else {
    showLogin();
  }
}

boot();
