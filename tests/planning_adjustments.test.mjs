// boundary 多项冲突调整的合并语义（批次 9 UI #2 修复）：node 运行的纯逻辑测试。
// 场景：dry-run 返回多个冲突 → 逐个修正 → 已修正者从下一次 dry-run 响应中
// 消失 → 重绘后 DOM 不再含它的行 → 最终 payload 必须保留全部调整。
import assert from 'node:assert/strict';
import {
  mergeBoundaryAdjustments, rememberedAdjustment,
} from './planning_adjustments.mjs';

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

const adj = (id, start, end) => ({ task_id: id, window_start_tod: start, window_end_tod: end });

test('多项冲突依次修正：已修正者从后续 DOM 消失后仍保留在集合中', () => {
  // 第一轮：dry-run 返回 A、B、C；用户修正 A（A'）→ collect
  let state = [];
  state = mergeBoundaryAdjustments(state, [adj(11, '13:00', '15:00')]);
  assert.deepEqual(state, [adj(11, '13:00', '15:00')]);
  // 第二轮：服务端只返回 B、C（A 已合法）；重绘后 DOM 只有 B、C 两行
  // （A 行被移除）——collect 只拿到 B'，A' 必须原地保留
  state = mergeBoundaryAdjustments(state, [adj(22, '09:30', '11:00')]);
  assert.deepEqual(state, [adj(11, '13:00', '15:00'), adj(22, '09:30', '11:00')]);
  // 第三轮：只剩 C；collect 拿到 C'——A'、B' 都不丢
  state = mergeBoundaryAdjustments(state, [adj(33, null, '23:30')]);
  assert.deepEqual(state, [
    adj(11, '13:00', '15:00'), adj(22, '09:30', '11:00'), adj(33, null, '23:30'),
  ]);
});

test('再次修改同一待办只覆盖它自己的旧调整', () => {
  let state = [adj(11, '13:00', '15:00'), adj(22, '09:30', '11:00')];
  state = mergeBoundaryAdjustments(state, [adj(11, '14:00', '16:00')]);
  assert.deepEqual(state, [adj(11, '14:00', '16:00'), adj(22, '09:30', '11:00')]);
});

test('单端清除（null）同值保留、同 task 覆盖', () => {
  let state = mergeBoundaryAdjustments([], [adj(7, null, '23:00')]);
  state = mergeBoundaryAdjustments(state, [adj(7, null, null)]);
  assert.deepEqual(state, [adj(7, null, null)]);
});

test('空集合与缺省参数安全', () => {
  assert.deepEqual(mergeBoundaryAdjustments([], []), []);
  assert.deepEqual(mergeBoundaryAdjustments(undefined, [adj(1, '01:00', '02:00')]),
    [adj(1, '01:00', '02:00')]);
  assert.deepEqual(mergeBoundaryAdjustments([adj(1, '01:00', '02:00')], undefined),
    [adj(1, '01:00', '02:00')]);
});

test('rememberedAdjustment：重绘冲突行时恢复已录入值', () => {
  const state = [adj(11, '13:00', '15:00')];
  assert.deepEqual(rememberedAdjustment(state, 11), adj(11, '13:00', '15:00'));
  assert.equal(rememberedAdjustment(state, 99), null);
  assert.equal(rememberedAdjustment(undefined, 11), null);
});

for (const line of results) console.log(line);
