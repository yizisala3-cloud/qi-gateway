-- Keep both memory-recall channels on the same reviewed-memory metadata.
-- public.chat_messages remains an immutable source and is not read or written.

begin;

-- PostgreSQL cannot replace a function while changing its RETURNS TABLE row
-- type. Drop only the exact existing signature, without CASCADE, then recreate
-- it with the complete metadata required by hybrid recall.
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
    memory_type text,
    continuity_type text,
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
        1 - (memory.embedding <=> query_embedding) as similarity,
        memory.memory_type,
        memory.continuity_type,
        memory.subject,
        memory.source_type,
        memory.thread_state,
        memory.continuity_value,
        memory.retention_class,
        memory.participants,
        memory.memory_time,
        memory.evidence_start_time,
        memory.evidence_end_time
    from public.memories as memory
    where memory.is_active = true
      and memory.verified = 'verified'
      and memory.embedding is not null
      and 1 - (memory.embedding <=> query_embedding)
          > least(greatest(coalesce(match_threshold, 0.5), 0.0), 1.0)
    order by memory.embedding <=> query_embedding
    limit least(greatest(coalesce(match_count, 20), 1), 50);
$function$;

revoke all on function public.match_memories(
    extensions.vector, double precision, integer
) from public, anon, authenticated;

grant execute on function public.match_memories(
    extensions.vector, double precision, integer
) to service_role;

create or replace function public.search_memories_by_keywords(
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
    memory_type text,
    continuity_type text,
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
        select distinct left(btrim(input.keyword), 64) as keyword
        from unnest(coalesce(search_keywords, '{}'::text[]))
            with ordinality as input(keyword, position)
        where input.position <= 5
          and char_length(btrim(input.keyword)) between 1 and 64
    )
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
        memory.memory_type,
        memory.continuity_type,
        memory.subject,
        memory.source_type,
        memory.thread_state,
        memory.continuity_value,
        memory.retention_class,
        memory.participants,
        memory.memory_time,
        memory.evidence_start_time,
        memory.evidence_end_time
    from public.memories as memory
    cross join lateral (
        select count(*) as keyword_matches
        from bounded_keywords as candidate
        where position(lower(candidate.keyword) in lower(coalesce(memory.content, ''))) > 0
           or position(lower(candidate.keyword) in lower(coalesce(memory.title, ''))) > 0
           or exists (
                select 1
                from unnest(coalesce(memory.tags, '{}'::text[])) as tag(value)
                where position(lower(candidate.keyword) in lower(tag.value)) > 0
           )
    ) as relevance
    where memory.is_active = true
      and memory.verified = 'verified'
      and relevance.keyword_matches > 0
    order by relevance.keyword_matches desc, memory.created_at desc
    limit least(greatest(coalesce(result_limit, 20), 1), 50);
$function$;

revoke all on function public.search_memories_by_keywords(
    text[], integer
) from public, anon, authenticated;

grant execute on function public.search_memories_by_keywords(
    text[], integer
) to service_role;

commit;
