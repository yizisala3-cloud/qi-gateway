// Merge only a still-applicable committed order, independently of the drag UI.
const ESTIMATE_FIELDS = ['est_start', 'est_end', 'nominal_start',
  'estimated_time_source', 'fixed_source', 'schedule_managed', 'is_fixed'];

export function mergeOrderReceipt(board, ids, result, expectedBoard) {
  if (result?.replayed || !expectedBoard) return board;
  if (board) {
    // A read may already contain a later lifecycle, window, order or time change.
    // Never roll it back with the snapshot of an earlier commit.
    const expected = new Map(expectedBoard.progress.map((occ) => [occ.id, occ]));
    if (board.progress.length !== expected.size
        || board.recompute?.enabled !== expectedBoard.recompute?.enabled
        || board.progress.some((occ) => {
          const before = expected.get(occ.id);
          return !before || Object.keys({ ...before, ...occ }).some((key) =>
            !['time_updating', 'schedule_label'].includes(key) && JSON.stringify(occ[key]) !== JSON.stringify(before[key]));
        })) return board;
  }
  const source = board || expectedBoard;
  const positions = new Map(ids.map((id, index) => [id, index]));
  if (source.progress.length !== positions.size
      || source.progress.some((occ) => !positions.has(occ.id))) return board;
  const estimates = new Map((result?.estimates || []).map((occ) => [occ.id, occ]));
  const enabled = typeof result?.recompute_enabled === 'boolean'
    ? result.recompute_enabled : source.recompute?.enabled;
  const recompute = { ...source.recompute, enabled };
  if (result?.schedule_applied || enabled === false) {
    Object.assign(recompute, { pending: false, requested_at: null,
      request_token: null, wait_minutes: null, reason: null });
  }
  return { ...source, recompute,
    conflicts: result?.schedule_applied ? [] : source.conflicts,
    progress: source.progress.map((occ) => {
      const estimate = estimates.get(occ.id);
      const fields = result?.schedule_applied && estimate
        ? Object.fromEntries(ESTIMATE_FIELDS.filter((key) => Object.hasOwn(estimate, key))
          .map((key) => [key, estimate[key]])) : {};
      return { ...occ, ...fields, sort_order: positions.get(occ.id),
        time_updating: enabled !== false && !result?.schedule_applied };
    }).sort((a, b) => a.sort_order - b.sort_order),
  };
}
