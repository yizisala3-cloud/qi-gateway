-- General detail save: reminder fields share the atomic edit and durable receipt.
begin;

create or replace function public.planning_commit_detail_edit(
    p_expected_task jsonb, p_expected_round jsonb, p_task_patch jsonb,
    p_occurrence_patches jsonb, p_request_key uuid, p_intent jsonb, p_now timestamptz
) returns jsonb language plpgsql set search_path=public,pg_temp as $$
declare
    v_task public.planning_task%rowtype;
    v_occ public.planning_occurrence%rowtype;
    v_receipt public.planning_detail_edit_request%rowtype;
    v_patch jsonb;
    v_columns text;
    v_id bigint;
    v_ids bigint[];
    v_current_ids bigint[];
    v_round jsonb;
    v_scope jsonb;
    v_result jsonb;
begin
    if p_request_key is null or p_now is null or jsonb_typeof(p_intent) is distinct from 'object'
       or jsonb_typeof(p_expected_task) is distinct from 'object'
       or jsonb_typeof(p_expected_round) is distinct from 'array'
       or jsonb_typeof(p_task_patch) is distinct from 'object'
       or jsonb_typeof(p_occurrence_patches) is distinct from 'array' then
        raise exception 'planning detail edit: invalid payload';
    end if;
    -- Same-key replays serialize before any task/occurrence lock. This lock
    -- namespace is independent of goal commands and the existing round locks.
    perform pg_advisory_xact_lock(170101, hashtext(p_request_key::text));
    select * into v_receipt from public.planning_detail_edit_request where request_key=p_request_key;
    if found then
        if v_receipt.intent is distinct from p_intent then
            raise exception 'planning detail edit: request reused' using errcode='PC001';
        end if;
        return v_receipt.result || jsonb_build_object('replayed',true);
    end if;
    select * into v_task from public.planning_task where id=(p_expected_task->>'id')::bigint for update;
    if not found or to_jsonb(v_task) is distinct from p_expected_task then
        raise exception 'planning detail edit: schedule inputs drifted (task)' using errcode='PC001';
    end if;
    -- Notes synchronize every open row, so take the complete task's occurrence
    -- locks in stable order before touching any field or invoking its trigger.
    perform 1 from public.planning_occurrence where task_id=v_task.id order by id for update;
    select coalesce(array_agg((e->>'id')::bigint order by (e->>'id')::bigint),'{}'::bigint[])
      into v_ids from jsonb_array_elements(p_expected_round) e;
    if cardinality(v_ids) <> (select count(distinct x) from unnest(v_ids) x) then
        raise exception 'planning detail edit: invalid round';
    end if;
    if exists(select 1 from jsonb_array_elements(p_expected_round) e
       left join public.planning_occurrence o on o.id=(e->>'id')::bigint
       where o.id is null or o.task_id<>v_task.id or to_jsonb(o) is distinct from e) then
        raise exception 'planning detail edit: schedule inputs drifted (round)' using errcode='PC001';
    end if;
    -- Generation takes this same task lock. An empty once context is an
    -- expected absence, not permission to ignore a newly generated sole round.
    if v_task.task_type='once' and cardinality(v_ids)=0
       and exists(select 1 from public.planning_occurrence where task_id=v_task.id) then
        raise exception 'planning detail edit: schedule inputs drifted (once generated)' using errcode='PC001';
    end if;
    if cardinality(v_ids)>0 then
        select * into v_occ from public.planning_occurrence where id=v_ids[1];
        select coalesce(array_agg(id order by id),'{}'::bigint[]) into v_current_ids
          from public.planning_occurrence where task_id=v_task.id
           and (id=v_occ.id or (v_occ.phase is not null and round_key=v_occ.round_key
                and phase_group=v_occ.phase_group));
        if v_ids is distinct from v_current_ids then
            raise exception 'planning detail edit: schedule inputs drifted (members)' using errcode='PC001';
        end if;
    end if;
    if exists(select 1 from jsonb_object_keys(p_task_patch) k where k not in (
       'content','note','hollow_start_content','hollow_end_content','estimated_minutes','hollow_start_minutes','hollow_end_minutes',
       'window_start_tod','window_end_tod','alarm_start','alarm_end','timer_minutes','updated_at')) then
        raise exception 'planning detail edit: invalid task patch';
    end if;
    if exists(select 1 from jsonb_array_elements(p_occurrence_patches) e
       where jsonb_typeof(e) is distinct from 'object' or not e ?& array['id','updated_at']
       or not (e->>'id')::bigint=any(v_ids)
       or exists(select 1 from jsonb_object_keys(e) k where k not in (
          'id','updated_at','content_snapshot','display_content','planned_minutes',
          'window_start_at','window_end_at','est_start','est_end','estimated_time_source',
          'fixed_source','schedule_managed','is_fixed','is_parked','nominal_start',
          'display_cycle_date','display_reason')))
       or (select count(distinct e->>'id') from jsonb_array_elements(p_occurrence_patches) e)
          <> jsonb_array_length(p_occurrence_patches) then
        raise exception 'planning detail edit: invalid occurrence patch';
    end if;
    if exists(select 1 from jsonb_array_elements(p_occurrence_patches) e where e ? 'is_parked')
       and (jsonb_array_length(p_occurrence_patches)<>cardinality(v_ids)
            or exists(select 1 from jsonb_array_elements(p_occurrence_patches) e where not e ? 'is_parked')
            or (select count(distinct e->'is_parked') from jsonb_array_elements(p_occurrence_patches) e)<>1) then
        raise exception 'planning detail edit: parking must cover the whole round';
    end if;
    select jsonb_build_object(
        'name',case when exists(select 1 from public.planning_occurrence where id=any(v_ids)
                     and status in ('pending','in_progress','deferred','partial')
                     and (p_task_patch ? 'content' or (phase='start' and p_task_patch ? 'hollow_start_content')
                          or (phase='end' and p_task_patch ? 'hollow_end_content')))
                    then 'current_and_future' else 'future' end,
        'text',case when exists(select 1 from public.planning_occurrence where id=any(v_ids)
                     and status in ('pending','in_progress','deferred','partial'))
                    then 'current_and_future' else 'future' end,
        'time',case when cardinality(v_ids)>0 and not exists(
                     select 1 from public.planning_occurrence where id=any(v_ids) and (
                        status not in ('pending','deferred') or actual_start is not null
                        or actual_end is not null or partial_at is not null
                        or handled_at is not null or closed_at is not null))
                    then 'current_and_future' else 'future' end) into v_scope;
    for v_patch in select * from jsonb_array_elements(p_occurrence_patches) loop
        select * into v_occ from public.planning_occurrence where id=(v_patch->>'id')::bigint;
        if v_occ.status not in ('pending','in_progress','deferred','partial') then
            raise exception 'planning detail edit: round already closed' using errcode='PC001';
        end if;
        if exists(select 1 from jsonb_object_keys(v_patch) k
                  where k not in ('id','updated_at','content_snapshot','display_content'))
           and v_scope->>'time'<>'current_and_future' then
            raise exception 'planning detail edit: schedule inputs drifted (execution facts)' using errcode='PC001';
        end if;
        if v_patch ? 'is_parked' and (
            v_occ.window_end_at <= p_now or v_occ.fixed_expires_at <= p_now
            or v_task.request_state='superseded') then
            raise exception 'planning detail edit: round already closed (deadline)' using errcode='PC001';
        end if;
    end loop;
    -- One UPDATE per row, with a hard column whitelist and typed JSON decoding.
    -- Any constraint/trigger/late-row failure rolls back the template and round.
    select string_agg(format('%I = r.%I',k,k),',') into v_columns from jsonb_object_keys(p_task_patch) k;
    if v_columns is not null then
        execute format('update public.planning_task t set %s from jsonb_populate_record(null::public.planning_task,$1) r where t.id=$2',v_columns)
          using p_task_patch,v_task.id;
    end if;
    for v_patch in select * from jsonb_array_elements(p_occurrence_patches) loop
        v_id := (v_patch->>'id')::bigint;
        select string_agg(format('%I = r.%I',k,k),',') into v_columns
          from jsonb_object_keys(v_patch) k where k<>'id';
        execute format('update public.planning_occurrence o set %s from jsonb_populate_record(null::public.planning_occurrence,$1) r where o.id=$2',v_columns)
          using v_patch,v_id;
    end loop;
    -- Scheduling work is registered in this same transaction, respecting the
    -- existing automatic switch when the worker consumes it. No completion or
    -- goal fact is synthesized by these commands.
    if p_task_patch ?| array['estimated_minutes','hollow_start_minutes','hollow_end_minutes','window_start_tod','window_end_tod']
       or exists(select 1 from jsonb_array_elements(p_occurrence_patches) e where e ? 'is_parked') then
        perform public.planning_request_recompute('detail_edit',p_now);
    end if;
    select * into v_task from public.planning_task where id=v_task.id;
    select coalesce(jsonb_agg(to_jsonb(o) order by o.id),'[]'::jsonb) into v_round
      from public.planning_occurrence o where id=any(v_ids);
    v_result := jsonb_build_object('task',to_jsonb(v_task),'occurrences',v_round,'scope',v_scope);
    insert into public.planning_detail_edit_request values(p_request_key,p_intent,v_result,p_now);
    return v_result;
end;
$$;
revoke all on function public.planning_commit_detail_edit(jsonb,jsonb,jsonb,jsonb,uuid,jsonb,timestamptz) from public,anon,authenticated;
grant execute on function public.planning_commit_detail_edit(jsonb,jsonb,jsonb,jsonb,uuid,jsonb,timestamptz) to service_role;

commit;
