-- Atomic, calendar-day idempotent heat decay for P2 memory retrieval.
-- chat_messages remains an immutable read-only source and is not modified.

create table if not exists public.memory_heat_runs (
    run_date date primary key,
    elapsed_days integer not null check (elapsed_days between 1 and 30),
    updated_count integer not null default 0 check (updated_count >= 0),
    archived_count integer not null default 0 check (archived_count >= 0),
    executed_at timestamptz not null default now()
);

comment on table public.memory_heat_runs is
    'One successful memory heat maintenance run per Asia/Shanghai calendar day.';

alter table public.memory_heat_runs enable row level security;

revoke all on table public.memory_heat_runs from public, anon, authenticated;
grant select, insert, update on table public.memory_heat_runs to service_role;

create or replace function public.run_memory_heat_decay(
    run_at timestamptz default now()
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_run_at timestamptz := coalesce(run_at, now());
    v_run_date date;
    v_elapsed_days integer;
    v_updated_count integer := 0;
    v_archived_count integer := 0;
begin
    v_run_date := timezone('Asia/Shanghai', v_run_at)::date;

    select greatest(
        1,
        least(coalesce(v_run_date - max(history.run_date), 1), 30)
    )
    into v_elapsed_days
    from public.memory_heat_runs as history
    where history.run_date < v_run_date;

    insert into public.memory_heat_runs (run_date, elapsed_days, executed_at)
    values (v_run_date, v_elapsed_days, v_run_at)
    on conflict (run_date) do nothing;

    if not found then
        return jsonb_build_object(
            'status', 'already_ran',
            'run_date', v_run_date,
            'elapsed_days', 0,
            'updated_count', 0,
            'archived_count', 0
        );
    end if;

    with candidates as (
        select
            memory.id,
            greatest(
                0.0,
                least(
                    100.0,
                    memory.heat * power(
                        0.95,
                        v_elapsed_days::double precision
                            / (1.0 + least(
                                greatest(coalesce(memory.emotion_weight, 0.5), 0.0),
                                1.0
                            ) * 0.5)
                    )
                )
            ) as new_heat,
            coalesce(memory.last_recalled_at, memory.created_at, v_run_at) as reference_at
        from public.memories as memory
        where memory.is_active = true
          and memory.verified = 'verified'
          and memory.importance < 10
    ),
    changed as (
        update public.memories as memory
        set
            heat = round(candidate.new_heat::numeric, 2)::double precision,
            is_active = case
                when memory.layer = '碎片'
                 and memory.importance <= 3
                 and candidate.new_heat < 5.0
                 and candidate.reference_at < v_run_at - interval '90 days'
                then false
                else true
            end
        from candidates as candidate
        where memory.id = candidate.id
          and (
              abs(candidate.new_heat - memory.heat) >= 0.005
              or (
                  memory.layer = '碎片'
                  and memory.importance <= 3
                  and candidate.new_heat < 5.0
                  and candidate.reference_at < v_run_at - interval '90 days'
              )
          )
        returning not memory.is_active as archived
    )
    select
        count(*)::integer,
        count(*) filter (where changed.archived)::integer
    into v_updated_count, v_archived_count
    from changed;

    update public.memory_heat_runs
    set
        updated_count = v_updated_count,
        archived_count = v_archived_count,
        executed_at = v_run_at
    where run_date = v_run_date;

    return jsonb_build_object(
        'status', 'succeeded',
        'run_date', v_run_date,
        'elapsed_days', v_elapsed_days,
        'updated_count', v_updated_count,
        'archived_count', v_archived_count
    );
end;
$function$;

revoke all on function public.run_memory_heat_decay(timestamptz)
from public, anon, authenticated;

grant execute on function public.run_memory_heat_decay(timestamptz)
to service_role;

