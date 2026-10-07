-- 规划调整批次（2026-10-07 执行文档 §4 / §6）：删除按执行事实保留或物理删除、
-- after_completion 分钟级间隔、创建请求登记（已删除操作身份）与实际耗时来源。
-- 2026-10-07 审查修复（R01 / R04 / R09 / R10）：新私有表补 RLS 与显式收权；
--   完成事实表去掉对任务行的外键（FK 检查的 KEY SHARE 与删除 RPC 的任务行
--   FOR UPDATE 构成「实例 → 任务」反向锁序，真实 PG 40P01 死锁）；登记表
--   只存请求键 + 语义摘要（sha256），业务正文随任务行物理删除真正删除；
--   after_completion 分钟 CHECK 补非空判定（between 对 NULL 返回 UNKNOWN
--   绕过校验）。
--
-- 组成：
-- * planning_task.deleted_at：任务级删除标记（区别于暂停刷新 / 其他停用）。
--   有完成 / 部分完成事实的任务删除后保留任务行作为历史归档载体
--   （is_active=false + deleted_at），开放实例按删除语义收口；无事实者
--   物理删除任务与实例（occurrence.task_id 外键无 CASCADE，先删实例）。
-- * planning_task_completion_fact：「曾经有过完成/部分完成事实」的持久门槛
--   （执行文档 §6.3 第 2 条——完成标签后续更正、partial 字段被编辑时门槛
--   不消失）。由 planning_occurrence 触发器在 completed 状态或 partial_at
--   写入的同一事务内登记；**无外键、不触碰任务行**（R04：外键检查会在
--   完成语句内反向申请任务行 KEY SHARE，与删除 RPC「任务行 → 实例」锁序
--   形成死锁环；物理删除分支在本事务内显式清理门槛行）。存量行按当前
--   可靠证据回填；无法判定者保守保留。
-- * planning_creation_request：已删除创建操作的登记（tombstone）。任务行在
--   存续期间继续承载 creation_request_key / content / feedback（单行原子）；
--   物理删除时把请求键与**规范化语义摘要**（sha256，应用层按归一化内容
--   计算后传入）移入本表，同事务完成——旧请求重发按摘要比对返回已删除
--   结果、不同内容 409，绝不复建任务；新键创建同内容不受影响。业务正文
--   不落本表（R09：物理删除即真正删除，不把完整快照移到别表永久保留）。
-- * planning_task.after_completion_minutes：处理后刷新间隔的分钟权威
--   （§9.5：d/h/m 组合、纯数字默认天、1 分钟–365 天）。旧 interval_days
--   天数按 days×1440 等价回填后置空，双列不能各自变化造成两个真实间隔；
--   固定时间轴（fixed_interval）继续使用 interval_days。
-- * planning_occurrence.actual_time_source：实际起止来源（null=旧数据来源
--   不明；'user'=用户开始/结束/补填；'system'=系统收口或合成）。耗时展示
--   的自动实际值只信 'user'（§12.3 三层优先级、A05 合成零时长不冒充实测）。
-- * planning_discard_task 重写（返回 jsonb）：锁任务 → 锁全部实例（id 升序）
--   → 锁内事实判定 → 历史保留分支（收口开放行 + 停用 + deleted_at）或
--   物理删除分支（删实例 + 删门槛 + 删任务 + 登记请求身份与摘要），任一
--   失败整体回滚；已删除任务重复删除返回稳定结果。
-- * planning_split_occurrence 换参 p_after_completion_days →
--   p_after_completion_minutes（同名同型整数参数，drop+create 换名，
--   不产生 overload；与代码同批部署）。
-- * planning_insert_round_occurrence 的 expected 定义字段加入
--   after_completion_minutes（生成定义漂移保护覆盖新间隔字段）。

begin;

-- ── 1. 任务级删除标记 ─────────────────────────────────────────────
alter table public.planning_task
    add column if not exists deleted_at timestamptz;

comment on column public.planning_task.deleted_at is
    '任务被用户明确删除的时刻（§25 / §31.1）；NULL = 未删除。暂停刷新'
    '(refresh_enabled=false) 与删除是两个操作，本列只由删除入口写入。';

-- ── 2. 完成 / 部分完成事实门槛（持久、不可逆） ────────────────────
-- R04（2026-10-07 审查 #4）：task_id **不设外键**。外键检查（SELECT FOR
-- KEY SHARE 父行）会在完成 / 部分完成 / 中空阶段 / 提前完成的写入语句内
-- 反向申请任务行锁，与 planning_discard_task「任务行 FOR UPDATE → 实例
-- FOR UPDATE」锁序构成反向等待环（真实 PG 40P01）。门槛行只由
-- planning_occurrence 触发器按既有外键保证的任务存在性登记（task_id 主键
-- 自身防重复）；物理删除任务时由删除 RPC 在同一事务内显式清理。
create table if not exists public.planning_task_completion_fact (
    task_id bigint primary key,
    first_fact_at timestamptz not null default now()
);

comment on table public.planning_task_completion_fact is
    '「任务曾经有过完成 / 部分完成事实」的持久门槛（§25.1）。删除判定与'
    '旧数据清理以此为权威证据之一：状态标签后续更正、partial 字段被编辑'
    '都不能让门槛消失。无外键（R04 锁序），物理删除任务时由删除事务显式'
    '清理。';

-- R01（2026-10-07 审查 #1）：新私有表不得沿袭 public 新表对 anon /
-- authenticated 的默认授权——启用 RLS（无策略 = 默认拒绝）并显式收权；
-- 后端经 service_role（bypassrls + 显式授权）读写，与既有
-- memory_requests / todo_reminder_state 的收权模式一致。
alter table public.planning_task_completion_fact enable row level security;
revoke all on table public.planning_task_completion_fact
    from public, anon, authenticated;
grant select, insert, update, delete
    on table public.planning_task_completion_fact to service_role;

create or replace function public.planning_mark_completion_fact()
returns trigger language plpgsql as $$
begin
    if new.status = 'completed' or new.partial_at is not null then
        insert into public.planning_task_completion_fact (task_id)
        values (new.task_id)
        on conflict (task_id) do nothing;
    end if;
    return new;
end;
$$;

revoke all on function public.planning_mark_completion_fact()
    from public, anon, authenticated;
grant execute on function public.planning_mark_completion_fact()
    to service_role;

drop trigger if exists planning_occurrence_completion_fact_guard
    on public.planning_occurrence;
create trigger planning_occurrence_completion_fact_guard
after insert or update on public.planning_occurrence
for each row execute function public.planning_mark_completion_fact();

-- 存量回填：仅按当前可靠证据（completed 状态行 / partial_at 非空）；
-- 无法判定的历史保持无事实行，删除清理时按保守保留处理。
insert into public.planning_task_completion_fact (task_id)
select o.task_id
  from public.planning_occurrence o
 where o.status = 'completed'
    or o.partial_at is not null
 group by o.task_id
on conflict (task_id) do nothing;

-- ── 3. 已删除创建操作登记（tombstone，§30.7 / 执行文档 §6.1） ─────
-- R09（2026-10-07 审查 #9）：**不存业务正文**。任务物理删除时其
-- creation_request_content 随任务行一起真正删除；本表只保留最低操作身份
-- ——请求键、规范化语义摘要（sha256，应用层按归一化内容计算传入）、
-- task_id 与删除时刻。同键重放按摘要比对：同内容 → 已删除结果（HTTP
-- 200），不同内容 → 409，绝不复建任务。摘要不能在库内对 jsonb::text
-- 计算（jsonb 键序按长度优先，与 Python canonical JSON 字典序不一致，
-- 两侧无法对上），由删除调用方计算后经 p_creation_digest 传入。
create table if not exists public.planning_creation_request (
    request_key text primary key,
    content_digest text not null default '',
    task_id bigint,
    deleted_at timestamptz not null,
    created_at timestamptz not null default now()
);

comment on table public.planning_creation_request is
    '已删除创建操作的请求身份登记（§30.7）：任务物理删除时同事务移入'
    '请求键与规范化语义摘要（sha256），阻止网络重发的旧请求复建任务；'
    '不是待办历史，不在任何页面展示，不保存业务正文（R09：正文随任务行'
    '真正删除）。content_digest 为空表示该登记不可核对内容，同键重放按'
    '内容冲突（409）收敛，不放行复建。';

-- R01（2026-10-07 审查 #1）：同上——RLS + 显式收权，匿名 / 普通登录
-- 角色不得读取删除登记或改写保护信息。
alter table public.planning_creation_request enable row level security;
revoke all on table public.planning_creation_request
    from public, anon, authenticated;
grant select, insert, update, delete
    on table public.planning_creation_request to service_role;

-- ── 4. after_completion 间隔分钟权威（§9.5） ──────────────────────
alter table public.planning_task
    add column if not exists after_completion_minutes integer;

update public.planning_task
   set after_completion_minutes = interval_days * 1440
 where refresh_mode = 'after_completion'
   and after_completion_minutes is null
   and interval_days is not null;

update public.planning_task
   set interval_days = null
 where refresh_mode = 'after_completion'
   and interval_days is not null;

-- R10（2026-10-07 审查 #10）：between 对 NULL 分钟返回 UNKNOWN，CHECK
-- 视为通过——after_completion 行的分钟必须显式非空且在 1–525600。存量
-- 校验失败（既有 after_completion 行两列皆空）会让本迁移显式失败，暴露
-- 需人工处理的异常数据，不为部署方便猜测间隔回填。
alter table public.planning_task
    drop constraint if exists planning_task_after_completion_interval_check;
alter table public.planning_task
    add constraint planning_task_after_completion_interval_check
    check ((refresh_mode is distinct from 'after_completion')
           or (after_completion_minutes is not null
               and after_completion_minutes between 1 and 525600));

comment on column public.planning_task.after_completion_minutes is
    '处理后刷新（after_completion）间隔的唯一权威，单位分钟（§9.5，'
    '2026-10-07）：1 分钟至 365 天；旧 interval_days 天数已按 days×1440 '
    '等价回填后置空。fixed_interval 固定时间轴继续使用 interval_days，'
    '本列保持 NULL。';

-- ── 5. 实际时间来源（§12.3 / 执行文档 §6.4） ──────────────────────
alter table public.planning_occurrence
    add column if not exists actual_time_source text;

comment on column public.planning_occurrence.actual_time_source is
    '实际起止的来源：NULL=旧数据来源不明（展示回退预估，不猜测）；'
    '''user''=用户开始 / 结束 / 补填；''system''=系统收口（废弃 / 拆分'
    '事务补终点）或提前完成合成的同刻起止。自动实际耗时只信 ''user''。';

-- 存量回填（保守）：已完成且起止齐全、起于止前的普通轮次视为用户计时
-- （开始 / 结束按钮是旧版唯一写入路径）；提前完成合成同刻与被收口 /
-- 超时行保持 NULL，不把系统合成时间冒充用户计时。
update public.planning_occurrence
   set actual_time_source = 'system'
 where source = 'early'
   and actual_start is not null and actual_end is not null
   and actual_start = actual_end
   and actual_time_source is null;

update public.planning_occurrence
   set actual_time_source = 'user'
 where status = 'completed'
   and source <> 'early'
   and actual_start is not null and actual_end is not null
   and actual_start < actual_end
   and actual_time_source is null;

-- ── 6. planning_discard_task 重写：按事实保留或物理删除 ───────────
drop function if exists public.planning_discard_task(bigint, timestamptz, bigint, jsonb);

create or replace function public.planning_discard_task(
    p_task_id bigint,
    p_now timestamptz,
    p_target_id bigint default null,
    p_target_patch jsonb default null,
    p_creation_digest text default null
) returns jsonb
language plpgsql as $$
declare
    v_task public.planning_task%rowtype;
    v_row public.planning_occurrence%rowtype;
    v_start timestamptz;
    v_end timestamptz;
    v_minutes integer;
    v_raw double precision;
    v_whole double precision;
    v_frac double precision;
    v_closed integer := 0;
    v_has_fact boolean;
begin
    -- 语句 1：锁任务行（任务行 → 实例按 id 升序的既有锁序纪律）。完成 /
    --   部分完成语句经触发器登记事实门槛时**不申请任务行锁**（R04：事实表
    --   无外键），本 FOR UPDATE 与其无冲突，删除与首次完成重叠执行收敛、
    --   不再形成 40P01 反向等待环。
    select * into v_task
      from public.planning_task
     where id = p_task_id
       for update;
    if not found then
        raise exception 'planning_discard_task: task not found';
    end if;

    -- 已删除任务的重复删除：稳定结果，不再改写历史或登记。
    if v_task.deleted_at is not null then
        return jsonb_build_object(
            'deleted', true,
            'history_preserved', true,
            'already_deleted', true,
            'closed', 0);
    end if;

    -- 语句 2：按稳定 id 序锁本任务全部实例行（历史收口与物理删除都作用于
    -- 这组锁内的行；行锁获取时 EvalPlanQual 以最新已提交版本复核——并发
    -- 完成先提交者在本扫描后以最新版本参与下方事实判定，绝不误删）。
    perform 1
      from public.planning_occurrence
     where task_id = p_task_id
     order by id
     for update;

    -- 语句 3：锁内事实判定（§25）：持久事实门槛或当前行证据任一成立即
    -- 保留全部历史。仅超时 / 此次不执行 / 开始 / 延后不构成事实。
    select exists (
               select 1 from public.planning_task_completion_fact f
                where f.task_id = p_task_id
           ) or exists (
               select 1 from public.planning_occurrence o
                where o.task_id = p_task_id
                  and (o.status = 'completed' or o.partial_at is not null)
           )
      into v_has_fact;

    if v_has_fact then
        -- ── 历史保留分支 ────────────────────────────────────────
        -- 语句 4a（可选）：pending 目标行的实际时间事实补齐（仅无事实的
        -- pending 行；并发已产生事实时旧补丁不覆盖）。
        if p_target_id is not null and p_target_patch is not null then
            if exists (
                select 1
                  from jsonb_object_keys(p_target_patch) as k
                 where k not in ('actual_start', 'actual_end', 'actual_minutes',
                                 'actual_time_source', 'updated_at')
            ) then
                raise exception
                    'planning_discard_task: target patch contains unsupported field';
            end if;
            update public.planning_occurrence set
                actual_start = case when p_target_patch ? 'actual_start'
                    then (p_target_patch->>'actual_start')::timestamptz else actual_start end,
                actual_end = case when p_target_patch ? 'actual_end'
                    then (p_target_patch->>'actual_end')::timestamptz else actual_end end,
                actual_minutes = case when p_target_patch ? 'actual_minutes'
                    then (p_target_patch->>'actual_minutes')::integer else actual_minutes end,
                actual_time_source = case when p_target_patch ? 'actual_time_source'
                    then (p_target_patch->>'actual_time_source') else actual_time_source end,
                updated_at = case when p_target_patch ? 'updated_at'
                    then (p_target_patch->>'updated_at')::timestamptz else updated_at end
            where id = p_target_id
              and task_id = p_task_id
              and status = 'pending'
              and actual_start is null
              and actual_end is null
              and partial_at is null
              and handled_at is null
              and closed_at is null;
        end if;

        -- 语句 4b：执行中实例的结束事实补齐（系统收口 → actual_time_source=
        -- 'system'，不冒充用户计时）；耗时按锁内最终起止重算。
        for v_row in
            select *
              from public.planning_occurrence
             where task_id = p_task_id
               and status in ('pending', 'in_progress', 'deferred', 'partial')
             order by id
             for update
        loop
            if v_row.status = 'in_progress'
               and v_row.handled_at is null
               and v_row.closed_at is null then
                v_start := v_row.actual_start;
                v_end := v_row.actual_end;
                if v_row.id = p_target_id and p_target_patch is not null then
                    if v_start is null and p_target_patch ? 'actual_start' then
                        v_start := (p_target_patch->>'actual_start')::timestamptz;
                    end if;
                    if v_end is null and p_target_patch ? 'actual_end' then
                        v_end := (p_target_patch->>'actual_end')::timestamptz;
                    end if;
                end if;
                if v_end is null then
                    v_end := p_now;
                end if;
                if v_start is not null and v_end < v_start then
                    raise exception
                        'planning_discard_task: actual_end must not precede actual_start';
                end if;
                if v_start is null then
                    v_minutes := null;
                else
                    v_raw := extract(epoch from (v_end - v_start)) / 60.0;
                    v_whole := trunc(v_raw);
                    v_frac := v_raw - v_whole;
                    v_minutes := greatest(0, (v_whole + case
                        when v_frac > 0.5 then 1
                        when v_frac < 0.5 then 0
                        else case when (v_whole::bigint % 2) = 0 then 0 else 1 end
                    end)::integer);
                end if;
                if v_start is distinct from v_row.actual_start
                   or v_end is distinct from v_row.actual_end
                   or v_minutes is distinct from v_row.actual_minutes then
                    update public.planning_occurrence
                       set actual_start = v_start,
                           actual_end = v_end,
                           actual_minutes = v_minutes,
                           actual_time_source = 'system'
                     where id = v_row.id;
                elsif v_row.actual_time_source is null then
                    update public.planning_occurrence
                       set actual_time_source = 'system'
                     where id = v_row.id;
                end if;
            end if;
        end loop;

        -- 语句 5：关闭全部开放实例（删除语义收口；既有完成 / 部分完成 /
        -- 超时 / 此次不执行历史原样保留，不覆写）。
        update public.planning_occurrence
           set status = 'discarded', closed_at = p_now, updated_at = p_now
         where task_id = p_task_id
           and status in ('pending', 'in_progress', 'deferred', 'partial');
        get diagnostics v_closed = row_count;

        -- 语句 6：停用 + 标记删除。任务已被并发停用 → 并发拒绝（整体回滚）。
        update public.planning_task
           set is_active = false, deleted_at = p_now, updated_at = p_now
         where id = p_task_id
           and is_active;
        if not found then
            raise exception 'planning_discard_task: task already inactive (concurrent change)'
                using errcode = 'PC001';
        end if;
        return jsonb_build_object(
            'deleted', true,
            'history_preserved', true,
            'already_deleted', false,
            'closed', v_closed);
    end if;

    -- ── 物理删除分支（§25.2：从无完成 / 部分完成事实） ────────────
    -- 语句 7：删除实例（外键无 CASCADE，先删实例再删任务）。
    delete from public.planning_occurrence where task_id = p_task_id;
    get diagnostics v_closed = row_count;
    -- 语句 7b：清除完成事实门槛（R04：事实表无外键，无级联——物理删除
    -- 分支在同一事务内显式清理，不留下孤儿门槛行）。
    delete from public.planning_task_completion_fact where task_id = p_task_id;
    -- 语句 8：删除任务行（R09：创建快照正文随任务行一起真正删除，不把
    -- 完整业务内容移入登记表永久保留）。
    delete from public.planning_task where id = p_task_id;
    if not found then
        raise exception 'planning_discard_task: task already inactive (concurrent change)'
            using errcode = 'PC001';
    end if;
    -- 语句 9：登记已删除创建操作的最小身份（同事务，R09）：只保留请求键、
    -- 规范化语义摘要（应用层计算传入）、task_id 与删除时刻。摘要为空 =
    -- 不可核对内容（同键重放按 409 收敛，不放行复建）。
    if v_task.creation_request_key is not null then
        insert into public.planning_creation_request
            (request_key, content_digest, task_id, deleted_at)
        values (
            v_task.creation_request_key,
            coalesce(p_creation_digest, ''),
            p_task_id,
            p_now)
        on conflict (request_key) do nothing;
    end if;
    return jsonb_build_object(
        'deleted', true,
        'history_preserved', false,
        'already_deleted', false,
        'closed', v_closed);
end;
$$;

-- ── 7. planning_split_occurrence：间隔参数换分钟 ──────────────────
drop function if exists public.planning_split_occurrence(bigint, text, date, timestamptz, jsonb, integer);

create or replace function public.planning_split_occurrence(
    p_task_id bigint,
    p_round_key text,
    p_target_date date,
    p_now timestamptz,
    p_parts jsonb,
    p_after_completion_minutes integer default null
) returns jsonb
language plpgsql as $$
declare
    v_ids jsonb := '[]'::jsonb;
    v_part jsonb;
    v_id bigint;
    v_handled timestamptz;
begin
    if not exists (
        select 1 from public.planning_task
        where id = p_task_id
        for update
    ) then
        raise exception 'planning_split_occurrence: task not found';
    end if;

    if jsonb_typeof(p_parts) is distinct from 'array'
       or jsonb_array_length(p_parts) < 1
       or jsonb_array_length(p_parts) > 10 then
        raise exception 'planning_split_occurrence: parts must be an array of 1-10 items';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(p_parts) as e
        where jsonb_typeof(e.value) is distinct from 'object'
           or exists (
                select 1 from jsonb_object_keys(e.value) as k
                where k not in ('content', 'estimated_minutes'))
           or not e.value ?& array['content', 'estimated_minutes']
           or jsonb_typeof(e.value->'content') is distinct from 'string'
           or btrim(e.value->>'content') = ''
           or char_length(e.value->>'content') > 500
           or jsonb_typeof(e.value->'estimated_minutes') is distinct from 'number'
           or (e.value->>'estimated_minutes')::numeric
              <> floor((e.value->>'estimated_minutes')::numeric)
           or (e.value->>'estimated_minutes')::int < 1
           or (e.value->>'estimated_minutes')::int > 1440
    ) then
        raise exception 'planning_split_occurrence: invalid part item';
    end if;
    if p_after_completion_minutes is not null
       and (p_after_completion_minutes < 1
            or p_after_completion_minutes > 525600) then
        raise exception 'planning_split_occurrence: invalid after_completion interval';
    end if;

    perform 1
      from public.planning_occurrence
     where task_id = p_task_id
       and round_key = p_round_key
       and status in ('pending', 'in_progress', 'deferred', 'partial')
     order by id
     for update;

    update public.planning_occurrence
       set status = 'discarded_this', closed_at = p_now,
           handled_at = p_now, updated_at = p_now
     where task_id = p_task_id
       and round_key = p_round_key
       and status in ('pending', 'in_progress', 'deferred', 'partial');
    if not found then
        raise exception 'planning_split_occurrence: round already closed (concurrent change)'
            using errcode = 'PC001';
    end if;

    for v_part in select * from jsonb_array_elements(p_parts) loop
        insert into public.planning_task (
            content, task_type, time_mode, estimated_minutes,
            target_date, refresh_mode, is_active, created_at, updated_at
        ) values (
            btrim(v_part->>'content'), 'once', 'duration',
            (v_part->>'estimated_minutes')::int, p_target_date, 'none',
            true, p_now, p_now
        ) returning id into v_id;
        v_ids := v_ids || to_jsonb(v_id);
    end loop;

    if p_after_completion_minutes is not null then
        v_handled := (
            select max(handled_at)
              from public.planning_occurrence
             where task_id = p_task_id
               and round_key = p_round_key
               and handled_at is not null
        );
        if v_handled is not null
           and not exists (
                select 1
                  from public.planning_occurrence
                 where task_id = p_task_id
                   and round_key = p_round_key
                   and handled_at is null
           ) then
            update public.planning_task
               set last_handled_at = v_handled,
                   refresh_next_due_at = v_handled
                       + make_interval(mins => p_after_completion_minutes),
                   updated_at = p_now
             where id = p_task_id
               and is_active;
            if not found then
                raise exception 'planning_split_occurrence: task already inactive (concurrent change)'
                    using errcode = 'PC001';
            end if;
        end if;
    end if;

    return v_ids;
end;
$$;

-- ── 8. planning_insert_round_occurrence：expected 定义加入分钟字段 ─
drop function if exists public.planning_insert_round_occurrence(bigint, jsonb, jsonb);

create or replace function public.planning_insert_round_occurrence(
    p_task_id bigint,
    p_rows jsonb,
    p_expected_task jsonb
) returns void
language plpgsql as $$
declare
    current_row public.planning_task%rowtype;
    current_snapshot jsonb;
begin
    select * into current_row
    from public.planning_task
    where id = p_task_id
    for update;
    if not found then
        raise exception 'planning_insert_round_occurrence: task not found';
    end if;
    if current_row.is_active is not true
       or current_row.refresh_enabled is not true then
        raise exception 'planning_insert_round_occurrence: task no longer active'
            using errcode = 'PC001';
    end if;
    current_snapshot := to_jsonb(current_row);
    if p_expected_task is null or not (p_expected_task ?& array[
        'task_type', 'refresh_mode', 'refresh_enabled', 'is_active',
        'request_state', 'content', 'time_mode', 'estimated_minutes',
        'is_hollow', 'hollow_start_minutes', 'hollow_wait_minutes',
        'hollow_end_minutes', 'hollow_start_content', 'hollow_end_content',
        'interval_days', 'weekdays', 'month_days', 'target_date',
        'created_at', 'refresh_anchor_at',
        'last_handled_at', 'refresh_next_due_at', 'window_start_tod',
        'window_end_tod', 'after_completion_minutes'
    ]) then
        raise exception 'planning_insert_round_occurrence: expected task incomplete';
    end if;
    if exists (
        select 1
        from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb)) as r
        where r.value->>'status' is distinct from 'pending'
           or r.value->>'source' is distinct from 'schedule'
    ) then
        raise exception 'planning_insert_round_occurrence: rows must be pending schedule occurrences';
    end if;
    if exists (
        select 1 from unnest(array[
            'task_type', 'refresh_mode', 'refresh_enabled', 'is_active',
            'request_state', 'content', 'time_mode', 'estimated_minutes',
            'is_hollow', 'hollow_start_minutes', 'hollow_wait_minutes',
            'hollow_end_minutes', 'hollow_start_content', 'hollow_end_content',
            'interval_days', 'weekdays', 'month_days', 'target_date',
            'after_completion_minutes'
        ]) as fields(field)
        where current_snapshot->field is distinct from p_expected_task->field
    ) or current_row.created_at is distinct from
            (p_expected_task->>'created_at')::timestamptz
       or current_row.refresh_anchor_at is distinct from
            (p_expected_task->>'refresh_anchor_at')::timestamptz
       or current_row.last_handled_at is distinct from
            (p_expected_task->>'last_handled_at')::timestamptz
       or current_row.refresh_next_due_at is distinct from
            (p_expected_task->>'refresh_next_due_at')::timestamptz
       or current_row.window_start_tod is distinct from
            (p_expected_task->>'window_start_tod')::time
       or current_row.window_end_tod is distinct from
            (p_expected_task->>'window_end_tod')::time then
        raise exception 'planning_insert_round_occurrence: task definition changed during generation'
            using errcode = 'PC001';
    end if;
    insert into public.planning_occurrence (
        task_id, for_date, round_key, schedule_date, display_cycle_date,
        display_reason, phase_group, phase, fixed_due_at, est_start, est_end,
        nominal_start, status, planned_minutes, planned_wait_minutes, sort_order,
        is_fixed, estimated_time_source, fixed_source, schedule_managed,
        is_limited, window_start_at, window_end_at, fixed_expires_at, source,
        early_period_date, deadline_at, content_snapshot, display_content,
        time_mode_snapshot, created_at, updated_at
    )
    select r.task_id, r.for_date, r.round_key, r.schedule_date, r.display_cycle_date,
           r.display_reason, r.phase_group, r.phase, r.fixed_due_at, r.est_start,
           r.est_end, r.nominal_start, r.status, r.planned_minutes,
           r.planned_wait_minutes, r.sort_order, r.is_fixed, r.estimated_time_source,
           r.fixed_source, r.schedule_managed, r.is_limited, r.window_start_at,
           r.window_end_at, r.fixed_expires_at, r.source, r.early_period_date,
           r.deadline_at, r.content_snapshot, r.display_content,
           r.time_mode_snapshot, r.created_at, r.updated_at
    from jsonb_populate_recordset(null::public.planning_occurrence, p_rows) r;
end;
$$;

commit;
