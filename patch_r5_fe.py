"""One-off patch: browser uses the pure absorb-display module."""
p = 'admin/js/pages/_memory_browser.js'
s = open(p, encoding='utf-8').read()

# (1) import the pure module
old = "import { openMemoryForm } from './_memory_form.js?v=20260903-retrotime1';"
new = """import { openMemoryForm } from './_memory_form.js?v=20260903-retrotime1';
import { absorbTargetViews, absorbImpactViews, absorbImpactSummary } from '../lib/absorb_display.js?v=20260905-absorb1';"""
assert old in s, "import"
s = s.replace(old, new, 1)

# (2) REQUEST_FIELDS gains the immutable snapshots
old = "evidence_time_precision,recall_scene,recall_tags,producer_path,absorbed_fast_path_memory_ids,created_at,reviewed_at,reviewed_by,review_note';"
new = "evidence_time_precision,recall_scene,recall_tags,producer_path,absorbed_fast_path_memory_ids,absorbed_fast_path_memory_snapshots,created_at,reviewed_at,reviewed_by,review_note';"
assert old in s, "REQUEST_FIELDS"
s = s.replace(old, new, 1)

# (3) hydrateAbsorbTargets: delegate to the pure module (fixes the
#     ReferenceError on failed reads; per-target fallback id preserved)
old = '''async function hydrateAbsorbTargets(root, request) {
  const box = root.querySelector('#absorb-targets');
  if (!box) return;
  const ids = request.absorbed_fast_path_memory_ids || [];
  if (!ids.length) {
    box.innerHTML = `
      <div class="kv"><span class="k">拟交接目标</span><span class="v muted">无 · 通过后不会停用其他正式记忆</span></div>`;
    return;
  }
  box.innerHTML = `
    <div class="kv"><span class="k">拟交接目标</span><span class="v">${tag(`通过后停用 ${ids.length} 条快速路径记忆`, 'amber')}</span></div>
    <div class="absorb-target-list">${loading()}</div>`;
  const rows = await Promise.all(ids.map(async (id) => {
    try {
      const result = await gw(`/admin/api/data/memories/${encodeURIComponent(id)}`);
      return result.data?.[0] || null;
    } catch { return null; }
  }));
  const items = rows.map((m) => {
    if (!m) {
      return `<div class="kv"><span class="k">memory</span><span class="v" style="color:var(--red)">#${esc(id)} · 无法读取（通过将被拒绝）</span></div>`;
    }
    const content = String(m.content || '');
    const snippet = content.length > 60 ? `${content.slice(0, 60)}…` : content;
    return `<div class="kv"><span class="k">memory #${esc(m.id)}</span><span class="v">${esc(m.continuity_type || '-')} · ${esc(m.title || '(未命名)')} · ${esc(snippet)}${m.is_active ? '' : ' · 已非 active（通过将被拒绝）'}</span></div>`;
  });
  const list = box.querySelector('.absorb-target-list');
  if (list) list.innerHTML = items.join('');
}'''
new = '''async function hydrateAbsorbTargets(root, request) {
  const box = root.querySelector('#absorb-targets');
  if (!box) return;
  const ids = request.absorbed_fast_path_memory_ids || [];
  if (!ids.length) {
    box.innerHTML = `
      <div class="kv"><span class="k">拟交接目标</span><span class="v muted">无 · 通过后不会停用其他正式记忆</span></div>`;
    return;
  }
  box.innerHTML = `
    <div class="kv"><span class="k">拟交接目标</span><span class="v">${tag(`通过后停用 ${ids.length} 条快速路径记忆`, 'amber')}</span></div>
    <div class="absorb-target-list">${loading()}</div>`;
  const rows = await Promise.all(ids.map(async (id) => {
    try {
      const result = await gw(`/admin/api/data/memories/${encodeURIComponent(id)}`);
      return result.data?.[0] || null;
    } catch { return null; }
  }));
  const views = absorbTargetViews(ids, rows);
  const items = views.map((view) => `<div class="kv"><span class="k">memory</span><span class="v"${view.ok ? '' : ' style="color:var(--red)"'}>${esc(view.label)}</span></div>`);
  const list = box.querySelector('.absorb-target-list');
  if (list) list.innerHTML = items.join('');
}'''
assert old in s, "hydrateAbsorbTargets"
s = s.replace(old, new, 1)

# (4) hydrateAbsorbImpact: delegate to the pure module + snapshot warning
old = '''  /* 通过按钮附近再次显示吸收影响范围；目标已变化时提示通过会被拒绝。 */
  async function hydrateAbsorbImpact(root, request) {
    const impact = root.querySelector('#absorb-impact');
    const confirmBox = root.querySelector('#absorb-impact-confirm');
    if (!impact || !confirmBox) return;
    const ids = request.absorbed_fast_path_memory_ids || [];
    if (!ids.length) {
      impact.innerHTML = `<div class="kv"><span class="k">影响范围</span><span class="v muted">无 · 通过后不会停用其他正式记忆</span></div>`;
      return;
    }
    const rows = await Promise.all(ids.map(async (id) => {
      try {
        const result = await gw(`/admin/api/data/memories/${encodeURIComponent(id)}`);
        return result.data?.[0] || null;
      } catch { return null; }
    }));
    const missing = rows.some((m) => !m || !m.is_active);
    const lines = ids.map((id, index) => {
      const m = rows[index];
      if (!m) return `#${esc(id)} · 无法读取（通过将被拒绝）`;
      const content = String(m.content || '');
      const snippet = content.length > 40 ? `${content.slice(0, 40)}…` : content;
      return `#${esc(m.id)} ${esc(m.continuity_type || '-')} · ${esc(m.title || '(未命名)')} · ${esc(snippet)}${m.is_active ? '' : ' · 已非 active'}`;
    });
    impact.innerHTML = `
      <div class="field">
        <label>影响范围：通过后将停用以下 ${ids.length} 条快速路径正式记忆</label>
        <div class="kv-block">${lines.join('<br>')}</div>
        ${missing ? '<div class="disabled-note" style="margin-top:4px">部分目标已变化或不再可用：服务端将拒绝本次通过，不会部分生效。</div>' : ''}
      </div>`;
    confirmBox.innerHTML = missing
      ? `<div class="banner banner-danger" style="margin-bottom:8px"><span class="banner-ico">${icon('alert')}</span><div>拟交接目标已变化，通过操作会被服务端整笔拒绝。</div></div>`
      : '';
  }'''
new = '''  /* 通过按钮附近再次显示吸收影响范围；目标相对申请快照发生变化时提示
   * 通过会被服务端整笔拒绝。 */
  async function hydrateAbsorbImpact(root, request) {
    const impact = root.querySelector('#absorb-impact');
    const confirmBox = root.querySelector('#absorb-impact-confirm');
    if (!impact || !confirmBox) return;
    const ids = request.absorbed_fast_path_memory_ids || [];
    if (!ids.length) {
      impact.innerHTML = `<div class="kv"><span class="k">影响范围</span><span class="v muted">无 · 通过后不会停用其他正式记忆</span></div>`;
      return;
    }
    const rows = await Promise.all(ids.map(async (id) => {
      try {
        const result = await gw(`/admin/api/data/memories/${encodeURIComponent(id)}`);
        return result.data?.[0] || null;
      } catch { return null; }
    }));
    const views = absorbImpactViews(
      ids, rows, request.absorbed_fast_path_memory_snapshots,
    );
    const summary = absorbImpactSummary(views);
    const lines = views.map((view) => {
      const marker = view.changed ? ' · <strong>目标已变化，本次通过会被拒绝</strong>' : '';
      return `#${esc(view.id)} ${esc(view.label)}${marker}`;
    });
    impact.innerHTML = `
      <div class="field">
        <label>影响范围：通过后将停用以下 ${ids.length} 条快速路径正式记忆</label>
        <div class="kv-block">${lines.join('<br>')}</div>
        ${summary.blocked ? '<div class="disabled-note" style="margin-top:4px">部分目标已变化或不再可用：服务端将拒绝本次通过，不会部分生效。</div>' : ''}
      </div>`;
    confirmBox.innerHTML = summary.blocked
      ? `<div class="banner banner-danger" style="margin-bottom:8px"><span class="banner-ico">${icon('alert')}</span><div>拟交接目标已变化或不再可用，通过操作会被服务端整笔拒绝。</div></div>`
      : '';
  }'''
assert old in s, "hydrateAbsorbImpact"
s = s.replace(old, new, 1)

open(p, 'w', encoding='utf-8').write(s)
print("browser rewired to pure module")
