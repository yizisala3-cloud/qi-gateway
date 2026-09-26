-- ============================================================
-- 00 · 备份（第一步，必须最先执行）
-- 在 Supabase SQL Editor 整个文件一次运行。
-- 生成两张带日期后缀的备份表；回滚时由此恢复。
-- ============================================================

create table public._backup_planning_task_20260927 as
select * from public.planning_task;

create table public._backup_planning_occurrence_20260927 as
select * from public.planning_occurrence;

create table public._backup_planning_recompute_state_20260927 as
select * from public.planning_recompute_state;

-- 备份结果确认：三个数字应分别为 4 / 一万二千余 / 1
select 'task' as t, count(*) from public._backup_planning_task_20260927
union all
select 'occurrence', count(*) from public._backup_planning_occurrence_20260927
union all
select 'recompute_state', count(*) from public._backup_planning_recompute_state_20260927;
