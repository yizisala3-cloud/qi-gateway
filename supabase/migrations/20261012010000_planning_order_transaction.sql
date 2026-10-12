-- #45/#46: commit the validated complete order and its recompute request together.
-- A short NOWAIT table lock protects membership (including concurrent inserts),
-- task fallback inputs and settings without introducing triggers into other writes.
-- Contention is a clean PC001 rejection, never a wait with inverted row locks.
begin;

create table if not exists public.planning_reorder_request (
    request_key uuid primary key,
    ordered_ids jsonb not null,
    result jsonb not null,
    created_at timestamptz not null
);
alter table public.planning_reorder_request enable row level security;
revoke all on public.planning_reorder_request from public, anon, authenticated;
grant select, insert on public.planning_reorder_request to service_role;

create or replace function public.planning_save_order(
    p_order jsonb, p_cycle_date date, p_expected jsonb, p_tasks jsonb,
    p_settings jsonb, p_now timestamptz, p_request_key uuid,
    p_recompute_enabled boolean
) returns jsonb
language plpgsql set search_path = public, pg_temp as $$
declare
    v_ids bigint[];
    v_current bigint[];
    v_result jsonb;
    v_replay public.planning_reorder_request%rowtype;
begin
    if jsonb_typeof(p_order) is distinct from 'array'
       or jsonb_typeof(p_expected) is distinct from 'array'
       or jsonb_typeof(p_tasks) is distinct from 'array'
       or jsonb_typeof(p_settings) is distinct from 'object'
       or p_cycle_date is null or p_now is null or p_recompute_enabled is null then
        raise exception 'planning_save_order: invalid payload';
    end if;
    select array_agg(value::bigint order by ordinality) into v_ids
      from jsonb_array_elements_text(p_order) with ordinality;
    v_ids := coalesce(v_ids, '{}'::bigint[]);
    if cardinality(v_ids) <> (select count(distinct x) from unnest(v_ids) x)
       or exists(select 1 from unnest(v_ids) x where x is null or x < 1) then
        raise exception 'planning_save_order: invalid order';
    end if;

    -- Take every potentially conflicting table lock before acquiring any row lock.
    -- NOWAIT avoids cycles with lifecycle commands that already hold row locks.
    begin
        lock table public.planning_task, public.planning_occurrence,
                   public.app_settings, public.planning_recompute_state
          in exclusive mode nowait;
    exception when lock_not_available then
        raise exception 'planning_save_order: inputs changed (concurrent write)'
          using errcode = 'PC001';
    end;

    if p_request_key is not null then
        select * into v_replay from public.planning_reorder_request
          where request_key = p_request_key;
        if found then
            if v_replay.ordered_ids is distinct from p_order then
                raise exception 'planning_save_order: inputs changed (request reused)'
                  using errcode = 'PC001';
            end if;
            return v_replay.result;
        end if;
    end if;

    select coalesce(array_agg(id order by id), '{}'::bigint[]) into v_current
      from public.planning_occurrence
     where display_cycle_date = p_cycle_date
       and status in ('pending', 'in_progress', 'deferred', 'partial');
    if v_current is distinct from (select coalesce(array_agg(x order by x), '{}'::bigint[])
                                   from unnest(v_ids) x)
       or jsonb_array_length(p_expected) <> cardinality(v_ids)
       or v_current is distinct from (
         select coalesce(array_agg((e->>'id')::bigint order by (e->>'id')::bigint), '{}'::bigint[])
           from jsonb_array_elements(p_expected) e)
       or exists (
         select 1 from jsonb_array_elements(p_expected) e
         left join public.planning_occurrence o on o.id = (e->>'id')::bigint
         where o.id is null
            or to_jsonb(o) is distinct from
               to_jsonb(jsonb_populate_record(null::public.planning_occurrence, e)))
       or (select count(distinct (e->>'id')::bigint) from jsonb_array_elements(p_expected) e)
          <> cardinality(v_ids) then
        raise exception 'planning_save_order: inputs changed (members or occurrences)'
          using errcode = 'PC001';
    end if;
    if (select count(distinct task_id) from public.planning_occurrence where id = any(v_ids))
          <> jsonb_array_length(p_tasks)
       or exists (
         select 1 from jsonb_array_elements(p_tasks) e
         left join public.planning_task t on t.id = (e->>'id')::bigint
         where t.id is null or not exists (
            select 1 from public.planning_occurrence o where o.id = any(v_ids) and o.task_id = t.id)
            or to_jsonb(t) is distinct from
               to_jsonb(jsonb_populate_record(null::public.planning_task, e)))
       or (select count(distinct (e->>'id')::bigint) from jsonb_array_elements(p_tasks) e)
          <> jsonb_array_length(p_tasks) then
        raise exception 'planning_save_order: inputs changed (tasks)'
          using errcode = 'PC001';
    end if;
    if not p_settings ?& array['planning.refresh_boundary_state', 'planning.auto_recompute_enabled']
       or exists (
         select 1 from jsonb_each(p_settings) s
          where s.value is distinct from coalesce(
            (select value from public.app_settings where key = s.key), 'null'::jsonb)) then
        raise exception 'planning_save_order: inputs changed (settings)'
          using errcode = 'PC001';
    end if;

    update public.planning_occurrence o
       set sort_order = candidate.ordinality - 1, updated_at = p_now
      from unnest(v_ids) with ordinality candidate(id, ordinality)
     where o.id = candidate.id;
    if p_recompute_enabled then
        perform public.planning_request_recompute('reorder', p_now);
    end if;
    v_result := jsonb_build_object('saved', cardinality(v_ids),
                                  'recompute_enabled', p_recompute_enabled);
    if p_request_key is not null then
        insert into public.planning_reorder_request(request_key, ordered_ids, result, created_at)
          values(p_request_key, p_order, v_result, p_now);
    end if;
    return v_result;
end;
$$;
revoke all on function public.planning_save_order(jsonb,date,jsonb,jsonb,jsonb,timestamptz,uuid,boolean)
  from public, anon, authenticated;
grant execute on function public.planning_save_order(jsonb,date,jsonb,jsonb,jsonb,timestamptz,uuid,boolean)
  to service_role;

commit;
