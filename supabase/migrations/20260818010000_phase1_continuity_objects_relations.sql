-- Phase 1: six-class continuity schema, stable object identity, and reviewed relations.
-- public.chat_messages is immutable evidence and is only selected below.
begin;

-- Return shapes depend on memory_type, so remove only these exact signatures first.
drop function if exists public.match_memories(extensions.vector, double precision, integer);
drop function if exists public.search_memories_by_keywords(text[], integer);
drop trigger if exists sync_reviewed_memory_request_metadata on public.memory_requests;
drop function if exists public.sync_reviewed_memory_request_metadata();
drop function if exists public.create_memory_request(text,text,bigint,text,text,text[],integer,text,text,text,integer);
drop function if exists public.create_memory_request_v2(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text);

alter table public.memory_requests
    drop constraint if exists memory_requests_memory_type_values,
    drop column if exists memory_type;
alter table public.memories
    drop constraint if exists memories_memory_type_values,
    drop column if exists memory_type;

create table public.memory_continuity_objects (
    continuity_id uuid primary key default gen_random_uuid(),
    assistant_id text not null,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
alter table public.memory_continuity_objects enable row level security;

alter table public.memory_requests
    add column continuity_id uuid,
    add column continuity_schema_version smallint,
    add column continuity_data jsonb,
    add column proposed_relations jsonb;
alter table public.memories
    add column continuity_id uuid,
    add column continuity_schema_version smallint,
    add column continuity_data jsonb;

alter table public.memory_requests
    add constraint memory_requests_continuity_object_fkey foreign key (continuity_id)
        references public.memory_continuity_objects(continuity_id),
    add constraint memory_requests_proposed_relations_array
        check (proposed_relations is null or jsonb_typeof(proposed_relations) = 'array');
alter table public.memories
    add constraint memories_continuity_object_fkey foreign key (continuity_id)
        references public.memory_continuity_objects(continuity_id);

create or replace function public.continuity_text_ok(p_data jsonb, p_key text, p_required boolean default false, p_max integer default 600)
returns boolean language sql immutable set search_path to 'pg_catalog' as $function$
    select case
        when not (p_data ? p_key) or p_data->p_key = 'null'::jsonb then not p_required
        else jsonb_typeof(p_data->p_key) = 'string'
             and char_length(btrim(p_data->>p_key)) between case when p_required then 1 else 0 end and p_max
    end;
$function$;

create or replace function public.continuity_string_array_ok(p_data jsonb, p_key text, p_required boolean default false)
returns boolean language sql immutable set search_path to 'pg_catalog' as $function$
    select case
        when not (p_data ? p_key) or p_data->p_key = 'null'::jsonb then not p_required
        when jsonb_typeof(p_data->p_key) <> 'array' then false
        when jsonb_array_length(p_data->p_key) > 8 then false
        when p_required and jsonb_array_length(p_data->p_key) = 0 then false
        else not exists (
            select 1 from jsonb_array_elements(p_data->p_key) item
            where jsonb_typeof(item) <> 'string'
               or char_length(btrim(item #>> '{}')) not between 1 and 120
        )
    end;
$function$;

create or replace function public.validate_continuity_data(p_type text, p_thread_state text, p_data jsonb)
returns boolean language plpgsql immutable set search_path to 'pg_catalog', 'public' as $function$
declare v_closed boolean;
begin
    if p_type not in ('moment','thread','episode','inside_joke','profile','interaction_rule')
       or jsonb_typeof(p_data) <> 'object' then return false; end if;
    if p_type = 'thread' then
        if p_thread_state not in ('open','paused','resolved','dissolved','abandoned','unknown') then return false; end if;
    elsif p_thread_state is not null then return false; end if;

    if p_type = 'moment' then
        return public.continuity_text_ok(p_data,'scene',true)
           and public.continuity_text_ok(p_data,'event',true)
           and public.continuity_text_ok(p_data,'response')
           and public.continuity_text_ok(p_data,'outcome')
           and p_data->>'moment_state' in ('standalone','linked','absorbed')
           and public.continuity_text_ok(p_data,'salience_reason');
    elsif p_type = 'thread' then
        v_closed := p_thread_state in ('resolved','dissolved','abandoned');
        if not public.continuity_text_ok(p_data,'open_question',true)
           or not public.continuity_text_ok(p_data,'current_state',true)
           or not public.continuity_string_array_ok(p_data,'closure_criteria')
           or not public.continuity_string_array_ok(p_data,'abstract_retrieval_hints')
           or not public.continuity_string_array_ok(p_data,'concrete_retrieval_hints') then return false; end if;
        if v_closed then
            return public.continuity_text_ok(p_data,'closure_summary',true)
               and public.continuity_text_ok(p_data,'closure_reason',true)
               and public.continuity_text_ok(p_data,'closed_at',true,40);
        elsif p_thread_state in ('open','paused') then
            return coalesce(p_data->>'closure_summary','') = ''
               and coalesce(p_data->>'closure_reason','') = ''
               and coalesce(p_data->>'closed_at','') = '';
        end if;
        return true;
    elsif p_type = 'episode' then
        return public.continuity_text_ok(p_data,'beginning',true)
           and public.continuity_text_ok(p_data,'development',true)
           and public.continuity_text_ok(p_data,'outcome',true)
           and p_data->>'closure_quality' in ('complete','partial','uncertain');
    elsif p_type = 'inside_joke' then
        return public.continuity_text_ok(p_data,'origin',true)
           and public.continuity_string_array_ok(p_data,'trigger_phrases',true)
           and public.continuity_text_ok(p_data,'shared_meaning',true)
           and public.continuity_string_array_ok(p_data,'usage_context')
           and public.continuity_string_array_ok(p_data,'avoid_context')
           and jsonb_typeof(coalesce(p_data->'reinforcement_count','0'::jsonb)) = 'number'
           and (p_data->>'reinforcement_count')::integer >= 0;
    elsif p_type = 'profile' then
        return public.continuity_text_ok(p_data,'facet',true)
           and public.continuity_text_ok(p_data,'statement',true)
           and public.continuity_text_ok(p_data,'scope',true)
           and p_data->>'stability' in ('stable','contextual','provisional')
           and public.continuity_string_array_ok(p_data,'exceptions')
           and p_data->>'basis' in ('explicit_self_report','explicit_preference','repeated_observation','reviewed_summary');
    end if;
    return public.continuity_text_ok(p_data,'trigger',true)
       and public.continuity_text_ok(p_data,'expected_behavior',true)
       and public.continuity_string_array_ok(p_data,'forbidden_behavior')
       and public.continuity_text_ok(p_data,'scope',true)
       and jsonb_typeof(p_data->'priority') = 'number'
       and (p_data->>'priority')::integer between 1 and 10
       and p_data->>'rule_state' in ('active','revoked','superseded')
       and public.continuity_string_array_ok(p_data,'exceptions')
       and public.continuity_text_ok(p_data,'explicit_instruction',true);
end;
$function$;

create or replace function public.validate_proposed_relations(p_value jsonb)
returns boolean language sql immutable set search_path to 'pg_catalog' as $function$
    select p_value is null or (
        jsonb_typeof(p_value) = 'array' and jsonb_array_length(p_value) <= 20
        and not exists (
            select 1 from jsonb_array_elements(p_value) item
            where jsonb_typeof(item) <> 'object'
               or item->>'relation_type' not in ('part_of','advances','resolves','dissolves','origin_of','evokes','supports','contradicts','governed_by')
               or (item ? 'confidence' and (
                    jsonb_typeof(item->'confidence') <> 'number'
                    or (item->>'confidence')::double precision not between 0 and 1
               ))
               or (item ? 'description' and (
                    jsonb_typeof(item->'description') <> 'string'
                    or char_length(item->>'description') > 600
               ))
        )
    );
$function$;

alter table public.memory_requests
    drop constraint if exists memory_requests_continuity_type_check,
    drop constraint if exists memory_requests_continuity_thread_state_check,
    add constraint memory_requests_continuity_type_check check (
        continuity_type is null or continuity_type in ('moment','thread','episode','inside_joke','profile','interaction_rule')
    ),
    add constraint memory_requests_continuity_thread_state_check check (
        (continuity_type = 'thread' and thread_state in ('open','paused','resolved','dissolved','abandoned','unknown'))
        or (continuity_type is distinct from 'thread' and thread_state is null)
    ),
    add constraint memory_requests_continuity_v1_check check (
        (continuity_schema_version is null and continuity_data is null)
        or (continuity_schema_version = 1 and continuity_id is not null and public.validate_continuity_data(continuity_type,thread_state,continuity_data))
    ),
    add constraint memory_requests_automatic_type_check check (
        source <> 'daily_digest' or continuity_type in ('moment','thread','episode','inside_joke')
    ),
    add constraint memory_requests_interaction_rule_source_check check (
        continuity_type is distinct from 'interaction_rule' or source = 'orangechat_plugin'
    ),
    add constraint memory_requests_proposed_relations_shape_check check (
        public.validate_proposed_relations(proposed_relations)
    );

alter table public.memories
    drop constraint if exists memories_continuity_type_check,
    drop constraint if exists memories_continuity_thread_state_check,
    add constraint memories_continuity_type_check check (
        continuity_type is null or continuity_type in ('moment','thread','episode','inside_joke','profile','interaction_rule')
    ),
    add constraint memories_continuity_thread_state_check check (
        (continuity_type = 'thread' and thread_state in ('open','paused','resolved','dissolved','abandoned','unknown'))
        or (continuity_type is distinct from 'thread' and thread_state is null)
    ),
    add constraint memories_continuity_v1_check check (
        (continuity_schema_version is null and continuity_data is null)
        or (continuity_schema_version = 1 and continuity_id is not null and public.validate_continuity_data(continuity_type,thread_state,continuity_data))
    );

create unique index memories_one_active_version_per_continuity
    on public.memories(continuity_id)
    where continuity_id is not null and verified = 'verified' and is_active = true;

create or replace function public.allocate_memory_continuity_id(p_assistant_id text, p_update_mode text, p_memory_key text)
returns uuid language plpgsql set search_path to 'public' as $function$
declare v_id uuid; v_memory_id integer;
begin
    if p_update_mode = 'replace' then
        select id, continuity_id into v_memory_id, v_id from public.memories
        where memory_key = p_memory_key and verified = 'verified' and is_active = true for update;
        if v_memory_id is null then raise exception 'memory_request_stale_update'; end if;
        if v_id is null then
            insert into public.memory_continuity_objects(assistant_id) values (p_assistant_id) returning continuity_id into v_id;
            update public.memories set continuity_id = v_id where id = v_memory_id;
        end if;
        return v_id;
    end if;
    insert into public.memory_continuity_objects(assistant_id) values (p_assistant_id) returning continuity_id into v_id;
    return v_id;
end;
$function$;

create or replace function public.create_memory_request_v3(
    p_assistant_id text, p_conversation_id text, p_source_message_id bigint, p_content text,
    p_title text, p_tags text[], p_importance integer, p_reason text, p_content_hash text,
    p_idempotency_key text, p_rate_limit integer, p_memory_key text, p_update_mode text,
    p_continuity_type text, p_thread_state text, p_continuity_schema_version smallint,
    p_continuity_data jsonb, p_proposed_relations jsonb, p_subject text, p_source_type text,
    p_continuity_value integer, p_retention_class text, p_participants text[]
) returns jsonb language plpgsql security definer set search_path to 'public' as $function$
declare v_request public.memory_requests%rowtype; v_id uuid; v_recent_count integer;
begin
    if p_continuity_schema_version <> 1 or not public.validate_continuity_data(p_continuity_type,p_thread_state,p_continuity_data)
       then raise exception 'memory_request_invalid_continuity_data'; end if;
    if not public.validate_proposed_relations(p_proposed_relations) then raise exception 'memory_request_invalid_proposed_relations'; end if;
    if p_update_mode not in ('append','replace')
       or (p_update_mode='append' and p_memory_key is not null)
       or (p_update_mode='replace' and p_memory_key is null)
       then raise exception 'memory_request_invalid_update_mode'; end if;
    if p_continuity_type = 'interaction_rule' and coalesce(p_continuity_data->>'explicit_instruction','') = ''
       then raise exception 'memory_request_interaction_rule_requires_instruction'; end if;
    perform pg_advisory_xact_lock(hashtextextended(p_assistant_id,0));
    select count(*) into v_recent_count from public.memory_requests
    where assistant_id=p_assistant_id and source='orangechat_plugin' and created_at>now()-interval '1 hour';
    if v_recent_count>=least(greatest(coalesce(p_rate_limit,6),1),60) then raise exception 'memory_request_rate_limited'; end if;
    select * into v_request from public.memory_requests where assistant_id=p_assistant_id and idempotency_key=p_idempotency_key limit 1;
    if found then return jsonb_build_object('created',false,'request',to_jsonb(v_request)); end if;
    select * into v_request from public.memory_requests
    where assistant_id=p_assistant_id and content_hash=p_content_hash and status in ('pending','approved','merged')
    order by id desc limit 1;
    if found then return jsonb_build_object('created',false,'request',to_jsonb(v_request)); end if;
    v_id := public.allocate_memory_continuity_id(p_assistant_id,p_update_mode,p_memory_key);
    insert into public.memory_requests(
        assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,content_hash,
        idempotency_key,status,source,memory_key,update_mode,evidence_message_ids,
        continuity_type,thread_state,continuity_id,continuity_schema_version,continuity_data,proposed_relations,
        subject,source_type,continuity_value,retention_class,participants
    ) values (
        p_assistant_id,nullif(trim(p_conversation_id),''),p_source_message_id,p_content,p_title,p_tags,p_importance,p_reason,p_content_hash,
        p_idempotency_key,'pending','orangechat_plugin',p_memory_key,p_update_mode,
        case when p_source_message_id is null then '{}'::bigint[] else array[p_source_message_id] end,
        p_continuity_type,p_thread_state,v_id,1,p_continuity_data,coalesce(p_proposed_relations,'[]'::jsonb),
        p_subject,p_source_type,p_continuity_value,p_retention_class,p_participants
    ) returning * into v_request;
    return jsonb_build_object('created',true,'request',to_jsonb(v_request));
end;
$function$;

-- Shared candidate writer used by both automatic pipelines. It never creates formal relations.
create or replace function public.store_continuity_candidate(p_run public.memory_digest_runs, p_item jsonb)
returns integer language plpgsql security definer set search_path to 'public','extensions' as $function$
declare v_ids bigint[]; v_source bigint; v_conversation text; v_id uuid; v_delta integer; v_mode text; v_key text; v_type text;
        v_related_request bigint; v_related_memory integer; v_dedupe text:='none'; v_dedupe_reason text;
begin
    v_type := p_item->>'continuity_type';
    if v_type not in ('moment','thread','episode','inside_joke') then raise exception 'memory_digest_invalid_continuity_type'; end if;
    if coalesce((p_item->>'continuity_schema_version')::integer,0) <> 1
       or not public.validate_continuity_data(v_type,p_item->>'thread_state',p_item->'continuity_data')
       then raise exception 'memory_digest_invalid_continuity_data'; end if;
    if not public.validate_proposed_relations(p_item->'proposed_relations') then raise exception 'memory_digest_invalid_proposed_relations'; end if;
    if nullif(p_item->>'embedding','') is null then raise exception 'memory_digest_missing_embedding'; end if;
    select coalesce(array_agg(m.id order by m.id),'{}'::bigint[]) into v_ids
    from public.chat_messages m join (
        select distinct value::bigint id from jsonb_array_elements_text(coalesce(p_item->'evidence_message_ids','[]'::jsonb))
        where value ~ '^[0-9]+$'
    ) e on e.id=m.id where m.assistant_id=p_run.assistant_id and m.id between p_run.source_first_message_id and p_run.source_last_message_id;
    if cardinality(v_ids) not between 1 and 8 then raise exception 'memory_digest_invalid_evidence'; end if;
    v_source:=v_ids[1]; select conversation_id into v_conversation from public.chat_messages where id=v_source;
    if exists(select 1 from public.memory_requests where assistant_id=p_run.assistant_id and content_hash=(p_item->>'content_hash'))
       or exists(select 1 from public.memories where is_active=true and verified='verified' and content_hash=(p_item->>'content_hash'))
       then return 0; end if;
    v_mode:=case when p_item->>'update_mode'='replace' then 'replace' else 'append' end;
    v_key:=case when v_mode='replace' then nullif(p_item->>'memory_key','') else null end;
    select id into v_related_request from public.memory_requests
    where assistant_id=p_run.assistant_id and status in ('pending','approved','merged')
      and public.memory_dedupe_text_similarity(content,left(trim(p_item->>'content'),600))>=.86
    order by created_at desc limit 1;
    if v_related_request is not null then v_dedupe:='possible_duplicate'; v_dedupe_reason:='similar_to_existing_request';
    else
        select id into v_related_memory from public.memories where is_active=true and verified='verified'
          and public.memory_dedupe_text_similarity(content,left(trim(p_item->>'content'),600))>=.86 order by created_at desc limit 1;
        if v_related_memory is not null then v_dedupe:='possible_duplicate'; v_dedupe_reason:='similar_to_active_memory'; end if;
    end if;
    v_id:=public.allocate_memory_continuity_id(p_run.assistant_id,v_mode,v_key);
    insert into public.memory_requests(
        assistant_id,conversation_id,source_message_id,content,title,tags,importance,reason,content_hash,idempotency_key,
        status,source,memory_key,update_mode,confidence,evidence_message_ids,source_time,memory_time,time_precision,digest_run_id,embedding,
        dedupe_state,dedupe_reason,related_request_id,related_memory_id,
        continuity_type,subject,source_type,thread_state,continuity_value,retention_class,participants,evidence_start_time,evidence_end_time,
        continuity_id,continuity_schema_version,continuity_data,proposed_relations
    ) values (
        p_run.assistant_id,v_conversation,v_source,left(trim(p_item->>'content'),600),nullif(left(trim(coalesce(p_item->>'title','')),100),''),
        array[v_type],least(greatest(coalesce((p_item->>'importance')::integer,5),1),10),'自动总结提取，等待用户审核',
        p_item->>'content_hash','continuity-'||p_run.id||'-'||(p_item->>'content_hash'),'pending','daily_digest',v_key,v_mode,
        least(greatest(coalesce((p_item->>'confidence')::double precision,0.6),0),1),v_ids,
        nullif(p_item->>'source_time','')::timestamptz,
        case when p_item->>'time_precision'='day' and p_item->>'memory_time' ~ '^\d{4}-\d{2}-\d{2}$'
             then (p_item->>'memory_time')::date::timestamp at time zone 'Asia/Shanghai'
             else nullif(p_item->>'memory_time','')::timestamptz end,coalesce(p_item->>'time_precision','unknown'),
        p_run.id,(p_item->>'embedding')::extensions.vector,v_dedupe,v_dedupe_reason,v_related_request,v_related_memory,
        v_type,p_item->>'subject',p_item->>'source_type',p_item->>'thread_state',
        least(greatest(coalesce((p_item->>'continuity_value')::integer,5),1),10),coalesce(p_item->>'retention_class','normal'),
        array(select value from jsonb_array_elements_text(coalesce(p_item->'participants','[]'::jsonb)) limit 3),
        nullif(p_item->>'evidence_start_time','')::timestamptz,nullif(p_item->>'evidence_end_time','')::timestamptz,
        v_id,1,p_item->'continuity_data',coalesce(p_item->'proposed_relations','[]'::jsonb)
    ) on conflict do nothing;
    get diagnostics v_delta=row_count; return v_delta;
end;
$function$;

create or replace function public.commit_memory_digest_run(p_run_id bigint,p_memories jsonb default '[]'::jsonb)
returns integer language plpgsql security definer set search_path to 'public','extensions' as $function$
declare v_run public.memory_digest_runs%rowtype; v_item jsonb; v_count integer:=0; v_preview jsonb:='[]'::jsonb;
begin
    if jsonb_typeof(coalesce(p_memories,'[]'::jsonb))<>'array' then raise exception 'p_memories must be a JSON array'; end if;
    select * into v_run from public.memory_digest_runs where id=p_run_id for update;
    if not found or v_run.mode<>'execute' or v_run.status<>'running' then raise exception 'memory_digest_invalid_run'; end if;
    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id,0));
    for v_item in select value from jsonb_array_elements(p_memories) loop
        v_count:=v_count+public.store_continuity_candidate(v_run,v_item);
        v_preview:=v_preview||jsonb_build_array(v_item-'embedding'-'content_hash');
    end loop;
    insert into public.memory_digest_cursors(assistant_id,last_processed_message_id,last_success_at,updated_at)
    values(v_run.assistant_id,v_run.source_last_message_id,now(),now()) on conflict(assistant_id) do update
    set last_processed_message_id=greatest(public.memory_digest_cursors.last_processed_message_id,excluded.last_processed_message_id),last_success_at=now(),updated_at=now();
    update public.memory_digest_runs set status='succeeded',extracted_count=jsonb_array_length(p_memories),inserted_count=v_count,
        preview_memories=v_preview,completed_at=now(),error_code=null,error_message=null where id=p_run_id;
    return v_count;
end;
$function$;

create or replace function public.commit_memory_continuity_run(p_run_id bigint,p_candidates jsonb default '[]'::jsonb)
returns integer language plpgsql security definer set search_path to 'public','extensions' as $function$
declare v_run public.memory_digest_runs%rowtype; v_item jsonb; v_count integer:=0; v_preview jsonb:='[]'::jsonb;
begin
    if jsonb_typeof(coalesce(p_candidates,'[]'::jsonb))<>'array' or jsonb_array_length(p_candidates)=0 then raise exception 'memory_continuity_invalid_candidates'; end if;
    select * into v_run from public.memory_digest_runs where id=p_run_id for update;
    if not found or v_run.pipeline<>'continuity' or v_run.mode<>'execute' or v_run.status<>'running' then raise exception 'memory_continuity_invalid_run'; end if;
    perform pg_advisory_xact_lock(hashtextextended(v_run.assistant_id,0));
    for v_item in select value from jsonb_array_elements(p_candidates) loop
        v_count:=v_count+public.store_continuity_candidate(v_run,v_item);
        v_preview:=v_preview||jsonb_build_array(v_item-'embedding'-'content_hash');
    end loop;
    update public.memory_continuity_cursors set last_processed_message_id=greatest(last_processed_message_id,v_run.source_last_message_id),status='ready',
        last_success_at=now(),auto_cooldown_until=now()+interval '1 hour',blocked_first_message_id=null,blocked_last_message_id=null,
        blocked_message_count=null,blocked_at=null,pause_reason=null,updated_at=now() where assistant_id=v_run.assistant_id;
    update public.memory_digest_runs set status='succeeded',extracted_count=jsonb_array_length(p_candidates),inserted_count=v_count,
        preview_memories=v_preview,completed_at=now(),heartbeat_at=null,error_code=null,error_message=null where id=p_run_id;
    return v_count;
end;
$function$;

create or replace function public.review_memory_request_v5(
    p_request_id bigint,p_action text,p_content text default null,p_title text default null,p_tags text[] default null,
    p_importance integer default null,p_content_hash text default null,p_reviewed_by text default 'gateway_admin',
    p_review_note text default null,p_memory_key text default null,p_update_mode text default null,p_related_memory_id integer default null
) returns jsonb language plpgsql security definer set search_path to 'public' as $function$
declare v_request public.memory_requests%rowtype; v_mode text; v_key text; v_id uuid;
begin
    select * into v_request from public.memory_requests where id=p_request_id for update;
    if not found then raise exception 'memory_request_not_found'; end if;
    if lower(trim(p_action)) in ('approve','merge') and (
        v_request.continuity_id is null or v_request.continuity_schema_version<>1
        or not public.validate_continuity_data(v_request.continuity_type,v_request.thread_state,v_request.continuity_data)
    ) then raise exception 'memory_request_unclassified_legacy'; end if;
    if lower(trim(p_action))='approve' then
        v_mode:=lower(trim(coalesce(p_update_mode,v_request.update_mode,'append')));
        v_key:=case when v_mode='replace' then nullif(lower(trim(coalesce(p_memory_key,v_request.memory_key,''))),'') else null end;
        if v_mode not in ('append','replace') or (v_mode='replace' and v_key is null) then raise exception 'memory_request_invalid_update_mode'; end if;
        if v_mode is distinct from v_request.update_mode or v_key is distinct from v_request.memory_key then
            v_id:=public.allocate_memory_continuity_id(v_request.assistant_id,v_mode,v_key);
            update public.memory_requests set continuity_id=v_id,update_mode=v_mode,memory_key=v_key,updated_at=now()
            where id=v_request.id returning * into v_request;
        end if;
    end if;
    return public.review_memory_request_v4(p_request_id,p_action,p_content,p_title,p_tags,p_importance,p_content_hash,
        p_reviewed_by,p_review_note,p_memory_key,p_update_mode,p_related_memory_id);
end;
$function$;

create or replace function public.sync_reviewed_memory_request_metadata()
returns trigger language plpgsql set search_path to 'public','extensions' as $function$
declare v_existing_continuity_id uuid; v_replace_continuity boolean:=false;
begin
    if new.status in ('approved','merged') and new.memory_id is not null then
        select continuity_id into v_existing_continuity_id from public.memories where id=new.memory_id for update;
        v_replace_continuity := (v_existing_continuity_id is null or v_existing_continuity_id=new.continuity_id)
            and new.continuity_id is not null and new.continuity_data is not null
            and new.continuity_schema_version=1
            and public.validate_continuity_data(new.continuity_type,new.thread_state,new.continuity_data);
        update public.memories as memory set
            evidence_message_ids=coalesce(new.evidence_message_ids,memory.evidence_message_ids),
            source_time=coalesce(new.source_time,memory.source_time),memory_time=coalesce(new.memory_time,memory.memory_time),
            time_precision=coalesce(new.time_precision,memory.time_precision),
            source_first_message_id=coalesce((select min(value) from unnest(new.evidence_message_ids) value),memory.source_first_message_id),
            source_last_message_id=coalesce((select max(value) from unnest(new.evidence_message_ids) value),memory.source_last_message_id),
            digest_run_id=coalesce(new.digest_run_id,memory.digest_run_id),embedding=coalesce(new.embedding,memory.embedding),
            confidence=coalesce(new.confidence,memory.confidence),
            continuity_type=case when v_replace_continuity
                then coalesce(new.continuity_type,memory.continuity_type) else memory.continuity_type end,
            continuity_id=case when v_replace_continuity
                then coalesce(new.continuity_id,memory.continuity_id) else memory.continuity_id end,
            continuity_schema_version=case when v_replace_continuity
                then coalesce(new.continuity_schema_version,memory.continuity_schema_version) else memory.continuity_schema_version end,
            continuity_data=case when v_replace_continuity
                then new.continuity_data else memory.continuity_data end,
            subject=case when v_replace_continuity
                then coalesce(new.subject,memory.subject) else memory.subject end,
            source_type=case when v_replace_continuity
                then coalesce(new.source_type,memory.source_type) else memory.source_type end,
            thread_state=case when v_replace_continuity
                then coalesce(new.thread_state,memory.thread_state) else memory.thread_state end,
            continuity_value=case when v_replace_continuity
                then coalesce(new.continuity_value,memory.continuity_value) else memory.continuity_value end,
            retention_class=case when v_replace_continuity
                then coalesce(new.retention_class,memory.retention_class) else memory.retention_class end,
            participants=case when v_replace_continuity
                then coalesce(new.participants,memory.participants) else memory.participants end,
            evidence_start_time=coalesce(new.evidence_start_time,memory.evidence_start_time),
            evidence_end_time=coalesce(new.evidence_end_time,memory.evidence_end_time),
            source=case when new.source='daily_digest' then 'daily_digest' else memory.source end
        where memory.id=new.memory_id;
        update public.memory_continuity_objects set updated_at=now() where continuity_id=new.continuity_id;
    end if; return new;
end;
$function$;
create trigger sync_reviewed_memory_request_metadata after insert or update of status,memory_id on public.memory_requests
for each row execute function public.sync_reviewed_memory_request_metadata();

create table public.memory_relations(
    id bigint generated by default as identity primary key,
    from_continuity_id uuid not null references public.memory_continuity_objects(continuity_id),
    to_continuity_id uuid not null references public.memory_continuity_objects(continuity_id),
    relation_type text not null check(relation_type in ('part_of','advances','resolves','dissolves','origin_of','evokes','supports','contradicts','governed_by')),
    description text check(description is null or char_length(description)<=600),
    confidence double precision not null default 1 check(confidence between 0 and 1),
    evidence_message_ids bigint[] not null default '{}', source text not null,
    is_active boolean not null default true,created_at timestamptz not null default now(),updated_at timestamptz not null default now(),
    constraint memory_relations_no_self_link check(from_continuity_id<>to_continuity_id)
);
alter table public.memory_relations enable row level security;

create or replace function public.validate_memory_relation()
returns trigger language plpgsql set search_path to 'public' as $function$
declare v_from text; v_to text;
begin
    select continuity_type into v_from from public.memories where continuity_id=new.from_continuity_id and verified='verified' and is_active=true;
    select continuity_type into v_to from public.memories where continuity_id=new.to_continuity_id and verified='verified' and is_active=true;
    if v_from is null or v_to is null then raise exception 'memory_relation_endpoint_not_active'; end if;
    if not ((new.relation_type='part_of' and v_from='moment' and v_to='episode')
       or (new.relation_type in ('advances','resolves','dissolves') and v_from in ('moment','episode') and v_to='thread')
       or (new.relation_type='origin_of' and v_from in ('moment','episode') and v_to='inside_joke')
       -- v1 direction: a remembered moment or episode evokes an inside joke.
       or (new.relation_type='evokes' and v_from in ('moment','episode') and v_to='inside_joke')
       or (new.relation_type='supports' and v_from in ('moment','episode') and v_to='profile')
       or (new.relation_type='contradicts' and v_from in ('moment','episode') and v_to in ('profile','thread'))
       or (new.relation_type='governed_by' and v_from='inside_joke' and v_to='interaction_rule'))
       then raise exception 'memory_relation_invalid_direction'; end if;
    new.updated_at:=now(); return new;
end;
$function$;
create trigger validate_memory_relation before insert or update on public.memory_relations
for each row execute function public.validate_memory_relation();

create function public.match_memories(query_embedding extensions.vector,match_threshold double precision default .5,match_count integer default 20)
returns table(id integer,content text,title text,tags text[],heat double precision,importance integer,layer text,created_at timestamptz,
last_recalled_at timestamptz,similarity double precision,continuity_id uuid,continuity_type text,continuity_schema_version smallint,continuity_data jsonb,
subject text,source_type text,thread_state text,continuity_value integer,retention_class text,participants text[],memory_time timestamptz,evidence_start_time timestamptz,evidence_end_time timestamptz)
language sql stable set search_path to 'public','extensions' as $function$
select m.id,m.content,m.title,m.tags,m.heat,m.importance,m.layer,m.created_at,m.last_recalled_at,1-(m.embedding<=>query_embedding),
m.continuity_id,m.continuity_type,m.continuity_schema_version,m.continuity_data,m.subject,m.source_type,m.thread_state,m.continuity_value,m.retention_class,
m.participants,m.memory_time,m.evidence_start_time,m.evidence_end_time from public.memories m where m.is_active=true and m.verified='verified' and m.embedding is not null
and 1-(m.embedding<=>query_embedding)>least(greatest(coalesce(match_threshold,.5),0),1) order by m.embedding<=>query_embedding limit least(greatest(coalesce(match_count,20),1),50);
$function$;

create function public.search_memories_by_keywords(search_keywords text[],result_limit integer default 20)
returns table(id integer,content text,title text,tags text[],heat double precision,importance integer,layer text,created_at timestamptz,last_recalled_at timestamptz,
continuity_id uuid,continuity_type text,continuity_schema_version smallint,continuity_data jsonb,subject text,source_type text,thread_state text,
continuity_value integer,retention_class text,participants text[],memory_time timestamptz,evidence_start_time timestamptz,evidence_end_time timestamptz)
language sql stable set search_path to 'public' as $function$
with k as(select distinct left(btrim(keyword),64) keyword from unnest(coalesce(search_keywords,'{}')) with ordinality x(keyword,n) where n<=5 and char_length(btrim(keyword)) between 1 and 64)
select m.id,m.content,m.title,m.tags,m.heat,m.importance,m.layer,m.created_at,m.last_recalled_at,m.continuity_id,m.continuity_type,
m.continuity_schema_version,m.continuity_data,m.subject,m.source_type,m.thread_state,m.continuity_value,m.retention_class,m.participants,m.memory_time,
m.evidence_start_time,m.evidence_end_time from public.memories m where m.is_active=true and m.verified='verified' and exists(select 1 from k where
position(lower(k.keyword) in lower(coalesce(m.content,'')))>0 or position(lower(k.keyword) in lower(coalesce(m.title,'')))>0 or exists(select 1 from unnest(coalesce(m.tags,'{}')) t where position(lower(k.keyword) in lower(t))>0))
order by m.created_at desc limit least(greatest(coalesce(result_limit,20),1),50);
$function$;

revoke all on table public.memory_continuity_objects,public.memory_relations from anon,authenticated;
grant select,insert,update on table public.memory_continuity_objects,public.memory_relations to service_role;
grant usage,select on sequence public.memory_relations_id_seq to service_role;
revoke all on function public.create_memory_request_v3(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,jsonb,text,text,integer,text,text[]) from public,anon,authenticated;
grant execute on function public.create_memory_request_v3(text,text,bigint,text,text,text[],integer,text,text,text,integer,text,text,text,text,smallint,jsonb,jsonb,text,text,integer,text,text[]) to service_role;
revoke all on function public.review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) from public,anon,authenticated;
grant execute on function public.review_memory_request_v5(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) to service_role;
revoke execute on function public.review_memory_request_v2(bigint,text,text,text,text[],integer,text,text,text,text,text) from service_role;
revoke execute on function public.review_memory_request_v3(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) from service_role;
revoke execute on function public.review_memory_request_v4(bigint,text,text,text,text[],integer,text,text,text,text,text,integer) from service_role;
revoke all on function public.allocate_memory_continuity_id(text,text,text),public.store_continuity_candidate(public.memory_digest_runs,jsonb) from public,anon,authenticated;
revoke all on function public.commit_memory_digest_run(bigint,jsonb),public.commit_memory_continuity_run(bigint,jsonb),public.match_memories(extensions.vector,double precision,integer),public.search_memories_by_keywords(text[],integer) from public,anon,authenticated;
grant execute on function public.commit_memory_digest_run(bigint,jsonb),public.commit_memory_continuity_run(bigint,jsonb),public.match_memories(extensions.vector,double precision,integer),public.search_memories_by_keywords(text[],integer) to service_role;

commit;
