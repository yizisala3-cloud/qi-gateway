-- Memory digest cross-instance claim and heartbeat
-- 2026-08-11: Replaces time-based stale-run recovery with lease-based heartbeat.
-- Also supports error persistence for scheduled digest failures.

alter table public.memory_digest_runs
    add column if not exists claimed_at timestamptz,
    add column if not exists heartbeat_at timestamptz;

alter table public.memory_digest_runs
    drop constraint if exists memory_digest_runs_status_check,
    add constraint memory_digest_runs_status_check
        check (status = any (array['claimed'::text, 'running'::text, 'succeeded'::text, 'failed'::text, 'skipped'::text]));

create index if not exists memory_digest_runs_heartbeat_idx
    on public.memory_digest_runs (assistant_id, heartbeat_at desc)
    where status in ('claimed', 'running');

comment on column public.memory_digest_runs.claimed_at is
    'When this gateway instance claimed the task. Used for lease-based concurrency control.';
comment on column public.memory_digest_runs.heartbeat_at is
    'Last time the instance reported it is still alive. Stale recovery uses this instead of started_at.';

-- Atomically check for existing non-stale claims and optionally create one.
create or replace function public.claim_digest_slot(
    p_assistant_id text,
    p_trigger text,
    p_mode text
)
returns jsonb
language plpgsql
set search_path to 'public'
as $$
declare
    v_run_id bigint;
    v_stale_cutoff timestamptz := now() - interval '30 minutes';
begin
    -- Serialize per assistant using a dedicated advisory lock namespace
    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 1));

    -- Check for existing non-stale claim or running run
    select id into v_run_id
    from public.memory_digest_runs
    where assistant_id = p_assistant_id
      and status in ('claimed', 'running')
      and claimed_at is not null
      and claimed_at > v_stale_cutoff
      and (heartbeat_at is null or heartbeat_at > v_stale_cutoff)
    order by claimed_at desc
    limit 1;

    if found then
        return jsonb_build_object('status', 'already_running', 'run_id', v_run_id);
    end if;

    -- No active claim: create a claimed run
    insert into public.memory_digest_runs (
        assistant_id,
        trigger,
        mode,
        status,
        claimed_at,
        heartbeat_at,
        started_at
    ) values (
        p_assistant_id,
        p_trigger,
        p_mode,
        'claimed',
        now(),
        now(),
        now()
    )
    returning id into v_run_id;

    return jsonb_build_object('status', 'claimed', 'run_id', v_run_id);
end;
$$;

revoke all on function public.claim_digest_slot(text, text, text) from public, anon, authenticated;
grant execute on function public.claim_digest_slot(text, text, text) to service_role;

-- Update an existing run's heartbeat. Called periodically during long model calls.
create or replace function public.update_digest_heartbeat(p_run_id bigint)
returns void
language plpgsql
set search_path to 'public'
as $$
begin
    update public.memory_digest_runs
    set heartbeat_at = now()
    where id = p_run_id
      and status in ('claimed', 'running');
end;
$$;

revoke all on function public.update_digest_heartbeat(bigint) from public, anon, authenticated;
grant execute on function public.update_digest_heartbeat(bigint) to service_role;

-- Update commit_memory_digest_run to also clear heartbeat on completion
-- (No structural change needed; the existing update already covers it)
