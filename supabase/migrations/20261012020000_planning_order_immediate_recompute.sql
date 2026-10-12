-- Sorting confirmation reuses its already validated schedule. Order, estimates,
-- consumed waiting request and idempotent result commit in one transaction.
-- The original RPC remains available for older gateway versions during rollout.
begin;

create or replace function public.planning_save_order_with_schedule(
    p_order jsonb, p_cycle_date date, p_expected jsonb, p_tasks jsonb,
    p_settings jsonb, p_now timestamptz, p_request_key uuid,
    p_recompute_enabled boolean, p_singles jsonb, p_rounds jsonb
) returns jsonb
language plpgsql set search_path = public, pg_temp as $$
declare
    v_replay public.planning_reorder_request%rowtype;
    v_expected jsonb;
    v_written jsonb := '[]'::jsonb;
    v_estimates jsonb := '[]'::jsonb;
    v_token uuid;
    v_result jsonb;
begin
    if jsonb_typeof(p_singles) is distinct from 'array'
       or jsonb_typeof(p_rounds) is distinct from 'array' then
        raise exception 'planning_save_order_with_schedule: invalid schedule';
    end if;

    -- Serialize replay checks and consume only requests preceding this commit.
    -- Every table lock precedes row locks, as in the existing order RPC.
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
            return v_replay.result || jsonb_build_object('replayed', true);
        end if;
    end if;

    -- Reuse all complete-member, occurrence, task and setting guards. This inner
    -- call writes only order; it neither records a result nor requests delayed work.
    if p_recompute_enabled is null then
        raise exception 'planning_save_order_with_schedule: missing recompute setting';
    end if;
    perform public.planning_save_order(p_order, p_cycle_date, p_expected, p_tasks,
                                      p_settings, p_now, null::uuid, false);
    if p_recompute_enabled then
        select request_token into v_token from public.planning_recompute_state where id = 1;
        -- Expected estimates, windows, ownership and lifecycle facts stay at the
        -- calculation snapshot; only the order just written by this transaction
        -- changes. Existing batch/round guards and rollback behavior are reused.
        select coalesce(jsonb_agg(e || jsonb_build_object('sort_order', c.ordinality - 1)), '[]'::jsonb)
          into v_expected
          from jsonb_array_elements(p_expected) e
          join jsonb_array_elements_text(p_order) with ordinality c(id, ordinality)
            on (e->>'id')::bigint = c.id::bigint;
        if jsonb_array_length(p_singles) + jsonb_array_length(p_rounds) > 0 then
            v_written := public.planning_apply_recompute_batch(v_expected, p_singles, p_rounds);
        end if;
        perform public.planning_clear_recompute_mark(v_token);
        select coalesce(jsonb_agg(jsonb_build_object(
            'id', o.id, 'est_start', o.est_start, 'est_end', o.est_end,
            'nominal_start', o.nominal_start, 'estimated_time_source', o.estimated_time_source,
            'fixed_source', o.fixed_source, 'schedule_managed', o.schedule_managed,
            'is_fixed', o.is_fixed) order by o.sort_order), '[]'::jsonb)
          into v_estimates from public.planning_occurrence o
         where o.id in (select value::bigint from jsonb_array_elements_text(p_order));
    elsif jsonb_array_length(p_singles) + jsonb_array_length(p_rounds) > 0 then
        raise exception 'planning_save_order_with_schedule: automatic recompute disabled';
    end if;
    v_result := jsonb_build_object('saved', jsonb_array_length(p_order),
        'recompute_enabled', p_recompute_enabled, 'schedule_applied', p_recompute_enabled,
        'updated', jsonb_array_length(v_written), 'estimates', v_estimates);
    if p_request_key is not null then
        insert into public.planning_reorder_request(request_key, ordered_ids, result, created_at)
          values(p_request_key, p_order, v_result, p_now);
    end if;
    return v_result;
end;
$$;
revoke all on function public.planning_save_order_with_schedule(jsonb,date,jsonb,jsonb,jsonb,timestamptz,uuid,boolean,jsonb,jsonb)
  from public, anon, authenticated;
grant execute on function public.planning_save_order_with_schedule(jsonb,date,jsonb,jsonb,jsonb,timestamptz,uuid,boolean,jsonb,jsonb)
  to service_role;

commit;
