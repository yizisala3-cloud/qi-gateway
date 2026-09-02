// pages/_memory_patch.js - 编辑 patch 与时间处理纯函数
// 无 DOM、无 import，可被 Node 直接执行测试。
//
// 时间语义（与浏览器/服务器系统时区无关）：
// - 带明确偏移（±HH:MM 或 Z）的时间按其偏移解释；
// - 无时区的墙上时钟一律按项目固定的 Asia/Shanghai（+08:00）解释；
// - date-only 是上海自然日；
// - datetime-local 输入值提交时规范化为带 +08:00 的 ISO 8601。

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

const SHANGHAI_OFFSET_MS = 8 * 3600 * 1000;

/** 任意存储形态 → epoch 毫秒；无时区字符串按 Asia/Shanghai，与运行环境时区无关。 */
export function parseInstant(value) {
  if (value === null || value === undefined || value === '') return NaN;
  const text = String(value).trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
    return Date.parse(`${text}T00:00:00+08:00`);
  }
  const naive = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)$/);
  if (naive) {
    return Date.parse(`${naive[1]}T${naive[2]}+08:00`);
  }
  return Date.parse(text);
}

function epochToShanghaiLocalInput(epoch) {
  const shifted = new Date(epoch + SHANGHAI_OFFSET_MS);
  const pad = (n) => String(n).padStart(2, '0');
  return `${shifted.getUTCFullYear()}-${pad(shifted.getUTCMonth() + 1)}-${pad(shifted.getUTCDate())}T${pad(shifted.getUTCHours())}:${pad(shifted.getUTCMinutes())}`;
}

/** 数据库存储值 → datetime-local 显示值（Asia/Shanghai 墙上时钟）。 */
export function toDatetimeLocal(value) {
  if (!value) return '';
  const text = String(value).trim();
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return `${text}T00:00`;
  const naive = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?)$/);
  if (naive) {
    // 无时区存储值本身就是上海墙上时钟，取墙上时钟即可，绝不做时区换算。
    return `${naive[1]}T${naive[2].slice(0, 5)}`;
  }
  const epoch = Date.parse(text);
  if (Number.isNaN(epoch)) return '';
  return epochToShanghaiLocalInput(epoch);
}

/** datetime-local 输入值 → 项目规范化存储值（+08:00 明确偏移）；空输入 → null。 */
export function fromDatetimeLocal(input) {
  if (!input) return null;
  const m = String(input).trim().match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
  if (!m) return null;
  return `${m[1]}T${m[2]}:00+08:00`;
}

/** 同一分钟语义即视为未修改（输入控件粒度是分钟）。 */
export function isSameMinute(a, b) {
  const ia = parseInstant(a);
  const ib = parseInstant(b);
  if (Number.isNaN(ia) || Number.isNaN(ib)) return String(a ?? '') === String(b ?? '');
  return Math.floor(ia / 60000) === Math.floor(ib / 60000);
}

/** 时间字段未修改判断：双空相等；单空视为修改；非空按分钟语义比较。 */
export function timeValueUnchanged(stored, inputValue) {
  const hasStored = stored !== undefined && stored !== null && stored !== '';
  const hasInput = inputValue !== undefined && inputValue !== null && inputValue !== '';
  if (!hasStored && !hasInput) return true;
  if (!hasStored || !hasInput) return false;
  return isSameMinute(stored, inputValue);
}

/**
 * 由输入数据与原 continuity_data 构造提交用的结构：
 * - 时间字段未修改 → 原样保留存储值（绝不改写、不丢时区含义）；
 * - 真正修改 → 规范化为 +08:00 的 ISO 8601；
 * - 清空可选时间 → 该字段被明确删除（键不出现）。
 * timeKeys: 该类型中时间字段的键名列表。
 */
export function mergeContinuityForSubmit(data, stored, timeKeys) {
  const result = {};
  const keys = new Set([
    ...Object.keys(data || {}),
    ...Object.keys(stored || {}),
  ]);
  for (const key of keys) {
    const isTime = timeKeys.includes(key);
    const inputValue = data ? data[key] : undefined;
    const storedValue = stored ? stored[key] : undefined;
    if (isTime) {
      if (timeValueUnchanged(storedValue, inputValue)) {
        if (storedValue !== undefined && storedValue !== null && storedValue !== '') {
          result[key] = storedValue;
        }
      } else if (inputValue) {
        result[key] = fromDatetimeLocal(inputValue);
      }
      // 真正清空：键不写入，提交后该可选字段被删除。
    } else if (inputValue !== undefined && inputValue !== null && inputValue !== ''
      && !(Array.isArray(inputValue) && inputValue.length === 0)) {
      result[key] = inputValue;
    }
  }
  return result;
}

/** continuity_data 语义比较：时间字段按分钟语义，其余按规范化 JSON。 */
export function continuityEquals(newData, stored, timeKeys) {
  const keys = new Set([
    ...Object.keys(newData || {}),
    ...Object.keys(stored || {}),
  ]);
  for (const key of keys) {
    if (timeKeys.includes(key)) {
      if (!timeValueUnchanged(
        stored ? stored[key] : undefined,
        newData ? newData[key] : undefined,
      )) return false;
    } else if (stableJson(newData ? newData[key] : undefined)
      !== stableJson(stored ? stored[key] : undefined)) {
      return false;
    }
  }
  return true;
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
    ? (!memory.memory_time || !isSameMinute(values.memoryTime, memory.memory_time))
    : Boolean(memory.memory_time);
  const precisionChanged = (values.precision || null) !== (memory.time_precision || null);
  if (timeChanged) {
    patch.memory_time = values.memoryTime ? fromDatetimeLocal(values.memoryTime) : null;
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
