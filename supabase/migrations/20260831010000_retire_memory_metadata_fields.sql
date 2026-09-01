-- Permanent retirement of six generic memory metadata fields:
--   memories.layer / emotion_weight / subject / participants /
--   continuity_value / retention_class
--   memory_requests.subject / participants / continuity_value / retention_class
-- source_type, importance, continuity_type, continuity_data, recall_scene,
-- recall_tags and recall_embedding are all retained untouched, and no
-- replacement column is created for any retired field. Historical values in
-- the dropped columns are discarded permanently by design: no backup, no
-- migration into content/tags/continuity_data, no reclassification of the 59
-- existing formal memories, and no status change for any request or memory.
-- public.chat_messages remains an immutable, select-only evidence source.
--
-- Every SQL function whose current production definition reads or writes a
-- retired column is rebuilt from its final implementation BEFORE the columns
-- are dropped. Only exact old signatures are dropped, never with CASCADE.

begin;

-- ---------------------------------------------------------------------------
-- 1. Rebuild the SQL-language recall functions. Their parsed query plans pin
--    the retired columns, so PostgreSQL would refuse the drop while they
--    depend on them. The rebuilt bodies keep matching scope, input bounds,
--    active/verified conditions, ordering, caps and grants verbatim; only the
--    retired metadata leaves their RETURNS TABLE and SELECT lists.
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
      and relevance.keyword_matches > 0
    order by relevance.keyword_matches desc, memory.created_at desc
    limit least(greatest(coalesce(result_limit,20),1),50);
$function$;

revoke all on function public.search_memories_by_keywords(text[], integer) from public, anon, authenticated;

grant execute on function public.search_memories_by_keywords(text[], integer) to service_role;

-- ---------------------------------------------------------------------------
-- 2. Rebuild the plpgsql functions whose runtime bodies still read or write a
--    retired column. Signatures are unchanged where noted, so create or
--    replace keeps their grants and the metadata trigger attachment intact.
-- ---------------------------------------------------------------------------

-- Same signature; with the layer column retired the heat task decays heat
-- only. It never flips is_active: automatic archiving is suspended, the
-- archived_count outputs stay 0 for compatibility, and already-archived rows
-- are neither archived further nor reactivated.
-- SECURITY DEFINER like the other internal maintenance RPCs (digest commit,
-- continuity commit): service_role is the only caller, and the memories CHECK
-- constraints evaluate validate_continuity_data with the caller's privileges,
-- which service_role does not hold.
create or replace function public.run_memory_heat_decay(
    run_at timestamptz default now()
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_run_at timestamptz := coalesce(run_at, now());
    v_run_date date;
    v_elapsed_days integer;
    v_updated_count integer := 0;
begin
    v_run_date := timezone('Asia/Shanghai', v_run_at)::date;

    select greatest(
        1,
        least(coalesce(v_run_date - max(history.run_date), 1), 30)
    )
    into v_elapsed_days
    from public.memory_heat_runs as history
    where history.run_date < v_run_date;

    insert into public.memory_heat_runs (run_date, elapsed_days, executed_at)
    values (v_run_date, v_elapsed_days, v_run_at)
    on conflict (run_date) do nothing;

    if not found then
        return jsonb_build_object(
            'status', 'already_ran',
            'run_date', v_run_date,
            'elapsed_days', 0,
            'updated_count', 0,
            'archived_count', 0
        );
    end if;

    with candidates as (
        select
            memory.id,
            greatest(
                0.0,
                least(
                    100.0,
                    memory.heat * power(
                        0.95,
                        v_elapsed_days::double precision / (
                            1.0
                            + least(
                                greatest(coalesce(memory.importance, 5), 1),
                                10
                            )::double precision / 10.0 * 0.5
                        )
                    )
                )
            ) as new_heat
        from public.memories as memory
        where memory.is_active = true
          and memory.verified = 'verified'
    ),
    changed as (
        update public.memories as memory
        set heat = round(candidate.new_heat::numeric, 2)::double precision
        from candidates as candidate
        where memory.id = candidate.id
          and abs(candidate.new_heat - memory.heat) >= 0.005
        returning memory.id
    )
    select count(*)::integer
    into v_updated_count
    from changed;

    update public.memory_heat_runs
    set
        updated_count = v_updated_count,
        archived_count = 0,
        executed_at = v_run_at
    where run_date = v_run_date;

    return jsonb_build_object(
        'status', 'succeeded',
        'run_date', v_run_date,
        'elapsed_days', v_elapsed_days,
        'updated_count', v_updated_count,
        'archived_count', 0
    );
end;
$function$;

-- Same signature; the approve path no longer writes layer or emotion_weight.
create or replace function public.review_memory_request_v2(
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
    p_update_mode text default null
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_memory_id integer;
    v_existing_content_id integer;
    v_current_memory_id integer;
    v_superseded_memory_id integer;
    v_content text;
    v_title text;
    v_tags text[];
    v_importance integer;
    v_content_hash text;
    v_memory_key text;
    v_update_mode text;
    v_status text := 'approved';
begin
    if p_action not in ('approve', 'reject') then
        raise exception 'memory_request_invalid_action';
    end if;

    select * into v_request
    from public.memory_requests
    where id = p_request_id
    for update;

    if not found then
        raise exception 'memory_request_not_found';
    end if;

    if (p_action = 'approve' and v_request.status in ('approved', 'merged'))
       or (p_action = 'reject' and v_request.status = 'rejected') then
        return jsonb_build_object(
            'changed', false,
            'superseded_memory_id', null,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'memory_id', v_request.memory_id,
                'reviewed_at', v_request.reviewed_at
            )
        );
    end if;

    if v_request.status <> 'pending' then
        raise exception 'memory_request_not_pending';
    end if;

    if p_action = 'reject' then
        update public.memory_requests set
            status = 'rejected',
            reviewed_at = now(),
            reviewed_by = left(coalesce(nullif(trim(p_reviewed_by), ''), 'gateway_admin'), 120),
            review_note = nullif(left(trim(coalesce(p_review_note, '')), 500), ''),
            updated_at = now()
        where id = v_request.id
        returning * into v_request;

        return jsonb_build_object(
            'changed', true,
            'superseded_memory_id', null,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'memory_id', v_request.memory_id,
                'reviewed_at', v_request.reviewed_at
            )
        );
    end if;

    v_content := trim(coalesce(nullif(p_content, ''), v_request.content));
    v_title := nullif(left(trim(coalesce(p_title, v_request.title, '')), 100), '');
    v_tags := coalesce(p_tags, v_request.tags, '{}'::text[]);
    v_importance := coalesce(p_importance, v_request.importance, 5);
    v_content_hash := coalesce(nullif(trim(p_content_hash), ''), v_request.content_hash);
    v_memory_key := nullif(lower(trim(coalesce(p_memory_key, v_request.memory_key, ''))), '');
    v_update_mode := lower(trim(coalesce(p_update_mode, v_request.update_mode, 'append')));

    if char_length(v_content) not between 5 and 600 then
        raise exception 'memory_request_invalid_content';
    end if;
    if cardinality(v_tags) > 5 then
        raise exception 'memory_request_invalid_tags';
    end if;
    if v_importance not between 1 and 10 then
        raise exception 'memory_request_invalid_importance';
    end if;
    if char_length(v_content_hash) <> 64 then
        raise exception 'memory_request_invalid_hash';
    end if;
    if v_update_mode not in ('append', 'replace') then
        raise exception 'memory_request_invalid_update_mode';
    end if;
    if v_memory_key is not null
       and v_memory_key !~ '^[a-z0-9][a-z0-9._:/-]{2,119}$' then
        raise exception 'memory_request_invalid_memory_key';
    end if;
    if (v_update_mode = 'replace' and v_memory_key is null)
       or (v_update_mode = 'append' and v_memory_key is not null) then
        raise exception 'memory_request_invalid_replace_key';
    end if;

    if v_update_mode = 'replace' then
        perform pg_advisory_xact_lock(
            hashtextextended('memory-key:' || v_memory_key, 0)
        );

        select memory.id into v_current_memory_id
        from public.memories as memory
        where memory.memory_key = v_memory_key
          and memory.is_active = true
          and memory.verified = 'verified'
        for update;

        if v_current_memory_id is not null and exists (
            select 1
            from public.memory_requests as newer_request
            where newer_request.memory_id = v_current_memory_id
              and newer_request.status in ('approved', 'merged')
              and newer_request.created_at > v_request.created_at
        ) then
            raise exception 'memory_request_stale_update';
        end if;
    end if;

    select memory.id into v_existing_content_id
    from public.memories as memory
    where memory.content_hash = v_content_hash
    for update;

    if v_update_mode = 'replace'
       and v_current_memory_id is not null
       and v_current_memory_id is distinct from v_existing_content_id then
        update public.memories
        set
            is_active = false,
            superseded_at = now(),
            superseded_by_memory_id = null
        where id = v_current_memory_id;
    end if;

    insert into public.memories (
        content,
        title,
        tags,
        heat,
        importance,
        embedding,
        source,
        verified,
        is_active,
        recall_count,
        assistant_id,
        digest_run_id,
        source_first_message_id,
        source_last_message_id,
        confidence,
        content_hash,
        memory_key,
        supersedes_memory_id,
        superseded_by_memory_id,
        superseded_at
    ) values (
        v_content,
        v_title,
        v_tags,
        least(greatest(v_importance * 10.0, 0), 100),
        v_importance,
        null,
        'ai_tool_request',
        'verified',
        true,
        0,
        v_request.assistant_id,
        null,
        v_request.source_message_id,
        v_request.source_message_id,
        1.0,
        v_content_hash,
        case when v_update_mode = 'replace' then v_memory_key else null end,
        case
            when v_update_mode = 'replace'
             and v_current_memory_id is not null
             and v_existing_content_id is null
            then v_current_memory_id
            else null
        end,
        null,
        null
    )
    on conflict (content_hash) do update set
        content = excluded.content,
        title = excluded.title,
        tags = excluded.tags,
        heat = greatest(public.memories.heat, excluded.heat),
        importance = excluded.importance,
        source = excluded.source,
        verified = 'verified',
        is_active = true,
        assistant_id = coalesce(public.memories.assistant_id, excluded.assistant_id),
        source_first_message_id = coalesce(
            public.memories.source_first_message_id,
            excluded.source_first_message_id
        ),
        source_last_message_id = coalesce(
            public.memories.source_last_message_id,
            excluded.source_last_message_id
        ),
        confidence = greatest(public.memories.confidence, excluded.confidence),
        memory_key = case
            when v_update_mode = 'replace' then excluded.memory_key
            else public.memories.memory_key
        end,
        superseded_by_memory_id = null,
        superseded_at = null
    returning id into v_memory_id;

    if v_update_mode = 'replace'
       and v_current_memory_id is not null
       and v_current_memory_id <> v_memory_id then
        v_superseded_memory_id := v_current_memory_id;
        update public.memories
        set
            is_active = false,
            superseded_at = now(),
            superseded_by_memory_id = v_memory_id
        where id = v_current_memory_id;
    end if;

    if exists (
        select 1
        from public.memory_requests as existing
        where existing.id <> v_request.id
          and existing.assistant_id = v_request.assistant_id
          and existing.content_hash = v_content_hash
          and existing.status in ('pending', 'approved', 'merged')
    ) then
        v_status := 'merged';
    end if;

    update public.memory_requests set
        content = v_content,
        title = v_title,
        tags = v_tags,
        importance = v_importance,
        content_hash = v_content_hash,
        memory_key = case when v_update_mode = 'replace' then v_memory_key else null end,
        update_mode = v_update_mode,
        status = v_status,
        memory_id = v_memory_id,
        reviewed_at = now(),
        reviewed_by = left(coalesce(nullif(trim(p_reviewed_by), ''), 'gateway_admin'), 120),
        review_note = nullif(left(trim(coalesce(p_review_note, '')), 500), ''),
        updated_at = now()
    where id = v_request.id
    returning * into v_request;

    return jsonb_build_object(
        'changed', true,
        'superseded_memory_id', v_superseded_memory_id,
        'request', jsonb_build_object(
            'id', v_request.id,
            'status', v_request.status,
            'memory_id', v_request.memory_id,
            'reviewed_at', v_request.reviewed_at,
            'memory_key', v_request.memory_key,
            'update_mode', v_request.update_mode
        )
    );
end;
$function$;

-- Same signature; the merge path no longer writes layer or emotion_weight.
create or replace function public.review_memory_request_v3(
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
    p_related_memory_id integer default null
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_target public.memories%rowtype;
    v_result jsonb;
    v_result_memory_id integer;
    v_from_status text;
    v_action text := lower(trim(coalesce(p_action, '')));
    v_content text;
    v_title text;
    v_tags text[];
    v_importance integer;
    v_content_hash text;
    v_target_memory_key text;
    v_reviewed_by text := left(
        coalesce(nullif(trim(p_reviewed_by), ''), 'gateway_admin'),
        120
    );
    v_review_note text := nullif(left(trim(coalesce(p_review_note, '')), 500), '');
begin
    if v_action not in ('approve', 'reject', 'merge', 'duplicate', 'conflict') then
        raise exception 'memory_request_invalid_action';
    end if;

    select * into v_request
    from public.memory_requests
    where id = p_request_id
    for update;

    if not found then
        raise exception 'memory_request_not_found';
    end if;

    v_from_status := v_request.status;

    -- Approval and rejection retain the proven v2 behavior. A conflict is an
    -- unresolved review state, so the user may later resolve it either way.
    if v_action in ('approve', 'reject') then
        if (v_action = 'approve' and v_request.status in ('approved', 'merged'))
           or (v_action = 'reject' and v_request.status = 'rejected') then
            return jsonb_build_object(
                'changed', false,
                'related_memory_id', v_request.related_memory_id,
                'request', jsonb_build_object(
                    'id', v_request.id,
                    'status', v_request.status,
                    'memory_id', v_request.memory_id,
                    'reviewed_at', v_request.reviewed_at
                )
            );
        end if;

        if v_request.status not in ('pending', 'conflict') then
            raise exception 'memory_request_not_pending';
        end if;

        if v_request.status = 'conflict' then
            update public.memory_requests
            set status = 'pending', updated_at = now()
            where id = v_request.id;
        end if;

        v_result := public.review_memory_request_v2(
            p_request_id,
            v_action,
            p_content,
            p_title,
            p_tags,
            p_importance,
            p_content_hash,
            v_reviewed_by,
            v_review_note,
            p_memory_key,
            p_update_mode
        );

        if coalesce((v_result->>'changed')::boolean, false) then
            insert into public.memory_request_review_events (
                request_id,
                action,
                from_status,
                to_status,
                target_memory_id,
                result_memory_id,
                reviewed_by,
                review_note,
                review_snapshot
            ) values (
                v_request.id,
                v_action,
                v_from_status,
                v_result->'request'->>'status',
                v_request.related_memory_id,
                nullif(v_result->'request'->>'memory_id', '')::integer,
                v_reviewed_by,
                v_review_note,
                jsonb_strip_nulls(jsonb_build_object(
                    'content', p_content,
                    'title', p_title,
                    'tags', p_tags,
                    'importance', p_importance,
                    'memory_key', p_memory_key,
                    'update_mode', p_update_mode
                ))
            );
        end if;

        return v_result || jsonb_build_object(
            'related_memory_id', v_request.related_memory_id
        );
    end if;

    if v_request.status not in ('pending', 'conflict') then
        raise exception 'memory_request_not_pending';
    end if;

    if p_related_memory_id is null then
        raise exception 'memory_request_related_memory_required';
    end if;

    -- Use the same memory-key lock order as mutable-fact replacement before
    -- taking the target lock, avoiding cross-flow races and deadlocks.
    select memory_key into v_target_memory_key
    from public.memories
    where id = p_related_memory_id;

    if not found then
        raise exception 'memory_request_related_memory_not_found';
    end if;

    if v_target_memory_key is not null then
        perform pg_advisory_xact_lock(
            hashtextextended('memory-key:' || v_target_memory_key, 0)
        );
    end if;

    -- Serialize every relational decision around the selected durable memory.
    perform pg_advisory_xact_lock(
        hashtextextended('memory-review-target:' || p_related_memory_id::text, 0)
    );

    select * into v_target
    from public.memories
    where id = p_related_memory_id
    for update;

    if not found then
        raise exception 'memory_request_related_memory_not_found';
    end if;
    if v_target.verified <> 'verified' or v_target.is_active is not true then
        raise exception 'memory_request_related_memory_inactive';
    end if;

    if v_action in ('duplicate', 'conflict') then
        if p_content is not null
           or p_title is not null
           or p_tags is not null
           or p_importance is not null
           or p_content_hash is not null
           or p_memory_key is not null
           or p_update_mode is not null then
            raise exception 'memory_request_relation_disallows_edits';
        end if;

        if v_action = 'conflict'
           and v_request.status = 'conflict'
           and v_request.related_memory_id = v_target.id then
            return jsonb_build_object(
                'changed', false,
                'related_memory_id', v_target.id,
                'request', jsonb_build_object(
                    'id', v_request.id,
                    'status', v_request.status,
                    'memory_id', v_request.memory_id,
                    'reviewed_at', v_request.reviewed_at
                )
            );
        end if;

        update public.memory_requests
        set
            status = v_action,
            memory_id = case when v_action = 'duplicate' then v_target.id else null end,
            related_memory_id = v_target.id,
            reviewed_at = now(),
            reviewed_by = v_reviewed_by,
            review_note = v_review_note,
            updated_at = now()
        where id = v_request.id
        returning * into v_request;

        insert into public.memory_request_review_events (
            request_id,
            action,
            from_status,
            to_status,
            target_memory_id,
            result_memory_id,
            reviewed_by,
            review_note,
            review_snapshot
        ) values (
            v_request.id,
            v_action,
            v_from_status,
            v_request.status,
            v_target.id,
            case when v_action = 'duplicate' then v_target.id else null end,
            v_reviewed_by,
            v_review_note,
            jsonb_build_object(
                'request_content_hash', v_request.content_hash,
                'target_content_hash', v_target.content_hash
            )
        );

        return jsonb_build_object(
            'changed', true,
            'related_memory_id', v_target.id,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'memory_id', v_request.memory_id,
                'reviewed_at', v_request.reviewed_at
            )
        );
    end if;

    -- A merge is never inferred. The user supplies the final merged wording,
    -- while the selected target is soft-deactivated and linked to the result.
    v_content := trim(coalesce(p_content, ''));
    v_title := nullif(left(trim(coalesce(p_title, '')), 100), '');
    v_tags := coalesce(p_tags, '{}'::text[]);
    v_importance := coalesce(p_importance, 5);
    v_content_hash := trim(coalesce(p_content_hash, ''));

    if char_length(v_content) not between 5 and 600 then
        raise exception 'memory_request_invalid_content';
    end if;
    if cardinality(v_tags) > 5 then
        raise exception 'memory_request_invalid_tags';
    end if;
    if v_importance not between 1 and 10 then
        raise exception 'memory_request_invalid_importance';
    end if;
    if char_length(v_content_hash) <> 64 then
        raise exception 'memory_request_invalid_hash';
    end if;
    if p_memory_key is not null or p_update_mode is not null then
        raise exception 'memory_request_merge_disallows_update_mode';
    end if;
    if v_content_hash = v_target.content_hash then
        raise exception 'memory_request_merge_unchanged';
    end if;
    if exists (
        select 1 from public.memories
        where content_hash = v_content_hash
          and id <> v_target.id
    ) then
        raise exception 'memory_request_merge_content_exists';
    end if;

    update public.memories
    set
        is_active = false,
        superseded_at = now(),
        superseded_by_memory_id = null
    where id = v_target.id;

    insert into public.memories (
        content,
        title,
        tags,
        heat,
        importance,
        embedding,
        source,
        verified,
        is_active,
        recall_count,
        assistant_id,
        digest_run_id,
        source_first_message_id,
        source_last_message_id,
        confidence,
        content_hash,
        memory_key,
        supersedes_memory_id,
        superseded_by_memory_id,
        superseded_at
    ) values (
        v_content,
        v_title,
        v_tags,
        greatest(coalesce(v_target.heat, 0), least(greatest(v_importance * 10.0, 0), 100)),
        v_importance,
        null,
        'ai_tool_request',
        'verified',
        true,
        coalesce(v_target.recall_count, 0),
        coalesce(v_target.assistant_id, v_request.assistant_id),
        null,
        case
            when v_target.source_first_message_id is null then v_request.source_message_id
            when v_request.source_message_id is null then v_target.source_first_message_id
            else least(v_target.source_first_message_id, v_request.source_message_id)
        end,
        case
            when v_target.source_last_message_id is null then v_request.source_message_id
            when v_request.source_message_id is null then v_target.source_last_message_id
            else greatest(v_target.source_last_message_id, v_request.source_message_id)
        end,
        greatest(coalesce(v_target.confidence, 0), 1.0),
        v_content_hash,
        v_target.memory_key,
        v_target.id,
        null,
        null
    )
    returning id into v_result_memory_id;

    update public.memories
    set superseded_by_memory_id = v_result_memory_id
    where id = v_target.id;

    update public.memory_requests
    set
        content = v_content,
        title = v_title,
        tags = v_tags,
        importance = v_importance,
        content_hash = v_content_hash,
        status = 'merged',
        memory_id = v_result_memory_id,
        related_memory_id = v_target.id,
        reviewed_at = now(),
        reviewed_by = v_reviewed_by,
        review_note = v_review_note,
        updated_at = now()
    where id = v_request.id
    returning * into v_request;

    insert into public.memory_request_review_events (
        request_id,
        action,
        from_status,
        to_status,
        target_memory_id,
        result_memory_id,
        reviewed_by,
        review_note,
        review_snapshot
    ) values (
        v_request.id,
        'merge',
        v_from_status,
        v_request.status,
        v_target.id,
        v_result_memory_id,
        v_reviewed_by,
        v_review_note,
        jsonb_build_object(
            'content', v_content,
            'title', v_title,
            'tags', v_tags,
            'importance', v_importance,
            'content_hash', v_content_hash
        )
    );

    return jsonb_build_object(
        'changed', true,
        'related_memory_id', v_target.id,
        'request', jsonb_build_object(
            'id', v_request.id,
            'status', v_request.status,
            'memory_id', v_request.memory_id,
            'reviewed_at', v_request.reviewed_at
        )
    );
end;
$function$;

-- ---------------------------------------------------------------------------
-- 3. Rebuild the write path RPCs without the four retired request parameters.
--    The exact old signatures are dropped first; CASCADE is never used.
-- ---------------------------------------------------------------------------
drop function if exists public.create_memory_request_v4(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text, text, text, smallint, jsonb, text, text, integer,
    text, text[], text, text, text[]
);

create or replace function public.create_memory_request_v4(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_source_type text, p_source text,
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
        source_type,
        recall_scene,recall_tags,evidence_start_time,evidence_end_time,source_time,evidence_time_precision
    ) values (
        p_assistant_id,nullif(trim(p_conversation_id),''),p_source_message_id,p_content,p_title,p_tags,p_importance,p_reason,p_content_hash,
        p_idempotency_key,'pending',p_source,p_memory_key,p_update_mode,
        case when p_source_message_id is null then '{}'::bigint[] else array[p_source_message_id] end,
        p_continuity_type,p_thread_state,null,1,p_continuity_data,
        -- source_type is optional and stays NULL when unprovided; a blank
        -- string is normalized to NULL and never guessed into a real value.
        nullif(btrim(coalesce(p_source_type,'')),''),
        v_recall_scene,v_recall_tags,v_evidence_time,v_evidence_time,v_evidence_time,v_evidence_precision
    )
    returning * into v_request;
    return jsonb_build_object('created',true,'request',to_jsonb(v_request));
end;
$function$;

drop function if exists public.write_memory_direct_v1(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text, text, text, smallint, jsonb, text, text, integer,
    text, text[], text, text, text, text[], extensions.vector
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

-- Same signature; the staged candidate no longer writes the retired request
-- columns, so pending rows created by the digest keep their review flow.
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
    if v_delta = 1 and v_type in ('moment','thread','inside_joke')
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

-- Same signature and trigger attachment; the metadata copy no longer touches
-- the retired columns on either side.
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
            source = case when new.source = 'daily_digest' then 'daily_digest' else memory.source end
        where memory.id = new.memory_id;
        update public.memory_continuity_objects
        set updated_at = now()
        where continuity_id = new.continuity_id;
    end if;
    return new;
end;
$function$;

-- ---------------------------------------------------------------------------
-- 4. Drop the retired columns now that every dependency has been removed.
--    The stored values are discarded permanently by confirmed decision.
-- ---------------------------------------------------------------------------
alter table public.memories
    drop column layer,
    drop column emotion_weight,
    drop column subject,
    drop column participants,
    drop column continuity_value,
    drop column retention_class;

alter table public.memory_requests
    drop column subject,
    drop column participants,
    drop column continuity_value,
    drop column retention_class;

-- ---------------------------------------------------------------------------
-- 5. Grants: the two RPCs whose signatures changed need fresh grants; the
--    rebuilt same-signature functions keep theirs from earlier migrations.
-- ---------------------------------------------------------------------------
revoke all on function public.create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text[]) from public,anon,authenticated;
revoke all on function public.write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text,text[],extensions.vector) from public,anon,authenticated;
grant execute on function public.create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text[]) to service_role;
grant execute on function public.write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,text,text,text[],extensions.vector) to service_role;

commit;
