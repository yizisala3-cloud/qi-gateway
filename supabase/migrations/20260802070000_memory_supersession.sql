-- Auditable soft replacement for mutable facts such as progress and status.
-- chat_messages remains an immutable read-only source and is not modified.

alter table public.memory_requests
    add column if not exists memory_key text,
    add column if not exists update_mode text not null default 'append';

alter table public.memory_requests
    drop constraint if exists memory_requests_memory_key_format,
    drop constraint if exists memory_requests_update_mode_values,
    drop constraint if exists memory_requests_replace_requires_key;

alter table public.memory_requests
    add constraint memory_requests_memory_key_format
        check (
            memory_key is null
            or memory_key ~ '^[a-z0-9][a-z0-9._:/-]{2,119}$'
        ),
    add constraint memory_requests_update_mode_values
        check (update_mode in ('append', 'replace')),
    add constraint memory_requests_replace_requires_key
        check (
            (update_mode = 'append' and memory_key is null)
            or (update_mode = 'replace' and memory_key is not null)
        );

alter table public.memories
    add column if not exists memory_key text,
    add column if not exists supersedes_memory_id integer,
    add column if not exists superseded_by_memory_id integer,
    add column if not exists superseded_at timestamptz;

alter table public.memories
    drop constraint if exists memories_memory_key_format,
    drop constraint if exists memories_supersedes_memory_fkey,
    drop constraint if exists memories_superseded_by_memory_fkey;

alter table public.memories
    add constraint memories_memory_key_format
        check (
            memory_key is null
            or memory_key ~ '^[a-z0-9][a-z0-9._:/-]{2,119}$'
        ),
    add constraint memories_supersedes_memory_fkey
        foreign key (supersedes_memory_id)
        references public.memories(id)
        on delete set null,
    add constraint memories_superseded_by_memory_fkey
        foreign key (superseded_by_memory_id)
        references public.memories(id)
        on delete set null;

create unique index if not exists memories_active_memory_key_idx
    on public.memories (memory_key)
    where memory_key is not null
      and is_active = true
      and verified = 'verified';

create index if not exists memories_supersedes_memory_idx
    on public.memories (supersedes_memory_id)
    where supersedes_memory_id is not null;

create index if not exists memories_superseded_by_memory_idx
    on public.memories (superseded_by_memory_id)
    where superseded_by_memory_id is not null;

create index if not exists memory_requests_memory_key_idx
    on public.memory_requests (memory_key, created_at desc)
    where memory_key is not null;

comment on column public.memory_requests.memory_key is
    'Stable ASCII topic key proposed for a user-reviewed mutable fact replacement.';
comment on column public.memory_requests.update_mode is
    'append creates an independent memory; replace soft-supersedes the active memory_key version.';
comment on column public.memories.memory_key is
    'Stable topic key; only one verified active version may exist for each key.';
comment on column public.memories.supersedes_memory_id is
    'Previous memory version replaced by this row.';
comment on column public.memories.superseded_by_memory_id is
    'New active memory version that replaced this row.';

create or replace function public.create_memory_request_v2(
    p_assistant_id text,
    p_conversation_id text,
    p_source_message_id bigint,
    p_content text,
    p_title text,
    p_tags text[],
    p_importance integer,
    p_reason text,
    p_content_hash text,
    p_idempotency_key text,
    p_rate_limit integer default 6,
    p_memory_key text default null,
    p_update_mode text default 'append'
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_recent_count integer := 0;
    v_memory_key text := nullif(lower(trim(coalesce(p_memory_key, ''))), '');
    v_update_mode text := lower(trim(coalesce(p_update_mode, 'append')));
begin
    if char_length(trim(coalesce(p_assistant_id, ''))) < 1
       or char_length(p_assistant_id) > 160 then
        raise exception 'memory_request_invalid_assistant';
    end if;
    if char_length(trim(coalesce(p_content, ''))) not between 5 and 600 then
        raise exception 'memory_request_invalid_content';
    end if;
    if char_length(trim(coalesce(p_reason, ''))) not between 3 and 500 then
        raise exception 'memory_request_invalid_reason';
    end if;
    if p_importance not between 1 and 10 then
        raise exception 'memory_request_invalid_importance';
    end if;
    if char_length(trim(coalesce(p_content_hash, ''))) <> 64 then
        raise exception 'memory_request_invalid_hash';
    end if;
    if char_length(trim(coalesce(p_idempotency_key, ''))) not between 8 and 128 then
        raise exception 'memory_request_invalid_idempotency_key';
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

    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id, 0));

    select * into v_request
    from public.memory_requests
    where idempotency_key = p_idempotency_key
       or (
            assistant_id = p_assistant_id
            and content_hash = p_content_hash
            and status in ('pending', 'approved', 'merged')
       )
    order by (idempotency_key = p_idempotency_key) desc, id desc
    limit 1;

    if found then
        return jsonb_build_object(
            'created', false,
            'request', jsonb_build_object(
                'id', v_request.id,
                'status', v_request.status,
                'created_at', v_request.created_at,
                'memory_key', v_request.memory_key,
                'update_mode', v_request.update_mode
            )
        );
    end if;

    select count(*) into v_recent_count
    from public.memory_requests
    where assistant_id = p_assistant_id
      and source = 'orangechat_plugin'
      and created_at >= now() - interval '1 minute';

    if v_recent_count >= least(greatest(coalesce(p_rate_limit, 6), 1), 60) then
        raise exception using
            errcode = 'P0001',
            message = 'memory_request_rate_limited';
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
        update_mode
    ) values (
        p_assistant_id,
        nullif(trim(p_conversation_id), ''),
        p_source_message_id,
        trim(p_content),
        nullif(trim(p_title), ''),
        coalesce(p_tags, '{}'::text[]),
        p_importance,
        trim(p_reason),
        p_content_hash,
        p_idempotency_key,
        'pending',
        'orangechat_plugin',
        v_memory_key,
        v_update_mode
    )
    returning * into v_request;

    return jsonb_build_object(
        'created', true,
        'request', jsonb_build_object(
            'id', v_request.id,
            'status', v_request.status,
            'created_at', v_request.created_at,
            'memory_key', v_request.memory_key,
            'update_mode', v_request.update_mode
        )
    );
end;
$function$;

revoke all on function public.create_memory_request_v2(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text
) from public, anon, authenticated;

grant execute on function public.create_memory_request_v2(
    text, text, bigint, text, text, text[], integer, text, text, text,
    integer, text, text
) to service_role;

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
        layer,
        embedding,
        source,
        verified,
        is_active,
        emotion_weight,
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
        case when v_importance >= 8 then '场景' else '碎片' end,
        null,
        'ai_tool_request',
        'verified',
        true,
        0.5,
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
        layer = excluded.layer,
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

revoke all on function public.review_memory_request_v2(
    bigint, text, text, text, text[], integer, text, text, text, text, text
) from public, anon, authenticated;

grant execute on function public.review_memory_request_v2(
    bigint, text, text, text, text[], integer, text, text, text, text, text
) to service_role;

