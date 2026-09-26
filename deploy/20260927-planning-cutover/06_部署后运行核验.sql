-- ============================================================
-- 06 · 部署后运行核验（新网关启动并运行 2～3 分钟后执行）
-- 新版 planning_loop 约每分钟跑一次：生成当前周期应有实例 →
-- 限时超时打标 → 到期自动重算 → 旧版遗留清理。
-- ============================================================

-- 1. 当前任务（部署后你应在管理台重新创建待办；此处若为空属正常）
select id, content, task_type, refresh_mode, refresh_enabled, is_active
from public.planning_task order by id;

-- 2. 新生成的实例（应全部 round_key 非空；round_key 为空 = 混入了旧逻辑写入，异常）
select id, task_id, round_key, schedule_date, display_cycle_date, status, source
from public.planning_occurrence
order by id desc limit 20;

-- 3. 唯一性自检：同一任务同一轮不应有两行（应返回 0 行）
select task_id, round_key, count(*) as n
from public.planning_occurrence
where round_key is not null
group by task_id, round_key
having count(*) > 1;

-- 4. 旧写入源已消失的自检：旧列 cursor_date / next_due 不应再被推进
--    （部署新网关并清理后，planning_task 里根本没有行，此查询仅留档）
select count(*) as legacy_task_rows_with_cursor
from public.planning_task
where cursor_date is not null or next_due is not null;

-- 5. 备份表仍在（应返回 3 张，确认回滚能力未丢）
select table_name from information_schema.tables
where table_schema = 'public' and table_name like '_backup_planning_%'
order by table_name;
