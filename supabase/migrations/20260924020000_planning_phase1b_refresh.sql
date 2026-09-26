-- Phase 1B refresh settings and rule validation. This is source only;
-- Phase 5 controls deployment after old writers have stopped.
--
-- Boundary changes take effect from the next planning cycle: the transition
-- record lives in app_settings (previous boundary + change instant), not on
-- tasks, because no round identity may depend on a boundary.
begin;

alter table public.planning_task
    add column if not exists refresh_generated_through date,
    add column if not exists refresh_next_due_at timestamptz;

alter table public.planning_task
    add column if not exists request_key text,
    add column if not exists request_est_start timestamptz,
    add column if not exists request_state text not null default 'pending',
    -- BF1/BF2（第七轮）：重排请求接管模型——同一旧超时实例的再次「修改
    -- 时间」接管当前业务待办（同一实例保持身份），新请求身份覆盖记录；
    -- 被吸收的旧键留档，迟到重放只返回现状，不再改写时间。
    add column if not exists request_source_occurrence_id bigint,
    add column if not exists request_absorbed_keys text[] not null default '{}';

alter table public.planning_occurrence
    add column if not exists generation_request_key text,
    add column if not exists early_period_date date;

comment on column public.planning_task.refresh_generated_through is
    'Last fixed-rule date successfully checked for generation; progress only, never a refresh baseline or round identity.';
comment on column public.planning_task.refresh_next_due_at is
    'Persisted next due instant after confirmed handling; never derived from display or estimated execution time.';
comment on column public.planning_task.request_key is
    'Request identity for task-creating operations (e.g. timeout reschedule); persists before any occurrence exists so retries and concurrent replays converge on one task.';
comment on column public.planning_task.request_state is
    'Reschedule request lifecycle: pending (accepted, occurrence/anchor not yet fulfilled), completed (occurrence created and user anchor applied; replays return the current instance without re-anchoring), superseded (this task row is no longer the current business todo for its source timeout — replaced by a newer action or already closed; background never revives it).';
comment on column public.planning_task.request_est_start is
    'Absolute execution instant requested by the user, persisted with the request identity: the request content survives occurrence-generation failures and background recomputes, so recovery can always restore the user-chosen time.';
comment on column public.planning_task.request_source_occurrence_id is
    'The timeout occurrence this reschedule request replaces; one current business todo per source is enforced by the database.';
comment on column public.planning_task.request_absorbed_keys is
    'Superseded request keys absorbed by later modify-time actions on the same business todo; late replays of absorbed keys return the current state and never re-anchor.';
comment on column public.planning_occurrence.generation_request_key is
    'Client action identity for retry-safe early handling; distinct from business round and display identities.';
comment on column public.planning_occurrence.early_period_date is
    'Fixed-refresh period identity of an extra early completion; one extra record per task and period.';

alter table public.planning_task
    add constraint planning_task_request_state_check
    check (request_state in ('pending', 'completed', 'superseded'));

-- I1/I6：superseded 是终态且任务必须保持停用——正常应用路径（含重新启用）
-- 不得复活被取代请求。
alter table public.planning_task
    add constraint planning_task_superseded_inactive_check
    check (request_state <> 'superseded' or is_active = false);

create or replace function public.planning_validate_request_state()
returns trigger language plpgsql as $$
begin
    -- 终态不可逆：completed / superseded 不得回退为其他状态。
    if tg_op = 'UPDATE' and old.request_state in ('completed', 'superseded')
        and new.request_state is distinct from old.request_state then
        raise exception 'reschedule request state is terminal';
    end if;
    return new;
end;
$$;

create trigger planning_task_request_state_guard
before update on public.planning_task
for each row execute function public.planning_validate_request_state();

alter table public.planning_task
    add constraint planning_task_after_completion_interval_check
    check ((refresh_mode is distinct from 'after_completion')
           or (interval_days between 1 and 365) is true);

alter table public.planning_task
    add constraint planning_task_next_due_mode_check
    check (refresh_mode = 'after_completion' or refresh_next_due_at is null);

create index planning_occurrence_refresh_round_idx
    on public.planning_occurrence (task_id, created_at desc, id desc)
    where round_key is not null;

create unique index planning_occurrence_generation_request_uq
    on public.planning_occurrence (task_id, generation_request_key)
    where generation_request_key is not null;

-- 同一「超时重排」逻辑请求的幂等身份：请求身份持久化在任务行上（早于
-- 实例成立），重试 / 并发重放 / 实例缺失恢复都收敛到同一份替代任务。
create unique index planning_task_request_key_uq
    on public.planning_task (request_key)
    where request_key is not null;

-- BF2/BF4（第七轮）：同一旧超时实例同时至多存在一个当前业务待办——
-- 请求仍为 pending，或其名下仍有开放实例。并发首次创建由数据库收敛：
-- advisory lock 把「检查」串行化到先到事务提交之后，后到者的 INSERT 被
-- 拒绝，应用层随即将其收敛到接管路径（绝不产生第二个业务待办）。
-- 前一轮业务待办正常关闭后，槽位自动释放：对该超时记录的再次重排属于
-- 新的一次业务安排，允许新建（历史请求行保留为已完成历史）。
create or replace function public.planning_validate_reschedule_todo()
returns trigger language plpgsql as $$
declare
    active_count integer;
begin
    if new.request_source_occurrence_id is null then
        return new;
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'planning_reschedule_todo:' || new.request_source_occurrence_id::text, 0));
    if new.request_key like 'reschedule:%'
        and new.request_source_occurrence_id::text <> split_part(new.request_key, ':', 2) then
        raise exception 'reschedule request source must match its request key';
    end if;
    select count(*) into active_count
    from public.planning_task t
    where t.request_source_occurrence_id = new.request_source_occurrence_id
      and t.id <> new.id
      and t.request_state is distinct from 'superseded'
      and (
          t.request_state = 'pending'
          or exists (
              select 1 from public.planning_occurrence o
              where o.task_id = t.id
                and o.status in ('pending', 'in_progress', 'deferred', 'partial')
          )
      );
    if active_count > 0 then
        raise exception 'another reschedule todo for this timeout is still current';
    end if;
    return new;
end;
$$;

create trigger planning_reschedule_todo_guard
before insert on public.planning_task
for each row execute function public.planning_validate_reschedule_todo();

-- F1（最终并发收口）：重排实例的锚定标记必须与任务行当前请求身份一致。
-- 被接管的旧创建请求即使通过了应用层资格检查（检查与写入之间的间隙），
-- 其锚定写库时也会被本触发器拒绝——旧请求不可能再把实例锚定到自己的
-- 旧时刻覆盖接管方已成功的较新修改。仅约束重排任务（任务行带
-- request_key）：普通任务的提前完成幂等键标记（request_key 为 NULL）与
-- 不触碰标记的更新（用户人工编辑、状态更正）均不受影响。
create or replace function public.planning_validate_reschedule_anchor()
returns trigger language plpgsql as $$
declare
    current_request_key text;
begin
    if new.generation_request_key is null
        or new.generation_request_key is not distinct from old.generation_request_key then
        return new;
    end if;
    select request_key into current_request_key
      from public.planning_task where id = new.task_id;
    if current_request_key is not null
        and current_request_key is distinct from new.generation_request_key then
        raise exception 'reschedule anchor marker must match the current request identity';
    end if;
    return new;
end;
$$;

create trigger planning_reschedule_anchor_guard
before update on public.planning_occurrence
for each row execute function public.planning_validate_reschedule_anchor();

-- F2（最终并发收口）：request_absorbed_keys 的登记必须原子合并——应用层
-- read-modify-write（读取数组 → Python 追加 → 整体覆盖）在并发下互删。
-- 两个函数都在单条 UPDATE 内于行锁下读取最新数组并去重合并：
-- * planning_takeover_reschedule_request：adopt 身份接管（CAS 条件保留），
--   被接管的 expected key 随同一语句原子进入 absorbed；
-- * planning_absorb_reschedule_request：stand-down 键登记（幂等：已是
--   current 或已 absorbed 时不写）。
create or replace function public.planning_takeover_reschedule_request(
    p_task_id bigint,
    p_new_key text,
    p_new_est_start timestamptz,
    p_expected_key text,
    p_now timestamptz
) returns boolean language plpgsql as $$
begin
    if p_new_key is null or p_expected_key is null then
        return false;
    end if;
    update public.planning_task
       set request_key = p_new_key,
           request_est_start = p_new_est_start,
           request_absorbed_keys = (
               select coalesce(array_agg(distinct k), '{}')
               from unnest(request_absorbed_keys || p_expected_key) as k
           ),
           updated_at = p_now
     where id = p_task_id
       and request_key = p_expected_key;
    return found;
end;
$$;

create or replace function public.planning_absorb_reschedule_request(
    p_task_id bigint,
    p_request_key text,
    p_now timestamptz
) returns boolean language plpgsql as $$
begin
    if p_request_key is null or p_request_key = '' then
        return false;
    end if;
    update public.planning_task
       set request_absorbed_keys = (
               select coalesce(array_agg(distinct k), '{}')
               from unnest(request_absorbed_keys || p_request_key) as k
           ),
           updated_at = p_now
     where id = p_task_id
       and request_key is distinct from p_request_key
       and not (p_request_key = any(request_absorbed_keys));
    return found;
end;
$$;

-- BF3/G（第七轮）：after_completion 的提前完成在真实 PostgreSQL 上收敛——
-- 同一任务 30 分钟内（服务端持久化的成功处理时间差）只允许一条成功成立的
-- after_completion 提前完成事实。固定刷新型的提前完成携带
-- early_period_date 周期身份，继续由 (task_id, early_period_date) 唯一索引
-- 约束，不受本触发器影响。
create or replace function public.planning_validate_after_completion_early_window()
returns trigger language plpgsql as $$
begin
    if new.source is distinct from 'early' or new.early_period_date is not null
        or new.handled_at is null then
        return new;
    end if;
    perform pg_advisory_xact_lock(hashtextextended(
        'planning_after_completion_early:' || new.task_id::text, 0));
    if exists (
        select 1 from public.planning_occurrence o
        where o.task_id = new.task_id
          and o.source = 'early'
          and o.early_period_date is null
          and o.handled_at is not null
          and o.id <> new.id
          and abs(extract(epoch from (o.handled_at - new.handled_at))) < 1800
    ) then
        raise exception 'after_completion early completions within the same 30-minute window must converge';
    end if;
    return new;
end;
$$;

create trigger planning_after_completion_early_window_guard
before insert on public.planning_occurrence
for each row execute function public.planning_validate_after_completion_early_window();

-- 同一任务同一固定刷新周期的额外提前完成最多一条（数据库兜底并发窗口）。
create unique index planning_occurrence_early_period_uq
    on public.planning_occurrence (task_id, early_period_date)
    where source = 'early' and early_period_date is not null;

insert into public.app_settings (key, value)
values ('planning.daily_refresh_enabled', 'true'::jsonb)
on conflict (key) do nothing;

commit;
