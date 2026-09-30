-- 批次 6 最终验收修复（2026-09-28）：「废弃整个任务」的原子 RPC
-- （最终修复问题 5）。跨 task + occurrence 的原子命令：锁任务行 →
-- 单语句关闭全部开放 occurrence（含中空同轮两阶段——同一终态值由一条
-- UPDATE 命中，语句级原子）→ 单语句停用任务 → 任一失败整体回滚。
-- 仅承载既有正式废弃语义（需求 25：移除开放实例 + 停止刷新 + 保留关闭
-- 历史），不是通用 task+occurrence 事务框架。
-- Python 仍负责业务前置校验（废弃资格 / 并发收口语义）；本函数负责锁、
-- 状态验证与原子写。重放安全；未在 production 执行。

begin;

create or replace function public.planning_discard_task(
    p_task_id bigint,
    p_now timestamptz,
    p_target_id bigint default null,
    p_target_patch jsonb default null
) returns integer
language plpgsql as $$
declare
    closed integer;
begin
    -- 语句 1：锁任务行。
    if not exists (
        select 1 from public.planning_task
        where id = p_task_id
        for update
    ) then
        raise exception 'planning_discard_task: task not found';
    end if;
    -- 语句 1b（可选，最终 Debug 问题 1B）：目标行的实际时间事实补齐——
    -- 属于废弃命令成功必须产生的结果，与关闭/停用在同一事务内写入；
    -- 白名单仅限 actual 字段（closed_at/status 已由语句 2 覆盖）。
    -- 明日可用 BLOCKER 3：写前锁内复核目标行现状态——并发已完成 / 超时 /
    -- 已关闭（或已携带与本命令预期不同的生命周期事实）时，旧 target patch
    -- 不得覆盖新历史（actual_end / actual_minutes 保持并发写入值）；
    -- 仅当目标行仍为 pending（Python 读取时的预期状态）才应用事实补齐。
    if p_target_id is not null and p_target_patch is not null then
        if exists (
            select 1
            from jsonb_object_keys(p_target_patch) as k
            where k not in ('actual_start', 'actual_end', 'actual_minutes', 'updated_at')
        ) then
            raise exception 'planning_discard_task: target patch contains unsupported field';
        end if;
        update public.planning_occurrence set
            actual_start = case when p_target_patch ? 'actual_start'
                then (p_target_patch->>'actual_start')::timestamptz else actual_start end,
            actual_end = case when p_target_patch ? 'actual_end'
                then (p_target_patch->>'actual_end')::timestamptz else actual_end end,
            actual_minutes = case when p_target_patch ? 'actual_minutes'
                then (p_target_patch->>'actual_minutes')::integer else actual_minutes end,
            updated_at = case when p_target_patch ? 'updated_at'
                then (p_target_patch->>'updated_at')::timestamptz else updated_at end
        where id = p_target_id
          and task_id = p_task_id
          and status = 'pending'
          and actual_start is null
          and actual_end is null
          and partial_at is null
          and handled_at is null
          and closed_at is null;
    end if;

    -- 语句 2（新快照）：关闭全部开放 occurrence——中空同轮两阶段由同一
    -- 语句以相同终态值命中，不存在一半 timeout / 一半 pending。
    update public.planning_occurrence
       set status = 'discarded', closed_at = p_now, updated_at = p_now
     where task_id = p_task_id
       and status in ('pending', 'in_progress', 'deferred', 'partial');
    get diagnostics closed = row_count;
    -- 语句 4（新快照）：停用任务；任务已被并发停用 → 并发拒绝（整体回滚）。
    update public.planning_task
       set is_active = false, updated_at = p_now
     where id = p_task_id
       and is_active;
    if not found then
        raise exception 'planning_discard_task: task already inactive (concurrent change)'
            using errcode = 'PC001';
    end if;
    return closed;
end;
$$;

commit;
