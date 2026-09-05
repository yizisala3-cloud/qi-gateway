// lib/absorb_display.js - pure rendering data for rumination absorption targets.
// Dependency-free by design: admin tests execute these functions directly
// (quickjs) so the failure-branch scoping stays verifiable without a browser.
// Every view derives its label from the ORIGINAL id at the same index; a
// failed or missing row can never leak an undefined id into the UI.

function absorbTargetViews(ids, rows) {
  return (ids || []).map((id, index) => {
    const m = rows ? rows[index] : null;
    if (!m) {
      return {
        id,
        ok: false,
        changed: false,
        label: `#${id} · 无法读取（通过将被拒绝）`,
      };
    }
    const content = String(m.content || '');
    const snippet = content.length > 60 ? `${content.slice(0, 60)}…` : content;
    return {
      id,
      ok: Boolean(m.is_active),
      changed: false,
      label: `#${m.id} ${m.continuity_type || '-'} · ${m.title || '(未命名)'} · ${snippet}${m.is_active ? '' : ' · 已非 active（通过将被拒绝）'}`,
    };
  });
}

function absorbImpactViews(ids, rows, snapshots) {
  return (ids || []).map((id, index) => {
    const m = rows && rows[index] ? rows[index] : null;
    if (!m) {
      return {
        id,
        ok: false,
        changed: false,
        label: `#${id} · 无法读取（通过将被拒绝）`,
      };
    }
    const snap = (snapshots || []).find(
      (entry) => entry && Number(entry.memory_id) === Number(id),
    ) || null;
    const changed = Boolean(snap) && (
      snap.content_hash !== m.content_hash
      || (snap.continuity_id || null) !== (m.continuity_id || null)
      || (snap.continuity_type || null) !== (m.continuity_type || null)
      || (snap.memory_key || null) !== (m.memory_key || null)
      || (snap.thread_state || null) !== (m.thread_state || null)
      || JSON.stringify(snap.evidence_message_ids || [])
          !== JSON.stringify(m.evidence_message_ids || [])
      || (snap.producer_path || null) !== (m.producer_path || null)
      || Boolean(snap.is_active) !== Boolean(m.is_active)
    );
    const content = String(m.content || '');
    const snippet = content.length > 40 ? `${content.slice(0, 40)}…` : content;
    return {
      id,
      ok: Boolean(m.is_active),
      changed,
      label: `#${m.id} ${m.continuity_type || '-'} · ${m.title || '(未命名)'} · ${snippet}${m.is_active ? '' : ' · 已非 active'}`,
    };
  });
}

function absorbImpactSummary(views) {
  const missing = views.some((view) => !view.ok);
  const changed = views.some((view) => view.changed);
  return {
    missing,
    changed,
    blocked: missing || changed,
  };
}

export { absorbTargetViews, absorbImpactViews, absorbImpactSummary };
