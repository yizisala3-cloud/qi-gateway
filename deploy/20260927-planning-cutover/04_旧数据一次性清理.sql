-- ============================================================
-- 04 · 旧数据一次性清理（03 核验全部通过后执行）
-- 经 user 2026-09-27 确认：旧规划数据不做迁移，直接删除。
-- 依据需求规范 §31（一次性旧版升级允许删除旧实例记录，含旧历史实例），
-- 并经 user 追加确认旧任务定义一并删除，之后在管理台重新创建待办。
-- 安全网：00 步骤的 _backup_planning_*_20260927 三张备份表已留存，
-- 任何时候可从中恢复。
-- 单事务执行：以下清理要么全部成功，要么整体回滚；任一语句失败时
-- 后续语句会被 PostgreSQL 以「current transaction is aborted」拒绝，
-- 会话结束即整体回滚，可直接重跑本文件。
-- ============================================================

begin;

-- 1. 删除全部旧实例（含 12,339 条 pending 与 1 条 completed 历史）
delete from public.planning_occurrence;

-- 2. 删除 4 条旧任务定义（做饭 / 洗澡 / 洗衣服 / 拿快递）
--    如想保留某一个改日重建，删除前先把它的 id 从本语句排除，
--    但保留的任务需要另行分类 refresh_mode 后新网关才能刷新（见 README）。
delete from public.planning_task;

-- 3. 重置重算等待标记（单行表，清成干净初始态）
update public.planning_recompute_state
   set requested_at = null, reason = null, updated_at = now();

-- 4. 清理确认（应返回 0 / 0；本查询在事务内读到的即提交后的状态）
select
  (select count(*) from public.planning_occurrence) as occ_left,
  (select count(*) from public.planning_task) as task_left;

commit;
