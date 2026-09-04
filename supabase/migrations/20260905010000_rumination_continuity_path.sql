-- Rumination continuity path: an independent daily digest lane with its own
-- cursor, run records, claim/heartbeat lease and atomic batch commit.
--
-- public.chat_messages remains immutable evidence: every reference below is a
-- SELECT. memories stays the single source of truth for formal memory content;
-- no second content store is created. Provenance is explicit and queryable:
--   memories.producer_path / memory_requests.producer_path
--       'fast_path' (existing continuity fast path, plugins, MCP, manual)
--       'rumination' (produced by the rumination lane)
--   memories.maintained_by
--       which lane currently maintains the lifecycle (thread takeover flips it)
--   memory_path_handoffs
--       audit rows for every fast-path thread a rumination op takes over
--
-- Recall changes: closed threads (resolved/dissolved/abandoned) leave the
-- default chat recall hot path in BOTH channels; history stays visible in the
-- admin console because the admin data API reads the table directly.
--
-- Fast-path write gating: a fast-path thread candidate that clearly matches an
-- active rumination-maintained thread is no longer auto-approved and direct
-- thread writes refuse to create or rewrite a rumination-maintained truth.
-- Prompts and classification rules of the fast path are untouched.

begin;

-- ---------------------------------------------------------------------------
-- 1. Provenance columns
-- ---------------------------------------------------------------------------

alter table public.memories
    add column if not exists producer_path text not null default 'fast_path',
    add column if not exists maintained_by text not null default 'fast_path';

alter table public.memories
    drop constraint if exists memories_producer_path_check;
alter table public.memories
    add constraint memories_producer_path_check
        check (producer_path in ('fast_path', 'rumination'));

alter table public.memories
    drop constraint if exists memories_maintained_by_check;
alter table public.memories
    add constraint memories_maintained_by_check
        check (maintained_by in ('fast_path', 'rumination'));

alter table public.memory_requests
    add column if not exists producer_path text not null default 'fast_path';

alter table public.memory_requests
    drop constraint if exists memory_requests_producer_path_check;
alter table public.memory_requests
    add constraint memory_requests_producer_path_check
        check (producer_path in ('fast_path', 'rumination'));

comment on column public.memories.producer_path is
    'Production lane that created this row: fast_path or rumination.';
comment on column public.memories.maintained_by is
    'Lane that currently maintains the lifecycle; rumination takeover flips it without erasing the original producer.';
comment on column public.memory_requests.producer_path is
    'Lane that created this review request: fast_path or rumination.';

-- memories.source gains the rumination lane alongside the existing values.
alter table public.memories
    drop constraint if exists memories_source_check;
alter table public.memories
    add constraint memories_source_check
    check (source in (
        'auto_extract',
        'daily_digest',
        'manual',
        'dream',
        'import',
        'ai_tool_request',
        'rumination'
    ));

-- memory_requests.source gains the rumination lane. The interaction_rule
-- source check is extended because the rumination lane files rule requests.
alter table public.memory_requests
    drop constraint if exists memory_requests_source_values;
alter table public.memory_requests
    add constraint memory_requests_source_values
        check (source in ('orangechat_plugin', 'mcp_memory', 'daily_digest', 'rumination'));

alter table public.memory_requests
    drop constraint if exists memory_requests_interaction_rule_source_check;
alter table public.memory_requests
    add constraint memory_requests_interaction_rule_source_check check (
        continuity_type is distinct from 'interaction_rule'
        or source in ('orangechat_plugin', 'mcp_memory', 'rumination')
    );

-- Reviewed approvals/merges copy the request's lane onto the formal memory so
-- an approved rumination request stays rumination-provenance after review.
-- Rebuilt verbatim from the final pre-migration implementation with the
-- producer_path/maintained_by copies added.
create or replace function public.sync_reviewed_memory_request_metadata()
returns trigger
language plpgsql
set search_path to 'public','extensions'
as $function$
declare
    v_existing_continuity_id uuid;
    v_replace_continuity boolean := false;
begin
    if new.status in ('approved','merged') and new.memory_id is not null then
        select continuity_id into v_existing_continuity_id
        from public.memories
        where id = new.memory_id
        for update;
        if v_existing_continuity_id is not null
           and v_existing_continuity_id is distinct from new.continuity_id then
            raise exception 'memory_request_continuity_identity_conflict';
        end if;
        v_replace_continuity := (
            v_existing_continuity_id is null
            or v_existing_continuity_id = new.continuity_id
        )
            and new.continuity_id is not null
            and new.continuity_data is not null
            and new.continuity_schema_version = 1
            and public.validate_continuity_data(
                new.continuity_type,new.thread_state,new.continuity_data
            );

        update public.memories as memory set
            evidence_message_ids = coalesce(new.evidence_message_ids,memory.evidence_message_ids),
            source_time = coalesce(new.source_time,memory.source_time),
            memory_time = coalesce(new.memory_time,memory.memory_time),
            time_precision = coalesce(new.time_precision,memory.time_precision),
            source_first_message_id = coalesce(
                (select min(value) from unnest(new.evidence_message_ids) as value),
                memory.source_first_message_id
            ),
            source_last_message_id = coalesce(
                (select max(value) from unnest(new.evidence_message_ids) as value),
                memory.source_last_message_id
            ),
            digest_run_id = coalesce(new.digest_run_id,memory.digest_run_id),
            embedding = coalesce(new.embedding,memory.embedding),
            confidence = coalesce(new.confidence,memory.confidence),
            recall_scene = coalesce(new.recall_scene,memory.recall_scene),
            recall_tags = coalesce(new.recall_tags,memory.recall_tags),
            continuity_type = case when v_replace_continuity
                then coalesce(new.continuity_type,memory.continuity_type)
                else memory.continuity_type end,
            continuity_id = case when v_replace_continuity
                then coalesce(new.continuity_id,memory.continuity_id)
                else memory.continuity_id end,
            continuity_schema_version = case when v_replace_continuity
                then coalesce(new.continuity_schema_version,memory.continuity_schema_version)
                else memory.continuity_schema_version end,
            continuity_data = case when v_replace_continuity
                then new.continuity_data
                else memory.continuity_data end,
            source_type = case when v_replace_continuity
                then coalesce(new.source_type,memory.source_type)
                else memory.source_type end,
            thread_state = case when v_replace_continuity
                then coalesce(new.thread_state,memory.thread_state)
                else memory.thread_state end,
            evidence_start_time = coalesce(new.evidence_start_time,memory.evidence_start_time),
            evidence_end_time = coalesce(new.evidence_end_time,memory.evidence_end_time),
            evidence_time_precision = coalesce(new.evidence_time_precision,memory.evidence_time_precision),
            source = case
                when new.source = 'daily_digest' then 'daily_digest'
                when new.source = 'rumination' then 'rumination'
                else memory.source
            end,
            producer_path = case
                when new.producer_path in ('fast_path', 'rumination') then new.producer_path
                else memory.producer_path
            end,
            maintained_by = case
                when new.producer_path in ('fast_path', 'rumination') then new.producer_path
                else memory.maintained_by
            end
        where memory.id = new.memory_id;
        update public.memory_continuity_objects
        set updated_at = now()
        where continuity_id = new.continuity_id;
    end if;
    return new;
end;
$function$;

revoke all on function public.sync_reviewed_memory_request_metadata()
    from public, anon, authenticated;
grant execute on function public.sync_reviewed_memory_request_metadata()
    to service_role;

drop trigger if exists sync_reviewed_memory_request_metadata
    on public.memory_requests;
create trigger sync_reviewed_memory_request_metadata
after insert or update of status, memory_id
on public.memory_requests
for each row
execute function public.sync_reviewed_memory_request_metadata();

-- ---------------------------------------------------------------------------
-- 2. Run bookkeeping: rumination pipeline marker and op counters
-- ---------------------------------------------------------------------------

alter table public.memory_digest_runs
    add column if not exists op_counts jsonb;

alter table public.memory_digest_runs
    drop constraint if exists memory_digest_runs_pipeline_check;
alter table public.memory_digest_runs
    add constraint memory_digest_runs_pipeline_check
        check (pipeline in ('legacy', 'continuity', 'rumination'));

alter table public.memory_digest_runs
    drop constraint if exists memory_digest_runs_trigger_check;
alter table public.memory_digest_runs
    add constraint memory_digest_runs_trigger_check
        check (trigger in (
            'manual_preview', 'manual_execute', 'scheduled_daily', 'idle_six_hours',
            'continuity_threshold', 'continuity_manual', 'continuity_retry', 'continuity_skip',
            'rumination_scheduled', 'rumination_manual', 'rumination_retry'
        ));

-- ---------------------------------------------------------------------------
-- 3. Independent rumination cursor
-- ---------------------------------------------------------------------------

create table if not exists public.memory_rumination_cursors (
    assistant_id text primary key,
    initialized boolean not null default false,
    last_processed_message_id bigint not null default 0,
    status text not null default 'ready',
    last_scheduled_date date,
    last_success_at timestamptz,
    last_batch_first_message_id bigint,
    last_batch_last_message_id bigint,
    last_batch_message_count integer,
    updated_at timestamptz not null default now(),
    constraint memory_rumination_cursors_position_check
        check (last_processed_message_id >= 0),
    constraint memory_rumination_cursors_status_check
        check (status = 'ready')
);

alter table public.memory_rumination_cursors enable row level security;

comment on table public.memory_rumination_cursors is
    'Independent cursor for the rumination continuity path; never shared with the continuity fast path.';
comment on column public.memory_rumination_cursors.initialized is
    'false until the first formal run commits; the first run consumes only the latest 120 messages.';
comment on column public.memory_rumination_cursors.last_scheduled_date is
    'Asia/Shanghai calendar date of the last scheduled trigger that reached a terminal state, so the 06:00 run happens once per day.';

create or replace function public.get_or_create_rumination_cursor(
    p_assistant_id text
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_cursor public.memory_rumination_cursors%rowtype;
begin
    if nullif(trim(coalesce(p_assistant_id, '')), '') is null then
        raise exception 'memory_rumination_invalid_assistant';
    end if;

    insert into public.memory_rumination_cursors (assistant_id)
    values (p_assistant_id)
    on conflict (assistant_id) do nothing;

    select * into v_cursor
    from public.memory_rumination_cursors
    where assistant_id = p_assistant_id;

    return to_jsonb(v_cursor);
end;
$function$;

revoke all on function public.get_or_create_rumination_cursor(text)
    from public, anon, authenticated;
grant execute on function public.get_or_create_rumination_cursor(text)
    to service_role;

-- ---------------------------------------------------------------------------
-- 4. Cross-path handoff audit ("absorbed/adopted by rumination")
-- ---------------------------------------------------------------------------

create table if not exists public.memory_path_handoffs (
    id bigint generated by default as identity primary key,
    assistant_id text not null,
    run_id bigint,
    kind text not null,
    fast_path_memory_id integer references public.memories(id) on delete set null,
    rumination_memory_id integer references public.memories(id) on delete set null,
    memory_key text,
    continuity_id uuid,
    note text,
    created_at timestamptz not null default now(),
    constraint memory_path_handoffs_kind_check
        check (kind in ('adopt_thread')),
    constraint memory_path_handoffs_note_length
        check (note is null or char_length(note) <= 500)
);

alter table public.memory_path_handoffs enable row level security;

create index if not exists memory_path_handoffs_fast_path_idx
    on public.memory_path_handoffs (fast_path_memory_id, created_at desc);
create index if not exists memory_path_handoffs_run_idx
    on public.memory_path_handoffs (run_id);

comment on table public.memory_path_handoffs is
    'Audit trail for cross-lane handoffs: why a fast-path memory left the active truth or is now rumination-maintained.';

-- ---------------------------------------------------------------------------
-- 5. Claim / skip RPCs (claim-lease + heartbeat pattern, rumination scope)
-- ---------------------------------------------------------------------------

create or replace function public.claim_rumination_batch(
    p_assistant_id text,
    p_trigger text,
    p_first_message_id bigint,
    p_last_message_id bigint,
    p_message_count bigint,
    p_first_batch boolean
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_run_id bigint;
    v_cursor public.memory_rumination_cursors%rowtype;
    v_stale_cutoff timestamptz := now() - interval '30 minutes';
begin
    if p_trigger not in ('rumination_scheduled', 'rumination_manual', 'rumination_retry') then
        raise exception 'memory_rumination_invalid_trigger';
    end if;
    if p_first_message_id is null or p_last_message_id is null
       or p_last_message_id < p_first_message_id
       or coalesce(p_message_count, 0) < 1 then
        raise exception 'memory_rumination_invalid_batch';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 7));

    select id into v_run_id
    from public.memory_digest_runs
    where assistant_id = p_assistant_id
      and pipeline = 'rumination'
      and status in ('claimed', 'running')
      and claimed_at is not null
      and claimed_at > v_stale_cutoff
      and (heartbeat_at is null or heartbeat_at > v_stale_cutoff)
    order by claimed_at desc
    limit 1;
    if found then
        return jsonb_build_object('status', 'already_running', 'run_id', v_run_id);
    end if;

    insert into public.memory_rumination_cursors (assistant_id)
    values (p_assistant_id)
    on conflict (assistant_id) do nothing;

    select * into v_cursor
    from public.memory_rumination_cursors
    where assistant_id = p_assistant_id
    for update;

    if p_first_batch then
        if v_cursor.initialized then
            return jsonb_build_object('status', 'already_initialized');
        end if;
    else
        if not v_cursor.initialized then
            return jsonb_build_object('status', 'not_initialized');
        end if;
    end if;
    if p_first_message_id <= v_cursor.last_processed_message_id then
        return jsonb_build_object(
            'status', 'batch_stale',
            'cursor', v_cursor.last_processed_message_id
        );
    end if;

    insert into public.memory_digest_runs (
        assistant_id, pipeline, trigger, mode, status,
        claimed_at, heartbeat_at, started_at,
        source_first_message_id, source_last_message_id, message_count
    ) values (
        p_assistant_id, 'rumination', p_trigger, 'execute', 'running',
        now(), now(), now(),
        p_first_message_id, p_last_message_id, p_message_count
    ) returning id into v_run_id;

    return jsonb_build_object('status', 'claimed', 'run_id', v_run_id);
end;
$function$;

revoke all on function public.claim_rumination_batch(text, text, bigint, bigint, bigint, boolean)
    from public, anon, authenticated;
grant execute on function public.claim_rumination_batch(text, text, bigint, bigint, bigint, boolean)
    to service_role;

create or replace function public.record_rumination_skipped(
    p_assistant_id text,
    p_trigger text,
    p_backlog_count bigint,
    p_reason text
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_run_id bigint;
begin
    if p_trigger not in ('rumination_scheduled', 'rumination_manual', 'rumination_retry') then
        raise exception 'memory_rumination_invalid_trigger';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 7));

    insert into public.memory_digest_runs (
        assistant_id, pipeline, trigger, mode, status,
        message_count, extracted_count, inserted_count, preview_memories,
        error_code, error_message,
        started_at, completed_at
    ) values (
        p_assistant_id, 'rumination', p_trigger, 'execute', 'skipped',
        greatest(coalesce(p_backlog_count, 0), 0), 0, 0, '[]'::jsonb,
        'waiting_for_batch_threshold',
        nullif(left(trim(coalesce(p_reason, '')), 500), ''),
        now(), now()
    ) returning id into v_run_id;

    -- A skipped scheduled run still consumed the day's 06:00 slot; the cursor
    -- stays untouched either way.
    if p_trigger = 'rumination_scheduled' then
        update public.memory_rumination_cursors set
            last_scheduled_date = (now() at time zone 'Asia/Shanghai')::date,
            updated_at = now()
        where assistant_id = p_assistant_id;
    end if;

    return jsonb_build_object('status', 'skipped', 'run_id', v_run_id);
end;
$function$;

revoke all on function public.record_rumination_skipped(text, text, bigint, text)
    from public, anon, authenticated;
grant execute on function public.record_rumination_skipped(text, text, bigint, text)
    to service_role;

-- ---------------------------------------------------------------------------
-- 6. Atomic rumination batch commit
-- ---------------------------------------------------------------------------

create or replace function public.commit_rumination_batch(
    p_run_id bigint,
    p_ops jsonb
)
returns jsonb
language plpgsql
security definer
set search_path to 'public', 'extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_cursor public.memory_rumination_cursors%rowtype;
    v_ops jsonb;
    v_op jsonb;
    v_preview jsonb := '[]'::jsonb;
    v_counts jsonb;
    v_op_type text;
    v_reason text;
    v_requested_evidence bigint[];
    v_evidence bigint[];
    v_evidence_count integer;
    v_ev_start timestamptz;
    v_ev_end timestamptz;
    v_content text;
    v_content_hash text;
    v_title text;
    v_importance integer;
    v_confidence double precision;
    v_source_type text;
    v_recall_scene text;
    v_recall_tags text[];
    v_recall_embedding extensions.vector;
    v_embedding extensions.vector;
    v_memory_time timestamptz;
    v_time_precision text;
    v_continuity_type text;
    v_thread_state text;
    v_continuity_data jsonb;
    v_memory_key text;
    v_target_id integer;
    v_target public.memories%rowtype;
    v_new_memory_id integer;
    v_new_continuity_id uuid;
    v_conversation_id text;
    v_delta integer;
    v_op_count integer := 0;
    v_written integer := 0;
    v_dup_max_evidence bigint;
    v_prior_request_id bigint;
    v_idempotency_key text;
    v_ev_precision text := 'minute';
    v_update_mode text;
begin
    v_counts := jsonb_build_object(
        'ignored', 0,
        'created_memories', 0,
        'created_threads', 0,
        'adopted_threads', 0,
        'evidence_only', 0,
        'updated_versions', 0,
        'paused', 0,
        'resumed', 0,
        'resolved', 0,
        'created_requests', 0,
        'skipped_duplicates', 0
    );

    if jsonb_typeof(coalesce(p_ops, '{}'::jsonb)) <> 'object'
       or jsonb_typeof(coalesce(p_ops->'operations', '[]'::jsonb)) <> 'array' then
        raise exception 'memory_rumination_ops_invalid';
    end if;
    v_ops := p_ops->'operations';
    if jsonb_array_length(v_ops) > 24 then
        raise exception 'memory_rumination_too_many_operations';
    end if;

    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;
    if not found or v_run.pipeline <> 'rumination'
       or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_rumination_invalid_run';
    end if;
    if v_run.source_first_message_id is null or v_run.source_last_message_id is null
       or coalesce(v_run.message_count, 0) < 1 then
        raise exception 'memory_rumination_missing_batch';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id, 7));

    select * into v_cursor
    from public.memory_rumination_cursors
    where assistant_id = v_run.assistant_id
    for update;
    if not found then
        raise exception 'memory_rumination_cursor_missing';
    end if;
    if v_cursor.initialized and v_run.source_first_message_id <= v_cursor.last_processed_message_id then
        raise exception 'memory_rumination_batch_stale';
    end if;

    for v_op in select value from jsonb_array_elements(v_ops) loop
        v_op_count := v_op_count + 1;
        v_op_type := v_op->>'op';
        if v_op_type not in (
            'ignore', 'create_memory', 'create_tracked_thread', 'adopt_thread',
            'evidence_only', 'update_thread', 'pause_thread', 'resume_thread',
            'resolve_thread', 'create_request'
        ) then
            raise exception 'memory_rumination_invalid_op';
        end if;
        v_reason := nullif(left(trim(coalesce(v_op->>'reason', '')), 500), '');

        -- Evidence: every referenced id must really exist inside this batch
        -- window for this assistant. Hallucinated or out-of-batch ids abort
        -- the whole batch (nothing is committed, cursor never moves).
        v_requested_evidence := coalesce(array_agg(distinct value::bigint order by value::bigint), '{}'::bigint[])
            from jsonb_array_elements_text(coalesce(v_op->'evidence_message_ids', '[]'::jsonb)) as entry(value)
            where entry.value ~ '^[0-9]+$';
        v_evidence_count := coalesce(cardinality(v_requested_evidence), 0);
        if v_evidence_count <> jsonb_array_length(coalesce(v_op->'evidence_message_ids', '[]'::jsonb))
           or v_evidence_count not between 1 and 8 then
            raise exception 'memory_rumination_invalid_evidence';
        end if;

        select coalesce(array_agg(message.id order by message.id), '{}'::bigint[]),
               min(message.created_at),
               max(message.created_at)
        into v_evidence, v_ev_start, v_ev_end
        from public.chat_messages as message
        where message.id = any(v_requested_evidence)
          and message.assistant_id = v_run.assistant_id
          and message.id between v_run.source_first_message_id and v_run.source_last_message_id;
        if cardinality(v_evidence) <> v_evidence_count then
            raise exception 'memory_rumination_invalid_evidence';
        end if;

        select message.conversation_id into v_conversation_id
        from public.chat_messages as message
        where message.id = v_evidence[1];

        if v_op_type = 'ignore' then
            v_counts := jsonb_set(v_counts, '{ignored}', (coalesce((v_counts->>'ignored')::integer, 0) + 1)::text::jsonb);
            v_preview := v_preview || jsonb_build_object(
                'op', 'ignore', 'commit_status', 'ignored',
                'reason', v_reason,
                'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        -- Shared metadata extraction for content-bearing ops.
        v_content := left(trim(coalesce(v_op->>'content', '')), 600);
        v_content_hash := lower(trim(coalesce(v_op->>'content_hash', '')));
        v_title := nullif(left(trim(coalesce(v_op->>'title', '')), 100), '');
        v_importance := least(greatest(coalesce((v_op->>'importance')::integer, 5), 1), 10);
        v_confidence := least(greatest(coalesce((v_op->>'confidence')::double precision, 0.6), 0), 1);
        v_source_type := nullif(trim(coalesce(v_op->>'source_type', '')), '');
        if v_source_type is not null and v_source_type not in (
            'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
            'roleplay', 'tool_result', 'system_meta', 'unknown'
        ) then
            raise exception 'memory_rumination_invalid_source_type';
        end if;
        v_recall_scene := nullif(btrim(coalesce(v_op->>'recall_scene', '')), '');
        v_recall_tags := case
            when jsonb_typeof(coalesce(v_op->'recall_tags', '[]'::jsonb)) = 'array'
            then array(
                select t.value
                from jsonb_array_elements_text(v_op->'recall_tags') as t(value)
                where nullif(btrim(t.value), '') is not null
            )
            else null
        end;
        -- A non-empty recall_scene must always arrive with its own vector: the
        -- recall embedding serves the scene alone, and a scene saved without a
        -- vector is a permanently incomplete write. The op-level failure
        -- aborts the whole batch transaction, so no memory row, no continuity
        -- object, no handoff row and no cursor advance can survive it.
        if v_recall_scene is not null
           and (not (v_op ? 'recall_embedding') or v_op->'recall_embedding' = 'null'::jsonb) then
            raise exception 'memory_rumination_missing_recall_embedding';
        end if;
        v_recall_embedding := case
            when v_recall_scene is not null
                 and v_op ? 'recall_embedding'
                 and v_op->'recall_embedding' <> 'null'::jsonb
            then (v_op->>'recall_embedding')::extensions.vector
            else null
        end;
        v_time_precision := case
            when v_op->>'time_precision' in ('minute', 'hour', 'day', 'approximate', 'unknown')
            then v_op->>'time_precision'
            else 'unknown'
        end;
        v_memory_time := case
            when nullif(v_op->>'memory_time', '') is null then null
            when v_time_precision = 'day' and (v_op->>'memory_time') ~ '^\d{4}-\d{2}-\d{2}$'
            then ((v_op->>'memory_time')::date::timestamp at time zone 'Asia/Shanghai')
            else (v_op->>'memory_time')::timestamptz
        end;
        v_embedding := case
            when v_op ? 'embedding' and v_op->'embedding' <> 'null'::jsonb
            then (v_op->>'embedding')::extensions.vector
            else null
        end;

        -- adopt_thread may be contentless (pure in-place takeover); all other
        -- content-bearing ops require a valid body and hash.
        if v_op_type in ('create_memory', 'create_tracked_thread', 'update_thread', 'pause_thread', 'resume_thread', 'resolve_thread', 'create_request') then
            if char_length(v_content) < 5 or v_content_hash !~ '^[0-9a-f]{64}$' then
                raise exception 'memory_rumination_invalid_content';
            end if;
            v_continuity_type := v_op->>'continuity_type';
            v_thread_state := nullif(trim(coalesce(v_op->>'thread_state', '')), '');
            v_continuity_data := v_op->'continuity_data';
            if v_continuity_data is not null and jsonb_typeof(v_continuity_data) <> 'object' then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
        elsif v_op_type = 'adopt_thread' then
            v_continuity_type := 'thread';
            v_thread_state := nullif(trim(coalesce(v_op->>'thread_state', '')), '');
            v_continuity_data := v_op->'continuity_data';
            if v_continuity_data is not null and jsonb_typeof(v_continuity_data) <> 'object' then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if char_length(v_content) >= 5 or char_length(trim(coalesce(v_op->>'content', ''))) > 0 then
                if char_length(v_content) < 5 or v_content_hash !~ '^[0-9a-f]{64}$' then
                    raise exception 'memory_rumination_invalid_content';
                end if;
            end if;
        end if;

        -- Thread-target ops share target resolution. Any unfinished thread is
        -- addressable; acting on a fast_path thread implicitly takes it over
        -- (maintained_by flips and an audit row is written in this transaction).
        -- A takeover must end with a stable memory_key: ops other than
        -- adopt_thread refuse a keyless fast_path target unless the op itself
        -- carries the key to fill in.
        if v_op_type in ('adopt_thread', 'evidence_only', 'update_thread', 'pause_thread', 'resume_thread', 'resolve_thread') then
            v_target_id := nullif(trim(coalesce(v_op->>'target_memory_id', '')), '')::integer;
            if v_target_id is null then
                raise exception 'memory_rumination_target_required';
            end if;
            select * into v_target
            from public.memories
            where id = v_target_id
              and assistant_id = v_run.assistant_id
              and continuity_type = 'thread'
              and verified = 'verified'
              and is_active = true
            for update;
            if not found then
                raise exception 'memory_rumination_invalid_target';
            end if;
            if v_target.thread_state not in ('open', 'paused') then
                raise exception 'memory_rumination_target_closed';
            end if;
            if v_op_type <> 'adopt_thread'
               and v_target.maintained_by = 'fast_path'
               and v_target.memory_key is null
               and nullif(trim(coalesce(v_op->>'memory_key', '')), '') is null then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
        end if;

        if v_op_type = 'create_memory' then
            -- Terminal and ordinary direct writes: moment and inside_joke only.
            -- episode/profile/interaction_rule must go through create_request;
            -- thread has its own ops and is never a terminal class.
            if v_continuity_type not in ('moment', 'inside_joke') then
                raise exception 'memory_rumination_invalid_direct_type';
            end if;
            if v_thread_state is not null then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;
            if not public.validate_continuity_data(v_continuity_type, null, v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;

            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.content_hash = v_content_hash
                  and request.status in ('pending', 'approved', 'merged')
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_memory', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.is_active = true
                  and memory.verified = 'verified'
                  and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
                  and memory.content_hash = v_content_hash
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_memory', 'commit_status', 'skipped_active_memory',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            insert into public.memory_continuity_objects (assistant_id)
            values (v_run.assistant_id)
            returning continuity_id into v_new_continuity_id;

            insert into public.memories (
                content, title, tags, heat, importance, embedding,
                source, verified, is_active, recall_count,
                assistant_id, digest_run_id,
                source_first_message_id, source_last_message_id,
                confidence, content_hash,
                continuity_id, continuity_schema_version, continuity_data,
                continuity_type, thread_state,
                memory_time, time_precision,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision,
                recall_scene, recall_tags, recall_embedding,
                source_type, producer_path, maintained_by
            ) values (
                v_content, v_title, array[v_continuity_type],
                least(greatest(v_importance * 10.0, 0), 100), v_importance, v_embedding,
                'rumination', 'verified', true, 0,
                v_run.assistant_id, v_run.id,
                v_evidence[1], v_evidence[cardinality(v_evidence)],
                v_confidence, v_content_hash,
                v_new_continuity_id, 1, v_continuity_data,
                v_continuity_type, null,
                v_memory_time, v_time_precision,
                v_evidence, v_ev_start, v_ev_end,
                v_ev_precision,
                v_recall_scene, coalesce(v_recall_tags, '{}'::text[]), v_recall_embedding,
                v_source_type, 'rumination', 'rumination'
            )
            returning id into v_new_memory_id;

            v_counts := jsonb_set(v_counts, '{created_memories}',
                ((v_counts->>'created_memories')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'create_memory', 'commit_status', 'inserted_memory',
                'memory_id', v_new_memory_id,
                'continuity_type', v_continuity_type,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'create_tracked_thread' then
            if v_continuity_type is not null and v_continuity_type <> 'thread' then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            v_memory_key := lower(trim(coalesce(v_op->>'memory_key', '')));
            if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            if v_thread_state is null or v_thread_state <> 'open' then
                raise exception 'memory_rumination_invalid_thread_state';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;
            if not public.validate_continuity_data('thread', 'open', v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.memory_key = v_memory_key
                  and memory.is_active = true
                  and memory.verified = 'verified'
            ) then
                raise exception 'memory_rumination_memory_key_conflict';
            end if;
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.content_hash = v_content_hash
                  and request.status in ('pending', 'approved', 'merged')
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_tracked_thread', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.is_active = true
                  and memory.verified = 'verified'
                  and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
                  and memory.content_hash = v_content_hash
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_tracked_thread', 'commit_status', 'skipped_active_memory',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            insert into public.memory_continuity_objects (assistant_id)
            values (v_run.assistant_id)
            returning continuity_id into v_new_continuity_id;

            insert into public.memories (
                content, title, tags, heat, importance, embedding,
                source, verified, is_active, recall_count,
                assistant_id, digest_run_id,
                source_first_message_id, source_last_message_id,
                confidence, content_hash,
                memory_key,
                continuity_id, continuity_schema_version, continuity_data,
                continuity_type, thread_state,
                memory_time, time_precision,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision,
                recall_scene, recall_tags, recall_embedding,
                source_type, producer_path, maintained_by
            ) values (
                v_content, v_title, array['thread'],
                least(greatest(v_importance * 10.0, 0), 100), v_importance, v_embedding,
                'rumination', 'verified', true, 0,
                v_run.assistant_id, v_run.id,
                v_evidence[1], v_evidence[cardinality(v_evidence)],
                v_confidence, v_content_hash,
                v_memory_key,
                v_new_continuity_id, 1, v_continuity_data,
                'thread', 'open',
                v_memory_time, v_time_precision,
                v_evidence, v_ev_start, v_ev_end,
                v_ev_precision,
                v_recall_scene, coalesce(v_recall_tags, '{}'::text[]), v_recall_embedding,
                v_source_type, 'rumination', 'rumination'
            )
            returning id into v_new_memory_id;

            v_counts := jsonb_set(v_counts, '{created_threads}',
                ((v_counts->>'created_threads')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'create_tracked_thread', 'commit_status', 'inserted_thread',
                'memory_id', v_new_memory_id,
                'memory_key', v_memory_key,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'adopt_thread' then
            -- Takeover is only meaningful for fast-path threads; rumination's
            -- own threads are addressed with evidence_only/update/... ops.
            if v_target.maintained_by <> 'fast_path' then
                raise exception 'memory_rumination_not_fast_path';
            end if;
            v_memory_key := lower(trim(coalesce(v_op->>'memory_key', v_target.memory_key, '')));
            if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            if exists (
                select 1 from public.memories as memory
                where memory.memory_key = v_memory_key
                  and memory.is_active = true
                  and memory.verified = 'verified'
                  and memory.id <> v_target.id
            ) then
                raise exception 'memory_rumination_memory_key_conflict';
            end if;
            if nullif(trim(coalesce(v_op->>'thread_state', '')), '') is not null
               and nullif(trim(coalesce(v_op->>'thread_state', '')), '') <> v_target.thread_state then
                raise exception 'memory_rumination_state_change_forbidden';
            end if;

            if char_length(v_content) >= 5
               and v_content_hash !~ '^[0-9a-f]{64}$' then
                raise exception 'memory_rumination_invalid_content';
            end if;

            if char_length(v_content) >= 5 and v_content_hash <> v_target.content_hash then
                -- Versioned takeover. Soft-deactivate first, then insert the
                -- successor and link both directions: the whole sequence is
                -- one transaction, so a failure restores the old row while
                -- the partial unique index never sees two active versions of
                -- the same memory_key.
                if v_embedding is null then
                    raise exception 'memory_rumination_missing_embedding';
                end if;
                if v_continuity_data is null
                   or not public.validate_continuity_data('thread', v_target.thread_state, v_continuity_data) then
                    raise exception 'memory_rumination_invalid_continuity_data';
                end if;
                update public.memories
                set is_active = false,
                    superseded_at = now(),
                    superseded_by_memory_id = null
                where id = v_target.id;
                insert into public.memories (
                    content, title, tags, heat, importance, embedding,
                    source, verified, is_active, recall_count,
                    assistant_id, digest_run_id,
                    source_first_message_id, source_last_message_id,
                    confidence, content_hash,
                    memory_key, supersedes_memory_id,
                    continuity_id, continuity_schema_version, continuity_data,
                    continuity_type, thread_state,
                    memory_time, time_precision,
                    evidence_message_ids, evidence_start_time, evidence_end_time,
                    evidence_time_precision,
                    recall_scene, recall_tags, recall_embedding,
                    source_type, producer_path, maintained_by
                ) values (
                    v_content, coalesce(v_title, v_target.title), v_target.tags,
                    v_target.heat, v_importance, v_embedding,
                    'rumination', 'verified', true, 0,
                    v_run.assistant_id, v_run.id,
                    v_evidence[1], v_evidence[cardinality(v_evidence)],
                    v_confidence, v_content_hash,
                    v_memory_key, v_target.id,
                    v_target.continuity_id, 1, v_continuity_data,
                    'thread', v_target.thread_state,
                    v_memory_time, v_time_precision,
                    (select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                     from unnest(v_target.evidence_message_ids || v_evidence) as id),
                    least(coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                    greatest(coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                    v_ev_precision,
                    coalesce(v_recall_scene, v_target.recall_scene),
                    coalesce(v_recall_tags, v_target.recall_tags, '{}'::text[]),
                    coalesce(v_recall_embedding, v_target.recall_embedding),
                    coalesce(v_source_type, v_target.source_type),
                    'rumination', 'rumination'
                )
                returning id into v_new_memory_id;

                update public.memories
                set superseded_by_memory_id = v_new_memory_id
                where id = v_target.id;
            else
                -- In-place takeover: keep the fast-path row and its evidence;
                -- only the maintainer marker and the stable key change.
                v_new_memory_id := v_target.id;
                update public.memories
                set memory_key = v_memory_key,
                    maintained_by = 'rumination',
                    evidence_message_ids = (
                        select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                        from unnest(v_target.evidence_message_ids || v_evidence) as id
                    ),
                    evidence_start_time = least(
                        coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                    evidence_end_time = greatest(
                        coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                    evidence_time_precision = case
                        when v_ev_start is null then v_target.evidence_time_precision
                        else 'minute'
                    end
                where id = v_target.id;
            end if;

            insert into public.memory_path_handoffs (
                assistant_id, run_id, kind,
                fast_path_memory_id, rumination_memory_id,
                memory_key, continuity_id, note
            ) values (
                v_run.assistant_id, v_run.id, 'adopt_thread',
                v_target.id, v_new_memory_id,
                v_memory_key, v_target.continuity_id,
                left(coalesce(v_reason, ''), 500)
            );

            v_counts := jsonb_set(v_counts, '{adopted_threads}',
                ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'adopt_thread', 'commit_status', 'adopted',
                'memory_id', v_new_memory_id,
                'fast_path_memory_id', v_target.id,
                'memory_key', v_memory_key,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'evidence_only' then
            -- Repeated expression: merge evidence into the current active
            -- version, never rewrite the body, never create a version. An op
            -- (or merge) carrying a key fills in a keyless fast_path takeover.
            v_memory_key := coalesce(
                nullif(lower(trim(coalesce(v_op->>'memory_key', ''))), ''),
                v_target.memory_key
            );
            if v_memory_key is not null
               and v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                raise exception 'memory_rumination_invalid_memory_key';
            end if;
            update public.memories
            set maintained_by = 'rumination',
                memory_key = v_memory_key,
                evidence_message_ids = (
                    select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                    from unnest(v_target.evidence_message_ids || v_evidence) as id
                ),
                evidence_start_time = least(
                    coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                evidence_end_time = greatest(
                    coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                evidence_time_precision = case
                    when v_ev_start is null then v_target.evidence_time_precision
                    else 'minute'
                end
            where id = v_target.id;

            if v_target.maintained_by = 'fast_path' then
                insert into public.memory_path_handoffs (
                    assistant_id, run_id, kind,
                    fast_path_memory_id, rumination_memory_id,
                    memory_key, continuity_id, note
                ) values (
                    v_run.assistant_id, v_run.id, 'adopt_thread',
                    v_target.id, v_target.id,
                    v_memory_key, v_target.continuity_id,
                    left(coalesce(v_reason, 'evidence merge takeover'), 500)
                );
                v_counts := jsonb_set(v_counts, '{adopted_threads}',
                    ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
            end if;
            v_counts := jsonb_set(v_counts, '{evidence_only}',
                ((v_counts->>'evidence_only')::integer + 1)::text::jsonb);
            v_preview := v_preview || jsonb_build_object(
                'op', 'evidence_only', 'commit_status', 'evidence_merged',
                'memory_id', v_target.id,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type in ('update_thread', 'pause_thread', 'resume_thread', 'resolve_thread') then
            v_thread_state := case v_op_type
                when 'update_thread' then coalesce(nullif(trim(coalesce(v_op->>'thread_state', '')), ''), v_target.thread_state)
                when 'pause_thread' then 'paused'
                when 'resume_thread' then 'open'
                else 'resolved'
            end;
            if v_op_type = 'update_thread' and v_thread_state <> v_target.thread_state then
                raise exception 'memory_rumination_state_change_forbidden';
            end if;
            if v_op_type = 'pause_thread' and v_target.thread_state <> 'open' then
                raise exception 'memory_rumination_invalid_state_transition';
            end if;
            if v_op_type = 'resume_thread' and v_target.thread_state <> 'paused' then
                raise exception 'memory_rumination_invalid_state_transition';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;
            if v_content_hash = v_target.content_hash and v_op_type = 'update_thread' then
                -- Unchanged body: the batch only adds evidence.
                update public.memories
                set evidence_message_ids = (
                        select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                        from unnest(v_target.evidence_message_ids || v_evidence) as id
                    ),
                    evidence_start_time = least(
                        coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                    evidence_end_time = greatest(
                        coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end)
                where id = v_target.id;
                v_counts := jsonb_set(v_counts, '{evidence_only}',
                    ((v_counts->>'evidence_only')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'update_thread', 'commit_status', 'evidence_merged_unchanged',
                    'memory_id', v_target.id,
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;
            if not public.validate_continuity_data('thread', v_thread_state, v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;

            -- The version keeps the thread's stable key; an op-provided key
            -- only fills in the key of a keyless fast_path takeover, and it
            -- never re-keys an existing rumination thread.
            v_memory_key := coalesce(
                case when v_target.memory_key is null
                     then nullif(lower(trim(coalesce(v_op->>'memory_key', ''))), '') end,
                v_target.memory_key
            );
            if v_memory_key is not null then
                if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                    raise exception 'memory_rumination_invalid_memory_key';
                end if;
                if exists (
                    select 1 from public.memories as memory
                    where memory.memory_key = v_memory_key
                      and memory.is_active = true
                      and memory.verified = 'verified'
                      and memory.id <> v_target.id
                ) then
                    raise exception 'memory_rumination_memory_key_conflict';
                end if;
            end if;

            -- Soft-deactivate first, then insert the successor and link both
            -- directions: one transaction, so a failure restores the old row
            -- while the partial unique index never sees two active versions
            -- of the same memory_key. The version keeps the same memory_key
            -- and continuity_id.
            update public.memories
            set is_active = false,
                superseded_at = now(),
                superseded_by_memory_id = null
            where id = v_target.id;

            insert into public.memories (
                content, title, tags, heat, importance, embedding,
                source, verified, is_active, recall_count,
                assistant_id, digest_run_id,
                source_first_message_id, source_last_message_id,
                confidence, content_hash,
                memory_key, supersedes_memory_id,
                continuity_id, continuity_schema_version, continuity_data,
                continuity_type, thread_state,
                memory_time, time_precision,
                evidence_message_ids, evidence_start_time, evidence_end_time,
                evidence_time_precision,
                recall_scene, recall_tags, recall_embedding,
                source_type, producer_path, maintained_by
            ) values (
                v_content, coalesce(v_title, v_target.title), v_target.tags,
                v_target.heat, v_importance, v_embedding,
                'rumination', 'verified', true, 0,
                v_run.assistant_id, v_run.id,
                v_evidence[1], v_evidence[cardinality(v_evidence)],
                v_confidence, v_content_hash,
                v_memory_key, v_target.id,
                v_target.continuity_id, 1, v_continuity_data,
                'thread', v_thread_state,
                v_memory_time, v_time_precision,
                (select coalesce(array_agg(distinct id order by id), '{}'::bigint[])
                 from unnest(v_target.evidence_message_ids || v_evidence) as id),
                least(coalesce(v_target.evidence_start_time, v_ev_start), v_ev_start),
                greatest(coalesce(v_target.evidence_end_time, v_ev_end), v_ev_end),
                v_ev_precision,
                coalesce(v_recall_scene, v_target.recall_scene),
                coalesce(v_recall_tags, v_target.recall_tags, '{}'::text[]),
                coalesce(v_recall_embedding, v_target.recall_embedding),
                coalesce(v_source_type, v_target.source_type),
                'rumination', 'rumination'
            )
            returning id into v_new_memory_id;

            update public.memories
            set superseded_by_memory_id = v_new_memory_id
            where id = v_target.id;

            -- Takeover bookkeeping: the acted-on fast-path thread is now
            -- rumination-maintained; the audit row references both versions.
            if v_target.maintained_by = 'fast_path' then
                insert into public.memory_path_handoffs (
                    assistant_id, run_id, kind,
                    fast_path_memory_id, rumination_memory_id,
                    memory_key, continuity_id, note
                ) values (
                    v_run.assistant_id, v_run.id, 'adopt_thread',
                    v_target.id, v_new_memory_id,
                    v_memory_key, v_target.continuity_id,
                    left(coalesce(v_reason, 'thread op takeover'), 500)
                );
                v_counts := jsonb_set(v_counts, '{adopted_threads}',
                    ((v_counts->>'adopted_threads')::integer + 1)::text::jsonb);
            end if;

            v_counts := jsonb_set(
                v_counts,
                (case v_op_type
                    when 'update_thread' then '{updated_versions}'
                    when 'pause_thread' then '{paused}'
                    when 'resume_thread' then '{resumed}'
                    else '{resolved}'
                end)::text[],
                (
                    coalesce((
                        case v_op_type
                            when 'update_thread' then v_counts->>'updated_versions'
                            when 'pause_thread' then v_counts->>'paused'
                            when 'resume_thread' then v_counts->>'resumed'
                            else v_counts->>'resolved'
                        end
                    )::integer, 0) + 1
                )::text::jsonb
            );
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', v_op_type,
                'commit_status', 'version_created',
                'memory_id', v_new_memory_id,
                'previous_memory_id', v_target.id,
                'thread_state', v_thread_state,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        if v_op_type = 'create_request' then
            -- episode / profile / interaction_rule enter the review queue as
            -- rumination requests; thread is never a request class here.
            if v_continuity_type not in ('episode', 'profile', 'interaction_rule') then
                raise exception 'memory_rumination_invalid_request_type';
            end if;
            if v_thread_state is not null then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if not public.validate_continuity_data(v_continuity_type, null, v_continuity_data) then
                raise exception 'memory_rumination_invalid_continuity_data';
            end if;
            if char_length(trim(coalesce(v_op->>'reason', ''))) < 3 then
                raise exception 'memory_rumination_invalid_reason';
            end if;
            if v_embedding is null then
                raise exception 'memory_rumination_missing_embedding';
            end if;

            if v_continuity_type = 'interaction_rule' then
                v_update_mode := 'replace';
                v_memory_key := lower(trim(coalesce(v_op->>'memory_key', '')));
                if v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
                    raise exception 'memory_rumination_invalid_memory_key';
                end if;
            else
                v_update_mode := 'append';
                if nullif(trim(coalesce(v_op->>'memory_key', '')), '') is not null then
                    raise exception 'memory_rumination_invalid_memory_key';
                end if;
                v_memory_key := null;
            end if;

            -- The content is already in flight or finalized by a request from
            -- ANY lane. A pending row from another source shares the partial
            -- unique index (assistant_id, content_hash), and an approved or
            -- merged request has already produced its formal result: in both
            -- cases a second rumination request must not be created.
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.content_hash = v_content_hash
                  and request.status in ('pending', 'approved', 'merged')
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- The content already exists as a formal memory: a new request
            -- would duplicate an active truth.
            if exists (
                select 1 from public.memories as memory
                where memory.is_active = true
                  and memory.verified = 'verified'
                  and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
                  and memory.content_hash = v_content_hash
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_active_memory',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- Same-content pending rumination request already exists: never a
            -- second one.
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.source = 'rumination'
                  and request.content_hash = v_content_hash
                  and request.status = 'pending'
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- Semantically identical pending rumination request: skip.
            if exists (
                select 1 from public.memory_requests as request
                where request.assistant_id = v_run.assistant_id
                  and request.source = 'rumination'
                  and request.status = 'pending'
                  and public.memory_dedupe_text_similarity(request.content, v_content) >= 0.86
            ) then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_similar_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            -- A rejected/duplicate/conflict rumination request with the same
            -- content may only be re-filed with at least one piece of new raw
            -- evidence that postdates every evidence the prior request saw.
            -- Batch windows are strictly id-ordered, so evidence inside this
            -- batch is automatically newer; the explicit check keeps the rule
            -- enforced even if batch ordering ever changes.
            select max(evidence_id) into v_dup_max_evidence
            from public.memory_requests as request
            cross join unnest(request.evidence_message_ids) as evidence_id
            where request.assistant_id = v_run.assistant_id
              and request.source = 'rumination'
              and request.content_hash = v_content_hash
              and request.status in ('rejected', 'duplicate', 'conflict');
            if v_dup_max_evidence is not null
               and (select max(value) from unnest(v_evidence) as value) <= v_dup_max_evidence then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_without_new_evidence',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            v_idempotency_key := 'rumination-' || v_run.id::text || '-' || v_content_hash;
            select id into v_prior_request_id
            from public.memory_requests as request
            where request.assistant_id = v_run.assistant_id
              and request.idempotency_key = v_idempotency_key
            limit 1;
            if found then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'request_id', v_prior_request_id,
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            insert into public.memory_requests (
                assistant_id, conversation_id, source_message_id,
                content, title, tags, importance, reason, content_hash,
                idempotency_key, status, source, producer_path,
                memory_key, update_mode, confidence,
                evidence_message_ids, source_time, memory_time, time_precision,
                digest_run_id, embedding,
                dedupe_state, dedupe_reason,
                continuity_type, thread_state, continuity_id,
                continuity_schema_version, continuity_data,
                source_type,
                evidence_start_time, evidence_end_time, evidence_time_precision,
                recall_scene, recall_tags
            ) values (
                v_run.assistant_id, nullif(trim(v_conversation_id), ''), v_evidence[1],
                v_content, v_title, array[v_continuity_type], v_importance,
                left(trim(v_op->>'reason'), 500), v_content_hash,
                v_idempotency_key, 'pending', 'rumination', 'rumination',
                v_memory_key, v_update_mode, v_confidence,
                v_evidence, v_ev_end, v_memory_time, v_time_precision,
                v_run.id, v_embedding,
                'none', null,
                v_continuity_type, null, null,
                1, v_continuity_data,
                v_source_type,
                v_ev_start, v_ev_end, v_ev_precision,
                v_recall_scene, coalesce(v_recall_tags, '{}'::text[])
            )
            on conflict (idempotency_key) do nothing
            returning id into v_prior_request_id;

            if v_prior_request_id is null then
                v_counts := jsonb_set(v_counts, '{skipped_duplicates}',
                    ((v_counts->>'skipped_duplicates')::integer + 1)::text::jsonb);
                v_preview := v_preview || jsonb_build_object(
                    'op', 'create_request', 'commit_status', 'skipped_existing_request',
                    'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
                );
                continue;
            end if;

            v_counts := jsonb_set(v_counts, '{created_requests}',
                ((v_counts->>'created_requests')::integer + 1)::text::jsonb);
            v_written := v_written + 1;
            v_preview := v_preview || jsonb_build_object(
                'op', 'create_request', 'commit_status', 'inserted_request',
                'request_id', v_prior_request_id,
                'continuity_type', v_continuity_type,
                'reason', v_reason, 'evidence_message_ids', to_jsonb(v_evidence)
            );
            continue;
        end if;

        raise exception 'memory_rumination_invalid_op';
    end loop;

    -- The batch is fully validated and applied: advance the independent
    -- cursor to the last message of this batch and close the run.
    update public.memory_rumination_cursors set
        initialized = true,
        last_processed_message_id = greatest(
            last_processed_message_id, v_run.source_last_message_id),
        status = 'ready',
        last_success_at = now(),
        last_batch_first_message_id = v_run.source_first_message_id,
        last_batch_last_message_id = v_run.source_last_message_id,
        last_batch_message_count = v_run.message_count,
        last_scheduled_date = case
            when v_run.trigger = 'rumination_scheduled'
            then (now() at time zone 'Asia/Shanghai')::date
            else last_scheduled_date
        end,
        updated_at = now()
    where assistant_id = v_run.assistant_id
    returning * into v_cursor;

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = v_op_count,
        inserted_count = v_written,
        preview_memories = v_preview,
        op_counts = v_counts,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = v_run.id;

    return jsonb_build_object(
        'run_id', v_run.id,
        'cursor', to_jsonb(v_cursor),
        'op_counts', v_counts,
        'preview', v_preview,
        'inserted_count', v_written
    );
end;
$function$;

revoke all on function public.commit_rumination_batch(bigint, jsonb)
    from public, anon, authenticated;
grant execute on function public.commit_rumination_batch(bigint, jsonb)
    to service_role;

-- ---------------------------------------------------------------------------
-- 7. Recall: closed threads leave the default hot path
-- ---------------------------------------------------------------------------

drop function if exists public.match_memories(
    extensions.vector, double precision, integer
);

create function public.match_memories(
    query_embedding extensions.vector,
    match_threshold double precision default 0.5,
    match_count integer default 20
)
returns table(
    id integer,
    content text,
    title text,
    tags text[],
    heat double precision,
    importance integer,
    created_at timestamptz,
    last_recalled_at timestamptz,
    similarity double precision,
    continuity_id uuid,
    continuity_type text,
    continuity_schema_version smallint,
    continuity_data jsonb,
    source_type text,
    thread_state text,
    memory_time timestamptz,
    evidence_start_time timestamptz,
    evidence_end_time timestamptz,
    source_time timestamptz,
    time_precision text,
    evidence_time_precision text,
    recall_scene text,
    recall_tags text[]
)
language sql
stable
set search_path to 'public', 'extensions'
as $function$
    select
        memory.id,
        memory.content,
        memory.title,
        memory.tags,
        memory.heat,
        memory.importance,
        memory.created_at,
        memory.last_recalled_at,
        1 - (memory.recall_embedding <=> query_embedding) as similarity,
        memory.continuity_id,
        memory.continuity_type,
        memory.continuity_schema_version,
        memory.continuity_data,
        memory.source_type,
        memory.thread_state,
        memory.memory_time,
        memory.evidence_start_time,
        memory.evidence_end_time,
        memory.source_time,
        memory.time_precision,
        memory.evidence_time_precision,
        memory.recall_scene,
        memory.recall_tags
    from public.memories as memory
    where memory.is_active = true
      and memory.verified = 'verified'
      and memory.recall_embedding is not null
      and not (
          memory.continuity_type = 'thread'
          and memory.thread_state in ('resolved', 'dissolved', 'abandoned')
      )
      and 1 - (memory.recall_embedding <=> query_embedding)
          > least(greatest(coalesce(match_threshold, 0.5), 0.0), 1.0)
    order by memory.recall_embedding <=> query_embedding
    limit least(greatest(coalesce(match_count, 20), 1), 50);
$function$;

revoke all on function public.match_memories(
    extensions.vector, double precision, integer
) from public, anon, authenticated;

grant execute on function public.match_memories(
    extensions.vector, double precision, integer
) to service_role;

drop function if exists public.search_memories_by_keywords(text[], integer);

create function public.search_memories_by_keywords(
    search_keywords text[],
    result_limit integer default 20
)
returns table(
    id integer,
    content text,
    title text,
    tags text[],
    heat double precision,
    importance integer,
    created_at timestamptz,
    last_recalled_at timestamptz,
    continuity_id uuid,
    continuity_type text,
    continuity_schema_version smallint,
    continuity_data jsonb,
    source_type text,
    thread_state text,
    memory_time timestamptz,
    evidence_start_time timestamptz,
    evidence_end_time timestamptz,
    source_time timestamptz,
    evidence_time_precision text
)
language sql
stable
set search_path to 'public'
as $function$
    with bounded_keywords as (
        select distinct left(btrim(input.keyword),64) as keyword
        from unnest(coalesce(search_keywords,'{}'::text[]))
            with ordinality as input(keyword,position)
        where input.position <= 5
          and char_length(btrim(input.keyword)) between 1 and 64
    )
    select
        memory.id,memory.content,memory.title,memory.tags,memory.heat,memory.importance,
        memory.created_at,memory.last_recalled_at,memory.continuity_id,
        memory.continuity_type,memory.continuity_schema_version,memory.continuity_data,
        memory.source_type,memory.thread_state,memory.memory_time,
        memory.evidence_start_time,memory.evidence_end_time,memory.source_time,
        memory.evidence_time_precision
    from public.memories as memory
    cross join lateral (
        select count(*) as keyword_matches
        from bounded_keywords as candidate
        where position(lower(candidate.keyword) in lower(coalesce(memory.content,''))) > 0
           or position(lower(candidate.keyword) in lower(coalesce(memory.title,''))) > 0
           or exists (
                select 1
                from unnest(coalesce(memory.tags,'{}'::text[])) as tag(value)
                where position(lower(candidate.keyword) in lower(tag.value)) > 0
           )
    ) as relevance
    where memory.is_active = true
      and memory.verified = 'verified'
      and not (
          memory.continuity_type = 'thread'
          and memory.thread_state in ('resolved', 'dissolved', 'abandoned')
      )
      and relevance.keyword_matches > 0
    order by relevance.keyword_matches desc, memory.created_at desc
    limit least(greatest(coalesce(result_limit,20),1),50);
$function$;

revoke all on function public.search_memories_by_keywords(text[], integer) from public, anon, authenticated;

grant execute on function public.search_memories_by_keywords(text[], integer) to service_role;

-- ---------------------------------------------------------------------------
-- 8. Fast-path write gating (no prompt or classification change)
-- ---------------------------------------------------------------------------

-- Rebuild store_continuity_candidate verbatim from its final implementation
-- with one added gate: a daily_digest thread candidate that clearly matches an
-- active rumination-maintained thread is no longer auto-approved; it stays
-- pending for the human, and its evidence remains available to the rumination
-- lane through the raw chat log.
create or replace function public.store_continuity_candidate(
    p_run public.memory_digest_runs,
    p_item jsonb
)
returns integer
language plpgsql
security definer
set search_path to 'public','extensions'
as $function$
declare
    v_ids bigint[];
    v_source bigint;
    v_conversation text;
    v_delta integer;
    v_mode text;
    v_key text;
    v_type text;
    v_requested_evidence_count integer;
    v_content text;
    v_content_hash text;
    v_embedding extensions.vector;
    v_recall_scene text;
    v_recall_tags text[];
    v_recall_embedding extensions.vector;
    v_evidence_precision text;
    v_related_request bigint;
    v_related_memory integer;
    v_dedupe text := 'none';
    v_dedupe_reason text;
    v_request_id bigint;
    v_review jsonb;
    v_rumination_thread_conflict boolean := false;
begin
    v_type := p_item->>'continuity_type';
    if v_type not in ('moment','thread','episode','inside_joke') then
        raise exception 'memory_digest_invalid_continuity_type';
    end if;
    if coalesce((p_item->>'continuity_schema_version')::integer,0) <> 1
       or not public.validate_continuity_data(v_type,p_item->>'thread_state',p_item->'continuity_data') then
        raise exception 'memory_digest_invalid_continuity_data';
    end if;
    if nullif(p_item->>'embedding','') is null then
        raise exception 'memory_digest_missing_embedding';
    end if;
    v_content := left(trim(coalesce(p_item->>'content','')),600);
    v_content_hash := lower(trim(coalesce(p_item->>'content_hash','')));
    if char_length(v_content) < 5 or v_content_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'memory_digest_invalid_content';
    end if;
    v_embedding := (p_item->>'embedding')::extensions.vector;
    v_recall_scene := nullif(btrim(coalesce(p_item->>'recall_scene','')),'');
    v_recall_tags := case
        when p_item ? 'recall_tags'
             and jsonb_typeof(p_item->'recall_tags') = 'array'
             and jsonb_array_length(p_item->'recall_tags') > 0
        then array(
            select t.value
            from jsonb_array_elements_text(p_item->'recall_tags') as t(value)
            where nullif(btrim(t.value),'') is not null
        )
        else null
    end;
    -- A recall embedding may only exist when the scene it was derived from is
    -- present; the gateway computes it from recall_scene alone.
    v_recall_embedding := case
        when v_recall_scene is null then null
        else (p_item->>'recall_embedding')::extensions.vector
    end;
    -- Evidence precision describes the evidence clock itself, never the model
    -- output's memory_time precision; the gateway only emits values that are
    -- actually supported by the evidence messages.
    v_evidence_precision := case
        when p_item->>'evidence_time_precision' in ('minute','hour','day','approximate','unknown')
        then p_item->>'evidence_time_precision'
        else null
    end;

    select count(distinct value::bigint)
    into v_requested_evidence_count
    from jsonb_array_elements_text(coalesce(p_item->'evidence_message_ids','[]'::jsonb))
    where value ~ '^[0-9]+$';
    if v_requested_evidence_count not between 1 and 8
       or jsonb_array_length(coalesce(p_item->'evidence_message_ids','[]'::jsonb)) <> v_requested_evidence_count then
        raise exception 'memory_digest_invalid_evidence';
    end if;

    select coalesce(array_agg(message.id order by message.id),'{}'::bigint[])
    into v_ids
    from public.chat_messages as message
    join (
        select distinct value::bigint as id
        from jsonb_array_elements_text(coalesce(p_item->'evidence_message_ids','[]'::jsonb))
        where value ~ '^[0-9]+$'
    ) as evidence on evidence.id = message.id
    where message.assistant_id = p_run.assistant_id
      and message.id between p_run.source_first_message_id and p_run.source_last_message_id;
    if cardinality(v_ids) <> v_requested_evidence_count then
        raise exception 'memory_digest_invalid_evidence';
    end if;

    v_source := v_ids[1];
    select conversation_id into v_conversation
    from public.chat_messages
    where id = v_source;
    v_mode := case when p_item->>'update_mode' = 'replace' then 'replace' else 'append' end;
    v_key := case when v_mode = 'replace' then nullif(p_item->>'memory_key','') else null end;

    select id into v_related_request
    from public.memory_requests
    where assistant_id = p_run.assistant_id
      and status in ('pending','approved','merged','duplicate','conflict','rejected')
      and (
        content_hash = v_content_hash
        or (
            v_key is not null
            and memory_key = v_key
            and created_at >= coalesce(nullif(p_item->>'source_time','')::timestamptz,p_run.started_at,now()) - interval '15 minutes'
        )
        or (
            public.memory_dedupe_text_similarity(content,v_content) >= .72
            and (
                source_message_id = any(v_ids)
                or (
                    nullif(trim(v_conversation),'') is not null
                    and conversation_id = nullif(trim(v_conversation),'')
                    and created_at between coalesce(nullif(p_item->>'source_time','')::timestamptz,p_run.started_at,now()) - interval '15 minutes'
                        and now() + interval '1 minute'
                )
            )
        )
      )
    order by (content_hash = v_content_hash) desc, created_at desc, id desc
    limit 1;
    if found then
        return 0;
    end if;

    -- The retired goal classifier was the only semantic basis for todo
    -- suppression. A continuity thread is not necessarily a todo.
    select id into v_related_memory
    from public.memories
    where is_active = true
      and verified = 'verified'
      and (assistant_id = p_run.assistant_id or assistant_id is null)
      and content_hash = v_content_hash
    order by id desc
    limit 1;
    if found then
        return 0;
    end if;

    v_related_request := null;
    v_related_memory := null;
    select id into v_related_request
    from public.memory_requests
    where assistant_id = p_run.assistant_id
      and status in ('pending','approved','merged')
      and not (v_mode = 'replace' and v_key is not null and memory_key = v_key)
      and (
        public.memory_dedupe_text_similarity(content,v_content) >= .86
        or (
            embedding is not null
            and 1 - (embedding <=> v_embedding) >= .94
            and public.memory_dedupe_text_similarity(content,v_content) >= .38
        )
      )
    order by created_at desc
    limit 1;
    if v_related_request is not null then
        v_dedupe := 'possible_duplicate';
        v_dedupe_reason := 'similar_to_existing_request';
    else
        select id into v_related_memory
        from public.memories
        where is_active = true
          and verified = 'verified'
          and (assistant_id = p_run.assistant_id or assistant_id is null)
          and not (v_mode = 'replace' and v_key is not null and memory_key = v_key)
          and (
            public.memory_dedupe_text_similarity(content,v_content) >= .86
            or (
                embedding is not null
                and 1 - (embedding <=> v_embedding) >= .94
                and public.memory_dedupe_text_similarity(content,v_content) >= .38
            )
          )
        order by id desc
        limit 1;
        if v_related_memory is not null then
            v_dedupe := 'possible_duplicate';
            v_dedupe_reason := 'similar_to_active_memory';
        end if;
    end if;

    insert into public.memory_requests(
        assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,content_hash,idempotency_key,
        status,source,memory_key,update_mode,confidence,evidence_message_ids,source_time,memory_time,time_precision,digest_run_id,embedding,
        dedupe_state,dedupe_reason,related_request_id,related_memory_id,
        continuity_type,source_type,thread_state,evidence_start_time,evidence_end_time,
        evidence_time_precision,
        continuity_id,continuity_schema_version,continuity_data,
        recall_scene,recall_tags
    ) values (
        p_run.assistant_id,v_conversation,v_source,v_content,nullif(left(trim(coalesce(p_item->>'title','')),100),''),
        array[v_type],least(greatest(coalesce((p_item->>'importance')::integer,5),1),10),'自动总结提取，等待用户审核',
        v_content_hash,'continuity-' || p_run.id || '-' || v_content_hash,'pending','daily_digest',v_key,v_mode,
        least(greatest(coalesce((p_item->>'confidence')::double precision,0.6),0),1),v_ids,
        nullif(p_item->>'source_time','')::timestamptz,
        case
            when p_item->>'time_precision' = 'day' and p_item->>'memory_time' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
            then (p_item->>'memory_time')::date::timestamp at time zone 'Asia/Shanghai'
            else nullif(p_item->>'memory_time','')::timestamptz
        end,
        coalesce(p_item->>'time_precision','unknown'),p_run.id,v_embedding,v_dedupe,v_dedupe_reason,v_related_request,v_related_memory,
        v_type,
        nullif(btrim(coalesce(p_item->>'source_type','')),''),
        p_item->>'thread_state',
        nullif(p_item->>'evidence_start_time','')::timestamptz,nullif(p_item->>'evidence_end_time','')::timestamptz,
        v_evidence_precision,
        null,1,p_item->'continuity_data',
        v_recall_scene,v_recall_tags
    )
    on conflict do nothing
    returning id into v_request_id;
    get diagnostics v_delta = row_count;
    -- 自动通过必须"场景非空且召回向量非空"；缺任一项保留 pending，
    -- 由叶子在审核表单补充召回场景后再通过。
    -- 反刍交接门控：thread 候选若与反刍长期 thread 明确同义，不自动通过，
    -- 保留 pending 由人工裁决，避免双 active 真源。
    if v_delta = 1 and v_type = 'thread' then
        select exists(
            select 1
            from public.memories as memory
            where memory.is_active = true
              and memory.verified = 'verified'
              and memory.continuity_type = 'thread'
              and memory.maintained_by = 'rumination'
              and memory.thread_state in ('open','paused')
              and (memory.assistant_id = p_run.assistant_id or memory.assistant_id is null)
              and public.memory_dedupe_text_similarity(memory.content, v_content) >= .86
        ) into v_rumination_thread_conflict;
    end if;
    if v_delta = 1 and v_type in ('moment','thread','inside_joke')
       and not v_rumination_thread_conflict
       and v_recall_scene is not null
       and v_recall_embedding is not null then
        v_review := public.review_memory_request_v5(
            v_request_id,'approve',v_content,nullif(left(trim(coalesce(p_item->>'title','')),100),''),
            array[v_type],least(greatest(coalesce((p_item->>'importance')::integer,5),1),10),v_content_hash,
            'daily_digest_ai','automatic low-risk continuity memory',v_key,v_mode,null,
            v_recall_embedding,v_recall_scene,v_recall_tags,
            nullif(p_item->>'evidence_end_time','')::timestamptz,v_evidence_precision
        );
    end if;
    return v_delta;
end;
$function$;

-- Rebuild write_memory_direct_v1 verbatim from its final implementation with
-- two added thread gates: a direct thread append that clearly matches an
-- active rumination-maintained thread is refused (evidence stays with the raw
-- log for the rumination lane), and a replace whose target key is maintained
-- by rumination is refused (the fast path must not rewrite rumination state).
-- Pre-retirement signature (20260830010000, 27 params):
drop function if exists public.write_memory_direct_v1(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text, text, text, smallint, jsonb, text, text,
    integer, text, text[], text, text, text[], extensions.vector
);
-- Post-retirement signature (20260831010000, 23 params):
drop function if exists public.write_memory_direct_v1(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text, text, text, smallint, jsonb, text, text,
    text, text, text[], extensions.vector
);

create or replace function public.write_memory_direct_v1(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_source_type text, p_source text,
    p_reviewed_by text, p_recall_scene text, p_recall_tags text[],
    p_recall_embedding extensions.vector
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_created jsonb;
    v_request jsonb;
    v_review jsonb;
    v_had_previous boolean := false;
begin
    if p_continuity_type not in ('moment','thread','inside_joke') then
        raise exception 'memory_request_type_requires_user_review';
    end if;
    if p_continuity_type = 'thread' then
        if p_update_mode = 'append' and exists (
            select 1
            from public.memories as memory
            where memory.is_active = true
              and memory.verified = 'verified'
              and memory.continuity_type = 'thread'
              and memory.maintained_by = 'rumination'
              and memory.thread_state in ('open','paused')
              and (memory.assistant_id = p_assistant_id or memory.assistant_id is null)
              and public.memory_dedupe_text_similarity(memory.content, p_content) >= .86
        ) then
            raise exception 'memory_thread_rumination_conflict';
        end if;
        if p_update_mode = 'replace' and exists (
            select 1
            from public.memories as memory
            where memory.is_active = true
              and memory.verified = 'verified'
              and memory.memory_key = p_memory_key
              and memory.maintained_by = 'rumination'
        ) then
            raise exception 'memory_thread_rumination_maintained';
        end if;
    end if;
    if p_update_mode = 'replace' then
        select exists(
            select 1 from public.memories
            where assistant_id = p_assistant_id
              and memory_key = p_memory_key
              and verified = 'verified'
              and is_active = true
        ) into v_had_previous;
    end if;
    v_created := public.create_memory_request_v4(
        p_assistant_id,p_conversation_id,p_source_message_id,p_content,p_title,p_tags,p_importance,p_reason,
        p_content_hash,p_idempotency_key,p_rate_limit,p_memory_key,p_update_mode,p_continuity_type,p_thread_state,
        p_continuity_schema_version,p_continuity_data,p_source_type,p_source,p_recall_scene,p_recall_tags
    );
    v_request := v_created->'request';
    if v_request->>'continuity_type' is distinct from p_continuity_type then
        raise exception 'memory_request_idempotency_conflict';
    end if;
    v_review := public.review_memory_request_v5(
        (v_request->>'id')::bigint,'approve',p_content,p_title,p_tags,p_importance,p_content_hash,
        left(coalesce(nullif(trim(p_reviewed_by),''),'orangechat_ai'),120),null,p_memory_key,p_update_mode,null,
        p_recall_embedding,p_recall_scene,p_recall_tags,
        nullif(v_request->>'evidence_end_time','')::timestamptz,
        nullif(v_request->>'evidence_time_precision','')
    );
    select to_jsonb(request_row) into v_request
    from public.memory_requests as request_row
    where request_row.id = (v_request->>'id')::bigint;
    return jsonb_set(v_review,'{request}',v_request) || jsonb_build_object(
        'created',coalesce((v_created->>'created')::boolean,false) and not v_had_previous,
        'updated',v_had_previous and coalesce((v_review->>'changed')::boolean,false)
    );
end;
$function$;

commit;
