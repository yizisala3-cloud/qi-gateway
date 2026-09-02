// pages/_memory_patch.js - 编辑 patch 纯函数（无 DOM、无 import，供 node 测试）
// 时间与精度分别比较；清空记忆时间时精度同步为 unknown，绝不给空时间
// 保留 minute/hour/day 这类伪造精度。

export function sameInstant(a, b) {
  const da = Date.parse(a);
  const db = Date.parse(b);
  if (Number.isNaN(da) || Number.isNaN(db)) return String(a || '') === String(b || '');
  return da === db;
}

export function stableJson(value) {
  if (value === null || value === undefined) return '';
  if (Array.isArray(value)) return JSON.stringify([...value].sort());
  if (typeof value === 'object') {
    return JSON.stringify(Object.keys(value).sort().map((k) => [k, value[k]]));
  }
  return JSON.stringify(value);
}

/**
 * 由表单值与原记忆行构造普通编辑 patch：只包含真正发生变化的字段。
 * values: {title, content, tags, importance, sourceType, memoryTime,
 *          precision, recallScene, recallTags, evidenceIds}
 */
export function buildEditPatch(memory, values) {
  const patch = {};
  if ((values.title || null) !== (memory.title || null)) patch.title = values.title || null;
  if (values.content !== memory.content) patch.content = values.content;
  if (stableJson(values.tags) !== stableJson(memory.tags || [])) patch.tags = values.tags;
  if (values.importance !== (Number(memory.importance) || 5)) patch.importance = values.importance;
  if ((values.sourceType || null) !== (memory.source_type || null)) patch.source_type = values.sourceType || null;

  const timeChanged = values.memoryTime
    ? (!memory.memory_time || !sameInstant(values.memoryTime, memory.memory_time))
    : Boolean(memory.memory_time);
  const precisionChanged = (values.precision || null) !== (memory.time_precision || null);
  if (timeChanged) {
    patch.memory_time = values.memoryTime || null;
    if (values.memoryTime) {
      if (precisionChanged) patch.time_precision = values.precision || null;
    } else {
      // 清空记忆时间：精度保存为 unknown，绝不保留 minute/hour/day。
      patch.time_precision = 'unknown';
    }
  } else if (precisionChanged) {
    // 没有记忆时间时不伪造 minute/hour/day。
    patch.time_precision = values.memoryTime ? (values.precision || null) : 'unknown';
  }

  if (values.recallScene !== (memory.recall_scene || '')) {
    patch.recall_scene = values.recallScene || null;
  }
  if (stableJson(values.recallTags) !== stableJson(memory.recall_tags || [])) {
    patch.recall_tags = values.recallTags;
  }
  const originalEvidence = (memory.evidence_message_ids || []).map(String).join(',');
  if (values.evidenceIds.map(String).join(',') !== originalEvidence) {
    patch.evidence_message_ids = values.evidenceIds.map(Number);
  }
  return patch;
}
