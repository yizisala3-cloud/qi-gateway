-- 后续待修复清单 #16 第二轮修复（2026-10-01）：在 20261001010000（同日
-- 第一轮，未提交、未部署）基础上收敛两个审查发现（审查报告 26.10.1.15.26）。
-- 部署状态：原迁移 20260928040000 已于 2026-10-01 Batch 9 生产部署窗口应用
-- （施工计划 9B/C 完成记录：生产 8 个 planning RPC 各恰一签名）；本迁移是
-- 基于该已上产签名的同签名 create or replace 增量（仅替换函数体，不产生
-- 新 overload），待随下次受控部署应用；发布时按清单 #15 核对目标 schema
-- 无旧签名 overload。

-- 审查 R1（HIGH）：第一轮的执行中分支逐字段「只补 NULL」，会把不同快照的
-- 数据拼成自相矛盾的历史——保留数据库已有起止，却接受按旧快照（或请求
-- 改写值）计算的 patch 耗时。已确认复现：Python 读开始 08:00、09:00 删除
-- 算 60 分钟，提交前开始被修正为 08:30 → 落库 08:30–09:00 却带 60 分钟；
-- 库内开始 10:00、请求提供开始 09:00 / 结束 11:30 → Python 算 150 分钟，
-- 落库区间实际只有 90 分钟。根因：起止是计算输入、耗时是派生输出。
-- 审查 R2（MEDIUM）：任务详情「删除整个待办」（update_task is_active=false）
-- 不传目标 id / patch，第一轮逻辑整体跳过，执行中实例仍丢失结束事实。
-- 第三轮（复审三发现）：
--   1) 补入的 actual_start 未落库——锁内补入的开始只用于计算耗时，最终
--      UPDATE 漏写该列（开始 NULL、耗时 60 的自相矛盾历史）。修复：最终
--      采用的开始时间一并落库，其变化纳入更新判断；已有开始事实仍让位
--      保留（写入的是同值，仅补缺时才实际改变）。
--   2) 删除扫描期间刚开始的记录仍丢结束事实——事实补齐的锁扫描只覆盖
--      扫描瞬间已 in_progress 的行；等待行锁期间经真实 start_occurrence
--      开始的行被批量关闭时无事实。修复：锁扫描改为覆盖全部开放行
--      （pending / in_progress / deferred / partial，按 id 稳定锁序），
--      行锁获取时由 EvalPlanQual 复核最新已提交版本——扫描后才
--      开始的行在锁内以 in_progress 身份补齐事实，扫描后并发关闭的行
--      不再匹配开放谓词、自然退出本组（BLOCKER 3 保持）；批量关闭仍由
--      语句 2 以同一快照命中同一组已锁行。
--   3) 执行超过七天导致删除失败——无截止窗口单次待办属持续开放型
--      （需求 §9.4），§12.2 实际时间只记录执行事实、不存在七天分钟
--      上限；actual_minutes 的基表 CHECK（上限一周）是实现产物，长执行
--      实例的真实耗时（如八天 = 11520 分钟）违反 CHECK 使删除整体回滚
--      503。修复：放开上界（保留非负下限兜底；倒置区间已由语句 1c
--      显式拒绝），真实耗时完整保存、不截断、不清空开始时间绕过。
-- 本迁移语义：
-- * pending 且完全无事实：维持原守卫与原应用语义（语句 1b 逐字保留）；
-- * 执行中（in_progress 且无 handled_at / closed_at）行——覆盖两个废弃
--   入口及目标的其余执行中行（中空兄弟阶段、同任务其他执行中实例）：
--   锁内逐行选取最终采用的实际起止——目标行缺失的起止以补丁值补齐（已有
--   事实让位，BLOCKER 3），其余行缺失的结束时间以 p_now 收口；耗时一律按
--   最终起止在事务内重算（Python _compute_actual_minutes 契约：end <
--   start 整体拒绝回滚；max(0, round(总秒数/60))，x.5 就近取偶），不再
--   接受 patch 携带的耗时；
-- * partial / deferred / 已关闭（completed / timeout / discarded）行：
--   不补写、不覆盖，历史原样（本轮不扩展）。
-- 任务停用与全部开放实例关闭（含中空配对）的单事务原子性、并发停用
-- PC001 拒绝契约保持不变；校验或写入失败整体回滚。

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
    v_row public.planning_occurrence%rowtype;
    v_start timestamptz;
    v_end timestamptz;
    v_minutes integer;
    v_raw double precision;
    v_whole double precision;
    v_frac double precision;
begin
    -- 语句 1：锁任务行。
    if not exists (
        select 1 from public.planning_task
        where id = p_task_id
        for update
    ) then
        raise exception 'planning_discard_task: task not found';
    end if;
    -- 语句 1b（可选，最终 Debug 问题 1B）：pending 目标行的实际时间事实
    -- 补齐——原守卫与应用语义逐字保留。明日可用 BLOCKER 3：写前锁内复核
    -- 目标行现状态，并发已完成 / 超时 / 已关闭（或已携带生命周期事实）时
    -- 旧 target patch 不得覆盖新历史；仅当目标行仍为 pending 且完全无
    -- 事实（Python 读取时的预期状态）才按补丁原样应用（补丁耗时由 Python
    -- 按同一请求快照的起止算出，自洽）。
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

    -- 语句 1c（清单 #16 第二轮，审查 R1 + R2；第三轮扩为全部开放行）：
    -- 执行中实例的结束事实在事务内补齐，覆盖两个废弃入口——
    -- * 实例入口（携带目标补丁）：目标行缺失的起止以补丁值补齐，已有
    --   事实让位（BLOCKER 3）；
    -- * 任务详情入口（update_task is_active=false，无目标 id / patch）：
    --   与目标行以外的执行中行（中空兄弟阶段、同任务其他执行中实例）
    --   一样，缺失的结束时间以 p_now 收口。
    -- 锁扫描覆盖全部开放行并按 id 稳定加锁；行锁获取时 EvalPlanQual 以
    -- 最新已提交版本复核开放谓词——扫描后才开始的行在锁内以 in_progress
    -- 身份进入补齐，扫描后并发关闭的行自然退出本组。
    -- 耗时一律按锁内最终采用的起止重算，不接受 patch 携带的耗时——按
    -- 字段各自「只补 NULL」会把不同快照的数据拼接成自相矛盾的历史（审查
    -- R1）。补入 / 已有的开始时间一并落库，其变化纳入更新判断（第三轮
    -- #1）。已有 actual_end 的行起止不动，耗时按保留起止校准。倒置区间
    -- 按应用层同契约拒绝（整体回滚）。
    for v_row in
        select *
          from public.planning_occurrence
         where task_id = p_task_id
           and status in ('pending', 'in_progress', 'deferred', 'partial')
         order by id
         for update
    loop
        if v_row.status = 'in_progress'
           and v_row.handled_at is null
           and v_row.closed_at is null then
            v_start := v_row.actual_start;
            v_end := v_row.actual_end;
            if v_row.id = p_target_id and p_target_patch is not null then
                if v_start is null and p_target_patch ? 'actual_start' then
                    v_start := (p_target_patch->>'actual_start')::timestamptz;
                end if;
                if v_end is null and p_target_patch ? 'actual_end' then
                    v_end := (p_target_patch->>'actual_end')::timestamptz;
                end if;
            end if;
            if v_end is null then
                v_end := p_now;
            end if;
            if v_start is not null and v_end < v_start then
                raise exception
                    'planning_discard_task: actual_end must not precede actual_start';
            end if;
            if v_start is null then
                v_minutes := null;
            else
                -- Python _minutes_between 契约：max(0, round(总秒数 / 60))，
                -- x.5 就近取偶（round-half-even）。
                v_raw := extract(epoch from (v_end - v_start)) / 60.0;
                v_whole := trunc(v_raw);
                v_frac := v_raw - v_whole;
                v_minutes := greatest(0, (v_whole + case
                    when v_frac > 0.5 then 1
                    when v_frac < 0.5 then 0
                    else case when (v_whole::bigint % 2) = 0 then 0 else 1 end
                end)::integer);
            end if;
            if v_start is distinct from v_row.actual_start
               or v_end is distinct from v_row.actual_end
               or v_minutes is distinct from v_row.actual_minutes then
                update public.planning_occurrence
                   set actual_start = v_start,
                       actual_end = v_end,
                       actual_minutes = v_minutes
                 where id = v_row.id;
            end if;
        end if;
    end loop;

    -- 语句 2（新快照）：关闭全部开放 occurrence——与语句 1c 已锁定的同一
    -- 组行（行锁未释放，状态不可能变化）；中空同轮两阶段由同一语句以相同
    -- 终态值命中，不存在一半 timeout / 一半 pending。
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

-- 第三轮 #3：actual_minutes 七天分钟上限是基表实现产物，与需求
-- §12.2「实际时间只记录执行事实」及 §9.4 持续开放型实例（无截止窗口实例
-- 可合法执行任意长）冲突——长执行实例的真实耗时违反 check 使删除/完成
-- 整体回滚。放开上界；非负下限保留兜底（倒置区间在语句 1c 显式拒绝）。
alter table public.planning_occurrence
    drop constraint planning_occurrence_actual_minutes_check;
alter table public.planning_occurrence
    add constraint planning_occurrence_actual_minutes_check
    check (actual_minutes is null or actual_minutes >= 0);

commit;
