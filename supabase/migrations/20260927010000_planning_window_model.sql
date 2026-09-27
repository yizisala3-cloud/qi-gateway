-- 待办时间窗口模型（2026-09-27「待办时间窗口模型与表单重构」批次 2）：
-- 可安排时段（最早开始 / 最晚完成；一期规范 §6.7 / §18）取代显式起止、
-- 独立固定待办与限时截止。本迁移**纯增量**：只新增列与列级形状 CHECK。
--
-- 零改写承诺（施工计划 §3.2 / §3.4）：
-- * 既有 1A 身份 CHECK / 触发器、1B 请求守卫、既有索引一律不动；
-- * 旧 explicit / deadline 列（est_start_tod / est_end_tod / deadline_tod /
--   deadline_end_tod / 任务级 is_fixed / is_limited / deadline_at）全部不
--   DROP——存量 deadline / is_limited 行按历史语义走完生命周期（不变量
--   33），退役方式是应用层停止写入与判定（批次 3/6），本迁移不改写任何
--   既有数据行；
-- * 新窗口行 is_limited=false、deadline_at=null、estimated_time_source=
--   'unassigned'（或零自由度预锚定的 rule 形状）、est 成对为空——天然
--   满足 1A 身份 CHECK，所有权约束零改动。
--
-- 校验边界（施工计划 §3.2）：窗口是否跨越每日刷新 boundary、剩余空间
-- 是否容纳占用跨度，依赖动态配置与实例快照，无法进静态 CHECK——由应用
-- 层共享领域函数（gateway/planning_window.py，批次 1）在创建 / 编辑 /
-- 排程路径统一校验；本迁移只固化数据库层「形状」不变量，不建任何触发器。
-- boundary 原子 RPC（planning_update_cycle_boundary）属批次 7，本批不含。
--
-- 已生成实例的冻结窗口、快照与身份不受本迁移影响（§28.1、不变量 31/36）。
-- 迁移一次性、原子（begin/commit）；列用 if not exists、约束经
-- pg_constraint 存在性判断条件添加，重复执行安全。
begin;

-- ── 模板窗口（任务层权威，施工计划 §2.3）──────────────────────────
-- 一对可选的当日时刻（分钟精度，业务本地，无时区——与 est_start_tod 同
-- 惯例）。两列各自独立可空，支持 §18 四种组合（双端 / 只有最早开始 /
-- 只有最晚完成 / 两端皆空）；双端允许 end < start（结束在次日的跨自然
-- 午夜写法）；不得要求成对 NULL / 非 NULL。
alter table public.planning_task
    add column if not exists window_start_tod time,
    add column if not exists window_end_tod time;

-- 双端同时非空时要求 start <> end（start == end 不解释为 24h 窗口，
-- §6.7）；任一端为空即放行（单侧约束合法，且不构成可跨越的区间）。
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'planning_task_window_tod_shape_check'
          and conrelid = 'public.planning_task'::regclass
    ) then
        alter table public.planning_task
            add constraint planning_task_window_tod_shape_check
            check (
                window_start_tod is null
                or window_end_tod is null
                or window_start_tod <> window_end_tod
            );
    end if;
end $$;

-- ── 实例窗口（生成时解析冻结的绝对区间，§6.7「生成即冻结」）────────
-- 一对可选的绝对时刻。两列各自独立可空（§18 四种组合；单侧冻结后只有
-- 一端）；NULL = 无该端约束。窗口一经写入即冻结：顺延、展示周期变化、
-- 模板后续修改均不改写（不变量 36）——该写入纪律由应用层承担（批次
-- 3/6），数据库层不加触发器。超时判定换源（window_end_at）属批次 5。
alter table public.planning_occurrence
    add column if not exists window_start_at timestamptz,
    add column if not exists window_end_at timestamptz;

-- 双端同时非空时要求 end > start（绝对瞬间先后，跨偏移比较对
-- timestamptz 天然成立）；任一端为空即放行。既有 1A/1B 行窗口列为 NULL，
-- 本 CHECK 对存量数据恒放行，零回归。
do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'planning_occurrence_window_at_order_check'
          and conrelid = 'public.planning_occurrence'::regclass
    ) then
        alter table public.planning_occurrence
            add constraint planning_occurrence_window_at_order_check
            check (
                window_start_at is null
                or window_end_at is null
                or window_end_at > window_start_at
            );
    end if;
end $$;

comment on column public.planning_task.window_start_tod is
    'Schedulable-window template start (earliest start), business-local minute-of-day time; independently nullable; end < start means the window ends on the following day (crossing natural midnight is legal, equal endpoints are not).';
comment on column public.planning_task.window_end_tod is
    'Schedulable-window template end (latest completion), business-local minute-of-day time; independently nullable; with both endpoints present start must differ from end (never interpreted as a 24h window).';
comment on column public.planning_occurrence.window_start_at is
    'Frozen absolute window start resolved from the template when the round is generated; independently nullable; never rewritten by carryover, display-cycle changes or later template edits.';
comment on column public.planning_occurrence.window_end_at is
    'Frozen absolute window end (latest completion) resolved at generation; independently nullable; unified timeout source per requirements 18.2/22.5 (wiring lands in later batches).';

commit;
