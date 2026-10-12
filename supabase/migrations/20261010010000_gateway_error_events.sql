-- 全网关报错日志接入（§36 独立批次，2026-10-10）：
-- 结构化错误事件表 gateway_error_events + 只读查询 / 清理 RPC。
--
-- * 事件唯一 ID（event_id uuid 主键）：写入方重试同一日志必须复用同一 ID，
--   INSERT ... ON CONFLICT DO NOTHING 保证幂等，汇总次数不翻倍（L08）。
-- * occurred_at 为真实发生时间（UTC 存储）；ingested_at 标记实际入库时间，
--   用于识别缓冲 / spool 补写。
-- * commit_xid（xid8，默认 pg_current_xact_id()）：插入事务的 64 位事务
--   ID，配合会话可见性快照（pg_current_snapshot / pg_visible_in_snapshot）
--   实现「快照 + 已看集合」的真实可见性冻结（BUG-05）——ingested_at 是
--   事务开始时间，晚提交行仍满足时间条件，不能单独承担快照语义。
-- * key_ref / credential_role / provider（TASK-02）：实际调用密钥的完整
--   HMAC 稳定标识（kh1- 前缀，不可逆）、用途与安全上游。空值表示未关联
--   密钥或历史未知记录；fingerprint 在有标识时加入密钥身份（同功能不同
--   密钥分组），无标识保持与旧公式一致（不与已识别密钥混组）。
-- * 原始事件表是唯一事实来源；「重复错误汇总」在查询时按 fingerprint 聚合
--   （gateway_log_query），不落第二张汇总表，次数与首末时间始终准确。
-- * fingerprint = 模块|操作|错误码|异常类|logger|归一化来源[|key_ref] 的
--   sha256 前 16 位（来源不含行号，避免代码版本间碎片化）；不是事件唯一 ID。
-- * RLS 启用且无策略（默认拒绝）；显式收权 anon / authenticated / public，
--   后端经 service_role（bypassrls + 显式授权）读写，与既有
--   planning_task_completion_fact 的收权模式一致。
-- * RPC 全部 security invoker + 固定 search_path，只授予 service_role。
--
-- 幂等：可重复执行（if not exists / or replace / add column if not exists）；
-- 不修改任何既有业务表。本文件属本批未发布迁移，原位更新。

begin;

-- ── 1. 事件表 ───────────────────────────────────────────────────
create table if not exists public.gateway_error_events (
    event_id uuid primary key,
    occurred_at timestamptz not null,
    ingested_at timestamptz not null default now(),
    level text not null,
    module text not null,
    operation text not null default '',
    error_code text not null default '',
    message_zh text not null default '',
    exception_type text not null default '',
    message_raw text not null default '',
    stack_trace text not null default '',
    logger_name text not null default '',
    source text not null default '',
    request_id text not null default '',
    operation_id text not null default '',
    route text not null default '',
    method text not null default '',
    http_status int,
    upstream_status int,
    outcome text not null default '',
    instance_id text not null default '',
    release_id text not null default '',
    safe_context jsonb not null default '{}'::jsonb,
    fingerprint text not null default '',
    truncated_fields jsonb not null default '[]'::jsonb,
    key_ref text not null default '',
    credential_role text not null default '',
    provider text not null default '',
    commit_xid xid8 not null default pg_current_xact_id(),
    constraint gateway_error_events_level_chk
        check (level in ('WARNING', 'ERROR', 'CRITICAL')),
    constraint gateway_error_events_outcome_chk
        check (outcome in ('', 'failed', 'degraded', 'committed_side_effect_failed')),
    constraint gateway_error_events_status_range_chk
        check (http_status is null or http_status between 100 and 599),
    constraint gateway_error_events_upstream_range_chk
        check (upstream_status is null or upstream_status between 100 and 599)
);

-- 本批未发布迁移的原位更新：对已按旧结构建出的本地库补列（幂等）。
alter table public.gateway_error_events
    add column if not exists key_ref text not null default '';
alter table public.gateway_error_events
    add column if not exists credential_role text not null default '';
alter table public.gateway_error_events
    add column if not exists provider text not null default '';
alter table public.gateway_error_events
    add column if not exists commit_xid xid8 not null default pg_current_xact_id();

comment on table public.gateway_error_events is
    '全网关报错日志事件（§36）：WARNING/ERROR/CRITICAL 结构化脱敏事件。'
    'event_id 幂等（重试复用）；occurred_at 为真实发生时间；查询按 '
    'fingerprint 汇总。不保存正文 / 密钥（写入前已脱敏）；key_ref 为实际'
    '调用密钥的不可逆 HMAC 标识，commit_xid 支撑会话可见性快照。';

create index if not exists gateway_error_events_occurred_idx
    on public.gateway_error_events (occurred_at desc);
create index if not exists gateway_error_events_module_level_idx
    on public.gateway_error_events (module, level, occurred_at desc);
create index if not exists gateway_error_events_fingerprint_idx
    on public.gateway_error_events (fingerprint, occurred_at desc);
create index if not exists gateway_error_events_level_idx
    on public.gateway_error_events (level, occurred_at desc);
create index if not exists gateway_error_events_request_idx
    on public.gateway_error_events (request_id);
create index if not exists gateway_error_events_key_ref_idx
    on public.gateway_error_events (key_ref, occurred_at desc);

alter table public.gateway_error_events enable row level security;
revoke all on table public.gateway_error_events
    from public, anon, authenticated;
grant select, insert, delete
    on public.gateway_error_events to service_role;

-- ── 2. 汇总列表查询（查询时聚合，稳定 cursor 分页） ─────────────
--    BUG-05：会话快照 = 入库时间上限（p_snapshot）+ 事务可见性快照
--    （p_visible，pg_snapshot 文本）。第一页由本函数取 pg_current_snapshot()
--    并随 JSON 返回（visible_snapshot），API 把它编入 cursor 随翻页回传：
--    * 快照之后入库（ingested_at > p_snapshot）的事件不进入本次会话；
--    * 快照时刻已提交可见的事件（v0_visible）参与聚合——已看组（p_seen）
--      的次数 / 末次 / 样本在整个会话内冻结，详情与列表一致；
--    * 快照时刻尚未提交可见的晚提交行（ingested_at ≤ p_snapshot 且
--      v0_visible = false）：其所属组在快照时刻完全不可见（无任何
--      v0_visible 行）时，整组按当前可见行进入后续页（不漏组）；其所属
--      组已有可见行时（已看组新增），本会话内不并入——刷新才变化。
--    cursor 按已看 fingerprint 集合排除重复：已看组在任何后续页都不重现，
--    未看组按 last_seen 降序补齐。列表投影不含 message_raw / stack_trace
--    （BUG-16）：完整原文与堆栈由受限详情 RPC（gateway_log_group /
--    gateway_log_event）读取。
--    TASK-02：p_key_ref 按完整密钥标识筛选；组投影携带 key_ref /
--    credential_role / provider。
create or replace function public.gateway_log_query(
    p_level text default null,
    p_module text default null,
    p_start timestamptz default null,
    p_end timestamptz default null,
    p_search text default null,
    p_limit int default 50,
    p_seen text default null,
    p_snapshot timestamptz default null,
    p_visible text default null,
    p_key_ref text default null
) returns json
language sql stable security invoker
set search_path = public
set timezone = 'UTC'
as $$
    with snap as (
        select coalesce(nullif(p_visible, ''), pg_current_snapshot()::text)::pg_snapshot as v0
    ),
    base as (
        select
            e.event_id, e.occurred_at, e.ingested_at, e.level, e.module,
            e.operation, e.error_code, e.message_zh, e.exception_type,
            e.logger_name, e.source, e.request_id, e.outcome,
            e.instance_id, e.fingerprint,
            e.key_ref, e.credential_role, e.provider,
            pg_visible_in_snapshot(e.commit_xid, (select v0 from snap)) as v0_visible
        from public.gateway_error_events e
        where (p_level is null or e.level = p_level)
          and (p_module is null or e.module = p_module)
          and (p_start is null or e.occurred_at >= p_start)
          and (p_end is null or e.occurred_at <= p_end)
          and (p_snapshot is null or e.ingested_at <= p_snapshot)
          and (p_key_ref is null or p_key_ref = '' or e.key_ref = p_key_ref)
          and (
                p_search is null or p_search = ''
                or e.error_code ilike '%' || p_search || '%'
                or e.request_id = p_search
                or e.exception_type ilike '%' || p_search || '%'
                or e.operation ilike '%' || p_search || '%'
                or e.message_zh ilike '%' || p_search || '%'
          )
    ),
    gflags as (
        select e.fingerprint, bool_or(e.v0_visible) as has_v0
        from base e
        group by e.fingerprint
    ),
    session_events as (
        select b.*
        from base b
        join gflags g on g.fingerprint = b.fingerprint
        where b.v0_visible or not g.has_v0
    ),
    grouped as (
        select
            e.fingerprint,
            count(*)::bigint as event_count,
            min(e.occurred_at) as first_seen_at,
            max(e.occurred_at) as last_seen_at,
            case
                when bool_or(e.level = 'CRITICAL') then 'CRITICAL'
                when bool_or(e.level = 'ERROR') then 'ERROR'
                else 'WARNING'
            end as level,
            (array_agg(e.module order by e.occurred_at desc))[1] as module,
            (array_agg(e.operation order by e.occurred_at desc))[1] as operation,
            (array_agg(e.error_code order by e.occurred_at desc))[1] as error_code,
            (array_agg(e.message_zh order by e.occurred_at desc))[1] as message_zh,
            (array_agg(e.exception_type order by e.occurred_at desc))[1] as exception_type,
            (array_agg(e.logger_name order by e.occurred_at desc))[1] as logger_name,
            (array_agg(e.source order by e.occurred_at desc))[1] as source,
            (array_agg(e.outcome order by e.occurred_at desc))[1] as outcome,
            (array_agg(e.instance_id order by e.occurred_at desc))[1] as instance_id,
            (array_agg(e.key_ref order by e.occurred_at desc))[1] as key_ref,
            (array_agg(e.credential_role order by e.occurred_at desc))[1] as credential_role,
            (array_agg(e.provider order by e.occurred_at desc))[1] as provider,
            (array_agg(e.event_id order by e.occurred_at desc))[1] as sample_event_id,
            (array_agg(e.request_id order by e.occurred_at desc))[1] as sample_request_id
        from session_events e
        group by e.fingerprint
    ),
    -- BUG-05：已看集合排除（p_seen 为逗号分隔的 fingerprint 列表，由
    -- API 侧校验格式与数量上限）。空 / null 表示第一页。
    unseen as (
        select g.* from grouped g
        where (
            p_seen is null
            or p_seen = ''
            or not (g.fingerprint = any(string_to_array(p_seen, ',')))
        )
    )
    select coalesce(
        json_build_object(
            'groups', (
                select coalesce(json_agg(row_to_json(p)), '[]'::json)
                from (
                    select * from unseen
                    order by last_seen_at desc, fingerprint desc
                    limit least(greatest(coalesce(p_limit, 50), 1), 200)
                ) p
            ),
            'has_more', (
                select count(*) > least(greatest(coalesce(p_limit, 50), 1), 200)
                from unseen
            ),
            'visible_snapshot', (select v0::text from snap)
        ),
        json_build_object('groups', '[]'::json, 'has_more', false)
    );
$$;

revoke all on function public.gateway_log_query(text, text, timestamptz, timestamptz, text, int, text, timestamptz, text, text)
    from public, anon, authenticated;
grant execute on function public.gateway_log_query(text, text, timestamptz, timestamptz, text, int, text, timestamptz, text, text)
    to service_role;

-- ── 3. 单事件详情（不存在返回 null → API 404） ──────────────────
--    commit_xid 是内部可见性锚点，不进入公开投影。
create or replace function public.gateway_log_event(p_event_id uuid)
returns json
language sql stable security invoker
set search_path = public
set timezone = 'UTC'
as $$
    select (
        select json_build_object('event', to_jsonb(e) - 'commit_xid')
        from public.gateway_error_events e
        where e.event_id = p_event_id
    );
$$;

revoke all on function public.gateway_log_event(uuid)
    from public, anon, authenticated;
grant execute on function public.gateway_log_event(uuid)
    to service_role;

-- ── 4. 汇总组详情（摘要 + 有限样本；样本范围由 p_sample_limit 声明） ──
--    BUG-06：详情复用列表的完整筛选（level / module / search / 快照），
--    次数与样本和列表一致，不静默扩大到整组。
--    BUG-05：p_visible 与列表共享同一会话可见性快照——已看组在会话内
--    的次数 / 末次 / 样本不漂移。TASK-02：p_key_ref 与列表筛选一致。
create or replace function public.gateway_log_group(
    p_fingerprint text,
    p_start timestamptz default null,
    p_end timestamptz default null,
    p_level text default null,
    p_module text default null,
    p_search text default null,
    p_snapshot timestamptz default null,
    p_sample_limit int default 20,
    p_visible text default null,
    p_key_ref text default null
) returns json
language sql stable security invoker
set search_path = public
set timezone = 'UTC'
as $$
    with snap as (
        select coalesce(nullif(p_visible, ''), pg_current_snapshot()::text)::pg_snapshot as v0
    ),
    base as (
        select
            e.*, pg_visible_in_snapshot(e.commit_xid, (select v0 from snap)) as v0_visible
        from public.gateway_error_events e
        where e.fingerprint = p_fingerprint
          and (p_start is null or e.occurred_at >= p_start)
          and (p_end is null or e.occurred_at <= p_end)
          and (p_level is null or e.level = p_level)
          and (p_module is null or e.module = p_module)
          and (p_snapshot is null or e.ingested_at <= p_snapshot)
          and (p_key_ref is null or p_key_ref = '' or e.key_ref = p_key_ref)
          and (
                p_search is null or p_search = ''
                or e.error_code ilike '%' || p_search || '%'
                or e.request_id = p_search
                or e.exception_type ilike '%' || p_search || '%'
                or e.operation ilike '%' || p_search || '%'
                or e.message_zh ilike '%' || p_search || '%'
          )
    ),
    gflags as (
        select e.fingerprint, bool_or(e.v0_visible) as has_v0
        from base e
        group by e.fingerprint
    ),
    session_events as (
        select b.*
        from base b
        join gflags g on g.fingerprint = b.fingerprint
        where b.v0_visible or not g.has_v0
    ),
    summary as (
        select
            e.fingerprint,
            count(*)::bigint as event_count,
            min(e.occurred_at) as first_seen_at,
            max(e.occurred_at) as last_seen_at,
            case
                when bool_or(e.level = 'CRITICAL') then 'CRITICAL'
                when bool_or(e.level = 'ERROR') then 'ERROR'
                else 'WARNING'
            end as level,
            (array_agg(e.module order by e.occurred_at desc))[1] as module,
            (array_agg(e.operation order by e.occurred_at desc))[1] as operation,
            (array_agg(e.error_code order by e.occurred_at desc))[1] as error_code,
            (array_agg(e.message_zh order by e.occurred_at desc))[1] as message_zh,
            (array_agg(e.exception_type order by e.occurred_at desc))[1] as exception_type,
            (array_agg(e.logger_name order by e.occurred_at desc))[1] as logger_name,
            (array_agg(e.source order by e.occurred_at desc))[1] as source,
            (array_agg(e.outcome order by e.occurred_at desc))[1] as outcome,
            (array_agg(e.instance_id order by e.occurred_at desc))[1] as instance_id,
            (array_agg(e.key_ref order by e.occurred_at desc))[1] as key_ref,
            (array_agg(e.credential_role order by e.occurred_at desc))[1] as credential_role,
            (array_agg(e.provider order by e.occurred_at desc))[1] as provider
        from session_events e
        group by e.fingerprint
    )
    select coalesce(
        (
            select json_build_object(
                'group', (select row_to_json(s) from summary s),
                'samples', (
                    select coalesce(json_agg(row_to_json(t)), '[]'::json)
                    from (
                        select
                            event_id, occurred_at, level, module, operation,
                            error_code, message_zh, exception_type,
                            message_raw, stack_trace, logger_name, source,
                            request_id, operation_id, route, method,
                            http_status, upstream_status, outcome,
                            instance_id, release_id, safe_context,
                            key_ref, credential_role, provider,
                            truncated_fields
                        from session_events
                        order by occurred_at desc
                        limit least(greatest(coalesce(p_sample_limit, 20), 1), 50)
                    ) t
                )
            )
            where exists (select 1 from summary)
        ),
        json_build_object(
            'group', null,
            'samples', '[]'::json
        )
    );
$$;

revoke all on function public.gateway_log_group(text, timestamptz, timestamptz, text, text, text, timestamptz, int, text, text)
    from public, anon, authenticated;
grant execute on function public.gateway_log_group(text, timestamptz, timestamptz, text, text, text, timestamptz, int, text, text)
    to service_role;

-- ── 5. 筛选元数据（模块 / 级别 / 密钥计数 + 时间范围） ──────────
--    TASK-02：keys 返回去重 key_ref（含用途 / 上游 / 计数），驱动日志页
--    的密钥筛选；空 key_ref 不进入密钥选项（未关联密钥不是一把密钥）。
create or replace function public.gateway_log_facets(
    p_start timestamptz default null,
    p_end timestamptz default null
) returns json
language sql stable security invoker
set search_path = public
set timezone = 'UTC'
as $$
    with base as (
        select * from public.gateway_error_events e
        where (p_start is null or e.occurred_at >= p_start)
          and (p_end is null or e.occurred_at <= p_end)
    )
    select coalesce(
        json_build_object(
            'modules', (
                select coalesce(json_agg(row_to_json(m)), '[]'::json)
                from (
                    select module, count(*)::bigint as count
                    from base group by module order by count desc, module
                ) m
            ),
            'levels', (
                select coalesce(json_agg(row_to_json(l)), '[]'::json)
                from (
                    select level, count(*)::bigint as count
                    from base group by level
                ) l
            ),
            'keys', (
                select coalesce(json_agg(row_to_json(k)), '[]'::json)
                from (
                    select
                        key_ref,
                        (array_agg(credential_role order by occurred_at desc))[1] as credential_role,
                        (array_agg(provider order by occurred_at desc))[1] as provider,
                        count(*)::bigint as count
                    from base
                    where key_ref <> ''
                    group by key_ref
                    order by count desc, key_ref
                ) k
            ),
            'total_events', (select count(*) from base),
            'oldest_at', (select min(occurred_at) from base),
            'newest_at', (select max(occurred_at) from base)
        ),
        json_build_object(
            'modules', '[]'::json, 'levels', '[]'::json, 'keys', '[]'::json,
            'total_events', 0
        )
    );
$$;

revoke all on function public.gateway_log_facets(timestamptz, timestamptz)
    from public, anon, authenticated;
grant execute on function public.gateway_log_facets(timestamptz, timestamptz)
    to service_role;

-- ── 6. 保留期清理（分批 + advisory xact 锁，多实例互斥） ─────────
--    按真实 occurred_at 清理；事务级锁随事务结束自动释放，不会跨请求泄漏。
create or replace function public.gateway_log_cleanup(
    p_retention_days int default 30,
    p_batch_size int default 5000
) returns json
language plpgsql volatile security invoker
set search_path = public
set timezone = 'UTC'
as $$
declare
    v_cutoff timestamptz :=
        now() - (greatest(coalesce(p_retention_days, 30), 1) * interval '1 day');
    v_batch int := least(greatest(coalesce(p_batch_size, 5000), 100), 50000);
    v_deleted bigint := 0;
    v_round_deleted bigint := 0;
    v_rounds int := 0;
begin
    if not pg_try_advisory_xact_lock(hashtext('gateway_log_cleanup')) then
        return json_build_object('status', 'skipped_locked', 'deleted', 0);
    end if;
    loop
        v_rounds := v_rounds + 1;
        with doomed as (
            select event_id from public.gateway_error_events
            where occurred_at < v_cutoff
            order by occurred_at
            limit v_batch
        )
        delete from public.gateway_error_events e
        using doomed d
        where e.event_id = d.event_id;
        get diagnostics v_round_deleted = row_count;
        v_deleted := v_deleted + v_round_deleted;
        exit when v_round_deleted = 0 or v_rounds >= 100;
    end loop;
    return json_build_object(
        'status', 'ok', 'deleted', v_deleted, 'cutoff', v_cutoff
    );
end;
$$;

revoke all on function public.gateway_log_cleanup(int, int)
    from public, anon, authenticated;
grant execute on function public.gateway_log_cleanup(int, int)
    to service_role;

commit;
