-- Task notes update open occurrences and future templates atomically;
-- completed, discarded and timed-out occurrences retain their historical text.
begin;

alter table public.planning_task add column if not exists note text;
alter table public.planning_occurrence add column if not exists note text;

do $$
begin
    if not exists (select 1 from pg_constraint
                   where conrelid = 'public.planning_task'::regclass
                     and conname = 'planning_task_note_length_check') then
        alter table public.planning_task add constraint planning_task_note_length_check
            check (note is null or char_length(note) <= 60);
    end if;
    if not exists (select 1 from pg_constraint
                   where conrelid = 'public.planning_occurrence'::regclass
                     and conname = 'planning_occurrence_note_length_check') then
        alter table public.planning_occurrence add constraint planning_occurrence_note_length_check
            check (note is null or char_length(note) <= 60);
    end if;
end $$;

comment on column public.planning_task.note is
    'Optional note, at most 60 Unicode characters. Changes apply to open and future occurrences only.';
comment on column public.planning_occurrence.note is
    'Task-note snapshot. Open occurrences track the template; terminal occurrences retain their text, including NULL.';

create or replace function public.planning_initialize_occurrence_note()
returns trigger language plpgsql set search_path = public, pg_temp as $$
begin
    -- Task lock precedes occurrence locks. SHARE also serializes direct inserts
    -- with a template update, ignoring any stale caller-provided snapshot.
    select t.note into new.note from public.planning_task t
        where t.id = new.task_id for share;
    return new;
end;
$$;
drop trigger if exists planning_occurrence_note_initialize on public.planning_occurrence;
create trigger planning_occurrence_note_initialize
    before insert on public.planning_occurrence
    for each row execute function public.planning_initialize_occurrence_note();

create or replace function public.planning_sync_open_task_notes()
returns trigger language plpgsql set search_path = public, pg_temp as $$
declare
    target_id bigint;
begin
    -- Stable task -> occurrence-id lock order matches existing planning RPCs.
    for target_id in
        select o.id from public.planning_occurrence o
         where o.task_id = new.id
           and o.status in ('pending', 'in_progress', 'deferred', 'partial')
         order by o.id for update
    loop
        update public.planning_occurrence
           set note = new.note, updated_at = new.updated_at
         where id = target_id
           and status in ('pending', 'in_progress', 'deferred', 'partial');
    end loop;
    return new;
end;
$$;
drop trigger if exists planning_task_note_sync on public.planning_task;
create trigger planning_task_note_sync
    after update of note on public.planning_task
    for each row when (old.note is distinct from new.note)
    execute function public.planning_sync_open_task_notes();

create or replace function public.planning_preserve_historical_note()
returns trigger language plpgsql set search_path = public, pg_temp as $$
begin
    if old.status not in ('pending', 'in_progress', 'deferred', 'partial')
       and old.note is distinct from new.note then
        raise exception 'planning note: historical note is immutable'
            using errcode = 'PC001';
    end if;
    return new;
end;
$$;
drop trigger if exists planning_occurrence_note_preserve on public.planning_occurrence;
create trigger planning_occurrence_note_preserve
    before update of note on public.planning_occurrence
    for each row execute function public.planning_preserve_historical_note();

revoke all on function public.planning_initialize_occurrence_note() from public, anon, authenticated;
revoke all on function public.planning_sync_open_task_notes() from public, anon, authenticated;
revoke all on function public.planning_preserve_historical_note() from public, anon, authenticated;
grant execute on function public.planning_initialize_occurrence_note() to service_role;
grant execute on function public.planning_sync_open_task_notes() to service_role;
grant execute on function public.planning_preserve_historical_note() to service_role;

commit;
