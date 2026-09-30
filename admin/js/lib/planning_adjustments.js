// lib/planning_adjustments.js - boundary 修改流程的关联调整集合合并（纯逻辑）
// 批次 9 UI 修复：boundary dry-run 只返回**仍未通过**的冲突行（已修正者
// 不再出现），冲突弹窗按响应整体重绘后 DOM 里不再含已通过行——若每次都
// 从 DOM 重新收集，前一轮刚录入的调整就会整体丢失。用稳定 task_id → 调整
// 映射合并：新收集的覆盖同 task 旧值（同一待办再次修改只覆盖它自己），
// 其余原样保留；payload 与 DOM 渲染次序无关。

/**
 * 合并已录入的调整集合与新收集的调整集合。
 * @param {Array<{task_id:number, window_start_tod:string|null, window_end_tod:string|null}>} existing
 * @param {Array<{task_id:number, window_start_tod:string|null, window_end_tod:string|null}>} collected
 * @returns {Array} 合并后的调整数组（task_id 首次出现次序保持稳定）
 */
export function mergeBoundaryAdjustments(existing, collected) {
  const map = new Map();
  for (const item of existing || []) map.set(item.task_id, item);
  for (const item of collected || []) map.set(item.task_id, item);
  return [...map.values()];
}

/**
 * 在已录入集合中查找某待办的调整值（重绘冲突行时恢复用户刚录入的值）。
 */
export function rememberedAdjustment(existing, taskId) {
  return (existing || []).find((item) => item.task_id === taskId) || null;
}
