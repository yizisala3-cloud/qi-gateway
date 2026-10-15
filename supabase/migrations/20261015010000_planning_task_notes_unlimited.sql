-- Remove the note-specific length cap without changing snapshot/history rules.
begin;
set local lock_timeout = '3s';
set local statement_timeout = '15s';

alter table public.planning_task
    drop constraint if exists planning_task_note_length_check;
alter table public.planning_occurrence
    drop constraint if exists planning_occurrence_note_length_check;

comment on column public.planning_task.note is
    'Optional note with no application length limit. Changes apply to open and future occurrences only.';

commit;
