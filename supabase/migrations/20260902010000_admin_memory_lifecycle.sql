-- Admin memory lifecycle: user-authored formal memories, full editing,
-- type-change versioning, last type-change undo, and natural-archive restore.
--
-- Five purpose-built atomic RPCs replace generic table PATCHes for every
-- memory-authoring flow. The browser never controls assistant_id, source,
-- verified, is_active, heat, content_hash, continuity_id, or embeddings:
-- each RPC derives or guards them server-side inside one transaction. The
-- Python gateway computes content hashes with the project-wide formula and
-- generates recall vectors BEFORE calling these RPCs, so a failed embedding
-- never leaves a half-written formal memory.
--
-- public.chat_messages stays a select-only evidence source and is never
-- altered. No new table, no delete-pending state, no scheduled cleanup:
-- version history keeps exactly the current version and its direct parent.

begin;

-- ---------------------------------------------------------------------------
-- Shared guard: a formal memory row is editable only while it is the current
-- version -- active, verified, and not replaced by a newer version.
-- ---------------------------------------------------------------------------
create or replace function public.admin_memory_is_current(p_memory public.memories)
returns boolean
language sql
immutable
set search_path to 'pg_catalog'
as $function$
    select p_memory.is_active is true
       and p_memory.verified = 'verified'
       and p_memory.superseded_by_memory_id is null;
$function$;

-- Reap a continuity object that no retained memory, relation, or request
-- needs. Append-only review history is never deleted; the nullable request
-- reference is released instead. Objects still referenced by
-- memory_relations are kept untouched.
create or replace function public.admin_memory_reap_continuity_object(
    p_continuity_id uuid
)
returns void
language plpgsql
set search_path to 'public'
as $function$
begin
    if p_continuity_id is null then
        return;
    end if;
    if exists (
        select 1 from public.memories as memory
        where memory.continuity_id = p_continuity_id
    ) then
        return;
    end if;
    if exists (
        select 1 from public.memory_relations as relation
        where relation.from_continuity_id = p_continuity_id
           or relation.to_continuity_id = p_continuity_id
    ) then
        return;
    end if;
    update public.memory_requests as request
        set continuity_id = null
        where request.continuity_id = p_continuity_id;
    delete from public.memory_continuity_objects as object
        where object.continuity_id = p_continuity_id;
end;
$function$;

-- Derive the evidence window from the immutable chat log. Only messages
-- that exist and belong to the assistant are stored; with no surviving
-- evidence every evidence time stays NULL -- nothing is guessed from
-- created_at, and no evidence time is fabricated.
create or replace function public.admin_memory_resolve_evidence(
    p_assistant_id text,
    p_evidence_message_ids bigint[]
)
returns jsonb
language plpgsql
stable
set search_path to 'public'
as $function$
declare
    v_ids bigint[];
    v_start timestamptz;
    v_end timestamptz;
begin
    if p_evidence_message_ids is null or cardinality(p_evidence_message_ids) = 0 then
        return jsonb_build_object(
            'ids', null::bigint[],
            'start', null::timestamptz,
            'end', null::timestamptz,
            'precision', null::text
        );
    end if;
    select coalesce(array_agg(message.id order by message.id), '{}'::bigint[]),
           min(message.created_at),
           max(message.created_at)
    into v_ids, v_start, v_end
    from public.chat_messages as message
    where message.id = any(p_evidence_message_ids)
      and message.assistant_id = p_assistant_id;
    return jsonb_build_object(
        'ids', case when cardinality(v_ids) = 0 then null else v_ids end,
        'start', v_start,
        'end', v_end,
        -- The chat client stores wall-clock timestamps down to the minute,
        -- so evidence precision is 'minute' whenever evidence exists.
        'precision', case when v_end is null then null else 'minute' end
    );
end;
$function$;

-- Event-time normalization shared by create/edit/change: a date-only value
-- with 'day' precision means a calendar day in Asia/Shanghai, matching the
-- digest pipeline; anything else is a plain timestamptz cast.
create or replace function public.admin_memory_normalize_event_time(
    p_memory_time text,
    p_time_precision text
)
returns timestamptz
language plpgsql
immutable
set search_path to 'public'
as $function$
begin
    if nullif(btrim(coalesce(p_memory_time, '')), '') is null then
        return null;
    end if;
    if p_time_precision = 'day'
       and btrim(p_memory_time) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then
        return btrim(p_memory_time)::date::timestamp at time zone 'Asia/Shanghai';
    end if;
    return btrim(p_memory_time)::timestamptz;
end;
$function$;

-- The gateway layer owns tag hygiene (strip, dedupe, 200-char cap); these
-- RPCs refuse to persist anything that violates the contract anyway.
create or replace function public.admin_memory_tags_ok(p_tags text[])
returns boolean
language sql
immutable
set search_path to 'pg_catalog'
as $function$
    select not exists (
        select 1
        from unnest(coalesce(p_tags, '{}'::text[])) as tag(value)
        where btrim(tag.value) = ''
           or btrim(tag.value) is distinct from tag.value
           or char_length(tag.value) > 200
    );
$function$;

-- Rebuild a bigint[] from the jsonb evidence object. jsonb text rendering
-- ([1, 2]) is not a valid array literal, so extraction goes through
-- jsonb_array_elements_text instead of a plain cast.
create or replace function public.admin_memory_ids_from_evidence(p_evidence jsonb)
returns bigint[]
language sql
immutable
set search_path to 'pg_catalog'
as $function$
    select coalesce(array_agg(entry.value::bigint order by entry.value::bigint),
                    '{}'::bigint[])
    from jsonb_array_elements_text(
        case
            when p_evidence is null or jsonb_typeof(p_evidence->'ids') <> 'array'
            then '[]'::jsonb
            else p_evidence->'ids'
        end
    ) as entry(value)
$function$;

-- ---------------------------------------------------------------------------
-- 1. Create a user-authored formal memory. Writes public.memories directly
--    and never enters the review queue. The gateway resolves assistant_id
--    and computes the content hash; a JSON array arrives for
--    p_recall_embedding via the PostgREST vector cast.
-- ---------------------------------------------------------------------------
create or replace function public.create_admin_memory_v1(
    p_assistant_id text,
    p_content text,
    p_content_hash text,
    p_title text,
    p_tags text[],
    p_importance integer,
    p_source_type text,
    p_memory_time text,
    p_time_precision text,
    p_recall_scene text,
    p_recall_tags text[],
    p_recall_embedding extensions.vector,
    p_continuity_type text,
    p_thread_state text,
    p_continuity_data jsonb,
    p_evidence_message_ids bigint[]
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_assistant_id text := nullif(btrim(coalesce(p_assistant_id, '')), '');
    v_content text := btrim(coalesce(p_content, ''));
    v_title text := nullif(left(btrim(coalesce(p_title, '')), 100), '');
    v_recall_scene text := nullif(btrim(coalesce(p_recall_scene, '')), '');
    v_continuity_id uuid;
    v_evidence jsonb;
    v_memory public.memories%rowtype;
begin
    if v_assistant_id is null then
        raise exception 'admin_memory_assistant_required';
    end if;
    if char_length(v_content) not between 5 and 600 then
        raise exception 'admin_memory_invalid_content';
    end if;
    if p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'admin_memory_invalid_content_hash';
    end if;
    if p_importance is null or p_importance not between 1 and 10 then
        raise exception 'admin_memory_invalid_importance';
    end if;
    if not public.admin_memory_tags_ok(p_tags)
       or not public.admin_memory_tags_ok(p_recall_tags) then
        raise exception 'admin_memory_invalid_tags';
    end if;
    if p_continuity_type not in (
        'moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'
    ) then
        raise exception 'admin_memory_invalid_type';
    end if;
    -- Six-class memories always carry a fully validated v1 structure; the
    -- admin console never creates unclassified rows.
    if not public.validate_continuity_data(p_continuity_type, p_thread_state, p_continuity_data) then
        raise exception 'admin_memory_invalid_continuity_data';
    end if;
    if p_time_precision is not null
       and p_time_precision not in ('minute', 'hour', 'day', 'approximate', 'unknown') then
        raise exception 'admin_memory_invalid_time_precision';
    end if;
    if p_source_type is not null
       and p_source_type not in (
           'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
           'roleplay', 'tool_result', 'system_meta', 'unknown'
       ) then
        raise exception 'admin_memory_invalid_source_type';
    end if;
    -- A scene-carrying write must arrive with its vector: the gateway embeds
    -- before this transaction, so a null vector under a live scene means the
    -- caller tried to skip generation.
    if v_recall_scene is not null and p_recall_embedding is null then
        raise exception 'admin_memory_recall_vector_missing';
    end if;

    perform pg_advisory_xact_lock(hashtextextended(v_assistant_id, 0));

    insert into public.memory_continuity_objects (assistant_id)
    values (v_assistant_id)
    returning continuity_id into v_continuity_id;

    v_evidence := public.admin_memory_resolve_evidence(v_assistant_id, p_evidence_message_ids);

    insert into public.memories (
        content, title, tags, heat, importance, embedding,
        source, verified, is_active, recall_count,
        assistant_id, digest_run_id, confidence, content_hash,
        memory_key, supersedes_memory_id, superseded_by_memory_id, superseded_at,
        continuity_id, continuity_type, continuity_schema_version, continuity_data,
        source_type, thread_state,
        evidence_message_ids, source_time,
        memory_time, time_precision,
        evidence_start_time, evidence_end_time, evidence_time_precision,
        recall_scene, recall_tags, recall_embedding
    ) values (
        v_content, v_title,
        coalesce(p_tags, '{}'::text[]),
        50.0, p_importance, null,
        'manual', 'verified', true, 0,
        v_assistant_id, null, 1.0, p_content_hash,
        null, null, null, null,
        v_continuity_id, p_continuity_type, 1, p_continuity_data,
        nullif(btrim(coalesce(p_source_type, '')), ''),
        case when p_continuity_type = 'thread' then p_thread_state else null end,
        public.admin_memory_ids_from_evidence(v_evidence),
        -- source_time is the AI pipeline's own evidence clock; the admin
        -- console has no AI source, so it always stays NULL.
        null,
        public.admin_memory_normalize_event_time(p_memory_time, p_time_precision),
        coalesce(nullif(btrim(coalesce(p_time_precision, '')), ''), 'unknown'),
        (v_evidence->>'start')::timestamptz,
        (v_evidence->>'end')::timestamptz,
        (v_evidence->>'precision'),
        v_recall_scene,
        p_recall_tags,
        case when v_recall_scene is null then null else p_recall_embedding end
    )
    returning * into v_memory;

    return jsonb_build_object(
        'memory_id', v_memory.id,
        'continuity_id', v_memory.continuity_id,
        'source', v_memory.source,
        'verified', v_memory.verified,
        'is_active', v_memory.is_active,
        'heat', v_memory.heat
    );
end;
$function$;

-- ---------------------------------------------------------------------------
-- 2. Edit a formal memory. Ordinary-field edits update the row in place with
--    no version history and no undo. Same-type continuity_data edits pass
--    the full six-class validation. An unclassified legacy row may edit
--    ordinary fields without being forced into a class, and may adopt a
--    class exactly once -- which requires its complete required structure.
-- ---------------------------------------------------------------------------
create or replace function public.edit_admin_memory_v1(
    p_memory_id integer,
    p_patch jsonb,
    p_content_hash text,
    p_recall_embedding extensions.vector,
    p_assistant_id text default null
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_memory public.memories%rowtype;
    v_new_type text;
    v_new_thread_state text;
    v_continuity_data jsonb;
    v_write_continuity boolean := false;
    v_content text;
    v_evidence jsonb;
    v_updated public.memories%rowtype;
    v_new_continuity_id uuid;
begin
    if p_patch is null or jsonb_typeof(p_patch) <> 'object' then
        raise exception 'admin_memory_invalid_patch';
    end if;

    select * into v_memory
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if not public.admin_memory_is_current(v_memory) then
        raise exception 'admin_memory_not_editable';
    end if;

    v_new_type := nullif(btrim(coalesce(p_patch->>'continuity_type', '')), '');
    if v_new_type is not null
       and v_new_type not in (
           'moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'
       ) then
        raise exception 'admin_memory_invalid_type';
    end if;
    if v_new_type is not null and v_memory.continuity_type is not null
       and v_new_type <> v_memory.continuity_type then
        -- Switching an existing class must go through the versioned
        -- type-change flow, never through an in-place edit.
        raise exception 'admin_memory_type_change_forbidden';
    end if;
    v_new_type := coalesce(v_new_type, v_memory.continuity_type);

    if v_new_type is null then
        -- Unclassified legacy row: ordinary-field edits never trigger
        -- continuity validation, and continuity structure cannot be
        -- attached without adopting a class first.
        if p_patch ? 'continuity_data' or p_patch ? 'thread_state' then
            raise exception 'admin_memory_class_required';
        end if;
    elsif p_patch ? 'continuity_data'
       or p_patch ? 'thread_state'
       or (p_patch ? 'continuity_type' and v_memory.continuity_type is null) then
        -- The continuity structure is being written (same-type edit or a
        -- one-time class adoption), so the complete type validation runs.
        v_write_continuity := true;
        v_new_thread_state := coalesce(
            nullif(btrim(coalesce(p_patch->>'thread_state', '')), ''),
            v_memory.thread_state
        );
        if p_patch ? 'continuity_data' then
            v_continuity_data := p_patch->'continuity_data';
        elsif v_memory.continuity_data is not null then
            v_continuity_data := v_memory.continuity_data;
        else
            raise exception 'admin_memory_invalid_continuity_data';
        end if;
        if not public.validate_continuity_data(v_new_type, v_new_thread_state, v_continuity_data) then
            raise exception 'admin_memory_invalid_continuity_data';
        end if;
    end if;
    -- else: ordinary fields only -- continuity fields are left untouched and
    -- unvalidated, so incomplete legacy rows are never forced to complete.

    if p_patch ? 'content' then
        v_content := btrim(coalesce(p_patch->>'content', ''));
        if char_length(v_content) not between 5 and 600 then
            raise exception 'admin_memory_invalid_content';
        end if;
        if p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$' then
            raise exception 'admin_memory_invalid_content_hash';
        end if;
        -- An in-place edit must not steal another memory's content_hash
        -- (table-wide unique constraint); refuse with a stable code.
        if exists (
            select 1 from public.memories as other
            where other.content_hash = p_content_hash
              and other.id <> v_memory.id
        ) then
            raise exception 'admin_memory_content_exists';
        end if;
    elsif p_content_hash is not null then
        raise exception 'admin_memory_invalid_content_hash';
    end if;

    if p_patch ? 'recall_scene' then
        -- The patch is authoritative for the whole recall pair: an empty
        -- scene clears the vector, a live scene must arrive embedded.
        if nullif(btrim(coalesce(p_patch->>'recall_scene', '')), '') is not null
           and p_recall_embedding is null then
            raise exception 'admin_memory_recall_vector_missing';
        end if;
    elsif p_recall_embedding is not null then
        raise exception 'admin_memory_recall_vector_missing';
    end if;

    if p_patch ? 'importance'
       and (p_patch->>'importance')::integer not between 1 and 10 then
        raise exception 'admin_memory_invalid_importance';
    end if;
    if p_patch ? 'time_precision'
       and nullif(btrim(coalesce(p_patch->>'time_precision', '')), '') is not null
       and p_patch->>'time_precision' not in ('minute', 'hour', 'day', 'approximate', 'unknown') then
        raise exception 'admin_memory_invalid_time_precision';
    end if;
    if p_patch ? 'source_type'
       and nullif(btrim(coalesce(p_patch->>'source_type', '')), '') is not null
       and p_patch->>'source_type' not in (
           'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
           'roleplay', 'tool_result', 'system_meta', 'unknown'
       ) then
        raise exception 'admin_memory_invalid_source_type';
    end if;
    if (p_patch ? 'tags'
        and not public.admin_memory_tags_ok((
            select array_agg(t.value order by t.value)
            from jsonb_array_elements_text(p_patch->'tags') as t(value)
       )))
       or (p_patch ? 'recall_tags'
        and not public.admin_memory_tags_ok((
            select array_agg(t.value order by t.value)
            from jsonb_array_elements_text(p_patch->'recall_tags') as t(value)
       ))) then
        raise exception 'admin_memory_invalid_tags';
    end if;

    if p_patch ? 'evidence_message_ids' then
        v_evidence := public.admin_memory_resolve_evidence(
            coalesce(v_memory.assistant_id, nullif(btrim(coalesce(p_assistant_id, '')), '')),
            (select array_agg((value)::bigint order by (value)::bigint)
             from jsonb_array_elements_text(p_patch->'evidence_message_ids') as entry(value)
             where entry.value ~ '^[0-9]+$')
        );
        -- Re-derived evidence replaces the old window wholesale; an empty
        -- list leaves no times behind.
    else
        v_evidence := null;
    end if;

    update public.memories as memory set
        title = case
            when p_patch ? 'title'
            then nullif(left(btrim(coalesce(p_patch->>'title', '')), 100), '')
            else memory.title end,
        content = case
            when p_patch ? 'content' then btrim(p_patch->>'content')
            else memory.content end,
        content_hash = case
            when p_patch ? 'content' then p_content_hash
            else memory.content_hash end,
        tags = case
            when p_patch ? 'tags'
            then coalesce((
                    select array_agg(distinct t.value)
                    from jsonb_array_elements_text(p_patch->'tags') as t(value)
                    where nullif(btrim(t.value), '') is not null
                 ), '{}'::text[])
            else memory.tags end,
        importance = case
            when p_patch ? 'importance' then (p_patch->>'importance')::integer
            else memory.importance end,
        source_type = case
            when p_patch ? 'source_type'
            then nullif(btrim(coalesce(p_patch->>'source_type', '')), '')
            else memory.source_type end,
        memory_time = case
            when p_patch ? 'memory_time'
            then public.admin_memory_normalize_event_time(
                     p_patch->>'memory_time',
                     case when p_patch ? 'time_precision'
                          then nullif(btrim(coalesce(p_patch->>'time_precision', '')), '')
                          else memory.time_precision end)
            else memory.memory_time end,
        time_precision = case
            when p_patch ? 'time_precision'
            then coalesce(nullif(btrim(coalesce(p_patch->>'time_precision', '')), ''), 'unknown')
            else memory.time_precision end,
        recall_scene = case
            when p_patch ? 'recall_scene'
            then nullif(btrim(coalesce(p_patch->>'recall_scene', '')), '')
            else memory.recall_scene end,
        recall_tags = case
            when p_patch ? 'recall_tags'
            then coalesce((
                    select array_agg(distinct t.value)
                    from jsonb_array_elements_text(p_patch->'recall_tags') as t(value)
                    where nullif(btrim(t.value), '') is not null
                 ), '{}'::text[])
            else memory.recall_tags end,
        recall_embedding = case
            when p_patch ? 'recall_scene'
            then case
                when nullif(btrim(coalesce(p_patch->>'recall_scene', '')), '') is null
                then null
                else p_recall_embedding end
            else memory.recall_embedding end,
        evidence_message_ids = case
            when p_patch ? 'evidence_message_ids' then public.admin_memory_ids_from_evidence(v_evidence)
            else memory.evidence_message_ids end,
        evidence_start_time = case
            when p_patch ? 'evidence_message_ids' then (v_evidence->>'start')::timestamptz
            else memory.evidence_start_time end,
        evidence_end_time = case
            when p_patch ? 'evidence_message_ids' then (v_evidence->>'end')::timestamptz
            else memory.evidence_end_time end,
        evidence_time_precision = case
            when p_patch ? 'evidence_message_ids' then (v_evidence->>'precision')
            else memory.evidence_time_precision end,
        thread_state = case
            when not v_write_continuity then memory.thread_state
            when v_new_type is distinct from 'thread' then null
            else nullif(btrim(coalesce(p_patch->>'thread_state', '')), '') end,
        continuity_type = case
            when v_write_continuity then v_new_type
            else memory.continuity_type end,
        continuity_schema_version = case
            when v_write_continuity then 1
            else memory.continuity_schema_version end,
        continuity_data = case
            when v_write_continuity then v_continuity_data
            else memory.continuity_data end
    where memory.id = v_memory.id
    returning * into v_updated;

    -- A newly adopted class needs a continuity identity allocated here. The
    -- gateway resolves the assistant for this purpose; nothing is invented.
    if v_write_continuity and v_updated.continuity_id is null then
        insert into public.memory_continuity_objects (assistant_id)
        values (coalesce(
            v_updated.assistant_id,
            nullif(btrim(coalesce(p_assistant_id, '')), ''),
            ''
        ))
        returning continuity_id into v_new_continuity_id;
        update public.memories
            set continuity_id = v_new_continuity_id
            where id = v_updated.id
            returning * into v_updated;
    end if;

    if v_updated.continuity_id is not null then
        update public.memory_continuity_objects
            set updated_at = now()
            where continuity_id = v_updated.continuity_id;
    end if;

    return jsonb_build_object('memory', to_jsonb(v_updated));
end;
$function$;

-- ---------------------------------------------------------------------------
-- 3. Change the continuity class: never overwrite in place. Insert a new
--    version, retire the old one, and physically drop the now-older
--    generation so only the current version and its direct parent remain.
--    One transaction: any failure rolls the whole chain back.
-- ---------------------------------------------------------------------------
create or replace function public.change_memory_type_v1(
    p_memory_id integer,
    p_content text,
    p_content_hash text,
    p_title text,
    p_tags text[],
    p_importance integer,
    p_source_type text,
    p_memory_time text,
    p_time_precision text,
    p_recall_scene text,
    p_recall_tags text[],
    p_recall_embedding extensions.vector,
    p_continuity_type text,
    p_thread_state text,
    p_continuity_data jsonb,
    p_evidence_message_ids bigint[]
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_source public.memories%rowtype;
    v_earlier public.memories%rowtype;
    v_new public.memories%rowtype;
    v_continuity_id uuid;
    v_content text := btrim(coalesce(p_content, ''));
    v_recall_scene text := nullif(btrim(coalesce(p_recall_scene, '')), '');
    v_evidence jsonb;
    v_removed_earlier boolean := false;
    v_same_hash boolean := false;
begin
    if char_length(v_content) not between 5 and 600 then
        raise exception 'admin_memory_invalid_content';
    end if;
    if p_content_hash is null or p_content_hash !~ '^[0-9a-f]{64}$' then
        raise exception 'admin_memory_invalid_content_hash';
    end if;
    if p_importance is null or p_importance not between 1 and 10 then
        raise exception 'admin_memory_invalid_importance';
    end if;
    if not public.admin_memory_tags_ok(p_tags)
       or not public.admin_memory_tags_ok(p_recall_tags) then
        raise exception 'admin_memory_invalid_tags';
    end if;
    if p_continuity_type not in (
        'moment', 'thread', 'episode', 'inside_joke', 'profile', 'interaction_rule'
    ) then
        raise exception 'admin_memory_invalid_type';
    end if;
    if not public.validate_continuity_data(p_continuity_type, p_thread_state, p_continuity_data) then
        raise exception 'admin_memory_invalid_continuity_data';
    end if;
    if p_time_precision is not null
       and p_time_precision not in ('minute', 'hour', 'day', 'approximate', 'unknown') then
        raise exception 'admin_memory_invalid_time_precision';
    end if;
    if p_source_type is not null
       and p_source_type not in (
           'natural_chat', 'persona_prompt', 'code', 'document', 'quote',
           'roleplay', 'tool_result', 'system_meta', 'unknown'
       ) then
        raise exception 'admin_memory_invalid_source_type';
    end if;
    if v_recall_scene is not null and p_recall_embedding is null then
        raise exception 'admin_memory_recall_vector_missing';
    end if;

    select * into v_source
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if not public.admin_memory_is_current(v_source) then
        raise exception 'admin_memory_not_editable';
    end if;
    if v_source.continuity_type is null then
        raise exception 'admin_memory_source_unclassified';
    end if;
    if p_continuity_type = v_source.continuity_type then
        raise exception 'admin_memory_type_unchanged';
    end if;

    -- Serialize same-identity operations (change/undo/restore) so two
    -- concurrent flows can never both believe they own the single active
    -- slot for this continuity identity.
    if v_source.continuity_id is null then
        insert into public.memory_continuity_objects (assistant_id)
        values (coalesce(v_source.assistant_id, ''))
        returning continuity_id into v_continuity_id;
        update public.memories
            set continuity_id = v_continuity_id
            where id = v_source.id;
    else
        v_continuity_id := v_source.continuity_id;
    end if;
    perform pg_advisory_xact_lock(hashtextextended(v_continuity_id::text, 0));

    -- content_hash carries a table-wide unique constraint. A type change may
    -- keep the content untouched, so the new version legitimately reuses the
    -- source hash. Occupation by unrelated memories is still refused; the
    -- source itself and the to-be-deleted parent are handled below inside
    -- this same transaction.
    v_same_hash := (p_content_hash = v_source.content_hash);
    if exists (
        select 1 from public.memories as other
        where other.content_hash = p_content_hash
          and other.id <> v_source.id
          and (v_source.supersedes_memory_id is null
               or other.id <> v_source.supersedes_memory_id)
    ) then
        raise exception 'admin_memory_content_exists';
    end if;

    v_evidence := public.admin_memory_resolve_evidence(
        v_source.assistant_id, p_evidence_message_ids
    );

    -- Retire the source version FIRST: the one-active-per-identity index
    -- must never see two active rows, even inside this transaction.
    update public.memories
        set is_active = false,
            superseded_at = now()
        where id = v_source.id;

    -- Same-content type change: the retired source temporarily releases the
    -- hash so the new version can hold the real value; the undo hands it
    -- back. Any failure rolls the whole release back with the transaction.
    if v_same_hash then
        update public.memories
            set content_hash = null
            where id = v_source.id;
    end if;

    -- Keep exactly two generations, and free the parent BEFORE the insert:
    -- deleting it releases a chain-internal hash occupation (A -> B -> C
    -- with unchanged content) and nulls every dangling reference through
    -- the on-delete set null foreign keys.
    if v_source.supersedes_memory_id is not null then
        select * into v_earlier
        from public.memories as memory
        where memory.id = v_source.supersedes_memory_id
        for update;
        if found then
            if v_earlier.is_active is true then
                raise exception 'admin_memory_chain_conflict';
            end if;
            delete from public.memories where id = v_earlier.id;
            v_removed_earlier := true;
            if v_earlier.continuity_id is not null
               and v_earlier.continuity_id <> v_continuity_id then
                perform public.admin_memory_reap_continuity_object(v_earlier.continuity_id);
            end if;
        end if;
    end if;

    insert into public.memories (
        content, title, tags, heat, importance, embedding,
        source, verified, is_active, recall_count,
        assistant_id, digest_run_id, confidence, content_hash,
        memory_key, supersedes_memory_id, superseded_by_memory_id, superseded_at,
        continuity_id, continuity_type, continuity_schema_version, continuity_data,
        source_type, thread_state,
        evidence_message_ids, source_time,
        memory_time, time_precision,
        evidence_start_time, evidence_end_time, evidence_time_precision,
        recall_scene, recall_tags, recall_embedding
    ) values (
        v_content,
        nullif(left(btrim(coalesce(p_title, '')), 100), ''),
        coalesce(p_tags, '{}'::text[]),
        50.0, p_importance, null,
        'manual', 'verified', true, 0,
        v_source.assistant_id, null, 1.0, p_content_hash,
        null, v_source.id, null, null,
        v_continuity_id, p_continuity_type, 1, p_continuity_data,
        nullif(btrim(coalesce(p_source_type, '')), ''),
        case when p_continuity_type = 'thread' then p_thread_state else null end,
        public.admin_memory_ids_from_evidence(v_evidence),
        null,
        public.admin_memory_normalize_event_time(p_memory_time, p_time_precision),
        coalesce(nullif(btrim(coalesce(p_time_precision, '')), ''), 'unknown'),
        (v_evidence->>'start')::timestamptz,
        (v_evidence->>'end')::timestamptz,
        (v_evidence->>'precision'),
        v_recall_scene,
        p_recall_tags,
        case when v_recall_scene is null then null else p_recall_embedding end
    )
    returning * into v_new;

    update public.memories
        set superseded_by_memory_id = v_new.id
        where id = v_source.id;

    update public.memory_continuity_objects
        set updated_at = now()
        where continuity_id = v_continuity_id;

    return jsonb_build_object(
        'memory', to_jsonb(v_new),
        'previous_version_id', v_source.id,
        'removed_version_id', case when v_removed_earlier then v_earlier.id else null end
    );
end;
$function$;

-- ---------------------------------------------------------------------------
-- 4. Undo the most recent type change of a current version. Only the pair
--    created by change_memory_type_v1 (manual source with a live parent
--    link) qualifies. Physical deletion is preferred; only a genuine
--    reference-constraint refusal archives the new version instead -- any
--    other failure propagates and rolls the whole undo back.
-- ---------------------------------------------------------------------------
create or replace function public.undo_memory_type_change_v1(
    p_memory_id integer
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_current public.memories%rowtype;
    v_previous public.memories%rowtype;
    v_deleted boolean := true;
    v_current_hash text;
begin
    select * into v_current
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if v_current.is_active is not true then
        raise exception 'admin_memory_not_undoable';
    end if;
    -- Ordinary edits and manual originals never create a parent link; only
    -- type-change versions carry source='manual' plus supersedes_memory_id.
    if v_current.supersedes_memory_id is null
       or v_current.source is distinct from 'manual' then
        raise exception 'admin_memory_no_previous_version';
    end if;

    select * into v_previous
    from public.memories as memory
    where memory.id = v_current.supersedes_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_no_previous_version';
    end if;
    -- Occupancy check: restoring must never yield two effective versions.
    if v_previous.is_active is true
       or v_previous.superseded_by_memory_id is distinct from v_current.id then
        raise exception 'admin_memory_previous_conflict';
    end if;

    if v_current.continuity_id is not null then
        perform pg_advisory_xact_lock(hashtextextended(v_current.continuity_id::text, 0));
    end if;

    -- The current version may hold the chain's real content_hash (a
    -- same-content type change released the parent's hash). Capture it
    -- before the row leaves so the parent can take it back.
    v_current_hash := v_current.content_hash;

    begin
        delete from public.memories where id = v_current.id;
    exception
        when foreign_key_violation then
            -- A hard reference outside the version chain pins this row, so
            -- demote it instead of deleting. The hash is released first so
            -- the restored parent can retake it without ever leaving two
            -- non-null copies of the same hash. Any other error type must
            -- propagate; it is never swallowed into a fake success.
            v_deleted := false;
            update public.memories
                set is_active = false,
                    supersedes_memory_id = null,
                    superseded_by_memory_id = null,
                    superseded_at = null,
                    content_hash = null
                where id = v_current.id;
    end;

    update public.memories
        set is_active = true,
            superseded_by_memory_id = null,
            superseded_at = null,
            content_hash = coalesce(content_hash, v_current_hash)
        where id = v_previous.id;

    if v_deleted and v_current.continuity_id is not null
       and v_current.continuity_id is distinct from v_previous.continuity_id then
        perform public.admin_memory_reap_continuity_object(v_current.continuity_id);
    end if;

    return jsonb_build_object(
        'restored_memory_id', v_previous.id,
        'undo_memory_id', v_current.id,
        'undo_deleted', v_deleted
    );
end;
$function$;

-- ---------------------------------------------------------------------------
-- 5. Restore a naturally archived memory: inactive, not replaced by any
--    newer version, and free of conflicts that would produce two effective
--    versions. heat resets to the base 50; every other field is preserved.
-- ---------------------------------------------------------------------------
create or replace function public.restore_archived_memory_v1(
    p_memory_id integer
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_memory public.memories%rowtype;
begin
    select * into v_memory
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if v_memory.is_active is true then
        raise exception 'admin_memory_not_archived';
    end if;
    -- A superseded old version is history, not a natural archive: restoring
    -- it would resurrect a dead generation.
    if v_memory.superseded_by_memory_id is not null then
        raise exception 'admin_memory_superseded';
    end if;

    if v_memory.continuity_id is not null then
        perform pg_advisory_xact_lock(hashtextextended(v_memory.continuity_id::text, 0));
    end if;

    if v_memory.continuity_id is not null and exists (
        select 1 from public.memories as other
        where other.continuity_id = v_memory.continuity_id
          and other.id <> v_memory.id
          and other.is_active = true
          and other.verified = 'verified'
    ) then
        raise exception 'admin_memory_continuity_conflict';
    end if;
    if v_memory.memory_key is not null and exists (
        select 1 from public.memories as other
        where other.memory_key = v_memory.memory_key
          and other.id <> v_memory.id
          and other.is_active = true
          and other.verified = 'verified'
    ) then
        raise exception 'admin_memory_key_conflict';
    end if;
    if exists (
        select 1 from public.memories as other
        where other.supersedes_memory_id = v_memory.id
          and other.is_active = true
          and other.verified = 'verified'
    ) then
        raise exception 'admin_memory_chain_conflict';
    end if;
    -- A live parent link means the chain still has an effective version
    -- above this row: restoring would put two active generations in one
    -- chain. The check is on the parent row itself being active + verified,
    -- never on whether the parent has an even older supersedes pointer.
    if v_memory.supersedes_memory_id is not null and exists (
        select 1 from public.memories as parent
        where parent.id = v_memory.supersedes_memory_id
          and parent.is_active = true
          and parent.verified = 'verified'
    ) then
        raise exception 'admin_memory_chain_conflict';
    end if;

    update public.memories
        set is_active = true,
            heat = 50.0,
            superseded_by_memory_id = null,
            superseded_at = null
        where id = v_memory.id
        returning * into v_memory;

    if v_memory.continuity_id is not null then
        update public.memory_continuity_objects
            set updated_at = now()
            where continuity_id = v_memory.continuity_id;
    end if;

    return jsonb_build_object('memory', to_jsonb(v_memory));
end;
$function$;

-- ---------------------------------------------------------------------------
-- 6. Archive a current formal memory. This is the only write path the admin
--    console may use to set is_active=false: the generic data PATCH no
--    longer accepts is_active or heat, so the lifecycle rules (which rows
--    are archivable, and that restore alone resets heat to 50) cannot be
--    bypassed. Only is_active changes; every other column stays untouched.
-- ---------------------------------------------------------------------------
create or replace function public.archive_admin_memory_v1(
    p_memory_id integer
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_memory public.memories%rowtype;
begin
    select * into v_memory
    from public.memories as memory
    where memory.id = p_memory_id
    for update;

    if not found then
        raise exception 'admin_memory_not_found';
    end if;
    if v_memory.superseded_by_memory_id is not null then
        raise exception 'admin_memory_superseded';
    end if;
    if v_memory.is_active is not true then
        raise exception 'admin_memory_already_archived';
    end if;
    if v_memory.verified is distinct from 'verified' then
        raise exception 'admin_memory_not_archivable';
    end if;

    update public.memories
        set is_active = false
        where id = v_memory.id
        returning * into v_memory;

    return jsonb_build_object('memory', to_jsonb(v_memory));
end;
$function$;

-- ---------------------------------------------------------------------------
-- Grants: these RPCs are server-only. The browser reaches them exclusively
-- through the gateway's token-checked admin API; the MCP memory token has no
-- path here, and anon/authenticated keep no execute rights.
-- ---------------------------------------------------------------------------
revoke all on function public.admin_memory_is_current(public.memories) from public, anon, authenticated;
revoke all on function public.admin_memory_reap_continuity_object(uuid) from public, anon, authenticated;
revoke all on function public.admin_memory_resolve_evidence(text, bigint[]) from public, anon, authenticated;
revoke all on function public.admin_memory_normalize_event_time(text, text) from public, anon, authenticated;
revoke all on function public.admin_memory_tags_ok(text[]) from public, anon, authenticated;
revoke all on function public.admin_memory_ids_from_evidence(jsonb) from public, anon, authenticated;
revoke all on function public.create_admin_memory_v1(text, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) from public, anon, authenticated;
revoke all on function public.edit_admin_memory_v1(integer, jsonb, text, extensions.vector, text) from public, anon, authenticated;
revoke all on function public.change_memory_type_v1(integer, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) from public, anon, authenticated;
revoke all on function public.undo_memory_type_change_v1(integer) from public, anon, authenticated;
revoke all on function public.restore_archived_memory_v1(integer) from public, anon, authenticated;
revoke all on function public.archive_admin_memory_v1(integer) from public, anon, authenticated;

grant execute on function public.create_admin_memory_v1(text, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) to service_role;
grant execute on function public.edit_admin_memory_v1(integer, jsonb, text, extensions.vector, text) to service_role;
grant execute on function public.change_memory_type_v1(integer, text, text, text, text[], integer, text, text, text, text, text[], extensions.vector, text, text, jsonb, bigint[]) to service_role;
grant execute on function public.undo_memory_type_change_v1(integer) to service_role;
grant execute on function public.restore_archived_memory_v1(integer) to service_role;
grant execute on function public.archive_admin_memory_v1(integer) to service_role;

commit;
