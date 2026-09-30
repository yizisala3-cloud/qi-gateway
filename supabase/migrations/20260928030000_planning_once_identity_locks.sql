-- 批次 6 最终修复（2026-09-28 user 批准）：once 身份编辑与生成的跨进程
-- 数据库级互斥（最终修复问题 3）。
-- 背景：进程内 RLock 在多 worker / 多进程部署下不是同一把锁。两个函数以
-- planning_task 行上的 FOR UPDATE 行锁为同一数据库级互斥点：
--   * planning_update_once_task_guarded：once 身份编辑侧——锁任务行 →
--     锁内重新确认该任务不存在任何 occurrence（生成无法在锁窗口内插入）
--     → 原子保存补丁（硬白名单逐列 case-when，缺键保持现值；危险字段
--     拒绝）。Python 仍负责全部业务校验与中文错误；本函数只负责锁、
--     once 存在性权威复核与原子写。
--   * planning_insert_once_occurrence：生成侧——锁同一任务行 → 校验
--     任务当前 target_date / 窗口模板与生成计算所用定义一致（不一致 =
--     编辑已并发生效，本轮生成作废，由下一次维护按新定义重新生成）→
--     插入预构造的 occurrence 行（status 必须 pending、source 必须
--     schedule）。轮次唯一键继续防重复。
-- 两侧以同一把任务行锁串行化后，「检查无实例 → 生成旧实例 → 保存新
-- 日期」与「保存新日期 → 生成旧日期实例」两类交错都不可能发生。
-- 不新增锁表；不修改表结构；重放安全（仅 create or replace function）。

begin;

-- ── 编辑侧：once 身份字段的锁内条件保存 ────────────────────────────
create or replace function public.planning_update_once_task_guarded(
    p_task_id bigint,
    p_patch jsonb
) returns void
language plpgsql as $$
begin
    if exists (
        select 1
        from jsonb_object_keys(coalesce(p_patch, '{}'::jsonb)) as k
        where k not in (
            'content', 'task_type', 'interval_days', 'weekdays', 'month_days',
            'target_date', 'refresh_mode', 'refresh_anchor_at', 'refresh_enabled',
            'estimated_minutes', 'window_start_tod', 'window_end_tod',
            'alarm_start', 'alarm_end', 'timer_minutes', 'is_active',
            'is_hollow', 'hollow_start_content', 'hollow_start_minutes',
            'hollow_wait_minutes', 'hollow_wait_note', 'hollow_end_content',
            'hollow_end_minutes', 'time_mode', 'est_start_tod', 'est_end_tod',
            'is_fixed', 'refresh_generated_through', 'last_handled_at',
            'refresh_next_due_at', 'updated_at'
        )
    ) then
        raise exception 'planning_update_once_task_guarded: patch contains unsupported field';
    end if;

    -- 语句 1：取得任务行锁（FOR UPDATE 等待期间发生的并发提交，
    -- 由后续语句的新快照看见——禁止把锁与复核塞进同一 statement）。
    if not exists (
        select 1 from public.planning_task
        where id = p_task_id
        for update
    ) then
        raise exception 'planning_update_once_task_guarded: task missing';
    end if;
    -- 语句 2（新快照）：锁等待结束后复核 occurrence 是否已被并发生成。
    if exists (
        select 1 from public.planning_occurrence
        where task_id = p_task_id
    ) then
        raise exception 'planning_update_once_task_guarded: task missing or once identity locked: occurrence exists';
    end if;
    -- 语句 3：原子保存。
    update public.planning_task set
        content = case when p_patch ? 'content' then p_patch->>'content' else content end,
        task_type = case when p_patch ? 'task_type' then p_patch->>'task_type' else task_type end,
        interval_days = case when p_patch ? 'interval_days'
            then (p_patch->>'interval_days')::integer else interval_days end,
        weekdays = case when p_patch ? 'weekdays'
            then (select array_agg((v)::integer) from jsonb_array_elements_text(p_patch->'weekdays') v)
            else weekdays end,
        month_days = case when p_patch ? 'month_days'
            then (select array_agg((v)::integer) from jsonb_array_elements_text(p_patch->'month_days') v)
            else month_days end,
        target_date = case when p_patch ? 'target_date'
            then (p_patch->>'target_date')::date else target_date end,
        refresh_mode = case when p_patch ? 'refresh_mode'
            then p_patch->>'refresh_mode' else refresh_mode end,
        refresh_anchor_at = case when p_patch ? 'refresh_anchor_at'
            then (p_patch->>'refresh_anchor_at')::timestamptz else refresh_anchor_at end,
        refresh_enabled = case when p_patch ? 'refresh_enabled'
            then (p_patch->>'refresh_enabled')::boolean else refresh_enabled end,
        estimated_minutes = case when p_patch ? 'estimated_minutes'
            then (p_patch->>'estimated_minutes')::integer else estimated_minutes end,
        window_start_tod = case when p_patch ? 'window_start_tod'
            then (p_patch->>'window_start_tod')::time else window_start_tod end,
        window_end_tod = case when p_patch ? 'window_end_tod'
            then (p_patch->>'window_end_tod')::time else window_end_tod end,
        alarm_start = case when p_patch ? 'alarm_start'
            then (p_patch->>'alarm_start')::boolean else alarm_start end,
        alarm_end = case when p_patch ? 'alarm_end'
            then (p_patch->>'alarm_end')::boolean else alarm_end end,
        timer_minutes = case when p_patch ? 'timer_minutes'
            then (p_patch->>'timer_minutes')::integer else timer_minutes end,
        is_active = case when p_patch ? 'is_active'
            then (p_patch->>'is_active')::boolean else is_active end,
        is_hollow = case when p_patch ? 'is_hollow'
            then (p_patch->>'is_hollow')::boolean else is_hollow end,
        hollow_start_content = case when p_patch ? 'hollow_start_content'
            then p_patch->>'hollow_start_content' else hollow_start_content end,
        hollow_start_minutes = case when p_patch ? 'hollow_start_minutes'
            then (p_patch->>'hollow_start_minutes')::integer else hollow_start_minutes end,
        hollow_wait_minutes = case when p_patch ? 'hollow_wait_minutes'
            then (p_patch->>'hollow_wait_minutes')::integer else hollow_wait_minutes end,
        hollow_wait_note = case when p_patch ? 'hollow_wait_note'
            then p_patch->>'hollow_wait_note' else hollow_wait_note end,
        hollow_end_content = case when p_patch ? 'hollow_end_content'
            then p_patch->>'hollow_end_content' else hollow_end_content end,
        hollow_end_minutes = case when p_patch ? 'hollow_end_minutes'
            then (p_patch->>'hollow_end_minutes')::integer else hollow_end_minutes end,
        time_mode = case when p_patch ? 'time_mode'
            then p_patch->>'time_mode' else time_mode end,
        est_start_tod = case when p_patch ? 'est_start_tod'
            then (p_patch->>'est_start_tod')::time else est_start_tod end,
        est_end_tod = case when p_patch ? 'est_end_tod'
            then (p_patch->>'est_end_tod')::time else est_end_tod end,
        is_fixed = case when p_patch ? 'is_fixed'
            then (p_patch->>'is_fixed')::boolean else is_fixed end,
        refresh_generated_through = case when p_patch ? 'refresh_generated_through'
            then (p_patch->>'refresh_generated_through')::date else refresh_generated_through end,
        last_handled_at = case when p_patch ? 'last_handled_at'
            then (p_patch->>'last_handled_at')::timestamptz else last_handled_at end,
        refresh_next_due_at = case when p_patch ? 'refresh_next_due_at'
            then (p_patch->>'refresh_next_due_at')::timestamptz else refresh_next_due_at end,
        updated_at = case when p_patch ? 'updated_at'
            then (p_patch->>'updated_at')::timestamptz else updated_at end
    where id = p_task_id;
    if not found then
        raise exception 'planning_update_once_task_guarded: task missing or once identity locked: occurrence exists';
    end if;
end;
$$;

-- ── 生成侧：once 轮次插入的定义一致性守卫 ──────────────────────────
create or replace function public.planning_insert_once_occurrence(
    p_task_id bigint,
    p_expected_target_date date,
    p_expected_window_start_tod text,
    p_expected_window_end_tod text,
    p_rows jsonb,
    p_expected_task jsonb default null
) returns void
language plpgsql as $$
declare
    current_task_type text;
    current_refresh_mode text;
    current_refresh_enabled boolean;
    current_is_active boolean;
    current_target_date date;
    current_window_start_tod time;
    current_window_end_tod time;
    current_content text;
    current_estimated_minutes integer;
    current_time_mode text;
    current_is_hollow boolean;
    current_hollow_start_minutes integer;
    current_hollow_wait_minutes integer;
    current_hollow_end_minutes integer;
begin
    -- 锁任务行（与编辑侧同一把锁）并校验定义未漂移：生成计算所依据的
    -- target_date / 窗口模板与任务行当前值不一致 = 编辑已并发生效，
    -- 本轮按旧定义预构造的行作废（下一次维护按新定义重新生成）。
    -- 语句 1：锁任务行并重读当前权威状态（FOR UPDATE 的 INTO 读取的是
    -- 取得锁之后的最新已提交版本——锁前读到的旧快照一律不作数）。
    select task_type, refresh_mode, refresh_enabled, is_active,
           target_date, window_start_tod, window_end_tod,
           content, estimated_minutes, time_mode, is_hollow,
           hollow_start_minutes, hollow_wait_minutes, hollow_end_minutes
    into current_task_type, current_refresh_mode, current_refresh_enabled,
         current_is_active, current_target_date, current_window_start_tod,
         current_window_end_tod, current_content, current_estimated_minutes,
         current_time_mode, current_is_hollow, current_hollow_start_minutes,
         current_hollow_wait_minutes, current_hollow_end_minutes
    from public.planning_task
    where id = p_task_id
    for update;
    if not found then
        raise exception 'planning_insert_once_occurrence: task not found';
    end if;
    -- 语句 2（新快照）：锁内复核完整生成资格——类型 / 刷新模式 / 启用 /
    -- 未暂停 / 定义（target_date + 窗口模板）与生成计算所依据的一致；
    -- 任一漂移 = 旧快照生成作废，由下一次维护按新定义重新生成。
    if current_task_type is distinct from 'once'
       or current_refresh_mode is distinct from 'none'
       or current_is_active is not true
       or current_refresh_enabled is not distinct from false
       or current_target_date is distinct from p_expected_target_date
       or current_window_start_tod is distinct from p_expected_window_start_tod::time
       or current_window_end_tod is distinct from p_expected_window_end_tod::time then
        raise exception 'planning_insert_once_occurrence: once definition changed during generation';
    end if;
    -- 最终 Debug 问题 2：复核全部会冻结进 occurrence 的任务输入
    --（content / estimated_minutes / time_mode / hollow 形状配置）——
    -- 任一漂移 = 旧 snapshot 生成作废（occurrence 不得以旧文案 / 旧耗时
    -- 出生），由后续维护按最新定义重新生成。缺键跳过（NULL = 该输入未
    -- 参与本次生成）；未知键拒绝。
    if p_expected_task is not null then
        if exists (
            select 1
            from jsonb_object_keys(p_expected_task) as k
            where k not in ('content', 'estimated_minutes', 'time_mode', 'is_hollow',
                            'hollow_start_minutes', 'hollow_wait_minutes',
                            'hollow_end_minutes')
        ) then
            raise exception 'planning_insert_once_occurrence: expected task contains unsupported field';
        end if;
        if exists (
            select 1 from public.planning_task
            where id = p_task_id
              and ((p_expected_task ? 'content' and content is distinct from p_expected_task->>'content')
                or (p_expected_task ? 'estimated_minutes' and estimated_minutes is distinct from (p_expected_task->>'estimated_minutes')::integer)
                or (p_expected_task ? 'time_mode' and time_mode is distinct from p_expected_task->>'time_mode')
                or (p_expected_task ? 'is_hollow' and is_hollow is distinct from (p_expected_task->>'is_hollow')::boolean)
                or (p_expected_task ? 'hollow_start_minutes' and hollow_start_minutes is distinct from (p_expected_task->>'hollow_start_minutes')::integer)
                or (p_expected_task ? 'hollow_wait_minutes' and hollow_wait_minutes is distinct from (p_expected_task->>'hollow_wait_minutes')::integer)
                or (p_expected_task ? 'hollow_end_minutes' and hollow_end_minutes is distinct from (p_expected_task->>'hollow_end_minutes')::integer))
        ) then
            raise exception 'planning_insert_once_occurrence: once definition changed during generation';
        end if;
    end if;
    if exists (
        select 1
        from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
        where r.value->>'status' is distinct from 'pending'
           or r.value->>'source' is distinct from 'schedule'
    ) then
        raise exception 'planning_insert_once_occurrence: rows must be pending schedule occurrences';
    end if;
    insert into public.planning_occurrence (
        task_id, for_date, round_key, schedule_date, display_cycle_date,
        display_reason, phase_group, phase, fixed_due_at, est_start, est_end,
        nominal_start, status, planned_minutes, planned_wait_minutes, sort_order,
        is_fixed, estimated_time_source, fixed_source, schedule_managed,
        is_limited, window_start_at, window_end_at, fixed_expires_at, source,
        early_period_date, deadline_at, content_snapshot, display_content,
        time_mode_snapshot, created_at, updated_at
    )
    select r.task_id, r.for_date, r.round_key, r.schedule_date, r.display_cycle_date,
           r.display_reason, r.phase_group, r.phase, r.fixed_due_at, r.est_start,
           r.est_end, r.nominal_start, r.status, r.planned_minutes,
           r.planned_wait_minutes, r.sort_order, r.is_fixed, r.estimated_time_source,
           r.fixed_source, r.schedule_managed, r.is_limited, r.window_start_at,
           r.window_end_at, r.fixed_expires_at, r.source, r.early_period_date,
           r.deadline_at, r.content_snapshot, r.display_content,
           r.time_mode_snapshot, r.created_at, r.updated_at
    from jsonb_populate_recordset(null::public.planning_occurrence, p_rows) r;
end;
$$;

-- ── 最终 Debug（明日可用 BLOCKER 2）：非 once 生成锁内复核 ──────────
-- 普通周期任务的最终 INSERT 与 once 共用同一把任务行锁 + 定义一致性复核：
-- 生成侧 Python 预构造行后，锁内重读任务当前定义，任一会冻结进 occurrence
-- 的输入（active / refresh_enabled / content / estimated_minutes /
-- window 模板）与生成快照漂移 → 整体拒绝（0 行），后续维护按新定义重新
-- 生成。不插入后补救删除。
-- 旧两参数版本没有完整定义复核，重放或增量安装时必须移除，避免旧调用绕过。
drop function if exists public.planning_insert_round_occurrence(bigint, jsonb);

create or replace function public.planning_insert_round_occurrence(
    p_task_id bigint,
    p_rows jsonb,
    p_expected_task jsonb
) returns void
language plpgsql as $$
declare
    current_row public.planning_task%rowtype;
    current_snapshot jsonb;
begin
    select * into current_row
    from public.planning_task
    where id = p_task_id
    for update;
    if not found then
        raise exception 'planning_insert_round_occurrence: task not found';
    end if;
    if current_row.is_active is not true
       or current_row.refresh_enabled is not true then
        raise exception 'planning_insert_round_occurrence: task no longer active'
            using errcode = 'PC001';
    end if;
    current_snapshot := to_jsonb(current_row);
    -- Python 生成事件轴、行形状、展示和窗口时读取的定义必须完整送入。
    -- 缺键不能被解释为「不比较」，否则调用者可绕过并发保护。
    if p_expected_task is null or not (p_expected_task ?& array[
        'task_type', 'refresh_mode', 'refresh_enabled', 'is_active',
        'request_state', 'content', 'time_mode', 'estimated_minutes',
        'is_hollow', 'hollow_start_minutes', 'hollow_wait_minutes',
        'hollow_end_minutes', 'hollow_start_content', 'hollow_end_content',
        'interval_days', 'weekdays', 'month_days', 'target_date',
        'created_at', 'refresh_anchor_at',
        'last_handled_at', 'refresh_next_due_at', 'window_start_tod',
        'window_end_tod'
    ]) then
        raise exception 'planning_insert_round_occurrence: expected task incomplete';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
        where r.value->>'status' is distinct from 'pending'
           or r.value->>'source' is distinct from 'schedule'
    ) then
        raise exception 'planning_insert_round_occurrence: rows must be pending schedule occurrences';
    end if;
    -- 一般标量/数组按 JSONB 值比较；时间类型按数据库类型比较，容忍
    -- 09:00/09:00:00 和不同时区表示的同一时刻。
    if exists (
        select 1 from unnest(array[
            'task_type', 'refresh_mode', 'refresh_enabled', 'is_active',
            'request_state', 'content', 'time_mode', 'estimated_minutes',
            'is_hollow', 'hollow_start_minutes', 'hollow_wait_minutes',
            'hollow_end_minutes', 'hollow_start_content', 'hollow_end_content',
            'interval_days', 'weekdays', 'month_days', 'target_date'
        ]) as fields(field)
        where current_snapshot->field is distinct from p_expected_task->field
    ) or current_row.created_at is distinct from
            (p_expected_task->>'created_at')::timestamptz
       or current_row.refresh_anchor_at is distinct from
            (p_expected_task->>'refresh_anchor_at')::timestamptz
       or current_row.last_handled_at is distinct from
            (p_expected_task->>'last_handled_at')::timestamptz
       or current_row.refresh_next_due_at is distinct from
            (p_expected_task->>'refresh_next_due_at')::timestamptz
       or current_row.window_start_tod is distinct from
            (p_expected_task->>'window_start_tod')::time
       or current_row.window_end_tod is distinct from
            (p_expected_task->>'window_end_tod')::time then
        raise exception 'planning_insert_round_occurrence: task definition changed during generation'
            using errcode = 'PC001';
    end if;
    insert into public.planning_occurrence (
        task_id, for_date, round_key, schedule_date, display_cycle_date,
        display_reason, phase_group, phase, fixed_due_at, est_start, est_end,
        nominal_start, status, planned_minutes, planned_wait_minutes, sort_order,
        is_fixed, estimated_time_source, fixed_source, schedule_managed,
        is_limited, window_start_at, window_end_at, fixed_expires_at, source,
        early_period_date, deadline_at, content_snapshot, display_content,
        time_mode_snapshot, created_at, updated_at
    )
    select r.task_id, r.for_date, r.round_key, r.schedule_date, r.display_cycle_date,
           r.display_reason, r.phase_group, r.phase, r.fixed_due_at, r.est_start,
           r.est_end, r.nominal_start, r.status, r.planned_minutes,
           r.planned_wait_minutes, r.sort_order, r.is_fixed, r.estimated_time_source,
           r.fixed_source, r.schedule_managed, r.is_limited, r.window_start_at,
           r.window_end_at, r.fixed_expires_at, r.source, r.early_period_date,
           r.deadline_at, r.content_snapshot, r.display_content,
           r.time_mode_snapshot, r.created_at, r.updated_at
    from jsonb_populate_recordset(null::public.planning_occurrence, p_rows) r;
end;
$$;

commit;
