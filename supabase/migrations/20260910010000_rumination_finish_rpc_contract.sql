-- Finish RPC contract: return jsonb instead of void so that the Python
-- _rpc_object() caller can validate the response structure.
--
-- Forward-only follow-up to 20260909010000:
-- 1. finish_rumination_scheduled_execution changes from returns void to
--    returns jsonb. The response contains status, execution_id and changed.
--    A missing execution raises a stable error instead of silently
--    succeeding. Security definer, fixed search_path, service_role only.
-- 2. The function does NOT touch chat_messages, memories, memory_requests,
--    memory_rumination_cursors or memory_relations.
--
-- The old void signature is dropped first so calls with the old shape
-- resolve unambiguously to the new jsonb version.

begin;

drop function if exists public.finish_rumination_scheduled_execution(bigint);

create or replace function public.finish_rumination_scheduled_execution(
    p_execution_id bigint
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_execution public.memory_rumination_scheduled_executions%rowtype;
    v_changed boolean := false;
begin
    select * into v_execution
    from public.memory_rumination_scheduled_executions
    where id = p_execution_id
    for update;
    if not found then
        raise exception 'memory_rumination_scheduled_execution_not_found';
    end if;

    if v_execution.status = 'running' then
        update public.memory_rumination_scheduled_executions
        set status = 'finished',
            finished_at = now()
        where id = v_execution.id;
        v_changed := true;
    end if;

    return jsonb_build_object(
        'status', 'finished',
        'execution_id', v_execution.id,
        'changed', v_changed
    );
end;
$function$;

revoke all on function public.finish_rumination_scheduled_execution(bigint)
    from public, anon, authenticated;
grant execute on function public.finish_rumination_scheduled_execution(bigint)
    to service_role;

commit;
