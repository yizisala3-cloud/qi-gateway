-- 后续待修复清单 #20（2026-10-02）：删除目标行与中空编辑的反向锁序死锁。
-- 复现（真库多连接，探针：审查日志/规划管理-合并前检查-26.10.1.18.23/
-- 锁顺序死锁复现探针.py）：中空 pending 配对 start（小 id）+ end（大 id），
-- 同轮编辑持 start 行锁；业务删除（目标 = end）经 20261001010000 版函数
-- 时，语句 1b 的目标行 UPDATE 先锁 end，随后语句 1c 的稳定 id 序扫描等
-- 待 start → 编辑（round patch 按 id 升序锁 start→end）申请 end 形成双向
-- 等待，PostgreSQL 检测 40P01，被中止的删除映射为 database_unavailable /
-- HTTP 503。锁序不取决于语句 1c 循环里的 order by id——函数前段的目标
-- UPDATE 不遵守同一锁序才是根因。
-- 修复：语句 1b（目标补丁写入）**之前**，按稳定 id 序统一获取本任务全部
-- 开放行锁（与语句 1c 同一谓词与排序、与 planning_patch_occurrence_round
-- 的「按 id 升序锁两阶段」同序）——此后 1b / 1c / 语句 2 都作用于本事务
-- 已持有的行锁，删除与同轮编辑（以及任何遵守 id 升序的同轮写入方）之间
-- 不再出现反向等待环。
-- 语义保持：语句 1b pending 原守卫与应用语义逐字保留；语句 1c 全开放行
-- 事实补齐、耗时锁内重算、并发事实让位；语句 2 批量关闭、语句 4 停用与
-- PC001 并发拒绝、失败整体回滚——全部与 20261001010000 版本一致，本次
-- 仅插入语句 1a，不改任何其他语句。
-- 部署状态：同签名 create or replace（4 参数，不产生新 overload；重放
-- 安全；不新增表列 / 触发器 / 约束 / 数据回填）；基线 = 20261001010000
-- 版函数（该迁移亦未在 production 执行，两者随同一次受控部署按序应用）；
-- 发布时按清单 #15 核对 planning_discard_task 目标 schema 恰一签名。

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

    -- 语句 1a（清单 #20，2026-10-02）：目标补丁写入**前**，按稳定 id 序
    -- 统一获取本任务全部开放行锁——语句 1b 的目标行 UPDATE 与语句 1c 的
    -- 逐行扫描都发生在同一锁序之后。删除与中空同轮编辑（round patch 按
    -- id 升序锁两阶段）从此遵守同一加锁顺序，消除「删除先锁大 id 目标行
    -- 再等小 id、编辑持小 id 等大 id」的反向等待环（40P01 → 503）。
    -- 谓词与语句 1c 一致（开放状态全集）；行锁获取时 EvalPlanQual 以最新
    -- 已提交版本复核，语句 1c 循环内再次复核同一组行（已在锁内，无额外
    -- 等待）。
    perform 1
      from public.planning_occurrence
     where task_id = p_task_id
       and status in ('pending', 'in_progress', 'deferred', 'partial')
     order by id
     for update;

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

    -- 语句 2（新快照）：关闭全部开放 occurrence——与语句 1a / 1c 已锁定的
    -- 同一组行（行锁未释放，状态不可能变化）；中空同轮两阶段由同一语句以
    -- 相同终态值命中，不存在一半 timeout / 一半 pending。
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
