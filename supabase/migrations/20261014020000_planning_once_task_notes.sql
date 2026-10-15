-- Upgrade after 20261014010000_planning_task_notes.sql.
-- Retain the row lock, post-lock occurrence check, whitelist and ACLs.
-- Missing note preserves its value; JSON null clears it; text replaces it.
begin;

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
            'note', 'content', 'task_type', 'interval_days', 'weekdays', 'month_days',
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
        note = case when p_patch ? 'note' then p_patch->>'note' else note end,
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

commit;
