-- Atomic user review for pending AI memory applications.
-- Approval creates or verifies a durable memory; rejection retains the request
-- for audit. chat_messages is never modified.

-- The original memories table limits source to a fixed allowlist. Extend it
-- explicitly so an approved tool request can be persisted without weakening
-- the other source values.
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
        'ai_tool_request'
    ));

-- A reviewed duplicate is retained as `merged` and may share a content hash
-- with the original application. Pending/approved applications remain unique.
drop index if exists public.memory_requests_active_content_idx;
create unique index memory_requests_active_content_idx
    on public.memory_requests (assistant_id, content_hash)
    where status in ('pending', 'approved');

create or replace function public.review_memory_request(
    p_request_id bigint,
    p_action text,
    p_content text default null,
    p_title text default null,
    p_tags text[] default null,
    p_importance integer default null,
    p_content_hash text default null,
    p_reviewed_by text default 'gateway_admin',
    p_review_note text default null
)
returns jsonb
language plpgsql
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_memory_id integer;
    v_content text;
    v_title text;
    v_tags text[];
    v_importance integer;
    v_content_hash text;
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

    -- Repeated identical review calls are safe and return the existing result.
    if (p_action = 'approve' and v_request.status in ('approved', 'merged'))
       or (p_action = 'reject' and v_request.status = 'rejected') then
        return jsonb_build_object(
            'changed', false,
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
        content_hash
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
        v_content_hash
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
        confidence = greatest(public.memories.confidence, excluded.confidence)
    returning id into v_memory_id;

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
        'request', jsonb_build_object(
            'id', v_request.id,
            'status', v_request.status,
            'memory_id', v_request.memory_id,
            'reviewed_at', v_request.reviewed_at
        )
    );
end;
$function$;

revoke all on function public.review_memory_request(
    bigint, text, text, text, text[], integer, text, text, text
) from public, anon, authenticated;

grant execute on function public.review_memory_request(
    bigint, text, text, text, text[], integer, text, text, text
) to service_role;

