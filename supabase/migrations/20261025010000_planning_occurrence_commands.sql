-- Common lifecycle commands commit their fact, interval baseline, goal trigger
-- and recompute request together. Request identity survives a lost HTTP reply.
begin;
create table if not exists public.planning_occurrence_command_request (
    request_key uuid primary key,
    intent jsonb not null,
    result jsonb not null,
    created_at timestamptz not null default now()
);
alter table public.planning_occurrence_command_request enable row level security;
revoke all on public.planning_occurrence_command_request from public, anon, authenticated;
grant all on public.planning_occurrence_command_request to service_role;
create index if not exists planning_occurrence_command_request_task_idx
  on public.planning_occurrence_command_request ((result #>> '{task,id}'));

-- Physical deletion must erase every full receipt in the same transaction.
-- Keep only the request identity/digest so an uncertain retry cannot replay
-- deleted business content or reuse the identity for another operation.
create or replace function public.planning_erase_task_command_receipts()
returns trigger language plpgsql set search_path = pg_catalog, public as $$
begin
    update public.planning_occurrence_command_request
       set result = jsonb_build_object('deleted', true)
     where result #>> '{task,id}' = old.id::text;
    return old;
end $$;
revoke all on function public.planning_erase_task_command_receipts()
  from public, anon, authenticated;
drop trigger if exists planning_erase_task_command_receipts on public.planning_task;
create trigger planning_erase_task_command_receipts after delete on public.planning_task
  for each row execute function public.planning_erase_task_command_receipts();

create or replace function public.planning_occurrence_command(
    p_occurrence_id bigint, p_status text, p_request_key uuid,
    p_partial_note text, p_logged_seconds bigint, p_now timestamptz
) returns jsonb language plpgsql
set search_path = pg_catalog, public as $$
declare
    intent_value jsonb := jsonb_build_object('id', p_occurrence_id, 'status', p_status,
        'partial_note', p_partial_note, 'logged_seconds', p_logged_seconds);
    intent_digest jsonb := jsonb_build_object('sha256',
        encode(sha256(convert_to(intent_value::text, 'UTF8')), 'hex'));
    previous public.planning_occurrence_command_request%rowtype;
    occurrence public.planning_occurrence%rowtype;
    task public.planning_task%rowtype;
    task_id_value bigint;
    next_start timestamptz;
    next_end timestamptz;
    next_source text;
    minutes_value integer;
    raw_minutes numeric;
    newly_handled boolean;
    handled timestamptz;
    interval_minutes integer;
    recompute_enabled boolean;
    result_value jsonb;
begin
    if p_request_key is null or p_now is null or p_status is null
       or p_status not in ('in_progress', 'partial', 'completed')
       or (p_status = 'partial' and (nullif(btrim(p_partial_note), '') is null
                                   or length(p_partial_note) > 1000))
       or (p_logged_seconds is not null and
           (p_status <> 'completed' or p_logged_seconds not between 1 and 86400)) then
        raise exception using errcode = 'PO400', message = '待办操作参数无效';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(p_request_key::text, 20261025));
    select * into previous from public.planning_occurrence_command_request
     where request_key = p_request_key;
    if found then
        if previous.intent <> intent_value and previous.intent <> intent_digest then
            raise exception using errcode = 'PO409', message = '操作身份已用于其它内容，请核实原操作';
        end if;
        if previous.result ->> 'deleted' = 'true' then
            raise exception using errcode = 'PO404', message = '待办记录已删除';
        end if;
        return previous.result || jsonb_build_object('replayed', true);
    end if;
    select task_id into task_id_value from public.planning_occurrence where id = p_occurrence_id;
    if not found then
        raise exception using errcode = 'PO404', message = '待办记录不存在';
    end if;
    -- Same task/round lock order as the round editing/deletion commands.
    select * into task from public.planning_task where id = task_id_value for update;
    if not found then
        raise exception using errcode = 'PO404', message = '待办任务不存在';
    end if;
    perform id from public.planning_occurrence where task_id = task_id_value
      and round_key = (select round_key from public.planning_occurrence where id = p_occurrence_id)
      order by id for update;
    select * into occurrence from public.planning_occurrence where id = p_occurrence_id;
    if not found then
        raise exception using errcode = 'PO404', message = '待办记录不存在';
    end if;
    if occurrence.round_key is null then
        raise exception using errcode = 'PO409', message = '旧实例须在受控升级后处理';
    end if;
    if (p_status = 'in_progress' and occurrence.status not in ('pending','deferred','partial'))
       or (p_status = 'partial' and occurrence.status not in ('pending','in_progress','deferred','partial'))
       or (p_status = 'completed' and occurrence.status not in
           ('pending','in_progress','deferred','partial','completed','discarded','discarded_this')) then
        raise exception using errcode = 'PO422', message = '待办状态已改变，请刷新后重试';
    end if;
    next_start := occurrence.actual_start;
    next_end := occurrence.actual_end;
    next_source := occurrence.actual_time_source;
    if p_status = 'in_progress' and next_start is null then
        next_start := p_now;
        next_source := 'user';
    end if;
    if p_status = 'completed' and next_end is null then
        next_end := p_now;
        next_source := case when next_start is null then 'user'
                            else coalesce(next_source, 'system') end;
    end if;
    minutes_value := occurrence.actual_minutes;
    if p_status = 'completed' or next_start is distinct from occurrence.actual_start then
        minutes_value := null;
        if next_start is not null and next_end is not null then
            if next_end < next_start then
                raise exception using errcode = 'PO400', message = '实际结束时间不能早于实际开始时间';
            end if;
            -- Python round(): ties round to the nearest even minute.
            raw_minutes := extract(epoch from (next_end - next_start)) / 60;
            minutes_value := floor(raw_minutes)::integer;
            if raw_minutes - floor(raw_minutes) > 0.5
               or (raw_minutes - floor(raw_minutes) = 0.5 and minutes_value % 2 = 1) then
                minutes_value := minutes_value + 1;
            end if;
        end if;
    end if;
    newly_handled := p_status = 'completed' and occurrence.handled_at is null;
    update public.planning_occurrence set
        status = p_status, actual_start = next_start, actual_end = next_end,
        actual_time_source = next_source, actual_minutes = minutes_value,
        actual_logged_seconds = coalesce(p_logged_seconds, actual_logged_seconds),
        partial_note = case when p_status = 'partial' then p_partial_note else partial_note end,
        partial_at = case when p_status = 'partial' then p_now else partial_at end,
        handled_at = case when p_status = 'partial' then null
                          when newly_handled then p_now else handled_at end,
        closed_at = case when p_status <> 'completed' then null
                         when occurrence.status in ('pending','in_progress','deferred','partial')
                         then p_now else closed_at end,
        updated_at = p_now
     where id = p_occurrence_id returning * into occurrence;
    if newly_handled and task.task_type = 'interval'
       and task.refresh_mode = 'after_completion' and task.is_active then
        select case when bool_and(status in ('completed','discarded_this') and handled_at is not null)
                    then max(handled_at) end into handled
          from public.planning_occurrence where task_id = task.id and round_key = occurrence.round_key;
        if handled is not null then
            interval_minutes := coalesce(task.after_completion_minutes, task.interval_days * 1440);
            if interval_minutes is null or interval_minutes not between 1 and 525600 then
                raise exception using errcode = 'PO409', message = '处理后刷新间隔无效，无法推进下一轮';
            end if;
            update public.planning_task set last_handled_at = handled,
                refresh_next_due_at = handled + make_interval(mins => interval_minutes),
                updated_at = p_now where id = task.id returning * into task;
        end if;
    end if;
    if p_status = 'completed' then
        select case when jsonb_typeof(value) = 'boolean' then value::text::boolean else true end
          into recompute_enabled from public.app_settings where key = 'planning.auto_recompute_enabled';
        if coalesce(recompute_enabled, true) then
            perform public.planning_request_recompute('status_change', p_now);
        end if;
    end if;
    result_value := jsonb_build_object('occurrence', to_jsonb(occurrence), 'task', to_jsonb(task));
    insert into public.planning_occurrence_command_request(request_key, intent, result, created_at)
      values(p_request_key, intent_digest, result_value, p_now);
    return result_value;
end $$;
revoke all on function public.planning_occurrence_command(bigint,text,uuid,text,bigint,timestamptz)
  from public, anon, authenticated;
grant execute on function public.planning_occurrence_command(bigint,text,uuid,text,bigint,timestamptz)
  to service_role;
-- Read both creation identities in one snapshot; a deleted request remains a
-- tombstone and must never turn into a second task on an uncertain retry.
create or replace function public.planning_creation_identity(p_request_key text)
returns jsonb language sql stable set search_path = pg_catalog, public as $$
    select jsonb_build_object(
        'task', (select to_jsonb(t) from public.planning_task t where creation_request_key = p_request_key),
        'tombstone', (select to_jsonb(r) from public.planning_creation_request r where request_key = p_request_key))
$$;
revoke all on function public.planning_creation_identity(text) from public, anon, authenticated;
grant execute on function public.planning_creation_identity(text) to service_role;

-- Also handle replay of this unreleased migration over a previous local build:
-- normalize old raw intents, then erase receipts whose tasks were already removed.
update public.planning_occurrence_command_request
   set intent = jsonb_build_object('sha256',
       encode(sha256(convert_to(intent::text, 'UTF8')), 'hex'))
 where not (intent ? 'sha256');
update public.planning_occurrence_command_request r
   set result = jsonb_build_object('deleted', true)
 where r.result #>> '{task,id}' is not null
   and not exists (select 1 from public.planning_task t
       where t.id = (r.result #>> '{task,id}')::bigint);
commit;
