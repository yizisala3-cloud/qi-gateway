-- 固定刷新型到期死亡边界的实例级冻结（2026-09-28「待办时间窗口模型与表单
-- 重构」批次 5 三轮 Review 裁决）：
--
-- 正式不变量（一期规范 §28.1 / §32.31「已生成实例冻结」）：task recurrence
-- rule（interval_days / weekdays / month_days）后续修改只影响未来尚未生成
-- 的实例，不得追溯重解释已生成轮次的生命周期。固定刷新型「到达下一规则点
-- 死亡」（§8.4）的死亡边界 = 轴上晚于本轮 due 的下一个规则事件——该事实
-- 此前从未落库，事件轴又只存在于 task 当前规则，规则一经编辑便无法唯一
-- 恢复。本迁移为生成入口提供冻结载体：新实例生成时按**生成当时的规则**
-- 计算下一规则点并随行一次性冻结（与 window / planned_minutes /
-- content_snapshot 同一「生成即冻结」模式），此后两个关闭入口（固定到期
-- 清理与窗口 sweep）都只读该实例自身的冻结事实，规则编辑不再影响任何
-- 已生成轮次的死亡边界。
--
-- 零改写承诺：
-- * 纯增量：只新增一列，不回填、不建触发器 / 函数 / RPC、不建 CHECK；
-- * 存量行保持 NULL = 无冻结事实：关闭入口不按 task 当前规则回算历史
--   （user 裁决：旧 planning 测试数据受控部署时直接清理，不建 backfill）；
-- * daily / once / after_completion / idle 及提前完成（source='early'）行
--   不使用固定到期死亡，保持 NULL；
-- * refresh_enabled（暂停刷新）只暂停到期清理的执行，不删除、不改写冻结值；
-- * 任务规则编辑不改写任何已生成 occurrence 的本列（occurrence 级冻结，
--   应用层无任何改写路径，测试锁定）。
--
-- 迁移一次性、原子（begin/commit）；列用 if not exists，重复执行安全。
begin;

-- 该固定轮次生成时按当时 fixed recurrence rule 确定的「下一固定规则点 /
-- 本轮固定到期死亡边界」（绝对时刻）；NULL = 非固定轴轮或存量行无冻结
-- 事实。一次 reconcile 补生成多个历史轮次时，每轮分别冻结规则序列中属于
-- 自己的下一个事件，不按扫描时刻推算；中空同轮两阶段共享同一值。
alter table public.planning_occurrence
    add column if not exists fixed_expires_at timestamptz;

comment on column public.planning_occurrence.fixed_expires_at is
    'Fixed-round expiration boundary (the next rule event after this round''s due) frozen from the recurrence rule in effect when the round was generated; independently nullable; NULL for non-fixed rounds and legacy rows; never rewritten by later rule edits, pause or sweep.';

commit;
