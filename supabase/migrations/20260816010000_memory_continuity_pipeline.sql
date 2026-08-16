-- Promote continuity extraction to a reviewed, cursor-driven digest pipeline.
-- public.chat_messages remains immutable evidence: this migration only selects it.

create table if not exists public.memory_continuity_cursors (
    assistant_id text primary key,
    last_processed_message_id bigint not null default 177,
    status text not null default 'ready',
    last_success_at timestamptz,
    manual_cooldown_until timestamptz,
    auto_cooldown_until timestamptz,
    blocked_first_message_id bigint,
    blocked_last_message_id bigint,
    blocked_message_count integer,
    blocked_at timestamptz,
    pause_reason text,
    updated_at timestamptz not null default now(),
    constraint memory_continuity_cursors_position_check
        check (last_processed_message_id >= 0),
    constraint memory_continuity_cursors_status_check
        check (status in ('ready', 'paused_empty')),
    constraint memory_continuity_cursors_pause_reason_check
        check (pause_reason is null or pause_reason = 'empty_candidates'),
    constraint memory_continuity_cursors_blocked_state_check
        check (
            (
                status = 'ready'
                and blocked_first_message_id is null
                and blocked_last_message_id is null
                and blocked_message_count is null
                and blocked_at is null
                and pause_reason is null
            )
            or (
                status = 'paused_empty'
                and blocked_first_message_id is not null
                and blocked_last_message_id is not null
                and blocked_last_message_id >= blocked_first_message_id
                and blocked_message_count > 0
                and blocked_at is not null
                and pause_reason = 'empty_candidates'
            )
        )
);

alter table public.memory_continuity_cursors enable row level security;

comment on table public.memory_continuity_cursors is
    'Independent cursor and pause/cooldown state for the reviewed continuity digest pipeline.';
comment on column public.memory_continuity_cursors.last_processed_message_id is
    'Last raw chat message included in a successfully committed or explicitly skipped continuity batch.';

alter table public.memory_requests
    add column if not exists continuity_type text,
    add column if not exists subject text,
    add column if not exists source_type text,
    add column if not exists thread_state text,
    add column if not exists continuity_value integer,
    add column if not exists retention_class text,
    add column if not exists participants text[],
    add column if not exists evidence_start_time timestamptz,
    add column if not exists evidence_end_time timestamptz;

alter table public.memories
    add column if not exists continuity_type text,
    add column if not exists subject text,
    add column if not exists source_type text,
    add column if not exists thread_state text,
    add column if not exists continuity_value integer,
    add column if not exists retention_class text,
    add column if not exists participants text[],
    add column if not exists evidence_start_time timestamptz,
    add column if not exists evidence_end_time timestamptz;

alter table public.memory_requests
    drop constraint if exists memory_requests_continuity_type_check,
    drop constraint if exists memory_requests_continuity_subject_check,
    drop constraint if exists memory_requests_continuity_source_type_check,
    drop constraint if exists memory_requests_continuity_thread_state_check,
    drop constraint if exists memory_requests_continuity_value_check,
    drop constraint if exists memory_requests_retention_class_check,
    drop constraint if exists memory_requests_continuity_participants_check;

alter table public.memory_requests
    add constraint memory_requests_continuity_type_check
        check (continuity_type is null or continuity_type in (
            'moment', 'thread', 'episode', 'inside_joke', 'relationship', 'profile'
        )),
    add constraint memory_requests_continuity_subject_check
        check (subject is null or subject in ('yezi', 'qi', 'shared', 'project', 'other')),
    add constraint memory_requests_continuity_source_type_check
        check (source_type is null or source_type in (
            'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
            'roleplay', 'tool_result', 'system_meta', 'unknown'
        )),
    add constraint memory_requests_continuity_thread_state_check
        check (
            (continuity_type = 'thread' and thread_state in ('open', 'paused', 'resolved', 'abandoned', 'unknown'))
            or (continuity_type is distinct from 'thread' and thread_state is null)
        ),
    add constraint memory_requests_continuity_value_check
        check (continuity_value is null or continuity_value between 1 and 10),
    add constraint memory_requests_retention_class_check
        check (retention_class is null or retention_class in ('normal', 'core')),
    add constraint memory_requests_continuity_participants_check
        check (
            participants is null
            or participants <@ array['yezi', 'qi', 'other']::text[]
        );

alter table public.memories
    drop constraint if exists memories_continuity_type_check,
    drop constraint if exists memories_continuity_subject_check,
    drop constraint if exists memories_continuity_source_type_check,
    drop constraint if exists memories_continuity_thread_state_check,
    drop constraint if exists memories_continuity_value_check,
    drop constraint if exists memories_retention_class_check,
    drop constraint if exists memories_continuity_participants_check;

alter table public.memories
    add constraint memories_continuity_type_check
        check (continuity_type is null or continuity_type in (
            'moment', 'thread', 'episode', 'inside_joke', 'relationship', 'profile'
        )),
    add constraint memories_continuity_subject_check
        check (subject is null or subject in ('yezi', 'qi', 'shared', 'project', 'other')),
    add constraint memories_continuity_source_type_check
        check (source_type is null or source_type in (
            'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
            'roleplay', 'tool_result', 'system_meta', 'unknown'
        )),
    add constraint memories_continuity_thread_state_check
        check (
            (continuity_type = 'thread' and thread_state in ('open', 'paused', 'resolved', 'abandoned', 'unknown'))
            or (continuity_type is distinct from 'thread' and thread_state is null)
        ),
    add constraint memories_continuity_value_check
        check (continuity_value is null or continuity_value between 1 and 10),
    add constraint memories_retention_class_check
        check (retention_class is null or retention_class in ('normal', 'core')),
    add constraint memories_continuity_participants_check
        check (
            participants is null
            or participants <@ array['yezi', 'qi', 'other']::text[]
        );

create index if not exists memory_requests_continuity_queue_idx
    on public.memory_requests (continuity_type, status, created_at desc)
    where continuity_type is not null;

alter table public.memory_digest_runs
    add column if not exists pipeline text not null default 'legacy';

alter table public.memory_digest_runs
    drop constraint if exists memory_digest_runs_pipeline_check;

alter table public.memory_digest_runs
    add constraint memory_digest_runs_pipeline_check
        check (pipeline in ('legacy', 'continuity'));

-- Older environments used either an explicit name or PostgreSQL's generated
-- column check name. Replace only trigger checks on memory_digest_runs.
do $block$
declare
    v_constraint text;
begin
    for v_constraint in
        select con.conname
        from pg_constraint as con
        where con.conrelid = 'public.memory_digest_runs'::regclass
          and con.contype = 'c'
          and pg_get_constraintdef(con.oid) ~* '\mtrigger\M'
    loop
        execute format(
            'alter table public.memory_digest_runs drop constraint %I',
            v_constraint
        );
    end loop;
end;
$block$;

alter table public.memory_digest_runs
    add constraint memory_digest_runs_trigger_check
        check (trigger in (
            'manual_preview', 'manual_execute', 'scheduled_daily', 'idle_six_hours',
            'continuity_threshold', 'continuity_manual', 'continuity_retry', 'continuity_skip'
        ));

create index if not exists memory_digest_runs_pipeline_started_idx
    on public.memory_digest_runs (pipeline, assistant_id, started_at desc);

-- Keep the existing assistant-wide claim/heartbeat lease. Trigger naming
-- determines pipeline without changing the legacy three-argument RPC contract.
create or replace function public.claim_digest_slot(
    p_assistant_id text,
    p_trigger text,
    p_mode text
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_run_id bigint;
    v_stale_cutoff timestamptz := now() - interval '30 minutes';
    v_pipeline text := case
        when p_trigger like 'continuity_%' then 'continuity'
        else 'legacy'
    end;
begin
    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 1));

    if p_trigger = 'continuity_retry' and not exists (
        select 1 from public.memory_continuity_cursors
        where assistant_id = p_assistant_id and status = 'paused_empty'
    ) then
        return jsonb_build_object('status', 'not_paused');
    end if;
    if p_trigger in ('continuity_manual', 'continuity_threshold') and exists (
        select 1 from public.memory_continuity_cursors
        where assistant_id = p_assistant_id and status = 'paused_empty'
    ) then
        return jsonb_build_object('status', 'paused_empty');
    end if;

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

    insert into public.memory_digest_runs (
        assistant_id, pipeline, trigger, mode, status,
        claimed_at, heartbeat_at, started_at
    ) values (
        p_assistant_id, v_pipeline, p_trigger, p_mode, 'claimed',
        now(), now(), now()
    ) returning id into v_run_id;

    return jsonb_build_object('status', 'claimed', 'run_id', v_run_id);
end;
$function$;

revoke all on function public.claim_digest_slot(text, text, text)
    from public, anon, authenticated;
grant execute on function public.claim_digest_slot(text, text, text)
    to service_role;

create or replace function public.get_or_create_memory_continuity_cursor(
    p_assistant_id text
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_cursor public.memory_continuity_cursors%rowtype;
begin
    if nullif(trim(coalesce(p_assistant_id, '')), '') is null then
        raise exception 'memory_continuity_invalid_assistant';
    end if;

    insert into public.memory_continuity_cursors (
        assistant_id, last_processed_message_id
    ) values (
        p_assistant_id, 177
    ) on conflict (assistant_id) do nothing;

    select * into v_cursor
    from public.memory_continuity_cursors
    where assistant_id = p_assistant_id;

    return to_jsonb(v_cursor);
end;
$function$;

revoke all on function public.get_or_create_memory_continuity_cursor(text)
    from public, anon, authenticated;
grant execute on function public.get_or_create_memory_continuity_cursor(text)
    to service_role;

create or replace function public.pause_memory_continuity_empty(
    p_run_id bigint
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_cursor public.memory_continuity_cursors%rowtype;
begin
    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;

    if not found or v_run.pipeline <> 'continuity'
       or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_continuity_invalid_run';
    end if;
    if v_run.source_first_message_id is null
       or v_run.source_last_message_id is null
       or coalesce(v_run.message_count, 0) < 1 then
        raise exception 'memory_continuity_missing_batch';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id, 0));

    insert into public.memory_continuity_cursors (
        assistant_id, last_processed_message_id
    ) values (
        v_run.assistant_id, 177
    ) on conflict (assistant_id) do nothing;

    select * into v_cursor
    from public.memory_continuity_cursors
    where assistant_id = v_run.assistant_id
    for update;

    update public.memory_continuity_cursors set
        status = 'paused_empty',
        manual_cooldown_until = case
            when v_run.trigger in ('continuity_manual', 'continuity_retry')
            then now() + interval '10 seconds'
            else manual_cooldown_until
        end,
        blocked_first_message_id = v_run.source_first_message_id,
        blocked_last_message_id = v_run.source_last_message_id,
        blocked_message_count = v_run.message_count,
        blocked_at = now(),
        pause_reason = 'empty_candidates',
        updated_at = now()
    where assistant_id = v_run.assistant_id
    returning * into v_cursor;

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = 0,
        inserted_count = 0,
        preview_memories = '[]'::jsonb,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = v_run.id;

    return to_jsonb(v_cursor);
end;
$function$;

revoke all on function public.pause_memory_continuity_empty(bigint)
    from public, anon, authenticated;
grant execute on function public.pause_memory_continuity_empty(bigint)
    to service_role;

create or replace function public.commit_memory_continuity_run(
    p_run_id bigint,
    p_candidates jsonb default '[]'::jsonb
)
returns integer
language plpgsql
set search_path to 'public', 'extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_cursor public.memory_continuity_cursors%rowtype;
    v_item jsonb;
    v_preview jsonb := '[]'::jsonb;
    v_inserted integer := 0;
    v_delta integer := 0;
    v_evidence_ids bigint[];
    v_requested_evidence_count integer;
    v_source_message_id bigint;
    v_conversation_id text;
    v_content text;
    v_content_hash text;
    v_continuity_type text;
    v_memory_type text;
    v_thread_state text;
    v_time_precision text;
    v_source_time timestamptz;
    v_memory_time timestamptz;
    v_embedding extensions.vector;
    v_related_request_id bigint;
    v_related_memory_id integer;
    v_dedupe_state text;
    v_dedupe_reason text;
begin
    if jsonb_typeof(coalesce(p_candidates, '[]'::jsonb)) <> 'array' then
        raise exception 'memory_continuity_candidates_not_array';
    end if;
    if jsonb_array_length(coalesce(p_candidates, '[]'::jsonb)) = 0 then
        raise exception 'memory_continuity_empty_requires_pause';
    end if;

    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;

    if not found or v_run.pipeline <> 'continuity'
       or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_continuity_invalid_run';
    end if;
    if v_run.source_first_message_id is null or v_run.source_last_message_id is null then
        raise exception 'memory_continuity_missing_batch';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id, 0));

    insert into public.memory_continuity_cursors (
        assistant_id, last_processed_message_id
    ) values (
        v_run.assistant_id, 177
    ) on conflict (assistant_id) do nothing;

    select * into v_cursor
    from public.memory_continuity_cursors
    where assistant_id = v_run.assistant_id
    for update;

    for v_item in
        select value from jsonb_array_elements(p_candidates)
    loop
        select count(distinct value::bigint)
        into v_requested_evidence_count
        from jsonb_array_elements_text(
            coalesce(v_item->'evidence_message_ids', '[]'::jsonb)
        )
        where value ~ '^[0-9]+$';

        if v_requested_evidence_count not between 1 and 8
           or jsonb_array_length(coalesce(v_item->'evidence_message_ids', '[]'::jsonb))
              <> v_requested_evidence_count then
            raise exception 'memory_continuity_invalid_evidence';
        end if;

        select coalesce(array_agg(message.id order by message.id), '{}'::bigint[])
        into v_evidence_ids
        from public.chat_messages as message
        join (
            select distinct value::bigint as id
            from jsonb_array_elements_text(
                coalesce(v_item->'evidence_message_ids', '[]'::jsonb)
            )
            where value ~ '^[0-9]+$'
        ) as evidence on evidence.id = message.id
        where message.assistant_id = v_run.assistant_id
          and message.id between v_run.source_first_message_id and v_run.source_last_message_id;

        if cardinality(v_evidence_ids) = 0
           or cardinality(v_evidence_ids) <> v_requested_evidence_count then
            raise exception 'memory_continuity_invalid_evidence';
        end if;

        v_source_message_id := v_evidence_ids[1];
        select message.conversation_id into v_conversation_id
        from public.chat_messages as message
        where message.id = v_source_message_id;

        v_content := left(trim(coalesce(v_item->>'content', '')), 600);
        v_content_hash := lower(trim(coalesce(v_item->>'content_hash', '')));
        if char_length(v_content) < 1 or v_content_hash !~ '^[0-9a-f]{64}$' then
            raise exception 'memory_continuity_invalid_content';
        end if;

        v_continuity_type := v_item->>'continuity_type';
        if v_continuity_type not in (
            'moment', 'thread', 'episode', 'inside_joke', 'relationship', 'profile'
        ) then
            raise exception 'memory_continuity_invalid_type';
        end if;
        if v_item->>'subject' not in ('yezi', 'qi', 'shared', 'project', 'other')
           or v_item->>'source_type' not in (
               'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
               'roleplay', 'tool_result', 'system_meta', 'unknown'
           ) then
            raise exception 'memory_continuity_invalid_source';
        end if;
        v_memory_type := case
            when v_continuity_type = 'profile' then 'profile'
            when v_continuity_type = 'relationship' then 'relationship'
            else 'other'
        end;
        v_thread_state := case
            when v_continuity_type = 'thread'
             and v_item->>'thread_state' in ('open', 'paused', 'resolved', 'abandoned', 'unknown')
            then v_item->>'thread_state'
            when v_continuity_type = 'thread' then 'unknown'
            else null
        end;
        v_time_precision := case
            when v_item->>'time_precision' in ('minute', 'day', 'approximate', 'unknown')
            then v_item->>'time_precision'
            else 'unknown'
        end;
        v_source_time := nullif(v_item->>'source_time', '')::timestamptz;
        v_memory_time := case
            when nullif(v_item->>'memory_time', '') is null then null
            when v_time_precision = 'day'
                 and (v_item->>'memory_time') ~ '^\d{4}-\d{2}-\d{2}$'
            then ((v_item->>'memory_time')::date::timestamp at time zone 'Asia/Shanghai')
            else (v_item->>'memory_time')::timestamptz
        end;
        v_embedding := case
            when v_item ? 'embedding' and v_item->'embedding' <> 'null'::jsonb
            then (v_item->>'embedding')::extensions.vector
            else null
        end;
        if v_embedding is null then
            raise exception 'memory_continuity_missing_embedding';
        end if;

        v_related_request_id := null;
        v_related_memory_id := null;
        v_dedupe_state := 'none';
        v_dedupe_reason := null;

        select request.id into v_related_request_id
        from public.memory_requests as request
        where request.assistant_id = v_run.assistant_id
          and request.content_hash = v_content_hash
          and request.status in ('pending', 'approved', 'merged', 'duplicate', 'conflict', 'rejected')
        order by request.id desc
        limit 1;

        if found then
            v_preview := v_preview || jsonb_build_array(
                (v_item - 'embedding' - 'content_hash') || jsonb_build_object(
                    'commit_status', 'skipped_existing_request',
                    'dedupe_reason', 'same_content_in_existing_request',
                    'related_request_id', v_related_request_id
                )
            );
            continue;
        end if;

        select memory.id into v_related_memory_id
        from public.memories as memory
        where memory.is_active = true
          and memory.verified = 'verified'
          and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
          and memory.content_hash = v_content_hash
        order by memory.id desc
        limit 1;

        if found then
            v_preview := v_preview || jsonb_build_array(
                (v_item - 'embedding' - 'content_hash') || jsonb_build_object(
                    'commit_status', 'skipped_active_memory',
                    'dedupe_reason', 'same_content_in_active_memory',
                    'related_memory_id', v_related_memory_id
                )
            );
            continue;
        end if;

        select request.id into v_related_request_id
        from public.memory_requests as request
        where request.assistant_id = v_run.assistant_id
          and request.status in ('pending', 'approved', 'merged')
          and public.memory_dedupe_text_similarity(request.content, v_content) >= 0.86
        order by request.created_at desc, request.id desc
        limit 1;

        if found then
            v_dedupe_state := 'possible_duplicate';
            v_dedupe_reason := 'similar_to_existing_request';
        else
            v_related_request_id := null;
            select memory.id into v_related_memory_id
            from public.memories as memory
            where memory.is_active = true
              and memory.verified = 'verified'
              and (memory.assistant_id = v_run.assistant_id or memory.assistant_id is null)
              and public.memory_dedupe_text_similarity(memory.content, v_content) >= 0.86
            order by memory.id desc
            limit 1;

            if found then
                v_dedupe_state := 'possible_duplicate';
                v_dedupe_reason := 'similar_to_active_memory';
            end if;
        end if;

        insert into public.memory_requests (
            assistant_id, conversation_id, source_message_id,
            content, title, tags, importance, reason, content_hash,
            idempotency_key, status, source, memory_key, update_mode,
            memory_type, confidence, evidence_message_ids, source_time,
            memory_time, time_precision, digest_run_id, embedding,
            dedupe_state, dedupe_reason, related_request_id, related_memory_id,
            continuity_type, subject, source_type, thread_state,
            continuity_value, retention_class, participants,
            evidence_start_time, evidence_end_time
        ) values (
            v_run.assistant_id, nullif(trim(v_conversation_id), ''), v_source_message_id,
            v_content, nullif(left(trim(coalesce(v_item->>'title', '')), 100), ''),
            array[v_continuity_type, coalesce(v_item->>'subject', 'other')],
            least(greatest(coalesce((v_item->>'importance')::integer, 5), 1), 10),
            case
                when char_length(trim(coalesce(v_item->>'reason', ''))) >= 3
                then left(trim(v_item->>'reason'), 500)
                else '连续感总结提取，等待用户审核'
            end,
            v_content_hash, 'continuity-' || v_run.id::text || '-' || v_content_hash,
            'pending', 'daily_digest', null, 'append', v_memory_type,
            least(greatest(coalesce((v_item->>'confidence')::double precision, 0.6), 0), 1),
            v_evidence_ids, v_source_time, v_memory_time, v_time_precision,
            v_run.id, v_embedding, v_dedupe_state, v_dedupe_reason,
            v_related_request_id, v_related_memory_id,
            v_continuity_type, v_item->>'subject', v_item->>'source_type', v_thread_state,
            least(greatest(coalesce((v_item->>'continuity_value')::integer, 5), 1), 10),
            case when v_item->>'retention_class' = 'core' then 'core' else 'normal' end,
            array(
                select distinct value
                from jsonb_array_elements_text(coalesce(v_item->'participants', '[]'::jsonb))
                where value in ('yezi', 'qi', 'other')
                limit 3
            ),
            nullif(v_item->>'evidence_start_time', '')::timestamptz,
            nullif(v_item->>'evidence_end_time', '')::timestamptz
        ) on conflict do nothing;

        get diagnostics v_delta = row_count;
        v_inserted := v_inserted + v_delta;
        v_preview := v_preview || jsonb_build_array(
            (v_item - 'embedding' - 'content_hash') || jsonb_build_object(
                'commit_status', case
                    when v_delta = 1 then 'inserted_pending'
                    else 'skipped_active_content'
                end,
                'dedupe_state', v_dedupe_state,
                'dedupe_reason', v_dedupe_reason,
                'related_request_id', v_related_request_id,
                'related_memory_id', v_related_memory_id
            )
        );
    end loop;

    update public.memory_continuity_cursors set
        last_processed_message_id = greatest(last_processed_message_id, v_run.source_last_message_id),
        status = 'ready',
        last_success_at = now(),
        manual_cooldown_until = case
            when v_run.trigger in ('continuity_manual', 'continuity_retry')
            then now() + interval '10 seconds'
            else manual_cooldown_until
        end,
        auto_cooldown_until = now() + interval '1 hour',
        blocked_first_message_id = null,
        blocked_last_message_id = null,
        blocked_message_count = null,
        blocked_at = null,
        pause_reason = null,
        updated_at = now()
    where assistant_id = v_run.assistant_id;

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = jsonb_array_length(p_candidates),
        inserted_count = v_inserted,
        preview_memories = v_preview,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = v_run.id;

    return v_inserted;
end;
$function$;

revoke all on function public.commit_memory_continuity_run(bigint, jsonb)
    from public, anon, authenticated;
grant execute on function public.commit_memory_continuity_run(bigint, jsonb)
    to service_role;

create or replace function public.skip_memory_continuity_blocked(
    p_assistant_id text
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_cursor public.memory_continuity_cursors%rowtype;
    v_run_id bigint;
begin
    -- Serialize with claim_digest_slot so retry and skip cannot both win.
    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 1));
    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 0));

    if exists (
        select 1
        from public.memory_digest_runs
        where assistant_id = p_assistant_id
          and status in ('claimed', 'running')
          and claimed_at > now() - interval '30 minutes'
          and (heartbeat_at is null or heartbeat_at > now() - interval '30 minutes')
    ) then
        raise exception 'memory_continuity_already_running';
    end if;

    select * into v_cursor
    from public.memory_continuity_cursors
    where assistant_id = p_assistant_id
    for update;

    if not found or v_cursor.status <> 'paused_empty' then
        raise exception 'memory_continuity_not_paused';
    end if;
    if v_cursor.blocked_first_message_id is null
       or v_cursor.blocked_last_message_id is null
       or coalesce(v_cursor.blocked_message_count, 0) < 1 then
        raise exception 'memory_continuity_blocked_batch_missing';
    end if;

    insert into public.memory_digest_runs (
        assistant_id, pipeline, trigger, mode, status,
        source_first_message_id, source_last_message_id, message_count,
        extracted_count, inserted_count, preview_memories,
        started_at, completed_at
    ) values (
        p_assistant_id, 'continuity', 'continuity_skip', 'execute', 'succeeded',
        v_cursor.blocked_first_message_id, v_cursor.blocked_last_message_id,
        v_cursor.blocked_message_count, 0, 0, '[]'::jsonb, now(), now()
    ) returning id into v_run_id;

    update public.memory_continuity_cursors set
        last_processed_message_id = greatest(last_processed_message_id, blocked_last_message_id),
        status = 'ready',
        manual_cooldown_until = now() + interval '10 seconds',
        auto_cooldown_until = now() + interval '1 hour',
        blocked_first_message_id = null,
        blocked_last_message_id = null,
        blocked_message_count = null,
        blocked_at = null,
        pause_reason = null,
        updated_at = now()
    where assistant_id = p_assistant_id
    returning * into v_cursor;

    return jsonb_build_object(
        'run_id', v_run_id,
        'cursor', to_jsonb(v_cursor)
    );
end;
$function$;

revoke all on function public.skip_memory_continuity_blocked(text)
    from public, anon, authenticated;
grant execute on function public.skip_memory_continuity_blocked(text)
    to service_role;

-- Existing review RPCs keep continuity metadata untouched when editing title,
-- content, or importance. This trigger copies the metadata after approve/merge.
create or replace function public.sync_reviewed_memory_request_metadata()
returns trigger
language plpgsql
set search_path to 'public', 'extensions'
as $function$
begin
    if new.status in ('approved', 'merged') and new.memory_id is not null then
        update public.memories as memory
        set
            memory_type = new.memory_type,
            evidence_message_ids = new.evidence_message_ids,
            source_time = new.source_time,
            memory_time = new.memory_time,
            time_precision = new.time_precision,
            digest_run_id = coalesce(new.digest_run_id, memory.digest_run_id),
            source_first_message_id = coalesce(
                (select min(value) from unnest(new.evidence_message_ids) as value),
                memory.source_first_message_id
            ),
            source_last_message_id = coalesce(
                (select max(value) from unnest(new.evidence_message_ids) as value),
                memory.source_last_message_id
            ),
            source = case
                when new.source = 'daily_digest' then 'daily_digest'
                else memory.source
            end,
            embedding = coalesce(new.embedding, memory.embedding),
            confidence = coalesce(new.confidence, memory.confidence),
            continuity_type = coalesce(new.continuity_type, memory.continuity_type),
            subject = case when new.continuity_type is not null then new.subject else memory.subject end,
            source_type = case when new.continuity_type is not null then new.source_type else memory.source_type end,
            thread_state = case when new.continuity_type is not null then new.thread_state else memory.thread_state end,
            continuity_value = case when new.continuity_type is not null then new.continuity_value else memory.continuity_value end,
            retention_class = case when new.continuity_type is not null then new.retention_class else memory.retention_class end,
            participants = case when new.continuity_type is not null then new.participants else memory.participants end,
            evidence_start_time = case when new.continuity_type is not null then new.evidence_start_time else memory.evidence_start_time end,
            evidence_end_time = case when new.continuity_type is not null then new.evidence_end_time else memory.evidence_end_time end
        where memory.id = new.memory_id;
    end if;
    return new;
end;
$function$;

revoke all on function public.sync_reviewed_memory_request_metadata()
    from public, anon, authenticated;
grant execute on function public.sync_reviewed_memory_request_metadata()
    to service_role;

-- Recreate explicitly so installations missing the older trigger converge.
drop trigger if exists sync_reviewed_memory_request_metadata
    on public.memory_requests;
create trigger sync_reviewed_memory_request_metadata
after insert or update of status, memory_id
on public.memory_requests
for each row
execute function public.sync_reviewed_memory_request_metadata();
