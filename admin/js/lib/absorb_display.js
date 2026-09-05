// lib/absorb_display.js - pure rendering data for rumination absorption targets.
// Dependency-free by design: admin tests execute these functions directly
// (quickjs) so the failure-branch scoping and snapshot-mismatch behaviour stay
// verifiable without a browser. Every view derives its label from the ORIGINAL
// id at the same index; a failed or missing row can never leak an undefined id
// into the UI. The label carries the memory id exactly once.

function absorbImpactViews(ids, rows, snapshots) {
  return (ids || []).map((id, index) => {
    const m = rows && rows[index] ? rows[index] : null;
    if (!m) {
      return {
        id,
        ok: false,
        snapshotMissing: false,
        changed: false,
        label: `#${id} · 无法读取（通过将被拒绝）`,
      };
    }
    const view = compareAgainstSnapshot(id, m, snapshots);
    const content = String(m.content || '');
    const snippet = content.length > 40 ? `${content.slice(0, 40)}…` : content;
    const marker = view.snapshotMissing
      ? ' · <strong>申请快照缺失，本次通过会被拒绝</strong>'
      : view.changed ? ' · <strong>目标已变化，本次通过会被拒绝</strong>' : '';
    return {
      id,
      ok: Boolean(m.is_active) && !view.changed && !view.snapshotMissing,
      snapshotMissing: view.snapshotMissing,
      changed: view.changed,
      label: `#${m.id} ${m.continuity_type || '-'} · ${m.title || '(未命名)'} · ${snippet}${marker}`,
    };
  });
}

// 与数据库的九项快照复核保持一致：content_hash、continuity_id、
// continuity_type、memory_key、thread_state、evidence_message_ids、
// producer_path、verified、is_active。快照缺失是独立的阻塞状态——
// 数据库会以 memory_rumination_absorb_target_changed 拒绝整笔审核。
function compareAgainstSnapshot(id, m, snapshots) {
  const snap = (snapshots || []).find(
    (entry) => entry && Number(entry.memory_id) === Number(id),
  ) || null;
  if (!snap) {
    return { snapshotMissing: true, changed: false };
  }
  const changed = (
    snap.content_hash !== m.content_hash
    || (snap.continuity_id || null) !== (m.continuity_id || null)
    || (snap.continuity_type || null) !== (m.continuity_type || null)
    || (snap.memory_key || null) !== (m.memory_key || null)
    || (snap.thread_state || null) !== (m.thread_state || null)
    || JSON.stringify(snap.evidence_message_ids || [])
        !== JSON.stringify(m.evidence_message_ids || [])
    || (snap.producer_path || null) !== (m.producer_path || null)
    || (snap.verified || null) !== (m.verified || null)
    || Boolean(snap.is_active) !== Boolean(m.is_active)
  );
  return { snapshotMissing: false, changed };
}

function absorbImpactSummary(views) {
  const missing = views.some((view) => !view.ok);
  const snapshotMissing = views.some((view) => view.snapshotMissing);
  const changed = views.some((view) => view.changed);
  return {
    missing,
    snapshotMissing,
    changed,
    blocked: missing || changed || snapshotMissing,
  };
}

export { absorbImpactViews, absorbImpactSummary };
