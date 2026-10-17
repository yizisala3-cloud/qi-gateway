// Committed scheduling fields can move between caches without copying facts.
const ESTIMATE_FIELDS = ['est_start', 'est_end', 'nominal_start',
  'estimated_time_source', 'fixed_source', 'schedule_managed', 'is_fixed'];
const COMMITTED_FIELDS = [...ESTIMATE_FIELDS, 'sort_order'];

function timestampMicros(value) {
  if (typeof value !== 'string') return null;
  const milliseconds = Date.parse(value);
  if (!Number.isFinite(milliseconds)) return null;
  const fraction = value.match(/\.(\d+)/)?.[1] || '';
  return BigInt(milliseconds) * 1000n + BigInt(fraction.padEnd(6, '0').slice(3, 6));
}

function sameValue(key, left, right) {
  if (left === right) return true;
  // RPC timestamptz and GET serialization may use different UTC offsets.
  if (typeof left === 'string' && typeof right === 'string'
      && /(_at|_start|_end)$/.test(key) && /^\d{4}-\d{2}-\d{2}T/.test(left)
      && /^\d{4}-\d{2}-\d{2}T/.test(right)) {
    const parsed = timestampMicros(left);
    return parsed !== null && parsed === timestampMicros(right);
  }
  return JSON.stringify(left) === JSON.stringify(right);
}

function matchesSnapshot(occ, before, committed = null) {
  if (!before) return false;
  return Object.keys({ ...before, ...occ }).every((key) =>
    ['time_updating', 'schedule_label'].includes(key)
    || sameValue(key, occ[key], before[key])
    || (COMMITTED_FIELDS.includes(key) && committed && Object.hasOwn(committed, key)
      && sameValue(key, occ[key], committed[key])));
}

// Keep planning_serialization.schedule_label semantics, using the server board
// clock rather than the browser clock. No lifecycle or scheduling writes occur.
export function scheduleLabel(occ, now) {
  if (occ.status === 'timeout') return '超时';
  if (occ.is_parked) return '已沉底';
  if (occ.status === 'deferred') return '落后';
  const clock = timestampMicros(now);
  if (clock === null) return occ.schedule_label;
  const start = timestampMicros(occ.est_start);
  if (start !== null && start < clock && occ.status === 'pending') return '落后';
  const nominal = timestampMicros(occ.nominal_start);
  if (start !== null && nominal !== null && start < nominal - 60000000n) return '前进';
  return '正常';
}

function mergeFields(occ, committed, now) {
  const fields = Object.fromEntries(COMMITTED_FIELDS.filter((key) => Object.hasOwn(committed, key))
    .map((key) => [key, committed[key]]));
  const merged = { ...occ, ...fields, time_updating: !!committed.time_updating };
  merged.schedule_label = scheduleLabel(merged, now);
  const changed = COMMITTED_FIELDS.some((key) => !sameValue(key, merged[key], occ[key]))
    || merged.schedule_label !== occ.schedule_label || !!merged.time_updating !== !!occ.time_updating;
  return changed ? merged : occ;
}

export function mergeOrderReceipt(board, ids, result, expectedBoard, priorOrderBoard = null) {
  if (result?.replayed || !expectedBoard) return board;
  const source = board || expectedBoard;
  const positions = new Map(ids.map((id, index) => [id, index]));
  if (source.progress.length !== positions.size
      || source.progress.some((occ) => !positions.has(occ.id))) return board;
  const estimates = new Map((result?.estimates || []).map((occ) => [occ.id, occ]));
  const committed = new Map(expectedBoard.progress.map((occ) => {
    const estimate = result?.schedule_applied ? estimates.get(occ.id) : null;
    const fields = estimate ? Object.fromEntries(ESTIMATE_FIELDS.filter((key) => Object.hasOwn(estimate, key))
      .map((key) => [key, estimate[key]])) : {};
    return [occ.id, { ...occ, ...fields, sort_order: positions.get(occ.id),
      time_updating: result?.recompute_enabled !== false && !result?.schedule_applied }];
  }));
  const expected = new Map(expectedBoard.progress.map((occ) => [occ.id, occ]));
  const prior = new Map((priorOrderBoard?.progress || []).map((occ) => [occ.id, occ]));
  if (source.progress.some((occ) => !matchesSnapshot(occ, expected.get(occ.id), committed.get(occ.id))
      && !matchesSnapshot(occ, prior.get(occ.id), committed.get(occ.id)))) return board;

  // Order/time and the automatic switch have independent authority. A read
  // matching this receipt is compatible; a contradictory newer switch survives.
  const currentEnabled = source.recompute?.enabled;
  const incomingEnabled = result?.recompute_enabled;
  const canApplySwitch = !board || currentEnabled === expectedBoard.recompute?.enabled
    || currentEnabled === incomingEnabled;
  const recompute = { ...source.recompute };
  if (canApplySwitch) {
    if (typeof incomingEnabled === 'boolean') recompute.enabled = incomingEnabled;
    const priorMark = expectedBoard.recompute || {};
    const sameMark = ['pending', 'requested_at', 'request_token', 'reason']
      .every((key) => sameValue(key, recompute[key], priorMark[key]));
    if ((sameMark && result?.schedule_applied) || incomingEnabled === false) {
      Object.assign(recompute, { pending: false, requested_at: null,
        request_token: null, wait_minutes: null, reason: null });
    }
  }
  return { ...source, recompute,
    conflicts: result?.schedule_applied ? [] : source.conflicts,
    progress: source.progress.map((occ) => mergeFields(occ, committed.get(occ.id), source.now || expectedBoard.now))
      .sort((a, b) => Number(!!a.is_parked) - Number(!!b.is_parked) || a.sort_order - b.sort_order),
  };
}

// A filtered/history list keeps its members and row order. Only compatible
// scheduling fields of the same occurrences are synchronized from the board.
export function mergeOccurrencesFromBoard(occurrences, board, previousBoard) {
  if (!board || !previousBoard) return occurrences;
  const latest = new Map(board.progress.map((occ) => [occ.id, occ]));
  const before = new Map(previousBoard.progress.map((occ) => [occ.id, occ]));
  const updated = occurrences.map((occ) => {
    const committed = latest.get(occ.id);
    return committed && matchesSnapshot(occ, before.get(occ.id), committed)
      ? mergeFields(occ, committed, board.now || previousBoard.now) : occ;
  });
  return updated.some((occ, index) => occ !== occurrences[index]) ? updated : occurrences;
}
