-- Stale-batch guard for the continuity pipeline: a request that read the
-- cursor before another run completed the same batch must not re-pause the
-- already-processed range (paused_empty regression) nor write duplicate
-- candidates after the cursor has moved past the batch.
--
--  * pause_memory_continuity_empty: rebuilt from the 20260816010000 body.
--    Under the cursor row lock, if the run's window is already fully
--    processed (source_last <= cursor.last), the cursor is left untouched,
--    the run is finalized as an empty success, and the caller receives
--    {'status': 'already_processed', 'last_processed_message_id': ...}
--    instead of the paused cursor row.
--  * commit_memory_continuity_run: rebuilt from the 20260830010000 body
--    (latest definition, recall-scene era). The cursor row is now locked
--    explicitly and the same staleness comparison raises
--    memory_continuity_batch_already_processed before any candidate write.
--
-- Comparison semantics match the in-place structure guard style: plain
-- cursor-position comparison under the row lock, serialized with the
-- per-assistant continuity advisory lock. No table or column changes.

begin;

create or replace function public.pause_memory_continuity_empty(
    p_run_id bigint
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_cursor public.memory_continuity_cursors%rowtype;
begin
    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;

    if not found or v_run.pipeline <> 'continuity'
       or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_continuity_invalid_run';
    end if;
    if v_run.source_first_message_id is null
       or v_run.source_last_message_id is null
       or coalesce(v_run.message_count, 0) < 1 then
        raise exception 'memory_continuity_missing_batch';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id, 0));

    insert into public.memory_continuity_cursors (
        assistant_id, last_processed_message_id
    ) values (
        v_run.assistant_id, 177
    ) on conflict (assistant_id) do nothing;

    select * into v_cursor
    from public.memory_continuity_cursors
    where assistant_id = v_run.assistant_id
    for update;

    -- 陈旧批次守卫：游标已越过本批末尾说明批次已被其它运行处理完毕。
    -- 不得把已完成的批次重新 paused_empty：保持游标不动，本 run 以空
    -- 成功收束，并返回 already_processed 供网关识别。
    if v_run.source_last_message_id <= v_cursor.last_processed_message_id then
        update public.memory_digest_runs set
            status = 'succeeded',
            extracted_count = 0,
            inserted_count = 0,
            preview_memories = '[]'::jsonb,
            completed_at = now(),
            heartbeat_at = null,
            error_code = null,
            error_message = null
        where id = v_run.id;
        return jsonb_build_object(
            'status', 'already_processed',
            'last_processed_message_id', v_cursor.last_processed_message_id
        );
    end if;

    update public.memory_continuity_cursors set
        status = 'paused_empty',
        manual_cooldown_until = case
            when v_run.trigger in ('continuity_manual', 'continuity_retry')
            then now() + interval '10 seconds'
            else manual_cooldown_until
        end,
        blocked_first_message_id = v_run.source_first_message_id,
        blocked_last_message_id = v_run.source_last_message_id,
        blocked_message_count = v_run.message_count,
        blocked_at = now(),
        pause_reason = 'empty_candidates',
        updated_at = now()
    where assistant_id = v_run.assistant_id
    returning * into v_cursor;

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = 0,
        inserted_count = 0,
        preview_memories = '[]'::jsonb,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = v_run.id;

    return to_jsonb(v_cursor);
end;
$function$;

revoke all on function public.pause_memory_continuity_empty(bigint)
    from public, anon, authenticated;
grant execute on function public.pause_memory_continuity_empty(bigint)
    to service_role;

create or replace function public.commit_memory_continuity_run(
    p_run_id bigint,
    p_candidates jsonb default '[]'::jsonb
)
returns integer
language plpgsql
security definer
set search_path to 'public','extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_item jsonb;
    v_count integer := 0;
    v_preview jsonb := '[]'::jsonb;
    v_cursor public.memory_continuity_cursors%rowtype;
begin
    if jsonb_typeof(coalesce(p_candidates,'[]'::jsonb)) <> 'array'
       or jsonb_array_length(p_candidates) = 0 then
        raise exception 'memory_continuity_invalid_candidates';
    end if;
    select * into v_run from public.memory_digest_runs where id = p_run_id for update;
    if not found or v_run.pipeline <> 'continuity' or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_continuity_invalid_run';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id,0));
    insert into public.memory_continuity_cursors (assistant_id, last_processed_message_id)
    values (v_run.assistant_id, 177) on conflict (assistant_id) do nothing;
    select * into v_cursor from public.memory_continuity_cursors
    where assistant_id = v_run.assistant_id for update;
    -- 陈旧批次守卫：游标已越过本批末尾说明批次已被其它运行处理完毕，
    -- 拒绝重复写入（整批回滚，游标与既有数据保持不变）。
    if v_run.source_last_message_id <= v_cursor.last_processed_message_id then
        raise exception 'memory_continuity_batch_already_processed';
    end if;
    for v_item in select value from jsonb_array_elements(p_candidates) loop
        v_count := v_count + public.store_continuity_candidate(v_run,v_item);
        v_preview := v_preview || jsonb_build_array(v_item - 'embedding' - 'content_hash' - 'recall_embedding');
    end loop;
    update public.memory_continuity_cursors set
        last_processed_message_id = greatest(last_processed_message_id,v_run.source_last_message_id),
        status = 'ready',
        last_success_at = now(),
        auto_cooldown_until = now() + interval '1 hour',
        blocked_first_message_id = null,
        blocked_last_message_id = null,
        blocked_message_count = null,
        blocked_at = null,
        pause_reason = null,
        updated_at = now()
    where assistant_id = v_run.assistant_id;
    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = jsonb_array_length(p_candidates),
        inserted_count = v_count,
        preview_memories = v_preview,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = p_run_id;
    return v_count;
end;
$function$;

revoke all on function public.commit_memory_continuity_run(bigint, jsonb)
    from public, anon, authenticated;
grant execute on function public.commit_memory_continuity_run(bigint, jsonb)
    to service_role;

commit;
