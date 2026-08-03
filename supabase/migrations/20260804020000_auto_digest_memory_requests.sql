-- Route automatic memory digests through the same private review queue as the
-- OrangeChat memory tool. chat_messages is evidence-only and remains read-only.

create extension if not exists pg_trgm with schema extensions;

alter table public.memory_requests
    add column if not exists memory_type text not null default 'other',
    add column if not exists confidence double precision,
    add column if not exists evidence_message_ids bigint[] not null default '{}'::bigint[],
    add column if not exists source_time timestamptz,
    add column if not exists memory_time timestamptz,
    add column if not exists time_precision text not null default 'unknown',
    add column if not exists digest_run_id bigint,
    add column if not exists embedding extensions.vector,
    add column if not exists dedupe_state text not null default 'none',
    add column if not exists dedupe_reason text,
    add column if not exists related_request_id bigint;

alter table public.memory_requests
    drop constraint if exists memory_requests_memory_type_values,
    drop constraint if exists memory_requests_confidence_range,
    drop constraint if exists memory_requests_time_precision_values,
    drop constraint if exists memory_requests_digest_run_fkey,
    drop constraint if exists memory_requests_dedupe_state_values,
    drop constraint if exists memory_requests_dedupe_reason_length,
    drop constraint if exists memory_requests_related_request_fkey;

alter table public.memory_requests
    add constraint memory_requests_memory_type_values
        check (memory_type in (
            'profile', 'preference', 'relationship', 'habit',
            'event', 'goal', 'other'
        )),
    add constraint memory_requests_confidence_range
        check (confidence is null or confidence between 0 and 1),
    add constraint memory_requests_time_precision_values
        check (time_precision in ('minute', 'day', 'approximate', 'unknown')),
    add constraint memory_requests_digest_run_fkey
        foreign key (digest_run_id)
        references public.memory_digest_runs(id)
        on delete set null,
    add constraint memory_requests_dedupe_state_values
        check (dedupe_state in ('none', 'possible_duplicate')),
    add constraint memory_requests_dedupe_reason_length
        check (dedupe_reason is null or char_length(dedupe_reason) <= 240),
    add constraint memory_requests_related_request_fkey
        foreign key (related_request_id)
        references public.memory_requests(id)
        on delete set null;

create index if not exists memory_requests_type_queue_idx
    on public.memory_requests (memory_type, status, created_at desc);

create index if not exists memory_requests_digest_run_idx
    on public.memory_requests (digest_run_id)
    where digest_run_id is not null;

create index if not exists memory_requests_related_request_idx
    on public.memory_requests (related_request_id)
    where related_request_id is not null;

alter table public.memories
    add column if not exists memory_type text not null default 'other',
    add column if not exists evidence_message_ids bigint[] not null default '{}'::bigint[],
    add column if not exists source_time timestamptz,
    add column if not exists memory_time timestamptz,
    add column if not exists time_precision text not null default 'unknown';

alter table public.memories
    drop constraint if exists memories_memory_type_values,
    drop constraint if exists memories_time_precision_values;

alter table public.memories
    add constraint memories_memory_type_values
        check (memory_type in (
            'profile', 'preference', 'relationship', 'habit',
            'event', 'goal', 'other'
        )),
    add constraint memories_time_precision_values
        check (time_precision in ('minute', 'day', 'approximate', 'unknown'));

comment on column public.memory_requests.memory_type is
    'Semantic content type; mutability remains separately represented by update_mode.';
comment on column public.memory_requests.evidence_message_ids is
    'Read-only chat message IDs that directly support this application.';
comment on column public.memory_requests.source_time is
    'Latest normalized Asia/Shanghai time among the cited source messages.';
comment on column public.memory_requests.memory_time is
    'When the remembered event occurred or current state became effective.';
comment on column public.memory_requests.time_precision is
    'Precision of memory_time: minute, day, approximate, or unknown.';
comment on column public.memory_requests.dedupe_state is
    'Conservative digest-time duplicate hint. Uncertain matches remain pending for user review.';
comment on column public.memory_requests.related_request_id is
    'Existing application that may describe the same fact; never auto-merged.';
comment on table public.memory_requests is
    'Private review queue for OrangeChat tool requests and automatic digest candidates.';

create or replace function public.memory_dedupe_text_similarity(
    p_left text,
    p_right text
)
returns double precision
language sql
immutable
parallel safe
set search_path to 'public', 'extensions'
as $function$
    with normalized as (
        select
            regexp_replace(
                lower(coalesce(p_left, '')),
                '[[:space:][:punct:]，。！？、：；“”‘’（）【】《》]+',
                '',
                'g'
            ) as left_text,
            regexp_replace(
                lower(coalesce(p_right, '')),
                '[[:space:][:punct:]，。！？、：；“”‘’（）【】《》]+',
                '',
                'g'
            ) as right_text
    ), canonicalized as (
        select
            regexp_replace(
                left_text,
                '^(用户|user)(明确)?(表示|说|提到|计划|打算|希望|想要|想|需要|要|会|将|目前|当前|已经|已)?',
                '',
                'i'
            ) as left_text,
            regexp_replace(
                right_text,
                '^(用户|user)(明确)?(表示|说|提到|计划|打算|希望|想要|想|需要|要|会|将|目前|当前|已经|已)?',
                '',
                'i'
            ) as right_text
        from normalized
    )
    select case
        when left_text = '' or right_text = '' then 0::double precision
        else greatest(
            similarity(left_text, right_text)::double precision,
            case
                when least(char_length(left_text), char_length(right_text)) >= 5
                 and (
                    position(left_text in right_text) > 0
                    or position(right_text in left_text) > 0
                 )
                then 0.94::double precision
                else 0::double precision
            end
        )
    end
    from canonicalized;
$function$;

revoke all on function public.memory_dedupe_text_similarity(text, text)
    from public, anon, authenticated;
grant execute on function public.memory_dedupe_text_similarity(text, text)
    to service_role;

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
            confidence = coalesce(new.confidence, memory.confidence)
        where memory.id = new.memory_id;
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

create or replace function public.commit_memory_digest_run(
    p_run_id bigint,
    p_memories jsonb default '[]'::jsonb
)
returns integer
language plpgsql
set search_path to 'public', 'extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_item jsonb;
    v_preview jsonb := '[]'::jsonb;
    v_inserted integer := 0;
    v_delta integer := 0;
    v_evidence_ids bigint[];
    v_source_message_id bigint;
    v_conversation_id text;
    v_content text;
    v_content_hash text;
    v_memory_type text;
    v_update_mode text;
    v_memory_key text;
    v_time_precision text;
    v_source_time timestamptz;
    v_memory_time timestamptz;
    v_embedding extensions.vector;
    v_related_request_id bigint;
    v_related_memory_id integer;
    v_existing_todo_id text;
    v_dedupe_state text;
    v_dedupe_reason text;
begin
    if jsonb_typeof(coalesce(p_memories, '[]'::jsonb)) <> 'array' then
        raise exception 'p_memories must be a JSON array';
    end if;

    select * into v_run
    from public.memory_digest_runs
    where id = p_run_id
    for update;

    if not found then
        raise exception 'memory digest run % not found', p_run_id;
    end if;
    if v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory digest run % is not a running execute run', p_run_id;
    end if;
    if v_run.source_last_message_id is null then
        raise exception 'memory digest run % has no source range', p_run_id;
    end if;

    -- Serialize plugin writes and digest commits for this assistant. The
    -- OrangeChat request RPC takes the same lock.
    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id, 0));

    for v_item in
        select value
        from jsonb_array_elements(coalesce(p_memories, '[]'::jsonb))
    loop
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

        if cardinality(v_evidence_ids) = 0 then
            raise exception 'memory digest item has no valid source evidence';
        end if;

        v_source_message_id := v_evidence_ids[1];
        select message.conversation_id into v_conversation_id
        from public.chat_messages as message
        where message.id = v_source_message_id;

        v_content := left(trim(v_item->>'content'), 600);
        v_content_hash := nullif(trim(v_item->>'content_hash'), '');
        v_memory_type := case
            when v_item->>'memory_type' in (
                'profile', 'preference', 'relationship', 'habit',
                'event', 'goal', 'other'
            ) then v_item->>'memory_type'
            else 'other'
        end;
        v_update_mode := case
            when v_item->>'update_mode' = 'replace' then 'replace'
            else 'append'
        end;
        v_memory_key := case
            when v_update_mode = 'replace'
            then nullif(v_item->>'memory_key', '')
            else null
        end;
        v_source_time := nullif(v_item->>'source_time', '')::timestamptz;
        v_embedding := case
            when v_item ? 'embedding' and v_item->'embedding' <> 'null'::jsonb
            then (v_item->>'embedding')::extensions.vector
            else null
        end;
        v_related_request_id := null;
        v_related_memory_id := null;
        v_existing_todo_id := null;
        v_dedupe_state := 'none';
        v_dedupe_reason := null;

        v_time_precision := case
            when v_item->>'time_precision' in ('minute', 'day', 'approximate', 'unknown')
            then v_item->>'time_precision'
            else 'unknown'
        end;

        v_memory_time := case
            when nullif(v_item->>'memory_time', '') is null then null
            when v_time_precision = 'day'
                 and (v_item->>'memory_time') ~ '^\d{4}-\d{2}-\d{2}$'
            then ((v_item->>'memory_time')::date::timestamp at time zone 'Asia/Shanghai')
            else (v_item->>'memory_time')::timestamptz
        end;

        -- The plugin-created request is a durable receipt. Rephrased content
        -- is suppressed only when provenance or a stable key ties it to the
        -- same source-time window; unrelated facts from one message survive.
        select request.id into v_related_request_id
        from public.memory_requests as request
        where request.assistant_id = v_run.assistant_id
          and request.status in (
              'pending', 'approved', 'merged', 'duplicate', 'conflict', 'rejected'
          )
          and (
            request.content_hash = v_content_hash
            or (
                v_memory_key is not null
                and request.memory_key = v_memory_key
                and request.created_at >= coalesce(
                    v_source_time,
                    v_run.started_at,
                    now()
                ) - interval '15 minutes'
            )
            or (
                public.memory_dedupe_text_similarity(request.content, v_content) >= 0.72
                and (
                    request.source_message_id = any(v_evidence_ids)
                    or (
                        nullif(trim(v_conversation_id), '') is not null
                        and request.conversation_id = nullif(trim(v_conversation_id), '')
                        and request.created_at between
                            coalesce(v_source_time, v_run.started_at, now()) - interval '15 minutes'
                            and now() + interval '1 minute'
                    )
                )
            )
          )
        order by
            (request.content_hash = v_content_hash) desc,
            request.created_at desc,
            request.id desc
        limit 1;

        if found then
            v_preview := v_preview || jsonb_build_array(
                (v_item - 'embedding' - 'content_hash')
                || jsonb_build_object(
                    'commit_status', 'skipped_existing_request',
                    'dedupe_reason', 'already_handled_by_memory_tool',
                    'related_request_id', v_related_request_id
                )
            );
            continue;
        end if;

        -- Daily digest never creates todos. It also avoids turning an existing
        -- open todo into a second, long-term goal memory application.
        if v_memory_type = 'goal' then
            select todo.id::text into v_existing_todo_id
            from public.todos as todo
            where coalesce(todo.is_completed, false) = false
              and coalesce(todo.is_hidden, false) = false
              and coalesce(todo.is_start_marker, false) = false
              and coalesce(todo.is_end_marker, false) = false
              and coalesce(todo.status, '') <> 'hollow'
              and public.memory_dedupe_text_similarity(todo.content, v_content) >= 0.58
            order by todo.updated_at desc nulls last, todo.created_at desc nulls last
            limit 1;

            if found then
                v_preview := v_preview || jsonb_build_array(
                    (v_item - 'embedding' - 'content_hash')
                    || jsonb_build_object(
                        'commit_status', 'skipped_existing_todo',
                        'dedupe_reason', 'already_handled_by_todo_tool',
                        'related_todo_id', v_existing_todo_id
                    )
                );
                continue;
            end if;
        end if;

        -- Exact active memory content is definitive and can be skipped.
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
                (v_item - 'embedding' - 'content_hash')
                || jsonb_build_object(
                    'commit_status', 'skipped_active_memory',
                    'dedupe_reason', 'same_content_in_active_memory',
                    'related_memory_id', v_related_memory_id
                )
            );
            continue;
        end if;

        -- Similarity alone is not authoritative. Keep uncertain candidates in
        -- the queue and attach a review hint instead of auto-merging them.
        select request.id into v_related_request_id
        from public.memory_requests as request
        where request.assistant_id = v_run.assistant_id
          and request.status in ('pending', 'approved', 'merged')
          and not (
              v_update_mode = 'replace'
              and v_memory_key is not null
              and request.memory_key = v_memory_key
          )
          and (
            public.memory_dedupe_text_similarity(request.content, v_content) >= 0.86
            or (
                v_embedding is not null
                and request.embedding is not null
                and 1 - (request.embedding <=> v_embedding) >= 0.94
                and public.memory_dedupe_text_similarity(request.content, v_content) >= 0.38
            )
          )
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
              and not (
                  v_update_mode = 'replace'
                  and v_memory_key is not null
                  and memory.memory_key = v_memory_key
              )
              and (
                public.memory_dedupe_text_similarity(memory.content, v_content) >= 0.86
                or (
                    v_embedding is not null
                    and memory.embedding is not null
                    and 1 - (memory.embedding <=> v_embedding) >= 0.94
                    and public.memory_dedupe_text_similarity(memory.content, v_content) >= 0.38
                )
              )
            order by memory.id desc
            limit 1;

            if found then
                v_dedupe_state := 'possible_duplicate';
                v_dedupe_reason := 'similar_to_active_memory';
            end if;
        end if;

        insert into public.memory_requests (
            assistant_id,
            conversation_id,
            source_message_id,
            content,
            title,
            tags,
            importance,
            reason,
            content_hash,
            idempotency_key,
            status,
            source,
            memory_key,
            update_mode,
            memory_type,
            confidence,
            evidence_message_ids,
            source_time,
            memory_time,
            time_precision,
            digest_run_id,
            embedding,
            dedupe_state,
            dedupe_reason,
            related_request_id,
            related_memory_id
        ) values (
            v_run.assistant_id,
            nullif(trim(v_conversation_id), ''),
            v_source_message_id,
            v_content,
            nullif(left(trim(coalesce(v_item->>'title', '')), 100), ''),
            array(
                select jsonb_array_elements_text(
                    coalesce(v_item->'tags', '[]'::jsonb)
                )
                limit 5
            ),
            least(greatest(coalesce((v_item->>'importance')::integer, 5), 1), 10),
            case
                when v_dedupe_state = 'possible_duplicate'
                then '自动总结从对话原文提取；与现有内容相似，等待用户判断'
                else '自动总结从对话原文提取，等待用户审核'
            end,
            v_content_hash,
            'digest-' || v_run.id::text || '-' || left(v_content_hash, 64),
            'pending',
            'daily_digest',
            v_memory_key,
            v_update_mode,
            v_memory_type,
            least(greatest(coalesce((v_item->>'confidence')::double precision, 0.6), 0), 1),
            v_evidence_ids,
            v_source_time,
            v_memory_time,
            v_time_precision,
            v_run.id,
            v_embedding,
            v_dedupe_state,
            v_dedupe_reason,
            v_related_request_id,
            v_related_memory_id
        )
        on conflict do nothing;

        get diagnostics v_delta = row_count;
        v_inserted := v_inserted + v_delta;
        v_preview := v_preview || jsonb_build_array(
            (v_item - 'embedding' - 'content_hash')
            || jsonb_build_object(
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

    insert into public.memory_digest_cursors (
        assistant_id,
        last_processed_message_id,
        last_success_at,
        updated_at
    ) values (
        v_run.assistant_id,
        v_run.source_last_message_id,
        now(),
        now()
    )
    on conflict (assistant_id) do update set
        last_processed_message_id = greatest(
            public.memory_digest_cursors.last_processed_message_id,
            excluded.last_processed_message_id
        ),
        last_success_at = now(),
        updated_at = now();

    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = jsonb_array_length(coalesce(p_memories, '[]'::jsonb)),
        inserted_count = v_inserted,
        preview_memories = v_preview,
        completed_at = now(),
        error_code = null,
        error_message = null
    where id = v_run.id;

    return v_inserted;
end;
$function$;

revoke all on function public.commit_memory_digest_run(bigint, jsonb)
    from public, anon, authenticated;
grant execute on function public.commit_memory_digest_run(bigint, jsonb)
    to service_role;
