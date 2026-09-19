-- 规划管理一期：独立待办体系（任务定义 / 出现实例 / 重算等待标记）。
--
-- 与需求约定对齐（唯一需求来源：前端/前端后续改动方向/规划管理-需求与一期约定.md；
-- 工程约定：后端/后端后续改动方向/规划管理-一期-后端实现要点.md）：
-- * 全新表，绝不触碰 public.todos、public.chat_messages 与任何记忆表结构。
-- * 出现实例的生成只依据任务定义上的规则游标（cursor_date / next_due），
--   绝不以出现记录的存在性为依据——这是 72 小时清理安全前提：删除
--   已废弃 / 此次废弃的出现记录不会造成重新生成或漏生成。
-- * planning_occurrence 上 (task_id, for_date, phase) 的部分唯一索引
--   （仅 source='schedule'）只用于挡生成竞态；提前完成产生的
--   source='early' 记录不受其约束。
-- * 状态机：pending / in_progress / completed / partial / deferred /
--   discarded_this / discarded / timeout。废弃 / 此次废弃出现记录满 72
--   小时由网关循环删除；已完成 / 部分完成 / 延后的记录永久保留。
-- * 时间基准：北京时间（Asia/Shanghai）；显式起止/限时截止以「当日时刻」
--   存储（est_start_tod 等），与 for_date 组合得到绝对时间，重复任务无需
--   每天改定义。单次待办 = target_date + 当日时刻。
-- * RLS：三张表全部启用但不出策略（单用户私人网关，服务端 key 绕过 RLS，
--   与 memory_continuity_objects 同惯例）。

begin;

-- ── 任务定义表 ────────────────────────────────────────────────────
create table public.planning_task (
    id bigint generated always as identity primary key,
    content text not null,
    -- daily 每日 / interval 按间隔 / weekly 按星期 / monthly 每月日期 / once 单次 / idle 闲时
    task_type text not null
        constraint planning_task_type_check
        check (task_type in ('daily', 'interval', 'weekly', 'monthly', 'once', 'idle')),
    -- 重复配置（按类型取用）
    interval_days integer
        constraint planning_task_interval_days_check
        check (interval_days is null or interval_days between 1 and 3650),
    weekdays integer[],
    month_days integer[],
    target_date date,
    -- 时间模式：duration 仅填预估耗时（可自动排程）/ explicit 显式开始结束（固定时间位）
    time_mode text not null default 'duration'
        constraint planning_task_time_mode_check
        check (time_mode in ('duration', 'explicit')),
    estimated_minutes integer
        constraint planning_task_estimated_minutes_check
        check (estimated_minutes is null or estimated_minutes between 1 and 1440),
    est_start_tod time,
    est_end_tod time,
    is_fixed boolean not null default false,
    -- 限时截止：当日时刻（单一时点或范围起点/终点）
    deadline_tod time,
    deadline_end_tod time,
    -- 中空待办：开始阶段 + 中间等待 + 结束阶段，列表拆成两个条目
    is_hollow boolean not null default false,
    hollow_start_content text,
    hollow_start_minutes integer
        constraint planning_task_hollow_start_minutes_check
        check (hollow_start_minutes is null or hollow_start_minutes between 1 and 1440),
    hollow_wait_minutes integer
        constraint planning_task_hollow_wait_minutes_check
        check (hollow_wait_minutes is null or hollow_wait_minutes between 1 and 1440),
    hollow_wait_note text,
    hollow_end_content text,
    hollow_end_minutes integer
        constraint planning_task_hollow_end_minutes_check
        check (hollow_end_minutes is null or hollow_end_minutes between 1 and 1440),
    -- 提醒关联：开始闹钟 / 结束闹钟 / 计时器时长（分钟）
    alarm_start boolean not null default false,
    alarm_end boolean not null default false,
    timer_minutes integer
        constraint planning_task_timer_minutes_check
        check (timer_minutes is null or timer_minutes between 1 and 1440),
    -- 启用状态；废弃任务（is_active=false）终止一切后续生成
    is_active boolean not null default true,
    -- 生成游标：daily/weekly/monthly/once/idle 记录已生成到的 for_date；
    -- interval 用 next_due（完成时刻 + 间隔；提前完成即重置），null 表示无到期
    cursor_date date,
    next_due timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.planning_task enable row level security;

create index planning_task_active_idx on public.planning_task (is_active);

-- ── 出现实例表 ────────────────────────────────────────────────────
create table public.planning_occurrence (
    id bigint generated always as identity primary key,
    task_id bigint not null
        constraint planning_occurrence_task_fkey
        references public.planning_task(id),
    for_date date not null,
    -- 中空待办拆分条目：start 开始阶段 / end 结束阶段；普通条目为 null
    phase text
        constraint planning_occurrence_phase_check
        check (phase in ('start', 'end')),
    -- 预估起止（可被重算更新）；实际起止（可补填修改），独立保存
    est_start timestamptz,
    est_end timestamptz,
    -- 名义开始时间：创建/用户改动（延后、重新安排、手动改时间）时刷新，
    -- 自动重算不写。仅用于「前进」展示标签。
    nominal_start timestamptz,
    actual_start timestamptz,
    actual_end timestamptz,
    actual_minutes integer
        constraint planning_occurrence_actual_minutes_check
        check (actual_minutes is null or actual_minutes between 0 and 10080),
    status text not null default 'pending'
        constraint planning_occurrence_status_check
        check (status in (
            'pending', 'in_progress', 'completed', 'partial',
            'deferred', 'discarded_this', 'discarded', 'timeout'
        )),
    partial_note text,
    sort_order integer not null default 0,
    is_fixed boolean not null default false,
    is_limited boolean not null default false,
    -- 进入关闭态的时刻；72 小时清理只看 discarded / discarded_this
    closed_at timestamptz,
    source text not null default 'schedule'
        constraint planning_occurrence_source_check
        check (source in ('schedule', 'early')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.planning_occurrence enable row level security;

-- 生成竞态保护：同一任务同一天同一阶段的排程实例唯一。
-- 提前完成（source='early'）不受此约束，可与已废弃的当日记录共存。
create unique index planning_occurrence_schedule_slot_uq
    on public.planning_occurrence (task_id, for_date, phase)
    where source = 'schedule';

create index planning_occurrence_for_date_idx on public.planning_occurrence (for_date);
create index planning_occurrence_status_idx on public.planning_occurrence (status);
create index planning_occurrence_task_idx on public.planning_occurrence (task_id);
create index planning_occurrence_closed_at_idx on public.planning_occurrence (closed_at)
    where status in ('discarded', 'discarded_this');

-- ── 重算等待标记（单行） ──────────────────────────────────────────
-- 列表顺序变化 / 有待办完成时落 requested_at；15 分钟内无手动重算则由
-- 网关循环执行一次重算；手动重算立即执行并清空标记。
create table public.planning_recompute_state (
    id smallint primary key default 1
        constraint planning_recompute_state_singleton check (id = 1),
    requested_at timestamptz,
    reason text,
    updated_at timestamptz not null default now()
);

alter table public.planning_recompute_state enable row level security;

insert into public.planning_recompute_state (id) values (1)
    on conflict (id) do nothing;

commit;
