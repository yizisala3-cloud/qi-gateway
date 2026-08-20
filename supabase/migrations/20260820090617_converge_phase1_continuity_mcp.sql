-- Converge the partially applied Phase 1 split migrations to the MCP runtime.
-- public.chat_messages remains immutable evidence and is only selected below.
begin;

-- Fail closed when the previously applied split migrations are not present.
do $preflight$
begin
    if to_regclass('public.memory_continuity_objects') is null
       or to_regclass('public.memory_relations') is null then
        raise exception 'phase1_convergence_missing_base_tables';
    end if;
    if not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'memories' and column_name = 'continuity_id'
    ) or not exists (
        select 1 from information_schema.columns
        where table_schema = 'public' and table_name = 'memory_requests' and column_name = 'continuity_id'
    ) then
        raise exception 'phase1_convergence_missing_base_columns';
    end if;
end;
$preflight$;

-- Remove runtime objects that still expose or copy retired columns before the
-- columns themselves are dropped. The vector recall function already has the
-- final shape in production and is intentionally left untouched.
drop trigger if exists sync_reviewed_memory_request_metadata on public.memory_requests;
drop function if exists public.sync_reviewed_memory_request_metadata();
drop function if exists public.search_memories_by_keywords(text[], integer);

create or replace function public.continuity_text_ok(
    p_data jsonb,
    p_key text,
    p_required boolean default false,
    p_max integer default 600
)
returns boolean
language sql
immutable
set search_path to 'pg_catalog'
as $function$
    select case
        when not (p_data ? p_key) or p_data->p_key = 'null'::jsonb then not p_required
        else jsonb_typeof(p_data->p_key) = 'string'
             and char_length(
                 regexp_replace(btrim(p_data->>p_key), '[[:space:]]+', ' ', 'g')
             ) between case when p_required then 1 else 0 end and p_max
    end;
$function$;

create or replace function public.continuity_object_keys_ok(p_data jsonb, p_allowed text[])
returns boolean
language sql
immutable
set search_path to 'pg_catalog'
as $function$
    select jsonb_typeof(p_data) = 'object'
       and not exists (
            select 1
            from jsonb_object_keys(p_data) as key
            where not (key = any(p_allowed))
       );
$function$;

create or replace function public.continuity_integer_ok(
    p_data jsonb,
    p_key text,
    p_min integer,
    p_max integer,
    p_required boolean default true
)
returns boolean
language plpgsql
immutable
set search_path to 'pg_catalog'
as $function$
declare
    v_text text;
    v_number numeric;
begin
    if not (p_data ? p_key) or p_data->p_key = 'null'::jsonb then
        return not p_required;
    end if;
    if jsonb_typeof(p_data->p_key) <> 'number' then
        return false;
    end if;
    v_text := p_data->>p_key;
    if v_text !~ '^-?[0-9]+$' then
        return false;
    end if;
    begin
        v_number := v_text::numeric;
    exception when others then
        return false;
    end;
    return v_number between p_min and p_max;
end;
$function$;

create or replace function public.continuity_string_array_ok(
    p_data jsonb,
    p_key text,
    p_required boolean default false
)
returns boolean
language sql
immutable
set search_path to 'pg_catalog'
as $function$
    select case
        when not (p_data ? p_key) or p_data->p_key = 'null'::jsonb then not p_required
        when jsonb_typeof(p_data->p_key) <> 'array' then false
        when jsonb_array_length(p_data->p_key) > 8 then false
        when p_required and jsonb_array_length(p_data->p_key) = 0 then false
        else not exists (
            select 1
            from jsonb_array_elements(p_data->p_key) as item
            where jsonb_typeof(item) <> 'string'
               or char_length(
                   regexp_replace(btrim(item #>> '{}'), '[[:space:]]+', ' ', 'g')
               ) not between 1 and 120
        )
    end;
$function$;

create or replace function public.validate_continuity_data(
    p_type text,
    p_thread_state text,
    p_data jsonb
)
returns boolean
language plpgsql
immutable
set search_path to 'pg_catalog', 'public'
as $function$
declare
    v_closed boolean;
begin
    if p_type is null
       or p_type not in ('moment','thread','episode','inside_joke','profile','interaction_rule')
       or p_data is null
       or jsonb_typeof(p_data) <> 'object' then
        return false;
    end if;
    if p_type = 'thread' then
        if p_thread_state is null
           or p_thread_state not in ('open','paused','resolved','dissolved','abandoned','unknown') then
            return false;
        end if;
    elsif p_thread_state is not null then
        return false;
    end if;

    if p_type = 'moment' then
        return coalesce((public.continuity_object_keys_ok(
                   p_data,array['scene','event','response','outcome','moment_state','salience_reason']
               )
           and public.continuity_text_ok(p_data,'scene',true)
           and public.continuity_text_ok(p_data,'event',true)
           and public.continuity_text_ok(p_data,'response')
           and public.continuity_text_ok(p_data,'outcome')
           and p_data->>'moment_state' in ('standalone','linked','absorbed')
           and public.continuity_text_ok(p_data,'salience_reason')),false);
    elsif p_type = 'thread' then
        if not public.continuity_object_keys_ok(
            p_data,array[
                'open_question','current_state','next_expected','closure_criteria',
                'closure_summary','closure_reason','opened_at','closed_at',
                'abstract_retrieval_hints','concrete_retrieval_hints'
            ]
        ) then
            return false;
        end if;
        if not public.continuity_text_ok(p_data,'open_question',true)
           or not public.continuity_text_ok(p_data,'current_state',true)
           or not public.continuity_text_ok(p_data,'next_expected')
           or not public.continuity_string_array_ok(p_data,'closure_criteria')
           or not public.continuity_text_ok(p_data,'closure_summary')
           or not public.continuity_text_ok(p_data,'closure_reason')
           or not public.continuity_text_ok(p_data,'opened_at',false,40)
           or not public.continuity_text_ok(p_data,'closed_at',false,40)
           or not public.continuity_string_array_ok(p_data,'abstract_retrieval_hints')
           or not public.continuity_string_array_ok(p_data,'concrete_retrieval_hints') then
            return false;
        end if;
        if coalesce(btrim(p_data->>'opened_at'),'') <> ''
           and btrim(p_data->>'opened_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$' then
            return false;
        end if;
        if coalesce(btrim(p_data->>'closed_at'),'') <> ''
           and btrim(p_data->>'closed_at') !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$' then
            return false;
        end if;
        v_closed := p_thread_state in ('resolved','dissolved','abandoned');
        if v_closed then
            return public.continuity_text_ok(p_data,'closure_summary',true)
               and public.continuity_text_ok(p_data,'closure_reason',true)
               and public.continuity_text_ok(p_data,'closed_at',true,40);
        elsif p_thread_state in ('open','paused') then
            return coalesce(btrim(p_data->>'closure_summary'),'') = ''
               and coalesce(btrim(p_data->>'closure_reason'),'') = ''
               and coalesce(btrim(p_data->>'closed_at'),'') = '';
        end if;
        return true;
    elsif p_type = 'episode' then
        return coalesce((public.continuity_object_keys_ok(
                   p_data,array['beginning','development','turning_point','outcome','aftereffect','episode_start_time','episode_end_time','closure_quality']
               )
           and public.continuity_text_ok(p_data,'beginning',true)
           and public.continuity_text_ok(p_data,'development',true)
           and public.continuity_text_ok(p_data,'turning_point')
           and public.continuity_text_ok(p_data,'outcome',true)
           and public.continuity_text_ok(p_data,'aftereffect')
           and public.continuity_text_ok(p_data,'episode_start_time',false,40)
           and (coalesce(btrim(p_data->>'episode_start_time'),'') = '' or btrim(p_data->>'episode_start_time') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
           and public.continuity_text_ok(p_data,'episode_end_time',false,40)
           and (coalesce(btrim(p_data->>'episode_end_time'),'') = '' or btrim(p_data->>'episode_end_time') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
           and p_data->>'closure_quality' in ('complete','partial','uncertain')),false);
    elsif p_type = 'inside_joke' then
        return coalesce((public.continuity_object_keys_ok(
                   p_data,array['origin','trigger_phrases','shared_meaning','usage_context','avoid_context','response_style','first_seen_at','last_reinforced_at','reinforcement_count']
               )
           and public.continuity_text_ok(p_data,'origin',true)
           and public.continuity_string_array_ok(p_data,'trigger_phrases',true)
           and public.continuity_text_ok(p_data,'shared_meaning',true)
           and public.continuity_string_array_ok(p_data,'usage_context')
           and public.continuity_string_array_ok(p_data,'avoid_context')
           and public.continuity_text_ok(p_data,'response_style')
           and public.continuity_text_ok(p_data,'first_seen_at',false,40)
           and (coalesce(btrim(p_data->>'first_seen_at'),'') = '' or btrim(p_data->>'first_seen_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
           and public.continuity_text_ok(p_data,'last_reinforced_at',false,40)
           and (coalesce(btrim(p_data->>'last_reinforced_at'),'') = '' or btrim(p_data->>'last_reinforced_at') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
           and public.continuity_integer_ok(p_data,'reinforcement_count',0,2147483647,false)),false);
    elsif p_type = 'profile' then
        return coalesce((public.continuity_object_keys_ok(
                   p_data,array['facet','statement','scope','effective_from','effective_until','stability','exceptions','basis']
               )
           and public.continuity_text_ok(p_data,'facet',true)
           and public.continuity_text_ok(p_data,'statement',true)
           and public.continuity_text_ok(p_data,'scope',true)
           and public.continuity_text_ok(p_data,'effective_from',false,40)
           and (coalesce(btrim(p_data->>'effective_from'),'') = '' or btrim(p_data->>'effective_from') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
           and public.continuity_text_ok(p_data,'effective_until',false,40)
           and (coalesce(btrim(p_data->>'effective_until'),'') = '' or btrim(p_data->>'effective_until') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
           and p_data->>'stability' in ('stable','contextual','provisional')
           and public.continuity_string_array_ok(p_data,'exceptions')
           and p_data->>'basis' in ('explicit_self_report','explicit_preference','repeated_observation','reviewed_summary')),false);
    end if;
    return coalesce((public.continuity_object_keys_ok(
               p_data,array['trigger','expected_behavior','forbidden_behavior','scope','priority','rule_state','effective_from','effective_until','exceptions','explicit_instruction']
           )
       and public.continuity_text_ok(p_data,'trigger',true)
       and public.continuity_text_ok(p_data,'expected_behavior',true)
       and public.continuity_string_array_ok(p_data,'forbidden_behavior')
       and public.continuity_text_ok(p_data,'scope',true)
       and public.continuity_integer_ok(p_data,'priority',1,10,true)
       and p_data->>'rule_state' in ('active','revoked','superseded')
       and public.continuity_text_ok(p_data,'effective_from',false,40)
       and (coalesce(btrim(p_data->>'effective_from'),'') = '' or btrim(p_data->>'effective_from') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
       and public.continuity_text_ok(p_data,'effective_until',false,40)
       and (coalesce(btrim(p_data->>'effective_until'),'') = '' or btrim(p_data->>'effective_until') ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}([T ][0-9:.+-]+Z?)?$')
       and public.continuity_string_array_ok(p_data,'exceptions')
       and public.continuity_text_ok(p_data,'explicit_instruction',true)),false);
end;
$function$;

-- Validate all existing rows before replacing constraints or dropping columns.
do $compatibility$
declare
    v_count bigint;
begin
    select count(*) into v_count
    from public.memory_requests
    where source not in ('orangechat_plugin','mcp_memory','daily_digest');
    if v_count > 0 then
        raise exception 'phase1_incompatible_memory_request_source_count:%', v_count;
    end if;

    select count(*) into v_count
    from public.memory_requests
    where continuity_type is not null
      and continuity_type not in ('moment','thread','episode','inside_joke','profile','interaction_rule');
    if v_count > 0 then
        raise exception 'phase1_incompatible_memory_request_type_count:%', v_count;
    end if;

    select count(*) into v_count
    from public.memories
    where continuity_type is not null
      and continuity_type not in ('moment','thread','episode','inside_joke','profile','interaction_rule');
    if v_count > 0 then
        raise exception 'phase1_incompatible_memory_type_count:%', v_count;
    end if;

    select count(*) into v_count
    from public.memory_requests
    where not (
        (continuity_schema_version is null and continuity_data is null)
        or (
            continuity_schema_version = 1
            and public.validate_continuity_data(continuity_type,thread_state,continuity_data)
        )
    );
    if v_count > 0 then
        raise exception 'phase1_incompatible_memory_request_v1_count:%', v_count;
    end if;

    select count(*) into v_count
    from public.memories
    where not (
        (continuity_schema_version is null and continuity_data is null)
        or (
            continuity_schema_version = 1
            and continuity_id is not null
            and public.validate_continuity_data(continuity_type,thread_state,continuity_data)
        )
    );
    if v_count > 0 then
        raise exception 'phase1_incompatible_memory_v1_count:%', v_count;
    end if;

    select count(*) into v_count
    from public.memory_requests
    where continuity_type = 'interaction_rule'
      and source not in ('orangechat_plugin','mcp_memory');
    if v_count > 0 then
        raise exception 'phase1_incompatible_interaction_rule_source_count:%', v_count;
    end if;

    select count(*) into v_count
    from public.memory_requests
    where source = 'daily_digest'
      and continuity_type is not null
      and continuity_type not in ('moment','thread','episode','inside_joke');
    if v_count > 0 then
        raise exception 'phase1_incompatible_daily_digest_type_count:%', v_count;
    end if;

    select count(*) into v_count
    from (
        select continuity_id
        from public.memories
        where continuity_id is not null and verified = 'verified' and is_active = true
        group by continuity_id
        having count(*) > 1
    ) as duplicate_identity;
    if v_count > 0 then
        raise exception 'phase1_duplicate_active_continuity_count:%', v_count;
    end if;
end;
$compatibility$;

alter table public.memory_requests
    drop constraint if exists memory_requests_continuity_type_check,
    drop constraint if exists memory_requests_continuity_thread_state_check,
    drop constraint if exists memory_requests_continuity_v1_check,
    drop constraint if exists memory_requests_automatic_type_check,
    drop constraint if exists memory_requests_interaction_rule_source_check,
    drop constraint if exists memory_requests_source_values,
    drop constraint if exists memory_requests_source_check,
    add constraint memory_requests_continuity_type_check check (
        continuity_type is null
        or continuity_type in ('moment','thread','episode','inside_joke','profile','interaction_rule')
    ),
    add constraint memory_requests_continuity_thread_state_check check (
        (continuity_type = 'thread' and thread_state in ('open','paused','resolved','dissolved','abandoned','unknown'))
        or (continuity_type is distinct from 'thread' and thread_state is null)
    ),
    add constraint memory_requests_continuity_v1_check check (
        (continuity_schema_version is null and continuity_data is null)
        or (
            continuity_schema_version = 1
            and public.validate_continuity_data(continuity_type,thread_state,continuity_data)
        )
    ),
    add constraint memory_requests_automatic_type_check check (
        source <> 'daily_digest'
        or continuity_type is null
        or continuity_type in ('moment','thread','episode','inside_joke')
    ),
    add constraint memory_requests_interaction_rule_source_check check (
        continuity_type is distinct from 'interaction_rule'
        or source in ('orangechat_plugin','mcp_memory')
    ),
    add constraint memory_requests_source_values check (
        source in ('orangechat_plugin','mcp_memory','daily_digest')
    );

alter table public.memories
    drop constraint if exists memories_continuity_type_check,
    drop constraint if exists memories_continuity_thread_state_check,
    drop constraint if exists memories_continuity_v1_check,
    add constraint memories_continuity_type_check check (
        continuity_type is null
        or continuity_type in ('moment','thread','episode','inside_joke','profile','interaction_rule')
    ),
    add constraint memories_continuity_thread_state_check check (
        (continuity_type = 'thread' and thread_state in ('open','paused','resolved','dissolved','abandoned','unknown'))
        or (continuity_type is distinct from 'thread' and thread_state is null)
    ),
    add constraint memories_continuity_v1_check check (
        (continuity_schema_version is null and continuity_data is null)
        or (
            continuity_schema_version = 1
            and continuity_id is not null
            and public.validate_continuity_data(continuity_type,thread_state,continuity_data)
        )
    );

create unique index if not exists memories_one_active_version_per_continuity
    on public.memories(continuity_id)
    where continuity_id is not null and verified = 'verified' and is_active = true;

-- Retire fields only after every live SQL object that referenced them has been
-- removed or prepared for replacement. No continuity classification is guessed.
drop index if exists public.memory_requests_type_queue_idx;
alter table public.memory_requests
    drop constraint if exists memory_requests_memory_type_values,
    drop column if exists memory_type,
    drop column if exists proposed_relations;
alter table public.memories
    drop constraint if exists memories_memory_type_values,
    drop column if exists memory_type;

create or replace function public.allocate_memory_continuity_id(
    p_assistant_id text,
    p_update_mode text,
    p_memory_key text
)
returns uuid
language plpgsql
set search_path to 'public'
as $function$
declare
    v_id uuid;
    v_memory_id integer;
begin
    if p_update_mode = 'replace' then
        select id, continuity_id
        into v_memory_id, v_id
        from public.memories
        where assistant_id = p_assistant_id
          and memory_key = p_memory_key
          and verified = 'verified'
          and is_active = true
        for update;
        if v_memory_id is null then
            insert into public.memory_continuity_objects(assistant_id)
            values (p_assistant_id)
            returning continuity_id into v_id;
            return v_id;
        end if;
        if v_id is null then
            insert into public.memory_continuity_objects(assistant_id)
            values (p_assistant_id)
            returning continuity_id into v_id;
            update public.memories set continuity_id = v_id where id = v_memory_id;
        end if;
        return v_id;
    end if;
    insert into public.memory_continuity_objects(assistant_id)
    values (p_assistant_id)
    returning continuity_id into v_id;
    return v_id;
end;
$function$;

create or replace function public.create_memory_request_v4(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_subject text, p_source_type text,
    p_continuity_value integer, p_retention_class text, p_participants text[], p_source text
)
returns jsonb
language plpgsql
security definer
set search_path to 'public'
as $function$
declare
    v_request public.memory_requests%rowtype;
    v_recent_count integer;
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
        subject,source_type,continuity_value,retention_class,participants
    ) values (
        p_assistant_id,nullif(trim(p_conversation_id),''),p_source_message_id,p_content,p_title,p_tags,p_importance,p_reason,p_content_hash,
        p_idempotency_key,'pending',p_source,p_memory_key,p_update_mode,
        case when p_source_message_id is null then '{}'::bigint[] else array[p_source_message_id] end,
        p_continuity_type,p_thread_state,null,1,p_continuity_data,
        p_subject,p_source_type,p_continuity_value,p_retention_class,p_participants
    )
    returning * into v_request;
    return jsonb_build_object('created',true,'request',to_jsonb(v_request));
end;
$function$;

-- Shared candidate writer used by both automatic pipelines. It does not write
-- public.memory_relations and reads public.chat_messages only as evidence.
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
    v_related_request bigint;
    v_related_memory integer;
    v_dedupe text := 'none';
    v_dedupe_reason text;
    v_request_id bigint;
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
        continuity_id,continuity_schema_version,continuity_data
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
        null,1,p_item->'continuity_data'
    )
    on conflict do nothing
    returning id into v_request_id;
    get diagnostics v_delta = row_count;
    if v_delta = 1 and v_type in ('moment','thread','inside_joke') then
        perform public.review_memory_request_v5(
            v_request_id,'approve',v_content,nullif(left(trim(coalesce(p_item->>'title','')),100),''),
            array[v_type],least(greatest(coalesce((p_item->>'importance')::integer,5),1),10),v_content_hash,
            'daily_digest_ai','automatic low-risk continuity memory',v_key,v_mode,null
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
        v_preview := v_preview || jsonb_build_array(v_item - 'embedding' - 'content_hash');
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
        v_preview := v_preview || jsonb_build_array(v_item - 'embedding' - 'content_hash');
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
    p_related_memory_id integer default null
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

    return public.review_memory_request_v4(
        p_request_id,p_action,p_content,p_title,p_tags,p_importance,p_content_hash,
        p_reviewed_by,p_review_note,p_memory_key,p_update_mode,p_related_memory_id
    );
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
            source = case when new.source = 'daily_digest' then 'daily_digest' else memory.source end
        where memory.id = new.memory_id;
        update public.memory_continuity_objects
        set updated_at = now()
        where continuity_id = new.continuity_id;
    end if;
    return new;
end;
$function$;

create trigger sync_reviewed_memory_request_metadata
after insert or update of status,memory_id
on public.memory_requests
for each row
execute function public.sync_reviewed_memory_request_metadata();

-- Low-risk MCP/plugin writes are reviewed inside the same database transaction.
create or replace function public.write_memory_direct_v1(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_subject text, p_source_type text,
    p_continuity_value integer, p_retention_class text, p_participants text[], p_source text,
    p_reviewed_by text
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
        p_participants,p_source
    );
    v_request := v_created->'request';
    if v_request->>'continuity_type' is distinct from p_continuity_type then
        raise exception 'memory_request_idempotency_conflict';
    end if;
    v_review := public.review_memory_request_v5(
        (v_request->>'id')::bigint,'approve',p_content,p_title,p_tags,p_importance,p_content_hash,
        left(coalesce(nullif(trim(p_reviewed_by),''),'orangechat_ai'),120),null,p_memory_key,p_update_mode,null
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

-- Only keyword recall still had a retired memory_type result in the partially
-- applied production state, so only that exact signature is recreated here.
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
    layer text,
    created_at timestamptz,
    last_recalled_at timestamptz,
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
    evidence_end_time timestamptz
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
        memory.layer,memory.created_at,memory.last_recalled_at,memory.continuity_id,
        memory.continuity_type,memory.continuity_schema_version,memory.continuity_data,
        memory.subject,memory.source_type,memory.thread_state,memory.continuity_value,
        memory.retention_class,memory.participants,memory.memory_time,
        memory.evidence_start_time,memory.evidence_end_time
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

revoke all on function public.continuity_text_ok(jsonb,text,boolean,integer) from public,anon,authenticated;
revoke all on function public.continuity_object_keys_ok(jsonb,text[]) from public,anon,authenticated;
revoke all on function public.continuity_integer_ok(jsonb,text,integer,integer,boolean) from public,anon,authenticated;
revoke all on function public.continuity_string_array_ok(jsonb,text,boolean) from public,anon,authenticated;
revoke all on function public.validate_continuity_data(text,text,jsonb) from public,anon,authenticated;
revoke all on function public.allocate_memory_continuity_id(text,text,text) from public,anon,authenticated;
revoke all on function public.store_continuity_candidate(public.memory_digest_runs,jsonb) from public,anon,authenticated;
revoke all on function public.create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text) from public,anon,authenticated;
revoke all on function public.write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text) from public,anon,authenticated;
revoke all on function public.review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) from public,anon,authenticated;
revoke all on function public.commit_memory_digest_run(bigint,jsonb) from public,anon,authenticated;
revoke all on function public.commit_memory_continuity_run(bigint,jsonb) from public,anon,authenticated;
revoke all on function public.sync_reviewed_memory_request_metadata() from public,anon,authenticated;
revoke all on function public.search_memories_by_keywords(text[],integer) from public,anon,authenticated;

grant execute on function public.create_memory_request_v4(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text) to service_role;
grant execute on function public.write_memory_direct_v1(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,text,text,integer,text,text[],text,text) to service_role;
grant execute on function public.review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) to service_role;
grant execute on function public.commit_memory_digest_run(bigint,jsonb) to service_role;
grant execute on function public.commit_memory_continuity_run(bigint,jsonb) to service_role;
grant execute on function public.search_memories_by_keywords(text[],integer) to service_role;

-- Old review entry points remain available only for the v5 wrapper's owner;
-- the gateway role cannot bypass continuity validation or identity allocation.
revoke execute on function public.review_memory_request_v2(bigint,text,text,text,text[],integer,text,text,text,text,text) from service_role;
revoke execute on function public.review_memory_request_v3(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) from service_role;
revoke execute on function public.review_memory_request_v4(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) from service_role;

revoke all on table public.memory_continuity_objects,public.memory_relations from anon,authenticated;
grant select,insert,update on table public.memory_continuity_objects,public.memory_relations to service_role;
grant usage,select on sequence public.memory_relations_id_seq to service_role;

commit;
