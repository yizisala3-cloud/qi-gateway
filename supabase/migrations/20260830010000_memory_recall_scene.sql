-- AI-supplied recall scene metadata plus a dedicated recall-scene vector
-- channel. recall_scene / recall_tags are provided by the writing AI and are
-- never derived from memory content; historical rows keep NULL (no backfill),
-- so legacy memories stay reachable through the unchanged keyword channel only.
-- public.chat_messages remains an immutable, select-only evidence source.

begin;

alter table public.memory_requests
    add column if not exists recall_scene text,
    add column if not exists recall_tags text[],
    add column if not exists evidence_time_precision text;

alter table public.memories
    add column if not exists recall_scene text,
    add column if not exists recall_tags text[],
    add column if not exists evidence_time_precision text,
    add column if not exists recall_embedding extensions.vector;

-- Evidence times keep their own precision, separate from the memory_time
-- time_precision column: evidence_end_time/source_time come from real message
-- clocks, while time_precision keeps describing memory_time only.
alter table public.memory_requests
    drop constraint if exists memory_requests_evidence_time_precision_values;
alter table public.memory_requests
    add constraint memory_requests_evidence_time_precision_values
        check (evidence_time_precision in ('minute', 'hour', 'day', 'approximate', 'unknown'));

alter table public.memories
    drop constraint if exists memories_evidence_time_precision_values;
alter table public.memories
    add constraint memories_evidence_time_precision_values
        check (evidence_time_precision in ('minute', 'hour', 'day', 'approximate', 'unknown'));

-- recall_embedding is derived only from recall_scene. A blank scene can never
-- carry an embedding; no enum, length, count, or content limits apply to the
-- recall scene fields themselves.
alter table public.memories
    drop constraint if exists memories_recall_embedding_scene_check;
alter table public.memories
    add constraint memories_recall_embedding_scene_check check (
        recall_embedding is null
        or nullif(btrim(coalesce(recall_scene, '')), '') is not null
    );

-- The injection layer now honours hour-level precision; widen the existing
-- time_precision checks so stored precision can say 'hour' without padding a
-- fabricated minute. No historical rows are rewritten.
alter table public.memory_requests
    drop constraint if exists memory_requests_time_precision_values;
alter table public.memory_requests
    add constraint memory_requests_time_precision_values
        check (time_precision in ('minute', 'hour', 'day', 'approximate', 'unknown'));

alter table public.memories
    drop constraint if exists memories_time_precision_values;
alter table public.memories
    add constraint memories_time_precision_values
        check (time_precision in ('minute', 'hour', 'day', 'approximate', 'unknown'));

comment on column public.memory_requests.recall_scene is
    'Natural-language recall scene supplied by the writing AI; null when it has no reliable basis.';
comment on column public.memory_requests.recall_tags is
    'Free-form recall scene tags supplied by the writing AI; no enum, count, or length limits.';
comment on column public.memories.recall_scene is
    'Natural-language recall scene describing when this memory should be recalled; never treated as memory content.';
comment on column public.memories.recall_tags is
    'Free-form recall scene tags; no enum, count, or length limits.';
comment on column public.memories.recall_embedding is
    'Vector embedding of recall_scene only; null when recall_scene is blank. Powers vector recall; embedding keeps its dedupe semantics.';
comment on column public.memory_requests.evidence_time_precision is
    'Precision of evidence_start_time/evidence_end_time/source_time as supported by the actual evidence; null for legacy rows means unknown, never guessed.';
comment on column public.memories.evidence_time_precision is
    'Precision of the stored evidence times; independent of time_precision, which describes memory_time only.';

-- PostgreSQL cannot replace a function while changing its RETURNS TABLE row
-- type. Drop only the exact existing signature, without CASCADE, then rebuild
-- the vector channel on recall_embedding.
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
    layer text,
    created_at timestamptz,
    last_recalled_at timestamptz,
    similarity double precision,
    continuity_id uuid,
    continuity_type text,
    continuity_schema_version smallint,
    continuity_data jsonb,
    subject text,
    source_type text,
    thread_state text,
    continuity_value integer,
    retention_class text,
    participants text[],
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
        memory.layer,
        memory.created_at,
        memory.last_recalled_at,
        1 - (memory.recall_embedding <=> query_embedding) as similarity,
        memory.continuity_id,
        memory.continuity_type,
        memory.continuity_schema_version,
        memory.continuity_data,
        memory.subject,
        memory.source_type,
        memory.thread_state,
        memory.continuity_value,
        memory.retention_class,
        memory.participants,
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

-- Signatures below gain recall-scene parameters, so each exact old signature
-- is dropped (never CASCADE) before the extended replacement is created.
drop function if exists public.create_memory_request_v4(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text, text, text, smallint, jsonb, text, text, integer,
    text, text[], text
);

create or replace function public.create_memory_request_v4(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_subject text, p_source_type text,
    p_continuity_value integer, p_retention_class text, p_participants text[], p_source text,
    p_recall_scene text, p_recall_tags text[]
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_recent_count integer;
    v_recall_scene text;
    v_recall_tags text[];
    v_evidence_time timestamptz;
    v_evidence_precision text;
begin
    if p_continuity_schema_version <> 1
       or not public.validate_continuity_data(p_continuity_type,p_thread_state,p_continuity_data) then
        raise exception 'memory_request_invalid_continuity_data';
    end if;
    if p_source not in ('orangechat_plugin','mcp_memory') then
        raise exception 'memory_request_invalid_source';
    end if;
    if p_update_mode not in ('append','replace')
       or (p_update_mode = 'append' and p_memory_key is not null)
       or (p_update_mode = 'replace' and p_memory_key is null) then
        raise exception 'memory_request_invalid_update_mode';
    end if;
    if p_continuity_type = 'interaction_rule'
       and coalesce(p_continuity_data->>'explicit_instruction','') = '' then
        raise exception 'memory_request_interaction_rule_requires_instruction';
    end if;

    v_recall_scene := nullif(btrim(coalesce(p_recall_scene,'')),'');
    v_recall_tags := case
        when p_recall_tags is null or cardinality(p_recall_tags) = 0 then null
        else array(
            select t.value
            from unnest(p_recall_tags) as t(value)
            where nullif(btrim(t.value),'') is not null
        )
    end;

    -- Event time is the cited source message's own clock from the immutable
    -- chat log. A missing or mismatched message leaves it null, never guessed.
    -- The chat client stores wall-clock timestamps down to the minute, so the
    -- evidence precision is 'minute' whenever the evidence time exists.
    if p_source_message_id is not null then
        select message.created_at at time zone 'Asia/Shanghai'
        into v_evidence_time
        from public.chat_messages as message
        where message.id = p_source_message_id
          and message.assistant_id = p_assistant_id
          and (
              nullif(trim(coalesce(p_conversation_id,'')),'') is null
              or message.conversation_id = nullif(trim(coalesce(p_conversation_id,'')),'')
          );
    end if;
    v_evidence_precision := case when v_evidence_time is null then null else 'minute' end;

    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id,0));
    select * into v_request
    from public.memory_requests
    where assistant_id = p_assistant_id and idempotency_key = p_idempotency_key
    limit 1;
    if found then
        return jsonb_build_object('created',false,'request',to_jsonb(v_request));
    end if;

    select * into v_request
    from public.memory_requests
    where assistant_id = p_assistant_id
      and content_hash = p_content_hash
      and continuity_type = p_continuity_type
      and status in ('pending','approved','merged')
    order by id desc
    limit 1;
    if found then
        return jsonb_build_object('created',false,'request',to_jsonb(v_request));
    end if;

    select count(*) into v_recent_count
    from public.memory_requests
    where assistant_id = p_assistant_id
      and source = p_source
      and created_at > now() - interval '1 minute';
    if v_recent_count >= least(greatest(coalesce(p_rate_limit,6),1),60) then
        raise exception 'memory_request_rate_limited';
    end if;

    insert into public.memory_requests(
        assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,content_hash,
        idempotency_key,status,source,memory_key,update_mode,evidence_message_ids,
        continuity_type,thread_state,continuity_id,continuity_schema_version,continuity_data,
        subject,source_type,continuity_value,retention_class,participants,
        recall_scene,recall_tags,evidence_start_time,evidence_end_time,source_time,evidence_time_precision
    ) values (
        p_assistant_id,nullif(trim(p_conversation_id),''),p_source_message_id,p_content,p_title,p_tags,p_importance,p_reason,p_content_hash,
        p_idempotency_key,'pending',p_source,p_memory_key,p_update_mode,
        case when p_source_message_id is null then '{}'::bigint[] else array[p_source_message_id] end,
        p_continuity_type,p_thread_state,null,1,p_continuity_data,
        p_subject,p_source_type,p_continuity_value,p_retention_class,p_participants,
        v_recall_scene,v_recall_tags,v_evidence_time,v_evidence_time,v_evidence_time,v_evidence_precision
    )
    returning * into v_request;
    return jsonb_build_object('created',true,'request',to_jsonb(v_request));
end;
$function$;

drop function if exists public.write_memory_direct_v1(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text, text, text, smallint, jsonb, text, text, integer,
    text, text[], text, text
);

create or replace function public.write_memory_direct_v1(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_subject text, p_source_type text,
    p_continuity_value integer, p_retention_class text, p_participants text[], p_source text,
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
        p_continuity_schema_version,p_continuity_data,p_subject,p_source_type,p_continuity_value,p_retention_class,
        p_participants,p_source,p_recall_scene,p_recall_tags
    );
    v_request := v_created->'request';
    if v_request->>'continuity_type' is distinct from p_continuity_type then
        raise exception 'memory_request_idempotency_conflict';
    end if;
    v_review := public.review_memory_request_v5(
        (v_request->>'id')::bigint,'approve',p_content,p_title,p_tags,p_importance,p_content_hash,
        left(coalesce(nullif(trim(p_reviewed_by),''),'orangechat_ai'),120),null,p_memory_key,p_update_mode,null,
        p_recall_embedding
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

drop function if exists public.review_memory_request_v5(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer
);

create or replace function public.review_memory_request_v5(
    p_request_id bigint,
    p_action text,
    p_content text default null,
    p_title text default null,
    p_tags text[] default null,
    p_importance integer default null,
    p_content_hash text default null,
    p_reviewed_by text default 'gateway_admin',
    p_review_note text default null,
    p_memory_key text default null,
    p_update_mode text default null,
    p_related_memory_id integer default null,
    p_recall_embedding extensions.vector default null
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_mode text;
    v_key text;
    v_id uuid;
    v_result jsonb;
    v_memory_id integer;
begin
    select * into v_request
    from public.memory_requests
    where id = p_request_id
    for update;
    if not found then
        raise exception 'memory_request_not_found';
    end if;
    if lower(trim(p_action)) in ('approve','merge') and (
        v_request.continuity_schema_version <> 1
        or not public.validate_continuity_data(
            v_request.continuity_type,v_request.thread_state,v_request.continuity_data
        )
    ) then
        raise exception 'memory_request_unclassified_legacy';
    end if;

    if v_request.status in ('pending','conflict') and lower(trim(p_action)) = 'approve' then
        v_mode := lower(trim(coalesce(p_update_mode,v_request.update_mode,'append')));
        v_key := case
            when v_mode = 'replace'
            then nullif(lower(trim(coalesce(p_memory_key,v_request.memory_key,''))),'')
            else null
        end;
        if v_mode not in ('append','replace') or (v_mode = 'replace' and v_key is null) then
            raise exception 'memory_request_invalid_update_mode';
        end if;
        v_id := public.allocate_memory_continuity_id(v_request.assistant_id,v_mode,v_key);
        update public.memory_requests set
            continuity_id = v_id,
            update_mode = v_mode,
            memory_key = v_key,
            updated_at = now()
        where id = v_request.id
        returning * into v_request;
    elsif v_request.status in ('pending','conflict') and lower(trim(p_action)) = 'merge' then
        select continuity_id into v_id
        from public.memories
        where id = p_related_memory_id
          and verified = 'verified'
          and is_active = true
          and (assistant_id is null or assistant_id = v_request.assistant_id)
        for update;
        if not found then
            raise exception 'memory_request_related_memory_not_found';
        end if;
        if v_id is null then
            insert into public.memory_continuity_objects(assistant_id)
            values(v_request.assistant_id)
            returning continuity_id into v_id;
            update public.memories set continuity_id = v_id where id = p_related_memory_id;
        end if;
        update public.memory_requests set continuity_id = v_id, updated_at = now()
        where id = v_request.id
        returning * into v_request;
    end if;

    v_result := public.review_memory_request_v4(
        p_request_id,p_action,p_content,p_title,p_tags,p_importance,p_content_hash,
        p_reviewed_by,p_review_note,p_memory_key,p_update_mode,p_related_memory_id
    );

    -- The gateway derives this embedding from recall_scene only; the database
    -- never guesses one. Runs after the metadata trigger so the copy of
    -- recall_scene is already in place for the scene-presence constraint.
    if p_recall_embedding is not null then
        v_memory_id := nullif(v_result->'request'->>'memory_id','')::integer;
        if v_memory_id is not null then
            update public.memories
            set recall_embedding = p_recall_embedding
            where id = v_memory_id;
        end if;
    end if;
    return v_result;
end;
$function$;

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
        continuity_type,subject,source_type,thread_state,continuity_value,retention_class,participants,evidence_start_time,evidence_end_time,
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
        v_type,p_item->>'subject',p_item->>'source_type',p_item->>'thread_state',
        least(greatest(coalesce((p_item->>'continuity_value')::integer,5),1),10),coalesce(p_item->>'retention_class','normal'),
        array(select value from jsonb_array_elements_text(coalesce(p_item->'participants','[]'::jsonb)) limit 3),
        nullif(p_item->>'evidence_start_time','')::timestamptz,nullif(p_item->>'evidence_end_time','')::timestamptz,
        v_evidence_precision,
        null,1,p_item->'continuity_data',
        v_recall_scene,v_recall_tags
    )
    on conflict do nothing
    returning id into v_request_id;
    get diagnostics v_delta = row_count;
    if v_delta = 1 and v_type in ('moment','thread','inside_joke') then
        v_review := public.review_memory_request_v5(
            v_request_id,'approve',v_content,nullif(left(trim(coalesce(p_item->>'title','')),100),''),
            array[v_type],least(greatest(coalesce((p_item->>'importance')::integer,5),1),10),v_content_hash,
            'daily_digest_ai','automatic low-risk continuity memory',v_key,v_mode,null,
            v_recall_embedding
        );
    end if;
    return v_delta;
end;
$function$;

create or replace function public.commit_memory_digest_run(
    p_run_id bigint,
    p_memories jsonb default '[]'::jsonb
)
returns integer
language plpgsql
security definer
set search_path to 'public','extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_item jsonb;
    v_count integer := 0;
    v_preview jsonb := '[]'::jsonb;
begin
    if jsonb_typeof(coalesce(p_memories,'[]'::jsonb)) <> 'array' then
        raise exception 'p_memories must be a JSON array';
    end if;
    select * into v_run from public.memory_digest_runs where id = p_run_id for update;
    if not found or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_digest_invalid_run';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id,0));
    for v_item in select value from jsonb_array_elements(p_memories) loop
        v_count := v_count + public.store_continuity_candidate(v_run,v_item);
        v_preview := v_preview || jsonb_build_array(v_item - 'embedding' - 'content_hash' - 'recall_embedding');
    end loop;
    insert into public.memory_digest_cursors(assistant_id,last_processed_message_id,last_success_at,updated_at)
    values(v_run.assistant_id,v_run.source_last_message_id,now(),now())
    on conflict(assistant_id) do update set
        last_processed_message_id = greatest(public.memory_digest_cursors.last_processed_message_id,excluded.last_processed_message_id),
        last_success_at = now(),
        updated_at = now();
    update public.memory_digest_runs set
        status = 'succeeded',
        extracted_count = jsonb_array_length(p_memories),
        inserted_count = v_count,
        preview_memories = v_preview,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = p_run_id;
    return v_count;
end;
$function$;

create or replace function public.commit_memory_continuity_run(
    p_run_id bigint,
    p_candidates jsonb default '[]'::jsonb
)
returns integer
language plpgsql
security definer
set search_path to 'public','extensions'
as $function$
declare
    v_run public.memory_digest_runs%rowtype;
    v_item jsonb;
    v_count integer := 0;
    v_preview jsonb := '[]'::jsonb;
begin
    if jsonb_typeof(coalesce(p_candidates,'[]'::jsonb)) <> 'array'
       or jsonb_array_length(p_candidates) = 0 then
        raise exception 'memory_continuity_invalid_candidates';
    end if;
    select * into v_run from public.memory_digest_runs where id = p_run_id for update;
    if not found or v_run.pipeline <> 'continuity' or v_run.mode <> 'execute' or v_run.status <> 'running' then
        raise exception 'memory_continuity_invalid_run';
    end if;
    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id,0));
    for v_item in select value from jsonb_array_elements(p_candidates) loop
        v_count := v_count + public.store_continuity_candidate(v_run,v_item);
        v_preview := v_preview || jsonb_build_array(v_item - 'embedding' - 'content_hash' - 'recall_embedding');
    end loop;
    update public.memory_continuity_cursors set
        last_processed_message_id = greatest(last_processed_message_id,v_run.source_last_message_id),
        status = 'ready',
        last_success_at = now(),
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
        inserted_count = v_count,
        preview_memories = v_preview,
        completed_at = now(),
        heartbeat_at = null,
        error_code = null,
        error_message = null
    where id = p_run_id;
    return v_count;
end;
$function$;

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
            subject = case when v_replace_continuity
                then coalesce(new.subject,memory.subject)
                else memory.subject end,
            source_type = case when v_replace_continuity
                then coalesce(new.source_type,memory.source_type)
                else memory.source_type end,
            thread_state = case when v_replace_continuity
                then coalesce(new.thread_state,memory.thread_state)
                else memory.thread_state end,
            continuity_value = case when v_replace_continuity
                then coalesce(new.continuity_value,memory.continuity_value)
                else memory.continuity_value end,
            retention_class = case when v_replace_continuity
                then coalesce(new.retention_class,memory.retention_class)
                else memory.retention_class end,
            participants = case when v_replace_continuity
                then coalesce(new.participants,memory.participants)
                else memory.participants end,
            evidence_start_time = coalesce(new.evidence_start_time,memory.evidence_start_time),
            evidence_end_time = coalesce(new.evidence_end_time,memory.evidence_end_time),
            evidence_time_precision = coalesce(new.evidence_time_precision,memory.evidence_time_precision),
            source = case when new.source = 'daily_digest' then 'daily_digest' else memory.source end
        where memory.id = new.memory_id;
        update public.memory_continuity_objects
        set updated_at = now()
        where continuity_id = new.continuity_id;
    end if;
    return new;
end;
$function$;

revoke all on function public.create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text,text[]) from public,anon,authenticated;
revoke all on function public.write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text,text,text[],extensions.vector) from public,anon,authenticated;
revoke all on function public.review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,text,integer,extensions.vector) from public,anon,authenticated;
revoke all on function public.store_continuity_candidate(public.memory_digest_runs,jsonb) from public,anon,authenticated;
revoke all on function public.commit_memory_digest_run(bigint,jsonb),public.commit_memory_continuity_run(bigint,jsonb) from public,anon,authenticated;
revoke all on function public.sync_reviewed_memory_request_metadata() from public,anon,authenticated;

grant execute on function public.create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text,text[]) to service_role;
grant execute on function public.write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text,text,text[],extensions.vector) to service_role;
grant execute on function public.review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,text,integer,extensions.vector) to service_role;
grant execute on function public.store_continuity_candidate(public.memory_digest_runs,jsonb) to service_role;
grant execute on function public.commit_memory_digest_run(bigint,jsonb),public.commit_memory_continuity_run(bigint,jsonb) to service_role;

commit;
