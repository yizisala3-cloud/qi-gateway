-- Atomically keep the same open todo out of proactive prompts for three hours.
-- This intentionally has no daily or lifetime reminder-count limit.

create table if not exists public.todo_reminder_state (
    todo_id text primary key,
    last_offered_at timestamptz not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

alter table public.todo_reminder_state enable row level security;

revoke all on table public.todo_reminder_state from public, anon, authenticated;
grant select, insert, update on table public.todo_reminder_state to service_role;

create index if not exists todo_reminder_state_last_offered_idx
    on public.todo_reminder_state (last_offered_at);

create or replace function public.claim_proactive_todos(
    p_now timestamptz,
    p_tomorrow_utc timestamptz,
    p_limit integer default 8,
    p_cooldown_minutes integer default 180
)
returns table (
    todo_id text,
    content text,
    scheduled_start timestamptz
)
language plpgsql
security definer
set search_path = public
as $$
declare
    v_limit integer := greatest(1, least(coalesce(p_limit, 8), 20));
    v_cooldown_minutes integer := greatest(1, coalesce(p_cooldown_minutes, 180));
begin
    if p_now is null or p_tomorrow_utc is null or p_tomorrow_utc <= p_now then
        raise exception 'invalid proactive todo time window';
    end if;

    return query
    with candidates as materialized (
        select
            t.id::text as todo_id,
            t.content::text as content,
            t.scheduled_start
        from public.todos as t
        left join public.todo_reminder_state as reminder
            on reminder.todo_id = t.id::text
        where t.is_completed is not true
          and t.is_hidden is not true
          and t.is_start_marker is not true
          and t.is_end_marker is not true
          and coalesce(t.status::text, '') <> 'hollow'
          and (t.scheduled_start is null or t.scheduled_start < p_tomorrow_utc)
          and (
              reminder.last_offered_at is null
              or reminder.last_offered_at <= p_now - make_interval(mins => v_cooldown_minutes)
          )
        order by
            t.scheduled_start asc nulls last,
            coalesce(t.sort_order, 0),
            t.created_at
        limit v_limit
        for update of t skip locked
    ), claimed as (
        insert into public.todo_reminder_state as reminder_state (
            todo_id,
            last_offered_at,
            updated_at
        )
        select
            candidates.todo_id,
            p_now,
            p_now
        from candidates
        on conflict on constraint todo_reminder_state_pkey do update
        set
            last_offered_at = excluded.last_offered_at,
            updated_at = excluded.updated_at
        returning reminder_state.todo_id
    )
    select
        candidates.todo_id,
        candidates.content,
        candidates.scheduled_start
    from candidates
    inner join claimed
        on claimed.todo_id = candidates.todo_id
    order by candidates.scheduled_start asc nulls last;
end;
$$;

revoke all on function public.claim_proactive_todos(
    timestamptz,
    timestamptz,
    integer,
    integer
) from public, anon, authenticated;

grant execute on function public.claim_proactive_todos(
    timestamptz,
    timestamptz,
    integer,
    integer
) to service_role;

