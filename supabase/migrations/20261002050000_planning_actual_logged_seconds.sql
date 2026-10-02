-- 完成耗时手填（2026-10-01 确认，需求 §12.3 / §32.46 / 清单 #19）：
-- 点「完成」时 user 可手填实际耗时；手填值存独立秒粒度字段，与
-- actual_start / actual_end / actual_minutes（自动计算的真实经过时间）
-- 并存、互不覆盖——自动值照常按 §12.2 记录，仅供「每日总结」（待建）
-- 消费，不再作为已完成记录的默认展示值。
--
-- 展示口径（前端按此派生，不落库）：已完成 / 已删除记录手填值标注
-- 「实际耗时」优先；未手填展示预估并标注「预估耗时」，历史无手填行
-- （NULL）一律按未手填口径。
begin;

alter table public.planning_occurrence
    add column if not exists actual_logged_seconds bigint;

-- 形状约束：NULL = 未手填（合法）；手填必须为正秒数且不超过 24 小时
-- （与预计耗时的 1440 分钟上界同数量级的合理性上限）。
alter table public.planning_occurrence
    add constraint planning_occurrence_actual_logged_shape
    check (actual_logged_seconds is null
           or (actual_logged_seconds > 0
               and actual_logged_seconds <= 86400));

comment on column public.planning_occurrence.actual_logged_seconds is
    'user 手填的实际耗时（秒，2026-10-01 确认 §12.3）；NULL = 未手填。'
    '与 actual_start / actual_end / actual_minutes 自动事实并存，互不覆盖。';

commit;
