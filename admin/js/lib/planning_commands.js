import { gw } from '../api.js?v=20261025-console-latency-r1';

// Keep an uncertain command's identity across dialogs and page remounts.
const attempts = new Map();
export function pendingOccurrenceCommand(id, status) {
  const attempt = attempts.get(`${id}:${status}`);
  return attempt ? JSON.parse(attempt.intent) : null;
}
export async function sendOccurrenceCommand(id, status, values = {}) {
  const slot = `${id}:${status}`;
  const intent = JSON.stringify({ status, ...values });
  let attempt = attempts.get(slot);
  if (attempt && attempt.intent !== intent) {
    throw new Error('上次操作结果未确认，请先用原内容重试核实');
  }
  if (!attempt) {
    attempt = { intent, key: crypto.randomUUID() };
    attempts.set(slot, attempt);
  }
  try {
    const result = await gw(`/admin/api/planning/occurrences/${id}/command`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, ...values, request_key: attempt.key }),
    });
    attempts.delete(slot);
    return result;
  } catch (error) {
    if (error.status && error.status < 500 && !error.resultUnknown) attempts.delete(slot);
    else {
      error.resultUnknown = true;
      error.message = '操作结果未确认，请用原内容重试以核实同一次操作';
    }
    throw error;
  }
}
