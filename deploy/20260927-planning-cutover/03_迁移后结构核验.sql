-- ============================================================
-- 03 · 迁移后结构核验（02 运行成功后立即执行）
-- 每一行都应返回 true / 预期值；任何 false 都要停下排查。
-- ============================================================

-- 1. planning_task 新列（应全部 true）
select
  to_regclass('public.planning_task') is not null as t_task,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_task'
            and column_name='refresh_mode') as c_refresh_mode,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_task'
            and column_name='refresh_generated_through') as c_generated_through,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_task'
            and column_name='request_state') as c_request_state,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_task'
            and column_name='request_absorbed_keys') as c_absorbed;

-- 2. planning_occurrence 新列（应全部 true）
select
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_occurrence'
            and column_name='round_key') as c_round_key,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_occurrence'
            and column_name='schedule_date') as c_schedule_date,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_occurrence'
            and column_name='display_cycle_date') as c_display,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_occurrence'
            and column_name='handled_at') as c_handled_at,
  exists (select 1 from information_schema.columns
          where table_schema='public' and table_name='planning_occurrence'
            and column_name='early_period_date') as c_early_period;

-- 3. 触发器（应返回 5 行，tgenabled = 'O' 或 'D' 均为已启用形态）
select tgname, tgenabled from pg_trigger
where tgrelid = 'public.planning_occurrence'::regclass and not tgisinternal
order by tgname;
select tgname, tgenabled from pg_trigger
where tgrelid = 'public.planning_task'::regclass and not tgisinternal
order by tgname;

-- 4. 函数与索引（应全部 not null / true）
select
  to_regproc('public.planning_takeover_reschedule_request') is not null as f_takeover,
  to_regproc('public.planning_absorb_reschedule_request') is not null as f_absorb,
  to_regclass('public.planning_occurrence_round_phase_uq') is not null as i_round_phase,
  to_regclass('public.planning_occurrence_generation_request_uq') is not null as i_generation,
  to_regclass('public.planning_task_request_key_uq') is not null as i_request_key,
  to_regclass('public.planning_occurrence_early_period_uq') is not null as i_early_period,
  to_regclass('public.planning_occurrence_schedule_slot_uq') is null as old_slot_uq_dropped;

-- 5. 种子配置（应返回两行 planning.* 键）
select key, value from app_settings where key like 'planning.%' order by key;

-- 6. 旧数据仍然安全（旧行 round_key 为空，12,339 行左右，deleted 之前的读数）
select count(*) as total, count(round_key) as with_round_key
from planning_occurrence;
