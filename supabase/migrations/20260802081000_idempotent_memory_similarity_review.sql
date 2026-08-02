-- Idempotent retry wrapper for relational memory review decisions.
-- If the first HTTP response is lost, repeating the same terminal action with
-- the same selected memory returns the existing result without a second event.
-- chat_messages remains an immutable read-only source and is not modified.

create or replace function public.review_memory_request_v4(
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
    v_action text := lower(trim(coalesce(p_action, '')));
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

    if (v_action = 'approve' and v_request.status in ('approved', 'merged'))
       or (v_action = 'reject' and v_request.status = 'rejected')
       or (
            v_action = 'merge'
            and v_request.status = 'merged'
            and v_request.related_memory_id = p_related_memory_id
       )
       or (
            v_action = 'duplicate'
            and v_request.status = 'duplicate'
            and v_request.related_memory_id = p_related_memory_id
       )
       or (
            v_action = 'conflict'
            and v_request.status = 'conflict'
            and v_request.related_memory_id = p_related_memory_id
       ) then
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

    return public.review_memory_request_v3(
        p_request_id,
        v_action,
        p_content,
        p_title,
        p_tags,
        p_importance,
        p_content_hash,
        p_reviewed_by,
        p_review_note,
        p_memory_key,
        p_update_mode,
        p_related_memory_id
    );
end;
$function$;

revoke all on function public.review_memory_request_v4(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer
) from public, anon, authenticated;

grant execute on function public.review_memory_request_v4(
    bigint, text, text, text, text[], integer, text, text, text, text, text, integer
) to service_role;

