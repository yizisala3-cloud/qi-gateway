// api.js - gateway and token-protected admin data API wrapper
const DATA_API = '/admin/api/data';

export function getToken() {
  return localStorage.getItem('qi-token') || '';
}

export function setToken(t) {
  localStorage.setItem('qi-token', t);
}

export function clearToken() {
  localStorage.removeItem('qi-token');
}

export async function gw(path, opts = {}) {
  const headers = { ...opts.headers };
  const token = getToken();
  if (token) headers.Authorization = 'Bearer ' + token;
  const resp = await fetch(window.location.origin + path, { ...opts, headers });
  if (!resp.ok) {
    let detail = '';
    try {
      const body = await resp.json();
      detail = body.error ? `: ${body.error}` : '';
    } catch {}
    throw new Error(`${resp.status} ${resp.statusText}${detail}`);
  }
  return resp.json();
}

function dataPath(table, id = null, params = {}) {
  const path = `${DATA_API}/${encodeURIComponent(table)}${id === null ? '' : `/${id}`}`;
  const qs = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    qs.set(key, typeof value === 'object' ? JSON.stringify(value) : String(value));
  }
  const suffix = qs.toString();
  return suffix ? `${path}?${suffix}` : path;
}

export async function query(
  table,
  { select = '*', order, limit = 50, offset = 0, eq, search } = {},
) {
  const result = await gw(dataPath(table, null, {
    select,
    order: order?.col,
    asc: order?.asc ?? false,
    limit,
    offset,
    eq,
    search,
  }));
  return result.data || [];
}

export async function insert(table, row) {
  const result = await gw(dataPath(table), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(row),
  });
  return result.data || [];
}

export async function update(table, id, row) {
  const result = await gw(dataPath(table, id), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(row),
  });
  return result.data || [];
}

export async function remove(table, id) {
  return gw(dataPath(table, id), { method: 'DELETE' });
}

export async function count(table, { eq, search } = {}) {
  const result = await gw(dataPath(table, null, {
    count: true,
    eq,
    search,
  }));
  return result.count || 0;
}

export function esc(s) {
  if (s === null || s === undefined) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
