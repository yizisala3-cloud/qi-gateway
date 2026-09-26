-- Phase 1A: additive domain vocabulary only. Do not deploy this migration
-- while the legacy planning maintenance writer is still active. Phase 5 owns
-- the controlled backup, old-instance cleanup and deployment sequence.
-- Existing occurrence rows are deliberately not backfilled: old for_date,
-- estimated time and is_fixed cannot prove round or manual ownership.
--
-- Round identity is frozen at creation: a round generated under the rules of
-- its time is a settled business fact, and no later rule or boundary edit may
-- rebuild, re-verify or re-interpret it. Rule and boundary changes only shape
-- rounds that do not exist yet; a refresh-boundary change takes effect from
-- the next planning cycle, so display_cycle_date >= schedule_date always.
begin;

alter table public.planning_task
    add column if not exists refresh_mode text,
    add column if not exists refresh_anchor_at timestamptz,
    add column if not exists last_handled_at timestamptz,
    add column if not exists refresh_enabled boolean not null default true;

alter table public.planning_task
    add constraint planning_task_refresh_mode_check
    check (refresh_mode is null or refresh_mode in (
        'daily', 'fixed_interval', 'fixed_weekday', 'fixed_monthday',
        'after_completion', 'none'
    ));

alter table public.planning_task
    add constraint planning_task_refresh_type_check
    check (refresh_mode is null or (
        (task_type = 'daily' and refresh_mode = 'daily')
        or (task_type = 'interval' and refresh_mode in ('fixed_interval', 'after_completion'))
        or (task_type = 'weekly' and refresh_mode = 'fixed_weekday')
        or (task_type = 'monthly' and refresh_mode = 'fixed_monthday')
        or (task_type in ('once', 'idle') and refresh_mode = 'none')
    ));

alter table public.planning_task
    add constraint planning_task_refresh_anchor_check
    check (refresh_mode is null or
        ((refresh_mode = 'fixed_interval') = (refresh_anchor_at is not null)));

alter table public.planning_task
    add constraint planning_task_handled_baseline_check
    check (refresh_mode is null or refresh_mode = 'after_completion' or last_handled_at is null);

comment on column public.planning_task.refresh_mode is
    'New lifecycle mode; null denotes an unclassified legacy definition, never an inferred mode.';
comment on column public.planning_task.refresh_anchor_at is
    'Stable first baseline for fixed-interval rounds; never derived from estimated execution.';
comment on column public.planning_task.last_handled_at is
    'Confirmed treatment baseline for after-completion rounds only.';
comment on column public.planning_task.refresh_enabled is
    'Task-level future-round switch; existing occurrences are not closed by changing it.';

alter table public.planning_occurrence
    add column if not exists round_key text,
    add column if not exists schedule_date date,
    add column if not exists display_cycle_date date,
    add column if not exists display_reason text,
    add column if not exists fixed_due_at timestamptz,
    add column if not exists phase_group uuid,
    add column if not exists handled_at timestamptz,
    add column if not exists partial_at timestamptz,
    add column if not exists planned_minutes integer,
    add column if not exists planned_wait_minutes integer,
    add column if not exists estimated_time_source text,
    add column if not exists fixed_source text,
    add column if not exists schedule_managed boolean,
    -- BF5（第七轮）：生成时冻结的展示/规则快照——任务定义后续修改只影响
    -- 未来实例，已生成实例不再被当前任务定义重新解释。
    add column if not exists content_snapshot text,
    add column if not exists display_content text,
    add column if not exists time_mode_snapshot text,
    add column if not exists deadline_at timestamptz;

-- New rows are identified by round_key. Legacy rows remain null until the
-- one-time replacement; no guesses are made from for_date or est_start.
alter table public.planning_occurrence
    add constraint planning_occurrence_phase1a_identity_check
    check ((
        round_key is null or (
            length(btrim(round_key)) > 0
            and (
                round_key = 'once'
                or round_key = 'cycle:' || schedule_date::text
                or (
                    round_key ~ '^(fixed|handled|early):[0-9]{4}-[0-9]{2}-[0-9]{2}:[0-9a-f]{32}$'
                    and (
                        round_key like 'fixed:%'
                        or split_part(round_key, ':', 2) = schedule_date::text
                    )
                )
            )
            and schedule_date is not null
            and for_date = schedule_date
            and display_cycle_date is not null
            and display_cycle_date >= schedule_date
            and display_reason is not null
            and display_reason in ('initial', 'carryover', 'manual_defer')
            and ((display_reason = 'initial') = (display_cycle_date = schedule_date))
            and ((phase is null and phase_group is null)
                 or (phase is not null and phase_group is not null))
            and (phase_group is null or phase_group = md5(task_id::text || ':' || round_key)::uuid)
            and ((status in ('completed', 'discarded_this', 'discarded', 'timeout'))
                 = (closed_at is not null))
            and (status not in ('completed', 'discarded_this') or handled_at is not null)
            and (status not in ('pending', 'in_progress', 'deferred', 'partial', 'timeout')
                 or handled_at is null)
            and estimated_time_source is not null
            and estimated_time_source in ('unassigned', 'rule', 'automatic', 'manual')
            and (fixed_source is null or fixed_source in ('rule', 'manual'))
            and schedule_managed is not null
            -- BF5：生成时快照必须随新行落库——已生成实例的展示内容与规则
            -- 语义来自快照，绝不回读任务当前定义。
            and content_snapshot is not null
            and display_content is not null
            and time_mode_snapshot is not null
            and time_mode_snapshot in ('duration', 'explicit')
            and (is_limited = (deadline_at is not null))
            and (est_start is null) = (est_end is null)
            and (est_start is null or est_end > est_start)
            and (
                (estimated_time_source = 'unassigned' and est_start is null
                    and not is_fixed and fixed_source is null and schedule_managed)
                or (estimated_time_source = 'automatic' and est_start is not null
                    and not is_fixed and fixed_source is null and schedule_managed)
                or (estimated_time_source = 'rule' and est_start is not null
                    and schedule_managed and (
                        (is_fixed and fixed_source = 'rule')
                        or (not is_fixed and fixed_source is null)
                    ))
                or (estimated_time_source = 'manual' and est_start is not null
                    and is_fixed and fixed_source = 'manual' and not schedule_managed)
            )
        )
    ) is true);

-- 实例级排程耗时：创建时从任务定义快照，任务定义后续修改只影响未来轮次。
alter table public.planning_occurrence
    add constraint planning_occurrence_planned_minutes_check
    check (planned_minutes is null or planned_minutes between 1 and 1440);
alter table public.planning_occurrence
    add constraint planning_occurrence_planned_wait_minutes_check
    check (planned_wait_minutes is null or planned_wait_minutes between 1 and 1440);

-- The old date index rejects legal cross-day rounds; it is replaced only in
-- this controlled, not-yet-deployed migration. All sources share round identity.
drop index if exists public.planning_occurrence_schedule_slot_uq;
create unique index planning_occurrence_round_phase_uq
    on public.planning_occurrence (task_id, round_key, coalesce(phase, ''))
    where round_key is not null;

create or replace function public.planning_validate_occurrence_identity()
returns trigger language plpgsql as $$
declare
    current_row public.planning_occurrence%rowtype;
    task_record public.planning_task%rowtype;
    sibling public.planning_occurrence%rowtype;
    phase_count integer;
begin
    if tg_op = 'DELETE' then
        if old.round_key is null then
            return null;
        end if;
        select count(*) into phase_count from public.planning_occurrence
        where task_id = old.task_id and round_key = old.round_key;
        if phase_count <> 0 then
            raise exception 'cannot delete only part of a business round';
        end if;
        return null;
    end if;
    if tg_op = 'UPDATE' and old.round_key is not null and (
        old.task_id is distinct from new.task_id
        or old.round_key is distinct from new.round_key
        or old.schedule_date is distinct from new.schedule_date
        or old.phase is distinct from new.phase
        or old.phase_group is distinct from new.phase_group
        or old.source is distinct from new.source
        or old.fixed_due_at is distinct from new.fixed_due_at
    ) then
        raise exception 'business round identity is immutable';
    end if;
    -- 过去不重写：已关闭 / 已超时的实例不得通过直接改库回到开放生命周期
    -- （限时超时的出口是新建单次待办，不是改写旧实例状态）。
    if tg_op = 'UPDATE' and old.round_key is not null and (
        old.status in ('completed', 'discarded_this', 'discarded', 'timeout')
        and new.status in ('pending', 'in_progress', 'deferred', 'partial')
    ) then
        raise exception 'closed occurrences cannot re-enter the open lifecycle';
    end if;
    -- handled_at 是历史业务事实：确认处理一旦写入，后续状态不得改写。
    if tg_op = 'UPDATE' and old.round_key is not null and (
        old.handled_at is not null
        and new.handled_at is distinct from old.handled_at
    ) then
        raise exception 'confirmed handling instant is immutable once written';
    end if;
    -- L1：已超时实例与业务层同规则——只能转向废弃类，不得标记完成。
    if tg_op = 'UPDATE' and old.round_key is not null and (
        old.status = 'timeout'
        and new.status not in ('timeout', 'discarded_this', 'discarded')
    ) then
        raise exception 'timed-out occurrences can only be discarded';
    end if;
    select * into current_row from public.planning_occurrence where id = new.id;
    if not found or current_row.round_key is null then
        return null; -- legacy rows are handled only by the controlled Phase 5 upgrade
    end if;
    select * into task_record from public.planning_task where id = current_row.task_id;
    if not found or task_record.refresh_mode is null then
        raise exception 'new occurrence requires a classified task definition';
    end if;
    if (current_row.source = 'early') <> (current_row.round_key like 'early:%') then
        raise exception 'early round source and identity disagree';
    end if;
    if tg_op = 'INSERT' and current_row.source = 'early'
        and task_record.refresh_mode not in
            ('fixed_interval', 'fixed_weekday', 'fixed_monthday', 'after_completion') then
        raise exception 'early round requires a refreshable task definition';
    end if;
    -- N6/M5：固定刷新型的额外完成必须携带周期身份，且整个生命周期内
    -- 不可变——INSERT 与 UPDATE 都不得以 NULL 或改值绕过周期唯一约束。
    if current_row.source = 'early'
        and task_record.refresh_mode in
            ('daily', 'fixed_interval', 'fixed_weekday', 'fixed_monthday')
        and current_row.early_period_date is null then
        raise exception 'fixed-refresh early completion requires a period identity';
    end if;
    if tg_op = 'UPDATE' and old.round_key is not null and (
        old.source = 'early'
        and old.early_period_date is distinct from new.early_period_date
    ) then
        raise exception 'early completion period identity is immutable';
    end if;
    if tg_op = 'INSERT' and current_row.source = 'schedule' and not (
        (task_record.refresh_mode = 'none' and current_row.round_key = 'once')
        or (task_record.refresh_mode = 'after_completion' and current_row.round_key like 'handled:%')
        or (task_record.refresh_mode = 'fixed_interval' and current_row.round_key like 'fixed:%')
        or (task_record.refresh_mode in ('daily', 'fixed_weekday', 'fixed_monthday')
            and current_row.round_key = 'cycle:' || current_row.schedule_date::text)
    ) then
        raise exception 'round identity does not match task refresh mode';
    end if;
    if tg_op = 'INSERT' and ((task_record.is_hollow and current_row.phase is null)
        or (not task_record.is_hollow and current_row.phase is not null)) then
        raise exception 'new round shape disagrees with task definition';
    end if;
    if current_row.phase is not null then
        select count(*) into phase_count from public.planning_occurrence
        where task_id = current_row.task_id and round_key = current_row.round_key
          and phase in ('start', 'end');
        if phase_count <> 2 then
            raise exception 'hollow round requires both start and end phases';
        end if;
    end if;
    if current_row.phase is not null then
        select * into sibling from public.planning_occurrence
        where task_id = current_row.task_id and round_key = current_row.round_key
          and phase <> current_row.phase and id <> current_row.id limit 1;
        if found and (
            sibling.schedule_date is distinct from current_row.schedule_date
            or sibling.display_cycle_date is distinct from current_row.display_cycle_date
            or sibling.display_reason is distinct from current_row.display_reason
            or sibling.phase_group is distinct from current_row.phase_group
        ) then
            raise exception 'hollow phases disagree on round or display identity';
        end if;
    end if;
    return null;
end;
$$;

create constraint trigger planning_occurrence_identity_guard
after insert or update or delete on public.planning_occurrence
deferrable initially deferred
for each row execute function public.planning_validate_occurrence_identity();

create index planning_occurrence_display_cycle_idx
    on public.planning_occurrence (display_cycle_date, status)
    where round_key is not null;

comment on column public.planning_occurrence.round_key is
    'Stable business-round identity: cycle:YYYY-MM-DD, once, or fixed/handled/early date plus token digest. Fixed-interval rounds derive theirs from the due event, independent of any refresh boundary.';
comment on column public.planning_occurrence.schedule_date is
    'Original business-round planning cycle date; never updated by scheduling, carryover or boundary changes.';
comment on column public.planning_occurrence.display_cycle_date is
    'Current display cycle; always at or after schedule_date because a boundary change takes effect from the next cycle.';
comment on column public.planning_occurrence.display_reason is
    'Initial placement, automatic carryover, or explicit user defer.';
comment on column public.planning_occurrence.fixed_due_at is
    'Immutable rule due instant that produced this round; provenance for fixed refresh rounds.';
comment on column public.planning_occurrence.phase_group is
    'Shared identity of start and end rows belonging to one hollow round.';
comment on column public.planning_occurrence.handled_at is
    'Confirmed handling instant for this round; not estimated execution time.';
comment on column public.planning_occurrence.planned_minutes is
    'Instance-level scheduling duration snapshotted at creation; later task edits shape future rounds only.';
comment on column public.planning_occurrence.planned_wait_minutes is
    'Instance-level hollow wait used to anchor the end phase; snapshotted at creation.';
comment on column public.planning_occurrence.partial_at is
    'Latest partial-completion instant; the round stays open until it is fully completed.';
comment on column public.planning_occurrence.estimated_time_source is
    'Rule, automatic scheduler, user, or unassigned; independent of execution date.';
comment on column public.planning_occurrence.fixed_source is
    'Rule or user-owned fixed anchor; null means no fixed anchor.';
comment on column public.planning_occurrence.schedule_managed is
    'Whether system scheduling may manage estimated time; manual anchors are false.';
comment on column public.planning_occurrence.content_snapshot is
    'Task content frozen at generation; later task renames shape future rounds only.';
comment on column public.planning_occurrence.display_content is
    'User-facing display content frozen at generation (hollow phases carry their stage text).';
comment on column public.planning_occurrence.time_mode_snapshot is
    'Time mode frozen at generation; later task edits do not re-interpret generated rounds.';
comment on column public.planning_occurrence.deadline_at is
    'Effective limited-time deadline for this round; frozen at generation and only synced for still-open rounds per requirement 18.3.';
comment on column public.planning_occurrence.for_date is
    'Legacy compatibility mirror of schedule_date for new rows; never used as identity, display, execution date or ownership authority.';

insert into public.app_settings (key, value)
values ('planning.refresh_boundary_state',
        '{"boundary": "06:00", "transition": null, "absorbed": []}'::jsonb)
on conflict (key) do nothing;

commit;
