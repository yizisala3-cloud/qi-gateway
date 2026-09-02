// buildEditPatch 纯逻辑测试：node --input-type=module 运行。
// 覆盖：只改精度 / 只改时间 / 同时修改 / 清空时间 / 无变化不产生 patch。
import assert from 'node:assert/strict';
import { buildEditPatch, sameInstant, stableJson } from '../admin/js/pages/_memory_patch.js';

const BASE_MEMORY = {
  title: '标题',
  content: '这是一条用于编辑逻辑测试的记忆正文。',
  tags: ['标签'],
  importance: 5,
  source_type: 'natural_chat',
  // 无时区的本地时间：与 datetime-local 值逐字对应，测试随时区保持确定。
  memory_time: '2026-09-01 10:30',
  time_precision: 'minute',
  recall_scene: '聊到测试时',
  recall_tags: ['测试'],
  evidence_message_ids: [1, 2],
};

const BASE_VALUES = {
  title: '标题',
  content: '这是一条用于编辑逻辑测试的记忆正文。',
  tags: ['标签'],
  importance: 5,
  sourceType: 'natural_chat',
  memoryTime: '2026-09-01T10:30',
  precision: 'minute',
  recallScene: '聊到测试时',
  recallTags: ['测试'],
  evidenceIds: ['1', '2'],
};

function values(overrides = {}) {
  return { ...BASE_VALUES, ...overrides };
}

const results = [];
function test(name, fn) {
  try {
    fn();
    results.push(`PASS ${name}`);
  } catch (error) {
    results.push(`FAIL ${name}: ${error.message}`);
    process.exitCode = 1;
  }
}

test('只改 time_precision 也必须提交', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ precision: 'day' }));
  assert.deepEqual(Object.keys(patch), ['time_precision']);
  assert.equal(patch.time_precision, 'day');
});

test('只改 memory_time 正常提交且不带未变化的精度', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ memoryTime: '2026-09-02T08:00' }));
  assert.deepEqual(Object.keys(patch), ['memory_time']);
  assert.equal(patch.memory_time, '2026-09-02T08:00');
});

test('同时修改时间与精度', () => {
  const patch = buildEditPatch(
    BASE_MEMORY,
    values({ memoryTime: '2026-09-02T08:00', precision: 'approximate' }),
  );
  assert.equal(patch.memory_time, '2026-09-02T08:00');
  assert.equal(patch.time_precision, 'approximate');
});

test('清空 memory_time 时精度保存为 unknown，不伪造 minute/hour/day', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ memoryTime: '', precision: 'minute' }));
  assert.equal(patch.memory_time, null);
  assert.equal(patch.time_precision, 'unknown');
  const patch2 = buildEditPatch(BASE_MEMORY, values({ memoryTime: '', precision: 'day' }));
  assert.equal(patch2.time_precision, 'unknown');
});

test('原本无时间时选择 minute/hour/day 也会被归一化为 unknown', () => {
  const noTime = { ...BASE_MEMORY, memory_time: null, time_precision: 'unknown' };
  const patch = buildEditPatch(noTime, values({ memoryTime: '', precision: 'minute' }));
  assert.deepEqual(Object.keys(patch), ['time_precision']);
  assert.equal(patch.time_precision, 'unknown');
});

test('时间与精度均未变化时不产生 patch', () => {
  // 同一时刻（本地表示 vs UTC 存储）不算变化。
  const patch = buildEditPatch(BASE_MEMORY, values());
  assert.deepEqual(patch, {});
});

test('清空记忆时间且原来就有时间 → memory_time=null', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ memoryTime: '' }));
  assert.equal(patch.memory_time, null);
  assert.equal(patch.time_precision, 'unknown');
});

test('同一时刻不同时区表示不算修改', () => {
  assert.ok(sameInstant('2026-09-01T18:30', '2026-09-01T10:30:00+00:00'));
  assert.ok(!sameInstant('2026-09-01T10:30', '2026-09-01T10:30:00+00:00'));
});

test('普通字段只提交变化项', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({
    title: '新标题',
    content: BASE_MEMORY.content,
    tags: ['标签', '新增'],
    recallTags: ['测试'],
    evidenceIds: ['1', '2'],
  }));
  assert.deepEqual(Object.keys(patch), ['title', 'tags']);
});

test('stableJson 对数组与对象做顺序无关比较', () => {
  assert.equal(stableJson(['b', 'a']), stableJson(['a', 'b']));
  assert.equal(stableJson({ x: 1, y: 2 }), stableJson({ y: 2, x: 1 }));
  assert.notEqual(stableJson(['a']), stableJson(['a', 'b']));
});

if (process.exitCode) {
  console.error(results.join('\n'));
} else {
  console.log(results.join('\n'));
}
