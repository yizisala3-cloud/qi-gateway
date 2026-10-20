-- Parking persists for the selected round and every subsequently generated round.
-- Apply after 20261017040000 and before deploying the matching gateway/frontend.
begin;

-- Preserve existing open parked work on first installation only. Replaying the
-- migration must never re-enable a preference the user has since restored.
-- Keep the complete installation in one DO statement. Dashboard auto-RLS
-- processing must not append ALTER TABLE for a transaction-scoped staging table.
do $planning_upgrade$
declare
    v_anchors jsonb := '[]'::jsonb;
    v_anchor record;
    v_task jsonb;
    v_round jsonb;
    v_patches jsonb;
    v_now timestamptz := statement_timestamp();
begin
    if not exists(select 1 from pg_attribute
        where attrelid='public.planning_task'::regclass
          and attname='is_parked' and not attisdropped) then
        alter table public.planning_task add column is_parked boolean not null default false;
        -- Capture the earliest parked open round per task without a temporary
        -- table or any state that must survive between top-level statements.
        select coalesce(jsonb_agg(jsonb_build_object('task_id',a.task_id,'occurrence_id',a.id)), '[]'::jsonb)
          into v_anchors
          from (select distinct on (o.task_id) o.task_id,o.id
                  from public.planning_occurrence o
                  where o.is_parked and o.status in ('pending','in_progress','deferred','partial')
                  order by o.task_id,o.schedule_date,coalesce(o.fixed_due_at,o.created_at),o.id) a;
    end if;
comment on column public.planning_task.is_parked is
    'New rounds inherit this scheduling preference; parking/restoring also updates the selected complete round atomically.';

create or replace function public.planning_initialize_occurrence_parking()
returns trigger language plpgsql set search_path=public,pg_temp as $$
begin
    -- Use the locked live task, never a caller's stale generation snapshot.
    -- SHARE follows the existing note-initialization task lock protocol.
    if new.status in ('pending','in_progress','deferred','partial') then
        select t.is_parked into new.is_parked from public.planning_task t
            where t.id=new.task_id for share;
    end if;
    return new;
end;
$$;
drop trigger if exists planning_occurrence_parking_initialize on public.planning_occurrence;
create trigger planning_occurrence_parking_initialize before insert on public.planning_occurrence
    for each row execute function public.planning_initialize_occurrence_parking();
revoke all on function public.planning_initialize_occurrence_parking() from public,anon,authenticated;
grant execute on function public.planning_initialize_occurrence_parking() to service_role;

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
    v_anchor public.planning_occurrence%rowtype;
    v_future public.planning_occurrence%rowtype;
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
       'window_start_tod','window_end_tod','alarm_start','alarm_end','timer_minutes','is_parked','updated_at')) then
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
    -- Parking changes the selected complete round and the future template in
    -- the same transaction. Older callers may omit the template patch.
    if p_task_patch ? 'is_parked' and (
        jsonb_typeof(p_task_patch->'is_parked') is distinct from 'boolean'
        or jsonb_array_length(p_occurrence_patches)=0
        or exists(select 1 from jsonb_array_elements(p_occurrence_patches) e
            where e->'is_parked' is distinct from p_task_patch->'is_parked')) then
        raise exception 'planning detail edit: parking template must match the whole round';
    end if;
    if exists(select 1 from jsonb_array_elements(p_occurrence_patches) e where e ? 'is_parked') then
        if jsonb_typeof(p_occurrence_patches->0->'is_parked') is distinct from 'boolean' then
            raise exception 'planning detail edit: invalid parking value';
        end if;
        p_task_patch := p_task_patch || jsonb_build_object(
            'is_parked',p_occurrence_patches->0->'is_parked','updated_at',p_now);
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
    if p_task_patch ? 'is_parked' then
        select * into v_anchor from public.planning_occurrence where id=v_ids[1];
        -- Usually only the due round exists. Include any already-generated
        -- later rounds too; older rounds and terminal history stay frozen.
        for v_future in select o.* from public.planning_occurrence o
            where o.task_id=v_task.id and o.source='schedule'
              and o.status in ('pending','in_progress','deferred','partial')
              and o.round_key is distinct from v_anchor.round_key
              and (o.schedule_date>v_anchor.schedule_date
                or (o.schedule_date=v_anchor.schedule_date
                    and (coalesce(o.fixed_due_at,o.created_at),o.id)
                      >(coalesce(v_anchor.fixed_due_at,v_anchor.created_at),v_anchor.id)))
            order by o.id
        loop
            if exists(select 1 from public.planning_occurrence r
                where (r.id=v_future.id or (r.task_id=v_task.id and v_future.phase is not null
                    and r.round_key=v_future.round_key and r.phase_group=v_future.phase_group))
                and (r.status not in ('pending','deferred') or r.actual_start is not null
                    or r.actual_end is not null or r.partial_at is not null
                    or r.handled_at is not null or r.closed_at is not null)) then
                raise exception 'planning detail edit: schedule inputs drifted (future execution facts)' using errcode='PC001';
            end if;
            if v_future.window_end_at<=p_now or v_future.fixed_expires_at<=p_now
               or (not (p_task_patch->>'is_parked')::boolean
                   and (v_future.is_fixed or v_future.status='deferred') and v_future.est_end<=p_now) then
                raise exception 'planning detail edit: schedule inputs drifted (future deadline)' using errcode='PC001';
            end if;
            v_patch := jsonb_build_object('id',v_future.id,'is_parked',p_task_patch->'is_parked','updated_at',p_now);
            if not (p_task_patch->>'is_parked')::boolean and v_future.is_parked
                and not v_future.is_fixed and v_future.status='pending' then
                v_patch := v_patch || jsonb_build_object('est_start',null,'est_end',null,
                    'estimated_time_source','unassigned','fixed_source',null,'schedule_managed',true,'is_fixed',false);
            end if;
            p_occurrence_patches := p_occurrence_patches || jsonb_build_array(v_patch);
        end loop;
    end if;
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

-- First installation follows the same complete-round, deadline and execution
-- guards as a live parking command, including already-generated later rounds.
-- Any unsafe round aborts this entire migration, including schema and receipts.
-- On replay the captured anchor list is empty; restored preferences stay restored.
    for v_anchor in select * from jsonb_to_recordset(v_anchors)
        as a(task_id bigint,occurrence_id bigint) order by task_id loop
        select to_jsonb(t) into v_task from public.planning_task t where t.id=v_anchor.task_id for update;
        select jsonb_agg(to_jsonb(o) order by o.id),
               jsonb_agg(jsonb_build_object('id',o.id,'is_parked',true,'updated_at',v_now) order by o.id)
          into v_round,v_patches
          from public.planning_occurrence o
          join public.planning_occurrence a on a.id=v_anchor.occurrence_id
          where o.id=a.id or (o.task_id=a.task_id and a.phase is not null
              and o.round_key=a.round_key and o.phase_group=a.phase_group);
        begin
            perform public.planning_commit_detail_edit(v_task,v_round,
                jsonb_build_object('is_parked',true,'updated_at',v_now),v_patches,
                gen_random_uuid(),jsonb_build_object('migration','20261020010000',
                    'task_id',v_anchor.task_id,'occurrence_id',v_anchor.occurrence_id,'parked',true),v_now);
        exception when others then
            raise exception 'planning persistent parking upgrade: task %, round %: %',
                v_anchor.task_id,v_anchor.occurrence_id,SQLERRM using errcode=SQLSTATE;
        end;
    end loop;
end;
$planning_upgrade$;

commit;
