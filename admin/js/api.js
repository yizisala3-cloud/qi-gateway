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
  try {
    return await resp.json();
  } catch (error) {
    // R12（2026-10-07 复审 #12）：2xx 但正文不可解析（截断 / 非法 JSON）——
    // 保存可能已经成立，这是「结果未知」而不是确定失败。以稳定标记上抛，
    // 调用方按结果未知提示并沿用原操作身份重试；不得把 SyntaxError 文本
    // 当失败原因误报「保存失败：Unexpected end of JSON input」。
    const unknown = new Error('未收到完整的保存结果响应，无法确认保存是否成功');
    unknown.name = 'ResultUnknownError';
    unknown.resultUnknown = true;
    throw unknown;
  }
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
