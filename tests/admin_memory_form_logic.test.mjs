// buildEditPatch 与时间处理纯逻辑测试：node 运行，结果只依赖显式时区语义，
// 与运行环境的系统时区无关（pytest 包装器以 TZ=UTC 和 TZ=Asia/Shanghai 各跑一次）。
import assert from 'node:assert/strict';
import {
  buildEditPatch, toDatetimeLocal, fromDatetimeLocal, stableJson,
  isSameMinute, parseInstant, mergeContinuityForSubmit, continuityEquals,
  nowShanghaiLocalInput,
} from '../admin/js/pages/_memory_patch.js';

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

/* ---------- 存储值 ↔ 显示值（固定 Asia/Shanghai 语义） ---------- */

test('数据库值 2026-08-19T03:11:00+08:00 显示为 2026-08-19 03:11', () => {
  assert.equal(toDatetimeLocal('2026-08-19T03:11:00+08:00'), '2026-08-19T03:11');
});

test('UTC 序列化的 timestamptz 显示为上海墙上时钟 03:11（不随浏览器时区偏移）', () => {
  assert.equal(toDatetimeLocal('2026-08-18T19:11:00+00:00'), '2026-08-19T03:11');
  assert.equal(toDatetimeLocal('2026-08-18T19:11:00Z'), '2026-08-19T03:11');
});

test('datetime-local 输入值规范化为 +08:00 的 ISO 8601', () => {
  assert.equal(fromDatetimeLocal('2026-08-19T04:20'), '2026-08-19T04:20:00+08:00');
  assert.equal(fromDatetimeLocal(''), null);
  assert.equal(fromDatetimeLocal(null), null);
});

test('往返：存储值 → 显示 → 规范化，保持同一时刻', () => {
  const stored = '2026-08-19T03:11:00+08:00';
  const roundTrip = fromDatetimeLocal(toDatetimeLocal(stored));
  assert.ok(isSameMinute(stored, roundTrip));
});

test('isSameMinute 跨格式判定同一分钟', () => {
  assert.ok(isSameMinute('2026-08-19T03:11', '2026-08-19T03:11:00+08:00'));
  assert.ok(isSameMinute('2026-08-19 03:11', '2026-08-19T03:11:00+08:00'));
  assert.ok(!isSameMinute('2026-08-19T03:11', '2026-08-19T04:20:00+08:00'));
  assert.ok(isSameMinute(null, ''));
  assert.ok(!isSameMinute(null, '2026-08-19T03:11:00+08:00'));
});

test('nowShanghaiLocalInput 返回上海墙上时钟的 datetime-local 形态', () => {
  const now = nowShanghaiLocalInput();
  assert.match(now, /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/);
  // 分钟粒度：与当前时刻差不超过 1 分钟
  const diff = Math.abs(parseInstant(now) - Date.now());
  assert.ok(diff <= 60000, `now offset too large: ${diff}ms`);
});

test('parseInstant 对无时区字符串按 Asia/Shanghai 解释（与运行环境时区无关）', () => {
  assert.equal(parseInstant('2026-08-19T03:11'), parseInstant('2026-08-19T03:11:00+08:00'));
  assert.equal(parseInstant('2026-08-19 03:11:00'), parseInstant('2026-08-19T03:11:00+08:00'));
  assert.equal(parseInstant('2026-08-19'), parseInstant('2026-08-19T00:00:00+08:00'));
});

/* ---------- buildEditPatch：时间与精度独立比较 ---------- */

const BASE_MEMORY = {
  title: '标题',
  content: '这是一条用于编辑逻辑测试的记忆正文。',
  tags: ['标签'],
  importance: 5,
  source_type: 'natural_chat',
  memory_time: '2026-08-19T03:11:00+08:00',
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
  // 数据库值 03:11(+08:00) 的 datetime-local 表示：不改动 = 未修改。
  memoryTime: '2026-08-19T03:11',
  precision: 'minute',
  recallScene: '聊到测试时',
  recallTags: ['测试'],
  evidenceIds: ['1', '2'],
};

function values(overrides = {}) {
  return { ...BASE_VALUES, ...overrides };
}

test('打开表单未改动时间时，不产生 memory_time/precision patch', () => {
  const patch = buildEditPatch(BASE_MEMORY, values());
  assert.equal(patch.memory_time, undefined);
  assert.equal(patch.time_precision, undefined);
});

test('只改 time_precision 也必须提交', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ precision: 'day' }));
  assert.deepEqual(Object.keys(patch), ['time_precision']);
  assert.equal(patch.time_precision, 'day');
});

test('只改 memory_time 提交规范化 +08:00 值，不带未变化的精度', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ memoryTime: '2026-08-19T04:20' }));
  assert.deepEqual(Object.keys(patch), ['memory_time']);
  assert.equal(patch.memory_time, '2026-08-19T04:20:00+08:00');
});

test('同时修改时间与精度', () => {
  const patch = buildEditPatch(
    BASE_MEMORY,
    values({ memoryTime: '2026-08-19T04:20', precision: 'approximate' }),
  );
  assert.equal(patch.memory_time, '2026-08-19T04:20:00+08:00');
  assert.equal(patch.time_precision, 'approximate');
});

test('清空 memory_time 时精度保存为 unknown', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ memoryTime: '' }));
  assert.equal(patch.memory_time, null);
  assert.equal(patch.time_precision, 'unknown');
});

test('原本无时间时选择 minute/approximate 都归一为 unknown', () => {
  const noTime = { ...BASE_MEMORY, memory_time: null, time_precision: 'unknown' };
  const patch = buildEditPatch(noTime, values({ memoryTime: '', precision: 'minute' }));
  assert.deepEqual(Object.keys(patch), ['time_precision']);
  assert.equal(patch.time_precision, 'unknown');
  const patch2 = buildEditPatch(noTime, values({ memoryTime: '', precision: 'approximate' }));
  assert.equal(patch2.time_precision, 'unknown');
});

test('时间与精度均未变化时不产生时间 patch', () => {
  const patch = buildEditPatch(BASE_MEMORY, values());
  assert.deepEqual(patch, {});
});

test('仅修改标题：不产生任何时间或结构键', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({ title: '新标题' }));
  assert.deepEqual(Object.keys(patch), ['title']);
});

test('普通字段只提交变化项', () => {
  const patch = buildEditPatch(BASE_MEMORY, values({
    title: '新标题',
    tags: ['标签', '新增'],
  }));
  assert.deepEqual(Object.keys(patch), ['title', 'tags']);
});

/* ---------- continuity_data 时间字段：语义比较与合并 ---------- */

const THREAD_STORED = {
  open_question: '下周三赶海是否成行',
  current_state: '已约定待确认天气',
  opened_at: '2026-08-19T03:11:00+08:00',
  closed_at: '',
  abstract_retrieval_hints: ['赶海'],
};
const THREAD_TIME_KEYS = ['opened_at', 'closed_at'];

function threadInput(overrides = {}) {
  return {
    open_question: '下周三赶海是否成行',
    current_state: '已约定待确认天气',
    opened_at: '2026-08-19T03:11',
    closed_at: '',
    abstract_retrieval_hints: ['赶海'],
    ...overrides,
  };
}

test('打开表单不改 opened_at：语义比较判定未修改', () => {
  const merged = mergeContinuityForSubmit(threadInput(), THREAD_STORED, THREAD_TIME_KEYS);
  assert.ok(continuityEquals(merged, THREAD_STORED, THREAD_TIME_KEYS));
});

test('未改动的时间字段原样保留存储值（不改写、不丢时区含义）', () => {
  const merged = mergeContinuityForSubmit(threadInput(), THREAD_STORED, THREAD_TIME_KEYS);
  assert.equal(merged.opened_at, THREAD_STORED.opened_at);
});

test('真正把 opened_at 从 03:11 改成 04:20：提交规范化 +08:00 值', () => {
  const merged = mergeContinuityForSubmit(
    threadInput({ opened_at: '2026-08-19T04:20' }), THREAD_STORED, THREAD_TIME_KEYS);
  assert.equal(merged.opened_at, '2026-08-19T04:20:00+08:00');
  assert.ok(!continuityEquals(merged, THREAD_STORED, THREAD_TIME_KEYS));
});

test('清空 opened_at：该可选字段被明确删除', () => {
  const merged = mergeContinuityForSubmit(threadInput({ opened_at: '' }), THREAD_STORED, THREAD_TIME_KEYS);
  assert.ok(!('opened_at' in merged));
  assert.ok(!continuityEquals(merged, THREAD_STORED, THREAD_TIME_KEYS));
});

test('episode/inside_joke/profile/interaction_rule 的时间字段同样语义合并', () => {
  const episodeKeys = ['episode_start_time', 'episode_end_time'];
  const episodeStored = { beginning: '开端', development: '经过', outcome: '结局',
    closure_quality: 'complete', episode_start_time: '2026-08-19T05:00:00+08:00' };
  const episodeInput = { beginning: '开端', development: '经过', outcome: '结局',
    closure_quality: 'complete', episode_start_time: '2026-08-19T05:00' };
  const episodeMerged = mergeContinuityForSubmit(episodeInput, episodeStored, episodeKeys);
  assert.ok(continuityEquals(episodeMerged, episodeStored, episodeKeys), 'episode 未改动时间不得误判');
  assert.equal(episodeMerged.episode_start_time, episodeStored.episode_start_time);

  const jokeKeys = ['first_seen_at', 'last_reinforced_at'];
  const jokeStored = { origin: '来历', trigger_phrases: ['贝壳'], shared_meaning: '含义',
    first_seen_at: '2026-08-01T12:00:00+08:00' };
  const jokeMerged = mergeContinuityForSubmit(
    { origin: '来历', trigger_phrases: ['贝壳'], shared_meaning: '含义', first_seen_at: '' },
    jokeStored, jokeKeys);
  assert.ok(!('first_seen_at' in jokeMerged), 'inside_joke 清空时间删除字段');

  const profileKeys = ['effective_from', 'effective_until'];
  const profileStored = { facet: '作息', statement: '晚睡', scope: '全局',
    stability: 'stable', basis: 'explicit_self_report',
    effective_from: '2026-08-01T01:00:00+08:00' };
  const profileInput = { facet: '作息', statement: '晚睡', scope: '全局',
    stability: 'stable', basis: 'explicit_self_report', effective_from: '2026-08-01T01:00' };
  assert.ok(continuityEquals(
    mergeContinuityForSubmit(profileInput, profileStored, profileKeys),
    profileStored, profileKeys), 'profile 未改动时间不得误判');

  const ruleKeys = ['effective_from', 'effective_until'];
  const ruleStored = { trigger: '赶海', expected_behavior: '防晒', scope: '全局',
    priority: 5, rule_state: 'active', explicit_instruction: '提醒防晒',
    effective_until: '2026-09-01T23:59:00+08:00' };
  const ruleInput = { trigger: '赶海', expected_behavior: '防晒', scope: '全局',
    priority: 5, rule_state: 'active', explicit_instruction: '提醒防晒',
    effective_until: '2026-09-01T23:59' };
  assert.ok(continuityEquals(
    mergeContinuityForSubmit(ruleInput, ruleStored, ruleKeys),
    ruleStored, ruleKeys), 'interaction_rule 未改动时间不得误判');
});

test('thread_state 或非时间字段真正变化时判定为已修改', () => {
  const merged = mergeContinuityForSubmit(
    threadInput({ current_state: '天气确认完毕' }), THREAD_STORED, THREAD_TIME_KEYS);
  assert.ok(!continuityEquals(merged, THREAD_STORED, THREAD_TIME_KEYS));
});

/* ---------- 其他 ---------- */

test('stableJson 对数组与对象做顺序无关比较', () => {
  assert.equal(stableJson(['b', 'a']), stableJson(['a', 'b']));
  assert.equal(stableJson({ x: 1, y: 2 }), stableJson({ y: 2, x: 1 }));
});

if (process.exitCode) {
  console.error(results.join('\n'));
} else {
  console.log(results.join('\n'));
}
