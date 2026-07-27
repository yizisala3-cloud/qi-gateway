// api.js - Supabase client + gateway fetch wrapper
const SUPABASE_URL = 'https://dlgunkklpjqletgahdtp.supabase.co';
const SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImRsZ3Vua2tscGpxbGV0Z2FoZHRwIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NTI1NjY4MzQsImV4cCI6MjA2ODE0MjgzNH0.LGOxfCBMNRDUDZWJzliAdHJCbXkIiwHhVL23v1m15g4';

let _supabase = null;

export function getSupabase() {
  if (!_supabase) {
    // @supabase/supabase-js loaded via importmap or CDN
    _supabase = window.supabase.createClient(SUPABASE_URL, SUPABASE_KEY);
  }
  return _supabase;
}

export function getToken() {
  return localStorage.getItem('qi-token') || '';
}

export function setToken(t) {
  localStorage.setItem('qi-token', t);
}

export function clearToken() {
  localStorage.removeItem('qi-token');
}

// Gateway API fetch (for /health, /status etc)
export async function gw(path, opts = {}) {
  const base = window.location.origin;
  const url = base + path;
  const headers = { ...opts.headers };
  const token = getToken();
  if (token) headers['Authorization'] = 'Bearer ' + token;
  const resp = await fetch(url, { ...opts, headers });
  if (!resp.ok) throw new Error(`${resp.status} ${resp.statusText}`);
  return resp.json();
}

// Supabase table helpers
export async function query(table, { select = '*', order, limit, eq, neq, filters } = {}) {
  let q = getSupabase().from(table).select(select);
  if (eq) for (const [k, v] of Object.entries(eq)) q = q.eq(k, v);
  if (neq) for (const [k, v] of Object.entries(neq)) q = q.neq(k, v);
  if (filters) for (const fn of filters) q = fn(q);
  if (order) q = q.order(order.col, { ascending: order.asc ?? false });
  if (limit) q = q.limit(limit);
  const { data, error, count } = await q;
  if (error) throw new Error(error.message);
  return data;
}

export async function insert(table, row) {
  const { data, error } = await getSupabase().from(table).insert(row).select();
  if (error) throw new Error(error.message);
  return data;
}

export async function update(table, id, row) {
  const { data, error } = await getSupabase().from(table).update(row).eq('id', id).select();
  if (error) throw new Error(error.message);
  return data;
}

export async function remove(table, id) {
  const { error } = await getSupabase().from(table).delete().eq('id', id);
  if (error) throw new Error(error.message);
}

export async function count(table, { eq } = {}) {
  let q = getSupabase().from(table).select('*', { count: 'exact', head: true });
  if (eq) for (const [k, v] of Object.entries(eq)) q = q.eq(k, v);
  const { count: c, error } = await q;
  if (error) throw new Error(error.message);
  return c || 0;
}

// HTML escape
export function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
